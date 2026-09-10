"""
`GET /flota/health` — que responda, que declare, y que ningún campo caiga a 0.

La compuerta del paso 1 no es "el endpoint devuelve 200". Es que la diferencia
entre *medido* y *no medible* sea visible en la respuesta: un número es una
afirmación sobre la flota, `null` es una afirmación sobre el sistema. Un 0 por
defecto las colapsa y convierte el tablero en decoración con autoridad.
"""
from flota.adaptadores.medicion import MedidorSQL
from flota.api.health import _CAMPOS, _CAMPOS_SIN_TABLA

# Las cinco tablas de la tanda 1 ya existen: los doce campos se miden.
# `null` queda reservado para su único significado — la fuente no existe todavía —
# y eso se prueba aparte, quitándole la tabla al medidor.
#: La lista vive en `flota/api/health.py`, junto a `_CAMPOS`, y NO acá.
#: Estaba escrita dos veces —también inline en `test_mundos_incompletos.py`—
#: y agregar un campo exento exigía acordarse de los dos sitios.
#: `TestLaProcedenciaNoDependeDeNingunaTabla` es lo que impide que la
#: exención sea un pase libre: el campo tiene que ganársela.

_CAMPOS_MEDIDOS = tuple(c for c in _CAMPOS if c not in _CAMPOS_SIN_TABLA)


def _get(client, token):
    return client.get('/flota/health', headers={'Authorization': f'Bearer {token}'})


class TestHealthPideSesionDeGestion:
    """No es público: declara ambiente, si los datos son reales, y el inventario
    de lo que el sistema no sabe. Eso es reconocimiento de superficie."""

    def test_sin_token_no_responde(self, client):
        assert client.get('/flota/health').status_code == 401

    def test_un_rol_operativo_no_alcanza(self, client, jwt_token):
        assert _get(client, jwt_token).status_code == 403

    def test_gestion_si_entra(self, client, jwt_token_admin):
        assert _get(client, jwt_token_admin).status_code == 200


class TestHealthResponde:

    def test_responde_200(self, client, jwt_token_admin):
        assert _get(client, jwt_token_admin).status_code == 200

    def test_trae_exactamente_los_campos_de_la_especificacion(self, client, jwt_token_admin):
        cuerpo = _get(client, jwt_token_admin).get_json()
        assert set(cuerpo) == set(_CAMPOS), (
            'La respuesta no coincide con §5 de la especificación.\n'
            f'  sobran:  {sorted(set(cuerpo) - set(_CAMPOS))}\n'
            f'  faltan:  {sorted(set(_CAMPOS) - set(cuerpo))}'
        )

    def test_declara_ambiente_y_si_los_datos_son_reales(self, client, jwt_token_admin):
        cuerpo = _get(client, jwt_token_admin).get_json()
        assert isinstance(cuerpo['ambiente'], str) and cuerpo['ambiente']
        assert isinstance(cuerpo['datos_reales'], bool)


class TestNingunCampoCaeACero:
    """El corazón del paso 1, y sigue siéndolo con las tablas ya creadas."""

    def test_sin_fuente_el_campo_vale_null_y_jamas_cero(self, app, monkeypatch):
        """La propiedad, independiente del esquema que haya hoy.

        Se le quita la tabla al medidor y se exige `None`. Si en vez de eso
        devolviera 0, el tablero diría "0 documentos vencidos" cuando lo que
        pasa es que no hay de dónde saberlo — un 0 es una afirmación sobre la
        flota, `null` es una afirmación sobre el sistema.
        """
        import flota.adaptadores.medicion as med

        monkeypatch.setattr(med, '_tabla_existe', lambda nombre: False)
        medidor = med.MedidorSQL()
        with app.app_context():
            for campo in _CAMPOS_MEDIDOS:
                valor = getattr(medidor, campo)()
                assert valor is None, f'{campo} devolvió {valor!r} sin fuente; debía ser None'

    def test_con_las_tablas_creadas_todos_los_campos_miden(self, client, jwt_token_admin):
        cuerpo = _get(client, jwt_token_admin).get_json()
        sin_medir = [c for c in _CAMPOS_MEDIDOS if cuerpo[c] is None]
        assert not sin_medir, (
            f'Campos en null con su tabla ya creada: {sin_medir}. '
            'Si la tabla existe, la respuesta honesta es un número.'
        )

    def test_el_numero_medido_se_mueve_con_los_datos(self, db):
        """Prueba de que está medido y no escrito a mano.

        Un campo que devuelve una constante plausible es indistinguible de uno
        medido hasta el día en que importa. Se comprueba moviendo el dato.
        """
        from app.models.conductor import Conductor

        medidor = MedidorSQL()
        antes = medidor.conductores_activos_sin_cuenta()

        db.session.add(Conductor(
            nombre='Prueba Sin Cuenta', cedula='FLOTA-T1-0001',
            activo=True, usuario_id=None,
        ))
        db.session.commit()

        assert medidor.conductores_activos_sin_cuenta() == antes + 1

    def test_rutas_historicas_sin_placa_se_mide_no_se_deja_en_null(self, client, jwt_token_admin):
        """§5 lo muestra en `null` = 'aún no medido'. La tabla existe: se mide.

        Un 0 medido y un null son afirmaciones distintas, y esta es la primera.
        Importa porque `decision_ruta` (tanda 3) va a asumir placa, y la columna
        `vehiculo_id` es nullable aunque los dos caminos de creación la exijan.
        """
        assert _get(client, jwt_token_admin).get_json()['rutas_historicas_sin_placa'] is not None


class TestAmbienteNoDiverge:
    """El mismo concepto escrito dos veces diverge; escrito tres, más rápido.

    La política de "a qué Siesa apunta esto" ya vive inline dos veces en
    `app/routes/health.py` (`/ping` y `/siesa`). El paso 1 no toca código
    global, así que `MedidorSQL.ambiente()` es una tercera. Lo que impide que
    diverja es este test — comparar dos implementaciones del mismo concepto es
    el test más barato que hay, y es el que destapó el fallback de 25×.

    Cuando las tres se unifiquen en una sola función, este test se cae solo por
    falta de objeto que comparar. Eso es señal de éxito, no de rotura.
    """

    def test_flota_y_api_health_ping_dicen_el_mismo_ambiente(self, client, jwt_token_admin):
        ping = client.get('/api/health/ping').get_json()
        flota = _get(client, jwt_token_admin).get_json()
        assert flota['ambiente'] == ping['modo'], (
            f"\nDivergieron: /flota/health dice '{flota['ambiente']}' y "
            f"/api/health/ping dice '{ping['modo']}'.\n"
            'Son la misma pregunta. Dos respuestas distintas significan que una miente.'
        )

    def test_datos_reales_solo_es_true_con_produccion_explicito(self, client, jwt_token_admin):
        """Regla 0 aplicada al aviso: se apaga con una afirmación, no con una ausencia."""
        cuerpo = _get(client, jwt_token_admin).get_json()
        assert cuerpo['datos_reales'] is (cuerpo['ambiente'] == 'produccion')


class TestLaProcedenciaNoDependeDeNingunaTabla:
    """El campo tiene que GANARSE su exención, no recibirla.

    `procedencia` está en `_EXENTOS_DE_MEDIR`, así que
    `test_sin_fuente_el_campo_vale_null_y_jamas_cero` no lo mira. Sin esta
    clase, la exención sería un pase libre: el día que alguien le agregue un
    conteo de tablas, el campo empezaría a reventar —o peor, a devolver ceros—
    sin que nada se pusiera rojo.

    La justificación de la exención es que mide el SISTEMA y no la flota. Esto
    la comprueba: sin una sola tabla, sigue contestando entero.
    """

    def test_sin_ninguna_tabla_sigue_contestando_entero(self, app, monkeypatch):
        import flota.adaptadores.medicion as med

        monkeypatch.setattr(med, '_tabla_existe', lambda nombre: False)
        with app.app_context():
            p = med.MedidorSQL().procedencia_del_tablero()
        assert set(p) == {'ambiente', 'datos_reales', 'dia_operativo',
                          'calculado_ts'}
        assert p['ambiente'] and p['dia_operativo'] and p['calculado_ts']

    def test_el_instante_declara_su_huso_y_es_el_de_Bogota(self, app):
        """Regla 5 del WMS. Un tablero que dice «calculado a las 21:30» sin
        decir de qué huso se lee mal exactamente en la franja en que UTC ya
        cambió de día — que es la franja en que se cierra el turno de la tarde.
        """
        from datetime import datetime

        with app.app_context():
            ts = datetime.fromisoformat(
                MedidorSQL().procedencia_del_tablero()['calculado_ts'])
        assert ts.tzinfo is not None, 'el instante salió sin huso'
        assert ts.utcoffset().total_seconds() == -5 * 3600

    def test_el_dia_operativo_es_el_MISMO_que_usan_los_demas_campos(self, app):
        """Si el encabezado dijera un día y las ventanas otro, el tablero se
        contradice consigo mismo en la franja de las 7 p.m. — y las dos
        respuestas se ven igual de plausibles."""
        import flota.adaptadores.medicion as med

        with app.app_context():
            assert (med.MedidorSQL().procedencia_del_tablero()['dia_operativo']
                    == med._hoy().isoformat())
