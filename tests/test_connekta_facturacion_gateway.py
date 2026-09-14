"""
Tests directos de ConnektaFacturacionGateway
(app/services/connekta_facturacion_gateway.py), extraído de ConnektaGateway
el 2026-09-09 (paso 7 de la deuda de tamaño).

Comportamiento cubierto también por tests/test_09_guards_criticos.py,
tests/test_liquidacion_desglose.py y tests/test_cond_pago.py contra el
delegado en ConnektaGateway (sin cambios) — este archivo agrega cobertura
directa sobre la clase extraída, requerida por test_coverage_guard.py, y
confirma que el patrón `core = self._core` preserva el comportamiento
exacto tras el movimiento.
"""
import pytest


@pytest.fixture(autouse=True)
def _vars_facturacion_minimas(app, monkeypatch):
    """connekta es un singleton de módulo ya construido antes de que corra
    cualquier fixture — setear variables de entorno acá no lo alcanza (mismo
    hueco ya visto en test_connekta_ajustes_gateway.py / traslados)."""
    with app.app_context():
        from app.services.connekta_gateway import connekta

    if not connekta.tipo_docto_remision:
        monkeypatch.setattr(connekta, 'tipo_docto_remision', 'RM')
    if not connekta.motivo_ventas:
        monkeypatch.setattr(connekta, 'motivo_ventas', '01')
    if not connekta.tipo_docto_factura:
        monkeypatch.setattr(connekta, 'tipo_docto_factura', 'FEW')
    if not connekta.cond_pago_ruta:
        monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02')
    if not connekta.punto_envio_default:
        monkeypatch.setattr(connekta, 'punto_envio_default', '000')


class TestTriggerComprometerPedido:
    def test_modo_simulacion_no_llama_post(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', True)
            res = connekta.trigger_comprometer_pedido(
                '100', [{'referencia_item': 'P001', 'cant_base': 5,
                         'nro_registro': 1, 'cant_por_remisionar': 5}])
            assert res == {'simulado': True}

    def test_prioriza_id_item_sobre_referencia(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_comprometer_pedido('100', [{
                'referencia_item': 'P001', 'id_item': 12345,
                'cant_base': 8, 'nro_registro': 470418, 'cant_por_remisionar': 8,
            }])
            linea = capturado['payload']['Compromisos'][0]
            assert linea['f431_id_item'] == 12345
            assert linea['f431_referencia_item'] is None
            assert linea['f431_nro_registro'] == 470418
            assert linea['f431_cant_base'] == 8.0
            assert linea['f405_cant_por_remisionar_base'] == 8.0


class TestTriggerDespacho:
    def test_sin_tipo_docto_remision_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            monkeypatch.setattr(connekta, 'tipo_docto_remision', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_REMISION'):
                connekta.trigger_despacho('PD', '1', [{'producto_codigo': 'P001',
                                                        'cantidad_empacada': 1}])

    def test_sin_motivo_ventas_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            monkeypatch.setattr(connekta, 'motivo_ventas', '')
            with pytest.raises(ValueError, match='SIESA_ID_MOTIVO_VENTAS'):
                connekta.trigger_despacho('PD', '1', [{'producto_codigo': 'P001',
                                                        'cantidad_empacada': 1}])

    def test_sin_items_con_cantidad_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            with pytest.raises(ValueError, match='cantidad_empacada'):
                connekta.trigger_despacho('PD', '1', [{'producto_codigo': 'P001',
                                                        'cantidad_empacada': 0}])

    def test_filtra_items_en_cero_y_arma_payload(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_despacho('PD', '1', [
                {'producto_codigo': 'P001', 'cantidad_empacada': 5},
                {'producto_codigo': 'P002', 'cantidad_empacada': 0},
            ])
            movs = capturado['payload']['Movtoventascomercial']
            assert len(movs) == 1
            assert movs[0]['f470_referencia_item'] == 'P001'
            assert movs[0]['f470_cant_base'] == 5.0
            assert movs[0]['f470_ind_naturaleza'] == 2


class TestTriggerFactura:
    def test_tipo_docto_vacio_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            with pytest.raises(ValueError, match='tipo_docto_pedido'):
                connekta.trigger_factura('', '1', [])

    def test_pedido_ya_cumplido_no_llama_post(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            monkeypatch.setattr(connekta, 'get_estado_pedido', lambda t, c: '4')
            llamado = {}
            monkeypatch.setattr(connekta, '_post',
                                 lambda *a, **k: llamado.setdefault('si', True))
            res = connekta.trigger_factura('PD', '1', [])
            assert res == {'idempotente': True, 'mensaje': 'Pedido ya facturado en Siesa (estado=4)'}
            assert 'si' not in llamado

    def test_payload_referencia_el_pedido(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            monkeypatch.setattr(connekta, 'get_estado_pedido', lambda t, c: '3')
            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_factura('PD', '100', [])
            doc = capturado['payload']['Docto_ventas_comercial'][0]
            assert doc['F430_ID_TIPO_DOCTO'] == 'PD'
            assert doc['F430_CONSEC_DOCTO'] == 100


class TestTriggerFacturaDesdeRemision:
    def test_sin_cond_pago_ni_fallback_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'cond_pago_ruta', '')
            with pytest.raises(ValueError, match='SIESA_COND_PAGO_RUTA'):
                connekta.trigger_factura_desde_remision('RM', 1, {
                    'f200_id_pedido_fact': '900123', 'f461_id_punto_envio': '000',
                })

    def test_sin_punto_envio_ni_default_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'punto_envio_default', None)
            with pytest.raises(ValueError, match='f461_id_punto_envio'):
                connekta.trigger_factura_desde_remision('RM', 1, {
                    'f200_id_pedido_fact': '900123', 'f430_id_cond_pago': 'C02',
                })

    def test_payload_usa_condicion_de_ruta_si_pedido_no_trae_ninguna(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_factura_desde_remision('RM', 42, {
                'f200_id_pedido_fact': '900123', 'f461_id_punto_envio': '000',
            })
            doc = capturado['payload']['Doctoventascomercial'][0]
            rel = capturado['payload']['RelacionDoctos'][0]
            assert doc['f461_id_cond_pago'] == 'C02'
            assert doc['F350_ID_TERCERO'] == '900123'
            assert rel['F460_ID_TIPO_DOCTO'] == 'RM'
            assert rel['F460_CONSEC_DOCTO'] == 42

    def test_sin_tercero_en_modo_real_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            with pytest.raises(ValueError, match='f200_id_pedido_fact'):
                connekta.trigger_factura_desde_remision('RM', 1, {
                    'f461_id_punto_envio': '000', 'f430_id_cond_pago': 'C02',
                })


def test_connekta_gateway_delega_al_dominio_facturacion(app):
    from app.services.connekta_facturacion_gateway import ConnektaFacturacionGateway

    with app.app_context():
        from app.services.connekta_gateway import connekta

        assert isinstance(connekta._facturacion, ConnektaFacturacionGateway)
        assert connekta._facturacion._core is connekta
