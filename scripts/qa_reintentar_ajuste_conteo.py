"""
Reintenta los jobs AJUSTE_CONTEO pendientes de scripts/qa_conteo_real.db
(quedaron en PENDIENTE por el bug de SIESA_TIPO_DOCTO_AJUSTE=AFI, ya
corregido a ADI en .env.qa) contra Siesa QA real, y verifica el resultado
final (Siesa releido + WMS local) para los dos escenarios.
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

BODEGA = 'NB1'


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

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_conteo_real.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-real-32-bytes-o-mas-para-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

    from app import create_app
    from app.extensions import db
    app = create_app()
    with app.app_context():
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)

        import app.services.siesa_job_service as _sjs
        _sjs.disparar_dlq_inmediato = lambda *a, **k: None
        from app.services.siesa_job_service import procesar_jobs_pendientes

        from app.services.connekta_gateway import connekta
        from app.services.conteo_service import ConteoService
        from app.models.conteo import SesionConteo
        from app.models.siesa_job import SiesaJob
        from app.models.inventario import UbicacionProducto

        print(f'tipo_docto_ajuste efectivo: {connekta.tipo_docto_ajuste!r}')

        if args.disparar_real:
            if not args.si_de_verdad:
                print('[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('*** MODO_ENSAYO APAGADO — reintento REAL ***')

        print('\n--- Reintentando DLQ ---')
        for vuelta in range(4):
            for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
                j.proximo_intento = None
            db.session.commit()
            n = procesar_jobs_pendientes()
            print(f'  vuelta {vuelta + 1}: {n} job(s) procesados')
            if n == 0:
                break

        print('\n=== RESULTADO FINAL ===')
        for job in SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO').all():
            sesion = SesionConteo.query.get(job.referencia_id)
            inv = UbicacionProducto.query.filter_by(
                ubicacion_id=sesion.ubicacion_id, producto_id=sesion.producto_id).first()
            real_despues = ConteoService.consultar_existencia_siesa(
                sesion.producto_codigo_siesa, bodega=BODEGA) if args.disparar_real else None

            print(f'\n{sesion.producto_codigo_siesa} ({sesion.motivo_codigo})')
            print(f'  job {job.id}: {job.estado} (error={job.error_ultimo!r})')
            print(f'  sesion: estado={sesion.estado} siesa_triggered={sesion.siesa_triggered} '
                  f'diferencia={sesion.diferencia}')
            print(f'  WMS local: {inv.cantidad if inv else None}')
            if real_despues is not None:
                print(f'  Siesa releido: {real_despues}')


if __name__ == '__main__':
    main()
