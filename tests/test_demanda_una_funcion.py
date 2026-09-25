"""
La demanda diaria: UNA función para el numerador, UNA para el denominador.

Auditoría de compras del 2026-09-24 (D1, D6, D7, D8, D14). Todos los mundos se
arman con MOVIMIENTOS DE KARDEX y el RECONSTRUCTOR REAL
(`KardexService.reconstruir_stock_diario`) — nunca con `StockDiario` escrito a
mano. Escribir `StockDiario` a mano es exactamente lo que escondía D1: los
tests fabricaban una fila por día, y el reconstructor solo escribía los días
con movimiento.

D1 — LA CLASE: *«días con stock» contados sobre días con movimiento*. El
reconstructor escribía una fila solo en días con movimiento y los consumidores
contaban `count(distinct fecha) WHERE tuvo_stock`: un día sin movimiento no
existía, así que el denominador era «días con venta». Un SKU que vende 40 cada
25 días con el estante lleno salía a 40/día en vez de 1,56 (×25,7), el ROP a
332 en vez de 37, S-B en SUAVE (ADI 1) y la temporada ×3.

El trinquete de la clase está al final: por AST, NINGÚN sitio fuera de
`serie_demanda` / `intervalos_con_stock` (y su inventario declarado) lee los
conceptos de venta ni `StockDiario`.
"""
import ast
import math
import pathlib
from datetime import date, timedelta

import pytest

from app.utils.fecha import dia_operativo

RAIZ = pathlib.Path(__file__).resolve().parent.parent


# ── Mundo ────────────────────────────────────────────────────────────────────

def _hoy():
    return dia_operativo()


def _mov(db, ref, fecha, cant, concepto=501, nat=2, bod='NB1'):
    from app.services.kardex_service import KardexMovimiento
    db.session.add(KardexMovimiento(
        fecha=fecha, tipo_docto='X', bodega=bod, referencia=ref,
        concepto=concepto, naturaleza=nat, cantidad=cant, costo_promedio=100))


def _venta(db, ref, dias_atras, cant, bod='NB1'):
    _mov(db, ref, _hoy() - timedelta(days=dias_atras), cant, 501, 2, bod)


def _stock(db, ref, existencia, bod='NB1'):
    from app.models.stock_siesa import StockSiesa
    db.session.add(StockSiesa(bodega=bod, codigo_siesa=ref, existencia=existencia,
                              comprometido=0, salida_sin_conf=0))


def _cobertura(db, dias=359):
    """El kardex observa los 12 meses: un movimiento (compra) de otro SKU en el
    primer día de la ventana. La ventana se recorta a la cobertura."""
    _mov(db, 'ZZ-COBERTURA', _hoy() - timedelta(days=dias), 1, 601, 1)
    _stock(db, 'ZZ-COBERTURA', 1)


def _reconstruir(db):
    from app.services.kardex_service import KardexService
    db.session.commit()
    return KardexService.reconstruir_stock_diario()


def _referencia(ventas, con_stock, n_dias=360):
    """Media y sigma calculadas APARTE, sobre la rejilla diaria completa.

    `ventas`: {dias_atras: unidades}; `con_stock(dias_atras) -> bool`.
    El denominador son los días con stock — incluidos los días SIN movimiento.
    """
    xs = [ventas.get(i, 0.0) for i in range(n_dias) if con_stock(i)]
    n = len(xs)
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return m, math.sqrt(var), n


# ═════════════════════════════════════════════════════════════════════════════
# D1 — el denominador es el día, no el movimiento
# ═════════════════════════════════════════════════════════════════════════════

class TestElDenominadorEsElDiaNoElMovimiento:

    def _grumosa(self, db):
        for k in range(14):
            _venta(db, 'GRUMO', 5 + 25 * k, 40)
        _stock(db, 'GRUMO', 1000)
        _cobertura(db)
        _reconstruir(db)

    def test_grumosa_con_stock_siempre(self, app, db):
        """40 cada 25 días, estante siempre lleno: 560/360, no 40/día."""
        from app.services.kardex_service import KardexService
        self._grumosa(db)
        d = KardexService.demanda_descensurada(12, 'red')['GRUMO']
        m, s, n = _referencia({5 + 25 * k: 40 for k in range(14)}, lambda i: True)
        assert d['dias_con_stock'] == n == 360
        assert d['d_avg'] == pytest.approx(m, rel=1e-4), (
            f"d_avg={d['d_avg']} (×{d['d_avg'] / m:.1f}): volvió a dividir por "
            f'días con movimiento')
        assert d['sigma_d'] == pytest.approx(s, rel=1e-3)
        assert d['censurado'] is False
        assert d['factor_censura'] == 1.0

    def test_la_tasa_servida_es_la_misma_funcion(self, app, db):
        from app.services.kardex_service import KardexService
        self._grumosa(db)
        t = {x['referencia']: x for x in
             KardexService.calcular_tasa_servida_corregida(12, 'red')['tasas']}
        assert t['GRUMO']['tasa_servida_corregida'] == pytest.approx(560 / 360, rel=1e-3)

    def test_el_rop_de_la_grumosa(self, app, db):
        """El ROP nacional sale de la misma demanda: 37 y no 332."""
        from app.services.armador_service import ArmadorService, lead_time, sigma_ltd
        from app.services.kardex_service import _norm_ppf
        self._grumosa(db)
        m, s, _n = _referencia({5 + 25 * k: 40 for k in range(14)}, lambda i: True)
        lt = lead_time('NACIONAL')
        z = _norm_ppf(0.95)
        esperado = m * lt['lt_dias'] + z * sigma_ltd(lt['lt_dias'], s, m, lt['sigma_lt'])
        fila = next(f for f in ArmadorService.rop_dual(0.95)['nacional']['items']
                    if f['referencia'] == 'GRUMO')
        assert fila['rop'] == round(esperado), fila
        assert fila['rop'] < 60, 'el ROP de una grumosa volvió a inflarse'

    def test_sb_ve_la_intermitencia(self, app, db):
        """ADI = 360/14 ≈ 25,7 → INTERMITENTE (tamaño constante). Con el
        denominador viejo el ADI era 1 y caía en SUAVE."""
        from app.services.kardex_service import KardexService
        self._grumosa(db)
        c = {x['referencia']: x for x in
             KardexService.clasificar_syntetos_boylan(12)['clasificacion']}
        assert c['GRUMO']['adi'] == pytest.approx(360 / 14, abs=0.01)
        assert c['GRUMO']['cuadrante'] == 'INTERMITENTE'

    def test_lenta(self, app, db):
        from app.services.kardex_service import KardexService
        for k in range(51):
            _venta(db, 'LENTA', 3 + 7 * k, 3)
        _stock(db, 'LENTA', 200)
        _cobertura(db)
        _reconstruir(db)
        d = KardexService.demanda_descensurada(12, 'red')['LENTA']
        assert d['d_avg'] == pytest.approx(153 / 360, rel=1e-4)
        assert d['dias_con_stock'] == 360

    def test_los_dias_agotados_si_salen_del_denominador(self, app, db):
        """La descensura sigue funcionando: 80 días en cero no cuentan.

        Hoy hay 100 (entraron 100 hace 30 días). Entre −200 y −110 se vendieron
        10 por evento (10 eventos) hasta quedar en 0; de −110 a −31 no hubo
        stock. Con stock: −359…−111 (249 días) + −30…0 (31) = 280.
        """
        from app.services.kardex_service import KardexService
        _mov(db, 'AGOT', _hoy() - timedelta(days=30), 100, 601, 1)
        for k in range(10):
            _venta(db, 'AGOT', 200 - 10 * k, 10)
        _stock(db, 'AGOT', 100)
        _cobertura(db)
        _reconstruir(db)
        d = KardexService.demanda_descensurada(12, 'red')['AGOT']
        assert d['dias_con_stock'] == 280
        assert d['d_avg'] == pytest.approx(100 / 280, rel=1e-4)
        assert d['factor_censura'] == pytest.approx(360 / 280, rel=1e-3)

    def test_varias_bodegas_el_mismo_dia_son_un_dia(self, app, db):
        from app.services.kardex_service import KardexService
        for k in range(10):
            _venta(db, 'MULTI', 10 + 30 * k, 5, bod='NB1')
            _venta(db, 'MULTI', 20 + 30 * k, 5, bod='NC1')
        _stock(db, 'MULTI', 500, bod='NB1')
        _stock(db, 'MULTI', 500, bod='NC1')
        _cobertura(db)
        _reconstruir(db)
        d = KardexService.demanda_descensurada(12, 'red')['MULTI']
        assert d['dias_con_stock'] == 360
        assert d['d_avg'] == pytest.approx(100 / 360, rel=1e-4)

    def test_el_reconstructor_escribe_la_apertura(self, app, db):
        """El cierre del día anterior al primer movimiento: sin él, los días
        entre el inicio de la ventana y el primer movimiento no tenían valor."""
        from app.services.kardex_service import StockDiario
        _venta(db, 'APER', 100, 7)
        _stock(db, 'APER', 50)
        _reconstruir(db)
        fila = StockDiario.query.filter_by(
            referencia='APER', fecha=_hoy() - timedelta(days=101)).one()
        assert float(fila.stock_cierre) == 57.0

    def test_reconstruir_dos_veces_no_cambia_nada(self, app, db):
        from app.services.kardex_service import KardexService
        self._grumosa(db)
        a = KardexService.demanda_descensurada(12, 'red')['GRUMO']
        _reconstruir(db)
        b = KardexService.demanda_descensurada(12, 'red')['GRUMO']
        assert a == b

    def test_fuera_de_la_cobertura_no_es_cero(self, app, db):
        """Un kardex de 90 días no es un año con 270 días sin venta."""
        from app.services.kardex_service import KardexService
        for k in range(9):
            _venta(db, 'CORTO', 10 * k, 10)
        _stock(db, 'CORTO', 100)
        _cobertura(db, dias=89)
        _reconstruir(db)
        d = KardexService.demanda_descensurada(12, 'red')['CORTO']
        assert d['dias_ventana'] == 90
        assert d['d_avg'] == pytest.approx(90 / 90, rel=1e-4)


# ═════════════════════════════════════════════════════════════════════════════
# D6 — la devolución se netea contra la venta que devuelve
# ═════════════════════════════════════════════════════════════════════════════

class TestLaDevolucionRestaDeSuVenta:

    def test_devolucion_de_otro_dia(self, app, db):
        from app.services.kardex_service import KardexService, serie_demanda
        _venta(db, 'DEVOL', 10, 100)
        _mov(db, 'DEVOL', _hoy() - timedelta(days=5), 30, 502, 1)
        _stock(db, 'DEVOL', 500)
        _cobertura(db)
        _reconstruir(db)
        d = KardexService.demanda_descensurada(12, 'red')['DEVOL']
        t = {x['referencia']: x for x in
             KardexService.calcular_tasa_servida_corregida(12, 'red')['tasas']}
        assert d['demanda_neta'] == 70
        assert t['DEVOL']['demanda_neta'] == 70, 'dos políticas de neteo otra vez'
        assert d['devoluciones'] == 30
        s = serie_demanda(_hoy() - timedelta(days=359), _hoy())['DEVOL']
        assert s['por_dia'] == {_hoy() - timedelta(days=10): 70.0}

    def test_devolucion_de_una_venta_anterior_a_la_ventana_no_resta(self, app, db):
        """Restarla subestimaría una demanda que no la contiene."""
        from app.services.kardex_service import KardexService
        _venta(db, 'VIEJA', 400, 50)
        _venta(db, 'VIEJA', 20, 10)
        _mov(db, 'VIEJA', _hoy() - timedelta(days=3), 30, 502, 1)
        _stock(db, 'VIEJA', 500)
        _reconstruir(db)
        # La de hace 20 días (10) se neteó entera; 20 quedaron sin venta: la
        # demanda neta de la ventana es 0, no −20.
        assert 'VIEJA' not in KardexService.demanda_descensurada(12, 'red')
        from app.services.kardex_service import serie_demanda
        s = serie_demanda(_hoy() - timedelta(days=359), _hoy())['VIEJA']
        assert s['devuelta'] == 10
        assert s['devolucion_sin_venta'] == 20

    def test_la_mas_reciente_primero(self, app, db):
        from app.services.kardex_service import serie_demanda
        _venta(db, 'LIFO', 30, 10)
        _venta(db, 'LIFO', 20, 10)
        _mov(db, 'LIFO', _hoy() - timedelta(days=10), 15, 502, 1)
        db.session.commit()
        s = serie_demanda(_hoy() - timedelta(days=359), _hoy())['LIFO']
        assert s['por_dia'] == {_hoy() - timedelta(days=30): 5.0}


# ═════════════════════════════════════════════════════════════════════════════
# D7 — TSB hasta la semana actual
# ═════════════════════════════════════════════════════════════════════════════

def _lunes(f):
    return f - timedelta(days=f.weekday())


def _tsb_ref(serie, alpha=0.15):
    z = next((v for v in serie if v > 0), 0.0)
    p = 1.0 if serie and serie[0] > 0 else 0.5
    for y in serie:
        h = 1.0 if y > 0 else 0.0
        p = alpha * h + (1 - alpha) * p
        if h:
            z = alpha * y + (1 - alpha) * z
    return p, z


class TestTSBHastaLaSemanaActual:

    @staticmethod
    def _cant(f):
        # Menos en dic–feb: que no lo declare de TEMPORADA (≥ 40% en la
        # ventana escolar) y lo saque de S-B y de TSB.
        return 2 if f.month in (12, 1, 2) else 5

    def _mundo(self, db, ref='DESCON', stock=1000):
        """Una venta por semana durante 30 semanas, luego 20 SIN venta."""
        for w in range(30):
            f = _hoy() - timedelta(days=7 * (20 + w) + 2)
            _mov(db, ref, f, self._cant(f))
        _stock(db, ref, stock)

    def test_la_rejilla_llega_a_la_semana_actual(self, app, db):
        from app.services.kardex_service import KardexService
        self._mundo(db)
        _reconstruir(db)
        s = KardexService.serie_semanal_descensurada(12)['DESCON']
        assert s[-1][0] == _lunes(_hoy())

    def test_el_pronostico_decae_en_las_semanas_sin_venta(self, app, db):
        """Referencia aparte: rejilla semanal desde la primera semana
        observada hasta la actual, semanas parciales escaladas a 7 días."""
        from app.services.kardex_service import KardexService
        self._mundo(db)
        _reconstruir(db)
        hoy = _hoy()
        desde = hoy - timedelta(days=7 * 49 + 2)      # primera venta = cobertura
        ventas = {}
        for w in range(30):
            f = hoy - timedelta(days=7 * (20 + w) + 2)
            ventas[f] = self._cant(f)
        serie, sem = [], _lunes(desde)
        while sem <= hoy:
            a, b = max(sem, desde), min(sem + timedelta(days=6), hoy)
            obs = (b - a).days + 1
            v = sum(q for f, q in ventas.items() if a <= f <= b)
            serie.append(v * 7.0 / obs)
            sem += timedelta(days=7)
        p, z = _tsb_ref(serie)
        r = KardexService.pronostico_tsb(
            12, 0.15, solo_cuadrantes=['SUAVE', 'ERRATICA', 'INTERMITENTE', 'GRUMOSA'])
        fila = next(x for x in r['pronosticos'] if x['referencia'] == 'DESCON')
        assert fila['semanas'] == len(serie)
        assert fila['tsb_semanal'] == pytest.approx(p * z, abs=0.002)
        assert fila['tsb_semanal'] < 0.5, 'el pronóstico no decayó: rejilla cortada'

    def test_un_agotado_no_es_un_descontinuado(self, app, db):
        """Sin stock y sin venta = no se sabe (None), no cero: TSB no decae."""
        from app.services.kardex_service import KardexService
        for w in range(30):
            _venta(db, 'AGOTADO', 7 * (20 + w) + 2, 5)
        # Cuadra a 0 justo después de la última venta: 20 semanas sin stock.
        _stock(db, 'AGOTADO', 0)
        _cobertura(db, dias=7 * 49 + 2)
        _reconstruir(db)
        s = KardexService.serie_semanal_descensurada(12)['AGOTADO']
        cola = [v for _l, v in s[-18:]]
        assert all(v is None for v in cola), cola


# ═════════════════════════════════════════════════════════════════════════════
# D8 — los estacionales salen de S-B (con la política de temporada)
# ═════════════════════════════════════════════════════════════════════════════

class TestLosEstacionalesSalenDeSB:

    def test_un_sku_de_temporada_no_se_clasifica(self, app, db):
        from app.services.kardex_service import KardexService
        hoy = _hoy()
        # Diciembre–febrero de la temporada pasada: 20 cada 3 días.
        ini = date(hoy.year - 1 if hoy.month >= 3 else hoy.year - 2, 12, 1)
        for j in range(30):
            _mov(db, 'ESCOLAR', ini + timedelta(days=3 * j), 20)
        _mov(db, 'ESCOLAR', hoy - timedelta(days=40), 5)
        for k in range(20):
            _venta(db, 'PAREJO', 5 + 17 * k, 4)
        _stock(db, 'ESCOLAR', 300)
        _stock(db, 'PAREJO', 300)
        _cobertura(db)
        _reconstruir(db)
        r = KardexService.clasificar_syntetos_boylan(12)
        refs = {x['referencia'] for x in r['clasificacion']}
        assert 'ESCOLAR' not in refs
        assert 'ESCOLAR' in r['estacionales_detalle']
        assert 'PAREJO' in refs

    def test_ya_no_lee_la_columna_de_una_letra(self):
        """`ProductoClasificacionABC.clasificacion` es String(1): 'ESTACIONAL'
        no cabe, el filtro no podía dar verdadero nunca."""
        import inspect
        import textwrap
        from app.services.kardex_service import KardexService
        arbol = ast.parse(textwrap.dedent(
            inspect.getsource(KardexService.clasificar_syntetos_boylan)))
        nombres = {n.attr for n in ast.walk(arbol) if isinstance(n, ast.Attribute)}
        assert 'identificar_skus_temporada' in nombres
        assert 'ProductoClasificacionABC' not in {
            n.id for n in ast.walk(arbol) if isinstance(n, ast.Name)}


# ═════════════════════════════════════════════════════════════════════════════
# D14 — lo que dejó de venderse no se repone
# ═════════════════════════════════════════════════════════════════════════════

class TestSinVentaRecienteNoRepone:

    def test_descontinuado_con_estante_lleno(self, app, db):
        from app.services.armador_service import ArmadorService
        for dd in range(181, 360):
            _venta(db, 'DESC', dd, 10)
        _stock(db, 'DESC', 500)
        _reconstruir(db)
        fila = next(f for f in ArmadorService.rop_dual(0.95)['nacional']['items']
                    if f['referencia'] == 'DESC')
        assert fila['sin_venta_reciente'] is True
        assert fila['d_avg_diaria'] == 0
        assert fila['rop'] == 0
        assert fila['d_avg_historica'] == pytest.approx(1790 / 360, rel=1e-3)
        assert fila['motivo_d_cero']

    def test_una_lenta_no_se_declara_descontinuada(self, app, db):
        """1 cada 60 días: 90 días sin venta son azar (λ = 1,5 < 3)."""
        from app.services.kardex_service import KardexService
        for k in range(3):
            _venta(db, 'LENTISIMA', 120 + 60 * k, 1)
        _stock(db, 'LENTISIMA', 10)
        _cobertura(db)
        _reconstruir(db)
        d = KardexService.demanda_descensurada(12, 'red')['LENTISIMA']
        assert d['sin_venta_reciente'] is False

    def test_un_agotado_no_se_declara_descontinuado(self, app, db):
        """Sin venta porque no hay: eso es censura, no abandono."""
        from app.services.kardex_service import KardexService
        for dd in range(100, 360):
            _venta(db, 'SINSTOCK', dd, 10)
        _stock(db, 'SINSTOCK', 0)
        _reconstruir(db)
        d = KardexService.demanda_descensurada(12, 'red')['SINSTOCK']
        assert d['sin_venta_reciente'] is False
        assert d['d_avg'] > 5


# ═════════════════════════════════════════════════════════════════════════════
# La temporada, con el mismo escalón
# ═════════════════════════════════════════════════════════════════════════════

class TestLaTemporadaConElEscalon:

    def test_600_vendidas_con_stock_son_600(self, app, db):
        """20 cada 3 días en dic–feb, estante lleno: 600 por temporada, no 1.800."""
        from app.models.producto import Producto
        from app.services.temporada_service import TemporadaService
        hoy = _hoy()
        db.session.add(Producto(codigo='ESC1', nombre='Cuaderno', codigo_siesa='ESC1'))
        ultima = hoy.year - 1 if hoy.month >= 3 else hoy.year - 2
        for anio in (ultima - 2, ultima - 1, ultima):
            for j in range(30):
                _mov(db, 'ESC1', date(anio, 12, 1) + timedelta(days=3 * j), 20)
        _mov(db, 'ESC1', date(ultima, 11, 5), 500, 601, 1)
        _stock(db, 'ESC1', 300)
        _reconstruir(db)
        info = TemporadaService.identificar_skus_temporada()['ESC1']
        assert set(info['por_temporada'].values()) == {600.0}
        r = TemporadaService.preparar_pedido_temporada()
        fila = next(f for f in r['items'] if f['referencia'] == 'ESC1')
        assert fila['demanda_esperada'] == 600.0


# ═════════════════════════════════════════════════════════════════════════════
# EL TRINQUETE DE LA CLASE — por AST, con inventario declarado
# ═════════════════════════════════════════════════════════════════════════════

#: Los únicos sitios que pueden leer las primitivas de la demanda.
#: Clave: (archivo, función). Valor: por qué. Solo encoge.
LECTORES_DECLARADOS = {
    ('app/services/kardex_service.py', 'serie_demanda'):
        'EL numerador: la única que lee los conceptos de venta y devolución.',
    ('app/services/kardex_service.py', 'intervalos_con_stock'):
        'EL denominador: la única que lee StockDiario para contar días.',
    ('app/services/kardex_service.py', 'KardexService.reconstruir_stock_diario'):
        'El ESCRITOR de StockDiario (no cuenta días).',
    ('app/services/kardex_service.py', 'KardexService.reconciliar_kardex'):
        'Compuerta de completitud en UNIDADES contra facturas: no es demanda '
        'diaria, es el cuadre del kardex contra Siesa.',
    ('app/services/kardex_service.py', 'salud_kardex'):
        'Salud: cuenta filas y la última fecha de StockDiario para decir si se '
        'reconstruyó. No calcula demanda.',
    ('app/routes/kardex.py', 'stock_diario'):
        'Vista de detalle de las filas crudas para el comprador (evidencia), '
        'no un denominador.',
}

#: Primitivas: leerlas es calcular demanda por fuera de la función única.
PRIMITIVAS = {'StockDiario', 'CONCEPTOS_VENTA', 'CONCEPTOS_DEVOLUCION', 'tuvo_stock'}


def _usos(src, archivo):
    """[(archivo, función_de_primer_nivel, nombre)] — nombres de código, no texto:
    docstrings y comentarios no son `Name`/`Attribute`."""
    arbol = ast.parse(src)
    usos = []

    def visitar(nodo, funcion):
        for hijo in ast.iter_child_nodes(nodo):
            f = funcion
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # La función que manda es la EXTERNA (una anidada es parte de
                # ella); en una clase, Clase.metodo.
                f = funcion or hijo.name
            if isinstance(hijo, ast.ClassDef) and not funcion:
                visitar_clase(hijo)
                continue
            if isinstance(hijo, ast.Name) and hijo.id in PRIMITIVAS:
                usos.append((archivo, f, hijo.id))
            elif isinstance(hijo, ast.Attribute) and hijo.attr in PRIMITIVAS:
                usos.append((archivo, f, hijo.attr))
            visitar(hijo, f)

    def visitar_clase(clase):
        for miembro in clase.body:
            if isinstance(miembro, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visitar(miembro, f'{clase.name}.{miembro.name}')
            else:
                visitar(miembro, None)

    visitar(arbol, None)
    return usos


def _todos_los_usos():
    usos = []
    for base in ('app', 'flota', 'scripts'):
        for p in (RAIZ / base).rglob('*.py'):
            rel = str(p.relative_to(RAIZ))
            usos += _usos(p.read_text(encoding='utf-8'), rel)
    return usos


class TestNadieCalculaDemandaPorFuera:

    def test_ninguna_funcion_fuera_del_inventario_lee_las_primitivas(self):
        fuera = sorted({
            (a, f) for a, f, _n in _todos_los_usos()
            if f is not None and (a, f) not in LECTORES_DECLARADOS
        })
        assert not fuera, (
            f'{fuera}: leen StockDiario o los conceptos de venta por su cuenta. '
            f'La demanda diaria sale de `serie_demanda` + `intervalos_con_stock` '
            f'(vía `demanda_descensurada`). Así divergieron la descensura, S-B, '
            f'la temporada y la tasa servida.')

    def test_el_inventario_solo_encoge(self):
        assert len(LECTORES_DECLARADOS) <= 6

    def test_cada_entrada_dice_por_que(self):
        assert all(len(v) > 30 for v in LECTORES_DECLARADOS.values())

    def test_cada_entrada_existe_de_verdad(self):
        """Una entrada que ya no lee nada es un permiso que sobra."""
        usados = {(a, f) for a, f, _n in _todos_los_usos()}
        muertas = sorted(set(LECTORES_DECLARADOS) - usados)
        assert not muertas, f'entradas sin uso — sacarlas: {muertas}'


class TestElDetectorMuerde:
    """Un escáner que se desincroniza devuelve cero, y un cero se lee igual que
    «acá no hay nada que hacer»."""

    def test_ve_una_lectura_de_tuvo_stock(self):
        src = ('def mi_tasa():\n'
               '    return db.session.query(StockDiario.fecha).filter('
               'StockDiario.tuvo_stock == True).count()\n')
        assert ('x.py', 'mi_tasa', 'tuvo_stock') in _usos(src, 'x.py')

    def test_ve_los_conceptos_de_venta(self):
        src = 'def ventas():\n    return q.filter(K.concepto.in_(CONCEPTOS_VENTA))\n'
        assert ('x.py', 'ventas', 'CONCEPTOS_VENTA') in _usos(src, 'x.py')

    def test_ve_un_metodo_de_clase(self):
        src = 'class S:\n    def m(self):\n        return StockDiario.query.all()\n'
        assert ('x.py', 'S.m', 'StockDiario') in _usos(src, 'x.py')

    def test_una_anidada_es_de_su_funcion_externa(self):
        src = ('def serie_demanda():\n    def _por_dia():\n'
               '        return CONCEPTOS_VENTA\n    return _por_dia()\n')
        assert _usos(src, 'x.py') == [('x.py', 'serie_demanda', 'CONCEPTOS_VENTA')]

    def test_no_marca_docstrings_ni_comentarios(self):
        src = ('def f():\n    """Lee StockDiario.tuvo_stock y CONCEPTOS_VENTA."""\n'
               '    # StockDiario\n    return 1\n')
        assert _usos(src, 'x.py') == []

    def test_piso_minimo(self):
        """Si el escáner se rompe, esto se pone rojo en vez de reportar cero."""
        assert len(_todos_los_usos()) >= 25
