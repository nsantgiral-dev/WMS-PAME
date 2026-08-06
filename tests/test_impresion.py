"""
Los dos papeles que se generaban y no se podían imprimir.

`/api/admin/remision/<packing_id>` y `/api/muelle/manifiesto` existían, estaban
probados del lado del servidor, y **ninguna pantalla los llamaba**. Los dos son
documentos que salen de la bodega en papel: la remisión viaja con la mercancía y
descarga inventario; el manifiesto es contra lo que el cliente firma que recibió
sus bultos.

Hoy esa cuenta se hace de memoria. Una caja de menos aparece tres días después
sin forma de saber si salió del muelle o se perdió en la ruta.

El riesgo específico de esta clase de botón: **el endpoint exige JWT en un
header y un `<a href target=_blank>` no manda headers**. Ese enlace devuelve 401
siempre, y como nadie lo abre hasta el día que hay que imprimir, pasa por bueno
durante meses. Ya ocurrió con «ver foto» en flota.
"""
import re
from pathlib import Path

import pytest

_PWA = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa'


def _js(nombre):
    return (_PWA / nombre).read_text(encoding='utf-8')


class TestLosDocumentosSeAbrenConElToken:
    """Un `<a href>` a un endpoint con JWT es un 401 esperando su turno."""

    def test_hay_UNA_funcion_que_abre_documentos(self):
        """El mismo bloque estaba copiado tres veces — y la tercera copia decía
        «error al obtener la remisión» dentro de la función de la factura, que
        es lo que pasa cuando se copia en vez de compartir."""
        app = _js('app.js')
        assert app.count('async function imprimirDocumento(') == 1

    def test_manda_el_token_en_el_header(self):
        app = _js('app.js')
        i = app.index('async function imprimirDocumento(')
        cuerpo = app[i:i + 1400]
        assert "Authorization: 'Bearer ' + TOKEN" in cuerpo

    def test_avisa_si_el_navegador_bloquea_la_ventana(self):
        """Un `window.open` bloqueado devuelve null y el documento no aparece.
        Sin aviso, el usuario aprieta el botón y no pasa nada."""
        app = _js('app.js')
        i = app.index('async function imprimirDocumento(')
        cuerpo = app[i:i + 1400]
        assert 'if (!ventana)' in cuerpo
        assert 'popups' in cuerpo

    def test_ninguna_pantalla_abre_estos_endpoints_con_un_enlace(self):
        """TRINQUETE — el modo de fallo exacto que ya costó una vez.

        Un `href` a un endpoint autenticado no falla al escribirlo: falla el día
        que alguien lo abre, con un 401 que nadie relaciona con el enlace.
        """
        malas = []
        for archivo in sorted(_PWA.glob('*.js')):
            js = archivo.read_text(encoding='utf-8')
            for n, linea in enumerate(js.split('\n'), 1):
                if 'href=' in linea and '/api/' in linea:
                    malas.append(f'{archivo.name}:{n}  {linea.strip()[:70]}')
        assert not malas, (
            '\nEnlaces directos a endpoints con JWT — devuelven 401 siempre:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nUsar `imprimirDocumento()`, que manda el header.')


class TestLaRemisionTieneBoton:
    """La remisión descarga inventario; la factura cobra. Son dos papeles.

    La pantalla tenía solo el de factura desde que se escribió.
    """

    def test_existe_la_funcion(self):
        assert 'async function imprimirRemisionAdmin(' in _js('app.js')

    def test_el_pedido_despachado_ofrece_los_dos(self):
        app = _js('app.js')
        i = app.index('const btnRemision = p.packing_id')
        bloque = app[i:i + 700]
        assert 'imprimirRemisionAdmin(' in bloque
        assert 'imprimirFacturaAdmin(' in bloque

    def test_apunta_al_endpoint_de_gestion_no_al_de_empacador(self):
        """`/api/packing/<id>/remision` es del empacador y exige bultos.
        `/api/admin/remision/<id>` cubre además los pedidos reconciliados
        automáticamente, que no tienen caja física."""
        app = _js('app.js')
        i = app.index('async function imprimirRemisionAdmin(')
        assert '/api/admin/remision/' in app[i:i + 400]


class TestElManifiesto:

    def test_existe_y_lo_llama_la_pantalla(self):
        assert 'async function muelleImprimirManifiesto(' in _js('rutas.js')
        html = (_PWA / 'index.html').read_text(encoding='utf-8')
        assert 'muelleImprimirManifiesto()' in html

    def test_no_imprime_un_manifiesto_vacio(self):
        """Un papel en blanco con membrete es peor que no imprimirlo: se firma
        igual."""
        js = _js('rutas.js')
        i = js.index('async function muelleImprimirManifiesto(')
        cuerpo = js[i:i + 1200]
        assert 'if (!paradas.length)' in cuerpo

    def test_deja_dónde_firmar(self):
        """Es el punto entero del documento: sin firma es una lista, no un
        comprobante de entrega."""
        js = _js('rutas.js')
        i = js.index('async function muelleImprimirManifiesto(')
        cuerpo = js[i:i + 4000]
        assert 'Recibido' in cuerpo
        assert 'Entregó (bodega)' in cuerpo and 'Recibió (conductor)' in cuerpo

    def test_escapa_lo_que_viene_de_la_base(self):
        """El nombre de un cliente con `<` rompe el documento, y peor: se
        imprime cualquier cosa que alguien haya escrito en el maestro."""
        js = _js('rutas.js')
        i = js.index('async function muelleImprimirManifiesto(')
        cuerpo = js[i:i + 4000]
        assert 'const esc = s =>' in cuerpo
        assert 'esc(p.cliente)' in cuerpo and 'esc(par.destino)' in cuerpo


class TestElServidorSigueRespondiendoLoQueLaPantallaEspera:
    """Los tests de arriba miran el cliente. Estos miran el contrato.

    Una pantalla que llama un endpoint que cambió de forma falla en silencio:
    el `catch` la deja en blanco y nadie sabe por qué.
    """

    def test_el_manifiesto_trae_las_claves_que_el_documento_usa(self, client, jwt_token_admin):
        r = client.get('/api/muelle/manifiesto',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        d = r.get_json()
        for k in ('manifiesto', 'fecha', 'total_bultos'):
            assert k in d, f'el manifiesto dejó de traer {k}'

    def test_la_remision_de_una_tarea_no_despachada_es_409_y_no_un_papel_vacio(
            self, app, db, client, jwt_token_admin, almacen):
        """Imprimir la remisión de algo que Siesa no confirmó sería un papel que
        afirma un despacho que no ocurrió."""
        from app.models.packing import TareaPacking

        t = TareaPacking(codigo='PK-IMP-1', tipo_documento='PEDIDO',
                         estado='EN_PROCESO', almacen_id=almacen.id,
                         numero_pedido_siesa='PD-IMP', siesa_triggered=False)
        db.session.add(t)
        db.session.commit()

        r = client.get(f'/api/admin/remision/{t.id}',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 409

    def test_un_operario_no_imprime_la_remision_de_gestion(
            self, client, jwt_token):
        r = client.get('/api/admin/remision/1',
                       headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403


# ══════════════════════════════════════════════════════════════════════════
# El panel de recuperación de Siesa
# ══════════════════════════════════════════════════════════════════════════

class TestElPanelDeRecuperacionEstaCompleto:
    """Cinco recuperaciones existían y solo se alcanzaban por `curl`.

    Se necesitan el día que Siesa falla, que es a las 6 p.m. de un viernes y no
    a las 10 a.m. de un martes. Una capacidad de recuperación que exige armar un
    curl con un JWT **no existe cuando hace falta**: existe cuando hay tiempo, y
    cuando hay tiempo no hace falta.
    """

    _ESPERADOS = {
        'siesaVerCompromisos': '/api/despacho_parcial/${id}/compromisos',
        'siesaFacturarRMManual': '/api/despacho_parcial/${id}/facturar-rm-manual',
        'siesaForzarPacking': '/api/packing/${id}/forzar-siesa',
        'siesaRecuperarPackingTraslados': '/api/traslados/recuperar-packing',
        'siesaReintentarTraslado': '/api/traslados/${id}/reintentar-siesa',
    }

    def test_las_cinco_tienen_funcion_y_apuntan_a_su_endpoint(self):
        app = _js('app.js')
        for fn, url in self._ESPERADOS.items():
            assert f'async function {fn}(' in app, f'falta {fn}'
            i = app.index(f'async function {fn}(')
            assert url in app[i:i + 1800], f'{fn} no llama a {url}'

    def test_las_cinco_se_disparan_desde_la_pantalla(self):
        app = _js('app.js')
        i = app.index('async function siesaRecuperacionCargar(')
        panel = app[i:app.index('/** Diagnóstico:', i)]
        for fn in self._ESPERADOS:
            assert f'{fn}()' in panel, f'{fn} existe y ningún botón la llama'


class TestLoPeligrosoNoSeConfundeConLoInocuo:
    """Un panel de emergencia se usa con prisa.

    Tres de estas acciones solo LEEN; dos pueden dejar una factura duplicada en
    Siesa, y una factura duplicada se anula con nota crédito, a mano, en
    contabilidad. Ponerlas en la misma fila de botones es invitar al error justo
    cuando nadie tiene cabeza para distinguirlas.
    """

    _PELIGROSAS = ('siesaFacturarRMManual', 'siesaForzarPacking',
                   'siesaReintentarTraslado')
    _SEGURAS = ('siesaVerCompromisos', 'siesaReconciliarPacking',
                'siesaVerRemision')

    def test_las_que_escriben_piden_confirmacion(self):
        app = _js('app.js')
        for fn in self._PELIGROSAS:
            i = app.index(f'async function {fn}(')
            cuerpo = app[i:i + 1800]
            assert 'confirm(' in cuerpo, f'{fn} escribe en Siesa sin confirmar'

    def test_la_confirmacion_nombra_la_consecuencia(self):
        """Un «¿estás seguro?» pelado se contesta que sí sin leerlo."""
        app = _js('app.js')
        for fn in ('siesaFacturarRMManual', 'siesaForzarPacking'):
            i = app.index(f'async function {fn}(')
            cuerpo = app[i:i + 1800]
            assert 'DUPLICADA' in cuerpo or 'DUPLICADOS' in cuerpo, (
                f'{fn} no dice qué pasa si el documento ya existía')

    def test_las_que_solo_leen_NO_molestan_con_confirmacion(self):
        """Si todo pide confirmar, la confirmación deja de significar algo."""
        app = _js('app.js')
        for fn in self._SEGURAS:
            i = app.index(f'async function {fn}(')
            cuerpo = app[i:i + 1200]
            assert 'confirm(' not in cuerpo, (
                f'{fn} solo lee y pide confirmación — eso entrena a confirmar '
                'sin leer, y después la que importa también se confirma sin leer')

    def test_la_pantalla_separa_los_dos_grupos(self):
        app = _js('app.js')
        i = app.index('async function siesaRecuperacionCargar(')
        panel = app[i:app.index('/** Diagnóstico:', i)]
        assert 'Crean documentos en Siesa' in panel, (
            'el bloque peligroso no está rotulado como tal')
        assert 'Los tres solo <b>leen</b>' in panel

    def test_el_diagnostico_va_antes_que_la_escritura(self):
        """«¿Por qué falló?» tiene que estar arriba: es lo único que contesta
        la pregunta antes de que alguien apriete algo que crea documentos."""
        app = _js('app.js')
        i = app.index('async function siesaRecuperacionCargar(')
        panel = app[i:app.index('/** Diagnóstico:', i)]
        assert (panel.index('siesaVerCompromisos()')
                < panel.index('siesaFacturarRMManual()'))
        assert (panel.index('siesaReconciliarPacking()')
                < panel.index('siesaForzarPacking()'))


class TestLosEndpointsDeRecuperacionSiguenAhi:
    """Los tests de arriba miran el cliente. Estos, que el contrato exista."""

    @pytest.mark.parametrize('metodo,ruta', [
        ('GET', '/api/despacho_parcial/1/compromisos'),
        ('POST', '/api/despacho_parcial/1/facturar-rm-manual'),
        ('POST', '/api/packing/1/forzar-siesa'),
        ('POST', '/api/traslados/1/reintentar-siesa'),
        ('POST', '/api/traslados/recuperar-packing'),
    ])
    def test_existen_y_exigen_sesion(self, client, metodo, ruta):
        r = client.open(ruta, method=metodo)
        assert r.status_code == 401, f'{ruta} no exige sesión'

    def test_forzar_siesa_es_solo_de_admin(self, client, jwt_token):
        """Es el más peligroso: reejecuta la cadena entera."""
        r = client.post('/api/packing/1/forzar-siesa',
                        headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403
