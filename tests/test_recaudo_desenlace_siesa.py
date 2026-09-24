"""
El desenlace de NC/RC/DC queda en el recaudo, no solo en la cola (m036fotos).

`siesa_jobs` guarda qué pasó con cada documento, pero es una COLA: se
descarta, se reintenta, su `referencia_tipo/referencia_id` es polimórfico sin
FK. La analítica pedido → caja necesita la respuesta en la entidad de negocio.

Lo que se exige:
- un solo escritor (`RecaudoEntrega.anotar_documento_siesa`) y un solo
  vocabulario (`RESULTADOS_SIESA`);
- un `FALLIDO` posterior no borra un `ENVIADO`: el documento existe;
- el consecutivo se anota solo si Siesa lo devolvió — no se inventa;
- el modo ensayo no anota nada (no se creó ningún documento);
- el DLQ lo escribe en cada desenlace: enviado, ya saldada, sin verificar,
  fallido.
"""
import json
from unittest.mock import patch

import pytest


@pytest.fixture
def recaudo(db, almacen):
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    from app.models.usuario import Usuario
    cond = Usuario(email='cond_desenlace@test.com', nombre='C', rol='conductor', activo=True)
    cond.set_password('x')
    db.session.add(cond)
    db.session.flush()
    ruta = RutaDespacho(conductor_id=cond.id, tipo_ruta='Urbana', estado='ENTREGADA')
    db.session.add(ruta)
    db.session.flush()
    t = TareaPacking(codigo='PK-DES', estado='DESPACHADO', almacen_id=almacen.id,
                     numero_pedido_siesa='PD5', tipo_docto_pedido_siesa='PD',
                     consec_docto_pedido_siesa='5')
    db.session.add(t)
    db.session.flush()
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='ENTREGADO',
                       forma_pago='EFECTIVO', monto_cobrado=1000)
    db.session.add(r)
    db.session.commit()
    return r


class TestElEscritor:

    def test_anota_resultado_momento_y_consecutivo_si_viene(self, db, recaudo):
        recaudo.anotar_documento_siesa('RC', 'ENVIADO', respuesta={
            'codigo': 0, 'detalle': {'Table': [{'f350_consec_docto': 2744}]}})
        assert recaudo.siesa_rc_resultado == 'ENVIADO'
        assert recaudo.siesa_rc_consec == '2744'
        assert recaudo.siesa_rc_at is not None

    def test_la_respuesta_real_de_siesa_no_trae_consecutivo(self, db, recaudo):
        """La forma medida en vivo (CLAUDE.md, 18 documentos de traslado)."""
        recaudo.anotar_documento_siesa('NC', 'ENVIADO', respuesta={
            'codigo': 0, 'mensaje': 'Transacción Exitosa', 'detalle': 'Importacion exitosa'})
        assert recaudo.siesa_nc_resultado == 'ENVIADO'
        assert recaudo.siesa_nc_consec is None, 'no se inventa un consecutivo'

    def test_un_fallido_posterior_no_borra_un_enviado(self, db, recaudo):
        recaudo.anotar_documento_siesa('RC', 'ENVIADO', consec=10)
        recaudo.anotar_documento_siesa('RC', 'FALLIDO')
        assert recaudo.siesa_rc_resultado == 'ENVIADO' and recaudo.siesa_rc_consec == '10'

    def test_las_retenciones_son_n_documentos(self, db, recaudo):
        recaudo.anotar_documento_siesa('DC', 'ENVIADO', cuenta_puc='13551501', consec=7)
        recaudo.anotar_documento_siesa('DC', 'FALLIDO', cuenta_puc='13551701')
        recaudo.anotar_documento_siesa('DC', 'FALLIDO', cuenta_puc='13551501')
        det = json.loads(recaudo.siesa_dc_detalle)
        assert det['13551501']['resultado'] == 'ENVIADO' and det['13551501']['consec'] == '7'
        assert det['13551701']['resultado'] == 'FALLIDO'

    @pytest.mark.parametrize('doc,res', [('XX', 'ENVIADO'), ('RC', 'OK'), ('RC', None)])
    def test_vocabulario_cerrado(self, db, recaudo, doc, res):
        with pytest.raises(ValueError):
            recaudo.anotar_documento_siesa(doc, res)

    def test_el_ensayo_no_trae_consecutivo(self):
        from app.models.recaudo_entrega import consecutivo_en_respuesta
        assert consecutivo_en_respuesta({'modo_ensayo': True, 'consecutivo': 9}) is None
        assert consecutivo_en_respuesta({'consecutivo': 9}) == 9
        assert consecutivo_en_respuesta(None) is None


def _job(db, tipo, recaudo, **extra):
    from app.models.siesa_job import SiesaJob
    payload = {'recaudo_id': recaudo.id, 'tercero_nit': '900', 'sucursal': '001',
               'monto': 1000, 'forma_pago': 'EFECTIVO', 'tipo_docto_fe': 'FE',
               'consec_fe': '5020', **extra}
    job = SiesaJob.encolar(tipo, payload, referencia_tipo='RecaudoEntrega',
                           referencia_id=recaudo.id)
    db.session.commit()
    return job


class TestElDLQLoEscribe:

    def test_rc_enviado(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, 'RECIBO_CAJA', recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc, \
                patch('app.services.siesa_job_service._factura_saldada_en_siesa',
                      return_value=False):
            mc.trigger_recibo_caja.return_value = {'codigo': 0, 'consecutivo': 2744}
            _ejecutar_job(job)
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_resultado == 'ENVIADO' and recaudo.siesa_rc_consec == '2744'

    def test_rc_en_ensayo_no_anota(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, 'RECIBO_CAJA', recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc, \
                patch('app.services.siesa_job_service._factura_saldada_en_siesa',
                      return_value=False):
            mc.trigger_recibo_caja.return_value = {'modo_ensayo': True}
            _ejecutar_job(job)
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_resultado is None

    def test_rc_ya_saldada(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, 'RECIBO_CAJA', recaudo)
        with patch('app.services.connekta_gateway.connekta'), \
                patch('app.services.siesa_job_service._factura_saldada_en_siesa',
                      return_value=True):
            _ejecutar_job(job)
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_resultado == 'YA_SALDADA'

    def test_rc_que_no_se_puede_verificar(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, 'RECIBO_CAJA', recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc, \
                patch('app.services.siesa_job_service._factura_saldada_en_siesa',
                      side_effect=[False, None]):
            mc.trigger_recibo_caja.side_effect = Exception('timeout')
            _ejecutar_job(job)
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_resultado == 'SIN_VERIFICAR'

    def test_dc_enviado_por_cuenta(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        recaudo.siesa_rc_triggered = True
        db.session.commit()
        job = _job(db, 'DOCUMENTO_CONTABLE_RET', recaudo, cuenta_puc='13551501')
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_documento_contable.return_value = {'codigo': 0}
            _ejecutar_job(job)
        db.session.refresh(recaudo)
        assert json.loads(recaudo.siesa_dc_detalle)['13551501']['resultado'] == 'ENVIADO'

    def test_el_fallido_del_dlq_queda_en_el_recaudo(self, app, db, recaudo):
        """Reintentos agotados: la cola dice FALLIDO y el recaudo también."""
        from app.services.siesa_job_service import _run_dlq_jobs
        recaudo.siesa_rc_triggered = True
        db.session.commit()
        job = _job(db, 'DOCUMENTO_CONTABLE_RET', recaudo, cuenta_puc='13551501')
        job.intentos = job.max_intentos - 1
        db.session.commit()
        with patch('app.services.connekta_gateway.connekta') as mc, \
                patch('app.services.siesa_job_service._crear_alerta_admin'):
            mc.trigger_documento_contable.side_effect = Exception('Siesa rechazó')
            _run_dlq_jobs()
        db.session.refresh(recaudo)
        db.session.refresh(job)
        assert job.estado == 'FALLIDO'
        assert recaudo.siesa_dc_resultado == 'FALLIDO'

    def test_anotar_no_rompe_el_job(self, app, db, recaudo, monkeypatch):
        """El documento ya salió: un fallo al anotar no puede volverlo FALLIDO."""
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.siesa_job_service import _ejecutar_job

        def _revienta(self, *a, **k):
            raise RuntimeError('columna rota')
        monkeypatch.setattr(RecaudoEntrega, 'anotar_documento_siesa', _revienta)
        job = _job(db, 'RECIBO_CAJA', recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc, \
                patch('app.services.siesa_job_service._factura_saldada_en_siesa',
                      return_value=False):
            mc.trigger_recibo_caja.return_value = {'codigo': 0}
            assert _ejecutar_job(job) == {'codigo': 0}
