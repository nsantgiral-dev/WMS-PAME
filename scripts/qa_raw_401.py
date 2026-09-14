import os, sys, json, requests
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_raw_401.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-raw-401-32-bytes-o-mas-para-x')
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)
from app import create_app
from app.extensions import db
app = create_app()
with app.app_context():
    from app.services.connekta_gateway import connekta
    url = connekta.url_get_dinamico
    params = {'idCompania': connekta.id_compania, 'descripcion': 'api_tecnocedi_requisiciones_traslado',
              'paginacion': 'numPag=1|tamPag=20'}
    print('headers:', connekta.headers)
    print('url:', url)
    print('params:', params)
    r = requests.get(url, headers=connekta.headers, params=params, timeout=30)
    print('status:', r.status_code)
    print('body:', r.text[:2000])
