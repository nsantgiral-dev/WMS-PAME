"""Trinquete: **toda suma de stock en ubicación declara qué hace con la zona de averías.**

## El hueco que cierra

`test_politica_vendible_unica.py` prohíbe que el literal `'AVERIAS'` aparezca
fuera de `picking_service`: atrapa a quien **copia mal** la política. No atrapa a
quien **nunca la consulta**, y por ahí pasaron cuatro sitios —el descuento de
traslados, el catálogo de «Pedir desde», `Producto.stock_total` y la base del
umbral de mínimos—. Ninguno usaba el literal: simplemente no hacían la pregunta.

## Qué mide EXACTAMENTE, y qué no

Este archivo existió un día en una versión que medía dos proxies, y las dos
estaban rotas el mismo día que se escribió. Queda anotado porque la forma del
error importa más que el error:

  · **la suma** se detectaba buscando las subcadenas `u.cantidad` / `up.cantidad`
    en el texto del argumento. `app/routes/reposicion.py` suma
    `sum((i.cantidad or 0) for i in inventarios)` sobre filas de
    `UbicacionProducto` — invisible, porque la variable se llama `i`. Y al
    revés, `Producto.stock_disponible` entraba en la lista por accidente:
    `u.cantidad_disponible()` **contiene** `u.cantidad`.
  · **la declaración** se detectaba con `nombre in ast.unparse(fn)`, que incluye
    docstrings e imports sin usar. Una función que mencionara la política en su
    docstring y no la aplicara salía del radar en silencio — que es la dirección
    peligrosa, porque un falso «muda» molesta y un falso «declara» tapa.

Hoy mide, por AST:

  · **suma de stock** = una llamada a `sum` / `.sum()` en cuyo argumento hay un
    atributo llamado `cantidad` exacto. Si el objeto es un modelo que sabemos
    que NO es stock en ubicación (`OTROS_MODELOS`), se descarta; si no se puede
    resolver el tipo —`i.cantidad`, `reg.cantidad`— **se incluye**. Sobre-incluir
    cuesta una fila de declaración; sub-incluir cuesta un defecto invisible.
  · **declara** = la política se **llama**, resolviendo los alias de cualquier
    `from ... import X as Y` del módulo o de la función.

Lo que sigue sin ver, declarado para que nadie lea cobertura total donde no la
hay: acumuladores (`t += fila.cantidad`), `functools.reduce`, SQL crudo en
`db.text`, y sumas construidas en una variable intermedia. Cada una tiene su
meta-test en rojo — abajo, marcadas como `xfail`: son huecos conocidos, no
sorpresas.

## Una fila que se fue, y es el trinquete funcionando

`_run_carga_inicial` estuvo declarada con la razón «netea las averías contra el
bucket vendible». El 2026-09-16, al conectarse el aviso a Siesa, esa decisión
dejó de ser correcta —con las unidades en AV1, Siesa ya no las cuenta en la
bodega— y la función pasó a consultar la política. El trinquete exigió borrar
su fila. Escribir la razón fue lo que hizo visible que había que cambiarla.

## Declarar no es eximir

La mayoría de las filas dicen «acá sumar todas las zonas es lo correcto, y por
esto». Lo que el trinquete impide es que un sitio nuevo entre sin que nadie haya
hecho la pregunta. Y **la lista solo puede encoger**: un sitio que empieza a
consultar la política obliga a borrar su fila.
"""
import ast
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

POLITICA = {'filtro_ubicacion_vendible', 'filtro_ubicacion_averias',
            'es_ubicacion_vendible'}

#: Modelos cuyo `.cantidad` NO es stock en una ubicación. Se reconocen por
#: nombre porque es lo único que el AST puede resolver sin ejecutar nada.
OTROS_MODELOS = {
    'KardexMovimiento', 'MovimientoInventario', 'ItemPacking', 'ItemEnTransito',
    'TareaPicking', 'ItemRecepcion', 'EventoStockAgotado', 'LPN',
    'TareaReposicion', 'ItemSolicitudTraslado', 'LineaDevolucionCliente',
}

#: Sumas de stock que NO consultan la política, con razón y fecha.
#: Clave: 'ruta/relativa.py::nombre_funcion'.
DECLARADAS = {
    'app/models/producto.py::stock_total': (
        '2026-09-14 · es el total FÍSICO a propósito: incluye averías porque eso '
        'es lo que hay. El arreglo no fue filtrarlo —un «total» que no es el '
        'total miente en la otra dirección— sino declarar la composición: '
        '`stock_averiado` y `stock_vendible` viven al lado y `to_dict()` expone '
        'las tres, así que el número ya no viaja solo.'),
    'app/routes/almacenes.py::layout_completo': (
        '2026-09-14 · pinta qué hay en cada hueco del layout. Ocultar el bin de '
        'averías sería el defecto, no el arreglo.'),
    'app/routes/compras.py::cuarentena': (
        '2026-09-14 · lee SOLO la zona de averías, con el literal a mano. '
        'Declarada también en `test_politica_vendible_unica` — migrarla a '
        '`filtro_ubicacion_averias()` borra las dos filas a la vez.'),
    'app/routes/compras.py::velocity_abc': (
        '2026-09-16 · selecciona positivamente `Ubicacion.tipo_zona == '
        "'PICKING'` (`app/routes/compras.py:110`), así que averías queda fuera "
        'por construcción. CORREGIDA: la razón anterior decía que el stock era '
        '«solo referencia», y es falso — alimenta `dias_stock` y de ahí '
        '`es_alerta` (`:132`, `:135`). La exención se sostiene por el filtro, '
        'no por ser decorativa.'),
    'app/routes/reposicion.py::listar_ubicaciones_picking': (
        "2026-09-16 · filtra `Ubicacion.tipo_zona == 'PICKING'` "
        '(`app/routes/reposicion.py:308`): averías fuera por construcción. '
        'AGREGADA al corregir el scanner — la versión anterior no la veía '
        'porque la variable del comprehension se llama `i` y no `u`.'),
    'app/services/auditoria/inventario.py::_saldos_actuales': (
        '2026-09-14 · invariante de auditoría: tiene que ver TODO el stock, '
        'incluido el averiado. Un auditor con puntos ciegos no audita.'),
    'app/services/bloqueo_recompra_service.py::poblar_lista_inicial': (
        '2026-09-16 · la zona SÍ importa acá y la exención es un aplazamiento, '
        'no una absolución: `stock_map` suma todas las zonas y decide qué SKU '
        'entra a la lista de bloqueo, así que un SKU con todo su stock en '
        'averías se bloquearía por «tener stock» que no se puede vender — la '
        'misma clase que el defecto del Armador. No se toca porque la función '
        'tiene un problema anterior y mayor: su corte de 365 días '
        '(`bloqueo_recompra_service.py:39`) corre sobre el histórico que haya, '
        'y cuánto hay NO está medido en este repo.'),
    'app/services/bloqueo_recompra_service.py::vista_capital_inmovilizado': (
        '2026-09-16 · el capital sale siempre 0 porque multiplica por '
        '`producto.costo_unitario`, campo que no existe en `Producto` — el '
        '`hasattr` da False. Pero CORREGIDA: la función también publica '
        "`'stock'` por SKU, que sí se pinta y sí es ciego a la zona. La razón "
        'anterior hablaba solo del dinero y callaba el número que viaja.'),
    'app/services/inventario_siesa_service.py::_cuarentena_wms': (
        '2026-09-14 · lee SOLO la cuarentena, con el OR de tres señales '
        'legadas. Declarada también en `test_politica_vendible_unica`.'),
    'app/services/inventario_siesa_service.py::_stock_wms_por_bodega': (
        '2026-09-14 · la reconciliación compara bodega contra bodega. Filtrar '
        'averías acá rompería el cuadre a propósito: Siesa las tiene dentro de '
        'la misma bodega mientras nadie las mueva a AV1.'),
    'app/services/layout_service.py::_stock_activo': (
        '2026-09-14 · guard de remodulación: pregunta «¿este hueco tiene algo?» '
        'y la respuesta no puede depender de la zona. Quién puede moverse lo '
        'decide `_motivo_stock_no_reubicable`, que sí consulta la política.'),
    'app/services/ola_predictiva_service.py::pre_verificar_ola': (
        "2026-09-14 · selecciona positivamente `tipo_zona == 'PICKING'` "
        '(`ola_predictiva_service.py:62`), así que averías queda fuera por '
        'construcción, no por omisión.'),
}

#: Piso EXACTO, no holgado. Una versión anterior lo dejó tres por debajo del
#: real y una mutación plausible —saltarse dos archivos— se comía la holgura
#: dejando los cuatro tests en verde.
PISO_SITIOS = 20


# ── El scanner ─────────────────────────────────────────────────────────────

def _cantidad_de_stock(nodo):
    """La expresión sumada, si es `cantidad` de algo que puede ser stock."""
    for n in ast.walk(nodo):
        if isinstance(n, ast.Attribute) and n.attr == 'cantidad':
            v = n.value
            if isinstance(v, ast.Name) and v.id in OTROS_MODELOS:
                continue
            return ast.unparse(n)
    return None


def _suma_de_stock(fn):
    for n in ast.walk(fn):
        es_sum = (
            (isinstance(n.func, ast.Attribute) and n.func.attr == 'sum')
            or (isinstance(n.func, ast.Name) and n.func.id == 'sum')
        ) if isinstance(n, ast.Call) else False
        if es_sum:
            for a in n.args:
                expr = _cantidad_de_stock(a)
                if expr:
                    return expr
    return None


def _alias_de_politica(*ambitos):
    """Canónicos + alias de cualquier `from ... import X as Y` en el ámbito."""
    nombres = set(POLITICA)
    for amb in ambitos:
        for n in ast.walk(amb):
            if isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name in POLITICA and a.asname:
                        nombres.add(a.asname)
    return nombres


def _llama_politica(fn, nombres):
    """La política se LLAMA. Un import sin usar o un docstring no cuentan."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            nom = (f.id if isinstance(f, ast.Name)
                   else f.attr if isinstance(f, ast.Attribute) else None)
            if nom in nombres:
                return True
    return False


def _sitios(base: pathlib.Path = None):
    base = base or (RAIZ / 'app')
    raiz_rel = base.parent if base.name == 'app' else base
    hallados = {}
    for f in sorted(base.rglob('*.py')):
        try:
            arbol = ast.parse(f.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        del_modulo = _alias_de_politica(arbol)
        # Un closure no se cuenta aparte de la función que lo contiene.
        anidadas = {
            id(h)
            for n in ast.walk(arbol)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            for h in ast.walk(n)
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)) and h is not n
        }
        for n in ast.walk(arbol):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if id(n) in anidadas or _suma_de_stock(n) is None:
                continue
            clave = f'{f.relative_to(raiz_rel).as_posix()}::{n.name}'
            hallados[clave] = _llama_politica(
                n, del_modulo | _alias_de_politica(n))
    return hallados


# ── El trinquete ───────────────────────────────────────────────────────────

def test_ninguna_suma_de_stock_es_muda_sin_declararlo():
    mudas = {k for k, declara in _sitios().items() if not declara}
    sin_declarar = sorted(mudas - set(DECLARADAS))
    assert not sin_declarar, (
        'Estas funciones suman stock en ubicación y no dicen qué hacen con la '
        'zona de averías. O llaman a la política, o se agregan a DECLARADAS '
        'con razón y fecha:\n  ' + '\n  '.join(sin_declarar))


def test_la_lista_de_declaradas_solo_encoge():
    """Una fila sobra en dos casos, y los dos obligan a borrarla: el sitio ya
    consulta la política, o el sitio ya no existe. Sin esto la lista solo crece,
    y una lista que solo crece no es un trinquete: es un permiso tabulado."""
    sitios = _sitios()
    ya_migradas = sorted(k for k, d in sitios.items() if d and k in DECLARADAS)
    desaparecidas = sorted(set(DECLARADAS) - set(sitios))
    assert not ya_migradas, (
        'Ya consultan la política; su fila sobra:\n  ' + '\n  '.join(ya_migradas))
    assert not desaparecidas, (
        'No apuntan a ninguna función existente:\n  ' + '\n  '.join(desaparecidas))


def test_toda_declaracion_tiene_fecha_y_razon():
    malas = sorted(
        k for k, r in DECLARADAS.items()
        if not r.strip().startswith('2026-') or len(r.strip()) < 60)
    assert not malas, (
        'Una excepción sin fecha ISO o sin razón de verdad no se puede '
        'caducar ni discutir:\n  ' + '\n  '.join(malas))


def test_el_piso_de_sitios_se_sostiene():
    total = len(_sitios())
    assert total >= PISO_SITIOS, (
        f'el scanner encontró {total}, menos que el piso {PISO_SITIOS} — dejó '
        'de reconocer alguna forma de suma')


# ── Meta-tests: el scanner real, en las dos direcciones ────────────────────

def _arbol(tmp_path, codigo, sub='services', nombre='nuevo.py'):
    d = tmp_path / 'app' / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / nombre).write_text(codigo, encoding='utf-8')
    return tmp_path / 'app'


@pytest.mark.parametrize('cuerpo,clave', [
    ('return db.session.query(func.sum(UbicacionProducto.cantidad)).all()', 'orm'),
    ('return sum(u.cantidad for u in regs)', 'python-u'),
    ('return sum((i.cantidad or 0) for i in inventarios)', 'python-i'),
    ('return sum(reg.cantidad for reg in regs)', 'python-reg'),
    ('return db.session.query(func.coalesce(func.sum(UbicacionProducto.cantidad), 0)).scalar()', 'coalesce'),
    ('return q.with_entities(func.sum(UbicacionProducto.cantidad)).scalar()', 'with_entities'),
])
def test_el_scanner_ve_la_suma(tmp_path, cuerpo, clave):
    base = _arbol(tmp_path, f'def total():\n    {cuerpo}\n')
    assert _sitios(base) == {'app/services/nuevo.py::total': False}, clave


@pytest.mark.parametrize('cuerpo', [
    'return db.session.query(func.sum(KardexMovimiento.cantidad)).all()',
    'return sum(i.cantidad_real for i in items)',
    'return sum(u.cantidad_disponible() for u in self.ubicaciones)',
    'return sum(t.cantidad_recogida for t in tareas)',
    'return db.session.query(func.sum(ItemPacking.cantidad)).all()',
])
def test_el_scanner_no_dispara_sobre_otras_sumas(tmp_path, cuerpo):
    base = _arbol(tmp_path, f'def total():\n    {cuerpo}\n')
    assert _sitios(base) == {}


def test_el_scanner_no_lee_comentarios_ni_docstrings_como_suma(tmp_path):
    base = _arbol(tmp_path, '''
def documenta():
    """No suma func.sum(UbicacionProducto.cantidad) — lo explica."""
    # tampoco acá: sum(u.cantidad for u in regs)
    return 0
''')
    assert _sitios(base) == {}


# ── La mitad que exime: sus negativos, que es donde estaba el hueco ───────

def test_declarar_exige_llamar_no_mencionar(tmp_path):
    """Un docstring que nombra la política NO declara. Era el hueco: un falso
    «declara» saca el sitio del radar sin dejar rastro en DECLARADAS."""
    base = _arbol(tmp_path, '''
from sqlalchemy import func
def total():
    """OJO: esta suma NO usa filtro_ubicacion_vendible()."""
    return db.session.query(func.sum(UbicacionProducto.cantidad)).all()
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': False}


def test_un_import_sin_usar_no_declara(tmp_path):
    base = _arbol(tmp_path, '''
from sqlalchemy import func
from app.services.picking_service import filtro_ubicacion_vendible
def total():
    return db.session.query(func.sum(UbicacionProducto.cantidad)).all()
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': False}


def test_llamar_la_politica_si_declara(tmp_path):
    base = _arbol(tmp_path, '''
from sqlalchemy import func
from app.services.picking_service import filtro_ubicacion_vendible
def total():
    return (db.session.query(func.sum(UbicacionProducto.cantidad))
            .filter(filtro_ubicacion_vendible()).all())
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': True}


def test_un_alias_del_import_tambien_declara(tmp_path):
    """`_get_stock_wms` importa la política con alias. Sin resolverlo, el
    scanner la marcaba muda y empujaba a declarar un sitio que sí pregunta."""
    base = _arbol(tmp_path, '''
from sqlalchemy import func
from app.services.picking_service import filtro_ubicacion_vendible as _vend
def total():
    return (db.session.query(func.sum(UbicacionProducto.cantidad))
            .filter(_vend()).all())
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': True}


def test_un_closure_no_se_cuenta_aparte_de_su_contenedora(tmp_path):
    base = _arbol(tmp_path, '''
from sqlalchemy import func
def contenedora():
    def _leer():
        return db.session.query(func.sum(UbicacionProducto.cantidad)).all()
    return _leer()
''')
    assert _sitios(base) == {'app/services/nuevo.py::contenedora': False}


def test_el_scanner_alcanza_modelos_y_rutas(tmp_path):
    """5 de las 13 filas viven fuera de `app/services/`."""
    base = _arbol(tmp_path, 'def total(regs):\n    return sum(u.cantidad for u in regs)\n',
                  sub='routes', nombre='x.py')
    assert _sitios(base) == {'app/routes/x.py::total': False}


# ── Huecos CONOCIDOS del scanner, declarados en rojo ──────────────────────
# No son sorpresas: son las formas que este detector NO sabe ver. Cada xfail
# es una promesa de que, si alguien las enseña, el test se pone verde y hay
# que quitarle el marcador — no una excusa para no mirarlas.

@pytest.mark.xfail(reason='hueco conocido: acumulador, no llamada a sum()',
                   strict=True)
def test_hueco_acumulador(tmp_path):
    base = _arbol(tmp_path, '''
def total(regs):
    t = 0
    for u in regs:
        t += u.cantidad
    return t
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': False}


@pytest.mark.xfail(reason='hueco conocido: SQL crudo en db.text', strict=True)
def test_hueco_sql_crudo(tmp_path):
    base = _arbol(tmp_path, '''
def total():
    return db.session.execute(
        db.text('SELECT SUM(cantidad) FROM ubicaciones_productos')).scalar()
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': False}


@pytest.mark.xfail(reason='hueco conocido: la suma pasa por una variable',
                   strict=True)
def test_hueco_variable_intermedia(tmp_path):
    base = _arbol(tmp_path, '''
def total(regs):
    cantidades = [u.cantidad for u in regs]
    return sum(cantidades)
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': False}
