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

# Alias local — la fuente de verdad del literal vive en Ubicacion.CODIGO_GENERAL
# (compartida con layout_service.py, que también necesita reconocer este bucket).
_CODIGO_UBICACION_GENERAL = Ubicacion.CODIGO_GENERAL

# Estado compartido del proceso en background — UNA entrada por bodega.
# Antes de la Fase 1 de calibración de tiendas (2026-08-27) era un solo dict
# plano: correr la carga para dos bodegas se habría pisado entre sí (la
# segunda sobreescribe 'en_curso'/'ultimo_resultado' de la primera antes de
# que nadie los lea). `estado_carga_inventario()` sigue devolviendo el mismo
# contrato plano de siempre para NB1 (bodega=None) — no rompe al monitor ni
# al endpoint existente.
_estado_carga: dict = {}


def _estado_carga_bodega(bodega: str) -> dict:
    return _estado_carga.setdefault(bodega, {
        'en_curso': False,
        'ultimo_inicio': None,
        'ultimo_resultado': None,
        'ultimo_error': None,
        # [M19] Marca de sync completo: se actualiza DESPUÉS del bulk-zero y el commit final.
        # Si Railway reinicia a mitad del loop, 'ultimo_sync_completo' queda en el valor anterior
        # (o None) — el próximo sync detecta que el último no terminó y lo registra en log.
        'ultimo_sync_completo': None,
    })


# ─────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────

def _get_almacen(bodega_siesa_id: str = None):
    """Resuelve el almacén que corresponde a una bodega Siesa.

    Sin argumento, preserva el comportamiento histórico (bodega de
    `connekta.bodega`, con fallback a "cualquier almacén activo" — válido
    cuando solo existía un almacén en todo el WMS). Con una bodega explícita
    (Fase 1: calibración de NS1/NC1) el fallback NO aplica: devolver el
    almacén equivocado significaría escribir el stock de una tienda sobre
    otra, y eso es peor que fallar declarando que no hay almacén (Regla 0).
    """
    if bodega_siesa_id:
        return Almacen.query.filter_by(bodega_siesa_id=bodega_siesa_id, activo=True).first()

    almacen = Almacen.query.filter_by(bodega_siesa_id=connekta.bodega, activo=True).first()
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


_cache_inventario_siesa: dict = {}  # {bodega: {'data': ..., 'ts': ...}}
#: `ts` es la hora de la última descarga **que trajo datos de Siesa**, no la de
#: la última vez que se armó el diccionario. `degradado` dice si lo que hay
#: salió solo de la BD porque la API no respondió.
_cache_inventario_multibodega = {'data': None, 'ts': None, 'degradado': False}
_descarga_multibodega_en_curso = False
_CACHE_TTL_SEGUNDOS = 3600  # 1 hora — evita re-descargar en reconciliaciones frecuentes
_REFRESH_INTERVALO = 2700   # 45 min — refresh periódico del cache multi-bodega
_refresh_timer = None


_HORA_CARGA_DIARIA = 7  # 7am Colombia (UTC-5 = 12:00 UTC)
_ZONA_UTC_OFFSET = -5


def iniciar_refresh_periodico(app):
    """Inicia refresh cada 45 min + carga diaria a las 7am Colombia."""
    global _refresh_timer

    def _ejecutar_descarga():
        global _descarga_multibodega_en_curso
        if _descarga_multibodega_en_curso:
            return
        _descarga_multibodega_en_curso = True
        try:
            with app.app_context():
                _descargar_inventario_siesa_raw(forzar=True)
                logger.info('[INV-SIESA] Refresh completado')
        except Exception as exc:
            logger.error('[INV-SIESA] Refresh falló: %s', exc)
        finally:
            _descarga_multibodega_en_curso = False

    def _ciclo_refresh():
        """Refresh cada 45 min."""
        _ejecutar_descarga()
        _refresh_timer = threading.Timer(_REFRESH_INTERVALO, _ciclo_refresh)
        _refresh_timer.daemon = True
        _refresh_timer.start()

    def _programar_carga_diaria():
        """Programa la carga diaria a las 7am Colombia."""
        ahora = datetime.utcnow()
        hora_utc_objetivo = _HORA_CARGA_DIARIA - _ZONA_UTC_OFFSET
        proxima = ahora.replace(hour=hora_utc_objetivo, minute=0, second=0, microsecond=0)
        if proxima <= ahora:
            proxima += timedelta(days=1)
        segundos = (proxima - ahora).total_seconds()
        logger.info('[INV-SIESA] Carga diaria programada a las %d:00 Colombia (en %.0f min)',
                    _HORA_CARGA_DIARIA, segundos / 60)

        def _carga_y_reprogramar():
            logger.info('[INV-SIESA] === CARGA DIARIA 7AM INICIADA ===')
            _ejecutar_descarga()
            _programar_carga_diaria()

        t = threading.Timer(segundos, _carga_y_reprogramar)
        t.daemon = True
        t.start()

    _hilo = threading.Thread(target=_ciclo_refresh, daemon=True)
    _hilo.start()
    _programar_carga_diaria()
    # No usa APScheduler sino hilos, pero declara igual: el registro mira el
    # retorno para saber si esto quedó corriendo de verdad.
    return _hilo


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


# NS2 no es punto de venta: es la bodega de parqueo de licitaciones. Sin
# ella, su stock es invisible para el WMS y para la reconciliación.
_BODEGAS_PV = ['NB1', 'NC1', 'NS1', 'NS2', 'FC1', 'PC1', 'PT1', 'FF1', 'FN1', 'FP1']


def _descargar_una_pasada_custom():
    """Una pasada completa de la consulta custom. Retorna dict {bodega: {codigo: {...}}}."""
    import time as _time
    api_custom = 'papeleriamedellin_WMS_Stock_Bodega_v2'
    inventario = {}
    _errores_consecutivos = 0

    for pag in range(1, 200):
        resp = None
        for intento in range(3):
            try:
                resp = connekta._get(api_custom, {
                    'paginacion': f'numPag={pag}|tamPag=1000',
                }, url=connekta.url_get_dinamico)
                _errores_consecutivos = 0
                break
            except Exception as _e:
                if '429' in str(_e) or 'rate' in str(_e).lower():
                    wait = 10 * (intento + 1)
                    logger.warning('[INV-SIESA] CUSTOM pág %d: 429 — espera %ds', pag, wait)
                    _time.sleep(wait)
                else:
                    _errores_consecutivos += 1
                    _time.sleep(2)

        if resp is None:
            if _errores_consecutivos >= 3:
                return None
            continue

        rows = (
            resp.get('detalle', {}).get('Datos')
            or resp.get('detalle', {}).get('Table')
            or []
        )
        if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
            break

        for row in rows:
            bodega = (row.get('f150_id') or '').strip()
            codigo = (row.get('f120_referencia') or '').strip()
            if not bodega or not codigo or bodega not in _BODEGAS_PV:
                continue
            if bodega not in inventario:
                inventario[bodega] = {}
            if codigo not in inventario[bodega]:
                inventario[bodega][codigo] = {
                    'existencia': float(row.get('f400_cant_existencia_1') or 0),
                    'comprometido': float(row.get('f400_cant_comprometida_1') or 0),
                    'salida_sin_conf': float(row.get('f400_cant_salida_sin_conf_1') or 0),
                    'descripcion': '', 'unidad': 'UND',
                }

        if len(rows) < 1000:
            break

    return inventario if inventario else None


def _descargar_todas_bodegas_custom():
    """
    3 pasadas consecutivas para compensar paginación no determinística.
    Cada pasada devuelve un subconjunto diferente; al acumular se acerca al 100%.
    ~36s × 3 = ~2 min total.
    """
    acumulado = {}
    for pasada in range(1, 4):
        resultado = _descargar_una_pasada_custom()
        if resultado is None:
            logger.warning('[INV-SIESA] CUSTOM pasada %d falló', pasada)
            continue
        nuevos = 0
        for bod, productos in resultado.items():
            if bod not in acumulado:
                acumulado[bod] = {}
            for codigo, datos in productos.items():
                if codigo not in acumulado[bod]:
                    nuevos += 1
                acumulado[bod][codigo] = datos
        total = sum(len(v) for v in acumulado.values())
        logger.info('[INV-SIESA] CUSTOM pasada %d: +%d nuevos → %d total acumulado', pasada, nuevos, total)

    total = sum(len(v) for v in acumulado.values())
    bodegas = sorted(acumulado.keys())
    logger.info('[INV-SIESA] CUSTOM 3 pasadas completas: %d productos en %s', total, bodegas)
    return acumulado if acumulado else None


def _descargar_inventario_siesa_raw(forzar=False):
    """
    Descarga inventario de todas las bodegas PV.
    Primero intenta consulta custom (una sola llamada, ~36s, 100% cobertura).
    Si falla, lee de BD como fallback.
    Merge API + BD para cobertura máxima.

    Retorna dict {bodega: {codigo: {existencia, comprometido, salida_sin_conf, ...}}}
    """
    global _cache_inventario_multibodega
    ahora = datetime.utcnow()
    if (not forzar
            and _cache_inventario_multibodega['data'] is not None
            and _cache_inventario_multibodega['ts'] is not None
            and (ahora - _cache_inventario_multibodega['ts']).total_seconds() < _CACHE_TTL_SEGUNDOS):
        return _cache_inventario_multibodega['data']



    api_data = _descargar_todas_bodegas_custom()

    _degradado = api_data is None
    if _degradado:
        # La API no respondió: lo que sigue sale de `stock_siesa`, que puede
        # tener horas o días. Se devuelve igual —quedarse sin inventario
        # rompería más de lo que arregla— pero **no se sella como fresco**.
        logger.error(
            '[INV-SIESA] Custom query falló — se responde con el stock '
            'persistido en BD, que NO se acaba de verificar contra Siesa.')
        api_data = {}

    inventario_global = {}
    for bod in _BODEGAS_PV:
        inv_bd, _ = _leer_stock_de_bd(bod)
        inv_api = api_data.get(bod, {})
        if inv_bd and inv_api:
            merged = dict(inv_bd)
            merged.update(inv_api)
            inventario_global[bod] = merged
            if len(merged) > len(inv_api):
                logger.info('[INV-SIESA] %s: merge BD(%d) + API(%d) = %d',
                            bod, len(inv_bd), len(inv_api), len(merged))
        elif inv_api:
            inventario_global[bod] = inv_api
        elif inv_bd:
            inventario_global[bod] = inv_bd

    total = sum(len(v) for v in inventario_global.values())
    logger.info('[INV-SIESA] Descarga completa: %d productos en %s', total, sorted(inventario_global.keys()))

    _cache_inventario_multibodega['data'] = inventario_global
    _cache_inventario_multibodega['degradado'] = _degradado
    if not _degradado:
        _cache_inventario_multibodega['ts'] = datetime.utcnow()
    else:
        # NO se refresca la marca de tiempo.
        #
        # Ponerle `utcnow()` a un inventario que salió de la BD porque Siesa no
        # respondió es un sello fresco sobre un dato viejo: el TTL de una hora
        # lo daba por vigente y se usaba para **proponer traslados**. Nadie
        # podía distinguir «Siesa dice esto» de «esto es lo último que supimos».
        #
        # Dejando la marca vieja, el TTL vence y el siguiente llamador
        # reintenta. El circuit breaker acota el costo de reintentar contra un
        # Siesa caído.
        logger.warning(
            '[INV-SIESA] Cache marcado DEGRADADO — la marca de tiempo sigue '
            'siendo la de la última descarga real (%s)',
            _cache_inventario_multibodega['ts'])

    _guardar_stock_en_bd(inventario_global, degradado=_degradado)

    return inventario_global


def _guardar_stock_en_bd(inventario_global: dict, degradado: bool = False):
    """Persiste el inventario descargado en la tabla stock_siesa (upsert).

    Si `degradado` es True, `inventario_global` no trajo nada nuevo de
    Siesa — es la misma BD leída de vuelta (ver `_descargar_inventario_siesa_raw`).
    Re-escribirlo pondría `updated_at = utcnow()` sobre un dato que sigue
    siendo tan viejo como antes de esta corrida: un sello fresco sobre un
    dato viejo, el mismo error que la Regla 0 ya obligó a evitar en el `ts`
    del cache en memoria. Acá era el mismo bug, un nivel más abajo."""
    if degradado:
        logger.info('[INV-SIESA] BD: guardado omitido (cache degradado — nada nuevo que persistir)')
        return
    from app.models.stock_siesa import StockSiesa
    try:
        for bod, productos in inventario_global.items():
            for codigo, datos in productos.items():
                reg = StockSiesa.query.filter_by(bodega=bod, codigo_siesa=codigo).first()
                if reg:
                    reg.existencia = datos['existencia']
                    reg.comprometido = datos['comprometido']
                    reg.salida_sin_conf = datos['salida_sin_conf']
                    reg.descripcion = datos.get('descripcion', '')
                    reg.unidad_medida = datos.get('unidad', 'UND')
                    reg.updated_at = datetime.utcnow()
                else:
                    db.session.add(StockSiesa(
                        bodega=bod,
                        codigo_siesa=codigo,
                        existencia=datos['existencia'],
                        comprometido=datos['comprometido'],
                        salida_sin_conf=datos['salida_sin_conf'],
                        descripcion=datos.get('descripcion', ''),
                        unidad_medida=datos.get('unidad', 'UND'),
                    ))
            db.session.commit()
            logger.info('[INV-SIESA] BD: %s guardado (%d productos)', bod, len(productos))
            import time as _ti
            _ti.sleep(1)  # throttle: respiro entre bodegas (cada una escribe miles de rows)
    except Exception as exc:
        db.session.rollback()
        logger.error('[INV-SIESA] Error guardando en BD: %s', exc)


def _leer_stock_de_bd(bodega_id: str):
    """Lee inventario de una bodega desde PostgreSQL (sobrevive deploys).

    Retorna (inventario, actualizado_en) — actualizado_en es el `updated_at`
    más viejo entre las filas de la bodega, no el más nuevo: el snapshot
    completo no es más fresco que su parte más antigua (Regla 0, fallar
    hacia el lado conservador)."""
    from app.models.stock_siesa import StockSiesa
    try:
        rows = StockSiesa.query.filter_by(bodega=bodega_id).all()
        if not rows:
            return {}, None
        inv = {}
        actualizado_en = None
        for r in rows:
            inv[r.codigo_siesa] = {
                'existencia': r.existencia or 0,
                'comprometido': r.comprometido or 0,
                'salida_sin_conf': r.salida_sin_conf or 0,
                'descripcion': r.descripcion or '',
                'unidad': r.unidad_medida or 'UND',
            }
            if r.updated_at and (actualizado_en is None or r.updated_at < actualizado_en):
                actualizado_en = r.updated_at
        logger.info('[INV-SIESA] BD: %s leído (%d productos)', bodega_id, len(inv))
        return inv, actualizado_en
    except Exception as exc:
        logger.error('[INV-SIESA] Error leyendo BD para %s: %s', bodega_id, exc)
        return {}, None


def obtener_stock_bodega(bodega_id: str, forzar=False):
    """
    1. Cache en memoria → instantáneo
    2. Si cache vacío → lee de PostgreSQL (sobrevive deploys) → instantáneo
    3. Si BD vacía → lanza precalentamiento background → retorna {}

    Retorna (inventario, meta). `meta` trae `fuente` y `actualizado_en`
    (ISO 8601 o None) para que el consumidor (pantalla de Pedir) pueda
    mostrar qué tan viejo es el número antes de que alguien arme una
    solicitud sobre un dato que ya no es el de Siesa en vivo.
    """
    data = _cache_inventario_multibodega['data']
    if data is not None and bodega_id in data:
        ts = _cache_inventario_multibodega['ts']
        meta = {
            'fuente': 'siesa',
            'actualizado_en': ts.isoformat() if ts else None,
        }
        return data[bodega_id], meta

    inv_bd, actualizado_en_bd = _leer_stock_de_bd(bodega_id)
    if inv_bd:
        if _cache_inventario_multibodega['data'] is None:
            _cache_inventario_multibodega['data'] = {}
        _cache_inventario_multibodega['data'][bodega_id] = inv_bd
        meta = {
            'fuente': 'siesa_bd_snapshot',
            'actualizado_en': actualizado_en_bd.isoformat() if actualizado_en_bd else None,
        }
        return inv_bd, meta

    precalentar_cache_multibodega()
    return {}, {'fuente': 'sin_dato', 'actualizado_en': None}


def obtener_bodegas_disponibles():
    """Retorna lista de bodegas que tienen inventario en Siesa."""
    multi = _descargar_inventario_siesa_raw()
    return sorted(multi.keys())


def _descargar_inventario_siesa(forzar=False, bodega: str = None, almacen_id: int = None):
    """
    Retorna inventario de una bodega para carga inicial y reconciliación.

    Sin `bodega`, preserva el comportamiento histórico (connekta.bodega,
    típicamente NB1). El cache y el baseline de "respuesta sospechosa" están
    keyed por bodega — antes de la Fase 1 (2026-08-27) eran un solo par
    (data, ts) global: cargar una segunda bodega habría comparado su tamaño
    contra el baseline de la primera y podido abortar por un falso "respuesta
    parcial" (NC1 con 6.000 productos SIEMPRE se ve "parcial" al lado de un
    baseline armado con el total de NB1).

    `almacen_id`: si se pasa, el baseline de UbicacionProducto se cuenta SOLO
    en ese almacén — sin esto, cargar NS1 por primera vez compara su tamaño
    contra el conteo GLOBAL (que ya incluye miles de filas de NB1) y aborta
    con "respuesta parcial" aunque NS1 nunca haya tenido ni una fila.
    """
    global _cache_inventario_siesa
    bod = bodega or connekta.bodega
    cache = _cache_inventario_siesa.setdefault(bod, {'data': None, 'ts': None})
    ahora = datetime.utcnow()
    if (not forzar
            and cache['data'] is not None
            and cache['ts'] is not None
            and (ahora - cache['ts']).total_seconds() < _CACHE_TTL_SEGUNDOS):
        logger.info('[INV-SIESA] Usando inventario cacheado (TTL 1h) — bodega %s', bod)
        return cache['data']

    multi = _descargar_inventario_siesa_raw(forzar=forzar)
    inventario = multi.get(bod, {})

    logger.info(f'[INV-SIESA] Bodega {bod}: {len(inventario)} productos')

    _prev_count = len(cache['data']) if cache['data'] else 0
    if not _prev_count:
        try:
            from app.models.inventario import UbicacionProducto as _UP
            _q = _UP.query.filter(_UP.cantidad > 0)
            if almacen_id is not None:
                from app.models.ubicacion import Ubicacion as _Ub
                _q = _q.join(_Ub, _Ub.id == _UP.ubicacion_id).filter(_Ub.almacen_id == almacen_id)
            _prev_count = _q.count()
        except Exception as _e_prev:
            raise ValueError(
                f'No se pudo obtener baseline de inventario (cache frío + DB inaccesible): {_e_prev}'
            ) from _e_prev
    if len(inventario) < 50:
        raise ValueError(
            f'Respuesta de Siesa sospechosamente pequeña para {bod}: {len(inventario)} productos '
            f'(mínimo absoluto = 50) — abortando para evitar zeroing masivo'
        )
    if _prev_count and len(inventario) < _prev_count * 0.70:
        raise ValueError(
            f'Respuesta parcial de Siesa para {bod}: {len(inventario)} productos recibidos, '
            f'{_prev_count} esperados (< 70%) — abortando para evitar falsos positivos'
        )

    cache['data'] = inventario
    cache['ts'] = datetime.utcnow()
    return inventario


# ─────────────────────────────────────────────
# 1. CARGA INICIAL
# ─────────────────────────────────────────────

_ADVISORY_LOCK_INV_SIESA = 2002  # clave única para pg_advisory_lock


def _run_carga_inicial(app, bodega: str = None):
    """Lógica real de la carga inicial — corre en hilo de fondo.

    `bodega`: código Siesa (ej. 'NS1'). Sin argumento, preserva el
    comportamiento histórico (connekta.bodega, típicamente NB1).
    """
    global _estado_carga
    bod = bodega or connekta.bodega
    estado = _estado_carga_bodega(bod)

    with app.app_context():
        # Sufijo de la clave de idempotencia: con utcnow cambiaba a las 7 p.m.
        # y dos cargas separadas por ese minuto entraban las dos.
        from app.utils.fecha import fecha_hoy_bogota
        fecha_hoy = fecha_hoy_bogota()
        cargados = 0
        actualizados = 0
        sin_producto_wms = 0
        errores = 0

        # La corrida más importante de registrar: la carga inicial de stock va
        # UNA vez, y correrla dos veces duplica el inventario de arranque. Hasta
        # hoy la única defensa era la memoria de quien la ejecutó.
        from app.services import registro_sync_service as _reg
        _reg_id = _reg.abrir('stock')

        # Advisory lock de PostgreSQL — protege contra carga simultánea entre workers
        # Gunicorn. Es una sola clave global a propósito: además de proteger contra
        # una segunda corrida de la MISMA bodega, serializa cargas de bodegas
        # distintas entre sí — más lento si se piden varias a la vez, pero ninguna
        # pisa el `db.session` de otra a mitad de camino.
        from sqlalchemy import text as _text
        lock_adquirido = False
        try:
            lock_adquirido = db.session.execute(
                _text('SELECT pg_try_advisory_lock(:key)'), {'key': _ADVISORY_LOCK_INV_SIESA}
            ).scalar()
            if not lock_adquirido:
                logger.warning('[INV-SIESA] Otro worker ya ejecuta una carga — omitido (bodega %s)', bod)
                estado['en_curso'] = False
                return
        except Exception as e:
            logger.warning(f'[INV-SIESA] Advisory lock no disponible: {e} — continuando sin él')

        # [M19] Detectar sync previo incompleto (Railway reinició entre loop y bulk-zero).
        if estado.get('ultimo_inicio') and not estado.get('ultimo_sync_completo'):
            logger.warning(
                '[INV-SIESA] %s: el sync anterior inició (%s) pero no marcó ultimo_sync_completo '
                '— posible restart a mitad de carga. El sync actual sobreescribirá cantidades.',
                bod, estado['ultimo_inicio'],
            )
        elif (estado.get('ultimo_inicio') and estado.get('ultimo_sync_completo')
              and estado['ultimo_sync_completo'] < estado['ultimo_inicio']):
            logger.warning(
                '[INV-SIESA] %s: ultimo_sync_completo (%s) < ultimo_inicio (%s) '
                '— sync anterior incompleto detectado.',
                bod, estado['ultimo_sync_completo'], estado['ultimo_inicio'],
            )

        try:
            almacen = _get_almacen(bod)
            if not almacen:
                raise ValueError(f'No hay almacén activo en WMS para la bodega {bod}')

            ub_general = _get_o_crear_ubicacion_general(almacen.id)
            db.session.commit()

            inventario_siesa = _descargar_inventario_siesa(bodega=bod, almacen_id=almacen.id)

            # [30] Advertencia: la carga inicial sobrescribe cantidades en ubicaciones WMS
            # manuales. Si hay picking/packing activo EN ESTE ALMACÉN, el stock reservado
            # puede quedar incorrecto. Acotado por almacén — un picking en curso en NB1 no
            # tiene nada que ver con una carga a NS1, y viceversa.
            from app.models.picking import TareaPicking as _TareaPicking
            from app.models.packing import TareaPacking as _TareaPacking2
            picks_activos = (
                _TareaPicking.query
                .join(Ubicacion, Ubicacion.id == _TareaPicking.ubicacion_id)
                .filter(Ubicacion.almacen_id == almacen.id,
                        _TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO']))
                .count()
            )
            packs_activos = _TareaPacking2.query.filter(
                _TareaPacking2.almacen_id == almacen.id,
                _TareaPacking2.estado.in_(['PENDIENTE', 'EN_PROCESO', 'VERIFICADO'])
            ).count()
            # Productos con operaciones activas — sus ubicaciones se excluirán del bulk zero
            _prod_ids_activos: set = set()
            if picks_activos or packs_activos:
                logger.warning(
                    f'[INV-SIESA] ATENCIÓN: carga inicial de {bod} con operaciones activas — '
                    f'{picks_activos} picking(s) y {packs_activos} packing(s) en curso. '
                    f'Sus productos serán excluidos del bulk zero para proteger el stock reservado.'
                )
                # Recopilar product_ids activos para excluirlos del bulk zero
                _picks_prods = (
                    _TareaPicking.query
                    .join(Ubicacion, Ubicacion.id == _TareaPicking.ubicacion_id)
                    .filter(Ubicacion.almacen_id == almacen.id,
                            _TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO']))
                    .with_entities(_TareaPicking.producto_id).all()
                )
                _prod_ids_activos |= {r.producto_id for r in _picks_prods if r.producto_id}
                from app.models.packing import ItemPacking as _ItemPacking
                _pack_prods = (
                    _ItemPacking.query
                    .join(_TareaPacking2, _ItemPacking.tarea_id == _TareaPacking2.id)
                    .filter(_TareaPacking2.almacen_id == almacen.id,
                            _TareaPacking2.estado.in_(['PENDIENTE', 'EN_PROCESO', 'VERIFICADO']))
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

            # Mapa 3b: stock que YA vive en una ubicación real (no SIESA-GENERAL)
            # para cada producto de este almacén — típicamente porque Layout ya
            # lo trasladó a un hueco de picking (`_traspasar_desde_general`) o
            # porque una recepción lo mandó a Cross-Dock.
            #
            # Sin esto, el `reg.cantidad = existencia_siesa` de más abajo
            # sobrescribe SIESA-GENERAL con el número COMPLETO de Siesa sin
            # saber que una parte de esa misma mercancía ya está contada por
            # separado en un hueco real — cada corrida de Carga Inicial vuelve
            # a duplicar esa parte. Verificado en producción (2026-08-26):
            # 5 SKUs asignados a picking el 10 de agosto quedaron duplicados
            # exactos por la corrida de Carga Inicial de hoy (PAPELSL153: 400
            # unidades de más, ni una de más ni de menos que lo que había en
            # su hueco PIK-C2-C01-E02-H03).
            _stock_en_ubicaciones_reales: dict = {
                row.producto_id: int(row.total)
                for row in (
                    db.session.query(
                        UbicacionProducto.producto_id,
                        func.sum(UbicacionProducto.cantidad).label('total')
                    )
                    .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
                    .filter(
                        Ubicacion.almacen_id == almacen.id,
                        UbicacionProducto.ubicacion_id != ub_general.id,
                        UbicacionProducto.lote.is_(None),
                    )
                    .group_by(UbicacionProducto.producto_id)
                    .all()
                )
            }

            # SET de idempotency keys del día — evita reprocesar lo ya cargado hoy.
            # Filtrado por bodega: la clave incluye `bod` (ver más abajo) porque el
            # mismo producto existe en el catálogo de varias bodegas — sin el filtro,
            # cargar NB1 primero marcaría el producto como "ya cargado hoy" y NS1/NC1
            # lo saltarían sin escribir su propia fila.
            ikeys_hoy = {
                row.idempotency_key
                for row in MovimientoInventario.query.filter(
                    MovimientoInventario.idempotency_key.like(f'SIESA-INI-{bod}-%-{fecha_hoy}')
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
                    # Formato: SIESA-INI-{bod}-{prod.id}-{fecha_hoy} — el índice 3, no 2,
                    # desde que la bodega se insertó en la clave. Asume que ningún código
                    # de bodega Siesa trae un guion (cierto para las 10 reales, ver
                    # BODEGA_CO en CLAUDE.md).
                    _prod_ids_ya_hoy.add(int(_ikey.split('-')[3]))
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

                    # Idempotencia: clave única por bodega + producto + día
                    ikey = f'SIESA-INI-{bod}-{prod.id}-{fecha_hoy}'
                    # [45] Usar el SET pre-cargado en vez de hacer query individual
                    if ikey in ikeys_hoy:
                        _savepoint.commit()
                        continue  # Ya se cargó hoy

                    # Si el destino es SIESA-GENERAL, restar lo que ya está en una
                    # ubicación real (hueco de picking, Cross-Dock) — ese stock es
                    # la MISMA mercancía que reporta Siesa, ya localizada. Sin esto,
                    # cada corrida vuelve a poner el número completo en el bucket
                    # genérico ENCIMA de lo que ya se trasladó, duplicando esa
                    # porción para siempre.
                    if ub.id == ub_general.id:
                        ya_en_reales = _stock_en_ubicaciones_reales.get(prod.id, 0)
                        cantidad_destino = max(0, existencia_siesa - ya_en_reales)
                    else:
                        ya_en_reales = 0
                        cantidad_destino = existencia_siesa

                    # El zero de otras ubicaciones se hizo en bulk antes del loop (M5)
                    saldo_antes = reg.cantidad if reg else 0

                    if reg:
                        reg.cantidad = cantidad_destino
                        reg.row_version += 1
                        actualizados += 1
                    else:
                        reg = UbicacionProducto(
                            ubicacion_id=ub.id,
                            producto_id=prod.id,
                            cantidad=cantidad_destino,
                            fecha_ingreso=datetime.utcnow()
                        )
                        db.session.add(reg)
                        db.session.flush()
                        # Registrar en el mapa para que futuras iteraciones encuentren
                        # este registro sin ir a la DB (el producto puede aparecer
                        # dos veces en Siesa con distintos códigos)
                        _mapa_up[(ub.id, prod.id)] = reg
                        cargados += 1

                    _motivo = f'Carga inicial desde Siesa {fecha_hoy} · bodega {bod}'
                    if ya_en_reales:
                        _motivo += f' · {ya_en_reales} und ya en ubicación(es) real(es), restadas de SIESA-GENERAL'

                    movimiento = MovimientoInventario(
                        producto_id=prod.id,
                        ubicacion_id=ub.id,
                        almacen_id=almacen.id,
                        tipo='CARGA_INICIAL_SIESA',
                        cantidad=cantidad_destino,
                        saldo_antes=saldo_antes,
                        saldo_despues=cantidad_destino,
                        motivo=_motivo,
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
            estado['ultimo_sync_completo'] = datetime.utcnow()

        except Exception as e:
            # FM_RAILWAY_RESTART: si el proceso se mató a mitad del loop de páginas,
            # el bulk-zero nunca se ejecutó → productos de páginas no procesadas
            # conservan cantidad WMS potencialmente obsoleta. El siguiente sync
            # los actualizará, pero hay un gap hasta entonces.
            logger.error(f'[INV-SIESA] Error en carga inicial de {bod}: {e}', exc_info=True)
            db.session.rollback()
            estado['ultimo_error'] = str(e)
            estado['en_curso'] = False
            _reg.cerrar_error(_reg_id, e)
            try:
                from app.services.alertas_service import enviar_email, _config_resend
                if _config_resend():
                    enviar_email(
                        asunto=f'[WMS ALERTA] Sync inventario Siesa falló ({bod}) — bulk-zero puede estar incompleto',
                        cuerpo_texto=(
                            f'El sync de inventario Siesa para la bodega {bod} falló con error:\n{e}\n\n'
                            'Si el fallo ocurrió a mitad del loop de páginas, el bulk-zero '
                            '(zeroing de productos no reportados) puede no haberse ejecutado. '
                            'Los productos de páginas no procesadas conservan cantidades WMS posiblemente obsoletas. '
                            f'Disparar sync manual: POST /api/siesa/cargar-inventario?bodega={bod}'
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
            'bodega': bod,
            'cargados': cargados,
            'actualizados': actualizados,
            'sin_producto_wms': sin_producto_wms,
            'errores': errores,
            'total_siesa': len(inventario_siesa)
        }
        logger.info(f'[INV-SIESA] Carga inicial de {bod} completada: {resultado}')
        estado['ultimo_resultado'] = resultado
        estado['ultimo_error'] = None
        estado['en_curso'] = False
        _reg.cerrar_ok(_reg_id, resultado)


def iniciar_carga_inventario(app, forzar: bool = False, bodega: str = None):
    """Arranca la carga inicial en background. Retorna estado inmediatamente.

    `bodega`: código Siesa (ej. 'NS1'). Sin argumento, usa `connekta.bodega`
    (NB1) — mismo comportamiento de siempre.
    """
    global _estado_carga
    bod = bodega or connekta.bodega
    estado = _estado_carga_bodega(bod)

    if estado['en_curso']:
        return {'en_curso': True, 'mensaje': f'Carga de {bod} ya en proceso — espera que termine'}

    if connekta.modo_simulacion:
        return {'simulado': True, 'mensaje': 'Modo simulación — conecta credenciales Siesa'}

    # Guard: no sobrescribir stock si hay operaciones activas EN ESE ALMACÉN
    # (a menos que se fuerce). Acotado por almacén — antes de la Fase 1 de
    # calibración de tiendas (2026-08-27) esto miraba picking/packing de
    # TODO el WMS: un picking activo en NB1 habría bloqueado sin motivo una
    # carga a NS1, y viceversa.
    if not forzar:
        with app.app_context():
            almacen = _get_almacen(bod)
            if not almacen:
                return {'abortado': True, 'mensaje': f'No hay almacén activo en WMS para la bodega {bod}'}
            from app.models.picking import TareaPicking as _TP
            from app.models.packing import TareaPacking as _TP2
            picks_activos = (
                _TP.query
                .join(Ubicacion, Ubicacion.id == _TP.ubicacion_id)
                .filter(Ubicacion.almacen_id == almacen.id,
                        _TP.estado.in_(['PENDIENTE', 'EN_PROCESO']))
                .count()
            )
            packs_activos = _TP2.query.filter(
                _TP2.almacen_id == almacen.id,
                _TP2.estado.in_(['PENDIENTE', 'EN_PROCESO', 'VERIFICADO'])
            ).count()
            if picks_activos or packs_activos:
                return {
                    'abortado': True,
                    'mensaje': (
                        f'Carga de {bod} abortada: hay {picks_activos} picking(s) y {packs_activos} packing(s) activos '
                        f'en ese almacén. La carga sobreescribiría el stock reservado. '
                        f'Usa ?forzar=true solo si estás seguro.'
                    ),
                    'picks_activos': picks_activos,
                    'packs_activos': packs_activos,
                }

    estado['en_curso'] = True
    estado['ultimo_inicio'] = datetime.now(timezone.utc)

    hilo = threading.Thread(target=_run_carga_inicial, args=(app, bod), daemon=True)
    hilo.start()

    return {'iniciado': True, 'bodega': bod, 'mensaje': f'Carga de inventario de {bod} iniciada — refresca en ~60 seg'}


def estado_carga_inventario(bodega: str = None):
    bod = bodega or connekta.bodega
    estado = _estado_carga_bodega(bod)
    return {
        'bodega': bod,
        'en_curso': estado['en_curso'],
        'ultimo_inicio': estado['ultimo_inicio'].isoformat() if estado['ultimo_inicio'] else None,
        'ultimo_resultado': estado['ultimo_resultado'],
        'ultimo_error': estado['ultimo_error'],
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
    """Lógica real de reconciliación — corre en hilo de fondo.

    Compara, bodega por bodega, el stock WMS del almacén que le corresponde a
    esa bodega contra la existencia Siesa de esa MISMA bodega. Antes (hasta el
    2026-08-26) el lado WMS sumaba `UbicacionProducto` de TODOS los almacenes
    sin filtrar, mientras el lado Siesa solo traía una bodega
    (`connekta.bodega`, fija) — comparar "Siesa de una bodega" contra "WMS de
    todas" producía discrepancias falsas. Verificado en producción:
    ARTESA1119 (LANA ESCOLAR) reportaba 50 unidades de diferencia que no
    existían — NB1 contra NB1 cuadraba exacto; el sobrante eran 50 unidades
    sueltas en NC1 que no tenían nada que ver con esa comparación.
    """
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

        # El registro se abre DESPUÉS de tomar el lock — si dos workers reciben
        # el disparo casi al mismo tiempo, el que pierde el lock nunca abre fila
        # (no queda un registro "en_curso" zombie que tape el resultado real
        # del que sí corrió).
        from app.services import registro_sync_service as _reg
        _reg_id = _reg.abrir('reconciliacion')

        try:
            # Bodegas reales: solo almacenes con bodega_siesa_id configurado.
            # Cada bodega se reconcilia contra SU PROPIO almacén — nunca contra
            # la suma de todos.
            almacenes = Almacen.query.filter(
                Almacen.bodega_siesa_id.isnot(None), Almacen.activo == True
            ).all()
            almacen_por_bodega = {a.bodega_siesa_id: a.id for a in almacenes}

            if not almacen_por_bodega:
                resultado = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'abortado': True,
                    'motivo': 'Ningún almacén activo tiene bodega_siesa_id configurado',
                    'total_discrepancias': 0,
                    'discrepancias': [],
                }
                _estado_reconciliacion['ultimo_resultado'] = resultado
                _estado_reconciliacion['en_curso'] = False
                _reg.cerrar_ok(_reg_id, resultado)
                logger.warning('[RECONCILIACION] Abortada: ningún almacén con bodega_siesa_id')
                return

            # Stock WMS: una sola query bulk (no N+1), agrupada por PRODUCTO Y
            # ALMACÉN — agruparla solo por producto es lo que mezclaba bodegas.
            stock_wms_rows = (
                db.session.query(
                    UbicacionProducto.producto_id,
                    Ubicacion.almacen_id,
                    func.sum(UbicacionProducto.cantidad).label('total')
                )
                .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
                .group_by(UbicacionProducto.producto_id, Ubicacion.almacen_id)
                .all()
            )
            stock_wms_por_almacen: dict = {}
            for row in stock_wms_rows:
                stock_wms_por_almacen.setdefault(row.almacen_id, {})[row.producto_id] = int(row.total)

            # Guard: si ningún almacén con bodega_siesa_id tiene producto mapeado,
            # la carga inicial no se ha ejecutado — la reconciliación no tiene
            # sentido y generaría miles de discrepancias falsas (todo Siesa
            # aparecería como SIESA_MAYOR).
            tiene_stock_mapeado = any(
                stock_wms_por_almacen.get(almacen_id) for almacen_id in almacen_por_bodega.values()
            )
            if not tiene_stock_mapeado:
                resultado = {
                    'timestamp': datetime.utcnow().isoformat(),
                    'abortado': True,
                    'motivo': 'WMS sin stock mapeado — ejecuta la Carga Inicial primero',
                    'total_discrepancias': 0,
                    'discrepancias': [],
                }
                _estado_reconciliacion['ultimo_resultado'] = resultado
                _estado_reconciliacion['en_curso'] = False
                _reg.cerrar_ok(_reg_id, resultado)
                logger.warning('[RECONCILIACION] Abortada: ubicacion_productos vacía — ejecuta carga inicial')
                return

            # Liberar la conexión DB antes del HTTP download (puede tardar 2+ min).
            # Sin este commit, la sesión retiene la conexión del pool durante toda la descarga
            # bloqueando requests concurrentes en un pool pequeño (Railway: 5-10 conexiones).
            db.session.commit()

            # forzar=True: descarga datos frescos de Siesa ignorando el cache.
            # Multibodega: una sola descarga trae las 9 bodegas PV a la vez —
            # necesaria para poder comparar cada una contra su propio almacén.
            inventario_multibodega = _descargar_inventario_siesa_raw(forzar=True)

            discrepancias = []
            total_productos_siesa = 0
            total_productos_wms_con_stock = 0

            for bodega, almacen_id in sorted(almacen_por_bodega.items()):
                inventario_bodega = inventario_multibodega.get(bodega, {})
                stock_wms_almacen = stock_wms_por_almacen.get(almacen_id, {})
                if not inventario_bodega and not stock_wms_almacen:
                    continue  # nada que comparar en ninguno de los dos lados
                total_productos_siesa += len(inventario_bodega)
                total_productos_wms_con_stock += len(stock_wms_almacen)

                codigos_siesa = list(inventario_bodega.keys())
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

                productos_ids_wms = set(stock_wms_almacen.keys())

                for codigo, datos in inventario_bodega.items():
                    existencia_siesa = int(round(datos['existencia']))
                    prod = mapa_codigo.get(codigo)
                    if not prod:
                        continue
                    total_wms = stock_wms_almacen.get(prod.id, 0)
                    diferencia = total_wms - existencia_siesa
                    if diferencia != 0:
                        discrepancias.append({
                            'bodega': bodega,
                            'almacen_id': almacen_id,
                            'producto_id': prod.id,
                            'codigo': prod.codigo,
                            'nombre': prod.nombre,
                            'stock_wms': total_wms,
                            'stock_siesa': existencia_siesa,
                            'diferencia': diferencia,
                            'diferencia_abs': abs(diferencia),
                            'estado': 'WMS_MAYOR' if diferencia > 0 else 'SIESA_MAYOR'
                        })
                    productos_ids_wms.discard(prod.id)

                # [53] Pre-cargar productos SOLO_WMS en dict antes del loop (evita N+1)
                solo_wms_ids = [pid for pid in productos_ids_wms if stock_wms_almacen.get(pid, 0) > 0]
                solo_wms_prods = {
                    p.id: p
                    for p in Producto.query.filter(Producto.id.in_(solo_wms_ids)).all()
                } if solo_wms_ids else {}

                for prod_id in productos_ids_wms:
                    total_wms = stock_wms_almacen.get(prod_id, 0)
                    if total_wms == 0:
                        continue
                    prod = solo_wms_prods.get(prod_id)
                    if not prod:
                        continue
                    discrepancias.append({
                        'bodega': bodega,
                        'almacen_id': almacen_id,
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
            cobertura_pct = (
                total_productos_wms_con_stock / total_productos_siesa * 100
            ) if total_productos_siesa else 0

            # DEPRECATED (2026-07-28): antes se llamaba a
            # devolucion_service.crear_tareas_desde_discrepancias() para crear
            # TareaDevolucion ciegas (sin saber de qué pedido venía el excedente,
            # sin generar Nota Crédito). Reemplazado por el flujo proactivo de
            # DevolucionCliente (recepcionista busca el pedido/factura real) —
            # ver app/services/devolucion_cliente_service.py. La reconciliación
            # ahora es puramente informativa: las discrepancias SIESA_MAYOR
            # quedan visibles en GET /api/siesa/reconciliacion-estado para que
            # alguien las procese manualmente si corresponde a una devolución real.
            siesa_mayor = [d for d in discrepancias if d.get('estado') == 'SIESA_MAYOR']
            if cobertura_pct >= 20 and siesa_mayor:
                logger.warning(
                    f'[RECONCILIACION] {len(siesa_mayor)} discrepancia(s) SIESA_MAYOR — '
                    'informativo únicamente, no se crean tareas (módulo de devolución ciega desactivado)'
                )
            else:
                logger.warning(
                    f'[RECONCILIACION] Cobertura WMS={cobertura_pct:.1f}% (<20%) — informativo únicamente'
                )

            resultado = {
                'timestamp': ts,
                'bodegas_comparadas': sorted(almacen_por_bodega.keys()),
                'total_productos_siesa': total_productos_siesa,
                'total_productos_wms': total_productos_wms_con_stock,
                'cobertura_pct': round(cobertura_pct, 1),
                'devoluciones_activas': cobertura_pct >= 20,
                'total_discrepancias': len(discrepancias),
                'discrepancias': discrepancias[:100]
            }
            _estado_reconciliacion['ultimo_resultado'] = resultado
            _estado_reconciliacion['ultimo_error'] = None
            _reg.cerrar_ok(_reg_id, resultado)

        except Exception as e:
            logger.error('[RECONCILIACION] Error fatal — discrepancias SIESA_MAYOR sin procesar', exc_info=True)
            db.session.rollback()
            _estado_reconciliacion['ultimo_error'] = str(e)
            _estado_reconciliacion['ultimo_resultado'] = None
            _reg.cerrar_error(_reg_id, e)
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


def _estado_reconciliacion_memoria():
    return {
        'en_curso': _estado_reconciliacion['en_curso'],
        'ultimo_inicio': _estado_reconciliacion['ultimo_inicio'].isoformat() if _estado_reconciliacion['ultimo_inicio'] else None,
        'ultimo_resultado': _estado_reconciliacion['ultimo_resultado'],
        'ultimo_error': _estado_reconciliacion['ultimo_error'],
    }


def estado_reconciliacion():
    """Estado de la reconciliación — manda `registros_sync`, no la memoria.

    `_estado_reconciliacion` es del PROCESO que lo atiende. Con 2 workers
    Gunicorn (`--workers=2`, ver railway.toml), el POST que arranca la
    reconciliación puede caer en un worker y este GET en el otro, que nunca
    la vio correr — antes eso devolvía `ultimo_resultado: null` aunque el
    reporte existiera, y el reporte es la única salida de esta función: a
    diferencia de la carga inicial o el sync de catálogo, no queda respaldado
    en ninguna otra tabla si se pierde acá. `registro_sync_service` sobrevive
    tanto al worker equivocado como a un reinicio de Railway a mitad de la
    corrida (ver app/models/registro_sync.py).
    """
    from app.services import registro_sync_service as _reg
    persistido = _reg.ultimo('reconciliacion')

    if persistido and '_error_lectura' in persistido:
        memoria = _estado_reconciliacion_memoria()
        memoria['ultimo_error'] = (
            f"No se pudo leer el historial persistido ({persistido['_error_lectura']}) "
            f"— mostrando solo lo que sabe este proceso"
        )
        return memoria

    if persistido is None:
        # Ninguna reconciliación ha llegado a abrir su fila todavía en NINGÚN
        # proceso — cubre la ventana entre el POST y el primer commit del
        # hilo, donde la tabla legítimamente no tiene nada que decir.
        return _estado_reconciliacion_memoria()

    return {
        # `fin is None` en la fila más reciente == sigue corriendo, en este
        # worker o en cualquier otro — la tabla no distingue por worker.
        'en_curso': persistido['fin'] is None,
        'ultimo_inicio': persistido['inicio'],
        'ultimo_resultado': persistido['resultado'] if persistido['ok'] else None,
        'ultimo_error': persistido['error'] if persistido['ok'] is False else None,
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
    from app.services import registro_sync_service as _reg

    # El setup abre su propio registro además de los de catálogo y stock: los
    # tres pasos pueden correrse sueltos, y "se corrió la secuencia completa" es
    # una afirmación distinta de "se corrieron los pasos".
    with app.app_context():
        _reg_id = _reg.abrir('setup_inicial')

    try:
        _estado_setup['fase'] = 'catalogo'
        _run_sync(app)

        _estado_setup['fase'] = 'stock'
        # Marcar _estado_carga (bodega default) como en curso para que
        # iniciar_carga_inventario() concurrente no lance un segundo hilo
        # mientras el setup ejecuta la carga.
        _estado_default = _estado_carga_bodega(connekta.bodega)
        _estado_default['en_curso'] = True
        _estado_default['ultimo_inicio'] = datetime.now(timezone.utc)
        try:
            _run_carga_inicial(app)
        finally:
            _estado_default['en_curso'] = False

        _estado_setup['fase'] = 'completado'
        _estado_setup['ultimo_error'] = None
        with app.app_context():
            _reg.cerrar_ok(_reg_id, {'fase': 'completado'})
    except Exception as e:
        logger.error(f'[SETUP] Error en setup inicial: {e}')
        _estado_setup['ultimo_error'] = str(e)
        _estado_setup['fase'] = 'error'
        with app.app_context():
            _reg.cerrar_error(_reg_id, e)
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
    """Qué pasos del arranque corrieron — **leído de la tabla, no de la memoria**.

    Los campos `resultado_*` siguen viniendo de los dicts de módulo y siguen
    valiendo lo mismo que antes: se borran en cada deploy. Se conservan para no
    romper a quien ya los lee, pero **no son la respuesta a «¿ya se cargó?»**.

    Esa la contesta `persistido`, que sale de `registros_sync`. La diferencia se
    midió en producción el 2026-08-10: `resultado_catalogo: null` después de
    tres deploys el mismo día, sin forma de distinguir «nunca corrió» de «corrió
    antes del último reinicio».
    """
    from app.services.siesa_sync_service import estado_sync
    from app.services import registro_sync_service as _reg

    _cat = estado_sync().get('ultimo_resultado')
    _resultado_stock = _estado_carga_bodega(connekta.bodega)['ultimo_resultado']
    return {
        'en_curso': _estado_setup['en_curso'],
        'fase': _estado_setup['fase'],
        'ultimo_inicio': _estado_setup['ultimo_inicio'].isoformat() if _estado_setup['ultimo_inicio'] else None,
        # En memoria — se pierden al reiniciar. Ver docstring.
        'resultado_catalogo': _cat,
        'resultado_stock': _resultado_stock,
        'ultimo_error': _estado_setup['ultimo_error'],
        # En la base — sobreviven al deploy. ESTO es lo que hay que mirar.
        'persistido': {
            'catalogo': _reg.estado_persistido('catalogo', bool(_cat)),
            'barcodes': _reg.estado_persistido('barcodes'),
            'stock': _reg.estado_persistido('stock', bool(_resultado_stock)),
            'setup_inicial': _reg.estado_persistido('setup_inicial'),
        },
        'cobertura': cobertura_catalogo(),
    }


def cobertura_catalogo():
    """Cuántos productos hay y cuántos tienen código de barras. De la base.

    «Códigos de barras cargados» era una casilla que nadie podía marcar con
    honestidad: el endpoint de sync reportaba el resultado de la última corrida
    —en memoria— y no la cobertura real. Un sync exitoso que actualizó 3 de
    12.000 productos se veía igual que uno que los cubrió todos.

    Sin `porcentaje` calculado cuando no hay productos: dividir por cero para
    mostrar `0%` diría «no hay cobertura» cuando la verdad es «no hay catálogo»,
    que es un problema distinto y anterior.
    """
    from app.models.producto import Producto

    try:
        activos = Producto.query.filter_by(activo=True).count()
        con_barras = (Producto.query
                      .filter(Producto.activo.is_(True),
                              Producto.codigo_barras.isnot(None),
                              Producto.codigo_barras != '')
                      .count())
    except Exception as e:
        return {'_error_lectura': str(e)[:200]}

    return {
        'productos_activos': activos,
        'con_codigo_barras': con_barras,
        'sin_codigo_barras': activos - con_barras,
        'porcentaje': round(100.0 * con_barras / activos, 1) if activos else None,
        'hay_catalogo': activos > 0,
    }
