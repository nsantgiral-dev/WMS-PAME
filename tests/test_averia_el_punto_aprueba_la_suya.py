"""El administrador del punto puede aprobar SU avería, y nada más que eso.

`Roles.DESPACHO` es quién aprueba un traslado normal, y ahí quien aprueba es
el CD: la tienda pide, el CD decide si manda. Un traslado de averías corre al
revés —el punto manda— así que quien aprueba es el punto. Y ningún punto tiene
un usuario de DESPACHO: medido el 2026-09-17, cada punto satélite tiene un solo
usuario administrativo, con rol `tienda`.

Sin la excepción, la avería se traba en ENVIADA y nadie del punto la destraba.

La excepción NO ensancha `Roles.DESPACHO` —esa tupla decide sobre todos los
traslados de la red— sino que abre una puerta lateral con tres llaves
simultáneas. La mitad de este archivo prueba que abre; la otra mitad, que lo
que tiene que seguir cerrado sigue cerrado. Un detector que solo se prueba en
una dirección prueba la mitad.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models.almacen import Almacen
from app.models.traslado import ClaseTraslado, EstadoTraslado, SolicitudTraslado
from app.models.usuario import Usuario
from app.services.traslado_service import BODEGA_AVERIAS_DESTINO, TrasladoService


def _usuario(db, nombre, rol, bodega=None):
    u = Usuario(nombre=nombre, email=f'{nombre.replace(" ", ".").lower()}@t.co',
                rol=rol, bodega_siesa_id=bodega)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _hdr(app, usuario):
    with app.app_context():
        return {'Authorization': 'Bearer ' + create_access_token(identity=str(usuario.id))}


@pytest.fixture
def producto_ok(db, producto):
    producto.unidad_negocio_id = '001'
    db.session.commit()
    return producto


@pytest.fixture
def cd(db):
    a = Almacen.query.filter_by(bodega_siesa_id=BODEGA_AVERIAS_DESTINO).first()
    if not a:
        a = Almacen(codigo='CD-T', nombre='CD', bodega_siesa_id=BODEGA_AVERIAS_DESTINO)
        db.session.add(a)
        db.session.commit()
    return a


@pytest.fixture
def tienda_nc1(db):
    return _usuario(db, 'Tienda NC1', 'tienda', 'NC1')


def _averia_enviada(tienda, producto, origen='NC1'):
    s = TrasladoService.crear_solicitud(
        solicitante_id=tienda.id, bodega_destino=BODEGA_AVERIAS_DESTINO,
        nombre_punto_venta='Neiva Centro',
        items=[{'producto_id': producto.id, 'cantidad_solicitada': 2,
                'motivo_averia': 'empaque roto'}],
        bodega_origen=origen, clase_traslado=ClaseTraslado.AVERIAS)
    return TrasladoService.enviar_solicitud(s.id)


class TestLaPuertaAbre:

    def test_el_admin_del_punto_aprueba_su_averia(self, app, client, db, cd,
                                                  tienda_nc1, producto_ok):
        s = _averia_enviada(tienda_nc1, producto_ok)
        r = client.post(f'/api/traslados/{s.id}/aprobar',
                        headers=_hdr(app, tienda_nc1),
                        json={'averia_evidencia': 'conté 2, fotos en el grupo'})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['estado'] == EstadoTraslado.EN_PICKING
        assert r.get_json()['averia_evidencia'].startswith('conté 2')

    def test_el_cd_tambien_puede_aprobarla(self, app, client, db, cd,
                                           tienda_nc1, producto_ok):
        """La excepción SUMA, no reemplaza: quien podía antes sigue pudiendo."""
        s = _averia_enviada(tienda_nc1, producto_ok)
        jefe = _usuario(db, 'Jefe CD', 'jefe_almacen', BODEGA_AVERIAS_DESTINO)
        r = client.post(f'/api/traslados/{s.id}/aprobar', headers=_hdr(app, jefe),
                        json={'averia_evidencia': 'lo revisó el CD'})
        assert r.status_code == 200, r.get_json()


class TestLoQueSigueCerrado:
    """Las tres llaves, una por test. Si alguna deja de exigirse, la puerta
    lateral se vuelve un boquete."""

    def test_el_punto_NO_aprueba_un_traslado_normal_suyo(self, app, client, db,
                                                         tienda_nc1, producto_ok):
        """La llave de la clase. Un traslado normal desde un punto lo sigue
        aprobando el CD — si no, la tienda se auto-autoriza a despachar
        mercancía vendible.

        **El origen es NC1 a propósito.** La primera versión de este test
        armaba un traslado hacia NC1, que sale del CD por defecto — así que lo
        rechazaba la llave de la BODEGA y la de la clase nunca se ejercitaba.
        Pasaba por la razón equivocada, y una mutación que borraba la llave de
        la clase lo dejaba en verde. Un traslado NC1→NS1 es una ruta real de
        producción (10 de ellos) y aísla la llave que este test dice probar.
        """
        s = TrasladoService.crear_solicitud(
            solicitante_id=tienda_nc1.id, bodega_destino='NS1',
            nombre_punto_venta='Neiva Sur', bodega_origen='NC1',
            items=[{'producto_id': producto_ok.id, 'cantidad_solicitada': 2}])
        TrasladoService.enviar_solicitud(s.id)
        r = client.post(f'/api/traslados/{s.id}/aprobar',
                        headers=_hdr(app, tienda_nc1), json={})
        assert r.status_code == 403

    def test_el_punto_NO_aprueba_la_averia_de_otro_punto(self, app, client, db, cd,
                                                         tienda_nc1, producto_ok):
        """La llave de la bodega."""
        s = _averia_enviada(tienda_nc1, producto_ok, origen='NC1')
        otra_tienda = _usuario(db, 'Tienda NS1', 'tienda', 'NS1')
        r = client.post(f'/api/traslados/{s.id}/aprobar',
                        headers=_hdr(app, otra_tienda),
                        json={'averia_evidencia': 'yo la miro'})
        assert r.status_code == 403

    @pytest.mark.parametrize('rol', ['picker_traslado', 'packer_traslado',
                                     'operario', 'conductor', 'recepcionista'])
    def test_los_operativos_del_punto_NO_aprueban(self, app, client, db, cd,
                                                  tienda_nc1, producto_ok, rol):
        """La llave del rol. El picker del punto ve la misma pantalla; aprobar
        no es su trabajo."""
        s = _averia_enviada(tienda_nc1, producto_ok)
        op = _usuario(db, f'Op {rol} NC1', rol, 'NC1')
        r = client.post(f'/api/traslados/{s.id}/aprobar', headers=_hdr(app, op),
                        json={'averia_evidencia': 'x'})
        assert r.status_code == 403

    def test_una_tienda_sin_bodega_NO_aprueba(self, app, client, db, cd,
                                              tienda_nc1, producto_ok):
        """`bodega_del_usuario` falla cerrada: sin bodega no se resuelve
        alcance, y sin alcance no se aprueba. En producción hay 9 usuarios sin
        bodega — que caigan del lado abierto sería lo contrario de un guard."""
        s = _averia_enviada(tienda_nc1, producto_ok)
        huerfana = _usuario(db, 'Tienda Sin Bodega', 'tienda', None)
        r = client.post(f'/api/traslados/{s.id}/aprobar',
                        headers=_hdr(app, huerfana),
                        json={'averia_evidencia': 'x'})
        assert r.status_code == 403

    def test_el_punto_NO_dictamina_su_propia_averia(self, app, client, db, cd,
                                                    tienda_nc1, producto_ok):
        """La puerta lateral es SOLO para aprobar. El veredicto del CD sigue
        siendo del CD — si no, el punto declara, aprueba y se absuelve solo."""
        s = _averia_enviada(tienda_nc1, producto_ok)
        client.post(f'/api/traslados/{s.id}/aprobar', headers=_hdr(app, tienda_nc1),
                    json={'averia_evidencia': 'x'})
        s.estado = EstadoTraslado.ENTREGADA
        db.session.commit()
        r = client.post(f'/api/traslados/{s.id}/dictaminar-averia',
                        headers=_hdr(app, tienda_nc1), json={'confirmada': True})
        assert r.status_code == 403


def test_la_excepcion_no_ensancho_roles_despacho():
    """Trinquete. La tentación barata era meter `tienda` en `Roles.DESPACHO`
    y que todo compilara. Esa tupla decide sobre TODOS los traslados de la
    red: con `tienda` adentro, cualquier punto aprobaría, rechazaría y
    despacharía traslados normales de cualquier otro."""
    from app.routes._auth_helpers import Roles
    assert Roles.TIENDA not in Roles.DESPACHO, (
        'Roles.DESPACHO ahora incluye `tienda`. La aprobación de averías por '
        'el punto es una puerta lateral con tres llaves, no un ensanche de '
        'esta tupla.')
