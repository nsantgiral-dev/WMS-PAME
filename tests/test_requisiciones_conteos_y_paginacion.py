"""
GET /api/traslados/conteos-por-estado + `paginas` en GET /api/traslados/.

La pantalla de Requisiciones (traslados.js) pedía las 6 listas completas en
paralelo (una por estado) solo para pintar 6 badges y mostrar 1 pestaña — el
resto del payload se descartaba sin usarlo. Y no había forma de pedir la
página 2: con más de 30 solicitudes en un estado, el resto quedaba invisible
sin aviso (el backend ya paginaba a 30, solo faltaba `paginas` en la
respuesta y el control en la pantalla).

Este archivo cubre el backend nuevo: el conteo agrupado y el campo `paginas`.
"""
import pytest
from unittest.mock import patch


@pytest.fixture
def usuario_tienda(db, almacen):
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(
        nombre='Tienda Centro', email='tienda-conteos@test.com',
        password_hash=generate_password_hash('test123'),
        rol='tienda', almacen_id=almacen.id, activo=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def jwt_token_tienda(app, usuario_tienda):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario_tienda.id))


@pytest.fixture
def producto_con_unidad(db):
    from app.models.producto import Producto
    p = Producto(
        codigo='PROD-REQ-CONTEO', nombre='Folder Carta',
        codigo_siesa='PROD-REQ-CONTEO', unidad_negocio_id='UN1',
        unidad_medida='UND', activo=True,
    )
    db.session.add(p)
    db.session.commit()
    return p


@pytest.fixture
def usuario_tienda2(db, almacen):
    from app.models.usuario import Usuario
    from werkzeug.security import generate_password_hash
    u = Usuario(
        nombre='Tienda Sur', email='tienda-sur@test.com',
        password_hash=generate_password_hash('test123'),
        rol='tienda', almacen_id=almacen.id, activo=True,
    )
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def jwt_token_tienda2(app, usuario_tienda2):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario_tienda2.id))


def _crear_solicitud(db, usuario, producto, estado, codigo):
    from app.models.traslado import SolicitudTraslado, ItemSolicitudTraslado
    s = SolicitudTraslado(
        codigo=codigo,
        bodega_origen_siesa='NB1',
        bodega_destino_siesa='TC1',
        nombre_punto_venta='Tienda Test',
        estado=estado,
        modo_transferencia='EN_TRANSITO',
        bodega_transito_siesa='TR',
        solicitante_id=usuario.id,
    )
    db.session.add(s)
    db.session.flush()
    item = ItemSolicitudTraslado(
        solicitud_id=s.id,
        producto_id=producto.id,
        producto_codigo_siesa=producto.codigo_siesa,
        cantidad_solicitada=1,
    )
    db.session.add(item)
    db.session.commit()
    return s


class TestConteosPorEstado:
    def test_cuenta_por_estado_correctamente(self, app, db, client, jwt_token_admin,
                                              usuario_tienda, producto_con_unidad):
        _crear_solicitud(db, usuario_tienda, producto_con_unidad, 'ENVIADA', 'ST-C-0001')
        _crear_solicitud(db, usuario_tienda, producto_con_unidad, 'ENVIADA', 'ST-C-0002')
        _crear_solicitud(db, usuario_tienda, producto_con_unidad, 'EN_PICKING', 'ST-C-0003')

        r = client.get('/api/traslados/conteos-por-estado',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200
        conteos = r.get_json()['conteos']
        assert conteos['ENVIADA'] == 2
        assert conteos['EN_PICKING'] == 1
        assert conteos.get('PREPARADO', 0) == 0

    def test_tienda_solo_ve_sus_propios_conteos(self, app, db, client,
                                                 jwt_token_tienda, jwt_token_tienda2,
                                                 usuario_tienda, usuario_tienda2,
                                                 producto_con_unidad):
        _crear_solicitud(db, usuario_tienda, producto_con_unidad, 'ENVIADA', 'ST-T1-0001')
        _crear_solicitud(db, usuario_tienda2, producto_con_unidad, 'ENVIADA', 'ST-T2-0001')
        _crear_solicitud(db, usuario_tienda2, producto_con_unidad, 'ENVIADA', 'ST-T2-0002')

        r1 = client.get('/api/traslados/conteos-por-estado',
                         headers={'Authorization': f'Bearer {jwt_token_tienda}'})
        assert r1.get_json()['conteos']['ENVIADA'] == 1

        r2 = client.get('/api/traslados/conteos-por-estado',
                         headers={'Authorization': f'Bearer {jwt_token_tienda2}'})
        assert r2.get_json()['conteos']['ENVIADA'] == 2

    def test_rol_sin_permiso_da_403(self, app, db, client, almacen):
        from app.models.usuario import Usuario
        from werkzeug.security import generate_password_hash
        from flask_jwt_extended import create_access_token

        u = Usuario(
            nombre='Operario Test', email='op-conteos@test.com',
            password_hash=generate_password_hash('test123'),
            rol='operario', almacen_id=almacen.id, activo=True,
        )
        db.session.add(u)
        db.session.commit()
        with app.app_context():
            token = create_access_token(identity=str(u.id))

        r = client.get('/api/traslados/conteos-por-estado',
                        headers={'Authorization': f'Bearer {token}'})
        assert r.status_code == 403


class TestPaginasEnListado:
    def test_incluye_total_de_paginas(self, app, db, client, jwt_token_admin,
                                       usuario_tienda, producto_con_unidad):
        for i in range(35):
            _crear_solicitud(db, usuario_tienda, producto_con_unidad, 'ENVIADA', f'ST-P-{i:04d}')

        r1 = client.get('/api/traslados/?estado=ENVIADA&page=1',
                         headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d1 = r1.get_json()
        assert d1['total'] == 35
        assert d1['paginas'] == 2
        assert len(d1['solicitudes']) == 30

        r2 = client.get('/api/traslados/?estado=ENVIADA&page=2',
                         headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d2 = r2.get_json()
        assert len(d2['solicitudes']) == 5, (
            'las 5 solicitudes de la página 2 eran exactamente las que antes '
            'quedaban invisibles sin control de página'
        )

    def test_paginas_es_1_sin_resultados(self, app, db, client, jwt_token_admin):
        r = client.get('/api/traslados/?estado=CANCELADA',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d = r.get_json()
        assert d['total'] == 0
        assert d['paginas'] == 1
