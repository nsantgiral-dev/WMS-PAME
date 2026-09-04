"""
Solo lectura contra Siesa QA real -- confirma que el traslado NB1->NS1 de
PAPELSP6948 (5 und) quedo bien tanto en cantidad como en valor/costo del
inventario en ambas bodegas (API_v2_Inventarios_InvFecha via get_stock_bodega).
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_verificar_valor.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-verificar-valor-32-bytes-o-mas-x')
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

from app import create_app
from app.extensions import db

app = create_app()
with app.app_context():
    db.create_all()
    from app.services.connekta_gateway import connekta

    for bod in ('NB1', 'NS1'):
        print(f'\n=== Stock real PAPELSP6948 en {bod} ===')
        try:
            stock = connekta.get_stock_bodega(bod)
            filas = stock.get('detalle', {}).get('Table', [])
            for f in filas:
                if (f.get('f120_referencia') or '').strip() == 'PAPELSP6948':
                    print(f)
        except Exception as e:
            print('ERROR consultando stock:', e)
