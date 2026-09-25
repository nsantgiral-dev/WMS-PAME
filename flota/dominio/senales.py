"""
La bandeja del encargado: el semáforo de cada vehículo y las señales de fuga.

Puro: no sabe que existe SQLAlchemy ni Flask. El adaptador
(`flota/adaptadores/bandeja.py`) le trae hechos ya leídos y acá se decide.

## Una señal PROPONE, no culpa (regla 2 del módulo)

Ninguna función de este archivo recibe a una persona. Juzgan un tramo de
odómetro, una ruta, un tanqueo, una custodia. El adaptador agrega después, como
**contexto**, a nombre de quién estaba el turno — y la pantalla lo dice como
contexto («el turno estaba a nombre de…»), nunca como veredicto. El encargado
decide si hay algo; el sistema solo dice dónde mirar.

## Tres estados, no dos (regla 4)

Cada juicio devuelve `Veredicto` con `estado` en:

    'senal'         hay algo para mirar, con su evidencia
    'normal'        se pudo mirar y no hay nada
    'no_evaluable'  NO se pudo mirar, y `motivo` dice por qué

`no_evaluable` no es `normal`. Un detector que, sin dato, contesta «nada raro»
se apaga sin que nadie lo note — la misma lección que `excede_capacidad`
devolviendo `SIN_DATO` y no `False`. La bandeja publica los no evaluables en su
propia lista, con el motivo, para que se vea cuánto del parque está a oscuras.

## Los umbrales son pocos, con nombre, y se publican (regla 13)

Cada constante de abajo es un número elegido **sin una sola medición** de esta
flota, porque la medición no existe todavía. Por eso: van con nombre propio, en
un solo lugar, con su motivo, y la bandeja los devuelve en `umbrales` para que
quien lea una señal sepa contra qué vara se juzgó. Cuando haya un mes de
bandeja en producción se fijan con dato.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Dict, Optional, Sequence, Union

from flota.dominio.valores import SIN_DATO, Confianza

# ── Umbrales declarados ─────────────────────────────────────────────────────

#: Cuántos días hacia atrás mira una señal. Lo más viejo ya no se investiga:
#: nadie recuerda qué pasó hace dos meses, y una lista que acumula todo el
#: historial se deja de leer.
VENTANA_SENALES_DIAS = 30

#: Qué es «reciente» para un cierre forzado o un turno sin fotos de inicio en
#: la lista de pendientes. Una semana: lo que el encargado todavía puede
#: preguntar en persona.
VENTANA_RECIENTE_DIAS = 7

#: Kilómetros que un vehículo puede moverse en días sin ruta sin que valga la
#: pena mirarlo: ir a tanquear, al lavadero, al taller de la esquina. **Umbral
#: a ojo**, publicado en la respuesta. Una ida al taller de otra ciudad lo pasa
#: y está bien que lo pase: la señal pregunta, no acusa.
KM_TOLERANCIA_SIN_RUTA = 10

#: Cuántos recorridos medidos de una misma ruta maestra hacen falta para que su
#: mediana signifique algo. Con menos, «el doble de lo normal» es el doble de
#: un viaje.
MIN_RECORRIDOS_RUTA = 5

#: Cuántas veces la mediana de su ruta tiene que superar un recorrido para
#: marcarlo. 1,5: un desvío de media ruta, no un trancón.
FACTOR_KM_RUTA = Decimal('1.5')

#: Cuánto por encima de lo esperado pueden estar los galones de una ventana de
#: tanque lleno a tanque lleno. 25 %: el rendimiento de un camión cargado y uno
#: vacío difiere menos que eso, y un llenado «hasta el pico» contra uno «hasta
#: que salta» también.
TOLERANCIA_GALONES = Decimal('1.25')

#: Tanqueos de la flota necesarios para que la mediana del precio del galón
#: signifique algo, y en qué ventana se miran.
MIN_TANQUEOS_PRECIO = 5
VENTANA_PRECIO_DIAS = 90

#: Cuánto por encima de la mediana de la flota puede estar un galón. 15 %: el
#: precio regulado del ACPM en Colombia no varía tanto entre estaciones de la
#: misma región en tres meses.
FACTOR_PRECIO = Decimal('1.15')


def umbrales() -> Dict[str, str]:
    """Las varas, como texto, para que viajen con la respuesta."""
    return {
        'ventana_senales_dias': str(VENTANA_SENALES_DIAS),
        'ventana_reciente_dias': str(VENTANA_RECIENTE_DIAS),
        'km_tolerancia_sin_ruta': str(KM_TOLERANCIA_SIN_RUTA),
        'min_recorridos_ruta': str(MIN_RECORRIDOS_RUTA),
        'factor_km_ruta': str(FACTOR_KM_RUTA),
        'tolerancia_galones': str(TOLERANCIA_GALONES),
        'min_tanqueos_precio': str(MIN_TANQUEOS_PRECIO),
        'ventana_precio_dias': str(VENTANA_PRECIO_DIAS),
        'factor_precio': str(FACTOR_PRECIO),
    }


# ── El veredicto ────────────────────────────────────────────────────────────

SENAL = 'senal'
NORMAL = 'normal'
NO_EVALUABLE = 'no_evaluable'


@dataclass(frozen=True)
class Veredicto:
    estado: str
    motivo: Optional[str] = None
    datos: Dict[str, object] = field(default_factory=dict)


def mediana(valores: Sequence) -> Union[Decimal, str]:
    """Mediana de una lista. Vacía → `SIN_DATO`, no cero."""
    if not valores:
        return SIN_DATO
    orden = sorted(Decimal(v) for v in valores)
    n = len(orden)
    medio = n // 2
    if n % 2:
        return orden[medio]
    return (orden[medio - 1] + orden[medio]) / 2


def _dudoso(marca) -> bool:
    """Un tramo con un extremo dudoso, o con los dos, no se juzga."""
    return marca is SIN_DATO or marca == Confianza.DUDOSA


# ── Fuga 4: kilómetros en días sin ruta ─────────────────────────────────────

def km_sin_ruta(*, km: int, dias: Sequence[date], dias_con_ruta: set,
                marca) -> Veredicto:
    """¿El vehículo se movió en días en que no tenía ninguna ruta?

    `km` es el tramo entre dos lecturas consecutivas vigentes; `dias` son TODOS
    los días operativos que ese tramo abarca, de la primera lectura a la
    segunda inclusive. Basta con que **uno** de esos días tenga ruta para no
    marcar: el tramo pudo recorrerse ese día, y la señal no puede partir
    kilómetros entre días que no sabe separar (regla 0: ante la duda, no se
    acusa).

    Con un extremo dudoso no se juzga: primero se verifica el kilometraje —eso
    ya es un pendiente propio—, y un salto mal tecleado no es un paseo.
    """
    base = km_sin_explicar(km=km, marca=marca, tolerancia=KM_TOLERANCIA_SIN_RUTA)
    if base.estado != SENAL:
        return base
    if any(d in dias_con_ruta for d in dias):
        return Veredicto(NORMAL)
    return Veredicto(SENAL, datos={'km': km, 'dias': len(dias)})


#: El motivo de un tramo que no se juzga. Uno solo, para la bandeja y para la
#: jornada: el encargado lo lee en las dos y tiene que reconocerlo.
MOTIVO_TRAMO_EN_DUDA = ('uno de los kilometrajes del tramo está en duda: '
                        'verificalo primero contra su foto')


def km_sin_explicar(*, km: int, marca, tolerancia: int) -> Veredicto:
    """El núcleo de «kilómetros que nadie explica». UNA función para las dos
    preguntas que lo hacen:

        bandeja   ¿se movió en días sin ruta?            (`km_sin_ruta`)
        jornada   ¿sumó km mientras la sede tenía el turno? (`km_entre_turnos`)

    Hasta el 2026-09-24 la jornada restaba `km_inicio − km_fin` de dos turnos
    **sin mirar la confianza de las lecturas**: un número mal tecleado, sin
    foto, salía como «120 km que no le tocan a ningún turno» con el nombre de
    dos conductores al lado. La bandeja ya no lo juzgaba. Ahora ninguna.

    `marca` es `odometro.confianza_del_tramo(a, b)` (o `SIN_DATO` si falta una
    de las dos lecturas). Con un extremo dudoso no se juzga: primero se
    verifica el kilometraje —eso ya es un pendiente propio—, y un salto mal
    tecleado no es un paseo. `km <= tolerancia` es normal.
    """
    if _dudoso(marca):
        return Veredicto(NO_EVALUABLE, MOTIVO_TRAMO_EN_DUDA)
    if km <= tolerancia:
        return Veredicto(NORMAL)
    return Veredicto(SENAL, datos={'km': km})


# ── Km de una ruta contra la mediana de su ruta maestra ─────────────────────

def km_de_ruta(*, km: int, historico: Sequence[int]) -> Veredicto:
    """¿Este recorrido fue mucho más largo que lo habitual para su ruta?

    `historico` son los kilómetros de los OTROS recorridos medidos de la misma
    ruta maestra —sin este—, de cualquier vehículo: un motocarro y un NHR hacen
    los mismos kilómetros en la misma ruta, aunque no gasten lo mismo.

    Mediana y no promedio: un solo viaje con desvío mueve el promedio lo
    suficiente para esconder el siguiente.
    """
    n = len(historico)
    if n < MIN_RECORRIDOS_RUTA:
        return Veredicto(NO_EVALUABLE,
                         f'{n} recorrido(s) medido(s) de esta ruta; hacen '
                         f'falta {MIN_RECORRIDOS_RUTA} para saber qué es lo '
                         f'normal', {'n': n})
    med = mediana(historico)
    if Decimal(km) > med * FACTOR_KM_RUTA:
        return Veredicto(SENAL, datos={'km': km, 'mediana': med, 'n': n})
    return Veredicto(NORMAL, datos={'mediana': med, 'n': n})


# ── Fuga 5: combustible ─────────────────────────────────────────────────────

def galones_de_ventana(*, km: int, galones: Decimal, km_galon,
                       publicable: bool, motivo_rendimiento: Optional[str]
                       ) -> Veredicto:
    """¿Entraron más galones de los que ese recorrido debió gastar?

    `km` y `galones` son los de UNA ventana de tanque lleno a tanque lleno —la
    única forma de saber lo que se gastó—. Lo esperado es `km / km_galon` con
    el rendimiento medido **del mismo vehículo**.

    Sin rendimiento sostenible no hay señal: `publicable` lo decide
    `costos.rendimiento_publicable` (≥6 ventanas y ≥60 días), la misma función
    que decide qué ve el conductor. Un rendimiento provisional de dos ventanas
    haría disparar la mitad de los tanqueos sanos.

    El rendimiento de referencia incluye a la propia ventana. Con seis o más
    ventanas el sesgo es menor que la tolerancia; se declara y no se corrige.
    """
    if km_galon is SIN_DATO or not publicable:
        return Veredicto(NO_EVALUABLE,
                         'el rendimiento del vehículo todavía no se puede '
                         'sostener: ' + (motivo_rendimiento or 'sin motivo'))
    esperados = Decimal(km) / Decimal(km_galon)
    if Decimal(galones) > esperados * TOLERANCIA_GALONES:
        return Veredicto(SENAL, datos={'esperados': esperados,
                                       'exceso': Decimal(galones) - esperados})
    return Veredicto(NORMAL, datos={'esperados': esperados})


def precio_de_galon(*, precio, historico: Sequence[Decimal]) -> Veredicto:
    """¿El galón de este tanqueo costó mucho más que en el resto de la flota?

    `historico` son los precios por galón de los OTROS tanqueos de la flota en
    la ventana. Un precio `SIN_DATO` (cero galones) no se juzga: es una fila mal
    cargada, no combustible caro.
    """
    if precio is SIN_DATO:
        return Veredicto(NO_EVALUABLE, 'tanqueo sin galones: no hay precio')
    n = len(historico)
    if n < MIN_TANQUEOS_PRECIO:
        return Veredicto(NO_EVALUABLE,
                         f'{n} tanqueo(s) de la flota para comparar; hacen '
                         f'falta {MIN_TANQUEOS_PRECIO}', {'n': n})
    med = mediana(historico)
    if Decimal(precio) > med * FACTOR_PRECIO:
        return Veredicto(SENAL, datos={'mediana': med, 'n': n})
    return Veredicto(NORMAL, datos={'mediana': med, 'n': n})


# ── Fuga 12: la ruta de hoy no es de quien tiene el turno ───────────────────

#: Estados de ruta en los que el vehículo YA salió. Antes de salir (programada,
#: cargando) que el turno siga en la sede es lo normal: el traspaso se hace al
#: salir.
ESTADOS_RUTA_EN_LA_CALLE = ('EN_TRANSITO', 'ENTREGADA')

#: De esos, los que ya VOLVIERON. Para ellos el turno que se compara no es el
#: de ahora —al final de un día normal el camión está en la sede— sino el que
#: estaba vigente cuando la ruta se cerró: lo elige el adaptador (QA e2e
#: 2026-09-24: cada noche normal salía «ya salió y sigue en la sede»).
ESTADOS_RUTA_TERMINADA = ('ENTREGADA',)


def turno_de_la_ruta(*, conductor_ruta_id: Optional[int], estado_ruta: str,
                     custodia_tipo: Optional[str],
                     custodio_conductor_id: Optional[int],
                     otro_vehiculo_del_conductor: Optional[int]) -> Veredicto:
    """¿La ruta de hoy la hace alguien distinto de quien tiene el vehículo?

    Es el préstamo sin traspaso: el camión está a nombre de uno y lo maneja
    otro. Si pasa algo, el registro apunta a quien no estaba.

    Tres formas, juzgadas en este orden:

    1. el turno del vehículo está a nombre de OTRO conductor;
    2. el vehículo ya salió (en la calle o entregada) y su turno sigue en una
       sede, o no hay turno abierto;
    3. el conductor de la ruta tiene abierto el turno de OTRO vehículo.

    Sin conductor en la ruta no hay contra qué comparar.

    El «turno» que recibe es el del vehículo MIENTRAS la ruta estuvo en la
    calle: el vigente para una ruta en tránsito, el vigente al cierre para una
    ya entregada (`ESTADOS_RUTA_TERMINADA`). Compararla contra el turno de
    ahora convierte cada fin de día normal en señal.
    """
    if conductor_ruta_id is None:
        return Veredicto(NO_EVALUABLE, 'la ruta no tiene conductor')
    if custodia_tipo == 'conductor' and custodio_conductor_id is not None \
            and custodio_conductor_id != conductor_ruta_id:
        return Veredicto(SENAL, datos={'forma': 'turno_de_otro'})
    if estado_ruta in ESTADOS_RUTA_EN_LA_CALLE and custodia_tipo != 'conductor':
        return Veredicto(SENAL, datos={'forma': 'salio_sin_turno'})
    if otro_vehiculo_del_conductor is not None:
        return Veredicto(SENAL, datos={'forma': 'conductor_con_otro_vehiculo'})
    return Veredicto(NORMAL)


# ── El semáforo ─────────────────────────────────────────────────────────────
#
# Se mudó a `flota/dominio/salida.py` el 2026-09-24: «¿puede salir este
# camión?» la contestaban tres pantallas con tres políticas (esta, el despacho
# y el teléfono del conductor). Ahora hay una, y este módulo se queda con las
# señales de fuga.


__all__ = [
    'Veredicto', 'SENAL', 'NORMAL', 'NO_EVALUABLE', 'mediana', 'umbrales',
    'km_sin_ruta', 'km_de_ruta', 'galones_de_ventana', 'precio_de_galon',
    'turno_de_la_ruta', 'km_sin_explicar', 'MOTIVO_TRAMO_EN_DUDA',
    'VENTANA_SENALES_DIAS', 'VENTANA_RECIENTE_DIAS',
    'VENTANA_PRECIO_DIAS', 'ESTADOS_RUTA_EN_LA_CALLE', 'ESTADOS_RUTA_TERMINADA',
]
