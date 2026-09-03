"""
Políticas de inspección. Sin I/O, sin framework.

Dos reglas viven acá y no en la pantalla, porque las dos son decisiones de
diseño que alguien va a querer "mejorar" sin saber qué rompen.
"""
import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Sequence

from flota.dominio.valores import SIN_DATO, normalizar_tipo

# Regla 6: todo hallazgo nace con severidad y fecha límite, y el plazo se
# CALCULA, no se elige a mano. Una sola tabla, consumida por todos — si el
# plazo se pudiera escribir a dedo, la severidad dejaría de significar algo y
# "bloqueante" pasaría a ser una etiqueta que se negocia.
DIAS_DE_PLAZO = {'bloqueante': 0, 'mayor': 7, 'menor': 30}

# ── Regla 1: ningún ítem tiene valor por defecto ─────────────────────────────
#
# Las tres respuestas posibles, y `sin_dato` es una de ellas por derecho propio:
# un ítem no respondido es `sin_dato`, jamás `optimo`. La ausencia se declara
# con una palabra (regla 4) y no con una fila que no existe — sin la fila no se
# puede distinguir "no lo miró" de "el ítem no aplicaba ese día".
#
# `SIN_DATO` se IMPORTA de `valores`, no se redefine acá: es la misma palabra
# con la que el módulo entero dice «no sé», y dos constantes con el mismo texto
# se separan el día que una de las dos cambie. `NO_APTO` es a la vez respuesta
# de un ítem y veredicto de la inspección a propósito — significan lo mismo a
# dos escalas: «sé que está mal».
OPTIMO   = 'optimo'
NO_APTO  = 'no_apto'
RESPUESTAS = (OPTIMO, NO_APTO, SIN_DATO)

# ── El veredicto, y por qué son TRES y no dos ────────────────────────────────
#
# `incompleta` NO es lo mismo que `no_apto`. `no_apto` es «sé que está mal»;
# `incompleta` es «no sé». Los dos niegan el despacho, pero decir «no apto»
# cuando lo que pasó es que nadie miró convierte el registro en evidencia falsa
# —el vehículo aparece rechazado por una falla que nunca se constató— y decir
# «apto» sobre lo no mirado es la fábrica de evidencia falsa de seguridad que la
# regla 1 existe para impedir. Ninguna de las dos autoriza; las dos se corrigen
# distinto: `no_apto` se arregla en el taller, `incompleta` se arregla mirando.
APTO       = 'apto'
INCOMPLETA = 'incompleta'
VEREDICTOS = (APTO, NO_APTO, INCOMPLETA)


@dataclass(frozen=True)
class ItemRespondido:
    """Un ítem del día con lo que se contestó sobre él.

    Es lo mínimo que el veredicto necesita: qué tan grave es el ítem y qué se
    dijo. Ni el nombre ni el gesto entran — el veredicto no los mira, y pedirlos
    ataría esta política a la forma de la tabla.
    """

    criticidad: str
    respuesta: str


@dataclass(frozen=True)
class Conteos:
    """Los tres números que resumen una inspección, y que la BASE constriñe.

    Existen porque un `CHECK` no puede mirar las filas hijas: para que el
    veredicto tenga respaldo en disco —y no solo en Python— la fila padre lleva
    escritos los conteos de los que se deriva, y el `CHECK` verifica que
    veredicto y conteos digan lo mismo.

    Se calculan acá, una sola vez, para que el número que se guarda y el
    veredicto que se guarda salgan del mismo recorrido. Contarlos aparte en el
    adaptador sería la segunda implementación de la misma política.
    """

    items_esperados: int
    items_sin_dato: int
    bloqueantes_no_aptos: int


def dias_de_plazo(criticidad: str) -> int:
    """Días desde que nace el hallazgo hasta su fecha límite.

    `bloqueante` = 0 días: mismo día. No es "urgente", es hoy.
    """
    if criticidad not in DIAS_DE_PLAZO:
        raise ValueError(
            f'criticidad desconocida: {criticidad!r}. '
            f'Las válidas son {sorted(DIAS_DE_PLAZO)}.'
        )
    return DIAS_DE_PLAZO[criticidad]


def dia_limite(dia_reporte: date, criticidad: str) -> date:
    """El ÚLTIMO día en que el hallazgo todavía está a tiempo.

    QUÉ AFIRMA: que un hallazgo cerrado en cualquier momento de este día
    cumplió el plazo.

    QUÉ NO AFIRMA: el instante exacto en que vence. Eso depende de la zona
    horaria y de cómo la base guarda el tiempo, y son decisiones del adaptador
    — acá no hay UTC ni Bogotá, solo días.

    **Un día, no un instante.** Guardar `reportado_ts + 0 días` para un
    bloqueante lo dejaría vencido un segundo después de nacer: todos los
    bloqueantes saldrían siempre en rojo y el rojo dejaría de significar algo,
    que es la lección de los 639 avisos conocidos. «Mismo día» quiere decir
    *hasta que termine el día*, que es lo que entiende quien lo tiene que
    arreglar.

    La conversión a instante es del adaptador y es siempre la misma: la
    medianoche de Bogotá del día SIGUIENTE al que devuelve esta función.
    """
    return dia_reporte + timedelta(days=dias_de_plazo(criticidad))


def ordenar_para_el_dia(items: Sequence, dia: date) -> List:
    """Bloqueantes primero en orden fijo; el resto, barajado por día.

    QUÉ AFIRMA: que el orden es el que hay que mostrar hoy, y que es el mismo
    para todos los que inspeccionen hoy.

    QUÉ NO AFIRMA: nada sobre qué ítems tocan hoy. El filtro por periodicidad
    —diaria contra semanal— es otra decisión y va aparte.

    **Los bloqueantes van fijos** porque se citan por número y el orden es
    memoria útil: el freno siempre es el 1.

    **Los demás van barajados** porque con orden fijo, a la tercera semana el
    pulgar responde sin leer. Es la regla 11: la pregunta no es si el ítem está
    bien redactado, es cómo lo maximiza quien no quiere hacer el trabajo.

    La baraja se siembra con la fecha, así que es **reproducible**: dos
    conductores el mismo día ven el mismo orden, y una inspección de hace tres
    meses se puede auditar reconstruyendo lo que tenía en pantalla. Un
    `random.shuffle()` sin semilla haría eso imposible.
    """
    bloqueantes = sorted(
        (i for i in items if i.criticidad == 'bloqueante'),
        key=lambda i: i.orden,
    )
    resto = [i for i in items if i.criticidad != 'bloqueante']

    # Orden derivado del hash de (fecha, ítem): determinista, sin estado global,
    # y sin depender de la implementación de `random` de la versión de turno.
    def _clave(item):
        semilla = f'{dia.isoformat()}|{item.orden}|{item.nombre}'.encode()
        return hashlib.sha256(semilla).hexdigest()

    return bloqueantes + sorted(resto, key=_clave)


def items_del_dia(items: Sequence, dia: date) -> List:
    """Los ítems que corresponden hoy, ya ordenados.

    Los semanales entran solo los lunes. No es un detalle de calendario: un
    ítem semanal preguntado a diario se vuelve ruido, y el ruido entrena a
    marcar sin leer — el mismo daño que quiere evitar el barajado.
    """
    ES_LUNES = 0
    aplican = [
        i for i in items
        if i.periodicidad == 'diaria' or dia.weekday() == ES_LUNES
    ]
    return ordenar_para_el_dia(aplican, dia)


def conteos(items: Sequence[ItemRespondido]) -> Conteos:
    """Los tres números del resumen. Un solo recorrido, una sola política."""
    return Conteos(
        items_esperados=len(items),
        items_sin_dato=sum(1 for i in items if i.respuesta == SIN_DATO),
        bloqueantes_no_aptos=sum(
            1 for i in items
            if i.criticidad == 'bloqueante' and i.respuesta == NO_APTO),
    )


def veredicto(items: Sequence[ItemRespondido]) -> str:
    """`apto | no_apto | incompleta` sobre TODOS los ítems que tocaban hoy.

    QUÉ AFIRMA: si esta inspección habilita el despacho, y por cuál de las dos
    razones no lo hace cuando no lo hace.

    QUÉ NO AFIRMA: que el vehículo esté bien. Un `apto` dice que los
    bloqueantes se miraron y ninguno falló — puede haber un `mayor` en
    `no_apto` con sus siete días corriendo, y el camión sale igual. Eso no es
    una laguna: es el criterio del catálogo, donde «bloqueante» significa
    exactamente «hoy no sale».

    **El orden de precedencia es una decisión, no un accidente:**

    1. Un bloqueante en `no_apto` gana sobre todo lo demás. Saber que el freno
       falla es más informativo que saber que faltó contestar el radio, y el
       que va a leer esto mañana necesita el dato más fuerte, no el más
       reciente.
    2. Cualquier `sin_dato` —bloqueante o no— hace `incompleta`. La lectura
       estricta de la regla 1 sería mirar solo los bloqueantes, y es la lectura
       que abre el camino barato de la regla 11: contestar los nueve
       bloqueantes en veinte segundos, dejar los diecinueve restantes en blanco
       y llevarse un `apto`. Una inspección con diecinueve huecos no afirma que
       el vehículo esté bien: afirma que no se sabe.
    3. Todo contestado y ningún bloqueante caído: `apto`.

    Levanta con lista vacía. Una inspección de cero ítems no es `apto`: es una
    pregunta mal hecha, y responderla con la palabra que autoriza el despacho
    sería el peor default posible.
    """
    if not items:
        raise ValueError(
            'una inspección sin ítems no tiene veredicto: cero ítems mirados no '
            'es "todo bien", es que no se miró nada.'
        )
    desconocidas = sorted({i.respuesta for i in items} - set(RESPUESTAS))
    if desconocidas:
        raise ValueError(
            f'respuesta desconocida: {desconocidas}. Las válidas son '
            f'{sorted(RESPUESTAS)}.'
        )
    c = conteos(items)
    if c.bloqueantes_no_aptos > 0:
        return NO_APTO
    if c.items_sin_dato > 0:
        return INCOMPLETA
    return APTO


def habilita_despacho(veredicto_: str) -> bool:
    """Si con esta inspección el vehículo puede salir a ruta.

    QUÉ AFIRMA: que los ítems bloqueantes se miraron todos y ninguno falló.

    QUÉ NO AFIRMA: que alguien vaya a impedir la salida. Hoy esto se publica y
    no bloquea — la secuencia del módulo es medir → corregir → imponer, y si el
    primer día la app deja un camión en patio por un formulario a medias, la
    operación desmonta el sistema en 48 horas.

    Escrito como función y no como `== 'apto'` repartido por la frontera y la
    pantalla: la pregunta «¿puede salir?» se contesta en un solo lugar, y el día
    que `incompleta` deje de bloquear —o que empiece a bloquear de verdad— hay
    una sola línea que cambiar.
    """
    if veredicto_ not in VEREDICTOS:
        raise ValueError(
            f'veredicto desconocido: {veredicto_!r}. Los válidos son '
            f'{sorted(VEREDICTOS)}.'
        )
    return veredicto_ == APTO


# ── Qué plantilla le toca a un vehículo ──────────────────────────────────────
#
# Las claves son valores reales de `Vehiculo.tipo`, que es TEXTO LIBRE, igual
# que en `POSICIONES_LLANTA_POR_TIPO` — y se normalizan con la misma función,
# porque son el mismo texto libre leído dos veces.
#
# Los valores son el vocabulario `aplica_a` de `flota_plantilla_inspeccion`.
PLANTILLA_POR_TIPO = {
    # Seis posiciones de llanta y furgón: les toca el bloqueante 8 («puertas del
    # furgón aseguran»), que el furgón liviano no tiene.
    'nhr': 'camion', 'turbo': 'camion', 'camion': 'camion', 'sencillo': 'camion',
    # Cuatro posiciones y sin puertas de furgón que asegurar.
    'van': 'furgon_liviano', 'furgon': 'furgon_liviano',
    'furgon liviano': 'furgon_liviano', 'camioneta': 'furgon_liviano',
    'motocarro': 'motocarro',
}

#: Por qué un tipo conocido NO tiene plantilla. **Total sobre el complemento**
#: de `PLANTILLA_POR_TIPO` respecto de `POSICIONES_LLANTA_POR_TIPO`, y hay un
#: test que lo obliga: es el mismo trato que `MOTIVO_ORIGEN_NO_SUELTO`.
#:
#: Existe para que la respuesta a «este vehículo no puede inspeccionarse» sea
#: una frase escrita y no un `KeyError`. Un tipo nuevo en el desplegable obliga
#: a decidir de qué lado cae, en vez de caer al lado cómodo sin que nadie mire.
MOTIVO_TIPO_SIN_PLANTILLA = {
    'moto': 'Una moto no tiene furgón, ni puertas, ni cabina: la mitad del '
            'checklist de camión le quedaría en blanco, y un formulario lleno '
            'de casillas inaplicables entrena a marcar todo sin leer (regla '
            '11). Necesita catálogo propio, no el de otro con huecos.',
}


def plantilla_de_tipo(tipo_vehiculo: str) -> str:
    """El `aplica_a` de la plantilla que le corresponde a este tipo.

    QUÉ AFIRMA: que existe una decisión escrita sobre qué se le pregunta a un
    vehículo de este tipo.

    QUÉ NO AFIRMA: que esa plantilla esté sembrada. `motocarro` tiene decisión
    y no tiene filas —es tanda 3— y el adaptador distingue los dos fallos: «no
    sé qué preguntarle» y «sé qué preguntarle y el catálogo no está».

    **Levanta en vez de caer a un default** (regla 5). El default cómodo sería
    `camion`, que es el catálogo más largo, y suena conservador: preguntar de
    más. No lo es. Un motocarro contra `camion_v1` recibe nueve ítems que no
    puede tener —puertas del furgón, drenaje del separador de agua— y a la
    tercera mañana el pulgar ya aprendió a marcarlos óptimos sin leer. Ese
    reflejo no se queda en los ítems inaplicables: se lleva puestos también los
    nueve bloqueantes reales. `docs/flota/ESTADO.md` lo dice literal sobre el
    motocarro de la tanda 3.
    """
    clave = normalizar_tipo(tipo_vehiculo)
    if clave in PLANTILLA_POR_TIPO:
        return PLANTILLA_POR_TIPO[clave]
    if clave in MOTIVO_TIPO_SIN_PLANTILLA:
        raise ValueError(
            f'el tipo {tipo_vehiculo!r} no tiene plantilla de inspección: '
            f'{MOTIVO_TIPO_SIN_PLANTILLA[clave]}'
        )
    raise ValueError(
        f'tipo de vehículo desconocido: {tipo_vehiculo!r}. Los que tienen '
        f'plantilla declarada son {sorted(PLANTILLA_POR_TIPO)}. Agregalo a '
        f'PLANTILLA_POR_TIPO con el catálogo que le corresponde, o a '
        f'MOTIVO_TIPO_SIN_PLANTILLA con el motivo por el que no lleva.'
    )


__all__ = ['DIAS_DE_PLAZO', 'dias_de_plazo', 'dia_limite',
           'ordenar_para_el_dia', 'items_del_dia',
           'OPTIMO', 'NO_APTO', 'SIN_DATO', 'RESPUESTAS',
           'APTO', 'INCOMPLETA', 'VEREDICTOS',
           'ItemRespondido', 'Conteos', 'conteos', 'veredicto',
           'habilita_despacho', 'PLANTILLA_POR_TIPO',
           'MOTIVO_TIPO_SIN_PLANTILLA', 'plantilla_de_tipo']
