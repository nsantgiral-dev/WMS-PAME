"""
Conteo Definitivo (CC3) REAL de punta a punta contra Siesa QA:

  CC1 y CC2 discordantes entre si (no coinciden) -> se crea CC3 sin asignar
  -> un "supervisor" lo toma desde la cola (GET /api/conteo/definitivos,
  igual que la pantalla nueva) -> lo cuenta (blind, vía POST /api/mobile/
  escanear + /api/mobile/confirmar, igual que la pantalla) -> el resultado
  de CC3 se propaga a la raiz (CC1) -> el supervisor aprueba el ajuste
  (PUT /api/conteo/<raiz_id>/ajustar) -> Siesa real (142951).

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).
disparar_dlq_inmediato desactivado -- el DLQ se corre a mano.

Uso:
    venv/Scripts/python.exe scripts/qa_conteo_definitivo_real.py
    venv/Scripts/python.exe scripts/qa_conteo_definitivo_real.py --disparar-real --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

BODEGA = 'NB1'
CENTRO_OP = '003'
CODIGO_SIESA = 'PAPELSP9830'
NOMBRE_ITEM = 'SOBRE DE MANILA CARTA ESP 25X 31'
DELTA_CC1 = +5
DELTA_CC2 = -5
DELTA_CC3 = +8   # CC3 es definitivo — no necesita coincidir con CC1 ni CC2


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

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_conteo_definitivo.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-definitivo-32-bytes-o-mas-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)
        db.create_all()

        import app.services.siesa_job_service as _sjs
        _sjs.disparar_dlq_inmediato = lambda *a, **k: None
        from app.services.siesa_job_service import procesar_jobs_pendientes

        from app.services.connekta_gateway import connekta
        from app.services.conteo_service import ConteoService
        from app.models.almacen import Almacen
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        from app.models.inventario import UbicacionProducto
        from app.models.usuario import Usuario
        from app.models.conteo import SesionConteo
        from app.models.siesa_job import SiesaJob
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        print(f'=== Conteo Definitivo (CC3) real === modo_simulacion={connekta.modo_simulacion} '
              f'modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales'); sys.exit(1)

        real_antes = ConteoService.consultar_existencia_siesa(CODIGO_SIESA, bodega=BODEGA)
        if real_antes is None:
            print('[ERROR] Siesa no respondio la existencia — abortando'); sys.exit(1)
        print(f'existencia real en Siesa (antes): {real_antes}')

        cc1_val = real_antes + DELTA_CC1
        cc2_val = real_antes + DELTA_CC2
        cc3_val = real_antes + DELTA_CC3
        print(f'CC1={cc1_val}  CC2={cc2_val} (discordantes entre si -> fuerza CC3)')
        print(f'CC3 (definitivo, lo hace el supervisor)={cc3_val}')

        # ── Seed ──
        almacen = Almacen(codigo=BODEGA, nombre='Neiva Bodega CD',
                           bodega_siesa_id=BODEGA, centro_op_siesa=CENTRO_OP, activo=True)
        db.session.add(almacen)
        db.session.flush()
        producto = Producto(codigo=CODIGO_SIESA, nombre=NOMBRE_ITEM,
                             codigo_siesa=CODIGO_SIESA, unidad_negocio_id='001')
        db.session.add(producto)
        db.session.flush()
        ubicacion = Ubicacion(codigo=f'CC-{CODIGO_SIESA}', almacen_id=almacen.id,
                               tipo_zona='PICKING', stock_minimo=0, stock_maximo=99999,
                               secuencia_ruteo=1, activo=True)
        db.session.add(ubicacion)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                                          cantidad=real_antes, reservado=0, bloqueado=0))

        def _actor(email, nombre, rol):
            u = Usuario(nombre=nombre, email=email,
                        password_hash=generate_password_hash('qa123456'),
                        rol=rol, puede_picar=True, almacen_id=almacen.id, activo=True)
            db.session.add(u)
            db.session.flush()
            return u

        picker_a = _actor('picker_a_def_qa@wms-pame.local', 'Picker A Def QA', 'operario')
        picker_b = _actor('picker_b_def_qa@wms-pame.local', 'Picker B Def QA', 'operario')
        supervisor = _actor('supervisor_def_qa@wms-pame.local', 'Supervisor Def QA', 'supervisor')
        db.session.commit()

        client = app.test_client()
        with app.app_context():
            tok_sup = create_access_token(identity=str(supervisor.id))
        H = {'Authorization': f'Bearer {tok_sup}'}

        # ── CC1 ──
        print('\n--- CC1 ---')
        creado = ConteoService.crear_conteo_manual(almacen.id, CODIGO_SIESA)
        cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first()
        ConteoService.obtener_tarea_operario(cc1.id, picker_a.id)
        r1 = ConteoService.registrar_conteo(cc1.id, picker_a.id, int(cc1_val))
        print(f'  CC1={cc1_val} -> {r1["resultado"]}')
        assert r1['resultado'] == 'SEGUNDO_CONTEO', r1

        # ── CC2 (discordante con CC1 a propósito) ──
        print('--- CC2 ---')
        cc2_id = r1['segundo_conteo_id']
        cc2 = SesionConteo.query.get(cc2_id)
        ConteoService.obtener_tarea_operario(cc2_id, picker_b.id)
        r2 = ConteoService.registrar_conteo(cc2_id, picker_b.id, int(cc2_val))
        print(f'  CC2={cc2_val} -> {r2["resultado"]} — {r2["mensaje"]}')
        assert r2['resultado'] == 'TERCER_CONTEO', r2
        cc3_id = r2['tercer_conteo_id']

        cc3 = SesionConteo.query.get(cc3_id)
        print(f'  CC3 creado: {cc3.codigo} operario_id={cc3.operario_id} (debe ser None)')
        assert cc3.operario_id is None

        # ── El supervisor lo ve en la cola (misma vista que la pantalla) ──
        print('\n--- Supervisor: cola de Conteo Definitivo ---')
        r = client.get('/api/conteo/definitivos', headers=H)
        assert r.status_code == 200, r.get_json()
        print(f'  pendientes: {r.get_json()["total"]}')
        assert r.get_json()['total'] >= 1

        # ── Autoasignación (igual que abrir la tarea en la pantalla) ──
        r = client.get(f'/api/conteo/{cc3_id}/tarea', headers=H)
        assert r.status_code == 200, r.get_json()
        print(f'  supervisor autoasignado a CC3: {r.get_json()}')

        # ── Escaneo real (mismo endpoint que usa la pantalla) ──
        print('--- Supervisor cuenta CC3 (escaneo simulando lector) ---')
        for _ in range(int(cc3_val)):
            r = client.post('/api/mobile/escanear',
                             json={'tarea_id': cc3_id, 'tipo': 'CONTEO',
                                   'codigo': CODIGO_SIESA, 'cantidad': 1},
                             headers=H)
            assert r.status_code == 200, r.get_json()
        print(f'  cantidad_contada final: {r.get_json()["cantidad_contada"]}')

        # ── Confirmar CC3 -> propaga a la raíz (CC1) ──
        r = client.post('/api/mobile/confirmar',
                         json={'tarea_id': cc3_id, 'tipo': 'CONTEO', 'items_escaneados': []},
                         headers=H)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        print(f'  confirmar CC3: {body}')
        raiz_id = body['raiz_id']
        assert raiz_id == cc1.id

        if args.disparar_real:
            if not args.si_de_verdad:
                print('\n[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('\n*** MODO_ENSAYO APAGADO — el DLQ va a postear 142951 REAL a Siesa ***')

        # ── Supervisor aprueba el ajuste (PUT /api/conteo/<raiz_id>/ajustar) ──
        print('\n--- Supervisor aprueba el ajuste ---')
        r = client.put(f'/api/conteo/{raiz_id}/ajustar', headers=H)
        print(f'  status={r.status_code} body={r.get_json()}')

        print('\n--- Procesando DLQ (AJUSTE_CONTEO) ---')
        for vuelta in range(4):
            for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
                j.proximo_intento = None
            db.session.commit()
            n = procesar_jobs_pendientes()
            print(f'  vuelta {vuelta + 1}: {n} job(s) procesados')
            if n == 0:
                break

        print('\n=== RESULTADO FINAL ===')
        cc1_final = SesionConteo.query.get(cc1.id)
        job = SiesaJob.query.filter_by(
            referencia_tipo='SesionConteo', referencia_id=cc1.id,
            tipo='AJUSTE_CONTEO').order_by(SiesaJob.id.desc()).first()
        inv = UbicacionProducto.query.filter_by(
            ubicacion_id=ubicacion.id, producto_id=producto.id).first()
        real_despues = ConteoService.consultar_existencia_siesa(
            CODIGO_SIESA, bodega=BODEGA) if args.disparar_real else None

        print(f'Siesa ANTES: {real_antes}')
        print(f'CC1={cc1_val}  CC2={cc2_val}  CC3 (definitivo)={cc3_val}')
        print(f'sesion raiz: estado={cc1_final.estado} motivo={cc1_final.motivo_codigo} '
              f'cantidad_fisica={cc1_final.cantidad_fisica} diferencia={cc1_final.diferencia} '
              f'aprobador_id={cc1_final.aprobador_id} siesa_triggered={cc1_final.siesa_triggered}')
        print(f'job: {job.tipo if job else None} -> {job.estado if job else None} '
              f'(error={job.error_ultimo if job else None})')
        print(f'WMS local (UbicacionProducto.cantidad) DESPUES: {inv.cantidad if inv else None}')
        if real_despues is not None:
            print(f'Siesa DESPUES (real, releido): {real_despues}')
            print(f'alineado con CC3 (definitivo): {real_despues == cc3_val}')


if __name__ == '__main__':
    main()
