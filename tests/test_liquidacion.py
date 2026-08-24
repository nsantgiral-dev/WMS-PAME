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
              rc=False, nc=False, dc=False, motivo_desc=None, items_ent=None,
              monto_desc=0):
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
            monto_descuento=monto_desc,
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

    def test_rc_ya_en_cola_no_se_duplica_en_el_barrido_masivo(self, app, db, recaudo_liq):
        """Si `registrar_cobro_recaudo` ya encoló un RC (PENDIENTE, sin
        `siesa_rc_triggered` todavía) y después corre 'Liquidar Ruta'
        (barrido masivo sobre el mismo recaudo), no debe encolar un
        segundo RECIBO_CAJA."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1500000)
        from app.models.siesa_job import SiesaJob
        SiesaJob.encolar(
            tipo='RECIBO_CAJA',
            payload={'recaudo_id': recaudo.id, 'monto': 1500000},
            referencia_tipo='RecaudoEntrega', referencia_id=recaudo.id,
        )
        db.session.commit()

        _run_procesar(recaudo, db)

        jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').all()
        assert len(jobs) == 1

    def test_contado_entregado_con_retencion_encola_rc_y_dc(self, app, db, recaudo_liq):
        """`monto_desc` no viene declarado (default 0) — el motivo entró sin
        monto, como puede pasar con datos históricos (ver comentario en
        `registrar_cobro_recaudo`). El DC tiene que calcularse con la base
        real de Siesa (`f470_vlr_bruto`), no con `monto_cobrado`."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO',
                              motivo_desc='RETEFUENTE_2.5', monto=1000000)
        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=[{'f470_vlr_bruto': 840336, 'f470_vlr_imp': 159664,
                                  'f470_vlr_neto': 1000000, 'f120_referencia': 'REF001',
                                  'f470_rowid': 'R1'}]):
            resultado = _run_procesar(recaudo, db)
        assert resultado['rc'] == 1
        assert resultado['dc'] == 1

        from app.models.siesa_job import SiesaJob
        rc_jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='RECIBO_CAJA').all()
        dc_jobs = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').all()
        assert len(rc_jobs) == 1
        assert len(dc_jobs) == 1
        # Base correcta: 840.336 (f470_vlr_bruto real), no 1.000.000
        # (monto_cobrado, que incluye IVA) — antes esta rama sobreestimaba.
        assert dc_jobs[0].get_payload()['monto'] == round(840336 * 0.025, 2)

    def test_reteiva_por_liquidacion_masiva_usa_el_iva_no_el_monto_cobrado(self, app, db, recaudo_liq):
        """El caso que de verdad importaba: RETEIVA va sobre el IVA, no sobre
        lo cobrado. Con la fórmula vieja (`monto_cobrado * 0.15`) esto salía
        a $150.000 — casi 5x el valor real de $31.932,80."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO',
                              motivo_desc='RETEIVA', monto=1000000)
        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=[{'f470_vlr_bruto': 840336, 'f470_vlr_imp': 159664,
                                  'f470_vlr_neto': 1000000, 'f120_referencia': 'REF001',
                                  'f470_rowid': 'R1'}]):
            _run_procesar(recaudo, db)

        from app.models.siesa_job import SiesaJob
        dc = SiesaJob.query.filter_by(referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').first()
        assert dc is not None
        payload = dc.get_payload()
        assert payload['monto'] == round(159664 * 0.15, 2)
        assert payload['monto'] != round(1000000 * 0.15, 2)

    def test_dc_ya_encolado_no_se_duplica(self, app, db, recaudo_liq):
        """Simula lo que deja `/liquidar-completo`: el DC correcto ya está en
        cola (PENDIENTE) cuando `_procesar_recaudo` corre encima — no debe
        encolar un segundo job para la misma cuenta."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO',
                              motivo_desc='RETEFUENTE_2.5', monto=1000000,
                              monto_desc=21008.40)
        from app.models.siesa_job import SiesaJob
        SiesaJob.encolar(
            tipo='DOCUMENTO_CONTABLE_RET',
            payload={'recaudo_id': recaudo.id, 'cuenta_puc': '13551501',
                     'monto': 21008.40, 'tipo_docto_fe': 'FEW', 'consec_fe': '1'},
            referencia_tipo='RecaudoEntrega', referencia_id=recaudo.id,
        )
        db.session.commit()

        _run_procesar(recaudo, db)

        dc_jobs = SiesaJob.query.filter_by(
            referencia_id=recaudo.id, tipo='DOCUMENTO_CONTABLE_RET').all()
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
        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=[{'f470_vlr_bruto': 840336, 'f470_vlr_imp': 159664,
                                  'f470_vlr_neto': 1000000, 'f120_referencia': 'REF001',
                                  'f470_rowid': 'R1'}]):
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

    def test_get_pedido_cabecera_recibe_el_pedido_no_la_fe(self, app, db, recaudo_liq):
        """`co_factura` salía vacío en producción (PD1411, 2026-08-18):
        `get_pedido_cabecera` se llamaba con el tipo/consec de la FE en vez
        del pedido, y Siesa no encuentra un pedido de tipo 'FEW'. Un mock por
        `return_value` fijo nunca lo habría atrapado — hace falta afirmar CON
        QUÉ se llamó, no solo qué devolvió."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        mock_connekta = self._mock_siesa()
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', mock_connekta), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['ok'] is True
        # 'PD'/999 — tipo_docto_pedido_siesa/consec_docto_pedido_siesa del
        # fixture. NO el tipo/consecutivo de la FE resuelta.
        mock_connekta.get_pedido_cabecera.assert_called_with('PD', '999')

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

    def test_rc_ya_en_cola_bloquea_un_segundo_registro(self, app, db, recaudo_liq):
        """`siesa_rc_triggered` solo se enciende cuando el DLQ procesa el
        job — entre el primer 'Registrar Cobro' y ese momento, un segundo
        clic pasaba el guard de idempotencia limpio y encolaba un segundo
        RECIBO_CAJA para el mismo recaudo. Acá el job ya está PENDIENTE
        (como quedaría tras el primer clic), sin `siesa_rc_triggered` aún."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        from app.models.siesa_job import SiesaJob
        SiesaJob.encolar(
            tipo='RECIBO_CAJA',
            payload={'recaudo_id': recaudo.id, 'monto': 2000000},
            referencia_tipo='RecaudoEntrega', referencia_id=recaudo.id,
        )
        db.session.commit()

        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            with pytest.raises(ValueError, match='Ya hay un Recibo de Caja en cola'):
                LiquidacionService.registrar_cobro_recaudo(recaudo.id, admin_id=1)

        assert SiesaJob.query.filter_by(
            referencia_id=recaudo.id, tipo='RECIBO_CAJA').count() == 1

    def test_parcial_sin_nc_no_bloquea_el_rc(self, app, db, recaudo_liq):
        """2026-08-19: el RC ya no espera a que la NC dispare para poder
        crearse — es un documento distinto (lo que el conductor SÍ entregó,
        no lo que volvió). El DLQ (`depende_de_nc`) espera cortésmente antes
        de postear a Siesa, sin bloquear al admin de encolarlo — mismo
        patrón que ya usaba `_procesar_recaudo` (botón masivo de Rutas)."""
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=1500000, nc=False)
        from app.services.liquidacion_service import LiquidacionService
        from app.models.siesa_job import SiesaJob
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['ok'] is True

        job = SiesaJob.query.filter_by(tipo='RECIBO_CAJA', referencia_id=recaudo.id).first()
        assert job is not None
        assert job.get_payload()['depende_de_nc'] is True

    def test_parcial_usa_lo_entregado_no_el_total_de_la_factura(self, app, db, recaudo_liq):
        """El mock de Siesa devuelve total_neto=2000000 (la factura
        COMPLETA). El conductor entregó 1500000 — el cliente devolvió el
        resto. El RC tiene que ser por lo que entró, no por la factura."""
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=1500000, nc=False)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['monto_neto_rc'] == 1500000

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

    def test_toma_la_un_real_de_la_fila_no_el_env_global(self, app, db, recaudo_liq):
        """PD1411/FE-1416 (2026-08-18): la fila de cartera real traía
        f353_id_un_cruce=99, pero el RC salía con SIESA_UNIDAD_NEGOCIO fijo
        (001) — Siesa rechazó ("UN diferente a la del auxiliar de caja" +
        "documento de cruce no existe", mismo motivo). El job encolado debe
        llevar la UN de la fila, no depender del fallback en connekta."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        mock_connekta = self._mock_siesa()
        mock_connekta.get_cxc_general.return_value = [
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': 999,
             'f253_id': '13050502', 'f353_id_un_cruce': '99',
             'f353_total_db': 2000000, 'f353_total_cr': 0},
        ]
        from app.services.liquidacion_service import LiquidacionService
        from app.models.siesa_job import SiesaJob
        with patch('app.services.connekta_gateway.connekta', mock_connekta), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            LiquidacionService.registrar_cobro_recaudo(recaudo.id, admin_id=1, retenciones=[])

        job = SiesaJob.query.filter_by(tipo='RECIBO_CAJA', referencia_id=recaudo.id).first()
        assert job.get_payload()['unidad_negocio'] == '99'

    def test_el_dc_de_retencion_tambien_lleva_la_un_real(self, app, db, recaudo_liq):
        """Job 470 (recaudo 19, PD1421, ruta 22, 2026-08-20): el fix de la UN
        real del 2026-08-18 (test de arriba) solo se aplicó al RC — el DC de
        retención se encola aparte, directo con `SiesaJob.encolar`, y su
        payload nunca llevó `unidad_negocio`. Primera liquidación con
        retención real: FALLIDO 5/5, rechazo estructural de Siesa."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5')
        # Admin ya confirmó que el descuento sí correspondía (ver
        # `confirmar_retencion`) — si no, `registrar_cobro_recaudo` bloquea
        # antes de llegar a encolar nada.
        recaudo.retencion_confirmada = True
        db.session.commit()
        mock_connekta = self._mock_siesa()
        mock_connekta.get_cxc_general.return_value = [
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': 999,
             'f253_id': '13050502', 'f353_id_un_cruce': '99',
             'f353_total_db': 2000000, 'f353_total_cr': 0},
        ]
        mock_connekta.get_rowids_factura.return_value = [
            {'f470_vlr_bruto': 1680672, 'f470_vlr_imp': 319328,
             'f470_vlr_neto': 2000000, 'f120_referencia': 'REF001',
             'f470_rowid': 'R1'},
        ]
        from app.services.liquidacion_service import LiquidacionService
        from app.models.siesa_job import SiesaJob
        with patch('app.services.connekta_gateway.connekta', mock_connekta), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[{'tipo': 'RETEFUENTE_2.5'}])

        dc_job = SiesaJob.query.filter_by(
            tipo='DOCUMENTO_CONTABLE_RET', referencia_id=recaudo.id).first()
        assert dc_job is not None
        assert dc_job.get_payload()['unidad_negocio'] == '99'

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

    def test_badge_de_retencion_no_marca_verde_si_siesa_lo_rechazo(
            self, app, db, recaudo_liq):
        """Job 482 (recaudo 22, PD1425, ruta 23, 2026-08-21): el ✓ en
        pantalla salía de `retenciones_detalle[i].siesa_triggered`, guardado
        en True al ENCOLAR el job — Siesa rechazó el documento y el badge
        seguía en verde. `to_dict()` debe recalcularlo contra
        `pucs_enviadas()` (la marca real, revertida si el POST falla), no
        confiar en la copia guardada al encolar."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        recaudo.retenciones_detalle = [{
            'tipo': 'ICA_3X1000', 'puc': '13551802', 'tasa': 0.003,
            'monto': 152.03, 'base': 50676.0,
            'siesa_triggered': True,  # mentira: se guardó así al encolar
            'job_id': 482,
        }]
        db.session.commit()

        det = recaudo.to_dict()['retenciones_detalle']
        assert det[0]['siesa_triggered'] is False, (
            'El PUC nunca se marcó en pucs_enviadas() — Siesa lo rechazó, '
            'el badge no debe salir en verde'
        )

        recaudo.marcar_puc_enviada('13551802')
        db.session.commit()
        det2 = recaudo.to_dict()['retenciones_detalle']
        assert det2[0]['siesa_triggered'] is True, (
            'Con el PUC sí confirmado en pucs_enviadas(), el badge debe salir en verde'
        )


# ═══════════════════════════════════════════════════════════════════
# Diferencia grande sin explicar — decisión obligatoria del admin (2026-08-24)
# ═══════════════════════════════════════════════════════════════════

class TestDiferenciaGrandeSinExplicarBloqueaElRC:
    """Caso real PD1426: ENTREGADO completo (sin devolución), el conductor
    declaró $50.000 y la factura en Siesa daba $58.600 — sin retención ni
    devolución que expliquen la diferencia. El RC salió por el neto
    completo de todas formas, sin que nadie lo confirmara.

    Mismo principio que `AjustePorDiferencia` en gestor-cartera-pame: una
    diferencia dentro del residuo de redondeo (`tope_diferencia_recaudo`,
    $100 por defecto) se resuelve sola; una diferencia mayor exige
    `monto_override` explícito — no se tapa dentro del RC."""

    def _mock_siesa_neto(self, neto: float):
        mock_connekta = MagicMock()
        mock_connekta.get_rowids_factura.return_value = [
            {'f470_vlr_bruto': neto, 'f470_vlr_imp': 0, 'f470_vlr_neto': neto,
             'f120_referencia': 'REF001', 'f470_rowid': 'R1'}
        ]
        mock_connekta.get_pedido_cabecera.return_value = {
            'f430_id_co': '003', 'f200_id_pedido_fact': '900123456',
            'f461_id_sucursal_pedido_rem': '001',
        }
        mock_connekta.get_cxc_general = MagicMock(return_value=[
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': 999,
             'f253_id': '13050502', 'f353_total_db': neto, 'f353_total_cr': 0},
        ])
        return mock_connekta

    def test_diferencia_grande_sin_override_bloquea_el_rc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=50000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa_neto(58600)), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            with pytest.raises(ValueError, match=r'\$50.000.*\$58.600|diferencia'):
                LiquidacionService.registrar_cobro_recaudo(
                    recaudo.id, admin_id=1, retenciones=[])

    def test_monto_override_explicito_evita_el_bloqueo(self, app, db, recaudo_liq):
        """El admin decidió a mano — el guard no pelea con una decisión
        ya tomada, solo con una que nadie tomó."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=50000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa_neto(58600)), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[], monto_override=50000)
        assert resultado['ok'] is True
        assert resultado['monto_neto_rc'] == 50000

    def test_diferencia_dentro_del_tope_no_bloquea(self, app, db, recaudo_liq):
        """$50 de diferencia es residuo de redondeo, no un faltante —
        se resuelve solo con el neto de Siesa, igual que siempre."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=58550)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa_neto(58600)), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['ok'] is True
        assert resultado['monto_neto_rc'] == 58600

    def test_sin_monto_declarado_no_bloquea(self, app, db, recaudo_liq):
        """`monto_cobrado` en 0 (nunca declarado) no es una diferencia que
        confirmar — no hay nada que comparar. Cae al neto de Siesa como
        siempre lo hizo, sin exigir override."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=0)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa_neto(58600)), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['ok'] is True
        assert resultado['monto_neto_rc'] == 58600

    def test_parcial_no_activa_el_guard(self, app, db, recaudo_liq):
        """PARCIAL ya usa `monto_cobrado` por diseño (la diferencia es la
        devolución, la cierra la NC) — el guard es solo para ENTREGADO,
        donde hoy no hay ningún documento que explique una diferencia."""
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=50000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa_neto(58600)), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['ok'] is True
        assert resultado['monto_neto_rc'] == 50000

    def test_el_camino_masivo_tambien_queda_bloqueado(self, app, db, recaudo_liq):
        """El botón masivo 'Enviar a Siesa' (`_procesar_recaudo`,
        `liquidar_ruta_siesa`) es un segundo camino independiente para crear
        el mismo RC — antes de este fix mandaba `monto_cobrado` directo, sin
        consultar Siesa. Mismo caso PD1426, por el camino masivo esta vez."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=50000)
        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=[{'f470_vlr_bruto': 49244, 'f470_vlr_imp': 9356,
                                  'f470_vlr_neto': 58600, 'f120_referencia': 'REF001',
                                  'f470_rowid': 'R1'}]), \
             patch('app.services.liquidacion_service._obtener_tercero',
                   return_value=('900123456', '001')):
            from app.services.liquidacion_service import _procesar_recaudo
            with pytest.raises(ValueError, match='diferencia'):
                _procesar_recaudo(recaudo, 'test liquidacion masiva')

    def test_el_camino_masivo_no_bloquea_si_siesa_no_responde(self, app, db, recaudo_liq):
        """Fallo de red consultando la factura no puede bloquear el cobro —
        cae al monto declarado, igual que `registrar_cobro_recaudo` cuando
        `datos_siesa_ok` es False."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=50000)
        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   side_effect=Exception('timeout simulado')), \
             patch('app.services.liquidacion_service._obtener_tercero',
                   return_value=('900123456', '001')):
            from app.services.liquidacion_service import _procesar_recaudo
            resultado = _procesar_recaudo(recaudo, 'test liquidacion masiva')
        assert resultado['rc'] == 1


# ═══════════════════════════════════════════════════════════════════
# Retención declarada en campo — decisión obligatoria del admin (2026-08-19)
# ═══════════════════════════════════════════════════════════════════

class TestRetencionDeclaradaBloqueaElRC:
    """`motivo_descuento` es lo que el CONDUCTOR anotó en la pantalla de pago
    parcial — el motivo tributario que el cliente dijo, sin verificar. Antes
    era solo una casilla premarcada en Liquidación; ahora bloquea el RC hasta
    que el admin confirme o rechace explícitamente si el cliente de verdad
    tenía derecho."""

    def _mock_siesa(self):
        """Mismo mock que TestRegistrarCobroRecaudo (total_neto=2000000,
        fila PD/999) — no se comparte por herencia para no re-ejecutar los
        tests de esa clase bajo este nombre."""
        mock_connekta = MagicMock()
        mock_connekta.get_rowids_factura.return_value = [
            {'f470_vlr_bruto': 1680672, 'f470_vlr_imp': 319328, 'f470_vlr_neto': 2000000,
             'f120_referencia': 'REF001', 'f470_rowid': 'R1'}
        ]
        mock_connekta.get_pedido_cabecera.return_value = {
            'f430_id_co': '003', 'f200_id_pedido_fact': '900123456',
            'f461_id_sucursal_pedido_rem': '001',
        }
        mock_connekta.get_cxc_general = MagicMock(return_value=[
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': 999,
             'f253_id': '13050502', 'f353_total_db': 2000000, 'f353_total_cr': 0},
        ])
        return mock_connekta

    def test_pendiente_bloquea_el_rc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5')
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            with pytest.raises(ValueError, match='confírmalo o recházalo'):
                LiquidacionService.registrar_cobro_recaudo(recaudo.id, admin_id=1, retenciones=[])

    def test_confirmada_permite_el_rc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5')
        recaudo.retencion_confirmada = True
        db.session.commit()
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[{'tipo': 'RETEFUENTE_2.5'}])
        assert resultado['ok'] is True

    def test_rechazada_bloquea_hasta_pagar_el_valor_completo(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEFUENTE_2.5')
        recaudo.retencion_confirmada = False
        db.session.commit()
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            with pytest.raises(ValueError, match='valor completo'):
                LiquidacionService.registrar_cobro_recaudo(
                    recaudo.id, admin_id=1, retenciones=[], monto_override=1000000)

    def test_rechazada_con_monto_completo_permite_el_rc(self, app, db, recaudo_liq):
        """El admin corrigió el monto a mano tras cobrar la diferencia — se
        destraba solo, sin un segundo estado de 'ya pagó el resto'."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5')
        recaudo.retencion_confirmada = False
        db.session.commit()
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[], monto_override=2000000)
        assert resultado['ok'] is True
        assert resultado['monto_neto_rc'] == 2000000

    def test_rechazada_no_se_puede_forzar_en_la_lista_de_retenciones(self, app, db, recaudo_liq):
        """Aunque el monto ya cubra el valor completo, no se puede colar la
        retención rechazada en la lista que arma el RC — sería crear el
        documento de retención (DC) para un motivo que el admin ya dijo que
        no era legítimo."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5')
        recaudo.retencion_confirmada = False
        db.session.commit()
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            with pytest.raises(ValueError, match='no se puede aplicar'):
                LiquidacionService.registrar_cobro_recaudo(
                    recaudo.id, admin_id=1,
                    retenciones=[{'tipo': 'RETEFUENTE_2.5'}], monto_override=2000000)

    def test_sin_motivo_declarado_no_bloquea_nada(self, app, db, recaudo_liq):
        """El guard es específico al caso de retención declarada — un
        parcial sin motivo sigue funcionando exactamente como antes."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[])
        assert resultado['ok'] is True


class TestConfirmarRetencion:

    def test_confirma_y_registra_quien_y_cuando(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5')
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.confirmar_retencion(recaudo.id, admin_id=7, confirmar=True)
        assert resultado['retencion_confirmada'] is True
        db.session.refresh(recaudo)
        assert recaudo.retencion_confirmada is True
        assert recaudo.retencion_confirmada_por == 7
        assert recaudo.retencion_confirmada_en is not None

    def test_rechaza(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5')
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.confirmar_retencion(recaudo.id, admin_id=7, confirmar=False)
        assert resultado['retencion_confirmada'] is False

    def test_sin_motivo_declarado_no_hay_nada_que_confirmar(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000)
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(ValueError, match='no tiene motivo'):
            LiquidacionService.confirmar_retencion(recaudo.id, admin_id=7, confirmar=True)

    def test_no_se_puede_decidir_despues_de_disparado_el_rc(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=2000000,
                              motivo_desc='RETEFUENTE_2.5', rc=True)
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(ValueError, match='ya no aplica'):
            LiquidacionService.confirmar_retencion(recaudo.id, admin_id=7, confirmar=True)

    def test_recaudo_inexistente(self, app, db):
        from app.services.liquidacion_service import LiquidacionService
        with pytest.raises(LookupError):
            LiquidacionService.confirmar_retencion(999999, admin_id=7, confirmar=True)


# ═══════════════════════════════════════════════════════════════════
# El RC de un PARCIAL va por lo entregado (2026-08-19)
# ═══════════════════════════════════════════════════════════════════

class TestElPreviewExponeLoQueTrajoElConductor:
    """El backend ya decidía bien y la pantalla no se enteraba.

    `_liqRenderPanelCobro` lee `preview.monto_cobrado` para comparar contra el
    neto de Siesa y, en PARCIAL, para proponer el monto del RC. La clave nunca
    estuvo en el payload: el front recibía `undefined`, mostraba «Conductor:
    $0» y mandaba `monto_override` = factura completa — que en el servicio
    tiene prioridad sobre todo lo demás. O sea que la regla de «PARCIAL cobra
    lo entregado» quedaba muerta en el único camino que la usa.
    """

    def _mock_siesa(self):
        mock_connekta = MagicMock()
        mock_connekta.get_rowids_factura.return_value = [
            {'f470_vlr_bruto': 1680672, 'f470_vlr_imp': 319328, 'f470_vlr_neto': 2000000,
             'f120_referencia': 'REF001', 'f470_rowid': 'R1'}
        ]
        mock_connekta.get_pedido_cabecera.return_value = {
            'f430_id_co': '003', 'f200_id_pedido_fact': '900123456',
            'f461_id_sucursal_pedido_rem': '001',
        }
        mock_connekta.get_cxc_general = MagicMock(return_value=[])
        return mock_connekta

    def test_el_preview_trae_el_monto_del_conductor(self, app, db, recaudo_liq):
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=1500000)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            preview = LiquidacionService.preview_acciones_recaudo(recaudo.id)
        assert preview['monto_cobrado'] == 1500000, (
            '\nSin esta clave la pantalla cree que el conductor trajo $0 y '
            'propone cobrar la factura completa.')
        assert preview['estado_entrega'] == 'PARCIAL', (
            '\nEl front decide el monto por defecto con este campo.')


class TestLaPantallaNoTrabaElCobroDeUnParcial:
    """El guard del navegador, no del servidor.

    El bloqueo duro del backend se quitó el 2026-08-19 (el RC no espera a la
    NC: son documentos distintos, y el DLQ ya espera solo por `depende_de_nc`).
    Pero `liquidacion.js` conservó su propio `disabled` — el botón siguió
    diciendo «Registrar Cobro (espera NC)» y el arreglo no llegó a nadie. Es
    el mismo hueco de `ENTREGADO_SIN_PAGO`: el servidor decide bien y la
    pantalla no se entera.
    """

    import pathlib as _pl
    _JS = _pl.Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa' / 'liquidacion.js'

    def test_el_boton_no_se_deshabilita_esperando_la_nc(self):
        fuente = self._JS.read_text(encoding='utf-8')
        assert 'espera NC' not in fuente, (
            '\nVolvió el bloqueo del botón de RC para PARCIAL sin NC. El RC es '
            'por lo que el conductor SÍ entregó — se encola de una vez y el '
            'DLQ espera a la NC antes de postearlo a Siesa (Regla 7 intacta).')

    def test_el_monto_por_defecto_en_parcial_es_lo_entregado(self):
        fuente = self._JS.read_text(encoding='utf-8')
        assert "preview.estado_entrega === 'PARCIAL'" in fuente, (
            '\nLa pantalla dejó de distinguir el PARCIAL — vuelve a proponer '
            'el neto de Siesa como monto del RC.')
        i = fuente.find('const mDefault')
        assert i != -1, '¿se renombró el monto por defecto del panel de cobro?'
        linea = fuente[i:fuente.find('\n', i)]
        assert 'esParcial' in linea and 'mCobrado' in linea, (
            f'\nEl monto por defecto ya no depende del estado: {linea!r}\n'
            'En PARCIAL tiene que arrancar en lo que trajo el conductor: ese '
            'valor viaja como monto_override y pisa la lógica del servicio.')


class TestLaDecisionDelDescuentoViveEnLaTarjeta:
    """Dónde vive la puerta, no solo que exista.

    La decisión sobre el descuento declarado en campo estaba DENTRO del panel
    que se abre al pulsar «Registrar Cobro» — o sea, después del botón que
    pretendía custodiar. Para el admin eso es lo mismo que no existir: la
    lista no muestra nada que confirmar y el botón se ve normal. La puerta
    tiene que estar en la tarjeta de la parada, antes de que el botón sea
    pulsable.
    """

    import pathlib as _pl
    _JS = _pl.Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa' / 'liquidacion.js'

    def _fuente(self):
        return self._JS.read_text(encoding='utf-8')

    def test_la_tarjeta_pinta_la_decision_antes_del_boton(self):
        fuente = self._fuente()
        i = fuente.find('_liqBloqueRetencion(ruta.id')
        assert i != -1, (
            '\nLa lista dejó de pintar el bloque de decisión del descuento. '
            'Sin él, el admin no tiene dónde confirmar ni rechazar.')
        j = fuente.find('liqToggleCobro(${ruta.id}', i)
        assert j != -1 and j > i, (
            '\nEl bloque de decisión quedó después del botón de cobro (o el '
            'botón se renombró). La puerta va ANTES.')

    def test_el_boton_de_cobro_se_traba_con_el_descuento_sin_verificar(self):
        fuente = self._fuente()
        assert '_liqRetencionTraba(rec)' in fuente, (
            '\nLa lista dejó de consultar si el descuento traba el RC — el '
            'botón vuelve a ofrecerse sobre un descuento que nadie verificó.')
        i = fuente.find('function _liqRetencionTraba')
        cuerpo = fuente[i:i + 1200]
        assert 'motivo_descuento' in cuerpo and 'retencion_confirmada' in cuerpo, (
            '\nLa regla dejó de mirar el descuento declarado y su decision.')

    def test_decidir_recarga_la_lista_no_solo_el_panel(self):
        """El botón que la decisión desbloquea vive en la tarjeta. Refrescar
        solo el panel dejaba el candado puesto hasta recargar a mano."""
        fuente = self._fuente()
        i = fuente.find('async function liqConfirmarRetencion')
        assert i != -1, '¿se renombró la acción de decidir?'
        cuerpo = fuente[i:i + 1400]
        assert 'liquidacion-detalle' in cuerpo and '_liqRenderDetalle()' in cuerpo, (
            '\nDecidir volvió a refrescar solo el panel de cobro.')


class TestDescuentoRechazadoEnUnParcial:
    """El choque entre los dos cambios del 2026-08-19.

    Al rechazar el descuento, el guard exigía que lo cobrado alcanzara el
    `total_neto` de la factura. En un ENTREGADO eso está bien. En un PARCIAL
    el cliente devolvió mercancía y **nunca** va a pagar ese valor: el pedido
    quedaba trabado para siempre, sin salida en la pantalla. La referencia
    correcta es lo que debía pagar por lo que se quedó — `monto_cobrado +
    monto_descuento`, el "valor a cobrar" que el conductor tenía en la puerta
    antes de restarle el descuento. En un ENTREGADO esa suma da el neto, así
    que la regla es una sola para ambos estados.
    """

    def _mock_siesa(self):
        """total_neto = 2.000.000 — la factura COMPLETA, sin descontar lo
        devuelto."""
        mock_connekta = MagicMock()
        mock_connekta.get_rowids_factura.return_value = [
            {'f470_vlr_bruto': 1680672, 'f470_vlr_imp': 319328, 'f470_vlr_neto': 2000000,
             'f120_referencia': 'REF001', 'f470_rowid': 'R1'}
        ]
        mock_connekta.get_pedido_cabecera.return_value = {
            'f430_id_co': '003', 'f200_id_pedido_fact': '900123456',
            'f461_id_sucursal_pedido_rem': '001',
        }
        mock_connekta.get_cxc_general = MagicMock(return_value=[
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': 999,
             'f253_id': '13050502', 'f353_total_db': 2000000, 'f353_total_cr': 0},
        ])
        return mock_connekta

    def _recaudo_parcial_rechazado(self, db, recaudo_liq):
        """Devolvió 500.000 en mercancía (factura 2.000.000, a cobrar
        1.500.000), pagó 1.400.000 alegando una retención de 100.000 que el
        admin declaró improcedente."""
        recaudo = recaudo_liq(estado='PARCIAL', pago='EFECTIVO', monto=1400000,
                              motivo_desc='RETEFUENTE_2.5', monto_desc=100000)
        recaudo.retencion_confirmada = False
        db.session.commit()
        return recaudo

    def test_no_exige_la_factura_completa_que_incluye_lo_devuelto(self, app, db, recaudo_liq):
        recaudo = self._recaudo_parcial_rechazado(db, recaudo_liq)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()), \
             patch('app.services.siesa_job_service.disparar_dlq_inmediato', MagicMock()):
            resultado = LiquidacionService.registrar_cobro_recaudo(
                recaudo.id, admin_id=1, retenciones=[], monto_override=1500000)
        assert resultado['ok'] is True
        assert resultado['monto_neto_rc'] == 1500000

    def test_sigue_bloqueado_si_no_pago_lo_que_le_correspondia(self, app, db, recaudo_liq):
        """Bajar el listón no es quitarlo: mientras falte plata de la parte
        que sí se quedó, el RC no sale."""
        recaudo = self._recaudo_parcial_rechazado(db, recaudo_liq)
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            with pytest.raises(ValueError, match='debe pagar el valor completo'):
                LiquidacionService.registrar_cobro_recaudo(
                    recaudo.id, admin_id=1, retenciones=[], monto_override=1450000)

    def test_en_entregado_la_suma_sigue_dando_el_neto_de_la_factura(self, app, db, recaudo_liq):
        """Sin devolución de por medio la regla nueva no afloja nada: 1.900.000
        entregados + 100.000 descontados = los 2.000.000 de la factura."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1900000,
                              motivo_desc='RETEFUENTE_2.5', monto_desc=100000)
        recaudo.retencion_confirmada = False
        db.session.commit()
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            with pytest.raises(ValueError, match='debe pagar el valor completo'):
                LiquidacionService.registrar_cobro_recaudo(
                    recaudo.id, admin_id=1, retenciones=[], monto_override=1900000)

    def test_sin_monto_declarado_cae_al_neto_de_siesa(self, app, db, recaudo_liq):
        """La liquidación masiva puede dejar `motivo_descuento` sin cuánto se
        descontó. Ahí el neto de la factura sigue siendo la única referencia
        disponible — no se puede dar por bueno cualquier monto."""
        recaudo = recaudo_liq(estado='ENTREGADO', pago='EFECTIVO', monto=1000000,
                              motivo_desc='RETEFUENTE_2.5')
        recaudo.retencion_confirmada = False
        db.session.commit()
        from app.services.liquidacion_service import LiquidacionService
        with patch('app.services.connekta_gateway.connekta', self._mock_siesa()):
            with pytest.raises(ValueError, match='debe pagar el valor completo'):
                LiquidacionService.registrar_cobro_recaudo(
                    recaudo.id, admin_id=1, retenciones=[], monto_override=1000000)


class TestElRechazoNoEncierraAlAdmin:
    """Un candado que tapa su propia llave.

    La tarjeta bloqueaba el botón de cobro cuando el descuento estaba
    rechazado — pero el monto se corrige en un campo que vive DENTRO del panel
    que ese botón abre. El admin quedaba sin manera de llegar al único control
    que destraba el bloqueo. El gate de verdad es el del servicio; la pantalla
    solo traba lo que no tiene nada que resolver adentro (el pendiente).
    """

    import pathlib as _pl
    _JS = _pl.Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa' / 'liquidacion.js'

    def test_solo_el_pendiente_traba_el_boton(self):
        fuente = self._JS.read_text(encoding='utf-8')
        i = fuente.find('function _liqRetencionTraba')
        assert i != -1, '¿se renombró la regla de trabado?'
        cuerpo = fuente[i:fuente.find('\n}', i)]
        assert 'retencion_confirmada === false' not in cuerpo, (
            '\nLa pantalla volvió a trabar el botón con el descuento '
            'RECHAZADO. El monto que destraba se edita dentro del panel que '
            'ese botón abre — trabarlo encierra al admin.')
        assert 'retencion_confirmada === null' in cuerpo, (
            '\nEl pendiente dejó de trabar el botón: se puede registrar un '
            'cobro sobre un descuento que nadie verificó.')

    def test_la_pantalla_usa_la_misma_referencia_que_el_servicio(self):
        fuente = self._JS.read_text(encoding='utf-8')
        i = fuente.find('function _liqEsperadoRetencion')
        assert i != -1, '¿se renombró el cálculo de lo que el cliente debía?'
        cuerpo = fuente[i:fuente.find('\n}', i)]
        assert 'monto_descuento' in cuerpo and 'monto_cobrado' in cuerpo, (
            '\nLa pantalla volvió a calcular lo que falta contra el neto de '
            'Siesa. Si las dos reglas se separan, dice un número y el '
            'servicio exige otro.')
