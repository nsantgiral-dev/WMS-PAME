"""
Los tres números que decidían el rediseño de facturación.

Se estuvieron estimando toda la semana en un intercambio entre el analista de
cartera, el consultor de Siesa y este repo, y ninguno se podía sacar de las
pantallas: el dashboard de liquidación agrega por ruta y no desglosa.

  1. `forma_pago` × `estado_entrega` — cuánto del flujo es contado (lo único
     que se movería si la FE pasa a emitirse en la liquidación) y con qué
     frecuencia hay PARCIAL o RECHAZADO, que es cuando haría falta devolver
     mercancía al camión.
  2. Rezago de liquidación — días entre ENTREGADA y LIQUIDADA. **No existe
     ninguna alerta** cuando eso no pasa; se buscó en los schedulers y en el
     servicio de alertas.
  3. Cuántas veces se facturó como CONTADO porque el pedido no traía
     `f430_id_cond_pago`.

El tercero merece su propio párrafo. La pregunta «¿esto pasa alguna vez?» se
estuvo discutiendo con conteos de facturas de otro sistema —«de 382.812,
prácticamente ninguna tiene condición vacía»— y **ese conteo no puede
detectarlo**: el fallback rellena el campo antes de emitir, así que toda
factura sale con condición. Es la huella del fallback, no su ausencia. Lo que
sí lo detecta es la alerta que el gateway encola, y vive en esta base.

## Sobre el rezago, que es donde el número engaña

Hoy liquidar **no tiene consecuencia fiscal**. Si la factura pasa a emitirse
ahí, la tendría. Medir la latencia de un proceso sin consecuencias y
proyectarla a uno con consecuencias es un error de método — puede ir para los
dos lados: la gente se apura cuando importa, o se paraliza por miedo a
equivocarse.

Por eso el endpoint devuelve la advertencia **pegada al número**, y hay un test
que lo exige. Un dato que se puede malinterpretar viaja con su interpretación o
no viaja.
"""
from datetime import date, datetime, timedelta

import pytest

from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
from app.models.ruta_despacho import EstadoFinancieroRuta

_URL = '/api/rutas/liquidacion/desglose'


@pytest.fixture
def h(jwt_token_admin):
    return {'Authorization': f'Bearer {jwt_token_admin}'}


@pytest.fixture
def ruta_con_tarea(db, almacen):
    """Una ruta ENTREGADA con su tarea de packing. Mínimo para colgar recaudos."""
    import uuid

    from app.models.packing import TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    from app.models.usuario import Usuario

    cond = Usuario.query.filter_by(email='cond_desglose@test.com').first()
    if not cond:
        cond = Usuario(email='cond_desglose@test.com', nombre='Conductor',
                       rol='conductor', activo=True)
        cond.set_password('test123')
        db.session.add(cond)
        db.session.flush()

    ruta = RutaDespacho(conductor_id=cond.id, tipo_ruta='Urbana', estado='ENTREGADA')
    db.session.add(ruta)
    db.session.flush()

    tarea = TareaPacking(
        codigo=f'PK-DES-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
        almacen_id=almacen.id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=777,
        numero_pedido_siesa='PED-DES',
    )
    db.session.add(tarea)
    db.session.commit()
    return ruta, tarea


def _recaudo(db, ruta_id, tarea_id, estado, pago):
    r = RecaudoEntrega(ruta_id=ruta_id, tarea_id=tarea_id,
                       estado_entrega=estado, forma_pago=pago, monto_cobrado=0)
    db.session.add(r); db.session.commit()
    return r


class TestElDesgloseDeEntregas:

    def test_cruza_forma_de_pago_con_estado(self, client, db, h, ruta_con_tarea):
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.PARCIAL, 'EFECTIVO')
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'CREDITO')

        r = client.get(_URL, headers=h).get_json()['recaudos']
        assert r['total'] == 3
        assert r['matriz']['EFECTIVO | ENTREGADO'] == 1
        assert r['matriz']['CREDITO | ENTREGADO'] == 1
        assert r['por_forma_pago']['EFECTIVO'] == 2

    def test_cuenta_parciales_y_rechazados(self, client, db, h, ruta_con_tarea):
        """Es la frecuencia con que haría falta la devolución de remisión — y
        también con la que se ejerce el control «no paga, no se entrega»."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.PARCIAL, 'EFECTIVO')
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.RECHAZADO, None)

        r = client.get(_URL, headers=h).get_json()['recaudos']
        assert r['parcial_o_rechazado'] == 2
        assert r['pct_parcial_o_rechazado'] == 66.7

    def test_un_recaudo_sin_forma_de_pago_no_desaparece(self, client, db, h, ruta_con_tarea):
        """`forzar_cierre_ruta` crea recaudos con `forma_pago=None`. Agruparlos
        bajo una clave vacía los haría invisibles en el desglose."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.RECHAZADO, None)
        r = client.get(_URL, headers=h).get_json()['recaudos']
        assert r['total'] == 1
        assert '(SIN FORMA DE PAGO) | RECHAZADO' in r['matriz']

    def test_sin_entregas_no_inventa_porcentaje(self, client, db, h):
        """`0%` diría «ninguna es parcial». La verdad es «no hay entregas»."""
        r = client.get(_URL, headers=h).get_json()['recaudos']
        assert r['total'] == 0
        assert r['pct_parcial_o_rechazado'] is None


class TestElRezagoDeLiquidacion:

    def test_cuenta_las_entregadas_sin_liquidar(self, client, db, h, ruta_con_tarea):
        ruta, _ = ruta_con_tarea
        ruta.estado = 'ENTREGADA'
        ruta.estado_financiero = EstadoFinancieroRuta.PENDIENTE
        ruta.fecha_entregada = datetime.utcnow() - timedelta(days=4)
        db.session.commit()

        z = client.get(_URL, headers=h).get_json()['rezago_liquidacion']
        assert z['rutas_entregadas_sin_liquidar'] == 1
        assert z['dias_max'] >= 3          # margen por zona horaria

    def test_una_ruta_liquidada_no_cuenta(self, client, db, h, ruta_con_tarea):
        ruta, _ = ruta_con_tarea
        ruta.estado = 'ENTREGADA'
        ruta.estado_financiero = EstadoFinancieroRuta.LIQUIDADA
        ruta.fecha_entregada = datetime.utcnow() - timedelta(days=9)
        db.session.commit()
        z = client.get(_URL, headers=h).get_json()['rezago_liquidacion']
        assert z['rutas_entregadas_sin_liquidar'] == 0

    def test_el_numero_viaja_con_su_advertencia(self, client, db, h):
        """Un dato que se puede malinterpretar viaja con su interpretación o no
        viaja. El rezago de hoy es un PISO: hoy liquidar no tiene consecuencia
        fiscal y con el rediseño la tendría."""
        z = client.get(_URL, headers=h).get_json()['rezago_liquidacion']
        assert 'nota' in z and z['nota']
        assert 'iso' in z['nota'].lower() or 'estimación' in z['nota'].lower()

    def test_una_ruta_sin_fechas_no_revienta(self, client, db, h, ruta_con_tarea):
        """Sin fecha no hay días. Declarar `null` y no inventar 0, que se
        leería como «se liquidó el mismo día»."""
        ruta, _ = ruta_con_tarea
        ruta.estado = 'ENTREGADA'
        ruta.estado_financiero = EstadoFinancieroRuta.PENDIENTE
        ruta.fecha_entregada = None
        ruta.fecha_programada = None
        db.session.commit()
        z = client.get(_URL, headers=h).get_json()['rezago_liquidacion']
        assert z['rutas_entregadas_sin_liquidar'] == 1
        assert z['detalle'][0]['dias'] is None


class TestLasAlertasDeCondicionAusente:

    def test_cuenta_las_alertas_del_gateway(self, client, db, h):
        from app.models.siesa_job import SiesaJob
        SiesaJob.encolar('ALERTA_EMAIL', {
            'asunto': '[WMS ALERTA] Factura emitida como CONTADO por data incompleta en Siesa',
            'tercero': '900123456',
        })
        db.session.commit()
        c = client.get(_URL, headers=h).get_json()['condicion_pago_ausente']
        assert c['alertas'] == 1

    def test_no_cuenta_otras_alertas(self, client, db, h):
        from app.models.siesa_job import SiesaJob
        SiesaJob.encolar('ALERTA_EMAIL', {'asunto': 'otra cosa cualquiera'})
        db.session.commit()
        c = client.get(_URL, headers=h).get_json()['condicion_pago_ausente']
        assert c['alertas'] == 0

    def test_explica_por_que_no_sirve_contar_facturas(self, client, db, h):
        """La nota existe porque la pregunta se estuvo respondiendo con el
        conteo equivocado durante días."""
        c = client.get(_URL, headers=h).get_json()['condicion_pago_ausente']
        assert 'fallback' in (c.get('nota') or '').lower()


class TestPermisos:

    def test_sin_token_no_pasa(self, client):
        assert client.get(_URL).status_code == 401

    def test_un_operario_no_ve_el_desglose(self, client, jwt_token):
        r = client.get(_URL, headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403


class TestElModoDePantalla:
    """`LIBRE` es donde vive el riesgo y no se estaba contando.

    En LIBRE el conductor elige forma de pago sin restricción, incluido
    CREDITO en una parada de contado — y `confirmar_parada` solo valida que el
    valor esté en la lista, nada lo ata a la condición del pedido.

    Y los dos huecos se refuerzan: un pedido con `cond_pago` vacío produce a la
    vez una factura emitida como CONTADO (fallback del gateway) y una parada en
    LIBRE (`es_contado` no es `True` confirmado). El mismo dato faltante abre
    las dos puertas.
    """

    def test_cuenta_las_paradas_en_modo_libre(self, client, db, h, ruta_con_tarea):
        ruta, tarea = ruta_con_tarea
        for modo in ('LIBRE', 'LIBRE', 'DINAMICO'):
            r = _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')
            r.modo_pantalla = modo
        db.session.commit()

        d = client.get(_URL, headers=h).get_json()['recaudos']
        assert d['en_modo_libre'] == 2
        assert d['por_modo_pantalla']['DINAMICO'] == 1

    def test_lo_historico_no_se_cuenta_como_LIBRE(self, client, db, h, ruta_con_tarea):
        """Las paradas anteriores al 2026-08-13 no guardaban el modo. Contarlas
        como LIBRE inflaría justo el número de riesgo; como DINAMICO lo
        escondería. El hueco declarado es el único valor honesto."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')  # sin modo

        d = client.get(_URL, headers=h).get_json()['recaudos']
        assert d['en_modo_libre'] == 0
        assert d['por_modo_pantalla']['(sin registrar)'] == 1

    def test_el_modelo_rechaza_un_modo_inventado(self, db, ruta_con_tarea):
        from sqlalchemy.exc import IntegrityError
        ruta, tarea = ruta_con_tarea
        r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=tarea.id,
                           estado_entrega=EstadoEntrega.ENTREGADO,
                           forma_pago='EFECTIVO', modo_pantalla='CUALQUIERA')
        db.session.add(r)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestElDenominadorDelFallback:
    """Sin denominador, «0 alertas» no dice nada.

    Con 5 facturas emitidas significa «no hemos llegado a probarlo»; con 5.000,
    «el fallback es código muerto». Y acá va a ser chico —la cadena de despacho
    es nueva— así que un cero NO cierra la pregunta.
    """

    def test_viene_el_denominador(self, client, db, h):
        c = client.get(_URL, headers=h).get_json()['condicion_pago_ausente']
        assert 'facturas_emitidas_por_el_gateway' in c

    def test_con_pocas_facturas_no_es_concluyente(self, client, db, h, ruta_con_tarea):
        _, tarea = ruta_con_tarea
        tarea.rm_tipo, tarea.rm_consec = 'RS', 1
        db.session.commit()
        c = client.get(_URL, headers=h).get_json()['condicion_pago_ausente']
        assert c['facturas_emitidas_por_el_gateway'] == 1
        assert c['concluyente'] is False, (
            'con una sola factura emitida, cero alertas no prueba nada')

    def test_sin_remision_no_cuenta_como_factura_emitida(self, client, db, h, ruta_con_tarea):
        """Una tarea sin RM nunca llegó al 142943 — no puede estar en el
        denominador de un fallback que vive ahí."""
        c = client.get(_URL, headers=h).get_json()['condicion_pago_ausente']
        assert c['facturas_emitidas_por_el_gateway'] == 0


class TestLoQueCarteraNecesitaCruzar:
    """La hipótesis del Gestor: paradas donde el conductor pudo elegir y eligió
    CRÉDITO deberían aparecer como facturas abiertas en cartera.

    **No se puede probar hacia atrás.** `modo_pantalla` se empezó a registrar
    el 2026-08-13 y todo lo anterior es NULL — justo el período donde se
    acumularon los $79,3 M. Decirlo importa: una consulta que devuelve vacío
    porque el dato no existía se lee como «no ocurre».

    Lo que sí funciona sobre todo el histórico es el proxy directo: una parada
    marcada CRÉDITO **nunca dispara recibo de caja**, así que su factura queda
    abierta — se haya elegido en modo LIBRE o no. Y para cruzarla contra
    cartera hace falta el identificador, no el conteo.
    """

    def test_cruza_modo_con_forma_de_pago(self, client, db, h, ruta_con_tarea):
        ruta, tarea = ruta_con_tarea
        r1 = _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'CREDITO')
        r1.modo_pantalla = 'LIBRE'
        r2 = _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')
        r2.modo_pantalla = 'LIBRE'
        db.session.commit()

        d = client.get(_URL, headers=h).get_json()['recaudos']
        assert d['modo_x_forma_pago']['LIBRE | CREDITO'] == 1
        assert d['modo_x_forma_pago']['LIBRE | EFECTIVO'] == 1

    def test_lista_las_de_credito_con_identificadores(self, client, db, h, ruta_con_tarea):
        """Un conteo no se puede cruzar contra la cartera. Una lista sí."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'CREDITO')
        db.session.commit()

        pc = client.get(_URL, headers=h).get_json()['paradas_credito']
        assert pc['total'] == 1
        fila = pc['detalle'][0]
        assert fila['pedido'] == 'PD-777'
        assert 'ruta_id' in fila and 'cliente' in fila

    def test_una_parada_sin_modo_registrado_igual_aparece(self, client, db, h, ruta_con_tarea):
        """El proxy funciona hacia atrás: no depende de `modo_pantalla`."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'CREDITO')  # sin modo
        db.session.commit()
        pc = client.get(_URL, headers=h).get_json()['paradas_credito']
        assert pc['total'] == 1
        assert pc['detalle'][0]['modo_pantalla'] is None

    def test_declara_si_trunco(self, client, db, h):
        """Una lista recortada sin avisar se lee como «esas son todas» — la
        misma clase de silencio que el `0%` sin denominador."""
        pc = client.get(_URL, headers=h).get_json()['paradas_credito']
        assert 'truncado' in pc and 'listadas' in pc

    def test_explica_hasta_donde_sirve(self, client, db, h):
        pc = client.get(_URL, headers=h).get_json()['paradas_credito']
        assert '2026-08-13' in pc['nota'], (
            'la nota tiene que decir desde cuándo hay modo_pantalla — sin eso, '
            'un vacío en el histórico se lee como ausencia del fenómeno')


class TestLaFacturaParaElCruceDeCartera:
    """Cartera indexa por FACTURA; la lista salía por pedido, así que el cruce
    era aproximado por cliente y ventana de fecha — y acá ya sabemos qué pasa
    con las cifras aproximadas.

    La FE se lee de lo **persistido**. Este endpoint no llama a Siesa: con un
    tope de 500 paradas, resolverla al vuelo serían cientos de consultas al ERP
    en un solo request.
    """

    def test_incluye_la_factura_persistida(self, client, db, h, ruta_con_tarea):
        ruta, tarea = ruta_con_tarea
        tarea.fe_tipo, tarea.fe_consec = 'FEW', '1466'
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'CREDITO')
        db.session.commit()

        pc = client.get(_URL, headers=h).get_json()['paradas_credito']
        assert pc['detalle'][0]['factura'] == 'FEW-1466'
        assert pc['con_factura'] == 1 and pc['sin_factura'] == 0

    def test_declara_cuantas_no_tienen_factura(self, client, db, h, ruta_con_tarea):
        """Una lista con la mitad de las facturas en `null` se lee como si el
        cruce fuera completo. La cobertura va en la respuesta."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'CREDITO')
        db.session.commit()

        pc = client.get(_URL, headers=h).get_json()['paradas_credito']
        assert pc['detalle'][0]['factura'] is None
        assert pc['sin_factura'] == 1

    def test_el_endpoint_no_llama_a_siesa(self, client, db, h, ruta_con_tarea, monkeypatch):
        """Con 500 paradas, resolver la FE al vuelo sería un bombardeo al ERP
        desde una pantalla de dashboard."""
        from app.services.connekta_gateway import connekta

        def _prohibido(*a, **k):
            raise AssertionError('el desglose llamó a Siesa')

        monkeypatch.setattr(connekta, 'get_detalle_factura', _prohibido, raising=False)
        monkeypatch.setattr(connekta, 'get_pedido_cabecera', _prohibido, raising=False)

        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'CREDITO')
        db.session.commit()
        assert client.get(_URL, headers=h).status_code == 200


class TestLaFEQuedaAnotadaEnLaTarea:
    """La causa raíz del job 440: `TareaPacking` no guardaba la factura, así
    que cada consumidor tenía que ir a Siesa — y el que se equivocó pasó el
    pedido."""

    class _Tarea:
        id = 1
        rm_tipo, rm_consec = 'RS', 7
        tipo_docto_pedido_siesa = 'PD'
        consec_docto_pedido_siesa = '1308'
        fe_tipo = fe_consec = None

    def test_usa_lo_anotado_sin_volver_a_preguntar(self, monkeypatch):
        from app.services.connekta_gateway import connekta
        from app.services.fe_resolver import resolver_fe

        t = self._Tarea()
        t.fe_tipo, t.fe_consec = 'FEW', '1466'
        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(
            connekta, 'get_detalle_factura',
            lambda **k: (_ for _ in ()).throw(AssertionError('volvió a preguntar')),
            raising=False)
        assert resolver_fe(t) == ('FEW', '1466')

    def test_el_doble_de_simulacion_no_se_persiste(self, db, ruta_con_tarea, monkeypatch):
        """Un `SIMFE` guardado sobreviviría al modo simulación y después se
        leería como una factura real. Regla 8 de flota.

        Se ejerce sobre una tarea REAL de la base, no sobre un objeto plano: con
        un objeto plano el `commit` revienta y el `except` de `_anotar` se lo
        traga, así que el test pasaba aunque el guard no existiera. Lo descubrió
        una mutación que no falló.
        """
        from app.services.connekta_gateway import connekta
        from app.services.fe_resolver import resolver_fe

        _, tarea = ruta_con_tarea
        monkeypatch.setattr(connekta, 'modo_simulacion', True, raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura', lambda **k: [], raising=False)

        tipo, _c = resolver_fe(tarea)
        assert tipo == 'SIMFE'
        db.session.refresh(tarea)
        assert tarea.fe_tipo is None, 'el doble se persistió — se leería como real'


    def test_anotar_rechaza_el_doble_directamente(self, db, ruta_con_tarea):
        """El guard de `_anotar` no se alcanza hoy desde `resolver_fe` —la rama
        de simulación retorna antes— así que se ejerce directo.

        Un guard que nunca se ejecuta es indistinguible de uno que no está: la
        mutación que lo borró no tumbó ningún test hasta que se escribió esto.
        """
        from app.services.fe_resolver import _anotar

        _, tarea = ruta_con_tarea
        _anotar(tarea, 'SIMFE', '999')
        db.session.refresh(tarea)
        assert tarea.fe_tipo is None

        _anotar(tarea, 'FEW', '1466')
        db.session.refresh(tarea)
        assert (tarea.fe_tipo, tarea.fe_consec) == ('FEW', '1466')


class TestLaDistribucionDeValorPorParada:
    """El insumo del tope de contado declarado, que hoy no existía.

    El valor se calculaba al vuelo, se mostraba al conductor y se descartaba.
    Para elegir un tope hace falta la distribución, y sacarla exigía volver a
    consultar Siesa parada por parada.

    **No es `monto_cobrado`.** Ese es lo que se cobró, no lo que estaba en
    riesgo: en un rechazo vale cero y la exposición fue el total. Un tope
    calculado sobre esa variable quedaría sistemáticamente bajo — y bajo
    significa que deja pasar lo que venía a frenar.
    """

    def _con_valor(self, db, ruta, tarea, valores):
        from app.models.packing import TareaPacking
        import uuid
        for v in valores:
            t2 = TareaPacking(codigo=f'PK-V-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                              almacen_id=tarea.almacen_id, tipo_docto_pedido_siesa='PD',
                              consec_docto_pedido_siesa=1, valor_factura=v)
            db.session.add(t2); db.session.flush()
            _recaudo(db, ruta.id, t2.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')
        db.session.commit()

    def test_percentiles_sobre_los_valores_anotados(self, client, db, h, ruta_con_tarea):
        ruta, tarea = ruta_con_tarea
        self._con_valor(db, ruta, tarea, [100, 200, 300, 400, 8_000_000])

        d = client.get(_URL, headers=h).get_json()['distribucion_valor_parada']
        assert d['con_valor'] == 5
        assert d['percentiles']['p50'] == 300
        assert d['percentiles']['max'] == 8_000_000

    def test_la_cobertura_viaja_con_los_percentiles(self, client, db, h, ruta_con_tarea):
        """Un umbral puesto sobre una muestra sesgada deja pasar lo que venía a
        frenar. Y el sesgo no es aleatorio: tiene valor la parada cuya FE
        alguien resolvió."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')  # sin valor
        self._con_valor(db, ruta, tarea, [500])

        d = client.get(_URL, headers=h).get_json()['distribucion_valor_parada']
        assert d['con_valor'] == 1
        assert d['de_un_total_de'] == 2
        assert d['cobertura_pct'] == 50.0

    def test_sin_valores_no_inventa_percentiles(self, client, db, h, ruta_con_tarea):
        """`0` como p50 diría que la parada mediana vale cero. La verdad es que
        no hay ninguna medida."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')
        db.session.commit()

        d = client.get(_URL, headers=h).get_json()['distribucion_valor_parada']
        assert d['percentiles'] is None
        assert d['cobertura_pct'] is None
        assert d['con_valor'] == 0

    def test_los_percentiles_son_valores_reales_no_interpolados(self, client, db, h,
                                                                ruta_con_tarea):
        """Con pocas muestras, interpolar inventa un monto que no corresponde a
        ninguna parada — y sobre eso se pondría un tope."""
        ruta, tarea = ruta_con_tarea
        self._con_valor(db, ruta, tarea, [10, 20, 30])
        d = client.get(_URL, headers=h).get_json()['distribucion_valor_parada']
        assert d['percentiles']['p50'] in (10, 20, 30)

    def test_la_nota_advierte_contra_monto_cobrado(self, client, db, h, ruta_con_tarea):
        """La advertencia va donde hay percentiles: es ahí donde alguien está
        a punto de elegir un tope sobre la variable equivocada."""
        ruta, tarea = ruta_con_tarea
        self._con_valor(db, ruta, tarea, [1000])
        d = client.get(_URL, headers=h).get_json()['distribucion_valor_parada']
        assert 'cobrado' in d['nota']

    def test_el_modelo_rechaza_un_valor_negativo(self, db, ruta_con_tarea):
        from sqlalchemy.exc import IntegrityError
        _, tarea = ruta_con_tarea
        tarea.valor_factura = -1
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


class TestLaCondicionDeclaradaPorElPedido:
    """Responde la verificación que se venía planteando como «abrir un pedido
    en Siesa, dos minutos» — sobre todos los pedidos a la vez.

    Si sale todo `contado (C01)`, el bloqueo por mora nunca frena rutas y el
    método «No retener pedidos de contado» ya cubre el caso. Si sale `C02`,
    Siesa lo lee como crédito de un día y retiene.
    """

    def _parada(self, db, ruta, tarea, cond):
        from app.models.packing import TareaPacking
        import uuid
        t2 = TareaPacking(codigo=f'PK-C-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                          almacen_id=tarea.almacen_id, tipo_docto_pedido_siesa='PD',
                          consec_docto_pedido_siesa=1, cond_pago=cond)
        db.session.add(t2); db.session.flush()
        _recaudo(db, ruta.id, t2.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')
        db.session.commit()

    def test_clasifica_lo_que_declara_el_pedido(self, client, db, h, ruta_con_tarea,
                                                monkeypatch):
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        ruta, tarea = ruta_con_tarea
        self._parada(db, ruta, tarea, 'C01')
        self._parada(db, ruta, tarea, 'C02')

        d = client.get(_URL, headers=h).get_json()['recaudos']['condicion_declarada']
        assert d['contado (C01)'] == 1
        assert d['credito (C02)'] == 1

    def test_sin_consultar_no_es_sin_condicion(self, client, db, h, ruta_con_tarea):
        """`(sin consultar)` = nadie abrió esa ruta en línea. `''` = Siesa
        respondió y el pedido no trae condición. Colapsarlos es el defecto que
        ya costó una vez."""
        ruta, tarea = ruta_con_tarea
        _recaudo(db, ruta.id, tarea.id, EstadoEntrega.ENTREGADO, 'EFECTIVO')  # cond_pago None
        self._parada(db, ruta, tarea, '')

        d = client.get(_URL, headers=h).get_json()['recaudos']['condicion_declarada']
        assert d['(sin consultar)'] == 1
        assert d['ausente (vacío)'] == 1
