"""El conductor tenía el botón de Tanqueo y no tenía la pantalla.

`flotaCondTanquear` reusaba `flotaRenderGastos`, cuya primera línea pide
`GET /flota/gastos/<placa>` — que exige `MAESTROS_FLOTA`. Al conductor le
devolvía 403, el `catch` pintaba «No se pudieron cargar los gastos» y hacía
`return`, así que el formulario —que se construye más abajo, en la misma
función— no llegaba a dibujarse nunca.

Lo notable es que las dos mitades estaban decididas por escrito y en
direcciones opuestas:

  · el conductor DEBE poder registrar un tanqueo — `POST /flota/tanqueos` es
    `LECTURA_FLOTA` a propósito;
  · el conductor NO debe ver el CPK — «un CPK en la pantalla del conductor
    está a un paso de leerse como una medida suya».

Las dos son correctas. Lo que faltaba era separar el dato sensible del
vocabulario, y un detector que cruzara «los roles que el endpoint autoriza»
contra «los roles que pueden llegar al botón». El comentario de `flota.js`
afirmaba que ese detector existía desde el 2026-09-03. No existía.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models.usuario import Usuario


def _usuario(db, nombre, rol):
    u = Usuario(nombre=nombre, email=f'{nombre.replace(" ", ".").lower()}@t.co',
                rol=rol)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _hdr(app, u):
    with app.app_context():
        return {'Authorization': 'Bearer ' + create_access_token(identity=str(u.id))}


class TestElVocabularioLlegaAlConductor:

    def test_el_conductor_puede_pedir_el_vocabulario(self, app, client, db):
        u = _usuario(db, 'Conductor Uno', 'conductor')
        r = client.get('/flota/vocabulario', headers=_hdr(app, u))
        assert r.status_code == 200, r.get_json()

    def test_trae_las_cinco_listas_que_el_formulario_necesita(self, app, client, db):
        u = _usuario(db, 'Conductor Dos', 'conductor')
        d = client.get('/flota/vocabulario', headers=_hdr(app, u)).get_json()
        for k in ('categorias', 'categorias_con_periodo', 'categorias_de_campo',
                  'origenes_costo', 'estados_tanque'):
            assert k in d, f'falta {k}: el formulario de tanqueo no se puede armar'
            assert isinstance(d[k], list) and d[k], f'{k} llegó vacía'

    def test_NO_trae_ningun_dato_del_vehiculo(self, app, client, db):
        """La otra mitad de la decisión. Si este endpoint filtrara el CPK o el
        rendimiento, habría resuelto el bug rompiendo la razón por la que el
        listado estaba cerrado."""
        u = _usuario(db, 'Conductor Tres', 'conductor')
        d = client.get('/flota/vocabulario', headers=_hdr(app, u)).get_json()
        prohibidos = {'cpk', 'cpk_motivo', 'rendimiento_km_galon', 'gastos',
                      'km_recorridos', 'desde', 'hasta', 'placa'}
        colados = prohibidos & set(d)
        assert not colados, (
            f'{colados} llegó a la pantalla del conductor. El vocabulario es '
            f'vocabulario; el costo del vehículo es otra cosa.')


class TestLoQueSigueCerrado:
    """Arreglar el tanqueo no puede abrir el listado."""

    def test_el_conductor_sigue_SIN_ver_los_gastos(self, app, client, db):
        u = _usuario(db, 'Conductor Cuatro', 'conductor')
        r = client.get('/flota/gastos/ABC123', headers=_hdr(app, u))
        assert r.status_code == 403, (
            'el conductor recuperó el acceso al CPK — el arreglo del tanqueo '
            'no debía tocar esto')

    @pytest.mark.parametrize('rol', ['admin', 'supervisor', 'jefe_almacen',
                                     'control_flota'])
    def test_quien_ya_veia_los_gastos_sigue_viendolos(self, app, client, db, rol):
        u = _usuario(db, f'Gestion {rol}', rol)
        r = client.get('/flota/vocabulario', headers=_hdr(app, u))
        assert r.status_code == 200


def test_quien_puede_registrar_un_tanqueo_puede_armar_el_formulario():
    """EL trinquete, y el que no existía.

    La regla: **ningún rol puede tener el botón y no la pantalla.** Se cruza
    el guard del POST que registra el tanqueo contra el guard del GET que
    arma su formulario. Si alguien aprieta uno de los dos sin el otro, esto
    se pone rojo en vez de dejar un botón que muere en un 403.
    """
    import ast
    from pathlib import Path

    fuente = (Path(__file__).resolve().parents[2] / 'flota' / 'api'
              / 'gastos.py').read_text(encoding='utf-8')
    arbol = ast.parse(fuente)

    def guard_de(nombre_fn):
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == nombre_fn)
        for dec in fn.decorator_list:
            if isinstance(dec, ast.Call) and getattr(dec.func, 'id', '') == 'exige':
                return ast.unparse(dec.args[0])
        raise AssertionError(f'{nombre_fn} no tiene decorador @exige')

    assert guard_de('vocabulario') == guard_de('registrar_tanqueo'), (
        'El formulario de tanqueo y el registro del tanqueo exigen roles '
        'distintos. El rol que puede registrar y no puede armar el formulario '
        'tiene un botón que muere en un 403 — que es exactamente el defecto '
        'que este archivo vino a cerrar.')
