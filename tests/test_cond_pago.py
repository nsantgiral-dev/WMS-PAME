"""
Un pedido sin condición de pago no es un pedido de contado.

Hasta el 2026-08-13, `f430_id_cond_pago` vacío se leía como CONTADO en dos
sitios independientes:

  · `ruta_service._valor_y_cond_pago`: `es_contado = (not cond_pago) or ...`
    → con vacío daba **True**.
  · `connekta_gateway.trigger_factura_desde_remision`: cae a
    `SIESA_COND_PAGO_VENTAS` (obligado — Connekta V2 colapsa con null).

El primero llega a la pantalla del conductor. `rutas.js:1908` solo muestra el
modo CRÉDITO cuando `es_contado === false` **confirmado**, así que con el campo
vacío mostraba «Valor a Cobrar»: **se le pedía al conductor cobrarle a un
cliente cuya condición de pago nadie conocía.**

## Por qué el vacío no puede colapsar a contado

Significa tres cosas que no se distinguen: maestro incompleto, consulta fallida,
o cliente realmente de contado. El lado conservador acá no es cobrar — es no
afirmar. Regla 0.

Hoy el daño es acotado. Con la factura diferida al momento de la liquidación
—diseño en evaluación— sería: cliente de crédito leído como contado, sin
factura, sin CxC y sin consumir cupo. Un crédito otorgado por un campo vacío.

## Y sobre cómo se mide si esto pasa

Se estuvo discutiendo con conteos de facturas de otro sistema («de 382.812,
prácticamente ninguna tiene condición vacía»). **Ese conteo no puede
detectarlo**: el fallback rellena el campo antes de emitir, así que toda
factura sale con condición. Es la huella del fallback, no su ausencia.

Lo que sí lo detecta es el rastro que deja `registrar_ausencia`, y la alerta
que el gateway encola.
"""
import pytest

from app.services import cond_pago as cp


class TestElVacioNoEsContado:

    def test_vacio_es_ausente_no_contado(self):
        assert cp.clasificar('', 'C01') == cp.AUSENTE
        assert cp.clasificar(None, 'C01') == cp.AUSENTE
        assert cp.clasificar('   ', 'C01') == cp.AUSENTE

    def test_el_codigo_de_contado_es_contado(self):
        assert cp.clasificar('C01', 'C01') == cp.CONTADO

    @pytest.mark.parametrize('cond', ['C02', '30D', 'C30', 'CREDITO60'])
    def test_cualquier_otra_es_credito(self, cond):
        assert cp.clasificar(cond, 'C01') == cp.CREDITO

    def test_C02_es_credito_aunque_sea_de_un_dia(self):
        """Para Siesa, C02 es crédito a un día — no contado. Es la distinción
        que decide si un pedido de ruta se retiene por mora."""
        assert cp.clasificar('C02', 'C01') == cp.CREDITO


class TestLaPantallaRecibeNoSe:
    """`None` es el valor que antes se colapsaba a `True`."""

    def test_ausente_devuelve_None(self):
        assert cp.es_contado_o_none('', 'C01') is None

    def test_contado_devuelve_True(self):
        assert cp.es_contado_o_none('C01', 'C01') is True

    def test_credito_devuelve_False(self):
        assert cp.es_contado_o_none('30D', 'C01') is False

    def test_None_no_es_False(self):
        """Distinción que importa en el JS: `es_contado === false` dispara el
        modo CRÉDITO. `None` no debe disparar CRÉDITO —no sabemos que lo
        sea— sino caer al campo libre."""
        assert cp.es_contado_o_none('', 'C01') is not False


class TestUnaSolaPolitica:
    """Anti-divergencia: la lectura del vacío estaba escrita dos veces, las dos
    hacia contado. Un fix en un solo sitio reproduce el defecto."""

    from pathlib import Path
    _RAIZ = Path(__file__).resolve().parents[1]
    _ARCHIVOS = ['app/services/ruta_service.py',
                 'app/services/connekta_gateway.py']

    def test_nadie_vuelve_a_leer_el_vacio_por_su_cuenta(self):
        import re
        culpables = []
        for rel in self._ARCHIVOS:
            texto = (self._RAIZ / rel).read_text(encoding='utf-8')
            for n, linea in enumerate(texto.split('\n'), 1):
                if linea.strip().startswith('#'):
                    continue
                # `not cond_pago` o `not _cond_pago_siesa` en una expresión que
                # decide contado: la forma exacta del defecto original.
                if re.search(r'not\s+_?cond_pago\w*\s*\)?\s*(or|and|if)', linea):
                    culpables.append(f'{rel}:{n}  {linea.strip()[:70]}')
        assert not culpables, (
            '\nVuelven a interpretar la condición vacía por su cuenta:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nUsar `services/cond_pago.py`.')

    def test_los_dos_consumidores_importan_el_modulo(self):
        faltan = [r for r in self._ARCHIVOS
                  if 'cond_pago as' not in (self._RAIZ / r).read_text(encoding='utf-8')]
        assert not faltan, f'no consumen la política compartida: {faltan}'


class TestRutaServiceNoAfirmaContado:
    """El defecto medido, en el sitio donde estaba."""

    def test_es_contado_es_None_cuando_siesa_no_trae_condicion(self, monkeypatch):
        from app.services import ruta_service as rs
        from app.services.connekta_gateway import connekta

        class _Tarea:
            id = 1
            rm_tipo, rm_consec = 'RS', 7
            tipo_docto_pedido_siesa = 'PD'
            consec_docto_pedido_siesa = '999'

        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **kw: [{'f350_id_tipo_docto': 'FEW',
                                           'f350_consec_docto': '1'}], raising=False)
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [], raising=False)
        # Siesa responde, pero sin condición de pago — el caso real.
        monkeypatch.setattr(connekta, 'get_pedido_cabecera',
                            lambda *a, **k: {'f430_id_cond_pago': ''}, raising=False)

        _, es_contado, _, crudo = rs.RutaService._valor_y_cond_pago(_Tarea())
        assert crudo == '', (
            'el campo crudo tiene que viajar: `es_contado` lo deriva esta '
            'misma función y no sirve para verificar el supuesto que la gobierna')
        assert es_contado is None, (
            'con condición vacía volvió a afirmar contado — la pantalla del '
            'conductor le pediría cobrar sin saber si el cliente es de crédito')

    def test_es_contado_es_True_con_condicion_de_contado(self, monkeypatch):
        from app.services import ruta_service as rs
        from app.services.connekta_gateway import connekta

        class _Tarea:
            id = 1
            rm_tipo, rm_consec = 'RS', 7
            tipo_docto_pedido_siesa = 'PD'
            consec_docto_pedido_siesa = '999'

        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **kw: [{'f350_id_tipo_docto': 'FEW',
                                           'f350_consec_docto': '1'}], raising=False)
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [], raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'get_pedido_cabecera',
                            lambda *a, **k: {'f430_id_cond_pago': 'C01'}, raising=False)

        _, es_contado, _, crudo = rs.RutaService._valor_y_cond_pago(_Tarea())
        assert es_contado is True
        assert crudo == 'C01', 'el crudo permite distinguir C01 de un vacío'


class TestElCrudoDistingueLoQueElDerivadoColapsa:
    """`es_contado is None` significa dos cosas: Siesa dijo que no hay
    condición, o Siesa no respondió. El crudo las separa — `''` contra `None`.

    Importa porque la pregunta que se quiere responder («¿cuántos pedidos de
    ruta vienen sin condición?») se contesta distinto en cada caso: uno es un
    maestro incompleto, el otro es una consulta caída.
    """

    class _Tarea:
        id = 1
        rm_tipo, rm_consec = 'RS', 7
        tipo_docto_pedido_siesa = 'PD'
        consec_docto_pedido_siesa = '999'

    def _correr(self, monkeypatch, cabecera_fn):
        from app.services import ruta_service as rs
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **kw: [{'f350_id_tipo_docto': 'FEW',
                                           'f350_consec_docto': '1'}], raising=False)
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [], raising=False)
        monkeypatch.setattr(connekta, 'get_pedido_cabecera', cabecera_fn, raising=False)
        return rs.RutaService._valor_y_cond_pago(self._Tarea())

    def test_siesa_responde_sin_condicion(self, monkeypatch):
        _, es_contado, _, crudo = self._correr(
            monkeypatch, lambda *a, **k: {'f430_id_cond_pago': ''})
        assert crudo == '' and es_contado is None

    def test_siesa_no_responde(self, monkeypatch):
        def _explota(*a, **k):
            raise Exception('Connekta caído')
        _, es_contado, _, crudo = self._correr(monkeypatch, _explota)
        assert crudo is None, 'sin consulta no hay dato crudo — no es lo mismo que vacío'
        assert es_contado is None


class TestLaCadenaDelModoNoSeCortaEnElNavegador:
    """La columna, la migración y el conteo pueden estar perfectos y el modo no
    registrarse nunca — si el JS deja de mandarlo, todo queda `(sin registrar)`
    y el desglose se ve sano.

    Es función-sin-caller con una vuelta más: acá el eslabón que falta está en
    el cliente, así que ningún test de servidor lo nota. Una mutación que quitó
    el campo del payload no tumbó nada hasta que se escribió esto.
    """

    from pathlib import Path
    _RAIZ = Path(__file__).resolve().parents[1]
    _RUTAS_JS = _RAIZ / 'app' / 'static' / 'pwa' / 'rutas.js'

    def test_el_payload_de_confirmacion_manda_el_modo(self):
        fuente = self._RUTAS_JS.read_text(encoding='utf-8')
        i = fuente.find('async function condGuardarParada()')
        assert i != -1, 'no está condGuardarParada — ¿se renombró?'
        bloque = fuente[i:i + 6000]
        assert 'modo_pantalla:' in bloque, (
            'el payload de confirmación dejó de mandar `modo_pantalla`. La '
            'columna se llenaría con NULL siempre y el conteo de paradas en '
            'LIBRE —que es el caso de riesgo— daría 0 sin que nada falle.')

    def test_el_modo_lo_decide_el_backend(self):
        """La política vive en `cond_pago.modo_pantalla`. Si el JS vuelve a
        calcularlo, son dos implementaciones y divergen — que es exactamente lo
        que pasó con la lectura del `cond_pago` vacío."""
        fuente = self._RUTAS_JS.read_text(encoding='utf-8')
        assert "p.modo_pago || 'LIBRE'" in fuente, (
            'el JS volvió a calcular el modo por su cuenta')

    def test_ante_campo_ausente_cae_a_LIBRE(self):
        """LIBRE es el modo que NO afirma nada. Asumir DINAMICO le pediría al
        conductor cobrar un valor que nadie confirmó."""
        fuente = self._RUTAS_JS.read_text(encoding='utf-8')
        assert "|| 'LIBRE'" in fuente


class TestLaCondicionQuedaAnotadaEnLaTarea:
    """Sin esto, la restricción de la entrega no se puede aplicar.

    El diseño acordado prohíbe `forma_pago = CREDITO` sobre una parada que el
    pedido declaró de contado. `confirmar_parada` no puede validarlo yendo a
    Siesa: **tiene que funcionar sin señal**. El dato tiene que estar anotado
    antes.

    Y responde la verificación que se venía planteando como «abrir un pedido en
    Siesa, dos minutos» — sobre todos los pedidos a la vez.
    """

    class _Tarea:
        id = 1
        rm_tipo, rm_consec = 'RS', 7
        tipo_docto_pedido_siesa = 'PD'
        consec_docto_pedido_siesa = '999'
        fe_tipo = fe_consec = None
        valor_factura = None
        cond_pago = None

    def _preparar(self, monkeypatch, cabecera):
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **k: [{'f350_id_tipo_docto': 'FEW',
                                          'f350_consec_docto': '1'}], raising=False)
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [], raising=False)
        monkeypatch.setattr(connekta, 'get_pedido_cabecera', cabecera, raising=False)

    def test_usa_lo_anotado_sin_volver_a_preguntar(self, monkeypatch):
        """El ahorro no es lo importante: es que `confirmar_parada` pueda
        validar sin red."""
        from app.services import ruta_service as rs

        t = self._Tarea()
        t.cond_pago = 'C01'
        self._preparar(
            monkeypatch,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError('volvió a preguntar')))
        _, es_contado, _, crudo = rs.RutaService._valor_y_cond_pago(t)
        assert crudo == 'C01' and es_contado is True

    def test_lo_anotado_vacio_no_dispara_otra_consulta(self, monkeypatch):
        """`''` es una respuesta de Siesa —«este pedido no trae condición»—, no
        la ausencia de respuesta. Volver a preguntar por un vacío anotado sería
        consultar Siesa en cada carga para siempre."""
        from app.services import ruta_service as rs

        t = self._Tarea()
        t.cond_pago = ''
        self._preparar(
            monkeypatch,
            lambda *a, **k: (_ for _ in ()).throw(AssertionError('volvió a preguntar')))
        _, es_contado, _, crudo = rs.RutaService._valor_y_cond_pago(t)
        assert crudo == '' and es_contado is None


class TestLaFacturaDeRutaNoPuedeSerDeContado:
    """Lo que se probó en producción el 2026-08-13, en código.

    Dos facturas de contado ($263.963 y $14.200) quedaron **en Elaboración**
    con «el valor de la cartera debe ser igual al valor de las CxC» — el mismo
    mensaje de la Regla 21. Siesa no aprueba un documento cuya cartera no
    cuadra, y una FE de contado exige el recaudo en el mismo documento.

    En ruta ese recaudo no existe al facturar: lo hace el conductor horas
    después. Por eso la FE nace a crédito de un día y el RC la salda.
    """

    CONTADO = 'C01'
    RUTA = 'C02'

    def test_contado_no_es_aprobable(self):
        assert cp.aprobable_en_ruta(self.CONTADO, self.CONTADO) is False

    def test_credito_a_un_dia_si(self):
        assert cp.aprobable_en_ruta(self.RUTA, self.CONTADO) is True

    def test_el_vacio_no_se_declara_no_aprobable(self):
        """`ausente` no es contado. Lo que se emite en ese caso es la condición
        de ruta, así que el documento sí se aprueba — el problema del vacío es
        de maestro, no de aprobación, y confundirlos manda la alerta que no es.
        """
        assert cp.aprobable_en_ruta('', self.CONTADO) is True
        assert cp.aprobable_en_ruta(None, self.CONTADO) is True

    @pytest.mark.parametrize('codigo_contado', ['C01', 'CO', 'X9', '001'])
    def test_el_codigo_de_contado_sale_de_la_configuracion(self, codigo_contado):
        """Una política, una función — y probada con un código que NO es `C01`.

        Con `C01` en los dos lados, una implementación que lo hardcodee da el
        mismo resultado que una que consulte `clasificar`, y el test no
        distingue. La empresa puede cambiar el código de contado en Siesa; si
        esta función se lo quedó fijo, el desglose contaría un universo y el
        gateway emitiría sobre otro.
        """
        assert cp.aprobable_en_ruta(codigo_contado, codigo_contado) is False
        assert cp.aprobable_en_ruta('OTRO', codigo_contado) is True


class TestElHuecoNoSeTapaConElCodigoDeContado:
    """El fallback del gateway emitía **exactamente** el valor que hoy se sabe
    que no se aprueba.

    `cond_pago = _cond_pago_siesa or self.cond_pago_ventas` — y
    `cond_pago_ventas` es el código de contado. Un pedido sin condición producía
    una factura que Siesa deja en Elaboración, con la remisión ya hecha y el
    inventario ya descargado.
    """

    _GW = __import__('pathlib').Path(__file__).resolve().parents[1] / \
        'app' / 'services' / 'connekta_gateway.py'

    @pytest.fixture(scope='class')
    def fuente(self):
        t = self._GW.read_text(encoding='utf-8')
        i = t.find('    def trigger_factura_desde_remision')
        j = t.find('\n    def ', i + 10)
        return t[i:j]

    def test_el_fallback_es_la_condicion_de_ruta(self, fuente):
        assert 'self.cond_pago_ruta' in fuente, (
            'el hueco de f430_id_cond_pago volvió a taparse con otra cosa')
        assert 'or self.cond_pago_ventas or None' not in fuente, (
            '\nEl fallback volvió al código de CONTADO (`cond_pago_ventas`).\n'
            'Eso emite una FE que Siesa no aprueba — con la remisión ya hecha '
            'y el inventario ya descargado. Usar `cond_pago_ruta`.')

    def test_sin_condicion_de_ruta_no_se_emite(self, fuente):
        """Preferible a emitir contado: la RM ya está en BD, así que el
        reintento del DLQ entra directo al 142943 sin duplicarla."""
        assert 'SIESA_COND_PAGO_RUTA no está configurado' in fuente

    def test_la_alerta_del_vacio_ya_no_miente(self, fuente):
        """Decía «factura emitida como CONTADO» y con el fallback nuevo eso es
        falso. Una alerta que describe mal lo que pasó manda a corregir el
        documento equivocado."""
        assert 'fue facturado' not in fuente
        assert 'Factura emitida como CONTADO por data incompleta' not in fuente

    def test_la_variable_esta_en_el_catalogo(self):
        """Si no está, `/api/health/siesa` dice `ok` con el despacho roto."""
        from app.services.vars_criticas import VARS_CRITICAS
        nombres = {v.nombre for v in VARS_CRITICAS}
        assert 'SIESA_COND_PAGO_RUTA' in nombres


class TestElContadoDelPedidoNoPasaCallado:
    """Ejerce la rama, no el texto.

    La primera versión de este test buscaba `'FE_CONTADO_NO_APROBABLE'` en el
    fuente. Una mutación que reemplazaba la condición por `elif False:` dejaba
    el string intacto en el cuerpo muerto y el test seguía en verde — el mismo
    modo de fallo que CLAUDE.md ya documenta para los detectores de texto.

    No se bloquea el despacho: la remisión ya descargó el inventario y quedarse
    sin factura es peor. Pero tiene que quedar el aviso, porque hoy nadie se
    entera hasta que la liquidación no encuentra la cuenta por cobrar.
    """

    CONTADO = 'C01'

    def _gw(self):
        from app.services.connekta_gateway import ConnektaGateway
        g = ConnektaGateway()
        g.modo_simulacion = True
        g.cond_pago_ventas = self.CONTADO
        g.cond_pago_ruta = 'C02'
        g.punto_envio_default = '001'
        return g

    def _cabecera(self, cond):
        return {'f200_id_pedido_fact': '900123', 'f461_id_punto_envio': '001',
                'f430_id_cond_pago': cond}

    def _alertas(self):
        from app.models.siesa_job import SiesaJob
        return [j for j in SiesaJob.query.filter_by(tipo='ALERTA_EMAIL').all()]

    def _tipos(self):
        # `payload` es Text con JSON serializado, no un dict.
        import json
        return {json.loads(j.payload).get('tipo_alerta') for j in self._alertas()}

    def test_un_pedido_de_contado_deja_aviso(self, app, db):
        gw = self._gw()
        gw.trigger_factura_desde_remision('RM', 1, self._cabecera(self.CONTADO))
        db.session.commit()
        assert 'FE_CONTADO_NO_APROBABLE' in self._tipos(), (
            '\nSe facturó un pedido de contado sin dejar rastro. Esa FE queda '
            'en Elaboración y la liquidación no va a encontrar la CxC.')

    def test_un_pedido_a_credito_no_deja_aviso(self, app, db):
        """Si avisara siempre, el aviso no distingue nada — 639 avisos conocidos
        ya hicieron invisible al único real una vez."""
        gw = self._gw()
        gw.trigger_factura_desde_remision('RM', 2, self._cabecera('C02'))
        db.session.commit()
        assert not self._alertas()

    def test_un_pedido_sin_condicion_avisa_otra_cosa(self, app, db):
        """El vacío es un problema de maestro y la factura sale bien. Mandar la
        alerta de contado haría que alguien fuera a corregir un documento que
        no tiene nada malo."""
        gw = self._gw()
        gw.trigger_factura_desde_remision('RM', 3, self._cabecera(''))
        db.session.commit()
        assert self._tipos() == {'DATA_MAESTRA_COND_PAGO'}

    def test_la_factura_se_emite_igual(self, app, db):
        """Bloquear acá dejaría la remisión hecha, el inventario descargado y
        ninguna factura. El aviso no puede convertirse en un bloqueo."""
        gw = self._gw()
        r = gw.trigger_factura_desde_remision('RM', 4, self._cabecera(self.CONTADO))
        assert r is not None


class TestCobraEnLaPuerta:
    """C02 (condición de ruta) y C01 (contado) responden igual a "¿el
    conductor cobra acá?" aunque respondan distinto a "¿Siesa aprueba esta
    condición?" (`aprobable_en_ruta`). El vendedor puede capturar cualquiera
    de los dos para un pedido de ruta que de cara al cliente es de contado —
    C02 es el que además se aprueba en Siesa."""

    CONTADO = 'C01'
    RUTA = 'C02'

    def test_contado_cobra_en_la_puerta(self):
        assert cp.cobra_en_la_puerta(self.CONTADO, self.CONTADO, self.RUTA) is True

    def test_la_condicion_de_ruta_tambien_cobra_en_la_puerta(self):
        """El caso que antes no se reconocía: un pedido capturado como C02
        caía en CREDITO y la pantalla no pedía cobrar nada."""
        assert cp.cobra_en_la_puerta(self.RUTA, self.CONTADO, self.RUTA) is True

    def test_un_credito_real_no_cobra_en_la_puerta(self):
        assert cp.cobra_en_la_puerta('30D', self.CONTADO, self.RUTA) is False

    def test_el_vacio_y_el_None_NO_son_lo_mismo(self):
        """`''` = Siesa contestó y el tercero no tiene condición.
        `None`  = no se pudo preguntar.

        Con el vacío **sí se sabe qué lleva la factura**: el gateway cae a la
        condición de ruta, y su propia alerta lo dice —«la cartera la salda el
        recibo de caja del conductor»—. Dejar la pantalla en «no sé» ahí hacía
        que la FE saliera en una condición que exige cobro y el conductor no lo
        pidiera: el mismo fallback escrito dos veces con resultados opuestos.

        Con `None` no se sabe ni si la factura existe. Ahí no se afirma nada.
        """
        assert cp.cobra_en_la_puerta('', self.CONTADO, self.RUTA) is True
        assert cp.cobra_en_la_puerta(None, self.CONTADO, self.RUTA) is None

    def test_sin_condicion_de_ruta_configurada_no_se_afirma(self):
        """Sin `SIESA_COND_PAGO_RUTA`, C02 y C04 son **indistinguibles**.

        La versión anterior devolvía `False` en ese caso. Con `'30D'` eso es
        correcto —es crédito real— y por eso el test pasaba; pero **no probaba
        el C02**, que es donde `False` significa que ninguna parada de ruta
        pide cobrar, en silencio y sin un solo síntoma. Un test que ejerce solo
        el caso seguro certifica el peligroso.

        `None` cae a `LIBRE`, que se cuenta en `por_modo_pantalla`: la falta de
        configuración se ve en vez de callarse.
        """
        assert cp.cobra_en_la_puerta(self.RUTA, self.CONTADO, '') is None
        assert cp.cobra_en_la_puerta('30D', self.CONTADO, '') is None
        assert cp.cobra_en_la_puerta('', self.CONTADO, '') is None
        # El contado no depende de la condición de ruta: es el otro código.
        assert cp.cobra_en_la_puerta(self.CONTADO, self.CONTADO, '') is True

    def test_no_se_puede_confundir_con_aprobable_en_ruta(self):
        """Las dos preguntas dan respuestas OPUESTAS para los mismos dos
        códigos — es la razón de que vivan en funciones separadas."""
        assert cp.cobra_en_la_puerta(self.CONTADO, self.CONTADO, self.RUTA) is True
        assert cp.aprobable_en_ruta(self.CONTADO, self.CONTADO) is False

        assert cp.cobra_en_la_puerta(self.RUTA, self.CONTADO, self.RUTA) is True
        assert cp.aprobable_en_ruta(self.RUTA, self.CONTADO) is True


class TestLaPantallaDelConductorReconoceLaCondicionDeRuta:
    """`_valor_y_cond_pago` tiene que usar `cobra_en_la_puerta`, no
    `es_contado_o_none` — si vuelve a usar la vieja, C02 cae a CRÉDITO y la
    pantalla deja de pedir cobro para un pedido que sí se cobra en la
    puerta."""

    class _Tarea:
        id = 1
        rm_tipo, rm_consec = 'RS', 7
        tipo_docto_pedido_siesa = 'PD'
        consec_docto_pedido_siesa = '999'
        fe_tipo = fe_consec = None
        valor_factura = None
        cond_pago = 'C02'  # ya anotada — no debe volver a consultar Siesa

    def test_un_pedido_anotado_como_ruta_pide_cobro(self, monkeypatch):
        from app.services import ruta_service as rs
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02', raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **k: [{'f350_id_tipo_docto': 'FEW',
                                          'f350_consec_docto': '1'}], raising=False)
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [], raising=False)
        monkeypatch.setattr(connekta, 'get_pedido_cabecera',
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError('volvió a preguntar')), raising=False)

        _, cobra, _, crudo = rs.RutaService._valor_y_cond_pago(self._Tarea())
        assert crudo == 'C02'
        assert cobra is True, (
            'C02 anotado tiene que pedir cobro en la pantalla del conductor, '
            'igual que C01')


class TestLaRestriccionDeCreditoTambienCubreLaCondicionDeRuta:
    """`confirmar_parada` tenía que reconocer C02 con la misma restricción
    que C01: sin esto, un pedido capturado como condición de ruta se podía
    marcar `forma_pago=CREDITO` sin que nada lo impidiera."""

    @staticmethod
    def _parada_con_cond_pago(db, almacen, cond_pago):
        import uuid
        from app.models.usuario import Usuario
        from app.models.conductor import Conductor
        from app.models.ruta_despacho import RutaDespacho
        from app.models.packing import TareaPacking
        from app.models.bulto import Bulto

        user = Usuario(email=f'cond_ruta_{uuid.uuid4().hex[:6]}@test.com',
                       nombre='Conductor Ruta', rol='conductor', activo=True)
        user.set_password('test123')
        db.session.add(user)
        db.session.flush()
        conductor = Conductor(usuario_id=user.id, nombre='Conductor Ruta',
                              cedula=uuid.uuid4().hex[:10], activo=True)
        db.session.add(conductor)
        db.session.flush()

        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado='EN_TRANSITO')
        db.session.add(ruta)
        db.session.flush()

        tarea = TareaPacking(
            codigo=f'PK-RUTA-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
            almacen_id=almacen.id,
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1700,
            numero_pedido_siesa='PED-RUTA', cond_pago=cond_pago,
        )
        db.session.add(tarea)
        db.session.flush()

        bulto = Bulto(
            tarea_id=tarea.id, codigo_barras=f'RUTA-{uuid.uuid4().hex[:6]}',
            tipo='Caja', numero=1, total=1, estado='CARGADO',
            ruta_despacho_id=ruta.id,
        )
        db.session.add(bulto)
        db.session.commit()
        return ruta, tarea, conductor.usuario_id

    def test_condicion_de_ruta_rechaza_forma_pago_credito(self, app, db, almacen, monkeypatch):
        from app.services.ruta_service import RutaService
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02', raising=False)
        ruta, tarea, usuario_id = self._parada_con_cond_pago(db, almacen, 'C02')

        with pytest.raises(ValueError, match='no se puede registrar como crédito'):
            RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
                'estado_entrega': 'ENTREGADO',
                'forma_pago': 'CREDITO',
                'monto_cobrado': 100000,
            })

    def test_credito_real_si_permite_forma_pago_credito(self, app, db, almacen, monkeypatch):
        """Caso legítimo: el pedido de verdad es a crédito (no C01 ni C02)."""
        from app.services.ruta_service import RutaService
        from app.services.connekta_gateway import connekta

        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02', raising=False)
        ruta, tarea, usuario_id = self._parada_con_cond_pago(db, almacen, '30D')

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
            'estado_entrega': 'ENTREGADO',
            'forma_pago': 'CREDITO',
            'monto_cobrado': 100000,
        })
        assert recaudo_id is not None
