"""
Donde el conductor contesta — la tabla que faltaba de la tanda 2.

`flota/dominio/inspeccion.py` tiene la política desde la tanda 1: qué ítems
tocan hoy, en qué orden, y cuántos días de plazo lleva cada criticidad. Los 53
ítems están sembrados en producción desde el 2026-08-02. Lo que no existía era
**dónde responderlos**: el conductor no tenía formulario, así que la política
ordenaba una lista que nadie veía.

Este adaptador es la puerta. Dos verbos y ninguno más:

```
items_del_dia_de(vehiculo, dia)  →  qué se le pregunta hoy, en qué orden
registrar(...)                   →  la inspección respondida, con su veredicto
```

No hay `corregir` ni `reabrir`. Una inspección es lo que alguien dijo haber
visto en un momento; editarla después es cambiar el testimonio sin dejar
rastro. Si el conductor se equivocó, inspecciona de nuevo y quedan las dos —
que además es lo que hace visible el patrón de «reinspeccionar hasta que dé
apto».

## Las cuatro decisiones que no son de quien llama

· **El veredicto se calcula** (regla 1). No se recibe. Recibirlo dejaría que
  quien responde declare `apto` sobre una lista a medias, que es exactamente el
  camino barato de la regla 11.
· **El orden de los ítems lo decide `items_del_dia`** (regla 11). Bloqueantes
  fijos, el resto barajado por hash de la fecha. El cliente no lo elige y
  tampoco lo informa: `orden_mostrado` sale del servidor, o el registro
  afirmaría una pantalla que nadie vio.
· **La plantilla sale del tipo del vehículo** (regla 5). Sin `.get(tipo,
  'camion')`: un tipo sin catálogo levanta con el motivo escrito, en vez de
  recibir un formulario ajeno con la mitad de las casillas inaplicables.
· **El kilometraje entra por la misma puerta que el hallazgo** (regla 3), y es
  literalmente la misma función — `hallazgos.anclar_odometro`. Una inspección y
  los tres daños que encontró cuelgan de UNA lectura, porque eso es lo que
  pasó: alguien miró el tablero una vez.

## Por qué el hallazgo nace automático y no lo confirma el conductor

Un ítem marcado `no_apto` produce su hallazgo **en la misma transacción**, sin
un segundo gesto. La alternativa —«marcaste una falla, ¿querés reportarla?»—
suena más respetuosa de la regla 2 y es peor por dos razones:

1. **Regla 11, que es la pregunta que decide.** Con confirmación, el camino más
   barato no es «marco no_apto y no confirmo»: es **no marcar `no_apto`**. Un
   paso extra le pone precio a la honestidad, y lo que se paga con ese precio
   es que el freno quede en `optimo`. El sistema entero existe para que marcar
   la falla sea lo más fácil que se pueda hacer.
2. **Regla 2 no lo prohíbe, porque el hallazgo no imputa a nadie.** Lo dice el
   propio `hallazgos.reportar`: `reportado_por_usuario_id` es quien lo vio y
   `custodia_id` es bajo la custodia de quién apareció — dos hechos. Quién
   responde lo decide un humano fuera de esa tabla. Lo que la regla 2 prohíbe
   es que un cálculo señale a una persona, y acá no hay ningún cálculo: hay un
   humano que marcó una falla con el dedo. El automatismo no decide nada que él
   no haya dicho; le ahorra volver a escribirlo.

Y `sin_dato` **nunca** produce hallazgo. «No sé» no es «está mal»: bloquea el
despacho vía `incompleta` y no le abre a nadie una tarea con fecha límite sobre
un daño que nadie constató. El CHECK
`ck_flota_resp_hallazgo_solo_si_no_apto` lo respalda en disco.
"""
from datetime import datetime
from typing import Dict, List, Optional

from app.extensions import db
from app.utils.fecha import TZ_BOGOTA
from flota.adaptadores import hallazgos as adaptador_hallazgos
from flota.adaptadores.modelos import (Inspeccion, ItemInspeccion,
                                       PlantillaInspeccion, RespuestaItem)
from flota.adaptadores.traspaso import custodia_activa
from flota.dominio import inspeccion as dom
from flota.dominio.errores import ErrorFlota
from flota.dominio.valores import OrigenLectura


class InspeccionInvalida(ErrorFlota):
    """La inspección no se puede registrar. No se registra a medias."""


def _dia_bogota(ts: datetime):
    """El día que quien inspecciona LEE como hoy.

    `utcnow().date()` en Railway devuelve el día siguiente a partir de las
    7 p.m. de Colombia: una inspección de la tarde contaría para mañana, el
    vehículo aparecería sin inspección hoy y con dos mañana. Es la regla 5 del
    WMS aplicada a la única fecha de esta tabla que alguien lee como día — el
    timestamp técnico sigue en UTC, como debe.
    """
    from datetime import timezone

    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()


def plantilla_de(vehiculo) -> PlantillaInspeccion:
    """El catálogo activo que le corresponde a este vehículo.

    QUÉ AFIRMA: que existe una plantilla activa, sembrada, para el tipo de este
    vehículo.

    QUÉ NO AFIRMA: que sea la única que existió. Las plantillas se versionan y
    no se editan; esto devuelve la activa de hoy, y la inspección guarda cuál
    fue para que dentro de dos años se sepa qué se preguntó.

    **Distingue dos fallos que se ven parecidos y no lo son**: «no sé qué
    preguntarle a un vehículo de este tipo» (decisión que falta, la resuelve
    `PLANTILLA_POR_TIPO`) y «sé qué preguntarle y el catálogo no está sembrado»
    (falta correr `scripts/sembrar_plantillas_flota.py`). Con un solo mensaje,
    quien lo lea a las 5 a.m. no sabe a quién llamar.
    """
    try:
        aplica_a = dom.plantilla_de_tipo(vehiculo.tipo)
    except ValueError as e:
        raise InspeccionInvalida(str(e))

    plantilla = (PlantillaInspeccion.query
                 .filter_by(aplica_a=aplica_a, activa=True)
                 .order_by(PlantillaInspeccion.version.desc())
                 .first())
    if plantilla is None:
        raise InspeccionInvalida(
            f'el vehículo {vehiculo.placa} es de tipo {vehiculo.tipo!r}, que '
            f'usa el catálogo {aplica_a!r}, y no hay ninguna plantilla activa '
            f'con ese código en la base. Sembrala con '
            f'scripts/sembrar_plantillas_flota.py antes de inspeccionar: sin '
            f'catálogo, el formulario saldría vacío y una inspección de cero '
            f'ítems no es una inspección.'
        )
    return plantilla


def items_del_dia_de(vehiculo, dia) -> List[ItemInspeccion]:
    """Los ítems que hay que preguntar hoy, en el orden en que van en pantalla.

    QUÉ AFIRMA: que este es el orden que ve TODO el que inspeccione hoy, y que
    se puede reconstruir mañana para auditar una inspección vieja.

    QUÉ NO AFIRMA: que el conductor los haya visto. Eso lo afirman las filas de
    `flota_respuesta_item`, que es otra cosa.

    El orden lo pone `dominio.inspeccion.items_del_dia` — bloqueantes fijos, el
    resto barajado por hash de la fecha (regla 11). No se reimplementa acá ni se
    "mejora" ordenando por criticidad: con orden fijo, a la tercera semana el
    pulgar responde sin leer.
    """
    return dom.items_del_dia(plantilla_de(vehiculo).items, dia)


def _validar_respuestas(crudas, esperados) -> Dict[int, dict]:
    """`{item_id: {'respuesta':…, 'nota':…}}`, o levanta diciendo qué está mal.

    Se juzga TODO antes de escribir nada. Un ítem ajeno o una respuesta
    inventada no puede descubrirse a mitad del INSERT: la inspección quedaría
    con la mitad de las filas y un veredicto que no las describe.
    """
    if not isinstance(crudas, list):
        raise InspeccionInvalida(
            'respuestas tiene que ser una lista de {item_id, respuesta}; llegó '
            f'{type(crudas).__name__}.')

    ids_esperados = {i.id for i in esperados}
    por_item: Dict[int, dict] = {}
    for cruda in crudas:
        if not isinstance(cruda, dict) or 'item_id' not in cruda \
                or 'respuesta' not in cruda:
            raise InspeccionInvalida(
                'cada respuesta necesita `item_id` y `respuesta`. Sin default: '
                'un ítem sin respuesta declarada es `sin_dato` porque no vino, '
                'no porque se lo haya rellenado.')
        try:
            item_id = int(cruda['item_id'])
        except (TypeError, ValueError):
            raise InspeccionInvalida(f'item_id inválido: {cruda["item_id"]!r}')

        if item_id not in ids_esperados:
            raise InspeccionInvalida(
                f'el ítem {item_id} no es de los que tocaban hoy para este '
                f'vehículo. O la pantalla se quedó abierta hasta pasada la '
                f'medianoche —los semanales solo entran los lunes— o son de '
                f'otra plantilla. Volvé a abrir la inspección: no se guarda '
                f'nada contra una lista que ya no es la de hoy.')
        if item_id in por_item:
            raise InspeccionInvalida(
                f'el ítem {item_id} viene dos veces. Con dos respuestas para el '
                f'mismo ítem, los conteos del resumen dejan de describir lo que '
                f'se contestó.')

        respuesta = cruda['respuesta']
        if respuesta not in dom.RESPUESTAS:
            raise InspeccionInvalida(
                f'respuesta inválida para el ítem {item_id}: {respuesta!r}. '
                f'Las válidas son {sorted(dom.RESPUESTAS)}.')

        nota = cruda['nota'] if 'nota' in cruda else None
        por_item[item_id] = {
            'respuesta': str(respuesta),
            'nota': (nota or '').strip() or None,
        }
    return por_item


def _descripcion_del_hallazgo(item: ItemInspeccion, nota: Optional[str]) -> str:
    """Qué dice el daño que nace de un ítem marcado `no_apto`.

    El nombre del ítem ES la descripción, y es mejor que la que un conductor
    escribiría a las 5 a.m. con una mano: «Llantas: flanco, labrado y tuercas»
    dice qué mirar; «llanta mala» no. La nota se suma cuando existe, porque
    ahí está lo que el catálogo no puede saber —cuál rueda, qué tan grande—.

    Exigir la nota habría sido ponerle precio a marcar la falla (regla 11), y
    lo que se paga con ese precio es un `optimo`.
    """
    return f'{item.nombre} — {nota}' if nota else item.nombre


def registrar(
    *,
    vehiculo_id: int,
    tipo_vehiculo_obj,
    km: int,
    inspeccionada_por_usuario_id: int,
    respuestas: list,
    segundos_llenado: int,
    observacion: Optional[str] = None,
    ts: Optional[datetime] = None,
) -> Inspeccion:
    """Registra la inspección respondida y devuelve la fila con su veredicto.

    QUÉ AFIRMA al devolver: que quedaron escritas TODAS las respuestas del día
    —incluidos los ítems que nadie tocó, como `sin_dato`—, que el veredicto se
    calculó sobre esa lista completa, que el kilometraje quedó anclado a una
    lectura real, y que cada `no_apto` tiene su hallazgo con fecha límite
    corriendo.

    QUÉ NO AFIRMA: que el vehículo esté bien, ni que alguien haya mirado de
    verdad. Un `apto` afirma que se contestó todo y que ningún bloqueante
    falló; `segundos_llenado` está para que se pueda ver cuánto tardó en
    contestarlo, que es otra pregunta y la única que distingue mirar de marcar.

    Tampoco afirma que el vehículo vaya a quedarse en patio. Hoy esto se
    publica y no bloquea — medir → corregir → imponer, en ese orden.

    Todo o nada: una inspección con la mitad de sus respuestas escritas tendría
    un veredicto que no describe sus filas, que es peor que no tener ninguna.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    # ── 1. Juzgar ANTES de escribir ──────────────────────────────────────
    try:
        segundos = int(segundos_llenado)
    except (TypeError, ValueError):
        raise InspeccionInvalida(
            f'segundos_llenado inválido: {segundos_llenado!r}. Es el hecho que '
            f'permite ver si alguien marcó todo óptimo en veinte segundos.')
    if segundos < 0:
        raise InspeccionInvalida(
            f'segundos_llenado negativo ({segundos}): eso no es un llenado '
            f'rápido, es un reloj al revés.')

    dia = _dia_bogota(ahora)
    plantilla = plantilla_de(tipo_vehiculo_obj)
    esperados = dom.items_del_dia(plantilla.items, dia)
    if not esperados:
        raise InspeccionInvalida(
            f'la plantilla {plantilla.codigo} no tiene ningún ítem que aplique '
            f'el {dia}. Una inspección de cero ítems no es "todo bien": es que '
            f'no se miró nada.')

    por_item = _validar_respuestas(respuestas, esperados)

    # La lista COMPLETA del día, no solo lo que vino: el ítem que nadie tocó
    # entra como `sin_dato` y por eso cuenta para el veredicto (regla 1).
    respondidos = [
        dom.ItemRespondido(
            criticidad=i.criticidad,
            respuesta=(por_item[i.id]['respuesta'] if i.id in por_item
                       else str(dom.SIN_DATO)),
        )
        for i in esperados
    ]
    veredicto = dom.veredicto(respondidos)
    conteos = dom.conteos(respondidos)

    custodia = custodia_activa(vehiculo_id)

    try:
        # Regla 3, y la MISMA función que usa el hallazgo (regla 0 del WMS):
        # si el km no cambió se reutiliza la última lectura, así que la
        # inspección y los hallazgos que produzca cuelgan de una sola. Es lo
        # que de verdad pasó: alguien miró el tablero una vez.
        lectura = adaptador_hallazgos.anclar_odometro(
            vehiculo_id, km, inspeccionada_por_usuario_id, ahora,
            origen=OrigenLectura.PREOPERACIONAL)

        fila = Inspeccion(
            vehiculo_id=vehiculo_id,
            plantilla_id=plantilla.id,
            dia=dia,
            respondida_ts=ahora,
            inspeccionada_por_usuario_id=inspeccionada_por_usuario_id,
            lectura_id=lectura.id,
            custodia_id=custodia.id if custodia is not None else None,
            veredicto=veredicto,
            items_esperados=conteos.items_esperados,
            items_sin_dato=conteos.items_sin_dato,
            bloqueantes_no_aptos=conteos.bloqueantes_no_aptos,
            segundos_llenado=segundos,
            observacion=(observacion or '').strip() or None,
        )
        db.session.add(fila)
        db.session.flush()

        for orden, item in enumerate(esperados, 1):
            dicho = por_item[item.id] if item.id in por_item else None
            respuesta = dicho['respuesta'] if dicho else str(dom.SIN_DATO)
            nota = dicho['nota'] if dicho else None

            hallazgo_id = None
            if respuesta == dom.NO_APTO:
                # UNA sola vía de nacimiento de hallazgos (regla 6): la fecha
                # límite se calcula ahí y en ningún otro lado. `commit=False`
                # porque la transacción es de acá.
                hallazgo = adaptador_hallazgos.reportar(
                    vehiculo_id=vehiculo_id,
                    criticidad=item.criticidad,
                    descripcion=_descripcion_del_hallazgo(item, nota),
                    km=km,
                    reportado_por_usuario_id=inspeccionada_por_usuario_id,
                    item_id=item.id,
                    ts=ahora,
                    commit=False,
                )
                hallazgo_id = hallazgo.id

            db.session.add(RespuestaItem(
                inspeccion_id=fila.id,
                item_id=item.id,
                respuesta=respuesta,
                orden_mostrado=orden,
                nota=nota,
                hallazgo_id=hallazgo_id,
            ))

        db.session.commit()
        return fila
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba.
        # Es también el rollback que `reportar(commit=False)` NO hace, por
        # acuerdo explícito: el que abre la transacción la cierra.
        db.session.rollback()
        raise


def del_dia(vehiculo_id: int, dia) -> List[Inspeccion]:
    """Las inspecciones de ese vehículo ese día, la más reciente primero.

    Devuelve una lista y no `una o None` **a propósito**: la tabla permite dos
    turnos en el mismo día sobre el mismo vehículo, y un `one_or_none()` acá
    reventaría con `MultipleResultsFound` el primer día que eso pase — que es
    exactamente cómo se cayó `/flota/conductor/mi-turno` el 2026-08-13.
    """
    return (Inspeccion.query
            .filter_by(vehiculo_id=vehiculo_id, dia=dia)
            .order_by(Inspeccion.respondida_ts.desc(), Inspeccion.id.desc())
            .all())


__all__ = ['registrar', 'items_del_dia_de', 'plantilla_de', 'del_dia',
           'InspeccionInvalida']
