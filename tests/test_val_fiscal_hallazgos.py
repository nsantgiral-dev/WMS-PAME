"""
Validación crítica del frente fiscal (2026-09-26, SHA d9da743d).

Cada test reproduce un hallazgo P0/P1 del reporte y está marcado
`xfail(strict=True)`: pasa (en rojo esperado) mientras el defecto exista y se
pone en rojo de verdad el día que alguien lo arregle, para que se quite la
marca a conciencia.
"""
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest

from tests.test_documento_fiscal import (  # noqa: F401 — fixtures reutilizados
    _caja_para_cerrar, _tarea, siesa, siesa_real,
)


def _cerrar(t):
    from app.services.closing.pedido_closer import PedidoPackingCloser
    return PedidoPackingCloser().ejecutar_cierre(t.id, [{'tipo': 'Caja', 'cantidad': 1}], 1)


# ─────────────────────────────────────────────────────────────────────────────
# H1 · El circuito abierto de UN proceso web niega el cierre para siempre
# ─────────────────────────────────────────────────────────────────────────────

class TestElCierreDejaProbarAlCircuito:

    # Cerrado 2026-09-26 (v2-fiscal): circuito_admite_intento; el precheck es el probe.
    def test_pasado_el_intervalo_de_probe_el_cierre_pregunta(
            self, db, almacen, producto, siesa_real, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        hechas = []
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido',
                            lambda self, t, c: hechas.append('estado') or 3)
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido',
                            lambda self, t, c: hechas.append('fe') or [])
        monkeypatch.setattr(siesa_real, '_cb_state', 'OPEN')
        # Siesa cayó hace una hora; el intervalo de probe (60 s) venció hace rato.
        monkeypatch.setattr(siesa_real, '_cb_last_probe', time.monotonic() - 3600)
        t = _caja_para_cerrar(db, almacen, producto)
        r = _cerrar(t)
        assert r.exitoso, r.mensaje
        assert hechas, 'el cierre nunca le preguntó a Siesa: el circuito no puede cerrarse'


# ─────────────────────────────────────────────────────────────────────────────
# H2 · «Limpiar bultos» sobre una caja en cola: la FE sale y la caja queda sin bultos
# ─────────────────────────────────────────────────────────────────────────────

class TestResetearConLaEmisionEnCola:

    def _cerrar_en_cola(self, db, almacen, producto, monkeypatch):
        from app.models.siesa_job import SiesaJob
        from app.services.connekta_gateway import ConnektaGateway
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', lambda self, t, c: 3)
        t = _caja_para_cerrar(db, almacen, producto)
        assert _cerrar(t).exitoso
        job = SiesaJob.query.filter_by(tipo='DESPACHO_F470', referencia_id=t.id).one()
        assert job.estado == 'PENDIENTE'
        return t, job

    # Cerrado 2026-09-26 (v2-fiscal): exigir_sin_envio_vivo en resetear, cancelar y re-confirmar.
    def test_resetear_se_niega_con_el_job_vivo(self, db, almacen, producto, siesa,
                                               monkeypatch):
        from app.services.packing_service import PackingService
        t, _job = self._cerrar_en_cola(db, almacen, producto, monkeypatch)
        with pytest.raises(ValueError):
            PackingService.resetear_siesa(t.id, usuario_id=None)

    # Cerrado 2026-09-26 (v2-fiscal): idem.
    def test_la_emision_despues_del_reset_no_deja_una_caja_sin_bultos(
            self, db, almacen, producto, siesa, monkeypatch):
        from app.models.bulto import Bulto
        from app.services.packing_service import PackingService
        from app.services.siesa_job_service import _ejecutar_job
        t, job = self._cerrar_en_cola(db, almacen, producto, monkeypatch)
        try:
            PackingService.resetear_siesa(t.id, usuario_id=None)
        except ValueError:
            pass  # arreglado: el reset se niega → el test pasa por el assert de abajo
        siesa.remision = {'tipo': 'RM', 'consec': 77}
        _ejecutar_job(job)
        db.session.refresh(t)
        assert not (t.estado == 'DESPACHADO'
                    and Bulto.query.filter_by(tarea_id=t.id).count() == 0), \
            'FE emitida y la caja quedó sin bultos: no puede ir al muelle'


# ─────────────────────────────────────────────────────────────────────────────
# H3 · Los carriles manuales de la FE no comparten el candado de la DLQ
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def _lock_ocupado(clave, nombre):
    yield False


class TestLosCarrilesManualesTomanElCandado:

    # Cerrado 2026-09-26 (v2-fiscal): emision_exclusiva (candado por pedido + disponibilidad) en todo carril.
    def test_facturar_remision_con_la_dlq_ocupada_no_postea(
            self, app, client, db, almacen, siesa, usuario_admin, jwt_token_admin,
            monkeypatch):
        import app.utils.lock as lock_mod
        monkeypatch.setattr(lock_mod, 'advisory_lock', _lock_ocupado)
        t = _tarea(db, almacen, rm_tipo='RM', rm_consec=5,
                   rm_enviada_at=datetime.utcnow() - timedelta(minutes=20))
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.post(f'/api/despacho_parcial/{t.id}/facturar-remision', headers=h, json={})
        assert siesa.posts_142943 == 0 and r.status_code == 409, (r.status_code, r.get_json())


# ─────────────────────────────────────────────────────────────────────────────
# H4 · La búsqueda de la RM se rinde con más de 3.000 remisiones en 30 días
# ─────────────────────────────────────────────────────────────────────────────

class TestLaRemisionEnTemporada:

    # Cerrado 2026-09-26 (v2-fiscal): corte al encontrar; tope = RemisionBarridoIncompleto.
    def test_la_encuentra_en_la_primera_pagina_con_mas_de_tres_mil(self, app, monkeypatch):
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'modo_simulacion', False)

        from app.services.connekta_gateway import ConnektaGateway

        def _get(self, nombre, params_extra=None, url=None, **k):
            pag = int(params_extra['paginacion'].split('|')[0].split('=')[1])
            filas = [{'consec_pd': 100000 + pag * 1000 + i, 'tipo_rm': 'RM',
                      'consec_rm': 500000 + pag * 1000 + i} for i in range(100)]
            if pag == 1:
                filas[0] = {'consec_pd': 4321, 'tipo_rm': 'RM', 'consec_rm': 777}
            return {'detalle': {'Datos': filas}}

        monkeypatch.setattr(ConnektaGateway, '_get', _get)
        assert connekta.get_remision_desde_pedido('PD', '4321') == {'tipo': 'RM', 'consec': 777}


# ─────────────────────────────────────────────────────────────────────────────
# H5 · El barrido de cartera trae su propio horario (7:00–19:30)
# ─────────────────────────────────────────────────────────────────────────────

class TestElBarridoDeCarteraNoTieneHorarioPropio:

    # Cerrado 2026-09-27 (v2-inv, d861b040): los crons no traen su propia ventana.
    def test_sin_ventana_el_barrido_corre_de_noche(self, app, monkeypatch):
        import apscheduler.schedulers.background as bg
        capturados = []

        class _Sched:
            def __init__(self, *a, **k):
                pass

            def add_job(self, **k):
                capturados.append(k)

            def start(self):
                pass

        monkeypatch.setattr(bg, 'BackgroundScheduler', _Sched)
        monkeypatch.delenv('SIESA_VENTANA', raising=False)
        from app.services import cartera_service
        cartera_service.init_scheduler(app)
        trig = next(k['trigger'] for k in capturados if k.get('id') == 'cartera_barrido')
        horas = {str(f.name): str(f) for f in trig.fields}
        assert horas['hour'] == '*', f'el barrido de cartera tiene horario propio: {horas["hour"]}'


# ─────────────────────────────────────────────────────────────────────────────
# H6 · Resuelta a mano, la emisión sigue contando como «trabada»
# ─────────────────────────────────────────────────────────────────────────────

class TestLaSalidaHumanaDesatascaElContador:

    # Cerrado 2026-09-26 (v2-fiscal): cerrar_despachos_resueltos en _persistir_resultado.
    def test_facturar_rm_manual_saca_el_job_de_los_trabados(self, db, almacen, siesa):
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        from app.services.despacho_parcial_service import DespachoParialService
        from app.services.siesa_job_service import fallidos_vigentes
        t = _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=1))
        job = SiesaJob.encolar(tipo='DESPACHO_F470', referencia_tipo='TareaPacking',
                               referencia_id=t.id, payload={'tarea_id': t.id})
        job.estado = EstadoSiesaJob.FALLIDO
        job.error_ultimo = 'RemisionNoIdentificada'
        db.session.commit()
        DespachoParialService.facturar_rm_con_consec(t, 'RM', 88)
        db.session.refresh(t)
        assert t.estado == 'DESPACHADO' and t.rm_consec == 88
        ids = {j.id for j in fallidos_vigentes(tipos=['DESPACHO_F470'])['jobs']}
        assert job.id not in ids, 'la emisión resuelta a mano sigue contando como trabada'


# ─────────────────────────────────────────────────────────────────────────────
# H7 · Reabrir el picking de una caja que ya salió: el faltante no tiene caja
# ─────────────────────────────────────────────────────────────────────────────

class TestReabrirConLaCajaDespachada:

    # Cerrado 2026-09-27 (v2-inv, 3ec9e512): PickingService.motivo_caja_no_recibe.
    def test_no_nace_un_faltante_para_una_caja_que_ya_salio(self, db, almacen):
        from app.models.picking import TareaPicking
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        from tests.test_inventario_no_se_resta_sin_destino import _escenario
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7101')
        caja = PackingService.crear_desde_picking(
            tareas_picking_ids=[t.id], numero_pedido_siesa='PD7101',
            almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa='7101')
        caja.estado, caja.siesa_triggered = 'DESPACHADO', True
        caja.rm_tipo, caja.rm_consec = 'RM', 55
        caja.fe_confirmada_at = datetime.utcnow()
        db.session.commit()
        try:
            PickingService.reabrir_picking(t.id, sup.id, motivo='apareció el resto')
        except ValueError:
            return  # arreglado: se niega
        vivas = TareaPicking.query.filter_by(referencia_documento='PD7101',
                                             estado='PENDIENTE').count()
        assert vivas == 0, 'nació un picking por el faltante de una caja que ya salió'
