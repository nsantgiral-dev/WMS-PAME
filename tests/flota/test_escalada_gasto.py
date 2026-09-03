"""La escalada de `/flota/tanqueos`, convertida en trinquete.

## Qué pasó

Encontrada por la auditoría adversarial del 2026-09-02, ejecutando:

```
POST /flota/gastos    → exige(MAESTROS_FLOTA)
POST /flota/tanqueos  → exige(LECTURA_FLOTA)  → registrar_gasto(commit=False)

POST /flota/gastos   (rol conductor) → 403
POST /flota/tanqueos (rol conductor) → 201, fila en flota_gasto
GET  /flota/gastos/<placa> (conductor) → 403
```

Un conductor escribía en la tabla de la que sale el CPK **y no podía leer lo
que acababa de escribir**. Escribir sin poder leer es la firma de un permiso
puesto por la pantalla, no por la operación.

Es la forma exacta de `/liquidar-completo`, documentada en `CLAUDE.md`: *un
endpoint compuesto no puede exigir menos que el más estricto de sus
componentes*. Y la forma se repite sola — alguien agrupa pasos para que la
pantalla haga una sola llamada y le pone el permiso de quien va a usar la
pantalla, no el de lo que el endpoint ejecuta.

## Por qué el guard va en el servicio

`registrar_tanqueo` es una **segunda puerta** a `registrar_gasto`. Un guard en
la ruta protege esa ruta; uno en el servicio protege la operación — la lección
de packing, que ya costó una vez con dos endpoints y una sola guardia.

Y la autoridad **se resuelve contra la base**: `_exigir_autoridad` lee el rol de
la fila del usuario, no un parámetro. Un guard cuya precondición la manda quien
llama no es un guard.
"""
from datetime import date

import pytest

from flota.adaptadores import gastos as adaptador
from flota.adaptadores.modelos import Gasto
from flota.dominio import costos
from flota.dominio.errores import PermisoInsuficiente

_H = 'Authorization'


def _auth(t):
    return {_H: f'Bearer {t}'}


@pytest.fixture
def mundo(db, almacen):
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    v = Vehiculo(placa='ESC100', tipo='NHR', activo=True)
    roles = {'conductor': 'esc_cond@t.co', 'control_flota': 'esc_flota@t.co',
             'tienda': 'esc_tienda@t.co'}
    us = {}
    for rol, mail in roles.items():
        u = Usuario(nombre=rol, email=mail, rol=rol, almacen_id=almacen.id,
                    activo=True, password_hash=generate_password_hash('x'))
        db.session.add(u)
        us[rol] = u
    db.session.add(v)
    db.session.commit()
    return {'placa': v.placa, 'veh': v.id, 'db': db,
            'u': {r: u.id for r, u in us.items()},
            't': {r: create_access_token(identity=str(u.id))
                  for r, u in us.items()}}


def _tanqueo(client, token, placa, **extra):
    cuerpo = dict(placa=placa, fecha='2026-09-02', valor='120000',
                  galones='10', tanque='lleno', estacion='Terpel',
                  km=1000, proveedor='Terpel', origen_costo='efectivo_conductor')
    cuerpo.update(extra)
    return client.post('/flota/tanqueos', json=cuerpo, headers=_auth(token))


class TestElConductorSigueTanqueando:
    """La otra dirección, y va primero: si el arreglo cerrara la puerta del
    campo, el conductor dejaría de cargar tanqueos y la tabla del CPK se
    quedaría vacía. Cerrar de más también es un defecto."""

    def test_el_conductor_registra_su_tanqueo(self, client, mundo):
        r = _tanqueo(client, mundo['t']['conductor'], mundo['placa'])
        assert r.status_code == 201, r.get_json()
        assert Gasto.query.filter_by(categoria='combustible').count() == 1

    def test_y_control_de_flota_tambien(self, client, mundo):
        r = _tanqueo(client, mundo['t']['control_flota'], mundo['placa'])
        assert r.status_code == 201, r.get_json()


class TestLaEscaladaQueEstabaAbierta:
    def test_el_conductor_NO_registra_un_gasto_de_escritorio(self, client, mundo):
        """El endpoint directo siempre lo rechazó — esto solo lo confirma."""
        r = client.post('/flota/gastos', json={
            'placa': mundo['placa'], 'categoria': 'soat', 'fecha': '2026-09-02',
            'valor': '999999', 'proveedor': 'YO MISMO',
            'origen_costo': 'efectivo_conductor', 'km': 1000,
            'periodo_desde': '2026-09-01', 'periodo_hasta': '2027-08-31',
        }, headers=_auth(mundo['t']['conductor']))
        assert r.status_code == 403

    def test_y_TAMPOCO_por_el_servicio_que_el_tanqueo_usa(self, mundo):
        """**El agujero.** `registrar_tanqueo` llamaba a `registrar_gasto` sin
        que nadie volviera a preguntar quién pedía."""
        with pytest.raises(PermisoInsuficiente) as e:
            adaptador.registrar_gasto(
                vehiculo_id=mundo['veh'], categoria='soat',
                fecha=date(2026, 9, 2), valor='999999', proveedor='YO MISMO',
                origen_costo='efectivo_conductor', km=1000,
                periodo_desde=date(2026, 9, 1), periodo_hasta=date(2027, 8, 31),
                registrado_por_usuario_id=mundo['u']['conductor'])
        assert 'conductor' in str(e.value)
        assert Gasto.query.count() == 0, 'quedó una fila a medias'

    def test_un_usuario_inactivo_no_pasa(self, mundo):
        """Regla 0: lo que no se puede afirmar, no autoriza."""
        from app.models.usuario import Usuario
        u = Usuario.query.get(mundo['u']['control_flota'])
        u.activo = False
        mundo['db'].session.commit()
        with pytest.raises(PermisoInsuficiente):
            adaptador.registrar_gasto(
                vehiculo_id=mundo['veh'], categoria='soat',
                fecha=date(2026, 9, 2), valor='1', proveedor='X',
                origen_costo='sin_dato', km=1,
                periodo_desde=date(2026, 9, 1), periodo_hasta=date(2026, 9, 2),
                registrado_por_usuario_id=u.id)

    def test_un_usuario_que_no_existe_tampoco(self, mundo):
        with pytest.raises(PermisoInsuficiente):
            adaptador.registrar_gasto(
                vehiculo_id=mundo['veh'], categoria='soat',
                fecha=date(2026, 9, 2), valor='1', proveedor='X',
                origen_costo='sin_dato', km=1,
                periodo_desde=date(2026, 9, 1), periodo_hasta=date(2026, 9, 2),
                registrado_por_usuario_id=999999)


class TestLaPoliticaEsUnaSola:
    def test_una_categoria_desconocida_exige_maestros(self):
        """Sin `.get(x, default)`: lo que no se sabe clasificar no lo escribe el
        rol más amplio. Es cómo se cuela una escalada."""
        assert costos.exige_maestros('categoria_inventada') is True

    def test_combustible_es_de_campo_y_el_soat_no(self):
        assert costos.exige_maestros('combustible') is False
        assert costos.exige_maestros('soat') is True

    def test_toda_categoria_de_campo_es_una_categoria_real(self):
        """Una lista de excepciones que nombre algo inexistente es una
        exención que no exime nada — y nadie la revisa."""
        for c in costos.CATEGORIAS_DE_CAMPO:
            assert c in costos.CATEGORIAS_GASTO
