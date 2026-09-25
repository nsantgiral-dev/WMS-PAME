"""Trinquetes de «lo que viene» y del lead time — todo por AST.

**Los únicos de estas dos clases.** El frente del motor («Compras: los números
correctos») y el de las fuentes (m046compras) escribieron cada uno el suyo
(`armador_service.en_camino` / `lead_time` con `LECTURAS_PERMITIDAS`, y éstos);
al integrarlos quedó UNA función por pregunta —`compras_fuentes.en_camino` y
`compras_fuentes.lead_time`— y UN trinquete por clase, que junta lo que veía
cada uno.

Tres clases, cada una con su inventario que solo encoge, meta-tests (lo que
debe ver, lo sano que no) y un piso:

1. **Nadie calcula «en camino» fuera de `compras_fuentes`.** Lo que se detecta:
   restar cantidades de una OC (`f421_cant_pedida` − `f421_cant_entrada`, o los
   atributos `cant_pedida*`/`cant_entrada*`), leer `ItemEnTransito.cantidad`,
   leer `pendiente_base`, **sumar** (`sum(`/`func.sum(`) en una función que lee
   `ItemEnTransito` (la suma de lo que viene, escrita por instancia), o leer
   `ESTADOS_EN_CAMINO` (decidir qué estados cuentan como «viene»). Así nació el
   término que valía 0: el armador sumaba `ItemEnTransito.cantidad` por su
   cuenta, sobre una tabla sin escritor. Leer o escribir `ItemEnTransito` para
   operarlo (cargar un contenedor, contar ítems en G5) no es calcular.
2. **Nadie decide un lead time fuera de `lead_time`.** Leer los defaults
   (`LT_NACIONAL_DIAS`…), sus variables de entorno (`ROP_LT_NACIONAL_DIAS`,
   `ROP_SIGMA_LT_NACIONAL`), la observación de un contenedor
   (`.lead_time_real`) o restar una `fecha_oc` es elegir un lead time.
3. **El sync de OCs no cierra con paginación incompleta, y nada borra una
   línea de OC.** El cierre (`abierta = False`, `NO_APARECE_EN_ABIERTAS`) vive
   bajo `if completa`, y ningún `.delete(` toca `OcLineaSiesa`.
"""
import ast
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PAQUETES = ('app', 'flota')

DUENO = 'app/services/compras_fuentes.py'
#: Donde se DEFINEN las columnas y la propiedad de observación: no calculan.
DEFINICIONES = {'app/models/compras_fuentes.py', 'app/models/importacion.py'}

_CAMPOS_CANT_OC = {'f421_cant_pedida', 'f421_cant_entrada',
                   'f421_cant_pedida_base', 'f421_cant_entrada_base'}
_ATRIB_CANT_OC = {'cant_pedida', 'cant_entrada', 'cant_pedida_base', 'cant_entrada_base'}
_DEFAULTS_LT = {'LT_NACIONAL_DIAS', 'LT_CHINA_DIAS', 'SIGMA_LT_NACIONAL', 'SIGMA_LT_CHINA',
                'ENV_LT_NACIONAL', 'ENV_SIGMA_LT_NACIONAL'}
#: Las variables de entorno del lead time: leerlas es decidirlo.
_ENV_LT = {'ROP_LT_NACIONAL_DIAS', 'ROP_SIGMA_LT_NACIONAL'}
#: Qué estados cuentan como «viene»: es parte de la política de `en_camino`.
_ESTADOS_EN_CAMINO = {'ESTADOS_EN_CAMINO'}

#: Sitios fuera del dueño que restan cantidades de una OC. **No son «en
#: camino»**: contestan cuánto falta RECIBIR de una OC puntual en el muelle. Solo
#: encoge.
EN_CAMINO_FUERA = {
    ('app/routes/siesa.py', 'ordenes_compra'):
        'Muelle de recepción ciega: cuánto falta recibir de cada línea de la OC que '
        'se está escaneando. No alimenta la posición de compra.',
    ('app/routes/siesa.py', 'debug_oc'):
        'Diagnóstico de una OC puntual para admin (qué ve el muelle); no alimenta '
        'ninguna decisión de compra.',
    ('app/services/tienda_oc_service.py', 'TiendaOCService.listar_ocs'):
        'Recepción de OCs en la tienda: lo pendiente de recibir en esa bodega, no '
        '«en camino» para decidir una compra.',
}
#: Sitios fuera del dueño que eligen un lead time. Vacío: no crece sin decisión.
LEAD_TIME_FUERA = {}


# ──────────────────────────────────────────────────────────────────────────────
# Escáner
# ──────────────────────────────────────────────────────────────────────────────

_ANIDADAS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _docstrings(arbol):
    docs = set()
    for n in ast.walk(arbol):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            c = getattr(n, 'body', [])
            if c and isinstance(c[0], ast.Expr) and isinstance(c[0].value, ast.Constant):
                docs.add(id(c[0].value))
    return docs


def _propios(raices):
    nodos, pila = [], list(raices)
    while pila:
        n = pila.pop()
        if isinstance(n, _ANIDADAS):
            continue
        nodos.append(n)
        pila.extend(ast.iter_child_nodes(n))
    return nodos


def _defs(nodo, prefijo=''):
    """(qualname, def) de toda función, con el prefijo de su clase o función."""
    for h in ast.iter_child_nodes(nodo):
        if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
            q = prefijo + h.name
            yield q, h
            yield from _defs(h, q + '.')
        elif isinstance(h, ast.ClassDef):
            yield from _defs(h, prefijo + h.name + '.')
        else:
            yield from _defs(h, prefijo)


def _unidades(arbol):
    """{qualname: nodos propios}; el código de módulo es '<modulo>'. Una
    función anidada es su propia unidad: lo suyo no se le atribuye a la madre."""
    out = {'<modulo>': _propios(arbol.body)}
    for q, fn in _defs(arbol):
        out[q] = _propios(list(fn.body) + list(fn.args.defaults) + list(fn.decorator_list))
    return out


def _menciona_cantidad_oc(nodos, docs):
    for n in nodos:
        if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value in _CAMPOS_CANT_OC and id(n) not in docs):
            return True
        if isinstance(n, ast.Attribute) and n.attr in _ATRIB_CANT_OC \
                and isinstance(n.ctx, ast.Load):
            return True
    return False


def _lee_item_en_transito(nodos):
    return any(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
               and n.id == 'ItemEnTransito' for n in nodos)


def _es_suma(n):
    """`sum(...)` o `func.sum(...)` / `sa.func.sum(...)`."""
    if not isinstance(n, ast.Call):
        return False
    f = n.func
    return ((isinstance(f, ast.Name) and f.id == 'sum')
            or (isinstance(f, ast.Attribute) and f.attr == 'sum'))


def escanear_en_camino(fuente: str) -> dict:
    """`{qualname: [líneas]}` de lo que calcula «en camino»."""
    arbol = ast.parse(fuente)
    docs = _docstrings(arbol)
    out = {}
    for q, nodos in _unidades(arbol).items():
        lineas = []
        if _menciona_cantidad_oc(nodos, docs):
            lineas += [n.lineno for n in nodos
                       if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)]
        if _lee_item_en_transito(nodos):
            lineas += [n.lineno for n in nodos if _es_suma(n)]
        for n in nodos:
            if ((isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                 and n.id in _ESTADOS_EN_CAMINO)
                    or (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                        and n.attr in _ESTADOS_EN_CAMINO)):
                lineas.append(n.lineno)
            if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                    and n.attr == 'cantidad' and isinstance(n.value, ast.Name)
                    and n.value.id == 'ItemEnTransito'):
                lineas.append(n.lineno)
            if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                    and n.attr == 'pendiente_base'):
                lineas.append(n.lineno)
        if lineas:
            out[q] = sorted(set(lineas))
    return out


def escanear_lead_time(fuente: str) -> dict:
    """`{qualname: [líneas]}` de lo que elige un lead time."""
    arbol = ast.parse(fuente)
    docs = _docstrings(arbol)
    out = {}
    for q, nodos in _unidades(arbol).items():
        lineas = []
        for n in nodos:
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and n.value in _ENV_LT and id(n) not in docs):
                lineas.append(n.lineno)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in _DEFAULTS_LT:
                lineas.append(n.lineno)
            if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                    and n.attr in _DEFAULTS_LT | {'lead_time_real'}):
                lineas.append(n.lineno)
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub) \
                    and 'fecha_oc' in ast.unparse(n):
                lineas.append(n.lineno)
        if lineas:
            out[q] = sorted(set(lineas))
    return out


def _repo(escaner, excluir):
    out = {}
    archivos = 0
    for base in PAQUETES:
        for p in sorted((RAIZ / base).rglob('*.py')):
            rel = p.relative_to(RAIZ).as_posix()
            archivos += 1
            if rel in excluir:
                continue
            for q, ls in escaner(p.read_text(encoding='utf-8')).items():
                out[(rel, q)] = ls
    return out, archivos


# ══════════════════════════════════════════════════════════════════════════════
# 1 · en camino
# ══════════════════════════════════════════════════════════════════════════════

class TestNadieCalculaEnCaminoPorFuera:

    def test_ningun_sitio_nuevo(self):
        hallados, archivos = _repo(escanear_en_camino, {DUENO} | DEFINICIONES)
        assert archivos >= 200, 'el escáner no está recorriendo el repo'
        nuevos = {k: v for k, v in hallados.items() if k not in EN_CAMINO_FUERA}
        assert not nuevos, (
            'Calculan «en camino» (o un pendiente de OC) fuera de '
            '`compras_fuentes.en_camino` / `pendiente_de_linea`:\n'
            + '\n'.join(f'  · {a}::{q} (líneas {ls})' for (a, q), ls in nuevos.items()))

    def test_el_inventario_solo_encoge(self):
        hallados, _ = _repo(escanear_en_camino, {DUENO} | DEFINICIONES)
        fantasmas = [k for k in EN_CAMINO_FUERA if k not in hallados]
        assert not fantasmas, f'Ya no calculan: bórralos del inventario: {fantasmas}'

    def test_cada_excepcion_dice_por_que(self):
        for k, razon in EN_CAMINO_FUERA.items():
            assert razon and len(razon) > 40, k

    def test_piso_el_dueno_si_calcula(self):
        """Si el escáner se rompe devuelve cero y todo pasa: el dueño tiene que
        aparecer."""
        hallados = escanear_en_camino((RAIZ / DUENO).read_text(encoding='utf-8'))
        assert {'pendiente_de_linea', '_pendiente_de_ocs_abiertas',
                '_contenedores_en_camino'} <= set(hallados)

    def test_piso_el_armador_no_calcula(self):
        """El Armador (ROP, contenedor, posición) es el consumidor más grande:
        que no aparezca es la mitad de la propiedad, no un cero tranquilo."""
        src = (RAIZ / 'app/services/armador_service.py').read_text(encoding='utf-8')
        assert 'ItemEnTransito' in src and 'compras_fuentes' in src
        assert escanear_en_camino(src) == {}


class TestElEscanerDeEnCaminoMuerde:

    def test_ve_la_resta_con_los_campos_de_siesa(self):
        src = "def f(r):\n    return float(r['f421_cant_pedida']) - float(r['f421_cant_entrada'])\n"
        assert escanear_en_camino(src) == {'f': [2]}

    def test_ve_la_resta_por_atributos(self):
        src = "def f(l):\n    x = l.cant_pedida_base\n    return x - l.cant_entrada_base\n"
        assert 'f' in escanear_en_camino(src)

    def test_ve_la_suma_de_item_en_transito(self):
        src = "def f():\n    return db.session.query(func.sum(ItemEnTransito.cantidad)).all()\n"
        assert 'f' in escanear_en_camino(src)

    def test_ve_leer_el_pendiente(self):
        src = "def f(l):\n    return l.pendiente_base\n"
        assert 'f' in escanear_en_camino(src)

    def test_la_funcion_anidada_es_su_propia_unidad(self):
        src = "def f():\n    def g(l):\n        return l.pendiente_base\n    return g\n"
        assert set(escanear_en_camino(src)) == {'f.g'}

    def test_ve_la_suma_por_instancia(self):
        """La forma del motor: sumar lo que viene recorriendo los ítems."""
        src = ("def f():\n"
               "    items = ItemEnTransito.query.filter_by(estado='NAVEGANDO').all()\n"
               "    return sum(i.cantidad for i in items)\n")
        assert escanear_en_camino(src) == {'f': [3]}

    def test_ve_decidir_que_estados_vienen(self):
        src = 'def f(c):\n    return c.estado in armador.ESTADOS_EN_CAMINO\n'
        assert 'f' in escanear_en_camino(src)
        assert 'g' in escanear_en_camino('def g(c):\n    return c.estado in ESTADOS_EN_CAMINO\n')

    def test_no_marca_operar_el_contenedor(self):
        """Cargar, listar o contar ítems (G5) no es calcular lo que viene."""
        src = ('def cargar(p, c):\n'
               '    db.session.add(ItemEnTransito(producto_id=p, contenedor_id=c, cantidad=3))\n'
               '    for i in ItemEnTransito.query.filter_by(contenedor_id=c).all():\n'
               '        i.estado = "RECIBIDO"\n'
               '    return ItemEnTransito.query.count()\n'
               'def otra(xs):\n'
               '    return sum(xs)\n')
        assert escanear_en_camino(src) == {}

    def test_no_marca_lo_sano(self):
        src = (
            'def f(l, pendiente, p):\n'
            '    """Resta f421_cant_pedida - f421_cant_entrada en el docstring."""\n'
            '    # l.pendiente_base en un comentario\n'
            '    l.pendiente_base = pendiente\n'                       # escribir no es calcular
            '    i = ItemEnTransito(cantidad=3)\n'                     # crear no es sumar
            '    return p.cantidad - 1\n'                              # otra cantidad
        )
        assert escanear_en_camino(src) == {}


# ══════════════════════════════════════════════════════════════════════════════
# 2 · lead time
# ══════════════════════════════════════════════════════════════════════════════

class TestNadieDecideLeadTimePorFuera:

    def test_ningun_sitio_nuevo(self):
        hallados, archivos = _repo(escanear_lead_time, {DUENO} | DEFINICIONES)
        assert archivos >= 200
        nuevos = {k: v for k, v in hallados.items() if k not in LEAD_TIME_FUERA}
        assert not nuevos, (
            'Eligen un lead time fuera de `compras_fuentes.lead_time`:\n'
            + '\n'.join(f'  · {a}::{q} (líneas {ls})' for (a, q), ls in nuevos.items()))

    def test_el_inventario_solo_encoge(self):
        hallados, _ = _repo(escanear_lead_time, {DUENO} | DEFINICIONES)
        assert all(k in hallados for k in LEAD_TIME_FUERA)

    def test_piso_el_dueno_si_decide(self):
        hallados = escanear_lead_time((RAIZ / DUENO).read_text(encoding='utf-8'))
        assert {'lead_time', 'observaciones_lead_time', 'default_lead_time'} <= set(hallados)

    def test_el_armador_solo_reexporta(self):
        """Importar los defaults para compatibilidad no es usarlos."""
        src = (RAIZ / 'app/services/armador_service.py').read_text(encoding='utf-8')
        assert 'LT_NACIONAL_DIAS' in src and escanear_lead_time(src) == {}


class TestElEscanerDeLeadTimeMuerde:

    def test_ve_el_default(self):
        assert 'f' in escanear_lead_time('def f(es):\n    return LT_CHINA_DIAS if es else 5\n')

    def test_ve_el_default_por_modulo(self):
        assert 'f' in escanear_lead_time('def f():\n    return armador.SIGMA_LT_NACIONAL\n')

    def test_ve_la_observacion_del_contenedor(self):
        assert 'f' in escanear_lead_time('def f(c):\n    return c.lead_time_real\n')

    def test_ve_la_variable_de_entorno(self):
        src = "def f():\n    return float(os.getenv('ROP_LT_NACIONAL_DIAS', 5))\n"
        assert escanear_lead_time(src) == {'f': [2]}

    def test_no_ve_la_variable_en_el_docstring(self):
        src = 'def f():\n    """Configurable con ROP_LT_NACIONAL_DIAS."""\n    return 1\n'
        assert escanear_lead_time(src) == {}

    def test_ve_una_resta_de_fechas_de_oc(self):
        src = 'def f(c):\n    return (c.fecha_recepcion_cedi - c.fecha_oc).days\n'
        assert 'f' in escanear_lead_time(src)

    def test_no_marca_lo_sano(self):
        src = ('from x import LT_CHINA_DIAS\n'
               'def f(c, LT=1):\n'
               '    """LT_CHINA_DIAS y c.fecha_oc - 1 en el docstring."""\n'
               '    return c.fecha_oc\n')
        assert escanear_lead_time(src) == {}


# ══════════════════════════════════════════════════════════════════════════════
# 3 · el sync no borra ni cierra con paginación incompleta
# ══════════════════════════════════════════════════════════════════════════════

def _cierres_fuera_de_completa(fuente: str) -> list:
    """Líneas que cierran una línea de OC (`.abierta = False` o
    `.motivo_cierre = MOTIVO_NO_APARECE`) sin estar bajo un `if completa`."""
    arbol = ast.parse(fuente)
    padres = {}
    for n in ast.walk(arbol):
        for h in ast.iter_child_nodes(n):
            padres[h] = n

    def bajo_completa(n):
        while n in padres:
            p = padres[n]
            if isinstance(p, ast.If) and n in p.body and isinstance(p.test, ast.Name) \
                    and p.test.id == 'completa':
                return True
            n = p
        return False

    malas = []
    for n in ast.walk(arbol):
        if not isinstance(n, ast.Assign):
            continue
        for t in n.targets:
            if not isinstance(t, ast.Attribute):
                continue
            cierra = ((t.attr == 'abierta' and isinstance(n.value, ast.Constant)
                       and n.value.value is False)
                      or (t.attr == 'motivo_cierre' and 'NO_APARECE' in ast.unparse(n.value)))
            if cierra and not bajo_completa(n):
                malas.append(n.lineno)
    return malas


def _funcion(fuente, nombre):
    for n in ast.walk(ast.parse(fuente)):
        if isinstance(n, ast.FunctionDef) and n.name == nombre:
            return ast.unparse(n)
    raise AssertionError(f'{nombre} no existe')


class TestElSyncNoCierraSinVerTodo:

    def test_el_cierre_vive_bajo_if_completa(self):
        src = (RAIZ / 'app/services/compras_oc_sync.py').read_text(encoding='utf-8')
        cuerpo = _funcion(src, 'sincronizar_ocs')
        assert 'NO_APARECE' in cuerpo, 'piso: el cierre existe'
        assert _cierres_fuera_de_completa(cuerpo) == []

    def test_nada_borra_una_linea_de_oc(self):
        for base in PAQUETES:
            for p in (RAIZ / base).rglob('*.py'):
                arbol = ast.parse(p.read_text(encoding='utf-8'))
                for n in ast.walk(arbol):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                            and n.func.attr == 'delete':
                        texto = ast.unparse(n)
                        assert 'OcLineaSiesa' not in texto and 'oc_linea' not in texto, (
                            f'{p.relative_to(RAIZ)}:{n.lineno} borra líneas de OC')
                if p.name == 'compras_oc_sync.py':
                    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                                   and n.func.attr == 'delete' for n in ast.walk(arbol))

    def test_ve_un_cierre_suelto(self):
        src = ('def s(completa, l):\n'
               '    if completa:\n        l.abierta = False\n'
               '    l.abierta = False\n'
               '    l.motivo_cierre = MOTIVO_NO_APARECE\n')
        assert _cierres_fuera_de_completa(src) == [4, 5]

    def test_no_confunde_el_else_ni_otro_if(self):
        src = ('def s(completa, ok, l):\n'
               '    if completa:\n        pass\n    else:\n        l.abierta = False\n'
               '    if ok:\n        l.abierta = False\n'
               '    l.abierta = True\n')
        assert _cierres_fuera_de_completa(src) == [5, 7]
