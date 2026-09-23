"""
Conteo cíclico REAL contra Siesa QA en una bodega con POS pendiente: el ajuste
sale contra el TEÓRICO (`existencia − cant_pos`), no contra la existencia cruda.

Qué prueba, con los servicios reales (nada de filas insertadas a mano más allá
del seed mínimo de almacén / producto / ubicación / operarios):

  1. Lee la foto de Siesa (`ConteoService.consultar_foto_siesa`) y la muestra.
  2. Crea un conteo manual para el SKU en esa bodega.
  3. Registra CC1 y CC2 con el físico indicado (dos operarios distintos).
  4. Deja que el flujo encole el AJUSTE_CONTEO (CC1 == CC2 → automático) y
     corre ese job del DLQ, como los otros qa_conteo_*_real.py.
  5. Espera ~15 s (Regla 20) y relee Siesa.
  6. Verifica `existencia_después == existencia_antes + delta_esperado`, con
     `delta_esperado == físico − (existencia − pos)`, e imprime cuánto habría
     ajustado el código viejo (`físico − existencia`).
  7. Con --revertir, manda el ajuste inverso con la MISMA función del gateway
     (`enviar_ajuste_inventario`) y verifica que Siesa QA quedó como estaba.

Sin --si-de-verdad todo corre en MODO_ENSAYO (.env.qa lo trae en true): los GET
son reales, el POST lo bloquea el gateway y se imprime el payload que se habría
mandado. MODO_ENSAYO se apaga SOLO dentro de este proceso y SOLO con
--si-de-verdad; el archivo .env.qa no se toca.

Base: SQLite en un directorio temporal NUEVO por corrida, borrado al terminar.
**Nunca la Postgres de Railway**: `.env.qa` trae un DATABASE_URL de Postgres, y
`load_dotenv(override=True)` lo pondría encima de cualquier valor previo — por
eso acá la base se fija DESPUÉS de cargar el .env, y se verifica que sea sqlite
antes de crear la app.

Uso:
    venv/bin/python scripts/qa_conteo_pos_real.py --bodega NS1 --sku PAPELSP08 --delta-fisico -2
    venv/bin/python scripts/qa_conteo_pos_real.py --bodega NS1 --sku PAPELSP08 --fisico 150 \\
        --si-de-verdad --revertir
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

ESPERA_REGLA_20 = 15


def _register_pg_stubs(dbapi_conn, _connection_record):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


def _fmt_foto(f):
    if f is None:
        return 'SIN FOTO (Siesa no respondió o la fila no trae POS / salida sin confirmar)'
    return (f"existencia={f['existencia']:g}  pos={f['cant_pos']:g}  "
            f"salida_sin_conf={f['salida_sin_conf']:g}  "
            f"comprometida={f['comprometida'] if f['comprometida'] is not None else '—'}  "
            f"→ teórico={f['teorico']:g}")


def _movimiento(resultado):
    """El registro Movimientos del payload que el gateway armó (modo ensayo)."""
    try:
        return (resultado or {}).get('payload', {}).get('Movimientos', [{}])[0]
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--bodega', required=True)
    ap.add_argument('--sku', required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--fisico', type=int, help='cantidad física absoluta')
    g.add_argument('--delta-fisico', type=int,
                   help='físico relativo al teórico: físico = teórico + N')
    ap.add_argument('--si-de-verdad', action='store_true',
                    help='apaga MODO_ENSAYO en este proceso y postea 142951 REAL a Siesa QA')
    ap.add_argument('--revertir', action='store_true',
                    help='manda el ajuste inverso y verifica que Siesa QA quede como estaba')
    ap.add_argument('--env', default=os.path.join(REPO_ROOT, '.env.qa'),
                    help='archivo de credenciales de QA (por defecto <repo>/.env.qa)')
    args = ap.parse_args()
    bodega = args.bodega.strip().upper()
    sku = args.sku.strip().upper()

    if not os.path.exists(args.env):
        print(f'[ERROR] no existe {args.env}'); sys.exit(1)
    from dotenv import load_dotenv
    load_dotenv(args.env, override=True)

    tmpdir = tempfile.mkdtemp(prefix='qa_conteo_pos_')
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(tmpdir, 'qa_conteo_pos.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-conteo-pos-real-32-bytes-o-mas-xx')
    if not os.environ['DATABASE_URL'].startswith('sqlite:///'):
        print('[ERROR] la base no es sqlite local — abortando'); sys.exit(1)

    try:
        _correr(args, bodega, sku)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _correr(args, bodega, sku):
    from app import create_app
    from app.extensions import db
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        if not str(db.engine.url).startswith('sqlite'):
            print(f'[ERROR] la app abrió {db.engine.url.drivername}, no sqlite — abortando')
            sys.exit(1)
        from sqlalchemy import event as _sa_event
        _sa_event.listen(db.engine, 'connect', _register_pg_stubs)
        db.create_all()

        import app.services.siesa_job_service as _sjs
        _sjs.disparar_dlq_inmediato = lambda *a, **k: None
        from app.services.siesa_job_service import procesar_jobs_pendientes

        from app.services.connekta_gateway import connekta
        from app.services.conteo_service import ConteoService
        from app.services.bodegas import co_de_bodega
        from app.models.almacen import Almacen
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        from app.models.inventario import UbicacionProducto
        from app.models.usuario import Usuario
        from app.models.conteo import SesionConteo
        from app.models.siesa_job import SiesaJob
        from werkzeug.security import generate_password_hash

        print(f'=== Conteo con POS pendiente — real contra Siesa ===')
        print(f'  host={connekta.base_url}  modo_simulacion={connekta.modo_simulacion}  '
              f'modo_ensayo={connekta.modo_ensayo}')
        if connekta.modo_simulacion:
            print('[ERROR] sin credenciales Connekta'); sys.exit(1)
        if args.si_de_verdad and 'qa' not in connekta.base_url.lower():
            print(f'[ERROR] --si-de-verdad solo contra QA; el host es {connekta.base_url}')
            sys.exit(1)

        centro_op = co_de_bodega(bodega)
        if not centro_op:
            print(f'[ERROR] {bodega} no tiene CO en el maestro'); sys.exit(1)

        # ── 1. La foto ──────────────────────────────────────────────────────
        foto_antes = ConteoService.consultar_foto_siesa(sku, bodega=bodega)
        print(f'\n[1] Foto de Siesa ANTES ({sku} @ {bodega}, CO {centro_op}):')
        print(f'    {_fmt_foto(foto_antes)}')
        if foto_antes is None:
            sys.exit(1)
        if foto_antes['salida_sin_conf'] != foto_antes['cant_pos']:
            print('    ⚠ salida sin confirmar ≠ POS: el flujo debe NEGAR el ajuste '
                  '(salidas que no son de caja). Se sigue para verificarlo.')

        teorico = foto_antes['teorico']
        fisico = args.fisico if args.fisico is not None else int(round(teorico)) + args.delta_fisico
        delta_esperado = fisico - teorico
        delta_viejo = fisico - foto_antes['existencia']
        print(f'    físico a contar = {fisico}')
        print(f'    delta esperado  = físico − teórico     = {fisico} − {teorico:g} = {delta_esperado:+g}')
        print(f'    código viejo    = físico − existencia  = {fisico} − '
              f"{foto_antes['existencia']:g} = {delta_viejo:+g}")
        if fisico < 0:
            print('[ERROR] el físico no puede ser negativo'); sys.exit(1)
        if delta_esperado == 0:
            print('    el físico cuadra con el teórico: el conteo va a dar MATCH y no '
                  'habría ajuste. Elegí otro --fisico / --delta-fisico.')
            sys.exit(1)

        # ── Seed mínimo (como los otros qa_conteo_*_real.py) ────────────────
        almacen = Almacen(codigo=bodega, nombre=f'QA {bodega}', bodega_siesa_id=bodega,
                          centro_op_siesa=centro_op, activo=True)
        db.session.add(almacen)
        db.session.flush()
        producto = Producto(codigo=sku, nombre=sku, codigo_siesa=sku, unidad_negocio_id='001')
        db.session.add(producto)
        db.session.flush()
        ubic = Ubicacion(codigo=f'QA-POS-{sku}', almacen_id=almacen.id, tipo_zona='GENERAL',
                         stock_minimo=0, stock_maximo=999999, secuencia_ruteo=1, activo=True)
        db.session.add(ubic)
        db.session.flush()
        # El WMS arranca "en sincronía" con lo que debería haber en el estante.
        db.session.add(UbicacionProducto(ubicacion_id=ubic.id, producto_id=producto.id,
                                         cantidad=max(0, int(round(teorico))),
                                         reservado=0, bloqueado=0))

        def _actor(email, nombre):
            u = Usuario(nombre=nombre, email=email,
                        password_hash=generate_password_hash('qa123456'),
                        rol='operario', puede_picar=True, almacen_id=almacen.id, activo=True)
            db.session.add(u)
            db.session.flush()
            return u

        picker_a = _actor('pos_a_qa@wms-pame.local', 'Picker A POS QA')
        picker_b = _actor('pos_b_qa@wms-pame.local', 'Picker B POS QA')
        db.session.commit()

        # ── 2 y 3. Conteo, CC1 y CC2 por los servicios reales ────────────────
        creado = ConteoService.crear_conteo_manual(almacen.id, sku)
        cc1_id = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one().id
        ConteoService.obtener_tarea_operario(cc1_id, picker_a.id)
        r1 = ConteoService.registrar_conteo(cc1_id, picker_a.id, fisico)
        print(f'\n[2] Conteo {creado["codigos"][0]} creado')
        print(f'[3] CC1 = {fisico} → {r1["resultado"]}')
        if r1['resultado'] != 'SEGUNDO_CONTEO':
            print(f'[ERROR] se esperaba SEGUNDO_CONTEO: {r1}'); sys.exit(1)
        cc2_id = r1['segundo_conteo_id']
        cc2 = db.session.get(SesionConteo, cc2_id)
        operario_cc2 = cc2.operario_id or picker_b.id
        ConteoService.obtener_tarea_operario(cc2_id, operario_cc2)
        r2 = ConteoService.registrar_conteo(cc2_id, operario_cc2, fisico)
        print(f'    CC2 = {fisico} → {r2["resultado"]} — {r2["mensaje"]}')

        cc1 = db.session.get(SesionConteo, cc1_id)
        print(f'    raíz: estado={cc1.estado} diferencia={cc1.diferencia} '
              f'motivo={cc1.motivo_codigo} teórico_guardado={cc1.teorico_siesa} '
              f'pos_guardado={cc1.cant_pos_siesa} foto_at={cc1.foto_siesa_at}')
        if r2.get('ajuste_bloqueado'):
            print(f'\n[4] AJUSTE NO ENCOLADO — {r2["ajuste_bloqueado"]}')
            print('\n=== RESUMEN ===\n  El flujo negó el ajuste, como corresponde. Nada se '
                  'mandó a Siesa.')
            return
        if not r2.get('auto_encolado'):
            print(f'[ERROR] CC1 == CC2 no encoló el ajuste: {r2}'); sys.exit(1)

        job = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_tipo='SesionConteo',
                                       referencia_id=cc1_id).one()
        payload_job = json.loads(job.payload)
        print(f'\n[4] Job AJUSTE_CONTEO #{job.id}: {payload_job["motivo_codigo"]} '
              f'{payload_job["cantidad"]} und — bodega {payload_job["bodega"]} '
              f'CO {payload_job["centro_op"]}')
        signo = 1 if payload_job['motivo_codigo'] == 'AJ-ENT' else -1
        if signo * payload_job['cantidad'] != delta_esperado:
            print(f'[ERROR] el job no trae el delta esperado ({delta_esperado:+g})'); sys.exit(1)

        if args.si_de_verdad:
            connekta.modo_ensayo = False
            print('    *** MODO_ENSAYO APAGADO en este proceso — 142951 REAL a Siesa QA ***')
        else:
            print('    (modo ensayo: el gateway arma el payload y bloquea el POST)')

        for _ in range(3):
            for j in SiesaJob.query.filter_by(estado='PENDIENTE').all():
                j.proximo_intento = None
            db.session.commit()
            if procesar_jobs_pendientes() == 0:
                break
        db.session.refresh(job)
        cc1 = db.session.get(SesionConteo, cc1_id)
        resultado = json.loads(cc1.siesa_response) if cc1.siesa_response else None
        print(f'    job → {job.estado} (error={job.error_ultimo})  sesión → {cc1.estado} '
              f'siesa_triggered={cc1.siesa_triggered}')
        mov = _movimiento(resultado)
        if mov:
            print(f"    Movimientos (lo que {'SE MANDÓ' if args.si_de_verdad else 'se mandaría'}): "
                  f"bodega={mov.get('f470_id_bodega')} motivo={mov.get('f470_id_motivo')} "
                  f"cant_base={mov.get('f470_cant_base')} item={mov.get('f470_referencia_item')} "
                  f"co={mov.get('f470_id_co_movto')}")
        elif resultado is not None:
            print(f'    respuesta Siesa: {resultado}')

        if not args.si_de_verdad:
            if args.revertir:
                inv = 'AJ-SAL' if payload_job['motivo_codigo'] == 'AJ-ENT' else 'AJ-ENT'
                r_rev = connekta.enviar_ajuste_inventario(
                    motivo_codigo=inv, item_codigo=sku, cantidad=payload_job['cantidad'],
                    referencia=f'REV-{cc1.codigo}', bodega=bodega, centro_op=centro_op)
                mrev = _movimiento(r_rev)
                print(f"\n[7] Reverso (se mandaría): {inv} cant_base={mrev.get('f470_cant_base')} "
                      f"motivo={mrev.get('f470_id_motivo')} bodega={mrev.get('f470_id_bodega')}")
            print('\n=== RESUMEN (MODO ENSAYO — nada se escribió en Siesa) ===')
            print(f'  {sku} @ {bodega}: existencia {foto_antes["existencia"]:g}, POS '
                  f'{foto_antes["cant_pos"]:g} → teórico {teorico:g}; físico {fisico}')
            print(f'  ajuste que saldría: {payload_job["motivo_codigo"]} {payload_job["cantidad"]} '
                  f'(delta {delta_esperado:+g})')
            print(f'  el código viejo habría mandado: delta {delta_viejo:+g} '
                  f'({"AJ-ENT" if delta_viejo > 0 else "AJ-SAL"} {abs(delta_viejo):g})')
            print('  Para postear de verdad: agregar --si-de-verdad (y --revertir para dejar '
                  'Siesa QA como estaba).')
            return

        if job.estado != 'COMPLETADO':
            print('[ERROR] el ajuste no se completó en Siesa — ver error arriba'); sys.exit(1)

        # ── 5 y 6. Regla 20 y verificación ───────────────────────────────────
        print(f'\n[5] Esperando {ESPERA_REGLA_20} s (Regla 20)...')
        time.sleep(ESPERA_REGLA_20)
        foto_despues = ConteoService.consultar_foto_siesa(sku, bodega=bodega)
        print(f'[6] Foto DESPUÉS: {_fmt_foto(foto_despues)}')
        ok = False
        if foto_despues is not None:
            esperado = foto_antes['existencia'] + delta_esperado
            ok = foto_despues['existencia'] == esperado
            print(f'    existencia: {foto_antes["existencia"]:g} {delta_esperado:+g} = {esperado:g} '
                  f'esperado · {foto_despues["existencia"]:g} real → {"OK" if ok else "NO CUADRA"}')
            if foto_despues['cant_pos'] != foto_antes['cant_pos']:
                print('    ⚠ el POS pendiente cambió durante la prueba: hubo movimiento de '
                      'caja o acumulación — la comparación de existencia no es limpia.')

        rev_ok = None
        if args.revertir:
            inv = 'AJ-SAL' if payload_job['motivo_codigo'] == 'AJ-ENT' else 'AJ-ENT'
            print(f'\n[7] Reverso: {inv} {payload_job["cantidad"]} (misma función del gateway)')
            r_rev = connekta.enviar_ajuste_inventario(
                motivo_codigo=inv, item_codigo=sku, cantidad=payload_job['cantidad'],
                referencia=f'REV-{cc1.codigo}', bodega=bodega, centro_op=centro_op)
            print(f'    respuesta: {r_rev}')
            time.sleep(ESPERA_REGLA_20)
            foto_rev = ConteoService.consultar_foto_siesa(sku, bodega=bodega)
            print(f'    Foto tras el reverso: {_fmt_foto(foto_rev)}')
            rev_ok = foto_rev is not None and foto_rev['existencia'] == foto_antes['existencia']
            print(f'    existencia volvió a {foto_antes["existencia"]:g}: {"OK" if rev_ok else "NO"}')

        print('\n=== RESUMEN (REAL) ===')
        print(f'  {sku} @ {bodega}: existencia {foto_antes["existencia"]:g}, POS '
              f'{foto_antes["cant_pos"]:g} → teórico {teorico:g}; físico {fisico}')
        print(f'  ajuste enviado: {payload_job["motivo_codigo"]} {payload_job["cantidad"]} '
              f'(delta {delta_esperado:+g}); el código viejo habría mandado {delta_viejo:+g}')
        print(f'  Siesa después del ajuste cuadra: {"SÍ" if ok else "NO"}')
        if rev_ok is not None:
            print(f'  Siesa QA restaurada: {"SÍ" if rev_ok else "NO — revisar a mano"}')
        if not ok or rev_ok is False:
            sys.exit(2)


if __name__ == '__main__':
    main()
