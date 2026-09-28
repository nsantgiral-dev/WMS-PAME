"""
La cola sin señal del conductor — qué día fue lo que llega tarde.

El conductor inspecciona el lunes a las 5:30 en un patio sin señal y el
teléfono lo manda el martes. Hasta el 2026-09-27 el servidor juzgaba esa
inspección contra la lista **del martes** (el día en que llegó): los ítems
semanales, que solo entran los lunes, daban 409 «no es de los que tocaban
hoy», y la inspección —con sus respuestas— se perdía.

La regla: **lo que llega por la cola se juzga con el día en que se hizo**, que
es el que el teléfono anotó al encolar (`ts_dispositivo`). Pero al reloj del
teléfono no se le cree a ciegas (regla 5 del módulo): uno adelantado o uno que
dice «hace tres semanas» no decide nada, y se vuelve al día del servidor —
declarado.

Puro: sin base, sin Flask. El día de Bogotá lo pone quien llama
(`app.utils.fecha.dia_operativo_de`, la única implementación de la regla 5 del
WMS): acá se recibe como función, no se reescribe.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Optional

#: Cuánto puede ir adelantado el reloj del teléfono respecto del servidor antes
#: de dejar de creerle. Diez minutos cubren un reloj sin sincronizar de un
#: teléfono barato; más que eso es un reloj corrido, y el día que dice no vale.
TOLERANCIA_RELOJ_ADELANTADO = timedelta(minutes=10)

#: Cuántos días atrás se acepta que una operación estuvo guardada en el
#: teléfono. Una semana sin señal no existe en la operación de hoy; un
#: `ts_dispositivo` más viejo que eso es más probablemente un reloj mal puesto
#: que un teléfono que estuvo siete días en el fondo de un cajón.
MAX_DIAS_EN_EL_TELEFONO = 7


@dataclass(frozen=True)
class DiaDeLaOperacion:
    """El día con que se juzga una operación, y de dónde salió.

    `fuente` ∈ `telefono` · `servidor`. `motivo` dice por qué, siempre: un día
    elegido sin motivo no se puede auditar.
    """

    dia: date
    fuente: str
    motivo: str


def dia_de_la_operacion(ts_dispositivo: Optional[datetime], ahora: datetime,
                        dia_de: Callable[[datetime], date]) -> DiaDeLaOperacion:
    """El día en que se HIZO lo que llega por la cola.

    QUÉ AFIRMA: que el día devuelto es el del teléfono cuando su reloj es
    creíble —no va adelantado más de `TOLERANCIA_RELOJ_ADELANTADO` y no dice
    más de `MAX_DIAS_EN_EL_TELEFONO` días atrás—, y el del servidor en cualquier
    otro caso, con el motivo escrito.

    QUÉ NO AFIRMA: que el teléfono tenga la hora bien. Afirma que no hay nada
    evidente que la contradiga, que es lo mismo que afirma `declarada` en una
    lectura de odómetro.

    `ts_dispositivo` y `ahora` en UTC ingenuo, como todo timestamp técnico;
    `dia_de` convierte un instante UTC en el día de Bogotá.
    """
    hoy = dia_de(ahora)
    if ts_dispositivo is None:
        return DiaDeLaOperacion(hoy, 'servidor',
                                'no llegó la hora del teléfono: se usa la del servidor')
    if ts_dispositivo > ahora + TOLERANCIA_RELOJ_ADELANTADO:
        return DiaDeLaOperacion(
            hoy, 'servidor',
            'el reloj del teléfono va adelantado respecto del servidor: su día '
            'no se usa')
    if ahora - ts_dispositivo > timedelta(days=MAX_DIAS_EN_EL_TELEFONO):
        return DiaDeLaOperacion(
            hoy, 'servidor',
            f'el teléfono dice que se hizo hace más de {MAX_DIAS_EN_EL_TELEFONO} '
            f'días: más probable un reloj mal puesto que una semana sin señal')
    dia = dia_de(ts_dispositivo)
    if dia == hoy:
        return DiaDeLaOperacion(dia, 'telefono', 'se hizo hoy')
    return DiaDeLaOperacion(
        dia, 'telefono',
        f'se hizo el {dia.isoformat()} sin señal y llegó el {hoy.isoformat()}: '
        f'se juzga con el día en que se hizo')


__all__ = ['DiaDeLaOperacion', 'dia_de_la_operacion',
           'TOLERANCIA_RELOJ_ADELANTADO', 'MAX_DIAS_EN_EL_TELEFONO']
