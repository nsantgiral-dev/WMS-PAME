"""
«Devolución contada» tiene UNA definición: `EstadoDevolucionCliente.CONTADAS`
(CONFIRMADA **y** FALTANTE_TOTAL). P1-13, 2026-09-25.

`senales_ruta.faltantes_de_retorno_de_recaudos` —el lote que leen
Liquidación, 💸 Fugas y el tablero de liquidación— filtraba
`estado == 'CONFIRMADA'`: una devolución contada en cero (el faltante más
grande) era invisible justo donde se liquida. La función de una sola
devolución (`faltante_de_retorno`) sí la veía: la misma pregunta, dos
respuestas.

Trinquete (AST, sobre `app/`): ninguna comparación del estado de una
devolución contra un literal de «contada» (`'CONFIRMADA'`,
`'FALTANTE_TOTAL'`). Se escribe con el vocabulario del modelo, donde
`CONTADAS` está a la vista; quien quiera solo CONFIRMADA lo escribe
(`E.CONFIRMADA`) y queda leíble por qué.

Lo que NO ve: una comparación contra un literal con otra forma (una variable
intermedia con el texto). En los módulos que no importan el modelo de
devoluciones no mira (`'CONFIRMADA'` es también un estado de recepción).
"""
import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'

_LITERALES = {'CONFIRMADA', 'FALTANTE_TOTAL'}
_MARCAS_DEL_MODELO = ('DevolucionCliente', 'EstadoDevolucionCliente', 'devolucion_ruta')

#: Sitio → por qué compara contra el literal. **Solo encoge.**
DECLARADAS = {
    'app/services/auditoria/devoluciones.py::una_linea_de_factura_no_se_devuelve_de_mas':
        'DEV-10 suma lo devuelto por línea: una FALTANTE_TOTAL devuelve 0 y no cambia la suma',
    'app/services/auditoria/devoluciones.py::ningun_reingreso_es_vendible_sin_nc_aprobada':
        'DEV-11 mira lo que reingresó: una FALTANTE_TOTAL no reingresó nada',
    'app/services/analitica_recorrido.py::_evaluar':
        'recorrido: «devolución del cliente» es lo que VOLVIÓ; la contada en cero es '
        'faltante de retorno (senales_ruta). Revisar con el frente de analítica',
}


def _literales(nodo):
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return {nodo.value}
    if isinstance(nodo, (ast.Tuple, ast.List, ast.Set)):
        return {e.value for e in nodo.elts if isinstance(e, ast.Constant)
                and isinstance(e.value, str)}
    return set()


def _es_estado(nodo):
    return isinstance(nodo, ast.Attribute) and nodo.attr == 'estado'


def sitios(fuentes):
    out = set()
    for archivo, src in fuentes.items():
        if not any(m in src for m in _MARCAS_DEL_MODELO):
            continue
        arbol = ast.parse(src)
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for n in ast.walk(fn):
                lit = set()
                if isinstance(n, ast.Compare) and (_es_estado(n.left) or any(
                        _es_estado(c) for c in n.comparators)):
                    for c in [n.left] + n.comparators:
                        lit |= _literales(c)
                elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                      and n.func.attr in ('in_', 'notin_') and _es_estado(n.func.value)):
                    for a in n.args:
                        lit |= _literales(a)
                if lit & _LITERALES:
                    out.add(f'{archivo}::{fn.name}')
    return out


def _fuentes():
    return {str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8') for p in APP.rglob('*.py')}


class TestContadaTieneUnaDefinicion:

    def test_nadie_compara_contra_el_literal(self):
        malos = sorted(sitios(_fuentes()) - set(DECLARADAS))
        assert not malos, (
            f'\n{malos}: comparan el estado de una devolución contra '
            "'CONFIRMADA'/'FALTANTE_TOTAL'. «Contada» es `EstadoDevolucionCliente.CONTADAS`: "
            'filtrar solo CONFIRMADA dejó el faltante total invisible en Liquidación.')

    def test_el_inventario_solo_encoge_y_dice_por_que(self):
        s = sitios(_fuentes())
        assert set(DECLARADAS) <= s, f'ya no comparan (sacar del inventario): {set(DECLARADAS) - s}'
        assert len(DECLARADAS) <= 3
        assert all(len(v) > 30 for v in DECLARADAS.values())

    def test_meta(self):
        src = ("from x import DevolucionCliente\n"
               "def a(d):\n    return d.estado == 'CONFIRMADA'\n"
               "def b(q):\n    return q.filter(DevolucionCliente.estado.in_(('CONFIRMADA', 'X')))\n"
               "def c(d):\n    return d.estado in E.CONTADAS\n"
               "def e(d):\n    return d.estado not in ('FALTANTE_TOTAL',)\n"
               "def f(d):\n    '''d.estado == 'CONFIRMADA' '''\n    return d.nombre == 'CONFIRMADA'\n")
        assert sitios({'x.py': src}) == {'x.py::a', 'x.py::b', 'x.py::e'}
        assert sitios({'y.py': "def a(r):\n    return r.estado == 'CONFIRMADA'\n"}) == set(), (
            'un módulo sin el modelo de devoluciones (recepción) no se mira')

    def test_piso(self):
        assert len(sitios(_fuentes())) >= 2, 'el escáner dejó de ver lo declarado: ¿roto?'

    def test_el_lote_ve_el_faltante_total(self, db, almacen):
        from app.services import senales_ruta
        from tests.test_e2e_total_20260925 import _faltante_total
        _t, r, _d = _faltante_total(db, almacen)
        assert senales_ruta.faltantes_de_retorno_de_recaudos([r.id])[r.id]['faltante_unidades'] == 8
