"""
Sincronización de inventario Siesa ↔ WMS.

Dos operaciones:

1. CARGA INICIAL (cargar_inventario_siesa)
   - Descarga todas las existencias de Siesa (API_v2_Inventarios_InvFecha)
     filtrando por bodega (NB1 o lo que diga CONNEKTA_BODEGA).
   - Para cada producto con existencia > 0:
       a. Busca el producto en WMS por f120_referencia.
       b. Crea o actualiza UbicacionProducto en la ubicación SIESA-GENERAL.
       c. Registra MovimientoInventario tipo CARGA_INICIAL_SIESA (idempotente por día).
   - Corre en hilo de fondo (puede tardar minutos con 5000 productos).
   - Idempotente: correr dos veces el mismo día no duplica nada.

2. RECONCILIACIÓN (reconciliar_inventario)
   - Descarga existencias de Siesa (mismo proceso).
   - Compara con totales WMS por producto (SUM de todas las ubicaciones).
   - NO modifica nada. Solo informa diferencias.
   - Retorna lista de discrepancias ordenada por diferencia absoluta.
   - El admin decide: "aceptar Siesa" (ajuste WMS) o "hacer conteo físico".

Regla de oro: funciona en producción con 5000+ productos y 200 pedidos/día.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from app.extensions import db
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.almacen import Almacen
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

_CODIGO_UBICACION_GENERAL = 'SIESA-GENERAL'

# Estado compartido del proceso en background
_estado_carga = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
    # [M19] Marca de sync completo: se actualiza DESPUÉS del bulk-zero y el commit final.
    # Si Railway reinicia a mitad del loop, 'ultimo_sync_completo' queda en el valor anterior
    # (o None) — el próximo sync detecta que el último no terminó y lo registra en log.
    'ultimo_sync_completo': None,
}


# ─────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────

def _get_almacen(app_context=True):
    """Resuelve el almacén que corresponde a la bodega Connekta."""
    almacen = Almacen.query.filter_by(codigo=connekta.bodega, activo=True).first()
    if not almacen:
        almacen = Almacen.query.filter_by(activo=True).first()
    return almacen


def _get_o_crear_ubicacion_general(almacen_id: int) -> Ubicacion:
    """
    Devuelve la ubicación SIESA-GENERAL del almacén.
    La crea si no existe — representa el stock sin ubicación asignada todavía.
    """
    ub = Ubicacion.query.filter_by(codigo=_CODIGO_UBICACION_GENERAL,
                                    almacen_id=almacen_id).first()
    if not ub:
        ub = Ubicacion(
            codigo=_CODIGO_UBICACION_GENERAL,
            almacen_id=almacen_id,
            zona='GENERAL',
            tipo='estanteria',
            activo=True
        )
        db.session.add(ub)
        db.session.flush()
    return ub


_cache_inventario_siesa = {'data': None, 'ts': None}
_cache_inventario_multibodega = {'data': None, 'ts': None}
_descarga_multibodega_en_curso = False
_CACHE_TTL_SEGUNDOS = 3600  # 1 hora — evita re-descargar en reconciliaciones frecuentes
_REFRESH_INTERVALO = 2700   # 45 min — refresh periódico del cache multi-bodega
_refresh_timer = None


def iniciar_refresh_periodico(app):
    """Inicia el ciclo de refresh cada 30 min. Llamar una vez desde app startup."""
    global _refresh_timer

    def _ciclo():
        global _refresh_timer, _descarga_multibodega_en_curso
        if _descarga_multibodega_en_curso:
            _refresh_timer = threading.Timer(_REFRESH_INTERVALO, _ciclo)
            _refresh_timer.daemon = True
            _refresh_timer.start()
            return
        _descarga_multibodega_en_curso = True
        try:
            with app.app_context():
                _descargar_inventario_siesa_raw(forzar=True)
                logger.info('[INV-SIESA] Refresh periódico completado')
        except Exception as exc:
            logger.error('[INV-SIESA] Refresh periódico falló: %s', exc)
        finally:
            _descarga_multibodega_en_curso = False
        _refresh_timer = threading.Timer(_REFRESH_INTERVALO, _ciclo)
        _refresh_timer.daemon = True
        _refresh_timer.start()

    threading.Thread(target=_ciclo, daemon=True).start()


def precalentar_cache_multibodega(app=None):
    """Lanza descarga en background. Llamar desde app startup o primer request."""
    global _descarga_multibodega_en_curso
    if _descarga_multibodega_en_curso:
        return
    if _cache_inventario_multibodega['data'] is not None:
        return

    if app is None:
        try:
            from flask import current_app
            app = current_app._get_current_object()
        except RuntimeError:
            logger.warning('[INV-SIESA] No hay app context para pre-calentamiento')
            return

    _descarga_multibodega_en_curso = True
    _app = app

    def _worker():
        global _descarga_multibodega_en_curso
        try:
            with _app.app_context():
                _descargar_inventario_siesa_raw(forzar=True)
                logger.info('[INV-SIESA] Cache multi-bodega pre-calentado en background')
        except Exception as exc:
            logger.error('[INV-SIESA] Pre-calentamiento falló: %s', exc)
        finally:
            _descarga_multibodega_en_curso = False

    threading.Thread(target=_worker, daemon=True).start()


_BODEGAS_INVENTARIO = None  # cache de IDs de bodegas en Siesa


def _obtener_bodegas():
    """Descubre las bodegas activas en Siesa (cachea en memoria)."""
    global _BODEGAS_INVENTARIO
    if _BODEGAS_INVENTARIO:
        return _BODEGAS_INVENTARIO
    try:
        resp = connekta.get_bodegas_siesa()
        rows = resp.get('detalle', {}).get('Table', []) or []
        bodegas = [
            (r.get('f150_id') or '').strip()
            for r in rows if (r.get('f150_id') or '').strip()
        ]
        if bodegas:
            _BODEGAS_INVENTARIO = bodegas
            logger.info('[INV-SIESA] Bodegas descubiertas: %s', bodegas)
            return bodegas
    except Exception as exc:
        logger.warning('[INV-SIESA] No se pudo listar bodegas: %s — usando fallback', exc)
    return ['NB1', 'NC1', 'NS1', 'FC1', 'FN1', 'FF1', 'BC99', 'FP1', 'ND1']


def _scan_una_pasada(bodega_id: str, max_pag: int = 200):
    """Una pasada completa de paginación secuencial para una bodega."""
    api = 'API_v2_Inventarios_InvFecha'
    inventario = {}
    filas_totales = 0

    for pag in range(1, max_pag + 1):
        try:
            resp = connekta._get(api, {
                'paginacion': f'numPag={pag}|tamPag=100',
                'parametros': f"f150_id = ''{bodega_id}'' AND f400_cant_existencia_1 > 0",
            })
        except Exception:
            continue

        rows = resp.get('detalle', {}).get('Table', [])
        if not rows:
            continue
        if len(rows) == 1 and 'alerta' in (rows[0] or {}):
            continue

        filas_totales += len(rows)
        for row in rows:
            codigo = (row.get('f120_referencia') or '').strip()
            if not codigo:
                continue
            existencia = float(row.get('f400_cant_existencia_1') or 0)
            comprometido = float(row.get('f400_cant_comprometida_1') or 0)
            salida_sin_conf = float(row.get('f400_cant_salida_sin_conf_1') or 0)
            descripcion = (row.get('f120_descripcion') or row.get('f120_id_descripcion') or '').strip()
            unidad = (row.get('f120_id_unidad_inventario') or row.get('f120_id_unidad_medida_inventario') or '').strip() or 'UND'

            if codigo not in inventario:
                inventario[codigo] = {
                    'existencia': 0.0, 'comprometido': 0.0, 'salida_sin_conf': 0.0,
                    'descripcion': descripcion, 'unidad': unidad,
                    'ubicacion_aux': (row.get('f400_id_ubicacion_aux') or '').strip(),
                }
            inventario[codigo]['existencia'] += existencia
            inventario[codigo]['comprometido'] += comprometido
            inventario[codigo]['salida_sin_conf'] += salida_sin_conf

    return inventario, filas_totales


def _descargar_bodega_individual(bodega_id: str):
    """Descarga inventario de UNA bodega con múltiples pasadas + merge.

    La API de Connekta retorna datos no-determinísticos en cada llamada
    para bodegas grandes (NC1). Cada pasada captura ~70-80% de los
    productos. Con 3 pasadas y merge, la cobertura sube a ~99%.
    """
    inv_final, filas_p1 = _scan_una_pasada(bodega_id)
    count_p1 = len(inv_final)

    for pasada in range(2, 4):
        inv_extra, filas_extra = _scan_una_pasada(bodega_id)
        nuevos = 0
        for cod, datos in inv_extra.items():
            if cod not in inv_final:
                inv_final[cod] = datos
                nuevos += 1
        logger.info(
            '[INV-SIESA] %s pasada %d: +%d nuevos (total %d)',
            bodega_id, pasada, nuevos, len(inv_final),
        )
        if nuevos == 0:
            break

    if len(inv_final) > count_p1:
        logger.info(
            '[INV-SIESA] %s merge: %d→%d productos (+%d recuperados)',
            bodega_id, count_p1, len(inv_final), len(inv_final) - count_p1,
        )

    return inv_final


def _descargar_inventario_siesa_raw(forzar=False):
    """
    Descarga inventario de TODAS las bodegas con filtro f150_id individual.
    Cada bodega se descarga secuencialmente con su propio filtro para garantizar
    datos completos (la descarga sin filtro pierde filas).

    Retorna dict {bodega: {codigo: {existencia, comprometido, salida_sin_conf, ...}}}
    Cachea 1 hora en _cache_inventario_multibodega.
    """
    global _cache_inventario_multibodega
    ahora = datetime.utcnow()
    if (not forzar
            and _cache_inventario_multibodega['data'] is not None
            and _cache_inventario_multibodega['ts'] is not None
            and (ahora - _cache_inventario_multibodega['ts']).total_seconds() < _CACHE_TTL_SEGUNDOS):
        return _cache_inventario_multibodega['data']

    bodegas = _obtener_bodegas()
    inventario_global = {}

    for bod in bodegas:
        try:
            inv = _descargar_bodega_individual(bod)
            if inv:
                inventario_global[bod] = inv
                logger.info('[INV-SIESA] Bodega %s: %d productos', bod, len(inv))
        except Exception as exc:
            logger.error('[INV-SIESA] Bodega %s falló: %s', bod, exc)

    total_prods = sum(len(v) for v in inventario_global.values())
    logger.info('[INV-SIESA] Descarga completa: %d productos en %d bodegas', total_prods, len(inventario_global))

    _cache_inventario_multibodega['data'] = inventario_global
    _cache_inventario_multibodega['ts'] = datetime.utcnow()
    return inventario_global


_descargas_bodega_en_curso = set()


def obtener_stock_bodega(bodega_id: str, forzar=False):
    """
    Retorna inventario de UNA bodega desde el cache multi-bodega.
    Si el cache tiene datos para esa bodega, los sirve inmediatamente.
    Si no (cold start), lanza descarga de ESA bodega en background
    y retorna {} (el caller usará fallback WMS). La siguiente llamada
    ya tendrá datos.
    """
    data = _cache_inventario_multibodega['data']

    if data is not None and bodega_id in data:
        return data[bodega_id]

    _lanzar_descarga_bodega_background(bodega_id)
    return {}


def _lanzar_descarga_bodega_background(bodega_id: str):
    """Descarga una bodega en background y la mete en el cache."""
    if bodega_id in _descargas_bodega_en_curso:
        return
    _descargas_bodega_en_curso.add(bodega_id)
    logger.info('[INV-SIESA] Cache miss %s — descarga background iniciada', bodega_id)

    def _worker():
        try:
            from flask import current_app
            app = current_app._get_current_object()
        except RuntimeError:
            app = None

        try:
            ctx = app.app_context() if app else None
            if ctx:
                ctx.__enter__()
            inv = _descargar_bodega_individual(bodega_id)
            if inv:
                if _cache_inventario_multibodega['data'] is None:
                    _cache_inventario_multibodega['data'] = {}
                _cache_inventario_multibodega['data'][bodega_id] = inv
                logger.info('[INV-SIESA] Bodega %s on-demand OK: %d productos', bodega_id, len(inv))
            if ctx:
                ctx.__exit__(None, None, None)
        except Exception as exc:
            logger.error('[INV-SIESA] Descarga on-demand %s falló: %s', bodega_id, exc)
        finally:
            _descargas_bodega_en_curso.discard(bodega_id)

    threading.Thread(target=_worker, daemon=True).start()


def obtener_bodegas_disponibles():
    """Retorna lista de bodegas que tienen inventario en Siesa."""
    multi = _descargar_inventario_siesa_raw()
    return sorted(multi.keys())


def _descargar_inventario_siesa(forzar=False):
    """
    Retorna inventario de la bodega principal (connekta.bodega) para
    compatibilidad con carga inicial y reconciliación existentes.
    """
    global _cache_inventario_siesa
    ahora = datetime.utcnow()
    if (not forzar
            and _cache_inventario_siesa['data'] is not None
            and _cache_inventario_siesa['ts'] is not None
            and (ahora - _cache_inventario_siesa['ts']).total_seconds() < _CACHE_TTL_SEGUNDOS):
        logger.info('[INV-SIESA] Usando inventario cacheado (TTL 1h)')
        return _cache_inventario_siesa['data']

    bodega = connekta.bodega
    multi = _descargar_inventario_siesa_raw(forzar=forzar)
    inventario = multi.get(bodega, {})

    logger.info(f'[INV-SIESA] Bodega {bodega}: {len(inventario)} productos')

    _prev_count = len(_cache_inventario_siesa['data']) if _cache_inventario_siesa['data'] else 0
    if not _prev_count:
        try:
            from app.models.inventario import UbicacionProducto as _UP
            _prev_count = _UP.query.filter(_UP.cantidad > 0).count()
        except Exception as _e_prev:
            raise ValueError(
                f'No se pudo obtener baseline de inventario (cache frío + DB inaccesible): {_e_prev}'
            ) from _e_prev
    if len(inventario) < 50:
        raise ValueError(
            f'Respuesta de Siesa sospechosamente pequeña: {len(inventario)} productos '
            f'(mínimo absoluto = 50) — abortando para evitar zeroing masivo'
        )
    if _prev_count and len(inventario) < _prev_count * 0.70:
        raise ValueError(
            f'Respuesta parcial de Siesa: {len(inventario)} productos recibidos, '
            f'{_prev_count} esperados (< 70%) — abortando para evitar falsos positivos'
        )

    _cache_inventario_siesa['data'] = inventario
    _cache_inventario_siesa['ts'] = datetime.utcnow()
    return inventario


# ─────────────────────────────────────────────
# 1. CARGA INICIAL
# ─────────────────────────────────────────────

_ADVISORY_LOCK_INV_SIESA = 2002  # clave única para pg_advisory_lock


def _run_carga_inicial(app):
    """Lógica real de la carga inicial — corre en hilo de fondo."""
    global _estado_carga

    with app.app_context():
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        cargados = 0
        actualizados = 0
        sin_producto_wms = 0
        errores = 0

        # Advisory lock de PostgreSQL — protege contra carga simultánea entre workers Gunicorn
        from sqlalchemy import text as _text
        lock_adquirido = False
        try:
            lock_adquirido = db.session.execute(
                _text('SELECT pg_try_advisory_lock(:key)'), {'key': _ADVISORY_LOCK_INV_SIESA}
            ).scalar()
            if not lock_adquirido:
                logger.warning('[INV-SIESA] Otro worker ya ejecuta la carga — omitido')
                _estado_carga['en_curso'] = False
                return
        except Exception as e:
            logger.warning(f'[INV-SIESA] Advisory lock no disponible: {e} — continuando sin él')

        # [M19] Detectar sync previo incompleto (Railway reinició entre loop y bulk-zero).
        if _estado_carga.get('ultimo_inicio') and not _estado_carga.get('ultimo_sync_completo'):
            logger.warning(
                '[INV-SIESA] El sync anterior inició (%s) pero no marcó ultimo_sync_completo '
                '— posible restart a mitad de carga. El sync actual sobreescribirá cantidades.',
                _estado_carga['ultimo_inicio'],
            )
        elif (_estado_carga.get('ultimo_inicio') and _estado_carga.get('ultimo_sync_completo')
              and _estado_carga['ultimo_sync_completo'] < _estado_carga['ultimo_inicio']):
            logger.warning(
                '[INV-SIESA] ultimo_sync_completo (%s) < ultimo_inicio (%s) '
                '— sync anterior incompleto detectado.',
                _estado_carga['ultimo_sync_completo'], _estado_carga['ultimo_inicio'],
            )

        try:
            almacen = _get_almacen()
            if not almacen:
                raise ValueError('No hay almacén activo en WMS — crea uno primero')

            ub_general = _get_o_crear_ubicacion_general(almacen.id)
            db.session.commit()

            inventario_siesa = _descargar_inventario_siesa()

            # [30] Advertencia: la carga inicial sobrescribe cantidades en ubicaciones WMS manuales.
            # Si hay picking/packing activo, el stock reservado puede quedar incorrecto.
            # Revisar tareas activas antes de ejecutar en producción con operaciones en curso.
            from app.models.picking import TareaPicking as _TareaPicking
            from app.models.packing import TareaPacking as _TareaPacking2
            picks_activos = _TareaPicking.query.filter(
                _TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
            ).count()
            packs_activos = _TareaPacking2.query.filter(
                _TareaPacking2.estado.in_(['PENDIENTE', 'EN_PROCESO', 'VERIFICADO'])
            ).count()
            # Productos con operaciones activas — sus ubicaciones se excluirán del bulk zero
            _prod_ids_activos: set = set()
            if picks_activos or packs_activos:
                logger.warning(
                    f'[INV-SIESA] ATENCIÓN: carga inicial con operaciones activas — '
                    f'{picks_activos} picking(s) y {packs_activos} packing(s) en curso. '
                    f'Sus productos serán excluidos del bulk zero para proteger el stock reservado.'
                )
                # Recopilar product_ids activos para excluirlos del bulk zero
                _picks_prods = _TareaPicking.query.filter(
                    _TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
                ).with_entities(_TareaPicking.producto_id).all()
                _prod_ids_activos |= {r.producto_id for r in _picks_prods if r.producto_id}
                from app.models.packing import ItemPacking as _ItemPacking
                _pack_prods = (
                    _ItemPacking.query
                    .join(_TareaPacking2, _ItemPacking.tarea_id == _TareaPacking2.id)
                    .filter(_TareaPacking2.estado.in_(['PENDIENTE', 'EN_PROCESO', 'VERIFICADO']))
                    .with_entities(_ItemPacking.producto_id).all()
                )
                _prod_ids_activos |= {r.producto_id for r in _pack_prods if r.producto_id}
                logger.info(f'[INV-SIESA] {len(_prod_ids_activos)} productos excluidos del bulk zero por operaciones activas')

            # ── Pre-cargar los 3 mapas en memoria — elimina N+1 del loop ──────────
            # Sin esto: hasta 3 queries × N productos (≈15.000 queries en catálogo de 5k)
            # Con esto: 3 queries bulk + 1 SET query, independiente del tamaño del catálogo

            # Mapa 1: Productos por codigo_siesa y por codigo (fallback)
            _todos_prods = Producto.query.all()
            _mapa_siesa = {p.codigo_siesa: p for p in _todos_prods if p.codigo_siesa}
            _mapa_codigo = {p.codigo: p for p in _todos_prods}

            # Mapa 2: Ubicaciones del almacén por código
            _mapa_ubicaciones = {
                ub.codigo: ub
                for ub in Ubicacion.query.filter_by(almacen_id=almacen.id).all()
            }

            # Mapa 3: Registros UbicacionProducto existentes (ubicacion_id, producto_id) → reg
            # Solo los sin lote, que son los que crea/actualiza la carga inicial
            _mapa_up = {
                (r.ubicacion_id, r.producto_id): r
                for r in (UbicacionProducto.query
                          .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
                          .filter(
                              Ubicacion.almacen_id == almacen.id,
                              UbicacionProducto.lote.is_(None)
                          ).all())
            }

            # SET de idempotency keys del día — evita reprocesar lo ya cargado hoy
            ikeys_hoy = {
                row.idempotency_key
                for row in MovimientoInventario.query.filter(
                    MovimientoInventario.idempotency_key.like(f'SIESA-INI-%-{fecha_hoy}')
                ).with_entities(MovimientoInventario.idempotency_key).all()
            }

            # Mapa 4: ubicaciones con ajuste manual en últimas 12h por producto_id
            # Pre-cargado UNA VEZ aquí — evita N+1 dentro del loop de 5000 productos (P10).
            _corte_12h = datetime.utcnow() - timedelta(hours=12)
            _ajustes_recientes: dict[int, set] = {}  # producto_id → {ubicacion_id, ...}
            for row in (
                db.session.query(MovimientoInventario.producto_id, MovimientoInventario.ubicacion_id)
                .filter(
                    MovimientoInventario.tipo != 'CARGA_INICIAL_SIESA',
                    MovimientoInventario.fecha >= _corte_12h,
                    MovimientoInventario.ubicacion_id.isnot(None),
                )
                .distinct()
                .all()
            ):
                _ajustes_recientes.setdefault(row.producto_id, set()).add(row.ubicacion_id)

            logger.info(
                f'[INV-SIESA] Mapas cargados: {len(_todos_prods)} productos · '
                f'{len(_mapa_ubicaciones)} ubicaciones · {len(_mapa_up)} registros UP'
            )

            # Precalcular sets para el bulk zero diferido (se ejecuta AL FINAL del loop)
            # El zero se mueve al final para evitar que un reinicio de Railway deje
            # productos en 0 cuando solo se commiteó el primer lote y el proceso murió.
            _ubs_almacen_ids = {ub.id for ub in _mapa_ubicaciones.values()}
            _excluir_ub_ids = {uid for ubs in _ajustes_recientes.values() for uid in ubs}
            _ubs_a_zero = _ubs_almacen_ids - _excluir_ub_ids
            _prod_ids_ya_hoy: set = set()
            for _ikey in ikeys_hoy:
                try:
                    _prod_ids_ya_hoy.add(int(_ikey.split('-')[2]))
                except (IndexError, ValueError):
                    pass
            # Productos actualizados en este sync — se excluyen del bulk zero final
            _prod_ids_actualizados: set = set()

            for codigo, datos in inventario_siesa.items():
                existencia_siesa = int(round(datos['existencia']))
                if existencia_siesa <= 0:
                    continue

                _savepoint = db.session.begin_nested()
                try:
                    # Lookup O(1) en vez de query individual
                    prod = _mapa_siesa.get(codigo) or _mapa_codigo.get(codigo)

                    if not prod:
                        sin_producto_wms += 1
                        _savepoint.commit()
                        continue

                    # Lookup O(1) de ubicación — fallback a ub_general si no existe en WMS
                    codigo_ub = datos.get('ubicacion_aux') or _CODIGO_UBICACION_GENERAL
                    ub = _mapa_ubicaciones.get(codigo_ub) or ub_general

                    # Lookup O(1) del registro existente
                    reg = _mapa_up.get((ub.id, prod.id))

                    # Idempotencia: clave única por producto + día
                    ikey = f'SIESA-INI-{prod.id}-{fecha_hoy}'
                    # [45] Usar el SET pre-cargado en vez de hacer query individual
                    if ikey in ikeys_hoy:
                        _savepoint.commit()
                        continue  # Ya se cargó hoy

                    # El zero de otras ubicaciones se hizo en bulk antes del loop (M5)
                    saldo_antes = reg.cantidad if reg else 0

                    if reg:
                        reg.cantidad = existencia_siesa
                        reg.row_version += 1
                        actualizados += 1
                    else:
                        reg = UbicacionProducto(
                            ubicacion_id=ub.id,
                            producto_id=prod.id,
                            cantidad=existencia_siesa,
                            fecha_ingreso=datetime.utcnow()
                        )
                        db.session.add(reg)
                        db.session.flush()
                        # Registrar en el mapa para que futuras iteraciones encuentren
                        # este registro sin ir a la DB (el producto puede aparecer
                        # dos veces en Siesa con distintos códigos)
                        _mapa_up[(ub.id, prod.id)] = reg
                        cargados += 1

                    movimiento = MovimientoInventario(
                        producto_id=prod.id,
                        ubicacion_id=ub.id,
                        almacen_id=almacen.id,
                        tipo='CARGA_INICIAL_SIESA',
                        cantidad=existencia_siesa,
                        saldo_antes=saldo_antes,
                        saldo_despues=existencia_siesa,
                        motivo=f'Carga inicial desde Siesa {fecha_hoy} · bodega {connekta.bodega}',
                        numero_documento='CARGA-SIESA',
                        idempotency_key=ikey
                    )
                    db.session.add(movimiento)
                    _savepoint.commit()
                    # Solo añadir al set después del savepoint exitoso —
                    # evita marcar como procesado un producto que falló y fue revertido
                    ikeys_hoy.add(ikey)
                    _prod_ids_actualizados.add(prod.id)

                    # Commit cada 200 productos para no acumular transacciones enormes
                    if (cargados + actualizados) % 200 == 0:
                        db.session.commit()
                        logger.info(f'[INV-SIESA] Commit parcial: {cargados} cargados · {actualizados} actualizados')

                except Exception as e:
                    logger.warning(f'[INV-SIESA] Error en producto {codigo}: {e}')
                    _savepoint.rollback()  # solo revierte este producto, no los anteriores
                    errores += 1

            # [M5] Bulk zero DIFERIDO — se ejecuta DESPUÉS del loop, no antes.
            # Antes estaba al inicio junto al primer lote: si Railway reiniciaba después del
            # commit 200 pero antes del commit 400, los productos 201-5000 quedaban en 0.
            # Ahora solo zeroeamos productos que Siesa NO reportó en este sync — los que
            # ya se procesaron retienen su cantidad real.
            if _ubs_a_zero:
                _excl_prods = _prod_ids_ya_hoy | _prod_ids_activos | _prod_ids_actualizados
                _q_zero = UbicacionProducto.query.filter(
                    UbicacionProducto.ubicacion_id.in_(_ubs_a_zero),
                    UbicacionProducto.lote.is_(None),
                )
                if _excl_prods:
                    _q_zero = _q_zero.filter(
                        ~UbicacionProducto.producto_id.in_(_excl_prods)
                    )
                _q_zero.update({'cantidad': 0}, synchronize_session=False)
                logger.info(
                    f'[INV-SIESA] Bulk zero diferido OK: {len(_prod_ids_actualizados)} productos '
                    f'actualizados, {len(_excl_prods)} excluidos del zero'
                )

            db.session.commit()
            # [M19] Marcar sync como completado — si Railway mata el proceso antes de
            # llegar aquí, 'ultimo_sync_completo' queda en el valor previo y el siguiente
            # sync puede detectar el gap con 'ultimo_inicio'.
            _estado_carga['ultimo_sync_completo'] = datetime.utcnow()

        except Exception as e:
            # FM_RAILWAY_RESTART: si el proceso se mató a mitad del loop de páginas,
            # el bulk-zero nunca se ejecutó → productos de páginas no procesadas
            # conservan cantidad WMS potencialmente obsoleta. El siguiente sync
            # los actualizará, pero hay un gap hasta entonces.
            logger.error(f'[INV-SIESA] Error en carga inicial: {e}', exc_info=True)
            db.session.rollback()
            _estado_carga['ultimo_error'] = str(e)
            _estado_carga['en_curso'] = False
            try:
                from app.services.alertas_service import enviar_email, _config_resend
                if _config_resend():
                    enviar_email(
                        asunto='[WMS ALERTA] Sync inventario Siesa falló — bulk-zero puede estar incompleto',
                        cuerpo_texto=(
                            f'El sync de inventario Siesa falló con error:\n{e}\n\n'
                            'Si el fallo ocurrió a mitad del loop de páginas, el bulk-zero '
                            '(zeroing de productos no reportados) puede no haberse ejecutado. '
                            'Los productos de páginas no procesadas conservan cantidades WMS posiblemente obsoletas. '
                            'Disparar sync manual: POST /api/inventario-siesa/cargar'
                        ),
                        cuerpo_html=None,
                    )
            except Exception:
                pass
            return
        finally:
            if lock_adquirido:
                try:
                    db.session.execute(
                        _text('SELECT pg_advisory_unlock(:key)'), {'key': _ADVISORY_LOCK_INV_SIESA}
                    )
                    db.session.commit()
                except Exception as _e:
                    logger.error('[INV-SIESA] Error liberando advisory lock — podría quedar bloqueado: %s', _e)

        resultado = {
            'timestamp': datetime.utcnow().isoformat(),
            'cargados': cargados,
            'actualizados': actualizados,
            'sin_producto_wms': sin_producto_wms,
            'errores': errores,
            'total_siesa': len(inventario_siesa)
        }
        logger.info(f'[INV-SIESA] Carga inicial completada: {resultado}')
        _estado_carga['ultimo_resultado'] = resultado
        _estado_carga['ultimo_error'] = None
        _estado_carga['en_curso'] = False


def iniciar_carga_inventario(app, forzar: bool = False):
    """Arranca la carga inicial en background. Retorna estado inmediatamente."""
    global _estado_carga

    if _estado_carga['en_curso']:
        return {'en_curso': True, 'mensaje': 'Carga ya en proceso — espera que termine'}

    if connekta.modo_simulacion:
        return {'simulado': True, 'mensaje': 'Modo simulación — conecta credenciales Siesa'}

    # Guard: no sobrescribir stock si hay operaciones activas (a menos que se fuerce)
    if not forzar:
        with app.app_context():
            from app.models.picking import TareaPicking as _TP
            from app.models.packing import TareaPacking as _TP2
            picks_activos = _TP.query.filter(_TP.estado.in_(['PENDIENTE', 'EN_PROCESO'])).count()
            packs_activos = _TP2.query.filter(_TP2.estado.in_(['PENDIENTE', 'EN_PROCESO', 'VERIFICADO'])).count()
            if picks_activos or packs_activos:
                return {
                    'abortado': True,
                    'mensaje': (
                        f'Carga abortada: hay {picks_activos} picking(s) y {packs_activos} packing(s) activos. '
                        f'La carga sobreescribiría el stock reservado. '
                        f'Usa ?forzar=true solo si estás seguro.'
                    ),
                    'picks_activos': picks_activos,
                    'packs_activos': packs_activos,
                }

    _estado_carga['en_curso'] = True
    _estado_carga['ultimo_inicio'] = datetime.now(timezone.utc)

    hilo = threading.Thread(target=_run_carga_inicial, args=(app,), daemon=True)
    hilo.start()

    return {'iniciado': True, 'mensaje': 'Carga de inventario iniciada — refresca en ~60 seg'}


def estado_carga_inventario():
    return {
        'en_curso': _estado_carga['en_curso'],
        'ultimo_inicio': _estado_carga['ultimo_inicio'].isoformat() if _estado_carga['ultimo_inicio'] else None,
        'ultimo_resultado': _estado_carga['ultimo_resultado'],
        'ultimo_error': _estado_carga['ultimo_error'],
    }


# ─────────────────────────────────────────────
# 2. RECONCILIACIÓN (background — puede tardar 2+ min)
# ─────────────────────────────────────────────

_estado_reconciliacion = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}


def _run_reconciliacion(app):
    """Lógica real de reconciliación — corre en hilo de fondo."""
    global _estado_reconciliacion

    with app.app_context():
        # Advisory lock — protege contra reconciliaciones paralelas entre workers Gunicorn.
        # Sin esto, dos workers pueden calcular la misma diferencia y enviar ajustes duplicados a Siesa.
        from sqlalchemy import text as _text_rec
        try:
            _lock_rec = db.session.execute(
                _text_rec('SELECT pg_try_advisory_lock(:key)'), {'key': 1003}
            ).scalar()
        except Exception as _lock_err:
            logger.error(f'[RECONCILIACION] Fallo al adquirir advisory lock: {_lock_err}')
            _estado_reconciliacion['en_curso'] = False
            return
        if not _lock_rec:
            logger.warning('[RECONCILIACION] Otro worker ya ejecuta — omitido')
            _estado_reconciliacion['en_curso'] = False
            return
        try:
            # Stock WMS: una sola query bulk (no N+1)
            stock_wms_rows = (
                db.session.query(
                    UbicacionProducto.producto_id,
                    func.sum(UbicacionProducto.cantidad).label('total')
                )
                .group_by(UbicacionProducto.producto_id)
                .all()
            )
            stock_wms = {row.producto_id: int(row.total) for row in stock_wms_rows}
            productos_ids = set(stock_wms.keys())

            # Guard: si el WMS no tiene ningún producto mapeado, la carga inicial
            # no se ha ejecutado — la reconciliación no tiene sentido y generaría
            # miles de devoluciones falsas (todo Siesa aparecería como SIESA_MAYOR).
            if not stock_wms:
                _estado_reconciliacion['ultimo_resultado'] = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'abortado': True,
                    'motivo': 'WMS sin stock mapeado — ejecuta la Carga Inicial primero',
                    'total_discrepancias': 0,
                    'discrepancias': [],
                }
                _estado_reconciliacion['en_curso'] = False
                logger.warning('[RECONCILIACION] Abortada: ubicacion_productos vacía — ejecuta carga inicial')
                return

            # Liberar la conexión DB antes del HTTP download (puede tardar 2+ min).
            # Sin este commit, la sesión retiene la conexión del pool durante toda la descarga
            # bloqueando requests concurrentes en un pool pequeño (Railway: 5-10 conexiones).
            db.session.commit()

            # forzar=True: descarga datos frescos de Siesa ignorando el cache de la carga inicial.
            # Sin esto, si la carga inicial corrió hace <1h, la reconciliación compararía
            # el WMS (ya actualizado) contra los mismos datos Siesa → falsos negativos Y
            # si el WMS tiene picks intermedios → TareaDevolucion falsas.
            inventario_siesa = _descargar_inventario_siesa(forzar=True)

            codigos_siesa = list(inventario_siesa.keys())
            prods_siesa = (
                Producto.query
                .filter(
                    db.or_(
                        Producto.codigo_siesa.in_(codigos_siesa),
                        Producto.codigo.in_(codigos_siesa)
                    )
                )
                .all()
            )
            mapa_codigo = {}
            for p in prods_siesa:
                if p.codigo_siesa:
                    mapa_codigo[p.codigo_siesa] = p
                mapa_codigo[p.codigo] = p

            discrepancias = []

            for codigo, datos in inventario_siesa.items():
                existencia_siesa = int(round(datos['existencia']))
                prod = mapa_codigo.get(codigo)
                if not prod:
                    continue
                total_wms = stock_wms.get(prod.id, 0)
                diferencia = total_wms - existencia_siesa
                if diferencia != 0:
                    discrepancias.append({
                        'producto_id': prod.id,
                        'codigo': prod.codigo,
                        'nombre': prod.nombre,
                        'stock_wms': total_wms,
                        'stock_siesa': existencia_siesa,
                        'diferencia': diferencia,
                        'diferencia_abs': abs(diferencia),
                        'estado': 'WMS_MAYOR' if diferencia > 0 else 'SIESA_MAYOR'
                    })
                productos_ids.discard(prod.id)

            # [53] Pre-cargar productos SOLO_WMS en dict antes del loop (evita N+1)
            solo_wms_ids = [pid for pid in productos_ids if stock_wms.get(pid, 0) > 0]
            solo_wms_prods = {
                p.id: p
                for p in Producto.query.filter(Producto.id.in_(solo_wms_ids)).all()
            } if solo_wms_ids else {}

            for prod_id in productos_ids:
                total_wms = stock_wms.get(prod_id, 0)
                if total_wms == 0:
                    continue
                prod = solo_wms_prods.get(prod_id)
                if not prod:
                    continue
                discrepancias.append({
                    'producto_id': prod_id,
                    'codigo': prod.codigo,
                    'nombre': prod.nombre,
                    'stock_wms': total_wms,
                    'stock_siesa': 0,
                    'diferencia': total_wms,
                    'diferencia_abs': total_wms,
                    'estado': 'SOLO_WMS'
                })

            discrepancias.sort(key=lambda x: x['diferencia_abs'], reverse=True)

            ts = datetime.utcnow().isoformat()

            # Disparar creación automática de tareas de logística inversa
            # Solo si WMS tiene cobertura suficiente: >= 20% de los productos de Siesa.
            # Si la cobertura es baja, la reconciliación es informativa únicamente —
            # no creamos devoluciones porque casi todo aparecería como SIESA_MAYOR.
            productos_wms_con_stock = len(stock_wms)
            productos_siesa = len(inventario_siesa)
            cobertura_pct = (productos_wms_con_stock / productos_siesa * 100) if productos_siesa else 0

            try:
                from app.services.devolucion_service import crear_tareas_desde_discrepancias
                almacen = _get_almacen()
                if almacen and cobertura_pct >= 20:
                    resumen_dev = crear_tareas_desde_discrepancias(discrepancias, almacen.id, ts)
                    logger.info(f'[RECONCILIACION] Tareas devolución: {resumen_dev}')
                elif almacen:
                    logger.warning(
                        f'[RECONCILIACION] Cobertura WMS={cobertura_pct:.1f}% (<20%) — '
                        f'devoluciones automáticas desactivadas para evitar falsos positivos'
                    )
            except Exception as e_dev:
                logger.exception('[RECONCILIACION] Error creando tareas devolución — discrepancias sin procesar en esta ejecución')
                try:
                    from app.services.alertas_service import _enviar_email_con_dlq
                    _enviar_email_con_dlq(
                        asunto='[WMS ALERTA] Reconciliación — tareas devolución fallaron',
                        cuerpo_texto=(
                            f'Error al crear tareas de logística inversa:\n{e_dev}\n\n'
                            'Las discrepancias de este ciclo no generaron TareaDevolucion. '
                            'Se reintentará en el próximo ciclo (~5 min).'
                        ),
                        cuerpo_html=None,
                        tipo_alerta='reconciliacion_dev_fallo',
                    )
                except Exception as _e_alert:
                    logger.error('[RECONCILIACION] Email de alerta también falló: %s', _e_alert)

            _estado_reconciliacion['ultimo_resultado'] = {
                'timestamp': ts,
                'total_productos_siesa': productos_siesa,
                'total_productos_wms': productos_wms_con_stock,
                'cobertura_pct': round(cobertura_pct, 1),
                'devoluciones_activas': cobertura_pct >= 20,
                'total_discrepancias': len(discrepancias),
                'discrepancias': discrepancias[:100]
            }
            _estado_reconciliacion['ultimo_error'] = None

        except Exception as e:
            logger.error('[RECONCILIACION] Error fatal — discrepancias SIESA_MAYOR sin procesar', exc_info=True)
            db.session.rollback()
            _estado_reconciliacion['ultimo_error'] = str(e)
            _estado_reconciliacion['ultimo_resultado'] = None
            try:
                from app.services.alertas_service import enviar_email, _config_resend
                if _config_resend():
                    enviar_email(
                        asunto='[WMS ALERTA] Reconciliación Siesa falló — discrepancias sin procesar',
                        cuerpo_texto=(
                            f'La reconciliación automática de inventario falló con error:\n{e}\n\n'
                            'Las discrepancias SIESA_MAYOR de este ciclo no generaron TareaDevolucion. '
                            'Se reintentará en el próximo ciclo (~5 min). '
                            'Si el error persiste, verificar conectividad con Siesa/Connekta.'
                        ),
                        cuerpo_html=None,
                    )
            except Exception as _e_alert:
                logger.error('[RECONCILIACION] Email de alerta también falló: %s', _e_alert)

        finally:
            _estado_reconciliacion['en_curso'] = False
            if _lock_rec:
                try:
                    db.session.execute(_text_rec('SELECT pg_advisory_unlock(:key)'), {'key': 1003})
                    db.session.commit()
                except Exception as _e_unlock:
                    logger.error('[RECONCILIACION] Error liberando advisory lock 1003: %s', _e_unlock)


def iniciar_reconciliacion(app):
    """Arranca la reconciliación en background. Retorna estado inmediatamente."""
    global _estado_reconciliacion

    if connekta.modo_simulacion:
        return {'simulado': True}

    if _estado_reconciliacion['en_curso']:
        return {'en_curso': True, 'mensaje': 'Reconciliación ya en proceso — espera que termine'}

    _estado_reconciliacion['en_curso'] = True
    _estado_reconciliacion['ultimo_inicio'] = datetime.now(timezone.utc)
    _estado_reconciliacion['ultimo_resultado'] = None
    _estado_reconciliacion['ultimo_error'] = None

    hilo = threading.Thread(target=_run_reconciliacion, args=(app,), daemon=True)
    hilo.start()

    return {'iniciado': True, 'mensaje': 'Reconciliación iniciada — refresca en ~2 min'}


def estado_reconciliacion():
    return {
        'en_curso': _estado_reconciliacion['en_curso'],
        'ultimo_inicio': _estado_reconciliacion['ultimo_inicio'].isoformat() if _estado_reconciliacion['ultimo_inicio'] else None,
        'ultimo_resultado': _estado_reconciliacion['ultimo_resultado'],
        'ultimo_error': _estado_reconciliacion['ultimo_error'],
    }


# ─────────────────────────────────────────────
# 3. SETUP INICIAL UNIFICADO (catálogo → stock)
# ─────────────────────────────────────────────

_estado_setup = {
    'en_curso': False,
    'fase': None,   # 'catalogo' | 'stock' | 'completado' | 'error'
    'ultimo_inicio': None,
    'ultimo_error': None,
}


def _run_setup_inicial(app):
    """Ejecuta sync de catálogo y luego carga de stock en secuencia, en un solo hilo."""
    global _estado_setup
    from app.services.siesa_sync_service import _run_sync

    try:
        _estado_setup['fase'] = 'catalogo'
        _run_sync(app)

        _estado_setup['fase'] = 'stock'
        # Marcar _estado_carga como en curso para que iniciar_carga_inicial()
        # concurrente no lance un segundo hilo mientras el setup ejecuta la carga.
        _estado_carga['en_curso'] = True
        _estado_carga['ultimo_inicio'] = datetime.now(timezone.utc)
        try:
            _run_carga_inicial(app)
        finally:
            _estado_carga['en_curso'] = False

        _estado_setup['fase'] = 'completado'
        _estado_setup['ultimo_error'] = None
    except Exception as e:
        logger.error(f'[SETUP] Error en setup inicial: {e}')
        _estado_setup['ultimo_error'] = str(e)
        _estado_setup['fase'] = 'error'
    finally:
        _estado_setup['en_curso'] = False


def iniciar_setup_inicial(app):
    """Arranca el setup inicial unificado en background. Retorna inmediatamente."""
    global _estado_setup

    if _estado_setup['en_curso']:
        return {'en_curso': True, 'fase': _estado_setup['fase'],
                'mensaje': 'Setup ya en proceso — espera que termine'}

    if connekta.modo_simulacion:
        return {'simulado': True, 'mensaje': 'Modo simulación — conecta credenciales Siesa'}

    _estado_setup['en_curso'] = True
    _estado_setup['fase'] = 'iniciando'
    _estado_setup['ultimo_inicio'] = datetime.now(timezone.utc)
    _estado_setup['ultimo_error'] = None

    hilo = threading.Thread(target=_run_setup_inicial, args=(app,), daemon=True)
    hilo.start()

    return {'iniciado': True, 'mensaje': 'Setup iniciado — fase 1/2: sincronizando catálogo'}


def estado_setup_inicial():
    from app.services.siesa_sync_service import estado_sync
    return {
        'en_curso': _estado_setup['en_curso'],
        'fase': _estado_setup['fase'],
        'ultimo_inicio': _estado_setup['ultimo_inicio'].isoformat() if _estado_setup['ultimo_inicio'] else None,
        'resultado_catalogo': estado_sync().get('ultimo_resultado'),
        'resultado_stock': _estado_carga['ultimo_resultado'],
        'ultimo_error': _estado_setup['ultimo_error'],
    }
