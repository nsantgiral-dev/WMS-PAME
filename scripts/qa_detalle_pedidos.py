"""
Detalle de solo lectura de los 7 pedidos reales elegidos para el ciclo E2E
contra Siesa QA (conversación 2026-09-04) — ítems, cantidades pendientes y
stock real en NB1, antes de tocar nada.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_verificacion.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-verificacion-solo-lectura-32-bytes-o-mas')

from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

from app import create_app
from app.extensions import db

PEDIDOS = ['PD1113', 'PD1125', 'PD1134', 'PD1135', 'PD1141', 'PD1157', 'PD1146',
           'PD1147', 'PD1149', 'PD1150', 'PD1151', 'PD1152']

app = create_app()
with app.app_context():
    db.create_all()
    from app.services.connekta_gateway import connekta

    pedidos = connekta.get_pedidos_aprobados()
    items = pedidos.get('items', pedidos) if isinstance(pedidos, dict) else pedidos

    por_pedido = {}
    for fila in items:
        por_pedido.setdefault(fila['numero_pedido'], []).append(fila)

    codigos_a_verificar = set()
    for num in PEDIDOS:
        lineas = por_pedido.get(num)
        print(f'\n=== {num} ===')
        if not lineas:
            print('  [FALTA] NO esta en la lista de comprometidos (ya se despacho o cambio de estado?)')
            continue
        for l in lineas:
            print(f"  {l['item_codigo']:15s} {l['item_descripcion']:35s} "
                  f"pedida={l['cantidad_pedida']:>8} pendiente={l['cantidad_pendiente']:>8} "
                  f"cliente={l['cliente']}")
            codigos_a_verificar.add(l['item_codigo'])

    print('\n=== Stock real en NB1 para esos items ===')
    try:
        stock = connekta.get_stock_bodega('NB1')
        items_stock = stock.get('detalle', {}).get('Table', [])
        stock_por_codigo = {}
        for fila in items_stock:
            cod = (fila.get('f120_referencia') or '').strip()
            if cod in codigos_a_verificar:
                cant = float(fila.get('f400_cant_existencia_1', 0) or 0)
                stock_por_codigo[cod] = stock_por_codigo.get(cod, 0) + cant
        for cod in sorted(codigos_a_verificar):
            print(f'  {cod:15s} stock_NB1={stock_por_codigo.get(cod, "NO ENCONTRADO (0 filas)")}')
    except Exception as e:
        print(f'  [ERROR] get_stock_bodega fallo: {e}')
