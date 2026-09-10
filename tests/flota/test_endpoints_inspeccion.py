"""La frontera HTTP de la inspección diaria.

Lo que se prueba acá no es que devuelvan 200: es que **la frontera no afloje
ninguna política del dominio**. Un endpoint que acepta lo que el adaptador
rechaza es un rodeo alrededor de todo lo demás — y como devuelve 201, nadie se
entera.

## Las dos escaladas que este archivo existe para impedir

El repo ya tuvo dos, las dos en `CLAUDE.md`: `/liquidar-completo` pedía **menos**
que las dos operaciones que ejecutaba, y packing tenía dos puertas y una sin
guardia. La forma se repite sola.

Acá la asimetría es deliberada y hay que poder afirmarla: **el conductor
inspecciona y no cierra.** Su `no_apto` hace nacer un hallazgo con su reloj
corriendo, y apagar ese reloj es `MAESTROS_FLOTA`. Si quien inspecciona pudiera
cerrar, el camino barato sería marcar `no_apto` y cerrarlo en el mismo minuto
(regla 11).

## Y las horas del día

`dia` lo decide el servidor en hora de Bogotá. Los tests lo piden con
`dia_operativo()` y no con `date.today()`: entre las 7 p.m. y medianoche los dos
relojes están en días distintos y el build de Railway se cae solo de noche.
"""
import pytest

from app.utils.fecha import dia_operativo
from flota.adaptadores import catalogo
from flota.adaptadores.modelos import Inspeccion, RespuestaItem
from flota.dominio import inspeccion as dom

_H = 'Authorization'


def _auth(token):
    return {_H: f'Bearer {token}'}


@pytest.fixture
def mundo(db, almacen):
    """Un camión con catálogo, un conductor, control de flota y un ajeno."""
    from flask_jwt_extended import create_access_token
    from werkzeug.security import generate_password_hash

    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    veh = Vehiculo(placa='INE300', tipo='camion', activo=True)
    # Un tipo con decisión escrita y sin catálogo sembrado (tanda 3): sirve para
    # comprobar que el 409 dice a quién llamar en vez de reventar con un 500.
    sin_catalogo = Vehiculo(placa='INE301', tipo='motocarro', activo=True)
    cond = Usuario(nombre='Conductor INE', email='ine_cond@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control INE', email='ine_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    tienda = Usuario(nombre='Tienda INE', email='ine_tienda@test.com',
                     password_hash=generate_password_hash('x'), rol='tienda',
                     almacen_id=almacen.id, activo=True)
    # Cerrar un daño es `DECIDE_FLOTA` desde el 2026-09-09: la mitad «quien sí
    # puede, puede» de este archivo necesita un actor de gestión.
    jefe = Usuario(nombre='Gestion INE', email='ine_gestion@test.com',
                   password_hash=generate_password_hash('x'), rol='admin',
                   almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, sin_catalogo, cond, flota, tienda, jefe])
    db.session.commit()
    catalogo.sembrar(db)
    return {
        'placa': veh.placa,
        'placa_sin_catalogo': sin_catalogo.placa,
        'vehiculo_id': veh.id,
        't_cond': create_access_token(identity=str(cond.id)),
        't_flota': create_access_token(identity=str(flota.id)),
        't_tienda': create_access_token(identity=str(tienda.id)),
        't_gestion': create_access_token(identity=str(jefe.id)),
        'usuario_conductor_id': cond.id,
        'db': db,
    }


def _items(client, mundo, token=None):
    r = client.get(f"/flota/inspeccion/items/{mundo['placa']}",
                   headers=_auth(token or mundo['t_cond']))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _cuerpo(items, respuesta=dom.OPTIMO, salvo=None, omitir=(), **extra):
    salvo = salvo or {}
    cuerpo = {
        'respuestas': [
            {'item_id': i['item_id'],
             'respuesta': salvo[i['item_id']] if i['item_id'] in salvo
             else respuesta}
            for i in items if i['item_id'] not in omitir
        ],
        'km': 10_000,
        'segundos_llenado': 95,
    }
    cuerpo.update(extra)
    return cuerpo


def _registrar(client, mundo, token=None, **cuerpo):
    datos = {'placa': mundo['placa']}
    datos.update(cuerpo)
    return client.post('/flota/inspeccion', json=datos,
                       headers=_auth(token or mundo['t_cond']))


class TestSesionObligatoria:
    def test_los_dos_exigen_sesion(self, client, mundo):
        assert client.get(
            f"/flota/inspeccion/items/{mundo['placa']}").status_code == 401
        assert client.post('/flota/inspeccion', json={}).status_code == 401


class TestQuienPuedeQue:
    """La asimetría deliberada: inspeccionar es del que maneja; el desenlace no."""

    def test_el_conductor_SI_ve_los_items_de_hoy(self, client, mundo):
        """Si necesitara un jefe para abrir la lista, la inspección se hace a
        las nueve o no se hace."""
        r = client.get(f"/flota/inspeccion/items/{mundo['placa']}",
                       headers=_auth(mundo['t_cond']))
        assert r.status_code == 200, r.get_json()

    def test_el_conductor_SI_registra_su_inspeccion(self, client, mundo):
        """Es **su** turno y **su** respaldo. Que la registre un admin por él
        convierte la app en un registro *sobre* el conductor hecho por otro."""
        items = _items(client, mundo)['items']
        r = _registrar(client, mundo, **_cuerpo(items))
        assert r.status_code == 201, r.get_json()

    def test_control_de_flota_tambien(self, client, mundo):
        """La otra dirección: un `exige()` que rechazara a todo el mundo pasaría
        los tests de abajo."""
        items = _items(client, mundo, mundo['t_flota'])['items']
        r = _registrar(client, mundo, token=mundo['t_flota'], **_cuerpo(items))
        assert r.status_code == 201, r.get_json()

    def test_un_usuario_de_tienda_no_toca_nada(self, client, mundo):
        """Hasta el 2026-08-03 cualquiera con sesión escribía en flota."""
        assert client.get(f"/flota/inspeccion/items/{mundo['placa']}",
                          headers=_auth(mundo['t_tienda'])).status_code == 403
        assert _registrar(client, mundo, token=mundo['t_tienda'],
                          respuestas=[], km=1,
                          segundos_llenado=10).status_code == 403

    def test_el_conductor_NO_puede_cerrar_el_dano_que_su_inspeccion_produjo(
            self, client, mundo):
        """**El agujero de la regla 11, medido de punta a punta.**

        Sin esto el camino barato es marcar `no_apto`, ver nacer el hallazgo y
        cerrarlo en el mismo minuto: el registro queda perfecto y ningún daño se
        arregla. La guarda vive en los endpoints de hallazgo y este test
        comprueba que el puente nuevo no la rodea.
        """
        items = _items(client, mundo)['items']
        bloqueante = next(i for i in items if i['bloqueante'])
        r = _registrar(client, mundo, **_cuerpo(
            items, salvo={bloqueante['item_id']: dom.NO_APTO}))
        hallazgo_id = r.get_json()['hallazgos'][0]['hallazgo_id']

        cerrar = client.post(f'/flota/hallazgos/{hallazgo_id}/cerrar', json={},
                             headers=_auth(mundo['t_cond']))
        assert cerrar.status_code == 403
        assert cerrar.get_json()['tu_rol'] == 'conductor'

        # Y la otra dirección: quien sí puede, puede. Es gestión desde el
        # 2026-09-09 — control de flota tampoco cierra, porque los días de
        # hallazgo abierto son una de las señales que lo miden a él.
        assert client.post(f'/flota/hallazgos/{hallazgo_id}/cerrar',
                           json={'nota': 'se cambió la pastilla'},
                           headers=_auth(mundo['t_gestion'])).status_code == 200


class TestLoQuePublicaLaListaDelDia:

    def test_cada_item_llega_con_su_gesto_y_su_posicion(self, client, mundo):
        """Sin el gesto la criticidad es decorativa: «revisar frenos» termina en
        un óptimo marcado sin mirar."""
        d = _items(client, mundo)
        assert d['dia'] == dia_operativo().isoformat()
        assert d['plantilla'] == 'camion_v1'
        assert len(d['items']) > 20
        for campo in ('item_id', 'nombre', 'gesto', 'criticidad',
                      'orden_mostrado', 'bloqueante', 'dias_de_plazo'):
            assert campo in d['items'][0], f'la pantalla no puede pintar sin {campo}'
        assert [i['orden_mostrado'] for i in d['items']] == \
            list(range(1, len(d['items']) + 1))

    def test_los_bloqueantes_van_primero_y_se_cuentan_aparte(self, client, mundo):
        """«9 de 28» antes de empezar es lo que hace que la pantalla se lea como
        una tarea de dos minutos y no como un trámite."""
        d = _items(client, mundo)
        n = d['bloqueantes']
        assert n > 0
        assert all(i['bloqueante'] for i in d['items'][:n])
        assert not any(i['bloqueante'] for i in d['items'][n:])

    def test_el_plazo_viaja_calculado_y_no_lo_deduce_la_pantalla(
            self, client, mundo):
        """Regla 6. Deducirlo en el JS sería la cuarta copia de la tabla de
        plazos, y la copia de la pantalla es la que la gente mira."""
        d = _items(client, mundo)
        for i in d['items']:
            assert i['dias_de_plazo'] == dom.dias_de_plazo(i['criticidad'])

    def test_arranca_sin_ninguna_respondida_hoy(self, client, mundo):
        assert _items(client, mundo)['ya_respondidas_hoy'] == []

    def test_despues_de_registrar_la_lista_dice_que_ya_hay_una(
            self, client, mundo):
        """No bloquea la segunda —dos turnos en un día son reales— pero quien
        abra la pantalla tiene que ver que ya hay una, y con qué veredicto."""
        items = _items(client, mundo)['items']
        _registrar(client, mundo, **_cuerpo(items))
        ya = _items(client, mundo)['ya_respondidas_hoy']
        assert len(ya) == 1
        assert ya[0]['veredicto'] == dom.APTO
        assert ya[0]['habilita_despacho'] is True

    def test_una_placa_que_no_existe_es_404(self, client, mundo):
        r = client.get('/flota/inspeccion/items/NOEXISTE',
                       headers=_auth(mundo['t_cond']))
        assert r.status_code == 404

    def test_un_vehiculo_sin_catalogo_sembrado_es_409_y_dice_a_quien_llamar(
            self, client, mundo):
        """Un 500 a las 5 a.m. deja al conductor sin saber si es su teléfono o
        el sistema. El mensaje nombra el script que falta correr."""
        r = client.get(f"/flota/inspeccion/items/{mundo['placa_sin_catalogo']}",
                       headers=_auth(mundo['t_cond']))
        assert r.status_code == 409
        assert 'sembrala' in r.get_json()['error'].lower()


class TestLaFronteraNoAflojaNada:

    def test_faltan_campos_es_400_y_dice_cuales(self, client, mundo):
        r = client.post('/flota/inspeccion', json={'placa': mundo['placa']},
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 400
        for campo in ('km', 'respuestas', 'segundos_llenado'):
            assert campo in r.get_json()['error']

    def test_sin_segundos_llenado_no_se_rellena_con_cero(self, client, mundo):
        """**Regla 11 en la frontera.** Un `0` puesto por el servidor haría ver
        idéntica una inspección que no midió el tiempo y una contestada al
        instante — que es justo el caso que el campo existe para poder ver."""
        items = _items(client, mundo)['items']
        cuerpo = _cuerpo(items)
        del cuerpo['segundos_llenado']
        r = _registrar(client, mundo, **cuerpo)
        assert r.status_code == 400
        assert 'segundos_llenado' in r.get_json()['error']
        assert Inspeccion.query.count() == 0

    def test_los_segundos_llegan_a_la_respuesta_como_hecho(self, client, mundo):
        """La otra dirección: no alcanza con exigirlo, tiene que quedar visible.
        Un campo que nadie mira es el defecto que este módulo lleva toda la
        semana arreglando."""
        items = _items(client, mundo)['items']
        d = _registrar(client, mundo, **_cuerpo(items, segundos_llenado=21)).get_json()
        assert d['segundos_llenado'] == 21
        assert d['items_esperados'] == len(items), (
            'sin los ítems al lado, «21 segundos» no dice nada')

    def test_un_km_no_numerico_es_400(self, client, mundo):
        items = _items(client, mundo)['items']
        r = _registrar(client, mundo, **_cuerpo(items, km='lleno'))
        assert r.status_code == 400

    def test_una_respuesta_inventada_es_409_y_no_una_inspeccion_a_medias(
            self, client, mundo):
        items = _items(client, mundo)['items']
        cuerpo = _cuerpo(items)
        cuerpo['respuestas'][0]['respuesta'] = 'mas o menos'
        r = _registrar(client, mundo, **cuerpo)
        assert r.status_code == 409
        assert Inspeccion.query.count() == 0
        assert RespuestaItem.query.count() == 0

    def test_un_veredicto_mandado_en_el_cuerpo_se_IGNORA(self, client, mundo):
        """**Regla 1 en la frontera.** Recibirlo dejaría que quien responde
        declare `apto` sobre una lista a medias, que es el camino barato de la
        regla 11 con una sola línea de JSON."""
        items = _items(client, mundo)['items']
        bloqueante = next(i for i in items if i['bloqueante'])
        r = _registrar(client, mundo, **_cuerpo(
            items, omitir={bloqueante['item_id']}, veredicto=dom.APTO))
        assert r.status_code == 201
        assert r.get_json()['veredicto'] == dom.INCOMPLETA
        assert r.get_json()['habilita_despacho'] is False

    def test_un_orden_mandado_en_el_cuerpo_se_IGNORA(self, client, mundo):
        """`orden_mostrado` promete ser la pantalla que se vio. Si viniera del
        JSON, el registro afirmaría una pantalla que nadie vio."""
        items = _items(client, mundo)['items']
        cuerpo = _cuerpo(items)
        for r in cuerpo['respuestas']:
            r['orden_mostrado'] = 99
        assert _registrar(client, mundo, **cuerpo).status_code == 201
        assert RespuestaItem.query.filter_by(orden_mostrado=99).count() == 0

    def test_la_firma_sale_del_TOKEN_y_no_del_cuerpo(self, client, mundo):
        """Quién dice haber mirado el camión no lo elige quien manda el JSON.
        Esta firma es la que se lee frente a una aseguradora."""
        items = _items(client, mundo)['items']
        d = _registrar(client, mundo, **_cuerpo(
            items, inspeccionada_por_usuario_id=99_999)).get_json()
        assert d['inspeccionada_por_usuario_id'] == mundo['usuario_conductor_id']

    def test_un_odometro_que_retrocede_es_409(self, client, mundo):
        items = _items(client, mundo)['items']
        _registrar(client, mundo, **_cuerpo(items, km=90_000))
        r = _registrar(client, mundo, **_cuerpo(items, km=100))
        assert r.status_code == 409
        assert Inspeccion.query.count() == 1

    def test_las_horas_viajan_declarando_su_zona(self, client, mundo):
        """Una hora sin zona la interpreta el teléfono como local y corre el
        reloj cinco horas — ya pasó con el recibo del TGZ653."""
        items = _items(client, mundo)['items']
        d = _registrar(client, mundo, **_cuerpo(items)).get_json()
        assert d['respondida_ts'].endswith('+00:00')


class TestElPuenteAlHallazgoLlegaHastaLaPantalla:

    def test_un_no_apto_aparece_en_el_expediente_del_vehiculo(self, client, mundo):
        """De punta a punta: el conductor marca, el daño nace con su plazo y
        quien abra los daños del camión lo ve. Sin este tramo, marcar `no_apto`
        no produce nada visible y el conductor deja de marcar."""
        items = _items(client, mundo)['items']
        item = next(i for i in items if i['criticidad'] == 'mayor')
        d = _registrar(client, mundo, **_cuerpo(
            items, salvo={item['item_id']: dom.NO_APTO})).get_json()

        assert len(d['hallazgos']) == 1
        assert d['hallazgos'][0]['item_id'] == item['item_id']
        assert d['hallazgos'][0]['criticidad'] == 'mayor'

        expediente = client.get(f"/flota/hallazgos/{mundo['placa']}",
                                headers=_auth(mundo['t_flota'])).get_json()
        assert expediente['abiertos'] == 1
        assert expediente['hallazgos'][0]['descripcion'] == item['nombre']
        assert expediente['hallazgos'][0]['vencido'] is False, (
            'un mayor con siete días no puede nacer vencido')

    def test_una_inspeccion_toda_optima_no_deja_ningun_dano(self, client, mundo):
        """La otra dirección. Si el puente creara hallazgos de más, el
        expediente de cada camión se llenaría de ruido en una semana."""
        items = _items(client, mundo)['items']
        d = _registrar(client, mundo, **_cuerpo(items)).get_json()
        assert d['hallazgos'] == []
        expediente = client.get(f"/flota/hallazgos/{mundo['placa']}",
                                headers=_auth(mundo['t_flota'])).get_json()
        assert expediente['abiertos'] == 0

    def test_un_hueco_no_abre_ninguna_tarea(self, client, mundo):
        """«No sé» no le abre a nadie una tarea con fecha límite sobre un daño
        que nadie constató."""
        items = _items(client, mundo)['items']
        d = _registrar(client, mundo, **_cuerpo(
            items, omitir={items[0]['item_id']})).get_json()
        assert d['veredicto'] == dom.INCOMPLETA
        assert d['hallazgos'] == []


class TestElHealthLoCuenta:
    """La medida nace con la pantalla, no un mes después.

    `flota_lectura_odometro` vivió desde el 2026-08-01 con cero campos en el
    health, y por eso nada avisó de las 26 lecturas sin foto. La inspección no
    repite eso.
    """

    def _health(self, client, mundo):
        r = client.get('/flota/health', headers=_auth(mundo['t_flota']))
        assert r.status_code == 200, r.get_json()
        return r.get_json()

    def test_el_vehiculo_sin_inspeccionar_se_cuenta(self, client, mundo):
        d = self._health(client, mundo)
        assert d['vehiculos_sin_inspeccion_hoy'] == 2, (
            'los dos vehículos activos arrancan sin inspección de hoy')
        assert d['inspecciones_incompletas_hoy'] == 0

    def test_inspeccionar_baja_el_contador(self, client, mundo):
        """La otra dirección: un contador que no se mueve es una constante
        plausible, indistinguible de una medición hasta el día que importa."""
        items = _items(client, mundo)['items']
        _registrar(client, mundo, **_cuerpo(items))
        assert self._health(client, mundo)['vehiculos_sin_inspeccion_hoy'] == 1

    def test_una_incompleta_se_cuenta_aparte(self, client, mundo):
        """Contador propio y no sumado: «inspeccionado» e «inspeccionado a
        medias» se atienden distinto, y un solo total esconde el segundo.

        Se registran **dos**: una completa y una a medias. Con una sola, un
        contador que no filtrara por veredicto —que cuenta todas las
        inspecciones del día— daría el mismo 1 y el test pasaría sin medir
        nada. Es la forma exacta de los seis guards en verde de `CLAUDE.md`.
        """
        items = _items(client, mundo)['items']
        _registrar(client, mundo, **_cuerpo(items, km=10_000))
        _registrar(client, mundo, **_cuerpo(
            items, km=10_050, omitir={items[0]['item_id']}))
        d = self._health(client, mundo)
        assert d['inspecciones_incompletas_hoy'] == 1
        assert d['vehiculos_sin_inspeccion_hoy'] == 1, (
            'una inspección incompleta SÍ es una inspección: el vehículo dejó '
            'de estar sin inspeccionar, y por eso hacen falta los dos números')

    def test_los_segundos_se_publican_como_hecho_sin_umbral(self, client, mundo):
        """Regla 13: no hay una sola medición todavía. El health publica cuánto
        tardó la más rápida y sobre cuántos ítems, para poder fijar un techo con
        dato dentro de un mes en vez de a ojo hoy."""
        items = _items(client, mundo)['items']
        _registrar(client, mundo, **_cuerpo(items, segundos_llenado=19))
        s = self._health(client, mundo)['segundos_llenado_30d']
        assert s['n'] == 1
        assert s['minimo']['segundos'] == 19
        assert s['minimo']['items'] == len(items)

    def test_sin_inspecciones_el_campo_dice_POR_QUE_esta_vacio(
            self, client, mundo):
        """`None` ya significa «no hay tabla de dónde sacarlo». Un mismo valor
        con dos significados es el defecto que este módulo persigue."""
        s = self._health(client, mundo)['segundos_llenado_30d']
        assert s['n'] == 0
        assert s['minimo'] is None
        assert 'nota' in s
