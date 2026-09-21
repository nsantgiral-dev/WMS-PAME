# -*- coding: utf-8 -*-
"""
Prueba real del CRUD completo de Layout, como ADMIN, click por click contra
la app real (Flask dev server + sqlite descartable + Playwright):

  CREATE     -- crea un cuerpo PICKING (1 entrepano, 2 huecos) via el wizard real.
  UPDATE (1) -- asigna SKU_A al hueco 1 (cantidad, capacidad, minimo).
  UPDATE (2) -- intenta "Cambiar SKU" del hueco 1 a SKU_B (prueba real de si
                el flujo de UI de verdad permite reasignar un hueco que ya
                tiene un SKU distinto con stock contado).
  UPDATE (3) -- edita capacidad_maxima del hueco 2 (vacio, sin SKU) via
                "Editar ubicacion".
  UPDATE (4) -- reclasifica el hueco 2 de PICKING a RESERVA via "Reclasificar".
  DELETE (1) -- intenta eliminar el hueco 1 (con stock) -- debe bloquear, y
                al forzar (confirm anidado, solo admin) debe borrar igual.
  DELETE (2) -- elimina el hueco 2 (ya RESERVA, vacio) sin forzar -- debe
                borrar limpio, sin bloqueo.

Reporta la verdad de cada paso (exito real o error real devuelto por el
backend), no lo que "debería" pasar.
"""
import os
import sys
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')

REPO_ROOT = r"C:\Users\SSJUAN03\Desktop\WMS-PAME"
sys.path.insert(0, REPO_ROOT)

SCRATCH = os.path.join(REPO_ROOT, 'scripts')
DB_PATH = os.path.join(SCRATCH, 'qa_layout_crud.db')
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

os.environ['DATABASE_URL'] = 'sqlite:///' + DB_PATH
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-layout-crud-32-bytes-o-mas-xxxxx')

PORT = 5964
ADMIN_EMAIL = 'admin_crud_qa@wms-pame.local'
ADMIN_PASS = 'qa12345678'

from app import create_app
from app.extensions import db

app = create_app()
with app.app_context():
    db.create_all()

    from app.models.almacen import Almacen
    from app.models.usuario import Usuario
    from app.models.producto import Producto
    from werkzeug.security import generate_password_hash

    almacen = Almacen(codigo='NB1', nombre='Neiva Bodega CD',
                       bodega_siesa_id='NB1', centro_op_siesa='003', activo=True)
    db.session.add(almacen)
    db.session.flush()

    admin = Usuario(nombre='Admin CRUD QA', email=ADMIN_EMAIL,
                     password_hash=generate_password_hash(ADMIN_PASS),
                     rol='admin', almacen_id=almacen.id, activo=True)
    sku_a = Producto(codigo='PAPELSP8001', nombre='RESMA CARTA CRUD A',
                      codigo_siesa='PAPELSP8001', unidad_negocio_id='001', activo=True)
    sku_b = Producto(codigo='PAPELSP8002', nombre='RESMA CARTA CRUD B',
                      codigo_siesa='PAPELSP8002', unidad_negocio_id='001', activo=True)
    db.session.add_all([admin, sku_a, sku_b])
    db.session.commit()
    print('Seed OK -- almacen, admin, 2 SKU listos')
    ALMACEN_ID = almacen.id
    SKU_A_ID = sku_a.id
    SKU_B_ID = sku_b.id

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

    dialog_log = []

    def _on_dialog(dialog):
        dialog_log.append(dialog.message)
        print(f'    [confirm() nativo] "{dialog.message}"')
        dialog.accept()

    page.on('dialog', _on_dialog)

    page.goto(f'http://127.0.0.1:{PORT}/pwa')
    page.wait_for_selector('#login-email', timeout=10000)
    page.fill('#login-email', ADMIN_EMAIL)
    page.fill('#login-password', ADMIN_PASS)
    page.click('#btn-login')
    page.wait_for_selector('#pantalla-admin', state='visible', timeout=10000)
    page.click("div.nav-tab[onclick=\"tab('tab-layout')\"]")
    page.wait_for_selector('#layout-lista-ubicaciones', timeout=10000)

    print()
    print('=' * 78)
    print('CREATE -- crea el cuerpo PICKING (1 entrepano, 2 huecos)')
    print('=' * 78)
    page.click("button:has-text('+ Picking')")
    page.wait_for_selector('#modal-layout-cuerpo', state='visible', timeout=5000)
    page.select_option('#layout-cuerpo-pasillo', 'A')
    page.select_option('#layout-cuerpo-fila', '1')
    page.fill('#layout-cuerpo-numero', '1')
    page.fill('#layout-cuerpo-entrepanos', '1')
    page.click('#layout-cuerpo-btn-siguiente')
    page.wait_for_selector('#layout-cuerpo-paso2', state='visible', timeout=5000)
    page.fill('#layout-cuerpo-hueco-nivel-1', '2')
    page.click('#layout-cuerpo-paso2 button:has-text("Crear")')
    page.wait_for_timeout(500)

    huecos = page.evaluate(
        "_layoutUbicacionesCache.filter(u => u.pasillo==='A' && u.fila===1 && u.cuerpo===1)"
        ".sort((a,b) => a.hueco - b.hueco).map(u => ({id:u.id, codigo:u.codigo}))"
    )
    assert len(huecos) == 2, f'se esperaban 2 huecos, llegaron {len(huecos)}'
    h1, h2 = huecos[0], huecos[1]
    print(f'  OK -- creados {h1["codigo"]} y {h2["codigo"]}')

    print()
    print('=' * 78)
    print('UPDATE 1 -- asigna SKU_A al hueco 1 (cantidad=50, capacidad=100, minimo=20)')
    print('=' * 78)
    page.evaluate("layoutAbrirModalCuerpoDetalle('A', 1, 1)")
    page.wait_for_selector('#modal-layout-cuerpo-detalle', state='visible', timeout=5000)
    page.locator('#layout-cuerpo-detalle-body button:has-text("Asignar SKU")').first.click()
    page.wait_for_selector('#modal-layout-asignar-entrepano', state='visible', timeout=5000)
    page.fill(f'#layout-asignar-ent-codigo-{h1["id"]}', 'PAPELSP8001')
    page.locator(f'#layout-asignar-ent-codigo-{h1["id"]}').press('Enter')
    page.wait_for_timeout(150)
    page.fill(f'#layout-asignar-ent-cantidad-{h1["id"]}', '50')
    page.fill(f'#layout-asignar-ent-capacidad-{h1["id"]}', '100')
    page.fill(f'#layout-asignar-ent-minimo-{h1["id"]}', '20')
    page.click('#modal-layout-asignar-entrepano button:has-text("Confirmar")')
    page.wait_for_timeout(500)

    from app.models.ubicacion import Ubicacion
    with app.app_context():
        ub1 = Ubicacion.query.get(h1['id'])
        print(f'  Backend real: {ub1.codigo} -> producto_asignado_id={ub1.producto_asignado_id}, '
              f'capacidad_maxima={ub1.capacidad_maxima}, stock_minimo={ub1.stock_minimo}')
        assert ub1.producto_asignado_id is not None
    print('  OK -- SKU_A asignado')

    print()
    print('=' * 78)
    print('UPDATE 2 -- intenta "Cambiar SKU" del hueco 1: SKU_A -> SKU_B (con stock ya contado)')
    print('=' * 78)
    page.evaluate("layoutAbrirModalCuerpoDetalle('A', 1, 1)")
    page.wait_for_timeout(200)
    page.locator('#layout-cuerpo-detalle-body button:has-text("Asignar SKU")').first.click()
    page.wait_for_selector('#modal-layout-asignar-entrepano', state='visible', timeout=5000)
    page.click(f'button[onclick="layoutEditarSkuEntrepano({h1["id"]})"]')
    page.wait_for_timeout(150)
    page.fill(f'#layout-asignar-ent-codigo-{h1["id"]}', 'PAPELSP8002')
    page.locator(f'#layout-asignar-ent-codigo-{h1["id"]}').press('Enter')
    page.wait_for_timeout(150)
    page.fill(f'#layout-asignar-ent-cantidad-{h1["id"]}', '30')
    page.click('#modal-layout-asignar-entrepano button:has-text("Confirmar")')
    page.wait_for_timeout(500)
    resultado_texto = page.locator('#layout-asignar-ent-resultado').inner_text()
    with app.app_context():
        ub1 = Ubicacion.query.get(h1['id'])
        print(f'  Mensaje real de error en la UI: "{resultado_texto.strip()}"' if resultado_texto.strip() else '  Sin errores en la UI')
        print(f'  Backend real DESPUES del intento: producto_asignado_id sigue = {ub1.producto_asignado_id} '
              f'({"SKU_A, NO cambio" if ub1.producto_asignado_id != SKU_B_ID else "SKU_B, SI cambio"})')
    page.evaluate("layoutCerrarModalAsignarEntrepano()")
    page.evaluate("layoutCerrarModalCuerpoDetalle()")

    print()
    print('=' * 78)
    print('UPDATE 3 -- edita capacidad_maxima del hueco 2 (vacio) via "Editar ubicacion"')
    print('=' * 78)
    page.evaluate("layoutAbrirModalCuerpoDetalle('A', 1, 1)")
    page.wait_for_timeout(200)
    page.locator('#layout-cuerpo-detalle-body button:has-text("Ver")').first.click()
    page.wait_for_selector('#modal-layout-ver-entrepano', state='visible', timeout=5000)
    page.click(f'button[onclick="layoutAbrirModalEditarUbicacion({h2["id"]})"]')
    page.wait_for_selector('#modal-layout-editar-ubicacion', state='visible', timeout=5000)
    page.fill('#layout-editar-ub-capacidad', '75')
    page.click('#modal-layout-editar-ubicacion button:has-text("Guardar cambios")')
    page.wait_for_timeout(500)

    with app.app_context():
        ub2 = Ubicacion.query.get(h2['id'])
        print(f'  Backend real: {ub2.codigo} -> capacidad_maxima={ub2.capacidad_maxima}')
        assert ub2.capacidad_maxima == 75
    print('  OK -- capacidad_maxima actualizada')

    print()
    print('=' * 78)
    print('UPDATE 4 -- reclasifica el hueco 2: PICKING -> RESERVA via "Reclasificar"')
    print('=' * 78)
    page.evaluate("layoutAbrirModalVerEntrepano('%d')" % h2['id'])
    page.wait_for_selector('#modal-layout-ver-entrepano', state='visible', timeout=5000)
    page.click(f'button[onclick="layoutAbrirModalReclasificar({h2["id"]})"]')
    page.wait_for_selector('#modal-layout-reclasificar', state='visible', timeout=5000)
    page.select_option('#layout-reclasificar-zona', 'RESERVA')
    page.click('#modal-layout-reclasificar button:has-text("Guardar")')
    page.wait_for_timeout(500)

    with app.app_context():
        ub2 = Ubicacion.query.get(h2['id'])
        print(f'  Backend real: {ub2.codigo} -> tipo_zona={ub2.tipo_zona}  (codigo NO cambia de prefijo: sigue diciendo "{ub2.codigo.split("-")[0]}-")')
        assert ub2.tipo_zona == 'RESERVA'
    print('  OK -- zona reclasificada a RESERVA')
    page.evaluate("layoutCerrarModalVerEntrepano()")

    print()
    print('=' * 78)
    print('DELETE 1 -- intenta eliminar el hueco 1 (tiene stock) -- debe bloquear y luego forzar')
    print('=' * 78)
    page.evaluate("layoutCargarUbicaciones()")
    page.wait_for_timeout(300)
    page.evaluate("layoutAbrirModalCuerpoDetalle('A', 1, 1)")
    page.wait_for_timeout(200)
    page.locator('#layout-cuerpo-detalle-body button:has-text("Ver")').first.click()
    page.wait_for_selector('#modal-layout-ver-entrepano', state='visible', timeout=5000)
    page.click(f'button[onclick="layoutEliminarUbicacion({h1["id"]}, \'{h1["codigo"]}\')"]')
    page.wait_for_timeout(700)

    with app.app_context():
        existe_h1 = Ubicacion.query.get(h1['id'])
    print(f'  Dialogs nativos disparados: {len(dialog_log)} (esperado 3 -- confirmar, ofrecer forzar, confirmar forzar)')
    print(f'  {h1["codigo"]} sigue existiendo despues del ciclo bloqueo+forzar: {existe_h1 is not None}')
    if existe_h1 is None:
        print('  OK -- bloqueado primero (stock activo), forzado despues por ser admin -- eliminado de verdad')
    else:
        print('  (no se elimino -- revisar mensajes de arriba)')
    page.evaluate("layoutCerrarModalVerEntrepano(); layoutCerrarModalCuerpoDetalle();")

    print()
    print('=' * 78)
    print('DELETE 2 -- elimina el hueco 2 (ya RESERVA, vacio) -- debe borrar limpio, sin forzar')
    print('=' * 78)
    dialogs_antes = len(dialog_log)
    page.evaluate("layoutCargarUbicaciones()")
    page.wait_for_timeout(300)
    # Hueco 2 ya es RESERVA -- buscarlo en esa zona ahora, no en PICKING.
    page.evaluate("layoutZonaTab('RESERVA')")
    page.wait_for_timeout(300)
    page.evaluate(f"layoutEliminarUbicacion({h2['id']}, '{h2['codigo']}')")
    page.wait_for_timeout(500)

    with app.app_context():
        existe_h2 = Ubicacion.query.get(h2['id'])
    print(f'  Dialogs nativos disparados en este paso: {len(dialog_log) - dialogs_antes} (esperado 1 -- solo confirmar)')
    print(f'  {h2["codigo"]} sigue existiendo: {existe_h2 is not None}')
    assert existe_h2 is None, 'el hueco 2 (vacio) debio eliminarse sin bloqueo'
    print('  OK -- eliminado limpio, sin necesidad de forzar')

    page.close()
    browser.close()

print()
print('=' * 78)
print('RESUMEN CRUD')
print('=' * 78)
print('  CREATE            -- OK (cuerpo + 2 huecos via wizard real)')
print('  UPDATE asignar SKU -- OK (SKU_A asignado con cantidad/capacidad/minimo)')
print('  UPDATE cambiar SKU -- ver mensaje real arriba (con stock contado)')
print('  UPDATE editar ub.  -- OK (capacidad_maxima)')
print('  UPDATE reclasificar -- OK (PICKING -> RESERVA)')
print('  DELETE bloqueado+forzado -- ver resultado arriba')
print('  DELETE limpio      -- OK')
