"""
Validación de la tanda BEF de compras (2026-09-27) — casos del comprador.

Cada test reproduce un hallazgo P1 del validador. Nacieron `xfail(strict=True)`
(validación del 2026-09-27) y quedaron verdes con el arreglo del 2026-09-29:
se quitó la marca.

Los mundos usan la fuente de producción (`demanda_dia_siesa` +
`demanda_dia_cubierto`) SIN `StockDiario` — que es como está producción
(0 filas): no se sabe qué días hubo existencias el año pasado.

Horizonte nacional: LT 10 + ciclo 7 = 17 → ventana del año pasado de 28 días,
días atrás 337..364; tendencia: días 1..56 contra 365..420.
"""
from datetime import timedelta

import pytest

from app.utils.fecha import dia_operativo
from tests.test_demanda_horizonte import _cubrir, _serie, _vende

EN_VENTANA_LY = range(337, 365)


def _hoy():
    return dia_operativo()


@pytest.fixture
def horizonte(app, db, monkeypatch):
    for v in ('DEMANDA_BODEGAS_PROYECTO', 'DEMANDA_TENDENCIA_PISO', 'DEMANDA_TENDENCIA_TECHO',
              'ROP_CICLO_NACIONAL_DIAS'):
        monkeypatch.delenv(v, raising=False)
    _cubrir(db, 430)
    # Básico estable: 10/día todo el año.
    _vende(db, 'BASICO', _serie(lambda d: 10))
    # Escolar: 10/día, 30/día en la época que viene.
    _vende(db, 'ESCOLAR', _serie(lambda d: 30 if d in EN_VENTANA_LY else 10))
    # Escolar que el año pasado se AGOTÓ en el pico: debió vender 30/día en
    # las 4 semanas que vienen; tuvo existencias solo la última semana
    # (días 337..343 = 7 días a 30) y 21 días en cero.
    _vende(db, 'AGOTADO_LY', _serie(lambda d: (30 if d <= 343 else 0) if d in EN_VENTANA_LY else 10))
    # El mismo, agotado las 4 semanas enteras.
    _vende(db, 'AGOTADO_MES_LY', _serie(lambda d: 0 if d in EN_VENTANA_LY else 10))
    # Básico que se agotó HACE 5 SEMANAS y sigue agotado: días 1..35 en cero.
    _vende(db, 'AGOTADO_HOY', _serie(lambda d: 0 if d <= 35 else 10))
    db.session.commit()

    def _calc():
        from app.services.kardex_service import KardexService, demanda_para_horizonte
        base = KardexService.demanda_descensurada(ventana_meses=12, nivel='red')
        return base, demanda_para_horizonte(17, base=base)
    return _calc


class TestCasosSanos:

    def test_basico_y_escolar(self, horizonte):
        base, h = horizonte()
        assert h['BASICO']['d_dia'] == pytest.approx(10, abs=0.01)
        assert h['ESCOLAR']['d_dia'] == pytest.approx(30, abs=0.01)


class TestElAgotadoDelAnoPasadoSeHereda:
    """P1-A: «mismo período del año pasado» hereda el agotado del año pasado y,
    sin `StockDiario` (producción), no hay nada que lo corrija ni lo diga."""

    def test_agotado_parcial_no_pide_menos_que_el_promedio_en_el_pico(self, horizonte):
        base, h = horizonte()
        # Hoy: 7,5/día (210 / 28) — por DEBAJO del nivel de temporada baja (10)
        # y del promedio anual; lo sano es ≥ 30 y como piso el promedio.
        assert h['AGOTADO_LY']['d_dia'] >= base['AGOTADO_LY']['d_avg']

    def test_agotado_el_mes_entero_no_da_demanda_cero(self, horizonte):
        base, h = horizonte()
        # ly = 0 pero las 8 semanas previas sí vendieron → no cae al promedio:
        # d = 0 / 28 × 1,0 = 0. El ROP queda en 0 y el SKU no se propone nunca
        # en su pico, y el agotado se repite el año siguiente.
        assert h['AGOTADO_MES_LY']['d_dia'] > 0

    def test_el_porque_avisa_que_la_base_puede_venir_de_un_agotado(self, horizonte):
        _base, h = horizonte()
        assert h['AGOTADO_LY']['ano_anterior']['faltan_datos_de_agotados'] is True
        assert 'agot' in h['AGOTADO_LY']['texto'].lower()


class TestLaTendenciaCuentaElAgotadoDeHoyComoCaidaDeDemanda:
    """P1-B: la tendencia (últimas 8 semanas / mismas del año pasado) lee un
    agotado de este año como demanda que cayó, y el piso 0,5 la parte en dos."""

    def test_un_agotado_reciente_no_parte_la_demanda_en_dos(self, app, db, horizonte):
        from app.models.stock_siesa import StockSiesa
        # Y el WMS SABE que hoy está en cero en la red.
        db.session.add(StockSiesa(codigo_siesa='AGOTADO_HOY', bodega='NB1', existencia=0))
        db.session.commit()
        _base, h = horizonte()
        assert h['AGOTADO_HOY']['d_dia'] >= 9


class TestLoPedidoSinOCVenceAntesQueElLeadTime:
    """P1-C: «Ya se pidió» cuenta 7 días; el lead time nacional default es
    10 ± 5. Si la OC no aparece en el espejo (sync apagado, OC digitada al
    recibir), al día 8 la línea vuelve y se pide dos veces."""

    def test_lo_pedido_cuenta_mientras_no_pudo_haber_llegado(self, app, db, monkeypatch):
        from datetime import datetime
        from app.models.decision_compra import DecisionCompra
        from app.services import compras_fuentes
        monkeypatch.delenv('COMPRAS_PEDIDO_EN_CAMINO_DIAS', raising=False)
        monkeypatch.delenv('ROP_LT_NACIONAL_DIAS', raising=False)
        hoy = _hoy()
        db.session.add(DecisionCompra(
            referencia='X1', accion='PEDIDO', cantidad_decidida=100, dia=hoy - timedelta(days=8),
            vigente_hasta=hoy - timedelta(days=2), usuario_nombre='compras',
            creada_en=datetime.utcnow() - timedelta(days=8)))
        db.session.commit()
        lt = compras_fuentes.default_lead_time('NACIONAL')
        assert lt['lt_dias'] >= 10
        # Pedido hace 8 días, entrega en 10: todavía no pudo llegar.
        assert compras_fuentes.en_camino(['X1'])['por_sku'].get('X1') == 100.0


    def test_control_un_lo_pedi_vigente_si_cuenta(self, app, db, monkeypatch):
        """Control del caso de arriba: el mismo mundo con la decisión vigente
        cuenta — el xfail falla por la vigencia, no por el mundo."""
        from datetime import datetime
        from app.models.decision_compra import DecisionCompra
        from app.services import compras_fuentes
        monkeypatch.delenv('COMPRAS_PEDIDO_EN_CAMINO_DIAS', raising=False)
        hoy = _hoy()
        db.session.add(DecisionCompra(
            referencia='X2', accion='PEDIDO', cantidad_decidida=100, dia=hoy - timedelta(days=2),
            vigente_hasta=hoy + timedelta(days=4), usuario_nombre='compras',
            creada_en=datetime.utcnow() - timedelta(days=2)))
        db.session.commit()
        assert compras_fuentes.en_camino(['X2'])['por_sku'].get('X2') == 100.0


class TestPosponerMasDe14DiasNoTieneSalida:
    """P1-D: «Posponer» hasta 90 días saca la línea; «Ya decidido» lista solo
    14 días. Pasados los 14, la decisión sigue ocultando la línea y no aparece
    en ninguna lista: no se ve ni se puede deshacer. Si se pospuso ya URGENTE,
    nunca vuelve (el escalamiento exige que NO lo fuera)."""

    def test_toda_decision_que_oculta_una_linea_se_puede_ver(self, app, db):
        from datetime import datetime
        from app.models.decision_compra import DecisionCompra
        from app.services import compras_decisiones as cd
        hoy = _hoy()
        db.session.add(DecisionCompra(
            referencia='P1', accion='POSPUESTO', dia=hoy - timedelta(days=20),
            vigente_hasta=hoy + timedelta(days=40), urgencia_vista='URGENTE',
            usuario_nombre='compras', creada_en=datetime.utcnow() - timedelta(days=20)))
        db.session.commit()
        assert 'P1' in cd.vigentes(['P1'])            # oculta la línea
        visibles = {x['referencia'] for x in cd.recientes(14)}
        assert 'P1' in visibles, 'la bandeja la oculta y «Ya decidido» no la lista'
