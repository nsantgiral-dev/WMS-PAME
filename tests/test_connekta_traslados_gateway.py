"""
Tests directos de ConnektaTrasladosGateway (app/services/connekta_traslados_gateway.py),
extraído de ConnektaGateway el 2026-09-09 (paso 6 de la deuda de tamaño).

10 métodos migrados verbatim (self. -> core.). Estos tests confirman que el
patrón `core = self._core` preserva el comportamiento exacto tras el
movimiento, complementando la cobertura de comportamiento ya existente en
tests/test_10_traslados.py (87 tests, corre contra el delegado en
ConnektaGateway sin conocer la existencia de este archivo).
"""
import pytest


@pytest.fixture(autouse=True)
def _vars_siesa_minimas(app, monkeypatch):
    """connekta es un singleton de módulo ya construido antes de que corra
    cualquier fixture — setear variables de entorno acá no lo alcanza (mismo
    hueco ya visto en test_connekta_ajustes_gateway.py). Se monkeypatchean
    los atributos directamente, solo si vinieron vacíos del entorno real."""
    with app.app_context():
        from app.services.connekta_gateway import connekta

    if not connekta.tipo_docto_req_traslado:
        monkeypatch.setattr(connekta, 'tipo_docto_req_traslado', 'RIT')
    if not connekta.tipo_docto_transito_salida:
        monkeypatch.setattr(connekta, 'tipo_docto_transito_salida', 'STS')
    if not connekta.tipo_docto_transito_entrada:
        monkeypatch.setattr(connekta, 'tipo_docto_transito_entrada', 'ETS')
    if not connekta.motivo_traslado:
        monkeypatch.setattr(connekta, 'motivo_traslado', '01')


class TestCrearRequisicionTraslado:
    def test_sin_tipo_docto_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'tipo_docto_req_traslado', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_TRASLADO'):
                connekta.crear_requisicion_traslado(
                    'NB1', 'NC1', [{'codigo_siesa': 'P001', 'cantidad': 5}], 'ST-1')

    def test_item_sin_codigo_siesa_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            with pytest.raises(ValueError, match='codigo_siesa'):
                connekta.crear_requisicion_traslado(
                    'NB1', 'NC1', [{'codigo': 'INTERNO-1', 'cantidad': 5}], 'ST-1')

    def test_payload_arma_documentos_y_movimientos(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.crear_requisicion_traslado(
                'NB1', 'NC1', [{'codigo_siesa': 'PAPELSP9218', 'cantidad': 5}], 'ST-20260909-1')

            doc = capturado['payload']['Documentos'][0]
            mov = capturado['payload']['Movimientos'][0]
            assert doc['f440_id_bodega_salida'] == 'NB1'
            assert doc['f440_id_bodega_entrada'] == 'NC1'
            assert doc['f440_referencia'] == 'ST-20260909-1'
            assert mov['f441_referencia_item'] == 'PAPELSP9218'
            assert mov['f441_cant_base'] == 5.0


class TestComprosimosDesdeRequisicion:
    def test_sin_consec_rit_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            with pytest.raises(ValueError, match='consec_rit obligatorio'):
                connekta.compromisos_desde_requisicion(
                    None, 'NB1', 'NC1', [{'codigo_siesa': 'P001', 'cantidad': 5}])

    def test_filtra_items_sin_codigo_o_cantidad_cero(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.compromisos_desde_requisicion(
                42, 'NB1', 'NC1',
                [
                    {'codigo_siesa': 'PAPELSP9218', 'cantidad': 5},
                    {'codigo_siesa': '', 'cantidad': 3},
                    {'codigo_siesa': 'OTRO', 'cantidad': 0},
                ])

            compromisos = capturado['payload']['Compromisos']
            assert len(compromisos) == 1
            assert compromisos[0]['f441_referencia_item'] == 'PAPELSP9218'
            assert compromisos[0]['f440_consec_docto'] == 42


class TestTransferenciaTransitoSalida:
    def test_sin_tipo_docto_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'tipo_docto_transito_salida', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_TRANSITO_SALIDA'):
                connekta.transferencia_transito_salida(
                    'NB1', 'TRA1', [{'codigo_siesa': 'P001', 'cantidad': 5}], 'ST-1')

    def test_payload_usa_bodega_destino_no_transito_en_bodega_entrada(self, app, monkeypatch):
        """f450_id_bodega_entrada debe ser bodega_destino (validación 62485 del ETS
        exige que coincida con lo que el STS registró)."""
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.transferencia_transito_salida(
                'NB1', 'TRA1', [{'codigo_siesa': 'PAPELSP9218', 'cantidad': 5}], 'ST-1',
                bodega_destino='NC1')

            doc = capturado['payload']['Documentos'][0]
            assert doc['f450_id_bodega_salida'] == 'NB1'
            assert doc['f450_id_bodega_entrada'] == 'NC1'

    def test_desc_varible_es_string_vacio_no_none(self, app, monkeypatch):
        """DEBE ser '' — None omite el campo y Siesa rechaza por tamaño de registro."""
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.transferencia_transito_salida(
                'NB1', 'TRA1', [{'codigo_siesa': 'PAPELSP9218', 'cantidad': 5}], 'ST-1')

            mov = capturado['payload']['Movimientos'][0]
            assert mov['f470_desc_varible'] == ''


class TestTransferenciaTransitoEntrada:
    def test_sin_consec_salida_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            with pytest.raises(ValueError, match='consec_salida obligatorio'):
                connekta.transferencia_transito_entrada(
                    'TRA1', 'NC1', [{'codigo_siesa': 'P001', 'cantidad': 5}], 'ST-1')

    def test_usa_bodega_origen_no_transito_ni_en_bodega_salida_ni_en_movimientos(
            self, app, monkeypatch):
        """Regresión del bug del commit 1344c7a: f450_id_bodega_salida y
        f470_id_bodega deben ser bodega_origen, NUNCA bodega_transito."""
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload, url=None, extra_params=None):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.transferencia_transito_entrada(
                'TRA1', 'NC1', [{'codigo_siesa': 'PAPELSP9218', 'cantidad': 5}], 'ST-1',
                consec_salida=53, bodega_origen='NB1')

            doc = capturado['payload']['Documentos'][0]
            mov = capturado['payload']['Movimientos'][0]
            assert doc['f450_id_bodega_salida'] == 'NB1'
            assert doc['f450_id_bodega_salida'] != 'TRA1'
            assert mov['f470_id_bodega'] == 'NB1'
            assert doc['f350_consec_docto_base'] == 53


class TestRecoverySTS:
    def test_get_consec_salida_transito_by_alterno_usa_llamada_intra_dominio(
            self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(
                connekta._traslados, 'get_sts_info_by_alterno',
                lambda codigo: {'consec': 77, 'bodega_transito': 'TRA1'})
            assert connekta.get_consec_salida_transito_by_alterno('ST-1') == 77

    def test_get_sts_info_by_alterno_sin_alterno_retorna_none(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, '_fmt_alterno', lambda x: None)
            assert connekta.get_sts_info_by_alterno('') is None

    def test_get_sts_info_by_alterno_parsea_consec_y_bodega(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            def _fake_get(nombre, params_extra=None, url=None):
                return {'detalle': {'Table': [{
                    'f350_consec_docto': 53,
                    'f150_id_bodega_entrada': ' TRA1 ',
                }]}}

            monkeypatch.setattr(connekta, '_get', _fake_get)
            info = connekta.get_sts_info_by_alterno('ST-1')
            assert info == {'consec': 53, 'bodega_transito': 'TRA1'}

    def test_get_sts_info_by_alterno_sin_filas_retorna_none(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, '_get', lambda *a, **k: {'detalle': {'Table': []}})
            assert connekta.get_sts_info_by_alterno('ST-1') is None

    def test_get_sts_info_by_alterno_excepcion_no_propaga(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            def _boom(*a, **k):
                raise RuntimeError('caído')

            monkeypatch.setattr(connekta, '_get', _boom)
            assert connekta.get_sts_info_by_alterno('ST-1') is None


class TestRecoveryETS:
    def test_get_consec_entrada_transito_by_alterno_parsea_consec(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            def _fake_get(nombre, params_extra=None, url=None):
                return {'detalle': {'Table': [{'f350_consec_docto': 19}]}}

            monkeypatch.setattr(connekta, '_get', _fake_get)
            assert connekta.get_consec_entrada_transito_by_alterno('ST-1') == 19

    def test_get_consec_entrada_transito_by_alterno_excepcion_no_propaga(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            def _boom(*a, **k):
                raise RuntimeError('caído')

            monkeypatch.setattr(connekta, '_get', _boom)
            assert connekta.get_consec_entrada_transito_by_alterno('ST-1') is None


class TestRecoveryRIT:
    def test_get_consec_rit_by_referencia_filtra_por_referencia_exacta(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            def _fake_get(nombre, params_extra=None, url=None):
                return {'detalle': {'Table': [
                    {'f440_referencia': 'ST-OTRO', 'f440_consec_docto': 1},
                    {'f440_referencia': 'ST-1', 'f440_consec_docto': 42},
                ]}}

            monkeypatch.setattr(connekta, '_get', _fake_get)
            assert connekta.get_consec_rit_by_referencia('ST-1') == 42

    def test_get_consec_rit_by_referencia_sin_match_retorna_none(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            def _fake_get(nombre, params_extra=None, url=None):
                return {'detalle': {'Table': [{'f440_referencia': 'ST-OTRO', 'f440_consec_docto': 1}]}}

            monkeypatch.setattr(connekta, '_get', _fake_get)
            assert connekta.get_consec_rit_by_referencia('ST-1') is None


class TestTransferenciaDirecta:
    def test_item_sin_codigo_siesa_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            with pytest.raises(ValueError, match='codigo_siesa'):
                connekta.transferencia_directa(
                    'NB1', 'NC1', [{'codigo': 'INTERNO-1', 'cantidad': 5}], 'ST-1')

    def test_payload_sin_docto_alterno_ni_referencia_a_base(self, app, monkeypatch):
        """spec 173066 no tiene f450_docto_alterno/f350_id_co_base — no deben aparecer."""
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.transferencia_directa(
                'NB1', 'NC1', [{'codigo_siesa': 'PAPELSP9218', 'cantidad': 5}], 'ST-1')

            doc = capturado['payload']['Documentos'][0]
            assert 'f450_docto_alterno' not in doc
            assert 'f350_id_co_base' not in doc
            assert doc['f450_id_bodega_salida'] == 'NB1'
            assert doc['f450_id_bodega_entrada'] == 'NC1'


class TestTransferenciaDesdeRequisicion:
    def test_sin_tipo_docto_transito_salida_lanza_valueerror(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            monkeypatch.setattr(connekta, 'tipo_docto_transito_salida', '')
            with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_TRANSITO_SALIDA'):
                connekta.transferencia_desde_requisicion(42)

    def test_payload_referencia_la_rit_por_consec(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.transferencia_desde_requisicion(42)

            doc = capturado['payload']['Documentos'][0]
            assert doc['f440_consec_docto_req_int'] == 42


def test_connekta_gateway_delega_al_dominio_traslados(app):
    from app.services.connekta_traslados_gateway import ConnektaTrasladosGateway

    with app.app_context():
        from app.services.connekta_gateway import connekta

        assert isinstance(connekta._traslados, ConnektaTrasladosGateway)
        assert connekta._traslados._core is connekta
