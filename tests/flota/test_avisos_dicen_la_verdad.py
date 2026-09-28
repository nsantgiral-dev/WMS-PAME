"""Un papel vencido le llega a alguien, y el panel dice la verdad (2026-09-27).

La clase: **un aviso que no le llega a nadie y un panel que responde con las
variables del proceso equivocado** (hallazgos P0-2, P1-8, P1-9 y P1-10 de la
noche de flota). En producción:

- el único canal era WhatsApp (sin aprobar) y solo al rol `mantenimiento`;
- el correo semanal filtraba `fecha_vencimiento >= hoy`: los vencidos, que son
  los que dejan el camión ilegal, no salían nunca;
- un daño bloqueante no avisaba a nadie;
- `GET /flota/avisos` contestaba `encendido` con las variables de la WEB, y el
  cron corre en el WORKER;
- un aviso que fallaba una vez no se reintentaba nunca;
- una foto con `storage_ref` relativo quedaba 410 si `FLOTA_FOTOS_DIR` cambió.

El inventario del correo es el VOCABULARIO de la política: cada estado de papel
distinto de «vigente» tiene que aparecer (un estado nuevo entra solo al test).
"""
import ast
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from flota.dominio import salida as dom

RAIZ = Path(__file__).resolve().parents[2]


@pytest.fixture
def correos(monkeypatch):
    """Lo que habría salido por Resend."""
    enviados = []

    def _enviar(asunto, html, texto, dest=None, devolver_id=False):
        enviados.append({'asunto': asunto, 'html': html, 'texto': texto, 'dest': dest})
        return f'resend-{len(enviados)}' if devolver_id else True

    monkeypatch.setattr('app.services.alertas_service.enviar_email', _enviar)
    return enviados


@pytest.fixture
def encendido(monkeypatch):
    monkeypatch.setenv('FLOTA_AVISOS', 'true')
    monkeypatch.setenv('RESEND_API_KEY', 'x')
    monkeypatch.setenv('ALERTA_EMAIL_DEST', 'wms@papeleria.test')


@pytest.fixture
def mundo(db):
    """Un vehículo por estado de papel, uno inactivo con todo vencido, un daño
    bloqueante, y conductores con licencia vencida, sin cargar y al día."""
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from app.utils.fecha import dia_operativo
    from flota.adaptadores.modelos import DocumentoVehiculo

    hoy = dia_operativo()
    u = Usuario(email='avisos@verdad.test', nombre='Quien Reporta', password_hash='x',
                rol='control_flota', activo=True)
    db.session.add(u)
    veh = {}
    for placa, activo in (('VEN001', True), ('PVE001', True), ('SIN001', True),
                          ('COR001', True), ('NOE001', True), ('INA001', False)):
        veh[placa] = Vehiculo(placa=placa, tipo='Camión', activo=activo)
        db.session.add(veh[placa])
    db.session.flush()

    def papel(placa, tipo, exp, venc, estado='vigente'):
        db.session.add(DocumentoVehiculo(
            vehiculo_id=veh[placa].id, tipo=tipo, estado=estado,
            numero='' if estado != 'vigente' else f'{tipo}-{placa}',
            entidad='' if estado != 'vigente' else 'Aseguradora',
            fecha_expedicion=exp, fecha_vencimiento=venc))

    def al_dia(placa, salvo=()):
        for t in ('soat', 'rtm', 'poliza_rc'):
            if t not in salvo:
                papel(placa, t, hoy - timedelta(days=100), hoy + timedelta(days=265))
        if 'tarjeta_propiedad' not in salvo:
            papel(placa, 'tarjeta_propiedad', hoy - timedelta(days=900), None)

    al_dia('VEN001', salvo=('soat',))
    papel('VEN001', 'soat', hoy - timedelta(days=370), hoy - timedelta(days=5))
    al_dia('PVE001', salvo=('rtm',))
    papel('PVE001', 'rtm', hoy - timedelta(days=350), hoy + timedelta(days=10))
    al_dia('SIN001', salvo=('soat',))
    al_dia('COR001', salvo=('rtm',))
    papel('COR001', 'rtm', date(2025, 11, 11), date(2025, 11, 11))
    al_dia('NOE001', salvo=('poliza_rc',))
    papel('NOE001', 'poliza_rc', None, None, estado='no_encontrado')
    papel('INA001', 'soat', hoy - timedelta(days=400), hoy - timedelta(days=30))

    for nombre, vence in (('Beto Vencido', hoy - timedelta(days=2)),
                          ('Carla Sin Licencia', None),
                          ('Dora Al Dia', hoy + timedelta(days=500))):
        db.session.add(Conductor(nombre=nombre, cedula=nombre[:4], activo=True,
                                 licencia_numero='L-1' if vence else None,
                                 licencia_categoria='C2' if vence else None,
                                 licencia_vence=vence))
    db.session.commit()
    return {'veh': {k: v.id for k, v in veh.items()}, 'usr': u.id, 'hoy': hoy}


# ═════════════════════════════════════════════════════════════════════════
# 1 · El correo diario
# ═════════════════════════════════════════════════════════════════════════

class TestElCorreoDiario:

    def test_dice_los_vencidos_y_los_por_vencer_de_los_activos(self, mundo, encendido, correos):
        from flota.adaptadores.avisos import correo_diario
        r = correo_diario()
        assert r['enviado'] is True, r
        [c] = correos
        assert 'VEN001 — SOAT vencido desde' in c['texto'] and 'PROHIBIDO SALIR' in c['texto']
        assert 'PVE001 — Revisión técnico-mecánica vence el' in c['texto']
        assert 'INA001' not in c['texto'], 'un vehículo inactivo no se persigue'
        assert c['dest'] is None, 'sin FLOTA_AVISO_CORREOS hereda ALERTA_EMAIL_DEST'

    @pytest.mark.parametrize('estado', [e for e in dom.ESTADOS_PAPEL if e != dom.VIGENTE])
    def test_todo_estado_de_papel_que_no_esta_al_dia_sale(self, estado, mundo, encendido,
                                                          correos):
        """Inventario = el vocabulario de la política. Un estado nuevo de papel
        entra solo a este test: o sale en el correo, o el test se pone rojo."""
        from flota.adaptadores.avisos import lo_que_hay_que_avisar
        c = lo_que_hay_que_avisar()
        sufijo = {dom.VENCIDO: '_vencido', dom.POR_VENCER: '_por_vencer',
                  dom.NO_ENCONTRADO: '_no_encontrado', dom.SIN_CARGAR: '_sin_registro',
                  dom.DATO_A_CORREGIR: '_dato_a_corregir'}[estado]
        assert any(p['clave'].endswith(sufijo) for p in c['papeles']), (estado, c['papeles'])

    def test_la_rtm_imposible_sale_como_dato_a_corregir(self, mundo, encendido, correos):
        from flota.adaptadores.avisos import correo_diario
        correo_diario()
        assert 'COR001 — Revisión técnico-mecánica: dato a corregir.' in correos[0]['texto']

    def test_licencias_y_danos_bloqueantes(self, db, mundo, encendido, correos):
        from flota.adaptadores import hallazgos
        from flota.adaptadores.avisos import correo_diario
        hallazgos.reportar(vehiculo_id=mundo['veh']['PVE001'], criticidad='bloqueante',
                           descripcion='Freno de mano no <b>sostiene</b>', km=1000,
                           reportado_por_usuario_id=mundo['usr'])
        correo_diario()
        texto, html = correos[0]['texto'], correos[0]['html']
        assert 'Beto Vencido — Licencia de conducción del conductor vencida' in texto
        assert 'Carla Sin Licencia — Licencia de conducción sin cargar' in texto
        assert 'Dora Al Dia' not in texto
        assert 'PVE001 — Freno de mano no <b>sostiene</b>' in texto
        assert '&lt;b&gt;sostiene' in html and '<b>sostiene' not in html

    def test_uno_por_dia_y_el_fallido_se_reintenta(self, db, mundo, encendido, monkeypatch):
        from flota.adaptadores.avisos import correo_diario
        from flota.adaptadores.modelos import Aviso
        intentos = []

        def _falla_y_despues_sale(asunto, html, texto, dest=None, devolver_id=False):
            intentos.append(1)
            if len(intentos) == 1:
                raise RuntimeError('Resend 503')
            return 'resend-ok'

        monkeypatch.setattr('app.services.alertas_service.enviar_email', _falla_y_despues_sale)
        assert correo_diario()['enviado'] is False
        assert Aviso.query.one().estado == 'fallido'
        assert correo_diario()['enviado'] is True
        assert correo_diario()['motivo'] == 'ya salió hoy'
        fila = Aviso.query.one()
        assert (fila.estado, fila.proveedor_msg_id, fila.telefono) == (
            'entregado_al_proveedor', 'resend-ok', 'correo')
        assert len(intentos) == 2

    def test_apagado_no_manda_y_lo_dice(self, mundo, correos, monkeypatch):
        from flota.adaptadores.avisos import correo_diario
        monkeypatch.delenv('FLOTA_AVISOS', raising=False)
        r = correo_diario()
        assert (r['enviado'], correos) == (False, []) and 'FLOTA_AVISOS' in r['motivo']

    def test_sin_resend_queda_fallido_con_el_motivo(self, mundo, monkeypatch):
        from flota.adaptadores.avisos import correo_diario
        monkeypatch.setenv('FLOTA_AVISOS', 'true')
        monkeypatch.delenv('RESEND_API_KEY', raising=False)
        monkeypatch.delenv('ALERTA_EMAIL_DEST', raising=False)
        r = correo_diario()
        assert r['enviado'] is False and 'Resend no está configurado' in r['motivo']

    def test_el_correo_no_cuenta_como_whatsapp_sin_confirmar(self, db, mundo, encendido,
                                                            correos):
        from flota.adaptadores.avisos import avisos_sin_confirmar, correo_diario
        from flota.adaptadores.modelos import Aviso
        correo_diario()
        Aviso.query.one().creado_ts = datetime.utcnow() - timedelta(hours=10)
        db.session.commit()
        assert avisos_sin_confirmar(6) == 0


class TestElReporteSemanal:

    def test_ya_no_esconde_los_vencidos_ni_cuenta_inactivos(self, mundo):
        from flota.adaptadores.reporte_semanal import armar_reporte
        r = armar_reporte()
        assert [d['placa'] for d in r['documentos_vencidos']] == ['VEN001']
        assert [d['placa'] for d in r['documentos_por_vencer_30d']] == ['PVE001']
        sin = {(d['placa'], d['estado']) for d in r['documentos_sin_cargar']}
        assert ('SIN001', dom.SIN_CARGAR) in sin and ('COR001', dom.DATO_A_CORREGIR) in sin
        assert ('NOE001', dom.NO_ENCONTRADO) in sin
        assert all(d['placa'] != 'INA001' for k in ('documentos_vencidos',
                   'documentos_por_vencer_30d', 'documentos_sin_cargar') for d in r[k])


# ═════════════════════════════════════════════════════════════════════════
# 2 · El daño bloqueante avisa al nacer
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sincronico(monkeypatch):
    monkeypatch.setattr('flota.adaptadores.avisos._lanzar', lambda fn: fn())


class TestElDanoBloqueanteAvisaAlNacer:

    def test_al_registrarse_sale_el_correo(self, db, mundo, encendido, correos, sincronico):
        from flota.adaptadores import hallazgos
        from flota.adaptadores.modelos import Aviso
        h = hallazgos.reportar(vehiculo_id=mundo['veh']['VEN001'], criticidad='bloqueante',
                               descripcion='Dirección con juego', km=500,
                               reportado_por_usuario_id=mundo['usr'])
        [c] = correos
        assert 'VEN001' in c['asunto'] and 'Dirección con juego' in c['texto']
        fila = Aviso.query.filter_by(plantilla='correo_flota_dano_bloqueante').one()
        assert fila.clave == f'flota_hallazgo_bloqueante:hallazgo:{h.id}:nacio:correo'

    def test_un_dano_menor_no_avisa(self, db, mundo, encendido, correos, sincronico):
        from flota.adaptadores import hallazgos
        hallazgos.reportar(vehiculo_id=mundo['veh']['VEN001'], criticidad='menor',
                           descripcion='rayón', km=500, reportado_por_usuario_id=mundo['usr'])
        assert correos == []

    def test_si_la_transaccion_se_deshace_no_avisa(self, db, mundo, encendido, correos,
                                                   sincronico):
        from flota.adaptadores import hallazgos
        hallazgos.reportar(vehiculo_id=mundo['veh']['VEN001'], criticidad='bloqueante',
                           descripcion='nunca existió', km=500,
                           reportado_por_usuario_id=mundo['usr'], commit=False)
        db.session.rollback()
        db.session.commit()
        assert correos == []

    def test_volverse_bloqueante_tambien_avisa_y_una_sola_vez(self, db, mundo, encendido,
                                                             correos, sincronico):
        from flota.adaptadores import hallazgos
        from flota.adaptadores.avisos import avisar_dano_bloqueante
        h = hallazgos.reportar(vehiculo_id=mundo['veh']['VEN001'], criticidad='mayor',
                               descripcion='fuga de aceite', km=500,
                               reportado_por_usuario_id=mundo['usr'])
        h.criticidad = 'bloqueante'
        db.session.commit()
        assert len(correos) == 1
        assert avisar_dano_bloqueante(h.id)['correo'] == 'ya_avisado'
        assert len(correos) == 1

    def test_apagado_no_avisa_y_el_correo_diario_lo_repite(self, db, mundo, correos,
                                                          sincronico, monkeypatch):
        from flota.adaptadores import hallazgos
        from flota.adaptadores.avisos import lo_que_hay_que_avisar
        monkeypatch.delenv('FLOTA_AVISOS', raising=False)
        hallazgos.reportar(vehiculo_id=mundo['veh']['VEN001'], criticidad='bloqueante',
                           descripcion='sin frenos', km=500, reportado_por_usuario_id=mundo['usr'])
        assert correos == []
        assert [d['placa'] for d in lo_que_hay_que_avisar()['danos']] == ['VEN001']


# ═════════════════════════════════════════════════════════════════════════
# 3 · El WhatsApp que falló se reintenta
# ═════════════════════════════════════════════════════════════════════════

class TestElWhatsappFallidoSeReintenta:

    def test_el_segundo_barrido_lo_manda(self, db, mundo, encendido, monkeypatch):
        from flota.adaptadores.avisos import barrer_documentos_por_vencer
        from flota.adaptadores.gupshup import AvisoNoEnviado
        from flota.adaptadores.modelos import Aviso
        monkeypatch.setenv('FLOTA_AVISO_TELEFONOS', json.dumps({'mantenimiento': ['573001']}))

        class Canal:
            simulado, veces = True, 0

            def enviar(self, telefono, plantilla, parametros):
                Canal.veces += 1
                if Canal.veces == 1:
                    raise AvisoNoEnviado('caído')
                return f'msg-{Canal.veces}'

        primero = barrer_documentos_por_vencer(canal_usado=Canal())
        segundo = barrer_documentos_por_vencer(canal_usado=Canal())
        assert primero['fallidos'] >= 1
        assert segundo['enviados'] >= 1
        assert not Aviso.query.filter_by(estado='fallido').count()


# ═════════════════════════════════════════════════════════════════════════
# 4 · El panel lee lo que corrió el worker
# ═════════════════════════════════════════════════════════════════════════

def _latido(db, servicio, resumen, ok=True, hace_horas=1):
    from app.models.cron_latido import CronLatido
    t = datetime.utcnow() - timedelta(hours=hace_horas)
    db.session.add(CronLatido(nombre='flota_avisos_barrido', servicio=servicio,
                              ultimo_inicio=t, ultimo_fin=t, ultimo_ok=ok,
                              ultimo_ok_en=t if ok else None, corridas=1, fallos_seguidos=0,
                              ultimo_resumen=None if resumen is None else json.dumps(resumen)))
    db.session.commit()


def _panel(app, client, db, almacen):
    from app.models.usuario import Usuario
    from flask_jwt_extended import create_access_token
    u = Usuario(email='panel@avisos.test', nombre='Panel', password_hash='x', rol='admin',
                almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.commit()
    with app.app_context():
        h = {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}
    r = client.get('/flota/avisos', headers=h)
    assert r.status_code == 200
    return r.get_json()


def _textos(d):
    return ' | '.join(f"{f['servicio']}: {f['texto']}" for f in d['que_falta'])


class TestElPanelDiceQueFaltaYDonde:

    def test_sin_latido_dice_que_nunca_corrio(self, app, client, db, almacen):
        d = _panel(app, client, db, almacen)
        assert 'no ha corrido en ningún servicio' in _textos(d)

    def test_la_web_encendida_no_tapa_al_worker_apagado(self, app, client, db, almacen,
                                                       monkeypatch):
        monkeypatch.setenv('FLOTA_AVISOS', 'true')      # la WEB tiene la variable
        _latido(db, 'WMS-Worker', {'config': {'servicio': 'WMS-Worker', 'FLOTA_AVISOS': False,
                                              'FLOTA_AVISOS_REALES': False,
                                              'telefonos_por_rol': {}, 'telefonos_legibles': True,
                                              'correo_destinatarios': None, 'resend': False}})
        d = _panel(app, client, db, almacen)
        t = _textos(d)
        assert 'WMS-Worker: En el servicio «WMS-Worker» (el que corre el barrido diario) falta FLOTA_AVISOS=true' in t
        assert 'RESEND_API_KEY' in t
        assert d['barrido']['servicio'] == 'WMS-Worker'

    def test_un_latido_sin_resumen_se_declara(self, app, client, db, almacen):
        _latido(db, 'WMS-Worker', None)
        assert 'no se sabe qué variables tiene' in _textos(_panel(app, client, db, almacen))

    def test_todo_configurado_no_pide_nada_grave(self, app, client, db, almacen, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISOS', 'true')
        monkeypatch.setenv('RESEND_API_KEY', 'x')
        monkeypatch.setenv('ALERTA_EMAIL_DEST', 'a@b.c')
        _latido(db, 'WMS-Worker', {'config': {'servicio': 'WMS-Worker', 'FLOTA_AVISOS': True,
                                              'FLOTA_AVISOS_REALES': True,
                                              'telefonos_por_rol': {'mantenimiento': 2},
                                              'telefonos_legibles': True,
                                              'correo_destinatarios': 'FLOTA_AVISO_CORREOS',
                                              'resend': True}})
        d = _panel(app, client, db, almacen)
        assert not [f for f in d['que_falta'] if f['grave']], d['que_falta']

    def test_un_barrido_viejo_se_dice(self, app, client, db, almacen):
        _latido(db, 'WMS-Worker', {'config': {'FLOTA_AVISOS': True, 'resend': True,
                                              'correo_destinatarios': 'x'}}, hace_horas=50)
        assert 'no corre desde hace más de un día' in _textos(_panel(app, client, db, almacen))


class TestElLatidoGuardaElResumen:

    def test_con_latido_guarda_lo_que_la_corrida_devolvio(self, app, db):
        from app.models.cron_latido import CronLatido
        from app.services.cron_latido import con_latido
        fn = con_latido('prueba_resumen', lambda app=None: {'motivo': 'apagado',
                                                            'config': {'FLOTA_AVISOS': False}})
        fn(app=app)
        fila = CronLatido.query.filter_by(nombre='prueba_resumen').one()
        assert fila.resumen() == {'motivo': 'apagado', 'config': {'FLOTA_AVISOS': False}}
        # Una corrida que no dice qué hizo (el proceso que perdió el candado)
        # no borra el resumen del que sí trabajó (validación 2026-09-27, P3).
        con_latido('prueba_resumen', lambda app=None: None)(app=app)
        db.session.expire_all()
        assert CronLatido.query.filter_by(nombre='prueba_resumen').one().resumen() == {
            'motivo': 'apagado', 'config': {'FLOTA_AVISOS': False}}

    def test_el_barrido_diario_publica_su_configuracion(self, mundo, monkeypatch, correos):
        from flota.adaptadores.avisos import barrido_diario
        monkeypatch.delenv('FLOTA_AVISOS', raising=False)
        r = barrido_diario()
        assert r['config']['FLOTA_AVISOS'] is False and 'servicio' in r['config']
        assert r['correo']['enviado'] is False


class TestElBotonTomaElCandado:

    def test_con_el_candado_tomado_contesta_409(self, app, client, db, almacen, monkeypatch):
        from contextlib import contextmanager
        from app.models.usuario import Usuario
        from flask_jwt_extended import create_access_token

        @contextmanager
        def _ocupado(clave, nombre=None):
            yield False
        monkeypatch.setattr('app.utils.lock.advisory_lock', _ocupado)
        u = Usuario(email='boton@avisos.test', nombre='B', password_hash='x', rol='admin',
                    almacen_id=almacen.id, activo=True)
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            h = {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}
        r = client.post('/flota/avisos/barrer', headers=h)
        assert r.status_code == 409


class TestElPanelNoLeeElEntornoDeLaWeb:
    """Trinquete (AST): ningún módulo de `flota/api/` lee una variable
    `FLOTA_AVISOS*` con `os.getenv`/`os.environ`. La verdad del cron está en su
    latido; la configuración de este proceso sale de
    `avisos.configuracion_de_este_servicio`, que la etiqueta con su servicio."""

    @staticmethod
    def _lecturas(fuentes):
        malas = []
        for nombre, texto in fuentes:
            for n in ast.walk(ast.parse(texto)):
                if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) \
                        and isinstance(n.args[0].value, str) \
                        and n.args[0].value.startswith('FLOTA_AVISO'):
                    f = n.func
                    if isinstance(f, ast.Attribute) and f.attr in ('getenv', 'get'):
                        malas.append((nombre, n.lineno))
                if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                        and str(n.slice.value).startswith('FLOTA_AVISO'):
                    malas.append((nombre, n.lineno))
        return malas

    def test_el_repo(self):
        fuentes = [(str(p.relative_to(RAIZ)), p.read_text(encoding='utf-8'))
                   for p in sorted((RAIZ / 'flota' / 'api').glob('*.py'))]
        assert len(fuentes) >= 10, 'piso: no se leyó flota/api'
        assert self._lecturas(fuentes) == []

    def test_ve_las_dos_formas_y_no_el_texto(self):
        rota = ('import os\n'
                'a = os.getenv("FLOTA_AVISOS")\n'
                'b = os.environ["FLOTA_AVISOS_REALES"]\n'
                '"""os.getenv("FLOTA_AVISOS")"""\n')
        assert [l for _n, l in self._lecturas([('x.py', rota)])] == [2, 3]


# ═════════════════════════════════════════════════════════════════════════
# 5 · Fotos: la raíz anterior y la alarma
# ═════════════════════════════════════════════════════════════════════════

class TestLasFotosDeUnaRaizAnterior:

    @pytest.fixture
    def raices(self, tmp_path, monkeypatch):
        nueva, vieja = tmp_path / 'nueva', tmp_path / 'vieja'
        nueva.mkdir()
        vieja.mkdir()
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(nueva))
        monkeypatch.setenv('FLOTA_FOTOS_DIRS_ANTERIORES', str(vieja))
        return nueva, vieja

    def _foto(self, db, usuario_admin, contenido, ref):
        from flota.adaptadores.modelos import Foto
        f = Foto(entidad_tipo='documento', entidad_id=0, ts_captura=datetime.utcnow(),
                 autor_usuario_id=usuario_admin.id, clase='foto_dato', mime='image/jpeg',
                 bytes=len(contenido), ancho=1600, alto=1200, storage_ref=ref,
                 hash_sha256=hashlib.sha256(contenido).hexdigest(), estado='ok')
        db.session.add(f)
        db.session.commit()
        return f

    def _h(self, app, u):
        from flask_jwt_extended import create_access_token
        with app.app_context():
            return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}

    def test_se_sirve_desde_la_raiz_anterior_si_el_hash_coincide(self, app, client, db,
                                                                raices, usuario_admin):
        _nueva, vieja = raices
        contenido = b'foto del soat'
        digest = hashlib.sha256(contenido).hexdigest()
        ref = f'2026/08/{digest}.jpg'
        (vieja / '2026' / '08').mkdir(parents=True)
        (vieja / ref).write_bytes(contenido)
        f = self._foto(db, usuario_admin, contenido, ref)
        r = client.get(f'/flota/foto/{f.id}', headers=self._h(app, usuario_admin))
        assert r.status_code == 200 and r.data == contenido

    def test_otro_contenido_con_el_mismo_nombre_no_se_sirve(self, app, client, db, raices,
                                                           usuario_admin):
        _nueva, vieja = raices
        contenido = b'la foto verdadera'
        ref = f'2026/08/{hashlib.sha256(contenido).hexdigest()}.jpg'
        (vieja / '2026' / '08').mkdir(parents=True)
        (vieja / ref).write_bytes(b'otra cosa del mismo largo!')
        f = self._foto(db, usuario_admin, contenido, ref)
        r = client.get(f'/flota/foto/{f.id}', headers=self._h(app, usuario_admin))
        assert r.status_code == 410

    def test_el_health_da_la_alarma(self, db, raices, usuario_admin):
        from flota.adaptadores.almacen_fotos import diagnostico_almacen
        _nueva, vieja = raices
        perdida = b'se perdio'
        self._foto(db, usuario_admin, perdida,
                   f'2026/08/{hashlib.sha256(perdida).hexdigest()}.jpg')
        viva = b'en la vieja'
        ref = f'2026/08/{hashlib.sha256(viva).hexdigest()}.jpg'
        (vieja / '2026' / '08').mkdir(parents=True)
        (vieja / ref).write_bytes(viva)
        self._foto(db, usuario_admin, viva, ref)
        d = diagnostico_almacen()
        assert d['fotos_ok_sin_archivo'] == 1 and d['fotos_ok_en_raiz_anterior'] == 1
        assert '1 foto(s) figuran guardadas' in d['alarma']
        assert 'raíz anterior' in d['alarma']
        assert str(vieja) in d['raices_anteriores']

    def test_sin_problemas_no_hay_alarma(self, db, raices):
        from flota.adaptadores.almacen_fotos import diagnostico_almacen
        d = diagnostico_almacen()
        assert d['alarma'] is None or 'disco del contenedor' in d['alarma']
