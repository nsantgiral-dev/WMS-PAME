"""El catálogo del PWA, EJECUTADO — no leído.

Prueba que la cifra de averías llegue al DOM y que el color del total deje de
decir «verde, hay stock» cuando todo lo que hay está roto.

Mismo criterio que `test_render_reconciliacion_js`: se ejecuta el `app.js` real
en Node con un DOM mínimo y `fetch` sembrado, y se mira lo que quedó pintado.
Un detector de texto sobre el archivo no sirve acá — la cifra se pinta dentro
de un template anidado en `${...}`, que es exactamente la forma que un scanner
de texto lee mal.
"""
import pytest

from tests.test_render_reconciliacion_js import _render


def _catalogo(tmp_path, productos):
    salida = _render(
        tmp_path, 'cargarCatalogo',
        {'/api/productos/': {'total': len(productos), 'paginas': 1,
                             'productos': productos}},
        ['lista-productos'])
    return salida['lista-productos']


def _p(**kw):
    base = {'id': 1, 'nombre': 'Resma Carta', 'codigo': 'PROD-001',
            'codigo_siesa': 'PROD-001', 'clasificacion_abc': 'A',
            'unidad_medida': 'UND', 'stock_total': 0,
            'stock_averiado': 0, 'stock_vendible': 0}
    base.update(kw)
    return base


def test_las_averias_se_pintan(tmp_path):
    html = _catalogo(tmp_path, [_p(stock_total=40, stock_averiado=12,
                                   stock_vendible=28)])
    assert '40' in html
    assert '12 averiadas' in html, html


def test_sin_averias_no_inventa_la_linea(tmp_path):
    """Dirección contraria: no puede pintar «0 averiadas» en todo el catálogo."""
    html = _catalogo(tmp_path, [_p(stock_total=40, stock_averiado=0,
                                   stock_vendible=40)])
    assert 'averiadas' not in html, html


def test_todo_averiado_no_se_pinta_en_verde(tmp_path):
    """12 unidades de las cuales 12 están rotas no son «hay stock»."""
    html = _catalogo(tmp_path, [_p(stock_total=12, stock_averiado=12,
                                   stock_vendible=0)])
    assert '#4ade80' not in html, 'el total se pintó como disponible'
    assert '12 averiadas' in html


def test_stock_sano_sigue_en_verde(tmp_path):
    html = _catalogo(tmp_path, [_p(stock_total=40, stock_averiado=0,
                                   stock_vendible=40)])
    assert '#4ade80' in html


def test_una_cifra_hostil_no_llega_al_DOM(tmp_path):
    """El guard `> 0` descarta cualquier valor no numérico antes de pintarlo:
    en JS, `'<img…>' > 0` es `false`. No queda markup porque no se pinta la
    línea, no porque se escape. Se deja escrito para que un futuro cambio del
    guard —por ejemplo a `!= 0`, que sí dejaría pasar la cadena— tenga que
    tumbar este test para entrar."""
    html = _catalogo(tmp_path, [_p(stock_total=9, stock_vendible=0,
                                   stock_averiado='<img src=x onerror=alert(1)>')])
    assert '<img' not in html, html
    assert 'averiadas' not in html


def test_el_texto_libre_de_la_fila_va_escapado(tmp_path):
    """`unidad_medida` sí es texto libre y sí se pinta — es el campo donde el
    escapado de esta fila se puede perder de verdad."""
    html = _catalogo(tmp_path, [_p(stock_total=40, stock_averiado=12,
                                   stock_vendible=28,
                                   unidad_medida='<img src=x onerror=alert(1)>')])
    assert '<img src=x' not in html, html
    assert '&lt;img' in html
