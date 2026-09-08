"""Seed minimo (solo admin) para probar visualmente el tab Manual de Usuario."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.extensions import db
from app.models.almacen import Almacen
from app.models.usuario import Usuario

app = create_app()
with app.app_context():
    db.create_all()
    almacen = Almacen(codigo='NB1', nombre='Bodega CD', bodega_siesa_id='NB1', activo=True)
    db.session.add(almacen)
    db.session.flush()
    admin = Usuario(email='admin@smoke.test', nombre='Admin Smoke', rol='admin', almacen_id=almacen.id, activo=True)
    admin.set_password('smoke1234')
    db.session.add(admin)
    db.session.commit()
    print('SEED OK')
