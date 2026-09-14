"""
Consulta real (solo lectura) de existencia/comprometida en NB1 para elegir
2 SKUs sanos (disponible amplio en ambas direcciones) para las pruebas
reales de conteo ciclico: uno para sobrante (AJ-ENT) y otro para faltante
(AJ-SAL). Ver la regla real del conector 142951 en connekta_gateway.py
(rechaza si el disponible RESULTANTE queda negativo).
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(REPO_ROOT, 'scripts', 'qa_elegir_skus.db')
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ.setdefault('SECRET_KEY', 'qa-elegir-skus-32-bytes-o-mas-para-x')
from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, '.env.qa'), override=True)

from app import create_app
from app.extensions import db

app = create_app()
with app.app_context():
    db.create_all()
    from app.services.connekta_gateway import connekta

    candidatos = ['PAPELSP6948', 'PAPELSP9830', 'BELLESB1382', 'PAPELSP9218']
    for cod in candidatos:
        res = connekta.get_inventario_fecha(cod, bodega='NB1')
        tabla = res.get('detalle', {}).get('Table', [])
        if not tabla:
            print(f'{cod}: sin fila (Table vacio)')
            continue
        f = tabla[0]
        exist = f.get('f400_cant_existencia_1', 0)
        comp = f.get('f400_cant_comprometida_1', 0)
        sal_sc = f.get('f400_cant_salida_sin_conf_1', 0)
        disp = exist - comp - sal_sc
        print(f'{cod}: existencia={exist} comprometida={comp} salida_sin_conf={sal_sc} '
              f'DISPONIBLE={disp}')
