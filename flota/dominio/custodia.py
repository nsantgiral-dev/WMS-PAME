"""
Políticas de custodia. Invariantes 2 (cardinalidad), 3 (cobertura temporal)
y 4 (arco exclusivo).

La base impone 2 y 4 por índice único parcial y CHECK; el 3 solo
parcialmente — ver la nota de `flota/adaptadores/modelos.py`.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

from flota.dominio.errores import CustodiaInvalida
from flota.dominio.valores import Custodia, CustodioEstado, CustodioTipo


@dataclass(frozen=True)
class Hueco:
    """Tramo en el que un vehículo activo no tuvo custodio. No debería existir."""

    desde: datetime
    hasta: datetime


def custodias_activas(custodias: Sequence[Custodia]) -> List[Custodia]:
    """Las custodias sin `fin_ts` de un mismo vehículo.

    QUÉ AFIRMA: cuáles tramos están abiertos ahora mismo según los datos dados.

    QUÉ NO AFIRMA: que sea legal que haya más de una. Eso lo dice
    `validar_cardinalidad`. Esta función cuenta; la otra juzga.
    """
    return [c for c in custodias if c.fin_ts is None]


def validar_cardinalidad(custodias: Sequence[Custodia]) -> None:
    """Un vehículo tiene exactamente 0 o 1 custodia activa. Nunca dos.

    Dos custodias abiertas a la vez significan que dos personas responden por el
    mismo camión, que en la práctica es que ninguna responde. Es el estado que
    el `UNIQUE` parcial `(vehiculo_id) WHERE fin_ts IS NULL` impide en la base;
    esta función es la misma política del lado del dominio, para que el traspaso
    no dependa de que la base lo atrape.

    Levanta `CustodiaInvalida`.
    """
    activas = custodias_activas(custodias)
    if len(activas) > 1:
        raise CustodiaInvalida(
            f'{len(activas)} custodias activas a la vez para el mismo vehículo. '
            f'Dos responsables del mismo camión es, en la práctica, ninguno.'
        )


def huecos_de_cobertura(custodias: Sequence[Custodia], ahora: datetime) -> List[Hueco]:
    """Tramos sin custodio entre la primera custodia y `ahora`.

    QUÉ AFIRMA: que entre la apertura de la primera custodia y el instante dado
    hubo momentos sin nadie responsable, y cuáles.

    QUÉ NO AFIRMA: nada sobre el período anterior a la primera custodia. Antes
    del arranque en frío el sistema no sabe y no pretende saber — por eso la
    primera custodia de cada vehículo se marca `linea_base` y sus daños nacen
    preexistentes, sin responsable.

    Lista vacía = cobertura completa. El traspaso es atómico justamente para que
    esta lista no pueda crecer: cierra la anterior y abre la nueva en una sola
    transacción, sin instante intermedio.
    """
    if not custodias:
        return []

    ordenadas = sorted(custodias, key=lambda c: c.inicio_ts)
    huecos = []
    # El reloj arranca en la primera custodia, nunca antes: sobre el período
    # previo al arranque en frío el sistema no sabe y no pretende saber.
    cursor = ordenadas[0].inicio_ts

    for c in ordenadas:
        if c.inicio_ts > cursor:
            huecos.append(Hueco(desde=cursor, hasta=c.inicio_ts))
        fin = ahora if c.fin_ts is None else c.fin_ts
        if fin > cursor:
            cursor = fin

    if cursor < ahora:
        huecos.append(Hueco(desde=cursor, hasta=ahora))
    return huecos


def validar_arco_exclusivo(custodia: Custodia) -> None:
    """Exactamente un `custodio_*_id` no nulo, y corresponde a `custodio_tipo`.

    Un `custodio_tipo = conductor` con `custodio_sede_id` lleno —o con los dos
    llenos, o con ninguno— es un registro que no dice de quién es la
    responsabilidad. En un acta eso no vale nada.

    Levanta `CustodiaInvalida`.
    """
    if custodia.custodio_estado == CustodioEstado.PENDIENTE_SEDE:
        # No afloja el invariante: lo hace condicional a un estado que a su vez
        # está constreñido. Una custodia `pendiente_sede` tiene que ser de tipo
        # sede y no puede traer NINGÚN custodio puesto — si trajera uno, sería
        # una sede resuelta que finge no serlo.
        if custodia.custodio_tipo != CustodioTipo.SEDE:
            raise CustodiaInvalida(
                'pendiente_sede solo aplica a custodia de sede: un conductor '
                'siempre tiene fila, y es la cédula lo que hace válida el acta.'
            )
        if (custodia.custodio_conductor_id is not None
                or custodia.custodio_sede_id is not None):
            raise CustodiaInvalida(
                'pendiente_sede con un custodio puesto: o la sede se pudo '
                'representar, o no. No las dos cosas.'
            )
        return

    llenos = [
        custodia.custodio_conductor_id is not None,
        custodia.custodio_sede_id is not None,
    ]
    if sum(llenos) != 1:
        raise CustodiaInvalida(
            'una custodia lleva exactamente un custodio: '
            f'conductor={custodia.custodio_conductor_id}, sede={custodia.custodio_sede_id}. '
            'Un registro con los dos, o con ninguno, no dice de quién es la responsabilidad.'
        )

    esperado = ('custodio_conductor_id' if custodia.custodio_tipo == CustodioTipo.CONDUCTOR
                else 'custodio_sede_id')
    if getattr(custodia, esperado) is None:
        raise CustodiaInvalida(
            f'custodio_tipo={custodia.custodio_tipo.value} exige {esperado}, '
            f'y el que viene lleno es el otro.'
        )


__all__ = [
    'Hueco',
    'custodias_activas',
    'validar_cardinalidad',
    'huecos_de_cobertura',
    'validar_arco_exclusivo',
]
