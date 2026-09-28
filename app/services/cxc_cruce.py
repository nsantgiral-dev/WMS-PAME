"""
Cómo se encuentra la fila de cartera de una factura. **Una función.**

`API_v2_CxC_General` devuelve las cuentas por cobrar de un tercero, y para
localizar la de una factura concreta hay que saber qué traen los campos de
cruce. La regla general, verificada en vivo el 2026-08-11 y escrita en
`liquidacion_service`:

    f353_id_tipo_docto_cruce / f353_consec_docto_cruce  →  traen el **PEDIDO**

Es una asimetría incómoda —`get_rowids_factura` sí necesita la FACTURA, porque
filtra por `f353_*` es del cruce y `f350_*` del documento— y por eso se
documentó con esas palabras.

**Y hoy no es la regla para las facturas del WMS** (hallazgo 2026-09-24,
contado contraentrega): la cartera de las FE que emite el WMS (142943) se
indexa por la **FACTURA** —`f353_id_tipo_docto_cruce` = tipo de la FE—, no
por el pedido. La búsqueda de abajo sigue probando primero por PEDIDO (no se
cambió el comportamiento) y cae a la FE, que es donde de verdad aparecen. Y
no sirve para saber la condición de pago: el WMS mandaba `F353_FECHA_VCTO =
hoy + 30` fijo y Siesa lo respeta, así que toda FE del WMS vencía a 30 días
sin importar la condición (desde el 2026-09-24 lo decide
`cond_pago.vencimiento_fe`; las ya emitidas quedan con +30).

**No es universal.** PD1411/FE-1416 (2026-08-18) trajo el cruce indexado por
la **FE**, no por el pedido — verificado en vivo contra Siesa (fila
`f353_id_tipo_docto_cruce='FE', consec='1416'`, saldo completo). Se busca
primero por PEDIDO (la regla, sigue siendo el caso común) y si no aparece
nada se reintenta por FE — nunca al revés, para no tapar en silencio un
verdadero "no encontrado" con un match que Siesa no haría.

## El CO es parte de la llave (2026-09-27)

La PK documental es CO + tipo + consecutivo + cuota (Regla 18), y en
producción la numeración de las FE **se solapa** entre centros de operación:
FE-17062 existe en el CO 003 y en el CO 004, de clientes distintos (medido el
2026-09-27, solo GET). Quien sabe el CO lo pasa (`co=`); la lectura exacta de
un documento (`get_cxc_de_factura`) se filtra con `fila_unica`, que no elige
entre dos filas.

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


def _co(fila) -> str:
    return str(fila.get('f353_id_co_cruce', '') or '').strip()


def _match(cxc: list, tipo_docto, consec_docto, co=None):
    """La primera fila de ese documento. Con `co`, el CO es parte de la llave
    (Regla 18): la numeración de las FE se solapa entre centros de operación
    (medido en producción el 2026-09-27: FE-17062 existe en el CO 003 y en el
    CO 004, de clientes distintos). Sin `co` (quien no lo sabe) se compara
    como antes — y una fila de OTRO CO que coincida en tipo y consecutivo es
    exactamente la equivocada que esto evita."""
    if not (tipo_docto and consec_docto):
        return None
    tipo = str(tipo_docto).strip()
    consec = str(consec_docto).strip()
    co = str(co).strip() if co else None
    return next((
        r for r in (cxc or [])
        if str(r.get('f353_id_tipo_docto_cruce', '')).strip() == tipo
        and str(r.get('f353_consec_docto_cruce', '')).strip() == consec
        and (co is None or _co(r) == co)
    ), None)


def fila_de_la_factura(cxc: list, tipo_docto_pedido: str, consec_docto_pedido,
                        tipo_docto_fe: str = None, consec_docto_fe=None, co: str = None):
    """La fila de cartera que corresponde a esa factura, o `None`.

    Se busca primero por el **PEDIDO** — es la regla, lo que traen los campos
    de cruce en el caso general (verificado en vivo el 2026-08-11). Si no
    aparece nada y se pasó la FE, se reintenta por FE — la excepción real
    encontrada en PD1411/FE-1416 (2026-08-18). Nunca al revés: el pedido es
    la apuesta correcta la mayoría de las veces, y probarla primero evita que
    un match por FE tape en silencio un "no encontrado" genuino cuando ambos
    identificadores coincidieran por casualidad.

    `co`: el CO del documento (`co_de_la_factura`). Quien lo sabe lo pasa.
    """
    return _match(cxc, tipo_docto_pedido, consec_docto_pedido, co) \
        or _match(cxc, tipo_docto_fe, consec_docto_fe, co)


class FilaAmbigua(ValueError):
    """Más de una fila de cartera para la misma llave documental."""


def fila_unica(filas: list, co, tipo_docto, consec_docto):
    """La fila de ESE documento en una lectura exacta (`get_cxc_de_factura`),
    `None` si no está, o `FilaAmbigua` si hay más de una (dos cuotas, o un
    filtro que no filtró): no se elige una a ciegas. Descarta filas de otro
    CO, tipo o consecutivo aunque la consulta las haya traído."""
    tipo = str(tipo_docto or '').strip()
    consec = str(consec_docto or '').strip()
    co = str(co or '').strip()
    propias = [r for r in (filas or [])
               if str(r.get('f353_id_tipo_docto_cruce', '')).strip() == tipo
               and str(r.get('f353_consec_docto_cruce', '')).strip() == consec
               and _co(r) == co]
    if len(propias) > 1:
        raise FilaAmbigua(
            f'{len(propias)} filas de cartera para {co}-{tipo}-{consec} '
            f'(cuotas: {sorted({str(r.get("f353_nro_cuota_cruce")) for r in propias})})')
    return propias[0] if propias else None


def co_de_la_factura(tarea, centro_op_default: str = None) -> str | None:
    """El CO en que se emitió la factura de la tarea: el de su `pedido_clave`
    (`003-PD-1502`, escrito al crear la tarea: sobrevive a un cambio de
    `CONNEKTA_CENTRO_OP`) o, sin clave, el de la emisión (`CONNEKTA_CENTRO_OP`,
    que es el `F350_ID_CO` del 142943). `None` si no hay ninguno."""
    clave = str(getattr(tarea, 'pedido_clave', None) or '').strip()
    partes = clave.split('-')
    if len(partes) >= 3 and partes[0].isdigit():
        return partes[0]
    if centro_op_default is None:
        from app.services.connekta_gateway import connekta
        centro_op_default = connekta.centro_op
    return str(centro_op_default).strip() if isinstance(centro_op_default, str) \
        and centro_op_default.strip() else None


def saldo_de_la_fila(fila) -> float:
    return float(fila.get('f353_total_db', 0) or 0) - float(fila.get('f353_total_cr', 0) or 0)


def esta_saldada(cxc: list, tipo_docto_pedido: str, consec_docto_pedido,
                  tipo_docto_fe: str = None, consec_docto_fe=None, co: str = None):
    """`True` | `False` | `None`.

    **`None` es el caso que importa** y el que antes se colapsaba a `False`: la
    fila no apareció, así que no se sabe si la factura quedó saldada. No es lo
    mismo que «tiene saldo».

    Quien pregunta después de un POST fallido tiene que distinguirlos: ante «no
    sé», la Regla 3 dice **no reintentar** —un timeout no significa que falló, y
    un recibo de caja duplicado es un documento financiero que alguien tiene que
    reversar a mano—. Ante «tiene saldo», reintentar es correcto.
    """
    fila = fila_de_la_factura(cxc, tipo_docto_pedido, consec_docto_pedido,
                               tipo_docto_fe, consec_docto_fe, co=co)
    if fila is None:
        return None
    return saldo_de_la_fila(fila) <= TOLERANCIA
