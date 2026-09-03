"""
La frontera HTTP del preventivo. **Que no afloje ninguna política del adaptador.**

Un endpoint que acepta lo que el adaptador rechaza es un rodeo alrededor de todo
lo demás — y como devuelve 201, nadie se entera. Por eso cada regla que
`flota/adaptadores/preventivo.py` impone se vuelve a ejercer acá por HTTP.

## La asimetría de roles que este archivo existe para afirmar

| | Quién | Por qué |
|---|---|---|
| Ver el plan | conductor incluido | El que maneja tiene derecho a saber que la correa está vencida. Es la información que decide si sale a ruta |
| Registrar que se hizo | **sin conductor** | Regla 11: la forma de maximizar este tablero sin hacer el trabajo es marcar todo como hecho — seis relojes reiniciados en veinte segundos y el vehículo impecable |
| Fijar el intervalo | sin conductor | Es escribir un número con autoridad sobre un vehículo. Lo mismo que `km_inicial` enseñó a no dejar en manos de cualquiera |
| Sembrar | sin conductor | Escribe el plan, que es un maestro |

Un test que sólo probara el camino feliz con token de admin no vería nunca esas
cuatro puertas.
"""
from datetime import datetime

import pytest

from flota.adaptadores import preventivo as adaptador
from flota.adaptadores.modelos import EjecucionTarea, FichaTecnica, PlanTarea
from flota.dominio.preventivo import DIAS_AVISO_PREVENTIVO, EstadoTarea

_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


@pytest.fixture
def mundo(db, almacen):
    """Un camión con ficha y los tres roles que el módulo distingue."""
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='PVE100', tipo='camion', activo=True)
    cond = Usuario(nombre='Conductor PVE', email='pve_cond@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control PVE', email='pve_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    tienda = Usuario(nombre='Tienda PVE', email='pve_tienda@test.com',
                     password_hash=generate_password_hash('x'), rol='tienda',
                     almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, cond, flota, tienda])
    db.session.flush()
    db.session.add(FichaTecnica(
        vehiculo_id=veh.id, posiciones_llanta=6, km_inicial=50000,
        km_inicial_ts=datetime(2026, 1, 1),
        distribucion='correa', distribucion_km_cambio=60000,
        distribucion_fuente='manual_fabricante',
        aceite_motor_spec='15W40 API CI-4'))
    db.session.commit()
    return {
        'vehiculo_id': veh.id, 'placa': veh.placa, 'usuario_id': flota.id,
        't_cond': create_access_token(identity=str(cond.id)),
        't_flota': create_access_token(identity=str(flota.id)),
        't_tienda': create_access_token(identity=str(tienda.id)),
        'db': db,
    }


def _sembrar(client, mundo):
    r = client.post(f'/flota/preventivo/{mundo["placa"]}/sembrar',
                    headers=_auth(mundo['t_flota']))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _plan_id(mundo, tipo):
    return PlanTarea.query.filter_by(
        vehiculo_id=mundo['vehiculo_id'], tipo=tipo).one().id


# ══════════════════════════════════════════════════════════════════════════
# Quién puede qué
# ══════════════════════════════════════════════════════════════════════════

class TestElConductorVeElPlanYNoLoEscribe:

    def test_el_conductor_puede_ver_el_plan(self, client, mundo):
        """Es la información que decide si sale a ruta. Un daño en la correa lo
        paga el que va manejando."""
        _sembrar(client, mundo)
        r = client.get(f'/flota/preventivo/{mundo["placa"]}',
                       headers=_auth(mundo['t_cond']))
        assert r.status_code == 200

    def test_el_conductor_NO_puede_declarar_que_una_tarea_se_hizo(self, client, mundo):
        """**Regla 11.** Si el que se beneficia de que no salga en rojo es el
        mismo que declara «ya se cambió», el registro queda decorativo."""
        _sembrar(client, mundo)
        r = client.post(
            f'/flota/preventivo/tarea/{_plan_id(mundo, "distribucion")}/ejecucion',
            json={'km': 55000}, headers=_auth(mundo['t_cond']))
        assert r.status_code == 403
        assert EjecucionTarea.query.count() == 0

    def test_el_conductor_NO_puede_fijar_un_intervalo(self, client, mundo):
        _sembrar(client, mundo)
        r = client.put(f'/flota/preventivo/tarea/{_plan_id(mundo, "aceite_motor")}',
                       json={'intervalo_km': 5000, 'fuente': 'taller'},
                       headers=_auth(mundo['t_cond']))
        assert r.status_code == 403

    def test_el_conductor_NO_puede_sembrar(self, client, mundo):
        r = client.post(f'/flota/preventivo/{mundo["placa"]}/sembrar',
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 403

    def test_un_usuario_de_tienda_no_ve_nada_de_flota(self, client, mundo):
        r = client.get(f'/flota/preventivo/{mundo["placa"]}',
                       headers=_auth(mundo['t_tienda']))
        assert r.status_code == 403

    def test_sin_token_no_se_entra(self, client, mundo):
        assert client.get(f'/flota/preventivo/{mundo["placa"]}').status_code == 401


# ══════════════════════════════════════════════════════════════════════════
# Lo que la respuesta publica, y por qué
# ══════════════════════════════════════════════════════════════════════════

class TestLaRespuestaTraeLoQueHaceFaltaParaDudar:

    def test_el_plan_sale_con_su_estado_y_sus_insumos(self, client, mundo):
        """Un estado suelto no se puede auditar: quien vea `vencida` tiene que
        poder ver a qué kilometraje tocaba y de dónde salió el intervalo."""
        _sembrar(client, mundo)
        d = client.get(f'/flota/preventivo/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota'])).get_json()
        t = [x for x in d['tareas'] if x['tipo'] == 'distribucion'][0]
        for campo in ('estado', 'intervalo_km', 'proximo_km', 'km_restante',
                      'dias_estimados', 'fuente', 'fuente_blanda', 'nombre'):
            assert campo in t

    def test_la_procedencia_viaja_SIEMPRE(self, client, mundo):
        """Decisión 2 del plan. Un intervalo sin procedencia se lee como si
        alguien lo hubiera verificado."""
        _sembrar(client, mundo)
        d = client.get(f'/flota/preventivo/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota'])).get_json()
        assert all('fuente' in t and 'fuente_blanda' in t for t in d['tareas'])
        t = [x for x in d['tareas'] if x['tipo'] == 'distribucion'][0]
        assert t['fuente'] == 'manual_fabricante'
        assert t['fuente_blanda'] is False

    def test_el_umbral_se_publica_en_vez_de_esconderse(self, client, mundo):
        """Un umbral que la interfaz no nombra es un umbral que nadie discute.
        Es el único número elegido de esta fase y sale con la respuesta."""
        _sembrar(client, mundo)
        d = client.get(f'/flota/preventivo/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota'])).get_json()
        assert d['dias_aviso'] == DIAS_AVISO_PREVENTIVO

    def test_el_ritmo_sale_con_n_y_dias_o_con_su_motivo(self, client, mundo):
        """Un km/día suelto sobre dos lecturas del mismo día dice lo mismo que
        sobre cuarenta de tres meses, y son dos números distintos."""
        _sembrar(client, mundo)
        d = client.get(f'/flota/preventivo/{mundo["placa"]}',
                       headers=_auth(mundo['t_flota'])).get_json()
        r = d['ritmo']
        assert r['km_dia'] == 'sin_dato'
        assert r['motivo']
        assert r['n'] == 0

    def test_una_placa_que_no_existe_da_404_y_no_una_lista_vacia(self, client, mundo):
        r = client.get('/flota/preventivo/NOEXISTE',
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# La frontera no afloja lo que el adaptador impone
# ══════════════════════════════════════════════════════════════════════════

class TestLaFronteraNoAflojaLaRegla3:

    def test_sin_km_no_se_registra_la_ejecucion(self, client, mundo):
        """Y no se acepta «el último conocido»: ése es exactamente el default
        peligroso que este módulo tiene prohibido."""
        _sembrar(client, mundo)
        r = client.post(
            f'/flota/preventivo/tarea/{_plan_id(mundo, "distribucion")}/ejecucion',
            json={}, headers=_auth(mundo['t_flota']))
        assert r.status_code == 400
        assert 'km' in r.get_json()['error']

    def test_un_km_no_numerico_da_400_y_no_500(self, client, mundo):
        _sembrar(client, mundo)
        r = client.post(
            f'/flota/preventivo/tarea/{_plan_id(mundo, "distribucion")}/ejecucion',
            json={'km': 'ayer'}, headers=_auth(mundo['t_flota']))
        assert r.status_code == 400

    def test_con_km_se_registra_y_la_lectura_queda(self, client, mundo):
        """La dirección contraria: una frontera que rechazara siempre pasaría
        los dos tests de arriba."""
        from flota.adaptadores.modelos import LecturaOdometro

        _sembrar(client, mundo)
        r = client.post(
            f'/flota/preventivo/tarea/{_plan_id(mundo, "distribucion")}/ejecucion',
            json={'km': 55000, 'taller': 'Taller de la esquina'},
            headers=_auth(mundo['t_flota']))
        assert r.status_code == 201
        assert EjecucionTarea.query.count() == 1
        assert LecturaOdometro.query.filter_by(valor_km=55000).count() == 1

    def test_sobre_una_tarea_sin_intervalo_devuelve_409_con_su_motivo(
            self, client, mundo):
        """El cuerpo está perfecto y el estado del mundo no lo admite. Un 400
        diría que el que llama escribió mal."""
        _sembrar(client, mundo)
        r = client.post(
            f'/flota/preventivo/tarea/{_plan_id(mundo, "aceite_motor")}/ejecucion',
            json={'km': 55000}, headers=_auth(mundo['t_flota']))
        assert r.status_code == 409
        assert 'intervalo' in r.get_json()['error']


class TestLaFronteraExigeLaProcedencia:

    def test_sin_fuente_en_el_cuerpo_no_se_guarda_el_intervalo(self, client, mundo):
        _sembrar(client, mundo)
        r = client.put(f'/flota/preventivo/tarea/{_plan_id(mundo, "aceite_motor")}',
                       json={'intervalo_km': 5000},
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 400
        assert 'procedencia' in r.get_json()['error']

    def test_una_fuente_inventada_da_400(self, client, mundo):
        _sembrar(client, mundo)
        r = client.put(f'/flota/preventivo/tarea/{_plan_id(mundo, "aceite_motor")}',
                       json={'intervalo_km': 5000, 'fuente': 'me_lo_dijeron'},
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 400

    def test_con_fuente_valida_se_guarda_y_el_estado_cambia(self, client, mundo):
        _sembrar(client, mundo)
        r = client.put(f'/flota/preventivo/tarea/{_plan_id(mundo, "aceite_motor")}',
                       json={'intervalo_km': 5000, 'fuente': 'concesionario'},
                       headers=_auth(mundo['t_flota']))
        assert r.status_code == 200
        t = [x for x in r.get_json()['tareas'] if x['tipo'] == 'aceite_motor'][0]
        assert t['estado'] == EstadoTarea.SIN_LINEA_BASE
        assert t['fuente'] == 'concesionario'


class TestSembrarEsIdempotenteYLoDice:

    def test_la_primera_corrida_crea_y_la_segunda_no(self, client, mundo):
        assert _sembrar(client, mundo)['resumen']['creadas'] == 2
        assert _sembrar(client, mundo)['resumen']['creadas'] == 0

    def test_devuelve_200_y_no_201_aunque_cree_filas(self, client, mundo):
        """La operación es «dejá el plan igual a la ficha». Un 201 diría que
        hubo un recurso nuevo, y la mitad de las veces no lo hay."""
        r = client.post(f'/flota/preventivo/{mundo["placa"]}/sembrar',
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 200

    def test_un_vehiculo_sin_ficha_devuelve_409_y_no_una_lista_vacia(
            self, client, mundo, db):
        from app.models.vehiculo import Vehiculo

        db.session.add(Vehiculo(placa='PVE900', tipo='camion', activo=True))
        db.session.commit()
        r = client.post('/flota/preventivo/PVE900/sembrar',
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 409
        assert 'ficha' in r.get_json()['error']


class TestElHealthCuentaLoMismoQueLaPantalla:
    """Una política, una función. Si el health contara por su cuenta, la copia
    del tablero sería la que diverge — y es la que la gente mira."""

    def test_los_cuatro_contadores_salen_y_coinciden_con_el_plan(
            self, client, mundo):
        _sembrar(client, mundo)
        h = client.get('/flota/health', headers=_auth(mundo['t_flota'])).get_json()
        assert h['tareas_sin_linea_base'] == 1     # la distribución
        assert h['tareas_sin_intervalo'] == 1      # el aceite de motor
        assert h['tareas_vencidas'] == 0
        assert h['tareas_por_vencer'] == 0

    def test_el_ritmo_por_vehiculo_sale_como_hecho_con_su_motivo(
            self, client, mundo):
        """Regla 13: es el hecho que permite fijar `DIAS_AVISO_PREVENTIVO` con
        dato dentro de un mes, en vez de a ojo hoy."""
        h = client.get('/flota/health', headers=_auth(mundo['t_flota'])).get_json()
        fila = [x for x in h['km_dia_por_vehiculo']
                if x['placa'] == mundo['placa']][0]
        assert fila['km_dia'] == 'sin_dato'
        assert fila['motivo']

    def test_sin_plan_los_contadores_valen_CERO_y_no_null(self, client, mundo):
        """`0` es una afirmación sobre la flota; `null` es una afirmación sobre
        el sistema —«esto no se puede medir»—. La tabla existe, así que el cero
        es medido."""
        h = client.get('/flota/health', headers=_auth(mundo['t_flota'])).get_json()
        assert h['tareas_vencidas'] == 0
        assert h['tareas_sin_intervalo'] == 0
