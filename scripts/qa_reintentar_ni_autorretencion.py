"""
Reintenta SOLO los jobs DOCUMENTO_CONTABLE_RET (142882/NI) que quedaron
FALLIDO/PENDIENTE por el bug de tercero en autorretencion (fix aplicado
2026-09-10 en connekta_liquidacion_gateway.py) -- contra la misma base
aislada del lote (scripts/qa_lote16.db), sin tocar picking/packing/despacho.
"""
import os, sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_lote16.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-lote16-real-32-bytes-o-mas-para-hmac')
os.environ['MODO_ENSAYO'] = 'false'
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)
os.environ['MODO_ENSAYO'] = 'false'

from app import create_app
from app.extensions import db


def _register_pg_stubs(dbapi_conn, _connection_record):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


app = create_app()
with app.app_context():
    from sqlalchemy import event as _sa_event
    _sa_event.listen(db.engine, 'connect', _register_pg_stubs)

    import app.services.siesa_job_service as _sjs
    _sjs.disparar_dlq_inmediato = lambda *a, **k: None

    from app.services.connekta_gateway import connekta
    print(f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')

    from app.models.siesa_job import SiesaJob
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services.siesa_job_service import procesar_jobs_pendientes

    jobs = SiesaJob.query.filter(
        SiesaJob.tipo == 'DOCUMENTO_CONTABLE_RET',
        SiesaJob.estado.in_(['FALLIDO', 'PENDIENTE']),
    ).all()
    print(f'{len(jobs)} jobs DOCUMENTO_CONTABLE_RET a reintentar: {[j.id for j in jobs]}')
    for j in jobs:
        j.estado = 'PENDIENTE'
        j.proximo_intento = None
    db.session.commit()

    for vuelta in range(5):
        n = procesar_jobs_pendientes()
        print(f'vuelta {vuelta + 1}: {n} job(s) procesados')
        if n == 0:
            break

    print('\n=== RESULTADO ===')
    for jid in [j.id for j in jobs]:
        j = SiesaJob.query.get(jid)
        print(f'  JOB {j.id} {j.tipo} (RecaudoEntrega#{j.referencia_id}) -> {j.estado} (error={j.error_ultimo!r})')

    print('\n--- Recaudos con motivo_descuento (autorretencion) ---')
    for r in RecaudoEntrega.query.filter(RecaudoEntrega.motivo_descuento.like('AUTORRETENCION%')).all():
        print(f'  recaudo={r.id} motivo={r.motivo_descuento} siesa_dc_triggered={r.siesa_dc_triggered}')
