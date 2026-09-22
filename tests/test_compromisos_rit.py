"""
El despacho suponía que el 174720 había entrado.

    elif s.siesa_requisicion_consec:
        # Flujo completo: 174646 + 174720 ya disparados — usar 174930

Eso mira que exista el consecutivo de la requisición. **No mira si los
compromisos entraron.**

Y ahí está el daño, porque los dos conectores no llevan lo mismo:

    174930  despachar_desde_rit(consec_rit)        → NO manda cantidades.
                                                     Siesa las toma de la RIT.
    173076  items=items_payload                    → `cantidad_enviada`,
                                                     lo que packing confirmó.

Con el 174720 fallido, la RIT conserva las cantidades **originales del
174646**, así que el STS sale por lo pedido:

    packing confirma      7 de 10
    174720 falla
    despacho usa 174930 → Siesa registra 10 en tránsito
    salen físicamente     7
                          ─────
                          3 unidades en la bodega de tránsito

Que es el caso que el propio módulo de traslados declara como el peor: *el
stock no falta ni sobra, está en la bodega puente, donde nadie pregunta. Y
nadie reclama: una tienda que no recibió un traslado que no pidió, no llama.*

## Por qué NO se frena el despacho

El primer diseño era no despachar hasta que los compromisos entraran. No hace
falta: el 173076 **sí** lleva las cantidades reales. Cuando el 174720 no entró
se cae a esa vía —el mismo camino que ya usa la RIT sin consecutivo legible— y
la mercancía sale correcta. Lo que queda es una RIT suelta: condición conocida,
declarada y con reporte propio.

Frenar habría cambiado el modo de fallo de un flujo que funciona para evitar
algo que la vía de respaldo ya resuelve.

## Y el job que prometía un reintento inexistente

`COMPROMISOS_RIT` se encolaba y **no tenía rama** en `_ejecutar_job`: caía en el
`raise` genérico, quemaba los 5 intentos en ~6 h y moría FALLIDO.
"""
import pytest

from app.models.traslado import SolicitudTraslado


@pytest.fixture
def solicitante(db):
    """`solicitante_id` es NOT NULL — un traslado siempre lo pide alguien."""
    from app.models.usuario import Usuario
    u = Usuario.query.filter_by(email='comp_rit@test.com').first()
    if not u:
        u = Usuario(email='comp_rit@test.com', nombre='op', rol='operario', activo=True)
        u.set_password('test123')
        db.session.add(u)
        db.session.commit()
    return u


class TestLaCompuertaDelDespacho:
    """El 174930 solo si los compromisos entraron de verdad."""

    def test_el_modelo_distingue_rit_creada_de_compromisos_registrados(self, db, solicitante):
        """Son dos hechos distintos y hasta hoy uno se leía como el otro."""
        s = SolicitudTraslado(codigo='TR-COMP-1', solicitante_id=solicitante.id, bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NC1', estado='APROBADA')
        s.siesa_requisicion_consec = 4321
        db.session.add(s)
        db.session.commit()
        assert s.siesa_requisicion_consec and not s.siesa_compromisos_ok

    def test_el_defecto_por_AST_no_por_texto(self):
        """La condición del 174930 tiene que exigir **las dos** cosas.

        Por AST: un detector de texto se atrapa en este propio docstring — pasó
        cinco veces en este repo.
        """
        import ast
        import pathlib

        arbol = ast.parse(pathlib.Path('app/services/traslado_service.py').read_text())
        objetivo = None
        for n in ast.walk(arbol):
            if (isinstance(n, ast.Call)
                    and getattr(n.func, 'attr', None) == 'despachar_desde_rit'):
                objetivo = n
        assert objetivo is not None, 'ya no se llama a despachar_desde_rit'

        # La rama que lo contiene tiene que nombrar `siesa_compromisos_ok`.
        guardas = []
        for n in ast.walk(arbol):
            if isinstance(n, ast.If) and n.test:
                cuerpo = [x for x in ast.walk(n) if x is objetivo]
                if cuerpo:
                    guardas += [a.attr for a in ast.walk(n.test)
                                if isinstance(a, ast.Attribute)]
        assert 'siesa_compromisos_ok' in guardas, (
            'el 174930 se dispara sin comprobar que el 174720 entró. Siesa toma '
            'las cantidades de la RIT: si los compromisos no se registraron, el '
            'STS sale por lo PEDIDO y la diferencia queda en tránsito.')

    def test_la_via_de_respaldo_manda_las_cantidades_reales(self):
        """Lo que hace innecesario frenar el despacho.

        Si algún día `registrar_salida_transito` dejara de recibir
        `items_payload`, caer al respaldo dejaría de ser seguro y este arreglo
        se volvería incorrecto en silencio.
        """
        import ast
        import pathlib

        arbol = ast.parse(pathlib.Path('app/services/traslado_service.py').read_text())
        con_items = [
            n for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and getattr(n.func, 'attr', None) in ('registrar_salida_transito',
                                                  'registrar_salida_directa')
            and any(k.arg == 'items' for k in n.keywords)
        ]
        assert len(con_items) >= 2, (
            'alguna vía de respaldo dejó de mandar `items`. Sin las cantidades '
            'de packing, caer al 173076 ya no corrige nada.')


class TestElJobQuePrometiaUnReintentoInexistente:
    def test_compromisos_rit_tiene_rama(self):
        """Por AST sobre el despachador, no por grep."""
        import ast
        import pathlib

        src = pathlib.Path('app/services/siesa_job_service.py').read_text()
        arbol = ast.parse(src)
        tipos = set()
        for n in ast.walk(arbol):
            if isinstance(n, ast.Compare) and isinstance(n.left, ast.Attribute) \
                    and n.left.attr == 'tipo':
                for c in n.comparators:
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        tipos.add(c.value)
        assert 'COMPROMISOS_RIT' in tipos, (
            'volvió a quedar sin rama: cae en el raise genérico, quema 5 '
            'intentos en ~6 h y muere FALLIDO')

    def test_no_pisa_un_despacho_ya_hecho(self, db, solicitante, monkeypatch):
        """Si ya salió el STS por la vía de respaldo, registrar compromisos
        ahora dejaría la RIT diciendo algo distinto de lo que se movió."""
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs

        s = SolicitudTraslado(codigo='TR-COMP-2', solicitante_id=solicitante.id, bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NC1', estado='EN_TRANSITO')
        s.siesa_requisicion_consec = 999
        s.siesa_salida_consec = 555          # ya despachado
        db.session.add(s)
        db.session.commit()

        llamadas = []
        from app.services.siesa_traslado_adapter import siesa_traslado
        monkeypatch.setattr(siesa_traslado, 'registrar_compromisos',
                            lambda **kw: llamadas.append(kw))

        job = SiesaJob(tipo='COMPROMISOS_RIT', estado='PENDIENTE')
        job.payload = f'{{"solicitud_id": {s.id}, "items": []}}'
        res = sjs._ejecutar_job(job)

        assert not llamadas, 'reescribió la RIT sobre un traslado ya despachado'
        assert res.get('omitido') and 'ya despachado' in res['motivo']

    def test_espera_si_la_rit_no_tiene_consecutivo(self, db, solicitante, monkeypatch):
        """`DependenciaPendiente` no gasta reintento: la RIT puede resolverse
        después, y con backoff de ~6 h el job moriría antes."""
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs

        s = SolicitudTraslado(codigo='TR-COMP-3', solicitante_id=solicitante.id, bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NC1', estado='APROBADA')
        db.session.add(s)
        db.session.commit()

        job = SiesaJob(tipo='COMPROMISOS_RIT', estado='PENDIENTE')
        job.payload = f'{{"solicitud_id": {s.id}, "items": []}}'
        with pytest.raises(sjs.DependenciaPendiente):
            sjs._ejecutar_job(job)

    def test_registra_y_abre_la_compuerta(self, db, solicitante, monkeypatch):
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        from app.services.siesa_traslado_adapter import siesa_traslado

        s = SolicitudTraslado(codigo='TR-COMP-4', solicitante_id=solicitante.id, bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NC1', estado='APROBADA')
        s.siesa_requisicion_consec = 777
        s.siesa_error = '174720: timeout'
        db.session.add(s)
        db.session.commit()

        monkeypatch.setattr(siesa_traslado, 'registrar_compromisos',
                            lambda **kw: {'ok': True})
        job = SiesaJob(tipo='COMPROMISOS_RIT', estado='PENDIENTE')
        job.payload = f'{{"solicitud_id": {s.id}, "items": []}}'
        res = sjs._ejecutar_job(job)

        assert res.get('compromisos_registrados')
        assert s.siesa_compromisos_ok is True
        assert s.siesa_error is None

    def test_no_reenvia_si_ya_estaban_registrados(self, db, solicitante, monkeypatch):
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        from app.services.siesa_traslado_adapter import siesa_traslado

        s = SolicitudTraslado(codigo='TR-COMP-5', solicitante_id=solicitante.id, bodega_origen_siesa='NB1',
                              bodega_destino_siesa='NC1', estado='APROBADA')
        s.siesa_requisicion_consec = 888
        s.siesa_compromisos_ok = True
        db.session.add(s)
        db.session.commit()

        llamadas = []
        monkeypatch.setattr(siesa_traslado, 'registrar_compromisos',
                            lambda **kw: llamadas.append(kw))
        job = SiesaJob(tipo='COMPROMISOS_RIT', estado='PENDIENTE')
        job.payload = f'{{"solicitud_id": {s.id}, "items": []}}'
        assert sjs._ejecutar_job(job).get('idempotente')
        assert not llamadas


class TestLaBanderaSeEnciendeDespuesDelPOST:
    """Al revés de la Regla 6, y a propósito.

    La Regla 6 pre-marca para que un crash entre el POST y el commit no produzca
    un documento duplicado. Acá la bandera **no evita un duplicado: abre una
    compuerta**. Pre-marcarla dejaría la puerta abierta ante ese mismo crash y
    despacharíamos por 174930 sobre una suposición.

    Ante la duda, reenviar el 174720 reafirma las mismas cantidades sobre la
    misma RIT; mandar un STS por cantidades que nadie empacó no se deshace.
    """

    def test_no_se_premarca(self):
        import ast
        import pathlib

        arbol = ast.parse(pathlib.Path('app/services/traslado_service.py').read_text())
        for n in ast.walk(arbol):
            if not (isinstance(n, ast.Try)):
                continue
            llamada = [c for c in ast.walk(n)
                       if isinstance(c, ast.Call)
                       and getattr(c.func, 'attr', None) == 'registrar_compromisos']
            if not llamada:
                continue
            marcas = [a.lineno for a in ast.walk(n)
                      if isinstance(a, ast.Assign)
                      for t in a.targets
                      if isinstance(t, ast.Attribute) and t.attr == 'siesa_compromisos_ok'
                      and isinstance(a.value, ast.Constant) and a.value.value is True]
            assert marcas, 'no se marca el éxito en ninguna parte'
            assert all(m > llamada[0].lineno for m in marcas), (
                'la bandera se enciende ANTES del POST. Un crash entre el POST y '
                'el commit dejaría la compuerta abierta y el 174930 despacharía '
                'sobre una suposición.')
