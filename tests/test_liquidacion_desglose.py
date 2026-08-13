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
