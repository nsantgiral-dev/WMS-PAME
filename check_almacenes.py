from app import create_app
from app.models.almacen import Almacen

app = create_app()
with app.app_context():
    almacenes = Almacen.query.all()
    print(f"Total almacenes: {len(almacenes)}")
    for a in almacenes:
        print(f"ID: {a.id}, Codigo: {a.codigo}, Siesa ID: {a.bodega_siesa_id}")
