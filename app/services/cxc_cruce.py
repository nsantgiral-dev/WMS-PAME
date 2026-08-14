"""
Cómo se encuentra la fila de cartera de una factura. **Una función.**

`API_v2_CxC_General` devuelve las cuentas por cobrar de un tercero, y para
localizar la de una factura concreta hay que saber qué traen los campos de
cruce. La respuesta, verificada en vivo el 2026-08-11 y escrita en
`liquidacion_service`:

    f353_id_tipo_docto_cruce / f353_consec_docto_cruce  →  traen el **PEDIDO**

Es una asimetría incómoda —`get_rowids_factura` sí necesita la FACTURA, porque
filtra por `f353_*` es del cruce y `f350_*` del documento— y por eso se
documentó con esas palabras.

## Por qué esto existe como módulo

Esa búsqueda estaba escrita **dos veces, con claves distintas**, las dos citando
la misma verificación en vivo:

    liquidacion_service.py   matcheaba contra el PEDIDO   ← el correcto
    siesa_job_service.py     matcheaba contra la FACTURA  ← nunca encontraba nada

Y la segunda es la que decide, tras un POST que lanzó excepción, si el recibo de
caja **sí entró**. Al no encontrar nunca la fila, respondía «no entró», el job
revertía la bandera y la cola reenviaba: **segundo recibo de caja**. Que es
exactamente el incidente que la Regla 3 existe para prevenir.

Dos implementaciones de la misma pregunta divergen. Ya pasó con la condición de
pago y con el fallback de días; acá costaba un documento financiero duplicado.
"""
import logging

logger = logging.getLogger(__name__)

#: Centavos. Un saldo de $0,3 es una factura saldada, no una con deuda.
TOLERANCIA = 0.5


def fila_de_la_factura(cxc: list, tipo_docto_pedido: str, consec_docto_pedido):
    """La fila de cartera que corresponde a esa factura, o `None`.

    Se busca por el **PEDIDO**, no por la factura: es lo que traen los campos
    de cruce. Pasarle la FE resuelta hace que no matchee ninguna fila — y el
    modo de fallo es silencioso, porque «ninguna fila» se lee igual que «no
    hay deuda» o que «no pude saber», según quién pregunte.
    """
    if not (tipo_docto_pedido and consec_docto_pedido):
        return None
    tipo = str(tipo_docto_pedido).strip()
    consec = str(consec_docto_pedido).strip()
    return next((
        r for r in (cxc or [])
        if str(r.get('f353_id_tipo_docto_cruce', '')).strip() == tipo
        and str(r.get('f353_consec_docto_cruce', '')).strip() == consec
    ), None)


def saldo_de_la_fila(fila) -> float:
    return float(fila.get('f353_total_db', 0) or 0) - float(fila.get('f353_total_cr', 0) or 0)


def esta_saldada(cxc: list, tipo_docto_pedido: str, consec_docto_pedido):
    """`True` | `False` | `None`.

    **`None` es el caso que importa** y el que antes se colapsaba a `False`: la
    fila no apareció, así que no se sabe si la factura quedó saldada. No es lo
    mismo que «tiene saldo».

    Quien pregunta después de un POST fallido tiene que distinguirlos: ante «no
    sé», la Regla 3 dice **no reintentar** —un timeout no significa que falló, y
    un recibo de caja duplicado es un documento financiero que alguien tiene que
    reversar a mano—. Ante «tiene saldo», reintentar es correcto.
    """
    fila = fila_de_la_factura(cxc, tipo_docto_pedido, consec_docto_pedido)
    if fila is None:
        return None
    return saldo_de_la_fila(fila) <= TOLERANCIA
