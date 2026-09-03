"""
El Armador compraba sobre existencia bruta: lo ya vendido contaba como stock.

El docstring de `rop_dual` declaraba la política correcta desde el primer día
—`posicion = stock_actual + en_transito - backorders`— y la línea que ejecuta
sumaba stock y tránsito y **nunca restaba nada**. `StockSiesa.comprometido`
existe, está poblada desde `f400_cant_comprometida_1`, y la palabra
`comprometido` no aparecía en ninguna línea de `armador_service.py`.

Qué costaba, y por qué pesa distinto que en los otros sitios que ya calculaban
esto bien — citados por nombre y no por línea, porque las tres citas originales
quedaron rotas en el mismo lote en que se escribieron: un agente vecino agregó
líneas encima y las desplazó sin tocar este archivo.

  · `routes/siesa.py::debug_stock_bodega`      — endpoint de debug
  · `routes/siesa.py::debug_traza_ref`         — endpoint de debug
  · `traslado_service.py::get_stock_disponible` — el único productivo

Conviene ser exacto con eso: **de los tres precedentes, dos son de debug**. La
política vivía en un solo sitio que decidiera algo, y el Armador era el segundo
—y el que compra—. Decir «ya estaba resuelto en tres lugares» suena a copia
redundante; lo que había era una política casi sin implementar.

  · `bajo_rop = posicion < rop` — un SKU con 100 en bodega y 80 ya vendidos
    pendientes de despachar tiene 20 reales. Con la posición inflada la
    reposición nacional no se disparaba: no se repone lo que ya está vendido.
  · `deficit = max(0, s_objetivo - posicion)` — es el número que **arma el
    contenedor**. Un déficit corto es un contenedor corto, y un contenedor es
    irreversible 120 días (Regla 0). El Armador era el cuarto sitio que
    contesta esta pregunta y el único que compra.

Detector en las dos direcciones: no alcanza con probar que dispara. Un SKU
con `comprometido = 0` tiene que dar exactamente la misma posición, el mismo
ROP y el mismo déficit que antes del arreglo — si no, el arreglo movió
números de operación sana.
"""
import math
import pytest


NIVEL = 0.95


def _z():
    from app.services.kardex_service import _norm_ppf
    return _norm_ppf(NIVEL)


def _demanda(**skus):
    """Payload de `demanda_descensurada` con las llaves que `rop_dual` lee."""
    return {
        ref: {
            'd_avg': d_avg,
            'sigma_d': 0.0,          # sin ruido de demanda: el ROP queda cerrado
            'dias_con_stock': 300,
            'dias_ventana': 360,
            'demanda_neta': d_avg * 300,
            'factor_censura': 1.0,
            'censurado': False,
        }
        for ref, d_avg in skus.items()
    }


@pytest.fixture
def sin_kardex(monkeypatch):
    """Inyecta la demanda sin tocar el kardex — es de otro agente y de otro
    defecto. Acá se prueba la posición, no la descensura."""
    from app.services.kardex_service import KardexService

    def _fijar(demanda):
        monkeypatch.setattr(KardexService, 'demanda_descensurada',
                            lambda *a, **k: demanda)
    return _fijar


def _producto(db, ref, origen):
    from app.models.producto import Producto
    p = Producto(codigo=ref, nombre=ref, codigo_siesa=ref,
                 origen=origen, activo=True)
    db.session.add(p)
    return p


def _stock(db, ref, existencia, comprometido=0, salida_sin_conf=0,
           bodega='NB1'):
    from app.models.stock_siesa import StockSiesa
    db.session.add(StockSiesa(
        bodega=bodega, codigo_siesa=ref, existencia=existencia,
        comprometido=comprometido, salida_sin_conf=salida_sin_conf))


def _fila(resultado, regimen, ref):
    return next(f for f in resultado[regimen]['items'] if f['referencia'] == ref)


# ── Los números, calculados aparte ──────────────────────────────────────────
# Con sigma_d = 0 el ROP nacional se cierra a mano:
#   sigma_LTD = sqrt(LT*0 + d^2*sigma_LT^2) = d * sigma_LT
#   ROP       = d*LT + z*d*sigma_LT
# Con d = 6: ROP = 30 + z*12 ≈ 49.7 → 50.  Un SKU con 100 en bodega y 80
# comprometidos tiene 20: bajo el ROP. Con la posición inflada, 100: encima.

D_AVG = 6.0
EXISTENCIA = 100.0
COMPROMETIDO = 80.0


def _rop_nacional_esperado(d_avg=D_AVG):
    from app.services.armador_service import LT_NACIONAL_DIAS, SIGMA_LT_NACIONAL
    return d_avg * LT_NACIONAL_DIAS + _z() * d_avg * SIGMA_LT_NACIONAL


def _s_objetivo_esperado(d_avg=D_AVG):
    from app.services.armador_service import (
        LT_CHINA_DIAS, SIGMA_LT_CHINA, R_CHINA_DIAS,
        MAX_COBERTURA_RELLENO_DIAS)
    s = d_avg * (LT_CHINA_DIAS + R_CHINA_DIAS) + _z() * d_avg * SIGMA_LT_CHINA
    return min(s, d_avg * MAX_COBERTURA_RELLENO_DIAS)


class TestLoVendidoNoEsStock:
    """Dispara: con comprometido, la posición baja y las dos decisiones cambian."""

    def test_bajo_rop_se_enciende_con_lo_comprometido(self, app, db, sin_kardex):
        _producto(db, 'NAC-COMP', 'NACIONAL')
        _stock(db, 'NAC-COMP', EXISTENCIA, comprometido=COMPROMETIDO)
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-COMP': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-COMP')

        assert fila['rop'] == round(_rop_nacional_esperado()), \
            'el ROP cambió: este test dejó de medir lo que dice medir'
        assert fila['posicion'] == 20, (
            'la posición sigue contando lo ya vendido como disponible: '
            f"{fila['posicion']} en vez de 100 - 80")
        assert fila['bajo_rop'] is True, (
            '20 unidades reales contra un ROP de 50 y el sistema no repone — '
            'esto es el agotado que nadie ve venir')

    def test_el_deficit_del_contenedor_crece_lo_comprometido(
            self, app, db, sin_kardex):
        """El lado caro: `deficit` es lo que arma el contenedor."""
        _producto(db, 'CHI-COMP', 'CHINA')
        _producto(db, 'CHI-LIBRE', 'CHINA')
        _stock(db, 'CHI-COMP', EXISTENCIA, comprometido=COMPROMETIDO)
        _stock(db, 'CHI-LIBRE', EXISTENCIA)
        db.session.commit()
        sin_kardex(_demanda(**{'CHI-COMP': D_AVG, 'CHI-LIBRE': D_AVG}))

        from app.services.armador_service import ArmadorService
        r = ArmadorService.rop_dual(NIVEL)
        comp = _fila(r, 'china', 'CHI-COMP')
        libre = _fila(r, 'china', 'CHI-LIBRE')

        assert comp['deficit'] - libre['deficit'] == COMPROMETIDO, (
            'dos SKU idénticos salvo por 80 unidades ya vendidas piden lo '
            f"mismo: {comp['deficit']} vs {libre['deficit']}. El contenedor "
            'sale corto justo en lo que ya se debe')
        assert comp['deficit'] == round(_s_objetivo_esperado() - 20)

    def test_la_posicion_puede_ser_negativa(self, app, db, sin_kardex):
        """Más comprometido que stock es información real: no se aplasta a 0.

        Aplastarla dejaría el déficit corto exactamente en el sobrevendido —
        el pedido llegaría y seguiría faltando lo que ya se debe.
        """
        _producto(db, 'CHI-NEG', 'CHINA')
        _stock(db, 'CHI-NEG', 100.0, comprometido=150.0)
        db.session.commit()
        sin_kardex(_demanda(**{'CHI-NEG': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'china', 'CHI-NEG')

        assert fila['posicion'] == -50, (
            f"posición {fila['posicion']}: se perdió el sobrevendido")
        assert fila['deficit'] == round(_s_objetivo_esperado() + 50)

    def test_la_salida_sin_confirmar_tambien_esta_comprometida(
            self, app, db, sin_kardex):
        """Misma política que los otros tres sitios del repo:
        `existencia - comprometida - salida_sin_conf`. Una cuarta variante es
        exactamente lo que la Regla 0 prohíbe."""
        _producto(db, 'NAC-SSC', 'NACIONAL')
        _stock(db, 'NAC-SSC', 100.0, comprometido=30.0, salida_sin_conf=25.0)
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-SSC': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-SSC')
        assert fila['posicion'] == 45, (
            f"posición {fila['posicion']}: la salida sin confirmar no se "
            'descontó — el Armador quedó como cuarta política divergente')

    def test_se_suma_el_comprometido_de_todas_las_bodegas(
            self, app, db, sin_kardex):
        """La existencia se suma sobre todas las bodegas; lo comprometido
        también, o el descuento queda parcial."""
        _producto(db, 'NAC-MULTI', 'NACIONAL')
        _stock(db, 'NAC-MULTI', 60.0, comprometido=50.0, bodega='NB1')
        _stock(db, 'NAC-MULTI', 40.0, comprometido=30.0, bodega='NC1')
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-MULTI': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-MULTI')
        assert fila['posicion'] == 20, (
            f"posición {fila['posicion']}: el comprometido de la segunda "
            'bodega no entró')


class TestNoDisparaSobreOperacionSana:
    """La otra mitad del detector.

    Un guard que solo prueba que dispara prueba la mitad. Con
    `comprometido = 0` y `salida_sin_conf = 0` **nada** puede moverse: los
    números tienen que ser los de antes del arreglo, calculados aparte.
    """

    def test_sin_comprometido_la_posicion_es_la_de_siempre(
            self, app, db, sin_kardex):
        _producto(db, 'NAC-SANO', 'NACIONAL')
        _stock(db, 'NAC-SANO', EXISTENCIA)
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-SANO': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-SANO')

        assert fila['stock_actual'] == round(EXISTENCIA)
        assert fila['posicion'] == round(EXISTENCIA)
        assert fila['rop'] == round(_rop_nacional_esperado())
        assert fila['bajo_rop'] is False, \
            '100 unidades libres contra un ROP de 50: no hay nada que reponer'
        assert fila['cobertura_dias'] == round(EXISTENCIA / D_AVG, 1)

    def test_sin_comprometido_el_deficit_china_es_el_de_siempre(
            self, app, db, sin_kardex):
        _producto(db, 'CHI-SANO', 'CHINA')
        _stock(db, 'CHI-SANO', EXISTENCIA)
        db.session.commit()
        sin_kardex(_demanda(**{'CHI-SANO': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'china', 'CHI-SANO')

        assert fila['posicion'] == round(EXISTENCIA)
        assert fila['s_objetivo'] == round(_s_objetivo_esperado())
        assert fila['deficit'] == round(_s_objetivo_esperado() - EXISTENCIA)

    def test_el_transito_sigue_sumando(self, app, db, sin_kardex):
        """El arreglo resta; no puede haber tocado el término que suma."""
        from app.models.importacion import ItemEnTransito
        p = _producto(db, 'CHI-TRA', 'CHINA')
        _stock(db, 'CHI-TRA', EXISTENCIA)
        db.session.flush()
        db.session.add(ItemEnTransito(producto_id=p.id, cantidad=40,
                                      estado='NAVEGANDO'))
        db.session.commit()
        sin_kardex(_demanda(**{'CHI-TRA': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'china', 'CHI-TRA')
        assert fila['en_transito'] == 40
        assert fila['posicion'] == round(EXISTENCIA + 40)

    def test_un_sku_sin_fila_de_stock_no_revienta(self, app, db, sin_kardex):
        """Sin snapshot no hay existencia ni comprometido: posición 0, no None."""
        _producto(db, 'NAC-VACIO', 'NACIONAL')
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-VACIO': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-VACIO')
        assert fila['posicion'] == 0
        assert fila['bajo_rop'] is True


class TestLaPosicionEsAuditable:
    """Canon por métrica: si un número tiene nombre, tiene procedencia.

    Sin ver el comprometido en la fila, el comprador no puede reconstruir por
    qué la posición no es la existencia — y un número que no se puede auditar
    se termina ignorando o creyendo a ciegas.
    """

    def test_la_fila_declara_de_donde_sale_la_posicion(
            self, app, db, sin_kardex):
        _producto(db, 'NAC-AUD', 'NACIONAL')
        _stock(db, 'NAC-AUD', 100.0, comprometido=30.0, salida_sin_conf=25.0)
        db.session.commit()
        sin_kardex(_demanda(**{'NAC-AUD': D_AVG}))

        from app.services.armador_service import ArmadorService
        fila = _fila(ArmadorService.rop_dual(NIVEL), 'nacional', 'NAC-AUD')

        assert fila['comprometido'] == 30
        assert fila['salida_sin_conf'] == 25
        assert (fila['stock_actual'] - fila['comprometido']
                - fila['salida_sin_conf'] == fila['posicion'])
