"""
Un lector no compara `tipo_documento` contra un valor que nadie escribe.

## El defecto (2026-09-24)

`metricas/venta_perdida.py` filtraba `EventoStockAgotado.tipo_documento ==
'PEDIDO'`. El evento copia el tipo de su `TareaPicking`, y el flujo real
escribe `'PEDIDO_SIESA'` (`routes/siesa.py`, `iniciar_despacho`). La métrica de
venta perdida daba ≈ 0 siempre. Es la **segunda** vez: PD1487 fue el mismo
literal en `mobile_service`.

## La clase y el trinquete

*Un literal de tipo de documento escrito a mano en un lector que no coincide con
el que escribe el flujo.* Este archivo lo mide por AST sobre `app/`:

1. **Descubre los escritores**, no los lista a mano: todo `tipo_documento=`
   literal (o `TipoDocumento.X`) pasado a un constructor o creador de tareas,
   y el `default=` de la columna. Así sale el conjunto de valores que cada
   modelo recibe de verdad.
2. **Recorre los lectores**: `==`, `!=`, `in`, `.in_()`, `.notin_()` y
   `filter_by(tipo_documento=...)` contra literales.
3. Exige que cada literal leído esté entre los que algún escritor de **ese
   modelo** produce. `EventoStockAgotado` hereda los de `TareaPicking` (los
   copia). Si el lector no se puede atribuir a un modelo (una instancia,
   `tarea.tipo_documento`), se usa la unión.

Leer a través de `TipoDocumento` (una constante, no un literal) queda fuera del
chequeo a propósito: el valor ya es el mismo objeto que usa el escritor.

Inventario de excepciones vacío.
"""
import ast
import pathlib

RAIZ = pathlib.Path(__file__).resolve().parents[1]

#: Modelos con columna `tipo_documento`, y de quién heredan sus valores.
MODELOS = ('TareaPicking', 'TareaPacking', 'EventoStockAgotado')
LINAJE = {'EventoStockAgotado': 'TareaPicking'}

#: Funciones que crean `TareaPicking` pasando `tipo_documento=` de largo.
CREADORES = {
    'crear_tareas': 'TareaPicking',
    'crear_tareas_con_compromiso': 'TareaPicking',
}

#: `(archivo, línea)` de lecturas que pueden comparar contra un valor que nadie
#: escribe, con su porqué. Vacío.
EXCEPCIONES: dict = {}


def _valores_constantes():
    from app.models.tipo_documento import TipoDocumento
    return {k: v for k, v in vars(TipoDocumento).items()
            if isinstance(v, str) and k.isupper()}


def _valor(nodo, constantes):
    """El string que representa `nodo`, o `None` si no es un valor fijo."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    if (isinstance(nodo, ast.Attribute) and isinstance(nodo.value, ast.Name)
            and nodo.value.id == 'TipoDocumento' and nodo.attr in constantes):
        return constantes[nodo.attr]
    return None


def _literales(nodo):
    """Solo LITERALES (lo que el trinquete fiscaliza), en escalar o colección."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return [nodo.value]
    if isinstance(nodo, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in nodo.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _alias(arbol):
    """`T = TareaPicking`, `X = aliased(TareaPicking)` → {'T': 'TareaPicking'}."""
    out = {}
    for n in ast.walk(arbol):
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            continue
        v = n.value
        if isinstance(v, ast.Call) and v.args:
            v = v.args[0]
        if isinstance(v, ast.Name) and v.id in MODELOS:
            out[n.targets[0].id] = v.id
    for n in ast.walk(arbol):
        if isinstance(n, ast.ImportFrom):
            for a in n.names:
                if a.name in MODELOS and a.asname:
                    out[a.asname] = a.name
    return out


def _base(nodo):
    """El nombre en la raíz de una cadena `A.b.c(...).d`."""
    while True:
        if isinstance(nodo, ast.Attribute):
            nodo = nodo.value
        elif isinstance(nodo, ast.Call):
            nodo = nodo.func
        elif isinstance(nodo, ast.Subscript):
            nodo = nodo.value
        else:
            return nodo.id if isinstance(nodo, ast.Name) else None


def _modelo(nombre, alias):
    m = alias.get(nombre, nombre)
    return LINAJE.get(m, m) if m in MODELOS else None


def _nombre_llamada(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None


def escritores(fuente: str, nombre='<src>', constantes=None):
    """[(modelo | None, valor, archivo, línea)] — lo que se ESCRIBE."""
    constantes = constantes if constantes is not None else _valores_constantes()
    arbol = ast.parse(fuente)
    alias = _alias(arbol)
    out = []
    for n in ast.walk(arbol):
        # default de la columna: `tipo_documento = db.Column(..., default='X')`
        if isinstance(n, ast.ClassDef) and n.name in MODELOS:
            for s in n.body:
                if (isinstance(s, ast.Assign) and len(s.targets) == 1
                        and isinstance(s.targets[0], ast.Name)
                        and s.targets[0].id == 'tipo_documento'
                        and isinstance(s.value, ast.Call)):
                    for kw in s.value.keywords:
                        if kw.arg == 'default':
                            v = _valor(kw.value, constantes)
                            if v is not None:
                                out.append((LINAJE.get(n.name, n.name), v, nombre, s.lineno))
        if not isinstance(n, ast.Call):
            continue
        llamada = _nombre_llamada(n)
        llamada = alias.get(llamada, llamada)
        if llamada in ('filter_by', 'update'):
            continue   # lectores (y `update` de otros campos filtrado por tipo)
        for kw in n.keywords:
            if kw.arg != 'tipo_documento':
                continue
            v = _valor(kw.value, constantes)
            if v is None:
                continue   # propagación: `tipo_documento=tarea.tipo_documento`
            modelo = (LINAJE.get(llamada, llamada) if llamada in MODELOS
                      else CREADORES.get(llamada))
            out.append((modelo, v, nombre, n.lineno))
    return out


def lectores(fuente: str, nombre='<src>'):
    """[(modelo | None, literal, archivo, línea)] — lo que se LEE contra un literal."""
    arbol = ast.parse(fuente)
    alias = _alias(arbol)
    out = []

    def _attr_tipo(nodo):
        for sub in ast.walk(nodo):
            if isinstance(sub, ast.Attribute) and sub.attr == 'tipo_documento':
                return sub
        return None

    for n in ast.walk(arbol):
        if isinstance(n, ast.Compare):
            lados = [n.left, *n.comparators]
            attr = next((a for a in (_attr_tipo(l) for l in lados
                                     if not _literales(l)) if a), None)
            if attr is None:
                continue
            modelo = _modelo(_base(attr.value), alias)
            for l in lados:
                for v in _literales(l):
                    out.append((modelo, v, nombre, n.lineno))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if (n.func.attr in ('in_', 'notin_')
                    and isinstance(n.func.value, ast.Attribute)
                    and n.func.value.attr == 'tipo_documento'):
                modelo = _modelo(_base(n.func.value.value), alias)
                for a in n.args:
                    for v in _literales(a):
                        out.append((modelo, v, nombre, n.lineno))
            elif n.func.attr == 'filter_by':
                for kw in n.keywords:
                    if kw.arg == 'tipo_documento':
                        modelo = _modelo(_base(n.func.value), alias)
                        for v in _literales(kw.value):
                            out.append((modelo, v, nombre, n.lineno))
    return out


def _archivos():
    return [p for p in sorted((RAIZ / 'app').rglob('*.py'))
            if 'static' not in p.parts]


def _todo():
    esc, lec = [], []
    constantes = _valores_constantes()
    for p in _archivos():
        rel = str(p.relative_to(RAIZ))
        src = p.read_text(encoding='utf-8')
        esc += escritores(src, rel, constantes)
        lec += lectores(src, rel)
    return esc, lec


def _escrito_por_modelo(esc):
    por = {}
    for modelo, v, *_ in esc:
        por.setdefault(modelo, set()).add(v)
    union = set().union(*por.values()) if por else set()
    return por, union


def test_todo_literal_leido_lo_escribe_alguien():
    esc, lec = _todo()
    por, union = _escrito_por_modelo(esc)
    malos = []
    for modelo, v, archivo, linea in lec:
        validos = por.get(modelo, set()) if modelo else union
        if v not in validos and (archivo, linea) not in EXCEPCIONES:
            malos.append(f'{archivo}:{linea} compara {modelo or "?"}.tipo_documento '
                         f'con {v!r}; los escritores de ese modelo producen '
                         f'{sorted(validos)}')
    assert not malos, (
        '\nUn lector compara `tipo_documento` contra un valor que ningún '
        'escritor produce — el filtro no coincide con ninguna fila real.\n'
        'Usá `app.models.tipo_documento.TipoDocumento` (y `es_venta()` para '
        '«no es traslado»).\n  ' + '\n  '.join(malos))


def test_las_excepciones_dicen_por_que():
    assert all(isinstance(v, str) and len(v) > 20 for v in EXCEPCIONES.values())


def test_el_flujo_de_venta_escribe_pedido_siesa():
    """El valor que el trinquete toma como verdad sale del escritor real."""
    esc, _ = _todo()
    assert ('TareaPicking', 'PEDIDO_SIESA') in {(m, v) for m, v, *_ in esc}, (
        'el escáner dejó de ver el escritor de `iniciar_despacho`')


class TestElDetectorMuerde:

    def test_ve_la_forma_rota(self):
        """El caso exacto de `venta_perdida.py`."""
        src = ("EventoStockAgotado.query.filter("
               "EventoStockAgotado.tipo_documento == 'PEDIDO')\n")
        assert lectores(src) == [('TareaPicking', 'PEDIDO', '<src>', 1)]

    def test_ve_las_otras_escrituras_de_la_misma_lectura(self):
        src = ("T = TareaPicking\n"
               "a = T.tipo_documento.in_(['PEDIDO', 'X'])\n"
               "b = TareaPacking.query.filter_by(tipo_documento='Y')\n"
               "c = t.tipo_documento in ('Z',)\n"
               "d = func.coalesce(TareaPicking.tipo_documento, '') != 'W'\n")
        vistos = {(m, v) for m, v, *_ in lectores(src)}
        assert vistos >= {('TareaPicking', 'PEDIDO'), ('TareaPicking', 'X'),
                          ('TareaPacking', 'Y'), (None, 'Z'),
                          ('TareaPicking', 'W')}

    def test_con_el_escritor_real_el_caso_roto_se_marca(self):
        """Lo que importa no es verlo: es que con los escritores reales el
        literal viejo NO pase."""
        esc, _ = _todo()
        por, _ = _escrito_por_modelo(esc)
        assert 'PEDIDO' not in por['TareaPicking'], (
            'algún escritor automático empezó a escribir PEDIDO en TareaPicking: '
            'revisar si es un flujo nuevo o un literal equivocado')

    def test_no_marca_lo_sano(self):
        src = ("a = TipoDocumento.es_venta(EventoStockAgotado.tipo_documento)\n"
               "b = TareaPicking.tipo_documento == TipoDocumento.TRASLADO\n"
               "c = TareaPicking.tipo_documento.in_(TipoDocumento.VENTA)\n"
               "d = TareaPicking.tipo_documento.is_(None)\n"
               "e = x.estado == 'PEDIDO'\n")
        assert lectores(src) == []

    def test_descubre_escritores(self):
        src = ("class TareaPacking(db.Model):\n"
               "    tipo_documento = db.Column(db.String(20), default='PEDIDO')\n"
               "TareaPicking(tipo_documento='TRASLADO')\n"
               "PickingService.crear_tareas(tipo_documento=TipoDocumento.PEDIDO_SIESA)\n"
               "Q.filter_by(tipo_documento='NO_ES_ESCRITOR')\n"
               "TareaPicking(tipo_documento=base.tipo_documento)\n"
               "from app.models.picking import TareaPicking as _TP\n"
               "_TP(tipo_documento='ALIAS')\n")
        vistos = {(m, v) for m, v, *_ in escritores(src)}
        assert vistos == {('TareaPacking', 'PEDIDO'), ('TareaPicking', 'TRASLADO'),
                          ('TareaPicking', 'PEDIDO_SIESA'), ('TareaPicking', 'ALIAS')}

    def test_pisos(self):
        """Si el escáner se desincroniza devuelve cero, y un cero se lee como
        «no hay nada que revisar»."""
        esc, lec = _todo()
        assert len(_archivos()) >= 100
        assert len(esc) >= 5, esc
        assert len(lec) >= 20, lec
