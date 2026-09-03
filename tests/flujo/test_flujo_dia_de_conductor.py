"""
El día completo de un conductor, por HTTP, y **el día siguiente**.

## Qué hueco tapa

`flota/` tiene 21 tablas y 44 endpoints, y cada tanda se probó sola: su propio
fixture, su propia `Custodia(...)`, sus propios datos fabricados coherentes. Es
la misma forma que `CLAUDE.md` documenta en «Auditoría de invariantes de
frontera» — *«cada archivo arma su propia `TareaPacking(...)` desde cero… No es
un fallo de los tests, es su forma»*. Lo que **ninguna** de esas tandas puede
fallar por es un defecto de frontera: qué deja cada etapa que la siguiente
necesita.

Acá el día se recorre entero por los endpoints reales (`tests/flujo/
conductor_de_flota.py`), en horas de operación de Bogotá, y lo que se verifica
no es que cada paso dé 201 — es lo que quedó escrito entre paso y paso.

## Las horas no son decorado

El módulo guarda `datetime.utcnow()` y el día operativo es Bogotá (UTC−5). Un
test que use las 08:00 UTC vive en la única franja donde las dos fechas
coinciden: ahí un defecto de frontera de día **no se puede ver**. Este archivo
cierra el turno a las **20:00 de Bogotá**, que en UTC ya es mañana, y ahí es
donde aparecen dos de los tres defectos registrados abajo.

## Lo que encontró y quedó registrado

· `custodias_sin_foto_completa` cuenta como incompleta **toda** custodia de un
  día bien hecho — `xfail(strict)` en `TestElHealthAlFinalDelDia`.
· `dias_abierto` de un hallazgo cuenta días **UTC**, no de Bogotá — dos
  `xfail(strict)` en `TestElDiaDelHallazgoSeCuentaEnUTC`.
· `GET /flota/hallazgos/<placa>` no publicaba `item_id`: **arreglado**, con su
  test acá.
· `km_dia_por_vehiculo[].marca` publicaba `"Confianza.DECLARADA"` mientras
  `cpk_mes[].marca` publicaba `"declarada"` en el mismo JSON: **arreglado**, con
  su test acá.
"""
import ast
from datetime import datetime, timedelta

import pytest

from tests.flujo import conductor_de_flota as arnes


@pytest.fixture
def mundo(db, tmp_path, monkeypatch):
    """El maestro: un camión, dos conductores con cuenta, control de flota."""
    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    return arnes.sembrar_flota(db)


@pytest.fixture
def reloj(monkeypatch):
    return arnes.Reloj(monkeypatch)


@pytest.fixture
def dia1(client, mundo, reloj):
    """El camión durmió en el patio y el conductor A hace su día completo."""
    reloj.en(arnes.DIA_1, (4, 0))
    r = arnes.dejar_en_sede(client, mundo, 100_000)
    assert r.status_code == 201, r.get_json()
    return arnes.dia_completo(client, mundo, dia=arnes.DIA_1, reloj=reloj,
                              km_apertura=100_000, km_dia=180)


@pytest.fixture
def dia2(client, mundo, reloj, dia1):
    """El día siguiente, con el OTRO conductor: el vehículo no es de nadie."""
    return arnes.dia_completo(
        client, mundo, dia=arnes.DIA_2, reloj=reloj,
        km_apertura=dia1['km']['entrega'], km_dia=210,
        conductor=mundo['conductor_b'], token=mundo['t_b'])


# ═══════════════════════════════════════════════════════════════════════════
class TestElArnesEsElQueDice:
    """Un arnés que escribe filas prueba que la base las acepta. Nada más.

    Los dos sentidos, porque un detector ciego prueba la mitad: que **no** llame
    a los adaptadores de escritura, y que **sí** vaya por el cliente HTTP.
    """

    @staticmethod
    def _llamadas(modulo):
        fuente = ast.parse(open(modulo.__file__, encoding='utf-8').read())
        nombres = set()
        for n in ast.walk(fuente):
            if isinstance(n, ast.Call):
                f = n.func
                nombres.add(f.attr if isinstance(f, ast.Attribute)
                            else getattr(f, 'id', ''))
        return nombres

    def test_no_llama_a_ningun_adaptador_de_escritura(self):
        """`traspaso.traspasar` avanzaría la etapa sin pasar por la frontera —
        y la frontera es donde vive la mitad de la política (permisos, el
        `quien_pide` que sale del ROL, la traducción ubicación→custodio)."""
        prohibidas = {'traspasar', 'registrar_tanqueo', 'registrar_gasto',
                      'anclar_odometro', 'verificar', 'abrir'}
        assert self._llamadas(arnes) & prohibidas == set()

    def test_avanza_las_etapas_por_el_cliente_HTTP(self):
        """El otro sentido. Si el arnés dejara de hacer requests, el test de
        arriba seguiría en verde sobre un arnés que no ejerce nada."""
        assert {'post', 'get'} <= self._llamadas(arnes)

    def test_el_reloj_cubre_todo_flota_que_guarda_la_hora(self):
        """Un módulo nuevo que enlace `datetime` y no esté declarado deja de
        estar bajo el reloj **en silencio**, y el día volvería a correr con la
        hora de la máquina sin que nada falle."""
        assert arnes.Reloj.verificar_cobertura() == [], (
            'hay módulos de flota que guardan la hora y no están ni bajo el '
            'reloj ni declarados fuera con su motivo')

    def test_el_dia_se_recorre_en_horas_reales_y_no_a_las_08_UTC(self, reloj):
        """Las 08:00 UTC son las 03:00 de Bogotá: la única franja del día en
        que la fecha UTC y la de Bogotá coinciden. Un día entero ahí adentro no
        puede ver un defecto de frontera de día."""
        from app.utils.fecha import dia_operativo

        reloj.en(arnes.DIA_1, arnes.ENTREGA_TURNO)
        assert reloj.ahora.date() == arnes.DIA_2, 'en UTC ya es mañana'
        assert dia_operativo() == arnes.DIA_1, 'y el día operativo sigue siendo hoy'

    def test_la_hora_que_el_reloj_dice_es_la_que_queda_en_la_fila(self, dia1,
                                                                  mundo):
        """El arnés se rompió a sí mismo una vez: al parchear la clase de la
        stdlib, el dialecto SQLite de SQLAlchemy dejó de reconocer un `datetime`
        como tal y guardó **todas** las lecturas a las 00:00:00.

        Un arnés escrito para cazar defectos de hora que borra la hora está en
        verde afirmando nada. `_MetaFecha` lo arregla; esto lo vigila.
        """
        horas = {l.ts.time() for l in arnes.lecturas_de(mundo['vehiculo_id'])}
        assert len(horas) > 1 and all(h.hour or h.minute for h in horas), (
            f'las lecturas quedaron con la hora en cero: {sorted(horas)}')


# ═══════════════════════════════════════════════════════════════════════════
class TestLoQueCadaEtapaLeDejaALaSiguiente:
    """El único lugar donde estas preguntas se pueden hacer."""

    def test_la_inspeccion_se_ancla_a_la_lectura_del_recibo(self, dia1, mundo):
        """No se crea una segunda lectura para el mismo número de tablero.

        Alguien miró el tablero UNA vez a las 5 a.m. Dos filas con el mismo
        kilometraje separadas por diez minutos son el ruido que
        `lecturas_ts_duplicado` existe para contar, producido por el sistema en
        vez de por un reintento.
        """
        lecturas = arnes.lecturas_de(mundo['vehiculo_id'])
        del_recibo = [l for l in lecturas
                      if l.valor_km == dia1['km']['recibo']
                      and l.origen == 'entrega'
                      and l.ts == arnes.instante_utc(arnes.DIA_1,
                                                     arnes.RECIBO_TURNO)]
        assert len(del_recibo) == 1
        assert dia1['inspeccion']['lectura_id'] == del_recibo[0].id, (
            'la inspección se colgó de otra lectura: el km del recibo y el de '
            'la inspección son el mismo gesto')

    def test_el_dia_no_produce_lecturas_con_el_mismo_segundo(self, dia1, client,
                                                             mundo):
        """`lecturas_ts_duplicado` cuenta diez filas en producción, del
        reintento del 2026-08-03. Un día completo no tiene que aportar ninguna."""
        assert arnes.health(client, mundo)['lecturas_ts_duplicado'] == 0

    def test_el_dia_deja_exactamente_cuatro_lecturas(self, dia1, mundo):
        """Recibo (= inspección), daño, tanqueo, entrega. Ni una de relleno.

        El número importa: si alguna etapa empezara a fabricar su propia
        lectura, este contador sube y nadie más lo mira.
        """
        del_dia = [l for l in arnes.lecturas_de(mundo['vehiculo_id'])
                   if l.ts >= arnes.instante_utc(arnes.DIA_1, arnes.RECIBO_TURNO)]
        assert [(l.valor_km, l.origen) for l in del_dia] == [
            (dia1['km']['recibo'], 'entrega'),
            (dia1['km']['dano'], 'hallazgo'),
            (dia1['km']['tanqueo'], 'tanqueo'),
            (dia1['km']['entrega'], 'entrega'),
        ]

    def test_el_hallazgo_de_la_inspeccion_hereda_criticidad_y_calcula_su_plazo(
            self, dia1, client, mundo):
        """Regla 6: el plazo sale de la criticidad y **no se elige a mano**.

        La criticidad la trae el ítem del catálogo, no el conductor: por eso se
        compara contra la del ítem que la propia pantalla publicó.
        """
        from app.utils.fecha import inicio_del_dia_utc
        from flota.dominio.inspeccion import dia_limite

        nacido = dia1['inspeccion']['hallazgos'][0]
        item = [i for i in dia1['items']['items']
                if i['item_id'] == nacido['item_id']][0]
        assert nacido['criticidad'] == item['criticidad']

        listado = arnes.hallazgos_de(client, mundo).get_json()['hallazgos']
        fila = [h for h in listado if h['id'] == nacido['hallazgo_id']][0]
        esperado = inicio_del_dia_utc(
            dia_limite(arnes.DIA_1, item['criticidad']) + timedelta(days=1))
        assert datetime.fromisoformat(fila['fecha_limite']).replace(
            tzinfo=None) == esperado, (
            'la fecha límite no es la medianoche de Bogotá del día siguiente '
            'al último a tiempo')

    def test_el_hallazgo_de_la_inspeccion_dice_de_que_item_nacio(
            self, dia1, client, mundo):
        """**El defecto que este archivo arregló.**

        El POST de la inspección devuelve `hallazgos: [{item_id, nombre, …}]`;
        `GET /flota/hallazgos/<placa>` —la lista que alguien abre mañana— no
        publicaba `item_id`. «Sudado de aceite» quedaba indistinguible de algo
        que alguien tecleó a mano, y los otros dos vínculos del hallazgo
        (`lectura_id`, `custodia_id`) sí viajaban.
        """
        nacido = dia1['inspeccion']['hallazgos'][0]
        listado = arnes.hallazgos_de(client, mundo).get_json()['hallazgos']
        fila = [h for h in listado if h['id'] == nacido['hallazgo_id']][0]
        assert fila['item_id'] == nacido['item_id']

        a_mano = [h for h in listado if h['id'] == dia1['hallazgo']['id']][0]
        assert a_mano['item_id'] is None, (
            'un daño reportado a mano no nació de ningún ítem: `null` es el '
            'dato, no un hueco')

    def test_el_hallazgo_del_dia_cuelga_del_turno_abierto(self, dia1):
        """`custodia_id` es bajo la custodia de quién apareció el daño — un
        hecho, no una imputación (regla 2). Si colgara de la custodia de la
        sede, diría que apareció mientras el camión estaba en el patio."""
        turno = dia1['custodia']['custodia_id']
        assert dia1['hallazgo']['custodia_id'] == turno
        assert dia1['inspeccion']['custodia_id'] == turno

    def test_el_tanqueo_NO_tiene_custodia_y_es_una_decision_escrita(self, dia1):
        """Medido, no supuesto: `flota_gasto` no tiene columna de custodia.

        Es la regla 2 escrita en el esquema —*«Ninguna columna de conductor,
        ninguna de custodia»*, `flota/adaptadores/gastos.py`—. Se afirma acá
        para que el día que alguien la agregue por simetría con el hallazgo,
        este test lo obligue a leer el motivo primero.
        """
        from flota.adaptadores.modelos import Gasto

        assert 'custodia_id' not in Gasto.__table__.columns

    def test_el_tanqueo_se_ata_a_una_lectura_real_del_dia(self, dia1, mundo):
        """Y no a una inventada ni a la del recibo: el odómetro se movió entre
        las 5 y las 11, y descartar el número del surtidor guardaría un
        kilometraje que nadie midió."""
        lectura = [l for l in arnes.lecturas_de(mundo['vehiculo_id'])
                   if l.id == dia1['tanqueo']['lectura_id']][0]
        assert lectura.valor_km == dia1['km']['tanqueo']
        assert lectura.origen == 'tanqueo'
        assert lectura.id != dia1['inspeccion']['lectura_id']

    def test_el_cierre_declara_el_km_del_dia_y_no_se_forzo(self, dia1, client,
                                                           mundo):
        """El `km_fin` tiene que ser coherente con TODO lo escrito durante el
        día, no solo mayor que el de apertura."""
        turno = [c for c in arnes.custodias_de(mundo['vehiculo_id'])
                 if c.id == dia1['custodia']['custodia_id']][0]
        assert turno.km_inicio == dia1['km']['recibo']
        assert turno.km_fin == dia1['km']['entrega']
        assert (turno.km_inicio <= dia1['km']['dano'] <= dia1['km']['tanqueo']
                <= turno.km_fin)
        assert turno.cierre_forzado is False
        assert turno.fin_ts == arnes.instante_utc(arnes.DIA_1,
                                                  arnes.ENTREGA_TURNO)

        forzados = client.get('/flota/custodia/cierres-forzados',
                              headers={'Authorization': f"Bearer {mundo['t_flota']}"})
        assert forzados.get_json()['cierres'] == []

    def test_un_no_apto_no_bloqueante_deja_el_veredicto_en_apto(self, dia1):
        """Medido, y es el criterio del catálogo: «bloqueante» significa
        exactamente «hoy no sale». Un `mayor` en `no_apto` tiene su reloj
        corriendo y el camión sale igual."""
        assert dia1['inspeccion']['veredicto'] == 'apto'
        assert dia1['inspeccion']['habilita_despacho'] is True
        assert dia1['inspeccion']['bloqueantes_no_aptos'] == 0
        assert len(dia1['inspeccion']['hallazgos']) == 1


# ═══════════════════════════════════════════════════════════════════════════
class TestElDiaSiguiente:

    def test_el_turno_2_recibe_de_la_sede_sin_forzar_nada(self, dia2, mundo):
        """Una sede no tiene turno que cerrar. Si el día 2 exigiera un admin de
        zona con motivo escrito, la fricción del caso anómalo caería sobre el
        caso normal de todos los días."""
        custodias = arnes.custodias_de(mundo['vehiculo_id'])
        assert [c.cierre_forzado for c in custodias] == [False] * len(custodias)
        turno2 = [c for c in custodias
                  if c.id == dia2['custodia']['custodia_id']][0]
        assert turno2.custodio_conductor_id == mundo['conductor_b']
        assert turno2.linea_base is False

    def test_la_inspeccion_del_dia_2_baraja_distinto(self, dia1, dia2):
        """Regla 11: con orden fijo, a la tercera semana el pulgar responde sin
        leer. La baraja se siembra con la fecha — mismos ítems, otro orden.

        Los bloqueantes van fijos a propósito (se citan por número y el orden
        es memoria útil); lo que tiene que moverse es el resto.
        """
        orden1 = [i['item_id'] for i in dia1['items']['items']]
        orden2 = [i['item_id'] for i in dia2['items']['items']]
        assert set(orden1) == set(orden2), 'martes y miércoles: mismos ítems'
        assert orden1 != orden2, (
            'el orden del día 2 es idéntico al del día 1: la baraja no se '
            'sembró con la fecha')

        bloq1 = [i['item_id'] for i in dia1['items']['items'] if i['bloqueante']]
        bloq2 = [i['item_id'] for i in dia2['items']['items'] if i['bloqueante']]
        assert bloq1 == bloq2, 'los bloqueantes van fijos'

    def test_el_dia_2_arranca_sin_inspeccion_previa_de_hoy(self, dia2):
        """`ya_respondidas_hoy` es del día operativo, no de las últimas 24
        horas: la del día 1 se cerró a las 20:00 de Bogotá, o sea ya en el día
        siguiente **de UTC**."""
        assert dia2['items']['dia'] == arnes.DIA_2.isoformat()
        assert dia2['items']['ya_respondidas_hoy'] == []

    def test_el_hallazgo_de_ayer_sigue_abierto_y_cuenta(self, dia1, dia2, client,
                                                        mundo):
        """Aplazar mueve el plazo; nada mueve `reportado_ts`. Un daño de ayer
        sigue vivo y su reloj sigue corriendo."""
        listado = arnes.hallazgos_de(client, mundo,
                                     token=mundo['t_b']).get_json()
        de_ayer = [h for h in listado['hallazgos']
                   if h['id'] == dia1['hallazgo']['id']][0]
        assert de_ayer['estado'] == 'abierto'
        assert de_ayer['entra_al_indicador'] is True
        assert de_ayer['dias_abierto'] >= 1
        assert listado['abiertos'] == 4, (
            'dos daños por día: el que nace de la inspección y el que el '
            'conductor reporta en la calle')


# ═══════════════════════════════════════════════════════════════════════════
class TestElHealthAlFinalDelDia:
    """¿Refleja lo que pasó, o quedó algún contador mintiendo?"""

    def test_cuenta_los_danos_las_inspecciones_y_el_cpk_del_dia(self, dia1,
                                                                client, mundo):
        h = arnes.health(client, mundo)
        assert h['hallazgos_abiertos'] == 2
        assert h['hallazgos_vencidos'] == 0
        assert h['vehiculos_sin_inspeccion_hoy'] == 0
        assert h['inspecciones_incompletas_hoy'] == 0
        assert h['vehiculos_sin_custodia_activa'] == 0
        assert h['custodias_cerradas_forzadas'] == 0
        assert h['custodias_pendiente_sede'] == 0

        cpk = [f for f in h['cpk_mes'] if f['placa'] == mundo['placa']][0]
        assert cpk['km'] == dia1['km']['entrega'] - dia1['km']['recibo']
        assert cpk['marca'] == 'declarada'

    def test_el_dia_operativo_del_health_aguanta_las_20_de_Bogota(self, dia1,
                                                                  client, mundo,
                                                                  reloj):
        """El health se consulta al cerrar el turno, cuando en UTC ya es mañana.

        Con `date.today()` en Railway, `vehiculos_sin_inspeccion_hoy` saltaría a
        1 en mitad del turno de la tarde y alguien saldría a llamar a un
        conductor que ya inspeccionó.
        """
        assert reloj.ahora.date() == arnes.DIA_2
        assert arnes.health(client, mundo)['vehiculos_sin_inspeccion_hoy'] == 0

    def test_la_marca_del_ritmo_es_una_palabra_del_vocabulario(self, dia1,
                                                               client, mundo):
        """**El defecto que este archivo arregló.**

        `Confianza` hereda de `str` y de `Enum`: en Python 3.11
        `str(Confianza.DECLARADA)` devuelve `'Confianza.DECLARADA'`. El health
        publicaba eso en `km_dia_por_vehiculo[].marca` y `'declarada'` en
        `cpk_mes[].marca` **en el mismo JSON**, y `flota.js:1745` imprimía el
        primero tal cual en el panel de salud.

        No lo veía nadie porque los tests que leen esa marca contra el endpoint
        real usan un vehículo **sin lecturas**, donde vale `sin_dato` —una
        cadena— y `str()` es un no-op. Hace falta un día que deje lecturas.
        """
        from flota.dominio.costos import MARCAS_TRAMO

        h = arnes.health(client, mundo)
        ritmo = [f for f in h['km_dia_por_vehiculo']
                 if f['placa'] == mundo['placa']][0]
        assert ritmo['marca'] in MARCAS_TRAMO, (
            f"la marca salió como {ritmo['marca']!r}, que no está en el "
            f"vocabulario {sorted(MARCAS_TRAMO)}")
        cpk = [f for f in h['cpk_mes'] if f['placa'] == mundo['placa']][0]
        assert ritmo['marca'] == cpk['marca'], (
            'el mismo JSON publica la misma confianza de dos formas distintas')

    def test_cuantas_lecturas_sin_respaldo_deja_un_turno(self, dia1, client,
                                                        mundo):
        """**Un hecho medido, sin umbral (regla 13).**

        El recibo y la entrega mandan foto del tablero (`foto_dato`) y sus
        lecturas nacen `declarada`. Las del **daño** y el **tanqueo** nacen
        `dudosa` y **ningún endpoint puede evitarlo**: `POST /flota/tanqueos` no
        recibe fotos, y las de `POST /flota/hallazgos` cuelgan del hallazgo —
        `anclar_odometro` nunca escribe `LecturaOdometro.foto_id`, que solo
        llena el traspaso.

        Son **dos por turno y por vehículo**, permanentes por construcción. La
        única salida es la cola de verificación, que exige `MAESTROS_FLOTA`:
        seis camiones × veintiséis turnos son ~312 filas al mes que alguien
        tiene que confirmar a mano. El campo del health existe justamente para
        ver «una cola creciendo con nadie mirándola».
        """
        h = arnes.health(client, mundo)
        assert h['lecturas_dudosas_pendientes'] == 2
        assert h['lecturas_verificadas_30d'] == 0

        origenes = {l.origen for l in arnes.lecturas_de(mundo['vehiculo_id'])
                    if l.confianza == 'dudosa'}
        assert origenes == {'hallazgo', 'tanqueo'}

    def test_el_health_no_distingue_un_bloqueante_no_apto_de_hoy(
            self, client, mundo, reloj):
        """**Un hueco medido, no un defecto de código.**

        El día que un camión sale con un bloqueante en `no_apto`, el tablero
        muestra `hallazgos_abiertos: 1` y `hallazgos_vencidos: 0` — idéntico a
        un guardabarros rayado. El bloqueante recién aparece como vencido
        **mañana**, y el único día en que la información podía parar el camión
        es el único día en que el tablero no la tiene.

        `inspecciones_incompletas_hoy` no lo cubre: su docstring dice que
        *«`no_apto` ya tiene su contador… entra en `hallazgos_abiertos`»*, y ese
        contador mezcla las tres criticidades.
        """
        reloj.en(arnes.DIA_1, (4, 0))
        arnes.dejar_en_sede(client, mundo, 100_000)
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        arnes.recibir_turno(client, mundo, 100_000)
        reloj.en(arnes.DIA_1, arnes.INSPECCION)
        items = arnes.items_del_dia(client, mundo).get_json()['items']
        bloqueante = next(i['item_id'] for i in items if i['bloqueante'])
        r = arnes.responder_inspeccion(client, mundo, items, 100_000,
                                       no_aptos=[bloqueante])
        assert r.get_json()['veredicto'] == 'no_apto'
        assert r.get_json()['habilita_despacho'] is False

        h = arnes.health(client, mundo)
        assert h['hallazgos_abiertos'] == 1
        assert h['hallazgos_vencidos'] == 0
        assert h['inspecciones_incompletas_hoy'] == 0
        assert not [c for c in h if 'bloqueante' in c or 'no_apto' in c], (
            'si aparece un campo que los distinga, este test sobra y hay que '
            'borrarlo — no aflojarlo')

    @pytest.mark.xfail(strict=True, reason=(
        'custodias_sin_foto_completa exige `len(angulos_de_custodia(n))` fotos '
        'en LOS DOS extremos de toda custodia (13 en un camión), y ningún gesto '
        'del sistema las manda las dos veces: la entrega manda 5 —los 4 de '
        '`ANGULOS_ENTREGA` + el tablero, asimetría deliberada y documentada— y '
        'un traspaso lleva un solo juego de fotos, así que la custodia que ABRE '
        'nace con cero de inicio y la que CIERRA queda con cero de fin. '
        'Resultado medido: 3 tras un día perfecto y +2 por turno, para siempre. '
        'Un contador que no puede valer cero es un contador que nadie lee — la '
        'lección de los 639 avisos. El arreglo es rediseñar qué exige cada '
        'extremo (13 a la custodia abierta por un recibo, 5 a la cerrada por '
        'una entrega) y es una decisión de quién define la evidencia, no un fix.'))
    def test_un_dia_bien_hecho_no_deja_custodias_sin_foto(self, dia1, client,
                                                          mundo):
        assert arnes.health(client, mundo)['custodias_sin_foto_completa'] == 0

    def test_cuantas_cuenta_hoy_de_mas(self, dia1, client, mundo):
        """El número exacto del `xfail` de arriba, pinchado.

        Existe para que el día que alguien toque `custodias_sin_foto_completa`
        el cambio se vea, aunque el `xfail` siga rojo por otra razón.
        """
        assert arnes.health(client, mundo)['custodias_sin_foto_completa'] == 3
        assert len(arnes.custodias_de(mundo['vehiculo_id'])) == 3


# ═══════════════════════════════════════════════════════════════════════════
class TestElDiaDelHallazgoSeCuentaEnUTC:
    """`dias_transcurridos` hace `(ahora.date() - reportado_ts.date()).days`.

    Las dos son fechas **UTC**. Es la regla 5 del WMS —*«lo que alguien LEE como
    día no sale de `utcnow()`»*— y el módulo la cumple en todo lo demás:
    `_instante_limite` pasa por Bogotá para la fecha límite y `_dia_bogota` para
    `Inspeccion.dia`. Esta línea es la que no.

    ## Acá NO va el `xfail`, y es a propósito

    El defecto ya está registrado —barrido de las 24 horas, tres `xfail(strict)`
    y una medición en verde— en `tests/flota/test_barrido_de_husos.py::
    TestContarDiasDeUnHallazgo`. Escribir el mío al lado no agregaría cobertura:
    agregaría **dos avisos rojos para un solo defecto**, que es exactamente cómo
    un canal de advertencias se vuelve ilegible. Un defecto, un registro.

    Lo que este archivo aporta es lo que el barrido de dominio no puede afirmar:
    que el número equivocado **sale por la frontera HTTP** en un día que ocurre,
    en el mismo `GET /flota/hallazgos/<placa>` en el que viaja una `fecha_limite`
    que sí está en Bogotá. Por eso los dos de abajo son mediciones en verde con
    el valor de hoy: el día que el arreglo llegue caen en rojo y hay que
    actualizarlos junto con los `xfail` de aquel archivo — que es lo que uno
    quiere que pase.
    """

    def _reportar_y_leer(self, client, mundo, reloj, hora_reporte, dia_lectura,
                         hora_lectura):
        reloj.en(arnes.DIA_1, (4, 0))
        arnes.dejar_en_sede(client, mundo, 100_000)
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        arnes.recibir_turno(client, mundo, 100_000)

        reloj.en(arnes.DIA_1, hora_reporte)
        r = arnes.reportar_dano(client, mundo, 100_050, criticidad='bloqueante')
        assert r.status_code == 201, r.get_json()
        hallazgo_id = r.get_json()['id']

        reloj.en(dia_lectura, hora_lectura)
        listado = arnes.hallazgos_de(client, mundo).get_json()['hallazgos']
        return [h for h in listado if h['id'] == hallazgo_id][0]

    @pytest.mark.xfail(strict=True, reason=(
        'dias_transcurridos resta fechas UTC. Un daño reportado a las 10:00 de '
        'Bogotá y mirado a las 20:00 del MISMO día operativo cae en dos fechas '
        'UTC distintas (el 15 y el 16) y la pantalla dice «1 día abierto». Para '
        'un bloqueante, cuyo plazo es «mismo día», eso se lee como ya atrasado. '
        'El arreglo —convertir a Bogotá antes de `.date()`— toca el dominio '
        'puro y la función que implementa un canon escrito: es una decisión de '
        'quién es dueño del canon, no un fix de frontera.'))
    def test_un_dano_reportado_y_mirado_el_mismo_dia_lleva_cero_dias(
            self, client, mundo, reloj):
        fila = self._reportar_y_leer(client, mundo, reloj,
                                     hora_reporte=(10, 0),
                                     dia_lectura=arnes.DIA_1,
                                     hora_lectura=(20, 0))
        assert fila['dias_abierto'] == 0

    @pytest.mark.xfail(strict=True, reason=(
        'La otra dirección, y la que subcuenta: reportado a las 20:00 de Bogotá '
        'del 15 (01:00 UTC del 16) y mirado a las 08:00 de Bogotá del 16 '
        '(13:00 UTC del 16). En Bogotá pasó un día; en UTC es la misma fecha y '
        'la pantalla dice 0. Un daño de anoche se ve como recién reportado.'))
    def test_un_dano_de_anoche_lleva_un_dia_a_la_manana_siguiente(
            self, client, mundo, reloj):
        fila = self._reportar_y_leer(client, mundo, reloj,
                                     hora_reporte=(20, 0),
                                     dia_lectura=arnes.DIA_2,
                                     hora_lectura=(8, 0))
        assert fila['dias_abierto'] == 1

    def test_la_fecha_limite_SI_esta_en_Bogota(self, client, mundo, reloj):
        """El contraste que hace que lo de arriba sea un descuido y no una
        política: el mismo hallazgo, reportado a las 20:00 de Bogotá, calcula su
        plazo sobre el día **15** y no sobre el 16 de UTC.

        `_instante_limite` sí pasa por `TZ_BOGOTA`. Con `utcnow().date()` un
        bloqueante de la noche nacería vencido antes de que llegue el mecánico.
        """
        from app.utils.fecha import inicio_del_dia_utc

        reloj.en(arnes.DIA_1, (4, 0))
        arnes.dejar_en_sede(client, mundo, 100_000)
        reloj.en(arnes.DIA_1, arnes.RECIBO_TURNO)
        arnes.recibir_turno(client, mundo, 100_000)
        reloj.en(arnes.DIA_1, (20, 0))
        assert reloj.ahora.date() == arnes.DIA_2, 'en UTC ya es mañana'

        r = arnes.reportar_dano(client, mundo, 100_050, criticidad='bloqueante')
        limite = datetime.fromisoformat(r.get_json()['fecha_limite']).replace(
            tzinfo=None)
        assert limite == inicio_del_dia_utc(arnes.DIA_1 + timedelta(days=1)), (
            'un bloqueante reportado de noche vence al terminar el día de '
            'Bogotá en que se reportó, no el anterior')
        assert r.get_json()['vencido'] is False
