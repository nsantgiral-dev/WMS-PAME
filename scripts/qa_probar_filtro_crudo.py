"""Solo lectura, requests crudo — para ver el CUERPO real del 400 que
`connekta._get` descarta. No modifica nada."""
import os
import sys
import requests

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

    intentos = [
        ("f430_consec_docto", "f350_id_co = ''003'' AND f430_consec_docto = 1113"),
        ("f350_consec_docto + tipo PD", "f350_id_co = ''003'' AND f350_consec_docto = 1113 AND f350_id_tipo_docto = ''PD''"),
        ("solo f350_id_co (sin mas filtro, control)", "f350_id_co = ''003''"),
        ("f461_num_docto_referencia", "f350_id_co = ''003'' AND f461_num_docto_referencia = ''PD1113''"),
    ]
    for etiqueta, parametros in intentos:
        print(f'\n=== {etiqueta} ===')
        r = requests.get(
            connekta.url_get, headers=connekta.headers,
            params={'idCompania': connekta.id_compania,
                    'descripcion': 'API_v2_Ventas_Facturas_DesdePedido',
                    'paginacion': 'numPag=1|tamPag=5',
                    'parametros': parametros},
            timeout=30,
        )
        print('status:', r.status_code)
        print('body  :', r.text[:1500])
