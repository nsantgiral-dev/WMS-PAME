"""
P3 de la validación 2026-09-26: el latido de un servicio renombrado no queda
«callado» para siempre, y el tope de verificación de anulados del sync de
pedidos no se lo comen los traslados.

(Los crons con rango de horas propio están en `test_ventana_siesa.py`; el lock
ocupado de la carga en `test_carga_fisica_vigente.py`; la sesión del llamador
del sello en `test_sello_ambiente.py`.)
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.services import cron_latido
from app.services import pedidos_sync_service as pss


class TestElLatidoSeJuzgaPorCron:

    def _fila(self, db, nombre, servicio, hace, ok=True):
        from app.models.cron_latido import CronLatido
        t = datetime.utcnow() - hace
        db.session.add(CronLatido(nombre=nombre, servicio=servicio, ultimo_inicio=t,
                                  ultimo_fin=t, ultimo_ok=ok, corridas=1, fallos_seguidos=0))
        db.session.commit()

    def test_un_servicio_renombrado_no_deja_el_cron_callado(self, db):
        self._fila(db, 'dlq_siesa_jobs', 'worker-viejo', timedelta(days=10))
        self._fila(db, 'dlq_siesa_jobs', 'worker', timedelta(minutes=2))
        e = cron_latido.estado()
        assert 'dlq_siesa_jobs' not in e['callados']
        assert 'dlq_siesa_jobs@worker-viejo' in e['caducados']

    def test_el_viejo_que_fallaba_tampoco_queda_fallando(self, db):
        self._fila(db, 'dlq_siesa_jobs', 'worker-viejo', timedelta(days=10), ok=False)
        self._fila(db, 'dlq_siesa_jobs', 'worker', timedelta(minutes=2))
        assert 'dlq_siesa_jobs' not in cron_latido.estado()['fallando']

    def test_un_cron_que_de_verdad_no_corre_sigue_callado(self, db):
        self._fila(db, 'dlq_siesa_jobs', 'worker', timedelta(hours=3))
        assert 'dlq_siesa_jobs' in cron_latido.estado()['callados']

    def test_el_ultimo_que_fallo_manda(self, db):
        self._fila(db, 'dlq_siesa_jobs', 'web', timedelta(minutes=30))
        self._fila(db, 'dlq_siesa_jobs', 'worker', timedelta(minutes=1), ok=False)
        assert cron_latido.estado()['fallando'] == ['dlq_siesa_jobs']


def _pk(id_, *, tipo_doc='PEDIDO', numero=None, tipo='PD', consec=None, anulado=False):
    return SimpleNamespace(id=id_, tipo_documento=tipo_doc,
                           numero_pedido_siesa=numero if numero is not None else f'PD{id_}',
                           tipo_docto_pedido_siesa=tipo,
                           consec_docto_pedido_siesa=consec if consec is not None else str(id_),
                           pedido_anulado_siesa=anulado)


class TestElTopeDeAnuladosNoSeLoComenLosTraslados:

    def setup_method(self):
        pss._ROTACION_ANULADOS['ultimo_id'] = 0

    def test_traslados_y_no_consultables_no_ocupan_turno(self):
        vivos = ([_pk(i, tipo_doc='TRASLADO', numero=f'ST-{i}') for i in range(1, 11)]
                 + [_pk(20, consec='PE-X'), _pk(21, tipo=None), _pk(30)])
        turno, esperan = pss.candidatos_a_verificar(vivos, set())
        assert [p.id for p in turno] == [30] and esperan == 0

    def test_rota_y_nadie_queda_sin_turno(self):
        vivos = [_pk(i) for i in range(1, 13)]
        t1, esperan = pss.candidatos_a_verificar(vivos, set())
        assert len(t1) == 10 and esperan == 2
        t2, _ = pss.candidatos_a_verificar(vivos, set())
        assert {11, 12} <= {p.id for p in t2}

    def test_los_comprometidos_y_los_ya_marcados_no_entran(self):
        vivos = [_pk(1), _pk(2, anulado=True), _pk(3)]
        turno, _ = pss.candidatos_a_verificar(vivos, {'PD1'})
        assert [p.id for p in turno] == [3]
