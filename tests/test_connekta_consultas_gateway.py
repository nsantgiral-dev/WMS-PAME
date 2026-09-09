"""
Tests directos de ConnektaConsultasGateway (app/services/connekta_consultas_gateway.py),
extraído de ConnektaGateway el 2026-09-09 (paso 5 de la deuda de tamaño,
en tres sub-lotes: bodegas/ubicaciones/stock, pedidos/FE/catálogo/OC/
compromisos, y terceros/facturas/CxC — el dominio Consultas es grande y
se extrae por partes, ver el docstring del módulo. Dominio completo con
el tercer sub-lote).

tests/test_alertas_dedup.py ya cubre `_fetch_stock_pages` a fondo, y
tests/test_puertas_factura_duplicada.py, test_cond_pago.py,
test_liquidacion.py, test_recepcion_service.py y otros (453+ tests en
total) ya ejercen el segundo y tercer sub-lote de punta a punta vía el
singleton `connekta` — esto agrega cobertura directa adicional de la
clase aislada.
"""
import pytest

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


def test_get_estado_pedido_simulacion_devuelve_comprometido(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', True)
        assert connekta.get_estado_pedido('PD', '123') == 3


def test_get_estado_pedido_sin_filas_es_menos_uno(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(connekta, '_get', lambda *a, **kw: {'detalle': {'Table': []}})
        assert connekta.get_estado_pedido('PD', '999') == -1


def test_validar_tipo_proveedor_usa_llamada_intra_dominio(app, monkeypatch):
    """`validar_tipo_proveedor` llama a `self.get_ordenes_compra_aprobadas`
    (mismo objeto, no `core.X`) — confirmar que sigue resolviendo bien tras
    la extracción."""
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        fila = {'f200_nit_prov': '900123456', 'f200_id_tipo_prov': '0001'}
        monkeypatch.setattr(
            connekta._consultas, 'get_ordenes_compra_aprobadas',
            lambda **kw: {'detalle': {'Table': [fila]}},
        )
        resultado = connekta.validar_tipo_proveedor('900123456')
        assert resultado == {'configurado': True, 'tipo_proveedor': '0001', 'mensaje': ''}


def test_get_pedido_rowid_map_usa_llamada_intra_dominio(app, monkeypatch):
    """`get_pedido_rowid_map` llama a `self.get_compromisos_pedido` (mismo
    objeto) — confirmar que el mapa se arma igual tras la extracción."""
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        compromisos = [{'f120_referencia': 'PAPELSP9218', 'f431_rowid': 555}]
        monkeypatch.setattr(
            connekta._consultas, 'get_compromisos_pedido',
            lambda *a, **kw: compromisos,
        )
        mapa = connekta.get_pedido_rowid_map('PD', '123')
        assert mapa == {'PAPELSP9218': 555}


def test_get_rowids_factura_arma_el_filtro_y_exige_datos(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        capturado = {}

        def _fake_get(nombre_api, params=None, **kw):
            capturado['params'] = params
            return {'detalle': {'Table': [{'f470_rowid': 1}]}}

        monkeypatch.setattr(connekta, '_get', _fake_get)
        rows = connekta.get_rowids_factura('FEW', '1470')
        assert rows == [{'f470_rowid': 1}]
        assert '1470' in capturado['params']['parametros']


def test_get_rowids_factura_sin_tipo_docto_lanza_valueerror(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        with pytest.raises(ValueError, match='tipo_docto_fe requerido'):
            connekta.get_rowids_factura('', '1470')


def test_get_vencimiento_factura_cae_a_fallback_si_no_hay_fecha(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(connekta, '_get', lambda *a, **kw: {'detalle': {'Table': []}})
        resultado = connekta.get_vencimiento_factura('FEW', '1470')
        assert len(resultado) == 8 and resultado.isdigit()


def test_get_cxc_general_filtra_por_nit(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        capturado = {}

        def _fake_get(nombre_api, params=None, **kw):
            capturado['params'] = params
            return {'detalle': {'Table': [{'f253_id': '13050501'}]}}

        monkeypatch.setattr(connekta, '_get', _fake_get)
        rows = connekta.get_cxc_general('1000124053')
        assert rows == [{'f253_id': '13050501'}]
        assert '1000124053' in capturado['params']['parametros']


def test_get_terceros_contacto_y_get_vendedor_contacto_delegan(app, monkeypatch):
    with app.app_context():
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(
            connekta, '_get',
            lambda *a, **kw: {'detalle': {'Datos': [{'f200_nit': '900123456'}]}},
        )
        assert connekta.get_terceros_contacto(nit='900123456') == [{'f200_nit': '900123456'}]

        monkeypatch.setattr(
            connekta, '_get',
            lambda *a, **kw: {'detalle': {'Datos': [{'codigo_vendedor': 'V1'}]}},
        )
        assert connekta.get_vendedor_contacto(codigo='V1') == [{'codigo_vendedor': 'V1'}]
