"""
La liquidación completa, con los servicios reales, capturando lo que saldría.

## Por qué hace falta esto además de `test_payload_vs_docx`

Aquel lee el **código fuente** y verifica que el diccionario declare los campos
del spec. Es necesario y no alcanza: el 2026-08-11, la primera vez que el
módulo corrió de punta a punta contra producción, **los tres conectores
financieros fallaron**.

    142888 (RC)  →  22 de 33 campos en el encabezado, 15 de 20 en Caja
    142882 (DC)  →  4 campos faltantes
    la NC        →  apuntaba a 250696, que no está registrado en Connekta,
                    y el llamador le pasaba el PEDIDO donde va la factura

Y el patrón que dejó ese día: **los conectores incompletos son exactamente los
que nunca se ejercitaron.** El 251126 se había probado en vivo en julio y
estaba entero. Los que nunca corrieron, no.

Un test que lee el fuente no distingue «el campo está declarado» de «el campo
sale con valor cuando alguien liquida de verdad». Este sí: **arma la ruta con
los servicios reales, liquida, y mira los payloads que habrían viajado.**

## Lo que este arnés NO prueba

Que Siesa los acepte. Eso es una corrida contra producción y es la única
compuerta que queda abierta antes del go-live — no hay forma de simularla.

Lo que sí cierra es el modo de fallo del 11 de agosto: un payload incompleto
que nadie vio porque nadie lo generó nunca.
"""
import pytest

from tests.flujo import conductor_de_flujo as cf

#: El pedido que `hacer_packing` le pone a la tarea. Vive acá porque la fila de
#: CxC del arnés tiene que cruzar contra ÉL —`f353_*_docto_cruce` referencia el
#: pedido, no la factura— y si el arnés cambia sin que esto cambie, el cruce
#: deja de encontrarse y el fallo se ve como «cuenta CxC vacía».
_PEDIDO_DEL_ARNES = ('PD', '1')


@pytest.fixture
def actores(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('operario', 'op_liq@test.com'),
                       ('conductor', 'cond_liq@test.com'),
                       ('admin', 'admin_liq@test.com')):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
            u.set_password('test123')
            db.session.add(u)
            db.session.flush()
        out[rol] = u
    db.session.commit()
    return out


@pytest.fixture
def capturados(monkeypatch):
    """Intercepta `_post` — el punto único por donde sale todo a Connekta."""
    from app.services.connekta_gateway import connekta
    salidas = []

    def _fake(id_conector, nombre_conector, payload, url=None, extra_params=None):
        salidas.append({'conector': str(id_conector), 'nombre': nombre_conector,
                        'payload': payload})
        return {'codigo': 0, 'mensaje': 'Transacción Exitosa (arnés)'}

    monkeypatch.setattr(connekta, '_post', _fake)
    monkeypatch.setattr(connekta, 'modo_simulacion', False)
    monkeypatch.setattr(connekta, 'modo_ensayo', False)
    return salidas


@pytest.fixture
def siesa_responde(monkeypatch):
    """Las LECTURAS de Siesa, con respuestas realistas.

    Se estuban solo los GET. Los POST siguen pasando por `_post`, que es lo
    que este arnés existe para mirar — estubar el envío haría que el test
    verificara su propio mock, que es el defecto que `test_siesa_contracts`
    tenía y por el que 25 tests estaban en verde sobre un conector roto.

    Sin esto la liquidación se niega a seguir —«co_factura vacío, no se puede
    crear cruce RC»— y hace bien: un RC sin cruce queda colgado en Siesa.
    """
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [
        {'f470_rowid_movto': 2857568, 'f470_vlr_neto': 50000.0,
         'f461_vlr_bruto': 42016.81, 'f461_vlr_imp': 7983.19,
         'f470_id_co': '003'},
    ])
    monkeypatch.setattr(connekta, 'get_pedido_cabecera', lambda *a, **k: {
        'f200_id_pedido_fact': '1000134388',
        'f461_id_sucursal_pedido_rem': '001',
        'f430_id_cond_pago': 'C02',
        'f430_id_co': '003',          # de acá sale `co_factura`
    })
    # `f353_*_docto_cruce` trae el **PEDIDO**, no la factura — verificado en
    # vivo el 2026-08-11. Es la asimetría que hizo que `_factura_saldada_en_siesa`
    # buscara por la clave equivocada y reenviara recibos de caja (RC-00002744).
    # El arnés siembra la fila con la clave real; si alguien vuelve a la de la
    # factura, el cruce deja de encontrarse y estos tests lo ven.
    monkeypatch.setattr(connekta, 'get_cxc_general', lambda *a, **k: [
        {'f253_id': '13050501', 'f353_id_co': '003',
         'f353_total_db': 50000.0, 'f353_total_cr': 0.0,
         'f353_id_tipo_docto_cruce': 'PD',
         'f353_consec_docto_cruce': _PEDIDO_DEL_ARNES[1],
         'f353_fecha_vcto': '2026-08-15'},
    ])
    return connekta


@pytest.fixture
def ruta_entregada(db, almacen, actores, monkeypatch, siesa_responde):
    """Una ruta de contado entregada y cobrada — el caso que produce RC."""
    from app.models.packing import TareaPacking

    connekta = siesa_responde
    monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01')
    monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02')
    flujo = cf.flujo_completo(
        db, almacen, actores['operario'].id, actores['conductor'].id,
        estado_entrega='ENTREGADO', forma_pago='EFECTIVO', monto_cobrado=50000)

    # El consecutivo de la FE **lo asigna Siesa** al procesar el 142943: sin
    # ERP no hay forma de que el arnés lo produzca, y `resolver_fe` saldría a
    # la red a buscarlo. Es el único dato que se siembra, y se siembra acá —en
    # la frontera— en vez de estubar `resolver_fe`, que es lo que el job 440
    # rompió (le pasaba el PEDIDO donde va la factura).
    tarea = TareaPacking.query.get(flujo.packing_id)
    tarea.fe_tipo, tarea.fe_consec = 'FEW', '1466'
    db.session.commit()
    return flujo


def _payload_de(salidas, conector):
    for s in salidas:
        if s['conector'] == conector:
            return s['payload']
    return None


def _campos_de(payload, seccion):
    """Los nombres de campo de una sección, sea lista de dicts o dict."""
    bloque = payload.get(seccion)
    if isinstance(bloque, list) and bloque:
        return set(bloque[0].keys())
    if isinstance(bloque, dict):
        return set(bloque.keys())
    return set()


class TestElReciboDeCajaSaleCompleto:
    """El conector que mandaba 22 de 33 campos y nadie lo sabía."""

    @pytest.fixture
    def rc(self, capturados):
        from app.services.connekta_gateway import connekta
        connekta.trigger_recibo_caja(
            tercero_nit='1000134388', sucursal='001', monto=50000,
            forma_pago='EFECTIVO', tipo_docto_fe='FEW', consec_fe=1466,
            co_factura='003', cuenta_cxc='13050501', notas='arnés')
        p = _payload_de(capturados, '142888')
        assert p is not None, 'el RC no llegó a `_post` — no se envió nada'
        return p

    def test_las_secciones_del_spec_estan_todas(self, rc):
        """Inicial → RCyotrosingresos → Caja → CxC → Final."""
        faltan = [s for s in ('RCyotrosingresos', 'Caja', 'CxC')
                  if not _campos_de(rc, s)]
        assert not faltan, f'secciones vacías o ausentes: {faltan}'

    @pytest.mark.parametrize('seccion,minimo', [
        ('RCyotrosingresos', 33),   # el que salió con 22
        ('Caja', 20),               # el que salió con 15
    ])
    def test_ninguna_seccion_sale_corta(self, rc, seccion, minimo):
        """Connekta arma un plano **posicional**: omitir un campo acorta la
        línea y corre todo lo que sigue. Por eso el conteo importa y no solo
        la presencia de los que nos parecen relevantes."""
        campos = _campos_de(rc, seccion)
        assert len(campos) >= minimo, (
            f'{seccion} sale con {len(campos)} campos, el spec exige {minimo}. '
            f'Es exactamente el fallo del 2026-08-11.')

    def test_ningun_campo_sale_en_none(self, rc):
        """`None` no es lo mismo que cadena vacía: serializa distinto y puede
        colapsar el registro. El spec pide el campo presente, aunque vaya
        vacío."""
        nulos = []
        for seccion in ('RCyotrosingresos', 'Caja', 'CxC'):
            bloque = rc.get(seccion)
            filas = bloque if isinstance(bloque, list) else [bloque or {}]
            for fila in filas:
                nulos += [f'{seccion}.{k}' for k, v in fila.items() if v is None]
        assert not nulos, f'campos en None: {nulos}'

    def test_el_cruce_apunta_a_la_factura_y_no_al_pedido(self, rc):
        """El defecto de la NC del 11 de agosto, en el otro conector.

        Allá el llamador pasaba `PD-1308` donde iba la factura, en seis sitios,
        con la variable llamada `tipo_docto_fe`. Acá se verifica que el
        consecutivo cruzado sea el de la FE que se le pasó.
        """
        cxc = rc.get('CxC')
        fila = (cxc[0] if isinstance(cxc, list) else cxc) or {}
        crudo = ' '.join(str(v) for v in fila.values())
        assert '1466' in crudo, (
            'el consecutivo de la factura no aparece en la sección CxC — '
            'el recibo no se va a cruzar contra nada')

    def test_el_valor_va_en_el_formato_de_21_caracteres(self, rc):
        """Regla 4. Un decimal mal formateado desalinea el plano entero."""
        fila = rc['RCyotrosingresos']
        fila = fila[0] if isinstance(fila, list) else fila
        valores = [v for k, v in fila.items()
                   if k.upper().startswith('F357_VALOR') and isinstance(v, str)]
        assert valores, 'no se encontró ningún F357_VALOR_* en el encabezado'
        assert all(len(v) == 21 for v in valores), \
            f'valores fuera de los 21 chars: {[v for v in valores if len(v) != 21]}'


class TestElCicloCompletoDeUnaRutaCobrada:
    """De la entrega al recibo, por el camino real."""

    def test_la_parada_quedo_cobrada(self, ruta_entregada):
        from app.models.recaudo_entrega import RecaudoEntrega
        r = RecaudoEntrega.query.get(ruta_entregada.recaudo_id)
        assert r is not None and float(r.monto_cobrado or 0) == 50000

    def test_liquidar_encola_el_recibo_de_caja(self, db, ruta_entregada,
                                               actores, capturados):
        """El paso que nunca completó una corrida.

        No se verifica que Siesa lo acepte —eso es producción— sino que el
        WMS llegue a generarlo: el 11 de agosto la cadena se cortaba antes.
        """
        from app.models.siesa_job import SiesaJob
        from app.services.liquidacion_service import LiquidacionService

        res = LiquidacionService.registrar_cobro_recaudo(
            ruta_entregada.recaudo_id, admin_id=actores['admin'].id)
        assert res, 'registrar_cobro_recaudo no devolvió nada'

        jobs = SiesaJob.query.filter(
            SiesaJob.tipo.in_(('RECIBO_CAJA', 'NOTA_CREDITO_FACTURA'))).all()
        assert jobs, ('la liquidación no encoló ningún documento — el ciclo '
                      'se corta antes de Siesa y el saldo queda abierto')

    def test_el_recaudo_no_se_marca_enviado_sin_haberlo_enviado(
            self, db, ruta_entregada, actores):
        """Las tres banderas de idempotencia financiera (2026-08-13).

        El endpoint encolaba y **acto seguido encendía la bandera que el
        ejecutor usa como guarda**: cada job se declaraba idempotente y se
        marcaba completado sin enviar nada. Ningún documento llegó nunca a
        Siesa por esa vía, y el tablero informaba éxito.
        """
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.services.liquidacion_service import LiquidacionService

        LiquidacionService.registrar_cobro_recaudo(
            ruta_entregada.recaudo_id, admin_id=actores['admin'].id)
        r = RecaudoEntrega.query.get(ruta_entregada.recaudo_id)
        assert not r.siesa_rc_triggered, (
            'el recaudo quedó marcado como enviado al ENCOLAR. El ejecutor va '
            'a leer esa bandera, declararse idempotente y no mandar nada.')

    def test_el_job_ejecutado_produce_el_payload_del_142888(
            self, db, ruta_entregada, actores, capturados):
        """**Éste es el tramo que falló el 2026-08-11.**

        Los tests de arriba llaman `trigger_recibo_caja` a mano. Éste ejecuta
        el job que la liquidación encoló, por el mismo camino que el DLQ — que
        es donde el llamador puede pasarle el argumento equivocado (la NC
        recibía el PEDIDO donde iba la factura, en seis sitios, con la variable
        llamada `tipo_docto_fe`).

        Un payload correcto invocado con datos incorrectos produce un
        documento incorrecto, y `test_payload_vs_docx` no lo puede ver.
        """
        from app.models.siesa_job import SiesaJob
        from app.services.liquidacion_service import LiquidacionService
        from app.services.siesa_job_service import _ejecutar_job

        LiquidacionService.registrar_cobro_recaudo(
            ruta_entregada.recaudo_id, admin_id=actores['admin'].id)

        job = SiesaJob.query.filter_by(tipo='RECIBO_CAJA').first()
        assert job is not None, 'la liquidación no encoló el recibo de caja'
        _ejecutar_job(job)

        rc = _payload_de(capturados, '142888')
        assert rc is not None, (
            'el job RECIBO_CAJA se ejecutó y NO llamó a `_post`. Es el modo de '
            'fallo de las banderas de idempotencia: el job se declara hecho y '
            'no manda nada, y el tablero informa éxito.')
        assert len(_campos_de(rc, 'RCyotrosingresos')) >= 33
        assert len(_campos_de(rc, 'Caja')) >= 20

    def test_el_recibo_cruza_contra_la_factura_de_esta_tarea(
            self, db, ruta_entregada, actores, capturados):
        """Que el documento salga completo no dice contra qué se cruza."""
        from app.models.siesa_job import SiesaJob
        from app.services.liquidacion_service import LiquidacionService
        from app.services.siesa_job_service import _ejecutar_job

        LiquidacionService.registrar_cobro_recaudo(
            ruta_entregada.recaudo_id, admin_id=actores['admin'].id)
        job = SiesaJob.query.filter_by(tipo='RECIBO_CAJA').first()
        _ejecutar_job(job)

        rc = _payload_de(capturados, '142888')
        cxc = rc.get('CxC')
        fila = (cxc[0] if isinstance(cxc, list) else cxc) or {}
        crudo = ' '.join(str(v) for v in fila.values())
        assert '1466' in crudo, (
            f'el RC no cruza contra FEW-1466. Sección CxC: {fila}')
        assert 'PD' not in str(fila.get('F353_ID_TIPO_DOCTO_CRUCE', '')), (
            'el cruce apunta al PEDIDO, no a la factura — es el defecto del '
            'job 440, que consultaba una factura de tipo PD')
