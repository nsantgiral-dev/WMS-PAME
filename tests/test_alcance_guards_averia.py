"""Los guards nuevos sí preguntan **de qué punto sos**.

## El hueco que cierran

Medido el 2026-09-17 sobre `app/routes/`: en 333 endpoints, el backend compara
la bodega del usuario contra la del recurso en **8 sitios**, y solo uno acota
una escritura.

Dos de esos huecos son de este trabajo y se cierran acá:

  · **`POST /api/recepcion/<id>/averia`** — lo escribí esta semana sin
    acotamiento, consistente con el resto del repo. Cualquier recepcionista de
    cualquier punto podía declarar avería sobre cualquier recepción.

  · **`GET /api/traslados/pendientes-recepcion`** — el parámetro `?bodega=` le
    ganaba a la bodega del usuario. Y el front lo manda desde `localStorage`,
    así que editarlo en el navegador alcanzaba para leer la cola de recepción
    de otro punto, sin tocar código.

## El admin queda exento, y no es pereza

Medido en producción: de 27 usuarios, **9 no tienen bodega resoluble** — entre
ellos el único `admin`, que no tiene ni `bodega_siesa_id` ni `almacen_id`. Un
guard que falle cerrado sin exención lo dejaría fuera de todo.

Los roles administrativos conservan además el parámetro en el listado: es su
único acceso, porque no tienen bodega propia contra la cual filtrar.
"""
import pytest

from app.models.almacen import Almacen
from app.models.recepcion import RecepcionMercancia
from app.models.usuario import Usuario


def _usuario(db, rol, bodega=None, almacen=None, correo=None):
    u = Usuario(nombre=f'U {rol}', email=correo or f'{rol}-{bodega}-{id(object())}@x.co',
                rol=rol, activo=True, bodega_siesa_id=bodega,
                almacen_id=almacen.id if almacen else None)
    u.set_password('x' * 10)
    db.session.add(u); db.session.commit()
    return u


def _token(app, usuario):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(usuario.id))


def _cab(app, usuario):
    return {'Authorization': f'Bearer {_token(app, usuario)}'}


@pytest.fixture
def recepcion_nb1(db, almacen, producto):
    r = RecepcionMercancia(codigo='REC-ALC-1', numero_oc_siesa='OC-ALC-1',
                           almacen_id=almacen.id, estado='ABIERTA')
    db.session.add(r); db.session.commit()
    return r


# ── Declarar avería: solo sobre el propio punto ────────────────────────────

def test_un_recepcionista_de_otro_punto_no_declara_averia(
        app, client, db, almacen, recepcion_nb1):
    """`almacen` es NB1. Un recepcionista de NS1 no tiene nada que hacer acá."""
    ajeno = _usuario(db, 'recepcionista', bodega='NS1')
    r = client.post(f'/api/recepcion/{recepcion_nb1.id}/averia',
                    json={'producto_id': 1, 'cantidad_averiada': 1},
                    headers=_cab(app, ajeno))
    assert r.status_code == 403, r.get_json()
    assert 'punto de venta' in r.get_json().get('error', '')


def test_el_recepcionista_del_punto_si_puede(app, client, db, almacen, recepcion_nb1):
    """Dirección contraria: el guard no puede dejar afuera a quien corresponde.
    Llega al servicio —falla por otra razón— pero NO con 403."""
    propio = _usuario(db, 'recepcionista', bodega=almacen.bodega_siesa_id)
    r = client.post(f'/api/recepcion/{recepcion_nb1.id}/averia',
                    json={'producto_id': 999999, 'cantidad_averiada': 1},
                    headers=_cab(app, propio))
    assert r.status_code != 403, r.get_json()


def test_el_del_cd_resuelve_por_su_almacen(app, client, db, almacen, recepcion_nb1):
    """Los 5 usuarios de NB1 en producción NO tienen `bodega_siesa_id`: su
    bodega sale del almacén. Si el guard mirara solo el campo directo, los
    dejaría a todos afuera."""
    jefe = _usuario(db, 'jefe_almacen', almacen=almacen)
    assert jefe.bodega_siesa_id is None
    r = client.post(f'/api/recepcion/{recepcion_nb1.id}/averia',
                    json={'producto_id': 999999, 'cantidad_averiada': 1},
                    headers=_cab(app, jefe))
    assert r.status_code != 403, r.get_json()


def test_el_admin_sin_bodega_no_queda_bloqueado(app, client, db, recepcion_nb1):
    """El admin de producción no tiene bodega ni almacén. Sin la exención, un
    guard que falla cerrado lo deja fuera de todo."""
    adm = _usuario(db, 'admin')
    assert adm.bodega_siesa_id is None and adm.almacen_id is None
    r = client.post(f'/api/recepcion/{recepcion_nb1.id}/averia',
                    json={'producto_id': 999999, 'cantidad_averiada': 1},
                    headers=_cab(app, adm))
    assert r.status_code != 403, r.get_json()


def test_una_recepcion_inexistente_da_404_no_403(app, client, db, almacen):
    propio = _usuario(db, 'recepcionista', bodega=almacen.bodega_siesa_id)
    r = client.post('/api/recepcion/999999/averia',
                    json={'producto_id': 1, 'cantidad_averiada': 1},
                    headers=_cab(app, propio))
    assert r.status_code == 404


# ── El listado: el parámetro ya no le gana al usuario ──────────────────────

def test_el_parametro_no_le_gana_al_recepcionista(app, client, db):
    """El agujero concreto: la bodega viajaba desde `localStorage` y el backend
    la obedecía. Pedir otra ya no sirve."""
    from app.models.traslado import SolicitudTraslado, EstadoTraslado
    rec = _usuario(db, 'recepcionista', bodega='NS1')
    s = SolicitudTraslado(codigo='ST-ALC-1', bodega_origen_siesa='NB1',
                          bodega_destino_siesa='NC1',
                          estado=EstadoTraslado.EN_TRANSITO,
                          solicitante_id=rec.id)
    db.session.add(s); db.session.commit()

    r = client.get('/api/traslados/pendientes-recepcion?bodega=NC1',
                   headers=_cab(app, rec))
    assert r.status_code == 200
    codigos = [x['codigo'] for x in r.get_json()['solicitudes']]
    assert 'ST-ALC-1' not in codigos, 'leyó la cola de otro punto con el parámetro'


def test_el_recepcionista_sigue_viendo_lo_suyo(app, client, db):
    """Dirección contraria: acotar no puede dejarlo sin su propia lista."""
    from app.models.traslado import SolicitudTraslado, EstadoTraslado
    rec = _usuario(db, 'recepcionista', bodega='NS1')
    s = SolicitudTraslado(codigo='ST-ALC-2', bodega_origen_siesa='NB1',
                          bodega_destino_siesa='NS1',
                          estado=EstadoTraslado.EN_TRANSITO,
                          solicitante_id=rec.id)
    db.session.add(s); db.session.commit()

    r = client.get('/api/traslados/pendientes-recepcion', headers=_cab(app, rec))
    assert r.status_code == 200
    assert 'ST-ALC-2' in [x['codigo'] for x in r.get_json()['solicitudes']]


def test_un_administrativo_conserva_el_parametro(app, client, db):
    """Es su ÚNICO acceso: no tiene bodega propia contra la cual filtrar."""
    from app.models.traslado import SolicitudTraslado, EstadoTraslado
    adm = _usuario(db, 'admin')
    s = SolicitudTraslado(codigo='ST-ALC-3', bodega_origen_siesa='NB1',
                          bodega_destino_siesa='PC1',
                          estado=EstadoTraslado.EN_TRANSITO,
                          solicitante_id=adm.id)
    db.session.add(s); db.session.commit()

    r = client.get('/api/traslados/pendientes-recepcion?bodega=PC1',
                   headers=_cab(app, adm))
    assert r.status_code == 200
    assert 'ST-ALC-3' in [x['codigo'] for x in r.get_json()['solicitudes']]
