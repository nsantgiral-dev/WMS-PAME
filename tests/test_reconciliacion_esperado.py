"""
«Debían cobrarse» es lo que el conductor tenía que traer, no la factura
(validación e2e 2026-09-26): `politica_cobro.esperado_en_caja`, una función
para la reconciliación.
"""
from types import SimpleNamespace
from unittest.mock import patch


def _rec(estado, monto, descuento=0, confirmada=None, motivo=None):
    return SimpleNamespace(id=1, estado_entrega=estado, monto_cobrado=monto,
                           monto_descuento=descuento, motivo_descuento=motivo,
                           retencion_confirmada=confirmada)


def _t(valor):
    return SimpleNamespace(valor_factura=valor)


def test_entregado_menos_la_retencion_que_procede():
    from app.services import politica_cobro as pc
    r = _rec('ENTREGADO', 97_500, 2_500, True, 'RETEFUENTE_2.5')
    assert pc.esperado_en_caja(r, _t(100_000)) == (97_500, 'FACTURA')
    r.retencion_confirmada = False                     # rechazada: la factura entera
    assert pc.esperado_en_caja(r, _t(100_000)) == (100_000, 'FACTURA')


def test_parcial_menos_lo_devuelto_a_precio_de_factura():
    from app.services import politica_cobro as pc
    dev = SimpleNamespace(declaracion_conductor={}, lineas=[
        SimpleNamespace(cantidad_declarada=2, valor_unitario=8_330)])
    with patch('app.services.devolucion_ruta.devolucion_vigente', lambda _id: dev):
        v, fuente = pc.esperado_en_caja(_rec('PARCIAL', 90_565), _t(113_050))
    assert fuente == 'FACTURA_MENOS_DEVUELTO' and v == 113_050 - 16_660
    # El faltante real (96.390 − 90.565 = 5.825) queda a la vista.
    assert round(v - 90_565, 2) == 5_825


def test_parcial_sin_valorizar_no_inventa_faltante():
    from app.services import politica_cobro as pc
    dev = SimpleNamespace(declaracion_conductor={}, lineas=[
        SimpleNamespace(cantidad_declarada=2, valor_unitario=None)])
    with patch('app.services.devolucion_ruta.devolucion_vigente', lambda _id: dev):
        assert pc.esperado_en_caja(_rec('PARCIAL', 96_390), _t(113_050)) == (96_390, 'DECLARADO')
