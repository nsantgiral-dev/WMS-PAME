"""
Tests de los endpoints HTTP del módulo de Layout (/api/almacenes/...).
"""
import io
from openpyxl import Workbook


def test_pasillos_disponibles(client, jwt_token_admin, almacen):
    resp = client.get(
        f'/api/almacenes/{almacen.id}/pasillos-disponibles?cantidad=3',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    assert resp.get_json()['letras'] == ['A', 'B', 'C']


def test_crear_fila_endpoint(client, jwt_token_admin, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1, 'cantidad_posiciones': 3, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 201
    ubs = resp.get_json()['ubicaciones']
    assert len(ubs) == 3
    assert ubs[0]['codigo'] == 'PIK-A01-01'


def test_crear_fila_rechaza_sin_admin(client, jwt_token, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1, 'cantidad_posiciones': 3, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token}'},
    )
    assert resp.status_code == 403


def test_crear_averias_endpoint(client, jwt_token_admin, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/averias',
        json={},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 201
    assert resp.get_json()['codigo'] == 'AVE1'


def test_asignar_y_reclasificar_endpoint(client, jwt_token_admin, almacen, producto):
    r1 = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1, 'cantidad_posiciones': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    ub_id = r1.get_json()['ubicaciones'][0]['id']

    r2 = client.post(
        f'/api/almacenes/ubicaciones/{ub_id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 40},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert r2.status_code == 200
    assert r2.get_json()['cantidad_total'] == 40

    # Reclasificar con stock activo debe fallar
    r3 = client.patch(
        f'/api/almacenes/ubicaciones/{ub_id}',
        json={'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert r3.status_code == 400
    assert 'activas' in r3.get_json()['error']


def test_layout_completo_endpoint(client, jwt_token_admin, almacen):
    client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1, 'cantidad_posiciones': 2, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    resp = client.get(
        f'/api/almacenes/{almacen.id}/layout',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['total'] == 2


def test_importar_excel_endpoint(client, jwt_token_admin, almacen, producto):
    r1 = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1, 'cantidad_posiciones': 1, 'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    codigo_ub = r1.get_json()['ubicaciones'][0]['codigo']

    wb = Workbook()
    ws = wb.active
    ws.append(['ubicacion_codigo', 'producto_codigo', 'cantidad'])
    ws.append([codigo_ub, producto.codigo, 100])
    ws.append(['NO-EXISTE', producto.codigo, 5])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/importar',
        data={'archivo': (buf, 'layout.xlsx')},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
        content_type='multipart/form-data',
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] == 1
    assert len(body['errores']) == 1
    assert body['errores'][0]['fila'] == 3
