"""
Solo lectura contra Siesa QA real -- confirma del lado de Siesa (no solo
el log del WMS) que la entrada de OC66 quedo bien: cantidad recibida en
la OC, stock resultante de BELLESB1382 en NB1, y el valor/costo si la
consulta lo trae.
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

app = create_app()
with app.app_context():
    db.create_all()
    from app.services.connekta_gateway import connekta

    print('=== OC66 — estado real en Siesa (API_v2_Compras_Ordenes) ===')
    try:
        ocs = connekta.get_ordenes_compra_aprobadas()
        items = ocs.get('items', ocs) if isinstance(ocs, dict) else ocs
        for fila in items:
            if str(fila.get('numero_oc') or fila.get('consec_docto') or '') in ('66', 'OC66'):
                print(fila)
    except Exception as e:
        print('ERROR consultando OC:', e)

    print('\n=== Stock real BELLESB1382 en NB1 ===')
    try:
        stock = connekta.get_stock_bodega('NB1')
        filas = stock.get('detalle', {}).get('Table', [])
        for f in filas:
            if (f.get('f120_referencia') or '').strip() == 'BELLESB1382':
                print(f)
    except Exception as e:
        print('ERROR consultando stock:', e)
