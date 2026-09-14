"""
Conteo Definitivo (CC3) — decisión 2026-09-04: cuando CC1≠CC2, el tercer
conteo que rompe el empate lo hace un supervisor desde una cola dedicada
(GET /api/conteo/definitivos), no un picker automático.

Antes de esto, `_crear_conteo_verificacion` corría la MISMA búsqueda de
"otro picker" para CC2 y CC3 — el único filtro era `!= operario_de_CC2`,
así que en un equipo chico CC3 podía tocarle de vuelta al mismo operario
que hizo CC1, rompiendo el double-blind justo en el conteo que define el
ajuste. Estos tests cubren que CC3 ahora nace sin asignar y que la cola
nueva es ciega (no expone lo que contaron CC1/CC2) y solo la ve gestión.
"""
import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


@pytest.fixture
def mundo(db, almacen):
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    from app.models.inventario import UbicacionProducto
    from app.models.usuario import Usuario

    producto = Producto(codigo='CC3ITEM', nombre='Item CC3', codigo_siesa='CC3ITEM',
                         unidad_negocio_id='001')
    db.session.add(producto)
    db.session.flush()
    ubicacion = Ubicacion(codigo='CC3-UB', almacen_id=almacen.id, tipo_zona='PICKING',
                           stock_minimo=0, stock_maximo=999, secuencia_ruteo=1, activo=True)
    db.session.add(ubicacion)
    db.session.flush()
    db.session.add(UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto.id,
                                      cantidad=100, reservado=0, bloqueado=0))

    def _actor(email, rol):
        u = Usuario(nombre=email, email=email, password_hash=generate_password_hash('x'),
                    rol=rol, puede_picar=True, almacen_id=almacen.id, activo=True)
        db.session.add(u)
        db.session.flush()
        return u

    picker_a = _actor('cc3-picker-a@test.com', 'operario')
    picker_b = _actor('cc3-picker-b@test.com', 'operario')
    supervisor = _actor('cc3-supervisor@test.com', 'supervisor')
    db.session.commit()
    return {
        'almacen_id': almacen.id, 'producto': producto, 'ubicacion': ubicacion,
        'picker_a': picker_a, 'picker_b': picker_b, 'supervisor': supervisor,
    }


def _crear_discordancia(mundo):
    """CC1=90, CC2=95 contra un WMS de 100 — discordantes entre sí -> CC3."""
    from app.services.conteo_service import ConteoService
    from app.models.conteo import SesionConteo

    creado = ConteoService.crear_conteo_manual(mundo['almacen_id'], 'CC3ITEM')
    cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).first()
    ConteoService.obtener_tarea_operario(cc1.id, mundo['picker_a'].id)
    r1 = ConteoService.registrar_conteo(cc1.id, mundo['picker_a'].id, 90)
    assert r1['resultado'] == 'SEGUNDO_CONTEO'
    cc2_id = r1['segundo_conteo_id']
    ConteoService.obtener_tarea_operario(cc2_id, mundo['picker_b'].id)
    r2 = ConteoService.registrar_conteo(cc2_id, mundo['picker_b'].id, 95)
    assert r2['resultado'] == 'TERCER_CONTEO'
    return cc1.id, r2['tercer_conteo_id']


class TestCC3NaceSinAsignar:

    def test_cc3_no_tiene_operario_al_crearse(self, app, db, mundo):
        from app.models.conteo import SesionConteo
        _, cc3_id = _crear_discordancia(mundo)
        cc3 = db.session.get(SesionConteo, cc3_id)
        assert cc3.operario_id is None, (
            'CC3 nació ya asignado — puede haber vuelto a tocarle al mismo '
            'operario que hizo CC1, rompiendo el double-blind.')


class TestColaDeDefinitivos:

    def test_supervisor_ve_el_cc3_pendiente(self, app, db, client, mundo):
        with app.app_context():
            tok = create_access_token(identity=str(mundo['supervisor'].id))
        _, cc3_id = _crear_discordancia(mundo)

        r = client.get('/api/conteo/definitivos', headers=_auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['total'] == 1
        assert body['pendientes'][0]['id'] == cc3_id

    def test_la_vista_sigue_siendo_ciega(self, app, db, client, mundo):
        """Ni existencia_siesa ni las cantidades de CC1/CC2 se exponen."""
        with app.app_context():
            tok = create_access_token(identity=str(mundo['supervisor'].id))
        _crear_discordancia(mundo)

        r = client.get('/api/conteo/definitivos', headers=_auth(tok))
        fila = r.get_json()['pendientes'][0]
        for campo_prohibido in ('existencia_siesa', 'cantidad_fisica', 'diferencia'):
            assert campo_prohibido not in fila, (
                f'{campo_prohibido} se filtró a la cola — deja de ser ciego')

    def test_un_operario_normal_no_puede_ver_la_cola(self, app, db, client, mundo):
        with app.app_context():
            tok = create_access_token(identity=str(mundo['picker_a'].id))
        _crear_discordancia(mundo)

        r = client.get('/api/conteo/definitivos', headers=_auth(tok))
        assert r.status_code == 403


class TestFlujoCompletoDelSupervisor:

    def test_autoasignacion_escaneo_y_confirmacion(self, app, db, client, mundo):
        from app.models.conteo import SesionConteo

        with app.app_context():
            tok = create_access_token(identity=str(mundo['supervisor'].id))
        cc1_id, cc3_id = _crear_discordancia(mundo)

        # Autoasignación al abrir la tarea.
        r = client.get(f'/api/conteo/{cc3_id}/tarea', headers=_auth(tok))
        assert r.status_code == 200, r.get_json()
        cc3 = db.session.get(SesionConteo, cc3_id)
        assert cc3.operario_id == mundo['supervisor'].id

        # Escaneo válido incrementa cantidad_fisica.
        r = client.post('/api/mobile/escanear', json={
            'tarea_id': cc3_id, 'tipo': 'CONTEO', 'codigo': 'CC3ITEM', 'cantidad': 1,
        }, headers=_auth(tok))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['cantidad_contada'] == 1

        # Confirmar propaga el resultado a la raíz (CC1), no a CC3.
        r = client.post('/api/mobile/confirmar', json={
            'tarea_id': cc3_id, 'tipo': 'CONTEO', 'items_escaneados': [],
        }, headers=_auth(tok))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['resultado'] == 'DESCUADRE'
        assert body['raiz_id'] == cc1_id

        # Sale de la cola una vez resuelto.
        r = client.get('/api/conteo/definitivos', headers=_auth(tok))
        assert r.get_json()['total'] == 0
