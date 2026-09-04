import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_verificar_costo.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-verificar-costo-32-bytes-o-mas-x')
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)
from app import create_app
from app.extensions import db
app = create_app()
with app.app_context():
    db.create_all()
    from app.services.connekta_gateway import connekta
    stock = connekta.get_stock_bodega('NB1')
    filas = stock.get('detalle', {}).get('Table', [])
    for f in filas:
        ref = (f.get('f120_referencia') or '').strip()
        if ref in ('PAPELSP6948', 'PAPELSP9218'):
            print(ref, {k: f[k] for k in (
                'f400_cant_existencia_1', 'f400_costo_prom_uni', 'f400_costo_prom_tot')})
