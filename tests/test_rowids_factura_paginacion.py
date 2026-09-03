"""
Las líneas de una factura se leen TODAS, o se declara que no.

`get_rowids_factura` pedía `numPag=1|tamPag=100` y devolvía esa única página
como si fuera la factura entera. La Regla 10 fija 100 como techo duro (≥200
hace que Siesa rechace la consulta con una fila `{'alerta': ...}`, ≥500 devuelve
registros fantasma con todos los campos NULL), así que la única salida es
paginar: subir el `tamPag` no es una opción.

Una factura de más de 100 líneas se leía truncada, y el truncamiento **no se
distingue de una factura corta**. Las dos consecuencias son financieras y
ninguna avisa:

· `routes/rutas.py:1261` y `services/liquidacion_service.py:1416` suman
  `f470_vlr_bruto`/`f470_vlr_imp` de estas filas para armar la base gravable del
  Documento Contable de retenciones. Base truncada → **el DC declara menos
  retención de la que el cliente efectivamente retuvo**. Es una diferencia
  contra la DIAN, no un número feo en una pantalla.

· Las mismas filas arman `_construir_lineas_nc(es_total=True)` y su
  `valor_cruce` (`F353_VLR_CRUCE`). Una NC total sobre una factura de >100
  líneas **cruza menos que el saldo** y la factura queda abierta en cartera.

`_exigir_datos` no lo detecta y no es su trabajo: ve el rechazo de Siesa, no el
truncamiento. Una página llena es una respuesta perfectamente válida.

**Detector en las dos direcciones** (regla dura del repo): estos tests también
exigen que la paginación NO dispare sobre operación sana — una factura de 7
líneas se lee completa, se declara completa y no levanta nada. Un detector que
solo prueba que dispara, prueba la mitad.
"""
import pytest

from app.services.connekta_gateway import (
    ConnektaGateway,
    ConnektaPaginacionError,
)


def _gw():
    gw = ConnektaGateway()
    gw.modo_simulacion = False
    gw.modo_ensayo = False
    gw.centro_op = '003'
    gw.id_cia_siesa = '1'
    return gw


def _linea(i, uom='UND'):
    """Una línea de factura como la devuelve API_v2_Ventas_Facturas_DesdePedido."""
    return {
        'f470_rowid': 2857000 + i,
        'f120_referencia': f'PROD-{i:04d}',
        'f470_cant_base': 1,
        'f470_id_unidad_medida': uom,
        'f150_id': 'NB1',
        'f470_vlr_bruto': 1000.0,
        'f470_vlr_imp': 190.0,
        'f470_vlr_neto': 1190.0,
    }


def _paginas(*tamanos):
    """`_get` falso que sirve páginas de los tamaños dados (mismo patrón que
    `tests/test_oc_paginacion.py`). Numera las líneas de corrido entre páginas
    para que un duplicado o un salto se vea en el resultado."""
    paginas = []
    n = 0
    for tam in tamanos:
        paginas.append([_linea(n + i) for i in range(tam)])
        n += tam

    def _get(_api, params=None, **k):
        num = int(params['paginacion'].split('numPag=')[1].split('|')[0])
        filas = paginas[num - 1] if num <= len(paginas) else []
        return {'detalle': {'Table': filas}}
    return _get


# ══════════════════════════════════════════════════════════════════════
# Dispara: la factura larga se lee entera
# ══════════════════════════════════════════════════════════════════════

class TestFacturaLarga:

    def test_ciento_cuarenta_y_dos_lineas_se_leen_las_142(self, monkeypatch):
        """El caso del defecto: 100 + 42. Antes devolvía 100 y nadie lo sabía."""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(100, 42))
        filas = gw.get_rowids_factura('FEW', '1466')
        assert len(filas) == 142, (
            f'leyó {len(filas)} de 142 líneas — la factura quedó truncada y el '
            'resultado no se distingue de una factura corta')

    def test_no_duplica_ni_saltea_lineas_entre_paginas(self, monkeypatch):
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(100, 42))
        rowids = [f['f470_rowid'] for f in gw.get_rowids_factura('FEW', '1466')]
        assert len(set(rowids)) == 142
        assert rowids == sorted(rowids)

    def test_tres_paginas(self, monkeypatch):
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(100, 100, 13))
        assert len(gw.get_rowids_factura('FEW', '1466')) == 213

    def test_ninguna_pagina_pide_mas_de_100(self, monkeypatch):
        """Regla 10 — el techo es duro. Paginar es la única salida, no subir
        el tamPag."""
        gw = _gw()
        pedidos = []
        base = _paginas(100, 42)

        def _spy(api, params=None, **k):
            pedidos.append(params['paginacion'])
            return base(api, params, **k)

        monkeypatch.setattr(gw, '_get', _spy)
        gw.get_rowids_factura('FEW', '1466')
        assert pedidos, 'no consultó nada'
        for p in pedidos:
            assert int(p.split('tamPag=')[1]) <= 100, f'tamPag por encima de 100: {p}'

    def test_el_filtro_es_el_mismo_en_todas_las_paginas(self, monkeypatch):
        """Cambiar el filtro entre páginas mezclaría líneas de otra factura —
        y eso sí sería una NC contra el documento equivocado."""
        gw = _gw()
        filtros = []
        base = _paginas(100, 42)

        def _spy(api, params=None, **k):
            filtros.append(params['parametros'])
            return base(api, params, **k)

        monkeypatch.setattr(gw, '_get', _spy)
        gw.get_rowids_factura('FEW', '1466')
        assert len(set(filtros)) == 1, f'el filtro cambió entre páginas: {set(filtros)}'


# ══════════════════════════════════════════════════════════════════════
# NO dispara: la operación sana sigue exactamente igual
# ══════════════════════════════════════════════════════════════════════

class TestFacturaNormal:

    def test_siete_lineas_se_leen_completas_y_no_levanta_nada(self, monkeypatch):
        """El caso de todos los días. Si el arreglo lo tocara, rompería el 99%
        de las facturas para arreglar el 1%."""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(7))
        filas = gw.get_rowids_factura('FEW', '1466')
        assert len(filas) == 7
        assert filas[0]['f120_referencia'] == 'PROD-0000'

    def test_una_factura_corta_consulta_una_sola_pagina(self, monkeypatch):
        """No se gastan GETs de más contra Siesa: la primera página vino corta,
        se sabe que no hay más."""
        gw = _gw()
        llamadas = []
        base = _paginas(7)

        def _spy(api, params=None, **k):
            llamadas.append(params['paginacion'])
            return base(api, params, **k)

        monkeypatch.setattr(gw, '_get', _spy)
        gw.get_rowids_factura('FEW', '1466')
        assert len(llamadas) == 1, f'{len(llamadas)} GETs para 7 líneas: {llamadas}'

    def test_exactamente_100_lineas_pregunta_por_la_segunda_pagina(self, monkeypatch):
        """Borde exacto: la página vino llena, así que **no se puede saber** si
        hay más — hay que preguntar. La segunda vuelve vacía y ahí sí se cierra."""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(100, 0))
        assert len(gw.get_rowids_factura('FEW', '1466')) == 100

    def test_factura_sin_lineas_devuelve_lista_vacia_sin_levantar(self, monkeypatch):
        """Cero es un resultado legítimo, no un truncamiento. Los callers ya
        tratan la lista vacía como «la factura no existe en Siesa»."""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(0))
        assert gw.get_rowids_factura('FEW', '9999') == []


# ══════════════════════════════════════════════════════════════════════
# El barrido incompleto se declara — nunca se devuelve una factura parcial
# ══════════════════════════════════════════════════════════════════════

class TestBarridoIncompleto:

    def test_tope_de_paginas_levanta_en_vez_de_devolver_parcial(self, monkeypatch):
        """Misma política que `_fetch_stock_pages` (`ConnektaPaginacionError`):
        esta función devuelve la lista directamente, no un dict con bandera, así
        que la única forma honesta de declarar el truncamiento es levantar.
        Devolver las filas leídas sería exactamente el defecto de origen."""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(*([100] * 200)))
        with pytest.raises(Exception) as exc:
            gw.get_rowids_factura('FEW', '1466')
        assert 'parcial' in str(exc.value).lower() or 'truncad' in str(exc.value).lower(), (
            f'levantó sin decir que el barrido quedó incompleto: {exc.value}')

    def test_una_pagina_que_falla_no_se_saltea(self, monkeypatch):
        """Saltar una página deja sus líneas afuera y el llamador recibe una
        factura incompleta que parece completa — el mismo agujero por otra
        puerta."""
        gw = _gw()
        base = _paginas(100, 100, 5)

        def _get(api, params=None, **k):
            num = int(params['paginacion'].split('numPag=')[1].split('|')[0])
            if num == 2:
                raise Exception('Connekta no respondió — reintenta')
            return base(api, params, **k)

        monkeypatch.setattr(gw, '_get', _get)
        with pytest.raises(Exception):
            gw.get_rowids_factura('FEW', '1466')

    def test_un_rechazo_de_siesa_en_la_segunda_pagina_no_se_lee_como_fin(self, monkeypatch):
        """`{'alerta': ...}` es un rechazo, no «se acabaron las páginas».
        Cortar ahí devolvería las 100 primeras líneas en silencio."""
        gw = _gw()
        base = _paginas(100, 42)

        def _get(api, params=None, **k):
            num = int(params['paginacion'].split('numPag=')[1].split('|')[0])
            if num == 2:
                return {'detalle': {'Table': [{'alerta': 'Por favor verifique los parámetros'}]}}
            return base(api, params, **k)

        monkeypatch.setattr(gw, '_get', _get)
        with pytest.raises(Exception):
            gw.get_rowids_factura('FEW', '1466')


# ══════════════════════════════════════════════════════════════════════
# Lo que costaba: las dos consecuencias financieras
# ══════════════════════════════════════════════════════════════════════

class TestConsecuenciaFinanciera:

    def test_la_base_gravable_del_documento_contable_sale_completa(self, monkeypatch):
        """La suma que hacen `rutas.py:1261` y `liquidacion_service.py:1416`.
        Con 142 líneas de $1.000 + $190 de IVA, la base es 142.000 / 26.980.
        Truncada a 100 líneas daba 100.000 / 19.000: **el DC declaraba un 30%
        menos de retención de la que el cliente ya retuvo.**"""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(100, 42))
        lineas = gw.get_rowids_factura('FEW', '1466')
        base_gravable = sum(float(l.get('f470_vlr_bruto', 0)) for l in lineas)
        total_iva = sum(float(l.get('f470_vlr_imp', 0)) for l in lineas)
        assert base_gravable == 142000.0, (
            f'base gravable {base_gravable} en vez de 142000 — el DC declara '
            'menos retención de la que el cliente retuvo')
        assert total_iva == 26980.0

    def test_el_cruce_de_una_nc_total_cubre_el_saldo_entero(self, monkeypatch):
        """`_construir_lineas_nc(es_total=True)` y su `valor_cruce`
        (`F353_VLR_CRUCE`). Con la factura truncada la NC total cruzaba
        $119.000 de un saldo de $168.980 y **la factura quedaba abierta en
        cartera** — sin que nada fallara."""
        from decimal import Decimal
        from app.services.siesa_job_service import _construir_lineas_nc

        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas(100, 42))
        rowids = gw.get_rowids_factura('FEW', '1466')
        lineas_nc = _construir_lineas_nc(
            rowids, es_total=True, items_devueltos=[], causal='01',
            motivo='01', uom_default='UND', bodega_default='NB1',
        )
        valor_cruce = sum(
            (l['f470_vlr_neto_prorrateado'] for l in lineas_nc), Decimal(0))
        assert len(lineas_nc) == 142
        assert valor_cruce == Decimal('168980.0'), (
            f'F353_VLR_CRUCE = {valor_cruce}, saldo real 168980 — la NC total '
            'cruza menos que el saldo y la factura queda abierta')


# ══════════════════════════════════════════════════════════════════════
# get_cxc_general — misma forma, mismo arreglo
# ══════════════════════════════════════════════════════════════════════

def _fila_cxc(i):
    return {
        'f353_id_tipo_docto_cruce': 'PD',
        'f353_consec_docto_cruce': 1000 + i,
        'f253_id': '13050501',
        'f353_total_db': 100000.0,
        'f353_total_cr': 100000.0,
        'f200_id': '1000124053',
    }


def _paginas_cxc(*tamanos):
    paginas = []
    n = 0
    for tam in tamanos:
        paginas.append([_fila_cxc(n + i) for i in range(tam)])
        n += tam

    def _get(_api, params=None, **k):
        num = int(params['paginacion'].split('numPag=')[1].split('|')[0])
        filas = paginas[num - 1] if num <= len(paginas) else []
        return {'detalle': {'Table': filas}}
    return _get


class TestCxcGeneralPagina:
    """`get_cxc_general` define el universo de `cxc_cruce.esta_saldada`, que es
    la guarda anti-duplicado del recibo de caja.

    Una fila que falta por truncamiento se lee como «esa factura no está en la
    cartera» → `esta_saldada` devuelve `None` («no sé»). En el pre-flight eso
    **no bloquea el POST**: el RC sale igual, y si la factura ya estaba saldada
    es un segundo recibo de caja. Un cliente con más de 100 filas de cartera
    abierta no es raro; el mayorista de ruta es exactamente ese cliente.
    """

    def test_ciento_cuarenta_y_dos_filas_de_cartera_se_leen_las_142(self, monkeypatch):
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas_cxc(100, 42))
        assert len(gw.get_cxc_general('1000124053')) == 142

    def test_la_factura_de_la_pagina_dos_se_encuentra(self, monkeypatch):
        """El caso que costaba: la fila existe en Siesa, está saldada, y el WMS
        no la veía."""
        from app.services import cxc_cruce
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas_cxc(100, 42))
        cxc = gw.get_cxc_general('1000124053')
        assert cxc_cruce.esta_saldada(cxc, 'PD', 1130) is True, (
            'la fila de la página 2 no se encontró — `esta_saldada` responde '
            '«no sé» y el pre-flight deja pasar un segundo recibo de caja')

    def test_tres_filas_se_leen_igual_que_antes(self, monkeypatch):
        """No dispara sobre operación sana: el cliente normal tiene 3 filas."""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas_cxc(3))
        assert len(gw.get_cxc_general('1000124053')) == 3

    def test_sin_cartera_devuelve_lista_vacia(self, monkeypatch):
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas_cxc(0))
        assert gw.get_cxc_general('1000124053') == []

    def test_barrido_incompleto_no_devuelve_cartera_parcial(self, monkeypatch):
        """Esta función tiene una política declarada y con trinquete propio
        (`test_error_no_propaga_devuelve_lista_vacia`): nunca bloquea el camino
        crítico del RC. Así que acá el barrido incompleto no levanta hacia
        afuera — degrada a `[]`, que aguas abajo es «no sé» (`esta_saldada` →
        `None`) y por Regla 3 no se reintenta. Devolver 100 filas parciales, en
        cambio, es afirmar «la cartera del cliente es ésta»."""
        gw = _gw()
        monkeypatch.setattr(gw, '_get', _paginas_cxc(*([100] * 60)))
        assert gw.get_cxc_general('1000124053') == []
