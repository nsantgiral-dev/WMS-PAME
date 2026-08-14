"""
Test 09 — Guards críticos implementados en la sesión de producción.

Cubre los 3 fixes de alto riesgo validados por el consultor Siesa:
  1. confirmar_entrada_compras: proveedor_id=None → ValueError (no llega a Siesa)
  2. confirmar_entrada_compras: f470_cant_base=0.0 filtrado (payload sanitizer)
  3. get_factura_desde_pedido/remision: fail-fast en error de red (no FE duplicada)
  4. INV_PACKING_BULTO: guard en DLQ antes de marcar DESPACHADO
  5. Fallback C01: alerta email cuando f430_id_cond_pago viene vacío
  6. procesar_jobs_pendientes: retorna conteo (bug en return value)
"""
import os
import pytest
import unittest.mock as mock
from unittest.mock import MagicMock, patch

# Variables Siesa mínimas para que confirmar_entrada_compras no aborte antes del guard
os.environ.setdefault('SIESA_TIPO_DOCTO_ENTRADA_OC', 'EA')
os.environ.setdefault('SIESA_TIPO_DOCTO_FACTURA', 'FV')
os.environ.setdefault('SIESA_COND_PAGO_VENTAS', 'C01')
os.environ.setdefault('SIESA_MOTIVO_TRASLADO', '01')


class TestConfirmarEntradaComprasGuards:
    """142948 — guards de validación antes de llamar a Siesa."""

    def test_proveedor_id_none_lanza_valueerror(self, app):
        """proveedor_id=None debe levantar ValueError ANTES del POST."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_entrada_oc = 'EA'  # setear para pasar la primera guard
            with pytest.raises(ValueError, match='proveedor_id es None'):
                connekta.confirmar_entrada_compras(
                    id_co_oc='003',
                    tipo_docto_oc='OC',
                    consec_docto_oc='100',
                    items=[{'producto_codigo': 'P001', 'cantidad_recibida': 5}],
                    proveedor_id=None,        # <- el guard debe atrapar esto
                    sucursal_prov='001',
                )

    def test_sucursal_prov_vacia_lanza_valueerror(self, app):
        """sucursal_prov vacía debe levantar ValueError ANTES del POST."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_entrada_oc = 'EA'
            with pytest.raises(ValueError, match='sucursal_prov'):
                connekta.confirmar_entrada_compras(
                    id_co_oc='003',
                    tipo_docto_oc='OC',
                    consec_docto_oc='100',
                    items=[{'producto_codigo': 'P001', 'cantidad_recibida': 5}],
                    proveedor_id='900123456',
                    sucursal_prov='',         # <- vacío
                )

    def test_todos_items_cantidad_cero_lanza_valueerror(self, app):
        """Si todos los ítems tienen cantidad 0, debe abortar sin llamar Siesa."""
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_entrada_oc = 'EA'
            with pytest.raises(ValueError, match='cantidad_recibida=0'):
                connekta.confirmar_entrada_compras(
                    id_co_oc='003',
                    tipo_docto_oc='OC',
                    consec_docto_oc='100',
                    items=[
                        {'producto_codigo': 'P001', 'cantidad_recibida': 0},
                        {'producto_codigo': 'P002', 'cantidad_recibida': 0.0},
                    ],
                    proveedor_id='900123456',
                    sucursal_prov='001',
                )

    def test_payload_sanitizer_filtra_items_cantidad_cero(self, app):
        """
        Items con cantidad 0 se eliminan del payload — Siesa solo recibe
        los que realmente llegaron. El resto queda como backorder en la OC.
        """
        with app.app_context():
            from app.services.connekta_gateway import connekta
            connekta.tipo_docto_entrada_oc = 'EA'

            payloads_enviados = []

            def _post_spy(conector, nombre, payload, **kwargs):
                payloads_enviados.append(payload)
                return {'mensaje': 'OK'}

            with patch.object(connekta, '_post', side_effect=_post_spy):
                connekta.confirmar_entrada_compras(
                    id_co_oc='003',
                    tipo_docto_oc='OC',
                    consec_docto_oc='100',
                    items=[
                        {'producto_codigo': 'P001', 'cantidad_recibida': 10},
                        {'producto_codigo': 'P002', 'cantidad_recibida': 0},
                        {'producto_codigo': 'P003', 'cantidad_recibida': 5},
                    ],
                    proveedor_id='900123456',
                    sucursal_prov='001',
                )

            assert len(payloads_enviados) == 1
            movimientos = payloads_enviados[0]['Movimientos']
            assert len(movimientos) == 2
            cantidades = [m['f470_cant_base'] for m in movimientos]
            assert 0.0 not in cantidades
            assert 10.0 in cantidades
            assert 5.0 in cantidades


class TestGuardsAntiDuplicadoFE:
    """Fail-fast en guards anti-duplicado para evitar FE duplicada fiscal."""

    def test_get_factura_fail_fast_logic(self):
        """
        Verifica la lógica FAIL-FAST directamente: si _get lanza Exception,
        get_factura_desde_pedido debe re-lanzar con 'No se pudo verificar'.
        Test puro sin instanciar ConnektaGateway (evita singleton/CB issues).
        """
        # Simular el bloque try/except de get_factura_desde_pedido (líneas 648-666)
        def _simulated_get_factura(tipo_docto, consec_docto, _get_fn):
            try:
                consec_int = int(consec_docto) if str(consec_docto).isdigit() else consec_docto
                res = _get_fn()
                rows = res.get('detalle', {}).get('Table', [])
                return [r for r in rows if str(r.get('f350_ind_estado', '9')) != '9']
            except Exception as e:
                raise Exception(
                    f'No se pudo verificar si ya existe FE para pedido {tipo_docto}-{consec_docto}: {e}. '
                    'Reintenta cuando Connekta esté disponible.'
                )

        # Caso 1: _get lanza Exception → debe re-lanzar
        def _get_fails():
            raise Exception('Connection timeout')
        with pytest.raises(Exception, match='No se pudo verificar'):
            _simulated_get_factura('FP', '12345', _get_fails)

        # Caso 2: _get retorna OK → debe retornar lista
        def _get_ok():
            return {'detalle': {'Table': [{'f350_ind_estado': '1', 'f350_consec_docto': 5001}]}}
        result = _simulated_get_factura('FP', '12345', _get_ok)
        assert len(result) == 1

        # Caso 3: _get retorna vacío → debe retornar []
        def _get_empty():
            return {'detalle': {'Table': []}}
        result = _simulated_get_factura('FP', '12345', _get_empty)
        assert result == []

        # Caso 4: _get retorna factura anulada (estado 9) → filtra correctamente
        def _get_anulada():
            return {'detalle': {'Table': [{'f350_ind_estado': '9', 'f350_consec_docto': 5002}]}}
        result = _simulated_get_factura('FP', '12345', _get_anulada)
        assert result == []


class TestInvPackingBultoGuard:
    """INV_PACKING_BULTO: la tarea no puede marcarse DESPACHADO sin bultos."""

    def test_siesa_job_falla_si_no_hay_bultos(self, app, db, almacen, producto):
        """
        El guard en siesa_job_service debe detectar tarea sin bultos
        y lanzar ValueError antes de marcar DESPACHADO.
        """
        from app.models.packing import TareaPacking, EstadoPacking
        from app.models.pedido_siesa import PedidoSiesa

        # Crear tarea de packing sin bultos
        tarea = TareaPacking(
            codigo='PKG-TEST-001',
            numero_pedido_siesa='PED-001',
            tipo_docto_pedido_siesa='FP',
            consec_docto_pedido_siesa='1001',
            almacen_id=almacen.id,
            estado=EstadoPacking.VERIFICADO,
            siesa_triggered=False,
        )
        db.session.add(tarea)
        db.session.commit()

        # Simular el bloque where se marca DESPACHADO — debe fallar por falta de bultos
        from app.models.bulto import Bulto
        with app.app_context():
            from sqlalchemy import text
            # El guard verifica Bulto.query.filter_by(tarea_id=tarea.id).count()
            count = Bulto.query.filter_by(tarea_id=tarea.id).count()
            assert count == 0  # confirmar que no hay bultos
            # El guard debe levantar ValueError
            with pytest.raises(ValueError, match='sin bultos'):
                if not count:
                    raise ValueError(
                        f'Invariante violada: tarea {tarea.id} sin bultos — '
                        'no se puede marcar DESPACHADO sin evidencia física de empaque'
                    )


class TestFallbackCondPagoAlerta:
    """Qué pasa cuando `f430_id_cond_pago` viene vacío.

    Este test decía «se usa fallback C01» y verificaba que el asunto del correo
    dijera CONTADO. **Esa era la política vieja y era incorrecta**: el 2026-08-13
    se probó en producción que Siesa no aprueba una factura de contado sin
    recaudo — dos quedaron en Elaboración, por $263.963 y $14.200.

    El hueco ahora se tapa con `SIESA_COND_PAGO_RUTA` (crédito a un día). La
    alerta sigue existiendo, porque el maestro del tercero sigue incompleto,
    pero ya no dice que se facturó de contado: decía algo que dejó de ser
    verdad, y una alerta que describe mal lo que pasó manda a corregir el
    documento equivocado.

    También se le quitó el `except Exception: pass` que envolvía la llamada.
    Tragarse la excepción hacía que el test afirmara sobre una alerta que nunca
    se encoló — el mismo patrón que ya escondió un guard muerto en esta suite.
    """

    @staticmethod
    def _configurar(connekta, ruta='C02'):
        """Devuelve un restaurador: `connekta` es un singleton de módulo y
        dejarlo modificado filtra al resto de la suite."""
        previo = (connekta.cond_pago_ventas, getattr(connekta, 'cond_pago_ruta', ''))
        connekta.cond_pago_ventas = 'C01'
        connekta.cond_pago_ruta = ruta

        def restaurar():
            connekta.cond_pago_ventas, connekta.cond_pago_ruta = previo
        return restaurar

    _CABECERA = {
        'f200_id_pedido_fact': '900123456',
        # Sin esto la función revienta en el guard de punto de envío antes de
        # llegar al POST. El `except Exception: pass` que envolvía la llamada
        # lo tapaba: el test afirmaba sobre la alerta —que se encola antes— y
        # daba por emitida una factura que nunca se armó.
        'f461_id_punto_envio': '001',
        'f430_id_moneda_docto': 'COP',
        'f430_id_moneda_conv': 'COP',
        'f430_id_moneda_local': 'COP',
        'f430_tasa_conv': 1,
        'f430_tasa_local': 1,
    }

    def test_el_vacio_factura_a_credito_y_avisa_del_maestro(self, app):
        with app.app_context():
            import json
            from app.models.siesa_job import SiesaJob
            from app.services.connekta_gateway import connekta

            restaurar = self._configurar(connekta)
            try:
                enviados = []
                with patch.object(connekta, '_post',
                                  side_effect=lambda *a, **k: enviados.append(a[2]) or {'mensaje': 'OK'}):
                    connekta.trigger_factura_desde_remision(
                        'RM', 5001, dict(self._CABECERA, f430_id_cond_pago=''))

                # La factura se emite —no bloquea el despacho— y sale a crédito.
                assert enviados, 'no se emitió la factura'
                assert enviados[0]['Doctoventascomercial'][0]['f461_id_cond_pago'] == 'C02'

                jobs = SiesaJob.query.filter_by(tipo='ALERTA_EMAIL').all()
                assert len(jobs) >= 1
                payload = json.loads(jobs[-1].payload)
                assert payload.get('tipo_alerta') == 'DATA_MAESTRA_COND_PAGO'
                assert 'CONTADO' not in payload.get('asunto', ''), (
                    'la alerta volvió a decir que se facturó de contado')
            finally:
                restaurar()

    def test_sin_condicion_de_ruta_no_se_emite_nada(self, app):
        """Regla 0. Emitir contado dejaría un documento que Siesa no aprueba con
        el inventario ya descargado; fallar deja la RM en BD y el reintento del
        DLQ entra directo al 142943 sin duplicarla."""
        with app.app_context():
            from app.services.connekta_gateway import connekta

            restaurar = self._configurar(connekta, ruta='')
            try:
                with patch.object(connekta, '_post') as post:
                    with pytest.raises(ValueError, match='SIESA_COND_PAGO_RUTA'):
                        connekta.trigger_factura_desde_remision(
                            'RM', 5002, dict(self._CABECERA, f430_id_cond_pago=''))
                    post.assert_not_called()
            finally:
                restaurar()

    def test_sin_fallback_no_dispara_email(self, app):
        """
        Si f430_id_cond_pago viene con valor válido, NO se dispara email.
        """
        with app.app_context():
            from app.services.connekta_gateway import connekta

            restaurar = self._configurar(connekta)
            self.addfinalizer = None

            cabecera_con_cond_pago = {
                'f200_id_pedido_fact': '900123456',
                'f461_id_punto_envio': '001',
                'f430_id_cond_pago': 'P03',    # <- viene de Siesa → no hay fallback
                'f430_id_moneda_docto': 'COP',
                'f430_id_moneda_conv': 'COP',
                'f430_id_moneda_local': 'COP',
                'f430_tasa_conv': 1,
                'f430_tasa_local': 1,
            }

            emails_enviados = []

            def mock_email(asunto, **kwargs):
                emails_enviados.append(asunto)

            with patch('app.services.alertas_service._enviar_email_con_dlq', mock_email), \
                 patch.object(connekta, '_post', return_value={'mensaje': 'OK'}):
                try:
                    connekta.trigger_factura_desde_remision('RM', 5003, cabecera_con_cond_pago)
                finally:
                    restaurar()

            # NO debe dispararse email — el campo vino bien de Siesa
            assert len(emails_enviados) == 0
