"""
Conteo Definitivo (CC3) REAL de punta a punta contra Siesa QA, verificando
ademas los fixes del 2026-09-11 sobre el flujo de conteo ciclico:

  CC1 y CC2 discordantes entre si (no coinciden) -> se crea CC3 sin asignar
  -> un supervisor lo toma desde la cola (GET /api/conteo/definitivos) ->
  lo cuenta (blind, via POST /api/mobile/escanear + /api/mobile/confirmar,
  el mismo camino que usa la pantalla) -> el resultado de CC3 se propaga a
  la raiz (CC1) -> el supervisor aprueba el ajuste (PUT /api/conteo/
  <raiz_id>/ajustar) -> Siesa real (142951).

A diferencia de qa_conteo_definitivo_real.py (2026-09-04, que ya probo este
flujo una vez), este script pasa TODO por el cliente HTTP de Flask (no
llamadas directas al servicio) para ejercitar los guards nuevos de
`MobileService.procesar_escaneo`:

  1. picker_a (dueño de CC1, NO asignado a CC3) intenta escanear el CC3 del
     supervisor -> debe ser rechazado (ownership).
  2. Una vez CC3 confirmado y la raiz en AJUSTANDO/AJUSTADO, un reintento de
     escaneo sobre CC3 -> debe ser rechazado (estado).

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_conteo_definitivo_fixes_e2e_real.py
    venv/Scripts/python.exe scripts/qa_conteo_definitivo_fixes_e2e_real.py --disparar-real --si-de-verdad
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


def _ok(msg):
    print(f'  [OK] {msg}')


def _falla(msg):
    print(f'  [FALLO] {msg}')
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--disparar-real', action='store_true')
    ap.add_argument('--si-de-verdad', action='store_true')
    args = ap.parse_args()

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_conteo_definitivo_fixes_real.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-definitivo-fixes-32-bytes-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)
        db.drop_all()
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
        from app.models.conteo import SesionConteo, EstadoConteo
        from app.models.siesa_job import SiesaJob
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        print(f'=== Conteo Definitivo (CC3) E2E real (fixes 2026-09-11) === '
              f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales Connekta — revisar .env.qa'); sys.exit(1)

        real_antes = ConteoService.consultar_existencia_siesa(CODIGO_SIESA, bodega=BODEGA)
        if real_antes is None:
            print('[ERROR] Siesa no respondio la existencia — abortando'); sys.exit(1)
        print(f'\nExistencia real en Siesa (antes): {real_antes}')

        cc1_val = real_antes + DELTA_CC1
        cc2_val = real_antes + DELTA_CC2
        cc3_val = real_antes + DELTA_CC3
        print(f'CC1={cc1_val}  CC2={cc2_val} (discordantes entre si -> fuerza CC3)')
        print(f'CC3 (definitivo, lo hace el supervisor)={cc3_val}')

        # ── Seed ──────────────────────────────────────────────────────────
        almacen = Almacen(codigo=BODEGA, nombre='Neiva Bodega CD',
                           bodega_siesa_id=BODEGA, centro_op_siesa=CENTRO_OP, activo=True)
        db.session.add(almacen)
        db.session.flush()
        producto = Producto(codigo=CODIGO_SIESA, nombre=NOMBRE_ITEM,
                             codigo_siesa=CODIGO_SIESA, unidad_negocio_id='001')
        db.session.add(producto)
        db.session.flush()
        ubicacion = Ubicacion(codigo=f'CC-DEF-FIX-{CODIGO_SIESA}', almacen_id=almacen.id,
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

        picker_a = _actor('picker_a_def_fix_qa@wms-pame.local', 'Picker A Def Fix QA', 'operario')
        picker_b = _actor('picker_b_def_fix_qa@wms-pame.local', 'Picker B Def Fix QA', 'operario')
        supervisor = _actor('supervisor_def_fix_qa@wms-pame.local', 'Supervisor Def Fix QA', 'supervisor')
        db.session.commit()

        client = app.test_client()
        tok_sup = create_access_token(identity=str(supervisor.id))
        tok_a = create_access_token(identity=str(picker_a.id))
        H_sup = {'Authorization': f'Bearer {tok_sup}'}
        H_a = {'Authorization': f'Bearer {tok_a}'}

        # ── CC1 ──────────────────────────────────────────────────────────
        print('\n--- CC1 ---')
        creado = ConteoService.crear_conteo_manual(almacen.id, CODIGO_SIESA)
        cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first()
        ConteoService.obtener_tarea_operario(cc1.id, picker_a.id)
        r1 = ConteoService.registrar_conteo(cc1.id, picker_a.id, int(cc1_val))
        print(f'  CC1={cc1_val} -> {r1["resultado"]}')
        assert r1['resultado'] == 'SEGUNDO_CONTEO', r1

        # ── CC2 (discordante con CC1 a propósito) ───────────────────────
        print('--- CC2 ---')
        cc2_id = r1['segundo_conteo_id']
        ConteoService.obtener_tarea_operario(cc2_id, picker_b.id)
        r2 = ConteoService.registrar_conteo(cc2_id, picker_b.id, int(cc2_val))
        print(f'  CC2={cc2_val} -> {r2["resultado"]} — {r2["mensaje"]}')
        assert r2['resultado'] == 'TERCER_CONTEO', r2
        cc3_id = r2['tercer_conteo_id']

        cc3 = SesionConteo.query.get(cc3_id)
        print(f'  CC3 creado: {cc3.codigo} operario_id={cc3.operario_id} (debe ser None)')
        assert cc3.operario_id is None

        # ── Supervisor: cola de Conteo Definitivo ───────────────────────
        print('\n--- Supervisor: cola de Conteo Definitivo ---')
        r = client.get('/api/conteo/definitivos', headers=H_sup)
        assert r.status_code == 200, r.get_json()
        print(f'  pendientes: {r.get_json()["total"]}')
        assert r.get_json()['total'] >= 1

        # ── Autoasignación del supervisor (igual que abrir la tarea) ────
        r = client.get(f'/api/conteo/{cc3_id}/tarea', headers=H_sup)
        assert r.status_code == 200, r.get_json()
        print(f'  supervisor autoasignado a CC3: {r.get_json()}')

        # FIX 1a — ownership: picker_a (dueño de CC1, NO de CC3) intenta escanear el CC3
        # ya asignado al supervisor. Antes del fix, cualquier operario de almacén podía
        # escanear hacia CUALQUIER sesion_id — exactamente el hueco que rompía el
        # double-blind justo en el conteo que DEFINE el ajuste.
        print('\n--- Guard: picker_a intenta escanear el CC3 del supervisor ---')
        r = client.post('/api/mobile/escanear',
                         json={'tarea_id': cc3_id, 'tipo': 'CONTEO',
                               'codigo': CODIGO_SIESA, 'cantidad': 1},
                         headers=H_a)
        if r.status_code == 400 and 'no está asignada a ti' in (r.get_json() or {}).get('error', ''):
            _ok(f'picker_a bloqueado al intentar escanear el CC3 ajeno: "{r.get_json()["error"]}"')
        else:
            _falla(f'picker_a NO fue bloqueado escaneando el CC3 del supervisor '
                   f'(status={r.status_code} body={r.get_json()}) — guard de ownership no funciono')

        # ── Escaneo real del supervisor (mismo endpoint que usa la pantalla) ──
        print('--- Supervisor cuenta CC3 (escaneo simulando lector) ---')
        r = client.post('/api/mobile/escanear',
                         json={'tarea_id': cc3_id, 'tipo': 'CONTEO',
                               'codigo': CODIGO_SIESA, 'total_acumulado': int(cc3_val)},
                         headers=H_sup)
        assert r.status_code == 200, r.get_json()
        print(f'  cantidad_contada: {r.get_json()["cantidad_contada"]}')

        # ── Confirmar CC3 -> propaga a la raíz (CC1) ────────────────────
        r = client.post('/api/mobile/confirmar',
                         json={'tarea_id': cc3_id, 'tipo': 'CONTEO', 'items_escaneados': []},
                         headers=H_sup)
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        print(f'  confirmar CC3: {body}')
        raiz_id = body['raiz_id']
        assert raiz_id == cc1.id

        # FIX 1b — estado: CC3 ya está DESCUADRE (o lo que sea tras confirmar), un
        # reintento de escaneo sobre él debe rechazarse — ya no es PENDIENTE/EN_PROCESO.
        print('\n--- Guard: reintento de escaneo sobre CC3 ya confirmado ---')
        r = client.post('/api/mobile/escanear',
                         json={'tarea_id': cc3_id, 'tipo': 'CONTEO',
                               'codigo': CODIGO_SIESA, 'cantidad': 1},
                         headers=H_sup)
        cc3_ahora = SesionConteo.query.get(cc3_id)
        if r.status_code == 400 and 'No se puede escanear' in (r.get_json() or {}).get('error', ''):
            _ok(f'reintento de escaneo sobre CC3 (estado={cc3_ahora.estado}) bloqueado: '
                f'"{r.get_json()["error"]}"')
        else:
            _falla(f'se pudo re-escanear un CC3 ya confirmado (estado={cc3_ahora.estado}) '
                   f'— guard de estado no funciono')

        if args.disparar_real:
            if not args.si_de_verdad:
                print('\n[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('\n*** MODO_ENSAYO APAGADO — el DLQ va a postear 142951 REAL a Siesa ***')

        # ── Supervisor aprueba el ajuste (PUT /api/conteo/<raiz_id>/ajustar) ──
        print('\n--- Supervisor aprueba el ajuste ---')
        r = client.put(f'/api/conteo/{raiz_id}/ajustar', headers=H_sup)
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

        print(f'Siesa ANTES: {real_antes}')
        print(f'CC1={cc1_val}  CC2={cc2_val}  CC3 (definitivo)={cc3_val}')
        print(f'sesion raiz: estado={cc1_final.estado} motivo={cc1_final.motivo_codigo} '
              f'cantidad_fisica={cc1_final.cantidad_fisica} diferencia={cc1_final.diferencia} '
              f'aprobador_id={cc1_final.aprobador_id} siesa_triggered={cc1_final.siesa_triggered}')
        print(f'job: {job.tipo if job else None} -> {job.estado if job else None} '
              f'(error={job.error_ultimo if job else None})')
        print(f'WMS local (UbicacionProducto.cantidad) DESPUES: {inv.cantidad if inv else None}')

        if args.disparar_real:
            real_despues = ConteoService.consultar_existencia_siesa(CODIGO_SIESA, bodega=BODEGA)
            print(f'Siesa DESPUES (real, releido): {real_despues}')
            alineado = real_despues == cc3_val
            print(f'alineado con CC3 (definitivo): {alineado}')
            if not (cc1_final.estado == EstadoConteo.AJUSTADO and cc1_final.siesa_triggered and alineado):
                _falla('el POST real a Siesa no dejo el estado esperado — revisar arriba')
            _ok('Siesa QA quedo alineado con el CC3 definitivo (142951 real, codigo:0) '
                '— ni con CC1 ni con CC2, como debe ser')

        print('\n=== Guards + flujo CC3 verificados de punta a punta ===')


if __name__ == '__main__':
    main()
