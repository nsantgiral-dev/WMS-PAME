"""
Conteo Definitivo (CC3) REAL de punta a punta contra Siesa QA — 3 variantes
NUEVAS, independientes entre si (no repite lo ya probado: CC1==CC2
sobrante/faltante en qa_conteo_ciclico_real.py, ni CC3-discordante-por-encima
en qa_conteo_definitivo_real.py):

  A) CC3 CONFIRMA_CC2 (PAPELSP9830): CC1 y CC2 discordantes -> CC3 (supervisor)
     cuenta exactamente lo mismo que CC2 (y distinto de CC1) -> el ajuste sale
     con la cantidad de CC2.
  B) CC3 CONFIRMA_CC1 (PAPELSP6948): mismo arranque -> CC3 cuenta exactamente
     lo mismo que CC1 (y distinto de CC2) -> el ajuste sale con la cantidad
     de CC1.
  C) CC3_NI_CC1_NI_CC2_ABAJO (PAPELSP9218): CC3 no coincide con ninguno de
     los dos Y queda POR DEBAJO de la existencia real en Siesa -> faltante
     (AJ-SAL) sobre la cantidad que dice el supervisor, no sobre CC1 ni CC2.

Cada escenario es independiente: producto, ubicacion y actores propios
(sufijo por escenario), y cada uno lee su "existencia real en Siesa" recien
antes de arrancar — igual que qa_conteo_definitivo_real.py.

Flujo real por escenario: ConteoService.registrar_conteo (CC1, CC2) ->
GET /api/conteo/definitivos (cola del supervisor) -> GET /api/conteo/<id>/tarea
(autoasignacion) -> POST /api/mobile/escanear x N -> POST /api/mobile/confirmar
(propaga a la raiz CC1) -> PUT /api/conteo/<raiz_id>/ajustar (aprobacion del
supervisor) -> DLQ real -> POST 142951 a Siesa.

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).
disparar_dlq_inmediato desactivado -- el DLQ se corre a mano.

Uso:
    venv/Scripts/python.exe scripts/qa_conteo_cc3_variantes_real.py
    venv/Scripts/python.exe scripts/qa_conteo_cc3_variantes_real.py --disparar-real --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

BODEGA = 'NB1'
CENTRO_OP = '003'

ESCENARIOS = [
    {
        'id': 'A', 'tipo': 'CONFIRMA_CC2', 'nombre': 'CC3 confirma CC2',
        'codigo_siesa': 'PAPELSP9830', 'nombre_item': 'SOBRE DE MANILA CARTA ESP 25X 31',
        'cc1_delta': +6, 'cc2_delta': -4, 'cc3_delta': -4,  # cc3 == cc2
    },
    {
        'id': 'B', 'tipo': 'CONFIRMA_CC1', 'nombre': 'CC3 confirma CC1',
        'codigo_siesa': 'PAPELSP6948', 'nombre_item': 'MARCADOR SHARPIE 0TANK GRUESO ROJO',
        'cc1_delta': +6, 'cc2_delta': -4, 'cc3_delta': +6,  # cc3 == cc1
    },
    {
        'id': 'C', 'tipo': 'NI_CC1_NI_CC2_ABAJO', 'nombre': 'CC3 no coincide con ninguno — por debajo de Siesa',
        'codigo_siesa': 'PAPELSP9218', 'nombre_item': 'RESMA DE PAPEL CARTA REPROGRAF',
        'cc1_delta': +4, 'cc2_delta': -3, 'cc3_delta': -7,  # cc3 != cc1, != cc2, < 0
    },
    {
        'id': 'D', 'tipo': 'NI_CC1_NI_CC2_ARRIBA', 'nombre': 'CC3 no coincide con ninguno — por encima de Siesa',
        'codigo_siesa': 'BELLESB1382', 'nombre_item': 'BELLEZA — item QA',
        'cc1_delta': +4, 'cc2_delta': -3, 'cc3_delta': +9,  # cc3 != cc1, != cc2, > 0
    },
]


def _register_pg_stubs(dbapi_conn, _connection_record):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--disparar-real', action='store_true')
    ap.add_argument('--si-de-verdad', action='store_true')
    ap.add_argument('--solo', help='Solo corre el/los escenario(s) con este id (ej: D, o A,C)')
    args = ap.parse_args()
    escenarios_a_correr = ESCENARIOS
    if args.solo:
        ids = {s.strip().upper() for s in args.solo.split(',')}
        escenarios_a_correr = [e for e in ESCENARIOS if e['id'] in ids]

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_conteo_cc3_variantes.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-cc3-variantes-32-bytes-x')
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

        print(f'=== CC3 — 3 variantes nuevas === modo_simulacion={connekta.modo_simulacion} '
              f'modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales'); sys.exit(1)

        client = app.test_client()

        resultados = []

        for esc in escenarios_a_correr:
            print(f"\n{'=' * 70}\n--- Escenario {esc['id']}: {esc['nombre']} ({esc['codigo_siesa']}) ---")

            # Almacen propio por escenario — apunta a la misma bodega/CO real de
            # Siesa (NB1/003), pero aisla el auto-pareo de CC2
            # (ConteoService._crear_conteo_verificacion busca "otro picker del
            # mismo almacen_id"): con un almacen local compartido entre
            # escenarios, ese auto-pareo podia agarrar el picker_b de OTRO
            # escenario en vez del de este.
            almacen = Almacen(codigo=f"NB1-CC3-{esc['id']}", nombre='Neiva Bodega CD',
                               bodega_siesa_id=BODEGA, centro_op_siesa=CENTRO_OP, activo=True)
            db.session.add(almacen)
            db.session.flush()

            def _actor(email, nombre, rol):
                u = Usuario(nombre=nombre, email=email,
                            password_hash=generate_password_hash('qa123456'),
                            rol=rol, puede_picar=True, almacen_id=almacen.id, activo=True)
                db.session.add(u)
                db.session.flush()
                return u

            real_antes = ConteoService.consultar_existencia_siesa(esc['codigo_siesa'], bodega=BODEGA)
            if real_antes is None:
                print('[ERROR] Siesa no respondio la existencia — abortando este escenario')
                continue
            print(f'  existencia real en Siesa (antes): {real_antes}')

            cc1_val = real_antes + esc['cc1_delta']
            cc2_val = real_antes + esc['cc2_delta']
            cc3_val = real_antes + esc['cc3_delta']
            assert cc1_val != cc2_val, 'CC1 y CC2 deben ser discordantes para forzar CC3'
            print(f'  CC1={cc1_val}  CC2={cc2_val} (discordantes -> fuerza CC3)')
            print(f'  CC3 (definitivo, supervisor)={cc3_val}')

            sufijo = esc['id']
            producto = Producto(codigo=f"CC3-{sufijo}-{esc['codigo_siesa']}", nombre=esc['nombre_item'],
                                 codigo_siesa=esc['codigo_siesa'], unidad_negocio_id='001')
            db.session.add(producto)
            db.session.flush()
            ubicacion = Ubicacion(codigo=f"CC3-{sufijo}-{esc['codigo_siesa']}", almacen_id=almacen.id,
                                   tipo_zona='PICKING', stock_minimo=0, stock_maximo=999999,
                                   secuencia_ruteo=1, activo=True)
            db.session.add(ubicacion)
            db.session.flush()
            db.session.add(UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                                              cantidad=real_antes, reservado=0, bloqueado=0))

            picker_a = _actor(f'picker_a_cc3{sufijo}_qa@wms-pame.local', f'Picker A CC3-{sufijo} QA', 'operario')
            picker_b = _actor(f'picker_b_cc3{sufijo}_qa@wms-pame.local', f'Picker B CC3-{sufijo} QA', 'operario')
            supervisor = _actor(f'supervisor_cc3{sufijo}_qa@wms-pame.local', f'Supervisor CC3-{sufijo} QA', 'supervisor')
            db.session.commit()

            with app.app_context():
                tok_sup = create_access_token(identity=str(supervisor.id))
            H = {'Authorization': f'Bearer {tok_sup}'}

            # ── CC1 ──
            creado = ConteoService.crear_conteo_manual(almacen.id, esc['codigo_siesa'])
            assert creado['tareas_creadas'] == 1, creado  # 1 sola ubicacion con stock por escenario
            cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first()
            ConteoService.obtener_tarea_operario(cc1.id, picker_a.id)
            r1 = ConteoService.registrar_conteo(cc1.id, picker_a.id, int(cc1_val))
            print(f'  CC1={cc1_val} -> {r1["resultado"]}')
            assert r1['resultado'] == 'SEGUNDO_CONTEO', r1

            # ── CC2 (discordante con CC1 a propósito) ──
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

            # ── Supervisor: cola de Conteo Definitivo (misma vista de la pantalla) ──
            r = client.get('/api/conteo/definitivos', headers=H)
            assert r.status_code == 200, r.get_json()
            ids_cola = [p['id'] for p in r.get_json()['pendientes']]
            assert cc3_id in ids_cola, (cc3_id, ids_cola)
            print(f'  CC3 visible en la cola del supervisor: OK (total={r.get_json()["total"]})')

            # ── Autoasignación (abrir la tarea) ──
            r = client.get(f'/api/conteo/{cc3_id}/tarea', headers=H)
            assert r.status_code == 200, r.get_json()

            # ── Escaneo real (mismo endpoint que usa la pantalla) ──
            for _ in range(int(cc3_val)):
                r = client.post('/api/mobile/escanear',
                                 json={'tarea_id': cc3_id, 'tipo': 'CONTEO',
                                       'codigo': esc['codigo_siesa'], 'cantidad': 1},
                                 headers=H)
                assert r.status_code == 200, r.get_json()
            print(f'  cantidad_contada final (CC3): {r.get_json()["cantidad_contada"]}')

            # ── Confirmar CC3 -> propaga a la raíz (CC1) ──
            r = client.post('/api/mobile/confirmar',
                             json={'tarea_id': cc3_id, 'tipo': 'CONTEO', 'items_escaneados': []},
                             headers=H)
            assert r.status_code == 200, r.get_json()
            body = r.get_json()
            raiz_id = body['raiz_id']
            assert raiz_id == cc1.id, (raiz_id, cc1.id)
            print(f'  CC3 confirmado — propagado a raiz CC1 #{raiz_id}: {body}')

            # ── Verificación específica del escenario ──
            if esc['tipo'] == 'CONFIRMA_CC2':
                assert cc3_val == cc2_val and cc3_val != cc1_val, 'CC3 debía coincidir solo con CC2'
                print('  [OK] CC3 coincide exactamente con CC2 (y no con CC1)')
            elif esc['tipo'] == 'CONFIRMA_CC1':
                assert cc3_val == cc1_val and cc3_val != cc2_val, 'CC3 debía coincidir solo con CC1'
                print('  [OK] CC3 coincide exactamente con CC1 (y no con CC2)')
            elif esc['tipo'] == 'NI_CC1_NI_CC2_ABAJO':
                assert cc3_val != cc1_val and cc3_val != cc2_val and cc3_val < real_antes, \
                    'CC3 debía ser distinto de CC1 y CC2, y quedar por debajo de Siesa'
                print('  [OK] CC3 no coincide con CC1 ni CC2, y queda por debajo de Siesa (faltante)')
            elif esc['tipo'] == 'NI_CC1_NI_CC2_ARRIBA':
                assert cc3_val != cc1_val and cc3_val != cc2_val and cc3_val > real_antes, \
                    'CC3 debía ser distinto de CC1 y CC2, y quedar por encima de Siesa'
                print('  [OK] CC3 no coincide con CC1 ni CC2, y queda por encima de Siesa (sobrante)')

            resultados.append({
                'escenario': esc['id'], 'tipo': esc['tipo'], 'nombre': esc['nombre'],
                'codigo_siesa': esc['codigo_siesa'], 'raiz_id': raiz_id,
                'real_antes': real_antes, 'cc1_val': cc1_val, 'cc2_val': cc2_val, 'cc3_val': cc3_val,
                'ubicacion_id': ubicacion.id, 'producto_id': producto.id,
                'supervisor_headers': H,
            })

        if args.disparar_real:
            if not args.si_de_verdad:
                print('\n[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('\n*** MODO_ENSAYO APAGADO — cada aprobacion va a postear 142951 REAL a Siesa ***')

        # ── Cada supervisor aprueba SU ajuste ──
        print(f"\n{'=' * 70}\n--- Aprobaciones (PUT /api/conteo/<raiz_id>/ajustar) ---")
        for r in resultados:
            resp = client.put(f"/api/conteo/{r['raiz_id']}/ajustar", headers=r['supervisor_headers'])
            print(f"  Escenario {r['escenario']} (raiz #{r['raiz_id']}): "
                  f"status={resp.status_code} body={resp.get_json()}")

        print('\n--- Procesando DLQ (AJUSTE_CONTEO) ---')
        for vuelta in range(4):
            for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
                j.proximo_intento = None
            db.session.commit()
            n = procesar_jobs_pendientes()
            print(f'  vuelta {vuelta + 1}: {n} job(s) procesados')
            if n == 0:
                break

        print(f"\n{'=' * 70}\n=== RESULTADO FINAL ===")
        for r in resultados:
            raiz = SesionConteo.query.get(r['raiz_id'])
            job = SiesaJob.query.filter_by(
                referencia_tipo='SesionConteo', referencia_id=r['raiz_id'],
                tipo='AJUSTE_CONTEO').order_by(SiesaJob.id.desc()).first()
            inv = UbicacionProducto.query.filter_by(
                ubicacion_id=r['ubicacion_id'], producto_id=r['producto_id']).first()
            real_despues = None
            if args.disparar_real:
                real_despues = ConteoService.consultar_existencia_siesa(r['codigo_siesa'], bodega=BODEGA)

            print(f"\nEscenario {r['escenario']} — {r['nombre']} ({r['codigo_siesa']})")
            print(f"  Siesa ANTES: {r['real_antes']}")
            print(f"  CC1={r['cc1_val']}  CC2={r['cc2_val']}  CC3 (definitivo)={r['cc3_val']}")
            print(f"  sesion raiz: estado={raiz.estado} motivo={raiz.motivo_codigo} "
                  f"cantidad_fisica={raiz.cantidad_fisica} diferencia={raiz.diferencia} "
                  f"aprobador_id={raiz.aprobador_id} siesa_triggered={raiz.siesa_triggered}")
            print(f"  job: {job.tipo if job else None} -> {job.estado if job else None} "
                  f"(error={job.error_ultimo if job else None})")
            print(f"  WMS local (UbicacionProducto.cantidad) DESPUES: {inv.cantidad if inv else None}")
            if real_despues is not None:
                print(f"  Siesa DESPUES (real, releido): {real_despues}")
                print(f"  alineado con CC3 (definitivo): {real_despues == r['cc3_val']}")


if __name__ == '__main__':
    main()
