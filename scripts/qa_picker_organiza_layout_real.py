# -*- coding: utf-8 -*-
"""
Flujo completo REAL, de punta a punta, todo manejado por un PICKER (no admin):

  1. Un operario con puede_organizar_layout=True + puede_abastecer=True entra
     con su propia sesion (no admin) y usa el boton "Layout" de su HUD.
  2. Crea un Cuerpo PICKING el mismo -- 4 entrepanos x 4 huecos = 16 huecos --
     a traves del wizard real (paso 1 + paso 2), clic por clic.
  3. Asigna un SKU distinto a cada uno de los 16 huecos, entrepano por
     entrepano, con capacidad_maxima=100 y stock_minimo=30, via el modal real
     "Asignar SKU" (batch por entrepano).
  4. Se le baja el stock a 6 de los 16 huecos por debajo del minimo (consumo
     simulado) y se dispara el motor real de Reposicion.
  5. Se ve la alarma real en el modulo Reposicion (vista admin).
  6. El mismo picker, de vuelta en su propia cola (mismo puede_abastecer),
     recibe las tareas de reposicion y las confirma una por una desde su HUD.
  7. Se verifica el estado real del backend al final.

Todo contra la app real (Flask dev server) + sqlite descartable + Playwright.
RESERVA se siembra por backend (no es el foco de esta prueba) -- PICKING es
lo que se crea 100% a traves de la UI real del picker.
"""
import os
import sys
import threading
import time

REPO_ROOT = r"C:\Users\SSJUAN03\Desktop\WMS-PAME"
sys.path.insert(0, REPO_ROOT)

SCRATCH = os.path.join(REPO_ROOT, 'scripts')
DB_PATH = os.path.join(SCRATCH, 'qa_picker_layout.db')
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

SHOTS = os.path.join(
    r"C:\Users\SSJUAN03\AppData\Local\Temp\claude\C--Users-SSJUAN03\c3294fab-aaa0-45f5-8d88-4c459de3ba4b\scratchpad"
)
os.makedirs(SHOTS, exist_ok=True)

os.environ['DATABASE_URL'] = 'sqlite:///' + DB_PATH
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-picker-layout-32-bytes-o-mas-xxxx')

PORT = 5963
ADMIN_EMAIL = 'admin_qa@wms-pame.local'
ADMIN_PASS = 'qa12345678'
PICKER_EMAIL = 'picker_layout_qa@wms-pame.local'
PICKER_PASS = 'qa12345678'

CAPACIDAD_MAXIMA = 100
STOCK_MINIMO = 30
STOCK_INICIAL = 80
STOCK_BAJADO = 10          # bajo el minimo de 30 -> dispara alarma
LPN_CANTIDAD = 90

ENTREPANOS = 4
HUECOS_POR_ENTREPANO = 4
TOTAL_HUECOS = ENTREPANOS * HUECOS_POR_ENTREPANO  # 16

# 6 huecos a los que se les baja el stock -- todo entrepano 1 (4) + dos del 3.
HUECOS_A_BAJAR = {(1, 1), (1, 2), (1, 3), (1, 4), (3, 1), (3, 2)}

from app import create_app
from app.extensions import db

app = create_app()
with app.app_context():
    db.create_all()

    from app.models.almacen import Almacen
    from app.models.usuario import Usuario
    from app.models.producto import Producto
    from app.models.inventario import UbicacionProducto
    from app.models.lpn import LPN
    from app.models.tarea_reposicion import TareaReposicion
    from app.services import layout_service, reposicion_service
    from werkzeug.security import generate_password_hash

    # SQLite no tiene pg_advisory_xact_lock -- bypass solo para este script.
    @classmethod
    def _generar_codigo_sqlite(cls):
        ultimo = db.session.query(db.func.max(cls.id)).scalar() or 0
        return f'REP-{(ultimo + 1):07d}'
    TareaReposicion.generar_codigo = _generar_codigo_sqlite

    print('=' * 78)
    print('ETAPA 0 -- Preparar almacen, usuarios y catalogo de SKU')
    print('=' * 78)

    almacen = Almacen(codigo='NB1', nombre='Neiva Bodega CD',
                       bodega_siesa_id='NB1', centro_op_siesa='003', activo=True)
    db.session.add(almacen)
    db.session.flush()

    admin = Usuario(nombre='Admin QA', email=ADMIN_EMAIL,
                     password_hash=generate_password_hash(ADMIN_PASS),
                     rol='admin', almacen_id=almacen.id, activo=True)
    # El picker: rol operario normal, con DOS flags -- puede_organizar_layout
    # (crear ubicaciones/asignar SKU) y puede_abastecer (para que la misma
    # persona reciba y confirme la reposicion que su propio trabajo disparo).
    # Habilitar estos flags es justo lo que un admin hace una vez desde
    # Admin -> Operarios -- se siembra directo por ser el paso de "setup",
    # no el flujo que se esta demostrando.
    picker = Usuario(nombre='Picker Layout QA', email=PICKER_EMAIL,
                      password_hash=generate_password_hash(PICKER_PASS),
                      rol='operario', almacen_id=almacen.id, activo=True,
                      puede_picar=True, puede_organizar_layout=True, puede_abastecer=True)
    db.session.add_all([admin, picker])
    db.session.flush()
    print(f'  Picker habilitado: {picker.email} (puede_organizar_layout=True, puede_abastecer=True)')

    # Diccionarios planos (codigo/id), no objetos ORM -- la sesion que los creo
    # se cierra al salir de este `with app.app_context()` y el hilo del
    # servidor Flask usa su propia sesion; un objeto ORM detached revienta al
    # leer un atributo perezoso mas tarde.
    productos = {}
    for nivel in range(1, ENTREPANOS + 1):
        for hueco in range(1, HUECOS_POR_ENTREPANO + 1):
            codigo = f'PAPELSP9{nivel}{hueco}9'
            p = Producto(codigo=codigo, nombre=f'RESMA ENTREPANO {nivel} HUECO {hueco}',
                         codigo_siesa=codigo, unidad_negocio_id='001', activo=True)
            db.session.add(p)
            db.session.flush()
            productos[(nivel, hueco)] = {'id': p.id, 'codigo': p.codigo}
    db.session.commit()
    print(f'  {len(productos)} SKU de catalogo listos para asignar (16 distintos, uno por hueco)')

    print()
    print('=' * 78)
    print('ETAPA 0b -- Sembrar RESERVA con LPN por SKU (plomeria, no es el foco)')
    print('=' * 78)
    ub_reserva_por_sku = {}
    for idx, ((nivel, hueco), p) in enumerate(productos.items(), start=1):
        ub_r = layout_service.crear_cuerpo(
            almacen_id=almacen.id, pasillo='Z', fila=2, cuerpo=idx,
            cantidad_entrepanos=1, tipo_zona='RESERVA', huecos_por_nivel=[1],
        )[0]
        db.session.add(UbicacionProducto(ubicacion_id=ub_r.id, producto_id=p['id'],
                                          cantidad=LPN_CANTIDAD, reservado=0, bloqueado=0))
        db.session.flush()
        db.session.add(LPN(codigo=f'LPN-{p["codigo"]}', producto_id=p['id'],
                            ubicacion_id=ub_r.id, almacen_id=almacen.id,
                            cantidad_actual=LPN_CANTIDAD, estado='ACTIVO', factor_conversion=1))
        ub_reserva_por_sku[(nivel, hueco)] = ub_r
    db.session.commit()
    print(f'  RESERVA: {len(productos)} huecos con LPN de {LPN_CANTIDAD} uds cada uno')

    # Enteros planos -- el objeto ORM `almacen` no sobrevive a que este `with
    # app.app_context()` termine (mismo motivo que los productos, arriba).
    ALMACEN_ID = almacen.id

print()
print(f'Levantando servidor Flask real en :{PORT}...')


def _run():
    app.run(host='127.0.0.1', port=PORT, debug=False, use_reloader=False)


t = threading.Thread(target=_run, daemon=True)
t.start()

import urllib.request
for _ in range(30):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{PORT}/api/health/ping', timeout=1)
        break
    except Exception:
        time.sleep(0.5)
print('Servidor arriba')

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 460, 'height': 900})

    # ── Login como PICKER -- no como admin ───────────────────────────────────
    page.goto(f'http://127.0.0.1:{PORT}/pwa')
    page.wait_for_selector('#login-email', timeout=10000)
    page.fill('#login-email', PICKER_EMAIL)
    page.fill('#login-password', PICKER_PASS)
    page.click('#btn-login')
    page.wait_for_selector('#pantalla-operario', state='visible', timeout=10000)
    page.wait_for_timeout(400)

    print()
    print('=' * 78)
    print('ETAPA 1 -- HUD del Picker: sin tareas pendientes, boton Layout visible')
    print('=' * 78)
    page.screenshot(path=os.path.join(SHOTS, 'p1_picker_hud_inicio.png'))
    print('  -> p1_picker_hud_inicio.png')

    # ── Entra a Layout desde SU propio HUD, no desde admin ───────────────────
    page.click('#btn-layout-operario')
    page.wait_for_selector('#tab-layout', state='visible', timeout=10000)
    page.wait_for_timeout(300)

    print()
    print('=' * 78)
    print('ETAPA 2 -- Picker dentro de Layout (vista restringida, sin Importar Excel)')
    print('=' * 78)
    page.screenshot(path=os.path.join(SHOTS, 'p2_picker_en_layout.png'))
    print('  -> p2_picker_en_layout.png')

    # ── Crea el Cuerpo PICKING: 4 entrepanos x 4 huecos ──────────────────────
    page.click("button:has-text('+ Picking')")
    page.wait_for_selector('#modal-layout-cuerpo', state='visible', timeout=5000)
    page.select_option('#layout-cuerpo-pasillo', 'A')
    page.select_option('#layout-cuerpo-fila', '1')
    page.fill('#layout-cuerpo-numero', '1')
    page.fill('#layout-cuerpo-entrepanos', str(ENTREPANOS))
    page.click('#layout-cuerpo-btn-siguiente')
    page.wait_for_selector('#layout-cuerpo-paso2', state='visible', timeout=5000)
    for nivel in range(1, ENTREPANOS + 1):
        page.fill(f'#layout-cuerpo-hueco-nivel-{nivel}', str(HUECOS_POR_ENTREPANO))

    print()
    print('=' * 78)
    print('ETAPA 3 -- Picker crea el cuerpo: pasillo A, cuerpo 1, 4 entrepanos x 4 huecos')
    print('=' * 78)
    page.screenshot(path=os.path.join(SHOTS, 'p3_picker_creando_cuerpo.png'))
    print('  -> p3_picker_creando_cuerpo.png')

    page.click('#layout-cuerpo-paso2 button:has-text("Crear")')
    page.wait_for_timeout(600)

    # IDs reales de los 16 huecos recien creados, agrupados por nivel.
    huecos_creados = page.evaluate(
        "_layoutUbicacionesCache.filter(u => u.pasillo==='A' && u.fila===1 && u.cuerpo===1)"
        ".sort((a,b) => a.nivel - b.nivel || a.hueco - b.hueco)"
        ".map(u => ({id:u.id, nivel:u.nivel, hueco:u.hueco, codigo:u.codigo}))"
    )
    assert len(huecos_creados) == TOTAL_HUECOS, f'se esperaban {TOTAL_HUECOS} huecos, llegaron {len(huecos_creados)}'
    print(f'  {len(huecos_creados)} huecos creados por el picker: {huecos_creados[0]["codigo"]} .. {huecos_creados[-1]["codigo"]}')

    # ── Abre el detalle del cuerpo y asigna SKU entrepano por entrepano ──────
    page.evaluate("layoutAbrirModalCuerpoDetalle('A', 1, 1)")
    page.wait_for_selector('#modal-layout-cuerpo-detalle', state='visible', timeout=5000)
    page.wait_for_timeout(300)

    print()
    print('=' * 78)
    print('ETAPA 4 -- Picker asigna SKU a cada hueco, entrepano por entrepano')
    print('=' * 78)
    for nivel in range(1, ENTREPANOS + 1):
        page.locator('#layout-cuerpo-detalle-body button:has-text("Asignar SKU")').nth(nivel - 1).click()
        page.wait_for_selector('#modal-layout-asignar-entrepano', state='visible', timeout=5000)
        huecos_nivel = [h for h in huecos_creados if h['nivel'] == nivel]
        for h in huecos_nivel:
            hid = h['id']
            producto = productos[(nivel, h['hueco'])]
            codigo_inp = page.locator(f'#layout-asignar-ent-codigo-{hid}')
            codigo_inp.fill(producto['codigo'])
            codigo_inp.press('Enter')
            page.wait_for_timeout(120)
            page.fill(f'#layout-asignar-ent-cantidad-{hid}', str(STOCK_INICIAL))
            page.fill(f'#layout-asignar-ent-capacidad-{hid}', str(CAPACIDAD_MAXIMA))
            page.fill(f'#layout-asignar-ent-minimo-{hid}', str(STOCK_MINIMO))
        if nivel == 1:
            page.screenshot(path=os.path.join(SHOTS, 'p4_picker_asignando_sku_entrepano1.png'))
            print('  -> p4_picker_asignando_sku_entrepano1.png (entrepano 1, 4 filas listas para confirmar)')
        page.click('#modal-layout-asignar-entrepano button:has-text("Confirmar")')
        page.wait_for_timeout(400)
        print(f'  Entrepano {nivel}: {len(huecos_nivel)} SKU asignados '
              f'(capacidad={CAPACIDAD_MAXIMA}, minimo={STOCK_MINIMO}, stock inicial={STOCK_INICIAL})')

    page.evaluate("layoutAbrirModalCuerpoDetalle('A', 1, 1)")
    page.wait_for_timeout(300)
    print()
    print('=' * 78)
    print('ETAPA 5 -- Cuerpo completo: 16/16 huecos asignados, visto por el picker')
    print('=' * 78)
    page.screenshot(path=os.path.join(SHOTS, 'p5_picker_cuerpo_completo.png'))
    print('  -> p5_picker_cuerpo_completo.png')
    page.evaluate("layoutCerrarModalCuerpoDetalle()")

    # ── Vuelve a su HUD de picking (boton Volver) ────────────────────────────
    page.click('#btn-volver-layout-operario')
    page.wait_for_selector('#pantalla-operario', state='visible', timeout=5000)
    page.close()
    browser.close()

# ── Consumo simulado: baja el stock de 6 huecos bajo el minimo ──────────────
with app.app_context():
    print()
    print('=' * 78)
    print(f'ETAPA 6 -- Consumo real: {STOCK_INICIAL} -> {STOCK_BAJADO} en {len(HUECOS_A_BAJAR)} huecos (bajo minimo {STOCK_MINIMO})')
    print('=' * 78)
    from app.models.ubicacion import Ubicacion
    for (nivel, hueco) in sorted(HUECOS_A_BAJAR):
        p = productos[(nivel, hueco)]
        ub = Ubicacion.query.filter_by(almacen_id=ALMACEN_ID, pasillo='A', fila=1,
                                        cuerpo=1, nivel=nivel, hueco=hueco).first()
        inv = UbicacionProducto.query.filter_by(ubicacion_id=ub.id, producto_id=p['id']).first()
        inv.cantidad = STOCK_BAJADO
        print(f'  {ub.codigo} ({p["codigo"]}): {STOCK_INICIAL} -> {STOCK_BAJADO}')
    db.session.commit()

    print()
    print('=' * 78)
    print('ETAPA 7 -- Motor de Reposicion detecta el faltante (verificar_stock_picking)')
    print('=' * 78)
    generadas = reposicion_service.verificar_stock_picking(almacen_id=ALMACEN_ID)
    db.session.commit()
    print(f'  TareaReposicion generadas: {generadas} (esperado: {len(HUECOS_A_BAJAR)})')
    assert generadas == len(HUECOS_A_BAJAR)

# ── Ver la alarma real en Reposicion (vista admin) + el picker confirma ─────
with sync_playwright() as p:
    browser = p.chromium.launch()

    admin_page = browser.new_page(viewport={'width': 460, 'height': 900})
    admin_page.goto(f'http://127.0.0.1:{PORT}/pwa')
    admin_page.wait_for_selector('#login-email', timeout=10000)
    admin_page.fill('#login-email', ADMIN_EMAIL)
    admin_page.fill('#login-password', ADMIN_PASS)
    admin_page.click('#btn-login')
    admin_page.wait_for_selector('#pantalla-admin', state='visible', timeout=10000)
    admin_page.click("div.nav-tab[onclick=\"tab('tab-reposicion')\"]")
    admin_page.wait_for_selector('#rep-lista-ubicaciones .tabla-card', timeout=10000)
    admin_page.evaluate("repAbrirModalCuerpoDetalle('A', 1, 1)")
    admin_page.wait_for_selector('#modal-rep-cuerpo-detalle', state='visible', timeout=5000)
    admin_page.wait_for_timeout(400)

    print()
    print('=' * 78)
    print('ETAPA 8 -- Vista real: modulo Reposicion muestra la alarma (Critico, rojo)')
    print('=' * 78)
    admin_page.screenshot(path=os.path.join(SHOTS, 'p6_reposicion_alarma.png'))
    print('  -> p6_reposicion_alarma.png')
    admin_page.close()

    # ── El mismo picker recibe y confirma las reposiciones ───────────────────
    picker_page = browser.new_page(viewport={'width': 420, 'height': 860})
    picker_page.goto(f'http://127.0.0.1:{PORT}/pwa')
    picker_page.wait_for_selector('#login-email', timeout=10000)
    picker_page.fill('#login-email', PICKER_EMAIL)
    picker_page.fill('#login-password', PICKER_PASS)
    picker_page.click('#btn-login')
    # No espera #pantalla-operario: con reposicion ya pendiente y
    # puede_abastecer=True, pedirTarea() salta directo al HUD de abastecedor
    # (pickingReponerAhora) -- pantalla-operario aparece y se oculta en el
    # mismo tick, nunca queda "visible" el tiempo suficiente para esperarla.
    print()
    print('=' * 78)
    print('ETAPA 9 -- El picker recibe la reposicion en su propia cola (nivel 2, sin pedidos pendientes)')
    print('=' * 78)
    picker_page.wait_for_selector('#abast-hud', state='visible', timeout=10000)
    picker_page.wait_for_timeout(300)
    picker_page.screenshot(path=os.path.join(SHOTS, 'p7_picker_recibe_reposicion.png'))
    print('  -> p7_picker_recibe_reposicion.png')

    confirmadas = 0
    for _ in range(len(HUECOS_A_BAJAR)):
        lpn_codigo = picker_page.evaluate("ABAST_TAREA ? ABAST_TAREA.lpn_codigo : null")
        if not lpn_codigo:
            break
        picker_page.fill('#abast-input-lpn', lpn_codigo)
        picker_page.click('#abast-btn-confirmar')
        picker_page.wait_for_selector('text=Reposición completada', state='visible', timeout=5000)
        if confirmadas == 0:
            picker_page.wait_for_timeout(200)
            picker_page.screenshot(path=os.path.join(SHOTS, 'p8_picker_confirma_reposicion.png'))
            print('  -> p8_picker_confirma_reposicion.png (primera confirmacion, toast real)')
        confirmadas += 1
        picker_page.wait_for_timeout(1500)  # deja que el HUD cargue la siguiente tarea real
    print(f'  Reposiciones confirmadas por el picker: {confirmadas}')
    assert confirmadas == len(HUECOS_A_BAJAR)

    # Ultima confirmacion agoto las reposiciones -- abastCerrarHUD() (ABAST_UNIFICADO)
    # vuelve a pantalla-operario y llama pedirTarea(), que renderiza "sin
    # tareas" en #contenido-tarea (no en #abast-contenido, ese es solo de la
    # pantalla dedicada del abastecedor puro).
    picker_page.wait_for_selector('#pantalla-operario', state='visible', timeout=5000)
    picker_page.wait_for_selector('text=Sin tareas pendientes', state='visible', timeout=5000)
    picker_page.wait_for_timeout(300)
    print()
    print('=' * 78)
    print('ETAPA 10 -- Sin mas tareas de reposicion: el picker queda libre')
    print('=' * 78)
    picker_page.screenshot(path=os.path.join(SHOTS, 'p9_picker_sin_mas_tareas.png'))
    print('  -> p9_picker_sin_mas_tareas.png')

    picker_page.close()
    browser.close()

# ── Backend despues de confirmar todo ────────────────────────────────────────
with app.app_context():
    db.session.expire_all()
    from app.models.ubicacion import Ubicacion
    from app.models.siesa_job import SiesaJob

    print()
    print('=' * 78)
    print('ETAPA 11 -- Backend real despues de confirmar (estado en la base)')
    print('=' * 78)
    completadas = TareaReposicion.query.filter_by(estado='COMPLETADA').count()
    pendientes = TareaReposicion.query.filter(TareaReposicion.estado.in_(['PENDIENTE', 'EN_PROCESO'])).count()
    print(f'  TareaReposicion COMPLETADA = {completadas}  (esperado {len(HUECOS_A_BAJAR)})')
    print(f'  TareaReposicion pendiente/en proceso = {pendientes}  (esperado 0)')
    assert completadas == len(HUECOS_A_BAJAR)
    assert pendientes == 0

    for (nivel, hueco) in sorted(HUECOS_A_BAJAR):
        p = productos[(nivel, hueco)]
        ub = Ubicacion.query.filter_by(almacen_id=ALMACEN_ID, pasillo='A', fila=1,
                                        cuerpo=1, nivel=nivel, hueco=hueco).first()
        inv = UbicacionProducto.query.filter_by(ubicacion_id=ub.id, producto_id=p['id']).first()
        print(f'  {ub.codigo}: cantidad = {inv.cantidad}  (era {STOCK_BAJADO}, +{LPN_CANTIDAD} del LPN roto -> {STOCK_BAJADO + LPN_CANTIDAD})')
        assert inv.cantidad == STOCK_BAJADO + LPN_CANTIDAD

    n_jobs = SiesaJob.query.count()
    print(f'  SiesaJob.query.count() = {n_jobs}  (debe ser 0 -- este flujo nunca toca Siesa)')
    assert n_jobs == 0

print()
print('=' * 78)
print('FLUJO COMPLETO OK -- creado por el picker (no admin), asignado, alarma real,')
print('reposicion confirmada por el mismo picker, backend consistente, cero Siesa.')
print('=' * 78)
