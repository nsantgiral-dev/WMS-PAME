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


def _register_pg_stubs(dbapi_conn, _connection_record):
    """
    Registra stubs SQLite que simulan las funciones de advisory lock de PostgreSQL.
    pg_advisory_xact_lock  → no-op (siempre concede el lock)
    pg_try_advisory_lock   → retorna 1 (éxito)
    pg_advisory_unlock     → retorna 1 (éxito)
    Permite correr los tests en SQLite sin modificar el código de producción.
    """
    dbapi_conn.create_function('pg_advisory_xact_lock', 1, lambda k: None)
    dbapi_conn.create_function('pg_try_advisory_xact_lock', 1, lambda k: 1)
    dbapi_conn.create_function('pg_try_advisory_lock',  1, lambda k: 1)
    dbapi_conn.create_function('pg_advisory_unlock',    1, lambda k: 1)


@pytest.fixture(scope='session')
def app():
    """App Flask con SQLite en memoria — una instancia por sesión de tests."""
    application = create_app()
    application.config.update({
        'TESTING': True,
        'SQLALCHEMY_DATABASE_URI': 'sqlite:///:memory:',
        'JWT_ACCESS_TOKEN_EXPIRES': False,
        # SQLite no soporta pool_size/max_overflow — limpiar opciones de PG
        'SQLALCHEMY_ENGINE_OPTIONS': {
            'pool_pre_ping': True,
        },
    })
    with application.app_context():
        # Registrar stubs de advisory locks de PG en cada conexión SQLite
        from sqlalchemy import event as _sa_event
        _sa_event.listen(_db.engine, 'connect', _register_pg_stubs)
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


@pytest.fixture(autouse=True)
def _sin_hilos_dlq_reales(monkeypatch):
    """
    disparar_dlq_inmediato() no tiene guard de modo_simulacion (a diferencia de
    los demás *_sync_service.iniciar_sync_background) — lanza un hilo daemon
    real e incondicional. Como `app` es session-scoped (un solo SQLite en
    memoria para toda la suite), un hilo que sigue vivo tras terminar un test
    puede ejecutar queries/commits reales mientras el siguiente test trunca
    las tablas en su fixture `db` — causa ObjectDeletedError en modelos
    completamente ajenos al test que aparenta fallar (ej. Producto en
    test_packing_service.py sin relación alguna con DLQ).
    Los tests que sí quieren observar disparar_dlq_inmediato lo patchean
    explícitamente con @patch — ese patch se aplica y se deshace por encima
    de este no-op sin conflicto.
    """
    monkeypatch.setattr(
        'app.services.siesa_job_service.disparar_dlq_inmediato',
        lambda *a, **k: None,
    )


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
    """Token JWT válido para el usuario de prueba (operario)."""
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario.id))


@pytest.fixture
def usuario_admin(db, almacen):
    """Usuario con rol admin — accede a todos los endpoints."""
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(
        nombre='Admin Test',
        email='admin@test.com',
        password_hash=generate_password_hash('test123'),
        rol='admin',
        almacen_id=almacen.id,
        activo=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def jwt_token_admin(app, usuario_admin):
    """Token JWT para usuario admin."""
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario_admin.id))


@pytest.fixture
def usuario_abastecedor(db, almacen):
    """Usuario operario con permiso puede_abastecer=True."""
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(
        nombre='Abastecedor Test',
        email='abast@test.com',
        password_hash=generate_password_hash('test123'),
        rol='operario',
        almacen_id=almacen.id,
        activo=True,
        puede_abastecer=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def jwt_token_abastecedor(app, usuario_abastecedor):
    """Token JWT para usuario abastecedor."""
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario_abastecedor.id))
