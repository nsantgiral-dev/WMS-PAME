"""
Verificación de solo lectura contra Siesa QA — primer paso antes de correr
el caso E2E real (ver conversación 2026-09-04, "un solo caso real, a fondo").

Qué hace:
  1. Carga `.env.qa` (credenciales + `MODO_ENSAYO=true` — GETs reales, POSTs
     bloqueados en el propio gateway, ver `connekta_gateway.py::_post`).
  2. Fuerza `DATABASE_URL` a un SQLite AISLADO antes de crear la app — NUNCA
     toca la Postgres real de `.env` (Railway), aunque `.env` la traiga.
     Mismo criterio que `bkops01_test.db` en sesiones anteriores.
  3. Confirma que el gateway quedó en modo real (no simulación) y en modo
     ensayo (POSTs bloqueados).
  4. Trae la lista de pedidos APROBADOS/COMPROMETIDOS (estado=3) del CO
     configurado — candidatos reales para elegir el pedido de prueba.

No escribe nada en Siesa. No toca la base de producción.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# ── Aislamiento — ANTES de cualquier import de `app` ────────────────────
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_verificacion.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-verificacion-solo-lectura-32-bytes-o-mas')

from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

from app import create_app
from app.extensions import db

app = create_app()
with app.app_context():
    db.create_all()

    from app.services.connekta_gateway import connekta

    print('=== Estado del gateway ===')
    print('URL Connekta   :', connekta.url_get.rsplit('/api/', 1)[0])
    print('modo_simulacion:', connekta.modo_simulacion, '(debe ser False — hay credenciales)')
    print('modo_ensayo    :', connekta.modo_ensayo, '(debe ser True — POSTs bloqueados)')
    print('id_compania    :', connekta.id_compania)
    print('centro_op      :', connekta.centro_op)
    print()

    if connekta.modo_simulacion:
        print('⛔ Sigue en modo simulación — revisar CONNEKTA_IKEY/ITOKEN en .env.qa')
        sys.exit(1)

    print('=== Pedidos comprometidos (estado=3) en CO', connekta.centro_op, '===')
    try:
        pedidos = connekta.get_pedidos_aprobados()
    except Exception as e:
        print('⛔ Error consultando Siesa QA:', e)
        sys.exit(1)

    items = pedidos.get('items', pedidos) if isinstance(pedidos, dict) else pedidos
    vistos = {}
    for fila in items:
        num = fila.get('numero_pedido', '?')
        v = vistos.setdefault(num, {
            'cliente': fila.get('cliente'), 'bodega': fila.get('bodega'),
            'fecha_entrega': fila.get('fecha_entrega'), 'lineas': 0,
        })
        v['lineas'] += 1
    print(f'{len(vistos)} pedido(s) comprometidos distintos ({len(items)} líneas):\n')
    for num, v in list(vistos.items())[:25]:
        print(f"  · {num:10s} {v['cliente']:35s} bodega={v['bodega']:5s} "
              f"lineas={v['lineas']:2d}  entrega={v['fecha_entrega']}")
    if len(vistos) > 25:
        print(f'  … y {len(vistos) - 25} más')
