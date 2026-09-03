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

2. RECONCILIACIÓN (_run_reconciliacion → _calcular_reconciliacion)
   - Descarga existencias de Siesa de TODAS las bodegas del universo
     (`_BODEGAS_INVENTARIO`), no solo de `connekta.bodega`.
   - Compara **bodega contra bodega**: el lado WMS se agrupa por
     `Almacen.bodega_siesa_id` uniendo UbicacionProducto → Ubicacion → Almacen.
   - Declara el tercer estado, «no se puede comparar», en vez de mezclarlo en
     la suma: almacén WMS sin bodega Siesa, bodega WMS sin datos de Siesa,
     bodega Siesa sin almacén WMS, SKU de Siesa sin producto en el catálogo.
   - Publica el DENOMINADOR (`cuadre_pct`, `skus_comparados`, `por_bodega`):
     un conteo de diferencias sin cuántos SKU se compararon no se interpreta.
   - NO modifica nada. Solo informa.
   - El admin decide: "aceptar Siesa" (ajuste WMS) o "hacer conteo físico".

   Hasta 2026-08-20 sumaba `UbicacionProducto` de TODOS los almacenes —
   incluidas las tiendas `PV-*`, que tienen stock físico real— contra el
   inventario de una sola bodega de Siesa. Dos poblaciones distintas restadas
   una de la otra, publicadas como «✓ Sin diferencias». Ese veredicto es la
   casilla de la Fase 4 de `docs/arranque_produccion.md`: la luz verde para que
   los operarios arranquen en producción.

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

#: Bodegas de SERVICIO — no son puntos de venta y **no llevan almacén en el
#: WMS** (ver la tabla de bodegas de `CLAUDE.md`). Se descargan igual porque no
#: descargarlas no las hace desaparecer: las vuelve invisibles.
#:
#:   · `AV1` (Averías CDI, `SIESA_BODEGA_AVERIAS`) — `siesa_job_service` mueve
#:     la avería en Siesa de NB1 a AV1, mientras `devolucion_cliente_service`
#:     (el escritor VIVO; `devolucion_service` está DEPRECATED) la deja en
#:     una ubicación `AVERIADOS` (zona CUARENTENA) **dentro del almacén NB1**.
#:     Sin AV1 descargada, cada avería producía un `WMS_MAYOR` permanente en NB1
#:     y nadie podía explicar de dónde salía. Su contraparte en el WMS existe
#:     pero es una UBICACIÓN, no un almacén: se declara y se mide, no se cuadra.
#:   · `TRA1` (bodega en Tránsito, `SIESA_BODEGA_TRANSITO`) — **no tiene ninguna
#:     contraparte en el WMS**. El STS descuenta del origen y el ETS acredita en
#:     el destino; lo que quede en TRA1 es el limbo que los invariantes de
#:     traslado existen para detectar.
#:
#: Se mantienen FUERA de `_BODEGAS_PV` a propósito: esa constante significa
#: «bodegas que el WMS OPERA» y `tests/test_bodegas_coherentes.py` la vigila
#: contra el maestro. Meterlas ahí las convertiría en destino válido de un
#: traslado y en opción de un desplegable de punto de venta.
_BODEGAS_SERVICIO = ['AV1', 'TRA1']

#: El universo que se le pide a Siesa. Operadas + servicio.
_BODEGAS_INVENTARIO = _BODEGAS_PV + _BODEGAS_SERVICIO


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
            if not bodega or not codigo or bodega not in _BODEGAS_INVENTARIO:
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
    for bod in _BODEGAS_INVENTARIO:
        inv_bd = _leer_stock_de_bd(bod)
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

    # La persistencia va DESPUÉS del guard anti-parcial, no antes.
    #
    # `stock_siesa` alimenta `armador_service.rop_dual` → `deficit` → el
    # contenedor, que es irreversible 120 días. Es **acumulativa** (upsert sin
    # borrado) y `_guardar_stock_en_bd` commitea **por bodega, dentro del
    # bucle**: un barrido que moría a mitad dejaba unas bodegas frescas y otras
    # viejas, ya commiteadas, y nada las distinguía — la tabla no guarda marca
    # de corrida, solo `updated_at`.
    #
    # El guard existía y protegía el **reporte** de la reconciliación (`:497`,
    # `:546`), no la **tabla**. O sea que la respuesta parcial abortaba el
    # veredicto y ya había escrito el inventario incompleto sobre el que se
    # compra.
    #
    # No se levanta: esta función tiene cinco llamadores que esperan el dict, y
    # dos ya corren su propio guard. Lo que cambia es que el dato parcial no
    # llega a la tabla. La bodega que no se refrescó conserva su última foto y
    # su `updated_at` viejo — que `rop_dual` ahora publica como `frescura_stock`,
    # porque el histórico ya escrito **no es distinguible** y purgarlo exigiría
    # un barrido completo confiable que hoy no tenemos.
    try:
        _verificar_respuesta_no_parcial(inventario_global.get(connekta.bodega, {}))
    except ValueError as _e_parcial:
        logger.error(
            '[INV-SIESA] Barrido parcial — NO se persiste en stock_siesa: %s. '
            'Cada bodega conserva su última foto; la antigüedad viaja en '
            '`frescura_stock` de la fila del Armador.', _e_parcial)
    else:
        _guardar_stock_en_bd(inventario_global)

    return inventario_global


def _guardar_stock_en_bd(inventario_global: dict):
    """Persiste el inventario descargado en la tabla stock_siesa (upsert)."""
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


def _leer_stock_de_bd(bodega_id: str) -> dict:
    """Lee inventario de una bodega desde PostgreSQL (sobrevive deploys)."""
    from app.models.stock_siesa import StockSiesa
    try:
        rows = StockSiesa.query.filter_by(bodega=bodega_id).all()
        if not rows:
            return {}
        inv = {}
        for r in rows:
            inv[r.codigo_siesa] = {
                'existencia': r.existencia or 0,
                'comprometido': r.comprometido or 0,
                'salida_sin_conf': r.salida_sin_conf or 0,
                'descripcion': r.descripcion or '',
                'unidad': r.unidad_medida or 'UND',
            }
        logger.info('[INV-SIESA] BD: %s leído (%d productos)', bodega_id, len(inv))
        return inv
    except Exception as exc:
        logger.error('[INV-SIESA] Error leyendo BD para %s: %s', bodega_id, exc)
        return {}


def obtener_stock_bodega(bodega_id: str, forzar=False):
    """
    1. Cache en memoria → instantáneo
    2. Si cache vacío → lee de PostgreSQL (sobrevive deploys) → instantáneo
    3. Si BD vacía → lanza precalentamiento background → retorna {}
    """
    data = _cache_inventario_multibodega['data']
    if data is not None and bodega_id in data:
        return data[bodega_id]

    inv_bd = _leer_stock_de_bd(bodega_id)
    if inv_bd:
        if _cache_inventario_multibodega['data'] is None:
            _cache_inventario_multibodega['data'] = {}
        _cache_inventario_multibodega['data'][bodega_id] = inv_bd
        return inv_bd

    precalentar_cache_multibodega()
    return {}


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

    _verificar_respuesta_no_parcial(inventario)

    _cache_inventario_siesa['data'] = inventario
    _cache_inventario_siesa['ts'] = datetime.utcnow()
    return inventario


def _verificar_respuesta_no_parcial(inventario: dict):
    """Aborta si la respuesta de Siesa para la bodega principal llegó a medias.

    Una respuesta parcial no se distingue de «Siesa dice que no hay»: en la
    carga inicial pone en cero miles de productos, y en la reconciliación
    convierte el catálogo entero en `WMS_MAYOR`. Las dos veces el resultado se
    ve como un dato, no como un fallo.

    Vive en una función porque la usan los dos caminos —carga inicial y
    reconciliación— y el mismo criterio escrito dos veces diverge (Regla 0,
    corolario «una política, una función»).
    """
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


def _descargar_inventario_multibodega_para_reconciliar():
    """El inventario de Siesa **por bodega**, con el mismo guard anti-parcial.

    La reconciliación necesita el diccionario completo `{bodega: {codigo: …}}`,
    no la vista aplastada de `connekta.bodega`. Lo que no puede perder al
    dejar de usar `_descargar_inventario_siesa()` es su guard: por eso lo llama
    acá explícitamente sobre la bodega principal.
    """
    multi = _descargar_inventario_siesa_raw(forzar=True)
    _verificar_respuesta_no_parcial(multi.get(connekta.bodega, {}))
    return multi


# ─────────────────────────────────────────────
# 1. CARGA INICIAL
# ─────────────────────────────────────────────

_ADVISORY_LOCK_INV_SIESA = 2002  # clave única para pg_advisory_lock


def _run_carga_inicial(app):
    """Lógica real de la carga inicial — corre en hilo de fondo."""
    global _estado_carga

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
            _reg.cerrar_error(_reg_id, e)
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
        _reg.cerrar_ok(_reg_id, resultado)


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

#: Motivos del tercer estado. «No se puede comparar» no es «cuadra»: es una
#: respuesta distinta, y mezclarla en la suma la hace desaparecer.
_SIN_BODEGA_SIESA = 'ALMACEN_WMS_SIN_BODEGA_SIESA'
_SIN_DATOS_SIESA = 'BODEGA_WMS_SIN_DATOS_DE_SIESA'
_SIN_ALMACEN_WMS = 'BODEGA_SIESA_SIN_ALMACEN_WMS'


def _stock_wms_por_bodega():
    """Stock del WMS agrupado por **bodega Siesa**, no por producto suelto.

    El defecto que esto arregla: la reconciliación sumaba
    `UbicacionProducto.cantidad` de TODOS los almacenes contra el inventario de
    una sola bodega de Siesa. Las tiendas (`PV-NC1`, `PV-PC1`…), que
    `tienda_oc_service.resolver_almacen` crea con stock físico real, entraban en
    el lado WMS y no en el de Siesa. El número resultante no era un error de
    redondeo: era la suma de dos poblaciones distintas, y salía por pantalla
    como «✓ Sin diferencias» o como un sobrante que nadie podía explicar.

    Se une con `outerjoin` a propósito. Un `join` interno descarta en silencio
    la fila cuya ubicación o cuyo almacén no existe — que es exactamente el dato
    que hay que declarar, no perder.

    Retorna `(por_bodega, sin_bodega)`:
      · `por_bodega` → `{bodega_siesa_id: {producto_id: cantidad}}`
      · `sin_bodega` → `{codigo_almacen: {producto_id: cantidad}}`, los almacenes
        sin `bodega_siesa_id` asignado. Regla 0: ante dato ausente no se
        inventa una bodega, se declara que no se puede comparar.
    """
    filas = (
        db.session.query(
            Almacen.bodega_siesa_id.label('bodega'),
            Almacen.codigo.label('almacen'),
            UbicacionProducto.producto_id.label('producto_id'),
            func.sum(UbicacionProducto.cantidad).label('total'),
        )
        .select_from(UbicacionProducto)
        .outerjoin(Ubicacion, UbicacionProducto.ubicacion_id == Ubicacion.id)
        .outerjoin(Almacen, Ubicacion.almacen_id == Almacen.id)
        .group_by(Almacen.bodega_siesa_id, Almacen.codigo,
                  UbicacionProducto.producto_id)
        .all()
    )

    por_bodega = {}
    sin_bodega = {}
    for fila in filas:
        cantidad = int(fila.total or 0)
        if cantidad == 0:
            continue
        bodega = (fila.bodega or '').strip()
        if bodega:
            por_bodega.setdefault(bodega, {})
            por_bodega[bodega][fila.producto_id] = (
                por_bodega[bodega].get(fila.producto_id, 0) + cantidad)
        else:
            # Sin almacén = ubicación huérfana. Se declara igual, con un nombre
            # que no miente sobre lo que es.
            clave = fila.almacen or '(ubicación sin almacén)'
            sin_bodega.setdefault(clave, {})
            sin_bodega[clave][fila.producto_id] = (
                sin_bodega[clave].get(fila.producto_id, 0) + cantidad)

    return por_bodega, sin_bodega


def _cuarentena_wms():
    """Unidades del WMS en ubicaciones de cuarentena (`AVERIADOS`).

    Es la contraparte real de la bodega `AV1` de Siesa, y no es un almacén: es
    una UBICACIÓN dentro de NB1 (`devolucion_cliente_service`, el escritor VIVO).
    Se mide para poder
    declararla al lado del saldo de AV1 — **no para cuadrar contra él**: son dos
    poblaciones que nadie ha verificado que coincidan, y afirmar que coinciden
    sería reintroducir el mismo defecto en pequeño.
    """
    try:
        filas = (
            db.session.query(func.sum(UbicacionProducto.cantidad),
                             func.count(func.distinct(UbicacionProducto.producto_id)))
            .select_from(UbicacionProducto)
            .join(Ubicacion, UbicacionProducto.ubicacion_id == Ubicacion.id)
            .filter(db.or_(Ubicacion.tipo == 'cuarentena',
                           Ubicacion.zona == 'CUARENTENA',
                           Ubicacion.tipo_zona == 'AVERIAS'))
            .first()
        )
        return {'unidades': int(filas[0] or 0), 'skus': int(filas[1] or 0)}
    except Exception as exc:   # pragma: no cover — no puede tumbar el veredicto
        logger.error('[RECONCILIACION] No se pudo medir la cuarentena: %s', exc)
        return {'unidades': None, 'skus': None}


def _calcular_reconciliacion(inventario_por_bodega: dict) -> dict:
    """Compara **bodega contra bodega** y declara lo que no se puede comparar.

    Tres respuestas posibles por bodega, no dos:

      · cuadra          — WMS y Siesa dicen lo mismo para ese SKU.
      · discrepa        — dicen cosas distintas. Va con su bodega en la fila:
                          una diferencia sin bodega no se puede ir a contar a
                          ningún lado.
      · no se puede     — falta un lado entero. Un almacén WMS sin
        comparar        `bodega_siesa_id`, una bodega WMS que Siesa no reportó,
                          o una bodega de Siesa sin almacén en el WMS. Antes
                          estos casos se mezclaban en la suma y desaparecían.

    El veredicto `sin_diferencias` exige cero discrepancias **y** cero
    incomparables IMPREVISTOS. Es la casilla de la Fase 4 de
    `docs/arranque_produccion.md` — la luz verde para que los operarios
    arranquen —, así que «no sé» tiene que pintar distinto de «está bien»…
    pero también tiene que poder ponerse en verde alguna vez: exigirle cero
    incomparables **de cualquier clase** la dejaba en ámbar para siempre,
    porque `AV1` no tiene almacén WMS y nunca lo va a tener. El corte lo hace
    `_incomparable_esperado()`, que es donde está escrito el porqué de cada
    exención.

    Nota sobre traslados recibidos: `traslado_service.confirmar_recepcion` manda
    el ETS a Siesa y no acredita `UbicacionProducto` en el almacén destino. Si
    las tiendas destino llevan bins en el WMS, ese traslado aparece acá como
    `SIESA_MAYOR` en la bodega de la tienda — visible y con nombre, en vez de
    tapado dentro de un total. Si no los llevan, la tienda no tiene almacén y
    cae en `BODEGA_SIESA_SIN_ALMACEN_WMS`. La pregunta sigue abierta; el
    resultado la muestra en cualquiera de las dos formas.
    """
    wms_por_bodega, wms_sin_bodega = _stock_wms_por_bodega()

    # Total plano — se conserva porque el guard de «carga inicial sin correr» y
    # la cobertura de catálogo se miden sobre el catálogo, no por bodega.
    stock_wms_total = {}
    for mapa in list(wms_por_bodega.values()) + list(wms_sin_bodega.values()):
        for pid, cant in mapa.items():
            stock_wms_total[pid] = stock_wms_total.get(pid, 0) + cant

    if not stock_wms_total:
        # Sin esto, todo Siesa aparecería como SIESA_MAYOR.
        logger.warning('[RECONCILIACION] Abortada: ubicacion_productos vacía — ejecuta carga inicial')
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'abortado': True,
            'motivo': 'WMS sin stock mapeado — ejecuta la Carga Inicial primero',
            'sin_diferencias': False,
            'total_discrepancias': 0,
            'discrepancias': [],
            'total_no_comparable': 0,
            'total_no_comparable_esperado': 0,
            'total_no_comparable_imprevisto': 0,
            'no_comparable': _no_comparable_vacio(),
            'por_bodega': [],
        }

    # Un solo mapa código→Producto para todas las bodegas.
    codigos_siesa = set()
    for productos in inventario_por_bodega.values():
        codigos_siesa.update(productos.keys())
    mapa_codigo = {}
    if codigos_siesa:
        for p in (Producto.query
                  .filter(db.or_(Producto.codigo_siesa.in_(codigos_siesa),
                                 Producto.codigo.in_(codigos_siesa)))
                  .all()):
            if p.codigo_siesa:
                mapa_codigo[p.codigo_siesa] = p
            mapa_codigo[p.codigo] = p

    # La tabla `almacenes` es la autoridad sobre qué bodega tiene almacén: un
    # almacén sin stock sigue siendo una contraparte válida.
    bodegas_con_almacen = {
        (a.bodega_siesa_id or '').strip(): a
        for a in Almacen.query.filter(Almacen.bodega_siesa_id.isnot(None)).all()
        if (a.bodega_siesa_id or '').strip()
    }
    # [53] Pre-cargar en un solo query los productos del lado WMS: el bucle de
    # SOLO_WMS los necesita por id y con 5000 SKU un N+1 acá cuesta minutos.
    ids_wms = {pid for mapa in wms_por_bodega.values() for pid in mapa}
    prods_wms = {
        p.id: p for p in Producto.query.filter(Producto.id.in_(ids_wms)).all()
    } if ids_wms else {}

    discrepancias = []
    por_bodega = []
    no_comparable = _no_comparable_vacio()

    universo = sorted(set(inventario_por_bodega) | set(wms_por_bodega))
    for bodega in universo:
        siesa_b = inventario_por_bodega.get(bodega, {})
        wms_b = wms_por_bodega.get(bodega, {})
        unidades_siesa = int(round(sum(d['existencia'] for d in siesa_b.values())))
        unidades_wms = sum(wms_b.values())

        tiene_almacen = bodega in bodegas_con_almacen
        tiene_siesa = bodega in inventario_por_bodega

        if not tiene_almacen:
            if not siesa_b:
                continue
            justificacion = _incomparable_esperado(bodega)
            entrada = {
                'bodega': bodega,
                'motivo': _SIN_ALMACEN_WMS,
                'skus_siesa': len(siesa_b),
                'unidades_siesa': unidades_siesa,
                'contraparte_wms': _contraparte_declarada(bodega),
                # «Previsto y declarado» vs. «nadie lo previó». Lo primero no
                # descalifica el veredicto; lo segundo sí. Viaja en la fila
                # para que la pantalla los pinte distinto sin volver a
                # decidirlo por su cuenta.
                'esperado': justificacion is not None,
                'justificacion': justificacion,
            }
            no_comparable['bodegas_siesa_sin_almacen_wms'].append(entrada)
            por_bodega.append({
                'bodega': bodega, 'comparable': False, 'motivo': _SIN_ALMACEN_WMS,
                'skus_wms': 0, 'skus_siesa': len(siesa_b),
                'unidades_wms': 0, 'unidades_siesa': unidades_siesa,
                'denominador': 0, 'cuadran': 0, 'cuadre_pct': None,
                'discrepancias': 0,
                # La misma distinción, en la fila de la tabla por bodega: si la
                # tabla pintara AV1 igual que una bodega inesperada, la
                # separación del veredicto no se vería donde se mira primero.
                'esperado': justificacion is not None,
            })
            continue

        if not tiene_siesa:
            if not wms_b:
                continue
            no_comparable['bodegas_wms_sin_datos_siesa'].append({
                'bodega': bodega,
                'motivo': _SIN_DATOS_SIESA,
                'almacen': bodegas_con_almacen[bodega].codigo,
                'skus_wms': len(wms_b),
                'unidades_wms': unidades_wms,
            })
            por_bodega.append({
                'bodega': bodega, 'comparable': False, 'motivo': _SIN_DATOS_SIESA,
                'skus_wms': len(wms_b), 'skus_siesa': 0,
                'unidades_wms': unidades_wms, 'unidades_siesa': 0,
                'denominador': 0, 'cuadran': 0, 'cuadre_pct': None,
                'discrepancias': 0,
                # Que Siesa no reporte una bodega que el WMS opera nunca está
                # previsto: o falló la descarga, o alguien dejó de usarla sin
                # decirlo. Las dos merecen que alguien mire.
                'esperado': False,
            })
            continue

        # ── Comparable: los dos lados existen ──────────────────────────
        vistos = set()
        denominador = 0
        cuadran = 0
        sin_mapeo = 0
        discrepancias_bodega = 0

        for codigo, datos in siesa_b.items():
            existencia = int(round(datos['existencia']))
            prod = mapa_codigo.get(codigo)
            if not prod:
                # Un SKU de Siesa sin producto en el catálogo del WMS tampoco
                # «cuadra»: no se puede comparar. Antes se descartaba callado.
                if existencia:
                    sin_mapeo += 1
                continue
            vistos.add(prod.id)
            total_wms = wms_b.get(prod.id, 0)
            if total_wms == 0 and existencia == 0:
                continue   # nada en juego: no infla el denominador
            denominador += 1
            diferencia = total_wms - existencia
            if diferencia == 0:
                cuadran += 1
                continue
            discrepancias_bodega += 1
            discrepancias.append({
                'bodega': bodega,
                'producto_id': prod.id,
                'codigo': prod.codigo,
                'nombre': prod.nombre,
                'stock_wms': total_wms,
                'stock_siesa': existencia,
                'diferencia': diferencia,
                'diferencia_abs': abs(diferencia),
                'estado': 'WMS_MAYOR' if diferencia > 0 else 'SIESA_MAYOR',
            })

        for pid, cantidad in wms_b.items():
            if pid in vistos or cantidad == 0:
                continue
            prod = prods_wms.get(pid)
            if not prod:
                continue
            denominador += 1
            discrepancias_bodega += 1
            discrepancias.append({
                'bodega': bodega,
                'producto_id': pid,
                'codigo': prod.codigo,
                'nombre': prod.nombre,
                'stock_wms': cantidad,
                'stock_siesa': 0,
                'diferencia': cantidad,
                'diferencia_abs': cantidad,
                'estado': 'SOLO_WMS',
            })

        no_comparable['skus_siesa_sin_producto_wms'] += sin_mapeo
        por_bodega.append({
            'bodega': bodega,
            'comparable': True,
            'motivo': None,
            'skus_wms': len(wms_b),
            'skus_siesa': len(siesa_b),
            'unidades_wms': unidades_wms,
            'unidades_siesa': unidades_siesa,
            'denominador': denominador,
            'cuadran': cuadran,
            'cuadre_pct': round(100.0 * cuadran / denominador, 1) if denominador else None,
            'discrepancias': discrepancias_bodega,
            'skus_siesa_sin_producto_wms': sin_mapeo,
        })

    # Almacenes sin bodega Siesa asignada — el tercer estado más silencioso:
    # su stock se sumaba al total del WMS y aparecía como sobrante de NB1.
    for codigo_almacen, productos in sorted(wms_sin_bodega.items()):
        no_comparable['almacenes_sin_bodega_siesa'].append({
            'almacen': codigo_almacen,
            'motivo': _SIN_BODEGA_SIESA,
            'skus': len(productos),
            'unidades': sum(productos.values()),
        })

    discrepancias.sort(key=lambda x: x['diferencia_abs'], reverse=True)

    total_no_comparable = (
        sum(a['skus'] for a in no_comparable['almacenes_sin_bodega_siesa'])
        + sum(b['skus_wms'] for b in no_comparable['bodegas_wms_sin_datos_siesa'])
        + sum(b['skus_siesa'] for b in no_comparable['bodegas_siesa_sin_almacen_wms'])
        + no_comparable['skus_siesa_sin_producto_wms']
    )

    # El mismo total, partido en dos: lo que se sabía que iba a pasar y lo que
    # no. `total_no_comparable` se conserva con el significado de siempre —lo
    # incomparable, todo— porque es lo que hay que seguir mirando; el veredicto
    # es el que deja de exigirle cero.
    total_no_comparable_esperado = sum(
        b['skus_siesa'] for b in no_comparable['bodegas_siesa_sin_almacen_wms']
        if b['esperado'])
    total_no_comparable_imprevisto = (
        total_no_comparable - total_no_comparable_esperado)

    denominador_total = sum(b['denominador'] for b in por_bodega)
    cuadran_total = sum(b['cuadran'] for b in por_bodega)

    productos_wms_con_stock = len(stock_wms_total)
    productos_siesa = len(codigos_siesa)
    cobertura_pct = (productos_wms_con_stock / productos_siesa * 100) if productos_siesa else 0

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
    almacen = _get_almacen()
    if almacen and cobertura_pct >= 20 and siesa_mayor:
        logger.warning(
            f'[RECONCILIACION] {len(siesa_mayor)} discrepancia(s) SIESA_MAYOR — '
            'informativo únicamente, no se crean tareas (módulo de devolución ciega desactivado)')
    elif almacen:
        logger.warning(
            f'[RECONCILIACION] Cobertura WMS={cobertura_pct:.1f}% (<20%) — informativo únicamente')

    if total_no_comparable_imprevisto:
        logger.warning(
            '[RECONCILIACION] %d SKU no se pueden comparar y nadie los previó '
            '— el veredicto NO es «sin diferencias» aunque las discrepancias '
            'sean cero', total_no_comparable_imprevisto)
    if total_no_comparable_esperado:
        logger.info(
            '[RECONCILIACION] %d SKU incomparables PREVISTOS (%s) — se declaran '
            'y no descalifican el veredicto', total_no_comparable_esperado,
            ', '.join(b['bodega'] for b in
                      no_comparable['bodegas_siesa_sin_almacen_wms']
                      if b['esperado']))

    return {
        'timestamp': datetime.utcnow().isoformat(),
        # ⚠ CAMBIO DE SIGNIFICADO (2026-08-20): antes exigía cero incomparables
        # de cualquier clase, y con AV1 sin almacén —que nunca lo va a tener—
        # el verde era inalcanzable. Ahora exige cero discrepancias y cero
        # incomparables IMPREVISTOS. Los previstos siguen publicados, contados
        # y pintados; lo que dejan de hacer es descalificar la Fase 4.
        'sin_diferencias': not discrepancias and not total_no_comparable_imprevisto,
        'total_productos_siesa': productos_siesa,
        'total_productos_wms': productos_wms_con_stock,
        'cobertura_pct': round(cobertura_pct, 1),
        'devoluciones_activas': cobertura_pct >= 20,
        'total_discrepancias': len(discrepancias),
        'discrepancias': discrepancias[:100],
        # El denominador del cuadre, visible. Un conteo de discrepancias sin
        # cuántos SKU se compararon no se puede interpretar.
        'cuadre_pct': round(100.0 * cuadran_total / denominador_total, 1) if denominador_total else None,
        'skus_comparados': denominador_total,
        'skus_cuadran': cuadran_total,
        'bodegas_comparadas': sorted(b['bodega'] for b in por_bodega if b['comparable']),
        'por_bodega': por_bodega,
        'total_no_comparable': total_no_comparable,
        'total_no_comparable_esperado': total_no_comparable_esperado,
        'total_no_comparable_imprevisto': total_no_comparable_imprevisto,
        'no_comparable': no_comparable,
    }


def _no_comparable_vacio():
    return {
        'almacenes_sin_bodega_siesa': [],
        'bodegas_wms_sin_datos_siesa': [],
        'bodegas_siesa_sin_almacen_wms': [],
        'skus_siesa_sin_producto_wms': 0,
    }


#: Por qué cada bodega de servicio no puede tener almacén en el WMS. Es la
#: **lista de excepciones al veredicto**, y por eso está escrita entera y con
#: motivo por bodega: una lista de excepciones sin justificación se convierte en
#: el sitio donde alguien mete lo que le molesta. Dos entradas, las mismas dos
#: que `CLAUDE.md` declara bodegas de servicio, vigiladas por
#: `tests/test_reconciliacion_por_bodega.py::TestLaListaDeExentasEsCortaYVigilada`.
_JUSTIFICACION_SIN_ALMACEN = {
    'AV1': ('Averías CDI — el WMS deja la avería en una ubicación AVERIADOS '
            '(cuarentena) dentro de NB1, no en un almacén propio. La '
            'contraparte existe, está medida y no se cuadra.'),
    'TRA1': ('Bodega en Tránsito — el STS descuenta del origen y el ETS '
             'acredita en el destino. Que quede saldo mientras un traslado '
             'viaja es la operación normal; el limbo que sí importa lo mide '
             '`auditoria/traslados`, que puede ver la ANTIGÜEDAD y esta '
             'comparación no.'),
}


def _incomparable_esperado(bodega: str):
    """¿Esta bodega de Siesa sin almacén WMS estaba prevista? Devuelve el motivo.

    **Una política, una función.** El veredicto y la pantalla preguntan lo
    mismo y tienen que recibir la misma respuesta; escribirlo dos veces es la
    divergencia que la Regla 0 prohíbe.

    Por qué existe la distinción: `sin_diferencias` exigía cero incomparables de
    cualquier clase, y `AV1` no tiene almacén WMS y **nunca lo va a tener**
    (`CLAUDE.md` la declara bodega de servicio). Cada avería la ponía en
    `bodegas_siesa_sin_almacen_wms`, o sea que la casilla de la Fase 4 de
    `docs/arranque_produccion.md` —la luz verde para que los operarios
    arranquen— quedaba en ámbar **estructuralmente**. Una casilla que no se
    puede poner en verde deja de mirarse, y lo que se deja de mirar no avisa de
    nada: el ámbar permanente no es conservador, es ruido.

    Lo que sí tiene que descalificar es el incomparable **que nadie previó** —
    una bodega de Siesa con saldo que no está en esta lista. Ahí «no sé» sigue
    sin ser «está bien».

    Exigir las DOS condiciones —estar en `_BODEGAS_SERVICIO` y tener
    contraparte declarada— no es redundante: es lo que impide que agregar una
    bodega a la lista de descarga la exima de paso del veredicto. Eximir cuesta
    escribir por qué.
    """
    if bodega not in _BODEGAS_SERVICIO:
        return None
    if _contraparte_declarada(bodega) is None:
        return None
    return _JUSTIFICACION_SIN_ALMACEN.get(bodega)


def _contraparte_declarada(bodega: str):
    """Dónde guarda el WMS lo que Siesa tiene en una bodega de servicio.

    `AV1`: el WMS deja la avería en una ubicación `AVERIADOS` **dentro de NB1**,
    así que la contraparte existe pero no es un almacén — se declara medida y
    con `comparable: False`. Sin esto, la avería producía un `WMS_MAYOR`
    permanente en NB1 que nadie sabía explicar.

    `TRA1`: no hay contraparte. El STS descuenta del origen y el ETS acredita en
    el destino; lo que quede en tránsito es el limbo, y decir que «cuadra» sería
    justo lo contrario de detectarlo.
    """
    if bodega == connekta.bodega_averias or bodega == 'AV1':
        medida = _cuarentena_wms()
        return {
            'tipo': 'UBICACION',
            'descripcion': f'Ubicación AVERIADOS (cuarentena) dentro de {connekta.bodega}',
            'comparable': False,
            **medida,
        }
    if bodega == connekta.bodega_transito or bodega == 'TRA1':
        return {
            'tipo': None,
            'descripcion': 'Sin contraparte en el WMS — mercancía en tránsito',
            'comparable': False,
        }
    return None


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
            # Liberar la conexión DB antes del HTTP download (puede tardar 2+ min).
            # Sin este commit, la sesión retiene la conexión del pool durante toda la descarga
            # bloqueando requests concurrentes en un pool pequeño (Railway: 5-10 conexiones).
            db.session.commit()

            # Se descarga el diccionario POR BODEGA, no la vista aplastada de
            # `connekta.bodega`: el lado de Siesa ya venía por bodega y se
            # aplastaba justo antes de comparar. Forzado, para no comparar el
            # WMS de ahora contra un cache de la carga inicial de hace un rato.
            inventario_por_bodega = _descargar_inventario_multibodega_para_reconciliar()

            # El guard de «WMS sin stock mapeado» vive DENTRO del cálculo, así
            # que ahora se evalúa después de la descarga y no antes. Es a
            # propósito: la política de cuándo abortar se escribe una sola vez
            # (Regla 0, corolario). El costo es una descarga en un estado que
            # solo ocurre antes de la primera carga inicial — y esa descarga
            # deja el cache caliente, no se tira.
            _estado_reconciliacion['ultimo_resultado'] = _calcular_reconciliacion(
                inventario_por_bodega)
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
    return {
        'en_curso': _estado_setup['en_curso'],
        'fase': _estado_setup['fase'],
        'ultimo_inicio': _estado_setup['ultimo_inicio'].isoformat() if _estado_setup['ultimo_inicio'] else None,
        # En memoria — se pierden al reiniciar. Ver docstring.
        'resultado_catalogo': _cat,
        'resultado_stock': _estado_carga['ultimo_resultado'],
        'ultimo_error': _estado_setup['ultimo_error'],
        # En la base — sobreviven al deploy. ESTO es lo que hay que mirar.
        'persistido': {
            'catalogo': _reg.estado_persistido('catalogo', bool(_cat)),
            'barcodes': _reg.estado_persistido('barcodes'),
            'stock': _reg.estado_persistido('stock', bool(_estado_carga['ultimo_resultado'])),
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
