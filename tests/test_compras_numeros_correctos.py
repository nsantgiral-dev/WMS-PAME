"""
Compras: los números correctos (auditoría del 2026-09-24).

D2 pedir = tener − hay − viene · D3 el S objetivo no se topa · D4 moneda y
acuerdos activos · D5 precio realizado sin impuesto · D9 σ_LT conservador hasta
6 contenedores · D10 deriva sin columnas inventadas · D11 capital inmovilizado ·
D12 un solo ratio crítico · D13 MOQ como mínimo y presupuesto declarado · D14
lead time en una función · en camino declarado · el tamiz de TSB pesa con costo
real · el bloqueo de recompra mide la velocidad en el kardex.

Los mundos se arman con kardex + el reconstructor REAL (ver
tests/test_demanda_una_funcion.py) y los servicios reales. Las respuestas se
calculan aparte.

Trinquetes (AST) al final: la moneda en una función, el lead time en una
función, lo que viene en una función, y el costo de un producto solo desde
`costo_service`.
"""
import ast
import math
import pathlib
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from app.utils.fecha import dia_operativo

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _hoy():
    return dia_operativo()


def _mov(db, ref, fecha, cant, concepto=501, nat=2, bod='NB1', costo=100):
    from app.services.kardex_service import KardexMovimiento
    db.session.add(KardexMovimiento(
        fecha=fecha, tipo_docto='X', bodega=bod, referencia=ref,
        concepto=concepto, naturaleza=nat, cantidad=cant, costo_promedio=costo))


def _stock(db, ref, existencia, bod='NB1', comprometido=0):
    from app.models.stock_siesa import StockSiesa
    db.session.add(StockSiesa(bodega=bod, codigo_siesa=ref, existencia=existencia,
                              comprometido=comprometido, salida_sin_conf=0))


def _producto(db, ref, origen=None):
    from app.models.producto import Producto
    p = Producto(codigo=ref, nombre=ref, codigo_siesa=ref, origen=origen, activo=True)
    db.session.add(p)
    db.session.flush()
    return p


def _diaria(db, ref, q=10, dias=359, bod='NB1'):
    for d in range(1, dias + 1):
        _mov(db, ref, _hoy() - timedelta(days=d), q, bod=bod)


def _reconstruir(db):
    from app.services.kardex_service import KardexService
    db.session.commit()
    KardexService.reconstruir_stock_diario()


def _phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ═════════════════════════════════════════════════════════════════════════════
# D2 — el pedido de temporada resta lo que hay y lo que viene
# ═════════════════════════════════════════════════════════════════════════════

class TestPedirEsTenerMenosHayMenosViene:

    def _mundo(self, db):
        from app.models.importacion import Contenedor, ItemEnTransito
        hoy = _hoy()
        p = _producto(db, 'ESC2')
        ultima = hoy.year - 1 if hoy.month >= 3 else hoy.year - 2
        for anio, q in ((ultima - 2, 18), (ultima - 1, 20), (ultima, 22)):
            for j in range(30):
                _mov(db, 'ESC2', date(anio, 12, 1) + timedelta(days=3 * j), q)
        _mov(db, 'ESC2', date(ultima, 11, 5), 500, 601, 1, costo=600)
        _stock(db, 'ESC2', 300, comprometido=20)
        c = Contenedor(numero='C-PROD', estado='EN_PRODUCCION')
        db.session.add(c)
        db.session.flush()
        db.session.add(ItemEnTransito(producto_id=p.id, contenedor_id=c.id,
                                      cantidad=100, estado='NAVEGANDO'))
        _reconstruir(db)

    def test_pedir(self, app, db):
        from app.services.temporada_service import TemporadaService
        self._mundo(db)
        r = TemporadaService.preparar_pedido_temporada()
        f = next(x for x in r['items'] if x['referencia'] == 'ESC2')
        assert f['tener'] == f['q_optimo']
        assert f['hay'] == 280, 'disponible = existencia − comprometido'
        assert f['viene'] == 100, 'un contenedor EN_PRODUCCION ya está pedido'
        assert f['pedir'] == max(0, f['q_optimo'] - 380)
        assert f['inversion_pedido'] == round(f['pedir'] * f['costo_unitario'])
        assert r['posicion']['fuente'] == 'armador_service.posicion_inventario'

    def test_sin_fila_de_stock_se_declara(self, app, db):
        """Sin ninguna fila de stock ni nada en camino no es «hay 0»: es no saber."""
        from app.models.importacion import ItemEnTransito
        from app.models.stock_siesa import StockSiesa
        from app.services.temporada_service import TemporadaService
        self._mundo(db)
        StockSiesa.query.filter_by(codigo_siesa='ESC2').delete()
        ItemEnTransito.query.delete()
        db.session.commit()
        r = TemporadaService.preparar_pedido_temporada()
        f = next(x for x in r['items'] if x['referencia'] == 'ESC2')
        assert f['hay_sin_dato'] is True
        assert f['pedir'] == f['q_optimo']
        assert r['posicion']['filas_sin_stock_conocido'] == 1

    def test_el_ratio_de_la_cabecera_es_el_de_las_filas(self, app, db):
        from app.services.temporada_service import TemporadaService
        self._mundo(db)
        r = TemporadaService.preparar_pedido_temporada()
        f = next(x for x in r['items'] if x['referencia'] == 'ESC2')
        assert r['ratio_critico_filas']['min'] <= f['ratio_critico'] <= r['ratio_critico_filas']['max']


# ═════════════════════════════════════════════════════════════════════════════
# D3 — el S objetivo de China no se topa a 180 días
# ═════════════════════════════════════════════════════════════════════════════

class TestElSObjetivoNoSeTopa:

    def test_s_cubre_lt_mas_r_al_nivel_pedido(self, app, db):
        from app.services.armador_service import ArmadorService, sigma_ltd
        from app.services.kardex_service import _norm_ppf
        _producto(db, 'CHI-S', 'CHINA')
        _diaria(db, 'CHI-S', 10)
        _stock(db, 'CHI-S', 5000)
        _reconstruir(db)
        r = ArmadorService.rop_dual(0.95)
        f = next(x for x in r['china']['items'] if x['referencia'] == 'CHI-S')
        d, s = f['d_avg_diaria'], f['sigma_d_diaria']
        lt, slt, R = f['lt_dias'], f['sigma_lt'], f['r_dias']
        esperado = d * (lt + R) + _norm_ppf(0.95) * sigma_ltd(lt, s, d, slt, R)
        assert f['s_objetivo'] == round(esperado)
        assert f['s_objetivo'] / d > 180, 'el tope de relleno volvió a cortar el S'
        servicio = _phi((f['s_objetivo'] - d * (lt + R)) / sigma_ltd(lt, s, d, slt, R))
        assert servicio >= 0.94, f'nivel de servicio implícito {servicio:.1%}'
        assert r['cobertura_max_aplica_a'] == 'RELLENO'


# ═════════════════════════════════════════════════════════════════════════════
# D4 — moneda y acuerdos activos
# ═════════════════════════════════════════════════════════════════════════════

class TestMonedaYAcuerdos:

    def _acuerdo(self, db, ref, precio, moneda='COP', activo=True):
        from app.models.acuerdo_marco import AcuerdoMarco, Proveedor
        p = _producto(db, ref, 'CHINA')
        pv = Proveedor.query.filter_by(codigo='P1').first()
        if pv is None:
            pv = Proveedor(codigo='P1', nombre='Quirond', pais='CHINA')
            db.session.add(pv)
            db.session.flush()
        db.session.add(AcuerdoMarco(
            producto_id=p.id, proveedor_id=pv.id, precio_unitario=precio,
            moneda=moneda, activo=activo, vigencia_desde=_hoy() - timedelta(days=10),
            vigencia_hasta=_hoy() + timedelta(days=80)))
        db.session.commit()
        return p

    def test_un_acuerdo_en_dolares_se_convierte(self, app, db, monkeypatch):
        from app.services.costo_service import resolver_costos
        monkeypatch.delenv('TRM_COP_USD', raising=False)
        monkeypatch.delenv('FACTOR_NACIONALIZACION', raising=False)
        self._acuerdo(db, 'USD1', 0.50, 'USD')
        c = resolver_costos(['USD1'])['USD1']
        assert c['fuente'] == 'ACUERDO_VIGENTE'
        assert c['costo'] == pytest.approx(0.50 * 4200 * 1.45)
        assert c['conversion']['trm_fuente'] == 'SUPUESTA'

    def test_la_trm_es_configurable(self, app, db, monkeypatch):
        from app.services.costo_service import resolver_costos
        monkeypatch.setenv('TRM_COP_USD', '4000')
        monkeypatch.setenv('FACTOR_NACIONALIZACION', '1.30')
        self._acuerdo(db, 'USD2', 1.0, 'USD')
        c = resolver_costos(['USD2'])['USD2']
        assert c['costo'] == pytest.approx(4000 * 1.30)
        assert c['conversion']['trm_fuente'] == 'CONFIGURADA'

    def test_un_acuerdo_inactivo_no_es_costo(self, app, db):
        from app.services.costo_service import resolver_costos
        self._acuerdo(db, 'INACT', 5000, 'COP', activo=False)
        assert resolver_costos(['INACT'])['INACT']['fuente'] != 'ACUERDO_VIGENTE'

    def test_una_moneda_sin_conversion_se_excluye(self, app, db):
        from app.services.costo_service import resolver_costos
        self._acuerdo(db, 'EUR1', 3.0, 'EUR')
        assert resolver_costos(['EUR1'])['EUR1']['fuente'] == 'SIN_COSTO'

    def test_una_cotizacion_no_vigente_no_es_costo(self, app, db):
        from app.models.acuerdo_marco import PrecioProveedor, Proveedor
        from app.services.costo_service import resolver_costos
        p = _producto(db, 'COTZ')
        pv = Proveedor(codigo='P2', nombre='Nacional', pais='COLOMBIA')
        db.session.add(pv)
        db.session.flush()
        db.session.add(PrecioProveedor(producto_id=p.id, proveedor_id=pv.id,
                                       precio_unitario=900, fecha=_hoy(), vigente=False))
        db.session.commit()
        assert resolver_costos(['COTZ'])['COTZ']['fuente'] != 'COTIZACION'


# ═════════════════════════════════════════════════════════════════════════════
# D5 — el precio realizado es sin impuesto; la serie de Vigía no cambia
# ═════════════════════════════════════════════════════════════════════════════

class TestPrecioSinImpuesto:

    def test_valor_de_linea(self):
        from app.services.vigia_service import valor_linea_sin_impuesto as v
        assert v({'f470_vlr_neto': 1190, 'f470_vlr_imp': 190}) == 1000
        assert v({'f470_vlr_bruto': 1100, 'f470_vlr_dscto_linea': 100}) == 1000
        assert v({'f470_vlr_neto': 1190}) is None, 'no se inventa una tasa de IVA'

    def _correr(self, filas):
        from app.services.vigia_service import VigiaService

        class _GW:
            modo_simulacion = False
            n = 0

            def _get(self, api, params):
                _GW.n += 1
                return {'detalle': {'Table': filas if _GW.n == 1 else []}}
        with patch('app.services.connekta_gateway.ConnektaGateway', return_value=_GW()):
            return VigiaService.alimentar_series_facturacion(cos=['003'])

    def test_la_ingesta_separa_el_iva_del_precio_y_no_de_la_serie(self, app, db):
        from app.models.precio_realizado import PrecioRealizado, PERIODO_VIVO
        from app.services.vigia_service import SerieVigia
        from app.services.vigia_service import _lunes_semana_actual
        semana_base = _lunes_semana_actual() - timedelta(days=14)
        db.session.add(SerieVigia(serie='facturacion_003', semana=semana_base,
                                  valor=1, registros=1, fuente='HISTORICO'))
        db.session.commit()
        self._correr([{'f470_cant_base': 1, 'f470_vlr_neto': 1190, 'f470_vlr_imp': 190,
                       'f350_id_tipo_docto': 'FE', 'f350_consec_docto': 1,
                       'f350_ind_estado': '1', 'f120_referencia': 'IVA1'}])
        semana = _lunes_semana_actual() - timedelta(days=7)
        serie = SerieVigia.query.filter_by(serie='facturacion_003', semana=semana).one()
        assert float(serie.valor) == 1190, 'la serie de facturación cambió de base'
        vivo = PrecioRealizado.query.filter_by(
            referencia='IVA1', centro_operacion=None, periodo=PERIODO_VIVO).one()
        assert float(vivo.precio_realizado) == 1000

    def test_el_cu_no_lleva_el_iva(self, app, db):
        from app.models.precio_realizado import PrecioRealizado, PERIODO_VIVO
        from app.services.costo_service import resolver_costos
        _producto(db, 'CU1')
        _mov(db, 'CU1', _hoy() - timedelta(days=5), 10, 601, 1, costo=600)
        db.session.add(PrecioRealizado(referencia='CU1', centro_operacion=None,
                                       periodo=PERIODO_VIVO, valor_total=1000,
                                       cantidad_total=1, precio_realizado=1000))
        # La fila vieja, CON IVA, no se lee.
        db.session.add(PrecioRealizado(referencia='CU1', centro_operacion=None,
                                       periodo='VIVO', valor_total=1190,
                                       cantidad_total=1, precio_realizado=1190))
        db.session.commit()
        c = resolver_costos(['CU1'])['CU1']
        assert c['cu'] == 400
        assert c['precio_sin_impuesto'] is True

    def test_el_total_del_txt_se_declara_sin_base(self, app, db):
        from app.models.precio_realizado import PrecioRealizado
        from app.services.costo_service import resolver_costos, resumen_por_fuente
        _producto(db, 'TXT1')
        _mov(db, 'TXT1', _hoy() - timedelta(days=5), 10, 601, 1, costo=600)
        db.session.add(PrecioRealizado(referencia='TXT1', centro_operacion=None,
                                       periodo='TOTAL', valor_total=1000,
                                       cantidad_total=1, precio_realizado=1000))
        db.session.commit()
        c = resolver_costos(['TXT1'])
        assert c['TXT1']['precio_sin_impuesto'] is None
        assert resumen_por_fuente(c)['precio_base_no_verificada'] == 1


# ═════════════════════════════════════════════════════════════════════════════
# D9 — σ_LT: con menos de 6 contenedores, el mayor
# ═════════════════════════════════════════════════════════════════════════════

class TestSigmaLtConservador:

    def _contenedores(self, db, lts):
        from app.models.importacion import Contenedor
        for i, lt in enumerate(lts):
            db.session.add(Contenedor(numero=f'C{i}', fecha_oc=date(2025, 1, 1),
                                      fecha_recepcion_cedi=date(2025, 1, 1) + timedelta(days=lt)))
        db.session.commit()

    def test_tres_contenedores_no_bajan_del_conservador(self, app, db):
        from app.services.armador_service import (
            ArmadorService, SIGMA_LT_CHINA, LT_CHINA_DIAS)
        self._contenedores(db, (118, 120, 122))
        r = ArmadorService.calcular_sigma_lt_real()
        assert r['sigma_lt'] == SIGMA_LT_CHINA
        assert r['lt_medio'] == 120 > LT_CHINA_DIAS
        assert r['sigma_lt_medida'] == 2.0

    def test_seis_contenedores_mandan(self, app, db):
        from app.services.armador_service import ArmadorService
        self._contenedores(db, (118, 120, 122, 119, 121, 120))
        r = ArmadorService.calcular_sigma_lt_real()
        assert r['fuente'] == 'MEDIDO'
        assert r['sigma_lt'] < 2


# ═════════════════════════════════════════════════════════════════════════════
# D10 — la deriva no revienta con el primer acuerdo
# ═════════════════════════════════════════════════════════════════════════════

class TestDeriva:

    def _acuerdo(self, db):
        return TestMonedaYAcuerdos()._acuerdo(db, 'DER1', 1000, 'COP')

    def test_con_un_acuerdo_declara_que_no_hay_precio_de_compra(self, app, db):
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        self._acuerdo(db)
        r = ComprasInteligenciaService.detectar_deriva()
        assert r['total'] == 0
        assert 'Sin precio de compra recibido' in r['nota']
        assert r['sin_precio_de_compra'] == ['DER1']

    def test_con_fuente_mide_la_plata(self, app, db):
        from app.services import compras_inteligencia_service as cis
        p = self._acuerdo(db)

        def _fuente(desde, ids):
            return [{'producto_id': p.id, 'precio_unitario': 1100, 'moneda': 'COP',
                     'cantidad': 50, 'fecha': _hoy(), 'proveedor': 'Quirond'}], 'PRUEBA'
        with patch.object(cis, 'precios_de_compra_recibidos', _fuente):
            r = cis.ComprasInteligenciaService.detectar_deriva()
        assert r['total'] == 1
        assert r['derivas'][0]['diferencia_pct'] == 10.0
        assert r['impacto_estimado_cop'] == 100 * 50
        assert r['nota'] is None


# ═════════════════════════════════════════════════════════════════════════════
# D11 — capital inmovilizado con costo real, o «sin costo»
# ═════════════════════════════════════════════════════════════════════════════

class TestCapitalInmovilizado:

    def _bloqueado(self, db, ref, stock, costo=None):
        from app.models.almacen import Almacen
        from app.models.inventario import UbicacionProducto
        from app.models.producto_bloqueado import ProductoBloqueado
        from app.models.ubicacion import Ubicacion
        a = Almacen.query.filter_by(codigo='ALM-K').first()
        if a is None:
            a = Almacen(codigo='ALM-K', nombre='K', bodega_siesa_id='NB1', activo=True)
            db.session.add(a)
            db.session.flush()
        u = Ubicacion(codigo=f'U-{ref}', almacen_id=a.id, tipo_zona='GENERAL', activo=True)
        db.session.add(u)
        p = _producto(db, ref)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=u.id, producto_id=p.id, cantidad=stock))
        db.session.add(ProductoBloqueado(producto_id=p.id, motivo='VELOCITY_CERO_12M'))
        if costo:
            _mov(db, ref, _hoy() - timedelta(days=30), 10, 601, 1, costo=costo)
        db.session.commit()

    def test_con_costo_y_sin_costo(self, app, db):
        from app.services.bloqueo_recompra_service import BloqueoRecompraService
        self._bloqueado(db, 'CAP1', 10, costo=600)
        self._bloqueado(db, 'CAP2', 5)
        r = BloqueoRecompraService.vista_capital_inmovilizado()
        items = {i['codigo']: i for i in r['items']}
        assert items['CAP1']['capital_inmovilizado'] == 6000
        assert items['CAP2']['capital_inmovilizado'] is None
        assert r['skus_sin_costo'] == 1
        assert r['total_es_cota_inferior'] is True
        assert r['items'][0]['codigo'] == 'CAP2', 'el que no se sabe cuánto vale, adelante'


# ═════════════════════════════════════════════════════════════════════════════
# D12 — un ratio crítico
# ═════════════════════════════════════════════════════════════════════════════

class TestRatioCritico:

    def test_bases_distintas(self):
        from app.services.kardex_service import ratio_critico, ratio_critico_desde_tasas
        assert ratio_critico_desde_tasas(0.40, 0.60) == pytest.approx(0.40 / 0.76)
        # Es el mismo ratio que Cu/Co en pesos con precio 1000 y costo 600.
        assert ratio_critico_desde_tasas(0.40, 0.60) == pytest.approx(
            ratio_critico(400, 0.60 * 600))

    def test_un_markup_se_rechaza_en_la_ruta(self, app, client, jwt_token_admin):
        r = client.post('/api/kardex/newsvendor', headers={
            'Authorization': f'Bearer {jwt_token_admin}'},
            json={'items': [{'referencia': 'X', 'ventas_pasadas': [10]}],
                  'margen_pct': 1.2})
        assert r.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# D13 — MOQ como mínimo, y el presupuesto que no alcanza se declara
# ═════════════════════════════════════════════════════════════════════════════

class TestMOQYPresupuesto:

    def test_cajas(self):
        from app.services.armador_service import cajas_a_pedir
        assert cajas_a_pedir(60, 12, 4) == 5, 'MOQ como múltiplo daba 8 cajas = 96 u'
        assert cajas_a_pedir(10, 12, 4) == 4
        assert cajas_a_pedir(0, 12, 4) == 0
        assert cajas_a_pedir(1810, 12, 4) == 151

    def _mundo(self, db):
        from app.models.importacion import FichaImportacion
        for ref in ('CHA', 'CHB'):
            p = _producto(db, ref, 'CHINA')
            _diaria(db, ref, 10)
            _stock(db, ref, 0)
            db.session.add(FichaImportacion(
                producto_id=p.id, unidades_por_caja=12, cbm_por_caja=0.05,
                peso_kg_por_caja=10, moq_cajas=4, costo_fob_usd=0.5,
                fuente='PACKING_LIST'))
        _reconstruir(db)

    def test_el_armador_usa_el_minimo(self, app, db):
        from app.services.armador_service import ArmadorService
        self._mundo(db)
        a = ArmadorService.armar_contenedor('40STD')
        for it in a['items']:
            assert it['cajas'] == max(math.ceil(it['deficit_unidades'] / 12), 4)
            assert it['moq_interpretacion'].startswith('MINIMO')

    def test_presupuesto_insuficiente(self, app, db):
        from app.services.armador_service import ArmadorService
        self._mundo(db)
        a = ArmadorService.armar_contenedor('40STD', presupuesto_cop=1000)
        assert a['presupuesto_insuficiente'] is True
        assert a['valor_nacionalizado_cop_estimado'] <= 1000
        motivos = {i['motivo_recorte'] for i in a['recorte_presupuesto']}
        assert 'PRESUPUESTO_DEFICIT' in motivos
        assert {d['referencia'] for d in a['deficit_sin_cubrir_por_presupuesto']} == {'CHA', 'CHB'}

    def test_el_valor_usa_la_trm_configurada(self, app, db, monkeypatch):
        from app.services.armador_service import ArmadorService
        self._mundo(db)
        monkeypatch.setenv('TRM_COP_USD', '4000')
        monkeypatch.setenv('FACTOR_NACIONALIZACION', '1.5')
        a = ArmadorService.armar_contenedor('40STD')
        assert a['valor_nacionalizado_cop_estimado'] == round(a['valor_fob_usd'] * 4000 * 1.5)


# ═════════════════════════════════════════════════════════════════════════════
# D14 — lead time en UNA función, configurable y declarado
# ═════════════════════════════════════════════════════════════════════════════

class TestLeadTime:

    def test_nacional_por_defecto_se_declara(self, app, db, monkeypatch):
        from app.services.armador_service import lead_time
        monkeypatch.delenv('ROP_LT_NACIONAL_DIAS', raising=False)
        lt = lead_time('NACIONAL')
        assert lt['lt_dias'] == 5 and lt['fuente'] == 'DEFAULT_DECLARADO'
        assert lt['nota']

    def test_configurado_mueve_el_rop(self, app, db, monkeypatch):
        from app.services.armador_service import ArmadorService
        _producto(db, 'NAC-LT')
        _diaria(db, 'NAC-LT', 10)
        _stock(db, 'NAC-LT', 5000)
        _reconstruir(db)
        monkeypatch.setenv('ROP_LT_NACIONAL_DIAS', '8')
        r = ArmadorService.rop_dual(0.95)
        f = next(x for x in r['nacional']['items'] if x['referencia'] == 'NAC-LT')
        assert f['lt_dias'] == 8
        assert r['nacional']['lt_fuente'] == 'CONFIGURADO'


# ═════════════════════════════════════════════════════════════════════════════
# En camino — declarado, con los contenedores en producción
# ═════════════════════════════════════════════════════════════════════════════

class TestEnCamino:

    def test_estados(self, app, db):
        from app.models.importacion import Contenedor, ItemEnTransito
        from app.services.armador_service import en_camino
        p = _producto(db, 'EC1')
        for est, cant in (('EN_PRODUCCION', 10), ('BORRADOR', 100), ('RECIBIDO', 1000)):
            c = Contenedor(numero=est, estado=est)
            db.session.add(c)
            db.session.flush()
            db.session.add(ItemEnTransito(producto_id=p.id, contenedor_id=c.id,
                                          cantidad=cant, estado='NAVEGANDO'))
        db.session.add(ItemEnTransito(producto_id=p.id, cantidad=5, estado='EN_PUERTO'))
        db.session.add(ItemEnTransito(producto_id=p.id, cantidad=7, estado='RECIBIDO'))
        db.session.commit()
        r = en_camino()
        assert r['por_ref'] == {'EC1': 15.0}
        assert r['hay_dato'] is True

    def test_sin_fuente_se_declara(self, app, db):
        from app.services.armador_service import ArmadorService
        r = ArmadorService.rop_dual(0.95)
        assert r['insumo_en_camino']['hay_dato'] is False
        assert 'NO SE SABE' in r['insumo_en_camino']['nota']

    def test_la_posicion_lo_suma(self, app, db):
        from app.models.importacion import ItemEnTransito
        from app.services.armador_service import ArmadorService
        p = _producto(db, 'EC2')
        _diaria(db, 'EC2', 10)
        _stock(db, 'EC2', 50)
        db.session.add(ItemEnTransito(producto_id=p.id, cantidad=40, estado='NAVEGANDO'))
        _reconstruir(db)
        f = next(x for x in ArmadorService.rop_dual()['nacional']['items']
                 if x['referencia'] == 'EC2')
        assert f['en_transito'] == 40
        assert f['posicion'] == 90


# ═════════════════════════════════════════════════════════════════════════════
# El tamiz de TSB pesa con el costo de la jerarquía
# ═════════════════════════════════════════════════════════════════════════════

class TestTamizTSBConCosto:

    def test_el_peso_no_es_cero(self, app, db):
        from app.services.kardex_service import KardexService
        _producto(db, 'TSBC')
        for k in range(40):
            _mov(db, 'TSBC', _hoy() - timedelta(days=5 + 8 * k), 3 + (k % 5))
        _mov(db, 'TSBC', _hoy() - timedelta(days=340), 500, 601, 1, costo=700)
        _stock(db, 'TSBC', 400)
        _reconstruir(db)
        r = KardexService.pronostico_tsb(
            12, solo_cuadrantes=['SUAVE', 'ERRATICA', 'INTERMITENTE', 'GRUMOSA'])
        assert any(p['referencia'] == 'TSBC' for p in r['pronosticos'])
        assert r['tamiz_mase']['valor_evaluado'] > 0
        assert r['tamiz_mase']['sin_costo_para_ponderar'] == 0


# ═════════════════════════════════════════════════════════════════════════════
# Bloqueo de recompra — la velocidad sale del kardex
# ═════════════════════════════════════════════════════════════════════════════

class TestBloqueoPorVelocidadDelKardex:

    def _mundo(self, db, cobertura=359, ultimo=1):
        cap = TestCapitalInmovilizado()
        # Se vende por POS: ninguna TareaPicking, pero ventas en el kardex.
        cap._bloqueado(db, 'POS1', 10)
        cap._bloqueado(db, 'MUERTO', 10, costo=500)
        from app.models.producto_bloqueado import ProductoBloqueado
        ProductoBloqueado.query.delete()
        _mov(db, 'POS1', _hoy() - timedelta(days=ultimo), 2, bod='PT1')
        _mov(db, 'ZZ', _hoy() - timedelta(days=cobertura), 1, 601, 1)
        db.session.commit()

    def test_lo_que_se_vende_por_pos_no_se_bloquea(self, app, db):
        from app.models.producto_bloqueado import ProductoBloqueado
        from app.services.bloqueo_recompra_service import BloqueoRecompraService
        self._mundo(db)
        r = BloqueoRecompraService.poblar_lista_inicial()
        bloqueados = {b.producto.codigo_siesa for b in ProductoBloqueado.query.all()}
        assert bloqueados == {'MUERTO'}
        assert r['total_capital_inmovilizado'] == 5000
        assert r['capital_sin_costo'] == 0

    def test_sin_kardex_de_12_meses_no_se_bloquea_nada(self, app, db):
        from app.models.producto_bloqueado import ProductoBloqueado
        from app.services.bloqueo_recompra_service import BloqueoRecompraService
        self._mundo(db, cobertura=100)
        r = BloqueoRecompraService.poblar_lista_inicial()
        assert r['no_se_bloqueo_por'] == 'KARDEX_NO_CUBRE_12_MESES'
        assert ProductoBloqueado.query.count() == 0

    def test_con_kardex_viejo_no_se_bloquea_nada(self, app, db):
        from app.services.bloqueo_recompra_service import BloqueoRecompraService
        self._mundo(db, ultimo=40)
        r = BloqueoRecompraService.poblar_lista_inicial()
        assert r['no_se_bloqueo_por'] == 'KARDEX_DESACTUALIZADO'


# ═════════════════════════════════════════════════════════════════════════════
# TRINQUETES — por AST, con inventario declarado
# ═════════════════════════════════════════════════════════════════════════════

def _archivos():
    for base in ('app', 'flota'):
        for p in (RAIZ / base).rglob('*.py'):
            yield str(p.relative_to(RAIZ)), p.read_text(encoding='utf-8')


def _por_funcion(src):
    """[(funcion_externa | None, nodo)] para todo nodo del árbol."""
    arbol = ast.parse(src)
    out = []

    def visitar(n, f):
        for h in ast.iter_child_nodes(n):
            g = f
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                g = f or h.name
            if isinstance(h, ast.ClassDef) and not f:
                for m in h.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append((f'{h.name}.{m.name}', m))
                        visitar(m, f'{h.name}.{m.name}')
                    else:
                        visitar(m, None)
                continue
            out.append((g, h))
            visitar(h, g)
    visitar(arbol, None)
    return out


# ── La moneda en una función ────────────────────────────────────────────────

LITERALES_MONEDA = {4200, 4200.0, 1.45}


def _literales_moneda(src):
    return [(f, n.value) for f, n in _por_funcion(src)
            if isinstance(n, ast.Constant) and not isinstance(n.value, bool)
            and isinstance(n.value, (int, float)) and n.value in LITERALES_MONEDA]


class TestLaMonedaEnUnaFuncion:

    def test_nadie_escribe_la_trm_ni_el_factor(self):
        fuera = [(a, f, v) for a, src in _archivos()
                 for f, v in _literales_moneda(src)
                 if a != 'app/services/costo_service.py']
        assert not fuera, (
            f'{fuera}: la TRM y el factor de nacionalización viven en '
            f'`costo_service.trm_cop_por_usd` / `factor_nacionalizacion`.')

    def test_el_detector_ve_la_multiplicacion_quemada(self):
        src = 'def armar():\n    return fob * 1.45 * 4200\n'
        assert _literales_moneda(src) == [('armar', 1.45), ('armar', 4200)]

    def test_el_detector_no_ve_el_comentario(self):
        assert _literales_moneda('def f():\n    # 1.45 * 4200\n    return 1\n') == []

    def test_piso(self):
        """La definición existe: si el escáner se rompe, esto se pone rojo."""
        src = (RAIZ / 'app/services/costo_service.py').read_text(encoding='utf-8')
        assert len(_literales_moneda(src)) >= 2


# ── El lead time y lo que viene, en una función ─────────────────────────────

#: Nombre → funciones que pueden leerlo (fuera de su definición de módulo).
LECTURAS_PERMITIDAS = {
    'LT_NACIONAL_DIAS': {'lead_time'},
    'SIGMA_LT_NACIONAL': {'lead_time'},
    'LT_CHINA_DIAS': {'ArmadorService.calcular_sigma_lt_real'},
    'SIGMA_LT_CHINA': {'ArmadorService.calcular_sigma_lt_real'},
    'ItemEnTransito': {'_en_camino_importacion',
                       # G5 cuenta si hay ítems registrados: es una compuerta
                       # de completitud del insumo, no una cantidad que viene.
                       'ArmadorService.verificar_g5'},
}


def _lecturas(src):
    return [(f, n.id if isinstance(n, ast.Name) else n.attr)
            for f, n in _por_funcion(src)
            if (isinstance(n, ast.Name) and n.id in LECTURAS_PERMITIDAS)
            or (isinstance(n, ast.Attribute) and n.attr in LECTURAS_PERMITIDAS)]


class TestLeadTimeYEnCaminoEnUnaFuncion:

    def test_nadie_mas_los_lee(self):
        fuera = []
        for a, src in _archivos():
            if a.startswith('app/models/'):
                continue           # la definición del modelo
            for f, nombre in _lecturas(src):
                if f is None:
                    continue       # definición de la constante / import de módulo
                if f not in LECTURAS_PERMITIDAS[nombre]:
                    fuera.append((a, f, nombre))
        assert not fuera, (
            f'{fuera}: el lead time sale de `armador_service.lead_time` y lo que '
            f'viene de `armador_service.en_camino`. Una segunda lectura es una '
            f'segunda política.')

    def test_el_detector_ve_la_constante(self):
        src = 'def rop():\n    return d * LT_NACIONAL_DIAS\n'
        assert _lecturas(src) == [('rop', 'LT_NACIONAL_DIAS')]

    def test_el_detector_no_ve_el_texto(self):
        assert _lecturas('def rop():\n    """LT_NACIONAL_DIAS"""\n    return 1\n') == []

    def test_piso(self):
        src = (RAIZ / 'app/services/armador_service.py').read_text(encoding='utf-8')
        assert len(_lecturas(src)) >= 8


# ── El costo de un producto solo sale de costo_service ─────────────────────

CAMPOS_COSTO_MAESTRO = {'precio_compra', 'costo_unitario'}


def _costos_del_maestro(src):
    """Lecturas de `x.precio_compra` / `x.costo_unitario` y
    `getattr/hasattr(x, 'costo_unitario')` — no escrituras ni claves de dict."""
    out = []
    for f, n in _por_funcion(src):
        if (isinstance(n, ast.Attribute) and n.attr in CAMPOS_COSTO_MAESTRO
                and isinstance(n.ctx, ast.Load)):
            out.append((f, n.attr))
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ('getattr', 'hasattr') and len(n.args) >= 2
                and isinstance(n.args[1], ast.Constant)
                and n.args[1].value in CAMPOS_COSTO_MAESTRO):
            out.append((f, n.args[1].value))
    return out


#: (archivo, función): por qué puede leer el costo del maestro.
COSTO_DEL_MAESTRO_DECLARADO = {
    ('app/services/costo_service.py', 'resolver_costos'):
        'Nivel 4 de la jerarquía (MAESTRO), declarado en la fila.',
}


class TestElCostoSaleDeLaJerarquia:

    def test_nadie_lee_el_costo_del_maestro(self):
        fuera = sorted({(a, f) for a, src in _archivos() if a.startswith('app/services/')
                        for f, _c in _costos_del_maestro(src)
                        if (a, f) not in COSTO_DEL_MAESTRO_DECLARADO})
        assert not fuera, (
            f'{fuera}: `Producto.precio_compra` no lo puebla ningún sync y '
            f'`Producto.costo_unitario` no existe. El costo sale de '
            f'`costo_service.resolver_costos`.')

    def test_el_detector_ve_el_hasattr(self):
        src = ("def capital(p):\n"
               "    return p.costo_unitario if hasattr(p, 'costo_unitario') else 0\n")
        assert ('capital', 'costo_unitario') in _costos_del_maestro(src)

    def test_no_marca_la_clave_de_un_dict(self):
        assert _costos_del_maestro("def f():\n    return {'costo_unitario': 1}\n") == []

    def test_piso(self):
        src = (RAIZ / 'app/services/costo_service.py').read_text(encoding='utf-8')
        assert len(_costos_del_maestro(src)) >= 1
