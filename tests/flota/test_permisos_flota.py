"""
Quién puede escribir en flota. Hasta el 2026-08-03: cualquiera con sesión.

Todos los endpoints del módulo llevaban `@jwt_required()` y **nada más**. Un
usuario de tienda o un empacador podía sobrescribir el `km_inicial` de un
vehículo —el ancla contra la que se valida todo el histórico de odómetro—,
declarar un SOAT vigente con la fecha que quisiera, o tomar la custodia de
cualquier camión.

Lo encontró el review automático. **Ni los tests ni una tarde de uso real lo
habrían encontrado:** nadie intenta sobrescribir una ficha desde una cuenta de
tienda, y ningún test afirmaba que no se pudiera. Es la contracara exacta de la
lección del mismo día — la persona encuentra lo que nadie pensó en afirmar; el
review encuentra lo que nadie iba a intentar.

La ironía que lo vuelve evidente: `/flota/health`, que es **solo lectura**, sí
tenía control de rol desde el primer día.
"""
import ast
from pathlib import Path

import pytest

from app.routes._auth_helpers import Roles
from flota.api._permisos import MAESTROS_FLOTA

_API = Path(__file__).resolve().parents[2] / 'flota' / 'api'
_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


def _token(app, db, rol, email=None):
    from flask_jwt_extended import create_access_token

    from app.models.usuario import Usuario

    u = Usuario(email=email or f'{rol}-perm@x.com', nombre=rol, rol=rol, activo=True)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    with app.app_context():
        return create_access_token(identity=str(u.id))


@pytest.fixture
def vehiculo(db):
    from app.models.vehiculo import Vehiculo

    v = Vehiculo(placa='PRM100', tipo='Turbo', activo=True)
    db.session.add(v)
    db.session.commit()
    return v.placa


# ── TRINQUETE: ninguna ruta puede quedar solo con @jwt_required ─────────────

class TestTodaRutaDeFlotaTieneControlDeRol:
    """El guard general, no los tres casos que encontró el review.

    Un endpoint nuevo que se agregue mañana con solo `@jwt_required()` falla
    acá. Sin esto, el arreglo cubre lo que ya existe y el próximo repite el
    agujero — que es como aparecieron estos.
    """

    @staticmethod
    def _rutas_sin_exige():
        malas = []
        for archivo in sorted(_API.glob('*.py')):
            arbol = ast.parse(archivo.read_text(encoding='utf-8'))
            for nodo in ast.walk(arbol):
                if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                nombres = []
                for d in nodo.decorator_list:
                    f = d.func if isinstance(d, ast.Call) else d
                    nombres.append(
                        f.attr if isinstance(f, ast.Attribute) else
                        getattr(f, 'id', ''))
                es_ruta = 'route' in nombres
                # `exige_secreto` es para lo que llama una MÁQUINA: un webhook
                # de proveedor no puede llevar JWT porque quien lo invoca no es
                # un usuario. No afloja el trinquete — una ruta sin ninguno de
                # los dos sigue fallando, y el guard de secreto nace cerrado
                # (503 sin la variable) en vez de abierto.
                if es_ruta and not {'exige', 'exige_secreto'} & set(nombres):
                    # `health` usa su propio guard, anterior a este módulo.
                    if '_es_control_flota' in ast.dump(nodo):
                        continue
                    malas.append(f'{archivo.name}:{nodo.name}')
        return malas

    def test_ninguna_ruta_queda_solo_con_sesion(self):
        malas = self._rutas_sin_exige()
        assert not malas, (
            '\nRutas de flota sin control de rol — cualquier usuario con sesión '
            'las puede llamar:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nAgregar @exige(...) debajo de @jwt_required().')

    def test_el_extractor_encuentra_rutas(self):
        """Si el AST deja de encontrarlas, el test de arriba pasa vacío."""
        rutas = sum(
            1
            for archivo in _API.glob('*.py')
            for nodo in ast.walk(ast.parse(archivo.read_text(encoding='utf-8')))
            if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any('route' in ast.dump(d) for d in nodo.decorator_list)
        )
        assert rutas >= 10, f'solo se detectaron {rutas} rutas'


# ── Los tres agujeros concretos que encontró el review ─────────────────────

class TestLosMaestrosNoLosToquiaCualquiera:

    def test_tienda_no_puede_sobrescribir_la_ficha(self, app, db, client, vehiculo):
        """`km_inicial` es el ancla de todo el módulo.

        Sobrescribirlo corrompe la validación de cada lectura posterior, y no
        hay forma de detectarlo después: el histórico queda coherente con el
        ancla equivocada.
        """
        t = _token(app, db, 'tienda')
        r = client.put(f'/flota/vehiculo/{vehiculo}/ficha',
                       json={'km_inicial': 1, 'posiciones_llanta': 6},
                       headers=_auth(t))
        assert r.status_code == 403

    def test_un_empacador_no_puede_declarar_un_soat_vigente(
            self, app, db, client, vehiculo):
        """Un documento de compliance declarado por quien no lo verificó es
        evidencia falsa frente a una autoridad de tránsito."""
        t = _token(app, db, 'empacador')
        r = client.post(f'/flota/vehiculo/{vehiculo}/documentos',
                        json={'tipo': 'soat', 'estado': 'vigente',
                              'numero': 'X', 'entidad': 'Y',
                              'fecha_expedicion': '2026-01-01',
                              'fecha_vencimiento': '2027-01-01'},
                        headers=_auth(t))
        assert r.status_code == 403

    def test_el_conductor_tampoco_toca_los_maestros(self, app, db, client, vehiculo):
        """Y este es el que no es obvio.

        El conductor opera el turno, pero la ficha es levantamiento de campo
        (FLO-PR-01, trabajo de control de flota). Dejarle escribir `km_inicial`
        sería darle la llave del dato que después lo respalda a él mismo.
        """
        t = _token(app, db, 'conductor')
        r = client.put(f'/flota/vehiculo/{vehiculo}/ficha',
                       json={'km_inicial': 1, 'posiciones_llanta': 6},
                       headers=_auth(t))
        assert r.status_code == 403

    def test_control_flota_si_puede_cargar_la_ficha(self, app, db, client, vehiculo):
        """Es su trabajo: si no puede, el procedimiento no se puede ejecutar."""
        t = _token(app, db, 'control_flota')
        r = client.get(f'/flota/vehiculo/{vehiculo}/ficha', headers=_auth(t))
        assert r.status_code != 403

    def test_control_flota_no_esta_en_gestion_pero_si_en_maestros(self):
        assert Roles.CONTROL_FLOTA not in Roles.GESTION
        assert Roles.CONTROL_FLOTA in MAESTROS_FLOTA
        assert Roles.CONDUCTOR not in MAESTROS_FLOTA


class TestLaOperacionDelTurnoSigueAbiertaAlConductor:
    """El arreglo no puede haber cerrado la puerta principal.

    Si el conductor no puede recibir su turno, el módulo entero deja de
    funcionar y nadie lo nota hasta las 5 a.m.
    """

    @pytest.mark.parametrize('ruta', [
        '/flota/custodia/activa/PRM100',
        '/flota/custodia/fuera-de-sede',
        '/flota/custodia/cierres-forzados',
    ])
    def test_el_conductor_lee_lo_de_su_turno(self, app, db, client, vehiculo, ruta):
        t = _token(app, db, 'conductor')
        assert client.get(ruta, headers=_auth(t)).status_code != 403

    def test_el_conductor_registra_odometro(self, app, db, client, vehiculo):
        t = _token(app, db, 'conductor')
        r = client.post('/flota/odometro',
                        json={'placa': vehiculo, 'valor_km': 100,
                              'origen': 'tanqueo'},
                        headers=_auth(t))
        assert r.status_code != 403

    def test_un_operario_ajeno_a_flota_no_entra(self, app, db, client, vehiculo):
        t = _token(app, db, 'operario')
        r = client.get(f'/flota/custodia/activa/{vehiculo}', headers=_auth(t))
        assert r.status_code == 403

    def test_el_403_dice_que_hace_falta(self, app, db, client, vehiculo):
        """Un "sin permiso" pelado termina en un WhatsApp al desarrollador."""
        t = _token(app, db, 'operario')
        cuerpo = client.get(f'/flota/custodia/activa/{vehiculo}',
                            headers=_auth(t)).get_json()
        assert cuerpo['tu_rol'] == 'operario'
        assert cuerpo['roles_permitidos']


class TestElGuardDeSecretoNoEsUnaPuertaAbierta:
    """`exige_secreto` existe para webhooks, no para saltarse el trinquete.

    Si fuera un decorador que solo marca la ruta como "exenta", habría cambiado
    un endpoint sin control por uno con la apariencia de control — que es peor,
    porque el trinquete queda en verde.
    """

    def test_sin_la_variable_responde_503_y_no_ejecuta(self, client, monkeypatch):
        monkeypatch.delenv('FLOTA_AVISO_WEBHOOK_TOKEN', raising=False)
        r = client.post('/flota/avisos/entrega',
                        json={'payload': {'gsId': 'x', 'type': 'delivered'}})
        assert r.status_code == 503, (
            'nace cerrado: un webhook que se abre porque falta configuración '
            'invierte la regla 10 — lo peligroso pasaría cuando alguien NO hizo algo')

    def test_sin_token_en_la_url_rechaza(self, client, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        assert client.post('/flota/avisos/entrega', json={}).status_code == 403

    def test_con_token_equivocado_rechaza(self, client, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        assert client.post('/flota/avisos/entrega?token=x',
                           json={}).status_code == 403

    def test_con_el_token_correcto_entra(self, app, db, client, monkeypatch):
        monkeypatch.setenv('FLOTA_AVISO_WEBHOOK_TOKEN', 'secreto')
        r = client.post('/flota/avisos/entrega?token=secreto',
                        json={'payload': {'gsId': 'x', 'type': 'delivered'}})
        assert r.status_code == 200

    def test_el_trinquete_sigue_atrapando_una_ruta_sin_ningun_guard(self):
        """Se ejerce contra código sintético: si el reconocimiento del guard
        nuevo hubiera vuelto permisivo al extractor, esto lo delata."""
        import ast as _ast

        fuente = (
            "@bp.route('/x', methods=['POST'])\n"
            "@jwt_required()\n"
            "def sin_guard():\n"
            "    return {}\n"
        )
        nodo = _ast.parse(fuente).body[0]
        nombres = []
        for d in nodo.decorator_list:
            f = d.func if isinstance(d, _ast.Call) else d
            nombres.append(f.attr if isinstance(f, _ast.Attribute)
                           else getattr(f, 'id', ''))
        assert 'route' in nombres
        assert not {'exige', 'exige_secreto'} & set(nombres)
