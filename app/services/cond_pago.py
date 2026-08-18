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
    """`True` | `False` | `None` — **contado documental**, no cobro en la puerta.

    `None` es el caso que antes se colapsaba a `True`. El frontend ya sabe
    manejarlo: cae al campo libre en vez de mostrar un valor a cobrar que nadie
    confirmó.

    ⚠️ Para decidir qué ve el conductor, la función es
    `cobra_en_la_puerta` — no ésta. Ver el bloque de abajo.
    """
    clase = clasificar(cond_pago_siesa, cond_pago_contado)
    if clase == AUSENTE:
        return None
    return clase == CONTADO


# ── Contado documental ≠ cobro en la puerta ───────────────────────────────
#
# Son **dos preguntas distintas** y solo coinciden en C01. Durante meses las
# contestó una sola función, y por eso el conductor no veía el cobro:
#
#                                    C01   C02   C04
#     ¿la FE trae su propio recaudo?  sí    no    no    ← clasificar()
#     ¿el conductor cobra?            sí    sí    no    ← cobra_en_la_puerta()
#
# La ruta factura en C02 **precisamente porque** C01 es imposible: una FE de
# contado no se aprueba sin el recaudo dentro del documento, y ese recaudo lo
# hace el conductor horas después (ver `aprobable_en_ruta`). Es decir: el
# motivo por el que C02 no es contado documental es el mismo por el que **sí**
# se cobra en la puerta. Una función no puede contestar las dos.
#
# `clasificar` no se toca: gobierna si la factura se puede aprobar. Cambiarla
# para arreglar la pantalla rompería la emisión.

def cond_pago_efectiva(cond_pago_siesa, cond_pago_ruta):
    """La condición con la que el documento **sale de verdad**, no la que el
    pedido declara. `''` si no se puede determinar.

    El pedido puede venir sin condición —maestro del tercero incompleto— y el
    gateway no puede emitir `null`: cae a la condición de ruta. Esa caída es
    una decisión con consecuencia, y hasta el 2026-08-14 estaba escrita **dos
    veces con resultados opuestos**:

        pedido sin condición
          ├── FE       → cae a C02  → «la salda el RC del conductor» (dice la alerta)
          └── pantalla → se queda en ausente → LIBRE → no se le pide cobrar

    La factura salía en una condición que exige cobro y la pantalla no lo
    pedía. Es el mismo defecto que `cobra_en_la_puerta` arregla,
    reintroducido por el caso ausente — y es el corolario de la Regla 0: **una
    política, una función.** El mismo fallback en dos sitios diverge.

    No es afirmar sobre un dato ausente: es leer lo que ya se emitió. La
    ausencia sigue declarándose por su canal (`registrar_ausencia` y la alerta
    `DATA_MAESTRA_COND_PAGO`) — lo que deja de pasar es que el documento diga
    una cosa y la pantalla otra.
    """
    return (cond_pago_siesa or '').strip() or (cond_pago_ruta or '').strip()


def cobra_en_la_puerta(cond_pago_siesa, cond_pago_contado, cond_pago_ruta):
    """`True` | `False` | `None` — ¿el conductor cobra en esta parada?

    Pregunta distinta de `aprobable_en_ruta`, aunque comparten los mismos dos
    códigos. Esa responde si Siesa **aprueba** la FE; ésta, si el cliente
    **paga al momento de la entrega** — y ahí C01 y C02 dan la MISMA
    respuesta. Fusionarlas ya se probó y rompe la otra: si `clasificar`
    tratara C02 como CONTADO, `aprobable_en_ruta('C02', ...)` daría `False`
    para el único código que sí se aprueba. Una función, una pregunta.

    `True` para el contado (C01) y para la condición de ruta (C02, crédito a un
    día que el RC del conductor salda el mismo día). `False` para el crédito
    real —C04 a 30 días—, que se entrega y se firma sin pedir plata.

    ## Por qué el `None` cuando falta `SIESA_COND_PAGO_RUTA`

    Sin ese código, C02 y C04 son **indistinguibles**: los dos son «distinto de
    contado». Devolver `False` ahí reintroduciría el defecto entero en silencio
    —ninguna parada de ruta pediría cobrar— y devolver `True` le pediría plata
    a un cliente de crédito.

    `None` cae a `LIBRE`, que es el modo que **no afirma nada** y deja elegir al
    conductor. Es Regla 0 aplicada a la pantalla: el lado conservador no es
    cobrar ni no cobrar, es no decidir por él. Y `LIBRE` se cuenta en el
    desglose, así que la falta de configuración se ve en vez de callarse.
    """
    # `None` no es `''`. El vacío es «Siesa respondió y el tercero no tiene
    # condición» —y entonces sabemos que la FE salió en la de ruta—; el `None`
    # es «no se pudo preguntar», y ahí no se sabe qué lleva la factura ni si
    # llegó a existir. Colapsarlos ya fue un defecto una vez; acá volvería a
    # serlo, en la dirección de pedirle plata a alguien sobre un supuesto.
    if cond_pago_siesa is None:
        return None
    ruta = (cond_pago_ruta or '').strip()
    # La condición que la FE lleva de verdad, no la que el pedido declara: un
    # pedido sin condición se factura en la de ruta, y esa factura **hay que
    # cobrarla**. Ver `cond_pago_efectiva`.
    valor = cond_pago_efectiva(cond_pago_siesa, ruta)
    if not valor:
        return None
    if valor == (cond_pago_contado or '').strip():
        return True
    if not ruta:
        logger.warning(
            '[COND_PAGO] SIESA_COND_PAGO_RUTA sin configurar: no se puede '
            'distinguir la condición de ruta del crédito real (cond=%s). '
            'La parada queda en LIBRE.', valor)
        return None
    return valor == ruta


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


def modo_pantalla(se_cobra_en_puerta, hay_valor_conocido: bool) -> str:
    """Qué modo de pago ve el conductor en esa parada.

    El primer argumento es **`cobra_en_la_puerta`**, no `es_contado`. Hasta
    el 2026-08-14 recibía el segundo, y como toda venta de ruta es C02 —que no
    es contado documental— **ninguna parada pedía cobrar**. Ver el bloque
    «Contado documental ≠ cobro en la puerta» arriba.

    `LIBRE` es donde vive el riesgo y por eso hay que poder contarlo: sobre una
    parada sin condición conocida, `confirmar_parada` **no bloquea** el crédito
    —no saber no es evidencia (Regla 0)— así que el conductor puede marcar
    CREDITO sobre algo que sí había que cobrar.

    Un pedido con `cond_pago` vacío cae acá en LIBRE, porque no hay `True`
    confirmado. Del lado de la factura ese mismo vacío ya no hace daño —cae a
    la condición de ruta— pero la parada sigue quedando sin restricción.

    Es `True` | `False` | `None`. Solo el `False` **confirmado** muestra
    CRÉDITO: ante `None` no se afirma nada y se deja elegir, que es lo correcto
    — pero hay que saber cuántas veces pasa.
    """
    if se_cobra_en_puerta is False:
        return CREDITO_PANTALLA
    if se_cobra_en_puerta is True and hay_valor_conocido:
        return DINAMICO
    return LIBRE
