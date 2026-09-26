"""Recepción puede decir que algo **llegó roto** — antes solo podía contar menos.

## El hueco que cierra

El proceso del negocio arranca acá: «llegó mal un producto, la auxiliar de
compras se contacta con el proveedor para pedir nota crédito o devolución».

El sistema no tenía dónde anotarlo. El payload del escaneo acepta producto,
cantidad, empaque, bonificación, lote y vencimiento — ningún campo de avería ni
de motivo. Con 100 pedidas y 20 rotas, el recepcionista escaneaba 80, y eso
queda **idéntico a «el proveedor mandó menos»**: mismo `es_faltante()`, mismo
`es_parcial` hacia Siesa, misma fila en el panel de discrepancias. La avería del
proveedor —la que el dueño nombró primero— era la única que el sistema no sabía
representar.

`ItemRecepcion.destino` declaraba `'BLOQUEADO'` en un comentario desde el inicio
y ningún camino lo escribía nunca. Un casillero hecho y vacío.

## Por qué la avería SE CUENTA como recibida

`cantidad_averiada` es un subconjunto de `cantidad_recibida`, no una resta, y lo
que viaja a Siesa sigue siendo `cantidad_recibida`: la unidad rota entra a la OC.

Lo decide el proceso: «si dan nota crédito se da de baja, o si no se devuelve».
Una NC **reversa una entrada** y devolver exige tener la mercancía — los dos
desenlaces presuponen que entró. Quien prefiera lo contrario sigue teniendo el
camino de hoy: contar de menos.

## Lo que estos tests vigilan

Conservación (lo bueno más lo averiado es siempre lo recibido, en las dos
direcciones), que la avería aterrice en la zona de averías y no en el hueco
vendible, que el motivo viaje hasta el movimiento, que el invariante muerda
antes de confirmar, y —dirección contraria— que una recepción sin averías se
comporte **exactamente** como antes.
"""
from unittest.mock import patch

import pytest

from app.models.inventario import MovimientoInventario, UbicacionProducto
from app.models.ubicacion import Ubicacion
from app.services.recepcion_service import RecepcionService


OC_SIESA = {
    'detalle': {'Table': [{
        'f200_nit_prov': 'PROV-001', 'f202_id_sucursal_prov': '001',
        'f120_referencia': 'PROD-001', 'f150_id': 'NB1',
        'f421_id_unidad_medida': 'UND', 'f421_fecha_entrega': '20260717',
        'f421_id_motivo': '01', 'f120_id': '', 'f420_id_co': '003',
        'f420_id_moneda_docto': 'COP', 'f420_id_moneda_conv': 'COP',
        'f420_id_moneda_local': 'COP', 'f420_tasa_conv': '1',
        'f420_tasa_local': '1', 'f200_nit_comprador': '',
    }]}
}


@pytest.fixture
def setup(db, almacen, producto, ub_picking, inv_picking, usuario):
    return {'almacen': almacen, 'producto': producto,
            'ubicacion': ub_picking, 'usuario': usuario}


def _bin_averias(db, almacen):
    u = Ubicacion(codigo='AVE-A1-EST01', almacen_id=almacen.id,
                  tipo_zona='AVERIAS', activo=True)
    db.session.add(u); db.session.commit()
    return u


def _recepcion(db, setup, ordenada=100):
    return RecepcionService.crear_recepcion(
        numero_oc_siesa='OC-AVE-001', almacen_id=setup['almacen'].id,
        proveedor_codigo='PROV-001', proveedor_nombre='Proveedor Test',
        co_oc_siesa='003', tipo_docto_oc_siesa='OCN',
        consec_docto_oc_siesa='12345',
        items=[{'producto_id': setup['producto'].id,
                'cantidad_ordenada': ordenada, 'tolerancia_exceso_pct': 10.0}])


def _stock(ubicacion_id, producto_id):
    reg = UbicacionProducto.query.filter_by(
        ubicacion_id=ubicacion_id, producto_id=producto_id).first()
    return reg.cantidad if reg else 0


# ── El escaneo ─────────────────────────────────────────────────────────────

def test_el_recepcionista_puede_declarar_la_averia(db, setup):
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    r = RecepcionService.escanear_producto(
        rec.id, setup['producto'].id, 100,
        cantidad_averiada=20, motivo_averia='Cajas mojadas en el transporte')

    item = rec.items[0]
    assert item.cantidad_recibida == 100
    assert item.cantidad_averiada == 20
    assert item.cantidad_buena() == 80
    assert 'mojadas' in item.motivo_averia
    assert 'AVERIADAS: 20' in (r['alerta'] or '')


def test_la_averia_se_acumula_entre_pasadas(db, setup):
    """El operario escanea en tandas; la avería se suma igual que la cantidad."""
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 50,
                                       cantidad_averiada=5, motivo_averia='Golpeadas')
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 50,
                                       cantidad_averiada=3, motivo_averia='Mojadas')
    item = rec.items[0]
    assert item.cantidad_recibida == 100
    assert item.cantidad_averiada == 8
    # los dos motivos sobreviven: son dos causas distintas de reclamo
    assert 'Golpeadas' in item.motivo_averia and 'Mojadas' in item.motivo_averia


def test_no_se_pueden_declarar_mas_averias_que_lo_escaneado(db, setup):
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    with pytest.raises(ValueError) as e:
        RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10,
                                           cantidad_averiada=11)
    assert 'parte de lo recibido' in str(e.value)


def test_la_guarda_acumulada_es_defensa_en_profundidad(db, setup):
    """El chequeo acumulado NO es alcanzable por el escaneo: si cada pasada
    valida `averiada <= cantidad`, la suma nunca supera la suma. Se deja igual
    —y el CHECK `ck_item_recepcion_averiada_subconjunto` en Postgres es el guard
    real— pero se declara acá para que nadie escriba un test que finja
    ejercitarlo. Lo que sí se comprueba es que el caso límite legítimo pase."""
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10,
                                       cantidad_averiada=10, motivo_averia='Todo roto')
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 5,
                                       cantidad_averiada=5, motivo_averia='Más roto')
    item = rec.items[0]
    assert item.cantidad_recibida == 15
    assert item.cantidad_averiada == 15
    assert item.cantidad_buena() == 0


# ── La confirmación: el reparto ────────────────────────────────────────────

@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_lo_bueno_y_lo_averiado_van_a_bins_distintos(mock_dlq, mock_ck, db, setup):
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    ave = _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 100,
                                       cantidad_averiada=20,
                                       motivo_averia='Cajas mojadas')
    antes_bueno = _stock(setup['ubicacion'].id, setup['producto'].id)

    RecepcionService.confirmar_recepcion(rec.id)

    assert _stock(setup['ubicacion'].id, setup['producto'].id) == antes_bueno + 80
    assert _stock(ave.id, setup['producto'].id) == 20


@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_no_se_pierde_ni_una_unidad(mock_dlq, mock_ck, db, setup):
    """Conservación: lo que entra al inventario es EXACTAMENTE lo recibido."""
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    ave = _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 100,
                                       cantidad_averiada=37, motivo_averia='x')
    antes = (_stock(setup['ubicacion'].id, setup['producto'].id)
             + _stock(ave.id, setup['producto'].id))

    RecepcionService.confirmar_recepcion(rec.id)

    despues = (_stock(setup['ubicacion'].id, setup['producto'].id)
               + _stock(ave.id, setup['producto'].id))
    assert despues - antes == 100


@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_el_motivo_viaja_hasta_el_movimiento(mock_dlq, mock_ck, db, setup):
    """Sin el motivo en el kardex, la avería es un número sin historia y la
    auxiliar de compras no tiene con qué reclamarle al proveedor."""
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    ave = _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10,
                                       cantidad_averiada=4,
                                       motivo_averia='Estiba volcada')
    RecepcionService.confirmar_recepcion(rec.id)

    mov = MovimientoInventario.query.filter_by(ubicacion_id=ave.id).one()
    assert mov.cantidad == 4
    assert 'AVERIADO EN RECEPCIÓN' in mov.motivo
    assert 'Estiba volcada' in mov.motivo


@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_todo_averiado_no_deja_nada_en_el_hueco_vendible(mock_dlq, mock_ck, db, setup):
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    ave = _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10,
                                       cantidad_averiada=10, motivo_averia='Todo roto')
    antes = _stock(setup['ubicacion'].id, setup['producto'].id)
    RecepcionService.confirmar_recepcion(rec.id)

    assert _stock(setup['ubicacion'].id, setup['producto'].id) == antes
    assert _stock(ave.id, setup['producto'].id) == 10


@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_sin_zona_de_averias_la_mercancia_no_se_pierde(mock_dlq, mock_ck, db, setup):
    """Degradación declarada: el almacén sin zona de averías armada sigue sin
    perder inventario — la cascada nunca devuelve None."""
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'

    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10,
                                       cantidad_averiada=6, motivo_averia='Rotas')
    RecepcionService.confirmar_recepcion(rec.id)

    mov = MovimientoInventario.query.filter(
        MovimientoInventario.motivo.like('%AVERIADO EN RECEPCIÓN%')).one()
    assert mov.cantidad == 6
    assert 'sin zona de averías' in mov.motivo
    ub = Ubicacion.query.get(mov.ubicacion_id)
    assert ub.tipo_zona == 'AVERIAS'


# ── Dirección contraria: sin averías, nada cambia ─────────────────────────

@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_sin_averias_se_comporta_exactamente_como_antes(mock_dlq, mock_ck, db, setup):
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup, ordenada=10)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10)
    antes = _stock(setup['ubicacion'].id, setup['producto'].id)

    RecepcionService.confirmar_recepcion(rec.id)

    assert _stock(setup['ubicacion'].id, setup['producto'].id) == antes + 10
    assert rec.items[0].cantidad_averiada == 0
    # un solo movimiento, sin rama de averías
    assert MovimientoInventario.query.filter(
        MovimientoInventario.motivo.like('%AVERIADO%')).count() == 0


@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_a_siesa_sigue_viajando_lo_recibido_completo(mock_dlq, mock_ck, db, setup):
    """La avería NO se le resta a Siesa: entró a la OC, y la nota crédito del
    proveedor es lo que después la reversa."""
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 100,
                                       cantidad_averiada=20, motivo_averia='x')
    RecepcionService.confirmar_recepcion(rec.id)

    import json
    from app.models.siesa_job import SiesaJob
    job = SiesaJob.query.filter_by(tipo='ENTRADA_OC').first()
    assert job is not None
    payload = job.payload if isinstance(job.payload, dict) else json.loads(job.payload)
    items = payload['items']
    assert items[0]['cantidad_recibida'] == 100, items[0]


# ── El put-away tampoco puede mandar lo BUENO a la zona de averías ────────

def test_el_putaway_nunca_elige_el_bin_de_averias(db, almacen, producto):
    """`_buscar_ubicacion_optima` cae a «cualquier ubicación activa» cuando no
    hay zona preferida. Sin filtro, la mercancía buena de una recepción puede
    aterrizar en el bin de averías — sale del FEFO en el acto y la recepción
    cierra en verde. Mismo defecto que el descuento de traslados, en un sitio
    donde no se suma: se elige."""
    ave = _bin_averias(db, almacen)
    ub = RecepcionService._buscar_ubicacion_optima(almacen.id, 'C')
    assert ub is None or ub.id != ave.id, (
        f'el put-away eligió el bin de averías: {ub.codigo}')


def test_el_putaway_si_elige_un_bin_vendible(db, almacen, producto, ub_general):
    """Dirección contraria: el filtro no puede dejar la recepción sin destino."""
    _bin_averias(db, almacen)
    ub = RecepcionService._buscar_ubicacion_optima(almacen.id, 'C')
    assert ub is not None and ub.tipo_zona != 'AVERIAS'


# ── La puerta de después de contar ────────────────────────────────────────

def test_se_puede_declarar_despues_de_contar(db, setup):
    """El gesto real: el recepcionista cuenta todo con la pistola y después
    marca lo roto. Obligarlo a saberlo en cada disparo es pedirle dos trabajos
    a la vez."""
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 100)

    RecepcionService.declarar_averia(rec.id, setup['producto'].id, 20,
                                     motivo='Dos cajas mojadas')
    item = rec.items[0]
    assert item.cantidad_recibida == 100
    assert item.cantidad_averiada == 20
    assert 'mojadas' in item.motivo_averia


def test_no_se_declara_averia_sobre_lo_que_no_se_conto(db, setup):
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    with pytest.raises(ValueError) as e:
        RecepcionService.declarar_averia(rec.id, setup['producto'].id, 5)
    assert 'Escanéelo primero' in str(e.value)


def test_la_averia_declarada_despues_no_puede_superar_lo_recibido(db, setup):
    """Acá SÍ es alcanzable el tope acumulado: es la puerta sin escaneo."""
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10)
    with pytest.raises(ValueError) as e:
        RecepcionService.declarar_averia(rec.id, setup['producto'].id, 11)
    assert 'no puede ser más que lo que llegó' in str(e.value)


@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_no_se_declara_averia_sobre_recepcion_confirmada(mock_dlq, mock_ck, db, setup):
    """Después de confirmar, la mercancía ya entró al inventario: reescribir la
    recepción dejaría el kardex diciendo una cosa y el stock otra."""
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup, ordenada=10)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10)
    RecepcionService.confirmar_recepcion(rec.id)

    with pytest.raises(ValueError) as e:
        RecepcionService.declarar_averia(rec.id, setup['producto'].id, 3)
    assert 'ya fue confirmada' in str(e.value)


def test_las_dos_puertas_validan_igual(db, setup):
    """Una sola regla: el tope es el mismo se entre por el escaneo o por la
    declaración posterior. Si divergieran, una de las dos sería la puerta
    floja."""
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10)

    with pytest.raises(ValueError):
        RecepcionService.declarar_averia(rec.id, setup['producto'].id, 11)
    with pytest.raises(ValueError):
        RecepcionService.escanear_producto(rec.id, setup['producto'].id, 1,
                                           cantidad_averiada=2)
    assert rec.items[0].cantidad_averiada == 0


def test_el_item_serializado_expone_la_averia(db, setup):
    """Sin esto la pantalla no tiene qué pintar."""
    rec = _recepcion(db, setup)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 100)
    RecepcionService.declarar_averia(rec.id, setup['producto'].id, 20, motivo='Mojadas')
    d = rec.items[0].to_dict()
    assert d['cantidad_averiada'] == 20
    assert d['cantidad_buena'] == 80
    assert d['motivo_averia'] == 'Mojadas'


# ── Que lo vea quien tiene que reclamar ───────────────────────────────────

@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_compras_ve_la_averia_aunque_la_entrega_este_completa(
        mock_dlq, mock_ck, db, setup, client, jwt_token_admin):
    """Una entrega COMPLETA con mercancía rota no es faltante ni exceso, así que
    no aparecía en la pantalla del comprador — y es justo quien tiene que llamar
    al proveedor. El panel de cuarentena tampoco servía: mide stock por
    producto, sin vínculo con la OC ni con el proveedor."""
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    _bin_averias(db, setup['almacen'])

    rec = _recepcion(db, setup, ordenada=100)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 100)
    RecepcionService.declarar_averia(rec.id, setup['producto'].id, 20,
                                     motivo='Cajas mojadas')
    RecepcionService.confirmar_recepcion(rec.id)

    r = client.get('/api/compras/dock-lock',
                   headers={'Authorization': f'Bearer {jwt_token_admin}'})
    assert r.status_code == 200, r.get_json()
    recs = r.get_json().get('recepciones', [])
    items = [i for rr in recs for i in rr.get('items_problema', [])]
    assert items, r.get_json()
    assert items[0]['tipo_problema'] == 'AVERIADO'
    assert items[0]['cantidad_averiada'] == 20
    assert 'mojadas' in (items[0]['motivo_averia'] or '')


@patch('app.services.recepcion_service.connekta')
@patch('app.services.siesa_job_service.disparar_dlq_inmediato')
def test_una_entrega_sana_sigue_sin_aparecer(mock_dlq, mock_ck, db, setup,
                                             client, jwt_token_admin):
    """Dirección contraria: el panel no puede llenarse de recepciones sanas."""
    mock_ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
    mock_ck.centro_op = '003'
    rec = _recepcion(db, setup, ordenada=10)
    RecepcionService.iniciar(rec.id, setup['usuario'].id)
    RecepcionService.escanear_producto(rec.id, setup['producto'].id, 10)
    RecepcionService.confirmar_recepcion(rec.id)

    r = client.get('/api/compras/dock-lock',
                   headers={'Authorization': f'Bearer {jwt_token_admin}'})
    assert r.status_code == 200
    assert r.get_json().get('recepciones', []) == []
