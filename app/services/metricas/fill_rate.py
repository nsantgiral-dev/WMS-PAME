"""
Nivel de servicio (fill rate por unidades) sobre la HISTORIA del pedido.

`pedidos_pendientes.calcular_pedidos_pendientes` calcula el fill rate sobre
`pedidos_siesa`, y `pedidos_siesa` **borra la línea en cuanto deja de tener
pendiente**. Lo que queda es lo que todavía no se cumplió: el fill rate sale
bajo por construcción — sesgo de supervivencia. `pedidos_historia`
(m036fotos) guarda también las líneas que salieron, y con ella la pregunta se
puede contestar.

## La definición

Cohorte = las líneas cuya **fecha de entrega** es el día `dia`.

    fill rate = Σ min(remisionado, pedido) / Σ pedido     (unidades)

- Línea abierta: lo remisionado según el último barrido (≤ 10 min de viejo).
- CUMPLIDO visto en un barrido (`estado_siesa_salida` NULL): el barrido la vio
  con remisionado ≥ pedido; cuenta con sus cantidades.
- **No se sabe cómo terminó** → fuera del cálculo y declarada, y el resultado
  queda `completo=False`: DESAPARECIDO (salió de la lista sin que se sepa por
  qué), OTRO_ESTADO (volvió a otro estado en Siesa) y CUMPLIDO por estado 4
  del pedido (se clasificó preguntando a Siesa: lo remisionado que quedó en la
  historia es el del último barrido **antes** de salir, no el final).
- ANULADO → fuera del denominador y declarada. Un pedido anulado no es un
  despacho incumplido; tampoco se puede probar que no lo sea, por eso se
  cuenta aparte.

## Qué la vuelve inmune al sesgo — y dónde no lo es

Una línea que salió antes de que empezara la historia no está en la tabla. Por
eso solo entran las líneas **pedidas desde el primer día de historia**
(`fecha_pedido ≥ inicio`): esas no pudieron salir antes de que se empezara a
mirar. Las pedidas antes se excluyen y se declaran (`pedidas_antes_de_la_historia`),
con `completo=False`. Sin historia, o para un día anterior a ella, no hay
número: `None` + motivo.

Límite heredado del sync: la historia solo ve líneas de pedidos que llegaron a
estado 3 (comprometido) del CO que sincroniza el WMS.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models.pedido_historia import MotivoSalidaPedido, PedidoHistoria

#: Salidas cuya cantidad final no se conoce.
_DESENLACE_DESCONOCIDO = (MotivoSalidaPedido.DESAPARECIDO, MotivoSalidaPedido.OTRO_ESTADO)
#: `estado_siesa_salida` con el que `clasificar_desaparecidas` pasa a CUMPLIDO.
_CUMPLIDO_POR_ESTADO = 4


def inicio_de_la_historia():
    """El primer día operativo con historia de pedidos, o `None` si no hay."""
    return db.session.query(func.min(PedidoHistoria.primer_dia_visto)).scalar()


def calcular_fill_rate_historia(dia: date, bodega: str = None) -> dict:
    """Fill rate por unidades de la cohorte con entrega `dia`. Ver el módulo.

    Devuelve `{'fill_rate', 'unidades_servidas', 'unidades_pedidas', 'lineas',
    'completo', 'motivo', 'excluidas': {...}, 'inicio_historia'}`. `fill_rate`
    es una proporción (0–1) o `None` con `motivo`.
    """
    inicio = inicio_de_la_historia()
    base = {'fill_rate': None, 'unidades_servidas': 0.0, 'unidades_pedidas': 0.0,
            'lineas': 0, 'completo': False, 'excluidas': {},
            'inicio_historia': inicio.isoformat() if inicio else None}
    if inicio is None:
        return {**base, 'motivo': ('sin historia de pedidos: pedidos_siesa solo guarda lo '
                                   'pendiente y medir ahí es sesgo de supervivencia')}
    if dia < inicio:
        return {**base, 'motivo': f'antes de que empezara la historia de pedidos ({inicio})'}

    q = PedidoHistoria.query.filter(PedidoHistoria.fecha_entrega == dia)
    if bodega:
        q = q.filter(PedidoHistoria.bodega == str(bodega).strip().upper())

    excl = {'pedidas_antes_de_la_historia': 0, 'sin_fecha_pedido': 0, 'anuladas': 0,
            'desenlace_desconocido': 0, 'sin_cantidad_pedida': 0}
    servidas = pedidas = Decimal(0)
    lineas = 0
    for h in q.all():
        if h.fecha_pedido is None:
            excl['sin_fecha_pedido'] += 1
            continue
        if h.fecha_pedido < inicio:
            excl['pedidas_antes_de_la_historia'] += 1
            continue
        if h.motivo_salida == MotivoSalidaPedido.ANULADO:
            excl['anuladas'] += 1
            continue
        if (h.motivo_salida in _DESENLACE_DESCONOCIDO
                or (h.motivo_salida == MotivoSalidaPedido.CUMPLIDO
                    and h.estado_siesa_salida == _CUMPLIDO_POR_ESTADO)):
            excl['desenlace_desconocido'] += 1
            continue
        pedida = h.cantidad_pedida
        if pedida is None or pedida <= 0:
            excl['sin_cantidad_pedida'] += 1
            continue
        remisionada = h.cantidad_remisionada or Decimal(0)
        servidas += min(remisionada, pedida)
        pedidas += pedida
        lineas += 1

    # Lo que no se sabe hace incompleto el número; lo anulado se declara y ya.
    desconocidas = (excl['pedidas_antes_de_la_historia'] + excl['sin_fecha_pedido']
                    + excl['desenlace_desconocido'] + excl['sin_cantidad_pedida'])
    res = {**base, 'unidades_servidas': float(servidas), 'unidades_pedidas': float(pedidas),
           'lineas': lineas, 'excluidas': excl, 'completo': desconocidas == 0}
    if not pedidas:
        res['motivo'] = ('ninguna línea medible con entrega ese día'
                         + (f' ({desconocidas} excluidas sin desenlace conocido)'
                            if desconocidas else ''))
        return res
    res['fill_rate'] = float(servidas / pedidas)
    res['motivo'] = (None if desconocidas == 0 else
                     f'{desconocidas} línea(s) de la cohorte sin desenlace medible, fuera del cálculo')
    return res
