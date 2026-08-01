"""
Políticas del odómetro. Invariantes 1 (monotonía) y 7 (borde degenerado).

La base impone lo mismo por trigger y por CHECK
(`flota/adaptadores/modelos.py`). Está dicho dos veces a propósito: acá para dar
un error legible a quien lo escribe, allá para que no exista camino que lo
esquive. Lo que no se vale es que digan cosas distintas — por eso las dos mitades
se prueban contra los mismos casos.
"""
from typing import Sequence, Union

from flota.dominio.errores import LecturaRechazada
from flota.dominio.valores import SIN_DATO, Lectura, OrigenLectura


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
    if nueva.origen == OrigenLectura.CORRECCION:
        motivo = (nueva.motivo_correccion or '').strip()
        if not motivo:
            raise LecturaRechazada(
                'una corrección exige motivo escrito: sin él es indistinguible '
                'de un error de digitación'
            )
        if not nueva.autor_usuario_id:
            raise LecturaRechazada('una corrección exige autor: el punto es dejar rastro')
        return

    if not previas:
        return

    # El tope es el MÁXIMO histórico, no la última lectura: si ya se registró
    # 100.450, una lectura de 100.000 decrece aunque la anterior fuera menor.
    tope = max(l.valor_km for l in previas)
    if nueva.valor_km < tope:
        raise LecturaRechazada(
            f'el odómetro no puede decrecer: {nueva.valor_km} < {tope}. '
            f'Si el valor es correcto, se registra con origen=correccion y motivo.'
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
    if not lecturas:
        return SIN_DATO
    # La más reciente por marca de tiempo, no la de mayor kilometraje: una
    # corrección posterior debe ganarle a la lectura que corrige.
    return max(lecturas, key=lambda l: l.ts).valor_km


__all__ = ['validar_lectura', 'odometro_actual', 'SIN_DATO']
