"""
Endpoints del tablero BI (app/routes/dashboard.py, prefijo /bi/).
Mismo patrón de auth que el resto del dashboard: 401 sin token, 400 sin
almacen_id, 403 sin rol de gestión, 200 con admin.
"""


class TestBiPedidosDespachados:

    def test_sin_token_401(self, client):
        resp = client.get('/api/dashboard/bi/pedidos-despachados')
        assert resp.status_code == 401

    def test_sin_almacen_id_400(self, client, jwt_token_admin):
        resp = client.get(
            '/api/dashboard/bi/pedidos-despachados',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 400
        assert 'almacen_id' in resp.get_json()['error']

    def test_operario_sin_permiso_403(self, client, jwt_token, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-despachados?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_admin_retorna_kpi_agregado(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-despachados?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data.keys()) == {'pedidos', 'lineas', 'unidades', 'valor_total',
                                     'fecha_desde', 'fecha_hasta'}
        assert data['pedidos'] == 0  # sin datos cargados, no debe fallar

    def test_admin_acepta_rango_de_fechas(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-despachados'
            f'?almacen_id={almacen.id}&fecha_desde=2026-01-01&fecha_hasta=2026-01-31',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['fecha_desde'] == '2026-01-01'
        assert data['fecha_hasta'] == '2026-01-31'


class TestBiPedidosDespachadosDetalle:

    def test_sin_token_401(self, client):
        resp = client.get('/api/dashboard/bi/pedidos-despachados/detalle')
        assert resp.status_code == 401

    def test_admin_retorna_pagina_vacia(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-despachados/detalle?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {'items': [], 'total': 0, 'page': 1, 'per_page': 50}

    def test_operario_sin_permiso_403(self, client, jwt_token, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-despachados/detalle?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403


class TestBiPedidosPendientes:

    def test_sin_token_401(self, client):
        resp = client.get('/api/dashboard/bi/pedidos-pendientes')
        assert resp.status_code == 401

    def test_sin_almacen_id_400(self, client, jwt_token_admin):
        resp = client.get(
            '/api/dashboard/bi/pedidos-pendientes',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 400

    def test_operario_sin_permiso_403(self, client, jwt_token, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-pendientes?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_admin_retorna_kpi_agregado(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-pendientes?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert set(data.keys()) == {'lineas_pendientes', 'fill_rate', 'por_motivo',
                                     'fecha_desde', 'fecha_hasta'}


class TestBiPedidosPendientesDetalle:

    def test_admin_retorna_pagina_vacia(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/bi/pedidos-pendientes/detalle?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {'items': [], 'total': 0, 'page': 1, 'per_page': 50}
