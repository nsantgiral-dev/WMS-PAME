"""
E2E real contra Siesa QA de los dos comportamientos nuevos de
`ConteoService.crear_conteo_manual()` (2026-09-14):

  1. RECLAMO: si la ubicación ya tiene una sesión PENDIENTE sin dueño (ej.
     generada por el barrido DIARIO_ABC, nadie la abrió todavía), forzar un
     operario sobre ese mismo SKU ya no bloquea con "ya existe un conteo
     activo" — reclama esa misma sesión y se la asigna, sin duplicar.
  2. PAUSA: si el operario forzado ya tiene OTRO conteo cíclico EN_PROCESO
     (de un SKU distinto), ese se pausa (vuelve a PENDIENTE, sin dueño) para
     que el dispensador le entregue el conteo forzado en su próximo pedido
     de tarea — el picking/packing NUNCA se toca (fuera de alcance, no se
     ejercita acá porque ya lo cubre el test unitario).

Termina completando el conteo forzado (CC1 discordante a propósito, CC2
"verdad de bodega") hasta el ajuste real 142951 en Siesa QA — mismo patrón
que scripts/qa_conteo_ciclico_real.py y qa_conteo_manual_forzado_e2e_real.py.

Base LOCAL AISLADA (nunca Postgres de producción), Siesa QA real (.env.qa).

Uso:
    venv/Scripts/python.exe scripts/qa_conteo_manual_reclamo_pausa_e2e_real.py
    venv/Scripts/python.exe scripts/qa_conteo_manual_reclamo_pausa_e2e_real.py --disparar-real --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

DELTA = 5.0
BODEGA = 'NB1'
CENTRO_OP = '003'
CODIGO_FORZADO = 'PAPELSP6948'
NOMBRE_FORZADO = 'MARCADOR SHARPIE 0TANK GRUESO ROJO'
CODIGO_OTRO = 'PAPELSP9218'
NOMBRE_OTRO = 'PAPEL BOND CARTA RESMA'


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

    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_conteo_manual_reclamo_pausa_real.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-reclamo-pausa-32-bytes-x')
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

        print(f'=== Conteo manual: reclamo PENDIENTE + pausa EN_PROCESO — E2E real === '
              f'modo_simulacion={connekta.modo_simulacion} modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales Connekta — revisar .env.qa'); sys.exit(1)

        # ── Seed ──────────────────────────────────────────────────────────
        almacen = Almacen(codigo=BODEGA, nombre='Neiva Bodega CD',
                           bodega_siesa_id=BODEGA, centro_op_siesa=CENTRO_OP, activo=True)
        db.session.add(almacen)
        db.session.flush()

        carlos = Usuario(nombre='Carlos Reclamo Pausa QA', email='carlos_reclamo_pausa_qa@wms-pame.local',
                          password_hash=generate_password_hash('qa123456'),
                          rol='operario', puede_picar=True, almacen_id=almacen.id, activo=True)
        db.session.add(carlos)
        db.session.flush()

        # SKU forzado (el que vamos a reclamar/contar de punta a punta)
        real_antes = ConteoService.consultar_existencia_siesa(CODIGO_FORZADO, bodega=BODEGA)
        if real_antes is None:
            print('[ERROR] Siesa no respondio la existencia del SKU forzado — abortando'); sys.exit(1)
        print(f'\nExistencia real en Siesa de {CODIGO_FORZADO} (antes): {real_antes}')
        cantidad_contada = int(real_antes + DELTA)
        print(f'CC1 = CC2 = {cantidad_contada} (sobrante, por encima de Siesa)')

        producto_forzado = Producto(codigo=CODIGO_FORZADO, nombre=NOMBRE_FORZADO,
                                     codigo_siesa=CODIGO_FORZADO, unidad_negocio_id='001')
        db.session.add(producto_forzado)
        db.session.flush()
        ub_forzado = Ubicacion(codigo=f'CC-RECL-{CODIGO_FORZADO}', almacen_id=almacen.id,
                                tipo_zona='PICKING', stock_minimo=0, stock_maximo=99999,
                                secuencia_ruteo=1, activo=True)
        db.session.add(ub_forzado)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ub_forzado.id, producto_id=producto_forzado.id,
                                          cantidad=real_antes, reservado=0, bloqueado=0))

        # SKU "otro" — lo que Carlos ya va a tener EN_PROCESO antes de que lo forcemos
        producto_otro = Producto(codigo=CODIGO_OTRO, nombre=NOMBRE_OTRO,
                                  codigo_siesa=CODIGO_OTRO, unidad_negocio_id='001')
        db.session.add(producto_otro)
        db.session.flush()
        ub_otro = Ubicacion(codigo=f'CC-RECL-{CODIGO_OTRO}', almacen_id=almacen.id,
                             tipo_zona='PICKING', stock_minimo=0, stock_maximo=99999,
                             secuencia_ruteo=2, activo=True)
        db.session.add(ub_otro)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ub_otro.id, producto_id=producto_otro.id,
                                          cantidad=100, reservado=0, bloqueado=0))
        db.session.commit()

        # ── 1. Sesión PENDIENTE sin dueño para el SKU forzado — simula DIARIO_ABC ──
        print('\n--- Paso 1: sesión PENDIENTE sin dueño ya existente (simula DIARIO_ABC) ---')
        preexistente = ConteoService.crear_conteo_manual(almacen.id, CODIGO_FORZADO)
        assert preexistente['tareas_creadas'] == 1, preexistente
        assert preexistente['operario_id'] is None, preexistente
        cc1_id_original = SesionConteo.query.filter_by(codigo=preexistente['codigos'][0]).first().id
        _ok(f'sesión #{cc1_id_original} creada PENDIENTE, sin dueño')

        # ── 2. Carlos toma otro SKU y queda EN_PROCESO — el que debe pausarse ──
        print('\n--- Paso 2: Carlos ya está contando OTRO SKU (EN_PROCESO) ---')
        otro_creado = ConteoService.crear_conteo_manual(almacen.id, CODIGO_OTRO, operario_id=carlos.id)
        otro_id = SesionConteo.query.filter_by(codigo=otro_creado['codigos'][0]).first().id
        ConteoService.obtener_tarea_operario(otro_id, carlos.id)
        otro_sesion = SesionConteo.query.get(otro_id)
        assert otro_sesion.estado == EstadoConteo.EN_PROCESO, otro_sesion.estado
        _ok(f'Carlos abrió sesión #{otro_id} ({CODIGO_OTRO}) -> EN_PROCESO')

        # ── 3. Forzar el SKU original a Carlos: debe RECLAMAR (no duplicar) y PAUSAR el otro ──
        print(f'\n--- Paso 3: forzar {CODIGO_FORZADO} a Carlos (debe reclamar #{cc1_id_original} y pausar #{otro_id}) ---')
        forzado = ConteoService.crear_conteo_manual(almacen.id, CODIGO_FORZADO, operario_id=carlos.id)
        if forzado.get('tareas_nuevas', 0) != 0 or forzado.get('tareas_reclamadas') != 1:
            _falla(f'esperaba reclamo puro (0 nuevas, 1 reclamada), salió {forzado}')
        if forzado['codigos'] != [preexistente['codigos'][0]]:
            _falla(f'reclamó una sesión distinta a la preexistente: {forzado}')
        _ok(f'reclamada sin duplicar: {forzado["codigos"]} (tareas_reclamadas=1, tareas_nuevas=0)')

        cc1 = SesionConteo.query.get(cc1_id_original)
        assert cc1.operario_id == carlos.id, cc1.operario_id
        assert cc1.estado == EstadoConteo.PENDIENTE, cc1.estado
        _ok(f'sesión #{cc1.id} ahora asignada a Carlos, PENDIENTE-pero-asignada')

        otro_sesion = SesionConteo.query.get(otro_id)
        if not (otro_sesion.estado == EstadoConteo.PENDIENTE and otro_sesion.operario_id is None
                and otro_sesion.fecha_inicio is None):
            _falla(f'el conteo #{otro_id} NO quedó pausado como se esperaba: '
                   f'estado={otro_sesion.estado} operario_id={otro_sesion.operario_id} '
                   f'fecha_inicio={otro_sesion.fecha_inicio}')
        _ok(f'sesión #{otro_id} ({CODIGO_OTRO}) pausada: PENDIENTE, sin dueño, fecha_inicio=None')

        total_sesiones_forzado = SesionConteo.query.filter_by(
            producto_id=producto_forzado.id, ubicacion_id=ub_forzado.id).count()
        if total_sesiones_forzado != 1:
            _falla(f'se duplicó la sesión del SKU forzado: {total_sesiones_forzado} filas')
        _ok('no se duplicó ninguna sesión para el SKU forzado')

        # ── 4. El dispensador le entrega a Carlos el conteo forzado, no el pausado ──
        print('\n--- Paso 4: get_tarea_actual(Carlos) debe devolver el conteo FORZADO ---')
        tarea_actual = MobileService.get_tarea_actual(carlos.id)
        if not tarea_actual or tarea_actual.get('id') != cc1.id:
            _falla(f'el dispensador no devolvió el conteo forzado #{cc1.id}, devolvió: {tarea_actual}')
        _ok(f'dispensador devolvió sesión #{tarea_actual["id"]} ({tarea_actual.get("producto_codigo")}) — correcto')

        cc1 = SesionConteo.query.get(cc1.id)
        assert cc1.estado == EstadoConteo.EN_PROCESO, cc1.estado
        _ok('la sesión forzada pasó a EN_PROCESO al pedirla por el dispensador')

        # ── 5. Contar CC1 discordante -> CC2 "verdad de bodega" -> auto-ajuste ──
        print('\n--- Paso 5: Carlos cuenta CC1 (discordante a propósito) ---')
        r_scan = MobileService.fijar_total_conteo(carlos.id, cc1.id, cantidad_contada)
        assert r_scan['exito']
        r1 = MobileService.confirmar_tarea(operario_id=carlos.id, tarea_id=cc1.id, tipo='CONTEO',
                                           total_contado=cantidad_contada)
        print(f'  CC1 confirmado: {r1["resultado"]}')
        assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
        cc2_id = r1['segundo_conteo_id']
        _ok('CC1 discordante -> CC2 generado (flujo estándar, sin cambios)')

        cc2 = SesionConteo.query.get(cc2_id)
        cc2_operario = cc2.operario_id
        if not cc2_operario:
            otro_picker = Usuario(nombre='Picker B Verificacion QA', email='picker_b_verif_qa@wms-pame.local',
                                   password_hash=generate_password_hash('qa123456'),
                                   rol='operario', puede_picar=True, almacen_id=almacen.id, activo=True)
            db.session.add(otro_picker)
            db.session.commit()
            ConteoService.obtener_tarea_operario(cc2_id, otro_picker.id)
            cc2_operario = otro_picker.id
        MobileService.fijar_total_conteo(cc2_operario, cc2_id, cantidad_contada)
        r2 = MobileService.confirmar_tarea(operario_id=cc2_operario, tarea_id=cc2_id, tipo='CONTEO',
                                           total_contado=cantidad_contada)
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
        inv = UbicacionProducto.query.filter_by(ubicacion_id=ub_forzado.id, producto_id=producto_forzado.id).first()

        print('\n=== RESULTADO FINAL ===')
        print(f'  Siesa ANTES: {real_antes}')
        print(f'  Conteo (CC1=CC2): {cantidad_contada}')
        print(f'  sesion CC1: estado={cc1.estado} motivo={cc1.motivo_codigo} '
              f'diferencia={cc1.diferencia} siesa_triggered={cc1.siesa_triggered}')
        print(f'  job: {job.tipo if job else None} -> {job.estado if job else None} '
              f'(error={job.error_ultimo if job else None})')
        print(f'  WMS local (UbicacionProducto.cantidad) DESPUES: {inv.cantidad if inv else None}')

        if args.disparar_real:
            real_despues = ConteoService.consultar_existencia_siesa(CODIGO_FORZADO, bodega=BODEGA)
            print(f'  Siesa DESPUES (real, releido): {real_despues}')
            alineado = real_despues == cantidad_contada
            print(f'  alineado con el conteo: {alineado}')
            if not (cc1.estado == EstadoConteo.AJUSTADO and cc1.siesa_triggered and alineado):
                _falla('el POST real a Siesa no dejo el estado esperado — revisar arriba')
            _ok('Siesa QA quedo alineado con el conteo fisico (142951 real, codigo:0)')

        print('\n=== Reclamo de PENDIENTE + pausa de EN_PROCESO verificados de punta a punta ===')


if __name__ == '__main__':
    main()
