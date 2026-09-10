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


def juicio_de(h: Hallazgo, ahora: datetime) -> dict:
    """Todo lo que el canon puede decir de UN hallazgo, de una sola pasada.

    Devuelve `entra`, y si entra, los dos números que el canon **prohíbe
    mezclar**: `dias` (duración cerrada, `dias_hallazgo_abierto`) y `dias_lleva`
    (antigüedad viva, `dias_transcurridos`). Uno mide riesgo terminado y el otro
    riesgo corriendo; sumarlos o promediarlos juntos es exactamente lo que el
    punto 6 del canon prohíbe.

    Un hallazgo que NO entra al indicador devuelve `entra: False` y ningún
    número — no `0`, no `None` en un campo numérico. Pedirle días a uno de línea
    base es una pregunta mal hecha, y `dias_hallazgo_abierto` levanta por eso.
    """
    if not entra_al_indicador(h):
        return {
            'entra': False,
            'criticidad': h.criticidad,
            'estado': h.estado,
            # El motivo de la exclusión, no solo el hecho. «No entra» sin decir
            # por qué se lee como un bug del tablero.
            'motivo_fuera': ('es de línea base: la primera inspección levanta lo '
                             'que ya había, sin responsable y sin reloj'
                             if h.linea_base else
                             f'está {h.estado}: no se resolvió, se determinó '
                             f'que no había nada que resolver'),
        }
    return {
        'entra': True,
        'criticidad': h.criticidad,
        'estado': h.estado,
        'dias': dias_hallazgo_abierto(h),
        'dias_lleva': dias_transcurridos(h, ahora),
        'vencido': vencido(h, ahora),
        'aplazado_veces': h.aplazado_veces,
    }


def indicador_dias_abierto(hallazgos: Sequence[Hallazgo],
                           ahora: datetime) -> dict:
    """El indicador **despromediado**: primero los casos, después el promedio.

    `docs/procedimientos/roles/especialista-control-flota.md:119` promete «Días
    promedio de hallazgo abierto» como señal de desempeño. El canon existe desde
    el 2026-08-03, `promedio_del_indicador` existe desde el mismo día — y **no
    tenía un solo caller de producción**, declarado como deuda en
    `tests/flota/test_trinquetes_flota.py`. Esta función es ese caller.

    ## Por qué el promedio va último y nunca solo

    Con seis vehículos y un puñado de hallazgos, el promedio es el resumen menos
    informativo que se puede publicar: no dice a qué camión llamar. Y el canon
    ya prohíbe compararlo entre zonas —los tiempos de taller y de repuesto son
    distintos en Neiva, Pitalito y Florencia—, así que un promedio suelto no
    sirve ni para lo único para lo que suele servir un promedio.

    Se publica igual, porque la ficha de rol lo promete, pero **con su `n` al
    lado y detrás de la enumeración**. Un promedio de 2 casos y uno de 200 son
    el mismo número con distinta autoridad.

    ## `n_fuera` es el denominador, y por eso está

    Un indicador que solo reporta lo que mira devuelve «0 días promedio» sobre
    una flota con veinte hallazgos de línea base, y eso se lee como «no hay
    demoras». El que lee tiene que ver cuántos quedaron afuera y por qué —es la
    misma lección de `SIN_CUBRIR` en la auditoría de invariantes: el denominador
    tiene que ser visible.

    `juicios` sale **en el mismo orden** que `hallazgos`, para que el adaptador
    pueda pegarle la placa sin que el dominio tenga que conocerla.
    """
    juicios = [juicio_de(h, ahora) for h in hallazgos]
    entran = [h for h in hallazgos if entra_al_indicador(h)]
    cerrados = [h for h in entran if h.cerrado_ts is not None]
    return {
        'juicios': juicios,
        # El promedio sale de `promedio_del_indicador`, no de un `sum/len`
        # escrito acá: la política de qué entra ya está escrita una vez, y la
        # copia sería la que diverge (regla 0).
        'promedio_dias': promedio_del_indicador(entran),
        'n': len(cerrados),
        'n_abiertos': len(entran) - len(cerrados),
        'n_fuera': len(hallazgos) - len(entran),
        'n_vencidos': sum(1 for h in entran if vencido(h, ahora)),
    }

__all__ = [
    'EstadoHallazgo', 'Hallazgo', 'entra_al_indicador', 'dias_hallazgo_abierto',
    'dias_transcurridos', 'vencido', 'promedio_del_indicador',
    'juicio_de', 'indicador_dias_abierto', 'SIN_DATO',
]
