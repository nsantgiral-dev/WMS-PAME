"""
Políticas de inspección. Sin I/O, sin framework.

Dos reglas viven acá y no en la pantalla, porque las dos son decisiones de
diseño que alguien va a querer "mejorar" sin saber qué rompen.
"""
import hashlib
from datetime import date
from typing import List, Sequence

# Regla 6: todo hallazgo nace con severidad y fecha límite, y el plazo se
# CALCULA, no se elige a mano. Una sola tabla, consumida por todos — si el
# plazo se pudiera escribir a dedo, la severidad dejaría de significar algo y
# "bloqueante" pasaría a ser una etiqueta que se negocia.
DIAS_DE_PLAZO = {'bloqueante': 0, 'mayor': 7, 'menor': 30}


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


__all__ = ['DIAS_DE_PLAZO', 'dias_de_plazo', 'ordenar_para_el_dia', 'items_del_dia']
