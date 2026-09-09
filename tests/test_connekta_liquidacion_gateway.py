"""
Tests directos de ConnektaLiquidacionGateway
(app/services/connekta_liquidacion_gateway.py), extraído de ConnektaGateway
el 2026-09-09 (paso 8 y último de la deuda de tamaño).

Comportamiento cubierto también por tests/test_payload_vs_docx.py (specs
DOCX 142946/251126/142888/142882), tests/test_nc_motivo_dian.py y
tests/test_idempotencia_retenciones.py contra el delegado en
ConnektaGateway (sin cambios) — este archivo agrega cobertura directa
sobre la clase extraída, requerida por test_coverage_guard.py, y confirma
que el patrón `core = self._core` preserva el comportamiento exacto tras
el movimiento.
"""
import pytest


@pytest.fixture(autouse=True)
def _vars_liquidacion_minimas(app, monkeypatch):
    """connekta es un singleton de módulo ya construido antes de que corra
    cualquier fixture — setear variables de entorno acá no lo alcanza (mismo
    hueco ya visto en los demás test_connekta_*_gateway.py)."""
    with app.app_context():
        from app.services.connekta_gateway import connekta

    if not connekta.tipo_docto_nota_credito:
        monkeypatch.setattr(connekta, 'tipo_docto_nota_credito', 'NCE')
    if not connekta.tipo_docto_recibo_caja:
        monkeypatch.setattr(connekta, 'tipo_docto_recibo_caja', 'RC')
    if not connekta.tipo_docto_docto_contable:
        monkeypatch.setattr(connekta, 'tipo_docto_docto_contable', 'NI')
    if not connekta.motivo_ventas:
        monkeypatch.setattr(connekta, 'motivo_ventas', '01')


class TestTriggerNotaFactura:
    def test_sin_tipo_docto_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'tipo_docto_nota_credito', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_NOTA_CREDITO'):
                connekta.trigger_nota_factura('FEW', '1', [])

    def test_payload_arma_header_transportador_y_movimientos(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_nota_factura('FEW', '100', [{
                'f120_referencia': 'P001', 'f470_cant_base': 3,
                'f470_rowid_movto': 55,
            }])
            doc = capturado['payload']['Doctoventascomercial'][0]
            mov = capturado['payload']['Movimientos'][0]
            assert doc['F430_ID_TIPO_DOCTO'] == 'FEW'
            assert doc['F430_CONSEC_DOCTO'] == 100
            assert doc['F350_IND_ESTADO'] == 0, 'NC siempre en Elaboración, nunca Aprobado'
            assert 'f462_id_vehiculo' in doc, 'falta el bloque transportador vacío'
            assert mov['f470_referencia_item'] == 'P001'
            assert mov['f470_rowid_movto'] == 55
            assert mov['f470_id_concepto'] == 502


class TestTriggerNotaFacturaCrearCruzar:
    def test_sin_tipo_docto_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'tipo_docto_nota_credito', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_NOTA_CREDITO'):
                connekta.trigger_nota_factura_crear_cruzar('FEW', '1', [], 1000.0)

    def test_payload_incluye_cuotas_cxc_con_el_valor_de_cruce(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'get_vencimiento_factura', lambda t, c: '20260930')
            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_nota_factura_crear_cruzar('FEW', '100', [{
                'f120_referencia': 'P001', 'f470_cant_base': 2,
                'f470_rowid_movto': 77,
            }], 45000.0)

            cuota = capturado['payload']['Cuotas CxC'][0]
            doc = capturado['payload']['Docto. ventas comercial'][0]
            assert cuota['F353_CONSEC_DOCTO_CRUCE'] == doc['F430_CONSEC_DOCTO']
            assert float(cuota['F353_VLR_CRUCE']) == 45000.0
            assert doc['F350_IND_ESTADO'] == 0


class TestPuedeFijarMotivoDian:
    def test_false_sin_consulta_configurada(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'consulta_nc_consecutivo', '')
            assert connekta.puede_fijar_motivo_dian is False

    def test_true_con_todo_configurado(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'consulta_nc_consecutivo', 'CONSULTA_X')
            monkeypatch.setattr(connekta, 'conector_nc_motivo_dian', '251546')
            monkeypatch.setattr(connekta, 'modo_simulacion', False)
            assert connekta.puede_fijar_motivo_dian is True


class TestFilasNcEncabezadoYConsecutivos:
    def test_filas_nc_encabezado_vacio_sin_consulta_configurada(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'consulta_nc_consecutivo', '')
            assert connekta._filas_nc_encabezado() == []

    def test_filas_nc_encabezado_descarta_fantasmas_sin_rowid(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'consulta_nc_consecutivo', 'CONSULTA_X')
            monkeypatch.setattr(connekta, '_get', lambda *a, **k: {'detalle': {'Datos': [
                {'f350_rowid': None}, {'f350_rowid': 5, 'f350_id_co': '003'},
            ]}})
            filas = connekta._filas_nc_encabezado()
            assert len(filas) == 1
            assert filas[0]['f350_rowid'] == 5

    def test_get_max_rowid_nc_no_propaga_excepcion(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import ConnektaGateway, connekta

            def _boom(self):
                raise RuntimeError('caído')

            # Parchea la CLASE, no la instancia: `monkeypatch.setattr(connekta,
            # ...)` sobre un método deja un atributo de instancia permanente
            # tras el teardown (pytest restaura con `setattr`, no `delattr`,
            # cuando el valor original venía heredado de la clase) — ese
            # sombreado sobrevivía al test y volvía sordos los parches por
            # clase de tests/test_nc_motivo_dian.py que corrían después.
            monkeypatch.setattr(ConnektaGateway, '_filas_nc_encabezado', _boom)
            assert connekta.get_max_rowid_nc() is None

    def test_get_max_rowid_nc_retorna_el_mayor(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import ConnektaGateway, connekta

            monkeypatch.setattr(ConnektaGateway, '_filas_nc_encabezado',
                                 lambda self: [{'f350_rowid': 3}, {'f350_rowid': 9}])
            assert connekta.get_max_rowid_nc() == 9

    def test_get_consec_nc_creada_falla_sin_candidatas(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import ConnektaGateway, connekta

            monkeypatch.setattr(ConnektaGateway, '_filas_nc_encabezado', lambda self: [])
            with pytest.raises(Exception, match='candidatas'):
                connekta.get_consec_nc_creada(1000.0, '20260909')

    def test_get_consec_nc_creada_falla_con_dos_candidatas(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import ConnektaGateway, connekta

            fila_base = {
                'f350_id_co': connekta.centro_op,
                'f350_id_tipo_docto': connekta.tipo_docto_nota_credito,
                'f350_ind_estado': 0,
                'f350_fecha': '2026-09-09',
                'f350_rowid': 10,
                'f350_consec_docto': 1,
            }
            monkeypatch.setattr(
                ConnektaGateway, '_filas_nc_encabezado',
                lambda self: [fila_base, dict(fila_base, f350_rowid=11, f350_consec_docto=2)])
            with pytest.raises(Exception, match='candidatas'):
                connekta.get_consec_nc_creada(1000.0, '20260909', rowid_minimo=5)

    def test_get_consec_nc_creada_retorna_la_unica_candidata(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import ConnektaGateway, connekta

            fila = {
                'f350_id_co': connekta.centro_op,
                'f350_id_tipo_docto': connekta.tipo_docto_nota_credito,
                'f350_ind_estado': 0,
                'f350_fecha': '2026-09-09',
                'f350_rowid': 10,
                'f350_consec_docto': 42,
            }
            monkeypatch.setattr(ConnektaGateway, '_filas_nc_encabezado', lambda self: [fila])
            assert connekta.get_consec_nc_creada(1000.0, '20260909', rowid_minimo=5) == 42


class TestTriggerMotivoDianNc:
    def test_payload_referencia_la_nc_y_el_concepto(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'concepto_dian_nc', '1')
            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_motivo_dian_nc(57, concepto='3')

            entidad = capturado['payload']['Entidades dinámicas'][0]
            assert entidad['f350_consec_docto'] == 57
            assert entidad['f753_id_maestro_detalle'] == '3'


class TestTriggerReciboCaja:
    def test_sin_tipo_docto_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'tipo_docto_recibo_caja', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_RECIBO_CAJA'):
                connekta.trigger_recibo_caja('900123', '001', 1000.0, 'EFECTIVO', 'FEW', '1')

    def test_forma_pago_desconocida_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            with pytest.raises(ValueError, match='forma_pago'):
                connekta.trigger_recibo_caja('900123', '001', 1000.0, 'CHEQUE', 'FEW', '1')

    def test_ajuste_mayor_o_igual_al_monto_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            with pytest.raises(ValueError, match='ajuste al peso'):
                connekta.trigger_recibo_caja('900123', '001', 1000.0, 'EFECTIVO', 'FEW', '1',
                                              ajuste_valor=1000.0)

    def test_sin_ajuste_cr_es_el_monto_completo(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_recibo_caja('900123', '001', 1000.0, 'EFECTIVO', 'FEW', '1')

            cxc = capturado['payload']['CxC'][0]
            caja = capturado['payload']['Caja'][0]
            assert float(cxc['F354_VALOR_CR']) == 1000.0
            assert float(cxc['F354_VALOR_APROVECHA']) == 0.0
            assert float(caja['F358_VALOR']) == 1000.0

    def test_ajuste_faltante_resta_del_cr_y_declara_aprovecha(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_recibo_caja('900123', '001', 1000.0, 'EFECTIVO', 'FEW', '1',
                                          ajuste_valor=100.0, ajuste_es_sobrante=False)

            cxc = capturado['payload']['CxC'][0]
            assert float(cxc['F354_VALOR_CR']) == 900.0
            assert float(cxc['F354_VALOR_APROVECHA']) == 100.0

    def test_ajuste_sobrante_sube_el_ingreso_y_declara_otro_ingreso(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_recibo_caja('900123', '001', 1000.0, 'EFECTIVO', 'FEW', '1',
                                          ajuste_valor=50.0, ajuste_es_sobrante=True)

            header = capturado['payload']['RCyotrosingresos'][0]
            cxc = capturado['payload']['CxC'][0]
            assert float(header['F357_VALOR_INGRESO']) == 1050.0
            assert float(cxc['F354_VALOR_CR']) == 1000.0
            assert float(cxc['F354_VALOR_APROVECHA']) == 0.0
            assert header['F351_ID_TERCERO_OTRO_ING'] == '900123'


class TestTriggerDocumentoContable:
    def test_sin_tipo_docto_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'tipo_docto_docto_contable', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_DOCTO_CONTABLE'):
                connekta.trigger_documento_contable(
                    '900123', '001', '13551501', 500.0, 5000.0, 'FEW', '1')

    def test_verifica_partida_doble(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            llamado = {}
            monkeypatch.setattr(connekta, '_verificar_partida_doble_dc',
                                 lambda payload: llamado.setdefault('payload', payload))
            monkeypatch.setattr(connekta, '_post', lambda *a, **k: {'codigo': 0})
            connekta.trigger_documento_contable(
                '900123', '001', '13551501', 500.0, 5000.0, 'FEW', '1')
            assert 'payload' in llamado, 'debe verificar partida doble antes de enviar'

    def test_ajuste_agrega_segunda_linea_de_movimiento_contable(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.trigger_documento_contable(
                '900123', '001', '13551501', 500.0, 5000.0, 'FEW', '1',
                ajuste_valor=30.0, ajuste_razon='faltante en ruta')

            movs = capturado['payload']['Movimientocontable']
            cxc = capturado['payload']['MovimientoCxC'][0]
            assert len(movs) == 2, 'la retención + el ajuste deben ir en líneas separadas'
            assert movs[1]['F351_ID_AUXILIAR'] == connekta.cuenta_ajuste_faltante
            assert float(movs[1]['F351_VALOR_DB']) == 30.0
            assert float(cxc['F351_VALOR_CR']) == 530.0, 'el crédito de cartera suma retención + ajuste'


def test_connekta_gateway_delega_al_dominio_liquidacion(app):
    from app.services.connekta_liquidacion_gateway import ConnektaLiquidacionGateway

    with app.app_context():
        from app.services.connekta_gateway import connekta

        assert isinstance(connekta._liquidacion, ConnektaLiquidacionGateway)
        assert connekta._liquidacion._core is connekta
