"""
Los pre-flag de ENTRADA_OC, TRASLADO_AVERIAS y AJUSTE_CONTEO: solo un «no»
los baja (tanda 2, 2026-09-25).

**La clase:** *un pre-flag que se baja ante una excepción que no prueba que el
documento no entró*. `_ejecutar_con_preflag` revertía ante todo lo que no
fuera un timeout de lectura: un 502 de un proxy o una conexión cortada a mitad
de la respuesta —el documento pudo haber entrado— bajaban la guarda y la cola
reenviaba. Una entrada por OC o un ajuste duplicados en el ERP. El traslado a
averías por movimiento ni siquiera tenía pre-flag.

Ahora: `ConnektaNoEnviado` (4xx, 429, `codigo≠0`, payload inválido, circuito
abierto, ambiente que no coincide) baja la bandera; lo demás es «no sé»:
FALLIDO sin reintento, la bandera queda, «Reintentar» se niega, y una persona
resuelve («¿Está en Siesa?», FORZAR en la bitácora).

El trinquete de clase (todo `except` de `siesa_job_service` que baja un
pre-flag es `except ConnektaNoEnviado`) vive en
`test_rc_verificado_por_documento.py::TestSoloUnaPruebaBajaLaBandera`, con su
inventario ya vacío.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.inventario import MovimientoInventario
from app.models.siesa_job import EstadoSiesaJob, SiesaJob
from app.services import siesa_job_service as sjs
from app.services.connekta_gateway import (ConnektaPayloadInvalido, ConnektaRechazado,
                                           ConnektaResultadoDesconocido)

from tests.test_averia_llega_a_siesa import (  # noqa: F401 — el fixture se registra al importarlo
    _connekta_ok, _correr, _job_entrada, _recibir_con_averia, setup)


# ═════════════════════════════════════════════════════════════════════════════
# 1 · El helper: qué excepción baja la bandera
# ═════════════════════════════════════════════════════════════════════════════

class TestElHelper:
    def _correr(self, exc):
        obj = SimpleNamespace(siesa_triggered=False, siesa_triggered_at=None)

        def _post():
            assert obj.siesa_triggered is True, 'el POST salió sin pre-flag'
            raise exc
        with pytest.raises(Exception) as info:
            sjs._ejecutar_con_preflag(obj, _post)
        return obj, info.value

    @pytest.mark.parametrize('exc', [
        ConnektaRechazado('HTTP 400'), ConnektaPayloadInvalido('sin tipo de docto')])
    def test_un_no_baja_la_bandera(self, db, exc):
        obj, e = self._correr(exc)
        assert obj.siesa_triggered is False
        assert e is exc

    @pytest.mark.parametrize('exc', [
        Exception('HTTP 502 Bad Gateway'), ConnectionError('conexión cortada'),
        ValueError('JSON ilegible')])
    def test_un_no_se_deja_la_bandera_y_lo_declara(self, db, exc):
        obj, e = self._correr(exc)
        assert obj.siesa_triggered is True
        assert isinstance(e, ConnektaResultadoDesconocido)
        assert str(e).startswith(sjs.MARCA_SIN_VERIFICAR)
        assert 'Recuperación' in str(e)

    def test_el_timeout_sigue_siendo_no_se(self, db):
        obj, e = self._correr(ConnektaResultadoDesconocido('ReadTimeout'))
        assert obj.siesa_triggered is True

    def test_el_ambiente_que_no_coincide_es_un_no(self):
        """El sello de ambiente frena ANTES de la red: el documento no existe."""
        from app.services.connekta_gateway import ConnektaNoEnviado
        from app.services.sello_ambiente import AmbienteNoCoincide
        assert issubclass(AmbienteNoCoincide, ConnektaNoEnviado)

    def test_el_gateway_declara_un_no_armable_como_no_enviado(self):
        """Lo que el gateway rechaza antes de armar el POST es un «no»: si fuera
        un `ValueError` pelado, el helper lo leería «no sé» y un documento que
        nunca salió quedaría esperando a una persona."""
        from app.services.connekta_gateway import connekta
        with pytest.raises(ConnektaPayloadInvalido):
            connekta.enviar_ajuste_inventario(motivo_codigo='AJ-XXX', item_codigo='X',
                                              cantidad=1, referencia='R')
        with pytest.raises(ConnektaPayloadInvalido):
            connekta.confirmar_entrada_compras(
                id_co_oc='003', tipo_docto_oc='OC', consec_docto_oc='1', items=[],
                proveedor_id=None, sucursal_prov='001')


# ═════════════════════════════════════════════════════════════════════════════
# 2 · ENTRADA_OC de punta a punta por la cola
# ═════════════════════════════════════════════════════════════════════════════

def _ck_que_falla(exc):
    ck = _connekta_ok()
    ck.confirmar_entrada_compras.side_effect = exc
    ck.transferir_a_averias.side_effect = exc
    return ck


def _por_la_cola(ck):
    with patch('app.services.connekta_gateway.connekta', ck), \
            patch('app.services.siesa_job_service._crear_alerta_admin'):
        sjs._run_dlq_jobs()


class TestEntradaOC:
    def test_un_502_queda_fallido_sin_reintento_y_con_la_bandera(self, db, setup):
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        _por_la_cola(_ck_que_falla(Exception('HTTP 502')))
        db.session.refresh(job)
        db.session.refresh(rec)
        assert job.estado == EstadoSiesaJob.FALLIDO and job.proximo_intento is None
        assert rec.siesa_triggered is True
        assert sjs.preflag_sin_verificar(job) is True

    def test_un_rechazo_baja_la_bandera_y_reintenta(self, db, setup):
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        _por_la_cola(_ck_que_falla(ConnektaRechazado('HTTP 400: tercero no es el de la OC')))
        db.session.refresh(job)
        db.session.refresh(rec)
        assert rec.siesa_triggered is False
        assert job.estado != EstadoSiesaJob.FALLIDO
        assert sjs.preflag_sin_verificar(job) is False

    def test_reintentar_se_niega_y_el_lote_lo_salta(self, db, setup, client, jwt_token_admin):
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        _por_la_cola(_ck_que_falla(Exception('HTTP 502')))
        with pytest.raises(ValueError, match='sin verificar'):
            sjs.reintentar_job(job.id)
        r = client.post('/api/siesa/resetear-jobs-fallidos?tipo=ENTRADA_OC',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        assert r.get_json()['sin_verificar'] == [job.id]
        assert db.session.get(SiesaJob, job.id).estado == EstadoSiesaJob.FALLIDO

    def test_el_panel_lo_marca(self, db, setup, client, jwt_token_admin):
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        _por_la_cola(_ck_que_falla(Exception('HTTP 502')))
        r = client.get('/api/siesa/jobs-fallidos',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        fila = next(j for j in r.get_json()['jobs'] if j['id'] == job.id)
        assert fila['sin_verificar'] is True

    def test_no_esta_en_siesa_baja_la_bandera_y_vuelve_a_enviar(self, db, setup, client,
                                                                jwt_token_admin):
        from app.models.bitacora import BitacoraAccion
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        _por_la_cola(_ck_que_falla(Exception('HTTP 502')))
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        assert client.post(f'/api/siesa/jobs/{job.id}/resolver-sin-verificar',
                           json={'motivo': 'x'}, headers=h).status_code == 400
        assert client.post(f'/api/siesa/jobs/{job.id}/resolver-sin-verificar',
                           json={'entro': False}, headers=h).status_code == 400
        r = client.post(f'/api/siesa/jobs/{job.id}/resolver-sin-verificar',
                        json={'entro': False, 'motivo': 'No aparece en Auditoría de documentos'},
                        headers=h)
        assert r.status_code == 200, r.get_json()
        db.session.refresh(rec)
        assert rec.siesa_triggered is False
        assert db.session.get(SiesaJob, job.id).estado == EstadoSiesaJob.PENDIENTE
        forz = BitacoraAccion.query.filter_by(accion='FORZAR').all()
        assert any((f.despues or {}).get('forzado') == 'envio_sin_verificar_resuelto_a_mano'
                   and (f.despues or {}).get('entro') is False for f in forz)
        ck = _connekta_ok()
        _por_la_cola(ck)
        ck.confirmar_entrada_compras.assert_called_once()

    def test_si_esta_en_siesa_cierra_sin_reenviar(self, db, setup):
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        _por_la_cola(_ck_que_falla(Exception('HTTP 502')))
        sjs.resolver_preflag_sin_verificar(job.id, usuario_id=setup['usuario'].id,
                                           entro=True, motivo='Está: EA-00000123')
        db.session.commit()
        ck = _connekta_ok()
        _por_la_cola(ck)
        ck.confirmar_entrada_compras.assert_not_called()
        assert db.session.get(SiesaJob, job.id).estado == EstadoSiesaJob.COMPLETADO

    def test_solo_admin_resuelve(self, db, setup, client, jwt_token):
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        _por_la_cola(_ck_que_falla(Exception('HTTP 502')))
        r = client.post(f'/api/siesa/jobs/{job.id}/resolver-sin-verificar',
                        json={'entro': True, 'motivo': 'x'},
                        headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403

    def test_sin_pendiente_no_hay_nada_que_resolver(self, db, setup):
        rec = _recibir_con_averia(db, setup)
        job = _job_entrada(rec)
        with pytest.raises(ValueError, match='no está pendiente'):
            sjs.resolver_preflag_sin_verificar(job.id, usuario_id=1, entro=True, motivo='x')


# ═════════════════════════════════════════════════════════════════════════════
# 3 · TRASLADO_AVERIAS por movimiento: ahora tiene pre-flag
# ═════════════════════════════════════════════════════════════════════════════

def _job_averia(db, setup):
    rec = _recibir_con_averia(db, setup)
    _correr(_job_entrada(rec), _connekta_ok())
    job = SiesaJob.query.filter_by(tipo='TRASLADO_AVERIAS').one()
    mov = db.session.get(MovimientoInventario, json.loads(job.payload)['movimiento_id'])
    return job, mov


class TestTrasladoAverias:
    def test_un_502_deja_el_movimiento_enviando_y_no_reenvia(self, db, setup):
        job, mov = _job_averia(db, setup)
        with pytest.raises(ConnektaResultadoDesconocido):
            _correr(job, _ck_que_falla(Exception('HTTP 503')))
        assert mov.siesa_sync == sjs.SIESA_SYNC_ENVIANDO
        ck = _connekta_ok()
        with pytest.raises(ConnektaResultadoDesconocido):
            _correr(job, ck)
        ck.transferir_a_averias.assert_not_called()

    def test_el_pre_flag_se_pone_antes_del_post(self, db, setup):
        job, mov = _job_averia(db, setup)
        visto = {}
        ck = _connekta_ok()

        def _post(**kw):
            visto['sync'] = db.session.get(MovimientoInventario, mov.id).siesa_sync
            return {'codigo': 0}
        ck.transferir_a_averias.side_effect = _post
        _correr(job, ck)
        assert visto['sync'] == sjs.SIESA_SYNC_ENVIANDO
        assert mov.siesa_sync == 'ENVIADO'

    def test_un_rechazo_lo_devuelve_a_pendiente(self, db, setup):
        job, mov = _job_averia(db, setup)
        with pytest.raises(ConnektaRechazado):
            _correr(job, _ck_que_falla(ConnektaRechazado('codigo=1')))
        assert mov.siesa_sync == 'PENDIENTE'

    def test_se_resuelve_a_mano_y_no_se_reencola_mientras_tanto(self, db, setup):
        job, mov = _job_averia(db, setup)
        _por_la_cola(_ck_que_falla(Exception('HTTP 503')))
        db.session.refresh(job)
        assert sjs.preflag_sin_verificar(job) is True
        # El encolador no abre un segundo traslado mientras el primero está sin verificar.
        assert sjs.encolar_traslado_averias(
            movimiento=mov, codigo_siesa='PROD-001', cantidad=1, referencia='r',
            bodega_del_almacen='NB1') is False
        sjs.resolver_preflag_sin_verificar(job.id, usuario_id=setup['usuario'].id,
                                           entro=True, motivo='Está: TRA-55')
        db.session.commit()
        assert mov.siesa_sync == 'ENVIADO'
        ck = _connekta_ok()
        _por_la_cola(ck)
        ck.transferir_a_averias.assert_not_called()
