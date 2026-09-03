"""
Taller y garantía — vocabulario y cálculo. Sin I/O, sin framework.

## Qué hace este módulo y qué NO hace

Hace **aritmética sobre hechos ya registrados**: sumar meses a un día, sumar
kilómetros a un odómetro, y decidir si una garantía todavía cubre. Nada de acá
lee la base ni decide permisos.

No decide **cuánto costó** nada. La orden de trabajo no lleva valor y este
módulo no tiene una sola función que devuelva pesos: el camión entra hoy y la
factura llega el 30, y una OT con `valor NOT NULL` obliga a inventar un número
para poder cerrarla. La plata vive en `flota_gasto` (§4 del plan, «Dónde vive el
dinero») y el cálculo que la divide entre kilómetros vive en
`flota/dominio/costos.py`.

No nombra a nadie. Ningún cálculo de acá toma un mecánico, un conductor ni un
custodio como entrada: la regla 2 del módulo no es una advertencia de redacción,
es una restricción de firma.

## Las dos decisiones que este archivo existe para sostener

**1. La garantía va en la INTERVENCIÓN, no en la orden de trabajo.** Una visita
al taller produce varios trabajos con garantías distintas: el embrague trae 6
meses, el cambio de aceite ninguna. Con un campo en la OT hay que dar una sola
respuesta para una bolsa mezclada, y la respuesta única que no miente es la peor
de todas — o sea, ninguna garantía.

**2. La fecha y el kilometraje de vencimiento se CALCULAN al nacer.** Es la
regla 6 del módulo aplicada a otra cosa: si alguien puede escribir «ésta vence
en marzo», la garantía dejó de significar algo. Acá está el cálculo; el
adaptador lo llama y la frontera **rechaza** un cuerpo que traiga los campos
derivados, en vez de ignorarlos.
"""
from calendar import monthrange
from datetime import date
from typing import Optional, Union

from flota.dominio.valores import SIN_DATO


# ── Vocabularios ─────────────────────────────────────────────────────────────
#
# Viven acá y la tabla los sigue, igual que `CATEGORIAS_GASTO` en `costos.py` y
# `EstadoHallazgo` en `hallazgo.py`. Un enum de Python que la base no conoce es
# una convención; un CHECK armado desde esta tupla es una garantía.

#: Por qué entró el camión al taller.
#:
#: Dos y no tres: `garantia` NO es un tipo. Un trabajo cubierto por garantía
#: sigue siendo una reparación correctiva —algo se rompió— y lo que cambia es
#: quién paga, que es un hecho de la factura y no del motivo de la visita.
#: Modelarlo como tipo obligaría a decidir la cobertura al abrir la OT, que es
#: exactamente el momento en que todavía no se sabe.
#:
#: `preventiva` existe porque una visita programada también es una visita, y sin
#: el valor habría que mentir llamándola correctiva. **La OT preventiva no se
#: engancha todavía con `flota_plan_tarea`** (fase 3, otro alcance): acá es una
#: palabra honesta, no una relación.
TIPOS_OT = ('correctiva', 'preventiva')

#: Dónde está la orden de trabajo.
#:
#: `anulada` no es un lujo: sin ella, la única salida de una OT abierta por
#: error es cerrarla como si el trabajo se hubiera hecho — y esa OT queda para
#: siempre contando en «trabajos hechos sin factura recibida». Exige motivo
#: escrito, igual que `descartar` en el hallazgo y por el mismo motivo: es la
#: salida que usaría quien no quiere hacer el trabajo (regla 11), y lo que la
#: hace cara no es prohibirla sino que quede escrito quién y por qué.
ESTADOS_OT = ('abierta', 'cerrada', 'anulada')

#: Qué parte del vehículo se tocó. **Catálogo cerrado, y por eso generoso.**
#:
#: Es la clave de la búsqueda que hace que esto valga la plata: al abrir una OT
#: correctiva se buscan intervenciones con garantía vigente **sobre el mismo
#: sistema del mismo vehículo**. Sin un vocabulario cerrado la comparación sería
#: entre dos textos libres y no encontraría nada — «caja» y «transmisión» son la
#: misma pieza escrita de dos formas, y el día que no coincidan la reparación se
#: paga dos veces, que es justo el caso que esto evita.
#:
#: `embrague` va aparte de `transmision` aunque mecánicamente cuelgue de ella,
#: porque es el ejemplo literal del plan («el embrague trae 6 meses») y porque
#: es lo que un mecánico escribe en una factura. Un catálogo que obliga a
#: traducir se llena de `otro`.
#:
#: `TIPOS_DOCUMENTO` se cerró en cuatro y los cuatro que el dueño pidió después
#: **dan 400**. Esa lección está a un mes de distancia: acá el catálogo nace
#: ancho y existe `otro` **con descripción obligatoria** (CHECK en la base) para
#: que un trabajo real nunca se quede sin poder registrarse. Si aparece tres
#: veces la misma palabra en esa descripción, eso es un sistema pidiendo
#: existir, y se agrega **acá**.
SISTEMAS = (
    'motor',
    'transmision',
    'embrague',
    'frenos',
    'suspension',
    'direccion',
    'electrico',
    'refrigeracion',
    'escape',
    'llantas',
    'carroceria',
    'aire_acondicionado',
    'otro',
)

#: Qué dice la factura sobre la garantía. **Tres valores, sin default.**
#:
#: Una factura que no dice nada **no es una factura sin garantía**: es que no se
#: preguntó. Son dos hechos distintos y colapsarlos tiene una dirección cara —
#: `no` cierra la puerta a reclamar, `sin_dato` la deja abierta con la duda
#: escrita. Regla 4 del módulo, y sin `server_default` en la columna: hay que
#: escribir la palabra, aunque la palabra sea `sin_dato`.
GARANTIA_DECLARADA = ('si', 'no', 'sin_dato')


def es_sistema(sistema: str) -> bool:
    """¿Este sistema está en el catálogo? Levanta si no.

    Total sobre el vocabulario y **sin `.get(sistema, False)`** (regla 5): un
    sistema desconocido tiene que reventar. Degradarlo a `otro` en silencio
    haría que la búsqueda de garantía vigente no lo encontrara nunca — sin
    error, sin aviso, y con la reparación pagada dos veces.
    """
    if sistema not in SISTEMAS:
        raise ValueError(
            f'sistema de vehículo desconocido: {sistema!r}. '
            f'Conocidos: {", ".join(SISTEMAS)}.'
        )
    return True


def exige_descripcion(sistema: str) -> bool:
    """¿Este sistema obliga a escribir qué se hizo?

    Solo `otro`. Es la válvula del catálogo cerrado y no puede ser la salida
    cómoda: cuesta escribir qué fue. Sin esto, `otro` se convierte en el 40% de
    las intervenciones y el desglose por sistema deja de decir nada —que es el
    mismo criterio de `categoria = 'otro'` en `flota_gasto`, escrito una vez por
    tabla porque son dos vocabularios distintos.
    """
    es_sistema(sistema)
    return sistema == 'otro'


# ── El cálculo de la garantía ────────────────────────────────────────────────

def sumar_meses(dia: date, meses: int) -> date:
    """`dia` más N meses calendario, recortando al último día del mes destino.

    QUÉ AFIRMA: que el día devuelto es el mismo número de día N meses después, o
    el último del mes cuando ese número no existe allá.

    QUÉ NO AFIRMA: que sean 30·N días. No lo son y no deben serlo — una garantía
    de «6 meses» dada el 15 de enero vence el 15 de julio, que es lo que dice la
    factura y lo que va a decir el taller cuando se reclame.

    El recorte es el caso que rompe todo lo demás: **31 de enero más un mes**. No
    existe el 31 de febrero, y `timedelta(days=30)` daría el 2 de marzo — una
    garantía dos días más larga que la que se pactó, en la dirección de reclamar
    algo que ya venció. Se recorta al 28 (o 29): el último día que sí existe.
    """
    if meses < 0:
        raise ValueError('una garantía no dura un número negativo de meses')
    total = (dia.year * 12 + (dia.month - 1)) + meses
    anio, mes = divmod(total, 12)
    mes += 1
    return date(anio, mes, min(dia.day, monthrange(anio, mes)[1]))


def dia_fin_garantia(dia_trabajo: date, meses: int) -> date:
    """El **último día** que la garantía todavía cubre.

    QUÉ AFIRMA: que un reclamo hecho en cualquier momento de este día está
    dentro del plazo.

    QUÉ NO AFIRMA: el instante exacto en que vence, ni que el taller vaya a
    responder. Es la fecha que la factura pactó; quién la honra es otra
    conversación, y este número existe para poder tenerla.

    **Un día, no un instante** — la misma forma que `inspeccion.dia_limite`, y
    por el mismo motivo: un plazo guardado como instante vence a la hora en que
    se hizo el trabajo, así que una garantía dada a las 4 p.m. estaría vencida a
    las 4:01 p.m. del último día para quien la reclama a las 5.

    El día del límite **cuenta como cubierto** (`<=`, no `<`). Es el lado
    conservador de la regla 0 aplicado acá: dar un día de más cuesta una llamada
    al taller; dar uno de menos cuesta pagar dos veces la misma reparación, que
    es exactamente el caso que esta función existe para evitar.
    """
    if meses <= 0:
        raise ValueError(
            'una garantía de cero meses no es una garantía corta: es una '
            'garantía que no se declaró. Para eso está `garantia_declarada`.')
    return sumar_meses(dia_trabajo, meses)


def km_fin_garantia(km_trabajo: int, km_garantia: int) -> int:
    """El **último kilometraje** que la garantía todavía cubre.

    QUÉ AFIRMA: que el odómetro de ese vehículo, hasta ese número, está dentro
    del plazo pactado.

    QUÉ NO AFIRMA: que el vehículo vaya a llegar ahí, ni cuándo. Los kilómetros
    y los meses son dos relojes distintos que corren a velocidades distintas, y
    por eso se guardan los dos.

    `km_trabajo` es el odómetro **al que se hizo el trabajo** —la lectura de la
    orden de trabajo—, no el de hoy. Calcularlo contra el de hoy le regalaría a
    la garantía todos los kilómetros que pasaron entre el taller y el momento en
    que alguien tecleó la factura.
    """
    if km_garantia <= 0:
        raise ValueError(
            'una garantía de cero kilómetros no es una garantía corta: es una '
            'garantía que no se declaró.')
    if km_trabajo < 0:
        raise ValueError('un odómetro no puede ser negativo')
    return km_trabajo + km_garantia


def vigencia(*, hasta_fecha: Optional[date], hasta_km: Optional[int],
             dia: date, km_actual: Optional[int]) -> dict:
    """¿Esta garantía todavía cubre? Con las dos dimensiones **por separado**.

    Devuelve `{'por_fecha', 'por_km', 'vigente'}`, cada uno en
    `{True, False, SIN_DATO}`.

    QUÉ AFIRMA: que sobre los datos registrados, la garantía cubre (o no) por
    cada dimensión declarada.

    QUÉ NO AFIRMA:

    · **Que el taller vaya a responder.** Una garantía vigente es un argumento
      para no pagar dos veces, no una decisión ya tomada.
    · **Que las dos dimensiones se hayan podido juzgar.** Por eso salen las tres
      juntas: `vigente=True` con `por_km=SIN_DATO` significa «cubre por fecha y
      del kilometraje no sabemos», y quien llame al taller tiene que saberlo.

    ## Por qué `o` y no `y`, que es lo contrario de lo que dice la factura

    Una garantía comercial real dice «6 meses **o** 10.000 km, lo que ocurra
    primero» — o sea que expira cuando *cualquiera* de las dos se pasa, que es un
    `y` sobre la vigencia. Acá se usa `o`: **basta que una dimensión cubra para
    proponer el caso**.

    Es deliberado y es la regla 0. Esto **propone, no bloquea**: lo que produce
    es un renglón en la pantalla de quien va a mandar el camión. Los dos errores
    no cuestan lo mismo — no mostrar una garantía que sí cubría cuesta pagar dos
    veces la misma reparación (el caso entero que esto evita); mostrar una que
    ya venció por kilómetros cuesta una llamada al taller. Se elige el error
    barato, y **las dos dimensiones viajan con su valor** para que quien mira
    pueda ver cuál de las dos es la que cubre.

    ## Las dos formas de `SIN_DATO`, y por qué no se distinguen acá

    Una dimensión sale `SIN_DATO` porque no se declaró (`hasta_km IS NULL`) o
    porque el vehículo no tiene ninguna lectura de odómetro con qué comparar.
    Las dos significan «no se puede juzgar», que es lo que esta función
    contesta. **Cuál de las dos fue se lee de los datos que viajan al lado** —
    `hasta_km` y `km_actual` están los dos en la respuesta— y no de una cuarta
    palabra que habría que mantener en tres archivos.

    Con las dos dimensiones sin juzgar, `vigente` es `SIN_DATO` y **nunca
    `False`**: `False` diría «se revisó y ya venció», y lo que pasa es que no
    hay contra qué revisar. Es el mismo criterio de `excede_capacidad` con una
    ficha sin capacidad de tanque.
    """
    por_fecha = SIN_DATO if hasta_fecha is None else (dia <= hasta_fecha)
    por_km = (SIN_DATO if (hasta_km is None or km_actual is None)
              else (km_actual <= hasta_km))

    if por_fecha is True or por_km is True:
        cubre = True
    elif por_fecha is SIN_DATO and por_km is SIN_DATO:
        cubre = SIN_DATO
    else:
        cubre = False
    return {'por_fecha': por_fecha, 'por_km': por_km, 'vigente': cubre}


def cubre(v: dict) -> bool:
    """`True` solo cuando la garantía cubre de verdad. `SIN_DATO` **no cuenta**.

    Existe para que ningún llamador escriba `if v['vigente']:` — `SIN_DATO` es
    una cadena no vacía y por lo tanto **verdadera en contexto booleano** (ver
    `valores.SIN_DATO`, propiedad 1). Ese `if` contaría como vigente toda
    garantía que no se pudo juzgar, que es el default optimista que la regla 1
    del módulo prohíbe — y lo haría en el número que el health publica.

    Vive acá y no en cada frontera porque tiene dos consumidores: el listado por
    vehículo y el contador del tablero. Escrito dos veces, el día que alguien
    cambie el criterio los dos números dirían cosas distintas de la misma flota
    (regla 0).
    """
    return v['vigente'] is True


__all__ = [
    'TIPOS_OT', 'ESTADOS_OT', 'SISTEMAS', 'GARANTIA_DECLARADA',
    'es_sistema', 'exige_descripcion',
    'sumar_meses', 'dia_fin_garantia', 'km_fin_garantia', 'vigencia', 'cubre',
    'SIN_DATO',
]
