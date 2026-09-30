"""
Tests directos de ConnektaAjustesGateway (app/services/connekta_ajustes_gateway.py),
extraído de ConnektaGateway el 2026-09-09 (paso 4 de la deuda de tamaño).

Ni enviar_ajuste_inventario ni transferir_a_averias tenían cobertura propia
antes de esta extracción (gap preexistente, no introducido acá) — esto es
la primera cobertura directa de ambos, además de confirmar que el patrón
`core = self._core` preserva el comportamiento exacto tras el movimiento.
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _vars_siesa_minimas():
    os.environ.setdefault('SIESA_TIPO_DOCTO_AJUSTE', 'ADI')
    os.environ.setdefault('SIESA_MOTIVO_TRASLADO', '01')


class TestEnviarAjusteInventario:
    def test_motivo_invalido_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            with pytest.raises(ValueError, match='Motivo inválido'):
                connekta.enviar_ajuste_inventario(
                    motivo_codigo='OTRO', item_codigo='P001',
                    cantidad=5, referencia='conteo cíclico',
                )

    def test_sin_tipo_docto_ajuste_lanza_valueerror(self, app):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            viejo = connekta.tipo_docto_ajuste
            connekta.tipo_docto_ajuste = ''
            os.environ.pop('SIESA_TIPO_DOCTO_AJUSTE', None)
            try:
                with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_AJUSTE'):
                    connekta.enviar_ajuste_inventario(
                        motivo_codigo='AJ-ENT', item_codigo='P001',
                        cantidad=5, referencia='conteo cíclico',
                    )
            finally:
                connekta.tipo_docto_ajuste = viejo
                os.environ['SIESA_TIPO_DOCTO_AJUSTE'] = 'ADI'

    def test_payload_sobrante_usa_motivo_entrada_y_no_llama_a_siesa_real(self, app, monkeypatch):
        """AJ-ENT arma el payload con motivo_ajuste_entrada — no dispara el
        POST real (modo simulación, sin credenciales)."""
        with app.app_context():
            from app.services.connekta_gateway import connekta, ConnektaGateway

            monkeypatch.setattr(connekta, 'tipo_docto_ajuste', 'ADI')
            capturado = {}

            def _fake_post(self, conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(ConnektaGateway, '_post', _fake_post)
            connekta.enviar_ajuste_inventario(
                motivo_codigo='AJ-ENT', item_codigo='PAPELSP9218',
                cantidad=5, referencia='conteo cíclico', bodega='NB1', centro_op='003',
            )
            mov = capturado['payload']['Movimientos'][0]
            assert mov['f470_id_motivo'] == connekta.motivo_ajuste_entrada
            assert mov['f470_id_bodega'] == 'NB1'
            assert mov['f470_cant_base'] == 5.0
            assert mov['f470_desc_varible'] == '', 'typo intencional del spec 142951'


class TestElCostoDeLaEntrada:
    """`f470_costo_prom_uni` (spec 142951, pos 212): vacío, Siesa valoriza
    con el promedio de la bodega — y sin fila ese promedio es 0 (ADI-00000040
    de QA, 2026-09-29). Solo una ENTRADA lo lleva."""

    def _payload(self, app, monkeypatch, motivo_entrada='01', **kw):
        with app.app_context():
            from app.services.connekta_gateway import connekta, ConnektaGateway
            monkeypatch.setattr(connekta, 'tipo_docto_ajuste', 'ADI')
            monkeypatch.setattr(connekta, 'motivo_entrada_inventario', motivo_entrada)
            capturado = {}

            def _fake_post(self, conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}
            monkeypatch.setattr(ConnektaGateway, '_post', _fake_post)
            connekta.enviar_ajuste_inventario(
                item_codigo='PAPELSP7877', cantidad=10, referencia='CC-X',
                bodega='NB1', centro_op='003', **kw)
            return capturado['payload']

    def test_la_entrada_con_costo_es_una_entrada_clase_61(self, app, monkeypatch):
        """La clase 63 rechaza cantidad + costo («El ajuste debe ser solo en
        costo o en solo cantidad», QA 2026-09-29): con costo va como ENTRADA,
        clase 61 / concepto 601 / motivo 0601-01, en el mismo tipo ADI."""
        p = self._payload(app, monkeypatch, motivo_codigo='AJ-ENT', costo_unitario=1044)
        doc, mov = p['Documentos'][0], p['Movimientos'][0]
        assert mov['f470_costo_prom_uni'] == 1044.0
        assert mov['f470_cant_base'] == 10.0
        assert doc['f350_id_clase_docto'] == 61
        assert doc['f450_id_concepto'] == 601
        assert mov['f470_id_concepto'] == 601
        assert mov['f470_id_motivo'] == '01'
        assert doc['f350_id_tipo_docto'] == 'ADI'

    def test_el_costo_va_con_los_decimales_de_la_moneda_local(self, app, monkeypatch):
        """PAPELSP11310 (QA, 2026-09-30): el promedio ponderado de otras bodegas
        dio 625,0994 y Siesa rechazó el documento: «La cantidad de decimales del
        costo unitario deben ser iguales a la cantidad de decimales de unidades
        de la moneda local». El peso lleva 2."""
        p = self._payload(app, monkeypatch, motivo_codigo='AJ-ENT', costo_unitario=625.0994)
        assert p['Movimientos'][0]['f470_costo_prom_uni'] == 625.10

    def test_un_costo_que_redondea_a_cero_no_sale(self, app, monkeypatch):
        with pytest.raises(ValueError, match='Costo unitario inválido'):
            self._payload(app, monkeypatch, motivo_codigo='AJ-ENT', costo_unitario=0.004)

    def test_la_entrada_con_costo_sin_motivo_configurado_no_sale(self, app, monkeypatch):
        with pytest.raises(ValueError, match='SIESA_MOTIVO_ENTRADA_INVENTARIO'):
            self._payload(app, monkeypatch, motivo_entrada='',
                          motivo_codigo='AJ-ENT', costo_unitario=1044)

    def test_sin_costo_sigue_siendo_un_ajuste_clase_63(self, app, monkeypatch):
        from app.services.connekta_gateway import connekta
        p = self._payload(app, monkeypatch, motivo_codigo='AJ-ENT')
        doc, mov = p['Documentos'][0], p['Movimientos'][0]
        assert mov['f470_costo_prom_uni'] is None
        assert doc['f350_id_clase_docto'] == 63
        assert mov['f470_id_concepto'] == connekta.concepto_ajustes
        assert mov['f470_id_motivo'] == connekta.motivo_ajuste_entrada

    def test_la_salida_nunca_lleva_costo(self, app, monkeypatch):
        p = self._payload(app, monkeypatch, motivo_codigo='AJ-SAL', costo_unitario=1044)
        assert p['Movimientos'][0]['f470_costo_prom_uni'] is None
        assert p['Documentos'][0]['f350_id_clase_docto'] == 63

    @pytest.mark.parametrize('malo', [0, -5])
    def test_un_costo_no_positivo_no_sale(self, app, monkeypatch, malo):
        with pytest.raises(ValueError, match='Costo unitario inválido'):
            self._payload(app, monkeypatch, motivo_codigo='AJ-ENT', costo_unitario=malo)


class TestTransferirAAverias:
    def test_payload_usa_bodega_y_bodega_averias_del_core(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta, ConnektaGateway

            capturado = {}

            def _fake_post(self, conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(ConnektaGateway, '_post', _fake_post)
            connekta.transferir_a_averias('PAPELSP9218', 3, referencia='avería en recepción')

            doc = capturado['payload']['Documentos'][0]
            mov = capturado['payload']['Movimientos'][0]
            assert doc['f450_id_bodega_salida'] == connekta.bodega
            assert doc['f450_id_bodega_entrada'] == connekta.bodega_averias
            assert mov['f470_referencia_item'] == 'PAPELSP9218'
            assert mov['f470_cant_base'] == 3.0


def test_connekta_gateway_delega_al_dominio_ajustes(app):
    from app.services.connekta_ajustes_gateway import ConnektaAjustesGateway

    with app.app_context():
        from app.services.connekta_gateway import connekta

        assert isinstance(connekta._ajustes, ConnektaAjustesGateway)
        assert connekta._ajustes._core is connekta
