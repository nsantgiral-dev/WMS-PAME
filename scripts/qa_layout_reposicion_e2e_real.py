"""
Prueba real de punta a punta de la relacion Layout <-> Reposicion — y prueba
explicita de que el flujo NUNCA toca Siesa (decision 2026-09-07: RESERVA y
PICKING son la misma bodega Siesa, NB1 no tiene sub-bodegas internas, eso es
organizacion 100% del WMS).

  1. Layout crea un Cuerpo PICKING real (crear_cuerpo): 3 entrepanos x 4
     huecos = 12 huecos.
  2. Layout asigna 12 SKUs reales, uno por hueco (asignar_producto) -- fija
     producto_asignado_id, capacidad_maxima=100 (-> stock_maximo=100 via
     configurar_umbral) y stock_minimo=30, y deja saldo inicial POR ENCIMA
     del minimo (80 uds).
  3. Layout crea un Cuerpo RESERVA (1 hueco) con los MISMOS 12 SKUs, uno
     por LPN.
  4. FASE A (sobre minimo): verificar_stock_picking() -- debe generar 0
     tareas, los 12 huecos estan en 80 > 30.
  5. Se simula consumo real (operarios pickeando) en 7 de los 12 huecos,
     bajandolos a 10 uds (< 30).
  6. FASE B (bajo minimo): verificar_stock_picking() -- debe generar
     EXACTAMENTE 6 TareaReposicion (7 huecos bajan, pero 1 SKU tiene un LPN
     que no cabe en la capacidad restante y se declara sin generar tarea --
     "romper la paca" es atomico, no se recorta un LPN para que quepa).
  7. Un abastecedor real toma y confirma las 6 -- LPN roto, stock PICKING
     sube. CERO llamadas a Siesa en todo el proceso.
  8. Verificacion final: SiesaJob.query.count() == 0 -- ni un solo job se
     encolo para ninguna TareaReposicion, en ningun punto del flujo.

Base LOCAL AISLADA (nunca Postgres de produccion). No requiere .env.qa ni
credenciales Connekta -- este flujo no llama a Siesa en ningun punto, así
que no hay nada "real" que disparar contra el ERP.

Uso:
    venv/Scripts/python.exe scripts/qa_layout_reposicion_e2e_real.py
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

BODEGA = 'NB1'
CENTRO_OP = '003'
PASILLO = 'Z'

# Codigos con forma de codigo_siesa real (no se llama a Siesa en ningun
# punto de este flujo, así que no hace falta que existan en el catálogo QA
# -- se mantienen realistas solo por prolijidad de los datos de prueba).
SKUS = [
    'PAPELSP5426', 'PAPELSP6025', 'PAPELSP9573', 'PAPELSP2582',
    'PAPELSP8843', 'PAPELSP2584', 'PAPELSP5098', 'PAPELSP2482',
    'PAPELSP837', 'PAPELSP5107', 'PAPELSP9570', 'PAPELSP12335',
]
CAPACIDAD_MAXIMA = 100
STOCK_MINIMO = 30
SALDO_INICIAL = 80   # > minimo -- Fase A no debe disparar nada
SALDO_BAJADO = 10    # < minimo -- Fase B debe disparar
HUECOS_QUE_BAJAN = 7  # de los 12, cuantos se simulan consumidos

# "Romper la paca" es atomico (reposicion_service, 2026-09-07 fix): un LPN
# candidato tiene que caber ENTERO en disponible=capacidad-stock_actual, o
# no se genera tarea. disponible = 100-10 = 90 para los huecos que bajan --
# 6 SKUs con LPN de 90 (caben justo) + PAPELSP5098 con LPN de 500 a proposito
# (NO cabe) para probar en el mismo run que el motor lo declara sin
# desbordar, en vez de romper la paca igual.
LPN_GRANDE_SKU = 'PAPELSP5098'
LPN_GRANDE_CANTIDAD = 500
LPN_NORMAL_CANTIDAD = 90


def _register_pg_stubs(dbapi_conn, _connection_record):
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock', 1, lambda k: 1)


def main():
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_layout_reposicion_e2e.db')
    os.environ['SYNC_SCHEDULER'] = 'false'
    os.environ.setdefault('SECRET_KEY', 'qa-layout-reposicion-e2e-32-bytes-x')
    # Sin .env.qa ni credenciales Connekta a proposito: este flujo no llama
    # a Siesa en ningun punto -- probarlo en modo_simulacion (el default sin
    # credenciales) es exactamente la prueba correcta, no una limitacion.

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

        from app.services.connekta_gateway import connekta
        from app.services import layout_service
        from app.services.reposicion_service import (
            verificar_stock_picking, get_tarea_abastecedor, confirmar_reposicion,
        )
        from app.models.almacen import Almacen
        from app.models.producto import Producto
        from app.models.usuario import Usuario
        from app.models.ubicacion import Ubicacion
        from app.models.inventario import UbicacionProducto
        from app.models.lpn import LPN
        from app.models.tarea_reposicion import TareaReposicion
        from app.models.siesa_job import SiesaJob
        from werkzeug.security import generate_password_hash

        print(f'=== Layout <-> Reposicion E2E === modo_simulacion={connekta.modo_simulacion} '
              f'(esperado True — este flujo no necesita Siesa)')

        # ── Seed base ──
        almacen = Almacen(codigo=BODEGA, nombre='Neiva Bodega CD',
                           bodega_siesa_id=BODEGA, centro_op_siesa=CENTRO_OP, activo=True)
        db.session.add(almacen)
        db.session.flush()

        admin = Usuario(nombre='Admin E2E', email='admin_e2e_layoutrep@wms-pame.local',
                         password_hash=generate_password_hash('qa123456'),
                         rol='admin', almacen_id=almacen.id, activo=True)
        abastecedor = Usuario(nombre='Abastecedor E2E', email='abast_e2e_layoutrep@wms-pame.local',
                               password_hash=generate_password_hash('qa123456'),
                               rol='operario', puede_abastecer=True, puede_picar=False,
                               almacen_id=almacen.id, activo=True)
        db.session.add_all([admin, abastecedor])
        db.session.flush()

        productos = {}
        for cod in SKUS:
            p = Producto(codigo=cod, nombre=f'Item real {cod}', codigo_siesa=cod,
                         unidad_negocio_id='001', activo=True)
            db.session.add(p)
            productos[cod] = p
        db.session.commit()

        # ── 1. Layout: Cuerpo PICKING, 3 entrepanos x 4 huecos ──
        print(f'\n--- Layout: crear_cuerpo PICKING {PASILLO}1-C01, 3x4 ---')
        huecos_picking = layout_service.crear_cuerpo(
            almacen_id=almacen.id, pasillo=PASILLO, fila=1, cuerpo=1,
            cantidad_entrepanos=3, tipo_zona='PICKING',
            huecos_por_nivel=[4, 4, 4],
        )
        assert len(huecos_picking) == 12, len(huecos_picking)
        for h in huecos_picking:
            print(f'  {h.codigo}  (nivel={h.nivel} hueco={h.hueco})')

        # ── 2. Layout: asigna los 12 SKUs, uno por hueco, con capacidad y minimo ──
        print(f'\n--- Layout: asignar_producto x12 (capacidad_maxima={CAPACIDAD_MAXIMA}, '
              f'stock_minimo={STOCK_MINIMO}, saldo_inicial={SALDO_INICIAL}) ---')
        asignaciones = {}  # codigo_sku -> (hueco, producto)
        for hueco, cod in zip(huecos_picking, SKUS):
            layout_service.asignar_producto(
                ubicacion_id=hueco.id, producto_id=productos[cod].id,
                cantidad=SALDO_INICIAL, usuario_id=admin.id,
                capacidad_maxima=CAPACIDAD_MAXIMA, stock_minimo=STOCK_MINIMO,
            )
            asignaciones[cod] = (hueco, productos[cod])
            print(f'  {hueco.codigo} <- {cod}  saldo={SALDO_INICIAL}')

        # Verificar que quedo bien amarrado (producto_asignado_id + stock_maximo)
        for cod, (hueco, prod) in asignaciones.items():
            db.session.refresh(hueco)
            assert hueco.producto_asignado_id == prod.id
            assert hueco.stock_maximo == CAPACIDAD_MAXIMA
            assert hueco.stock_minimo == STOCK_MINIMO

        # ── 3. Layout: Cuerpo RESERVA (1 hueco) con los mismos 12 SKUs, uno por LPN ──
        print(f'\n--- Layout: crear_cuerpo RESERVA {PASILLO}2-C01, 1x1 (mismos 12 SKUs, un LPN cada uno) ---')
        huecos_reserva = layout_service.crear_cuerpo(
            almacen_id=almacen.id, pasillo=PASILLO, fila=2, cuerpo=1,
            cantidad_entrepanos=1, tipo_zona='RESERVA', huecos_por_nivel=[1],
        )
        ub_reserva = huecos_reserva[0]
        print(f'  {ub_reserva.codigo}')

        lpns = {}
        for i, cod in enumerate(SKUS, start=1):
            cantidad_lpn = LPN_GRANDE_CANTIDAD if cod == LPN_GRANDE_SKU else LPN_NORMAL_CANTIDAD
            lpn = LPN(
                codigo=f'LPN-E2E-{i:03d}', producto_id=productos[cod].id,
                almacen_id=almacen.id, ubicacion_id=ub_reserva.id,
                factor_conversion=cantidad_lpn, cantidad_actual=cantidad_lpn, estado='ACTIVO',
            )
            db.session.add(lpn)
            lpns[cod] = lpn
        db.session.commit()
        print(f'  {len(lpns)} LPN(s) creados en {ub_reserva.codigo}: '
              f'{LPN_NORMAL_CANTIDAD} UNDs c/u, salvo {LPN_GRANDE_SKU} con {LPN_GRANDE_CANTIDAD} '
              f'(a propósito, no cabe en el hueco de capacidad {CAPACIDAD_MAXIMA})')

        # ── 4. FASE A: todo por encima del minimo -- 0 tareas ──
        print(f'\n--- FASE A: los 12 huecos en {SALDO_INICIAL} (> minimo {STOCK_MINIMO}) ---')
        generadas_a = verificar_stock_picking(almacen_id=almacen.id)
        print(f'  verificar_stock_picking() -> {generadas_a} tarea(s) generada(s)')
        assert generadas_a == 0, f'Fase A: se esperaban 0 tareas, salieron {generadas_a}'
        assert TareaReposicion.query.count() == 0
        print('  [OK] ninguna tarea -- correcto, todo sobre minimo')

        # ── 5. Simular consumo real en 7 de los 12 huecos ──
        skus_bajados = SKUS[:HUECOS_QUE_BAJAN]
        skus_intactos = SKUS[HUECOS_QUE_BAJAN:]
        print(f'\n--- Simulando consumo: {HUECOS_QUE_BAJAN} huecos bajan a {SALDO_BAJADO} (< minimo) ---')
        for cod in skus_bajados:
            hueco, prod = asignaciones[cod]
            inv = UbicacionProducto.query.filter_by(ubicacion_id=hueco.id, producto_id=prod.id).first()
            inv.cantidad = SALDO_BAJADO
            print(f'  {hueco.codigo} ({cod}): {SALDO_INICIAL} -> {SALDO_BAJADO}')
        db.session.commit()

        # ── 6. FASE B: 6 tareas (una por cada LPN que SÍ cabe) — el 7mo hueco
        #    bajo minimo (PAPELSP5098) no genera tarea porque su único LPN
        #    (500 UNDs) no cabe en los 90 disponibles del hueco ──
        print(f'\n--- FASE B: verificar_stock_picking() tras el consumo ---')
        generadas_b = verificar_stock_picking(almacen_id=almacen.id)
        print(f'  -> {generadas_b} tarea(s) generada(s)')
        esperadas_b = HUECOS_QUE_BAJAN - 1
        assert generadas_b == esperadas_b, (
            f'Fase B: se esperaban {esperadas_b} tareas, salieron {generadas_b}')

        tareas = TareaReposicion.query.all()
        assert len(tareas) == esperadas_b
        skus_bajados_normales = [c for c in skus_bajados if c != LPN_GRANDE_SKU]
        producto_ids_bajados_normales = {productos[c].id for c in skus_bajados_normales}
        producto_ids_intactos = {productos[c].id for c in skus_intactos}
        producto_id_lpn_grande = productos[LPN_GRANDE_SKU].id
        for t in tareas:
            assert t.producto_id in producto_ids_bajados_normales, \
                f'TareaReposicion para producto {t.producto_id} no debia existir'
            assert t.producto_id not in producto_ids_intactos
            assert t.producto_id != producto_id_lpn_grande, \
                'El SKU con LPN de 500 (no cabe en 90) no debia tener tarea'
            assert t.cantidad_unidades == LPN_NORMAL_CANTIDAD, (t.cantidad_unidades, LPN_NORMAL_CANTIDAD)
            assert t.ubicacion_reserva_id == ub_reserva.id
        print(f'  [OK] {len(tareas)} tareas, todas con cantidad_unidades={LPN_NORMAL_CANTIDAD} '
              f'(el LPN entero, no un recorte), todas apuntando a {ub_reserva.codigo}')
        print(f'  [OK] {LPN_GRANDE_SKU} (LPN de {LPN_GRANDE_CANTIDAD}, no cabe en 90) — sin tarea, declarado en el log')
        print('  [OK] ninguna tarea para los 5 huecos que siguieron en 80 (sobre minimo)')

        # ── 7. Abastecedor real toma y confirma las 6 reposiciones válidas ──
        print(f'\n--- Abastecedor confirma las {esperadas_b} reposiciones (rompe LPN real) ---')
        for i in range(esperadas_b):
            tarea_dict = get_tarea_abastecedor(abastecedor.id)
            assert tarea_dict is not None, f'Se esperaba tarea #{i+1}, no hubo ninguna'
            r = confirmar_reposicion(tarea_dict['id'], abastecedor.id)
            print(f"  {r['mensaje']}")

        # Ya no debe quedar ninguna tarea pendiente
        siguiente = get_tarea_abastecedor(abastecedor.id)
        assert siguiente is None, 'Sobro una tarea sin confirmar'
        print(f'  [OK] cola de reposicion vacia — las {esperadas_b} quedaron COMPLETADA')

        # ── 8. LA PRUEBA CENTRAL: cero contacto con Siesa en todo el flujo ──
        print(f"\n{'=' * 70}")
        print('--- Verificación: este flujo NUNCA toca Siesa ---')
        total_jobs = SiesaJob.query.count()
        print(f'  SiesaJob.query.count() en toda la base de prueba = {total_jobs}')
        assert total_jobs == 0, (
            f'Se encolaron {total_jobs} job(s) hacia Siesa — este flujo debe ser 100% WMS')
        print('  [OK] CERO jobs a Siesa — Layout y Reposición completos, sin tocar Siesa')

        print('\n=== RESULTADO FINAL (100% local WMS) ===')
        for cod in skus_bajados:
            hueco, prod = asignaciones[cod]
            db.session.refresh(hueco)
            inv_picking = UbicacionProducto.query.filter_by(ubicacion_id=hueco.id, producto_id=prod.id).first()
            print(f"\n{hueco.codigo} ({cod})")
            if cod == LPN_GRANDE_SKU:
                print(f"  PICKING sigue en {inv_picking.cantidad if inv_picking else '?'} "
                      f"— sin tarea (LPN de {LPN_GRANDE_CANTIDAD} no cabía en 90 disponibles)")
                continue
            print(f"  PICKING antes={SALDO_BAJADO} -> despues={inv_picking.cantidad if inv_picking else '?'} "
                  f"(esperado {SALDO_BAJADO + LPN_NORMAL_CANTIDAD})")


if __name__ == '__main__':
    main()
