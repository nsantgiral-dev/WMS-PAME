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

    Pasa de verdad: la inspección es el gesto que tiene el camión delante, y el
    botón de recibir turno está en otra pantalla.

    Medido: se acepta (201) y la inspección **cuelga de la custodia que esté
    abierta**, que a esa hora es la de la sede. En el arranque en frío esa
    custodia es `linea_base`, así que **todos los daños que el conductor
    encuentre nacen preexistentes y no entran al indicador**.

    ¿Me parece el que debería ser? La mitad. Que un daño encontrado durante el
    levantamiento no se le cuente a nadie es la regla escrita y es correcta. Lo
    que no me parece es que **no se diga**: la respuesta del POST publica
    `hallazgos: [{hallazgo_id, item_id, nombre, criticidad, nota}]` y ninguno de
    esos campos dice `linea_base` ni `entra_al_indicador`. El conductor ve que su
    reporte llegó; nadie ve que no va a contar. Y el mismo gesto diez minutos
    después —con el turno ya recibido— produce un daño que sí cuenta.
    """

    def test_se_acepta_sin_turno_abierto(self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, (4, 30))
        _items, r = _inspeccionar(client, mundo, 100_000)
        assert r.status_code == 201, r.get_json()

    def test_la_inspeccion_cuelga_de_la_custodia_de_la_sede(
            self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, (4, 30))
        _items, r = _inspeccionar(client, mundo, 100_000)
        assert r.get_json()['custodia_id'] == en_el_patio['custodia_id']

    def test_sus_hallazgos_nacen_linea_base_y_no_entran_al_indicador(
            self, client, mundo, reloj, en_el_patio):
        """El costo concreto: el daño existe, tiene fecha límite y **no cuenta**."""
        reloj.en(arnes.DIA_1, (4, 30))
        _items, r = _inspeccionar(client, mundo, 100_000)
        nacido = r.get_json()['hallazgos'][0]

        fila = [h for h in arnes.hallazgos_de(client, mundo).get_json()['hallazgos']
                if h['id'] == nacido['hallazgo_id']][0]
        assert fila['linea_base'] is True
        assert fila['entra_al_indicador'] is False
        assert fila['estado'] == 'abierto' and fila['fecha_limite']

    def test_la_respuesta_del_POST_no_dice_que_no_va_a_contar(
            self, client, mundo, reloj, en_el_patio):
        """**Lo que me parece que está mal.** El conductor recibe un acuse
        idéntico al del daño que sí cuenta.

        Si mañana se publica `entra_al_indicador` en esta lista, este test
        empieza a fallar: hay que borrarlo, no aflojarlo.
        """
        reloj.en(arnes.DIA_1, (4, 30))
        _items, r = _inspeccionar(client, mundo, 100_000)
        assert set(r.get_json()['hallazgos'][0]) == {
            'hallazgo_id', 'item_id', 'nombre', 'criticidad', 'nota'}

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
        assert r.status_code == 409, r.get_json()

    def test_B_tampoco_puede_entregar_el_vehiculo_de_A(self, client, mundo,
                                                       a_tiene_el_camion):
        """Entregar a la sede es la otra puerta a la misma operación: cierra la
        custodia de A. Un guard que solo mirara el recibo dejaría esta abierta."""
        r = arnes.entregar_turno(client, mundo, 100_010, token=mundo['t_b'])
        assert r.status_code == 409, r.get_json()

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

    def test_control_de_flota_NO_puede_forzar_y_el_mensaje_lo_dice(
            self, client, mundo, a_tiene_el_camion):
        """**Medido, y no era lo que yo esperaba.**

        `control_flota` está en `MAESTROS_FLOTA` y en `LECTURA_FLOTA`, así que
        entra al endpoint — pero el traspaso resuelve `quien_pide` con
        `_es_gestion()`, que **no** lo incluye. Son dos nociones de autoridad
        distintas dentro del mismo módulo, y acá gana la conservadora: Yesid,
        que es quien tiene el camión delante, recibe el mismo 409 que un
        conductor cualquiera aunque escriba el motivo.

        Me parece el que debería ser, y por lo que dice el propio módulo:
        *«Yesid no ordena, señala plazos vencidos y escala»* — cerrar el turno
        de otro es una decisión, no un levantamiento de campo. Lo que lo hace
        aceptable es que **el mensaje nombra la salida**: dice que un admin de
        zona puede forzarlo. Sin esa frase esto sería un callejón.
        """
        r = arnes.recibir_turno(client, mundo, 100_010,
                                conductor=mundo['conductor_b'],
                                token=mundo['t_flota'],
                                motivo_forzado='A se fue sin cerrar y hay ruta')
        assert r.status_code == 409
        assert 'admin de zona' in r.get_json()['error']

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

    Medido: si el conductor lo registra **después** de entregar el turno, el
    kilometraje real del surtidor ya es menor que el de cierre y el endpoint lo
    rechaza con 409. Lo que **sí** acepta es el kilometraje de cierre — o sea, un
    número que nadie leyó en el tablero, que entra al CPK y al rendimiento.

    ¿Me parece el que debería ser? El 409 sí: el odómetro no puede decrecer y
    eso protege la serie. Lo que no me parece es que **no haya una tercera
    salida**. `registrar_gasto` ya recibe `lectura_id` —y `taller.py:612` lo
    usa— pero ni `registrar_tanqueo` ni `POST /flota/tanqueos` lo exponen: el
    único gasto que se registra en campo es justamente el único que no puede
    anclarse a una lectura que ya existe.

    La otra salida que el mensaje de error sugiere —`origen=correccion`— existe
    y es peor: declara falsa una lectura que era correcta, y
    `vigentes_tras_la_ultima_correccion` descarta con ella toda la historia
    anterior del vehículo.
    """

    @pytest.fixture
    def turno_entregado(self, client, mundo, reloj, en_el_patio):
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        assert arnes.recibir_turno(client, mundo, 100_000).status_code == 201
        reloj.en(arnes.DIA_1, arnes.ENTREGA_TURNO)
        assert arnes.entregar_turno(client, mundo, 100_400).status_code == 201
        reloj.en(arnes.DIA_1, (21, 0))

    def test_el_km_real_del_surtidor_se_rechaza(self, client, mundo,
                                                turno_entregado):
        r = arnes.tanquear(client, mundo, 100_320, galones='10')
        assert r.status_code == 409
        assert 'no puede decrecer' in r.get_json()['error']

    def test_el_km_de_cierre_se_acepta_y_nada_marca_que_es_prestado(
            self, client, mundo, turno_entregado):
        """La salida barata, y la que la operación va a tomar: reusar el número
        del cierre. La lectura ni siquiera nace nueva —`anclar_odometro`
        reutiliza la de la entrega porque el kilometraje coincide—, así que el
        gasto queda colgado de una lectura de **origen `entrega`** y nada dice
        que el tanqueo fue seis horas antes."""
        r = arnes.tanquear(client, mundo, 100_400, galones='10')
        assert r.status_code == 201, r.get_json()

        lectura = [l for l in arnes.lecturas_de(mundo['vehiculo_id'])
                   if l.id == r.get_json()['lectura_id']][0]
        assert lectura.origen == 'entrega', (
            'el gasto de combustible quedó colgado de la lectura del cierre de '
            'turno; ninguna columna dice que el tanqueo ocurrió antes')

    def test_la_salida_que_el_mensaje_sugiere_borra_la_historia(
            self, client, mundo, turno_entregado):
        """*«Si el valor es correcto, se registra con origen=correccion y
        motivo»*. Se puede — y deja el ritmo del vehículo con dos lecturas.

        `vigentes_tras_la_ultima_correccion` existe para que una lectura
        envenenada no trabe el vehículo para siempre, y hace exactamente eso:
        una corrección declara «de acá en adelante, esto es lo cierto». Usarla
        para registrar un tanqueo tardío tira por la borda el histórico de un
        camión que no tenía nada malo.
        """
        antes = arnes.health(client, mundo)['km_dia_por_vehiculo'][0]
        assert antes['n'] >= 3

        r = client.post('/flota/odometro', json={
            'placa': mundo['placa'], 'valor_km': 100_320, 'origen': 'correccion',
            'motivo_correccion': 'tanqueo de las 11 registrado después del cierre',
        }, headers={'Authorization': f"Bearer {mundo['t_a']}"})
        assert r.status_code == 201, r.get_json()

        despues = arnes.health(client, mundo)['km_dia_por_vehiculo'][0]
        assert despues['n'] == 1, (
            'la corrección dejó la serie vigente en una sola lectura: el km/día '
            'del vehículo se perdió por registrar un tanqueo tarde')
        assert despues['km_dia'] == 'sin_dato'


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
