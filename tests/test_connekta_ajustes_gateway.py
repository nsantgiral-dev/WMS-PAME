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
            from app.services.connekta_gateway import connekta

            connekta.tipo_docto_ajuste = 'ADI'
            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
            connekta.enviar_ajuste_inventario(
                motivo_codigo='AJ-ENT', item_codigo='PAPELSP9218',
                cantidad=5, referencia='conteo cíclico', bodega='NB1', centro_op='003',
            )
            mov = capturado['payload']['Movimientos'][0]
            assert mov['f470_id_motivo'] == connekta.motivo_ajuste_entrada
            assert mov['f470_id_bodega'] == 'NB1'
            assert mov['f470_cant_base'] == 5.0
            assert mov['f470_desc_varible'] == '', 'typo intencional del spec 142951'


class TestTransferirAAverias:
    def test_payload_usa_bodega_y_bodega_averias_del_core(self, app, monkeypatch):
        with app.app_context():
            from app.services.connekta_gateway import connekta

            capturado = {}

            def _fake_post(conector, nombre, payload):
                capturado['payload'] = payload
                return {'codigo': 0}

            monkeypatch.setattr(connekta, '_post', _fake_post)
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
