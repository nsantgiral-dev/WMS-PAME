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


class TestCompuertaTSB:
    """Binaria y estricta: mayoría simple no basta para gobernar el colchón."""

    def test_el_canon_declara_el_criterio_y_la_consecuencia(self):
        c = _canon()['compuerta']
        assert '10' in c['criterio'] and '60' in c['criterio']
        assert 'sigma_d' in c['consecuencia_si_pasa']
        assert 'interino' in c['consecuencia_si_no_pasa']

    def test_sin_datos_la_compuerta_no_aprueba(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert r['backtest']['aprobado'] is False
        assert 'NO APROBADO' in r['backtest']['consecuencia']

    def test_declara_que_la_demanda_va_descensurada(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert 'DESCENSURADA' in r['demanda']

    def test_la_metrica_esta_declarada_en_la_respuesta(self, app, db):
        """Quien lea el backtest debe poder ver contra qué se comparó."""
        from app.services.kardex_service import KardexService
        r = KardexService.pronostico_tsb()
        assert 'naive un paso' in r['backtest']['metrica']


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


class TestPanelTSBMuestraLaCompuerta:
    """El win rate sin la compuerta es una cifra bonita sin consecuencia."""

    def _js(self):
        import os
        ruta = os.path.join(os.path.dirname(__file__), '..',
                            'app', 'static', 'pwa', 'compras_ia.js')
        return open(ruta).read()

    def test_muestra_aprobada_o_no(self):
        src = self._js()
        i = src.index('async function _cargarTSB')
        bloque = src[i:i + 3500]
        assert 'Compuerta de credibilidad' in bloque
        assert 'b.aprobado' in bloque

    def test_muestra_la_consecuencia_no_solo_el_porcentaje(self):
        """Que diga qué implica pasar o no: alimentar sigma_d o no hacerlo."""
        src = self._js()
        i = src.index('async function _cargarTSB')
        bloque = src[i:i + 3500]
        assert 'consecuencia' in bloque and 'criterio' in bloque

    def test_muestra_mase_de_ambos_para_poder_comparar(self):
        src = self._js()
        i = src.index('async function _cargarTSB')
        bloque = src[i:i + 3500]
        assert 'mase_tsb' in bloque and 'mase_mm8' in bloque
