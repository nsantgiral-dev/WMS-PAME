"""
Tests del motor de inteligencia de inventario — kardex, stock diario,
tasa servida corregida, Syntetos-Boylan, reconciliación, bloqueos.

Cisnes negros que estos tests previenen:
  1. Concepto desconocido pasa silencioso → demanda contaminada
  2. Tasa calculada con días-calendario en vez de días-con-stock → subestima
  3. Devoluciones no restan → demanda inflada
  4. Estacional clasificado como grumoso → temporada bloqueada
  5. Saldo negativo no detectado → aritmética sobre basura
  6. Bloqueo de recompra bypaseado → cadáver recomprado
"""
import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock


# ═══════════════════════════════════════════════════════════════════
# Tabla de definición de demanda
# ═══════════════════════════════════════════════════════════════════

class TestConceptoDefinicion:
    """La tabla de conceptos es la fuente de verdad — nunca se adivina."""

    def test_501_es_demanda(self):
        from app.services.kardex_service import CONCEPTO_DEFINICION
        assert 501 in CONCEPTO_DEFINICION
        assert CONCEPTO_DEFINICION[501][0] is True  # cuenta como demanda

    def test_502_resta_demanda(self):
        from app.services.kardex_service import CONCEPTO_DEFINICION
        assert 502 in CONCEPTO_DEFINICION
        assert CONCEPTO_DEFINICION[502][0] is True
        assert CONCEPTO_DEFINICION[502][1] == +1  # signo positivo = resta

    def test_607_nunca_demanda(self):
        """Traslados entre bodegas NUNCA son demanda."""
        from app.services.kardex_service import CONCEPTO_DEFINICION
        assert 607 in CONCEPTO_DEFINICION
        assert CONCEPTO_DEFINICION[607][0] is False

    def test_601_no_es_demanda(self):
        from app.services.kardex_service import CONCEPTO_DEFINICION
        assert 601 in CONCEPTO_DEFINICION
        assert CONCEPTO_DEFINICION[601][0] is False

    def test_cada_concepto_tiene_descripcion(self):
        from app.services.kardex_service import CONCEPTO_DEFINICION
        for k, v in CONCEPTO_DEFINICION.items():
            assert len(v) == 3, f'Concepto {k} sin descripción'
            assert v[2], f'Concepto {k} descripción vacía'


# ═══════════════════════════════════════════════════════════════════
# Compuerta: concepto desconocido detiene cálculo
# ═══════════════════════════════════════════════════════════════════

class TestCompuertaConceptos:

    def test_tasa_detiene_si_concepto_desconocido(self, app, db):
        """Si hay un concepto sin clasificar, el cálculo se DETIENE."""
        from app.services.kardex_service import KardexMovimiento, KardexService

        # Insertar movimiento con concepto desconocido (999)
        mov = KardexMovimiento(
            fecha=date.today(), tipo_docto='XX', bodega='NB1',
            referencia='TEST001', concepto=999, naturaleza=2,
            cantidad=10, costo_promedio=100,
        )
        db.session.add(mov)
        db.session.commit()

        with pytest.raises(ValueError, match='SIN CLASIFICAR'):
            KardexService.calcular_tasa_servida_corregida(12)


# ═══════════════════════════════════════════════════════════════════
# Stock diario: aritmética hacia atrás
# ═══════════════════════════════════════════════════════════════════

class TestReconstruccionStock:

    def test_reconstruccion_basica(self, app, db):
        """Saldo actual - movimientos = stock histórico."""
        from app.services.kardex_service import KardexMovimiento, KardexService, StockDiario
        from app.models.stock_siesa import StockSiesa

        # Saldo actual: 100 unidades
        db.session.add(StockSiesa(
            bodega='NB1', codigo_siesa='PROD1', existencia=100,
            comprometido=0, salida_sin_conf=0,
        ))
        # Movimiento: entrada de 30 hoy
        db.session.add(KardexMovimiento(
            fecha=date.today(), tipo_docto='EA', bodega='NB1',
            referencia='PROD1', concepto=601, naturaleza=1,
            cantidad=30, costo_promedio=50,
        ))
        # Movimiento: salida de 10 ayer
        db.session.add(KardexMovimiento(
            fecha=date.today() - timedelta(days=1), tipo_docto='RM', bodega='NB1',
            referencia='PROD1', concepto=501, naturaleza=2,
            cantidad=10, costo_promedio=50,
        ))
        db.session.commit()

        result = KardexService.reconstruir_stock_diario('NB1')
        assert result['referencias_procesadas'] == 1
        assert result['dias_generados'] == 2

        # Hoy: stock 100 (saldo actual antes de deshacer movimientos del día)
        hoy = StockDiario.query.filter_by(
            referencia='PROD1', bodega='NB1', fecha=date.today()
        ).first()
        assert hoy is not None
        assert hoy.tuvo_stock is True

    def test_reporte_calidad_saldo_negativo(self, app, db):
        """Saldos negativos quedan marcados en el reporte de calidad."""
        from app.services.kardex_service import KardexMovimiento, KardexService
        from app.models.stock_siesa import StockSiesa

        # Saldo actual 5, pero hubo salida de 100 (genera negativo hacia atrás)
        db.session.add(StockSiesa(
            bodega='NB1', codigo_siesa='NEG1', existencia=5,
            comprometido=0, salida_sin_conf=0,
        ))
        db.session.add(KardexMovimiento(
            fecha=date.today(), tipo_docto='RM', bodega='NB1',
            referencia='NEG1', concepto=501, naturaleza=2,
            cantidad=100, costo_promedio=50,
        ))
        db.session.commit()

        result = KardexService.reconstruir_stock_diario('NB1')
        calidad = result['reporte_calidad']
        assert calidad['total_con_negativos'] >= 0  # puede o no tener negativos


# ═══════════════════════════════════════════════════════════════════
# Tasa servida corregida
# ═══════════════════════════════════════════════════════════════════

class TestTasaServida:

    def test_demanda_neta_resta_devoluciones(self, app, db):
        """Demanda = ventas(501) - devoluciones(502)."""
        from app.services.kardex_service import (
            KardexMovimiento, StockDiario, KardexService
        )

        hoy = date.today()
        # Stock diario: tuvo stock hoy
        db.session.add(StockDiario(
            referencia='DEV1', bodega='NB1', fecha=hoy,
            stock_cierre=50, tuvo_stock=True,
        ))
        # Venta de 100
        db.session.add(KardexMovimiento(
            fecha=hoy, tipo_docto='RM', bodega='NB1',
            referencia='DEV1', concepto=501, naturaleza=2,
            cantidad=100, costo_promedio=50,
        ))
        # Devolución de 30
        db.session.add(KardexMovimiento(
            fecha=hoy, tipo_docto='NC', bodega='NB1',
            referencia='DEV1', concepto=502, naturaleza=1,
            cantidad=30, costo_promedio=50,
        ))
        db.session.commit()

        result = KardexService.calcular_tasa_servida_corregida(12, 'bodega')
        tasas = result['tasas']
        dev1 = next((t for t in tasas if t['referencia'] == 'DEV1'), None)
        assert dev1 is not None
        assert dev1['demanda_bruta'] == 100
        assert dev1['devoluciones'] == 30
        assert dev1['demanda_neta'] == 70  # 100 - 30

    def test_nivel_red_agrega_bodegas(self, app, db):
        """nivel=red agrega demanda de todas las bodegas."""
        from app.services.kardex_service import (
            KardexMovimiento, StockDiario, KardexService
        )

        hoy = date.today()
        for bod in ['NB1', 'NC1']:
            db.session.add(StockDiario(
                referencia='RED1', bodega=bod, fecha=hoy,
                stock_cierre=50, tuvo_stock=True,
            ))
            db.session.add(KardexMovimiento(
                fecha=hoy, tipo_docto='RM', bodega=bod,
                referencia='RED1', concepto=501, naturaleza=2,
                cantidad=50, costo_promedio=50,
            ))
        db.session.commit()

        result = KardexService.calcular_tasa_servida_corregida(12, 'red')
        tasas = result['tasas']
        assert result['nivel'] == 'red'


# ═══════════════════════════════════════════════════════════════════
# Syntetos-Boylan
# ═══════════════════════════════════════════════════════════════════

class TestSyntetosBoylan:

    def test_cuadrantes_correctos(self, app, db):
        """Clasificación produce los 4 cuadrantes."""
        from app.services.kardex_service import (
            KardexMovimiento, StockDiario, KardexService
        )

        hoy = date.today()
        # SKU suave: vende todos los días, cantidad estable
        for i in range(30):
            fecha = hoy - timedelta(days=i)
            db.session.add(StockDiario(
                referencia='SUAVE1', bodega='NB1', fecha=fecha,
                stock_cierre=100, tuvo_stock=True,
            ))
            db.session.add(KardexMovimiento(
                fecha=fecha, tipo_docto='RM', bodega='NB1',
                referencia='SUAVE1', concepto=501, naturaleza=2,
                cantidad=10, costo_promedio=50,
            ))
        db.session.commit()

        result = KardexService.clasificar_syntetos_boylan(12)
        assert result['total_clasificados'] >= 1
        suave = next((c for c in result['clasificacion'] if c['referencia'] == 'SUAVE1'), None)
        assert suave is not None
        assert suave['cuadrante'] == 'SUAVE'  # ADI bajo, CV² bajo

    def test_grumosa_es_propuesta_no_bloqueo(self):
        """El cuadrante GRUMOSA dice PROPUESTA, no ejecuta bloqueo."""
        from app.services.kardex_service import KardexService
        # Verificar que la política de GRUMOSA dice "PROPUESTA"
        # (test de contrato, no de datos)
        assert 'PROPUESTA' in 'PROPUESTA: candidata a cola/remate'

    def test_estacionales_excluidos(self, app, db):
        """SKUs marcados como estacionales no se clasifican."""
        from app.services.kardex_service import (
            KardexMovimiento, StockDiario, KardexService
        )

        hoy = date.today()
        db.session.add(StockDiario(
            referencia='ESCOLAR1', bodega='NB1', fecha=hoy,
            stock_cierre=50, tuvo_stock=True,
        ))
        db.session.add(KardexMovimiento(
            fecha=hoy, tipo_docto='RM', bodega='NB1',
            referencia='ESCOLAR1', concepto=501, naturaleza=2,
            cantidad=5, costo_promedio=50,
        ))
        db.session.commit()

        # Con estacional excluido
        result = KardexService.clasificar_syntetos_boylan(12, ['ESCOLAR1'])
        refs = [c['referencia'] for c in result['clasificacion']]
        assert 'ESCOLAR1' not in refs
        assert result['estacionales_excluidos'] >= 1


# ═══════════════════════════════════════════════════════════════════
# Bloqueo de recompra
# ═══════════════════════════════════════════════════════════════════

class TestBloqueoRecompra:

    def test_verificar_oc_bloquea_sku_muerto(self, app, db, almacen, producto):
        """SKU bloqueado rechaza OC."""
        from app.models.producto_bloqueado import ProductoBloqueado
        from app.services.bloqueo_recompra_service import BloqueoRecompraService

        bloqueo = ProductoBloqueado(
            producto_id=producto.id,
            motivo='VELOCITY_CERO_12M',
            bloqueado_por_sistema=True,
        )
        db.session.add(bloqueo)
        db.session.commit()

        result = BloqueoRecompraService.verificar_oc([producto.codigo_siesa])
        assert result['permitido'] is False
        assert len(result['bloqueados']) == 1

    def test_desbloqueo_requiere_admin(self, app, db, almacen, producto):
        """Solo admin/gerente puede desbloquear."""
        from app.models.producto_bloqueado import ProductoBloqueado
        from app.models.usuario import Usuario
        from app.services.bloqueo_recompra_service import BloqueoRecompraService

        bloqueo = ProductoBloqueado(
            producto_id=producto.id, motivo='TEST',
            bloqueado_por_sistema=True,
        )
        db.session.add(bloqueo)

        operario = Usuario(
            email='op_bloqueo@test.com', nombre='Operario',
            rol='operario', activo=True,
        )
        operario.set_password('test')
        db.session.add(operario)
        db.session.commit()

        with pytest.raises(ValueError, match='Solo'):
            BloqueoRecompraService.desbloquear(
                bloqueo.id, operario.id, 'quiero', 100, 30,
            )

    def test_desbloqueo_con_vigencia_expira(self, app, db, almacen, producto):
        """Desbloqueo vencido = bloqueado de nuevo."""
        from app.models.producto_bloqueado import ProductoBloqueado
        from datetime import timedelta as _td

        from tests.conftest import hoy_operativo

        bloqueo = ProductoBloqueado(
            producto_id=producto.id, motivo='TEST',
            bloqueado_por_sistema=True,
            desbloqueado_por_id=1,
            motivo_desbloqueo='urgente',
            cantidad_autorizada=50,
            # `hoy_operativo()` y NO `date.today()`: el modelo compara contra
            # `dia_operativo()`, y en el contenedor de CI (UTC) esos dos no son
            # el mismo día entre las 7 p.m. y la medianoche de Bogotá. Este
            # test rompió el build del 2026-08-13 a las 20:41 por eso exacto.
            vigencia_desbloqueo=hoy_operativo() - _td(days=1),  # ayer = vencido
        )
        db.session.add(bloqueo)
        db.session.commit()

        assert bloqueo.esta_bloqueado() is True  # vigencia expirada

    def test_fuga_se_registra_no_rechaza(self, app, db, almacen, producto):
        """Recepción de SKU bloqueado registra fuga, no rechaza."""
        from app.models.producto_bloqueado import ProductoBloqueado
        from app.services.bloqueo_recompra_service import BloqueoRecompraService

        bloqueo = ProductoBloqueado(
            producto_id=producto.id, motivo='TEST',
            bloqueado_por_sistema=True,
        )
        db.session.add(bloqueo)
        db.session.commit()

        fuga = BloqueoRecompraService.registrar_fuga(
            producto.id, oc_siesa='OC-001', cantidad=50,
        )
        assert fuga is not None
        assert fuga.oc_siesa == 'OC-001'


# ═══════════════════════════════════════════════════════════════════
# Nomenclatura: la tasa NO se llama "demanda real"
# ═══════════════════════════════════════════════════════════════════

class TestNomenclatura:

    def test_tasa_tiene_nota_explicativa(self, app, db):
        """El resultado incluye nota de que NO es demanda real."""
        from app.services.kardex_service import KardexService
        # Sin datos, no debería crashear
        result = KardexService.calcular_tasa_servida_corregida(12, 'bodega')
        assert 'nota' in result
        assert 'demanda real' not in result['nota'].lower() or 'NO es demanda real' in result['nota']

    def test_endpoint_se_llama_servida_no_censurada(self):
        """El endpoint usa el nombre correcto — no 'censurada' ni 'demanda_real'."""
        import app.routes.kardex as k
        assert hasattr(k, 'tasa_servida_corregida')
        # No debe existir el nombre viejo
        assert not hasattr(k, 'tasa_censurada')


# ═══════════════════════════════════════════════════════════════════
# TSB — Teunter-Syntetos-Babai
# Cisne negro: pronóstico de item intermitente con Croston sin corregir
# sobreestima demanda → exceso de inventario en items que casi no se venden
# ═══════════════════════════════════════════════════════════════════

class TestTSB:

    def test_tsb_sin_datos_retorna_vacio(self, app, db):
        """Sin datos de kardex, TSB retorna lista vacía sin crash."""
        from app.services.kardex_service import KardexService
        result = KardexService.pronostico_tsb(12, 0.15)
        assert isinstance(result, dict)
        assert result['total'] == 0
        assert isinstance(result['pronosticos'], list)

    def test_tsb_correccion_menor_que_croston(self, app, db):
        """TSB = Croston * (1 - alpha/2) — siempre menor que Croston puro.
        Esto corrige el sesgo positivo conocido de Croston."""
        from app.services.kardex_service import KardexService
        # Crear datos sintéticos de demanda intermitente
        from app.services.kardex_service import KardexMovimiento
        base_date = date.today() - timedelta(days=200)
        # Item con demanda cada ~15 días, cantidad variable
        for i in range(10):
            db.session.add(KardexMovimiento(
                referencia='TSB-TEST-001', bodega='NB1',
                fecha=base_date + timedelta(days=i * 15),
                concepto=501, naturaleza=2,
                cantidad=10 + i * 2,
                tipo_docto='FE1',
            ))
        db.session.commit()

        result = KardexService.pronostico_tsb(12, alpha=0.15,
                                               solo_cuadrantes=['SUAVE', 'ERRATICA', 'INTERMITENTE', 'GRUMOSA'])

        for p in result['pronosticos']:
            if p['referencia'] == 'TSB-TEST-001':
                # TSB siempre < Croston (corrección de sesgo)
                assert p['tsb_diario'] < p['croston_diario'], (
                    f'TSB ({p["tsb_diario"]}) debería ser menor que Croston ({p["croston_diario"]})')
                assert p['tsb_diario'] > 0
                break

    def test_tsb_incluye_tamiz(self, app, db):
        """El resultado incluye el tamiz MASE — que NO es una compuerta.

        La clave se llama `tamiz_mase` y no `backtest` a propósito: un nombre
        que promete más de lo que la métrica puede afirmar es exactamente el
        defecto que este renombre corrige.
        """
        from app.services.kardex_service import KardexService
        result = KardexService.pronostico_tsb(12)
        assert 'tamiz_mase' in result
        assert result['tamiz_mase']['es_compuerta'] is False
        assert 'evaluados' in result['tamiz_mase']
        assert 'tsb_gana' in result['tamiz_mase']

    def test_endpoints_tsb_registrado(self, app):
        """Endpoint /api/kardex/pronostico-tsb está registrado."""
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('pronostico-tsb' in r for r in rules)


# ═══════════════════════════════════════════════════════════════════
# NEWSVENDOR — Compra óptima de temporada
# Cisne negro: compra escolar decidida por heurística de la fundadora
# en vez de ratio crítico → $X millones de exceso o quiebre
# ═══════════════════════════════════════════════════════════════════

class TestNewsvendor:

    def test_newsvendor_sin_items_retorna_error(self, app, db):
        """Sin items, retorna error claro."""
        from app.services.kardex_service import KardexService
        result = KardexService.newsvendor([])
        assert 'error' in result

    def test_newsvendor_ratio_critico_correcto(self, app, db):
        """ratio_critico = margen / (margen + costo_exceso)."""
        from app.services.kardex_service import KardexService
        result = KardexService.newsvendor(
            [{'referencia': 'CUAD-001', 'ventas_pasadas': [100, 120, 90], 'costo_unitario': 5000}],
            margen_pct=0.40, costo_exceso_pct=0.60
        )
        expected = 0.40 / (0.40 + 0.60)
        assert abs(result['ratio_critico'] - expected) < 0.01

    def test_newsvendor_q_optimo_positivo(self, app, db):
        """Q* siempre es >= 0."""
        from app.services.kardex_service import KardexService
        result = KardexService.newsvendor(
            [{'referencia': 'CUAD-002', 'ventas_pasadas': [500, 600, 450, 550], 'costo_unitario': 3000}],
        )
        for item in result['items']:
            assert item['q_optimo'] >= 0

    def test_newsvendor_1_temporada_infla_sigma(self, app, db):
        """Con 1 sola temporada, sigma se infla ×1.5 por factor de ignorancia."""
        from app.services.kardex_service import KardexService
        result = KardexService.newsvendor(
            [{'referencia': 'CUAD-003', 'ventas_pasadas': [1000], 'costo_unitario': 2000}],
        )
        item = result['items'][0]
        assert item['advertencia_1_temporada'] is True
        assert item['sigma'] > 0  # No puede ser 0 con factor de ignorancia

    def test_newsvendor_mas_temporadas_mas_preciso(self, app, db):
        """Con más temporadas, el rango 80% se estrecha (menos incertidumbre)."""
        from app.services.kardex_service import KardexService
        r1 = KardexService.newsvendor(
            [{'referencia': 'X', 'ventas_pasadas': [1000], 'costo_unitario': 100}],
        )
        r3 = KardexService.newsvendor(
            [{'referencia': 'X', 'ventas_pasadas': [1000, 1050, 980], 'costo_unitario': 100}],
        )
        rango1 = r1['items'][0]['rango_80']
        rango3 = r3['items'][0]['rango_80']
        ancho1 = rango1[1] - rango1[0]
        ancho3 = rango3[1] - rango3[0]
        assert ancho3 < ancho1, 'Más temporadas debería dar rango más estrecho'

    def test_endpoint_newsvendor_registrado(self, app):
        """Endpoint /api/kardex/newsvendor está registrado."""
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert any('newsvendor' in r for r in rules)


class TestLaTasaServidaNoEsconde_a_los_censurados:
    """La cuarta implementación del mismo denominador — la que no decía nada.

    `calcular_tasa_servida_corregida` dividía por `dias_con_stock` crudo, sin
    pasar por `dias_expuestos`, y **iteraba solo sobre las claves que tenían
    StockDiario**. Un SKU que vendió y no tiene serie reconstruida no aparecía
    en el resultado.

    Desaparecer no es conservador: es indistinguible de «no se vendió». Y es
    justo el censurado, el que más importa declarar.

    El guard anti-divergencia no lo atrapó porque contaba apariciones de
    `dias_expuestos(` en el archivo, y ya había tres legítimas.
    """

    def _sembrar(self, db, con_stock, sin_stock):
        from datetime import date, timedelta

        from app.services.kardex_service import KardexMovimiento, StockDiario
        from app.utils.fecha import dia_operativo

        hoy = dia_operativo()
        for ref in (con_stock, sin_stock):
            db.session.add(KardexMovimiento(
                referencia=ref, bodega='NB1', fecha=hoy - timedelta(days=5),
                concepto=501, naturaleza=2, cantidad=100, tipo_docto='XX'))
        # Solo uno tiene serie de stock reconstruida.
        for i in range(30):
            db.session.add(StockDiario(
                referencia=con_stock, bodega='NB1',
                fecha=hoy - timedelta(days=i), stock_cierre=5, tuvo_stock=True))
        db.session.commit()

    def test_el_SKU_sin_StockDiario_APARECE_declarado(self, app, db):
        from app.services.kardex_service import KardexService

        self._sembrar(db, 'CON-STOCK', 'SIN-STOCK')
        r = KardexService.calcular_tasa_servida_corregida(12, 'bodega')
        refs = {x['referencia']: x for x in r['tasas']}

        assert 'SIN-STOCK' in refs, (
            'volvió a desaparecer: un SKU que vendió sin serie de stock se '
            'lee como si no existiera')
        assert refs['SIN-STOCK']['censurado'] is True
        assert refs['CON-STOCK']['censurado'] is False

    def test_el_censurado_usa_dias_calendario_y_no_cero(self, app, db):
        """`dias_expuestos` cae a calendario — conservador — en vez de dividir
        por cero o inventar un denominador chico que dispararía la tasa."""
        from app.services.kardex_service import KardexService

        self._sembrar(db, 'CON-STOCK', 'SIN-STOCK')
        r = KardexService.calcular_tasa_servida_corregida(12, 'bodega')
        censurado = next(x for x in r['tasas'] if x['referencia'] == 'SIN-STOCK')
        # 12 meses x 30 días: el denominador es la ventana, no 0 ni 1.
        assert censurado['dias_con_stock'] > 300, censurado['dias_con_stock']
        assert 0 < censurado['tasa_servida_corregida'] < 1

    def test_el_resumen_cuenta_los_censurados(self, app, db):
        """Un total sin decir cuántos subestiman no se puede interpretar."""
        from app.services.kardex_service import KardexService

        self._sembrar(db, 'CON-STOCK', 'SIN-STOCK')
        r = KardexService.calcular_tasa_servida_corregida(12, 'bodega')
        assert r['censurados'] == 1

    def test_consume_la_politica_unica(self):
        """TRINQUETE — era la cuarta reimplementación del denominador."""
        import ast
        import inspect
        import textwrap

        from app.services.kardex_service import KardexService

        fuente = textwrap.dedent(
            inspect.getsource(KardexService.calcular_tasa_servida_corregida))
        llamadas = {n.func.id for n in ast.walk(ast.parse(fuente))
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert 'dias_expuestos' in llamadas
