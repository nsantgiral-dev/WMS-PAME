"""
Conteo ciclico REAL de punta a punta contra Siesa QA, dos escenarios:

  SKU 1 (PAPELSP6948, NB1): CC1 y CC2 coinciden y dan POR ENCIMA de la
    existencia real en Siesa -> sobrante -> AJ-ENT (142951, motivo 01) ->
    Siesa sube, WMS sube.
  SKU 2 (PAPELSP9218, NB1): CC1 y CC2 coinciden y dan POR DEBAJO de la
    existencia real en Siesa -> faltante -> AJ-SAL (142951, motivo 02) ->
    Siesa baja, WMS baja.

En ambos casos CC1==CC2 dispara el ajuste automatico (ConteoService.
registrar_conteo -> 'CC1 == CC2: verdad de bodega -> ajuste automatico sin
esperar admin'), sin intervencion de un supervisor.

Base LOCAL AISLADA (nunca la Postgres de produccion), Siesa QA real
(.env.qa). disparar_dlq_inmediato desactivado -- el DLQ se corre a mano con
procesar_jobs_pendientes() para controlar el momento exacto del POST real.

Uso:
    venv/Scripts/python.exe scripts/qa_conteo_ciclico_real.py
    venv/Scripts/python.exe scripts/qa_conteo_ciclico_real.py --disparar-real --si-de-verdad
"""
import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

DELTA = 10.0
BODEGA = 'NB1'
CENTRO_OP = '003'

ESCENARIOS = [
    {'nombre': 'SOBRANTE', 'codigo_siesa': 'PAPELSP6948',
     'nombre_item': 'MARCADOR SHARPIE 0TANK GRUESO ROJO', 'signo': +1},
    {'nombre': 'FALTANTE', 'codigo_siesa': 'PAPELSP9218',
     'nombre_item': 'RESMA DE PAPEL CARTA REPROGRAF', 'signo': -1},
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

        print(f'=== Conteo ciclico real === modo_simulacion={connekta.modo_simulacion} '
              f'modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales'); sys.exit(1)

        # ── Seed: 1 almacen, 2 productos reales, 1 ubicacion cada uno, 2 operarios pares ──
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

        picker_a = _actor('picker_a_conteo_qa@wms-pame.local', 'Picker A Conteo QA')
        picker_b = _actor('picker_b_conteo_qa@wms-pame.local', 'Picker B Conteo QA')
        db.session.commit()

        resultados = []
        for esc in ESCENARIOS:
            print(f"\n--- {esc['nombre']} ({esc['codigo_siesa']}) ---")

            # Existencia real en Siesa AHORA MISMO — base de la prueba.
            real_antes = ConteoService.consultar_existencia_siesa(
                esc['codigo_siesa'], bodega=BODEGA)
            if real_antes is None:
                print('[ERROR] Siesa no respondio la existencia — abortando este escenario')
                continue
            print(f'  existencia real en Siesa (antes): {real_antes}')

            cantidad_contada = real_antes + (esc['signo'] * DELTA)
            print(f'  CC1 = CC2 = {cantidad_contada} '
                  f'({"por encima" if esc["signo"] > 0 else "por debajo"} de Siesa)')

            producto = Producto(codigo=esc['codigo_siesa'], nombre=esc['nombre_item'],
                                 codigo_siesa=esc['codigo_siesa'], unidad_negocio_id='001')
            db.session.add(producto)
            db.session.flush()
            ubicacion = Ubicacion(codigo=f'CC-{esc["codigo_siesa"]}', almacen_id=almacen.id,
                                   tipo_zona='PICKING', stock_minimo=0, stock_maximo=99999,
                                   secuencia_ruteo=1, activo=True)
            db.session.add(ubicacion)
            db.session.flush()
            # WMS arranca "en sincronia" con Siesa — es la premisa real de un
            # conteo ciclico: se cuenta para VERIFICAR, no porque ya se sepa
            # que esta mal.
            inv = UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                                     cantidad=real_antes, reservado=0, bloqueado=0)
            db.session.add(inv)
            db.session.commit()

            # ── CC1 ──
            creado = ConteoService.crear_conteo_manual(almacen.id, esc['codigo_siesa'])
            cc1_id = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first().id
            ConteoService.obtener_tarea_operario(cc1_id, picker_a.id)
            r1 = ConteoService.registrar_conteo(cc1_id, picker_a.id, int(cantidad_contada))
            print(f'  CC1 registrado: {r1["resultado"]}')
            assert r1['resultado'] == 'SEGUNDO_CONTEO', r1

            # ── CC2 (double-blind, otro operario) ──
            cc2_id = r1['segundo_conteo_id']
            cc2 = SesionConteo.query.get(cc2_id)
            if not cc2.operario_id:
                ConteoService.obtener_tarea_operario(cc2_id, picker_b.id)
            r2 = ConteoService.registrar_conteo(cc2_id, cc2.operario_id or picker_b.id,
                                                 int(cantidad_contada))
            print(f'  CC2 registrado: {r2["resultado"]} — {r2["mensaje"]}')
            assert r2['resultado'] == 'DESCUADRE', r2

            cc1 = SesionConteo.query.get(cc1_id)
            print(f'  CC1 (raiz) tras CC1==CC2: estado={cc1.estado} '
                  f'motivo={cc1.motivo_codigo} diferencia={cc1.diferencia}')

            resultados.append({
                'escenario': esc['nombre'], 'codigo_siesa': esc['codigo_siesa'],
                'cc1_id': cc1_id, 'real_antes': real_antes,
                'cantidad_contada': cantidad_contada,
                'ubicacion_id': ubicacion.id, 'producto_id': producto.id,
            })

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

        print('\n=== RESULTADO FINAL ===')
        for r in resultados:
            cc1 = SesionConteo.query.get(r['cc1_id'])
            job = SiesaJob.query.filter_by(
                referencia_tipo='SesionConteo', referencia_id=r['cc1_id'],
                tipo='AJUSTE_CONTEO').order_by(SiesaJob.id.desc()).first()
            inv = UbicacionProducto.query.filter_by(
                ubicacion_id=r['ubicacion_id'], producto_id=r['producto_id']).first()
            real_despues = None
            if args.disparar_real:
                real_despues = ConteoService.consultar_existencia_siesa(
                    r['codigo_siesa'], bodega=BODEGA)

            print(f"\n{r['escenario']} — {r['codigo_siesa']}")
            print(f"  Siesa ANTES:   {r['real_antes']}")
            print(f"  Conteo (CC1=CC2): {r['cantidad_contada']}")
            print(f"  sesion estado: {cc1.estado} motivo={cc1.motivo_codigo} "
                  f"diferencia={cc1.diferencia} siesa_triggered={cc1.siesa_triggered}")
            print(f"  job: {job.tipo if job else None} -> "
                  f"{job.estado if job else None} (error={job.error_ultimo if job else None})")
            print(f"  WMS local (UbicacionProducto.cantidad) DESPUES: {inv.cantidad if inv else None}")
            if real_despues is not None:
                print(f"  Siesa DESPUES (real, releido): {real_despues}")
                print(f"  alineado con el conteo: "
                      f"{real_despues == r['cantidad_contada']}")


if __name__ == '__main__':
    main()
