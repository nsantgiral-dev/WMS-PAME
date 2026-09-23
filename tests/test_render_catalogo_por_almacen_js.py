"""El catálogo de Stock, EJECUTADO: el desglose por bodega llega al DOM.

Sin bodega elegida, el total suma todos los almacenes. Pintado solo, un 446
se lee como «hay 446 en el CD» cuando eran 110 en el CD y 336 en tiendas
(2026-09-23). El desglose debajo del total es lo que evita esa lectura.

Mismo arnés que `test_render_catalogo_averias_js`: el `app.js` real en Node.
El arnés crea todo elemento que se pida y sin valor, así que el selector de
bodega queda vacío = «Todas las bodegas», que es el caso que pinta el desglose.
"""
from tests.test_render_reconciliacion_js import _render


def _catalogo(tmp_path, productos):
    salida = _render(
        tmp_path, 'cargarCatalogo',
        {'/api/productos/': {'total': len(productos), 'paginas': 1,
                             'productos': productos}},
        ['lista-productos'])
    return salida['lista-productos']


def _p(**kw):
    base = {'id': 1, 'nombre': 'Resma Carta', 'codigo': 'PAPELSP9218',
            'codigo_siesa': 'PAPELSP9218', 'clasificacion_abc': 'A',
            'unidad_medida': 'UND', 'stock_total': 446,
            'stock_averiado': 0, 'stock_vendible': 446}
    base.update(kw)
    return base


def test_sin_bodega_el_total_lleva_su_desglose(tmp_path):
    html = _catalogo(tmp_path, [_p(stock_por_almacen=[
        {'almacen': 'NB1', 'stock_total': 110},
        {'almacen': 'NC1', 'stock_total': 186},
        {'almacen': 'NS1', 'stock_total': 150}])])
    assert 'NB1 110 · NC1 186 · NS1 150' in html, html


def test_sin_desglose_no_inventa_la_linea(tmp_path):
    """Una API vieja, o un producto sin stock en ningún almacén: nada extra."""
    html = _catalogo(tmp_path, [_p(stock_total=0, stock_vendible=0)])
    assert '#93c5fd' not in html, html


def test_el_nombre_de_bodega_va_escapado(tmp_path):
    html = _catalogo(tmp_path, [_p(stock_por_almacen=[
        {'almacen': '<img src=x onerror=alert(1)>', 'stock_total': 5}])])
    assert '<img src=x' not in html, html
    assert '&lt;img' in html, html
