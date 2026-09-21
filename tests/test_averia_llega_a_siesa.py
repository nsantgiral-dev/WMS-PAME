"""La avería tiene que llegar al ERP, no quedarse en el WMS.

## El hueco que cierra

Con el bin de averías armado, la mercancía rota sale del FEFO del WMS. Pero
**Siesa la sigue contando como existencia vendible de la bodega**: un vendedor
puede venderla y el pedido llega días después sin con qué surtirlo.

El conector que lo resuelve —`transferir_a_averias`, 142951, NB1→AV1— existe,
está probado contra Siesa QA y **no tenía un solo llamador vivo**: su único
encolador estaba en `devolucion_service`, DEPRECATED desde el 2026-07-28.

## Y el handler estaba soldado a la tabla muerta

Su idempotencia colgaba SOLO de `TareaDevolucion`, que ya nadie escribe. Un job
encolado desde cualquier otro camino caía en «la tarea no existe» y **devolvía
éxito sin llamar a Siesa**. Encolar sin arreglar eso habría producido lo peor:
un job en verde y ningún traslado.

El ancla nueva es `MovimientoInventario.siesa_sync` — un campo que existía desde
el principio, con default 'PENDIENTE', que **nadie leía jamás**. Es el hecho
durable: si el movimiento está, la avería ocurrió.

## Por qué se encola desde el job de la entrada y no al confirmar

El traslado descuenta de NB1, y esas unidades solo existen ahí **después** de
que la entrada por OC aterrizó en Siesa. Encolarlo al confirmar la recepción
sería pedirle a Siesa que mueva stock que todavía no tiene. Mismo patrón que
`MOTIVO_DIAN_NC`, que se encola desde adentro del job que lo habilita.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app.models.inventario import MovimientoInventario
from app.models.siesa_job import SiesaJob
from app.models.ubicacion import Ubicacion
from app.services import siesa_job_service as sjs
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
    u = Ubicacion(codigo='AVE-A1-EST01', almacen_id=almacen.id,
                  tipo_zona='AVERIAS', activo=True)
    # El fixture compartido no trae `centro_op_siesa`, y desde el 2026-09-18 el
    # ajuste focalizado de auditoría lo exige: sin CO, el documento saldría con
    # el centro de operación equivocado, así que el servicio se niega antes de
    # emitirlo. Es la bodega CD, CO 003.
    almacen.centro_op_siesa = '003'
    db.session.add(u); db.session.commit()
    return {'almacen': almacen, 'producto': producto, 'usuario': usuario,
            'ave': u, 'ubicacion': ub_picking}


def _recibir_con_averia(db, s, averiadas=20, recibidas=100):
    with patch('app.services.recepcion_service.connekta') as ck, \
         patch('app.services.siesa_job_service.disparar_dlq_inmediato'):
        ck.get_ordenes_compra_aprobadas.return_value = OC_SIESA
        ck.centro_op = '003'
        rec = RecepcionService.crear_recepcion(
            numero_oc_siesa='OC-AVE-9', almacen_id=s['almacen'].id,
            proveedor_codigo='PROV-001', proveedor_nombre='P',
            co_oc_siesa='003', tipo_docto_oc_siesa='OCN',
            consec_docto_oc_siesa='9',
            items=[{'producto_id': s['producto'].id,
                    'cantidad_ordenada': recibidas,
                    'tolerancia_exceso_pct': 10.0}])
        RecepcionService.iniciar(rec.id, s['usuario'].id)
        RecepcionService.escanear_producto(rec.id, s['producto'].id, recibidas)
        if averiadas:
            RecepcionService.declarar_averia(rec.id, s['producto'].id, averiadas,
                                             motivo='Cajas mojadas')
        RecepcionService.confirmar_recepcion(rec.id)
    return rec


def _job_entrada(rec):
    return SiesaJob.query.filter_by(tipo='ENTRADA_OC').first()


def _correr(job, connekta_falso):
    # `connekta` se importa dentro de las funciones del módulo, así que el
    # parche va sobre el singleton en su origen, no sobre un atributo de módulo
    # que no existe.
    with patch('app.services.connekta_gateway.connekta', connekta_falso):
        return sjs._ejecutar_job(job)


def _connekta_ok():
    ck = MagicMock()
    ck.bodega = 'NB1'
    ck.bodega_averias = 'AV1'
    ck.centro_op = '003'
    ck.confirmar_entrada_compras.return_value = {'codigo': 0, 'mensaje': 'OK'}
    ck.transferir_a_averias.return_value = {'codigo': 0, 'mensaje': 'OK'}
    return ck


# ── El orden ───────────────────────────────────────────────────────────────

def test_al_confirmar_todavia_no_hay_job_de_averias(db, setup):
    """El traslado NO se encola al confirmar: en ese momento Siesa todavía no
    tiene las unidades en NB1."""
    _recibir_con_averia(db, setup)
    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0


def test_la_entrada_en_siesa_encola_el_traslado(db, setup):
    rec = _recibir_con_averia(db, setup)
    _correr(_job_entrada(rec), _connekta_ok())

    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    p = json.loads(job.payload)
    assert p['cantidad'] == 20
    assert p['item_codigo'] == 'PROD-001'
    assert 'OC-AVE-9' in p['referencia']
    assert job.referencia_tipo == 'movimiento_averia'


def test_el_traslado_llama_al_conector_y_marca_el_movimiento(db, setup):
    rec = _recibir_con_averia(db, setup)
    ck = _connekta_ok()
    _correr(_job_entrada(rec), ck)
    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()

    _correr(job, ck)

    ck.transferir_a_averias.assert_called_once()
    kw = ck.transferir_a_averias.call_args.kwargs
    assert kw['item_codigo'] == 'PROD-001' and kw['cantidad'] == 20
    mov = MovimientoInventario.query.filter_by(
        idempotency_key=f'REC-AVE-{rec.id}-{setup["producto"].id}').one()
    assert mov.siesa_sync == 'ENVIADO'


def test_segunda_corrida_no_vuelve_a_mover_stock(db, setup):
    """Idempotencia sobre el ancla nueva: el movimiento ya dice ENVIADO."""
    rec = _recibir_con_averia(db, setup)
    ck = _connekta_ok()
    _correr(_job_entrada(rec), ck)
    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    _correr(job, ck)
    ck.transferir_a_averias.reset_mock()

    r = _correr(job, ck)

    ck.transferir_a_averias.assert_not_called()
    assert r.get('idempotente') is True


# ── Los guards ─────────────────────────────────────────────────────────────

def test_otra_bodega_no_emite_documento_falso(db, setup):
    """El conector tiene la bodega de salida CABLEADA a NB1. Para un almacén de
    otra bodega, el documento diría que salió de donde nunca estuvo."""
    setup['almacen'].bodega_siesa_id = 'NC1'
    db.session.commit()
    rec = _recibir_con_averia(db, setup)

    _correr(_job_entrada(rec), _connekta_ok())

    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0


def test_sin_codigo_siesa_no_se_encola(db, setup):
    setup['producto'].codigo_siesa = None
    db.session.commit()
    rec = _recibir_con_averia(db, setup)

    _correr(_job_entrada(rec), _connekta_ok())

    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0


def test_el_modo_ensayo_no_marca_el_movimiento(db, setup):
    """Con el POST bloqueado del lado del servidor no hubo traslado real: marcar
    el movimiento dejaría el reintento bloqueado por una idempotencia vacía."""
    rec = _recibir_con_averia(db, setup)
    ck = _connekta_ok()
    _correr(_job_entrada(rec), ck)
    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    ck.transferir_a_averias.return_value = {'codigo': 0, 'modo_ensayo': True}

    _correr(job, ck)

    mov = MovimientoInventario.query.filter_by(
        idempotency_key=f'REC-AVE-{rec.id}-{setup["producto"].id}').one()
    assert mov.siesa_sync != 'ENVIADO'


def test_el_handler_ya_no_se_traga_la_llamada_sin_tarea_devolucion(db, setup):
    """El defecto que hacía inútil encolar: sin `TareaDevolucion` el handler
    devolvía éxito y NO llamaba a Siesa. Con el ancla de movimiento, llama."""
    rec = _recibir_con_averia(db, setup)
    ck = _connekta_ok()
    _correr(_job_entrada(rec), ck)
    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    assert 'tarea_id' not in json.loads(job.payload)

    _correr(job, ck)
    ck.transferir_a_averias.assert_called_once()


# ── Dirección contraria ────────────────────────────────────────────────────

def test_sin_averias_no_se_encola_nada(db, setup):
    rec = _recibir_con_averia(db, setup, averiadas=0, recibidas=10)
    _correr(_job_entrada(rec), _connekta_ok())
    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0


def test_no_se_duplica_aunque_el_encolado_corra_dos_veces(db, setup):
    """El dedup se ejercita LLAMANDO LA FUNCIÓN dos veces, no reintentando el
    job de entrada: ese reintento corta antes por la idempotencia de la propia
    entrada y nunca llega acá. El camino real que lo alcanza es el de
    emergencia — Siesa OK pero `siesa_triggered` sin persistir, y la DLQ
    reintentando."""
    rec = _recibir_con_averia(db, setup)
    ck = _connekta_ok()
    _correr(_job_entrada(rec), ck)
    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 1

    with patch('app.services.connekta_gateway.connekta', ck):
        sjs._encolar_averias_de_recepcion(rec)
    db.session.commit()

    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 1


def test_tras_enviarlo_tampoco_se_reencola(db, setup):
    """Y si el traslado YA se envió, el movimiento dice ENVIADO y no hay job
    nuevo aunque alguien vuelva a encolar."""
    rec = _recibir_con_averia(db, setup)
    ck = _connekta_ok()
    _correr(_job_entrada(rec), ck)
    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    _correr(job, ck)
    job.estado = 'COMPLETADO'
    db.session.commit()

    with patch('app.services.connekta_gateway.connekta', ck):
        sjs._encolar_averias_de_recepcion(rec)
    db.session.commit()

    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 1


# ── La avería detectada en picking ─────────────────────────────────────────

def _averiar_en_picking(db, s, cantidad=8):
    """Produce la avería por el camino real: auditoría de una tarea bloqueada."""
    from app.models.picking import TareaPicking, EstadoPicking
    from app.models.inventario import UbicacionProducto
    from app.services.picking_service import PickingService

    db.session.add(UbicacionProducto(ubicacion_id=s['ubicacion'].id,
                                     producto_id=s['producto'].id,
                                     cantidad=cantidad, bloqueado=cantidad))
    t = TareaPicking(codigo='PICK-AVE-9', producto_id=s['producto'].id,
                     cantidad_solicitada=cantidad, ubicacion_id=s['ubicacion'].id,
                     almacen_id=s['almacen'].id, estado=EstadoPicking.BLOQUEADO,
                     motivo_bloqueo='MERCANCIA_AVERIADA')
    db.session.add(t); db.session.commit()
    PickingService.auditar_tarea(t.id, s['usuario'].id, 'AVERIA',
                                 cantidad_hallada=cantidad)
    db.session.commit()
    return t


def test_la_averia_de_picking_tambien_le_avisa_a_siesa(db, setup):
    """Era el otro camino mudo: el WMS la sacaba del FEFO y Siesa la seguía
    vendiendo. Acá no hay que esperar ningún documento previo — las unidades ya
    están en la bodega para Siesa."""
    t = _averiar_en_picking(db, setup)

    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    p = json.loads(job.payload)
    assert p['cantidad'] == 8
    assert p['item_codigo'] == 'PROD-001'
    assert t.codigo in p['referencia']


def test_el_traslado_de_picking_mueve_stock_en_siesa(db, setup):
    _averiar_en_picking(db, setup)
    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    ck = _connekta_ok()

    _correr(job, ck)

    ck.transferir_a_averias.assert_called_once()
    mov = MovimientoInventario.query.filter(
        MovimientoInventario.idempotency_key.like('AUD-AVE-%')).one()
    assert mov.siesa_sync == 'ENVIADO'


def test_picking_en_otra_bodega_no_emite_documento_falso(db, setup):
    setup['almacen'].bodega_siesa_id = 'PC1'
    db.session.commit()
    _averiar_en_picking(db, setup)
    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0


def test_otros_veredictos_de_auditoria_no_encolan_nada(db, setup):
    """Dirección contraria: solo AVERIA avisa a Siesa."""
    from app.models.picking import TareaPicking, EstadoPicking
    from app.models.inventario import UbicacionProducto
    from app.services.picking_service import PickingService

    db.session.add(UbicacionProducto(ubicacion_id=setup['ubicacion'].id,
                                     producto_id=setup['producto'].id,
                                     cantidad=5, bloqueado=5))
    t = TareaPicking(codigo='PICK-OK-9', producto_id=setup['producto'].id,
                     cantidad_solicitada=5, ubicacion_id=setup['ubicacion'].id,
                     almacen_id=setup['almacen'].id,
                     estado=EstadoPicking.BLOQUEADO, motivo_bloqueo='FALTANTE')
    db.session.add(t); db.session.commit()

    # `ENCONTRADO_COMPLETO` dejó de ser un veredicto inerte: desde el
    # 2026-09-18 ajusta contra Siesa como un conteo cíclico focalizado en el
    # SKU, y ese camino se niega a ajustar a ciegas si Siesa no contesta la
    # existencia. Se stubea la consulta —no el ajuste— para que el veredicto
    # recorra su camino real y este test siga midiendo lo que dice medir:
    # que NINGÚN veredicto salvo AVERIA encola un traslado a la bodega de
    # averías.
    #
    # `patch.object` sobre la CLASE, no sobre una instancia: parchear la
    # instancia deja un atributo que sobrevive al teardown y vuelve sordos los
    # parches por clase de otros archivos en la misma sesión de pytest.
    from unittest.mock import patch
    from app.services.conteo_service import ConteoService
    with patch.object(ConteoService, 'consultar_existencia_siesa',
                      return_value=5.0):
        PickingService.auditar_tarea(t.id, setup['usuario'].id,
                                     'ENCONTRADO_COMPLETO', cantidad_hallada=5)
    db.session.commit()

    assert SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').count() == 0
