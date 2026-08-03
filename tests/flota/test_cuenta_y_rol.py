"""
Lo que desbloquea al conductor y lo que limita a Yesid.

Dos cosas chicas que el sistema no tenía y que hacen que el papel y el software
digan lo mismo.
"""
import pytest

from app.routes._auth_helpers import Roles
from app.services.ruta_service import ConflictError, RutaService


@pytest.fixture
def carlos(db):
    """Un conductor que YA existe, con historia, y sin cuenta.

    Es el caso de Carlos Pérez en producción: 5 rutas a su nombre y sin poder
    entrar a la app.
    """
    from app.models.conductor import Conductor

    c = Conductor(nombre='Carlos Pérez', cedula='1234567890', activo=True)
    db.session.add(c)
    db.session.commit()
    return c


class TestVincularCuentaAConductorExistente:

    def test_le_da_cuenta_sin_crear_otra_fila(self, db, carlos):
        """El punto entero: una sola ficha de conductor, con su historia."""
        from app.models.conductor import Conductor

        antes = Conductor.query.count()
        conductor, usuario = RutaService.crear_cuenta_para_conductor(
            carlos.id, 'carlos@papeleria.com', 'clave123')

        assert Conductor.query.count() == antes, 'se duplicó la ficha del conductor'
        assert conductor.id == carlos.id
        assert conductor.usuario_id == usuario.id
        assert usuario.rol == 'conductor'

    def test_no_toca_nada_mas_de_la_ficha(self, db, carlos):
        """Solo escribe `usuario_id`. Ni el nombre, ni la cédula."""
        RutaService.crear_cuenta_para_conductor(carlos.id, 'c@x.com', 'clave123')
        db.session.refresh(carlos)
        assert carlos.nombre == 'Carlos Pérez'
        assert carlos.cedula == '1234567890'
        assert carlos.activo is True

    def test_conserva_su_historia_de_rutas(self, db, carlos):
        """La razón por la que no se puede crear uno nuevo: las rutas viejas
        quedarían en una fila y la cuenta en otra."""
        from app.models.ruta_despacho import RutaDespacho

        db.session.add(RutaDespacho(conductor_id=carlos.id, tipo_ruta='Urbana',
                                    estado='ENTREGADA'))
        db.session.commit()
        RutaService.crear_cuenta_para_conductor(carlos.id, 'c@x.com', 'clave123')
        assert RutaDespacho.query.filter_by(conductor_id=carlos.id).count() == 1

    def test_si_ya_tiene_cuenta_falla_ruidosamente(self, db, carlos):
        """Sobrescribir el vínculo dejaría al anterior sin acceso, sin aviso."""
        RutaService.crear_cuenta_para_conductor(carlos.id, 'c1@x.com', 'clave123')
        with pytest.raises(ConflictError, match='ya tiene cuenta'):
            RutaService.crear_cuenta_para_conductor(carlos.id, 'c2@x.com', 'clave123')

    def test_un_email_repetido_no_pasa(self, db, carlos):
        from app.models.usuario import Usuario

        u = Usuario(email='ocupado@x.com', nombre='Otro', rol='operario', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        with pytest.raises(ConflictError):
            RutaService.crear_cuenta_para_conductor(carlos.id, 'ocupado@x.com', 'clave123')

    def test_sin_email_o_sin_clave_no_pasa(self, db, carlos):
        with pytest.raises(ValueError):
            RutaService.crear_cuenta_para_conductor(carlos.id, '', 'clave123')
        with pytest.raises(ValueError):
            RutaService.crear_cuenta_para_conductor(carlos.id, 'c@x.com', '')

    def test_un_conductor_que_no_existe_es_404(self, db):
        with pytest.raises(LookupError):
            RutaService.crear_cuenta_para_conductor(999999, 'c@x.com', 'clave123')


class TestRolControlFlota:
    """FLO-PR-01: ve el tablero, no aprueba nada.

    Si el rol entrara por `_es_gestion()`, el procedimiento diría "no aprueba
    gastos" y el sistema le dejaría aprobarlos. Ese desfase entre el papel y el
    software es lo que vuelve decorativo un procedimiento.
    """

    def test_no_esta_en_gestion(self):
        assert Roles.CONTROL_FLOTA not in Roles.GESTION

    def test_no_esta_en_ninguno_de_los_grupos_que_autorizan(self):
        """Ni almacén, ni despacho, ni compras. Solo lectura de flota."""
        for grupo in (Roles.GESTION, Roles.ALMACEN, Roles.DESPACHO,
                      Roles.SUPERVISION, Roles.LEAD, Roles.COMPRAS_ROLES,
                      Roles.PACKING_ROLES, Roles.RECEPCION_ROLES):
            assert Roles.CONTROL_FLOTA not in grupo

    def _usuario(self, db, rol):
        from app.models.usuario import Usuario

        u = Usuario(email=f'{rol}@x.com', nombre=rol, rol=rol, activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
        return u

    def _token(self, app, usuario):
        from flask_jwt_extended import create_access_token

        with app.app_context():
            return create_access_token(identity=str(usuario.id))

    def test_control_flota_lee_el_tablero(self, app, db, client):
        u = self._usuario(db, 'control_flota')
        r = client.get('/flota/health',
                       headers={'Authorization': f'Bearer {self._token(app, u)}'})
        assert r.status_code == 200

    def test_un_operario_no(self, app, db, client):
        u = self._usuario(db, 'operario')
        r = client.get('/flota/health',
                       headers={'Authorization': f'Bearer {self._token(app, u)}'})
        assert r.status_code == 403

    def test_gestion_sigue_entrando(self, app, db, client, jwt_token_admin):
        r = client.get('/flota/health',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200

    def test_el_backend_acepta_el_rol_al_crear_usuario(self):
        """La lista de roles válidos vive en OTRO archivo que el enum.

        `Roles` está en `_auth_helpers.py` y `_ROLES_VALIDOS` en `auth.py`.
        Agregar el rol a uno y no al otro deja la opción visible en el
        formulario y el backend rechazando con "Rol inválido" — el usuario ve
        una opción que no funciona, y el error no dice que falte registrarlo.
        """
        from app.routes.auth import _ROLES_VALIDOS

        assert Roles.CONTROL_FLOTA in _ROLES_VALIDOS

    def test_toda_opcion_del_selector_es_un_rol_valido(self):
        """El trinquete general, no el caso puntual.

        Cualquier opción que aparezca en el formulario tiene que ser aceptada
        por el backend. Si no, alguien la elige y recibe un error que no explica
        nada.
        """
        import os
        import re

        from app.routes.auth import _ROLES_VALIDOS

        pwa = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), 'app', 'static', 'pwa')
        with open(os.path.join(pwa, 'app.js'), encoding='utf-8') as f:
            app_js = f.read()
        # El bloque del selector de rol del formulario de usuario.
        i = app_js.index("<select id=\"u-rol\"")
        bloque = app_js[i:app_js.index('</select>', i)]
        opciones = set(re.findall(r'<option value="([a-z_]+)"', bloque))
        invalidas = sorted(opciones - set(_ROLES_VALIDOS))
        assert not invalidas, (
            f'\nOpciones del formulario que el backend rechaza: {invalidas}\n'
            'Quien las elija recibe "Rol inválido" sin saber que falta '
            'registrarlas en _ROLES_VALIDOS.'
        )

    def test_el_rol_se_puede_asignar_desde_la_pantalla(self):
        """Un rol que no aparece en el formulario no se le puede dar a nadie.

        Es el patrón función-sin-caller aplicado a la configuración: la
        capacidad existe en el backend y no hay gesto que la encienda.
        """
        import os

        pwa = os.path.join(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))), 'app', 'static', 'pwa')
        with open(os.path.join(pwa, 'app.js'), encoding='utf-8') as f:
            app_js = f.read()
        assert 'value="control_flota"' in app_js, (
            'El rol no está en el selector de usuarios: existe en el backend y '
            'no hay forma de asignárselo a Yesid.'
        )
