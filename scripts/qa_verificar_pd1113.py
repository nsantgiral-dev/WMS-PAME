"""Solo lectura: estado real de PD1113 en Siesa QA ahora mismo — para saber
si el 142945 (RM) que respondio codigo:0 realmente quedo creado."""
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

    print('=== estado del pedido PD1113 ===')
    try:
        estado = connekta.get_estado_pedido('PD', 1113)
        print('estado_siesa:', estado)
    except Exception as e:
        print('ERROR:', e)

    print('\n=== RM desde pedido (get_remision_desde_pedido) ===')
    try:
        rm = connekta.get_remision_desde_pedido('PD', 1113)
        print(rm)
    except Exception as e:
        print('ERROR:', e)

    print('\n=== FE desde pedido (API estandar, ya arreglada) ===')
    try:
        fe = connekta.get_factura_desde_pedido('PD', 1113)
        print(fe)
    except Exception as e:
        print('ERROR:', e)
