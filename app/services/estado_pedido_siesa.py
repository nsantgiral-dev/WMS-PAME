"""Los estados de un pedido en Siesa (`f430_ind_estado`). **Una tabla.**

Hasta el 2026-09-25 cada consumidor de `get_estado_pedido` traía su propia
lectura del número, y dos de ellas decían lo contrario:

| Sitio | Qué creía que era el 9 |
|---|---|
| `pedidos_sync_service` (detección de anulados) | anulado |
| `pedido_closer._precheck_siesa` | anulado (junto con -1 y 5) |
| `pedidos_historia.clasificar_desaparecidas` | anulado |
| `reconciliacion_service._ESTADOS_CUMPLIDO = {'9'}` | **«ya procesado»: RM + FE hechas** |

La cuarta es la que escribía. Con un pedido anulado por comercial mientras la
caja esperaba (retenida por cartera, por ejemplo), el barrido de reconciliación
leía el 9 como «Siesa ya facturó», ponía `siesa_triggered=True` y la tarea
`DESPACHADO` — y el muelle, que exige `siesa_triggered`, dejaba subir los bultos
al camión **sin remisión y sin factura**. Una pregunta, dos respuestas, y la
que actuaba era la equivocada.

Esta tabla no dice qué documentos existen. **Ningún estado de pedido es
evidencia documental**: el 4 (Cumplido) lo pone Siesa al procesar la RM, antes
de que exista la FE (por eso se sacó de la reconciliación); el 9 es anulado.
La evidencia de un documento es el documento con su consecutivo
(`reconciliacion_service`), no un número de estado.

Trinquete: `tests/test_estado_pedido_siesa.py` — todo llamador de
`get_estado_pedido` en `app/` interpreta el resultado con una función de acá.
"""

#: `get_estado_pedido` devuelve -1 cuando la consulta respondió y el pedido no
#: está (eliminado o nunca existió).
NO_ENCONTRADO = -1
EN_ELABORACION = 1
APROBADO = 2
COMPROMETIDO = 3
#: Siesa lo pone al procesar la remisión (antes de que exista la factura).
CUMPLIDO = 4
#: Anulado. **No es «ya procesado»** — ver el encabezado.
ANULADO = 9
#: Anulado según la tabla que traía `packing_service` (legado) y el precheck
#: del cierre. No está verificado en vivo; se trata como anulado porque el
#: error en ese sentido frena una factura, y el otro la emite (Regla 0).
ANULADO_LEGADO = 5

#: Se puede seguir alistando y facturando.
VIVOS = (EN_ELABORACION, APROBADO, COMPROMETIDO)
ANULADOS = (ANULADO, ANULADO_LEGADO)

# Clases que devuelve `clasificar`.
SIN_DATO = 'SIN_DATO'            # no se pudo preguntar (None)
VIVO = 'VIVO'
CLASE_CUMPLIDO = 'CUMPLIDO'
CLASE_ANULADO = 'ANULADO'
CLASE_NO_ENCONTRADO = 'NO_ENCONTRADO'
DESCONOCIDO = 'DESCONOCIDO'      # respondió con un número que esta tabla no conoce


def _entero(estado):
    if estado is None:
        return None
    try:
        return int(str(estado).strip())
    except (TypeError, ValueError):
        return None


def clasificar(estado) -> str:
    """La lectura del número. `None` (no se pudo preguntar) es `SIN_DATO`,
    nunca «no existe»: son cosas distintas y la segunda frena o libera."""
    if estado is None:
        return SIN_DATO
    n = _entero(estado)
    if n is None:
        return DESCONOCIDO
    if n == NO_ENCONTRADO:
        return CLASE_NO_ENCONTRADO
    if n in VIVOS:
        return VIVO
    if n == CUMPLIDO:
        return CLASE_CUMPLIDO
    if n in ANULADOS:
        return CLASE_ANULADO
    return DESCONOCIDO


def es_anulado(estado) -> bool:
    return clasificar(estado) == CLASE_ANULADO


def es_cumplido(estado) -> bool:
    return clasificar(estado) == CLASE_CUMPLIDO


def impide_facturar(estado) -> bool:
    """El pedido respondió y no se puede facturar: anulado o inexistente.
    `SIN_DATO` no impide aquí — quien necesite certeza la pide aparte."""
    return clasificar(estado) in (CLASE_ANULADO, CLASE_NO_ENCONTRADO)


def dejo_de_estar_vivo(estado) -> bool:
    """Respondió y ya no es un pedido en curso ni cumplido: anulado,
    inexistente o un número que esta tabla no conoce. Es lo que la detección
    de anulados del sync marca para revisión. `SIN_DATO` → `False`."""
    return clasificar(estado) in (CLASE_ANULADO, CLASE_NO_ENCONTRADO, DESCONOCIDO)


def motivo_salida(estado) -> str:
    """Para `PedidoHistoria`: cómo salió de «comprometido» un pedido que dejó
    de verse. `None` si no aplica (sigue comprometido o no se sabe)."""
    from app.models.pedido_historia import MotivoSalidaPedido
    clase = clasificar(estado)
    if clase in (SIN_DATO,):
        return None
    if _entero(estado) == COMPROMETIDO:
        return None
    if clase == CLASE_CUMPLIDO:
        return MotivoSalidaPedido.CUMPLIDO
    if clase == CLASE_ANULADO:
        return MotivoSalidaPedido.ANULADO
    return MotivoSalidaPedido.OTRO_ESTADO


def nombre(estado) -> str:
    """Texto para un mensaje o un log."""
    n = _entero(estado)
    return {
        NO_ENCONTRADO: 'no encontrado en Siesa',
        EN_ELABORACION: 'en elaboración',
        APROBADO: 'aprobado',
        COMPROMETIDO: 'comprometido',
        CUMPLIDO: 'cumplido',
        ANULADO: 'anulado',
        ANULADO_LEGADO: 'anulado',
    }.get(n, f'desconocido (código {estado})' if estado is not None else 'sin dato')
