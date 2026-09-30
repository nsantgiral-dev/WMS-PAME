"""
GET /api/traslados/stock-disponible — paginación y filtro server-side.

Antes, el endpoint mandaba el catálogo completo de la bodega (hasta ~4000 SKU
reales, ver CLAUDE.md) en cada carga/filtro/cambio de página — el costo real
lo paga el celular en la señal de bodega/tienda. Filtro y paginación ahora
corren sobre el mismo cache de Siesa (`TrasladoService.get_stock_disponible`,
TTL 1h) — no se repite el fetch a Siesa por paginar.

Cubre también un bug real encontrado al implementar esto: `get_stock_disponible`
devuelve el dict que vive DENTRO del cache por referencia, no una copia. Antes,
la ruta lo mutaba directo (`resultado['_debug'] = ...`) — con `debug=true` una
sola vez, esa clave quedaba pegada al cache compartido y se filtraba a todas
las peticiones siguientes de cualquier usuario, incluso sin `debug=true`.
"""
import pytest
from unittest.mock import patch


def _item(i):
    return {
        'codigo_siesa': f'SKU-{i:03d}',
        'nombre': f'Producto {i:03d}',
        'producto_id': i,
        'disponible': 10 + i,
        'existencia': 10 + i,
        'comprometida': 0,
        'unidad_medida': 'UND',
    }


def _resultado_cacheado(n=5):
    return {
        'items': [_item(i) for i in range(n)],
        'bodega': 'NS1',
        'total': n,
        'fuente': 'siesa',
        'actualizado_en': '2026-09-16T10:00:00',
        'siesa_total_productos': n,
        'siesa_con_stock': n,
    }


@pytest.fixture
def _mock_stock():
    with patch(
        'app.services.traslado_service.TrasladoService.get_stock_disponible'
    ) as m:
        m.return_value = _resultado_cacheado(5)
        yield m


class TestPaginacion:
    def test_primera_pagina_respeta_per_page(self, client, jwt_token_admin, _mock_stock):
        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&page=1&per_page=2',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert r.status_code == 200
        d = r.get_json()
        assert len(d['items']) == 2
        assert d['items'][0]['codigo_siesa'] == 'SKU-000'
        assert d['total_filtrado'] == 5
        assert d['total_paginas'] == 3
        assert d['pagina'] == 1

    def test_segunda_pagina_trae_el_resto(self, client, jwt_token_admin, _mock_stock):
        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&page=2&per_page=2',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        d = r.get_json()
        assert [it['codigo_siesa'] for it in d['items']] == ['SKU-002', 'SKU-003']

    def test_pagina_fuera_de_rango_se_recorta_a_la_ultima(self, client, jwt_token_admin, _mock_stock):
        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&page=99&per_page=2',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        d = r.get_json()
        assert d['pagina'] == 3  # última página real (5 items / 2 por página)
        assert len(d['items']) == 1

    def test_per_page_tiene_tope(self, client, jwt_token_admin, _mock_stock):
        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&per_page=99999',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert r.status_code == 200
        assert r.get_json()['por_pagina'] == 200


class TestFiltro:
    def test_q_filtra_por_codigo_siesa(self, client, jwt_token_admin, _mock_stock):
        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&q=SKU-003',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        d = r.get_json()
        assert d['total_filtrado'] == 1
        assert d['items'][0]['codigo_siesa'] == 'SKU-003'

    def test_q_filtra_por_nombre_sin_importar_mayusculas(self, client, jwt_token_admin, _mock_stock):
        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&q=PRODUCTO+004',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        d = r.get_json()
        assert d['total_filtrado'] == 1
        assert d['items'][0]['codigo_siesa'] == 'SKU-004'

    def test_q_sin_resultados_no_revienta(self, client, jwt_token_admin, _mock_stock):
        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&q=noexiste',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert r.status_code == 200
        d = r.get_json()
        assert d['items'] == []
        assert d['total_filtrado'] == 0
        assert d['total_paginas'] == 1


class TestNoMutaElCacheCompartido:
    """El bug real: `get_stock_disponible` devuelve el dict del cache por
    referencia. Pedir con page/q/debug distintos no puede dejar residuo en
    ese dict — otra petición (de otro usuario) lo va a volver a leer."""

    def test_paginar_no_le_recorta_los_items_al_cache(self, client, jwt_token_admin, _mock_stock):
        original = _mock_stock.return_value
        client.get(
            '/api/traslados/stock-disponible?bodega=NS1&page=1&per_page=1',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert len(original['items']) == 5, (
            'la ruta mutó el dict cacheado — la siguiente petición vería solo 1 ítem'
        )

    def test_debug_no_se_pega_al_cache_para_peticiones_sin_debug(self, client, jwt_token_admin, _mock_stock):
        original = _mock_stock.return_value
        r1 = client.get(
            '/api/traslados/stock-disponible?bodega=NS1&debug=true',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert '_debug' in r1.get_json()
        assert '_debug' not in original, (
            'la ruta escribió _debug directo sobre el dict cacheado — '
            'se filtraría a la próxima petición aunque no pida debug'
        )

        r2 = client.get(
            '/api/traslados/stock-disponible?bodega=NS1',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert '_debug' not in r2.get_json()


class TestPermiso:
    def test_rol_sin_permiso_da_403(self, app, client, db, almacen, _mock_stock):
        from app.models.usuario import Usuario
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        u = Usuario(
            nombre='Operario Test', email='op-stock@test.com',
            password_hash=generate_password_hash('test123'),
            rol='operario', almacen_id=almacen.id, activo=True,
        )
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            token = create_access_token(identity=str(u.id))

        r = client.get(
            '/api/traslados/stock-disponible?bodega=NS1',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert r.status_code == 403


class TestLaTiendaRecibeLaListaEntera:
    """«Solo salen algunos artículos» (2026-09-29, producción): la pantalla
    «Pedir» y «Averías» de la tienda busca y pagina en el celular, pero pedía
    sin `completo` y recibía solo la página 1 del servidor (30 productos).
    Buscaba entre esos 30 y creía que era todo."""

    def test_completo_trae_todo_lo_pedible(self, client, jwt_token_admin):
        items = [_item(i) for i in range(1, 76)]      # producto_id 0 no es producto
        items.append({**_item(90), 'disponible': 0})        # sin disponible
        items.append({**_item(91), 'producto_id': None})    # no está en el WMS
        cache = {**_resultado_cacheado(0), 'items': items, 'total': len(items)}
        with patch('app.services.traslado_service.TrasladoService.get_stock_disponible',
                   return_value=cache):
            r = client.get('/api/traslados/stock-disponible?bodega=NS1&completo=true',
                           headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d = r.get_json()
        assert r.status_code == 200
        assert len(d['items']) == 75
        assert d['total_paginas'] == 1
        assert {'SKU-090', 'SKU-091'}.isdisjoint({i['codigo_siesa'] for i in d['items']})

    def test_sin_completo_sigue_paginando(self, client, jwt_token_admin):
        cache = {**_resultado_cacheado(0), 'items': [_item(i) for i in range(75)]}
        with patch('app.services.traslado_service.TrasladoService.get_stock_disponible',
                   return_value=cache):
            r = client.get('/api/traslados/stock-disponible?bodega=NS1',
                           headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert len(r.get_json()['items']) == 30

    def test_las_dos_pantallas_de_tienda_piden_completo(self):
        """El detector lee el código (sin comentarios) de tienda.js: toda
        petición a stock-disponible desde la tienda lleva `completo=true`."""
        import pathlib, re
        src = pathlib.Path('app/static/pwa/tienda.js').read_text(encoding='utf-8')
        sin_coment = re.sub(r'//[^\n]*|/\*.*?\*/', '', src, flags=re.S)
        llamadas = re.findall(r'stock-disponible\?[^`\'"]*', sin_coment)
        assert len(llamadas) >= 2, llamadas            # piso: Pedir y Averías
        assert all('completo=true' in c for c in llamadas), llamadas
