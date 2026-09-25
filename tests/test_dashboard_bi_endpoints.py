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
        assert set(data.keys()) == {'pedidos', 'lineas', 'unidades', 'valor_total', 'por_dia',
                                     'fecha_desde', 'fecha_hasta', 'sin_valor_factura'}
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
        assert set(data.keys()) == {'lineas_pendientes', 'fill_rate', 'por_motivo', 'por_dia',
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


class TestBiVentaPerdida:

    def test_sin_token_401(self, client):
        resp = client.get('/api/dashboard/bi/venta-perdida')
        assert resp.status_code == 401

    def test_sin_almacen_id_400(self, client, jwt_token_admin):
        resp = client.get(
            '/api/dashboard/bi/venta-perdida',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 400

    def test_operario_sin_permiso_403(self, client, jwt_token, almacen):
        resp = client.get(
            f'/api/dashboard/bi/venta-perdida?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token}'},
        )
        assert resp.status_code == 403

    def test_admin_retorna_kpi_agregado(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/bi/venta-perdida?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        # `sin_precio` / `total_es_cota_inferior`: un evento sin precio no suma
        # $0 al total, se declara (Regla 0, 2026-09-24).
        assert set(data.keys()) == {'venta_perdida_total', 'por_categoria', 'por_dia',
                                     'fecha_desde', 'fecha_hasta', 'eventos',
                                     'sin_precio', 'total_es_cota_inferior',
                                     # por categoría: una con solo agotados sin
                                     # precio suma $0 y no es «nada perdido».
                                     'sin_precio_por_categoria'}
        assert data['venta_perdida_total'] == 0.0


class TestBiVentaPerdidaDetalle:

    def test_admin_retorna_pagina_vacia(self, client, jwt_token_admin, almacen):
        resp = client.get(
            f'/api/dashboard/bi/venta-perdida/detalle?almacen_id={almacen.id}',
            headers={'Authorization': f'Bearer {jwt_token_admin}'},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == {'items': [], 'total': 0, 'page': 1, 'per_page': 50}
