"""
Qué vehículo le toca hoy a un conductor. Cascada de resolución.

La pregunta parece trivial y no lo es: el recibo de turno pasa **antes** de la
ruta, a las 5 a.m. en el patio, y el vehículo se asigna por viaje.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class OrigenDelVehiculo(str, Enum):
    """De dónde salió la placa que se le propone al conductor.

    Importa mostrarlo: no es lo mismo "este es tu vehículo" que "creemos que es
    este, confirmá". La segunda necesita que el conductor mire la placa antes de
    seguir, y la primera no.
    """

    CUSTODIA  = 'custodia'    # ya lo tiene: no se pregunta, se muestra
    RUTA      = 'ruta'        # se propone, con confirmación de placa
    ELECCION  = 'eleccion'    # elige de la lista


@dataclass(frozen=True)
class Candidato:
    """Un vehículo que el conductor podría recibir, y si está libre."""

    vehiculo_id: int
    placa: str
    tipo: str
    ocupado_por: Optional[str] = None   # nombre del custodio actual, si tiene


@dataclass(frozen=True)
class Turno:
    origen: OrigenDelVehiculo
    vehiculo_id: Optional[int] = None
    placa: str = ''
    requiere_confirmacion: bool = False
    candidatos: List[Candidato] = field(default_factory=list)


def resolver_vehiculo_del_dia(custodia_activa, vehiculo_de_ruta_hoy,
                              candidatos: List[Candidato]) -> Turno:
    """En cascada, y el orden no es arbitrario.

    1. **¿Tiene custodia activa?** Ese es su vehículo. Se muestra la placa y no
       se pregunta — es el caso normal a partir del segundo día.

    2. **¿Sin custodia pero con ruta programada hoy y vehículo asignado?** Se
       propone ese, **con confirmación explícita de placa**.

    3. **Ninguno de los dos** → elige de la lista.

    **La ruta programada es sugerencia, nunca fuente de verdad.** Si el sistema
    dependiera de que la ruta esté armada antes de las 5 a.m., el día que no lo
    esté el conductor no puede registrar — y se salta el preoperacional. Un
    control que se puede saltar por un problema de logística ajena no es un
    control.

    QUÉ NO AFIRMA: que el vehículo esté libre. Eso lo dice `ocupado_por` de cada
    candidato, y quien decide si se puede recibir es `puede_recibir`.
    """
    if custodia_activa is not None:
        return Turno(
            origen=OrigenDelVehiculo.CUSTODIA,
            vehiculo_id=custodia_activa.vehiculo_id,
            placa=next((c.placa for c in candidatos
                        if c.vehiculo_id == custodia_activa.vehiculo_id), ''),
            requiere_confirmacion=False,
            candidatos=candidatos,
        )

    if vehiculo_de_ruta_hoy is not None:
        return Turno(
            origen=OrigenDelVehiculo.RUTA,
            vehiculo_id=vehiculo_de_ruta_hoy,
            placa=next((c.placa for c in candidatos
                        if c.vehiculo_id == vehiculo_de_ruta_hoy), ''),
            # Se propone, no se asume: la ruta puede estar mal armada y el
            # conductor es el único que está mirando el camión.
            requiere_confirmacion=True,
            candidatos=candidatos,
        )

    return Turno(
        origen=OrigenDelVehiculo.ELECCION,
        requiere_confirmacion=True,
        candidatos=candidatos,
    )


__all__ = ['OrigenDelVehiculo', 'Candidato', 'Turno', 'resolver_vehiculo_del_dia']
