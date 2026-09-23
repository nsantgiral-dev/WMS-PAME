"""
Un conteo no se convierte en ajuste mientras hay mercancía del SKU en proceso.

## El defecto

El conteo compara el estante contra el teórico de Siesa. Entre que el WMS mueve
mercancía y que su documento entra a Siesa, los dos miden cosas distintas:

- **Venta**: recogida del estante, pedido sin remisión → faltante falso. Si se
  ajusta, la remisión la descuenta otra vez.
- **Traslado saliente**: recogido, sin STS → faltante falso en el origen.
- **Recepción**: en el estante, sin EntradaOC → sobrante falso. Si se ajusta,
  entra doble cuando llegue la entrada.
- **Avería**: separada del estante, sin traslado a AV1 → faltante falso.
- **Devolución de cliente**: de vuelta en la bodega, NC sin aprobar (la
  aprobación reingresa el inventario a Siesa) → sobrante falso.

`motivo_bloqueo_ajuste` solo cubría el traslado ENTRANTE.

## La clase

«Se ajusta un SKU con un documento del WMS todavía en camino a Siesa». Un
núcleo (`procesos_en_curso`), juzgado contra el INSTANTE del conteo, y el caso 7
de `motivo_bloqueo_ajuste`: el auto-ajuste CC1==CC2, la aprobación, la auditoría
de picking y la pantalla lo heredan sin tocarlos.

## Cómo se arma cada situación

Con los servicios reales: `PickingService` (crear, iniciar, confirmar),
`PackingService.crear_desde_picking`, `RecepcionService`, el handler real de la
cola (`siesa_job_service._ejecutar_job`) y `DevolucionClienteService`. Lo único
que se escribe a mano es lo que en producción escribe Siesa o un camino que
llama a Siesa (el cierre de un traslado), y está dicho donde pasa.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import event

from tests.test_conteo_teorico_pos import SKU, _jobs, siesa, tienda  # noqa: F401 (fixtures)


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


@pytest.fixture
def cd(db, tienda):
    """La tienda del fixture, movida a la bodega CD (NB1, CO 003): la única a la
    que el conector de averías sabe emitir (`encolar_traslado_averias`)."""
    tienda['almacen'].bodega_siesa_id = 'NB1'
    tienda['almacen'].centro_op_siesa = '003'
    db.session.commit()
    return tienda


def _contar_cc1_cc2(tienda, siesa, fisico, existencia, pos=0):
    """CC1 y CC2 con el mismo físico contra la misma foto: coinciden, y si nada
    lo bloquea el ajuste sale solo."""
    from app.models.conteo import SesionConteo
    svc = _svc()
    siesa.poner(existencia=existencia, pos=pos)
    creado = svc.crear_conteo_manual(tienda['almacen'].id, SKU)
    cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one().id
    svc.obtener_tarea_operario(cc1, tienda['a'].id)
    r1 = svc.registrar_conteo(cc1, tienda['a'].id, fisico)
    assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
    svc.obtener_tarea_operario(r1['segundo_conteo_id'], tienda['b'].id)
    r2 = svc.registrar_conteo(r1['segundo_conteo_id'], tienda['b'].id, fisico)
    assert r2['resultado'] == 'DESCUADRE', r2
    return cc1, r2


def _exigir_bloqueado(db, tienda, cc1, r2, *frases):
    """No se encoló el ajuste, el mensaje nombra el documento, y el supervisor
    tampoco puede aprobarlo."""
    assert r2['auto_encolado'] is False, r2
    msg = r2['ajuste_bloqueado'] or ''
    assert 'en proceso' in msg, msg
    for f in frases:
        assert f in msg, (f, msg)
    with pytest.raises(ValueError, match='en proceso'):
        _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
    db.session.rollback()
    assert _jobs(cc1) == []


def _raiz(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Venta: recogido y sin remisión
# ─────────────────────────────────────────────────────────────────────────────

PEDIDO = 'PD-EP-1'


def _picking(tienda, *, recoger=3, confirmar=True, tipo='PEDIDO', ref=PEDIDO):
    from app.services.picking_service import PickingService
    tareas = PickingService.crear_tareas(
        producto_id=tienda['producto'].id, cantidad=recoger,
        almacen_id=tienda['almacen'].id, referencia_documento=ref,
        tipo_documento=tipo)
    for t in tareas:
        PickingService.iniciar_picking(t.id, tienda['a'].id)
        if confirmar:
            PickingService.confirmar_picking(t.id, recoger, tienda['a'].id)
    return tareas


def _empacar(tienda, tareas):
    from app.services.packing_service import PackingService
    return PackingService.crear_desde_picking(
        tareas_picking_ids=[t.id for t in tareas], numero_pedido_siesa=PEDIDO,
        almacen_id=tienda['almacen'].id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='1')


def _remisionar(db, packing):
    """El último paso real de DESPACHO_F470 (142945 → 142943): marca la tarea
    DESPACHADO con `siesa_triggered_at`. Lo anterior habla con Siesa."""
    from app.models.packing import TareaPacking
    from app.services.despacho_parcial_service import DespachoParialService
    DespachoParialService._persistir_resultado(
        db.session.get(TareaPacking, packing.id), 'RM-77', {'codigo': 0})
    db.session.commit()


class TestVentaSinRemision:

    def test_recogido_sin_empacar_no_ajusta(self, db, siesa, tienda):
        _picking(tienda)
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        _exigir_bloqueado(db, tienda, cc1, r2, PEDIDO,
                          f'Recontar cuando se remisione el pedido {PEDIDO}')

    def test_las_estadisticas_lo_agrupan_con_su_clave(self, db, siesa, tienda):
        """El tablero agrupa por la prosa del bloqueo (`metricas.conteo`): sin
        clave propia caería en OTRO."""
        from app.services.metricas.conteo import resumir_motivo_bloqueo
        _picking(tienda)
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        motivo = _svc().motivo_bloqueo_ajuste(_raiz(db, cc1))
        assert resumir_motivo_bloqueo(motivo) == 'MERCANCIA_EN_PROCESO', motivo

    def test_empacado_sin_remision_no_ajusta(self, db, siesa, tienda):
        _empacar(tienda, _picking(tienda))
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        _exigir_bloqueado(db, tienda, cc1, r2, PEDIDO)

    def test_picking_en_curso_no_ajusta(self, db, siesa, tienda):
        _picking(tienda, confirmar=False)
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        _exigir_bloqueado(db, tienda, cc1, r2, PEDIDO, 'en picking')

    def test_remisionado_antes_del_conteo_si_ajusta(self, db, siesa, tienda):
        """El caso sano: la remisión ya bajó a Siesa cuando se contó."""
        _remisionar(db, _empacar(tienda, _picking(tienda)))
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 6, existencia=7)
        assert r2['auto_encolado'] is True, r2
        assert len(_jobs(cc1)) == 1

    def test_remision_despues_del_conteo_sigue_bloqueando(self, db, siesa, tienda):
        """Se juzga contra el instante del conteo, no contra ahora: el delta se
        fijó con la mercancía fuera del estante y todavía en Siesa."""
        packing = _empacar(tienda, _picking(tienda))
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        assert r2['auto_encolado'] is False
        _remisionar(db, packing)
        assert _svc().mercancia_en_proceso(_raiz(db, cc1)), 'la remisión llegó tarde'
        with pytest.raises(ValueError, match='en proceso'):
            _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)

    def test_picking_empezado_despues_del_conteo_no_bloquea(self, db, siesa, tienda):
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        assert r2['auto_encolado'] is True, r2
        _picking(tienda)
        assert _svc().mercancia_en_proceso(_raiz(db, cc1)) is None

    def test_empaque_cancelado_sin_remision_se_declara(self, db, siesa, tienda):
        """No hay fecha que diga cuándo volvió la mercancía (si volvió): cuenta
        como vivo, nombra el pedido y lo marca sin fecha (Regla 0)."""
        from app.services.packing_service import PackingService
        packing = _empacar(tienda, _picking(tienda))
        PackingService.cancelar(packing.id, motivo='pedido anulado')
        h = _svc().procesos_en_curso(tienda['almacen'].id,
                                     producto_id=tienda['producto'].id)
        assert [x['clase'] for x in h] == ['VENTA']
        assert h[0]['sin_fecha'] is True
        assert 'canceló sin remisión' in h[0]['detalle']

    def test_otro_producto_no_bloquea(self, db, siesa, tienda):
        from app.models.inventario import UbicacionProducto
        from app.models.producto import Producto
        from app.services.picking_service import PickingService
        otro = Producto(codigo='OTRO-EP', nombre='Otro', codigo_siesa='OTRO-EP',
                        unidad_negocio_id='001')
        db.session.add(otro)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=tienda['ubicacion'].id,
                                         producto_id=otro.id, cantidad=10,
                                         reservado=0, bloqueado=0))
        db.session.commit()
        for t in PickingService.crear_tareas(producto_id=otro.id, cantidad=2,
                                             almacen_id=tienda['almacen'].id,
                                             referencia_documento='PD-OTRO',
                                             tipo_documento='PEDIDO'):
            PickingService.iniciar_picking(t.id, tienda['a'].id)
            PickingService.confirmar_picking(t.id, 2, tienda['a'].id)
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        assert r2['auto_encolado'] is True, r2


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Traslado saliente: recogido y sin STS
# ─────────────────────────────────────────────────────────────────────────────

TRASLADO = 'ST-EP-1'


def _solicitud(db, tienda):
    """La solicitud se escribe a mano en ENVIADA: `aprobar_solicitud` y el
    cierre del packing hablan con Siesa (RIT, STS). Queda fuera de EN_PICKING a
    propósito, para que confirmar el picking no dispare el cierre automático."""
    from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
    s = SolicitudTraslado(codigo=TRASLADO, bodega_origen_siesa='NS1',
                          bodega_destino_siesa='PC1', estado='ENVIADA',
                          solicitante_id=tienda['supervisor'].id)
    db.session.add(s)
    db.session.flush()
    db.session.add(ItemSolicitudTraslado(
        solicitud_id=s.id, producto_id=tienda['producto'].id,
        producto_codigo_siesa=SKU, cantidad_solicitada=3))
    db.session.commit()
    return s


def _sts(db, s):
    """Lo que escribe el handler DESPACHO_TRASLADO al volver el 173076."""
    s.siesa_salida_consec = 555
    s.estado = 'EN_TRANSITO'
    s.fecha_despacho = datetime.utcnow()
    db.session.commit()


class TestTrasladoSalienteSinSTS:

    def test_recogido_sin_sts_no_ajusta(self, db, siesa, tienda):
        _solicitud(db, tienda)
        _picking(tienda, tipo='TRASLADO', ref=TRASLADO)
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        _exigir_bloqueado(db, tienda, cc1, r2, TRASLADO,
                          f'Recontar cuando el traslado {TRASLADO} salga en Siesa')

    def test_despachado_en_fisico_sin_sts_no_ajusta(self, db, siesa, tienda):
        """`TrasladoService.despachar` pone `fecha_despacho` aunque Siesa haya
        rechazado el STS: sin consecutivo no hay salida en Siesa."""
        s = _solicitud(db, tienda)
        _picking(tienda, tipo='TRASLADO', ref=TRASLADO)
        s.estado, s.fecha_despacho = 'EN_TRANSITO', datetime.utcnow()
        db.session.commit()
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        _exigir_bloqueado(db, tienda, cc1, r2, TRASLADO)

    def test_sts_antes_del_conteo_si_ajusta(self, db, siesa, tienda):
        s = _solicitud(db, tienda)
        _picking(tienda, tipo='TRASLADO', ref=TRASLADO)
        _sts(db, s)
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 6, existencia=7)
        assert r2['auto_encolado'] is True, r2


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Recepción: en el estante y sin EntradaOC
# ─────────────────────────────────────────────────────────────────────────────

def _oc_siesa():
    return {'detalle': {'Table': [{
        'f200_nit_prov': 'PROV-001', 'f202_id_sucursal_prov': '001',
        'f120_referencia': SKU, 'f150_id': 'NB1',
        'f421_id_unidad_medida': 'UND', 'f421_fecha_entrega': '20260717',
        'f421_id_motivo': '01', 'f120_id': '', 'f420_id_co': '003',
        'f420_id_moneda_docto': 'COP', 'f420_id_moneda_conv': 'COP',
        'f420_id_moneda_local': 'COP', 'f420_tasa_conv': '1',
        'f420_tasa_local': '1', 'f200_nit_comprador': '',
    }]}}


def _recibir(tienda, recibidas=5, averiadas=0, confirmar=True):
    from app.services.recepcion_service import RecepcionService
    with patch('app.services.recepcion_service.connekta') as ck, \
         patch('app.services.siesa_job_service.disparar_dlq_inmediato'):
        ck.get_ordenes_compra_aprobadas.return_value = _oc_siesa()
        ck.centro_op = '003'
        rec = RecepcionService.crear_recepcion(
            numero_oc_siesa='OC-EP-9', almacen_id=tienda['almacen'].id,
            proveedor_codigo='PROV-001', proveedor_nombre='P',
            co_oc_siesa='003', tipo_docto_oc_siesa='OCN', consec_docto_oc_siesa='9',
            items=[{'producto_id': tienda['producto'].id,
                    'cantidad_ordenada': recibidas, 'tolerancia_exceso_pct': 10.0}])
        RecepcionService.iniciar(rec.id, tienda['a'].id)
        RecepcionService.escanear_producto(rec.id, tienda['producto'].id, recibidas)
        if averiadas:
            RecepcionService.declarar_averia(rec.id, tienda['producto'].id, averiadas,
                                             motivo='Cajas mojadas')
        if confirmar:
            RecepcionService.confirmar_recepcion(rec.id)
    return rec


def _connekta_ok():
    ck = MagicMock()
    ck.bodega = 'NB1'
    ck.bodega_averias = 'AV1'
    ck.centro_op = '003'
    ck.confirmar_entrada_compras.return_value = {'codigo': 0, 'mensaje': 'OK'}
    ck.transferir_a_averias.return_value = {'codigo': 0, 'mensaje': 'OK'}
    return ck


def _correr(job, ck):
    """El handler real de la cola, y lo que el bucle hace después: marcarlo."""
    from app.extensions import db
    from app.services import siesa_job_service as sjs
    with patch('app.services.connekta_gateway.connekta', ck):
        res = sjs._ejecutar_job(job)
    job.marcar_completado(res)
    db.session.commit()
    return res


def _job(tipo):
    from app.models.siesa_job import SiesaJob
    return SiesaJob.query.filter_by(tipo=tipo).order_by(SiesaJob.id.desc()).first()


class TestRecepcionSinEntrada:

    def test_confirmada_sin_entrada_no_ajusta(self, db, siesa, cd):
        _recibir(cd)
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 15, existencia=10)
        _exigir_bloqueado(db, cd, cc1, r2, 'OC-EP-9',
                          'Recontar cuando la entrada de la OC OC-EP-9 quede registrada')

    def test_en_curso_no_ajusta(self, db, siesa, cd):
        _recibir(cd, confirmar=False)
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 15, existencia=10)
        _exigir_bloqueado(db, cd, cc1, r2, 'en curso')

    def test_entrada_antes_del_conteo_si_ajusta(self, db, siesa, cd):
        _recibir(cd)
        _correr(_job('ENTRADA_OC'), _connekta_ok())
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 14, existencia=15)
        assert r2['auto_encolado'] is True, r2

    def test_entrada_con_resultado_desconocido_no_ajusta(self, db, siesa, cd):
        """Timeout (Regla 3): la bandera queda puesta y el job va a FALLIDO. No
        se sabe si la entrada llegó: no se ajusta."""
        from app.models.siesa_job import EstadoSiesaJob
        from app.services import siesa_job_service as sjs
        _recibir(cd)
        job = _job('ENTRADA_OC')
        ck = _connekta_ok()
        ck.confirmar_entrada_compras.side_effect = sjs._ResultadoDesconocido('timeout')
        with patch('app.services.connekta_gateway.connekta', ck), \
                pytest.raises(sjs._ResultadoDesconocido):
            sjs._ejecutar_job(job)
        job.estado = EstadoSiesaJob.FALLIDO          # lo que hace el despachador
        db.session.commit()
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 15, existencia=10)
        _exigir_bloqueado(db, cd, cc1, r2, 'resultado desconocido')


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Avería: separada del estante y sin traslado a AV1
# ─────────────────────────────────────────────────────────────────────────────

class TestAveriaSinTraslado:

    def test_averia_sin_traslado_no_ajusta(self, db, siesa, cd):
        """La entrada ya llegó a Siesa (con las 2 rotas adentro) y encoló el
        traslado a averías, que todavía no corrió: Siesa cuenta 2 que el estante
        no tiene."""
        _recibir(cd, recibidas=5, averiadas=2)
        _correr(_job('ENTRADA_OC'), _connekta_ok())
        assert _job('TRASLADO_AVERIAS') is not None
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 13, existencia=15)
        _exigir_bloqueado(db, cd, cc1, r2, 'avería', 'OC-EP-9',
                          'Recontar cuando el traslado a averías')

    def test_traslado_fallido_pide_reintentar(self, db, siesa, cd):
        from app.models.siesa_job import EstadoSiesaJob
        _recibir(cd, recibidas=5, averiadas=2)
        _correr(_job('ENTRADA_OC'), _connekta_ok())
        j = _job('TRASLADO_AVERIAS')
        j.estado = EstadoSiesaJob.FALLIDO
        db.session.commit()
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 13, existencia=15)
        _exigir_bloqueado(db, cd, cc1, r2, 'Reintentar el traslado a averías')

    def test_traslado_hecho_antes_del_conteo_si_ajusta(self, db, siesa, cd):
        _recibir(cd, recibidas=5, averiadas=2)
        ck = _connekta_ok()
        _correr(_job('ENTRADA_OC'), ck)
        _correr(_job('TRASLADO_AVERIAS'), ck)
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 12, existencia=13)
        assert r2['auto_encolado'] is True, r2


# ─────────────────────────────────────────────────────────────────────────────
# 5 · Devolución de cliente: de vuelta en la bodega, NC sin aprobar
# ─────────────────────────────────────────────────────────────────────────────

def _devolver(db, tienda, cantidad=4):
    from app.models.packing import TareaPacking
    from app.services.devolucion_cliente_service import DevolucionClienteService
    tp = TareaPacking(codigo='PK-EP-DEV', tipo_documento='PEDIDO', estado='DESPACHADO',
                      almacen_id=tienda['almacen'].id, numero_pedido_siesa='PD-EP-DEV',
                      tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='5',
                      rm_tipo='RM', rm_consec=70, siesa_triggered=True,
                      siesa_triggered_at=datetime.utcnow() - timedelta(days=3))
    db.session.add(tp)
    db.session.commit()
    fila = {'f470_rowid': '999', 'f120_referencia': SKU, 'f470_cant_base': 10,
            'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1'}
    with patch('app.services.devolucion_cliente_service.connekta') as ck, \
         patch('app.services.siesa_job_service.disparar_dlq_inmediato'):
        ck.get_rowids_factura.return_value = [fila]
        dev = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=tp.id, tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=tienda['almacen'].id, recepcionista_id=None,
            lineas=[{'producto_id': tienda['producto'].id, 'codigo_siesa': SKU,
                     'cantidad_facturada': 10, 'cantidad_devuelta': cantidad,
                     'es_averiado': False, 'f470_id_unidad_medida': 'UND',
                     'f150_id_bodega': 'NB1', 'f470_rowid': '999'}])
        DevolucionClienteService.confirmar_entrada_fisica(dev.id, recepcionista_id=tienda['a'].id)
    return dev


def _nc_creada_y_aprobada(db, dev, tienda):
    """El job de la NC marca `siesa_nc_triggered` (habla con Siesa); la
    aprobación la anota contabilidad con el servicio real."""
    from app.models.devolucion_cliente import DevolucionCliente
    from app.services.devolucion_cliente_service import DevolucionClienteService
    d = db.session.get(DevolucionCliente, dev.id)
    d.siesa_nc_triggered, d.siesa_nc_consec = True, '61'
    db.session.commit()
    DevolucionClienteService.marcar_nc_aprobada(dev.id, tienda['supervisor'].id)


class TestDevolucionSinNCAprobada:

    def test_nc_sin_aprobar_no_ajusta(self, db, siesa, cd):
        dev = _devolver(db, cd)
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 15, existencia=10)
        _exigir_bloqueado(db, cd, cc1, r2, dev.codigo, 'Aprobar la')

    def test_nc_aprobada_antes_del_conteo_si_ajusta(self, db, siesa, cd):
        dev = _devolver(db, cd)
        _nc_creada_y_aprobada(db, dev, cd)
        cc1, r2 = _contar_cc1_cc2(cd, siesa, 13, existencia=14)
        assert r2['auto_encolado'] is True, r2


# ─────────────────────────────────────────────────────────────────────────────
# El generador: el mismo núcleo, juzgado contra ahora
# ─────────────────────────────────────────────────────────────────────────────

class TestVarianteDelGenerador:

    def test_ve_lo_mismo_que_el_ajuste(self, db, siesa, tienda):
        pid, aid = tienda['producto'].id, tienda['almacen'].id
        assert _svc().mercancia_en_proceso_ahora(pid, aid) is None
        assert _svc().productos_con_mercancia_en_proceso(aid) == {}
        _picking(tienda)
        ahora = _svc().mercancia_en_proceso_ahora(pid, aid)
        assert PEDIDO in (ahora or '')
        lote = _svc().productos_con_mercancia_en_proceso(aid)
        assert set(lote) == {pid} and PEDIDO in lote[pid]

    def test_incluye_el_traslado_entrante(self, db, tienda):
        from app.models.traslado import ItemSolicitudTraslado, SolicitudTraslado
        s = SolicitudTraslado(codigo='ST-EP-ENT', bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NS1', estado='EN_TRANSITO',
                              solicitante_id=tienda['supervisor'].id,
                              fecha_despacho=datetime.utcnow() - timedelta(hours=1))
        db.session.add(s)
        db.session.flush()
        db.session.add(ItemSolicitudTraslado(
            solicitud_id=s.id, producto_id=tienda['producto'].id,
            producto_codigo_siesa=SKU, cantidad_solicitada=5, cantidad_enviada=5))
        db.session.commit()
        ahora = _svc().mercancia_en_proceso_ahora(tienda['producto'].id,
                                                   tienda['almacen'].id)
        assert 'ST-EP-ENT' in (ahora or '')

    def test_remisionado_hoy_ya_no_esta_en_proceso(self, db, siesa, tienda):
        _remisionar(db, _empacar(tienda, _picking(tienda)))
        assert _svc().mercancia_en_proceso_ahora(tienda['producto'].id,
                                                 tienda['almacen'].id) is None


# ─────────────────────────────────────────────────────────────────────────────
# Rendimiento: `to_dict` lo evalúa por fila DESCUADRE
# ─────────────────────────────────────────────────────────────────────────────

class TestConsultaAcotada:

    def _consultas(self, db, sesion):
        n = [0]

        def contar(*_a, **_k):
            n[0] += 1
        motor = db.engine
        event.listen(motor, 'before_cursor_execute', contar)
        try:
            _svc().mercancia_en_proceso(sesion)
        finally:
            event.remove(motor, 'before_cursor_execute', contar)
        return n[0]

    def test_el_numero_de_consultas_no_crece_con_la_historia(self, db, siesa, tienda):
        """Veinte pedidos ya remisionados del mismo SKU no agregan consultas:
        el filtro por remisión va en SQL (NOT EXISTS), no fila por fila."""
        from app.models.inventario import UbicacionProducto
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        cc1, _ = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        base = self._consultas(db, _raiz(db, cc1))

        reg = UbicacionProducto.query.filter_by(producto_id=tienda['producto'].id).first()
        reg.cantidad = 500
        db.session.commit()
        antes = datetime.utcnow() - timedelta(days=5)
        for i in range(20):
            ts = PickingService.crear_tareas(
                producto_id=tienda['producto'].id, cantidad=1,
                almacen_id=tienda['almacen'].id, referencia_documento=f'PD-H{i}',
                tipo_documento='PEDIDO')
            for t in ts:
                PickingService.iniciar_picking(t.id, tienda['a'].id)
                PickingService.confirmar_picking(t.id, 1, tienda['a'].id)
                t.fecha_inicio = antes
            pk = PackingService.crear_desde_picking(
                tareas_picking_ids=[t.id for t in ts], numero_pedido_siesa=f'PD-H{i}',
                almacen_id=tienda['almacen'].id)
            pk.siesa_triggered, pk.siesa_triggered_at = True, antes + timedelta(hours=1)
            pk.estado = 'DESPACHADO'
        db.session.commit()
        raiz = _raiz(db, cc1)
        assert _svc().mercancia_en_proceso(raiz) is None
        assert self._consultas(db, raiz) == base

    def test_las_consultas_por_producto_usan_su_indice(self, db, siesa, tienda):
        """Acotada por producto, no por almacén: el plan de cada consulta del
        núcleo sobre tablas que crecen con la operación usa el índice por
        producto (m031). Se capturan las sentencias reales y se les pide el plan
        a sqlite con los mismos parámetros."""
        cc1, _ = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        raiz = _raiz(db, cc1)
        capturadas = []

        def capturar(_conn, _cur, sentencia, parametros, _ctx, _many):
            capturadas.append((sentencia, parametros))
        event.listen(db.engine, 'before_cursor_execute', capturar)
        try:
            _svc().mercancia_en_proceso(raiz)
        finally:
            event.remove(db.engine, 'before_cursor_execute', capturar)

        esperados = {'tareas_picking': 'ix_tareas_picking_producto_almacen',
                     'items_recepcion': 'ix_items_recepcion_producto',
                     'lineas_devolucion_cliente': 'ix_lineas_devolucion_cliente_producto'}
        vistos = {}
        with db.engine.connect() as con:
            for sentencia, parametros in capturadas:
                desde = sentencia.split('FROM', 1)[1] if 'FROM' in sentencia else ''
                for tabla, indice in esperados.items():
                    if f' {tabla}' in desde.split('WHERE', 1)[0]:
                        plan = ' '.join(str(f[-1]) for f in con.exec_driver_sql(
                            'EXPLAIN QUERY PLAN ' + sentencia, parametros).fetchall())
                        vistos[tabla] = plan
                        assert indice in plan, (tabla, plan)
        assert set(vistos) == set(esperados), f'no se vio la consulta de {set(esperados) - set(vistos)}'


# ─────────────────────────────────────────────────────────────────────────────
# El generador no programa lo que el ajuste bloquearía
# ─────────────────────────────────────────────────────────────────────────────

class TestElGeneradorNoProgramaLoQueEstaEnProceso:
    """La misma regla en las dos puertas: `conteo_politica.filtrar_elegibles`
    (generador y watchdog) usa el núcleo del caso 7 de `motivo_bloqueo_ajuste`.
    Programar un SKU con un pedido recogido sin remisión gasta un cupo del día
    en un conteo que después no puede ajustar."""

    def _clasificar(self, db, tienda):
        from app.models.producto_clasificacion_abc import ProductoClasificacionABC
        db.session.add(ProductoClasificacionABC(producto_id=tienda['producto'].id,
                                                almacen_id=tienda['almacen'].id,
                                                clasificacion='A'))
        db.session.commit()

    def _del_plan(self, tienda):
        from app.models.conteo import SesionConteo
        return SesionConteo.query.filter_by(producto_id=tienda['producto'].id,
                                            tipo='DIARIO_ABC').count()

    def test_recogido_sin_remision_no_entra_al_plan(self, db, siesa, tienda):
        from app.services.abc_service import ABCService
        self._clasificar(db, tienda)
        _picking(tienda)
        r = ABCService.generar_tareas_conteo_diario(tienda['almacen'].id)
        assert self._del_plan(tienda) == 0, r
        assert r['excluidos_elegibilidad'] == {'mercancia_en_proceso': 1}, r

    def test_remisionado_vuelve_al_plan(self, db, siesa, tienda):
        from app.services.abc_service import ABCService
        self._clasificar(db, tienda)
        _remisionar(db, _empacar(tienda, _picking(tienda)))
        r = ABCService.generar_tareas_conteo_diario(tienda['almacen'].id)
        assert self._del_plan(tienda) == 1, r
        assert r['excluidos_elegibilidad'] == {}, r
