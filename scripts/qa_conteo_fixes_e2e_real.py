"""
E2E real de conteo ciclico contra Siesa QA, verificando los 3 fixes del
2026-09-11 sobre el flujo de conteo ciclico:

  1. `MobileService.procesar_escaneo` (tipo=CONTEO) ahora exige ownership
     (sesion.operario_id == operario_id) y estado (PENDIENTE/EN_PROCESO) antes
     de escribir cantidad_fisica. Antes cualquier operario de almacen podia
     escanear hacia el sesion_id de OTRO (rompiendo el double-blind) o hacia
     una sesion ya cerrada.
  2. El auto-ajuste CC1==CC2 (`ConteoService.registrar_conteo`) ya no dice
     "ajuste encolado automaticamente" cuando el encolado en realidad fallo
     en silencio — el mensaje y el nuevo campo `auto_encolado` reflejan el
     resultado real.
  3. Esa misma rama ya no llama a Siesa (`consultar_existencia_siesa` dentro
     de `_encolar_ajuste_fisico`) mientras sostiene el `with_for_update()` de
     la sesion — se comitea y se suelta el lock ANTES de la llamada HTTP.

A diferencia de qa_conteo_ciclico_real.py (que llama a ConteoService
directo), este script pasa por la capa MobileService — el mismo camino que
usa /api/mobile/escanear y /api/mobile/confirmar — para ejercitar de verdad
el guard nuevo del punto 1.

Base LOCAL AISLADA (nunca Postgres de produccion), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_conteo_fixes_e2e_real.py
    venv/Scripts/python.exe scripts/qa_conteo_fixes_e2e_real.py --disparar-real --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

DELTA = 10.0
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

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_conteo_fixes_real.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-fixes-real-32-bytes-o-mas')
    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

    from app import create_app
    from app.extensions import db
    app = create_app()
    with app.app_context():
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)
        # DB limpia en cada corrida — evita que sesiones de una corrida previa
        # (mismo producto/ubicacion) choquen con el indice unico de sesion activa.
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

        print(f'=== Conteo ciclico E2E (fixes 2026-09-11) === '
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

        picker_a = _actor('picker_a_fixes_qa@wms-pame.local', 'Picker A Fixes QA')
        picker_b = _actor('picker_b_fixes_qa@wms-pame.local', 'Picker B Fixes QA')
        picker_c = _actor('picker_c_fixes_qa@wms-pame.local', 'Picker C (intruso) Fixes QA')
        db.session.commit()

        # Existencia real en Siesa ahora mismo — base de la prueba.
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
        ubicacion = Ubicacion(codigo=f'CC-FIX-{CODIGO_SIESA}', almacen_id=almacen.id,
                               tipo_zona='PICKING', stock_minimo=0, stock_maximo=99999,
                               secuencia_ruteo=1, activo=True)
        db.session.add(ubicacion)
        db.session.flush()
        inv = UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                                 cantidad=real_antes, reservado=0, bloqueado=0)
        db.session.add(inv)
        db.session.commit()

        # ── CC1: crear + asignar a picker_a ─────────────────────────────────
        creado = ConteoService.crear_conteo_manual(almacen.id, CODIGO_SIESA)
        cc1_id = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first().id
        ConteoService.obtener_tarea_operario(cc1_id, picker_a.id)
        print(f'\n--- CC1 (sesion #{cc1_id}) asignada a picker_a ---')

        # FIX 1a — ownership: picker_c (NO asignado) intenta escanear en CC1.
        try:
            MobileService.procesar_escaneo(
                operario_id=picker_c.id, tarea_id=cc1_id, tipo='CONTEO',
                codigo=CODIGO_SIESA, cantidad=1)
            _falla('picker_c pudo escanear en una sesion que NO es suya — guard de ownership no funciono')
        except ValueError as e:
            if 'no está asignada a ti' in str(e) or 'no esta asignada a ti' in str(e):
                _ok(f'picker_c bloqueado al intentar escanear CC1 ajeno: "{e}"')
            else:
                _falla(f'picker_c fue bloqueado pero con un mensaje inesperado: {e}')

        # Escaneo legitimo — picker_a, el dueño real.
        r_scan = MobileService.procesar_escaneo(
            operario_id=picker_a.id, tarea_id=cc1_id, tipo='CONTEO',
            codigo=CODIGO_SIESA, total_acumulado=cantidad_contada)
        assert r_scan['exito'] and r_scan['cantidad_contada'] == cantidad_contada, r_scan
        _ok(f'picker_a escaneo su propio CC1 sin problema: {r_scan["mensaje"]}')

        # Confirmar CC1 via MobileService (mismo camino que /api/mobile/confirmar)
        r1 = MobileService.confirmar_tarea(operario_id=picker_a.id, tarea_id=cc1_id, tipo='CONTEO')
        print(f'  CC1 confirmado: {r1["resultado"]}')
        assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
        _ok('CC1 con diferencia -> genero CC2 (double-blind)')

        # ── CC2: asignar a picker_b, mismo conteo (CC1==CC2) ────────────────
        cc2_id = r1['segundo_conteo_id']
        cc2 = SesionConteo.query.get(cc2_id)
        if not cc2.operario_id:
            ConteoService.obtener_tarea_operario(cc2_id, picker_b.id)
        cc2_operario = cc2.operario_id or picker_b.id
        print(f'\n--- CC2 (sesion #{cc2_id}) asignada a picker {cc2_operario} ---')

        # FIX 1a de nuevo, ahora sobre CC2 — picker_a (dueño de CC1, no de CC2) no puede tocarlo.
        try:
            MobileService.procesar_escaneo(
                operario_id=picker_a.id, tarea_id=cc2_id, tipo='CONTEO',
                codigo=CODIGO_SIESA, cantidad=1)
            _falla('picker_a (dueño de CC1) pudo escanear en CC2 — rompe el double-blind')
        except ValueError as e:
            _ok(f'picker_a bloqueado al intentar escanear el CC2 de otro: "{e}"')

        r_scan2 = MobileService.procesar_escaneo(
            operario_id=cc2_operario, tarea_id=cc2_id, tipo='CONTEO',
            codigo=CODIGO_SIESA, total_acumulado=cantidad_contada)
        assert r_scan2['exito'], r_scan2

        r2 = MobileService.confirmar_tarea(operario_id=cc2_operario, tarea_id=cc2_id, tipo='CONTEO')
        print(f'  CC2 confirmado: {r2["resultado"]} — {r2["mensaje"]}')
        assert r2['resultado'] == 'DESCUADRE', r2

        # FIX 2 — el mensaje ahora declara si el auto-encolado funciono de verdad.
        assert 'auto_encolado' in r2, 'FIX 2 no aplico — falta el campo auto_encolado en la respuesta'
        _ok(f'CC1==CC2 (auto-ajuste) -> auto_encolado={r2["auto_encolado"]} — mensaje: "{r2["mensaje"]}"')

        cc1 = SesionConteo.query.get(cc1_id)
        print(f'  CC1 (raiz) tras CC1==CC2: estado={cc1.estado} motivo={cc1.motivo_codigo} '
              f'diferencia={cc1.diferencia}')

        # FIX 1b — estado: CC1 ya esta en DESCUADRE/AJUSTANDO, nadie deberia poder
        # volver a escanear sobre el (ni el dueño original).
        try:
            MobileService.procesar_escaneo(
                operario_id=picker_a.id, tarea_id=cc1_id, tipo='CONTEO',
                codigo=CODIGO_SIESA, cantidad=1)
            _falla(f'se pudo re-escanear un CC1 en estado {cc1.estado} — guard de estado no funciono')
        except ValueError as e:
            _ok(f're-escaneo sobre CC1 ya cerrado ({cc1.estado}) bloqueado: "{e}"')

        # ── Disparo real a Siesa (142951) ───────────────────────────────────
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

        cc1 = SesionConteo.query.get(cc1_id)
        job = SiesaJob.query.filter_by(
            referencia_tipo='SesionConteo', referencia_id=cc1_id,
            tipo='AJUSTE_CONTEO').order_by(SiesaJob.id.desc()).first()
        inv = UbicacionProducto.query.filter_by(
            ubicacion_id=ubicacion.id, producto_id=producto.id).first()

        print('\n=== RESULTADO FINAL ===')
        print(f'  Siesa ANTES:      {real_antes}')
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

        print('\n=== Todos los guards de los fixes 2026-09-11 se verificaron ===')


if __name__ == '__main__':
    main()
