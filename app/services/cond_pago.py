"""
Cómo se interpreta la condición de pago de un pedido. **Una función.**

`f430_id_cond_pago` viene del pedido de Siesa y puede llegar vacío cuando el
maestro del cliente está incompleto. Hasta el 2026-08-13 ese vacío se
interpretaba en **dos sitios**, los dos hacia contado:

  · `connekta_gateway.trigger_factura_desde_remision` — al facturar, cae a
    `SIESA_COND_PAGO_VENTAS` (obligado: Connekta V2 colapsa con HTTP 500 si el
    campo va en null) y avisa.
  · `ruta_service._valor_y_cond_pago` — `es_contado = (not cond_pago) or ...`,
    que con vacío da **True**.

El segundo llega a la pantalla del conductor: `rutas.js` solo muestra el modo
CRÉDITO cuando `es_contado === false` **confirmado**. Con vacío mostraba «Valor
a Cobrar» — es decir, **al conductor se le pedía cobrarle a un cliente cuya
condición de pago nadie conocía**.

## Por qué «no sé» no puede colapsar a «contado»

Un vacío significa tres cosas distintas que no se distinguen entre sí: el
maestro está incompleto, la consulta falló, o el cliente de verdad es de
contado. Tratarlas igual es la Regla 0 al revés — el lado conservador acá no es
cobrar, es **no afirmar**.

Hoy el daño es acotado (el conductor cobra de más y se devuelve). Con la
factura diferida al momento de la liquidación —diseño en evaluación— pasaría a
ser: cliente de crédito leído como contado, sin factura, sin cuenta por cobrar
y sin consumir cupo. Un crédito otorgado por un campo vacío.

## Lo que NO hace esta función

No decide qué hacer con cada caso: eso depende del llamador. La pantalla puede
mostrar «no sé» y dejar que el conductor elija; el gateway **no puede** mandar
null y tiene que caer a algo. Lo que sí es una sola política es **cómo se lee
el vacío**, y esa es la que vivía duplicada.
"""
import logging

logger = logging.getLogger(__name__)

CONTADO = 'contado'
CREDITO = 'credito'
#: Ni contado ni crédito: **no se sabe**. No es un tercer tipo de venta, es la
#: ausencia del dato — y se declara en vez de resolverse hacia un lado.
AUSENTE = 'ausente'


def clasificar(cond_pago_siesa, cond_pago_contado) -> str:
    """`contado` | `credito` | `ausente` a partir de `f430_id_cond_pago`.

    `cond_pago_contado` es el código que la empresa usa para contado
    (`SIESA_COND_PAGO_VENTAS`, típicamente `C01`).
    """
    valor = (cond_pago_siesa or '').strip()
    if not valor:
        return AUSENTE
    return CONTADO if valor == (cond_pago_contado or '').strip() else CREDITO


def es_contado_o_none(cond_pago_siesa, cond_pago_contado):
    """`True` | `False` | `None` — para las pantallas.

    `None` es el caso que antes se colapsaba a `True`. El frontend ya sabe
    manejarlo: cae al campo libre en vez de mostrar un valor a cobrar que nadie
    confirmó.
    """
    clase = clasificar(cond_pago_siesa, cond_pago_contado)
    if clase == AUSENTE:
        return None
    return clase == CONTADO


def registrar_ausencia(contexto: str, tercero: str = ''):
    """Deja rastro cuando el dato falta. **No es cosmético.**

    La pregunta «¿esto pasa alguna vez?» se estuvo discutiendo con cifras de
    otro sistema —conteos de facturas— que no pueden detectarlo: el fallback
    rellena el campo antes de emitir, así que toda factura sale con condición.
    Contar facturas mira el único lugar donde la evidencia está garantizada
    limpia.

    Lo que sí lo detecta es esto.
    """
    logger.warning(
        '[COND_PAGO] ausente en %s%s — no se asume contado',
        contexto, f' (tercero {tercero})' if tercero else '',
    )
