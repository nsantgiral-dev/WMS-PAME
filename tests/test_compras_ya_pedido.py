"""
Compras — «ya pedido» honesto (tanda E, 2026-09-27).

La clase: **lo ya pedido se suma y se muestra sin decir de qué orden es ni
hace cuánto debió llegar.** En producción (27-sep, solo lectura) el 100 % de lo
pendiente en OCs abiertas tenía la entrega vencida, el 63 % hace más de 90
días, y 50 OCs aprobadas hace más de 180 días sin una sola entrada sumaban
288.156 u que apagaban la compra de los SKU que citan. La bandeja escribía
«Ya pedido: N» y callaba la línea.

Ahora:
  · `compras_fuentes.politica_ocs_viejas`: el corte nace en 180 días
    (`COMPRAS_OC_EXCLUIR_MAS_DE_DIAS`, `nunca` = sin corte), declarado y
    partido en «sin ninguna entrada» y «saldo de una parcial»;
  · `en_camino` publica por SKU las OCs que cuentan (con su atraso) y las que
    no; `rop_dual` publica `en_transito_vencido`, `bajo_rop_sin_vencidas` y
    `deficit_sin_vencidas`;
  · la bandeja marca «Revisar OC» y lista aparte lo que solo una OC vieja
    cubre;
  · el lead time nacional por defecto es 10 ± 5 (lo medido en producción);
  · «lo que llegó» lee también las entradas de Siesa (el espejo de OCs).

Trinquete AST: toda función de compras que lee lo ya pedido de una fila del
ROP (`en_transito`) lee también su parte vencida.
"""
import ast
import json
import pathlib
from datetime import date, datetime, timedelta

import pytest

from app.utils.fecha import dia_operativo
from tests.test_compras_bandeja import _diaria, _producto, _stock, lt_de_este_mundo
from tests.test_compras_fuentes import ABIERTAS_1, ABIERTAS_2, SiesaFalsa, _sync, fila_oc

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _hoy():
    return dia_operativo()


def _dia(dias_atras):
    return f'{(_hoy() - timedelta(days=dias_atras)).isoformat()}T00:00:00'


def _ocs_de_produccion():
    """La forma de lo medido en producción: una OC aprobada hace 250 días sin
    ninguna entrada, una parcial de hace 200 con el saldo que nunca se cancela,
    una vencida hace 100 (se cuenta, pero ¿va a llegar?) y una de hace 5."""
    return {
        ABIERTAS_1: [
            fila_oc(1, ref='A', pedida=1000, entrada=0, consec=1, fecha=_dia(250), entrega=_dia(250)),
            fila_oc(3, ref='A', pedida=60, entrada=0, consec=3, fecha=_dia(100), entrega=_dia(100)),
            fila_oc(4, ref='A', pedida=40, entrada=0, consec=4, fecha=_dia(5), entrega=_dia(5)),
        ],
        ABIERTAS_2: [
            fila_oc(2, ref='B', pedida=100, entrada=81, consec=2, estado=2,
                    fecha=_dia(200), entrega=_dia(200)),
        ],
    }


@pytest.fixture
def sin_variables(monkeypatch):
    monkeypatch.delenv('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS', raising=False)
    monkeypatch.delenv('COMPRAS_OC_VENCIDA_DIAS', raising=False)


class TestElCorteDeOCsViejas:

    def test_por_defecto_no_cuenta_las_de_mas_de_180_dias(self, app, db, sin_variables):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa(_ocs_de_produccion()))
        r = en_camino()
        assert r['por_sku'] == {'A': 100.0}, 'la de 250 y el saldo de la de 200 no cuentan'
        c = r['declaracion']['corte_antiguedad']
        assert c['dias'] == 180 and c['fuente'] == 'DEFAULT_DECLARADO'
        assert c['ocs_excluidas'] == 2 and c['unidades_excluidas'] == 1019.0
        assert c['ocs_sin_ninguna_entrada'] == 1 and c['unidades_sin_ninguna_entrada'] == 1000.0
        assert c['ocs_con_saldo_parcial'] == 1 and c['unidades_saldo_parcial'] == 19.0

    def test_la_nota_en_palabras_del_comprador(self, app, db, sin_variables):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa(_ocs_de_produccion()))
        nota = en_camino()['declaracion']['corte_antiguedad']['nota']
        assert nota.startswith('No se cuentan como «ya pedido» 1.019 u de 2 orden(es)')
        assert 'hace más de 180 días: 1 sin ninguna entrada (1.000 u) y 1 con el saldo ' \
               'de una entrega parcial (19 u)' in nota
        assert 'anúlela en Siesa' in nota

    def test_por_sku_dice_de_que_oc_es_lo_que_viene(self, app, db, sin_variables):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa(_ocs_de_produccion()))
        d = en_camino()['detalle']
        a = d['A']
        assert [l['oc'] for l in a['lineas_oc']] == ['003-OC-3', '003-OC-4'], 'la más atrasada primero'
        assert a['lineas_oc'][0]['dias_vencida'] == 100 and a['lineas_oc'][0]['vieja'] is True
        assert a['lineas_oc'][1]['vieja'] is False
        assert a['oc_vencida'] == 60.0
        assert a['no_contado_por_viejo'] == 1000.0
        assert a['lineas_no_contadas'][0]['oc'] == '003-OC-1'
        assert a['lineas_no_contadas'][0]['sin_ninguna_entrada'] is True
        # B: solo lo no contado; no entra a por_sku, pero su detalle lo dice.
        assert d['B']['no_contado_por_viejo'] == 19.0

    def test_nunca_no_corta(self, app, db, monkeypatch):
        from app.services.compras_fuentes import en_camino
        monkeypatch.setenv('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS', 'nunca')
        _sync(SiesaFalsa(_ocs_de_produccion()))
        r = en_camino()
        assert r['por_sku'] == {'A': 1100.0, 'B': 19.0}
        assert r['declaracion']['corte_antiguedad']['dias'] is None

    def test_una_variable_ilegible_no_corta_y_lo_dice(self, app, db, monkeypatch):
        from app.services.compras_fuentes import politica_ocs_viejas
        monkeypatch.setenv('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS', 'medio año')
        p = politica_ocs_viejas()
        assert p['excluir_mas_de_dias'] is None and p['excluir_fuente'] == 'ILEGIBLE'
        assert p['problemas']


class TestElLeadTimeNacional:

    def test_el_default_es_lo_medido_en_produccion(self, app, monkeypatch):
        from app.services.compras_fuentes import default_lead_time, lead_time
        monkeypatch.delenv('ROP_LT_NACIONAL_DIAS', raising=False)
        monkeypatch.delenv('ROP_SIGMA_LT_NACIONAL', raising=False)
        d = default_lead_time('NACIONAL')
        assert (d['lt_dias'], d['sigma_lt'], d['fuente']) == (10.0, 5.0, 'DEFAULT_CONSERVADOR')
        r = lead_time(origen='NACIONAL', observaciones={'por_oc': [], 'contenedores': []})
        assert r['lt_dias'] == 10.0 and 'mediana de 10 días' in r['nota']


# ─────────────────────────────────────────────────────────────────────────────
# El ROP y la bandeja: «Revisar OC» en vez de callar la línea
# ─────────────────────────────────────────────────────────────────────────────

def _oc_linea(db, rowid, ref, pendiente, dias_atras, consec, proveedor='P9'):
    from app.models.compras_fuentes import OcLineaSiesa
    f = _hoy() - timedelta(days=dias_atras)
    db.session.add(OcLineaSiesa(
        rowid_linea=rowid, co='003', tipo_docto='OC', consec_docto=consec, fecha_oc=f,
        estado_oc=1, proveedor_codigo=proveedor, proveedor_nombre=f'PROVEEDOR {proveedor}',
        referencia=ref, bodega='NB1', moneda='COP', unidad_medida='UND', factor=1,
        cant_pedida=pendiente, cant_entrada=0, cant_pedida_base=pendiente,
        cant_entrada_base=0, pendiente_base=pendiente, fecha_entrega=f, abierta=True))


@pytest.fixture
def mundo(app, db, monkeypatch, sin_variables):
    """Venden 10/día (359 días de kardex): punto de pedido ≈ 83 con LT 5 ± 2.
      CUBRE:   30 en bodega + 200 de una OC vencida hace 100 días → 230 ≥ 83,
               pero sin la vencida 30 < 83 → «Revisar OC».
      MUERTA:  30 en bodega + 200 de una OC de hace 250 días → no se cuenta →
               URGENTE, y el porqué dice lo que no se contó.
      MEDIA:   10 en bodega + 50 de una OC vencida hace 100 días → 60 < 83 →
               en la bandeja con la marca «Revisar OC».
    """
    monkeypatch.delenv('ROP_CICLO_NACIONAL_DIAS', raising=False)
    lt_de_este_mundo(monkeypatch)
    for ref, stock in (('CUBRE', 30), ('MUERTA', 30), ('MEDIA', 10)):
        _producto(db, ref, precio=1000)
        _diaria(db, ref)
        _stock(db, ref, stock)
    _oc_linea(db, 9001, 'CUBRE', 200, 100, 71)
    _oc_linea(db, 9002, 'MUERTA', 200, 250, 72)
    _oc_linea(db, 9003, 'MEDIA', 50, 100, 73)
    db.session.commit()
    from app.services.kardex_service import KardexService
    KardexService.reconstruir_stock_diario()
    from app.services import compras_bandeja
    return compras_bandeja.bandeja()


def _lineas(b):
    return {l['referencia']: l for p in b['proveedores'] for l in p['lineas']}


class TestLaBandejaNoCallaLaOCVieja:

    def test_lo_que_solo_cubre_una_oc_vencida_va_a_revisar(self, mundo):
        assert 'CUBRE' not in _lineas(mundo)
        r = {x['referencia']: x for x in mundo['revisar_oc']}
        assert set(r) == {'CUBRE'}
        assert r['CUBRE']['vencido'] == 200
        assert r['CUBRE']['oc_mas_vieja']['oc'] == '003-OC-71'
        assert r['CUBRE']['oc_mas_vieja']['dias_vencida'] == 100
        assert r['CUBRE']['si_no_llega_pedir'] > 0
        assert mundo['resumen']['revisar_oc'] == 1

    def test_la_oc_muerta_no_apaga_la_compra(self, mundo):
        l = _lineas(mundo)['MUERTA']
        assert l['urgencia'] == 'URGENTE'
        assert l['porque']['ya_pedido'] == 0
        assert l['porque']['no_contado_por_viejo'] == 200
        assert l['porque']['ocs_no_contadas'][0]['oc'] == '003-OC-72'

    def test_la_linea_con_oc_vencida_se_marca(self, mundo):
        l = _lineas(mundo)['MEDIA']
        assert l['revisar_oc'] is True
        assert l['porque']['ya_pedido_vencido'] == 50
        assert l['porque']['ya_pedido_ocs'][0]['oc'] == '003-OC-73'
        assert _lineas(mundo)['MUERTA']['revisar_oc'] is False

    def test_la_bandeja_declara_el_corte(self, mundo):
        yp = mundo['ya_pedido']
        assert yp['corte_dias'] == 180 and yp['no_contado_unidades'] == 200.0
        assert 'No se cuentan como «ya pedido» 200 u' in yp['nota_corte']

    def test_explicar_sku_lo_dice(self, app, db, mundo):
        from app.services.compras_bandeja import explicar_sku
        r = explicar_sku('CUBRE')
        assert r['motivo'] == 'REVISAR_OC' and 'orden de compra vencida' in r['texto']


class TestLaPantalla:

    def test_pinta_revisar_oc_y_el_porque(self, app, db, mundo, tmp_path):
        from tests.test_compras_bandeja_js import _json, _limpio, _node
        d = _json(mundo)
        media = _lineas(d)['MEDIA']
        muerta = _lineas(d)['MUERTA']
        out = _node(tmp_path, pintar={
            'html': f'cmpBandejaHtml({json.dumps(d)}, null)',
            'media': f'cmpPorQueHtml({json.dumps(media)})',
            'muerta': f'cmpPorQueHtml({json.dumps(muerta)})'})
        texto = _limpio(out['html'])
        assert 'Revisar órdenes viejas (1)' in texto
        assert 'Lo cubre la OC 003-OC-71, que debió llegar hace 100 días (200 u vencidas)' in texto
        assert 'Revisar OC' in texto
        assert 'No se cuentan como «ya pedido» 200 u' in texto
        m = _limpio(out['media'])
        assert 'De la OC 003-OC-73 (50 u, debió llegar hace 100 días)' in m
        assert 'De eso, 50 u son de órdenes vencidas hace meses: ¿van a llegar?' in m
        mu = _limpio(out['muerta'])
        assert 'No se cuentan 200 u de órdenes con más tiempo sin llegar (la OC 003-OC-72, de hace 250 días)' in mu


# ─────────────────────────────────────────────────────────────────────────────
# Lo que llegó: también las entradas de Siesa
# ─────────────────────────────────────────────────────────────────────────────

class TestLoQueLlego:

    def _linea(self, db, rowid, consec, *, parcial=None, cumplido=None, entrada=10,
               estado=2, nombre='PROVEEDOR X'):
        from app.models.compras_fuentes import OcLineaSiesa
        db.session.add(OcLineaSiesa(
            rowid_linea=rowid, co='003', tipo_docto='OC', consec_docto=consec,
            fecha_oc=_hoy() - timedelta(days=40), estado_oc=estado, proveedor_codigo='PX',
            proveedor_nombre=nombre, referencia=f'R{rowid}', bodega='NB1', factor=1,
            cant_pedida=20, cant_entrada=entrada, pendiente_base=20 - entrada,
            fecha_parcial=parcial, fecha_cumplido=cumplido, abierta=estado != 3))

    def test_las_entradas_de_siesa_aparecen(self, app, db):
        from app.services.compras_fuentes import llegadas_recientes
        hace = lambda d: datetime.combine(_hoy() - timedelta(days=d), datetime.min.time()) + timedelta(hours=10)
        self._linea(db, 1, 81, parcial=hace(3))                                  # entra
        self._linea(db, 2, 81, parcial=hace(3), entrada=5)                        # misma OC
        self._linea(db, 3, 82, parcial=hace(60), cumplido=hace(2), entrada=20, estado=3)  # completa
        self._linea(db, 4, 83, parcial=hace(45))                                  # fuera de la ventana
        self._linea(db, 5, 84, parcial=hace(1), estado=9)                         # anulada
        db.session.commit()
        r = llegadas_recientes(30)
        e = {x['oc']: x for x in r['entradas_siesa']}
        assert set(e) == {'003-OC-81', '003-OC-82'}
        assert e['003-OC-81']['unidades'] == 15.0 and e['003-OC-81']['parcial'] is True
        assert e['003-OC-82']['completa'] is True
        assert e['003-OC-81']['fuente'] == 'ENTRADA_SIESA'
        assert r['fuentes']['ENTRADA_SIESA']['ocs'] == 2

    def test_la_del_muelle_no_se_repite(self, app, db, almacen):
        from app.models.recepcion import EstadoRecepcion, RecepcionMercancia
        from app.services.compras_fuentes import llegadas_recientes
        ahora = datetime.utcnow()
        self._linea(db, 1, 90, parcial=ahora - timedelta(days=1))
        db.session.add(RecepcionMercancia(
            codigo='REC-1', numero_oc_siesa='OC-90', co_oc_siesa='003', tipo_docto_oc_siesa='OC',
            consec_docto_oc_siesa='90', almacen_id=almacen.id,
            estado=EstadoRecepcion.CONFIRMADA, fecha_confirmacion=ahora))
        db.session.commit()
        r = llegadas_recientes(30)
        assert r['entradas_siesa'] == [] and [x['codigo'] for x in r['recepciones']] == ['REC-1']

    def test_la_pantalla_la_pinta_escapada(self, app, db, tmp_path):
        from app.services import compras_bandeja
        from tests.test_compras_bandeja_js import MALO, _json, _limpio, _node
        self._linea(db, 1, 91, parcial=datetime.utcnow() - timedelta(days=1), nombre=MALO)
        db.session.commit()
        d = _json(compras_bandeja.lo_pedido())
        out = _node(tmp_path, pintar={'html': f'cmpLoPedidoHtml({json.dumps(d)})'})
        assert '<img' not in out['html']
        t = _limpio(out['html'])
        assert 'OC 003-OC-91' in t and 'u entradas' in t and 'parcial' in t
        assert 'Según las órdenes de compra sincronizadas con Siesa' in t


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete: lo ya pedido no se lee sin su parte vencida
# ─────────────────────────────────────────────────────────────────────────────

_LECTURA = 'en_transito'
_ANTIGUEDAD = 'en_transito_vencido'


def _claves_leidas(fn):
    """Claves string leídas en la función (subíndice o `.get`), sin contar las
    funciones anidadas, los docstrings ni los literales de un dict que se arma."""
    leidas = set()
    pila = [n for n in fn.body]
    while pila:
        n = pila.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load):
            k = n.slice
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                leidas.add(k.value)
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get' and n.args
                and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
            leidas.add(n.args[0].value)
        pila.extend(ast.iter_child_nodes(n))
    return leidas


def _funciones(arbol):
    out = []

    def visitar(nodo, pref):
        for h in ast.iter_child_nodes(nodo):
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f'{pref}{h.name}', h))
                visitar(h, f'{pref}{h.name}.')
            elif isinstance(h, ast.ClassDef):
                visitar(h, f'{pref}{h.name}.')
    visitar(arbol, '')
    return out


def _sin_antiguedad(src, archivo):
    return [(archivo, q) for q, fn in _funciones(ast.parse(src))
            if _LECTURA in (k := _claves_leidas(fn)) and _ANTIGUEDAD not in k]


def _archivos_de_compras():
    ps = [p for p in sorted((RAIZ / 'app').rglob('*.py'))
          if any(x in p.name for x in ('compras', 'armador', 'temporada'))]
    return ps


class TestLoYaPedidoConSuAntiguedad:

    def test_ninguna_funcion_de_compras_lo_lee_sin_su_parte_vencida(self):
        malos = []
        for p in _archivos_de_compras():
            malos += _sin_antiguedad(p.read_text(encoding='utf-8'), p.relative_to(RAIZ).as_posix())
        assert not malos, (f'{malos}: leen lo ya pedido (`en_transito`) de una fila del ROP '
                           'sin leer `en_transito_vencido`: una OC vencida hace meses se '
                           'mostraría o decidiría callada.')

    def test_piso(self):
        ps = _archivos_de_compras()
        assert len(ps) >= 8
        lectores = [(p, q) for p in ps for q, fn in _funciones(ast.parse(p.read_text(encoding='utf-8')))
                    if _LECTURA in _claves_leidas(fn)]
        assert any('compras_bandeja' in str(p) for p, _q in lectores), \
            'el escáner dejó de ver el porqué de la bandeja'

    def test_ve_la_lectura_sola(self):
        src = 'def f(fila):\n    return fila.get("en_transito")\n'
        assert _sin_antiguedad(src, 'x.py') == [('x.py', 'f')]
        src = 'def f(fila):\n    return fila["en_transito"] + 1\n'
        assert _sin_antiguedad(src, 'x.py') == [('x.py', 'f')]

    def test_no_marca_lo_sano(self):
        src = ('def f(fila):\n'
               '    """fila["en_transito"] en un docstring"""\n'
               '    return {"en_transito": 1, "otra": fila.get("en_transito"),\n'
               '            "v": fila.get("en_transito_vencido")}\n'
               'def g():\n'
               '    return {"en_transito": 3}\n')
        assert _sin_antiguedad(src, 'x.py') == []

    def test_la_hija_no_cuenta(self):
        src = ('def f(fila):\n'
               '    x = fila["en_transito"]\n'
               '    def h():\n'
               '        return fila["en_transito_vencido"]\n'
               '    return x\n')
        assert ('x.py', 'f') in _sin_antiguedad(src, 'x.py')
