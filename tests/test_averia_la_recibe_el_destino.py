"""Quien confirma que algo llegó tiene que estar donde llegó.

El guard de `confirmar-recepcion` decía `s.solicitante_id != usuario_id`, y
funcionaba **por accidente**: hasta hoy todo traslado lo pedía su propio
destinatario, así que «sos el solicitante» y «estás en el destino» eran la
misma persona. Medido el 2026-09-18 sobre los 174 traslados de producción: en
166 el solicitante es del destino, y los 8 restantes los creó el Administrador,
que entra por el guard de admin. La regla no se apoyaba en un permiso: se
apoyaba en una coincidencia.

El traslado de averías es el primero que la rompe — lo pide el punto y llega al
CD. Con la regla vieja, el mismo punto que declaró la avería podía confirmar su
recepción en el CD, disparar el ETS 173079 y saltarse entero el TERCER momento
de validación del proceso («en NB1 recepción valida la cantidad») sin que nadie
del CD hubiera contado nada.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models.almacen import Almacen
from app.models.traslado import (ClaseTraslado, EstadoTraslado,
                                 ItemSolicitudTraslado, SolicitudTraslado)
from app.models.usuario import Usuario
from app.services.traslado_service import BODEGA_AVERIAS_DESTINO


def _usuario(db, nombre, rol, bodega=None):
    u = Usuario(nombre=nombre, email=f'{nombre.replace(" ", ".").lower()}@t.co',
                rol=rol, bodega_siesa_id=bodega)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _contado(s):
    """Lo que el receptor contó. El endpoint lo exige —«la recepción exige las
    cantidades contadas»— y ESO es el tercer momento de validación del
    proceso: no basta con apretar un botón, hay que decir cuánto llegó."""
    return {'items_recibidos': [{'id': i.id, 'cantidad_recibida': i.cantidad_enviada}
                                for i in s.items]}


def _hdr(app, u):
    with app.app_context():
        return {'Authorization': 'Bearer ' + create_access_token(identity=str(u.id))}


@pytest.fixture
def cd(db):
    a = Almacen.query.filter_by(bodega_siesa_id=BODEGA_AVERIAS_DESTINO).first()
    if not a:
        a = Almacen(codigo='CD-T', nombre='CD', bodega_siesa_id=BODEGA_AVERIAS_DESTINO)
        db.session.add(a)
        db.session.commit()
    return a


@pytest.fixture
def tienda(db):
    return _usuario(db, 'Tienda NC1', 'tienda', 'NC1')


def _traslado(db, solicitante, producto, *, clase, origen, destino):
    s = SolicitudTraslado(
        codigo=f'ST-{clase}-{origen}-{destino}',
        bodega_origen_siesa=origen, bodega_destino_siesa=destino,
        estado=EstadoTraslado.EN_TRANSITO, solicitante_id=solicitante.id,
        clase_traslado=clase,
        **({'averia_evidencia': 'x'} if clase == ClaseTraslado.AVERIAS else {}))
    db.session.add(s)
    db.session.flush()
    db.session.add(ItemSolicitudTraslado(
        solicitud_id=s.id, producto_id=producto.id,
        producto_codigo_siesa=producto.codigo_siesa or producto.codigo,
        cantidad_solicitada=2, cantidad_aprobada=2, cantidad_enviada=2))
    db.session.commit()
    return s


class TestElQueDeclaraNoRecibe:

    def test_el_punto_NO_confirma_la_recepcion_de_su_averia_en_el_CD(
            self, app, client, db, cd, tienda, producto):
        """LA regresión. El punto es el solicitante, pero la mercancía llegó al
        CD: confirmarla desde el punto dispara el ETS sin que nadie del CD la
        haya contado, y el tercer momento de validación desaparece."""
        s = _traslado(db, tienda, producto, clase=ClaseTraslado.AVERIAS,
                      origen='NC1', destino=BODEGA_AVERIAS_DESTINO)
        r = client.post(f'/api/traslados/{s.id}/recibir',
                        headers=_hdr(app, tienda), json={})
        assert r.status_code == 403, r.get_json()
        assert BODEGA_AVERIAS_DESTINO in r.get_json()['error']
        db.session.refresh(s)
        assert s.estado == EstadoTraslado.EN_TRANSITO, 'el ETS se disparó igual'

    def test_el_recepcionista_del_CD_si_la_confirma(self, app, client, db, cd,
                                                    tienda, producto):
        """La otra mitad: apretar el guard no puede dejar la avería sin quien
        la reciba. El recepcionista del CD es justamente el actor del momento 3."""
        s = _traslado(db, tienda, producto, clase=ClaseTraslado.AVERIAS,
                      origen='NC1', destino=BODEGA_AVERIAS_DESTINO)
        rec = _usuario(db, 'Recepcionista CD', 'recepcionista',
                       BODEGA_AVERIAS_DESTINO)
        r = client.post(f'/api/traslados/{s.id}/recibir',
                        headers=_hdr(app, rec), json=_contado(s))
        assert r.status_code == 200, r.get_json()

    def test_un_supervisor_del_CD_tambien(self, app, client, db, cd, tienda,
                                          producto):
        s = _traslado(db, tienda, producto, clase=ClaseTraslado.AVERIAS,
                      origen='NC1', destino=BODEGA_AVERIAS_DESTINO)
        sup = _usuario(db, 'Supervisor CD', 'supervisor', BODEGA_AVERIAS_DESTINO)
        r = client.post(f'/api/traslados/{s.id}/recibir',
                        headers=_hdr(app, sup), json=_contado(s))
        assert r.status_code == 200, r.get_json()


class TestLoQueYaFuncionabaSigueFuncionando:
    """166 de los 174 traslados de producción son de esta forma. Si esto se
    rompe, se rompe la operación entera de traslados."""

    def test_la_tienda_destino_confirma_su_traslado_normal(self, app, client,
                                                           db, tienda, producto):
        """El caso de siempre: el CD manda al punto y el punto recibe. Acá
        solicitante y destino coinciden, que es de donde venía la confusión."""
        s = _traslado(db, tienda, producto, clase=ClaseTraslado.NORMAL,
                      origen='NB1', destino='NC1')
        r = client.post(f'/api/traslados/{s.id}/recibir',
                        headers=_hdr(app, tienda), json=_contado(s))
        assert r.status_code == 200, r.get_json()

    def test_una_tienda_NO_confirma_el_traslado_de_otro_punto(self, app, client,
                                                              db, tienda,
                                                              producto):
        s = _traslado(db, tienda, producto, clase=ClaseTraslado.NORMAL,
                      origen='NB1', destino='NS1')
        r = client.post(f'/api/traslados/{s.id}/recibir',
                        headers=_hdr(app, tienda), json={})
        assert r.status_code == 403

    def test_el_admin_sin_bodega_sigue_pudiendo(self, app, client, db, tienda,
                                                producto):
        """Los 8 traslados de producción cuyo solicitante no es del destino los
        creó el Administrador, que no tiene bodega. Si el arreglo lo dejara
        afuera, rompería el único caso real que existía."""
        s = _traslado(db, tienda, producto, clase=ClaseTraslado.NORMAL,
                      origen='NB1', destino='NC1')
        admin = _usuario(db, 'Administrador', 'admin', None)
        r = client.post(f'/api/traslados/{s.id}/recibir',
                        headers=_hdr(app, admin), json=_contado(s))
        assert r.status_code == 200, r.get_json()

    def test_un_usuario_sin_bodega_y_sin_rol_alto_no_pasa(self, app, client, db,
                                                          tienda, producto):
        """`bodega_del_usuario` falla cerrada: sin bodega no hay alcance, y sin
        alcance no se confirma. En producción hay 9 usuarios sin bodega."""
        s = _traslado(db, tienda, producto, clase=ClaseTraslado.NORMAL,
                      origen='NB1', destino='NC1')
        huerfano = _usuario(db, 'Picker Suelto', 'picker_traslado', None)
        r = client.post(f'/api/traslados/{s.id}/recibir',
                        headers=_hdr(app, huerfano), json={})
        assert r.status_code == 403
