"""La cola del cuarto momento: averías que llegaron y nadie dictaminó.

Existe como endpoint propio y no como filtro sobre la lista general por dos
razones medidas:

· **La lista general pagina de a 30 y el front nunca manda `page`.** Una avería
  sin dictaminar entre traslados ENTREGADA dejaba de ser alcanzable en cuanto
  hubiera 30 entregados más nuevos que ella. La cola de una decisión pendiente
  no puede depender de cuántas cosas terminaron después.

· **«Historial» es el lugar equivocado.** Ahí se mira lo que ya pasó; esto es lo
  que falta hacer, y antes de ubicar la mercancía.
"""
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from app.models.almacen import Almacen
from app.models.traslado import ClaseTraslado, EstadoTraslado, SolicitudTraslado
from app.models.usuario import Usuario
from app.services.traslado_service import BODEGA_AVERIAS_DESTINO

RUTA = '/api/traslados/averias-pendientes'


def _usuario(db, nombre, rol, bodega=None):
    u = Usuario(nombre=nombre, email=f'{nombre.replace(" ", ".").lower()}@t.co',
                rol=rol, bodega_siesa_id=bodega)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


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


@pytest.fixture
def supervisor(db, cd):
    return _usuario(db, 'Supervisor CD', 'supervisor', BODEGA_AVERIAS_DESTINO)


def _sol(db, tienda, codigo, *, clase=ClaseTraslado.AVERIAS,
         estado=EstadoTraslado.ENTREGADA, veredicto=None, dias=1):
    kw = {'averia_evidencia': 'x'} if clase == ClaseTraslado.AVERIAS else {}
    if clase == ClaseTraslado.AVERIAS and veredicto is not None:
        kw['averia_veredicto'] = veredicto
        kw['averia_veredicto_at'] = datetime.utcnow()
    s = SolicitudTraslado(
        codigo=codigo, bodega_origen_siesa='NC1',
        bodega_destino_siesa=BODEGA_AVERIAS_DESTINO, estado=estado,
        solicitante_id=tienda.id, clase_traslado=clase,
        fecha_entrega=datetime.utcnow() - timedelta(days=dias), **kw)
    db.session.add(s)
    db.session.commit()
    return s


class TestQueTrae:

    def test_trae_las_que_esperan_dictamen(self, app, client, db, cd, tienda,
                                           supervisor):
        _sol(db, tienda, 'ST-P1')
        r = client.get(RUTA, headers=_hdr(app, supervisor))
        assert r.status_code == 200
        d = r.get_json()
        assert d['total'] == 1
        assert d['solicitudes'][0]['codigo'] == 'ST-P1'

    def test_la_mas_vieja_va_primero(self, app, client, db, cd, tienda,
                                     supervisor):
        """La que lleva más tiempo contada como vendible es la que más urge."""
        _sol(db, tienda, 'ST-NUEVA', dias=1)
        _sol(db, tienda, 'ST-VIEJA', dias=20)
        d = client.get(RUTA, headers=_hdr(app, supervisor)).get_json()
        assert [s['codigo'] for s in d['solicitudes']] == ['ST-VIEJA', 'ST-NUEVA']

    def test_no_pagina(self, app, client, db, cd, tienda, supervisor):
        """EL test de este archivo.

        La lista general devuelve 30 por página y el front nunca manda `page`.
        Con 35 pendientes, cinco quedarían invisibles — y son justamente las
        más nuevas o las más viejas según el orden, o sea que el corte lo
        decide el azar.
        """
        for i in range(35):
            _sol(db, tienda, f'ST-M{i:02d}', dias=i + 1)
        d = client.get(RUTA, headers=_hdr(app, supervisor)).get_json()
        assert d['total'] == 35
        assert len(d['solicitudes']) == 35, 'la cola se está paginando'


class TestQueNOTrae:
    """Un aviso que aparece donde no corresponde se vuelve ruido, y el canal
    deja de leerse."""

    def test_una_averia_ya_dictaminada_no_aparece(self, app, client, db, cd,
                                                  tienda, supervisor):
        _sol(db, tienda, 'ST-OK', veredicto=True)
        assert client.get(RUTA, headers=_hdr(app, supervisor)).get_json()['total'] == 0

    def test_una_dictaminada_NO_AVERIADA_tampoco(self, app, client, db, cd,
                                                 tienda, supervisor):
        """`False` es una decisión tomada. Con `.isnot(True)` en vez de
        `is_(None)`, esta cola nunca se vaciaría."""
        _sol(db, tienda, 'ST-NO', veredicto=False)
        assert client.get(RUTA, headers=_hdr(app, supervisor)).get_json()['total'] == 0

    def test_un_traslado_normal_entregado_no_aparece(self, app, client, db, cd,
                                                     tienda, supervisor):
        """Son 174 en producción. Si entraran, la cola sería inleíble."""
        _sol(db, tienda, 'ST-N1', clase=ClaseTraslado.NORMAL)
        assert client.get(RUTA, headers=_hdr(app, supervisor)).get_json()['total'] == 0

    @pytest.mark.parametrize('estado', [
        EstadoTraslado.EN_TRANSITO, EstadoTraslado.EN_PICKING,
        EstadoTraslado.RECHAZADA, EstadoTraslado.CANCELADA,
        EstadoTraslado.REVERTIDA,
    ])
    def test_una_averia_que_no_llego_no_aparece(self, app, client, db, cd,
                                                tienda, supervisor, estado):
        """Pedir un dictamen sobre mercancía que no llegó —o que se canceló—
        es pedir una decisión imposible."""
        _sol(db, tienda, f'ST-E-{estado}', estado=estado)
        assert client.get(RUTA, headers=_hdr(app, supervisor)).get_json()['total'] == 0

    def test_sin_pendientes_devuelve_cero_y_no_falla(self, app, client, db, cd,
                                                     supervisor):
        """Es el estado de producción HOY: cero traslados de clase AVERIAS."""
        r = client.get(RUTA, headers=_hdr(app, supervisor))
        assert r.status_code == 200
        assert r.get_json() == {'solicitudes': [], 'total': 0}


class TestQuienLaVe:

    @pytest.mark.parametrize('rol', ['tienda', 'recepcionista', 'operario',
                                     'picker_traslado', 'conductor'])
    def test_los_roles_operativos_no_ven_la_cola(self, app, client, db, cd,
                                                 tienda, rol):
        _sol(db, tienda, f'ST-R-{rol}')
        u = _usuario(db, f'Op {rol}', rol, BODEGA_AVERIAS_DESTINO)
        assert client.get(RUTA, headers=_hdr(app, u)).status_code == 403

    @pytest.mark.parametrize('rol', ['admin', 'supervisor', 'jefe_almacen'])
    def test_quien_dictamina_si_la_ve(self, app, client, db, cd, tienda, rol):
        """Quien no puede ver la cola no puede vaciarla: los roles de esta
        pantalla tienen que ser los mismos que los del dictamen."""
        _sol(db, tienda, f'ST-V-{rol}')
        u = _usuario(db, f'Jefe {rol}', rol, BODEGA_AVERIAS_DESTINO)
        assert client.get(RUTA, headers=_hdr(app, u)).status_code == 200


def test_los_roles_de_la_cola_son_los_del_dictamen():
    """Trinquete. Si divergieran, habría gente que ve averías que no puede
    dictaminar, o —peor— gente que puede dictaminar y no las encuentra."""
    from app.routes._auth_helpers import Roles
    from app.services.traslado_service import _ROLES_DICTAMEN_AVERIA
    assert set(_ROLES_DICTAMEN_AVERIA) == set(Roles.SUPERVISION)
