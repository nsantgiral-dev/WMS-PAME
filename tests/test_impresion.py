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

_RAIZ = Path(__file__).resolve().parents[1]
_PWA = _RAIZ / 'app' / 'static' / 'pwa'
_VENDOR = _RAIZ / 'app' / 'static' / 'vendor'


def _js(nombre):
    return (_PWA / nombre).read_text(encoding='utf-8')


class TestElMotorDeCodigoBarrasEstaAhi:
    """La impresión de etiquetas figuraba como «bloque construido y nunca
    ejercitado». No era falta de tiempo: **no tenía forma de fallar visiblemente.**

    JsBarcode es el único motor de código de barras del WMS —producto,
    ubicación, LPN y bulto salen todos de él— y venía de `cdn.jsdelivr.net`.
    `sw.js` cachea por lista blanca y **sólo el propio origen**, así que la
    librería nunca entraba al caché: con la red floja no cargaba.

    Y los cuatro sitios que la usaban la llamaban dentro de `try {} catch (_) {}`.
    El fallo no producía un error: producía una **etiqueta impresa sin código**,
    con su texto legible y su membrete, idéntica a una buena hasta que alguien
    la pasa por el láser. Un bulto sin código no entra al manifiesto, y esa
    cuenta se termina haciendo de memoria.
    """

    _ARCHIVO = 'JsBarcode.all.min.js'

    def test_la_libreria_vive_en_el_repo(self):
        f = _VENDOR / self._ARCHIVO
        assert f.exists(), (
            f'falta {f} — sin ella no se imprime ningún código de barras')
        assert f.stat().st_size > 10_000, 'el archivo parece truncado'

    def test_no_se_carga_desde_un_origen_externo(self):
        """TRINQUETE. Volver a apuntar al CDN reintroduce el defecto entero:
        `sw.js` no lo cachea y no hay forma de notarlo hasta el día de imprimir.
        """
        html = (_PWA / 'index.html').read_text(encoding='utf-8')
        externos = [l.strip() for l in html.split('\n')
                    if 'src=' in l and 'http' in l and 'arcode' in l.lower()]
        assert not externos, (
            '\nJsBarcode se está cargando desde otro origen:\n'
            + '\n'.join(f'  · {e[:110]}' for e in externos)
            + '\n\nServirlo desde /static/vendor/ — el service worker sólo '
              'cachea el propio origen.')
        assert '/static/vendor/' + self._ARCHIVO in html

    def test_el_service_worker_lo_cachea(self):
        """Si no está en el SHELL, la PWA offline imprime etiquetas mudas."""
        assert '/static/vendor/' + self._ARCHIVO in _js('sw.js'), (
            'JsBarcode no está en el SHELL de sw.js')

    def test_nadie_llama_a_JsBarcode_directo(self):
        """Una política, una función.

        Cuatro sitios lo llamaban con cuatro `catch` propios, y tres de los
        cuatro eran `catch (_) {}` — silencio puro. Ahora todos pasan por
        `pintarCodigoBarras()`, que devuelve `false` en vez de callarse.
        """
        malas = []
        for archivo in sorted(_PWA.glob('*.js')):
            if archivo.name == 'app.js':
                continue   # define el helper; es el único que puede tocarlo
            for n, linea in enumerate(archivo.read_text(encoding='utf-8').split('\n'), 1):
                if re.search(r'\bJsBarcode\s*\(', linea):
                    malas.append(f'{archivo.name}:{n}  {linea.strip()[:70]}')
        assert not malas, (
            '\nLlamadas directas a JsBarcode fuera de app.js:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nUsar `pintarCodigoBarras()`, que informa cuando no puede.')

    #: (archivo, función que imprime etiquetas con código de barras)
    _IMPRESORAS = (
        ('etiquetas.js', 'function etqImprimir('),
        ('packing.js', 'function imprimirEtiquetaLPN('),
        ('packing.js', 'function empImprimirEtiquetas('),
        ('layout.js', 'function layoutImprimirEtiquetasCuerpo('),
    )

    def test_toda_impresion_de_etiquetas_verifica_el_motor_antes(self):
        """El guard va ANTES de armar el `#print-area`: preferimos no imprimir
        a imprimir una etiqueta muda. La muda se pega en la caja y el problema
        aparece tres días después, en la ruta."""
        faltan = []
        for archivo, firma in self._IMPRESORAS:
            js = _js(archivo)
            i = js.find(firma)
            assert i != -1, f'{archivo}: no está {firma!r} — ¿se renombró?'
            cuerpo = js[i:i + 900]
            if 'puedeImprimirEtiquetas(' not in cuerpo:
                faltan.append(f'{archivo} › {firma}')
        assert not faltan, (
            '\nImprimen etiquetas sin verificar que haya motor:\n'
            + '\n'.join(f'  · {f}' for f in faltan))

    def test_el_helper_existe_una_sola_vez(self):
        app = _js('app.js')
        assert app.count('function puedeImprimirEtiquetas(') == 1
        assert app.count('function pintarCodigoBarras(') == 1


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

    Quedan **cuatro**: `siesaForzarPacking` se borró el 2026-08-19 por
    duplicar remisión y factura sobre una tarea ya despachada. Recuperar y
    duplicar no son lo mismo, y ese botón hacía lo segundo.

    Se necesitan el día que Siesa falla, que es a las 6 p.m. de un viernes y no
    a las 10 a.m. de un martes. Una capacidad de recuperación que exige armar un
    curl con un JWT **no existe cuando hace falta**: existe cuando hay tiempo, y
    cuando hay tiempo no hace falta.
    """

    _ESPERADOS = {
        'siesaVerCompromisos': '/api/despacho_parcial/${id}/compromisos',
        'siesaFacturarRMManual': '/api/despacho_parcial/${id}/facturar-rm-manual',
        'siesaRecuperarPackingTraslados': '/api/traslados/recuperar-packing',
        'siesaReintentarTraslado': '/api/traslados/${id}/reintentar-siesa',
    }

    def test_las_cuatro_tienen_funcion_y_apuntan_a_su_endpoint(self):
        app = _js('app.js')
        for fn, url in self._ESPERADOS.items():
            assert f'async function {fn}(' in app, f'falta {fn}'
            i = app.index(f'async function {fn}(')
            assert url in app[i:i + 1800], f'{fn} no llama a {url}'

    def test_las_cuatro_se_disparan_desde_la_pantalla(self):
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

    # `siesaForzarPacking` salió de acá el 2026-08-19: se borró. Era la única
    # que reejecutaba la cadena entera sobre una tarea ya despachada.
    _PELIGROSAS = ('siesaFacturarRMManual', 'siesaReintentarTraslado')
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
        for fn in ('siesaFacturarRMManual',):
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
                < panel.index('siesaFacturarRMManual()'))


class TestLosEndpointsDeRecuperacionSiguenAhi:
    """Los tests de arriba miran el cliente. Estos, que el contrato exista."""

    @pytest.mark.parametrize('metodo,ruta', [
        ('GET', '/api/despacho_parcial/1/compromisos'),
        ('POST', '/api/despacho_parcial/1/facturar-rm-manual'),
        ('POST', '/api/traslados/1/reintentar-siesa'),
        ('POST', '/api/traslados/recuperar-packing'),
    ])
    def test_existen_y_exigen_sesion(self, client, metodo, ruta):
        r = client.open(ruta, method=metodo)
        assert r.status_code == 401, f'{ruta} no exige sesión'

    def test_forzar_siesa_ya_no_existe(self, client, jwt_token):
        """Se borró el 2026-08-19. Apagaba `siesa_triggered` y su única
        guardia —`estado == DESPACHADO`— admitía justo el caso que duplicaba
        remisión y factura. El rescate legítimo es `resetear_siesa`, que sí
        exige `not siesa_triggered`.

        El test se conserva invertido a propósito: que la ruta vuelva a
        aparecer tiene que ponerse rojo, no pasar callado.
        """
        r = client.post('/api/packing/1/forzar-siesa',
                        headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 404, (
            'volvió a existir /api/packing/<id>/forzar-siesa. Reejecuta '
            '244328→142945→142943 sobre una tarea ya despachada: con el '
            'documento ya en Siesa, duplica.')


# ══════════════════════════════════════════════════════════════════════════
# Los tres botones de recuperación de traslados
# ══════════════════════════════════════════════════════════════════════════

class TestLosTresBotonesDeRecuperacionDeTraslado:
    """Mismo defecto que el panel de arriba, en otro módulo.

    `trasRevertir`, `trasReintentarDespachoSiesa` y `trasReintentarRecepcionSiesa`
    existían con sus endpoints probados del lado del servidor y **ningún onclick
    las alcanzaba**. El guard de endpoints sin consumidor no las veía porque
    comprobaba que la URL apareciera *escrita* en el JS, y estaba escrita — dentro
    de una función que nadie llamaba.

    Lo que lo volvía urgente: `traslado_service` le escribe al operario, en el
    `siesa_error` y en el correo de alerta, «WMS Admin → Traslados → Reintentar
    despacho». Ese botón no existía. El sistema daba una instrucción imposible
    justo en el momento en que la mercancía ya salió y Siesa no tiene documento.
    """

    _TRASLADOS = _RAIZ / 'app' / 'static' / 'pwa' / 'traslados.js'

    def _tarjeta(self):
        """El cuerpo de `_renderTrasladoCard` — la única pantalla del detalle."""
        js = self._TRASLADOS.read_text(encoding='utf-8')
        i = js.index('function _renderTrasladoCard(')
        return js[i:js.index('\nfunction ', i + 10)]

    def _cuerpo(self, fn):
        js = self._TRASLADOS.read_text(encoding='utf-8')
        i = js.index(f'async function {fn}(')
        return js[i:i + 1800]

    _PELIGROSAS = ('trasRevertir', 'trasReintentarDespachoSiesa',
                   'trasReintentarRecepcionSiesa')

    def test_las_tres_se_disparan_desde_la_tarjeta(self):
        """EL TEST. Antes las tres existían y ninguna se alcanzaba."""
        tarjeta = self._tarjeta()
        for fn in self._PELIGROSAS:
            assert f'{fn}(' in tarjeta, (
                f'{fn} existe, su endpoint existe, y ningún botón la llama')

    def test_las_tres_piden_confirmacion(self):
        for fn in self._PELIGROSAS:
            assert 'confirm(' in self._cuerpo(fn), (
                f'{fn} crea un documento en Siesa o mueve inventario sin confirmar')

    def test_la_confirmacion_nombra_la_consecuencia(self):
        """Un «¿estás seguro?» pelado se contesta que sí sin leerlo.

        Las dos de reintento comparten un riesgo concreto y asimétrico: si el
        documento YA existía en Siesa, el reintento lo duplica, y una entrada o
        salida duplicada se anula con un ajuste a mano. Eso tiene que estar en
        la pregunta, no en un manual.
        """
        for fn in ('trasReintentarDespachoSiesa', 'trasReintentarRecepcionSiesa'):
            cuerpo = self._cuerpo(fn)
            assert 'DUPLICAD' in cuerpo, (
                f'{fn} no dice qué pasa si el documento ya existía en Siesa')
        revertir = self._cuerpo('trasRevertir')
        assert 'inventario' in revertir and 'Siesa' in revertir, (
            'trasRevertir no dice que las unidades vuelven al inventario ni que '
            'el STS queda vivo en Siesa y hay que anularlo a mano')

    def test_las_que_solo_leen_NO_molestan_con_confirmacion(self):
        """Si todo pide confirmar, la confirmación deja de significar algo."""
        js = self._TRASLADOS.read_text(encoding='utf-8')
        for fn in ('trasVerLPNs',):
            i = js.index(f'async function {fn}(')
            assert 'confirm(' not in js[i:i + 1200], (
                f'{fn} solo lee y pide confirmación — eso entrena a confirmar '
                'sin leer, y después la que importa también se confirma sin leer')

    def test_solo_aparecen_cuando_falta_el_consecutivo(self):
        """Un botón de emergencia visible en un traslado sano deja de leerse
        como emergencia. La condición es la misma que usa `to_dict` para decidir
        si el error pide acción: falta el documento de cierre."""
        tarjeta = self._tarjeta()
        assert '!s.siesa_salida_consec' in tarjeta, (
            'el reintento de despacho se ofrece sin mirar si el STS ya existe')
        assert '!s.siesa_entrada_consec' in tarjeta, (
            'el reintento de recepción se ofrece sin mirar si el ETS ya existe')

    def test_el_rotulo_es_el_que_el_sistema_le_dicta_al_operario(self):
        """«Nombres que mienten», en su forma más barata de evitar.

        `traslado_service` manda por correo «WMS Admin → Traslados → Reintentar
        despacho». Si el botón se llama «Reintentar salida», el operario busca
        lo que le dijeron, no lo encuentra, y llama a soporte con la mercancía
        ya en la calle.
        """
        servicio = (_RAIZ / 'app' / 'services' / 'traslado_service.py').read_text(
            encoding='utf-8')
        assert 'Reintentar despacho' in servicio, (
            'cambió el instructivo del servicio — actualiza también el rótulo')
        tarjeta = self._tarjeta()
        assert 'Reintentar despacho' in tarjeta, (
            'el correo manda a apretar «Reintentar despacho» y el botón se '
            'llama de otra forma')

    @pytest.mark.parametrize('ruta', [
        '/api/traslados/1/revertir',
        '/api/traslados/1/reintentar-despacho',
        '/api/traslados/1/reintentar-recepcion',
    ])
    def test_los_endpoints_existen_y_exigen_sesion(self, client, ruta):
        assert client.post(ruta).status_code == 401, f'{ruta} no exige sesión'
