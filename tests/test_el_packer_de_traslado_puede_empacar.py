"""El rol que podía pickear y no empacar.

`picker_traslado` está declarado en el guard de picking
(`app/routes/picking.py:22`). Su gemelo `packer_traslado` NO estaba en
`PACKING_ROLES` — aunque `scope_packing` documenta y filtra su caso («bodega
+ solo TRASLADO»), o sea que el endpoint se diseñó para él y la puerta nunca
lo recibió.

**Funcionaba por una casilla.** Los cuatro packers de producción tienen
`puede_empacar=True`, un flag cuya etiqueta en la pantalla de usuarios dice
«Empacador / Auditor» —otro puesto— y que nace DESMARCADA. El quinto packer
que alguien cree quedaba mudo: `GET /api/packing/` le respondía 403, el
`catch` de `packing.js:50` pintaba la pantalla entera en rojo, y nada decía
por qué.

Medido el 2026-09-21: 4 de 4 con la casilla puesta. El defecto estaba latente,
no activo — y por eso ningún test lo tocaba.
"""
import pytest
from flask_jwt_extended import create_access_token

from app.models.usuario import Usuario
from app.routes._auth_helpers import Roles, _puede_empacar


def _usuario(db, nombre, rol, **kw):
    u = Usuario(nombre=nombre, email=f'{nombre.replace(" ", ".").lower()}@t.co',
                rol=rol, **kw)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _hdr(app, u):
    with app.app_context():
        return {'Authorization': 'Bearer ' + create_access_token(identity=str(u.id))}


class TestEntraSinLaCasilla:

    def test_un_packer_nuevo_entra_a_packing(self, app, client, db):
        """EL test. `puede_empacar` viene en False, que es como nace un
        usuario recién creado desde la pantalla."""
        u = _usuario(db, 'Packer Nuevo', 'packer_traslado',
                     bodega_siesa_id='NC1', puede_empacar=False)
        r = client.get('/api/packing/', headers=_hdr(app, u))
        assert r.status_code == 200, (
            f'un packer de traslado recién creado recibe {r.status_code}: la '
            f'pantalla entera le sale en rojo y nada explica por qué')

    def test_el_helper_lo_autoriza_sin_el_flag(self, db):
        u = _usuario(db, 'Packer Dos', 'packer_traslado', puede_empacar=False)
        assert _puede_empacar(u) is True

    def test_sigue_estando_en_el_guard_de_picking(self, app, client, db):
        """Su gemelo ya estaba; esto verifica que no se rompió al tocar el
        otro lado."""
        u = _usuario(db, 'Packer Tres', 'packer_traslado',
                     bodega_siesa_id='NC1', puede_empacar=False)
        r = client.get('/api/picking/', headers=_hdr(app, u))
        assert r.status_code == 200


class TestLaPuertaNoSeAbrioDeMas:
    """Un ensanche de tupla de permisos se mide por quién queda DENTRO, no
    solo por quién queríamos meter."""

    @pytest.mark.parametrize('rol', ['conductor', 'tienda', 'recepcionista',
                                     'compras', 'operario', 'picker_traslado'])
    def test_los_demas_roles_siguen_afuera(self, db, rol):
        u = _usuario(db, f'Otro {rol}', rol, puede_empacar=False)
        assert _puede_empacar(u) is False, (
            f'{rol} quedó autorizado a empacar sin la casilla: la tupla creció '
            f'por un borde que no se miró')

    def test_la_casilla_sigue_siendo_una_via_independiente(self, db):
        """Quien no tiene el rol pero sí el flag sigue pudiendo — es la vía
        que usa hoy la operación y no se toca."""
        u = _usuario(db, 'Operario Con Flag', 'operario', puede_empacar=True)
        assert _puede_empacar(u) is True

    def test_la_tupla_tiene_exactamente_los_cuatro(self):
        """Trinquete: si alguien agrega un quinto rol, que sea una decisión
        escrita y no un arrastre."""
        assert set(Roles.PACKING_ROLES) == {
            Roles.ADMIN, Roles.SUPERVISOR, Roles.EMPACADOR,
            Roles.PACKER_TRASLADO}


def test_el_gemelo_de_picking_y_el_de_packing_son_simetricos():
    """La asimetría era el síntoma: `picker_traslado` en el guard de picking y
    `packer_traslado` fuera del de packing, con el alcance escrito para los
    dos. Si mañana uno se mueve, el otro tiene que moverse con él o volver a
    divergir."""
    import ast
    from pathlib import Path
    fuente = (Path(__file__).resolve().parents[1] / 'app' / 'routes'
              / 'picking.py').read_text(encoding='utf-8')
    assert 'PICKER_TRASLADO' in fuente and 'PACKER_TRASLADO' in fuente, (
        'el guard de picking dejó de nombrar a los roles de traslado')
    assert Roles.PACKER_TRASLADO in Roles.PACKING_ROLES, (
        'packer_traslado salió del guard de packing mientras picker_traslado '
        'sigue en el de picking: vuelve la asimetría')
