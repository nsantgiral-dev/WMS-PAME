"""
Por qué no se entregó. Tipificado, y con una pregunta que el texto libre no hacía.

Hasta el 2026-08-13, `RECHAZADO` era **el estado más barato de los tres**:

    ENTREGADO   → forma de pago
    PARCIAL     → forma de pago + monto > 0 + observaciones
    RECHAZADO   → solo observaciones (texto libre)

Nadie decidió que devolver mercancía costara menos que cobrarla: salió de cómo
se fueron agregando las validaciones. Pero el efecto es real, y el control «si
no paga completo, no se entrega» lo empeora — el conductor que no quiere subir
bultos al camión marca el estado que menos pide.

## La pregunta que faltaba

El texto libre no distingue el único dato que importa contablemente:
**¿la mercancía volvió?**

Un rechazo donde el cliente se quedó con la mercancía y no pagó **no es un
rechazo**: es un crédito sin registro, sin cupo y sin que ningún control lo
evalúe. Y con el estado marcado como RECHAZADO, el sistema cree que el
inventario volvió al camión cuando no volvió — faltante de inventario
convertido en devolución falsa, que solo aparece en un conteo físico.

## Por qué el caso peligroso tiene su propia opción

`NO_PAGO_SE_QUEDO` existe a propósito. Prohibir sin dar alternativa no elimina
un comportamiento: lo esconde. Si el conductor no puede declarar «no pagó y se
quedó con la mercancía», va a marcar «cliente cerrado» y el dato va a mentir
sobre inventario, que es peor que mentir sobre cartera.

Darle nombre lo hace contable. Y contable es el primer paso para que alguien
decida si se autoriza o se prohíbe — decisión que hoy nadie puede tomar porque
el caso no se ve.

## Un motivo NO es una autorización

Que exista la opción no significa que esté permitido. Significa que cuando
ocurre queda registrado. La política sobre qué hacer con esos casos es del
negocio; esto solo garantiza que lleguen a la conversación.
"""
from typing import NamedTuple


class MotivoRechazo(NamedTuple):
    codigo: str
    etiqueta: str
    #: `True` = la mercancía vuelve al camión y hay que reingresarla.
    #: `False` = **se quedó con el cliente**. El estado dice RECHAZADO pero el
    #: inventario no volvió, y eso cambia qué documento hace falta en Siesa.
    retorna: bool


MOTIVOS = (
    MotivoRechazo('CLIENTE_CERRADO', 'Cliente cerrado / no había nadie', True),
    MotivoRechazo('DIRECCION_ERRADA', 'Dirección incorrecta o no se encontró', True),
    MotivoRechazo('NO_PIDIO', 'El cliente dice que no pidió esto', True),
    MotivoRechazo('MERCANCIA_AVERIADA', 'Mercancía averiada o incompleta', True),
    MotivoRechazo('FUERA_DE_HORARIO', 'Llegamos fuera del horario de recibo', True),
    MotivoRechazo('NO_PAGO', 'No pagó — se devuelve la mercancía', True),
    # El que hace visible lo que hoy es invisible.
    MotivoRechazo('NO_PAGO_SE_QUEDO', 'No pagó y se quedó con la mercancía', False),
)

CODIGOS = tuple(m.codigo for m in MOTIVOS)
#: Los que dejan mercancía en la calle. Hoy es uno; la lista existe para que
#: agregar otro no exija encontrar todos los `== 'NO_PAGO_SE_QUEDO'` del código.
SIN_RETORNO = tuple(m.codigo for m in MOTIVOS if not m.retorna)


def valido(codigo) -> bool:
    return (codigo or '').strip().upper() in CODIGOS


def retorna_mercancia(codigo) -> bool:
    """`True` si el inventario vuelve al camión.

    Ante un código desconocido devuelve `True` — el lado conservador es asumir
    que la mercancía SÍ vuelve, porque tratarla como perdida sin evidencia
    genera un ajuste de inventario que nadie pidió. Regla 0.
    """
    c = (codigo or '').strip().upper()
    return c not in SIN_RETORNO


def etiqueta(codigo) -> str:
    c = (codigo or '').strip().upper()
    return next((m.etiqueta for m in MOTIVOS if m.codigo == c), c or '—')


def para_frontend() -> list:
    """`[{codigo, etiqueta, retorna}]` — el desplegable del conductor.

    Sale de acá y no de una lista en el JS: dos catálogos del mismo dominio
    divergen, y ya pasó con la condición de pago y con los tipos de vehículo.
    """
    return [{'codigo': m.codigo, 'etiqueta': m.etiqueta, 'retorna': m.retorna}
            for m in MOTIVOS]
