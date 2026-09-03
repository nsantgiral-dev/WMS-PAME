"""La inspección diaria, desde que se pregunta hasta que se juzga — contra la
base real, no contra mocks.

## Por qué existe

`flota/adaptadores/inspecciones.py` nació con 370 líneas, **cero tests y cero
callers**. Los 53 ítems llevaban en producción desde el 2026-08-02 y la política
de ordenamiento desde la tanda 1: todo escrito, nada ejercido. Es el patrón
«función sin caller» cometido dentro del módulo que existe para evitarlo.

Estos tests ejercen la vía completa: adaptador → base → CHECK. Un test que
construyera `Inspeccion(...)` en memoria y mirara sus atributos pasaría con la
tabla sin un solo constraint, que es exactamente el modo de falla que este
módulo persigue.

## Las dos direcciones

Cada invariante se prueba de los dos lados: que **dispara** cuando se lo viola y
que **NO dispara** sobre la operación sana. Hay seis casos fechados en
`CLAUDE.md` de guards en verde sobre propiedades que la vía sana satisfacía por
construcción — los seis tenían solo la mitad de este par.

## Y los CHECK se ejercen con inserciones crudas

Un invariante que solo vive en el adaptador es una sugerencia: la segunda vía de
escritura no lo hereda. `TestLaBaseImponeLaRegla1` escribe filas a mano, sin
pasar por `registrar`, que es lo que hace un `UPDATE` desde psql.
"""
import ast
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.almacen import Almacen
from app.models.usuario import Usuario
from app.models.vehiculo import Vehiculo
from flota.adaptadores import catalogo
from flota.adaptadores import hallazgos as adaptador_hallazgos
from flota.adaptadores import inspecciones as adaptador
from flota.adaptadores.modelos import (Hallazgo, Inspeccion, LecturaOdometro,
                                       RespuestaItem)
from flota.adaptadores.traspaso import traspasar
from flota.dominio import inspeccion as dom
from flota.dominio.errores import ErrorFlota
from flota.dominio.valores import CustodioTipo, OrigenLectura

#: Un miércoles. Los ítems semanales NO entran — el catálogo de camión tiene uno
#: (`Drenaje del separador de agua`) y con un lunes la mitad de los tests
#: mediría otra lista sin decirlo.
UN_MIERCOLES = datetime(2026, 9, 2, 14, 0)      # 09:00 en Bogotá
EL_LUNES = datetime(2026, 9, 7, 14, 0)


@pytest.fixture
def escenario(db):
    """Un camión, un usuario, una sede y el catálogo sembrado.

    Depende de `db` y no de `app`: `app` es de sesión y no limpia nada entre
    tests, así que la segunda placa idéntica choca contra el UNIQUE.
    """
    v = Vehiculo(placa='INS001', tipo='camion', activo=True)
    u = Usuario(email='ins_conductor@test.com', nombre='Quien Inspecciona',
                rol='conductor', activo=True)
    u.set_password('x')
    sede = Almacen(codigo='INS-SEDE', nombre='Patio de prueba', activo=True)
    db.session.add_all([v, u, sede])
    db.session.commit()
    catalogo.sembrar(db)
    return {'vehiculo': v, 'vehiculo_id': v.id, 'usuario_id': u.id,
            'placa': v.placa, 'sede_id': sede.id, 'db': db}


def _items(escenario, ts=UN_MIERCOLES):
    return adaptador.items_del_dia_de(escenario['vehiculo'],
                                      _dia_bogota_de(ts))


def _dia_bogota_de(ts: datetime) -> date:
    """El día de Bogotá del instante UTC, con la MISMA función del adaptador.

    Reimplementarlo acá sería la segunda copia de la conversión: el test pasaría
    aunque el adaptador contara el día en UTC, que es justo el defecto de la
    regla 5 del WMS.
    """
    return adaptador._dia_bogota(ts)


def _respuestas(items, respuesta=dom.OPTIMO, salvo=None, omitir=()):
    """`[{item_id, respuesta}]` para toda la lista del día.

    `salvo` es `{item_id: respuesta}` y `omitir` los que directamente **no se
    mandan** — que es lo que de verdad pasa cuando alguien no toca un ítem, y no
    lo mismo que mandarlo con `sin_dato`.
    """
    salvo = salvo or {}
    return [
        {'item_id': i.id,
         'respuesta': salvo[i.id] if i.id in salvo else respuesta}
        for i in items if i.id not in omitir
    ]


def _registrar(escenario, respuestas, ts=UN_MIERCOLES, km=10_000,
               segundos=90, **extra):
    campos = dict(
        vehiculo_id=escenario['vehiculo_id'],
        tipo_vehiculo_obj=escenario['vehiculo'],
        km=km,
        inspeccionada_por_usuario_id=escenario['usuario_id'],
        respuestas=respuestas,
        segundos_llenado=segundos,
        ts=ts,
    )
    campos.update(extra)
    return adaptador.registrar(**campos)


def _bloqueante(items):
    return next(i for i in items if i.criticidad == 'bloqueante')


def _no_bloqueante(items):
    return next(i for i in items if i.criticidad != 'bloqueante')


# ══════════════════════════════════════════════════════════════════════════
# Qué se pregunta hoy — regla 11
# ══════════════════════════════════════════════════════════════════════════

class TestQueSePreguntaHoyYEnQueOrden:
    """El orden lo decide el servidor, y tiene que ser reconstruible.

    QUÉ AFIRMA esta clase: que dos conductores el mismo día ven la misma
    pantalla, que mañana es otra, y que los bloqueantes no se mueven.

    QUÉ NO AFIRMA: que alguien los haya leído. Eso no lo puede afirmar ningún
    test — es lo que `segundos_llenado` existe para poder mirar.
    """

    def test_el_mismo_dia_da_el_mismo_orden(self, escenario):
        """Reproducible: una inspección de hace tres meses se puede auditar
        reconstruyendo lo que tenía en pantalla. Un `random.shuffle()` sin
        semilla haría eso imposible."""
        uno = [i.id for i in _items(escenario)]
        otro = [i.id for i in _items(escenario)]
        assert uno == otro
        assert len(uno) > 20, 'la lista del día quedó vacía o casi'

    def test_dias_distintos_dan_ordenes_distintos(self, escenario):
        """La otra dirección, y la que importa: con orden fijo, a la tercera
        semana el pulgar responde sin leer (regla 11).

        Se compara la COLA —lo que no es bloqueante—, porque los bloqueantes van
        fijos a propósito y compararlos enteros escondería un barajado muerto
        detrás de un prefijo que siempre coincide.
        """
        def _cola(dia):
            return [i.id for i in adaptador.items_del_dia_de(
                escenario['vehiculo'], dia) if i.criticidad != 'bloqueante']

        ordenes = {tuple(_cola(date(2026, 9, d))) for d in range(2, 7)}
        assert len(ordenes) > 1, (
            'cinco días distintos produjeron el mismo orden: el barajado no '
            'está tomando la fecha, y el pulgar aprende la lista')

    def test_los_bloqueantes_van_primero_y_en_orden_fijo(self, escenario):
        """Se citan por número: el freno siempre es el 1. El orden fijo acá es
        memoria útil, no comodidad."""
        for dia in (date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4)):
            items = adaptador.items_del_dia_de(escenario['vehiculo'], dia)
            criticidades = [i.criticidad for i in items]
            cuantos = criticidades.count('bloqueante')
            assert criticidades[:cuantos] == ['bloqueante'] * cuantos
            bloqueantes = [i.orden for i in items[:cuantos]]
            assert bloqueantes == sorted(bloqueantes)

    def test_el_semanal_solo_entra_los_lunes(self, escenario):
        """Un ítem semanal preguntado a diario se vuelve ruido, y el ruido
        entrena a marcar sin leer — el mismo daño que evita el barajado."""
        miercoles = {i.periodicidad for i in _items(escenario, UN_MIERCOLES)}
        assert miercoles == {'diaria'}
        lunes = [i for i in _items(escenario, EL_LUNES)
                 if i.periodicidad == 'semanal']
        assert lunes, 'el catálogo de camión tiene un semanal y no apareció el lunes'

    def test_un_tipo_sin_plantilla_declarada_levanta_con_el_motivo(self, escenario):
        """Regla 5: sin `.get(tipo, 'camion')`. Un motocarro contra el catálogo
        de camión recibe nueve ítems que no puede tener, y el pulgar aprende a
        marcarlos óptimos sin leer."""
        escenario['vehiculo'].tipo = 'moto'
        escenario['db'].session.commit()
        with pytest.raises(ErrorFlota) as e:
            adaptador.plantilla_de(escenario['vehiculo'])
        assert 'moto' in str(e.value)

    def test_un_tipo_con_decision_y_sin_catalogo_dice_OTRA_cosa(self, escenario):
        """Dos fallos que se ven parecidos y no lo son: «no sé qué preguntarle»
        y «sé qué preguntarle y el catálogo no está sembrado». Con un solo
        mensaje, quien lo lea a las 5 a.m. no sabe a quién llamar."""
        escenario['vehiculo'].tipo = 'motocarro'
        escenario['db'].session.commit()
        with pytest.raises(ErrorFlota) as e:
            adaptador.plantilla_de(escenario['vehiculo'])
        assert 'sembrala' in str(e.value).lower()


# ══════════════════════════════════════════════════════════════════════════
# REGLA 1 — `incompleta` NO es `no_apto`, y ninguna de las dos autoriza
# ══════════════════════════════════════════════════════════════════════════

class TestIncompletaNoEsNoApto:
    """El corazón del módulo.

    `no_apto` es «sé que está mal». `incompleta` es «no sé». **No saber tampoco
    autoriza**, y decir «no apto» cuando lo que pasó es que nadie miró convierte
    el registro en evidencia falsa por el otro lado: el vehículo aparecería
    rechazado por una falla que nunca se constató.
    """

    def test_todo_contestado_y_ningun_bloqueante_caido_es_apto(self, escenario):
        """La dirección sana. Sin esto, un veredicto que devolviera siempre
        `incompleta` pasaría todos los tests de abajo."""
        items = _items(escenario)
        fila = _registrar(escenario, _respuestas(items))
        assert fila.veredicto == dom.APTO
        assert fila.items_sin_dato == 0
        assert dom.habilita_despacho(fila.veredicto) is True

    def test_un_bloqueante_sin_responder_es_incompleta_y_NO_optimo(self, escenario):
        """**Regla 1, literal.** Un ítem no respondido es `sin_dato`, jamás
        `optimo`. Con el default optimista el freno sin mirar saldría `apto` y
        esa evidencia se usa después frente a una aseguradora."""
        items = _items(escenario)
        freno = _bloqueante(items)
        fila = _registrar(escenario, _respuestas(items, omitir={freno.id}))

        assert fila.veredicto == dom.INCOMPLETA
        assert fila.items_sin_dato == 1
        respuesta = RespuestaItem.query.filter_by(
            inspeccion_id=fila.id, item_id=freno.id).one()
        assert respuesta.respuesta == dom.SIN_DATO, (
            'el ítem que nadie tocó entró como algo distinto de «no sé»')

    def test_incompleta_NO_habilita_despacho(self, escenario):
        """La consecuencia, escrita aparte del veredicto: no saber no autoriza."""
        items = _items(escenario)
        fila = _registrar(escenario,
                          _respuestas(items, omitir={_bloqueante(items).id}))
        assert dom.habilita_despacho(fila.veredicto) is False

    def test_incompleta_no_se_convierte_en_no_apto(self, escenario):
        """La otra mitad de la regla 1, y la que nadie prueba: `incompleta` no
        puede colapsar en `no_apto` «para simplificar». Se corrigen distinto —
        uno se arregla en el taller, el otro mirando."""
        items = _items(escenario)
        fila = _registrar(escenario,
                          _respuestas(items, omitir={_bloqueante(items).id}))
        assert fila.veredicto != dom.NO_APTO
        assert fila.bloqueantes_no_aptos == 0

    def test_un_hueco_en_un_item_NO_bloqueante_tambien_es_incompleta(
            self, escenario):
        """La lectura estricta —mirar solo los bloqueantes— es el camino barato
        de la regla 11: contestar los nueve bloqueantes en veinte segundos,
        dejar los diecinueve restantes en blanco y llevarse un `apto`."""
        items = _items(escenario)
        fila = _registrar(escenario,
                          _respuestas(items, omitir={_no_bloqueante(items).id}))
        assert fila.veredicto == dom.INCOMPLETA

    def test_un_bloqueante_no_apto_es_no_apto(self, escenario):
        items = _items(escenario)
        freno = _bloqueante(items)
        fila = _registrar(escenario,
                          _respuestas(items, salvo={freno.id: dom.NO_APTO}))
        assert fila.veredicto == dom.NO_APTO
        assert fila.bloqueantes_no_aptos == 1
        assert dom.habilita_despacho(fila.veredicto) is False

    def test_un_MAYOR_no_apto_no_impide_salir(self, escenario):
        """No es una laguna: es el criterio del catálogo, donde «bloqueante»
        significa exactamente «hoy no sale». El camión sale con el hallazgo
        abierto y sus siete días corriendo."""
        items = _items(escenario)
        mayor = next(i for i in items if i.criticidad == 'mayor')
        fila = _registrar(escenario,
                          _respuestas(items, salvo={mayor.id: dom.NO_APTO}))
        assert fila.veredicto == dom.APTO
        assert Hallazgo.query.count() == 1, 'el daño tiene que quedar igual'

    def test_saber_que_esta_mal_gana_sobre_no_saber(self, escenario):
        """Precedencia: el que lee mañana necesita el dato más fuerte. Que el
        freno falle es más informativo que que faltó contestar el radio."""
        items = _items(escenario)
        freno = _bloqueante(items)
        otro = _no_bloqueante(items)
        fila = _registrar(escenario, _respuestas(
            items, salvo={freno.id: dom.NO_APTO}, omitir={otro.id}))
        assert fila.veredicto == dom.NO_APTO
        assert fila.items_sin_dato == 1, 'el hueco se sigue contando'

    def test_no_mandar_ninguna_respuesta_no_es_apto(self, escenario):
        """El peor default posible sería que una lista vacía autorizara."""
        fila = _registrar(escenario, [])
        assert fila.veredicto == dom.INCOMPLETA
        assert fila.items_sin_dato == fila.items_esperados

    def test_se_escribe_UNA_fila_por_cada_item_del_dia(self, escenario):
        """Incluidos los que nadie tocó. Un ítem sin fila y un ítem `sin_dato`
        se leen igual desde el tablero y significan cosas distintas."""
        items = _items(escenario)
        fila = _registrar(escenario, _respuestas(items, omitir={items[0].id,
                                                               items[5].id}))
        assert RespuestaItem.query.filter_by(inspeccion_id=fila.id).count() \
            == len(items)
        assert fila.items_esperados == len(items)


# ══════════════════════════════════════════════════════════════════════════
# REGLA 3 — sin odómetro no se persiste ningún evento de flota
# ══════════════════════════════════════════════════════════════════════════

class TestElOdometroEntraPorLaMismaPuerta:

    def test_la_inspeccion_queda_anclada_a_una_lectura_real(self, escenario):
        fila = _registrar(escenario, _respuestas(_items(escenario)), km=54_321)
        assert fila.lectura_id is not None
        lectura = LecturaOdometro.query.get(fila.lectura_id)
        assert lectura.valor_km == 54_321
        assert lectura.origen == OrigenLectura.PREOPERACIONAL.value, (
            'el origen miente sobre de dónde vino la lectura')

    def test_no_fabrica_una_lectura_duplicada(self, escenario):
        """El mismo km que la última lectura reutiliza la fila. Escribir una
        idéntica por cada inspección produciría desde adentro el ruido que
        `lecturas_ts_duplicado` existe para contar."""
        adaptador_hallazgos.anclar_odometro(
            escenario['vehiculo_id'], 7_000, escenario['usuario_id'],
            UN_MIERCOLES - timedelta(hours=1))
        escenario['db'].session.commit()
        antes = LecturaOdometro.query.count()

        fila = _registrar(escenario, _respuestas(_items(escenario)), km=7_000)
        assert LecturaOdometro.query.count() == antes
        assert fila.lectura_id == LecturaOdometro.query.one().id

    def test_un_km_distinto_SI_escribe_lectura_nueva(self, escenario):
        """La otra dirección: reutilizar siempre guardaría un kilometraje que
        nadie midió."""
        adaptador_hallazgos.anclar_odometro(
            escenario['vehiculo_id'], 7_000, escenario['usuario_id'],
            UN_MIERCOLES - timedelta(hours=1))
        escenario['db'].session.commit()
        fila = _registrar(escenario, _respuestas(_items(escenario)), km=7_250)
        assert LecturaOdometro.query.get(fila.lectura_id).valor_km == 7_250
        assert LecturaOdometro.query.count() == 2

    def test_un_odometro_que_retrocede_no_entra_por_esta_puerta(self, escenario):
        """La inspección es la puerta nueva, y las puertas nuevas son por donde
        se cuela lo que las viejas frenan."""
        _registrar(escenario, _respuestas(_items(escenario)), km=9_000)
        with pytest.raises(ErrorFlota):
            _registrar(escenario, _respuestas(_items(escenario)), km=100,
                       ts=UN_MIERCOLES + timedelta(hours=1))
        assert Inspeccion.query.count() == 1

    def test_la_inspeccion_y_sus_hallazgos_cuelgan_de_UNA_lectura(self, escenario):
        """Es lo que de verdad pasó: alguien miró el tablero una vez."""
        items = _items(escenario)
        dos = [i for i in items if i.criticidad == 'mayor'][:2]
        fila = _registrar(escenario, _respuestas(
            items, salvo={i.id: dom.NO_APTO for i in dos}), km=3_000)
        lecturas = {h.lectura_id for h in Hallazgo.query.all()} | {fila.lectura_id}
        assert len(lecturas) == 1
        assert LecturaOdometro.query.count() == 1


# ══════════════════════════════════════════════════════════════════════════
# REGLA 11 — `segundos_llenado` se guarda, y no se le inventa un techo
# ══════════════════════════════════════════════════════════════════════════

class TestSegundosLlenado:
    """La pregunta es cómo maximiza esto quien no quiere hacer el trabajo, y la
    respuesta es «marcando todo óptimo en veinte segundos».

    Lo que el sistema hace con eso hoy es **guardarlo**, no juzgarlo: no hay una
    sola medición todavía (regla 13). Publicar el hecho es el paso para poder
    fijar un umbral con dato dentro de un mes, en vez de a ojo hoy.
    """

    def test_se_guarda_tal_cual(self, escenario):
        fila = _registrar(escenario, _respuestas(_items(escenario)), segundos=137)
        escenario['db'].session.refresh(fila)
        assert fila.segundos_llenado == 137

    def test_veinte_segundos_con_todo_optimo_ENTRA_y_queda_visible(self, escenario):
        """**Ningún umbral inventado.** Se registra igual — bloquearla hoy sería
        imponer antes de medir, y con la app dejando camiones en patio el primer
        día la operación desmonta el sistema en 48 horas.

        Lo que no puede pasar es que el dato se pierda: es el único que
        distingue mirar de marcar.
        """
        items = _items(escenario)
        fila = _registrar(escenario, _respuestas(items), segundos=20)
        assert fila.veredicto == dom.APTO
        assert fila.segundos_llenado == 20
        assert fila.items_esperados == len(items), (
            'sin los ítems esperados al lado, «20 segundos» no dice nada: '
            'veinte segundos para tres ítems no es lo mismo que para veintiocho')

    def test_un_cronometro_al_reves_no_entra(self, escenario):
        """Lo único que no puede ser es negativo. Eso no es un llenado rápido."""
        with pytest.raises(ErrorFlota) as e:
            _registrar(escenario, _respuestas(_items(escenario)), segundos=-5)
        assert 'reloj al revés' in str(e.value)
        assert Inspeccion.query.count() == 0

    def test_un_valor_no_numerico_levanta_y_no_se_rellena_con_cero(self, escenario):
        """Un `0` puesto por el sistema haría idénticas una inspección que no
        midió el tiempo y una contestada al instante — que es justo el caso que
        el campo existe para ver."""
        with pytest.raises(ErrorFlota):
            _registrar(escenario, _respuestas(_items(escenario)),
                       segundos='rapidito')
        assert Inspeccion.query.count() == 0


# ══════════════════════════════════════════════════════════════════════════
# El puente al hallazgo — regla 6, una sola vía de nacimiento
# ══════════════════════════════════════════════════════════════════════════

class TestElPuenteAlHallazgo:

    def _con_un_no_apto(self, escenario, criticidad='mayor', nota=None):
        items = _items(escenario)
        item = next(i for i in items if i.criticidad == criticidad)
        respuestas = _respuestas(items, salvo={item.id: dom.NO_APTO})
        if nota is not None:
            for r in respuestas:
                if r['item_id'] == item.id:
                    r['nota'] = nota
        return item, _registrar(escenario, respuestas)

    def test_un_no_apto_hace_nacer_su_hallazgo(self, escenario):
        item, fila = self._con_un_no_apto(escenario)
        h = Hallazgo.query.one()
        assert h.item_id == item.id, 'el daño no dice de qué ítem salió'
        assert h.descripcion == item.nombre
        assert h.criticidad == item.criticidad, 'la criticidad no se heredó'

    def test_la_fecha_limite_sale_de_la_criticidad_del_item(self, escenario):
        """Regla 6: se calcula al nacer, no se elige. Y se calcula **en la
        misma función** que usa el hallazgo reportado a mano — una segunda vía
        de nacimiento es siempre la que se olvida del plazo.

        Las tres criticidades en UNA inspección, que además es el caso real: un
        camión con el freno caído no tiene solo eso.
        """
        items = _items(escenario)
        uno_de_cada = {c: next(i for i in items if i.criticidad == c)
                       for c in ('bloqueante', 'mayor', 'menor')}
        _registrar(escenario, _respuestas(
            items, salvo={i.id: dom.NO_APTO for i in uno_de_cada.values()}))

        por_item = {h.item_id: h for h in Hallazgo.query.all()}
        assert len(por_item) == 3
        for criticidad, item in uno_de_cada.items():
            esperado = adaptador_hallazgos._instante_limite(
                UN_MIERCOLES, criticidad)
            assert por_item[item.id].fecha_limite == esperado, (
                f'el plazo de un {criticidad} no salió de la tabla del dominio')

    def test_la_respuesta_queda_atada_a_su_hallazgo(self, escenario):
        item, fila = self._con_un_no_apto(escenario)
        r = RespuestaItem.query.filter_by(inspeccion_id=fila.id,
                                          item_id=item.id).one()
        assert r.hallazgo_id == Hallazgo.query.one().id, (
            'sin este enlace, el daño y la respuesta que lo produjo quedan '
            'como dos hechos sueltos que nadie puede cruzar')

    def test_la_nota_del_conductor_entra_en_la_descripcion(self, escenario):
        """El nombre del ítem dice qué mirar; la nota dice lo que el catálogo no
        puede saber — cuál rueda, qué tan grande."""
        item, _fila = self._con_un_no_apto(escenario, nota='la trasera derecha')
        assert Hallazgo.query.one().descripcion == \
            f'{item.nombre} — la trasera derecha'

    def test_sin_nota_el_daño_se_reporta_igual(self, escenario):
        """La otra dirección. Exigir la nota le pondría precio a marcar la
        falla, y lo que se paga con ese precio es un `optimo`."""
        item, _fila = self._con_un_no_apto(escenario, nota=None)
        assert Hallazgo.query.one().descripcion == item.nombre

    def test_sin_dato_NUNCA_produce_hallazgo(self, escenario):
        """«No sé» no es «está mal»: bloquea el despacho vía `incompleta` y no
        le abre a nadie una tarea con fecha límite sobre un daño que nadie
        constató."""
        items = _items(escenario)
        fila = _registrar(escenario,
                          _respuestas(items, omitir={_bloqueante(items).id}))
        assert fila.veredicto == dom.INCOMPLETA
        assert Hallazgo.query.count() == 0

    def test_una_inspeccion_toda_optima_no_produce_ninguno(self, escenario):
        _registrar(escenario, _respuestas(_items(escenario)))
        assert Hallazgo.query.count() == 0
        assert RespuestaItem.query.filter(
            RespuestaItem.hallazgo_id.isnot(None)).count() == 0

    def test_tres_no_apto_producen_tres_hallazgos(self, escenario):
        items = _items(escenario)
        tres = [i for i in items if i.criticidad == 'menor'][:3]
        fila = _registrar(escenario, _respuestas(
            items, salvo={i.id: dom.NO_APTO for i in tres}))
        assert Hallazgo.query.count() == 3
        assert {r.hallazgo_id for r in fila.respuestas
                if r.hallazgo_id is not None} == {h.id for h in Hallazgo.query.all()}

    def test_el_hallazgo_nace_por_reportar_y_no_por_un_INSERT_propio(
            self, escenario, monkeypatch):
        """**Una sola vía de nacimiento** (regla 6 y regla 0 del WMS).

        Se espía la función real: si la inspección escribiera su propio
        `Hallazgo(...)`, este contador quedaría en cero y el hallazgo existiría
        igual — que es exactamente cómo se vería la divergencia.
        """
        llamadas = []
        original = adaptador_hallazgos.reportar

        def _espia(**kw):
            llamadas.append(kw)
            return original(**kw)

        monkeypatch.setattr(adaptador.adaptador_hallazgos, 'reportar', _espia)
        self._con_un_no_apto(escenario)

        assert len(llamadas) == 1
        assert llamadas[0]['commit'] is False, (
            'con commit=True el hallazgo se guarda solo y la inspección puede '
            'reventar después: quedaría un daño sin la inspección que lo vio')
        assert 'criticidad' in llamadas[0] and 'item_id' in llamadas[0]

    def test_el_adaptador_no_construye_hallazgos_a_mano(self):
        """TRINQUETE, por AST y no por texto: lo que hace ilegal a la llamada es
        que SEA una construcción, no que la palabra aparezca — el docstring del
        módulo nombra `flota_hallazgo` varias veces."""
        fuente = (Path(__file__).resolve().parents[2] / 'flota' / 'adaptadores'
                  / 'inspecciones.py').read_text(encoding='utf-8')
        malas = [n.lineno for n in ast.walk(ast.parse(fuente))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == 'Hallazgo']
        assert not malas, (
            f'segunda vía de nacimiento de hallazgos en las líneas {malas}: la '
            f'segunda es siempre la que se olvida de calcular la fecha límite')

    def test_el_hallazgo_hereda_la_custodia_de_la_inspeccion(self, escenario):
        """Es un hecho —bajo la custodia de quién apareció—, no una imputación
        (regla 2)."""
        traspasar(vehiculo_id=escenario['vehiculo_id'], km=1,
                  registrado_por_usuario_id=escenario['usuario_id'],
                  custodio_tipo=CustodioTipo.SEDE,
                  custodio_sede_id=escenario['sede_id'])
        c2 = traspasar(vehiculo_id=escenario['vehiculo_id'], km=2,
                       registrado_por_usuario_id=escenario['usuario_id'],
                       custodio_tipo=CustodioTipo.SEDE,
                       custodio_sede_id=escenario['sede_id'])
        _item, fila = self._con_un_no_apto(escenario)
        assert fila.custodia_id == c2.id
        assert Hallazgo.query.one().custodia_id == c2.id


# ══════════════════════════════════════════════════════════════════════════
# Todo o nada
# ══════════════════════════════════════════════════════════════════════════

class TestTodoONada:
    """Una inspección con la mitad de sus respuestas escritas tendría un
    veredicto que no describe sus filas — peor que no tener ninguna."""

    def _nada_quedo_escrito(self):
        assert Inspeccion.query.count() == 0
        assert RespuestaItem.query.count() == 0
        assert Hallazgo.query.count() == 0
        assert LecturaOdometro.query.count() == 0

    def test_una_respuesta_inventada_no_escribe_nada(self, escenario):
        items = _items(escenario)
        respuestas = _respuestas(items)
        respuestas[0]['respuesta'] = 'mas o menos'
        with pytest.raises(ErrorFlota) as e:
            _registrar(escenario, respuestas)
        assert 'mas o menos' in str(e.value)
        self._nada_quedo_escrito()

    def test_un_item_de_otra_plantilla_no_escribe_nada(self, escenario):
        """O la pantalla se quedó abierta pasada la medianoche, o son de otra
        plantilla. En los dos casos la lista ya no es la de hoy."""
        respuestas = _respuestas(_items(escenario))
        respuestas.append({'item_id': 999_999, 'respuesta': dom.OPTIMO})
        with pytest.raises(ErrorFlota):
            _registrar(escenario, respuestas)
        self._nada_quedo_escrito()

    def test_el_mismo_item_dos_veces_no_escribe_nada(self, escenario):
        """Con dos respuestas para el mismo ítem, los conteos del resumen dejan
        de describir lo que se contestó."""
        respuestas = _respuestas(_items(escenario))
        respuestas.append(dict(respuestas[0]))
        with pytest.raises(ErrorFlota):
            _registrar(escenario, respuestas)
        self._nada_quedo_escrito()

    def test_si_el_hallazgo_revienta_no_queda_media_inspeccion(
            self, escenario, monkeypatch):
        """**El caso que `commit=False` hace posible y peligroso.** La
        transacción es del llamador; si `reportar` levanta en el segundo daño,
        el rollback tiene que llevarse la inspección entera y el primer
        hallazgo.
        """
        items = _items(escenario)
        dos = [i for i in items if i.criticidad == 'menor'][:2]
        original = adaptador_hallazgos.reportar
        estado = {'n': 0}

        def _revienta_en_el_segundo(**kw):
            estado['n'] += 1
            if estado['n'] == 2:
                raise RuntimeError('el disco se llenó a mitad')
            return original(**kw)

        monkeypatch.setattr(adaptador.adaptador_hallazgos, 'reportar',
                            _revienta_en_el_segundo)
        with pytest.raises(RuntimeError):
            _registrar(escenario, _respuestas(
                items, salvo={i.id: dom.NO_APTO for i in dos}))
        self._nada_quedo_escrito()

    def test_una_lista_de_respuestas_que_no_es_lista_levanta(self, escenario):
        with pytest.raises(ErrorFlota):
            _registrar(escenario, {'item_id': 1, 'respuesta': dom.OPTIMO})
        self._nada_quedo_escrito()


# ══════════════════════════════════════════════════════════════════════════
# El orden mostrado y el día — lo que el cliente NO decide
# ══════════════════════════════════════════════════════════════════════════

class TestLoQueDecideElServidor:

    def test_el_orden_mostrado_sale_del_servidor(self, escenario):
        """Si viniera del JSON, quien quisiera podría declarar cualquier orden y
        el registro afirmaría una pantalla que nadie vio."""
        items = _items(escenario)
        fila = _registrar(escenario, _respuestas(items))
        por_orden = sorted(fila.respuestas, key=lambda r: r.orden_mostrado)
        assert [r.orden_mostrado for r in por_orden] == \
            list(range(1, len(items) + 1))
        assert [r.item_id for r in por_orden] == [i.id for i in items]

    def test_un_orden_mandado_en_el_cuerpo_se_ignora(self, escenario):
        """La otra dirección: no basta con calcularlo, hay que no dejarse pisar.

        El cuerpo manda una permutación **válida** —el orden dado vuelta—, no
        veintiocho veces el mismo número. Con el número repetido lo frenaría el
        UNIQUE de la base y el test estaría midiendo el constraint en vez de la
        política: quien quisiera falsear la pantalla mandaría un orden
        coherente, no uno imposible.
        """
        items = _items(escenario)
        respuestas = _respuestas(items)
        for posicion, r in enumerate(respuestas):
            r['orden_mostrado'] = len(respuestas) - posicion
        fila = _registrar(escenario, respuestas)

        por_orden = sorted(fila.respuestas, key=lambda r: r.orden_mostrado)
        assert [r.item_id for r in por_orden] == [i.id for i in items], (
            'el registro guardó el orden que declaró el cliente: afirma una '
            'pantalla que nadie vio')

    def test_una_inspeccion_de_la_noche_cuenta_para_HOY_en_Bogota(self, escenario):
        """Regla 5 del WMS. A las 8 p.m. de Colombia ya es mañana en UTC: con
        `utcnow().date()` el vehículo aparecería sin inspección hoy y con dos
        mañana."""
        # 2026-09-02 20:30 Bogotá = 2026-09-03 01:30 UTC
        fila = _registrar(escenario, _respuestas(_items(escenario)),
                          ts=datetime(2026, 9, 3, 1, 30))
        assert fila.dia == date(2026, 9, 2)

    def test_dos_turnos_el_mismo_dia_conviven(self, escenario):
        """No hay UNIQUE sobre `(vehiculo, día)`: dos turnos son reales, y
        negar el segundo empuja a no inspeccionar. Lo que impide que
        reinspeccionar sea la vía de escape es que los hallazgos ya nacidos no
        se borran."""
        items = _items(escenario)
        primera = _registrar(escenario, _respuestas(
            items, salvo={_bloqueante(items).id: dom.NO_APTO}), km=100)
        segunda = _registrar(escenario, _respuestas(items), km=120,
                             ts=UN_MIERCOLES + timedelta(hours=6))

        assert segunda.veredicto == dom.APTO
        assert Hallazgo.query.count() == 1, (
            'la segunda inspección «toda óptima» borró el daño de la primera: '
            'reinspeccionar hasta que dé apto se volvió gratis')
        del_dia = adaptador.del_dia(escenario['vehiculo_id'], date(2026, 9, 2))
        assert [f.id for f in del_dia] == [segunda.id, primera.id]

    def test_del_dia_devuelve_lista_vacia_y_no_revienta(self, escenario):
        """Un `one_or_none()` acá reventaría con `MultipleResultsFound` el
        primer día que haya dos turnos — así se cayó `/flota/conductor/mi-turno`
        el 2026-08-13."""
        assert adaptador.del_dia(escenario['vehiculo_id'], date(2026, 9, 2)) == []


# ══════════════════════════════════════════════════════════════════════════
# LA BASE — los CHECK, ejercidos con inserciones crudas
# ══════════════════════════════════════════════════════════════════════════

class TestLaBaseImponeLaRegla1:
    """`incompleta` ≠ `no_apto` **en disco**, no solo en Python.

    Un invariante que solo vive en el adaptador es una sugerencia: la segunda
    vía de escritura no lo hereda. Estas filas se escriben a mano, que es lo que
    hace un `UPDATE` desde psql — y es justo el gesto que usaría alguien
    poniendo `veredicto='apto'` sobre una inspección con nueve huecos.
    """

    def _lectura(self, escenario):
        db = escenario['db']
        l = LecturaOdometro(vehiculo_id=escenario['vehiculo_id'], valor_km=1,
                            ts=UN_MIERCOLES, origen='preoperacional',
                            autor_usuario_id=escenario['usuario_id'])
        db.session.add(l)
        db.session.flush()
        return l.id

    def _plantilla_id(self, escenario):
        return adaptador.plantilla_de(escenario['vehiculo']).id

    def _base(self, escenario, **extra):
        campos = dict(
            vehiculo_id=escenario['vehiculo_id'],
            plantilla_id=self._plantilla_id(escenario),
            dia=date(2026, 9, 2), respondida_ts=UN_MIERCOLES,
            inspeccionada_por_usuario_id=escenario['usuario_id'],
            lectura_id=self._lectura(escenario),
            veredicto=dom.APTO, items_esperados=28, items_sin_dato=0,
            bloqueantes_no_aptos=0, segundos_llenado=90,
        )
        campos.update(extra)
        return campos

    def test_una_fila_sana_entra(self, escenario):
        """La otra dirección de todo lo de abajo. Sin esto, un CHECK escrito de
        más —que rechace todo— pasaría los ocho tests siguientes."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(escenario)))
        db.session.commit()
        assert Inspeccion.query.count() == 1

    def test_apto_con_huecos_es_RECHAZADO_POR_LA_BASE(self, escenario):
        """**El invariante que sostiene el módulo entero.** Un `apto` sobre
        ítems no mirados es la fábrica de evidencia falsa de seguridad, y esa
        evidencia se usa frente a una aseguradora."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(
            escenario, veredicto=dom.APTO, items_sin_dato=9)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_apto_con_un_bloqueante_caido_es_rechazado(self, escenario):
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(
            escenario, veredicto=dom.APTO, bloqueantes_no_aptos=1)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_incompleta_SIN_ningun_hueco_es_rechazada(self, escenario):
        """`incompleta` afirma que falta información. Sin huecos, la fila dice
        dos cosas contrarias a la vez."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(
            escenario, veredicto=dom.INCOMPLETA, items_sin_dato=0)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_incompleta_con_un_bloqueante_caido_es_rechazada(self, escenario):
        """Si hay un bloqueante caído, el veredicto es `no_apto`: saber que está
        mal gana sobre no saber, y la base lo impone."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(
            escenario, veredicto=dom.INCOMPLETA, items_sin_dato=1,
            bloqueantes_no_aptos=1)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_no_apto_sin_ningun_bloqueante_caido_es_rechazado(self, escenario):
        """La dirección contraria: `no_apto` es «sé que está mal», y sin un
        bloqueante caído no se sabe nada de eso."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(
            escenario, veredicto=dom.NO_APTO, bloqueantes_no_aptos=0)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_un_veredicto_inventado_no_entra(self, escenario):
        """El `ELSE 1 = 0` del CHECK: un veredicto nuevo sin regla no pasa. El
        fallo es ruidoso y del lado conservador."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(escenario, veredicto='casi_apto')))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_una_inspeccion_de_cero_items_no_entra(self, escenario):
        """Con `items_esperados = 0` el CHECK de coherencia la declararía `apto`
        sin que nadie mirara un tornillo."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(escenario, items_esperados=0)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_segundos_negativos_no_entran(self, escenario):
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(escenario, segundos_llenado=-1)))
        with pytest.raises(IntegrityError):
            db.session.commit()

    def test_sin_lectura_no_se_puede_escribir_una_inspeccion(self, escenario):
        """Regla 3, impuesta por la base y no por acordarse."""
        db = escenario['db']
        db.session.add(Inspeccion(**self._base(escenario, lectura_id=None)))
        with pytest.raises(IntegrityError):
            db.session.commit()


class TestLaBaseImponeQueSoloUnNoAptoCuelgueUnHallazgo:
    """`ck_flota_resp_hallazgo_solo_si_no_apto`, con inserciones crudas.

    · un `optimo` con hallazgo es un daño que el registro dice que no existe;
    · un `sin_dato` con hallazgo convierte «no sé» en «está mal», que es la
      confusión exacta que separa `incompleta` de `no_apto`.
    """

    @pytest.fixture
    def contexto(self, escenario):
        """Una inspección real y un hallazgo real, para colgar filas a mano."""
        items = _items(escenario)
        item = next(i for i in items if i.criticidad == 'menor')
        fila = _registrar(escenario, _respuestas(
            items, salvo={item.id: dom.NO_APTO}))
        # Se borran las respuestas para poder escribir las de este test sin
        # chocar contra el UNIQUE de (inspección, ítem).
        RespuestaItem.query.delete()
        escenario['db'].session.commit()
        return {'inspeccion_id': fila.id, 'items': items,
                'hallazgo_id': Hallazgo.query.one().id, 'db': escenario['db']}

    def _fila(self, contexto, respuesta, con_hallazgo, indice=0):
        return RespuestaItem(
            inspeccion_id=contexto['inspeccion_id'],
            item_id=contexto['items'][indice].id,
            respuesta=respuesta, orden_mostrado=indice + 1,
            hallazgo_id=contexto['hallazgo_id'] if con_hallazgo else None,
        )

    def test_un_no_apto_CON_hallazgo_entra(self, contexto):
        """La dirección sana. Sin esto, un CHECK que rechazara todo dejaría los
        dos tests de abajo en verde y el puente al hallazgo roto."""
        contexto['db'].session.add(
            self._fila(contexto, dom.NO_APTO, con_hallazgo=True))
        contexto['db'].session.commit()
        assert RespuestaItem.query.count() == 1

    def test_un_optimo_con_hallazgo_es_rechazado(self, contexto):
        contexto['db'].session.add(
            self._fila(contexto, dom.OPTIMO, con_hallazgo=True))
        with pytest.raises(IntegrityError):
            contexto['db'].session.commit()

    def test_un_sin_dato_con_hallazgo_es_rechazado(self, contexto):
        """El peor de los dos: no saber no produce daños, produce una
        inspección que no autoriza."""
        contexto['db'].session.add(
            self._fila(contexto, dom.SIN_DATO, con_hallazgo=True))
        with pytest.raises(IntegrityError):
            contexto['db'].session.commit()

    def test_un_optimo_SIN_hallazgo_entra(self, contexto):
        """La otra dirección del par: lo que el CHECK prohíbe es el hallazgo
        colgado, no la respuesta."""
        contexto['db'].session.add(
            self._fila(contexto, dom.OPTIMO, con_hallazgo=False))
        contexto['db'].session.commit()
        assert RespuestaItem.query.count() == 1

    def test_una_respuesta_fuera_del_vocabulario_no_entra(self, contexto):
        contexto['db'].session.add(
            self._fila(contexto, 'regular', con_hallazgo=False))
        with pytest.raises(IntegrityError):
            contexto['db'].session.commit()

    def test_el_mismo_item_no_se_contesta_dos_veces(self, contexto):
        """Con dos filas, los conteos del padre dejan de describir a los hijos y
        el CHECK del veredicto valida contra un número inventado."""
        contexto['db'].session.add(
            self._fila(contexto, dom.OPTIMO, con_hallazgo=False, indice=0))
        contexto['db'].session.flush()
        fila = self._fila(contexto, dom.OPTIMO, con_hallazgo=False, indice=0)
        fila.orden_mostrado = 7
        contexto['db'].session.add(fila)
        with pytest.raises(IntegrityError):
            contexto['db'].session.commit()

    def test_dos_items_no_ocupan_el_mismo_renglon(self, contexto):
        """`orden_mostrado` promete ser la pantalla que se vio, y una pantalla
        con dos ítems en el renglón 3 no existió."""
        contexto['db'].session.add(
            self._fila(contexto, dom.OPTIMO, con_hallazgo=False, indice=0))
        contexto['db'].session.flush()
        fila = self._fila(contexto, dom.OPTIMO, con_hallazgo=False, indice=1)
        fila.orden_mostrado = 1
        contexto['db'].session.add(fila)
        with pytest.raises(IntegrityError):
            contexto['db'].session.commit()
