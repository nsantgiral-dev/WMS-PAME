"""
E2E real contra Siesa QA de la funcionalidad nueva: forzar el operario del
primer conteo (CC1) al crear un conteo manual (2026-09-11).

Cubre, todo por la capa MobileService (mismo camino que /api/mobile/*):

  1. ConteoService.crear_conteo_manual(..., operario_id=picker_a) deja la
     sesión PENDIENTE-pero-asignada a picker_a — no EN_PROCESO todavía.
  2. picker_b (otro operario) NO puede tomarla ni escanearla — el guard de
     ownership existente respeta la asignación forzada igual que respeta
     cualquier otra.
  3. picker_a SÍ puede tomarla (obtener_tarea_operario) y contarla.
  4. CC1 discordante -> CC2 se autoasigna como siempre (esto NO lo cambia
     la feature nueva) -> picker_b lo cuenta con el MISMO valor que CC1
     ("verdad de bodega") -> dispara el auto-ajuste ya arreglado el
     2026-09-11 (fe18709) -> 142951 real a Siesa.
  5. Bonus: operario_id inexistente/inactivo -> ValueError, sin crear nada.

Base LOCAL AISLADA (nunca Postgres de producción), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_conteo_manual_forzado_e2e_real.py
    venv/Scripts/python.exe scripts/qa_conteo_manual_forzado_e2e_real.py --disparar-real --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

DELTA = 6.0
BODEGA = 'NB1'
CENTRO_OP = '003'
CODIGO_SIESA = 'PAPELSP6948'
NOMBRE_ITEM = 'MARCADOR SHARPIE 0TANK GRUESO ROJO'


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

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_conteo_manual_forzado_real.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-manual-forzado-32-bytes-x')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

    from app import create_app
    from app.extensions import db
    app = create_app()
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
        from app.services.mobile_service import MobileService
        from app.models.almacen import Almacen
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        from app.models.inventario import UbicacionProducto
        from app.models.usuario import Usuario
        from app.models.conteo import SesionConteo, EstadoConteo
        from app.models.siesa_job import SiesaJob
        from werkzeug.security import generate_password_hash

        print(f'=== Conteo manual con operario forzado — E2E real === '
              f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales Connekta — revisar .env.qa'); sys.exit(1)

        # ── Seed ──────────────────────────────────────────────────────────
        almacen = Almacen(codigo=BODEGA, nombre='Neiva Bodega CD',
                           bodega_siesa_id=BODEGA, centro_op_siesa=CENTRO_OP, activo=True)
        db.session.add(almacen)
        db.session.flush()

        def _actor(email, nombre):
            u = Usuario(nombre=nombre, email=email,
                        password_hash=generate_password_hash('qa123456'),
                        rol='operario', puede_picar=True, almacen_id=almacen.id, activo=True)
            db.session.add(u)
            db.session.flush()
            return u

        picker_a = _actor('picker_a_forzado_qa@wms-pame.local', 'Picker A Forzado QA')
        picker_b = _actor('picker_b_forzado_qa@wms-pame.local', 'Picker B Forzado QA')
        db.session.commit()

        real_antes = ConteoService.consultar_existencia_siesa(CODIGO_SIESA, bodega=BODEGA)
        if real_antes is None:
            print('[ERROR] Siesa no respondio la existencia — abortando'); sys.exit(1)
        print(f'\nExistencia real en Siesa (antes): {real_antes}')
        cantidad_contada = int(real_antes + DELTA)
        print(f'CC1 = CC2 = {cantidad_contada} (sobrante, por encima de Siesa)')

        producto = Producto(codigo=CODIGO_SIESA, nombre=NOMBRE_ITEM,
                             codigo_siesa=CODIGO_SIESA, unidad_negocio_id='001')
        db.session.add(producto)
        db.session.flush()
        ubicacion = Ubicacion(codigo=f'CC-FORZ-{CODIGO_SIESA}', almacen_id=almacen.id,
                               tipo_zona='PICKING', stock_minimo=0, stock_maximo=99999,
                               secuencia_ruteo=1, activo=True)
        db.session.add(ubicacion)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                                          cantidad=real_antes, reservado=0, bloqueado=0))
        db.session.commit()

        # ── 0. Bonus: operario_id inexistente rechaza sin crear nada ────────
        print('\n--- Guard: operario_id inexistente ---')
        try:
            ConteoService.crear_conteo_manual(almacen.id, CODIGO_SIESA, operario_id=999999)
            _falla('crear_conteo_manual con operario_id inexistente NO lanzó ValueError')
        except ValueError as e:
            _ok(f'rechazado como se esperaba: "{e}"')
        assert SesionConteo.query.count() == 0, 'no debía haber creado ninguna sesión'

        # ── 1. Crear conteo manual FORZANDO el operario (la feature nueva) ──
        print('\n--- Crear conteo manual forzando CC1 a picker_a ---')
        creado = ConteoService.crear_conteo_manual(almacen.id, CODIGO_SIESA, operario_id=picker_a.id)
        assert creado['tareas_creadas'] == 1, creado
        assert creado['operario_id'] == picker_a.id, creado
        assert creado['operario_nombre'] == picker_a.nombre, creado
        _ok(f'sesión creada y pre-asignada a {creado["operario_nombre"]}')

        cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first()
        assert cc1.operario_id == picker_a.id
        assert cc1.estado == EstadoConteo.PENDIENTE, (
            f'debía quedar PENDIENTE-pero-asignado, salió {cc1.estado}')
        _ok(f'sesión #{cc1.id} en BD: operario_id={cc1.operario_id} estado={cc1.estado} '
            f'(PENDIENTE-pero-asignado, no EN_PROCESO todavía)')

        # ── 2. picker_b NO puede tomarla ni escanearla — ownership respeta la asignación forzada ──
        print('\n--- Guard: picker_b (no asignado) intenta tomar/escanear el CC1 forzado ---')
        try:
            ConteoService.obtener_tarea_operario(cc1.id, picker_b.id)
            _falla('picker_b pudo tomar una sesión forzada a otro operario')
        except ValueError as e:
            _ok(f'picker_b bloqueado en /tarea: "{e}"')

        try:
            MobileService.procesar_escaneo(operario_id=picker_b.id, tarea_id=cc1.id,
                                            tipo='CONTEO', codigo=CODIGO_SIESA, cantidad=1)
            _falla('picker_b pudo escanear una sesión forzada a otro operario')
        except ValueError as e:
            _ok(f'picker_b bloqueado en /escanear: "{e}"')

        # ── 3. picker_a SÍ puede tomarla y contarla ──────────────────────────
        print('\n--- picker_a toma y cuenta CC1 ---')
        ConteoService.obtener_tarea_operario(cc1.id, picker_a.id)
        cc1 = SesionConteo.query.get(cc1.id)
        assert cc1.estado == EstadoConteo.EN_PROCESO
        _ok(f'picker_a abrió la tarea -> estado={cc1.estado}')

        r_scan = MobileService.procesar_escaneo(operario_id=picker_a.id, tarea_id=cc1.id,
                                                 tipo='CONTEO', codigo=CODIGO_SIESA,
                                                 total_acumulado=cantidad_contada)
        assert r_scan['exito']
        r1 = MobileService.confirmar_tarea(operario_id=picker_a.id, tarea_id=cc1.id, tipo='CONTEO')
        print(f'  CC1 confirmado: {r1["resultado"]}')
        assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
        cc2_id = r1['segundo_conteo_id']
        _ok('CC1 discordante -> CC2 generado (esto no lo cambia la feature nueva, sigue automático)')

        # ── 4. CC2 (auto-asignado como siempre) cuenta lo MISMO -> auto-ajuste ──
        cc2 = SesionConteo.query.get(cc2_id)
        if not cc2.operario_id:
            ConteoService.obtener_tarea_operario(cc2_id, picker_b.id)
        cc2_operario = cc2.operario_id or picker_b.id
        MobileService.procesar_escaneo(operario_id=cc2_operario, tarea_id=cc2_id, tipo='CONTEO',
                                        codigo=CODIGO_SIESA, total_acumulado=cantidad_contada)
        r2 = MobileService.confirmar_tarea(operario_id=cc2_operario, tarea_id=cc2_id, tipo='CONTEO')
        print(f'  CC2 confirmado: {r2["resultado"]} — {r2["mensaje"]}')
        assert r2['resultado'] == 'DESCUADRE', r2
        assert r2.get('auto_encolado') is True, r2
        _ok(f'CC1==CC2 -> auto-ajuste encolado (auto_encolado={r2["auto_encolado"]})')

        if args.disparar_real:
            if not args.si_de_verdad:
                print('\n[BLOQUEADO] --disparar-real exige --si-de-verdad'); sys.exit(1)
            connekta.modo_ensayo = False
            print('\n*** MODO_ENSAYO APAGADO — el DLQ va a postear 142951 REAL a Siesa ***')

        print('\n--- Procesando DLQ (AJUSTE_CONTEO) ---')
        for vuelta in range(4):
            for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
                j.proximo_intento = None
            db.session.commit()
            n = procesar_jobs_pendientes()
            print(f'  vuelta {vuelta + 1}: {n} job(s) procesados')
            if n == 0:
                break

        cc1 = SesionConteo.query.get(cc1.id)
        job = SiesaJob.query.filter_by(referencia_tipo='SesionConteo', referencia_id=cc1.id,
                                        tipo='AJUSTE_CONTEO').order_by(SiesaJob.id.desc()).first()
        inv = UbicacionProducto.query.filter_by(ubicacion_id=ubicacion.id, producto_id=producto.id).first()

        print('\n=== RESULTADO FINAL ===')
        print(f'  Siesa ANTES: {real_antes}')
        print(f'  Conteo (CC1=CC2): {cantidad_contada}')
        print(f'  sesion CC1: estado={cc1.estado} motivo={cc1.motivo_codigo} '
              f'diferencia={cc1.diferencia} siesa_triggered={cc1.siesa_triggered}')
        print(f'  job: {job.tipo if job else None} -> {job.estado if job else None} '
              f'(error={job.error_ultimo if job else None})')
        print(f'  WMS local (UbicacionProducto.cantidad) DESPUES: {inv.cantidad if inv else None}')

        if args.disparar_real:
            real_despues = ConteoService.consultar_existencia_siesa(CODIGO_SIESA, bodega=BODEGA)
            print(f'  Siesa DESPUES (real, releido): {real_despues}')
            alineado = real_despues == cantidad_contada
            print(f'  alineado con el conteo: {alineado}')
            if not (cc1.estado == EstadoConteo.AJUSTADO and cc1.siesa_triggered and alineado):
                _falla('el POST real a Siesa no dejo el estado esperado — revisar arriba')
            _ok('Siesa QA quedo alineado con el conteo fisico (142951 real, codigo:0)')

        print('\n=== Feature de operario forzado verificada de punta a punta ===')


if __name__ == '__main__':
    main()
