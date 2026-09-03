"""
E2E simulado del soporte de vitrinas/estibas en Layout (commit fe8890c).

A diferencia de test_11_layout.py (llama layout_service directo) y
test_12_layout_endpoints.py (un endpoint a la vez), este archivo simula el
recorrido completo que haría un admin desde la pantalla: crear el mueble vía
HTTP -> verlo en GET /layout -> asignarle un SKU -> ver el efecto en la cola
real de picking (orden_ruta_fisica) -> remodularlo. Todo contra el test_client
real (JWT incluido), no contra el servicio en aislamiento.

El caso que más importa demostrar: una vitrina/estiba con pasillo asignado
participa en el orden de recorrido físico igual que un pasillo real — no cae
al fondo de la cola como GENERAL/AVERIAS (ver PickingService.orden_ruta_fisica,
nullslast() por eje). Es la tensión que motivó el diseño (ver conversación:
"ruteo físico vs. presentación").
"""
import pytest
from app.extensions import db as _db
from app.models.ubicacion import Ubicacion
from app.models.picking import TareaPicking
from app.services.picking_service import PickingService


def _crear_cuerpo_http(client, token, almacen_id, **kwargs):
    payload = {
        'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 1,
        'tipo_zona': 'PICKING',
    }
    payload.update(kwargs)
    return client.post(
        f'/api/almacenes/{almacen_id}/ubicaciones/cuerpo',
        json=payload,
        headers={'Authorization': f'Bearer {token}'},
    )


def test_e2e_crear_vitrina_por_http_aparece_en_layout_con_su_tipo(client, jwt_token_admin, almacen):
    resp = _crear_cuerpo_http(
        client, jwt_token_admin, almacen.id,
        pasillo='A', fila=1, cuerpo=1, tipo_zona='PICKING', tipo_mueble='vitrina',
    )
    assert resp.status_code == 201, resp.get_json()
    ubs = resp.get_json()['ubicaciones']
    assert len(ubs) == 1
    assert ubs[0]['codigo'] == 'PIK-A1-VIT01'
    assert ubs[0]['tipo'] == 'vitrina'

    layout_resp = client.get(
        f'/api/almacenes/{almacen.id}/layout',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert layout_resp.status_code == 200
    encontrada = next(u for u in layout_resp.get_json()['ubicaciones'] if u['codigo'] == 'PIK-A1-VIT01')
    assert encontrada['tipo'] == 'vitrina'
    assert encontrada['tipo_zona'] == 'PICKING'
    assert encontrada['stock_actual'] == 0


def test_e2e_asignar_sku_a_vitrina_exige_capacidad_como_cualquier_hueco_picking(client, jwt_token_admin, almacen, producto):
    resp = _crear_cuerpo_http(client, jwt_token_admin, almacen.id, tipo_mueble='vitrina')
    ub_id = resp.get_json()['ubicaciones'][0]['id']

    # Sin capacidad_maxima la primera vez -> rechaza, igual que un hueco de Cuerpo normal.
    sin_capacidad = client.post(
        f'/api/almacenes/ubicaciones/{ub_id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 20},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert sin_capacidad.status_code == 400
    assert 'capacidad_maxima' in sin_capacidad.get_json()['error']

    con_capacidad = client.post(
        f'/api/almacenes/ubicaciones/{ub_id}/asignar',
        json={'producto_id': producto.id, 'cantidad': 20, 'capacidad_maxima': 50},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert con_capacidad.status_code == 200, con_capacidad.get_json()
    assert con_capacidad.get_json()['cantidad_total'] == 20

    layout_resp = client.get(
        f'/api/almacenes/{almacen.id}/layout',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    encontrada = next(u for u in layout_resp.get_json()['ubicaciones'] if u['codigo'] == 'PIK-A1-VIT01')
    assert encontrada['producto_asignado_codigo'] == producto.codigo
    assert encontrada['stock_actual'] == 20


def test_e2e_dos_vitrinas_mismo_sku_respeta_slot_unico_picking(client, jwt_token_admin, almacen, producto):
    v1 = _crear_cuerpo_http(client, jwt_token_admin, almacen.id, pasillo='A', tipo_mueble='vitrina').get_json()['ubicaciones'][0]
    v2 = _crear_cuerpo_http(client, jwt_token_admin, almacen.id, pasillo='B', tipo_mueble='vitrina').get_json()['ubicaciones'][0]

    ok = client.post(
        f'/api/almacenes/ubicaciones/{v1["id"]}/asignar',
        json={'producto_id': producto.id, 'cantidad': 10, 'capacidad_maxima': 30},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert ok.status_code == 200

    # Mismo SKU, otra vitrina de la MISMA zona (PICKING) -> mismo pool de
    # exclusividad que un hueco de Cuerpo normal, se rechaza.
    choque = client.post(
        f'/api/almacenes/ubicaciones/{v2["id"]}/asignar',
        json={'producto_id': producto.id, 'cantidad': 5, 'capacidad_maxima': 30},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert choque.status_code == 400
    assert 'ya tiene un slot' in choque.get_json()['error']


def test_e2e_estiba_en_reserva_permite_varios_skus_sin_capacidad(client, jwt_token_admin, almacen, producto, producto2):
    resp = _crear_cuerpo_http(
        client, jwt_token_admin, almacen.id,
        pasillo='C', tipo_zona='RESERVA', tipo_mueble='estiba',
    )
    assert resp.status_code == 201
    ub = resp.get_json()['ubicaciones'][0]
    assert ub['codigo'] == 'RES-C1-EST01'

    r1 = client.post(
        f'/api/almacenes/ubicaciones/{ub["id"]}/asignar',
        json={'producto_id': producto.id, 'cantidad': 100},  # sin capacidad_maxima: RESERVA no la exige
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    r2 = client.post(
        f'/api/almacenes/ubicaciones/{ub["id"]}/asignar',
        json={'producto_id': producto2.id, 'cantidad': 40},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert r1.status_code == 200 and r2.status_code == 200

    layout_resp = client.get(
        f'/api/almacenes/{almacen.id}/layout',
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    encontrada = next(u for u in layout_resp.get_json()['ubicaciones'] if u['codigo'] == 'RES-C1-EST01')
    assert encontrada['stock_actual'] == 140  # los dos SKU comparten el mismo hueco de piso


def test_e2e_vitrina_participa_en_orden_de_ruta_fisica_no_va_siempre_al_final(client, jwt_token_admin, almacen, producto):
    """
    El hallazgo central del diseño: PickingService.orden_ruta_fisica() ordena
    por pasillo/fila/cuerpo/nivel/hueco con nullslast() — cualquier ubicación
    SIN pasillo cae siempre al final. Una vitrina/estiba creada con este
    mecanismo SÍ tiene pasillo (aquí 'B', entre el pasillo real 'A' y el
    AVERIAS numerado sin pasillo), así que debe intercalarse en la ruta según
    su posición, no quedar relegada como GENERAL/AVERIAS.
    """
    # Cuerpo real de estantería en pasillo A
    cuerpo = _crear_cuerpo_http(client, jwt_token_admin, almacen.id, pasillo='A').get_json()['ubicaciones'][0]
    # Vitrina en pasillo B (después de A en el orden de recorrido)
    vitrina = _crear_cuerpo_http(client, jwt_token_admin, almacen.id, pasillo='B', tipo_mueble='vitrina').get_json()['ubicaciones'][0]
    # AVERIAS numerada — sin pasillo, debe seguir cayendo al final
    averias = client.post(
        f'/api/almacenes/{almacen.id}/ubicaciones/averias', json={},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    ).get_json()
    ub_averias = Ubicacion.query.filter_by(codigo=averias['codigo']).first()
    assert ub_averias.pasillo is None  # confirma la premisa del test

    # Una TareaPicking real por cada ubicación, creadas fuera de orden a propósito
    _db.session.add_all([
        TareaPicking(codigo='PICK-E2E-AVERIAS', producto_id=producto.id, cantidad_solicitada=1,
                     ubicacion_id=ub_averias.id, almacen_id=almacen.id, estado='PENDIENTE'),
        TareaPicking(codigo='PICK-E2E-VITRINA', producto_id=producto.id, cantidad_solicitada=1,
                     ubicacion_id=vitrina['id'], almacen_id=almacen.id, estado='PENDIENTE'),
        TareaPicking(codigo='PICK-E2E-CUERPO', producto_id=producto.id, cantidad_solicitada=1,
                     ubicacion_id=cuerpo['id'], almacen_id=almacen.id, estado='PENDIENTE'),
    ])
    _db.session.commit()

    orden = PickingService.orden_ruta_fisica()
    fila_ordenada = (
        TareaPicking.query.join(Ubicacion, TareaPicking.ubicacion_id == Ubicacion.id)
        .filter(TareaPicking.codigo.like('PICK-E2E-%'))
        .order_by(*orden)
        .all()
    )
    codigos_en_orden = [t.codigo for t in fila_ordenada]

    # Pasillo A (Cuerpo real) antes que pasillo B (vitrina) antes que sin-pasillo (AVERIAS al final).
    assert codigos_en_orden == ['PICK-E2E-CUERPO', 'PICK-E2E-VITRINA', 'PICK-E2E-AVERIAS']


def test_e2e_editar_cuerpo_sobre_vitrina_conserva_codigo_y_tipo_por_http(client, jwt_token_admin, almacen):
    _crear_cuerpo_http(client, jwt_token_admin, almacen.id, tipo_mueble='vitrina')

    resp = client.put(
        f'/api/almacenes/{almacen.id}/ubicaciones/cuerpo',
        json={'pasillo': 'A', 'fila': 1, 'cuerpo': 1, 'cantidad_entrepanos': 5, 'huecos_por_nivel': [3, 3, 3, 3, 3]},
        headers={'Authorization': f'Bearer {jwt_token_admin}'},
    )
    assert resp.status_code == 200, resp.get_json()
    ubs = resp.get_json()['ubicaciones']
    # A pesar de pedir 5 entrepaños x 3 huecos, la vitrina se remodula 1x1 — el
    # tipo se conserva desde el registro existente, editar_cuerpo() no lo pierde.
    assert len(ubs) == 1
    assert ubs[0]['codigo'] == 'PIK-A1-VIT01'
    assert ubs[0]['tipo'] == 'vitrina'


def test_e2e_rechaza_tipo_mueble_invalido_por_http(client, jwt_token_admin, almacen):
    resp = _crear_cuerpo_http(client, jwt_token_admin, almacen.id, tipo_mueble='exhibidor')
    assert resp.status_code == 400
    assert 'tipo_mueble' in resp.get_json()['error']
