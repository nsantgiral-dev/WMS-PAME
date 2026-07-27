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


class TestPoliticaDatoAusente:
    """Una sola política, en una sola función.

    El mismo concepto implementado dos veces divergió en tres horas: S-B caía
    a calendario (conservador) y la descensura a días-con-venta (25x arriba).
    """

    def test_sin_stock_diario_cae_a_calendario_y_marca(self):
        from app.services.kardex_service import dias_expuestos
        n, censurado = dias_expuestos(0, 360)
        assert (n, censurado) == (360, True)

    def test_con_stock_diario_usa_ese_denominador(self):
        from app.services.kardex_service import dias_expuestos
        n, censurado = dias_expuestos(120, 360)
        assert (n, censurado) == (120, False)

    def test_nunca_excede_el_calendario(self):
        """Más días con stock que días de ventana es dato sucio."""
        from app.services.kardex_service import dias_expuestos
        n, _ = dias_expuestos(500, 360)
        assert n == 360

    def test_sb_y_descensura_usan_la_misma_funcion(self):
        """Guard anti-divergencia: si alguien reimplementa el fallback, falla."""
        import inspect
        from app.services import kardex_service as ks
        src = inspect.getsource(ks)
        # Ambos consumidores deben llamar a la política, no inventar la suya
        assert src.count('dias_expuestos(') >= 3, \
            'S-B y la descensura deben consumir dias_expuestos(), no reimplementarlo'


class TestBandaSensibilidad:
    """Protege al comité de una pelea de parámetros.

    Cu y Co no son hechos: son políticas. La banda muestra cuánta plata depende
    de esa política, sin que nadie tenga que discutir si el capital es 30% o 15%.
    """

    def test_cada_item_trae_su_banda(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [1000, 1100], 'costo_unitario': 50}])
        s = r['items'][0]['sensibilidad_cr']
        assert s['cr_menos_10']['q'] < s['cr_base']['q'] < s['cr_mas_10']['q'], \
            'un ratio crítico mayor debe pedir más'
        assert s['exposicion_pesos'] > 0

    def test_agregado_declara_la_exposicion_total(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.newsvendor(
            [{'referencia': 'A', 'ventas_pasadas': [1000, 1100], 'costo_unitario': 50},
             {'referencia': 'B', 'ventas_pasadas': [200, 260], 'costo_unitario': 900}])
        b = r['banda_sensibilidad']
        assert b['inversion_cr_menos_10'] < b['inversion_base'] < b['inversion_cr_mas_10']
        assert b['exposicion_pesos'] == b['inversion_cr_mas_10'] - b['inversion_cr_menos_10']
        assert 'políticas' in b['nota']


class TestCanonClasificacionSB:
    """S-B elige QUÉ MODELO se aplica: un error aquí es coherente y falso."""

    def _canon(self):
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / 'docs' / 'canones' / 'clasificacion_sb.json'
        assert p.exists(), 'falta docs/canones/clasificacion_sb.json'
        return json.loads(p.read_text(encoding='utf-8'))

    def test_los_cortes_del_canon_son_los_del_codigo(self):
        """Cambiar un corte reclasifica el catálogo entero. El canon lo vigila."""
        import re
        from pathlib import Path
        c = self._canon()['parametros']
        src = (Path(__file__).resolve().parents[1]
               / 'app/services/kardex_service.py').read_text(encoding='utf-8')
        assert f"ADI_CORTE = {c['adi_corte']}" in src
        assert f"CV2_CORTE = {c['cv2_corte']}" in src

    def test_documenta_el_truncamiento_del_dia_de_agotamiento(self):
        """La limitación de segundo orden queda anotada, no redescubierta."""
        lim = self._canon()['limitaciones_conocidas']
        assert lim['_severidad'].startswith('BAJA')
        texto = ' '.join(lim['truncamiento_del_dia_de_agotamiento'])
        assert 'truncado' in texto


class TestGuardDelReset:
    """Lo único pendiente que puede destruir algo irrecuperable.

    Si una limpieza se lleva serie_vigia HISTORICO, alarma_vigia o los canones,
    se vuelve a quedar ciego seis meses y se pierde la alarma de Florencia.
    """

    def _mod(self):
        import importlib.util
        from pathlib import Path
        ruta = Path(__file__).resolve().parents[1] / 'scripts' / 'reset_transaccional.py'
        assert ruta.exists(), 'falta scripts/reset_transaccional.py'
        spec = importlib.util.spec_from_file_location('reset_transaccional', ruta)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_las_analiticas_nunca_estan_en_la_lista_de_borrado(self):
        m = self._mod()
        for t in m.PROTEGIDAS_ANALITICAS:
            assert t not in m.OPERATIVAS, f'{t} NO puede borrarse: memoria analítica'

    def test_las_maestras_nunca_estan_en_la_lista_de_borrado(self):
        m = self._mod()
        for t in m.PROTEGIDAS_MAESTRAS:
            assert t not in m.OPERATIVAS, f'{t} NO puede borrarse: tabla maestra'

    def test_protege_las_cinco_criticas_por_nombre(self):
        """Guard explícito: si alguien renombra o quita una, esto falla."""
        m = self._mod()
        for t in ('serie_vigia', 'alarma_vigia', 'kardex_movimientos',
                  'stock_diario', 'juicios_temporada'):
            assert t in m.PROTEGIDAS_ANALITICAS, f'{t} debe estar protegida'

    def test_el_guard_aborta_si_contaminan_la_lista(self):
        m = self._mod()
        original = list(m.OPERATIVAS)
        try:
            m.OPERATIVAS.append('serie_vigia')
            assert m._guard() is False, 'el guard debe abortar con una protegida en la lista'
        finally:
            m.OPERATIVAS[:] = original
        assert m._guard() is True

    def test_cada_protegida_declara_por_que(self):
        m = self._mod()
        for t, razon in m.PROTEGIDAS_ANALITICAS.items():
            assert razon and len(razon) > 15, f'{t} sin explicar qué se pierde'

    def test_las_tablas_operativas_existen_de_verdad(self, app, db):
        """Una tabla mal escrita haría que el reset la salte en silencio."""
        m = self._mod()
        from app.extensions import db as _db
        reales = set(_db.metadata.tables)
        fantasmas = [t for t in m.OPERATIVAS if t not in reales]
        assert not fantasmas, f'tablas inexistentes en la lista: {fantasmas}'


class TestSelloDeHoraDelActa:
    """Un acta firmada a las 3 p.m. en Neiva no puede decir 20:00.

    Arreglo de PRESENTACIÓN: entra antes del congelamiento del 4 de agosto.
    La migración naive→aware del backend toca cientos de sitios y va después.
    """

    def _js(self):
        import os
        ruta = os.path.join(os.path.dirname(__file__), '..',
                            'app', 'static', 'pwa', 'temporada.js')
        return open(ruta, encoding='utf-8').read()

    def test_el_acta_no_usa_hora_utc(self):
        """Se ignoran los comentarios: un guard que se dispara con su propia
        documentación es ruido, no señal — es la segunda vez que pasa."""
        codigo = [l.split('//')[0] for l in self._js().splitlines()]
        malas = [l.strip() for l in codigo if 'toISOString' in l]
        assert not malas, \
            f'toISOString() da UTC — el acta quedaría corrida 5 horas: {malas}'

    def test_renderiza_en_bogota(self):
        assert "America/Bogota" in self._js()

    def test_la_zona_es_visible_en_el_documento(self):
        """Una hora sin zona declarada es una hora que hay que adivinar."""
        src = self._js()
        assert 'hora de Colombia' in src and 'UTC' in src
