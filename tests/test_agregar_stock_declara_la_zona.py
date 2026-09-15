"""Trinquete: **toda suma de stock en ubicación declara qué hace con la zona de averías.**

## El hueco que este archivo cierra

Ya existe `test_politica_vendible_unica.py`: prohíbe que el literal `'AVERIAS'`
aparezca fuera de `picking_service`. Atrapa a quien **copia mal** la política.

No atrapa a quien **nunca la consulta**. Y por ahí pasaron cuatro sitios:

  · `traslado_service._descontar_inventario_wms` — el descuento al despachar
    ordenaba por cantidad ascendente sin mirar zona, así que el bin de averías
    (pocas unidades) era el primero en vaciarse hacia otra bodega;
  · `traslado_service._get_stock_wms` — ofrecía lo averiado como disponible;
  · `Producto.stock_total` — sumaba averías en el número del catálogo, en la
    misma pantalla donde la alerta de mínimos sí las excluye;
  · `abc_service.poblar_stock_minimo_desde_abc` — anclaba el umbral en una base
    que incluye averías, contra una alerta que las excluye, mientras su propio
    docstring prometía que las dos bases eran la misma.

Ninguno usaba el literal. Todos eran invisibles para el trinquete anterior,
porque el defecto no era decir algo distinto: era **no decir nada**.

## Qué exige

Cada función de `app/` que sume `UbicacionProducto.cantidad` tiene que, o bien
referirse a la política (`filtro_ubicacion_vendible` / `filtro_ubicacion_averias`
/ `es_ubicacion_vendible`), o bien estar declarada abajo con **razón y fecha**.

Declarar no es eximir: la mayoría de las declaraciones dicen «acá sumar todas
las zonas es lo correcto, y por esto». Lo que el trinquete impide es que un
sitio nuevo se sume a la lista sin que nadie haya pensado la pregunta.

## Por AST, y en las dos direcciones

El scanner está parametrizado por directorio para que los meta-tests lo
ejerciten **a él**, no a un parche sobre él. Se prueba que dispara sobre una
suma nueva y muda, que NO dispara sobre una que declara, y que un piso de
sitios conocidos se rompe si el scanner deja de ver.
"""
import ast
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

POLITICA = ('filtro_ubicacion_vendible', 'filtro_ubicacion_averias',
            'es_ubicacion_vendible')

#: Sumas de stock que NO consultan la política, cada una con su razón y su
#: fecha. Clave: 'ruta/relativa.py::nombre_funcion'.
DECLARADAS = {
    'app/models/producto.py::stock_total': (
        '2026-09-14 · es el total FÍSICO a propósito: incluye averías porque '
        'eso es lo que hay. `stock_averiado` y `stock_vendible` viven al lado '
        'y `to_dict()` expone las tres, así que el número ya no viaja solo.'),
    'app/models/producto.py::stock_disponible': (
        '2026-09-14 · eje distinto (cantidad menos reservado y bloqueado), no '
        'el de zona. Abarca todas las zonas igual que `stock_total`.'),
    'app/routes/almacenes.py::layout_completo': (
        '2026-09-14 · pinta qué hay en cada hueco del layout. Ocultar el bin de '
        'averías sería el defecto, no el arreglo.'),
    'app/routes/compras.py::velocity_abc': (
        '2026-09-14 · la cifra que compara es de `MovimientoInventario`; el '
        'stock en ubicación entra solo como referencia de existencia actual.'),
    'app/routes/compras.py::cuarentena': (
        '2026-09-14 · lee SOLO la zona de averías, con el literal a mano. Está '
        'declarada también en `test_politica_vendible_unica` — migrarla a '
        '`filtro_ubicacion_averias()` borra las dos filas a la vez.'),
    'app/services/auditoria/inventario.py::_saldos_actuales': (
        '2026-09-14 · invariante de auditoría: tiene que ver TODO el stock, '
        'incluido el averiado. Un auditor con puntos ciegos no audita.'),
    'app/services/auditoria/inventario.py::_leer': (
        '2026-09-14 · misma razón que `_saldos_actuales`.'),
    'app/services/bloqueo_recompra_service.py::poblar_lista_inicial': (
        '2026-09-14 · el stock alimenta la decisión de bloquear un SKU para '
        'recompra. La función entera tiene un problema mayor y anterior a la '
        'zona: su regla de 365 días corre sobre menos de seis meses de '
        'historia. No se toca hasta resolver eso.'),
    'app/services/bloqueo_recompra_service.py::vista_capital_inmovilizado': (
        '2026-09-14 · multiplica el stock por `producto.costo_unitario`, campo '
        'que no existe en el modelo, así que el total sale siempre en 0. '
        'Filtrar la zona no cambiaría un cero.'),
    'app/services/inventario_siesa_service.py::_run_carga_inicial': (
        '2026-09-14 · es el escritor del sync: reparte lo que Siesa reporta '
        'para la bodega entera. La zona la deciden los bins, no esta suma.'),
    'app/services/inventario_siesa_service.py::_stock_wms_por_bodega': (
        '2026-09-14 · la reconciliación compara bodega contra bodega. Filtrar '
        'averías acá rompería el cuadre a propósito: Siesa las tiene dentro de '
        'la misma bodega mientras nadie las mueva a AV1.'),
    'app/services/inventario_siesa_service.py::_cuarentena_wms': (
        '2026-09-14 · lee SOLO la cuarentena, con el OR de tres señales '
        'legadas. Declarada también en `test_politica_vendible_unica`.'),
    'app/services/layout_service.py::_stock_activo': (
        '2026-09-14 · guard de remodulación: pregunta «¿este hueco tiene algo?» '
        'y la respuesta no puede depender de la zona. Quién puede moverse lo '
        'decide `_motivo_stock_no_reubicable`, que sí consulta la política.'),
    'app/services/ola_predictiva_service.py::pre_verificar_ola': (
        "2026-09-14 · selecciona positivamente `tipo_zona == 'PICKING'`, así "
        'que averías queda fuera por construcción, no por omisión.'),
}

#: Piso de sitios que el scanner tiene que seguir viendo. Si baja, el scanner
#: se rompió — y un scanner roto sale en verde igual que uno que no encuentra
#: nada.
PISO_SITIOS = 18


def _suma_de_stock(fn: ast.AST):
    """Devuelve la expresión sumada si la función suma stock en ubicación."""
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Attribute) and n.func.attr == 'sum')
            or (isinstance(n.func, ast.Name) and n.func.id == 'sum')
        ):
            for a in n.args:
                src = ast.unparse(a)
                if ('UbicacionProducto.cantidad' in src
                        or 'u.cantidad' in src or 'up.cantidad' in src):
                    return src
    return None


def _sitios(base: pathlib.Path = None):
    """Todas las funciones que suman stock, con su clave y si declaran."""
    base = base or (RAIZ / 'app')
    raiz_rel = base.parent if base.name == 'app' else base
    hallados = {}
    for f in sorted(base.rglob('*.py')):
        try:
            arbol = ast.parse(f.read_text(encoding='utf-8'))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for n in ast.walk(arbol):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if _suma_de_stock(n) is None:
                continue
            cuerpo = ast.unparse(n)
            clave = f'{f.relative_to(raiz_rel).as_posix()}::{n.name}'
            hallados[clave] = any(p in cuerpo for p in POLITICA)
    return hallados


# ── El trinquete ───────────────────────────────────────────────────────────

def test_ninguna_suma_de_stock_es_muda_sin_declararlo():
    mudas = {k for k, declara in _sitios().items() if not declara}
    sin_declarar = sorted(mudas - set(DECLARADAS))
    assert not sin_declarar, (
        'Estas funciones suman stock en ubicación y no dicen qué hacen con la '
        'zona de averías. O consultan la política '
        '(`filtro_ubicacion_vendible()` / `filtro_ubicacion_averias()` / '
        '`es_ubicacion_vendible()`), o se agregan a DECLARADAS con razón y '
        f'fecha:\n  ' + '\n  '.join(sin_declarar))


def test_la_lista_de_declaradas_solo_encoge():
    """Trinquete propiamente dicho. Una fila sobra en dos casos, y los dos
    obligan a borrarla:

      · el sitio ya consulta la política — se migró, la excepción cumplió;
      · el sitio ya no existe.

    Sin esto la lista solo crece, y una lista que solo crece no es un trinquete:
    es un permiso con formato de tabla."""
    sitios = _sitios()
    ya_migradas = sorted(k for k, declara in sitios.items()
                         if declara and k in DECLARADAS)
    desaparecidas = sorted(set(DECLARADAS) - set(sitios))
    assert not ya_migradas, (
        'Estas ya consultan la política y su fila en DECLARADAS sobra — '
        'borrala:\n  ' + '\n  '.join(ya_migradas))
    assert not desaparecidas, (
        'Declaraciones que no apuntan a ninguna función existente:\n  '
        + '\n  '.join(desaparecidas))


def test_toda_declaracion_tiene_fecha():
    sin_fecha = sorted(k for k, razon in DECLARADAS.items()
                       if not razon.strip()[:4].isdigit())
    assert not sin_fecha, (
        'Una excepción sin fecha no se puede caducar:\n  ' + '\n  '.join(sin_fecha))


def test_el_piso_de_sitios_se_sostiene():
    """Si el scanner deja de ver, este número cae y el trinquete queda en verde
    sobre nada."""
    total = len(_sitios())
    assert total >= PISO_SITIOS, (
        f'el scanner encontró {total} sitios, menos que el piso {PISO_SITIOS} — '
        'probablemente dejó de reconocer una forma de suma')


# ── Meta-tests: el scanner, en las dos direcciones ─────────────────────────

def _arbol_falso(tmp_path, nombre: str, codigo: str) -> pathlib.Path:
    app = tmp_path / 'app' / 'services'
    app.mkdir(parents=True, exist_ok=True)
    (app / nombre).write_text(codigo, encoding='utf-8')
    return tmp_path / 'app'


def test_el_scanner_ve_una_suma_orm_muda(tmp_path):
    base = _arbol_falso(tmp_path, 'nuevo.py', '''
from sqlalchemy import func
def total_por_producto():
    return db.session.query(func.sum(UbicacionProducto.cantidad)).all()
''')
    assert _sitios(base) == {'app/services/nuevo.py::total_por_producto': False}


def test_el_scanner_ve_una_suma_python_muda(tmp_path):
    base = _arbol_falso(tmp_path, 'nuevo.py', '''
def total(regs):
    return sum(u.cantidad for u in regs)
''')
    assert _sitios(base) == {'app/services/nuevo.py::total': False}


def test_el_scanner_reconoce_que_declara(tmp_path):
    """Dirección contraria: no puede marcar como muda una que sí consulta."""
    base = _arbol_falso(tmp_path, 'nuevo.py', '''
from sqlalchemy import func
from app.services.picking_service import filtro_ubicacion_vendible
def total_vendible():
    return (db.session.query(func.sum(UbicacionProducto.cantidad))
            .filter(filtro_ubicacion_vendible()).all())
''')
    assert _sitios(base) == {'app/services/nuevo.py::total_vendible': True}


def test_el_scanner_no_dispara_sobre_otras_sumas(tmp_path):
    """No puede confundir el kardex ni el packing con stock en ubicación."""
    base = _arbol_falso(tmp_path, 'nuevo.py', '''
from sqlalchemy import func
def demanda():
    return db.session.query(func.sum(KardexMovimiento.cantidad)).all()
def empacado(items):
    return sum(i.cantidad_real for i in items)
''')
    assert _sitios(base) == {}


def test_el_scanner_no_lee_docstrings(tmp_path):
    base = _arbol_falso(tmp_path, 'nuevo.py', '''
def documenta():
    """Esta función NO suma func.sum(UbicacionProducto.cantidad) — lo explica."""
    return 0
''')
    assert _sitios(base) == {}
