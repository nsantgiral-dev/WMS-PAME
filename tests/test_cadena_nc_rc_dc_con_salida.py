"""
La cadena NC → RC → DC no espera para siempre, y nada dice «llegó» sin
confirmarlo (P1-6, 2026-09-25).

| | Qué pasaba | Ahora |
|---|---|---|
| a | El DC esperaba su RC con `DependenciaPendiente` sin límite, aunque el RC estuviera FALLIDO, DESCARTADO o nunca encolado: se reprogramaba cada 2 min para siempre | Sin RC vivo en la cola, falla declarado (`ErrorDeterminista`). VTA-63 ve lo que quedó esperando |
| b | El RC de una PARCIAL esperaba `recaudo.siesa_nc_triggered`, que enciende el job de la NC; si ese puente fallaba, esperaba una NC que ya existía | El RC lo detecta (`nc_ya_salio`) y reconstruye el puente con la misma función. VTA-64 ve el que sigue esperando |
| c | Un RC con `verificacion_imposible` terminaba COMPLETADO, y VTA-60 y la reconciliación lo contaban como «llegó» | Sale FALLIDO; y los lectores usan `politica_cobro.rc_llegaron` (señal positiva) |
"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest


@pytest.fixture
def recaudo(db, almacen):
    def _make(estado='PARCIAL', rc=False, nc=False):
        from app.models.conductor import Conductor
        from app.models.packing import TareaPacking
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho
        c = Conductor(nombre='C', cedula=f'C{uuid.uuid4().hex[:8]}', activo=True)
        db.session.add(c)
        db.session.flush()
        ruta = RutaDespacho(conductor_id=c.id, tipo_ruta='Urbana', estado='ENTREGADA')
        db.session.add(ruta)
        db.session.flush()
        t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                         almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa=77)
        db.session.add(t)
        db.session.flush()
        r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega=estado,
                           forma_pago='EFECTIVO', monto_cobrado=50000,
                           siesa_rc_triggered=rc, siesa_nc_triggered=nc)
        db.session.add(r)
        db.session.commit()
        return r
    return _make


def _devolucion_con_nc_salida(db, r, respuesta=True):
    from app.models.devolucion_cliente import DevolucionCliente
    d = DevolucionCliente(codigo=f'DEV-{uuid.uuid4().hex[:6]}', tarea_packing_id=r.tarea_id,
                          tipo_docto_fe='FEW', consec_fe='1', almacen_id=r.tarea.almacen_id,
                          estado='CONFIRMADA', recaudo_entrega_id=r.id,
                          siesa_nc_triggered=True,
                          siesa_nc_response=('{"codigo": 0}' if respuesta else None))
    db.session.add(d)
    db.session.commit()
    return d


def _job(db, tipo, r, viejo=False, **extra):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob.encolar(tipo, {'recaudo_id': r.id, 'tercero_nit': '900', 'monto': 1,
                                'tipo_docto_fe': 'FEW', 'consec_fe': '1', **extra},
                         referencia_tipo='RecaudoEntrega', referencia_id=r.id)
    if viejo:
        j.fecha_creacion = datetime.utcnow() - timedelta(hours=3)
    db.session.commit()
    return j


def _total(codigo):
    from app.services import auditoria
    r = auditoria.auditar('venta')
    return next(x for x in r['resultados'] if x['codigo'] == codigo)['total']


class TestElRCReconstruyeElPuente:

    def test_la_nc_ya_salio_y_el_recaudo_no_lo_sabia(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        r = recaudo()
        _devolucion_con_nc_salida(db, r)
        job = _job(db, 'RECIBO_CAJA', r, depende_de_nc=True)
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja.return_value = {'codigo': 0}
            _ejecutar_job(job)
        db.session.refresh(r)
        assert r.siesa_nc_triggered is True and r.siesa_nc_resultado == 'ENVIADO'
        mc.trigger_recibo_caja.assert_called_once()

    def test_un_pre_flag_sin_respuesta_no_destraba_nada(self, app, db, recaudo):
        """La NC con la bandera puesta y sin respuesta puede ser un timeout sin
        verificar: eso no prueba que salió."""
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        r = recaudo()
        _devolucion_con_nc_salida(db, r, respuesta=False)
        job = _job(db, 'RECIBO_CAJA', r, depende_de_nc=True)
        with patch('app.services.connekta_gateway.connekta') as mc:
            with pytest.raises(DependenciaPendiente):
                _ejecutar_job(job)
        mc.trigger_recibo_caja.assert_not_called()


class TestLaAuditoriaLoVe:

    def test_vta63_ve_un_dc_esperando_un_rc_que_no_esta(self, db, recaudo):
        r = recaudo(estado='ENTREGADO')
        _job(db, 'DOCUMENTO_CONTABLE_RET', r, viejo=True, cuenta_puc='13551501')
        assert _total('VTA-63') == 1

    def test_vta63_no_grita_si_el_rc_esta_en_cola(self, db, recaudo):
        r = recaudo(estado='ENTREGADO')
        _job(db, 'RECIBO_CAJA', r)
        _job(db, 'DOCUMENTO_CONTABLE_RET', r, viejo=True, cuenta_puc='13551501')
        assert _total('VTA-63') == 0

    def test_vta63_no_grita_si_el_rc_ya_salio(self, db, recaudo):
        r = recaudo(estado='ENTREGADO', rc=True)
        _job(db, 'DOCUMENTO_CONTABLE_RET', r, viejo=True, cuenta_puc='13551501')
        assert _total('VTA-63') == 0

    def test_vta64_ve_un_rc_esperando_una_nc_que_ya_salio(self, db, recaudo):
        r = recaudo()
        _devolucion_con_nc_salida(db, r)
        _job(db, 'RECIBO_CAJA', r, viejo=True, depende_de_nc=True)
        assert _total('VTA-64') == 1

    def test_vta64_no_grita_mientras_la_nc_no_sale(self, db, recaudo):
        r = recaudo()
        _job(db, 'RECIBO_CAJA', r, viejo=True, depende_de_nc=True)
        assert _total('VTA-64') == 0

    def test_vta60_ve_el_completado_que_no_confirmaba(self, db, recaudo):
        """Filas anteriores a m036fotos: COMPLETADO con `verificacion_imposible`
        no es «llegó». VTA-60 contaba cualquier COMPLETADO."""
        r = recaudo(estado='ENTREGADO', rc=True)
        j = _job(db, 'RECIBO_CAJA', r)
        j.marcar_completado({'verificacion_imposible': True})
        db.session.commit()
        assert _total('VTA-60') == 1

    def test_vta60_no_grita_por_un_envio_en_curso(self, db, recaudo):
        r = recaudo(estado='ENTREGADO', rc=True)
        j = _job(db, 'RECIBO_CAJA', r)
        j.estado = 'PROCESANDO'
        db.session.commit()
        assert _total('VTA-60') == 0


class TestLaReconciliacionNoCuentaLoQueNoSeVerifico:

    def test_completado_sin_verificar_no_es_rc_en_siesa(self, db, recaudo):
        from app.services.reconciliacion_ruta import _rc_llego_a_siesa
        r = recaudo(estado='ENTREGADO', rc=True)
        j = _job(db, 'RECIBO_CAJA', r)
        j.marcar_completado({'verificacion_imposible': True})
        db.session.commit()
        assert _rc_llego_a_siesa(r) is False
        r.siesa_rc_resultado = 'ENVIADO'
        assert _rc_llego_a_siesa(r) is True


class TestResolverUnReciboSinVerificar:
    """La salida que no existía: el recibo sin verificar no se reenvía solo y
    nada lo destrababa. Una persona busca en Siesa y dice qué vio."""

    def test_no_entro_baja_la_bandera_y_deja_registrar_de_nuevo(self, db, recaudo):
        from app.models.bitacora import BitacoraAccion
        from app.services import politica_cobro as pc
        from app.services.liquidacion_service import LiquidacionService
        r = recaudo(estado='ENTREGADO', rc=True)
        r.siesa_rc_resultado = 'SIN_VERIFICAR'
        db.session.commit()
        LiquidacionService.resolver_recibo_sin_verificar(
            r.id, usuario_id=None, entro=False, motivo='buscado en auditoría, no está')
        db.session.refresh(r)
        assert r.siesa_rc_triggered is False and pc.puede_editar_cobro(r)
        [b] = BitacoraAccion.query.filter_by(accion='FORZAR').all()
        assert b.despues['entro'] is False

    def test_si_entro_queda_llegado_con_su_consecutivo(self, db, recaudo):
        from app.services import politica_cobro as pc
        from app.services.liquidacion_service import LiquidacionService
        r = recaudo(estado='ENTREGADO', rc=True)
        LiquidacionService.resolver_recibo_sin_verificar(
            r.id, usuario_id=None, entro=True, motivo='RC-2745 en auditoría',
            consecutivo='2745')
        db.session.refresh(r)
        assert pc.rc_llego_a_siesa(r) and r.siesa_rc_consec == '2745'

    def test_no_se_resuelve_lo_que_no_esta_pendiente_ni_sin_motivo(self, db, recaudo):
        from app.services.liquidacion_service import LiquidacionService
        r = recaudo(estado='ENTREGADO', rc=False)
        with pytest.raises(ValueError, match='nada que resolver'):
            LiquidacionService.resolver_recibo_sin_verificar(r.id, None, True, 'x')
        r2 = recaudo(estado='ENTREGADO', rc=True)
        with pytest.raises(ValueError):
            LiquidacionService.resolver_recibo_sin_verificar(r2.id, None, True, '   ')

    def test_no_con_un_envio_en_curso(self, db, recaudo):
        from app.services.liquidacion_service import LiquidacionService
        r = recaudo(estado='ENTREGADO', rc=True)
        _job(db, 'RECIBO_CAJA', r)
        with pytest.raises(ValueError, match='en curso'):
            LiquidacionService.resolver_recibo_sin_verificar(r.id, None, False, 'x')
