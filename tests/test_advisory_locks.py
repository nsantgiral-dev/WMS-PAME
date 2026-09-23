"""
Advisory locks: una sola forma de tomarlos, un solo registro de claves.

Tres defectos de la misma familia, todos silenciosos:

1. **El lock que no se libera** (2026-08-04). Los locks de sesión de
   PostgreSQL viven en la CONEXIÓN. 2014 y 2015 se tomaban y no se soltaban
   nunca: la conexión volvía al pool tomada y el job dejaba de correr.

2. **El lock que se suelta por la conexión equivocada** (2026-09-23). Los 18
   restantes sí liberaban en un `finally`… por `db.session`, que devuelve su
   conexión al pool en cada commit. Medido contra PostgreSQL 17 real con el
   pool de producción: toma en el pid 9668, el commit del trabajo devuelve 9668
   al pool CON el lock, el unlock sale por 9669 y devuelve false. El ciclo que
   cae en 9669 se salta («otro worker ya lo ejecuta»), el que cae en 9668 lo
   retoma aunque otro esté corriendo. Ver `TestContraPostgresReal`, que lo
   reproduce. La DLQ, con un lock de TRANSACCIÓN, lo perdía en el primer commit.

3. **Dos jobs, un número** (2026-08-27 y 2026-09-23). 2015 zombis/reposición;
   después 2016 reposición/avisos de flota, 2007 DLQ/alerta de rutas, y el
   watchdog ABC (3000 + almacén) sobre 3001-3003. Un número elegido a mano
   mirando solo los que uno conoce.

La política: `app/utils/lock.py` es el único sitio con SQL de advisory locks,
y el único sitio con números. Todo detector de este archivo es por AST.
"""
import ast
import os
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

RAIZ = Path(__file__).resolve().parents[1]
_HELPER = RAIZ / 'app' / 'utils' / 'lock.py'
_PAQUETES = ('app', 'flota')

#: Funciones del helper que reciben una clave como primer argumento.
_TOMADORES = {'advisory_lock', 'tomar_lock_de_sesion', 'lock_de_transaccion',
              'LockDeSesion'}

#: Sitios FUERA de `app/utils/lock.py` que pueden escribir SQL de advisory
#: locks, con su motivo. Vacío a propósito: no crece sin una decisión escrita.
EXCEPCIONES_SQL = {}


# ──────────────────────────────────────────────────────────────────────────────
# Escáneres (AST)
# ──────────────────────────────────────────────────────────────────────────────

def _fuentes():
    for paquete in _PAQUETES:
        for f in sorted((RAIZ / paquete).rglob('*.py')):
            yield f, f.read_text(encoding='utf-8')


def _nodos_docstring(arbol):
    """Los Constant que son docstrings. Un docstring que CITA el SQL no lo
    ejecuta — la primera versión de esta auditoría acusó por eso a
    `disparar_dlq_inmediato`."""
    docs = set()
    for n in ast.walk(arbol):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            cuerpo = getattr(n, 'body', [])
            if (cuerpo and isinstance(cuerpo[0], ast.Expr)
                    and isinstance(cuerpo[0].value, ast.Constant)
                    and isinstance(cuerpo[0].value.value, str)):
                docs.add(id(cuerpo[0].value))
    return docs


def sql_de_advisory(fuente: str):
    """Líneas donde el código (no un docstring) escribe `pg_…advisory…(`.

    Cubre las tres escrituras que había en el repo: `text('SELECT pg_…(:k)')`,
    `db.text('SELECT pg_…(2003)')` y la f-string `f'SELECT pg_…({clave})'`, cuya
    parte constante es `'SELECT pg_…('` (sin el paréntesis de cierre).
    """
    import re
    patron = re.compile(r'pg_\w*advisory\w*\s*\(')
    arbol = ast.parse(fuente)
    docs = _nodos_docstring(arbol)
    lineas = []
    for n in ast.walk(arbol):
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in docs and patron.search(n.value)):
            lineas.append(n.lineno)
    return sorted(set(lineas))


def _nombre_llamado(call):
    f = call.func
    return f.id if isinstance(f, ast.Name) else getattr(f, 'attr', None)


def _es_clave_registrada(nodo, asignaciones):
    """¿El argumento sale del registro? `LOCK_X`, `lock.LOCK_X`,
    `clave_en_rango(RANGO_X, n)`, o un nombre local asignado desde esto."""
    if isinstance(nodo, ast.Name) and nodo.id.startswith('LOCK_'):
        return True
    if isinstance(nodo, ast.Attribute) and nodo.attr.startswith('LOCK_'):
        return True
    if isinstance(nodo, ast.Call) and _nombre_llamado(nodo) == 'clave_en_rango':
        rango = nodo.args[0] if nodo.args else None
        return (isinstance(rango, ast.Name) and rango.id.startswith('RANGO_')) or \
               (isinstance(rango, ast.Attribute) and rango.attr.startswith('RANGO_'))
    if isinstance(nodo, ast.Name) and nodo.id in asignaciones:
        return all(_es_clave_registrada(v, {}) for v in asignaciones[nodo.id])
    return False


def _nodos_propios(fn):
    """Los nodos de `fn` sin los de sus funciones anidadas: cada función se
    evalúa una vez, con sus propias asignaciones."""
    pila, out = list(ast.iter_child_nodes(fn)), []
    while pila:
        n = pila.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        out.append(n)
        pila.extend(ast.iter_child_nodes(n))
    return out


def _funciones(arbol):
    return [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def claves_sueltas(fuente: str):
    """Llamadas a un tomador cuyo primer argumento NO sale del registro."""
    malas, total = [], 0
    for fn in _funciones(ast.parse(fuente)):
        propios = _nodos_propios(fn)
        asignaciones = {}
        for n in propios:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        asignaciones.setdefault(t.id, []).append(n.value)
        for n in propios:
            if isinstance(n, ast.Call) and _nombre_llamado(n) in _TOMADORES and n.args:
                total += 1
                if not _es_clave_registrada(n.args[0], asignaciones):
                    malas.append((n.lineno, ast.unparse(n.args[0])))
    return sorted(malas), total


def tomas_sin_liberar(fuente: str):
    """Funciones que llaman `tomar_lock_de_sesion` sin un `finally` que llame
    `.liberar()`. `with advisory_lock(...)` no cuenta acá: libera solo."""
    malas, total = [], 0
    for fn in _funciones(ast.parse(fuente)):
        propios = _nodos_propios(fn)
        if not any(isinstance(n, ast.Call) and _nombre_llamado(n) == 'tomar_lock_de_sesion'
                   for n in propios):
            continue
        total += 1
        libera = any(
            isinstance(t, ast.Try) and t.finalbody and any(
                isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == 'liberar'
                for s in t.finalbody for c in ast.walk(s))
            for t in propios)
        if not libera:
            malas.append(fn.name)
    return malas, total


def choques_del_registro(fijas: dict, rangos: dict):
    """Dos nombres con el mismo número, una fija dentro de un rango, o dos
    rangos que se pisan."""
    choques = []
    por_valor = {}
    for nombre, v in fijas.items():
        por_valor.setdefault(v, []).append(nombre)
    choques += [f'{v}: {sorted(ns)}' for v, ns in por_valor.items() if len(ns) > 1]
    for nombre, v in fijas.items():
        for rn, (b, t) in rangos.items():
            if b <= v < b + t:
                choques.append(f'{nombre}={v} cae dentro de {rn}')
    items = sorted(rangos.items(), key=lambda kv: kv[1][0])
    for (n1, (b1, t1)), (n2, (b2, _)) in zip(items, items[1:]):
        if b1 + t1 > b2:
            choques.append(f'{n1} se pisa con {n2}')
    return choques


def nombres_reasignados(fuente: str):
    """Un `LOCK_X = …` escrito dos veces: el segundo pisa al primero en
    silencio y el choque de valores ya no se ve en el módulo importado."""
    vistos, repetidos = set(), []
    for n in ast.parse(fuente).body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.startswith(('LOCK_', 'RANGO_')):
                    if t.id in vistos:
                        repetidos.append(t.id)
                    vistos.add(t.id)
    return repetidos


# ──────────────────────────────────────────────────────────────────────────────
# El repo
# ──────────────────────────────────────────────────────────────────────────────

class TestNingunSQLDeAdvisoryFueraDelHelper:

    def test_solo_el_helper_escribe_sql_de_advisory(self):
        fuera = {}
        for f, src in _fuentes():
            if f == _HELPER:
                continue
            lineas = sql_de_advisory(src)
            rel = str(f.relative_to(RAIZ))
            if lineas and rel not in EXCEPCIONES_SQL:
                fuera[rel] = lineas
        assert not fuera, (
            '\nSQL de advisory lock fuera de app/utils/lock.py:\n'
            + '\n'.join(f'  · {k}: líneas {v}' for k, v in fuera.items())
            + '\n\nUn lock de sesión por db.session se suelta por la conexión '
              'equivocada después de cualquier commit. Usar advisory_lock / '
              'tomar_lock_de_sesion / lock_de_transaccion.')

    def test_las_excepciones_dicen_por_que_y_siguen_existiendo(self):
        for rel, motivo in EXCEPCIONES_SQL.items():
            assert motivo and len(motivo) > 20, f'{rel}: excepción sin motivo'
            assert sql_de_advisory((RAIZ / rel).read_text(encoding='utf-8')), (
                f'{rel} ya no escribe SQL de advisory: sacarlo de EXCEPCIONES_SQL')

    def test_piso_el_escaner_ve_el_helper(self):
        """Si el escáner se rompe devuelve cero, y cero se lee como «limpio»."""
        lineas = sql_de_advisory(_HELPER.read_text(encoding='utf-8'))
        assert len(lineas) >= 3, f'el escáner ve {len(lineas)} sitios en lock.py'


class TestTodaClaveSaleDelRegistro:

    def test_ninguna_llamada_usa_un_numero_suelto(self):
        sueltas, total = {}, 0
        for f, src in _fuentes():
            if f == _HELPER:
                continue
            malas, n = claves_sueltas(src)
            total += n
            if malas:
                sueltas[str(f.relative_to(RAIZ))] = malas
        assert not sueltas, (
            '\nClaves de advisory lock que no salen del registro de '
            'app/utils/lock.py:\n'
            + '\n'.join(f'  · {k}: {v}' for k, v in sueltas.items())
            + '\n\nAsí nacieron 2015, 2016 y 2007 compartidos: cada número '
              'elegido mirando solo los que uno conocía.')
        assert total >= 24, f'el escáner ve solo {total} llamadas — ¿se rompió?'

    def test_el_registro_no_tiene_choques(self):
        from app.utils import lock
        choques = choques_del_registro(lock._claves_fijas(), lock._rangos())
        assert not choques, '\nChoques en el registro de locks:\n' + '\n'.join(choques)

    def test_ningun_nombre_del_registro_se_escribe_dos_veces(self):
        assert not nombres_reasignados(_HELPER.read_text(encoding='utf-8'))

    def test_los_choques_de_hoy_no_vuelven(self):
        from app.utils import lock as L
        assert L.LOCK_REPOSICION_BARRIDO != L.LOCK_FLOTA_AVISOS
        assert L.LOCK_ALERTA_RUTAS_SIN_LIQUIDAR != L.LOCK_DLQ
        watchdog = {L.clave_en_rango(L.RANGO_WATCHDOG_ABC, a) for a in range(1, 20)}
        assert not watchdog & {L.LOCK_CODIGO_LPN, L.LOCK_CODIGO_TAREA_REPOSICION,
                               L.LOCK_RECEPCION_POR_OC}


class TestTodoLockDeSesionSeLibera:

    def test_toda_toma_tiene_su_finally(self):
        malas, total = [], 0
        for f, src in _fuentes():
            if f == _HELPER:
                continue
            m, n = tomas_sin_liberar(src)
            total += n
            malas += [f'{f.relative_to(RAIZ)}:{x}' for x in m]
        assert not malas, (
            '\nToman un lock de sesión sin un finally que lo libere:\n'
            + '\n'.join(f'  · {m}' for m in malas))
        assert total >= 14, f'el escáner ve solo {total} funciones — ¿se rompió?'

    @pytest.mark.parametrize('archivo,funcion', [
        ('app/services/abc_service.py', '_liberar_zombis'),
        ('app/services/reconciliacion_service.py', '_ejecutar_sweep'),
    ])
    def test_los_dos_de_agosto_siguen_con_with(self, archivo, funcion):
        """2014 y 2015 no se liberaban nunca (2026-08-04)."""
        arbol = ast.parse((RAIZ / archivo).read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == funcion)
        assert any(isinstance(w, ast.With) and any(
            isinstance(i.context_expr, ast.Call)
            and _nombre_llamado(i.context_expr) == 'advisory_lock' for i in w.items)
            for w in ast.walk(fn))


# ──────────────────────────────────────────────────────────────────────────────
# Los detectores muerden (meta-tests)
# ──────────────────────────────────────────────────────────────────────────────

class TestLosDetectoresMuerden:

    @pytest.mark.parametrize('codigo', [
        "def f():\n    db.session.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': 1})\n",
        "def f():\n    db.session.execute(db.text('SELECT pg_advisory_unlock(2003)'))\n",
        "def f(k):\n    db.session.execute(db.text(f'SELECT pg_try_advisory_lock({k})'))\n",
        "def f():\n    db.session.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': 1})\n",
        "def f():\n    s.execute(text('SELECT pg_try_advisory_xact_lock(:k)'))\n",
    ])
    def test_sql_crudo_se_ve(self, codigo):
        assert sql_de_advisory(codigo)

    def test_un_docstring_que_lo_cita_no_es_sql(self):
        codigo = ('def f():\n'
                  '    """el lock pg_try_advisory_lock(1) protege esto"""\n'
                  '    return 1\n')
        assert sql_de_advisory(codigo) == []

    @pytest.mark.parametrize('arg', ['2016', '3000 + almacen_id', 'clave', "cfg['k']"])
    def test_un_numero_suelto_se_ve(self, arg):
        codigo = f'def f(almacen_id, clave, cfg):\n    with advisory_lock({arg}) as t:\n        pass\n'
        malas, total = claves_sueltas(codigo)
        assert total == 1 and malas, arg

    @pytest.mark.parametrize('codigo', [
        'def f():\n    with advisory_lock(LOCK_DLQ) as t:\n        pass\n',
        'def f():\n    l = tomar_lock_de_sesion(lock.LOCK_DLQ)\n',
        'def f(a):\n    lock_de_transaccion(clave_en_rango(RANGO_CUPO_CONTEO, a))\n',
        'def f(d):\n    k = clave_en_rango(RANGO_PEDIDO_CHICO, crc(d))\n'
        '    with advisory_lock(k, "x") as t:\n        pass\n',
    ])
    def test_una_clave_del_registro_no_se_marca(self, codigo):
        malas, total = claves_sueltas(codigo)
        assert total == 1 and not malas

    def test_un_rango_no_declarado_se_ve(self):
        codigo = 'def f(a):\n    lock_de_transaccion(clave_en_rango((9000, 10), a))\n'
        assert claves_sueltas(codigo)[0]

    def test_una_toma_sin_finally_se_ve(self):
        codigo = ('def f():\n'
                  '    l = tomar_lock_de_sesion(LOCK_DLQ)\n'
                  '    if not l:\n        return\n'
                  '    trabajo()\n'
                  '    l.liberar()\n')
        assert tomas_sin_liberar(codigo) == (['f'], 1)

    def test_una_toma_con_finally_no_se_marca(self):
        codigo = ('def f():\n'
                  '    l = tomar_lock_de_sesion(LOCK_DLQ)\n'
                  '    try:\n        trabajo()\n'
                  '    finally:\n        l.liberar()\n')
        assert tomas_sin_liberar(codigo) == ([], 1)

    def test_los_choques_del_registro_se_ven(self):
        assert choques_del_registro({'A': 2016, 'B': 2016}, {})
        assert choques_del_registro({'A': 3001}, {'W': (3000, 1000)})
        assert choques_del_registro({}, {'W': (3000, 1000), 'C': (3500, 10)})
        assert not choques_del_registro({'A': 2016, 'B': 2017},
                                        {'W': (5000, 1000), 'C': (4000, 1000)})

    def test_un_nombre_reasignado_se_ve(self):
        assert nombres_reasignados('LOCK_A = 1\nLOCK_B = 2\nLOCK_A = 3\n') == ['LOCK_A']


# ──────────────────────────────────────────────────────────────────────────────
# El helper, sin PostgreSQL
# ──────────────────────────────────────────────────────────────────────────────

class _ConexionFalsa:
    """Registra qué se ejecutó en ELLA — la propiedad es «misma conexión»."""

    def __init__(self, tomar=True, soltar=True, falla_al_soltar=False):
        self.sql, self.cerrada, self.invalidada = [], False, False
        self._tomar, self._soltar, self._falla = tomar, soltar, falla_al_soltar

    def execution_options(self, **kw):
        self.opciones = kw
        return self

    def execute(self, stmt, params=None):
        s = str(stmt)
        self.sql.append(s)
        r = MagicMock()
        if 'unlock' in s:
            if self._falla:
                raise RuntimeError('conexión caída')
            r.scalar.return_value = self._soltar
        else:
            r.scalar.return_value = self._tomar
        return r

    def invalidate(self):
        self.invalidada = True

    def close(self):
        self.cerrada = True


@pytest.fixture
def motor_pg_falso(app):
    """`db.engine` con dialecto postgresql que entrega `_ConexionFalsa`s."""
    from app.extensions import db
    conexiones = []
    motor = MagicMock()
    motor.dialect.name = 'postgresql'

    def _connect():
        c = conexiones.pop(0)
        motor.entregadas.append(c)
        return c
    motor.entregadas = []
    motor.connect.side_effect = _connect
    with app.app_context(), \
            patch.object(type(db), 'engine', new_callable=PropertyMock, return_value=motor), \
            patch.object(db, 'session', MagicMock()) as sesion:
        yield conexiones, motor, sesion


class TestElHelper:

    def test_una_clave_fuera_del_registro_no_se_toma(self, app):
        from app.utils.lock import advisory_lock, lock_de_transaccion
        with app.app_context():
            with pytest.raises(ValueError):
                with advisory_lock(9999):
                    pass
            with pytest.raises(ValueError):
                lock_de_transaccion(9999)

    def test_clave_en_rango_se_queda_en_el_rango(self):
        from app.utils.lock import RANGO_WATCHDOG_ABC, clave_en_rango
        assert clave_en_rango(RANGO_WATCHDOG_ABC, 3) == 5003
        with pytest.raises(ValueError):
            clave_en_rango(RANGO_WATCHDOG_ABC, 1000)
        with pytest.raises(ValueError):
            clave_en_rango((9000, 10), 1)

    def test_en_sqlite_concede_sin_tocar_la_sesion(self, app):
        from app.extensions import db
        from app.utils.lock import LOCK_DLQ, advisory_lock
        with app.app_context(), patch.object(db, 'session', MagicMock()) as sesion:
            with advisory_lock(LOCK_DLQ) as tomado:
                assert tomado is True
        assert not sesion.execute.called

    def test_toma_y_suelta_en_la_misma_conexion_no_en_la_sesion(self, motor_pg_falso):
        """La propiedad de fondo. La sesión puede comitear lo que quiera."""
        from app.utils.lock import LOCK_DLQ, advisory_lock
        conexiones, motor, sesion = motor_pg_falso
        c = _ConexionFalsa()
        conexiones.append(c)
        with advisory_lock(LOCK_DLQ) as tomado:
            assert tomado is True
            sesion.commit()
        assert any('pg_try_advisory_lock(' in s for s in c.sql)
        assert any('pg_advisory_unlock(' in s for s in c.sql), 'no se soltó en ELLA'
        assert c.opciones == {'isolation_level': 'AUTOCOMMIT'}
        assert c.cerrada and not c.invalidada
        # str() del TextClause, no del call: el repr de un call no trae el SQL
        sqls = ' '.join(str(x.args[0]) for x in sesion.execute.call_args_list if x.args)
        assert 'advisory' not in sqls, 'el lock pasó por db.session'

    def test_libera_aunque_el_trabajo_levante(self, motor_pg_falso):
        from app.utils.lock import LOCK_DLQ, advisory_lock
        conexiones, _, _ = motor_pg_falso
        c = _ConexionFalsa()
        conexiones.append(c)
        with pytest.raises(ValueError):
            with advisory_lock(LOCK_DLQ):
                raise ValueError('el trabajo falló')
        assert any('pg_advisory_unlock(' in s for s in c.sql) and c.cerrada

    def test_no_libera_lo_que_no_tomo_y_devuelve_la_conexion(self, motor_pg_falso):
        from app.utils.lock import LOCK_DLQ, advisory_lock
        conexiones, _, _ = motor_pg_falso
        c = _ConexionFalsa(tomar=False)
        conexiones.append(c)
        with advisory_lock(LOCK_DLQ) as tomado:
            assert tomado is False
        assert not any('unlock' in s for s in c.sql)
        assert c.cerrada and not c.invalidada

    @pytest.mark.parametrize('kw', [{'soltar': False}, {'falla_al_soltar': True}])
    def test_un_unlock_fallido_cierra_la_conexion_de_verdad(self, motor_pg_falso, kw):
        """Cerrar la sesión de PostgreSQL suelta sus locks. Lo único que no
        puede pasar es que vuelva al pool tomada."""
        from app.utils.lock import LOCK_DLQ, advisory_lock
        conexiones, _, _ = motor_pg_falso
        c = _ConexionFalsa(**kw)
        conexiones.append(c)
        with advisory_lock(LOCK_DLQ):
            pass
        assert c.invalidada, 'volvió al pool con el lock puesto'
        assert c.cerrada

    def test_liberar_dos_veces_no_hace_nada_la_segunda(self, motor_pg_falso):
        from app.utils.lock import LOCK_DLQ, tomar_lock_de_sesion
        conexiones, _, _ = motor_pg_falso
        c = _ConexionFalsa()
        conexiones.append(c)
        lock = tomar_lock_de_sesion(LOCK_DLQ)
        lock.liberar()
        lock.liberar()
        assert sum('pg_advisory_unlock(' in s for s in c.sql) == 1


# ──────────────────────────────────────────────────────────────────────────────
# Contra PostgreSQL real — la evidencia
# ──────────────────────────────────────────────────────────────────────────────

_HOSTS_LOCALES = {'localhost', '127.0.0.1', '::1', None, ''}


@pytest.fixture(scope='module')
def app_pg():
    """Una app mínima con el `db` real contra un PostgreSQL LOCAL, con el pool
    de producción (`app/__init__.py`: pool_size 20). Falla —no se salta— sin
    `FLOTA_TEST_PG_URL`, igual que el resto de los tests `postgres`."""
    import sqlalchemy as sa
    from flask import Flask

    from app.extensions import db

    url = os.getenv('FLOTA_TEST_PG_URL')
    if not url:
        pytest.fail('FLOTA_TEST_PG_URL no está definida (PostgreSQL local, desechable).')
    u = sa.engine.make_url(url)
    if u.get_backend_name() != 'postgresql' or u.host not in _HOSTS_LOCALES:
        pytest.fail(f'FLOTA_TEST_PG_URL debe ser un PostgreSQL local, no {u.host!r}')
    app = Flask('locks_pg')
    app.config['SQLALCHEMY_DATABASE_URI'] = url
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True, 'pool_size': 20, 'max_overflow': 10,
        # Como producción (lock_timeout 8 s): un lock bloqueante que no llega
        # falla en vez de colgar la corrida.
        'connect_args': {'options': '-c lock_timeout=2000 -c statement_timeout=10000'}}
    db.init_app(app)
    observador = sa.create_engine(url, poolclass=sa.pool.NullPool)
    yield app, db, observador
    with app.app_context():
        db.engine.dispose()
    observador.dispose()


def _tenedores(observador, clave):
    from sqlalchemy import text
    with observador.connect() as c:
        return c.execute(text(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND ((classid::bigint << 32) | objid::bigint) = :k AND objsubid = 1"),
            {'k': clave}).scalars().all()


def _pool_con_dos_conexiones(db):
    """Basta con que haya habido concurrencia una vez: el pool queda con más de
    una conexión ociosa, y la próxima que entrega no es la última que recibió."""
    a, b = db.engine.connect(), db.engine.connect()
    a.close()
    b.close()


def _pid(db):
    from sqlalchemy import text
    return db.session.execute(text('SELECT pg_backend_pid()')).scalar()


@pytest.mark.postgres
class TestContraPostgresReal:

    def test_el_patron_viejo_fugaba(self, app_pg):
        """La evidencia, reproducida: lock por db.session + commit + unlock por
        db.session. Si esto deja de fugar (otra versión de SQLAlchemy), el
        motivo de la conexión dedicada cambia — y hay que saberlo."""
        from sqlalchemy import text
        app, db, obs = app_pg
        k = 777001        # fuera del registro a propósito: es el patrón viejo
        with app.app_context():
            _pool_con_dos_conexiones(db)
            assert db.session.execute(text('SELECT pg_try_advisory_lock(:k)'), {'k': k}).scalar()
            pid_toma = _pid(db)
            db.session.commit()                         # el trabajo del cron
            pid_despues = _pid(db)
            soltado = db.session.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': k}).scalar()
            db.session.commit()
            db.session.remove()
            quedo = _tenedores(obs, k)
            # limpieza: soltarlo desde la conexión que lo tiene
            with obs.connect() as c:
                c.execute(text('SELECT pg_terminate_backend(:p)'), {'p': pid_toma})
            db.engine.dispose()
        assert pid_toma != pid_despues
        assert soltado is False
        assert quedo == [pid_toma]

    def test_el_helper_no_fuga_aunque_el_trabajo_comitee(self, app_pg):
        from app.utils.lock import LOCK_SYNC_PEDIDOS, advisory_lock
        app, db, obs = app_pg
        with app.app_context():
            _pool_con_dos_conexiones(db)
            for _ in range(3):
                with advisory_lock(LOCK_SYNC_PEDIDOS) as tomado:
                    assert tomado is True
                    _pid(db)
                    db.session.commit()
                    _pid(db)
                    db.session.commit()
                db.session.remove()
                assert _tenedores(obs, LOCK_SYNC_PEDIDOS) == []

    def test_excluye_durante_todo_el_trabajo(self, app_pg):
        """Lo que la DLQ perdía en el primer commit."""
        from sqlalchemy import text

        from app.utils.lock import LOCK_DLQ, advisory_lock
        app, db, obs = app_pg
        with app.app_context():
            _pool_con_dos_conexiones(db)
            with advisory_lock(LOCK_DLQ) as tomado:
                assert tomado
                for _ in range(3):
                    db.session.commit()
                    with obs.connect() as otro:
                        assert otro.execute(text('SELECT pg_try_advisory_lock(:k)'),
                                            {'k': LOCK_DLQ}).scalar() is False
                    # otro ciclo del mismo proceso, por el pool de la app
                    with advisory_lock(LOCK_DLQ) as segundo:
                        assert segundo is False
            db.session.remove()
            assert _tenedores(obs, LOCK_DLQ) == []

    def test_vuelve_limpia_al_pool_aunque_se_haya_tomado_dos_veces(self, app_pg):
        """Reentrante: un unlock baja el contador de 2 a 1 y devuelve true. Sin
        `pg_advisory_unlock_all` la conexión volvería al pool tomada."""
        from sqlalchemy import text

        from app.utils.lock import LOCK_SYNC_BARRAS, tomar_lock_de_sesion
        app, db, obs = app_pg
        with app.app_context():
            lock = tomar_lock_de_sesion(LOCK_SYNC_BARRAS)
            assert lock
            lock._conn.execute(text('SELECT pg_advisory_lock(:k)'), {'k': LOCK_SYNC_BARRAS})
            lock.liberar()
            assert _tenedores(obs, LOCK_SYNC_BARRAS) == []

    def test_una_conexion_muerta_no_vuelve_al_pool(self, app_pg):
        """El unlock levanta (la sesión ya no existe): no se propaga, y la
        conexión se invalida en vez de devolverse rota al pool."""
        from sqlalchemy import text

        from app.utils.lock import LOCK_SYNC_UBICACIONES, advisory_lock, tomar_lock_de_sesion
        app, db, obs = app_pg
        with app.app_context():
            lock = tomar_lock_de_sesion(LOCK_SYNC_UBICACIONES)
            conn = lock._conn
            pid = conn.execute(text('SELECT pg_backend_pid()')).scalar()
            with obs.connect() as c:
                c.execute(text('SELECT pg_terminate_backend(:p)'), {'p': pid})
            from app.utils import lock as modulo
            with patch.object(modulo, '_invalidar', wraps=modulo._invalidar) as inv:
                lock.liberar()                   # no levanta
            assert inv.call_args.args == (conn,), 'volvió al pool sin invalidar'
            assert _tenedores(obs, LOCK_SYNC_UBICACIONES) == []
            with advisory_lock(LOCK_SYNC_UBICACIONES) as tomado:
                assert tomado

    def test_lock_de_transaccion_se_suelta_en_el_commit(self, app_pg):
        from app.utils.lock import LOCK_CODIGO_LPN, lock_de_transaccion
        app, db, obs = app_pg
        with app.app_context():
            lock_de_transaccion(LOCK_CODIGO_LPN)
            assert len(_tenedores(obs, LOCK_CODIGO_LPN)) == 1
            db.session.commit()
            assert _tenedores(obs, LOCK_CODIGO_LPN) == []
            db.session.remove()
