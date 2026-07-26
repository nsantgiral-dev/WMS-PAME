"""
Tests del llamador del newsvendor.

Cisnes negros que previene:
- Demanda de temporada leída CRUDA → se pide de menos justo de lo que se agotó
- SKU con costo fantasma entra al Cu/Co → Q* que es una opinión con decimales
- SKU en lista negra se recompra por la puerta de temporada
- Cobertura del modelo invisible → el comité descubre en la sala que el modelo
  decidía el 40% del pedido
"""
import pytest
from datetime import date


class TestVentanaTemporada:
    """Dic, ene y feb de un mismo ciclo son la MISMA temporada."""

    def test_diciembre_y_enero_siguiente_son_la_misma(self):
        from app.services.temporada_service import temporada_de
        assert temporada_de(date(2025, 12, 15)) == '2025-26'
        assert temporada_de(date(2026, 1, 20)) == '2025-26'
        assert temporada_de(date(2026, 2, 10)) == '2025-26'

    def test_fuera_de_ventana_no_es_temporada(self):
        from app.services.temporada_service import temporada_de
        assert temporada_de(date(2026, 6, 15)) is None
        assert temporada_de(date(2026, 3, 1)) is None

    def test_temporadas_consecutivas_no_se_mezclan(self):
        from app.services.temporada_service import temporada_de
        assert temporada_de(date(2024, 12, 5)) == '2024-25'
        assert temporada_de(date(2025, 12, 5)) == '2025-26'


class TestDescensuraTemporada:
    """Lo que se agotó en enero no puede leerse como 'no se vendía'."""

    def test_sin_quiebres_no_cambia_nada(self):
        from app.services.temporada_service import TemporadaService
        assert TemporadaService._descensurar(900, 90, 90) == 900

    def test_agotado_la_mitad_duplica_la_demanda(self):
        """450 unidades en 45 de 90 días = 900 de demanda real."""
        from app.services.temporada_service import TemporadaService
        assert TemporadaService._descensurar(450, 45, 90) == 900

    def test_nunca_encoge_la_demanda(self):
        """Más días con stock que la ventana es dato sucio: no debe reducir."""
        from app.services.temporada_service import TemporadaService
        assert TemporadaService._descensurar(900, 120, 90) == 900

    def test_sin_dias_de_stock_devuelve_lo_observado(self):
        from app.services.temporada_service import TemporadaService
        assert TemporadaService._descensurar(500, 0, 90) == 500


class TestCuCoPorSku:
    """El ratio crítico depende del margen de ESE producto, no de un promedio."""

    def test_cu_co_por_item_manda_sobre_el_global(self, app, db):
        from app.services.kardex_service import KardexService
        # Cu alto y Co bajo → ratio alto → Q* por encima de la media
        caro = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [100, 120], 'costo_unitario': 10,
              'cu': 90, 'co': 10}])
        barato = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [100, 120], 'costo_unitario': 10,
              'cu': 10, 'co': 90}])
        assert caro['items'][0]['q_optimo'] > barato['items'][0]['q_optimo'], \
            'mayor Cu/Co debe pedir más'
        assert caro['items'][0]['ratio_critico'] > 0.8
        assert barato['items'][0]['ratio_critico'] < 0.2

    def test_sin_cu_co_usa_el_global(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [100, 120], 'costo_unitario': 10}],
            margen_pct=0.40, costo_exceso_pct=0.60)
        assert abs(r['items'][0]['ratio_critico'] - 0.40) < 0.01

    def test_una_sola_temporada_infla_la_incertidumbre(self, app, db):
        """Con 2025-26 sola, el sigma se infla ×1.5 y queda marcado."""
        from app.services.kardex_service import KardexService
        r = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [1000], 'costo_unitario': 10}])
        item = r['items'][0]
        assert item['advertencia_1_temporada'] is True
        assert item['sigma'] > 0, 'una temporada no puede dar sigma cero'


class TestCoberturaDelModelo:
    """El comité tiene que saber ANTES qué fracción de la decisión cubre."""

    def test_sin_kardex_no_inventa_pedido(self, app, db):
        from app.services.temporada_service import TemporadaService
        r = TemporadaService.preparar_pedido_temporada()
        assert 'error' in r
        assert r['cobertura']['skus_temporada'] == 0

    def test_endpoints_registrados(self, app):
        rutas = [str(x) for x in app.url_map.iter_rules()]
        assert any('/temporada/pedido' in r for r in rutas)


class TestDistribucionConHistoriaCorta:
    """Una observación NO es una distribución.

    Con n=1 la empírica colapsaría a la observación y el ratio crítico dejaría
    de tener efecto: el modelo diría "pide lo que vendiste", que es la
    heurística que vino a reemplazar.
    """

    def test_una_temporada_usa_normal_inflada_no_empirica(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [1000], 'costo_unitario': 10}])
        it = r['items'][0]
        assert 'Normal inflada' in it['distribucion']
        assert it['incertidumbre'] == 'ALTA'
        assert abs(it['sigma'] - 1000 * 0.30 * 1.5) < 1

    def test_el_ratio_critico_sigue_moviendo_el_q_con_una_temporada(self, app, db):
        """Si sigma colapsara a 0, Cu/Co dejaría de importar. Debe importar."""
        from app.services.kardex_service import KardexService
        alto = KardexService.newsvendor([{'referencia': 'A', 'ventas_pasadas': [1000],
                                          'costo_unitario': 10, 'cu': 90, 'co': 10}])
        bajo = KardexService.newsvendor([{'referencia': 'A', 'ventas_pasadas': [1000],
                                          'costo_unitario': 10, 'cu': 10, 'co': 90}])
        assert alto['items'][0]['q_optimo'] > bajo['items'][0]['q_optimo']

    def test_tres_temporadas_pasan_a_empirica(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [900, 1000, 1100], 'costo_unitario': 10}])
        it = r['items'][0]
        assert it['distribucion'].startswith('Empirica')
        assert it['incertidumbre'] != 'ALTA'


class TestDescensuraNoSobreestima:
    """El fallback sin StockDiario no puede inventar demanda.

    Usar días-con-venta como denominador daría demanda POR DÍA CON VENTA: un
    SKU que vende 40 cada 25 días saldría a 40/día en vez de 1.6 — 25x arriba,
    y multiplicado después por el z del colchón.
    """

    def test_sin_stock_diario_cae_a_calendario_y_se_marca(self, app, db):
        from app.services.kardex_service import (
            KardexService, KardexMovimiento)
        from datetime import date, timedelta
        hoy = date.today()
        # 3 ventas de 40 unidades, sin StockDiario alguno
        for i in range(3):
            db.session.add(KardexMovimiento(
                referencia='LUMPY', bodega='NB1', fecha=hoy - timedelta(days=30 * (i + 1)),
                concepto=501, naturaleza=2, cantidad=40))
        db.session.commit()

        r = KardexService.demanda_descensurada(ventana_meses=12, nivel='red')
        fila = r.get('LUMPY')
        assert fila is not None
        assert fila['censurado'] is True, 'sin StockDiario debe marcarse censurado'
        # 120 unidades sobre ~360 días de calendario, NO sobre 3 días con venta
        assert fila['d_avg'] < 1.0, f"d_avg={fila['d_avg']} — usó días con venta como denominador"
        assert fila['dias_con_stock'] == fila['dias_ventana']
