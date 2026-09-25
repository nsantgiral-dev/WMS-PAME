import re, json
from datetime import datetime
import pytest
from tests.flota.test_ficha_primera_vez_js import correr, ARCHIVOS_FICHA, EXPEDIENTE

def _auth(t): return {'Authorization': f'Bearer {t}'}

def test_explora(tmp_path, client, db, almacen):
    from flask_jwt_extended import create_access_token
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import FichaTecnica
    u = Usuario(email='lleno@x.test', nombre='Admin', rol='admin', activo=True, almacen_id=almacen.id); u.set_password('x')
    v = Vehiculo(placa='LLE001', tipo='NHR', activo=True)
    db.session.add_all([u, v]); db.session.flush()
    db.session.add(FichaTecnica(vehiculo_id=v.id, posiciones_llanta=6, km_inicial=50000,
        km_inicial_ts=datetime(2026, 1, 1), distribucion='correa', distribucion_km_cambio=60000,
        distribucion_fuente='manual_fabricante', aceite_motor_spec='15W40'))
    db.session.commit()
    t = create_access_token(identity=str(u.id))
    for d in [{'tipo':'soat','estado':'vigente','numero':'S1','entidad':'X','fecha_expedicion':'2026-01-01','fecha_vencimiento':'2027-01-01'},
              {'tipo':'poliza_rc','estado':'no_encontrado'},
              {'tipo':'tarjeta_propiedad','estado':'vigente','numero':'T1','entidad':'X','fecha_expedicion':'2020-01-01'}]:
        r = client.post('/flota/vehiculo/LLE001/documentos', headers=_auth(t), json=d); print('doc', r.status_code, r.get_json() if r.status_code>=300 else '')
    r = client.post('/flota/tanqueos', headers=_auth(t), json=dict(placa='LLE001', fecha='2026-09-24', valor='120000',
                 galones='10', tanque='lleno', estacion='Terpel', km=50100, proveedor='Terpel', origen_costo='efectivo_conductor')); print('tanq', r.status_code, r.get_json() if r.status_code>=300 else '')
    r = client.post('/flota/preventivo/LLE001/sembrar', headers=_auth(t)); print('sembrar', r.status_code)
    rutas = {}
    for url in ('/api/almacenes/', '/api/rutas/conductores?activos=true', '/flota/custodia/activa/LLE001',
                '/flota/vehiculo/LLE001/ficha', '/flota/vehiculo/LLE001/documentos', '/flota/hallazgos/LLE001',
                '/flota/vocabulario', '/flota/gastos/LLE001', '/flota/ordenes/LLE001', '/flota/llantas/LLE001',
                '/flota/preventivo/LLE001', '/flota/odometro/dudosas'):
        r = client.get(url, headers=_auth(t)); rutas[url] = r.get_json()
    for fn in EXPEDIENTE:
        out = correr(tmp_path, {'archivos': ARCHIVOS_FICHA, 'operario': {'rol': 'admin'}, 'rutas': rutas,
                                'pasos': [{'fn': fn, 'args': ['LLE001']}], 'leer': ['flota-recibo']})
        html = out['html']['flota-recibo'] or ''
        vis = re.sub(r'<[^>]*>', ' ', html)
        toks = sorted(set(re.findall(r'\b[a-z]+(?:_[a-z0-9]+)+\b', vis)))
        print('FN', fn, toks)
