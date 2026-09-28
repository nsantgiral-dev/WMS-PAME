"""
La cola de Siesa en temporada (2026-09-26).

1. **Orden**: `q.limit(20)` no tenía ORDER BY. Ahora `orden_de_la_cola`:
   lo que lleva > 30 min esperando turno primero, después la emisión fiscal
   (el muelle espera la factura), después NC → RC → DC, después FIFO.
2. **Esperar no traba**: una caja cuya RM todavía no aparece (Regla 20) se
   reprograma y la cola sigue con la siguiente en el mismo ciclo.
3. **Throughput medido** con un mundo simulado: reloj virtual, latencias
   típicas medidas en QA (GET ~1 s, POST ~3 s), la RM visible 12 s después
   del 142945, un ciclo por minuto con su tope de 50 s. El número queda
   escrito en CLAUDE.md («Fiscal v2»), y el test pone un piso.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from tests.test_documento_fiscal import siesa_real  # noqa: F401 — fixture

LAT_GET = 1.0
LAT_POST = 3.0
RM_VISIBLE_TRAS = 12.0      # Regla 20
TOPE_CICLO = 50             # `_DLQ_MAX_SECONDS`


class _Reloj:
    def __init__(self):
        self.t = 0.0
        self.base = datetime.utcnow()

    def ahora(self):
        return self.base + timedelta(seconds=self.t)


@pytest.fixture
def mundo(siesa_real, monkeypatch):
    """Siesa simulada con latencias sobre un reloj virtual. Cuenta GET/POST."""
    import time as _time

    from app.services import cartera_service
    from app.services import siesa_job_service as sjs
    from app.services.connekta_gateway import ConnektaGateway

    reloj = _Reloj()

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return reloj.ahora()

    monkeypatch.setattr(sjs, 'datetime', _DT)
    monkeypatch.setattr(_time, 'sleep', lambda s: setattr(reloj, 't', reloj.t + s))

    estado = {'rm_visible': {}, 'fe': set(), 'gets': 0, 'posts': 0, 'rm_n': 0}

    def get(dt=LAT_GET):
        reloj.t += dt
        estado['gets'] += 1

    def post():
        reloj.t += LAT_POST
        estado['posts'] += 1

    def factura(self, t, c):
        get()
        return ([{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': f'9{c}'}]
                if c in estado['fe'] else [])

    def estado_pedido(self, t, c):
        get()
        return 3

    def cabecera(self, t, c):
        get()
        return {'f430_rowid': 1, 'f430_id_cond_pago': 'C02'}

    def compromisos(self, t, c, r=None):
        get()
        return [{'f120_referencia': 'SKU1', 'f431_rowid': 11, 'f120_id': 5,
                 'f405_cant_por_remisionar_base': 10, 'f405_id_unidad_medida': 'UND'}]

    def comprometer(self, consec, payload):
        post()
        return {'codigo': 0}

    def despacho(self, tipo, consec, items, **k):
        post()
        estado['rm_n'] += 1
        estado['rm_visible'][str(consec)] = (reloj.t + RM_VISIBLE_TRAS, estado['rm_n'])
        return {'codigo': 0, 'mensaje': 'Transacción Exitosa'}

    def remision(self, tipo, consec, **k):
        get()
        v = estado['rm_visible'].get(str(consec))
        if v and reloj.t >= v[0]:
            return {'tipo': 'RM', 'consec': v[1]}
        return None

    def factura_rm(self, tipo, consec, cab):
        post()
        for c, (_, n) in estado['rm_visible'].items():
            if n == consec:
                estado['fe'].add(c)
        return {'codigo': 0, 'mensaje': 'Transacción Exitosa'}

    for nombre, fn in (('get_factura_desde_pedido', factura), ('get_estado_pedido', estado_pedido),
                       ('get_pedido_cabecera', cabecera), ('get_compromisos_pedido', compromisos),
                       ('trigger_comprometer_pedido', comprometer),
                       ('trigger_despacho', despacho), ('get_remision_desde_pedido', remision),
                       ('trigger_factura_desde_remision', factura_rm)):
        monkeypatch.setattr(ConnektaGateway, nombre, fn)
    monkeypatch.setattr(cartera_service, 'compuerta_emision', lambda t, cab: cab)
    monkeypatch.setattr(cartera_service, 'cabecera_para_factura', lambda t, cab: cab)
    return reloj, estado


def _caja(db, almacen, i, creada=None):
    from app.models.packing import TareaPacking
    from app.models.siesa_job import SiesaJob
    s = uuid.uuid4().hex[:6]
    consec = str(50000 + i)
    t = TareaPacking(codigo=f'PK-T-{s}', almacen_id=almacen.id, estado='VERIFICADO',
                     tipo_documento='PEDIDO', numero_pedido_siesa=f'PD{consec}',
                     tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=consec,
                     pedido_clave=f'003-PD-{consec}')
    db.session.add(t)
    db.session.flush()
    job = SiesaJob.encolar(tipo='DESPACHO_F470', referencia_tipo='TareaPacking',
                           referencia_id=t.id, payload={
                               'tarea_id': t.id, 'tipo_docto_pedido': 'PD',
                               'consec_docto_pedido': consec,
                               'numero_pedido_siesa': t.numero_pedido_siesa,
                               'items': [{'producto_codigo': 'SKU1', 'cantidad_empacada': 10}]})
    if creada is not None:
        job.fecha_creacion = creada
    db.session.commit()
    return t, job


def _correr_minutos(reloj, minutos):
    """Un ciclo por minuto; un ciclo que se pasa del minuto corre el siguiente
    enseguida (el scheduler no solapa: `max_instances=1`)."""
    from app.services.siesa_job_service import _run_dlq_jobs
    fin = minutos * 60
    proximo = 0.0
    while reloj.t < fin:
        if reloj.t < proximo:
            reloj.t = proximo
        inicio = reloj.t
        _run_dlq_jobs()
        proximo = max(inicio + 60, reloj.t)


class TestElOrdenDeLaCola:

    def _tipos(self, db, jobs, ahora):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import orden_de_la_cola
        return [j.tipo for j in SiesaJob.query.filter(SiesaJob.id.in_([x.id for x in jobs]))
                .order_by(*orden_de_la_cola(ahora)).all()]

    def _job(self, db, tipo, creada):
        from app.models.siesa_job import SiesaJob
        j = SiesaJob.encolar(tipo=tipo, payload={})
        j.fecha_creacion = creada
        db.session.commit()
        return j

    def test_la_emision_primero_y_la_plata_en_su_orden(self, db):
        ahora = datetime.utcnow()
        hace = ahora - timedelta(minutes=5)
        jobs = [self._job(db, t, hace) for t in
                ('DOCUMENTO_CONTABLE_RET', 'RECIBO_CAJA', 'ENTRADA_OC', 'NOTA_CREDITO_FACTURA',
                 'DESPACHO_F470')]
        assert self._tipos(db, jobs, ahora) == [
            'DESPACHO_F470', 'NOTA_CREDITO_FACTURA', 'RECIBO_CAJA',
            'DOCUMENTO_CONTABLE_RET', 'ENTRADA_OC']

    def test_lo_que_espera_mucho_pasa_adelante(self, db):
        ahora = datetime.utcnow()
        viejo = self._job(db, 'RECIBO_CAJA', ahora - timedelta(minutes=45))
        nuevos = [self._job(db, 'DESPACHO_F470', ahora - timedelta(minutes=1)) for _ in range(3)]
        assert self._tipos(db, [viejo, *nuevos], ahora)[0] == 'RECIBO_CAJA'

    def test_dentro_del_mismo_tipo_fifo(self, db):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import orden_de_la_cola
        ahora = datetime.utcnow()
        b = self._job(db, 'DESPACHO_F470', ahora - timedelta(minutes=2))
        a = self._job(db, 'DESPACHO_F470', ahora - timedelta(minutes=9))
        ids = [j.id for j in SiesaJob.query.filter(SiesaJob.id.in_([a.id, b.id]))
               .order_by(*orden_de_la_cola(ahora)).all()]
        assert ids == [a.id, b.id]


class TestEsperarNoTrabaLaCola:

    def test_la_rm_que_no_aparece_se_reprograma_y_la_cola_sigue(self, db, almacen, mundo):
        from app.models.siesa_job import EstadoSiesaJob
        from app.services.siesa_job_service import _run_dlq_jobs
        reloj, estado = mundo
        cajas = [_caja(db, almacen, i) for i in range(3)]
        _run_dlq_jobs()
        for t, job in cajas:
            db.session.refresh(job)
            db.session.refresh(t)
            # Las tres salieron (142945) en el mismo ciclo: ninguna esperó a otra.
            assert t.rm_enviada_at is not None
            assert job.estado == EstadoSiesaJob.PENDIENTE and job.proximo_intento is not None
        assert estado['posts'] == 6

    def test_tras_el_post_no_se_barre_la_consulta_entera(self, db, almacen, mundo):
        reloj, estado = mundo
        _caja(db, almacen, 0)
        from app.services.siesa_job_service import _run_dlq_jobs
        antes = estado['gets']
        _run_dlq_jobs()
        # reconciliación (FE + estado) + cabecera + compromisos + UNA página de RM
        assert estado['gets'] - antes == 5


class TestThroughputEnTemporada:
    """El mundo simulado de la temporada: más cajas de las que caben en una
    hora, cerradas de golpe. Mide cuántas quedan DESPACHADO (RM + FE)."""

    N = 400

    def _medir(self, db, almacen, mundo):
        from app.models.packing import TareaPacking
        reloj, estado = mundo
        for i in range(self.N):
            _caja(db, almacen, i)
        _correr_minutos(reloj, 60)
        return TareaPacking.query.filter_by(estado='DESPACHADO').count(), estado

    def test_cajas_por_hora(self, db, almacen, mundo):
        despachadas, estado = self._medir(db, almacen, mundo)
        # Cada caja: dos pasadas por la DLQ (emitir la RM; identificarla y
        # facturar), 10 GET y 3 POST ≈ 19 s de Siesa. Medido: 176 en la hora
        # (el tope de 50 s se mira ANTES de empezar un job, así que un ciclo
        # llega a ~58 s). Con el orden sin «lo empezado primero»: 86.
        print(f'\n[THROUGHPUT] {despachadas} cajas despachadas en 60 min simulados '
              f'({estado["gets"]} GET, {estado["posts"]} POST)')
        assert despachadas >= 160, despachadas
        assert estado['posts'] <= 3 * despachadas + 2 * (self.N - despachadas)

    def test_la_pausa_entre_jobs_solo_tras_una_caida(self, db, almacen, mundo, monkeypatch):
        """Con la pausa de 1 s por job de antes (bastaban 10 pendientes) salían
        168 en vez de 176 (~5 %). Ahora solo en los 10 min siguientes a una
        caída."""
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta._circuit, 'recien_recuperado', lambda *a, **k: True)
        con_pausa, _ = self._medir(db, almacen, mundo)
        print(f'\n[THROUGHPUT con pausa] {con_pausa} cajas en 60 min')
        assert con_pausa <= 170, con_pausa
