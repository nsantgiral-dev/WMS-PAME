"""Un consumidor que lee una clave que ningún productor escribe.

Dos instancias encontradas el 2026-09-21, las dos silenciosas:

  · `app/routes/traslados.py` leía `siesa_total_rows` del diccionario que
    produce `TrasladoService.get_stock_disponible` — que escribe
    `siesa_total_productos`. `.get()` devolvía `None`, así que el campo de
    diagnóstico salía SIEMPRE nulo, al lado de dos enteros. Un `null` entre
    números se lee como «Siesa no devolvió filas»: la confusión entre ausencia
    y vacío, justo en el campo que existe para explicar por qué faltan ítems.

  · `flota/api/conductor.py` publicaba `preventivo_vencido` y ninguna pantalla
    lo leía. Ver `test_el_conductor_ve_lo_vencido_js.py`.

La clase es la misma que el `KeyError` de `mi-turno` del mismo día, en su
versión que no revienta — y por eso es peor de diagnosticar: no hay traza, solo
un campo que siempre dice lo mismo.
"""
import ast
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def _claves_del_debug():
    """Las claves que el bloque `_debug` de stock-disponible lee del caché."""
    fuente = (RAIZ / 'app' / 'routes' / 'traslados.py').read_text(encoding='utf-8')
    arbol = ast.parse(fuente)
    leidas = set()
    for nodo in ast.walk(arbol):
        # `cacheado.get('x')`
        if (isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == 'get'
                and isinstance(nodo.func.value, ast.Name)
                and nodo.func.value.id == 'cacheado'
                and nodo.args
                and isinstance(nodo.args[0], ast.Constant)):
            leidas.add(nodo.args[0].value)
    return leidas


def _claves_del_servicio():
    """Las que `get_stock_disponible` y su fallback realmente escriben."""
    fuente = (RAIZ / 'app' / 'services'
              / 'traslado_service.py').read_text(encoding='utf-8')
    arbol = ast.parse(fuente)
    escritas = set()
    for nombre in ('get_stock_disponible', '_get_stock_wms'):
        fn = next((n for n in ast.walk(arbol)
                   if isinstance(n, ast.FunctionDef) and n.name == nombre), None)
        if fn is None:
            continue
        for nodo in ast.walk(fn):
            if isinstance(nodo, ast.Dict):
                escritas |= {k.value for k in nodo.keys
                             if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    return escritas


def test_toda_clave_del_debug_la_escribe_alguien():
    leidas, escritas = _claves_del_debug(), _claves_del_servicio()
    fantasmas = sorted(leidas - escritas)
    assert not fantasmas, (
        f'{fantasmas} se lee del caché de stock y NINGÚN productor la escribe. '
        f'`.get()` devuelve None y el campo sale siempre nulo — que en un '
        f'bloque de diagnóstico se lee como «Siesa no devolvió nada».')


def test_el_detector_no_esta_ciego():
    """Si el bloque cambia de forma o la variable de nombre, el test de arriba
    compararía conjuntos vacíos y pasaría diciendo que está todo bien."""
    leidas, escritas = _claves_del_debug(), _claves_del_servicio()
    assert len(leidas) >= 5, (
        f'solo se detectaron {len(leidas)} lecturas del caché: el lector se '
        f'desincronizó del código')
    assert len(escritas) >= 5, (
        f'solo se detectaron {len(escritas)} claves producidas')
    assert 'siesa_con_stock' in leidas and 'siesa_con_stock' in escritas, (
        'el detector no encuentra ni el par que sí coincide')


def test_sin_mapeo_no_resta_a_ciegas():
    """El fallback `_get_stock_wms` no devuelve `siesa_con_stock`, así que la
    resta daba `0 - total` = NEGATIVO cuando Siesa se caía. Un número negativo
    ahí no significa «sobran mapeos»: significa que no hay con qué comparar."""
    # Por AST y no por ventana de texto.
    #
    # La primera versión leía 500 caracteres desde `'sin_mapeo'` y buscaba
    # «is not None» ahí adentro. Una mutación que restauraba la resta ciega
    # quedaba VERDE: la frase seguía apareciendo en la línea siguiente, la de
    # `sin_mapeo_nota`. El guard medía la vecindad, no la expresión.
    fuente = (RAIZ / 'app' / 'routes' / 'traslados.py').read_text(encoding='utf-8')
    arbol = ast.parse(fuente)

    valor = None
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Dict):
            for k, v in zip(nodo.keys, nodo.values):
                if isinstance(k, ast.Constant) and k.value == 'sin_mapeo':
                    valor = v
    assert valor is not None, 'no se encontró la clave `sin_mapeo` en el debug'

    assert isinstance(valor, ast.IfExp), (
        'el valor de `sin_mapeo` es una resta directa. Cuando Siesa no '
        'contesta, el fallback `_get_stock_wms` no devuelve `siesa_con_stock` '
        'y la resta da `0 - total` = NEGATIVO — que no significa «sobran '
        'mapeos» sino «no hay con qué comparar». Tiene que ser condicional.')
    condicion = ast.unparse(valor.test)
    assert 'siesa_con_stock' in condicion and 'is not None' in condicion, (
        f'la condición de `sin_mapeo` es {condicion!r}: no comprueba que haya '
        f'con qué restar')

    assert 'sin_mapeo_nota' in fuente, (
        'se quitó la nota que explica por qué `sin_mapeo` puede ser None')
