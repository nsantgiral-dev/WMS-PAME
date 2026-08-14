"""
Cómo se interpreta la condición de pago de un pedido. **Una función.**

`f430_id_cond_pago` viene del pedido de Siesa y puede llegar vacío cuando el
maestro del cliente está incompleto. Hasta el 2026-08-13 ese vacío se
interpretaba en **dos sitios**, los dos hacia contado:

  · `connekta_gateway.trigger_factura_desde_remision` — al facturar caía a
    `SIESA_COND_PAGO_VENTAS`, **que es el código de contado**. Hoy cae a
    `SIESA_COND_PAGO_RUTA` (crédito a un día): ver `aprobable_en_ruta`.
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

El daño en pantalla es acotado: el conductor cobra de más y se devuelve. El
daño del otro lado no lo era — el vacío emitía una factura de contado, y el
2026-08-13 se probó en producción que **Siesa no la aprueba**. Quedaba el
inventario descargado, la factura en Elaboración y la liquidación sin CxC
contra la cual cruzar el recibo. Un campo vacío rompía el ciclo entero.

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
    (`SIESA_COND_PAGO_VENTAS`). Se configura para **reconocerlo, no para
    emitirlo** — ver `aprobable_en_ruta`.
    """
    valor = (cond_pago_siesa or '').strip()
    if not valor:
        return AUSENTE
    return CONTADO if valor == (cond_pago_contado or '').strip() else CREDITO


def aprobable_en_ruta(cond_pago, cond_pago_contado) -> bool:
    """`False` si esa condición produce una factura que Siesa **no va a aprobar**.

    Probado en producción el 2026-08-13: dos facturas de contado, por $263.963
    y $14.200, quedaron **en Elaboración** con el mensaje *«el valor de la
    cartera debe ser igual al valor de las CxC»*. Es el mismo mensaje de la
    Regla 21 — no es una rareza de la nota crédito, es el invariante de
    aprobación de Siesa: **un documento no se aprueba si su cartera no cuadra.**
    Una FE de contado exige el recaudo en el mismo documento, y en ruta ese
    recaudo no existe todavía: lo hace el conductor horas después.

    Por eso la factura de ruta nace **a crédito de un día** y el recibo de caja
    del conductor la salda. No es un rodeo: es lo que físicamente pasa.

    Y por eso `f462_id_caja` del 142943 se manda vacío. Llenarlo haría que
    Siesa registrara el ingreso al facturar —plata que nadie ha recibido— y
    otra vez cuando llegue el RC de la liquidación.

    Vive acá y no en el gateway porque la misma pregunta la hace el desglose
    para contar cuántos pedidos de ruta vienen con una condición que no sirve.
    """
    return clasificar(cond_pago, cond_pago_contado) != CONTADO


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


def cobra_en_la_puerta(cond_pago_siesa, cond_pago_contado, cond_pago_ruta):
    """`True` | `False` | `None` — si el conductor tiene que cobrar en la puerta.

    Pregunta distinta de `aprobable_en_ruta`, aunque comparten los mismos dos
    códigos. Esa responde si Siesa **aprueba** la FE con esa condición —ahí
    C01 (contado) es `False` y C02 (ruta) es `True`, porque el invariante de
    aprobación de Siesa mira si la cartera cuadra, no si el cliente paga en
    la puerta—. Esta responde si el cliente **paga al momento de la
    entrega** — y ahí C01 y C02 dan la MISMA respuesta: sí. El vendedor
    puede capturar cualquiera de los dos códigos para un pedido de ruta que
    de cara al cliente es de contado (C02 es el que además se aprueba en
    Siesa); la pantalla del conductor y la validación de consistencia tienen
    que reconocer los dos, no solo C01.

    Fusionarlas en una sola función ya se probó y rompe la otra pregunta:
    si `clasificar` tratara C02 como CONTADO, `aprobable_en_ruta('C02', ...)`
    empezaría a dar `False` para el único código que sí se aprueba. Por eso
    viven separadas — una función, una pregunta (Regla 0).

    `None` = ausente, mismo criterio que `es_contado_o_none`: no se afirma
    nada sobre un dato que no está.
    """
    clase = clasificar(cond_pago_siesa, cond_pago_contado)
    if clase == AUSENTE:
        return None
    if clase == CONTADO:
        return True
    valor = (cond_pago_siesa or '').strip()
    _ruta = (cond_pago_ruta or '').strip()
    return bool(_ruta) and valor == _ruta


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


# ── Modo de la pantalla del conductor ─────────────────────────────────────
#: Los tres modos que puede mostrar la parada. Vivían calculados en
#: `rutas.js:1908` y no se podían contar: el desglose sabía qué eligió el
#: conductor, no qué opciones tenía enfrente.
CREDITO_PANTALLA = 'CREDITO'   # no se cobra en la puerta, no se pregunta monto
DINAMICO = 'DINAMICO'          # contado confirmado y valor conocido → Total/Parcial
LIBRE = 'LIBRE'                # **el conductor elige sin restricción**


def modo_pantalla(es_contado, hay_valor_conocido: bool) -> str:
    """Qué modo de pago ve el conductor en esa parada.

    `LIBRE` es donde vive el riesgo y por eso hay que poder contarlo: el
    conductor puede marcar CREDITO en una parada de contado, y
    `confirmar_parada` no ata `forma_pago` a la condición del pedido — solo
    valida que el valor esté en la lista.

    Un pedido con `cond_pago` vacío cae acá en LIBRE, porque `es_contado` no
    es `True` confirmado. Del lado de la factura ese mismo vacío ya no hace
    daño —cae a la condición de ruta— pero la parada sigue quedando sin
    restricción, y eso hay que poder contarlo.

    `es_contado` es `True` | `False` | `None`. Solo el `False` **confirmado**
    muestra CRÉDITO: ante `None` no se afirma nada y se deja elegir, que es lo
    correcto — pero hay que saber cuántas veces pasa.
    """
    if es_contado is False:
        return CREDITO_PANTALLA
    if es_contado is True and hay_valor_conocido:
        return DINAMICO
    return LIBRE
