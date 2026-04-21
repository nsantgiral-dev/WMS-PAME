"""
Fixtures compartidos para toda la suite de tests WMS-PAME.

Usa SQLite en memoria para velocidad — sin necesidad de PostgreSQL ni Connekta.
El scheduler de APScheduler se desactiva con SYNC_SCHEDULER=false.
"""
import os
import pytest

# Forzar antes de importar la app
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SYNC_SCHEDULER'] = 'false'
os.environ['SECRET_KEY'] = 'test-secret-key'
os.environ['CONNEKTA_MODO_SIMULACION'] = 'true'


from app import create_app
from app.extensions import db as _db


@pytest.fixture(scope='session')
def app():
    """App Flask con SQLite en memoria — una instancia por sesión de tests."""
    application = create_app()
    application.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'JWT_ACCESS_TOKEN_EXPIRES': False,
    })
    with application.app_context():
        _db.create_all()
        yield application
        _db.drop_all()


@pytest.fixture(scope='function')
def db(app):
    """BD limpia por cada test — trunca todas las tablas al terminar."""
    with app.app_context():
        yield _db
        _db.session.rollback()
        # Truncar todas las tablas — ignorar si alguna no existe (SQLite)
        for table in reversed(_db.metadata.sorted_tables):
            try:
                _db.session.execute(table.delete())
            except Exception:
                _db.session.rollback()
        _db.session.commit()


@pytest.fixture(scope='function')
def client(app, db):
    return app.test_client()


# ─────────────────────────────────────────────────────────────────────────────
# Factories de datos de prueba
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def almacen(db):
    from app.models.almacen import Almacen
    a = Almacen(codigo='ALM-TEST', nombre='Almacén Test',
                bodega_siesa_id='NB1', activo=True)
    db.session.add(a)
    db.session.commit()
    return a


@pytest.fixture
def producto(db):
    from app.models.producto import Producto
    p = Producto(codigo='PROD-001', nombre='Resma Carta 500h',
                 codigo_siesa='PROD-001', activo=True)
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def producto2(db):
    from app.models.producto import Producto
    p = Producto(codigo='PROD-002', nombre='Lapicero Azul',
                 codigo_siesa='PROD-002', activo=True)
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def ub_reserva(db, almacen):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo='RES-01-A', almacen_id=almacen.id,
                  tipo_zona='RESERVA', activo=True)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def ub_picking(db, almacen):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo='PIK-01-A', almacen_id=almacen.id,
                  tipo_zona='PICKING', stock_minimo=50, stock_maximo=200,
                  secuencia_ruteo=1, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def ub_general(db, almacen):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo='GEN-01', almacen_id=almacen.id,
                  tipo_zona='GENERAL', activo=True)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def inv_picking(db, ub_picking, producto):
    """Inventario en zona PICKING — 30 UNDs (bajo el mínimo de 50)."""
    from app.models.inventario import UbicacionProducto
    inv = UbicacionProducto(
        ubicacion_id=ub_picking.id,
        producto_id=producto.id,
        cantidad=30, reservado=0, bloqueado=0,
    )
    db.session.add(inv)
    db.session.commit()
    return inv


@pytest.fixture
def inv_reserva(db, ub_reserva, producto):
    """Inventario en zona RESERVA — 1240 UNDs (una paca)."""
    from app.models.inventario import UbicacionProducto
    inv = UbicacionProducto(
        ubicacion_id=ub_reserva.id,
        producto_id=producto.id,
        cantidad=1240, reservado=0, bloqueado=0,
    )
    db.session.add(inv)
    db.session.commit()
    return inv


@pytest.fixture
def lpn_activo(db, producto, almacen, ub_reserva):
    """LPN sellado en zona RESERVA."""
    from app.models.lpn import LPN
    lpn = LPN(
        codigo='LPN-0000001',
        producto_id=producto.id,
        almacen_id=almacen.id,
        ubicacion_id=ub_reserva.id,
        factor_conversion=1240,
        cantidad_actual=1240,
        estado='ACTIVO',
    )
    db.session.add(lpn)
    db.session.commit()
    return lpn


@pytest.fixture
def usuario(db, almacen):
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(
        nombre='Operario Test',
        email='op@test.com',
        password_hash=generate_password_hash('test123'),
        rol='operario',
        almacen_id=almacen.id,
        activo=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def jwt_token(app, usuario):
    """Token JWT válido para el usuario de prueba."""
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario.id))
