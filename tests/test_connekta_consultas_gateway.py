"""
Tests directos de ConnektaConsultasGateway (app/services/connekta_consultas_gateway.py),
extraído de ConnektaGateway el 2026-09-09 (paso 5 de la deuda de tamaño —
sub-lote bodegas/ubicaciones/stock; el dominio Consultas es grande y se
extrae en varios pasos, ver el docstring del módulo).

tests/test_alertas_dedup.py ya cubre `_fetch_stock_pages` a fondo (fallos de
página, respuesta None del circuit breaker, camino feliz) llamándolo
directo sobre una instancia real de ConnektaGateway — esto agrega
cobertura de `get_bodegas_siesa`/`get_ubicaciones_siesa` y confirma que el
gateway delega correctamente a la clase nueva.
"""
from app.services.connekta_consultas_gateway import ConnektaConsultasGateway


def test_get_bodegas_siesa_llama_al_get_del_core(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        capturado = {}

        def _fake_get(nombre_api, params=None, **kw):
            capturado['nombre_api'] = nombre_api
            capturado['params'] = params
            return {'detalle': {'Table': []}}

        monkeypatch.setattr(connekta, '_get', _fake_get)
        connekta.get_bodegas_siesa()
        assert capturado['nombre_api'] == 'API_v2_Bodegas'
        assert capturado['params']['paginacion'] == 'numPag=1|tamPag=200'


def test_get_ubicaciones_siesa_filtra_por_bodega(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        capturado = {}

        def _fake_get(nombre_api, params=None, **kw):
            capturado['params'] = params
            return {'detalle': {'Table': []}}

        monkeypatch.setattr(connekta, '_get', _fake_get)
        connekta.get_ubicaciones_siesa(bodega_id='NB1', pagina=2)
        assert 'NB1' in capturado['params']['parametros']
        assert 'numPag=2' in capturado['params']['paginacion']


def test_connekta_gateway_delega_al_dominio_consultas(app):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        assert isinstance(connekta._consultas, ConnektaConsultasGateway)
        assert connekta._consultas._core is connekta
