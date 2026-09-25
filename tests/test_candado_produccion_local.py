"""
Candado anti-producción local (tanda 2 · E, 2026-09-25).

Un script local con el `DATABASE_URL` de producción arrancó los schedulers
contra producción dos segundos. La clase: **un proceso fuera de Railway que
arranca trabajo automático contra una base de Railway**. `create_app` no
arranca ningún scheduler y la DLQ no lanza su hilo automático.

Lo que el candado NO cubre: un proceso que se hace pasar por Railway
(`RAILWAY_ENVIRONMENT_NAME` puesta a mano en local) pasa; una base de
producción que no esté en Railway no se reconoce.
"""
import ast
import pathlib

import pytest

from app.utils.candado_local import base_es_de_railway, motivo_candado

_RAIZ = pathlib.Path(__file__).resolve().parent.parent
_URL_PROD = 'postgresql://u:p@metro.proxy.rlwy.net:51234/railway'


class TestLaPolitica:
    @pytest.mark.parametrize('url', [
        _URL_PROD,
        'postgres://u:p@postgres.railway.internal:5432/railway',
        'postgresql://u:p@RLWY.NET/x',
        'esto no es una url pero dice metro.proxy.rlwy.net',
    ])
    def test_local_contra_railway_bloquea(self, url):
        m = motivo_candado({'DATABASE_URL': url})
        assert m and 'CANDADO' in m

    def test_dentro_de_railway_no_bloquea(self):
        assert motivo_candado({'DATABASE_URL': _URL_PROD,
                               'RAILWAY_ENVIRONMENT_NAME': 'production'}) is None

    def test_variable_vacia_no_cuenta_como_railway(self):
        assert motivo_candado({'DATABASE_URL': _URL_PROD,
                               'RAILWAY_ENVIRONMENT_NAME': '  '})

    @pytest.mark.parametrize('url', [
        'sqlite:///:memory:', 'postgresql://u:p@localhost:5432/wms', '',
        'postgresql://u:p@notrlwy.net/x', 'postgresql://u:p@rlwy.net.ejemplo.com/x',
    ])
    def test_lo_que_no_es_railway_no_bloquea(self, url):
        assert not base_es_de_railway(url)
        assert motivo_candado({'DATABASE_URL': url}) is None


@pytest.fixture
def registro(monkeypatch):
    """Sustituye el arranque real de cada scheduler por una anotación: el test
    nunca levanta un cron (ni contra la URL falsa de Railway)."""
    import app as app_pkg
    llamados = []
    monkeypatch.setattr(app_pkg, '_registrar_scheduler',
                        lambda app, _il, _lg, mod, fn, tag, **kw: llamados.append(tag))
    monkeypatch.setenv('SYNC_SCHEDULER', 'true')
    monkeypatch.setenv('HEAVY_SCHEDULERS', 'true')
    monkeypatch.delenv('WORKER_SKIP_ESSENTIAL', raising=False)
    return llamados


class TestCreateApp:
    def test_local_con_base_de_railway_no_arranca_nada(self, registro, monkeypatch):
        import app as app_pkg
        monkeypatch.setenv('DATABASE_URL', _URL_PROD)
        monkeypatch.delenv('RAILWAY_ENVIRONMENT_NAME', raising=False)
        a = app_pkg.create_app()
        assert registro == []
        assert a.config['CANDADO_PRODUCCION_LOCAL']
        assert any('CANDADO' in x for x in a.config['SCHEDULERS_OMITIDOS'])

    def test_en_railway_arranca(self, registro, monkeypatch):
        """Lo sano: el mismo entorno, dentro de Railway, registra sus crons."""
        import app as app_pkg
        monkeypatch.setenv('DATABASE_URL', _URL_PROD)
        monkeypatch.setenv('RAILWAY_ENVIRONMENT_NAME', 'production')
        a = app_pkg.create_app()
        assert len(registro) >= 10
        assert a.config['CANDADO_PRODUCCION_LOCAL'] is None

    def test_local_con_base_local_arranca(self, registro, monkeypatch):
        import app as app_pkg
        monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
        monkeypatch.delenv('RAILWAY_ENVIRONMENT_NAME', raising=False)
        app_pkg.create_app()
        assert len(registro) >= 10


def _disparar_original():
    """La función real: la suite la reemplaza por un no-op (conftest)."""
    import inspect
    from app.services import siesa_job_service as sjs
    arbol = ast.parse(inspect.getsource(sjs))
    nodo = next(n for n in arbol.body
                if isinstance(n, ast.FunctionDef) and n.name == 'disparar_dlq_inmediato')
    ns = dict(vars(sjs))
    exec(compile(ast.Module(body=[nodo], type_ignores=[]), sjs.__file__, 'exec'), ns)
    return ns['disparar_dlq_inmediato']


class TestElHiloDeLaDlq:
    def test_con_candado_no_lanza_hilo(self, app, monkeypatch):
        import threading
        lanzados = []
        monkeypatch.setattr(threading, 'Thread',
                            lambda *a, **k: lanzados.append(k) or pytest.fail('lanzó hilo'))
        app.config['CANDADO_PRODUCCION_LOCAL'] = 'CANDADO de prueba'
        try:
            _disparar_original()(app)
        finally:
            app.config['CANDADO_PRODUCCION_LOCAL'] = None
        assert lanzados == []

    def test_sin_candado_lanza_hilo(self, app, monkeypatch):
        import threading

        class _Hilo:
            def __init__(self, *a, **k):
                lanzados.append(k.get('name'))

            def start(self):
                pass
        lanzados = []
        monkeypatch.setattr(threading, 'Thread', _Hilo)
        app.config['CANDADO_PRODUCCION_LOCAL'] = None
        _disparar_original()(app)
        assert lanzados == ['dlq-inmediato']


# ── Trinquete estructural: todo arranque está detrás del candado ───────────

def _registros_fuera_del_candado(src: str) -> tuple:
    """`(total, fuera)`: llamadas a `_registrar_scheduler` dentro de
    `create_app`, y las que NO cuelgan del `else` de `if _candado:`."""
    arbol = ast.parse(src)
    fn = next(n for n in ast.walk(arbol)
              if isinstance(n, ast.FunctionDef) and n.name == 'create_app')
    protegidas = set()
    for n in ast.walk(fn):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Name)
                and n.test.id == '_candado'):
            for hijo in n.orelse:
                for x in ast.walk(hijo):
                    protegidas.add(id(x))
    total = fuera = 0
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == '_registrar_scheduler'):
            total += 1
            if id(n) not in protegidas:
                fuera += 1
    return total, fuera


class TestTodoArranqueDetrasDelCandado:
    def test_create_app(self):
        src = (_RAIZ / 'app' / '__init__.py').read_text(encoding='utf-8')
        total, fuera = _registros_fuera_del_candado(src)
        assert total >= 3, 'el escáner no encontró los registros: se rompió'
        assert fuera == 0, f'{fuera} arranque(s) de scheduler fuera del candado'

    def test_meta_ve_un_arranque_fuera(self):
        src = ('def create_app():\n'
               '    _candado = x()\n'
               '    if _candado:\n'
               '        pass\n'
               '    else:\n'
               '        _registrar_scheduler(1)\n'
               '    _registrar_scheduler(2)\n')
        assert _registros_fuera_del_candado(src) == (2, 1)

    def test_meta_lo_sano_no_marca(self):
        src = ('def create_app():\n'
               '    if _candado:\n'
               '        pass\n'
               '    elif otra:\n'
               '        for m in l:\n'
               '            _registrar_scheduler(1)\n'
               '    # _registrar_scheduler(3) en un comentario no cuenta\n')
        assert _registros_fuera_del_candado(src) == (1, 0)
