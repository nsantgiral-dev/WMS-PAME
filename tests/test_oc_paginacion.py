"""
El tope de páginas de las órdenes de compra se declara.

`get_ordenes_compra_aprobadas` paginaba `range(1, 6)` con un comentario
—«máximo 5 páginas = 500 items»— y **nada en el resultado**. Con más de 500 OC
aprobadas el WMS ve 500 y el muelle de recepción lee eso como el universo
completo: las OC que no entraron no se distinguen de las que no existen.

Es el hallazgo K otra vez —el sync de pedidos que abortaba la paginación y
borraba lo no leído— por el lado que quedó sin cerrar. Allá el resultado
declara `paginacion_completa`; acá no declaraba nada.

> Un conteo sin su base no es una medición.
"""
import pytest


@pytest.fixture
def gw(monkeypatch):
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'modo_simulacion', False)
    return connekta


def _paginas(*tamanos):
    """Devuelve un `_get` falso que sirve páginas de los tamaños dados."""
    paginas = [[{'f420_consec_docto': i} for i in range(n)] for n in tamanos]

    def _get(_api, params=None, **k):
        num = int(params['paginacion'].split('numPag=')[1].split('|')[0])
        filas = paginas[num - 1] if num <= len(paginas) else []
        return {'detalle': {'Table': filas}}
    return _get


class TestCuandoAlcanza:
    def test_una_pagina_corta_cierra_y_declara_completa(self, gw, monkeypatch):
        monkeypatch.setattr(gw, '_get', _paginas(100, 42))
        r = gw.get_ordenes_compra_aprobadas()
        assert len(r['detalle']['Table']) == 142
        assert r['paginacion_completa'] is True

    def test_una_sola_pagina(self, gw, monkeypatch):
        monkeypatch.setattr(gw, '_get', _paginas(7))
        r = gw.get_ordenes_compra_aprobadas()
        assert r['paginacion_completa'] is True

    def test_sin_ninguna_oc(self, gw, monkeypatch):
        """Cero es un resultado legítimo, no un truncamiento."""
        monkeypatch.setattr(gw, '_get', _paginas(0))
        r = gw.get_ordenes_compra_aprobadas()
        assert r['detalle']['Table'] == []
        assert r['paginacion_completa'] is True


class TestCuandoNoAlcanza:
    def test_cinco_paginas_llenas_declaran_truncamiento(self, gw, monkeypatch):
        """**El detector ciego.** Cinco páginas llenas significan que hay más
        del otro lado, y antes eso se devolvía como si fuera todo."""
        monkeypatch.setattr(gw, '_get', _paginas(100, 100, 100, 100, 100, 100))
        r = gw.get_ordenes_compra_aprobadas()
        assert len(r['detalle']['Table']) == 500
        assert r['paginacion_completa'] is False, (
            'devolvió 500 OC sin declarar que se truncó — el muelle lo lee '
            'como el universo completo')

    def test_el_caso_de_borde_exacto(self, gw, monkeypatch):
        """Exactamente 500 y ni una más: la quinta página vino llena, así que
        **no se puede saber** si hay más. Regla 0 — se declara incompleta."""
        monkeypatch.setattr(gw, '_get', _paginas(100, 100, 100, 100, 100))
        r = gw.get_ordenes_compra_aprobadas()
        assert r['paginacion_completa'] is False
