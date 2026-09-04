"""
Retoma la liquidacion de un pedido cuyo despacho+recaudo ya quedaron
armados (por qa_ciclo_completo.py) en su base aislada -- confirma la
devolucion fisica si aplica y drena la cola real (procesar_jobs_pendientes,
el mismo que usa el scheduler) hasta que no quede nada pendiente.

Uso: venv/Scripts/python.exe scripts/qa_continuar_liquidacion.py PD1125
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

pedido = sys.argv[1]
disparar_real = '--disparar-real' in sys.argv and '--si-de-verdad' in sys.argv

os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', f'qa_{pedido.lower()}.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-ciclo-real-32-bytes-o-mas-para-hmac')
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

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
    if disparar_real:
        connekta.modo_ensayo = False
        print('*** MODO_ENSAYO APAGADO — lo que sigue es REAL ***')
    print(f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')

    from app.models.devolucion_cliente import DevolucionCliente, EstadoDevolucionCliente
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import SiesaJob
    from app.services.devolucion_cliente_service import DevolucionClienteService
    from app.services.siesa_job_service import procesar_jobs_pendientes

    dev = DevolucionCliente.query.filter_by(estado=EstadoDevolucionCliente.ABIERTA).first()
    if dev:
        print(f'confirmando entrada física de devolución {dev.id}...')
        DevolucionClienteService.confirmar_entrada_fisica(dev.id, recepcionista_id=1)
        db.session.commit()

    for vuelta in range(5):
        for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
            j.proximo_intento = None
        db.session.commit()
        n = procesar_jobs_pendientes()
        print(f'vuelta {vuelta + 1}: {n} job(s) procesados')
        if n == 0:
            break

    print('\n=== ESTADO FINAL ===')
    for r in RecaudoEntrega.query.all():
        print('recaudo:', r.to_dict())
    for j in SiesaJob.query.all():
        print(f'  JOB {j.id} {j.tipo} -> {j.estado} (error={j.error_ultimo!r})')
