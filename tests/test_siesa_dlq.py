"""
Tier 3 — Tests DLQ: pre-flag, secuencialidad NC→RC→DC, idempotencia.
Garantiza que los patrones anti-duplicado y de ordenamiento funcionan.
"""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime


@pytest.fixture
def recaudo_factory(db, almacen):
    """Factory para crear un RecaudoEntrega con sus dependencias."""
    def _make(estado_entrega='ENTREGADO', forma_pago='EFECTIVO',
              monto=1500000, rc=False, nc=False, dc=False, codigo='PK-DLQ'):
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.packing import TareaPacking
        from app.models.ruta_despacho import RutaDespacho
        from app.models.usuario import Usuario
        # Conductor (si no existe)
        conductor = Usuario.query.filter_by(email='cond_dlq@test.com').first()
        if not conductor:
            conductor = Usuario(email='cond_dlq@test.com', nombre='Conductor DLQ',
                                rol='conductor', activo=True)
            conductor.set_password('test123')
            db.session.add(conductor)
            db.session.flush()
        # Ruta
        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado='ENTREGADA')
        db.session.add(ruta)
        db.session.flush()
        # Tarea packing
        tarea = TareaPacking(
            codigo=codigo, estado='DESPACHADO', almacen_id=almacen.id,
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=100,
            numero_pedido_siesa='PED-DLQ',
        )
        db.session.add(tarea)
        db.session.flush()
        recaudo = RecaudoEntrega(
            ruta_id=ruta.id, tarea_id=tarea.id,
            estado_entrega=estado_entrega, forma_pago=forma_pago,
            monto_cobrado=monto,
            siesa_rc_triggered=rc, siesa_nc_triggered=nc, siesa_dc_triggered=dc,
        )
        db.session.add(recaudo)
        db.session.commit()
        return recaudo
    return _make


# ═══════════════════════════════════════════════════════════════════
# Pre-flag pattern — flag ANTES del POST, revert si falla
# ═══════════════════════════════════════════════════════════════════

class TestPreFlagRC:
    """Patrón pre-flag para RECIBO_CAJA (previene RC duplicado en crash)."""

    @staticmethod
    def _make_rc_job(db, recaudo):
        from app.models.siesa_job import SiesaJob
        return SiesaJob.encolar('RECIBO_CAJA', {
            'recaudo_id': recaudo.id,
            'tercero_nit': '900123456', 'sucursal': '001',
            'monto': 1500000, 'forma_pago': 'EFECTIVO',
            'tipo_docto_fe': 'FE', 'consec_fe': '5020',
        })

    def test_preflag_se_setea_antes_del_post(self, app, db, recaudo_factory):
        recaudo = recaudo_factory()
        job = self._make_rc_job(db, recaudo)
        db.session.commit()

        call_order = []
        def mock_trigger(*a, **kw):
            db.session.refresh(recaudo)
            call_order.append(recaudo.siesa_rc_triggered)
            return {'codigo': 0}

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja = mock_trigger
            mc.modo_simulacion = False
            mc.modo_ensayo = False
            from app.services.siesa_job_service import _ejecutar_job
            _ejecutar_job(job)

        assert call_order == [True], 'Flag debe ser True ANTES del POST'

    def test_preflag_revert_si_post_falla(self, app, db, recaudo_factory):
        recaudo = recaudo_factory()
        job = self._make_rc_job(db, recaudo)
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja.side_effect = Exception('Connekta timeout')
            mc.modo_simulacion = False
            mc.modo_ensayo = False
            from app.services.siesa_job_service import _ejecutar_job
            with pytest.raises(Exception, match='Connekta timeout'):
                _ejecutar_job(job)

        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_triggered is False, 'Flag debe revertir tras fallo'

    def test_idempotente_si_ya_triggered(self, app, db, recaudo_factory):
        recaudo = recaudo_factory(rc=True)
        job = self._make_rc_job(db, recaudo)
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            from app.services.siesa_job_service import _ejecutar_job
            resultado = _ejecutar_job(job)

        assert resultado.get('idempotente') is True
        mc.trigger_recibo_caja.assert_not_called()

    def test_preflight_factura_ya_saldada_no_llama_al_post(self, app, db, recaudo_factory):
        """API_v2_CxC_General ya muestra la factura sin saldo (otra vía la
        cruzó) — no se debe mandar un RC duplicado. Campos reales
        verificados en vivo 2026-08-11: f353_id_tipo_docto_cruce/
        f353_consec_docto_cruce/f353_total_db/f353_total_cr."""
        recaudo = recaudo_factory()
        job = self._make_rc_job(db, recaudo)
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            mc.modo_ensayo = False
            mc.get_cxc_general.return_value = [
                {'f353_id_tipo_docto_cruce': 'FE', 'f353_consec_docto_cruce': '5020',
                 'f353_total_db': 1500000, 'f353_total_cr': 1500000},
            ]
            from app.services.siesa_job_service import _ejecutar_job
            resultado = _ejecutar_job(job)

        assert resultado.get('ya_existente') is True
        mc.trigger_recibo_caja.assert_not_called()
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_triggered is True

    def test_timeout_pero_factura_ya_saldada_no_revierte_flag(self, app, db, recaudo_factory):
        """Regla #3: un timeout no significa que el POST falló. Si tras la
        excepción la factura ya no tiene saldo, el RC sí entró — no
        revertir el pre-flag (evita el duplicado del incidente RC-00002744)."""
        recaudo = recaudo_factory()
        job = self._make_rc_job(db, recaudo)
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            mc.modo_ensayo = False
            mc.trigger_recibo_caja.side_effect = Exception('Connekta timeout')
            # Primera llamada (pre-flight, antes del POST): aún con saldo, no
            # bloquea el intento. Segunda llamada (dentro del except, tras el
            # timeout): ya saldada — confirma que el POST sí entró a Siesa.
            mc.get_cxc_general.side_effect = [
                [{'f353_id_tipo_docto_cruce': 'FE', 'f353_consec_docto_cruce': '5020',
                  'f353_total_db': 1500000, 'f353_total_cr': 0}],
                [{'f353_id_tipo_docto_cruce': 'FE', 'f353_consec_docto_cruce': '5020',
                  'f353_total_db': 1500000, 'f353_total_cr': 1500000}],
            ]
            from app.services.siesa_job_service import _ejecutar_job
            resultado = _ejecutar_job(job)

        assert resultado.get('timeout_pero_exitoso') is True
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_triggered is True, 'No debe revertir — el RC sí entró'


# ═══════════════════════════════════════════════════════════════════
# Secuencialidad — NC → RC → DC
# ═══════════════════════════════════════════════════════════════════

class TestSecuencialidad:

    def test_rc_espera_nc_si_depende(self, app, db, recaudo_factory):
        recaudo = recaudo_factory(estado_entrega='PARCIAL', codigo='PK-SEQ1')
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('RECIBO_CAJA', {
            'recaudo_id': recaudo.id,
            'tercero_nit': '900123456', 'sucursal': '001',
            'monto': 800000, 'forma_pago': 'EFECTIVO',
            'tipo_docto_fe': 'FE', 'consec_fe': '5020',
            'depende_de_nc': True,
        })
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            from app.services.siesa_job_service import _ejecutar_job
            with pytest.raises(Exception, match='RC depende de NC pendiente'):
                _ejecutar_job(job)
        mc.trigger_recibo_caja.assert_not_called()

    def test_dc_espera_rc(self, app, db, recaudo_factory):
        recaudo = recaudo_factory(estado_entrega='ENTREGADO', codigo='PK-SEQ2')
        assert recaudo.siesa_rc_triggered is False

        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('DOCUMENTO_CONTABLE_RET', {
            'recaudo_id': recaudo.id,
            'tercero_nit': '900123456', 'sucursal': '001',
            'cuenta_puc': '13551501', 'monto': 25000,
            'base_gravable': 1000000,
            'tipo_docto_fe': 'FE', 'consec_fe': '5020',
        })
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            from app.services.siesa_job_service import _ejecutar_job
            with pytest.raises(Exception, match='DC depende de RC'):
                _ejecutar_job(job)
        mc.trigger_documento_contable.assert_not_called()

    def test_dc_ejecuta_si_rc_ya_paso(self, app, db, recaudo_factory):
        recaudo = recaudo_factory(rc=True, codigo='PK-SEQ3')
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('DOCUMENTO_CONTABLE_RET', {
            'recaudo_id': recaudo.id,
            'tercero_nit': '900123456', 'sucursal': '001',
            'cuenta_puc': '13551501', 'monto': 25000,
            'base_gravable': 1000000,
            'tipo_docto_fe': 'FE', 'consec_fe': '5020',
        })
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_documento_contable.return_value = {'codigo': 0}
            mc.modo_simulacion = False
            mc.modo_ensayo = False
            from app.services.siesa_job_service import _ejecutar_job
            _ejecutar_job(job)
        mc.trigger_documento_contable.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
# NOTA_CREDITO_DEVOLUCION_CLIENTE — devolución de cliente (Recepción),
# reutiliza el mismo conector 142946 que NOTA_CREDITO_FACTURA pero con su
# propio flag de idempotencia (DevolucionCliente.siesa_nc_triggered).
# ═══════════════════════════════════════════════════════════════════

class TestNotaCreditoDevolucionCliente:

    @staticmethod
    def _make_devolucion(db, almacen, siesa_nc_triggered=False):
        from app.models.packing import TareaPacking
        from app.models.devolucion_cliente import DevolucionCliente
        tarea = TareaPacking(
            codigo='PK-DEVC-DLQ', tipo_documento='PEDIDO', estado='DESPACHADO',
            almacen_id=almacen.id, numero_pedido_siesa='PD-DLQ',
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='500',
            siesa_triggered=True,
        )
        db.session.add(tarea)
        db.session.flush()
        devolucion = DevolucionCliente(
            codigo='DEVC-DLQ-001', tarea_packing_id=tarea.id,
            numero_pedido_siesa='PD-DLQ', tipo_docto_fe='FEW', consec_fe='5555',
            almacen_id=almacen.id, estado='CONFIRMADA',
            siesa_nc_triggered=siesa_nc_triggered,
        )
        db.session.add(devolucion)
        db.session.commit()
        return devolucion

    def test_idempotente_si_ya_triggered(self, app, db, almacen):
        devolucion = self._make_devolucion(db, almacen, siesa_nc_triggered=True)
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('NOTA_CREDITO_DEVOLUCION_CLIENTE', {
            'devolucion_id': devolucion.id,
            'tipo_docto_fe': 'FEW', 'consec_fe': '5555',
            'items_devueltos': [{'codigo': 'PROD-001', 'cantidad_devuelta': 4}],
        })
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            from app.services.siesa_job_service import _ejecutar_job
            resultado = _ejecutar_job(job)

        assert resultado.get('idempotente') is True
        mc.get_rowids_factura.assert_not_called()
        mc.trigger_nota_factura.assert_not_called()

    def test_dispara_142946_y_marca_triggered(self, app, db, almacen, producto):
        devolucion = self._make_devolucion(db, almacen, siesa_nc_triggered=False)
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('NOTA_CREDITO_DEVOLUCION_CLIENTE', {
            'devolucion_id': devolucion.id,
            'tipo_docto_fe': 'FEW', 'consec_fe': '5555',
            'items_devueltos': [{'codigo': producto.codigo_siesa, 'cantidad_devuelta': 4}],
            'es_total': False,
        })
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            mc.causal_devolucion_default = '01'
            mc.motivo_ventas = '01'
            mc.uom_default = 'UND'
            mc.bodega = 'NB1'
            mc.bodega_averias = 'AV1'
            mc.get_rowids_factura.return_value = [{
                'f470_rowid': '999', 'f120_referencia': producto.codigo_siesa,
                'f470_cant_base': 10, 'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
                'f470_vlr_neto': 1000,
            }]
            mc.trigger_nota_factura_crear_cruzar.return_value = {'codigo': 0}
            from app.services.siesa_job_service import _ejecutar_job
            _ejecutar_job(job)

        mc.trigger_nota_factura_crear_cruzar.assert_called_once()
        _, kwargs = mc.trigger_nota_factura_crear_cruzar.call_args
        assert kwargs['tipo_docto_fe'] == 'FEW'
        assert kwargs['consec_fe'] == '5555'
        assert kwargs['lineas'][0]['f470_cant_base'] == 4
        # Prorrateo: factura 10 unidades por $1000 neto, se devuelven 4 → $400
        assert kwargs['valor_cruce'] == 400.0

        db.session.refresh(devolucion)
        assert devolucion.siesa_nc_triggered is True
        assert devolucion.siesa_nc_triggered_at is not None
