"""Re-validación de la asignación por presencia (50920036, 2026-09-29).

Nació en val-asig-n1 (0a8ee53c) con `TestPorDecidirTieneSalida` en
`xfail(strict)`; cerrado, la marca se quitó. Dos ajustes a lo pedido después
de la re-validación: la ausencia se cierra con un **acto de trabajo** («Ya
volví» o un POST de trabajo), nunca con el sondeo de la PWA, y al cerrarla el
**regreso queda en hoy** (el registro se conserva, no se anula).
"""
from datetime import date, datetime, timedelta

import pytest

from tests.test_asignacion_presencia import _conteo, _picking, _sesion, _tok  # noqa: F401
from tests.test_asignacion_presencia import nb1  # noqa: F401  (fixture)


def _ausencia(db, u, *, desde, regreso=None, motivo='INCAPACIDAD', registrada=None):
    from app.models.ausencia import AusenciaUsuario
    a = AusenciaUsuario(usuario_id=u.id, motivo=motivo, desde=desde, regreso=regreso,
                        registrada_en=registrada or datetime.utcnow())
    db.session.add(a)
    db.session.commit()
    return a


class TestQuienVuelve:

    def test_incapacidad_sin_fecha_de_ayer_se_cierra_y_recibe_y_conserva(self, app, db, client, nb1):
        from app.services import asignacion, presencia
        from app.services.mobile_service import MobileService
        luis = nb1['luis']
        luis.ultima_senal_at = None
        db.session.commit()
        a = _ausencia(db, luis, desde=presencia._dia() - timedelta(days=3),
                      registrada=datetime.utcnow() - timedelta(days=3))
        _conteo(db, nb1['almacen'])
        # Abrir la app (el sondeo) no la cierra: toca «Ya volví».
        assert MobileService.get_tarea_actual(luis.id) is None
        r = client.post('/api/mobile/regrese', headers=_tok(app, luis))
        assert r.status_code == 200, r.get_json()
        t = MobileService.get_tarea_actual(luis.id)
        assert t and t['tipo'] == 'CONTEO', t
        db.session.expire_all()
        cerrada = db.session.get(type(a), a.id)
        assert (cerrada.anulada_en, cerrada.regreso) == (None, presencia._dia()), 'regreso = hoy'
        s = _sesion(db, t['id']); s.cantidad_fisica = 12; db.session.commit()
        asignacion.barrer()
        s = _sesion(db, t['id'])
        assert (s.operario_id, s.cantidad_fisica) == (luis.id, 12)

    def test_ausencia_de_hoy_no_se_cierra_no_recibe_nuevo_y_se_le_dice(self, app, db, client, nb1):
        from app.models.ausencia import AusenciaUsuario
        from app.services import asignacion, presencia
        luis = nb1['luis']
        luis.ultima_senal_at = None
        db.session.commit()
        a = _ausencia(db, luis, desde=presencia._dia(), motivo='PERMISO')
        mio = _conteo(db, nb1['almacen'], operario=luis, estado='EN_PROCESO')
        mio.ultima_actividad_at = datetime.utcnow(); db.session.commit()
        _conteo(db, nb1['almacen'])
        r = client.get('/api/mobile/tarea-actual', headers=_tok(app, luis)).get_json()
        assert r.get('id') == mio.id, 'lo que tiene en curso lo termina'
        asignacion.barrer()
        assert _sesion(db, mio.id).operario_id == luis.id
        # termina ese y pide otro: no le llega nada nuevo y se le dice por qué
        s = _sesion(db, mio.id); s.estado = 'MATCH'; db.session.commit()
        r = client.get('/api/mobile/tarea-actual', headers=_tok(app, luis)).get_json()
        assert r.get('ausente') and 'ausente' in r['mensaje'] and 'jefe' in r['mensaje'], r
        db.session.expire_all()
        assert db.session.get(AusenciaUsuario, a.id).anulada_en is None

    def test_vacaciones_con_fecha_no_se_cierran_solas(self, app, db, client, nb1):
        from app.models.ausencia import AusenciaUsuario
        from app.services import presencia
        luis = nb1['luis']
        a = _ausencia(db, luis, desde=presencia._dia() - timedelta(days=2),
                      regreso=presencia._dia() + timedelta(days=5), motivo='VACACIONES')
        _conteo(db, nb1['almacen'])
        r = client.get('/api/mobile/tarea-actual', headers=_tok(app, luis)).get_json()
        assert r.get('ausente'), r
        db.session.expire_all()
        assert db.session.get(AusenciaUsuario, a.id).anulada_en is None


class TestPorDecidirTieneSalida:

    def test_el_boton_devolver_a_la_cola_funciona_sobre_un_picking_en_proceso(self, app, db, client, nb1):
        from app.models.ausencia import AusenciaUsuario
        from app.services import presencia
        luis = nb1['luis']
        pk = _picking(db, nb1['almacen'], operario=luis, estado='EN_PROCESO', referencia='ST-PD')
        _ausencia(db, luis, desde=presencia._dia(), motivo='PERMISO')
        r = client.put(f'/api/picking/{pk.id}/reabrir', headers=_tok(app, nb1['sup']),
                       json={'motivo': 'Luis se fue a medio recoger'})
        assert r.status_code == 200, r.get_json()
        from app.models.bitacora import BitacoraAccion
        from app.models.picking import TareaPicking
        db.session.expire_all()
        t = db.session.get(TareaPicking, pk.id)
        assert (t.estado, t.operario_id, t.cantidad_recogida, t.ultimo_operario_id) == \
            ('PENDIENTE', None, 0, luis.id)
        assert 'carro' in r.get_json()['mensaje'], 'la advertencia de lo recogido'
        fila = BitacoraAccion.query.filter_by(entidad='TareaPicking', entidad_id=pk.id).one()
        assert fila.accion == 'DESASIGNAR' and 'Luis se fue' in fila.motivo

    def test_no_se_le_quita_a_quien_esta_recogiendo(self, app, db, client, nb1):
        pk = _picking(db, nb1['almacen'], operario=nb1['ana'], estado='EN_PROCESO', referencia='ST-PD2')
        r = client.put(f'/api/picking/{pk.id}/reabrir', headers=_tok(app, nb1['sup']),
                       json={'motivo': 'x'})
        assert r.status_code == 400 and 'en turno' in r.get_json()['error'], r.get_json()

    def test_un_operario_no_puede_devolverla(self, app, db, client, nb1):
        from app.services import presencia
        pk = _picking(db, nb1['almacen'], operario=nb1['luis'], estado='EN_PROCESO', referencia='ST-PD3')
        _ausencia(db, nb1['luis'], desde=presencia._dia(), motivo='PERMISO')
        r = client.put(f'/api/picking/{pk.id}/reabrir', headers=_tok(app, nb1['ana']),
                       json={'motivo': 'x'})
        assert r.status_code == 403


class TestSoloUnActoDeTrabajoCierraLaAusencia:

    def test_abrir_la_app_en_la_casa_no_cierra_la_incapacidad(self, app, db, client, nb1):
        from app.models.ausencia import AusenciaUsuario
        from app.services import presencia
        luis = nb1['luis']
        luis.ultima_senal_at = None
        db.session.commit()
        a = _ausencia(db, luis, desde=presencia._dia() - timedelta(days=2))
        h = _tok(app, luis)
        for url in ('/api/mobile/tarea-actual', '/api/conteo/mis-tareas', '/api/reposicion/tarea-actual'):
            client.get(url, headers=h)
        d = client.get('/api/mobile/tarea-actual', headers=h).get_json()
        assert d.get('ausente') and d.get('puede_regresar') and 'Ya volví' in d['mensaje'], d
        db.session.expire_all()
        assert db.session.get(AusenciaUsuario, a.id).regreso is None
        assert presencia.estado(luis)['codigo'] == 'AUSENTE'

    def test_un_post_de_trabajo_si_la_cierra(self, app, db, client, nb1):
        from app.models.ausencia import AusenciaUsuario
        from app.models.bitacora import BitacoraAccion
        from app.services import presencia
        luis = nb1['luis']
        a = _ausencia(db, luis, desde=presencia._dia() - timedelta(days=2))
        client.post('/api/picking/siguiente-tarea', headers=_tok(app, luis))
        db.session.expire_all()
        assert db.session.get(AusenciaUsuario, a.id).regreso == presencia._dia()
        fila = BitacoraAccion.query.filter_by(entidad='AusenciaUsuario').one()
        assert fila.accion == 'EDITAR' and 'Regresó antes' in fila.motivo

    def test_ya_volvi_no_quita_una_ausencia_con_fecha(self, app, db, client, nb1):
        from app.services import presencia
        luis = nb1['luis']
        _ausencia(db, luis, desde=presencia._dia() - timedelta(days=1),
                  regreso=presencia._dia() + timedelta(days=4), motivo='VACACIONES')
        r = client.post('/api/mobile/regrese', headers=_tok(app, luis))
        assert r.status_code == 409 and 'jefe' in r.get_json()['error']
