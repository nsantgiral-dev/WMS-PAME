"""
Tests de los endpoints HTTP del módulo de Layout (/api/almacenes/...).

editar_fila/eliminar_fila siguen siendo el mecanismo legado sobre el campo
'estante' — ya ningún endpoint de creación lo escribe, así que sus tests
insertan las ubicaciones directo en la BD (igual que haría una fila creada
antes del rediseño de 5 ejes) en vez de pasar por el endpoint de creación.
"""
import io
from openpyxl import Workbook
from app.extensions import db as _db
from app.models.ubicacion import Ubicacion


def _crear_legacy_fila(almacen_id, pasillo, fila_legacy, cantidad, tipo_zona):
    prefijo = {'PICKING': 'PIK', 'RESERVA': 'RES'}[tipo_zona]
    creadas = []
    for pos in range(1, cantidad + 1):
        ub = Ubicacion(
            codigo=f'{prefijo}-{pasillo}{fila_legacy:02d}-{pos:02d}',
            almacen_id=almacen_id, pasillo=pasillo, estante=str(fila_legacy),
            tipo_zona=tipo_zona, tipo='estanteria', origen='MANUAL', activo=True,
        )
        _db.session.add(ub)
        creadas.append(ub)
    _db.session.commit()
    return creadas


def test_pasillos_disponibles(client, jwt_token_admin, almacen):
    resp = client.get(
        f'/api/almacenes/{almacen.id}/pasillos-disponibles?cantidad=3',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    assert resp.get_json()['letras'] == ['A', 'B', 'C']


def test_crear_cuerpo_endpoint_picking(client, jwt_token_admin, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 3, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 201
    ubs = resp.get_json()['ubicaciones']
    assert len(ubs) == 3
    assert ubs[0]['codigo'] == 'PIK-A1-C01-E01-H01'
    # Un Cuerpo es 100% de una sola zona — los 3 entrepaños quedan en PICKING.
    assert all(u['tipo_zona'] == 'PICKING' for u in ubs)


def test_crear_cuerpo_endpoint_reserva(client, jwt_token_admin, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 3, 'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 201
    ubs = resp.get_json()['ubicaciones']
    assert len(ubs) == 3
    assert ubs[0]['codigo'] == 'RES-A1-C01-E01-H01'
    assert all(u['tipo_zona'] == 'RESERVA' for u in ubs)


def test_crear_cuerpo_endpoint_rechaza_zona_invalida(client, jwt_token_admin, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'AVERIAS'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert 'tipo_zona' in resp.get_json()['error']


def test_crear_cuerpo_endpoint_huecos_por_nivel_variables(client, jwt_token_admin, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={
            'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 2,
            'tipo_zona': 'PICKING', 'huecos_por_nivel': [3, 1],
        },
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 201
    ubs = resp.get_json()['ubicaciones']
    assert len(ubs) == 4
    codigos_nivel_1 = sorted(u['codigo'] for u in ubs if u['nivel'] == 1)
    assert codigos_nivel_1 == ['PIK-A1-C01-E01-H01', 'PIK-A1-C01-E01-H02', 'PIK-A1-C01-E01-H03']


def test_crear_cuerpo_rechaza_sin_admin(client, jwt_token, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 3, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token}'},
    )
    assert resp.status_code == 403


def test_crear_cuerpo_rechaza_fila_invalida(client, jwt_token_admin, almacen):
    resp = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 3, 'cuerpo': 1, 'cantidad_entrepanos': 2, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert 'fila' in resp.get_json()['error']


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
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
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


def test_asignar_endpoint_capacidad_maxima_solo_picking(client, jwt_token_admin, almacen, producto):
    r1 = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    ub_picking_id = r1.get_json()['ubicaciones'][0]['id']

    r2_setup = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'B', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    ub_reserva_id = r2_setup.get_json()['ubicaciones'][0]['id']

    r2 = client.post(
        f'/api/almacenes/ubicaciones/{ub_picking_id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 10, 'capacidad_maxima': 50},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert r2.status_code == 200
    assert r2.get_json()['ubicacion']['capacidad_maxima'] == 50

    r3 = client.post(
        f'/api/almacenes/ubicaciones/{ub_reserva_id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 10, 'capacidad_maxima': 50},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert r3.status_code == 400
    assert 'capacidad_maxima solo aplica a Huecos PICKING' in r3.get_json()['error']


def test_editar_fila_endpoint(client, jwt_token_admin, almacen):
    _crear_legacy_fila(almacen.id, 'A', 3, 3, 'PICKING')
    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 3, 'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['total_posiciones'] == 3
    assert len(body['actualizadas']) == 3
    assert body['bloqueadas'] == {}


def test_editar_fila_rechaza_sin_campos_a_cambiar(client, jwt_token_admin, almacen):
    _crear_legacy_fila(almacen.id, 'A', 1, 1, 'PICKING')
    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400


def test_editar_fila_rechaza_sin_admin(client, jwt_token, almacen):
    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1, 'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token}'},
    )
    assert resp.status_code == 403


def test_editar_fila_endpoint_no_encontrada(client, jwt_token_admin, almacen):
    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'Z', 'fila': 9, 'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert 'No hay posiciones' in resp.get_json()['error']


def test_eliminar_fila_endpoint(client, jwt_token_admin, almacen):
    _crear_legacy_fila(almacen.id, 'A', 5, 2, 'RESERVA')
    resp = client.delete(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 5},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body['eliminadas']) == 2
    assert body['bloqueadas'] == {}

    # Confirmar que ya no aparece en el layout
    layout = client.get(
        f'/api/almacenes/{almacen.id}/layout',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert layout.get_json()['total'] == 0


def test_eliminar_fila_bloquea_con_stock_endpoint(client, jwt_token_admin, almacen, producto):
    ub = _crear_legacy_fila(almacen.id, 'A', 1, 1, 'PICKING')[0]
    client.post(
        f'/api/almacenes/ubicaciones/{ub.id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 10},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )

    resp = client.delete(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['eliminadas'] == []
    assert 'stock activo' in body['bloqueadas']['PIK-A01-01']


def test_eliminar_fila_rechaza_sin_admin(client, jwt_token, almacen):
    resp = client.delete(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'A', 'fila': 1},
        headers={'Authorization': f'Bearer {jwt_token}'},
    )
    assert resp.status_code == 403


def test_eliminar_fila_endpoint_no_encontrada(client, jwt_token_admin, almacen):
    resp = client.delete(
        f'/api/almacenes/{almacen.id}/ubicaciones/fila',
        json={'pasillo': 'Z', 'fila': 9},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert 'No hay posiciones' in resp.get_json()['error']


def test_eliminar_ubicacion_individual_endpoint(client, jwt_token_admin, almacen):
    r1 = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    ub_id = r1.get_json()['ubicaciones'][0]['id']

    resp = client.delete(
        f'/api/almacenes/ubicaciones/{ub_id}',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    assert resp.get_json()['codigo'] == 'PIK-A1-C01-E01-H01'
    assert Ubicacion.query.get(ub_id) is None


def test_eliminar_ubicacion_individual_bloquea_con_stock(client, jwt_token_admin, almacen, producto):
    r1 = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    ub_id = r1.get_json()['ubicaciones'][0]['id']
    client.post(
        f'/api/almacenes/ubicaciones/{ub_id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 10},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )

    resp = client.delete(
        f'/api/almacenes/ubicaciones/{ub_id}',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert 'stock activo' in resp.get_json()['error']
    assert Ubicacion.query.get(ub_id) is not None


def test_layout_completo_endpoint(client, jwt_token_admin, almacen):
    client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 2, 'tipo_zona': 'PICKING'},
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
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 3, 'tipo_zona': 'PICKING'},
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


# ── Endpoints a nivel de Cuerpo completo (PUT/PATCH/DELETE sobre el mismo path) ──

def test_editar_cuerpo_endpoint(client, jwt_token_admin, almacen):
    client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 2, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )

    resp = client.put(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 3},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    ubs = resp.get_json()['ubicaciones']
    assert len(ubs) == 3
    assert all(u['tipo_zona'] == 'PICKING' for u in ubs)  # conserva la zona, no viene en el payload


def test_editar_cuerpo_endpoint_rechaza_sin_admin(client, jwt_token, almacen):
    resp = client.put(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 2},
        headers={'Authorization': f'Bearer {jwt_token}'},
    )
    assert resp.status_code == 403


def test_editar_cuerpo_endpoint_cuerpo_inexistente(client, jwt_token_admin, almacen):
    resp = client.put(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'Z', 'fila': 1, 'cuerpo': 9, 'cantidad_entrepanos': 2},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert 'No existe el cuerpo' in resp.get_json()['error']


def test_eliminar_cuerpo_endpoint(client, jwt_token_admin, almacen):
    client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 2, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )

    resp = client.delete(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    assert resp.get_json()['total'] == 2
    assert Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', fila=1, cuerpo=1).count() == 0


def test_eliminar_cuerpo_endpoint_bloquea_con_stock(client, jwt_token_admin, almacen, producto):
    r1 = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    ub_id = r1.get_json()['ubicaciones'][0]['id']
    client.post(
        f'/api/almacenes/ubicaciones/{ub_id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 10},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )

    resp = client.delete(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', fila=1, cuerpo=1).count() == 1


def test_eliminar_cuerpo_endpoint_rechaza_sin_admin(client, jwt_token, almacen):
    resp = client.delete(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1},
        headers={'Authorization': f'Bearer {jwt_token}'},
    )
    assert resp.status_code == 403


def test_reclasificar_cuerpo_endpoint(client, jwt_token_admin, almacen):
    client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 2, 'tipo_zona': 'RESERVA'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )

    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body['actualizadas']) == 2
    ubs = Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', fila=1, cuerpo=1).all()
    assert all(u.tipo_zona == 'PICKING' for u in ubs)


def test_reclasificar_cuerpo_endpoint_desactivar(client, jwt_token_admin, almacen):
    client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )

    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'activo': False},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200
    ub = Ubicacion.query.filter_by(almacen_id=almacen.id, pasillo='A', fila=1, cuerpo=1).first()
    assert ub.activo is False


def test_reclasificar_cuerpo_endpoint_rechaza_sin_campos(client, jwt_token_admin, almacen):
    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 400
    assert 'al menos un campo' in resp.get_json()['error']


def test_reclasificar_cuerpo_endpoint_rechaza_sin_admin(client, jwt_token, almacen):
    resp = client.patch(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'tipo_zona': 'PICKING'},
        headers={'Authorization': f'Bearer {jwt_token}'},
    )
    assert resp.status_code == 403
