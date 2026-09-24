"""
Trinquete de la retención de cartera: **ningún despacho a crédito sale sin
pasar por la política** (`cartera_service`).

La clase no es «G2 no se llamaba en el closer»: es *una emisión de
remisión o factura, o un inicio de despacho, que no pregunta a cartera*. Hoy
hay tres puertas a la emisión (el DLQ, el carril admin de despacho parcial y
los dos carriles de recuperación de la FE) y una al inicio. Mañana alguien
agrega la quinta.

Por AST sobre `app/` (sin los módulos del gateway, que son los conectores):

1. Toda función que llama `trigger_comprometer_pedido` (244328) o
   `trigger_despacho` (142945) llama `compuerta_emision` **en la misma
   función**.
2. Toda función que llama `trigger_factura_desde_remision` (142943) o
   `trigger_factura` (238925, muerto) llama `compuerta_emision` o
   `cabecera_para_factura` (la RM ya existe: solo cabe la conversión a contado).
3. Toda función que crea el picking de un pedido de Siesa
   (`crear_tareas_con_compromiso`) llama `compuerta_inicio`, o solo la llaman
   funciones que llaman `compuerta_inicio` (el cuerpo corre dentro del `with`).
4. Toda función que encola `DESPACHO_F470` está en el inventario, y el helper
   del closer solo lo llama una función que llama `compuerta_cierre`.

Los inventarios solo encogen y cada entrada dice por qué. Meta-tests: las
escrituras que debe ver (`connekta.x(`, `self.core.x(`, `gw.x(`), lo sano que
no (docstring, comentario, función anidada), y pisos.
"""
import ast
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'

#: Los conectores mismos: definen o delegan los triggers, no deciden un despacho.
_GATEWAY = {'connekta_gateway.py', 'connekta_facturacion_gateway.py'}

EMISION_FUERTE = {'trigger_comprometer_pedido', 'trigger_despacho'}
EMISION_FE = {'trigger_factura_desde_remision', 'trigger_factura'}
INICIO = {'crear_tareas_con_compromiso'}

#: Emisiones sin compuerta, con su porqué. Solo puede encoger.
EMISIONES_SIN_COMPUERTA = {}

#: Quién encola DESPACHO_F470 y por qué no es una puerta sin guardia.
ENCOLAN_F470 = {
    ('services/closing/pedido_closer.py', '_encolar_job'):
        'helper: su único llamador (ejecutar_cierre) llama compuerta_cierre — lo verifica '
        'test_el_helper_del_closer_solo_lo_llama_quien_pasa_la_compuerta',
    ('services/packing_service.py', '_cerrar_packing_pedido_legacy'):
        'sin ningún llamador («mantenida para referencia interna»); '
        'test_el_cierre_legacy_sigue_sin_llamador lo exige',
}


def _funciones(arbol):
    """Funciones de primer nivel y métodos — una anidada NO es la misma función."""
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n


def _llamadas_propias(fn):
    """Nombres llamados en el cuerpo de `fn`, sin entrar a funciones anidadas."""
    nombres = []

    def _visitar(nodo):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(hijo, ast.Call):
                f = hijo.func
                nombres.append(f.attr if isinstance(f, ast.Attribute)
                               else getattr(f, 'id', None))
            _visitar(hijo)
    _visitar(fn)
    return nombres


def _encola_f470(fn):
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and getattr(n.func, 'attr', None) == 'encolar':
            for k in n.keywords:
                if k.arg == 'tipo' and isinstance(k.value, ast.Constant) \
                        and k.value.value == 'DESPACHO_F470':
                    return True
    return False


def escanear(fuentes: dict):
    """`{ruta_relativa: código}` → hallazgos por regla. Separado para los
    meta-tests."""
    out = {'fuerte': [], 'fe': [], 'inicio': [], 'encola': [], 'emisiones': 0,
           'inicios': 0}
    for ruta, codigo in fuentes.items():
        arbol = ast.parse(codigo)
        for fn in _funciones(arbol):
            llamadas = set(_llamadas_propias(fn))
            fuerte = llamadas & EMISION_FUERTE
            fe = llamadas & EMISION_FE
            if fuerte or fe:
                out['emisiones'] += 1
            if fuerte and 'compuerta_emision' not in llamadas:
                out['fuerte'].append((ruta, fn.name))
            if fe and not ({'compuerta_emision', 'cabecera_para_factura'} & llamadas):
                out['fe'].append((ruta, fn.name))
            if llamadas & INICIO:
                out['inicios'] += 1
                if 'compuerta_inicio' not in llamadas:
                    # Un helper vale si TODO el que lo llama pasa la compuerta
                    # (el cuerpo de `iniciar_despacho` corre dentro del `with`).
                    llamadores = [g for g in _funciones(arbol)
                                  if fn.name in _llamadas_propias(g)]
                    if not llamadores or not all('compuerta_inicio' in _llamadas_propias(g)
                                                 for g in llamadores):
                        out['inicio'].append((ruta, fn.name))
            if _encola_f470(fn):
                out['encola'].append((ruta, fn.name))
    return out


def _fuentes_app():
    return {str(p.relative_to(APP)): p.read_text(encoding='utf-8')
            for p in APP.rglob('*.py') if p.name not in _GATEWAY}


@pytest.fixture(scope='module')
def hallazgos():
    return escanear(_fuentes_app())


class TestNingunDespachoACreditoSinCartera:

    def test_244328_y_142945_pasan_por_la_compuerta_de_emision(self, hallazgos):
        fuera = [h for h in hallazgos['fuerte'] if h not in EMISIONES_SIN_COMPUERTA]
        assert not fuera, (f'Emiten remisión/compromiso sin preguntar a cartera: {fuera}. '
                           'Llamá cartera_service.compuerta_emision(tarea, cabecera) antes.')

    def test_la_factura_lleva_al_menos_la_condicion_de_cartera(self, hallazgos):
        fuera = [h for h in hallazgos['fe'] if h not in EMISIONES_SIN_COMPUERTA]
        assert not fuera, f'Emiten la FE sin cabecera_para_factura/compuerta_emision: {fuera}'

    def test_el_picking_de_un_pedido_pasa_por_la_compuerta_de_inicio(self, hallazgos):
        assert not hallazgos['inicio'], hallazgos['inicio']

    def test_quien_encola_despacho_f470_esta_declarado(self, hallazgos):
        assert set(hallazgos['encola']) == set(ENCOLAN_F470), (
            f'Encolan DESPACHO_F470: {sorted(hallazgos["encola"])}; declarados: '
            f'{sorted(ENCOLAN_F470)}. Una puerta nueva al despacho tiene que pasar por '
            'cartera_service.compuerta_cierre (o compuerta_emision en el ejecutor).')

    def test_el_helper_del_closer_solo_lo_llama_quien_pasa_la_compuerta(self):
        arbol = ast.parse((APP / 'services/closing/pedido_closer.py').read_text(encoding='utf-8'))
        llamadores = [fn for fn in _funciones(arbol)
                      if '_encolar_job' in _llamadas_propias(fn)]
        assert [f.name for f in llamadores] == ['ejecutar_cierre']
        assert 'compuerta_cierre' in _llamadas_propias(llamadores[0])

    def test_el_cierre_legacy_sigue_sin_llamador(self):
        usos = [str(p) for p in APP.rglob('*.py')
                if '_cerrar_packing_pedido_legacy(' in p.read_text(encoding='utf-8')
                and p.name != 'packing_service.py']
        codigo = (APP / 'services/packing_service.py').read_text(encoding='utf-8')
        propios = sum(1 for fn in _funciones(ast.parse(codigo))
                      if '_cerrar_packing_pedido_legacy' in _llamadas_propias(fn))
        assert not usos and propios == 0, 'el cierre legacy ganó un llamador: necesita G2'


class TestElInventarioNoCrece:

    def test_solo_encoge(self):
        assert len(EMISIONES_SIN_COMPUERTA) <= 0

    def test_cada_entrada_dice_por_que(self):
        for k, v in {**EMISIONES_SIN_COMPUERTA, **ENCOLAN_F470}.items():
            assert isinstance(v, str) and len(v) > 30, k


class TestElDetectorMuerde:

    def test_ve_las_tres_escrituras_de_la_emision(self):
        codigo = '''
def a(t):
    connekta.trigger_despacho(1)
def b(t):
    self.core.trigger_comprometer_pedido(1)
def c(t):
    gw.trigger_factura_desde_remision(1)
'''
        h = escanear({'x.py': codigo})
        assert h['fuerte'] == [('x.py', 'a'), ('x.py', 'b')]
        assert h['fe'] == [('x.py', 'c')]

    def test_no_marca_lo_sano_ni_docstrings_ni_comentarios(self):
        codigo = '''
def a(t, cab):
    """connekta.trigger_despacho(1) en el docstring"""
    # connekta.trigger_comprometer_pedido(1)
    cab = cartera_service.compuerta_emision(t, cab)
    connekta.trigger_despacho(1)
def b(t, cab):
    cab = cartera_service.cabecera_para_factura(t, cab)
    connekta.trigger_factura_desde_remision(1)
'''
        h = escanear({'x.py': codigo})
        assert h['fuerte'] == [] and h['fe'] == []

    def test_la_compuerta_en_una_funcion_anidada_no_cuenta(self):
        codigo = '''
def a(t, cab):
    def _interna():
        compuerta_emision(t, cab)
    connekta.trigger_despacho(1)
'''
        assert escanear({'x.py': codigo})['fuerte'] == [('x.py', 'a')]

    def test_un_helper_de_inicio_vale_solo_si_su_llamador_pasa_la_compuerta(self):
        bien = '''
def ruta():
    with compuerta_inicio(1) as p:
        return _cuerpo()
def _cuerpo():
    PickingService.crear_tareas_con_compromiso(1)
'''
        mal = bien + '''
def otra_puerta():
    return _cuerpo()
'''
        assert escanear({'x.py': bien})['inicio'] == []
        assert escanear({'x.py': mal})['inicio'] == [('x.py', '_cuerpo')]

    def test_ve_el_inicio_y_el_encolado(self):
        codigo = '''
def a():
    PickingService.crear_tareas_con_compromiso(1)
def b():
    SiesaJob.encolar(tipo='DESPACHO_F470', payload={})
'''
        h = escanear({'x.py': codigo})
        assert h['inicio'] == [('x.py', 'a')] and h['encola'] == [('x.py', 'b')]

    def test_pisos(self, hallazgos):
        assert hallazgos['emisiones'] >= 3, 'el escáner dejó de ver las emisiones'
        assert hallazgos['inicios'] >= 1, 'el escáner dejó de ver el inicio del despacho'
