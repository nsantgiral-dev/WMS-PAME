"""
`dias_hallazgo_abierto` — canon en `docs/flota/canones/dias_hallazgo_abierto.md`.

El canon lo definió Santiago; el cálculo se escribe contra el documento, no al
revés. Cada regla de acá cita la decisión del punto 4 que la obliga.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence, Union

from flota.dominio.valores import SIN_DATO


class EstadoHallazgo:
    """Un hallazgo no pasa de `abierto` a `cerrado` directamente (regla 6):
    se cierra por OT con evidencia, o se `descarta` con motivo escrito."""

    ABIERTO    = 'abierto'
    CERRADO    = 'cerrado'
    DESCARTADO = 'descartado'
    NO_APLICA  = 'no_aplica'


@dataclass(frozen=True)
class Hallazgo:
    """Lo mínimo que el canon necesita para pronunciarse.

    `reportado_ts` no cambia nunca. `fecha_limite` sí —se puede aplazar— y por
    eso son campos distintos: el aplazamiento mueve el plazo, no el origen del
    reloj.
    """

    reportado_ts: datetime
    criticidad: str
    estado: str = EstadoHallazgo.ABIERTO
    cerrado_ts: Optional[datetime] = None
    linea_base: bool = False
    fecha_limite: Optional[datetime] = None
    aplazado_veces: int = 0


def entra_al_indicador(h: Hallazgo) -> bool:
    """Si este hallazgo cuenta para `dias_hallazgo_abierto`.

    QUÉ AFIRMA: que el hallazgo mide tiempo de resolución real.

    QUÉ NO AFIRMA: que no importe. Un descartado y uno de línea base importan —
    se cuentan aparte. Quedan fuera de ESTE indicador, que es distinto de no
    existir.

    Fuera quedan tres casos del punto 6 del canon:

    · **línea base** — la primera inspección levanta lo que ya había. Nace
      preexistente, sin responsable y sin reloj: el desorden viejo no entra.
    · **descartado** — no se resolvió; se determinó que no era un hallazgo.
    · **no_aplica** — el vehículo se dio de baja antes de reparar. Contarlo como
      cerrado inventaría una reparación que no ocurrió.
    """
    if h.linea_base:
        return False
    return h.estado not in (EstadoHallazgo.DESCARTADO, EstadoHallazgo.NO_APLICA)


def dias_hallazgo_abierto(h: Hallazgo) -> Union[int, str]:
    """Días calendario entre el reporte y la resolución física.

    QUÉ AFIRMA: cuánto tiempo estuvo vivo el riesgo.

    QUÉ NO AFIRMA — las cuatro del punto 3 del canon: que el vehículo estuvo
    detenido, que alguien fue negligente, que sirve para comparar zonas, ni
    cuánto costó.

    Arranca en `reportado_ts` y para en `cerrado_ts`, que es cuando el vehículo
    **vuelve reparado** — no cuando se aprueba la OT ni cuando entra al taller.
    Si cerrara al aprobar, un vehículo tres semanas en taller mostraría el
    indicador limpio.

    Días **calendario**: un vehículo con una falla el domingo sigue con la falla
    el domingo.

    Un hallazgo abierto devuelve `SIN_DATO`, **nunca 0 ni un número grande**.
    Cero diría "se resolvió al instante"; un número grande lo mezclaría con los
    cerrados lentos. Para saber cuántos días LLEVA está `dias_transcurridos`,
    que es otro número.

    Levanta si el hallazgo no entra al indicador: pedirle días a un hallazgo de
    línea base es una pregunta mal hecha, y responderla con un número la
    volvería una respuesta mal creída.
    """
    if not entra_al_indicador(h):
        raise ValueError(
            'este hallazgo no entra al indicador (línea base, descartado o '
            'no_aplica): pedirle días es una pregunta mal hecha, y un número '
            'como respuesta se acabaría promediando.'
        )
    if h.cerrado_ts is None:
        # Nunca 0 ni un número grande: está abierto, y lo que existe es
        # cuántos días LLEVA — eso lo da `dias_transcurridos`.
        return SIN_DATO
    # Días calendario: la resta de fechas ya los da, sin excluir fines de
    # semana ni festivos. Un vehículo con una falla el domingo sigue con la
    # falla el domingo.
    return (h.cerrado_ts.date() - h.reportado_ts.date()).days


def dias_transcurridos(h: Hallazgo, ahora: datetime) -> int:
    """Días calendario desde el reporte hasta `ahora`. El reloj corriendo.

    QUÉ AFIRMA: cuánto lleva abierto un hallazgo que sigue abierto.

    QUÉ NO AFIRMA: que sea `dias_hallazgo_abierto`. Son dos números distintos y
    mezclarlos es lo que el canon prohíbe — uno mide duración cerrada, el otro
    antigüedad viva. El aviso de WhatsApp usa este; el indicador usa el otro.
    """
    return (ahora.date() - h.reportado_ts.date()).days


def vencido(h: Hallazgo, ahora: datetime) -> bool:
    """Si pasó su fecha límite sin cerrarse.

    Usa `fecha_limite`, que el aplazamiento SÍ mueve — a diferencia de
    `reportado_ts`, que no se toca. Aplazar cambia cuándo se considera vencido;
    no borra el tiempo transcurrido.
    """
    if h.estado != EstadoHallazgo.ABIERTO or h.fecha_limite is None:
        return False
    return ahora > h.fecha_limite


def promedio_del_indicador(hallazgos: Sequence[Hallazgo]) -> Union[float, str]:
    """Promedio de días sobre los hallazgos que entran y están cerrados.

    QUÉ NO AFIRMA: nada comparable entre zonas. Los tiempos de taller y de
    repuesto son distintos en Neiva, Pitalito y Florencia — un promedio
    comparado entre zonas mide geografía.

    Sin hallazgos cerrados que entren al indicador devuelve `SIN_DATO`, no 0.
    Un promedio de cero elementos no es cero: es que todavía no hay nada que
    promediar, y las dos cosas se leen distinto en un tablero.
    """
    dias = [
        dias_hallazgo_abierto(h) for h in hallazgos
        if entra_al_indicador(h) and h.cerrado_ts is not None
    ]
    if not dias:
        return SIN_DATO
    return sum(dias) / len(dias)


__all__ = [
    'EstadoHallazgo', 'Hallazgo', 'entra_al_indicador', 'dias_hallazgo_abierto',
    'dias_transcurridos', 'vencido', 'promedio_del_indicador', 'SIN_DATO',
]
