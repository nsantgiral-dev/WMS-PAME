"""
Tests directos de ConnektaComprasGateway (app/services/connekta_compras_gateway.py),
extraído de ConnektaGateway el 2026-09-09 (paso 3 de la deuda de tamaño).

`test_09_guards_criticos.py` y `test_payload_vs_docx.py` ya cubren
confirmar_entrada_compras de punta a punta vía el singleton `connekta`
(la vía real) — esto agrega cobertura directa de la clase aislada,
confirmando que el patrón `core = self._core` no perdió nada al mover
la lógica fuera de ConnektaGateway.
"""
import pytest

from app.services.connekta_compras_gateway import ConnektaComprasGateway


def test_usa_la_config_del_core_no_una_copia(app):
    """El core no se copia — es la misma instancia. Mutar el core después
    de crear el gateway de dominio debe verse reflejado (mismo patrón que
    usa test_09_guards_criticos.py mutando connekta.tipo_docto_entrada_oc)."""
    with app.app_context():
        from app.services.connekta_gateway import connekta

        gw_compras = ConnektaComprasGateway(connekta)
        assert gw_compras._core is connekta

        connekta.tipo_docto_entrada_oc = ''
        with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_ENTRADA_OC'):
            gw_compras.confirmar_entrada_compras(
                id_co_oc='003', tipo_docto_oc='OC', consec_docto_oc='100',
                items=[{'producto_codigo': 'P001', 'cantidad_recibida': 5}],
                proveedor_id='900123456', sucursal_prov='001',
            )

        connekta.tipo_docto_entrada_oc = 'EA'
        with pytest.raises(ValueError, match='proveedor_id es None'):
            gw_compras.confirmar_entrada_compras(
                id_co_oc='003', tipo_docto_oc='OC', consec_docto_oc='100',
                items=[{'producto_codigo': 'P001', 'cantidad_recibida': 5}],
                proveedor_id=None, sucursal_prov='001',
            )


def test_connekta_gateway_delega_al_dominio_compras(app):
    """El gateway completo expone _compras y confirmar_entrada_compras sigue
    siendo la misma firma pública, ahora como delegado delgado."""
    with app.app_context():
        from app.services.connekta_gateway import connekta

        assert isinstance(connekta._compras, ConnektaComprasGateway)
        assert connekta._compras._core is connekta
