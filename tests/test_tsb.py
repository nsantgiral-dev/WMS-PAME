"""
Tests del TSB y de su COMPUERTA.

Cisnes negros que previene:
- MASE con el denominador equivocado → la compuerta da permiso con autoridad
- TSB sobre ventas crudas → aprende que un SKU agotado "no se vendía"
- Semanas sin venta omitidas → reintroduce el sesgo que TSB existe para evitar
- Croston confundido con TSB → un SKU que muere se queda congelado en su tasa
"""
import pytest


def _canon():
    import json
    from pathlib import Path
    p = Path(__file__).resolve().parents[1] / 'docs' / 'canones' / 'tsb.json'
    assert p.exists(), 'falta docs/canones/tsb.json'
    return json.loads(p.read_text(encoding='utf-8'))


class TestMaseCanonico:
    """El denominador es el naive de UN PASO in-sample, no la media.

    Una compuerta mal calculada es peor que no tener compuerta: da permiso
    con autoridad.
    """

    TOL = 0.001

    def test_reproduce_el_canon(self):
        from app.services.kardex_service import KardexService
        c = next(x for x in _canon()['casos'] if x['nombre'].startswith('MASE — serie'))
        m = KardexService.mase(c['entrada']['serie'], c['entrada']['pronostico'])
        assert abs(m - c['esperado']['mase']) < self.TOL, f'{m} != {c["esperado"]["mase"]}'

    def test_el_denominador_no_es_la_media(self):
        """Guard directo contra el bug anterior.

        Con la media como denominador el MASE de esta serie sería otro número.
        """
        from app.services.kardex_service import KardexService
        serie = [0, 10, 0, 0, 8, 0, 12, 0]
        m = KardexService.mase(serie, 3.75)
        media = sum(serie) / len(serie)
        mae = sum(abs(v - 3.75) for v in serie) / len(serie)
        mase_falso = mae / (sum(abs(v - media) for v in serie) / len(serie))
        assert abs(m - mase_falso) > 0.05, \
            'el MASE coincide con la versión de la media — denominador equivocado'

    def test_peor_pronostico_da_peor_mase(self):
        from app.services.kardex_service import KardexService
        c = next(x for x in _canon()['casos'] if 'peor pronostico' in x['nombre'])
        m = KardexService.mase(c['entrada']['serie'], c['entrada']['pronostico'])
        assert abs(m - c['esperado']['mase']) < self.TOL
        mejor = KardexService.mase(c['entrada']['serie'], 3.75)
        assert mejor < m, 'alejarse del óptimo debe empeorar el MASE'

    def test_serie_sin_variacion_devuelve_none(self):
        """Denominador cero: None, nunca un 1.0 que abre la compuerta gratis."""
        from app.services.kardex_service import KardexService
        assert KardexService.mase([5, 5, 5], 5.0) is None
        assert KardexService.mase([7], 7.0) is None

    def test_es_invariante_de_escala(self):
        """Propiedad del problema, no de la implementación: duplicar la serie y
        el pronóstico no puede cambiar el error relativo."""
        from app.services.kardex_service import KardexService
        serie = [0, 10, 0, 0, 8, 0, 12, 0]
        a = KardexService.mase(serie, 3.75)
        b = KardexService.mase([v * 2 for v in serie], 7.5)
        assert abs(a - b) < 1e-9


class TestTamizNoCompuerta:
    """MASE es un TAMIZ, no una compuerta — y la respuesta debe decirlo.

    MASE mide error punto a punto; Croston y TSB no pronostican CUÁNDO llega el
    grumo, estiman una TASA. Un pronóstico de 1.6/sem contra 0,0,0,40,0,0 se ve
    pésimo en MAE y es correcto para inventario. Castigarlo por no adivinar el
    timing es medirlo por algo que nunca prometió.
    """

    def test_se_declara_como_tamiz_y_no_como_compuerta(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert 'tamiz_mase' in r, 'la clave debe decir lo que es'
        assert r['tamiz_mase']['es_compuerta'] is False
        assert 'tasa' in r['tamiz_mase']['_por_que_no']

    def test_declara_cual_es_la_compuerta_real(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert 'SIMULACION DE INVENTARIO' in r['tamiz_mase']['compuerta_real']

    def test_sigma_d_no_cambia_por_el_tamiz(self, app, db):
        """Pasar el tamiz NO da permiso para gobernar el colchón."""
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert 'interino' in r['sigma_d_del_rop']
        assert 'SIMULACION DE INVENTARIO' in r['sigma_d_del_rop']

    def test_sin_datos_no_supera_el_tamiz(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert r['tamiz_mase']['supera_tamiz'] is False

    def test_declara_que_la_demanda_va_descensurada(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert 'DESCENSURADA' in r['demanda']

    def test_la_metrica_esta_declarada_en_la_respuesta(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert 'naive un paso' in r['tamiz_mase']['metrica']


class TestMuestraConPoder:
    """Con n=10 el filtro no filtra: decora."""

    def test_n_minimo_no_es_diez(self):
        from app.services.kardex_service import TSB_N_MINIMO
        assert TSB_N_MINIMO >= 100, (
            'con n=10 una moneda al aire supera el 60% en el 37.7% de los intentos')

    def test_wilson_reproduce_una_proporcion_conocida(self):
        from app.services.kardex_service import _wilson
        lo, hi = _wilson(60, 100)
        assert abs(lo - 0.5020) < 0.001 and abs(hi - 0.6906) < 0.001

    def test_una_moneda_al_aire_no_descarta_el_azar(self):
        """6 de 10 no puede pasar: el IC95 incluye el 50%."""
        from app.services.kardex_service import _wilson
        lo, _ = _wilson(6, 10)
        assert lo < 0.5, 'con n=10 el azar sigue dentro del intervalo'

    def test_la_misma_proporcion_con_muestra_grande_si_lo_descarta(self):
        from app.services.kardex_service import _wilson
        lo, _ = _wilson(600, 1000)
        assert lo > 0.5, 'con n=1000 el 60% sí es distinguible del azar'

    def test_wilson_no_crashea_con_n_cero(self):
        from app.services.kardex_service import _wilson
        assert _wilson(0, 0) == (0.0, 0.0)


class TestPonderacionEconomica:
    """Ganar en la cola y perder donde está la plata no debe pasar."""

    def test_la_respuesta_expone_ambos_porcentajes(self, app, db):
        from app.services.kardex_service import KardexService
        t = KardexService.pronostico_tsb()['tamiz_mase']
        assert 'porcentaje_tsb_gana' in t
        assert 'porcentaje_ponderado_por_valor' in t

    def test_el_criterio_exige_los_dos(self, app, db):
        from app.services.kardex_service import KardexService
        t = KardexService.pronostico_tsb()['tamiz_mase']
        assert 'AMBOS' in t['nota_ponderacion']


class TestSerieSemanalDescensurada:
    """Rejilla regular y completa — las semanas en cero cuentan."""

    def test_sin_datos_no_crashea(self, app, db):
        from app.services.kardex_service import KardexService
        assert KardexService.serie_semanal_descensurada() == {}

    def test_una_semana_a_medio_stock_se_proyecta_a_siete_dias(self, app, db):
        from app.services.kardex_service import (
            KardexService, KardexMovimiento, StockDiario)
        from datetime import date, timedelta
        lunes = date.today() - timedelta(days=date.today().weekday() + 28)
        # Vendió 30 unidades y solo tuvo stock 3 de los 7 días
        db.session.add(KardexMovimiento(
            referencia='SEMI', bodega='NB1', fecha=lunes,
            concepto=501, naturaleza=2, cantidad=30))
        for i in range(3):
            db.session.add(StockDiario(referencia='SEMI', bodega='NB1',
                                       fecha=lunes + timedelta(days=i),
                                       stock_cierre=5, tuvo_stock=True))
        db.session.commit()

        s = KardexService.serie_semanal_descensurada()
        valor = dict(s['SEMI'])[lunes]
        assert abs(valor - 70.0) < 0.01, f'30 uds en 3 de 7 días = 70, no {valor}'


class TestPanelTSBMuestraElTamizYLaCompuerta:
    """El panel debe distinguir lo mismo que la respuesta: tamiz != compuerta.

    Un win rate suelto, sin muestra ni intervalo ni consecuencia, es la cifra
    bonita que hace pasar por juez a algo que solo es filtro.
    """

    def _bloque(self):
        import os
        ruta = os.path.join(os.path.dirname(__file__), '..',
                            'app', 'static', 'pwa', 'compras_ia.js')
        src = open(ruta).read()
        i = src.index('async function _cargarTSB')
        return src[i:i + 4500]

    def test_dice_que_no_es_la_compuerta(self):
        b = self._bloque()
        assert 'Tamiz MASE' in b and 'no es la compuerta' in b

    def test_muestra_muestra_intervalo_y_ponderacion(self):
        """Sin n, IC e importancia economica el porcentaje no significa nada."""
        b = self._bloque()
        assert 'n_minimo' in b
        assert 'ic95_victorias' in b
        assert 'porcentaje_ponderado_por_valor' in b

    def test_nombra_la_compuerta_real_y_el_estado_de_sigma_d(self):
        b = self._bloque()
        assert 'compuerta_real' in b
        assert 'sigma_d_del_rop' in b

    def test_muestra_mase_de_ambos_para_poder_comparar(self):
        b = self._bloque()
        assert 'mase_tsb' in b and 'mase_mm8' in b


class TestCanonDeclaraLaDistincion:
    """El canon debe distinguir tamiz de compuerta, o el error vuelve."""

    def test_el_canon_declara_que_el_tamiz_no_es_compuerta(self):
        c = _canon()['compuerta']
        assert 'NO es la compuerta' in c['_ADVERTENCIA']
        assert 'TASA' in ' '.join(c['tamiz_mase']['por_que_no_es_compuerta']).upper()

    def test_documenta_por_que_n_no_es_diez(self):
        c = _canon()['compuerta']['tamiz_mase']
        assert c['n_minimo'] >= 100
        assert '37.7' in ' '.join(c['_por_que_no_diez'])

    def test_la_compuerta_real_es_la_simulacion_de_inventario(self):
        c = _canon()['compuerta']['compuerta_real']
        assert 'servicio' in c['pregunta'] and 'inventario' in c['pregunta']
        assert 'RMSE' in c['consecuencia_si_pasa']

    def test_el_criterio_del_canon_coincide_con_el_codigo(self):
        from app.services.kardex_service import TSB_N_MINIMO
        assert _canon()['compuerta']['tamiz_mase']['n_minimo'] == TSB_N_MINIMO
