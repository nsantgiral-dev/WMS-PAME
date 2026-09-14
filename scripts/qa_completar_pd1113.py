"""
Recuperacion puntual de PD1113 -- la RM (RM-1565) ya se creo de verdad en
Siesa QA (confirmado con get_remision_desde_pedido, solo lectura), pero la
base local aislada no se entero (una carrera entre disparar_dlq_inmediato()
y la llamada manual del script, ver conversacion 2026-09-04). Reconciliamos
el dato local con el hecho real, y disparamos SOLO el 142943 que falta.

MODO_ENSAYO arranca activo (heredado de .env.qa) -- primero muestra el
payload sin mandarlo. Con --disparar-real --si-de-verdad lo manda de verdad.
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_ciclo.db')
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--disparar-real', action='store_true')
    ap.add_argument('--si-de-verdad', action='store_true')
    args = ap.parse_args()

    app = create_app()
    with app.app_context():
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)
        db.create_all()

        import app.services.siesa_job_service as _sjs
        _sjs.disparar_dlq_inmediato = lambda *a, **k: None

        from app.services.connekta_gateway import connekta
        from app.models.packing import TareaPacking

        print(f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')

        tarea = TareaPacking.query.get(1)
        print(f'ANTES: siesa_triggered={tarea.siesa_triggered} rm={tarea.rm_tipo}/{tarea.rm_consec} '
              f'fe={tarea.fe_tipo}/{tarea.fe_consec}')

        if tarea.siesa_triggered:
            print('Ya está siesa_triggered=True — nada que hacer.')
            return

        tarea.rm_tipo = 'RM'
        tarea.rm_consec = '1565'
        db.session.commit()
        print('Reconciliado localmente: rm_tipo=RM rm_consec=1565 (RM real verificada en Siesa QA)')

        if args.disparar_real:
            if not args.si_de_verdad:
                print('[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO — el 142943 que sigue es REAL ***')

        from app.services.despacho_parcial_service import DespachoParialService
        resultado = DespachoParialService.despachar_parcial(tarea, {})
        print('\nresultado:', resultado)

        tarea = TareaPacking.query.get(1)
        print(f'\nDESPUÉS: siesa_triggered={tarea.siesa_triggered} rm={tarea.rm_tipo}/{tarea.rm_consec} '
              f'fe={tarea.fe_tipo}/{tarea.fe_consec}')


if __name__ == '__main__':
    main()
