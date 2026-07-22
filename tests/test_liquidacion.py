"""
Tier 4 — Tests de negocio: flujos de liquidación (_procesar_recaudo).
Valida que cada combinación estado_entrega × forma_pago dispara los
conectores Siesa correctos (NC, RC, DC).
"""
import pytest
from unittest.mock import patch, MagicMock, call


@pytest.fixture
def recaudo_liq(db, almacen):
    """Factory para RecaudoEntrega con dependencias mínimas."""
    def _make(estado='ENTREGADO', pago='EFECTIVO', monto=1500000,
              rc=False, nc=False, dc=False, motivo_desc=None, items_ent=None):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.packing import TareaPacking
        from app.models.ruta_despacho import RutaDespacho
        from app.models.usuario import Usuario

        conductor = Usuario.query.filter_by(email='cond_liq@test.com').first()
        if not conductor:
            conductor = Usuario(email='cond_liq@test.com', nombre='Conductor Liq',
                                rol='conductor', activo=True)
            conductor.set_password('test123')
            db.session.add(conductor)
            db.session.flush()

        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado='ENTREGADA')
        db.session.add(ruta)
        db.session.flush()

        import uuid
        tarea = TareaPacking(
            codigo=f'PK-LIQ-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
            almacen_id=almacen.id,
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=999,
            numero_pedido_siesa='PED-LIQ',
        )
        db.session.add(tarea)
        db.session.flush()

        recaudo = RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega=estado, forma_pago=pago, monto_cobrado=monto,
            siesa_rc_triggered=rc, siesa_nc_triggered=nc, siesa_dc_triggered=dc,
            motivo_descuento=motivo_desc,
            items_entregados=items_ent,
        )
        db.session.add(recaudo)
        db.session.commit()
        return recaudo
    return _make


def _run_procesar(recaudo, db):
    """Ejecuta _procesar_recaudo con _obtener_tercero mockeado."""
    with patch('app.services.liquidacion_service._obtener_tercero',
               return_value=('900123456', '001')):
        from app.services.liquidacion_service import _procesar_recaudo
        resultado = _procesar_recaudo(recaudo, 'test liquidacion')
        db.session.commit()
        return resultado


# ═══════════════════════════════════════════════════════════════════
# Flujos principales
# ═══════════════════════════════════════════════════════════════════

class TestContadoEntregado:

    def test_contado_entregado_encola_rc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1500000)
        resultado = _run_procesar(recaudo, db)
        assert resultado['rc'] == 1
        assert resultado['nc'] == 0
        assert resultado['dc'] == 0

        from app.models.siesa_job import SiesaJob
        jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').all()
        assert len(jobs) == 1

    def test_contado_entregado_con_retencion_encola_rc_y_dc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO',
                              motivo_desc='RETEFUENTE_2.5', monto=1000000)
        resultado = _run_procesar(recaudo, db)
        assert resultado['rc'] == 1
        assert resultado['dc'] == 1

        from app.models.siesa_job import SiesaJob
        rc_jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').all()
        dc_jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').all()
        assert len(rc_jobs) == 1
        assert len(dc_jobs) == 1

    def test_ya_procesado_no_duplica(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', rc=True)
        resultado = _run_procesar(recaudo, db)
        assert resultado['ya_procesado'] == 1
        assert resultado['rc'] == 0

        from app.models.siesa_job import SiesaJob
        jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').all()
        assert len(jobs) == 0


class TestContadoParcial:

    def test_parcial_contado_encola_nc_y_rc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000)
        resultado = _run_procesar(recaudo, db)
        assert resultado['nc'] == 1
        assert resultado['rc'] == 1

        from app.models.siesa_job import SiesaJob
        nc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='NOTA_CREDITO_FACTURA').all()
        rc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').all()
        assert len(nc) == 1
        assert len(rc) == 1

    def test_parcial_rc_depende_de_nc(self, app, db, recaudo_liq):
        """RC de parcial contado debe tener depende_de_nc=True."""
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000)
        _run_procesar(recaudo, db)

        import json
        from app.models.siesa_job import SiesaJob
        rc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').first()
        payload = json.loads(rc.payload)
        assert payload.get('depende_de_nc') is True


class TestCreditoEntregado:

    def test_credito_entregado_noop(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='CREDITO', monto=2000000)
        resultado = _run_procesar(recaudo, db)
        assert resultado['credito'] == 1
        assert resultado['rc'] == 0
        assert resultado['nc'] == 0

        from app.models.siesa_job import SiesaJob
        jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id).all()
        assert len(jobs) == 0


class TestCreditoParcial:

    def test_credito_parcial_solo_nc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='PARCIAL', pago='CREDITO', monto=0)
        resultado = _run_procesar(recaudo, db)
        assert resultado['nc'] == 1
        assert resultado['rc'] == 0
        assert resultado['dc'] == 0


class TestRechazado:

    def test_rechazado_encola_nc_total(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='RECHAZADO', pago='EFECTIVO', monto=0)
        resultado = _run_procesar(recaudo, db)
        assert resultado['nc'] == 1
        assert resultado['rc'] == 0

        import json
        from app.models.siesa_job import SiesaJob
        nc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='NOTA_CREDITO_FACTURA').first()
        payload = json.loads(nc.payload)
        assert payload.get('es_total') is True

    def test_rechazado_ya_procesado(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='RECHAZADO', pago='EFECTIVO', nc=True)
        resultado = _run_procesar(recaudo, db)
        assert resultado['ya_procesado'] == 1
        assert resultado['nc'] == 0


# ═══════════════════════════════════════════════════════════════════
# Retenciones — PUC correcto
# ═══════════════════════════════════════════════════════════════════

class TestRetencionesPUC:

    def test_retefuente_puc_correcto(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO',
                              motivo_desc='RETEFUENTE_2.5', monto=1000000)
        _run_procesar(recaudo, db)

        import json
        from app.models.siesa_job import SiesaJob
        dc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').first()
        assert dc is not None
        payload = json.loads(dc.payload)
        assert payload['cuenta_puc'] == '13551501'

    def test_sin_motivo_no_encola_dc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO',
                              motivo_desc=None, monto=1000000)
        resultado = _run_procesar(recaudo, db)
        assert resultado['dc'] == 0

        from app.models.siesa_job import SiesaJob
        dc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').all()
        assert len(dc) == 0


# ═══════════════════════════════════════════════════════════════════
# Per-recaudo methods (liquidación guiada)
# ═══════════════════════════════════════════════════════════════════

class TestEnviarNCRecaudo:

    def test_rechazado_encola_nc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='RECHAZADO', pago='EFECTIVO')
        from app.services.liquidacion_service import LiquidacionService
        mock_connekta = MagicMock()
        mock_connekta.get_factura_desde_pedido.return_value = []
        with patch('app.services.connekta_gateway.connekta', mock_connekta), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.enviar_nc_recaudo(recaudo.id, admin_id=1)
        assert resultado['ok'] is True
        assert resultado['job_id'] is not None
        db.session.refresh(recaudo)
        assert recaudo.siesa_nc_triggered is True

    def test_entregado_rechaza_nc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO')
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(ValueError, match='PARCIAL/RECHAZADO'):
            LiquidacionService.enviar_nc_recaudo(recaudo.id, admin_id=1)

    def test_nc_idempotente(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='RECHAZADO', pago='EFECTIVO', nc=True)
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(ValueError, match='idempotencia'):
            LiquidacionService.enviar_nc_recaudo(recaudo.id, admin_id=1)


class TestRegistrarCobroRecaudo:

    def _mock_siesa(self):
        """Mock Siesa API calls for registrar_cobro tests."""
        mock_connekta = MagicMock()
        mock_connekta.get_rowids_factura.return_value = [
            {'f470_vlr_bruto': 1680672, 'f470_vlr_imp': 319328, 'f470_vlr_neto': 2000000,
             'f120_referencia': 'REF001', 'f470_rowid': 'R1'}
        ]
        mock_connekta.get_pedido_cabecera.return_value = {
            'f430_id_co': '003', 'f200_id_pedido_fact': '900123456',
            'f461_id_sucursal_pedido_rem': '001',
        }
        mock_connekta.get_cxc_general = MagicMock(return_value={'f253_id': '13050502'})
        return mock_connekta

    def test_contado_sin_retenciones_rc_bruto(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['ok'] is True
        assert resultado['monto_neto_rc'] == 2000000  # sin retenciones = bruto
        assert len(resultado['dc_jobs']) == 0

    def test_contado_con_retenciones_rc_neto(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1,
                retenciones=[{'tipo': 'RETEFUENTE_2.5'}, {'tipo': 'RETEIVA'}])
        # RC = neto (bruto - retenciones)
        ret_rf = round(1680672 * 0.025, 2)   # RetefFuente 2.5% de base gravable
        ret_iva = round(319328 * 0.15, 2)    # ReteIVA 15% de IVA (NO de base)
        expected_neto = round(2000000 - ret_rf - ret_iva, 2)
        assert resultado['monto_neto_rc'] == expected_neto
        assert len(resultado['dc_jobs']) == 2
        # Verify RETEIVA base is IVA, not base_gravable
        riva_job = [j for j in resultado['dc_jobs'] if j['tipo'] == 'RETEIVA'][0]
        assert riva_job['monto'] == ret_iva

    def test_credito_rechaza_cobro(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='CREDITO', monto=2000000)
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(ValueError, match='CREDITO'):
            LiquidacionService.registrar_cobro_recaudo(recaudo.id, admin_id=1)

    def test_rc_idempotente(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', rc=True)
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(ValueError, match='idempotencia'):
            LiquidacionService.registrar_cobro_recaudo(recaudo.id, admin_id=1)

    def test_parcial_sin_nc_rechaza(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', nc=False)
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(ValueError, match='NC debe ir primero'):
            with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
                LiquidacionService.registrar_cobro_recaudo(recaudo.id, admin_id=1)

    def test_retenciones_detalle_guardado(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1,
                retenciones=[{'tipo': 'RETEFUENTE_2.5'}])
        db.session.refresh(recaudo)
        det = recaudo.retenciones_detalle
        assert det is not None
        assert len(det) == 1
        assert det[0]['tipo'] == 'RETEFUENTE_2.5'
        assert det[0]['puc'] == '13551501'
        assert det[0]['siesa_triggered'] is True
