"""
Políticas de custodia. Invariantes 2 (cardinalidad), 3 (cobertura temporal)
y 4 (arco exclusivo).

Sin implementar. Ver la nota de `odometro.py` sobre por qué existen las firmas.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Sequence

from flota.dominio.valores import Custodia


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
    raise NotImplementedError(
        'flota.dominio.custodia.custodias_activas — invariante 2 (cardinalidad) sin implementar'
    )


def validar_cardinalidad(custodias: Sequence[Custodia]) -> None:
    """Un vehículo tiene exactamente 0 o 1 custodia activa. Nunca dos.

    Dos custodias abiertas a la vez significan que dos personas responden por el
    mismo camión, que en la práctica es que ninguna responde. Es el estado que
    el `UNIQUE` parcial `(vehiculo_id) WHERE fin_ts IS NULL` impide en la base;
    esta función es la misma política del lado del dominio, para que el traspaso
    no dependa de que la base lo atrape.

    Levanta `CustodiaInvalida`.
    """
    raise NotImplementedError(
        'flota.dominio.custodia.validar_cardinalidad — invariante 2 (cardinalidad) sin implementar'
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
    raise NotImplementedError(
        'flota.dominio.custodia.huecos_de_cobertura — invariante 3 (cobertura temporal) sin implementar'
    )


def validar_arco_exclusivo(custodia: Custodia) -> None:
    """Exactamente un `custodio_*_id` no nulo, y corresponde a `custodio_tipo`.

    Un `custodio_tipo = conductor` con `custodio_sede_id` lleno —o con los dos
    llenos, o con ninguno— es un registro que no dice de quién es la
    responsabilidad. En un acta eso no vale nada.

    Levanta `CustodiaInvalida`.
    """
    raise NotImplementedError(
        'flota.dominio.custodia.validar_arco_exclusivo — invariante 4 (arco exclusivo) sin implementar'
    )


__all__ = [
    'Hueco',
    'custodias_activas',
    'validar_cardinalidad',
    'huecos_de_cobertura',
    'validar_arco_exclusivo',
]
