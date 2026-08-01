"""
Políticas del odómetro. Invariantes 1 (monotonía) y 7 (borde degenerado).

Sin implementar. Las firmas y las definiciones existen para que los tests de
propiedad fallen por AUSENCIA DE IMPLEMENTACIÓN y no por error de importación.
"""
from typing import Sequence, Union

from flota.dominio.valores import SIN_DATO, Lectura


def validar_lectura(previas: Sequence[Lectura], nueva: Lectura) -> None:
    """Acepta o rechaza una lectura nueva contra el historial del vehículo.

    QUÉ AFIRMA: que `nueva` puede persistirse sin romper la monotonía del
    odómetro de ese vehículo.

    QUÉ NO AFIRMA: nada sobre si el valor es *cierto*. La verificación contra la
    foto del tablero es otra cosa y vive en otra parte. Esto solo dice que la
    serie sigue siendo una serie.

    Regla: el odómetro nunca decrece. La única excepción es una lectura con
    `origen = correccion`, que además exige motivo escrito y autor — porque una
    corrección sin motivo es indistinguible de un error de digitación, y el
    punto de permitirla es dejar rastro de quién decidió y por qué.

    Levanta `LecturaRechazada` si no se puede aceptar. No devuelve un valor
    corregido: en la frontera no se degrada hacia el éxito (regla 5).
    """
    raise NotImplementedError(
        'flota.dominio.odometro.validar_lectura — invariante 1 (monotonía) sin implementar'
    )


def odometro_actual(lecturas: Sequence[Lectura]) -> Union[int, str]:
    """Kilometraje vigente de un vehículo, o SIN_DATO si nunca se leyó.

    QUÉ AFIRMA: el último kilometraje conocido, con su procedencia implícita en
    la lectura de la que sale.

    QUÉ NO AFIRMA: que el vehículo tenga ese recorrido hoy. Afirma lo último que
    alguien registró.

    Un vehículo sin lecturas devuelve SIN_DATO, jamás 0. Son estados distintos:
    0 km es "no ha rodado", SIN_DATO es "no sabemos". Devolver 0 aquí convierte
    todo CPK y todo preventivo por kilometraje aguas abajo en un número
    inventado con cara de medición.
    """
    raise NotImplementedError(
        'flota.dominio.odometro.odometro_actual — invariante 7 (borde degenerado) sin implementar'
    )


__all__ = ['validar_lectura', 'odometro_actual', 'SIN_DATO']
