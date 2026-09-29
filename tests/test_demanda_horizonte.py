"""
Compras — la demanda que entiende la temporada (tanda B, 2026-09-27).

La clase: **el punto de pedido pedía con el promedio de 12 meses en un
negocio de temporada escolar, y con la venta de proyecto adentro.** Un SKU
que vende 10/día nueve meses y 30/día en el pico salía a 15/día todo el año:
de más en temporada baja, de menos en el pico. Una licitación de 20.000
cuadernos entraba como un día de venta y se reponía todo el año.

Ahora (`kardex_service`):
  · `demanda_para_horizonte` — la venta de los próximos días que el pedido
    cubre: la misma ventana del año pasado × la tendencia (acotada); sin un
    año con qué comparar, el promedio, dicho;
  · `bodegas_de_proyecto` — NS2 y BC99 fuera de la venta repetible, en
    `serie_demanda` (la única que lee la venta), con `venta_proyecto` aparte;
  · `tope_atipicos` — un día fuera de toda proporción se cuenta hasta un tope.

Los mundos se arman con la fuente de producción (`demanda_dia_siesa` +
`demanda_dia_cubierto`, la venta diaria que Siesa suma) y los servicios
reales; las respuestas esperadas se calculan a mano.

Trinquete AST: fuera de `kardex_service`, el promedio de 12 meses (`d_avg`,
`sigma_d` de `demanda_descensurada`) solo se lee para publicarlo o filtrar —
nunca para pedir. Con meta-tests y piso.
"""
import ast
import math
import pathlib
from datetime import timedelta

import pytest

from app.utils.fecha import dia_operativo

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _hoy():
    return dia_operativo()


def _cubrir(db, dias):
    """Los días `1..dias` hacia atrás quedan LEÍDOS de Siesa (un día cubierto
    sin fila es un cero verdadero)."""
    from app.models.demanda_siesa import DemandaDiaCubierto
    for d in range(1, dias + 1):
        db.session.add(DemandaDiaCubierto(fecha=_hoy() - timedelta(days=d), consulta='x'))


def _vende(db, ref, por_dia_atras, bod='NB1'):
    """`por_dia_atras`: {días atrás: unidades}."""
    from app.models.demanda_siesa import DemandaDiaSiesa
    for d, q in por_dia_atras.items():
        if q:
            db.session.add(DemandaDiaSiesa(fecha=_hoy() - timedelta(days=d), bodega=bod,
                                           referencia=ref, vendido=q, devuelto=0))


def _serie(fn, desde=1, hasta=430):
    return {d: fn(d) for d in range(desde, hasta + 1)}


# Hoy = H, fin = H − 1 (hoy no está leído). Ventana del año pasado para un
# horizonte de ≤ 28 días: [H − 364, H − 337] = días atrás 337..364.
# Tendencia: últimas 8 semanas = días 1..56; las mismas del año pasado =
# días 365..420.
EN_VENTANA_LY = range(337, 365)
RECIENTE = range(1, 57)
RECIENTE_LY = range(365, 421)


def _estacional(d):
    if d in EN_VENTANA_LY:
        return 30
    if d in RECIENTE:
        return 11
    return 10


@pytest.fixture
def mundo(app, db, monkeypatch):
    for v in ('DEMANDA_BODEGAS_PROYECTO', 'DEMANDA_TENDENCIA_PISO', 'DEMANDA_TENDENCIA_TECHO',
              'ROP_CICLO_NACIONAL_DIAS'):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv('ROP_LT_NACIONAL_DIAS', '10')
    monkeypatch.setenv('ROP_SIGMA_LT_NACIONAL', '5')
    _cubrir(db, 430)
    _vende(db, 'EST', _serie(_estacional))
    # Racha: este año vende el triple → la tendencia se acota al techo (1,5).
    _vende(db, 'RACHA', _serie(lambda d: 30 if d in RECIENTE else 10))
    # Poca venta: 1 u cada 30 días → ~12 al año: la temporada no se mide.
    _vende(db, 'POCO', _serie(lambda d: 1 if d % 30 == 0 else 0))
    # Dormido: solo vende en la época que viene (el año pasado, 20/día).
    _vende(db, 'DORMIDO', _serie(lambda d: 20 if d in EN_VENTANA_LY else 0))
    # Dejado: vendía todo el año pasado, incluidas las semanas previas; hace
    # 100 días que no vende → sí dejó de venderse.
    _vende(db, 'DEJADO', _serie(lambda d: 10 if d > 100 else 0))
    # Proyecto: 10/día en NB1 y una licitación de 20.000 desde NS2.
    _vende(db, 'LICIT', _serie(lambda d: 10))
    _vende(db, 'LICIT', {200: 20000}, bod='NS2')
    # Atípico en una bodega normal: un día de 5.000.
    _vende(db, 'PICO', _serie(lambda d: 10))
    from app.models.demanda_siesa import DemandaDiaSiesa
    DemandaDiaSiesa.query.filter_by(referencia='PICO').filter(
        DemandaDiaSiesa.fecha == _hoy() - timedelta(days=150)).update({'vendido': 5000})
    db.session.commit()
    from app.services.kardex_service import demanda_para_horizonte
    return demanda_para_horizonte(17)


class TestElMismoPeriodoDelAnoPasado:

    def test_estacional_pide_la_epoca_que_viene_por_la_tendencia(self, mundo):
        """840 u en las 4 semanas que vienen el año pasado (30/día) × 616/560
        de tendencia (1,10) = 33/día. El promedio de 12 meses daba ~11."""
        h = mundo['EST']
        assert h['metodo'] == 'MISMO_PERIODO_ANO_ANTERIOR'
        assert h['ano_anterior']['vendido'] == 840
        assert h['ventana_dias'] == 28, 'nunca menos de 4 semanas'
        assert h['tendencia']['medida'] == pytest.approx(616 / 560, abs=1e-4)
        assert h['d_dia'] == pytest.approx(30 * 616 / 560, abs=1e-4)
        assert h['ano_anterior']['desde'] == (_hoy() - timedelta(days=364)).isoformat()

    def test_el_porque_en_palabras_del_comprador(self, mundo):
        t = mundo['EST']['texto']
        assert 'El año pasado, del ' in t and 'vendió 840' in t
        assert 'Este año va 10 % arriba (últimas 8 semanas: 616 contra 560 el año pasado)' in t
        assert 'Para las próximas 4 semanas se cuentan 33 al día' in t

    def test_sigma_sobre_residuales_no_sobre_la_serie_cruda(self, mundo, app):
        """Los tres niveles (10, 11, 30) caen en bloques de 4 semanas
        distintos: sin variabilidad DENTRO de cada bloque, σ = 0. La σ cruda
        del año los cuenta como ruido."""
        from app.services.kardex_service import KardexService
        base = KardexService.demanda_descensurada(12, 'red')['EST']
        assert mundo['EST']['sigma_dia'] == pytest.approx(0.0, abs=1e-6)
        assert base['sigma_d'] > 3

    def test_la_racha_se_acota_y_se_dice(self, mundo):
        h = mundo['RACHA']
        assert h['tendencia']['medida'] == pytest.approx(3.0)
        assert h['tendencia']['factor'] == 1.5 and h['tendencia']['acotada'] is True
        assert h['d_dia'] == pytest.approx(15.0)
        assert 'Este año va 200 % arriba' in h['texto']
        assert 'Se cuenta como mucho 50 % arriba: el tope declarado' in h['texto']

    def test_el_techo_se_configura(self, app, db, mundo, monkeypatch):
        from app.services.kardex_service import demanda_para_horizonte
        monkeypatch.setenv('DEMANDA_TENDENCIA_TECHO', '2')
        h = demanda_para_horizonte(17)['RACHA']
        assert h['tendencia']['factor'] == 2.0 and h['tendencia']['limites_fuente'] == 'CONFIGURADO'
        monkeypatch.setenv('DEMANDA_TENDENCIA_TECHO', 'mucho')
        from app.services.kardex_service import limites_tendencia
        lim = limites_tendencia()
        assert lim['techo'] == 1.5 and lim['problemas'], 'ilegible → default, dicho'


class TestCaeAlPromedioYLoDice:

    def test_poca_venta(self, mundo):
        h = mundo['POCO']
        assert h['metodo'] == 'PROMEDIO' and h['motivo'] == 'POCA_VENTA'
        assert h['texto'].startswith('Vende poco (')

    def test_sin_un_ano_de_ventas_usa_las_ultimas_13_semanas(self, app, db, monkeypatch):
        """P1-E: sin un año para comparar, las últimas 13 semanas (no el
        promedio de lo observado, que en septiembre trae el pico de enero)."""
        monkeypatch.delenv('DEMANDA_BODEGAS_PROYECTO', raising=False)
        _cubrir(db, 300)
        _vende(db, 'EST', _serie(_estacional, 1, 300))
        db.session.commit()
        from app.services.kardex_service import demanda_para_horizonte
        h = demanda_para_horizonte(17)['EST']
        assert h['metodo'] == 'ULTIMAS_SEMANAS' and h['motivo'] == 'SIN_ANO_ANTERIOR'
        assert h['d_dia'] == pytest.approx((56 * 11 + 35 * 10) / 91, abs=1e-4)
        assert 'Todavía no hay un año de ventas' in h['texto'] and 'hay 300 días' in h['texto']
        assert 'últimas 13 semanas' in h['texto']

    def test_producto_nuevo_no_se_divide_por_los_dias_que_no_existia(self, app, db):
        """P1-E: vende 10/día hace 120 días. El promedio de 12 meses daba
        3,34/día (dividía por 360 días en que el producto no existía)."""
        _cubrir(db, 430)
        _vende(db, 'NUEVO', _serie(lambda d: 10, 1, 120))
        db.session.commit()
        from app.services.kardex_service import KardexService, demanda_para_horizonte
        h = demanda_para_horizonte(17)['NUEVO']
        assert KardexService.demanda_descensurada(12, 'red')['NUEVO']['d_avg'] < 4
        assert h['metodo'] == 'ULTIMAS_SEMANAS' and h['motivo'] == 'PRODUCTO_NUEVO'
        assert h['d_dia'] == pytest.approx(10.0)
        assert h['texto'].startswith('Se vende desde el ')

    def test_producto_nuevo_de_pocas_ventas_no_dice_trece_semanas(self, app, db):
        """Existe hace 42 días y vendió 6 veces: no tiene 13 semanas, así que
        no es «vende de a ratos en las últimas 13 semanas»; se mide sobre los
        días que existe."""
        _cubrir(db, 430)
        _vende(db, 'NUEVO3', _serie(lambda d: 10 if d % 7 == 0 else 0, 1, 42))
        db.session.commit()
        from app.services.kardex_service import demanda_para_horizonte
        h = demanda_para_horizonte(17)['NUEVO3']
        assert h['motivo'] == 'PRODUCTO_NUEVO'
        assert 'de a ratos' not in h['texto'] and h['ventana_dias'] < 91

    def test_producto_nuevo_agotado_se_mide_con_los_dias_que_tuvo(self, app, db):
        """P1-E con censura: 10/día hace 91 días, con una racha de 10 días en
        cero en medio: la tasa es la de los días con venta."""
        _cubrir(db, 430)
        _vende(db, 'NUEVO2', _serie(lambda d: 0 if 40 <= d < 50 else 10, 1, 91))
        db.session.commit()
        from app.services.kardex_service import demanda_para_horizonte
        h = demanda_para_horizonte(17)['NUEVO2']
        assert h['d_dia'] == pytest.approx(10.0)
        assert 'No se cuentan 10 días en que parece haberse agotado' in h['texto']


class TestLaCensuraInferida:
    """Sin `StockDiario` (producción): qué días fueron agotados y cuáles no
    (validación del 2026-09-27, P1-A/P1-B)."""

    @pytest.fixture
    def calc(self, app, db, monkeypatch):
        for v in ('DEMANDA_BODEGAS_PROYECTO', 'DEMANDA_TENDENCIA_PISO', 'DEMANDA_TENDENCIA_TECHO'):
            monkeypatch.delenv(v, raising=False)
        _cubrir(db, 430)

        def _c():
            db.session.commit()
            from app.services.kardex_service import demanda_para_horizonte
            return demanda_para_horizonte(17)
        return _c

    def test_una_racha_larga_es_fuera_de_temporada_no_agotado(self, db, calc):
        """Vende solo en dos temporadas de 4 semanas; entre ellas 112 días en
        cero: eso no es un agotado (> 13 semanas) y la demanda de hoy es 0."""
        _vende(db, 'SOLO_TEMP', _serie(lambda d: 20 if (250 <= d <= 277 or 390 <= d <= 417) else 0))
        h = calc()['SOLO_TEMP']
        assert h['censura']['dias'] == 0 and h['d_dia'] == 0

    def test_una_caida_abrupta_se_cuenta_como_agotado(self, db, calc):
        _vende(db, 'CAE', _serie(lambda d: 1 if 344 <= d <= 350 else 10))
        h = calc()['CAE']
        assert any(e['tipo'] == 'CAIDA' for e in h['censura']['eventos'])
        assert h['d_dia'] == pytest.approx(10.0)
        assert 'parece haberse agotado ~7 de 28 días' in h['texto']

    def test_el_borde_de_una_temporada_fuerte_no_es_una_caida(self, db, calc):
        """80/día las 4 semanas del pico y 8/día el resto (10×): la semana
        después del pico cae contra el pico, pero no contra las que siguen. No
        es un agotado, y contarla como tal subiría la demanda (validación del
        2026-09-29)."""
        _vende(db, 'BORDE', _serie(lambda d: 80 if d in EN_VENTANA_LY else 8))
        h = calc()['BORDE']
        assert not any(e['tipo'] == 'CAIDA' for e in h['censura']['eventos']), h['censura']

    def test_un_hueco_en_un_vendedor_lento_no_es_agotado(self, db, calc):
        """1 u cada 5 días: 10 días sin venta pasan por azar ((0,8)^10 ≈ 11 %)."""
        _vende(db, 'LENTO', _serie(lambda d: 0 if 340 <= d <= 349 else (1 if d % 5 == 0 else 0)))
        h = calc()['LENTO']
        assert h['censura']['dias'] == 0 and 'agot' not in h['texto']

    def test_la_sigma_del_pico_es_al_menos_la_de_la_temporada(self, db, calc):
        _vende(db, 'PICOV', _serie(lambda d: (20 if d % 2 else 40) if d in EN_VENTANA_LY else 10))
        h = calc()['PICOV']
        assert h['sigma_dia'] >= 10

    def test_con_poca_base_la_tendencia_es_uno(self, db, calc):
        """1 u cada 2 días: 28 u en las 8 semanas del año pasado (< 30)."""
        _vende(db, 'POCA_BASE', _serie(lambda d: 1 if d % 2 == 0 else 0))
        h = calc()['POCA_BASE']
        assert h['tendencia']['factor'] == 1.0 and h['tendencia']['medida'] is None
        assert 'muy poco para saber' in h['texto']

    def test_nunca_por_debajo_de_su_venta_normal_sin_decirlo(self, db, calc):
        _vende(db, 'BAJA', _serie(lambda d: 3 if d in EN_VENTANA_LY else 10))
        h = calc()['BAJA']
        assert h['d_dia'] == pytest.approx(3.0) and h['base_normal'] == pytest.approx(10.0)
        assert 'Es menos que su venta normal (10 al día)' in h['texto']

    def test_con_agotado_nunca_menos_que_la_venta_normal(self, db, calc):
        """El año pasado la primera semana de la ventana vendió 3/día (se
        estaba acabando, sin llegar a ser una caída) y las otras 21 en cero:
        los días con existencias dan 3/día, menos que su venta normal (10). Con
        censura la tasa es el mayor de los dos (P1-A), y se dice."""
        _vende(db, 'AG_BAJO', _serie(lambda d: 3 if 358 <= d <= 364
                                     else (0 if 337 <= d <= 357 else 10)))
        h = calc()['AG_BAJO']
        assert h['motivo'] == 'CORREGIDA_POR_AGOTADO'
        assert h['d_dia'] == pytest.approx(10.0)
        assert 'se usa la venta normal' in h['texto']

    def test_el_agotado_de_hoy_sin_stock_conocido_no_se_inventa(self, db, calc):
        """Sin fila de existencias no se sabe si está en cero: la racha final no
        se cuenta como agotado (Regla 0: el SKU ni entra a la bandeja)."""
        _vende(db, 'HOY_NS', _serie(lambda d: 0 if d <= 35 else 10))
        h = calc()['HOY_NS']
        assert not any(e['tipo'] == 'AGOTADO_HOY' for e in h['censura']['eventos'])


class TestDormidoYDejado:

    def test_el_estacional_dormido_vuelve(self, mundo):
        assert mundo['DORMIDO']['vuelve_en_temporada'] is True
        assert mundo['DORMIDO']['d_dia'] == pytest.approx(20.0)

    def test_el_que_dejo_de_venderse_no_vuelve(self, mundo):
        assert mundo['DEJADO']['vuelve_en_temporada'] is False

    def test_en_el_rop(self, app, db, mundo):
        from app.services.armador_service import ArmadorService
        filas = {f['referencia']: f for f in ArmadorService.rop_dual()['nacional']['items']}
        assert filas['DORMIDO']['sin_venta_reciente'] is False
        assert filas['DORMIDO']['d_avg_diaria'] == pytest.approx(20.0)
        assert filas['DEJADO']['sin_venta_reciente'] is True
        assert filas['DEJADO']['d_avg_diaria'] == 0


class TestLoQueNoEsVentaRepetible:

    def test_la_licitacion_no_entra(self, mundo, app):
        from app.services.kardex_service import KardexService
        b = KardexService.demanda_descensurada(12, 'red')['LICIT']
        assert b['d_avg'] == pytest.approx(10.0)
        assert b['venta_proyecto'] == 20000
        assert 'Aparte vendió 20.000 por proyectos o licitaciones (NS2, BC99)' in mundo['LICIT']['texto']

    def test_sin_bodegas_de_proyecto_entra_como_siempre(self, app, db, mundo, monkeypatch):
        monkeypatch.setenv('DEMANDA_BODEGAS_PROYECTO', 'ninguna')
        from app.services.kardex_service import KardexService, bodegas_de_proyecto
        assert bodegas_de_proyecto()['bodegas'] == []
        b = KardexService.demanda_descensurada(12, 'red')['LICIT']
        # Sin la lista, la licitación entra a la venta repetible… y el tope de
        # atípicos la ataja igual: la segunda baranda.
        assert b['venta_proyecto'] == 0
        assert b['atipicos']['dias'][0]['vendido'] == 20010

    def test_la_lista_se_configura(self, app, monkeypatch):
        from app.services.kardex_service import bodegas_de_proyecto
        monkeypatch.setenv('DEMANDA_BODEGAS_PROYECTO', 'ns2, fp1')
        r = bodegas_de_proyecto()
        assert r['bodegas'] == ['FP1', 'NS2'] and r['fuente'] == 'CONFIGURADO'
        monkeypatch.delenv('DEMANDA_BODEGAS_PROYECTO')
        assert bodegas_de_proyecto()['fuente'] == 'DEFAULT_DECLARADO'

    def test_con_el_kardex_tambien(self, app, db, monkeypatch):
        """Una política para las tres fuentes: el kardex aparta NS2 igual."""
        monkeypatch.delenv('DEMANDA_BODEGAS_PROYECTO', raising=False)
        from app.services.kardex_service import KardexMovimiento, serie_demanda
        from app.services.demanda_fuentes import fuente_de_demanda
        for d, bod, q in ((3, 'NB1', 5), (3, 'NS2', 900), (2, 'BC99', 50)):
            db.session.add(KardexMovimiento(
                fecha=_hoy() - timedelta(days=d), tipo_docto='X', bodega=bod, referencia='K',
                concepto=501, naturaleza=2, cantidad=q, costo_promedio=0))
        db.session.commit()
        s = serie_demanda(_hoy() - timedelta(days=5), _hoy(), 'red', fuente=fuente_de_demanda())['K']
        assert sum(s['por_dia'].values()) == 5 and s['venta_proyecto'] == 950

    def test_el_dia_atipico_se_topa_y_se_dice(self, mundo, app):
        from app.services.kardex_service import KardexService
        b = KardexService.demanda_descensurada(12, 'red')['PICO']
        # 10 × la mediana (10) = 100; el p99 (4.º mayor) es 10.
        assert b['atipicos']['umbral'] == 100
        assert b['atipicos']['dias'][0]['vendido'] == 5000
        assert b['demanda_neta'] == pytest.approx(10 * (b['dias_ventana'] - 1) + 100)
        assert 'Un día atípico se contó hasta 100' in mundo['PICO']['texto']


class TestElTopeDeAtipicos:
    """La función sola: lo que ve y lo que no."""

    def _serie(self, valores):
        from datetime import date
        return {date(2025, 1, 1) + timedelta(days=i): v for i, v in enumerate(valores) if v}

    def test_pocos_dias_no_se_juzgan(self):
        from app.services.kardex_service import tope_atipicos
        s = self._serie([10] * 5 + [9000])
        assert tope_atipicos(s, 364)[1] is None

    def test_lo_que_se_repite_no_es_atipico(self):
        """Cinco días grandes en el año: el p99 ya es uno de ellos."""
        from app.services.kardex_service import tope_atipicos
        s = self._serie([10] * 300 + [500] * 5)
        assert tope_atipicos(s, 364)[1] is None

    def test_tres_dias_grandes_si(self):
        from app.services.kardex_service import tope_atipicos
        s = self._serie([10] * 300 + [500] * 3)
        topado, decl = tope_atipicos(s, 364)
        assert decl['umbral'] == 100 and len(decl['dias']) == 3
        assert decl['unidades_no_contadas'] == 3 * 400
        assert max(topado.values()) == 100


# ─────────────────────────────────────────────────────────────────────────────
# El ROP y la bandeja leen el horizonte
# ─────────────────────────────────────────────────────────────────────────────

class TestElRopPideElHorizonte:

    def test_nivel_objetivo_con_la_venta_de_lo_que_viene(self, app, db, mundo):
        from app.services.armador_service import ArmadorService, nivel_objetivo
        from app.services.kardex_service import _norm_ppf
        r = ArmadorService.rop_dual()
        f = next(x for x in r['nacional']['items'] if x['referencia'] == 'EST')
        d = 30 * 616 / 560
        assert f['d_avg_diaria'] == pytest.approx(d, abs=1e-3)
        assert f['d_avg_historica'] < 12, 'el promedio de 12 meses se publica, no decide'
        assert f['nivel_objetivo'] == round(nivel_objetivo(d, 0.0, 10, 5, 7, _norm_ppf(0.95)))
        assert f['demanda_horizonte']['metodo'] == 'MISMO_PERIODO_ANO_ANTERIOR'
        assert r['demanda_horizonte']['con_mismo_periodo'] >= 4
        assert r['demanda_horizonte']['bodegas_proyecto']['bodegas'] == ['NS2', 'BC99']

    def test_la_bandeja_lo_dice(self, app, db, mundo):
        from app.models.producto import Producto
        from app.models.stock_siesa import StockSiesa
        db.session.add(Producto(codigo='EST', nombre='Cuaderno', codigo_siesa='EST', activo=True))
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='EST', existencia=50,
                                  comprometido=0, salida_sin_conf=0))
        db.session.commit()
        from app.services import compras_bandeja
        b = compras_bandeja.bandeja()
        l = next(x for p in b['proveedores'] for x in p['lineas'] if x['referencia'] == 'EST')
        assert l['porque']['demanda']['metodo'] == 'MISMO_PERIODO_ANO_ANTERIOR'
        assert 'El año pasado, del ' in l['porque']['demanda']['texto']
        assert l['porque']['promedio_anual_dia'] < 12
        assert b['demanda']['horizonte']['por_metodo']['MISMO_PERIODO_ANO_ANTERIOR'] >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete: el promedio de 12 meses no pide
# ─────────────────────────────────────────────────────────────────────────────

_CAMPOS_PROMEDIO = {'d_avg', 'sigma_d'}
#: Lecturas del promedio de 12 meses fuera de su dueño, con su porqué. Solo
#: encoge. La forma permitida: publicarlo (asignarlo a `d_hist`) o filtrar
#: (una comparación). Pedir con él es el defecto.
LECTURAS_DECLARADAS = {
    ('app/services/armador_service.py', 'ArmadorService.calcular_demanda_rop'):
        'Filtra los SKU sin venta (d_avg <= 0) y publica el promedio en '
        'd_avg_historica para comparar; el ROP pide con demanda_para_horizonte.',
}
_FORMAS_PERMITIDAS = ('asigna_d_hist', 'compara')


def _lecturas(src, archivo):
    """[(archivo, funcion, forma)] de cada lectura de `['d_avg']`,
    `['sigma_d']` o `.get('d_avg'|'sigma_d')`. `forma` es `asigna_d_hist`,
    `compara` u `otra`."""
    arbol = ast.parse(src)
    salida = []

    def es_lectura(n):
        if isinstance(n, ast.Subscript):
            k = n.slice
            return isinstance(k, ast.Constant) and k.value in _CAMPOS_PROMEDIO
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get' and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value in _CAMPOS_PROMEDIO):
            return True
        return False

    def visitar(nodo, fn, padre_stmt):
        for hijo in ast.iter_child_nodes(nodo):
            nombre = fn
            stmt = padre_stmt
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nombre = f'{fn}.{hijo.name}' if fn else hijo.name
            elif isinstance(hijo, ast.ClassDef):
                nombre = f'{fn}.{hijo.name}' if fn else hijo.name
            if isinstance(hijo, ast.stmt):
                stmt = hijo
            if es_lectura(hijo):
                forma = 'otra'
                if (isinstance(stmt, ast.Assign) and stmt.value is hijo
                        and all(isinstance(t, ast.Name) and t.id == 'd_hist' for t in stmt.targets)):
                    forma = 'asigna_d_hist'
                elif _dentro_de_compare(stmt, hijo):
                    forma = 'compara'
                salida.append((archivo, nombre, forma))
            visitar(hijo, nombre, stmt)

    visitar(arbol, '', None)
    return salida


def _dentro_de_compare(stmt, nodo):
    if stmt is None:
        return False
    for n in ast.walk(stmt):
        if isinstance(n, ast.Compare) and any(x is nodo for x in ast.walk(n)):
            # Solo si la comparación no alimenta una asignación de otra cosa
            # que un booleano de control (if/continue): se exige que el stmt
            # sea un `if` o que la comparación esté en su test.
            return isinstance(stmt, ast.If) and any(x is n for x in ast.walk(stmt.test))
    return False


def _todas():
    salida, n = [], 0
    for base in ('app', 'flota'):
        for p in sorted((RAIZ / base).rglob('*.py')):
            rel = str(p.relative_to(RAIZ))
            n += 1
            if rel == 'app/services/kardex_service.py':
                continue      # el dueño
            salida.extend(_lecturas(p.read_text(encoding='utf-8'), rel))
    return salida, n


class TestElPromedioAnualNoPide:

    def test_nadie_pide_con_el_promedio_de_12_meses(self):
        lecturas, _n = _todas()
        malas = sorted({(a, f, forma) for a, f, forma in lecturas
                        if (a, f) not in LECTURAS_DECLARADAS or forma not in _FORMAS_PERMITIDAS})
        assert not malas, (
            f'{malas}: leen el promedio de 12 meses (d_avg/sigma_d de '
            'demanda_descensurada) para algo más que publicarlo o filtrar. La '
            'venta de lo que viene sale de kardex_service.demanda_para_horizonte.')

    def test_el_inventario_solo_encoge(self):
        assert len(LECTURAS_DECLARADAS) <= 1

    def test_cada_entrada_dice_por_que(self):
        assert all(len(v) > 40 for v in LECTURAS_DECLARADAS.values())

    def test_cada_entrada_existe(self):
        usados = {(a, f) for a, f, _ in _todas()[0]}
        assert not set(LECTURAS_DECLARADAS) - usados

    def test_piso(self):
        lecturas, n = _todas()
        assert n >= 200, 'el escáner recorrió muy pocos archivos'
        assert any(a == 'app/services/armador_service.py' for a, _f, _ in lecturas), \
            'el escáner dejó de ver el rop_dual'


class TestElDetectorMuerde:

    def test_ve_pedir_con_el_promedio(self):
        src = ('class ArmadorService:\n'
               '    def rop_dual():\n'
               '        for ref, dem in x.items():\n'
               '            d_avg = dem["d_avg"]\n')
        assert ('x.py', 'ArmadorService.rop_dual', 'otra') in _lecturas(src, 'x.py')

    def test_ve_el_get(self):
        src = 'def f(dem):\n    return dem.get("sigma_d", 0)\n'
        assert ('x.py', 'f', 'otra') in _lecturas(src, 'x.py')

    def test_ve_la_comparacion_que_alimenta_una_cuenta(self):
        src = 'def f(dem):\n    y = 3 if dem["d_avg"] > 1 else dem["d_avg"]\n'
        formas = [x[2] for x in _lecturas(src, 'x.py')]
        assert formas == ['otra', 'otra']

    def test_no_marca_lo_sano(self):
        src = ('def f(dem):\n'
               '    if dem["d_avg"] <= 0:\n'
               '        return\n'
               '    d_hist = dem["d_avg"]\n'
               '    """dem["d_avg"] en un docstring"""\n'
               '    # dem["d_avg"] en un comentario\n')
        assert [x[2] for x in _lecturas(src, 'x.py')] == ['compara', 'asigna_d_hist']


class TestLasBodegasDeProyectoTienenUnDueno:
    """Solo `bodegas_de_proyecto` lee `DEMANDA_BODEGAS_PROYECTO` o su default."""

    NOMBRES = {'DEMANDA_BODEGAS_PROYECTO', 'BODEGAS_PROYECTO_DEFAULT', 'ENV_BODEGAS_PROYECTO'}

    def _usos(self, src, archivo):
        arbol = ast.parse(src)
        usos = []

        def visitar(nodo, fn):
            for h in ast.iter_child_nodes(nodo):
                nombre = h.name if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if isinstance(h, ast.Name) and h.id in self.NOMBRES:
                    usos.append((archivo, fn))
                if isinstance(h, ast.Attribute) and h.attr in self.NOMBRES:
                    usos.append((archivo, fn))
                if isinstance(h, ast.Constant) and h.value == 'DEMANDA_BODEGAS_PROYECTO':
                    usos.append((archivo, fn))
                visitar(h, nombre)

        visitar(arbol, '<modulo>')
        return usos

    def test_un_dueno(self):
        usos = []
        for p in sorted((RAIZ / 'app').rglob('*.py')):
            usos.extend(self._usos(p.read_text(encoding='utf-8'), str(p.relative_to(RAIZ))))
        fuera = sorted({u for u in usos
                        if u not in {('app/services/kardex_service.py', 'bodegas_de_proyecto'),
                                     ('app/services/kardex_service.py', '<modulo>')}})
        assert not fuera, f'{fuera}: deciden qué bodega es de proyecto por su cuenta'
        assert ('app/services/kardex_service.py', 'bodegas_de_proyecto') in usos, 'piso'

    def test_el_detector_ve(self):
        assert self._usos('def f():\n    return os.getenv("DEMANDA_BODEGAS_PROYECTO")\n',
                          'x.py') == [('x.py', 'f')]


# ─────────────────────────────────────────────────────────────────────────────
# La pantalla (Node con util.js real, contra la respuesta real)
# ─────────────────────────────────────────────────────────────────────────────

class TestLaPantalla:

    def _bandeja(self, db, nombre='Cuaderno'):
        from app.models.producto import Producto
        from app.models.stock_siesa import StockSiesa
        db.session.add(Producto(codigo='EST', nombre=nombre, codigo_siesa='EST', activo=True))
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='EST', existencia=50,
                                  comprometido=0, salida_sin_conf=0))
        db.session.commit()
        from app.services import compras_bandeja
        from tests.test_compras_bandeja_js import _json
        return _json(compras_bandeja.bandeja())

    def test_el_porque_dice_de_donde_sale_la_venta(self, app, db, mundo, tmp_path):
        import json
        from tests.test_compras_bandeja_js import MALO, _limpio, _node
        d = self._bandeja(db, nombre=MALO)
        pos = next((i, j) for i, p in enumerate(d['proveedores'])
                   for j, l in enumerate(p['lineas']) if l['referencia'] == 'EST')
        linea = d['proveedores'][pos[0]]['lineas'][pos[1]]
        out = _node(tmp_path, pintar={
            'html': f'cmpBandejaHtml({json.dumps(d)}, null)',
            'porque': f'cmpPorQueHtml({json.dumps(linea)})'})
        texto = _limpio(out['html'])
        assert '<img' not in out['html'], 'el nombre escrito por una persona se escapa'
        porque = _limpio(out['porque'])
        assert 'Para lo que viene se cuentan 33 al día' in porque
        assert 'el promedio del año, que ya no decide' in porque
        assert 'El año pasado, del ' in porque and 'vendió 840' in porque
        assert 'Venta de lo que viene:' in texto
        assert 'con lo que vendieron en la misma época del año pasado' in texto
        assert 'NS2, BC99' in texto and 'no cuenta' in texto
        assert 'entre 50 % y 150 % del año pasado' in texto

    def test_la_venta_diaria_chica_no_se_redondea_a_cero(self, tmp_path):
        from tests.test_compras_bandeja_js import _node
        out = _node(tmp_path, pintar={'a': 'cmpNd(0.34)', 'b': 'cmpNd(33.2)', 'c': 'cmpNd(null)'})
        assert out['a'] == '0,3' and out['b'] == '33' and out['c'] == 'sin dato'
