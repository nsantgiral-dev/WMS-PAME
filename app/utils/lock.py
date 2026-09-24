"""
Advisory locks de PostgreSQL: el ÚNICO sitio del repo que los toma.

Dos formas, y no son intercambiables:

  · `advisory_lock(clave)` — lock de **sesión**, no espera. Para crons y
    barridos: «si otro worker ya lo está corriendo, me salto este ciclo».
    Sostenido en una conexión **dedicada**, no en `db.session`.
  · `lock_de_transaccion(clave)` — lock de **transacción**, espera. Para
    serializar un «leer y después insertar» dentro de la transacción en curso
    de `db.session`; se suelta solo en el commit o el rollback.

Toda clave sale del registro de abajo. Ver `tests/test_advisory_locks.py`.

────────────────────────────────────────────────────────────────────────────
Por qué el lock de sesión va en una conexión dedicada (verificado 2026-09-23)
────────────────────────────────────────────────────────────────────────────

Un lock de sesión vive en la CONEXIÓN. `db.session` no tiene una conexión: la
pide al pool al empezar cada transacción y la **devuelve en cada commit**. El
patrón que había en todos los locks de cron (14 escritos a mano y 6 por la
versión anterior de este helper) —`pg_try_advisory_lock` por `db.session`, trabajo
con commits, `pg_advisory_unlock` por `db.session` en el `finally`— hacía esto,
medido contra un PostgreSQL 17 real con el pool que usa producción:

    toma el lock          → conexión pid 9668
    commit del trabajo    → 9668 vuelve al pool CON el lock
    unlock del finally    → sale por pid 9669 → devuelve false, no libera nada
    siguiente ciclo       → cae en 9669: lock ocupado → «otro worker ya lo
                            ejecuta» → return. En silencio.

La fuga dura hasta que el pool recicla esa conexión (`pool_recycle`, 30 min), y
mientras tanto el lock **tampoco excluye**: cualquier sesión que saque 9668 del
pool lo vuelve a tomar (los locks de sesión son reentrantes), así que dos
corridas del mismo job pueden solaparse. Las dos garantías del lock se caían a
la vez: se saltaba corridas que debía hacer y dejaba pasar las que debía frenar.

Lo mismo le pasaba al `pg_try_advisory_xact_lock` de la DLQ por el otro lado:
un lock de transacción se suelta en el primer commit, y la DLQ comitea por
cada job — desde el segundo job en adelante, la exclusión ya no existía.

Una conexión dedicada, en AUTOCOMMIT, no participa de los commits de nadie: el
lock se toma y se suelta en la misma conexión, pase lo que pase en la sesión.
Y si el unlock falla, la conexión se **invalida** (se cierra de verdad): cerrar
la sesión de PostgreSQL suelta todos sus locks. Nunca vuelve al pool tomada.

En SQLite (tests) no hay advisory locks: `advisory_lock` concede sin tocar la
base. No es un atajo — el pool de SQLite en memoria es una sola conexión
compartida (StaticPool), y devolver una «conexión dedicada» ahí haría rollback
de la transacción de la sesión.
"""
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Registro único de claves
#
# Dos jobs distintos con la misma clave se vuelven mutuamente excluyentes sin
# que nadie lo haya querido, y el log no distingue «otro worker corre esto
# mismo» de «un job que no tiene nada que ver lo tiene». Ya pasó cuatro veces con
# números elegidos a mano, cada uno mirando solo los que conocía:
#   · 2015 — abc zombis y el barrido de reposición (arreglado 2026-08-27)…
#   · …que se mudó a 2016, que ya era del barrido de avisos de flota;
#   · 2007 — la DLQ y la alerta de rutas sin liquidar;
#   · 3000 + almacén (watchdog ABC) pisaba 3001/3002/3003 (códigos de LPN,
#     de TareaReposicion y la recepción por OC) para los almacenes 1, 2 y 3.
# El primero se arregló el 2026-08-27; los otros tres seguían vivos el
# 2026-09-23. Por eso las claves viven acá y en ningún otro sitio:
# `tests/test_advisory_locks.py` rechaza un número suelto en una llamada, un
# choque entre dos entradas y una fija que caiga dentro de un rango.
#
# Al agregar una: nombre nuevo, número nuevo, y el dueño en el comentario.
# ──────────────────────────────────────────────────────────────────────────────

LOCK_RECONCILIACION_INVENTARIO = 1003   # inventario_siesa_service._run_reconciliacion
LOCK_CARGA_INVENTARIO_SIESA = 2002      # inventario_siesa_service._run_carga_inicial
LOCK_ABC_SCHEDULER = 2003               # abc_service (job diario ABC)
LOCK_ALERTA_HUERFANAS = 2004            # alertas_service.verificar_y_alertar_huerfanas
LOCK_ALERTA_STOCK_CRITICO = 2005        # alertas_service.verificar_y_alertar_stock_critico
LOCK_SYNC_EMPAQUES = 2006               # empaques_sync_service._run_sync
LOCK_DLQ = 2007                         # siesa_job_service (DLQ, cada minuto)
LOCK_SYNC_PEDIDOS = 2008                # pedidos_sync_service._run_sync
LOCK_SYNC_UBICACIONES = 2009            # ubicaciones_sync_service._run_sync
LOCK_MONITOR_TRASLADOS = 2010           # traslado_monitor_service.check_pending_transfers
LOCK_RESUMEN_DIARIO = 2011              # alertas_service.enviar_resumen_diario
LOCK_SYNC_CATALOGO = 2012               # siesa_sync_service._run_sync
LOCK_SYNC_BARRAS = 2013                 # siesa_barcode_sync_service._run_sync
LOCK_RECONCILIACION_SWEEP = 2014        # reconciliacion_service._ejecutar_sweep
LOCK_LIBERAR_ZOMBIS_CONTEO = 2015       # abc_service._liberar_zombis
LOCK_FLOTA_AVISOS = 2016                # flota/adaptadores/avisos (dueño original del 2016)
LOCK_FLOTA_PREVENTIVO = 2017            # flota/adaptadores/preventivo
LOCK_REPOSICION_BARRIDO = 2018          # reposicion_service._barrido_stock_picking (era 2016)
LOCK_ALERTA_RUTAS_SIN_LIQUIDAR = 2019   # alertas_service (era 2007, el de la DLQ)
LOCK_FOTOS_SIESA = 2020                 # fotos_siesa_service (fotos diarias de Siesa, m034fotos)

# De transacción (`lock_de_transaccion`): serializan un «leer y después insertar».
LOCK_CODIGO_LPN = 3001                  # LPN.generar_codigo
LOCK_CODIGO_TAREA_REPOSICION = 3002     # TareaReposicion.generar_codigo
LOCK_RECEPCION_POR_OC = 3003            # recepcion_service.crear_recepcion

# Rangos: (base, tamaño). La clave es `clave_en_rango(RANGO_X, n)`, 0 ≤ n < tamaño.
RANGO_CUPO_CONTEO = (4000, 1000)        # conteo_politica.bloquear_cupo, n = almacen_id
RANGO_WATCHDOG_ABC = (5000, 1000)       # abc_service watchdog, n = almacen_id (era 3000+)
# Hash de un documento: fuera de int32 para que ningún hash pueda caer sobre una
# clave fija. Con 3000/4000 + crc32 en [0, 2^31) el choque era improbable pero
# posible; con este rango es imposible.
RANGO_PEDIDO_CHICO = (1 << 32, 1 << 31)  # mobile_service, n = crc32(documento)


def _claves_fijas() -> dict:
    return {n: v for n, v in globals().items()
            if n.startswith('LOCK_') and isinstance(v, int)}


def _rangos() -> dict:
    return {n: v for n, v in globals().items()
            if n.startswith('RANGO_') and isinstance(v, tuple)}


def clave_en_rango(rango: tuple, n: int) -> int:
    """La clave `n` dentro de un rango declarado. Levanta si se sale."""
    base, tamano = rango
    if rango not in _rangos().values():
        raise ValueError(f'rango de advisory lock no declarado: {rango!r}')
    if not 0 <= int(n) < tamano:
        raise ValueError(f'{n} fuera del rango de advisory lock {rango!r}')
    return base + int(n)


def _validar_clave(clave: int) -> None:
    """Una clave que no sale del registro no se toma — ni en tests."""
    if clave in _claves_fijas().values():
        return
    if any(b <= clave < b + t for b, t in _rangos().values()):
        return
    raise ValueError(
        f'advisory lock {clave} no está en el registro de app/utils/lock.py. '
        'Declararlo ahí es lo que impide que dos jobs compartan número.')


# ──────────────────────────────────────────────────────────────────────────────
# Lock de sesión, en conexión dedicada
# ──────────────────────────────────────────────────────────────────────────────

class LockDeSesion:
    """Un lock de sesión tomado (o no) en su propia conexión.

    `tomado` dice si se consiguió. `liberar()` es idempotente y no levanta:
    va en un `finally`, donde el error que importa es el del trabajo.
    """

    def __init__(self, clave: int, etiqueta: str = ''):
        self.clave = clave
        self.nombre = etiqueta or str(clave)
        self.tomado = False
        self._conn = None

    def __bool__(self):
        return self.tomado

    def _tomar(self):
        from sqlalchemy import text

        from app.extensions import db

        _validar_clave(self.clave)
        if db.engine.dialect.name != 'postgresql':
            self.tomado = True          # sin advisory locks: ver docstring del módulo
            return self

        conn = db.engine.connect()
        try:
            conn.execution_options(isolation_level='AUTOCOMMIT')
            self.tomado = bool(conn.execute(
                text('SELECT pg_try_advisory_lock(:k)'), {'k': self.clave}).scalar())
        except Exception:
            # No sabemos en qué estado quedó: se cierra de verdad, no al pool.
            _invalidar(conn)
            conn.close()
            raise
        if self.tomado:
            self._conn = conn
        else:
            conn.close()                # nada que soltar: vuelve al pool limpia
        return self

    def liberar(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        from sqlalchemy import text
        try:
            soltado = conn.execute(
                text('SELECT pg_advisory_unlock(:k)'), {'k': self.clave}).scalar()
            if not soltado:
                logger.error(
                    '[LOCK] pg_advisory_unlock(%s) devolvió false en la conexión '
                    'que lo tomó — se cierra la conexión para soltarlo', self.nombre)
                _invalidar(conn)
            else:
                # La conexión es de este lock y de nadie más: soltar todo lo
                # de la sesión no puede tocar un lock ajeno, y garantiza que
                # vuelva limpia al pool aunque se haya tomado dos veces
                # (reentrante: un unlock baja el contador, no lo pone en cero).
                conn.execute(text('SELECT pg_advisory_unlock_all()'))
        except Exception as e:
            # Ruidoso y con salida: cerrar la sesión de PostgreSQL suelta sus
            # locks. Lo que no puede pasar es que vuelva al pool tomada.
            logger.error('[LOCK] no se pudo liberar el advisory lock %s: %s — '
                         'se cierra la conexión para soltarlo', self.nombre, e)
            _invalidar(conn)
        finally:
            try:
                conn.close()
            except Exception as e:
                logger.error('[LOCK] cerrando la conexión del lock %s: %s', self.nombre, e)


def _invalidar(conn) -> None:
    try:
        conn.invalidate()
    except Exception as e:
        logger.error('[LOCK] no se pudo invalidar la conexión: %s', e)


def tomar_lock_de_sesion(clave: int, etiqueta: str = '') -> LockDeSesion:
    """Para los sitios con `try/finally` propio: `l = tomar_lock_de_sesion(K)`,
    `if not l: return`, y `l.liberar()` en el `finally`. Si no se tomó, no hay
    conexión que devolver: `liberar()` no hace nada."""
    return LockDeSesion(clave, etiqueta)._tomar()


@contextmanager
def advisory_lock(clave: int, etiqueta: str = ''):
    """Toma un advisory lock de sesión y lo libera pase lo que pase.

        with advisory_lock(LOCK_X, 'etiqueta') as tomado:
            if not tomado:
                return          # otro worker lo tiene: no es un error
            ...trabajo, con los commits que haga falta...

    Rinde `True` si lo consiguió, `False` si otro proceso lo tiene. **No levanta
    cuando no lo consigue**: que otro worker esté corriendo el mismo job es el
    caso normal con varios workers de Gunicorn, no una falla.
    """
    lock = tomar_lock_de_sesion(clave, etiqueta)
    try:
        yield lock.tomado
    finally:
        lock.liberar()


# ──────────────────────────────────────────────────────────────────────────────
# Lock de transacción
# ──────────────────────────────────────────────────────────────────────────────

def lock_de_transaccion(clave: int) -> None:
    """`pg_advisory_xact_lock` sobre la transacción en curso de `db.session`.

    **Espera** (hasta `lock_timeout`) en vez de rendirse, y se suelta en el
    próximo commit o rollback de la sesión. Sirve para un «leer y después
    insertar» que cabe en UNA transacción. No sirve para un job que comitea
    por el camino: el primer commit lo suelta —fue el defecto de la DLQ—.
    Para eso, `advisory_lock`.
    """
    from sqlalchemy import text

    from app.extensions import db

    _validar_clave(clave)
    db.session.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': clave})


__all__ = ['advisory_lock', 'tomar_lock_de_sesion', 'lock_de_transaccion',
           'clave_en_rango', 'LockDeSesion']
