"""
Nombre de los vendedores de Siesa — una regla, un caché.

La fuente es `papeleriamedellin_WMS_Vendedor_Contacto` (~100 filas, sin
filtro por parámetro: ver `get_vendedor_contacto`). Dos consumidores:

· Rutas la consulta en cada carga de ruta, en paralelo con las facturas, y
  cruza por **código** de vendedor (`f210_codigo_vendedor`).
· La cola de pedidos cruza por **NIT**: el pedido trae `f200_id_pedido_vend`,
  que es el NIT del vendedor, no su código (ver `get_pedido_cabecera`).

**La cola no espera a Siesa.** Lee lo que haya en el caché; si está vacío o
viejo, dispara un refresco en segundo plano y responde sin nombres. Siesa no
opera después de ~8 p. m. (Regla 14, timeout de 30 s): una pantalla que
bloquea en cada carga por un dato decorativo es peor que una que lo muestra
en la recarga siguiente. Un refresco fallido no borra lo que ya había.
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

#: Cada cuánto se vuelve a pedir la lista a Siesa. Los vendedores cambian poco.
TTL_SEGUNDOS = 30 * 60
#: Tras un fallo, cuánto esperar antes de reintentar (no martillar a Siesa
#: caída con un intento por cada carga de la cola).
ESPERA_TRAS_FALLO_SEGUNDOS = 5 * 60

_lock = threading.Lock()
_por_nit: dict = {}
_cargado_at: float = 0.0
_fallo_at: float = 0.0
_refrescando = False


def nombre_de_fila(v: dict):
    """Nombre legible de una fila de `get_vendedor_contacto`: nombres y
    apellidos; si no hay, la razón social; si tampoco, `None`."""
    nombre = ' '.join(filter(None, [
        str(v.get('f200_nombres', '') or '').strip(),
        str(v.get('f200_apellido1', '') or '').strip(),
        str(v.get('f200_apellido2', '') or '').strip(),
    ])).strip()
    return nombre or str(v.get('f200_razon_social', '') or '').strip() or None


def normalizar_nit(nit):
    """`' 53051164 '` → `'53051164'`; `'53051164-1'` → `'53051164'`."""
    s = str(nit or '').strip()
    return s.split('-')[0].strip() or None


def _refrescar():
    global _por_nit, _cargado_at, _fallo_at, _refrescando
    try:
        from app.services.connekta_gateway import connekta
        filas = connekta.get_vendedor_contacto()
        mapa = {}
        for v in filas:
            nit, nombre = normalizar_nit(v.get('f200_nit')), nombre_de_fila(v)
            if nit and nombre:
                mapa[nit] = nombre
        with _lock:
            if mapa:
                _por_nit, _cargado_at = mapa, time.monotonic()
            else:
                # `get_vendedor_contacto` devuelve [] también cuando falla:
                # vacío no reemplaza lo que ya se sabía.
                _fallo_at = time.monotonic()
    except Exception as e:                      # noqa: BLE001 — decorativo
        logger.warning('[VENDEDORES] no se pudo refrescar: %s', e)
        with _lock:
            _fallo_at = time.monotonic()
    finally:
        with _lock:
            _refrescando = False


def nombres_por_nit() -> dict:
    """`{nit: nombre}` con lo que haya en caché, **sin esperar a Siesa**.
    Si el caché está vacío o vencido, arranca un refresco en segundo plano."""
    global _refrescando
    ahora = time.monotonic()
    with _lock:
        vencido = not _por_nit or ahora - _cargado_at > TTL_SEGUNDOS
        en_espera = _fallo_at and ahora - _fallo_at < ESPERA_TRAS_FALLO_SEGUNDOS
        lanzar = vencido and not en_espera and not _refrescando
        if lanzar:
            _refrescando = True
        actual = dict(_por_nit)
    if lanzar:
        threading.Thread(target=_refrescar, name='vendedores-refresco', daemon=True).start()
    return actual


class EstadoVendedor:
    """Qué se sabe del vendedor de un pedido. **Un solo vocabulario**; la
    pantalla solo lo traduce a palabras."""
    CONOCIDO = 'CONOCIDO'            # hay nombre
    SIN_VENDEDOR = 'SIN_VENDEDOR'    # el pedido no trae, o trae el «Genérico»
    CARGANDO = 'CARGANDO'            # la lista todavía no llegó a este proceso
    NO_DISPONIBLE = 'NO_DISPONIBLE'  # la lectura FALLÓ y no hay lista previa
    DESCONOCIDO = 'DESCONOCIDO'      # la lista llegó y ese NIT no está


def _es_generico(vendedor_id) -> bool:
    return str(vendedor_id or '').strip().lower() in ('generico', 'genérico')


def lectura_fallida() -> bool:
    """¿El último intento de leer la lista falló? Solo importa sin lista:
    con nombres ya cargados, un fallo posterior no los invalida."""
    with _lock:
        return bool(_fallo_at) and _fallo_at >= _cargado_at


def resolver(vendedor_id, nombres: dict, fallida: bool = False) -> tuple:
    """`(estado, nombre)` del vendedor de un pedido. `nombres` es lo que
    devolvió `nombres_por_nit()`: vacío significa que la lista no ha llegado
    (la consulta real trae decenas; un maestro vacío no es un caso).
    `fallida` distingue «todavía no» de «falló»: el 2026-09-28 un error 500
    de Siesa se vio media hora como «cargando…» — decía que esperaba cuando
    ya había fallado."""
    if not str(vendedor_id or '').strip() or _es_generico(vendedor_id):
        return EstadoVendedor.SIN_VENDEDOR, None
    if not nombres:
        return (EstadoVendedor.NO_DISPONIBLE if fallida else EstadoVendedor.CARGANDO), None
    nombre = nombres.get(normalizar_nit(vendedor_id))
    if nombre:
        return EstadoVendedor.CONOCIDO, nombre
    return EstadoVendedor.DESCONOCIDO, None


def refrescar_ahora() -> dict:
    """Relee la lista YA, esperando la respuesta y sin la pausa tras un
    fallo. Para el administrador después de corregir la consulta en Siesa.
    **Solo refresca el proceso que atiende la llamada**: Gunicorn corre dos,
    cada uno con su caché. Por eso devuelve el `pid`."""
    import os
    global _refrescando
    with _lock:
        _refrescando = True
    _refrescar()
    with _lock:
        return {'pid': os.getpid(), 'vendedores': len(_por_nit),
                'ok': bool(_por_nit) and _cargado_at > _fallo_at}


def precalentar():
    """Arranca la lectura de la lista sin esperar a que alguien abra Pedidos.
    La llama cada worker de Gunicorn al nacer (`gunicorn.conf.py`)."""
    nombres_por_nit()


def _reiniciar_cache():
    """Solo tests."""
    global _por_nit, _cargado_at, _fallo_at, _refrescando
    with _lock:
        _por_nit, _cargado_at, _fallo_at, _refrescando = {}, 0.0, 0.0, False
