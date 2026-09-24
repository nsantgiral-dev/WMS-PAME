"""
El mismo día, en orden raro. Lo que pasa cuando la operación no coopera.

El día que sale bien está en `test_flujo_dia_de_conductor.py`. Este archivo
recorre el otro, que es el que ocurre: la inspección **antes** de recibir el
turno, el tanqueo registrado con el turno ya entregado, dos conductores sobre el
mismo camión, y un bloqueante en `no_apto` que nadie sabe si impide algo.

**No se asume cuál es el comportamiento correcto: se mide el que hay**, y cada
clase dice si me parece el que debería ser. Un test que declara «esto debería dar
403» sin haber mirado convierte una suposición en un trinquete.

Igual que el día bueno, todo pasa por los endpoints reales y con el reloj en
horas de Bogotá — varias de estas respuestas cambian de significado según de qué
lado de las 19:00 caiga el gesto.
"""
import pytest

from tests.flujo import conductor_de_flota as arnes


@pytest.fixture
def mundo(db, tmp_path, monkeypatch):
    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    return arnes.sembrar_flota(db)


@pytest.fixture
def reloj(monkeypatch):
    return arnes.Reloj(monkeypatch)


@pytest.fixture
def en_el_patio(client, mundo, reloj):
    """El camión durmió en la sede. Es el arranque en frío: `linea_base`."""
    reloj.en(arnes.DIA_1, (4, 0))
    r = arnes.dejar_en_sede(client, mundo, 100_000)
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _inspeccionar(client, mundo, km, token=None, no_aptos=None, omitir=()):
    items = arnes.items_del_dia(client, mundo, token=token).get_json()['items']
    if no_aptos is None:
        no_aptos = [next(i['item_id'] for i in items if not i['bloqueante'])]
    return items, arnes.responder_inspeccion(
        client, mundo, items, km, no_aptos=no_aptos, omitir=omitir, token=token)


# ═══════════════════════════════════════════════════════════════════════════
class TestInspeccionarAntesDeRecibirElTurno:
    """El conductor abre la app a las 4:30, inspecciona, y **después** recibe.

    **Hasta el 2026-09-24** se aceptaba (201) y la inspección colgaba de la
    custodia abierta a esa hora —la de la sede, `linea_base` en el arranque en
    frío—, así que todos los daños que el conductor encontraba nacían
    preexistentes y no entraban al indicador, **sin que la respuesta lo dijera**.

    **Desde ese día es 403 `sin_derecho`**: el conductor registra solo sobre el
    vehículo de su custodia activa (`flota/api/_permisos.py`, «El rol no
    alcanza»). Resuelve también lo que este archivo marcaba como mal: ya no hay
    un acuse idéntico para un daño que no va a contar, porque ese daño no se
    escribe. La pantalla ya lo pedía así —«Inspección de hoy» solo aparece con
    el turno abierto—, y el mensaje dice la salida: recibir primero.
    """

    def test_sin_turno_abierto_es_403_y_dice_la_salida(
            self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, (4, 30))
        _items, r = _inspeccionar(client, mundo, 100_000)
        assert r.status_code == 403, r.get_json()
        assert r.get_json()['motivo'] == 'sin_derecho'
        assert 'recibí el turno primero' in r.get_json()['error']

    def test_y_no_deja_nada_escrito(self, client, mundo, reloj, en_el_patio):
        """Ni inspección ni daño: un rechazo no deja la mitad de lo rechazado."""
        reloj.en(arnes.DIA_1, (4, 30))
        _inspeccionar(client, mundo, 100_000)
        assert arnes.hallazgos_de(client, mundo).get_json()['hallazgos'] == []

    def test_leer_los_items_antes_de_recibir_SI_se_puede(
            self, client, mundo, reloj, en_el_patio):
        """La lectura del catálogo queda abierta a propósito
        (`SIN_VERIFICAR` en `test_derecho_del_conductor.py`): mirar las
        preguntas no escribe nada."""
        reloj.en(arnes.DIA_1, (4, 30))
        assert arnes.items_del_dia(client, mundo).status_code == 200

    def test_diez_minutos_despues_el_mismo_daño_SI_cuenta(
            self, client, mundo, reloj, en_el_patio):
        """El mismo gesto, el mismo camión, el mismo ítem: lo único que cambió
        es qué botón se apretó primero."""
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        assert arnes.recibir_turno(client, mundo, 100_000).status_code == 201
        reloj.en(arnes.DIA_1, arnes.INSPECCION)
        _items, r = _inspeccionar(client, mundo, 100_000)
        nacido = r.get_json()['hallazgos'][0]

        fila = [h for h in arnes.hallazgos_de(client, mundo).get_json()['hallazgos']
                if h['id'] == nacido['hallazgo_id']][0]
        assert fila['linea_base'] is False
        assert fila['entra_al_indicador'] is True


# ═══════════════════════════════════════════════════════════════════════════
class TestDosConductoresElMismoDia:
    """La conversación es entre ellos dos, y el sistema no la reemplaza.

    Medido y **me parece el que debería ser**, incluida la tercera variante: el
    guard que se cerró el 2026-09-02 —`mismo_custodio` exigía además que quien
    pide SEA el custodio, y no solo que lo declare en el cuerpo— aguanta por
    HTTP, que es por donde entra el ataque.
    """

    @pytest.fixture
    def a_tiene_el_camion(self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        r = arnes.recibir_turno(client, mundo, 100_000)
        assert r.status_code == 201, r.get_json()
        reloj.en(arnes.DIA_1, (5, 30))
        return r.get_json()

    def test_B_no_puede_recibir_lo_que_tiene_A(self, client, mundo,
                                               a_tiene_el_camion):
        r = arnes.recibir_turno(client, mundo, 100_010,
                                conductor=mundo['conductor_b'],
                                token=mundo['t_b'])
        assert r.status_code == 409
        assert 'Conductor A' in r.get_json()['error'], (
            'el rechazo tiene que decir a quién llamar, no solo que no se puede')

    def test_B_declarandose_A_en_el_cuerpo_tampoco(self, client, mundo,
                                                   a_tiene_el_camion):
        """El atajo cerrado el 2026-09-02: B mandaba «el que recibe es A», el
        no-op se activaba, `puede_recibir` no se evaluaba nunca, y B le cerraba
        el turno a A con un `km_fin` inventado **y sin marca de forzado**."""
        r = arnes.recibir_turno(client, mundo, 100_010,
                                conductor=mundo['conductor_a'],
                                token=mundo['t_b'])
        # Desde el 2026-09-24 lo frena antes otra puerta, con 403: un conductor
        # solo deja el turno a SU nombre, y B está nombrando a A.
        assert r.status_code == 403, r.get_json()
        assert r.get_json()['motivo'] == 'sin_derecho'

    def test_B_tampoco_puede_entregar_el_vehiculo_de_A(self, client, mundo,
                                                       a_tiene_el_camion):
        """Entregar a la sede es la otra puerta a la misma operación: cierra la
        custodia de A. Un guard que solo mirara el recibo dejaría esta abierta."""
        r = arnes.entregar_turno(client, mundo, 100_010, token=mundo['t_b'])
        # 403 desde el 2026-09-24: a la sede solo manda un camión quien lo tiene.
        assert r.status_code == 403, r.get_json()

    def test_ni_A_ni_B_dejaron_rastro_de_cierre_forzado(self, client, mundo,
                                                        a_tiene_el_camion):
        """Los tres intentos fallaron, así que no hay nada que registrar. Si
        alguno hubiera pasado, esto lo delataría — es el detector en la otra
        dirección: nadie firmó y la custodia de A sigue abierta."""
        for intento in (
            lambda: arnes.recibir_turno(client, mundo, 100_010,
                                        conductor=mundo['conductor_b'],
                                        token=mundo['t_b']),
            lambda: arnes.entregar_turno(client, mundo, 100_010,
                                         token=mundo['t_b']),
        ):
            intento()
        abiertas = [c for c in arnes.custodias_de(mundo['vehiculo_id'])
                    if c.fin_ts is None]
        assert len(abiertas) == 1
        assert abiertas[0].custodio_conductor_id == mundo['conductor_a']
        assert arnes.health(client, mundo)['custodias_cerradas_forzadas'] == 0

    def test_control_de_flota_SI_fuerza_con_motivo_y_queda_con_su_nombre(
            self, client, mundo, a_tiene_el_camion):
        """**Cambió el 2026-09-24, y a propósito.**

        Hasta ese día este test medía un 409: el traspaso resolvía `quien_pide`
        con `_es_gestion()` y control de flota caía en CONDUCTOR. Y la pantalla
        de escritorio le ofrecía el campo «Motivo del cierre forzado» igual: le
        mostraba un gesto que el sistema le negaba.

        Se decidió darle el permiso —el patio lo opera él, y a las 5 a.m. gestión
        no está—, pero más estricto que al admin: a él las fotos de cierre no lo
        eximen, así que todo cierre ajeno pide motivo y queda contado en
        «cierres forzados», que es justamente la señal con la que se lo mide
        (`_permisos.FUERZA_CIERRE`, `Veredicto.fotos_no_eximen`).
        """
        sin = arnes.recibir_turno(client, mundo, 100_010,
                                  conductor=mundo['conductor_b'],
                                  token=mundo['t_flota'])
        assert sin.status_code == 409 and 'motivo' in sin.get_json()['error']

        r = arnes.recibir_turno(client, mundo, 100_010,
                                conductor=mundo['conductor_b'],
                                token=mundo['t_flota'],
                                motivo_forzado='A se fue sin cerrar y hay ruta')
        assert r.status_code == 201, r.get_json()
        assert arnes.health(client, mundo)['custodias_cerradas_forzadas'] == 1

    def test_un_admin_de_zona_SI_puede_forzar_y_queda_con_nombre(
            self, client, mundo, a_tiene_el_camion):
        """La única salida cuando alguien se fue sin cerrar. Exige motivo
        escrito y sale en `cierres-forzados` con quién lo autorizó."""
        r = arnes.recibir_turno(client, mundo, 100_010,
                                conductor=mundo['conductor_b'],
                                token=mundo['t_admin'],
                                motivo_forzado='A se fue sin cerrar y hay ruta')
        assert r.status_code == 201, r.get_json()

        cierres = client.get(
            '/flota/custodia/cierres-forzados',
            headers={'Authorization': f"Bearer {mundo['t_flota']}"}).get_json()
        assert len(cierres['cierres']) == 1
        assert cierres['cierres'][0]['lo_tenia'] == 'Conductor A'
        assert cierres['cierres'][0]['motivo']
        assert arnes.health(client, mundo)['custodias_cerradas_forzadas'] == 1

    def test_forzar_sin_motivo_escrito_no_pasa(self, client, mundo,
                                               a_tiene_el_camion):
        """Sin fotos de cierre, el motivo es lo único que va a explicar después
        por qué el turno siguiente arrancó sin comparación."""
        r = arnes.recibir_turno(client, mundo, 100_010,
                                conductor=mundo['conductor_b'],
                                token=mundo['t_admin'])
        assert r.status_code == 409, r.get_json()
        assert 'motivo escrito' in r.get_json()['error']


# ═══════════════════════════════════════════════════════════════════════════
class TestElBloqueanteNoAptoNoImpideNada:
    """Medido: el camión sale, tanquea, entrega y lo reciben al día siguiente.

    **Y me parece el que debería ser hoy.** Es la secuencia obligatoria del
    módulo —medir, corregir, imponer— y está escrita en tres docstrings: si el
    primer día la app deja un camión en patio por un ítem mal llenado, la
    operación desmonta el sistema en 48 horas.

    Lo que **no** me parece está en `test_flujo_dia_de_conductor.py`
    (`test_el_health_no_distingue_un_bloqueante_no_apto_de_hoy`): que se
    publique y no bloquee es una decisión; que el tablero no pueda distinguirlo
    de un guardabarros rayado hasta el día siguiente es un hueco.
    """

    @pytest.fixture
    def con_bloqueante_caido(self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        assert arnes.recibir_turno(client, mundo, 100_000).status_code == 201
        reloj.en(arnes.DIA_1, arnes.INSPECCION)
        items = arnes.items_del_dia(client, mundo).get_json()['items']
        bloqueante = next(i['item_id'] for i in items if i['bloqueante'])
        r = arnes.responder_inspeccion(client, mundo, items, 100_000,
                                       no_aptos=[bloqueante])
        assert r.get_json()['veredicto'] == 'no_apto'
        assert r.get_json()['habilita_despacho'] is False
        return r.get_json()

    def test_el_tanqueo_del_mediodia_pasa(self, client, mundo, reloj,
                                          con_bloqueante_caido):
        reloj.en(arnes.DIA_1, arnes.TANQUEO)
        assert arnes.tanquear(client, mundo, 100_100).status_code == 201

    def test_la_entrega_de_la_noche_pasa(self, client, mundo, reloj,
                                         con_bloqueante_caido):
        reloj.en(arnes.DIA_1, arnes.ENTREGA_TURNO)
        assert arnes.entregar_turno(client, mundo, 100_180).status_code == 201

    def test_el_dia_siguiente_lo_reciben_igual(self, client, mundo, reloj,
                                               con_bloqueante_caido):
        reloj.en(arnes.DIA_1, arnes.ENTREGA_TURNO)
        arnes.entregar_turno(client, mundo, 100_180)
        reloj.en(arnes.DIA_2, arnes.RECIBO_TURNO)
        r = arnes.recibir_turno(client, mundo, 100_180,
                                conductor=mundo['conductor_b'],
                                token=mundo['t_b'])
        assert r.status_code == 201, r.get_json()

    def test_el_hallazgo_bloqueante_vence_al_terminar_el_dia_de_Bogota(
            self, client, mundo, reloj, con_bloqueante_caido):
        """Bloqueante = mismo día (regla 6). A las 20:00 de Bogotá todavía no
        está vencido —aunque en UTC ya sea mañana— y a las 8 del día siguiente
        sí. Es el contraste que hace visible el defecto de `dias_abierto`: el
        plazo sí está en Bogotá."""
        nacido = con_bloqueante_caido['hallazgos'][0]['hallazgo_id']

        reloj.en(arnes.DIA_1, arnes.ENTREGA_TURNO)
        de_noche = [h for h in arnes.hallazgos_de(client, mundo).get_json()['hallazgos']
                    if h['id'] == nacido][0]
        assert de_noche['vencido'] is False

        reloj.en(arnes.DIA_2, (8, 0))
        de_mañana = [h for h in arnes.hallazgos_de(client, mundo).get_json()['hallazgos']
                     if h['id'] == nacido][0]
        assert de_mañana['vencido'] is True
        assert arnes.health(client, mundo)['hallazgos_vencidos'] == 1


# ═══════════════════════════════════════════════════════════════════════════
class TestElTanqueoQueSeRegistraTarde:
    """El caso que el propio módulo declara habitual: *«un tanqueo que solo
    puede registrar un jefe se registra el lunes, y para el lunes la factura ya
    se perdió — que es literalmente lo que pasa hoy»*.

    **Hasta el 2026-09-24** el conductor lo registraba después de entregar: el km
    real del surtidor daba 409 (el odómetro no decrece), el km de cierre pasaba
    —un número que nadie leyó en el tablero, colgado de la lectura `entrega`— y
    la salida que sugería el error, `origen=correccion`, borraba la historia del
    vehículo.

    **Desde ese día el conductor no registra sobre un camión que ya entregó**
    (403 `sin_derecho`) ni corrige odómetros (`MAESTROS_FLOTA`). Las dos salidas
    malas le quedaron cerradas a él. El tanqueo tardío lo registra control de
    flota con la factura en la mano — y ahí **el hueco sigue**: el km real
    todavía da 409 y el de cierre todavía pasa sin marca. Falta la tercera
    salida que ya se nombraba acá: exponer `lectura_id` en `POST /flota/tanqueos`
    para anclar el gasto a una lectura que ya existe. Declarado, no arreglado.
    """

    @pytest.fixture
    def turno_entregado(self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        assert arnes.recibir_turno(client, mundo, 100_000).status_code == 201
        reloj.en(arnes.DIA_1, arnes.ENTREGA_TURNO)
        assert arnes.entregar_turno(client, mundo, 100_400).status_code == 201
        reloj.en(arnes.DIA_1, (21, 0))

    def test_el_conductor_ya_no_lo_registra_despues_de_entregar(
            self, client, mundo, turno_entregado):
        for km in (100_320, 100_400):
            r = arnes.tanquear(client, mundo, km, galones='10')
            assert r.status_code == 403, (km, r.get_json())
            assert r.get_json()['motivo'] == 'sin_derecho'

    def test_ni_por_la_correccion_que_borraba_la_historia(
            self, client, mundo, turno_entregado):
        r = client.post('/flota/odometro', json={
            'placa': mundo['placa'], 'valor_km': 100_320, 'origen': 'correccion',
            'motivo_correccion': 'tanqueo de las 11 registrado después del cierre',
        }, headers={'Authorization': f"Bearer {mundo['t_a']}"})
        assert r.status_code == 403, r.get_json()
        assert arnes.health(client, mundo)['km_dia_por_vehiculo'][0]['n'] >= 3

    def test_control_de_flota_lo_registra_pero_el_hueco_del_km_sigue(
            self, client, mundo, turno_entregado):
        """Lo que queda abierto, medido para que no se lea como cerrado."""
        real = arnes.tanquear(client, mundo, 100_320, galones='10',
                              token=mundo['t_flota'])
        assert real.status_code == 409
        assert 'no puede decrecer' in real.get_json()['error']

        prestado = arnes.tanquear(client, mundo, 100_400, galones='10',
                                  token=mundo['t_flota'])
        assert prestado.status_code == 201, prestado.get_json()
        lectura = [l for l in arnes.lecturas_de(mundo['vehiculo_id'])
                   if l.id == prestado.get_json()['lectura_id']][0]
        assert lectura.origen == 'entrega'


# ═══════════════════════════════════════════════════════════════════════════
class TestLaInspeccionAMedias:
    """`sin_dato` no es `no_apto`: una dice «no sé» y la otra «sé que está mal».

    Medido y me parece el que debería ser. Se incluye acá porque es el caso que
    conecta las dos etapas: la inspección incompleta **no** abre tareas con
    fecha límite sobre daños que nadie constató, y el health la cuenta aparte.
    """

    def test_un_item_sin_contestar_deja_la_inspeccion_incompleta(
            self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        arnes.recibir_turno(client, mundo, 100_000)
        reloj.en(arnes.DIA_1, arnes.INSPECCION)
        items = arnes.items_del_dia(client, mundo).get_json()['items']
        r = arnes.responder_inspeccion(client, mundo, items, 100_000,
                                       no_aptos=[], omitir=[items[0]['item_id']])
        d = r.get_json()
        assert d['veredicto'] == 'incompleta'
        assert d['habilita_despacho'] is False
        assert d['items_sin_dato'] == 1
        assert d['hallazgos'] == [], (
            '«no sé» no le abre a nadie una tarea con fecha límite sobre un '
            'daño que nadie constató')

        h = arnes.health(client, mundo)
        assert h['inspecciones_incompletas_hoy'] == 1
        assert h['hallazgos_abiertos'] == 0
        assert h['vehiculos_sin_inspeccion_hoy'] == 0, (
            'una incompleta también es una inspección: contarla en los dos '
            'cubos impediría saber cuántos camiones se miraron')
