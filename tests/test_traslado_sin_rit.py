"""`TRASLADO_USA_RIT` — la RIT (174646 → 174720 → 174930) nace APAGADA.

Prueba real contra Siesa QA (2026-09-21):

  · ST-20260921-0471: la RIT entró, el WMS no pudo leer su consecutivo (el 401 de
    la consulta, que era el endpoint equivocado), el STS salió por el 173076 y en
    Siesa quedó una requisición suelta con unidades reservadas.
  · ST-20260921-574D, ya con la lectura arreglada: la RIT se creó y se leyó al
    instante (consecutivo 149) y el cierre de packing llamó al 174720, que Siesa
    RECHAZÓ (sección `Movimiento de Seriales` inexistente y, quitada, el registro
    405 con tamaño 226 contra 303). El cierre abortó y el traslado quedó atascado.

O sea: arreglar la lectura de la RIT sin arreglar el 174720 bloquea el cierre de
cada traslado. Por eso el default es apagado, y una RIT ya existente se IGNORA
aunque el traslado traiga su consecutivo guardado.

Se ejerce el servicio de verdad —no se busca el nombre de la función en el
fuente— porque quitar la condición dejaba la llamada intacta en una rama muerta
y un test de texto seguía en verde.
"""
import pytest


@pytest.fixture
def solicitud_en_picking(db, almacen):
    from app.models.usuario import Usuario
    from tests.flujo import conductor_de_flujo as cf

    ids = {}
    for rol, email in (('tienda', 'tienda_sinrit@test.com'),
                       ('admin', 'admin_sinrit@test.com'),
                       ('operario', 'op_sinrit@test.com')):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        ids[rol] = u.id
    db.session.commit()
    s = cf.flujo_traslado(db, almacen, ids['tienda'], ids['admin'], ids['operario'])
    return s, ids['operario']


@pytest.fixture
def llamadas_rit(monkeypatch):
    from app.services import traslado_service as ts

    llamadas = []

    def _crear_rit(**kw):
        llamadas.append(kw)
        return {'simulado': True}

    monkeypatch.setattr(ts.siesa_traslado, 'crear_rit', _crear_rit)
    return llamadas


class TestNaceApagada:
    def test_sin_variable_no_se_crea_ninguna_rit(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        from app.services.traslado_service import TrasladoService

        monkeypatch.delenv('TRASLADO_USA_RIT', raising=False)
        s, operario = solicitud_en_picking

        TrasladoService.confirmar_picking_traslado(s.id, operario)

        db.session.refresh(s)
        assert llamadas_rit == [], 'se creó una RIT sin que nadie la encendiera'
        assert s.estado == 'EN_PACKING'
        assert s.siesa_requisicion_consec is None
        assert s.siesa_error is None, (
            'sin RIT no hay huérfana que avisar: el aviso mandaría a alguien '
            'a cerrar una requisición que nunca se creó')

    @pytest.mark.parametrize('valor', ['', 'false', 'FALSE', '0', 'no', ' False ', 'cualquier cosa'])
    def test_solo_un_si_explicito_la_enciende(self, monkeypatch, valor):
        from app.services.traslado_service import traslado_usa_rit

        monkeypatch.setenv('TRASLADO_USA_RIT', valor)
        assert traslado_usa_rit() is False

    @pytest.mark.parametrize('valor', ['true', 'TRUE', ' True ', '1', 'yes', 'si', 'sí'])
    def test_estas_formas_la_encienden(self, monkeypatch, valor):
        from app.services.traslado_service import traslado_usa_rit

        monkeypatch.setenv('TRASLADO_USA_RIT', valor)
        assert traslado_usa_rit() is True


class TestEncendidaSigueFuncionando:
    def test_con_true_se_dispara_la_rit(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        from app.services.traslado_service import TrasladoService

        monkeypatch.setenv('TRASLADO_USA_RIT', 'true')
        s, operario = solicitud_en_picking

        TrasladoService.confirmar_picking_traslado(s.id, operario)

        assert len(llamadas_rit) == 1
        assert llamadas_rit[0]['codigo'] == s.codigo


class TestUnaRitYaExistenteSeIgnoraConLaVariableApagada:
    """El caso que dejó ST-20260921-574D atascado: el traslado YA tenía el
    consecutivo 149 guardado, y con la variable apagada el cierre igual habría
    entrado al 174720."""

    def test_el_consecutivo_efectivo_es_none_si_esta_apagada(
            self, db, solicitud_en_picking, monkeypatch):
        from app.services.traslado_service import TrasladoService

        s, _ = solicitud_en_picking
        s.siesa_requisicion_consec = 149
        monkeypatch.setenv('TRASLADO_USA_RIT', 'false')
        assert TrasladoService.consec_rit_efectivo(s) is None
        monkeypatch.setenv('TRASLADO_USA_RIT', 'true')
        assert TrasladoService.consec_rit_efectivo(s) == 149

    def test_el_despacho_sale_por_173076_aunque_haya_rit_guardada(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        from app.services import traslado_service as ts

        monkeypatch.setenv('TRASLADO_USA_RIT', 'false')
        s, operario = solicitud_en_picking
        s.modo_transferencia = 'EN_TRANSITO'
        s.bodega_transito_siesa = 'TRA1'
        db.session.commit()
        ts.TrasladoService.confirmar_picking_traslado(s.id, operario)
        s.siesa_requisicion_consec = 149
        s.siesa_compromisos_ok = True   # el caso más tentador para el 174930
        db.session.commit()

        enviados = []
        monkeypatch.setattr(
            ts.siesa_traslado, 'registrar_salida_transito',
            lambda **kw: enviados.append(kw) or {'simulado': True})
        monkeypatch.setattr(
            ts.siesa_traslado, 'despachar_desde_rit',
            lambda **kw: pytest.fail('con la RIT apagada no puede usarse el 174930'))

        ts.TrasladoService.despachar(s.id)

        assert len(enviados) == 1
        assert enviados[0]['consec_requisicion'] is None

    def test_el_cierre_de_packing_no_llama_al_174720(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        """El camino REAL: `cerrar_packing` → `traslado_closer`. Ahí fue donde el
        174720 rechazado bloqueó el cierre."""
        from app.services import traslado_service as ts
        from app.services.closing.traslado_closer import TrasladoPackingCloser
        from app.services.siesa_traslado_adapter import siesa_traslado
        from app.models.packing import TareaPacking

        monkeypatch.setenv('TRASLADO_USA_RIT', 'false')
        s, operario = solicitud_en_picking
        ts.TrasladoService.confirmar_picking_traslado(s.id, operario)
        s.siesa_requisicion_consec = 149
        tarea = TareaPacking.query.filter_by(solicitud_id=s.id).first()
        tarea.estado = 'VERIFICADO'
        for it in tarea.items:
            it.cantidad_real = it.cantidad_esperada
        db.session.commit()

        monkeypatch.setattr(
            siesa_traslado, 'registrar_compromisos',
            lambda **kw: pytest.fail('el cierre llamó al 174720 con la RIT apagada'))
        encolados = []
        from app.models.siesa_job import SiesaJob
        monkeypatch.setattr(SiesaJob, 'encolar',
                            classmethod(lambda cls, **kw: encolados.append(kw)))
        monkeypatch.setattr('app.services.siesa_job_service.disparar_dlq_inmediato',
                            lambda: None)

        res = TrasladoPackingCloser().ejecutar_cierre(
            tarea.id, [{'tipo': 'Caja', 'cantidad': 1}], usuario_id=operario)
        assert res.exitoso, res.error
        assert len(encolados) == 1
        assert encolados[0]['payload']['consec_rit'] is None, (
            'el job lleva una RIT que el flujo no debe usar: el STS iría por 174930')

    def test_el_job_encolado_antes_con_rit_en_el_payload_sale_por_173076(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        """El ejecutor del DLQ es quien decide entre 174930 y 173076. Un job ya
        encolado (antes de apagar la variable) trae `consec_rit` en su payload: no
        puede mandar al 174930."""
        from app.services import traslado_service as ts
        from app.services.siesa_job_service import _ejecutar_job
        from app.services.siesa_traslado_adapter import siesa_traslado
        from app.models.siesa_job import SiesaJob

        monkeypatch.setenv('TRASLADO_USA_RIT', 'false')
        s, operario = solicitud_en_picking
        s.modo_transferencia = 'EN_TRANSITO'
        s.bodega_transito_siesa = 'TRA1'
        db.session.commit()
        ts.TrasladoService.confirmar_picking_traslado(s.id, operario)
        s.siesa_requisicion_consec = 149
        db.session.commit()

        job = SiesaJob.encolar(
            tipo='DESPACHO_TRASLADO',
            payload={'solicitud_id': s.id, 'consec_rit': 149, 'codigo': s.codigo,
                     'bodega_origen': s.bodega_origen_siesa,
                     'bodega_destino': s.bodega_destino_siesa,
                     'items': [{'codigo_siesa': 'X', 'cantidad': 1}]},
            referencia_tipo='SolicitudTraslado', referencia_id=s.id)
        db.session.commit()

        enviados = []
        monkeypatch.setattr(siesa_traslado, 'registrar_salida_transito',
                            lambda **kw: enviados.append(kw) or {'simulado': True})
        monkeypatch.setattr(
            siesa_traslado, 'despachar_desde_rit',
            lambda **kw: pytest.fail('el job usó el 174930 con la RIT apagada'))

        _ejecutar_job(job)

        assert len(enviados) == 1
        assert enviados[0]['consec_requisicion'] is None
