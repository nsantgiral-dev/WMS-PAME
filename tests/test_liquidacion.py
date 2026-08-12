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


def _mock_rowids_una_linea(codigo_siesa='PROD-001', cant_base=5, vlr_neto=100000):
    """PARCIAL/RECHAZADO ahora arman una DevolucionCliente (_crear_devolucion_pendiente)
    que necesita get_rowids_factura real para construir sus líneas — antes
    _encolar_nota_credito no tocaba Siesa en este punto, solo encolaba el job."""
    return patch('app.services.connekta_gateway.connekta.get_rowids_factura', return_value=[{
        'f120_referencia': codigo_siesa, 'f470_cant_base': cant_base,
        'f470_vlr_neto': vlr_neto, 'f470_id_unidad_medida': 'UND',
        'f150_id': 'NB1', 'f470_rowid': '123',
    }])


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

    def test_parcial_contado_encola_nc_y_rc(self, app, db, recaudo_liq, producto):
        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 2}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000, items_ent=items)
        with _mock_rowids_una_linea(codigo_siesa=producto.codigo_siesa):
            resultado = _run_procesar(recaudo, db)
        assert resultado['nc'] == 1
        assert resultado['rc'] == 1

        from app.models.siesa_job import SiesaJob
        from app.models.devolucion_cliente import DevolucionCliente
        rc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').all()
        assert len(rc) == 1
        # NC ya no se encola directo — se arma una devolución pendiente que
        # recepción confirma; ESO dispara la NC real (251126).
        assert SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='NOTA_CREDITO_FACTURA').count() == 0
        devolucion = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo.id).first()
        assert devolucion is not None
        assert devolucion.estado == 'ABIERTA'
        assert devolucion.es_total is False
        assert len(devolucion.lineas) == 1
        assert float(devolucion.lineas[0].cantidad_devuelta) == 2

    def test_parcial_rc_depende_de_nc(self, app, db, recaudo_liq, producto):
        """RC de parcial contado debe tener depende_de_nc=True — sigue esperando
        a que la NC salga, solo que ahora la dispara recepción al confirmar la
        devolución pendiente, no Liquidación directo."""
        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 2}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=800000, items_ent=items)
        with _mock_rowids_una_linea(codigo_siesa=producto.codigo_siesa):
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

    def test_credito_parcial_solo_nc(self, app, db, recaudo_liq, producto):
        items = [{'codigo': producto.codigo, 'cantidad_devuelta': 1}]
        recaudo = recaudo_liq(estado='PARCIAL', pago='CREDITO', monto=0, items_ent=items)
        with _mock_rowids_una_linea(codigo_siesa=producto.codigo_siesa):
            resultado = _run_procesar(recaudo, db)
        assert resultado['nc'] == 1
        assert resultado['rc'] == 0
        assert resultado['dc'] == 0

        from app.models.devolucion_cliente import DevolucionCliente
        devolucion = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo.id).first()
        assert devolucion is not None
        assert devolucion.es_total is False


class TestRechazado:

    def test_rechazado_encola_nc_total(self, app, db, recaudo_liq, producto):
        recaudo = recaudo_liq(estado='RECHAZADO', pago='EFECTIVO', monto=0)
        with _mock_rowids_una_linea(codigo_siesa=producto.codigo_siesa):
            resultado = _run_procesar(recaudo, db)
        assert resultado['nc'] == 1
        assert resultado['rc'] == 0

        # Ya no se encola NOTA_CREDITO_FACTURA directo — Rechazado arma una
        # devolución pendiente TOTAL (todas las líneas de la factura real).
        from app.models.siesa_job import SiesaJob
        from app.models.devolucion_cliente import DevolucionCliente
        assert SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='NOTA_CREDITO_FACTURA').count() == 0
        devolucion = DevolucionCliente.query.filter_by(recaudo_entrega_id=recaudo.id).first()
        assert devolucion is not None
        assert devolucion.es_total is True
        assert len(devolucion.lineas) == 1
        assert float(devolucion.lineas[0].cantidad_devuelta) == 5  # = cant_base mockeada (línea completa)

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

# TestEnviarNCRecaudo se eliminó junto con LiquidacionService.enviar_nc_recaudo
# — ese camino disparaba la NC directo (250696, sin cruce automático),
# bypaseando la devolución pendiente que ahora arma _crear_devolucion_pendiente.
# Dejarlo vivo habría sido un atajo peligroso al lado del flujo real.

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
        # Forma real verificada en vivo (2026-08-11): lista de filas, una por
        # factura del cliente — NUNCA un dict único. tipo/consec deben matchear
        # tarea.tipo_docto_pedido_siesa/consec_docto_pedido_siesa del fixture
        # recaudo_liq ('PD'/999) para que registrar_cobro_recaudo la encuentre.
        mock_connekta.get_cxc_general = MagicMock(return_value=[
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': 999,
             'f253_id': '13050502', 'f353_total_db': 2000000, 'f353_total_cr': 0},
        ])
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

    def test_matchea_f253_id_correcto_entre_varias_filas(self, app, db, recaudo_liq):
        """
        get_cxc_general puede devolver varias filas del mismo cliente con
        f253_id DISTINTO por factura (verificado en vivo 2026-08-11, NIT
        1000124053: 9 filas con 13050501, 2 con 13050502) — hay que tomar
        la fila de LA factura que se está cobrando, no la primera del
        cliente ni una de otra factura.
        """
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        mock_connekta = self._mock_siesa()
        # Decoy (otra factura del mismo cliente, distinto f253_id) + la real
        mock_connekta.get_cxc_general.return_value = [
            {'f353_id_tipo_docto_cruce': 'FE', 'f353_consec_docto_cruce': 1,
             'f253_id': '99999999', 'f353_total_db': 100, 'f353_total_cr': 0},
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': 999,
             'f253_id': '13050502', 'f353_total_db': 2000000, 'f353_total_cr': 0},
        ]
        from app.services.liquidacion_service import LiquidacionService
        from app.models.siesa_job import SiesaJob
        with patch('app.services.connekta_gateway.connekta', mock_connekta), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            LiquidacionService.registrar_cobro_recaudo(recaudo.id, admin_id=1, retenciones=[])

        job = SiesaJob.query.filter_by(tipo='RECIBO_CAJA', referencia_id=recaudo.id).first()
        assert job is not None
        assert job.get_payload()['cuenta_cxc'] == '13050502', (
            'Tomó el f253_id de la factura equivocada (o el fallback)'
        )

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
