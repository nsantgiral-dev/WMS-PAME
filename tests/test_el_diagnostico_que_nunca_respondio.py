"""`GET /api/siesa/debug-cache-status` nunca devolvió 200.

    mono = _cache_inventario_siesa
    'tiene_data': mono['data'] is not None,      ← KeyError: 'data'

`_cache_inventario_siesa` es un caché **por bodega**
(`{bodega: {'data':…, 'ts':…}}`, `inventario_siesa_service.py:122`), no un
caché único. El endpoint lo leía como si fuera uno solo, así que fallaba en
los dos estados posibles: con el diccionario vacío —el de arranque— y con
bodegas adentro.

Encontrado el 2026-09-21 barriendo los 146 endpoints GET sin parámetro con
las credenciales de cada rol de producción. De los 11 que devolvían 5xx a un
admin, **diez eran la migración pendiente** (columnas que el modelo declara y
la base todavía no tiene). Este era el único defecto de código.

## Por qué sobrevivió

Es de diagnóstico: ninguna pantalla lo llama, así que el guard de rutas
huérfanas lo tiene declarado y nadie lo ejecuta. Un endpoint que solo se usa
el día que algo anda mal es exactamente el que no puede estar roto ese día —
y el costo de descubrirlo entonces es que quien lo abre concluye que el
problema es peor de lo que es.

`multibodega` sí funcionaba: **ese** caché sí tiene la forma `{data, ts}`. Dos
variables con nombres hermanos y formas distintas, y el endpoint las trataba
igual.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models.usuario import Usuario


@pytest.fixture
def admin(db):
    u = Usuario.query.filter_by(rol='admin').first()
    if not u:
        u = Usuario(nombre='Admin Cache', email='admin.cache@t.co', rol='admin')
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
    return u


def _hdr(app, u):
    with app.app_context():
        return {'Authorization': 'Bearer ' + create_access_token(identity=str(u.id))}


@pytest.fixture
def cache_limpio():
    """Deja los dos cachés como nacen y los restaura al terminar."""
    from app.services import inventario_siesa_service as inv
    mono_orig = dict(inv._cache_inventario_siesa)
    multi_orig = dict(inv._cache_inventario_multibodega)
    inv._cache_inventario_siesa.clear()
    inv._cache_inventario_multibodega.update({'data': None, 'ts': None})
    yield inv
    inv._cache_inventario_siesa.clear()
    inv._cache_inventario_siesa.update(mono_orig)
    inv._cache_inventario_multibodega.update(multi_orig)


class TestResponde:

    def test_con_el_cache_vacio(self, app, client, db, admin, cache_limpio):
        """EL test. El estado de arranque —y el que tiene cualquier proceso
        que todavía no descargó— daba `KeyError: 'data'`."""
        r = client.get('/api/siesa/debug-cache-status', headers=_hdr(app, admin))
        assert r.status_code == 200, (
            f'el endpoint de diagnóstico devuelve {r.status_code}: quien lo '
            f'abre para investigar un problema encuentra otro')
        assert r.get_json()['monobodega'] == {}

    def test_con_dos_bodegas_cargadas(self, app, client, db, admin, cache_limpio):
        """El otro estado. Con la forma vieja también reventaba: el
        diccionario tiene claves de bodega, no `'data'`."""
        from datetime import datetime
        cache_limpio._cache_inventario_siesa.update({
            'NB1': {'data': {'A': 1, 'B': 2, 'C': 3}, 'ts': datetime(2026, 9, 21)},
            'NS1': {'data': None, 'ts': None},
        })
        r = client.get('/api/siesa/debug-cache-status', headers=_hdr(app, admin))
        assert r.status_code == 200
        mono = r.get_json()['monobodega']
        assert set(mono) == {'NB1', 'NS1'}
        assert mono['NB1'] == {'tiene_data': True, 'productos': 3,
                               'ts': '2026-09-21 00:00:00'}
        assert mono['NS1']['tiene_data'] is False, (
            'una bodega sin descarga tiene que verse distinta de una con '
            'datos: el endpoint existe para contar eso')
        assert mono['NS1']['productos'] == 0

    def test_el_multibodega_sigue_igual(self, app, client, db, admin, cache_limpio):
        """El que SÍ funcionaba. Arreglar el hermano no puede cambiarlo —
        tiene otra forma (`{data, ts}`) a propósito."""
        from datetime import datetime
        cache_limpio._cache_inventario_multibodega.update({
            'data': {'NB1': {'A': 1}, 'PC1': {'A': 1, 'B': 2}},
            'ts': datetime(2026, 9, 21)})
        d = client.get('/api/siesa/debug-cache-status',
                       headers=_hdr(app, admin)).get_json()['multibodega']
        assert d['tiene_data'] is True
        assert d['bodegas'] == ['NB1', 'PC1']
        assert d['productos_por_bodega'] == {'NB1': 1, 'PC1': 2}


def test_sigue_siendo_solo_de_admin(app, client, db):
    """Expone nombres de bodega y volúmenes de inventario."""
    u = Usuario.query.filter_by(rol='operario').first()
    if not u:
        u = Usuario(nombre='Op Cache', email='op.cache@t.co', rol='operario')
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
    assert client.get('/api/siesa/debug-cache-status',
                      headers=_hdr(app, u)).status_code == 403


def test_las_dos_formas_estan_declaradas_donde_se_leen():
    """El defecto de fondo es que dos variables con nombres hermanos tienen
    formas distintas. Por AST: si alguien vuelve a leer el de bodegas como si
    fuera plano, el suscrito `['data']` reaparece sobre `mono`."""
    import ast
    from pathlib import Path
    fuente = (Path(__file__).resolve().parents[1] / 'app' / 'routes'
              / 'siesa.py').read_text(encoding='utf-8')
    for n in ast.walk(ast.parse(fuente)):
        if not (isinstance(n, ast.FunctionDef) and n.name == 'debug_cache_status'):
            continue
        for d in ast.walk(n):
            if (isinstance(d, ast.Subscript) and isinstance(d.value, ast.Name)
                    and d.value.id == 'mono'):
                clave = getattr(d.slice, 'value', None)
                assert clave not in ('data', 'ts'), (
                    f"`mono[{clave!r}]` volvió: `_cache_inventario_siesa` "
                    f"está indexado por bodega, no por campo")
        return
    pytest.fail('debug_cache_status desapareció de siesa.py')
