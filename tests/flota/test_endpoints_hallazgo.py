"""La frontera HTTP del hallazgo.

Lo que se prueba acá no es que devuelvan 200: es que **la frontera no afloje
ninguna política del dominio**. Un endpoint que acepta lo que el adaptador
rechaza es un rodeo alrededor de todo lo demás — y como devuelve 201, nadie se
entera.

## La escalada que este archivo existe para impedir

El repo ya tuvo dos:

· `/liquidar-completo` pedía **menos** que las dos operaciones que ejecuta;
· packing tenía dos puertas y una sin guardia.

Acá la asimetría es deliberada y hay que poder afirmarla: **el conductor
reporta y no cierra.** Si quien reporta también cierra, el camino barato es
reportar y descartar en el mismo minuto (regla 11) y el registro de daños se
vuelve decorativo. Un test que solo probara el camino feliz con token de admin
no vería nunca esa puerta abierta.
"""
import pytest

_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


@pytest.fixture
def mundo(db, almacen):
    """Vehículo, un conductor con cuenta y un usuario de control de flota."""
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='HZE200', tipo='NHR', activo=True)
    cond = Usuario(nombre='Conductor HZ', email='hz_cond@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control HZ', email='hz_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    tienda = Usuario(nombre='Tienda HZ', email='hz_tienda@test.com',
                     password_hash=generate_password_hash('x'), rol='tienda',
                     almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, cond, flota, tienda])
    db.session.commit()
    return {
        'placa': veh.placa,
        't_cond': create_access_token(identity=str(cond.id)),
        't_flota': create_access_token(identity=str(flota.id)),
        't_tienda': create_access_token(identity=str(tienda.id)),
        'db': db,
    }


def _reportar(client, token, placa, **extra):
    cuerpo = dict(placa=placa, criticidad='mayor', descripcion='fuga de aceite',
                  km=1000)
    cuerpo.update(extra)
    return client.post('/flota/hallazgos', json=cuerpo, headers=_auth(token))


class TestSesionObligatoria:
    def test_los_cinco_exigen_sesion(self, client, mundo):
        p = mundo['placa']
        assert client.get(f'/flota/hallazgos/{p}').status_code == 401
        assert client.post('/flota/hallazgos', json={}).status_code == 401
        assert client.post('/flota/hallazgos/1/cerrar', json={}).status_code == 401
        assert client.post('/flota/hallazgos/1/descartar', json={}).status_code == 401
        assert client.post('/flota/hallazgos/1/aplazar', json={}).status_code == 401


class TestQuienPuedeQue:
    """La asimetría deliberada: reportar es de todos; el desenlace no."""

    def test_el_conductor_SI_puede_reportar(self, client, mundo):
        """El que ve el golpe es el que maneja. Un daño que solo puede reportar
        un jefe es un daño que se reporta el lunes."""
        r = _reportar(client, mundo['t_cond'], mundo['placa'])
        assert r.status_code == 201, r.get_json()

    def test_el_conductor_NO_puede_cerrar_lo_que_reporto(self, client, mundo):
        """**El agujero de la regla 11.** Sin esto, el camino barato es
        reportar y cerrar en el mismo minuto: el registro queda perfecto y
        ningún daño se arregla."""
        h = _reportar(client, mundo['t_cond'], mundo['placa']).get_json()
        r = client.post(f"/flota/hallazgos/{h['id']}/cerrar", json={},
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 403
        assert r.get_json()['tu_rol'] == 'conductor'

    def test_el_conductor_NO_puede_descartar(self, client, mundo):
        h = _reportar(client, mundo['t_cond'], mundo['placa']).get_json()
        r = client.post(f"/flota/hallazgos/{h['id']}/descartar",
                        json={'motivo': 'nada'}, headers=_auth(mundo['t_cond']))
        assert r.status_code == 403

    def test_el_conductor_NO_puede_aplazar(self, client, mundo):
        h = _reportar(client, mundo['t_cond'], mundo['placa']).get_json()
        r = client.post(f"/flota/hallazgos/{h['id']}/aplazar",
                        json={'motivo': 'después'}, headers=_auth(mundo['t_cond']))
        assert r.status_code == 403

    def test_control_de_flota_SI_cierra(self, client, mundo):
        """La otra dirección. Sin esto, un `exige()` que rechazara a todo el
        mundo pasaría los tres tests de arriba."""
        h = _reportar(client, mundo['t_cond'], mundo['placa']).get_json()
        r = client.post(f"/flota/hallazgos/{h['id']}/cerrar",
                        json={'nota': 'se cambió el retenedor'},
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['estado'] == 'cerrado'

    def test_un_usuario_de_tienda_no_toca_nada_de_flota(self, client, mundo):
        """El agujero que el review encontró en agosto: hasta el 2026-08-03
        cualquiera con sesión escribía en flota."""
        assert _reportar(client, mundo['t_tienda'],
                         mundo['placa']).status_code == 403
        assert client.get(f"/flota/hallazgos/{mundo['placa']}",
                          headers=_auth(mundo['t_tienda'])).status_code == 403


class TestLaFronteraNoAflojaNada:
    def test_la_fecha_limite_mandada_en_el_cuerpo_se_ignora(self, client, mundo):
        """Regla 6: el plazo se calcula. Un cuerpo que traiga `fecha_limite` no
        la ve reflejada — y el que importa es que un `mayor` siga a siete días,
        no a los mil que pidió el cliente."""
        r = _reportar(client, mundo['t_flota'], mundo['placa'],
                      fecha_limite='2099-01-01T00:00:00')
        assert r.status_code == 201
        assert not r.get_json()['fecha_limite'].startswith('2099')

    def test_una_criticidad_inventada_es_409_y_no_un_hallazgo_menor(
            self, client, mundo):
        r = _reportar(client, mundo['t_flota'], mundo['placa'],
                      criticidad='urgente')
        assert r.status_code == 409
        assert 'urgente' in r.get_json()['error']

    def test_faltan_campos_es_400_y_dice_cuales(self, client, mundo):
        r = client.post('/flota/hallazgos', json={'placa': mundo['placa']},
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 400
        for campo in ('criticidad', 'descripcion', 'km'):
            assert campo in r.get_json()['error']

    def test_una_placa_que_no_existe_es_404_y_no_una_lista_vacia(
            self, client, mundo):
        """Un vehículo que nadie dio de alta se dice, no se rodea: una lista
        vacía se lee como «este camión no tiene daños»."""
        r = client.get('/flota/hallazgos/NOEXISTE', headers=_auth(mundo['t_flota']))
        assert r.status_code == 404

    def test_un_km_no_numerico_es_400(self, client, mundo):
        r = _reportar(client, mundo['t_flota'], mundo['placa'], km='mucho')
        assert r.status_code == 400

    def test_descartar_sin_motivo_es_409_tambien_por_HTTP(self, client, mundo):
        """El adaptador lo exige; la frontera no lo puede aflojar."""
        h = _reportar(client, mundo['t_flota'], mundo['placa']).get_json()
        r = client.post(f"/flota/hallazgos/{h['id']}/descartar", json={},
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 409

    def test_un_id_inexistente_no_devuelve_200(self, client, mundo):
        r = client.post('/flota/hallazgos/99999/cerrar', json={},
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 409
        assert '99999' in r.get_json()['error']


class TestLoQuePublicaLaLista:
    def test_los_juicios_vienen_del_dominio_y_llegan_a_la_pantalla(
            self, client, mundo):
        """`vencido` y `entra_al_indicador` se publican para que el JS no los
        recalcule. Una regla escrita dos veces diverge, y la copia que diverge
        es la de la pantalla."""
        _reportar(client, mundo['t_flota'], mundo['placa'])
        d = client.get(f"/flota/hallazgos/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])).get_json()
        h = d['hallazgos'][0]
        for campo in ('vencido', 'entra_al_indicador', 'dias_abierto',
                      'fecha_limite', 'aplazado_veces', 'linea_base'):
            assert campo in h, f'la pantalla no puede pintar sin {campo}'

    def test_abiertos_y_vencidos_son_contadores_distintos(self, client, mundo):
        """Sumarlos escondería el vencido, que es el que hay que atender."""
        _reportar(client, mundo['t_flota'], mundo['placa'])
        d = client.get(f"/flota/hallazgos/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert d['abiertos'] == 1
        assert d['vencidos'] == 0

    def test_por_defecto_solo_los_abiertos(self, client, mundo):
        h = _reportar(client, mundo['t_flota'], mundo['placa']).get_json()
        client.post(f"/flota/hallazgos/{h['id']}/cerrar", json={},
                    headers=_auth(mundo['t_flota']))
        d = client.get(f"/flota/hallazgos/{mundo['placa']}",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert d['hallazgos'] == []

    def test_con_todos_1_sale_el_historico(self, client, mundo):
        """La otra dirección: si el filtro escondiera todo, el test de arriba
        pasaría igual y el histórico sería inalcanzable."""
        h = _reportar(client, mundo['t_flota'], mundo['placa']).get_json()
        client.post(f"/flota/hallazgos/{h['id']}/cerrar", json={},
                    headers=_auth(mundo['t_flota']))
        d = client.get(f"/flota/hallazgos/{mundo['placa']}?todos=1",
                       headers=_auth(mundo['t_flota'])).get_json()
        assert [x['estado'] for x in d['hallazgos']] == ['cerrado']

    def test_las_horas_viajan_declarando_su_zona(self, client, mundo):
        """Una hora sin zona la interpreta el teléfono como local y corre el
        reloj cinco horas — ya pasó con el recibo del TGZ653."""
        r = _reportar(client, mundo['t_flota'], mundo['placa'])
        cuerpo = r.get_json()
        assert cuerpo['reportado_ts'].endswith('+00:00')
        assert cuerpo['fecha_limite'].endswith('+00:00')


class TestElHealthLosCuenta:
    """La medida nace con la tabla, no un mes después.

    `flota_lectura_odometro` vivió desde el 2026-08-01 con **cero campos** en el
    health, y por eso nada avisó de las 26 lecturas sin foto ni del salto de
    16,3 millones de km. La tabla de hallazgos no repite eso: sale medida desde
    el primer día.
    """

    def _health(self, client, mundo):
        r = client.get('/flota/health', headers=_auth(mundo['t_flota']))
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def test_los_dos_campos_existen_y_arrancan_en_cero(self, client, mundo):
        """Cero es una afirmación sobre la flota. El campo AUSENTE no dice
        nada, y es lo que pasaba antes."""
        d = self._health(client, mundo)
        assert d['hallazgos_abiertos'] == 0
        assert d['hallazgos_vencidos'] == 0

    def test_un_dano_reportado_se_cuenta(self, client, mundo):
        _reportar(client, mundo['t_flota'], mundo['placa'])
        d = self._health(client, mundo)
        assert d['hallazgos_abiertos'] == 1
        assert d['hallazgos_vencidos'] == 0, (
            'un daño en plazo no puede salir como vencido: si sale, el rojo '
            'deja de distinguir y el tablero se deja de mirar')

    def test_un_dano_vencido_se_cuenta_aparte(self, client, mundo):
        """Se fuerza el vencimiento moviendo la fecha límite hacia atrás — el
        camino que de verdad ocurre es esperar, y no se puede esperar en un
        test."""
        from datetime import datetime, timedelta

        from flota.adaptadores.modelos import Hallazgo

        h = _reportar(client, mundo['t_flota'], mundo['placa']).get_json()
        fila = Hallazgo.query.get(h['id'])
        fila.fecha_limite = datetime.utcnow() - timedelta(days=1)
        mundo['db'].session.commit()

        d = self._health(client, mundo)
        assert d['hallazgos_vencidos'] == 1
        assert d['hallazgos_abiertos'] == 1, (
            'un vencido sigue abierto: son dos preguntas, no dos estados')

    def test_lo_cerrado_deja_de_contar_en_los_dos(self, client, mundo):
        h = _reportar(client, mundo['t_flota'], mundo['placa']).get_json()
        client.post(f"/flota/hallazgos/{h['id']}/cerrar", json={},
                    headers=_auth(mundo['t_flota']))
        d = self._health(client, mundo)
        assert d['hallazgos_abiertos'] == 0
        assert d['hallazgos_vencidos'] == 0

    def test_el_descartado_tampoco_cuenta_como_abierto(self, client, mundo):
        """Descartar no es el camino barato para bajar el número —no mejora el
        indicador— pero sí saca el daño de la lista de pendientes: cuando de
        verdad no era nada, dejarlo abierto es ruido."""
        h = _reportar(client, mundo['t_flota'], mundo['placa']).get_json()
        client.post(f"/flota/hallazgos/{h['id']}/descartar",
                    json={'motivo': 'era barro'}, headers=_auth(mundo['t_flota']))
        assert self._health(client, mundo)['hallazgos_abiertos'] == 0
