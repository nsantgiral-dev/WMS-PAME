"""
Qué variables de Siesa son obligatorias — **una sola lista**.

Antes había dos, y ninguna tenía la que costó más caro:

  · `connekta_gateway.__init__` validaba 4 al arrancar (COND_PAGO_VENTAS,
    MOTIVO_TRASLADO, LISTA_PRECIO, UNIDAD_NEGOCIO).
  · `health._VARS_CRITICAS` reportaba 9, y **LISTA_PRECIO no estaba** entre
    ellas pese a ser obligatoria según la otra lista.

Mientras tanto el gateway tiene **14 variables con guard de fallo duro**. El
health miraba 9 y solo 4 coincidían con la validación de arranque: diez
variables podían reventar en producción con `/api/health/siesa` respondiendo
`ok`. Es el corolario de la Regla 0 —una política, una función— en su forma
más cara: no divergieron por un bug, divergieron por existir dos veces.

## El incidente que lo destapó (2026-08-08)

159 jobs en FALLIDO. **93 de ellos**, un 58% del total, con el mismo mensaje:

    SIESA_TIPO_DOCTO_AJUSTE no está configurado en variables de entorno.

Esa variable **tiene default** (`os.getenv('SIESA_TIPO_DOCTO_AJUSTE', 'ADI')`).
Si hubiera estado ausente, el default la habría llenado y el guard no habría
disparado nunca. Para que dispare tiene que estar **declarada con valor vacío**:
el string vacío le gana al default en silencio.

Ningún conteo cíclico llegó a Siesa entre abril y junio de 2026. Nadie se
enteró porque el health no miraba esa variable, y porque el único síntoma
—jobs acumulándose en el DLQ— no tiene a quién avisarle.

## Las dos formas de estar mal, que no son la misma

**`VACIA`** — declarada con valor vacío. Es un error **siempre**, incluso en
variables con default: quien la declaró tenía la intención de darle un valor,
y el vacío desactiva el default sin decirlo. No tiene falsos positivos.

**`FALTA`** — ausente y sin default usable. El guard va a disparar la primera
vez que alguien ejercite esa operación.

Una variable ausente **con** default usable es `ok` y no se reporta. Reportarla
sería ruido, y el ruido es cómo se pierde el aviso que sí importa: 639 avisos
conocidos hicieron invisible el único real. Un canal de advertencias solo sirve
mientras se pueda leer entero.
"""
import os
from typing import List, NamedTuple, Optional


class VarCritica(NamedTuple):
    """Una variable de entorno que puede tumbar una operación de Siesa.

    `default` es el valor que usa el gateway cuando la variable no está. `None`
    significa que no hay default usable —el atributo queda en `''` o `None`— y
    por lo tanto su ausencia dispara el guard.

    `rompe` no es decorativo: es lo que lee quien está mirando el health a las
    6 a.m. sin conocer el código. Un nombre de variable no le dice si puede
    despachar.
    """
    nombre: str
    default: Optional[str]
    rompe: str
    #: `True` = su guard solo dispara en una situación de datos concreta (la
    #: API no devolvió el campo), no en toda ejecución. Su ausencia NO se
    #: reporta: el camino normal es que el dato venga del pedido, y avisar
    #: siempre convertiría el health en ruido. Declarada vacía sigue siendo
    #: error, como todas.
    condicional: bool = False


#: Catálogo único. Toda variable con guard de fallo duro en
#: `connekta_gateway.py` tiene que estar acá — hay un trinquete que lo exige
#: (`tests/test_vars_criticas.py`), así que agregar un guard nuevo sin
#: declararlo acá rompe el build.
VARS_CRITICAS: tuple = (
    # ── Sin default: su ausencia dispara el guard ────────────────────────────
    VarCritica('SIESA_MOTIVO_TRASLADO', None,
               'Transferencias 173066/173076/174646 — motivo inválido es rechazo duro'),
    VarCritica('SIESA_TIPO_DOCTO_TRANSITO_SALIDA', None,
               'Despacho de traslado (STS, clase 65) — conectores 173076 y 174930'),
    VarCritica('SIESA_TIPO_DOCTO_TRANSITO_ENTRADA', None,
               'Recepción de traslado (ETS, clase 66) — conector 173079'),
    VarCritica('SIESA_BODEGA_TRANSITO', None,
               'Bodega puente de los traslados inter-bodega (ej. TRA1)'),
    VarCritica('SIESA_UBICACION_ENTRADA_DEFAULT', None,
               'Ubicación ancla en destino para 173079 en bodegas multi-ubicación '
               '— usar REC en todas las sedes'),
    VarCritica('SIESA_UNIDAD_NEGOCIO', None,
               'Traslados 173076/173079 — Siesa NO la hereda de la bodega'),
    VarCritica('SIESA_COND_PAGO_VENTAS', None,
               'El código de CONTADO. No se emite: se reconoce para NO emitirlo — '
               'una FE de contado no se aprueba sin recaudo (ver cond_pago.aprobable_en_ruta)'),
    VarCritica('SIESA_COND_PAGO_RUTA', None,
               'La condición que lleva la FE de ruta si el pedido no trae ninguna: '
               'crédito a un día, que el recibo de caja del conductor salda'),
    VarCritica('SIESA_LISTA_PRECIO', None,
               'Remisión 142945 (f470_id_lista_precio) — sin ella el documento sale sin precios'),
    VarCritica('SIESA_TIPO_DOCTO_REMISION', None,
               'Remisión 142945 — el cierre de packing completo y parcial'),
    VarCritica('SIESA_ID_MOTIVO_VENTAS', None,
               'Remisión 142945 y nota crédito 142946 — motivo de venta/devolución'),
    VarCritica('SIESA_TIPO_DOCTO_ENTRADA_OC', None,
               'Entrada por orden de compra — conector 142948'),

    # ── Con default: solo fallan si se declaran VACÍAS ───────────────────────
    #
    # `SIESA_TIPO_DOCTO_AJUSTE` encabeza la lista porque es la que costó 93
    # jobs. Su default es 'ADI' y aun así estuvo rota dos meses.
    VarCritica('SIESA_TIPO_DOCTO_AJUSTE', 'ADI',
               'Ajuste físico tras conteo cíclico — conector 142951 (AJ-ENT/AJ-SAL)'),
    VarCritica('SIESA_TIPO_DOCTO_TRASLADO', 'TRA',
               'Transferencia de averías y reposición 173066'),
    VarCritica('SIESA_TIPO_DOCTO_RIT', 'TRA',
               'Requisición de traslado — 174646 y 174930 (cae a TIPO_DOCTO_TRASLADO)'),
    VarCritica('SIESA_TIPO_DOCTO_FACTURA', 'FEW',
               'Factura electrónica desde remisión — conector 142943'),
    VarCritica('SIESA_TIPO_DOCTO_NOTA_CREDITO', 'NCE',
               'Nota crédito de devolución — conectores 142946 y 251126'),
    VarCritica('SIESA_TIPO_DOCTO_RECIBO_CAJA', 'RC',
               'Recibo de caja de liquidación de ruta — conector 142888'),
    VarCritica('SIESA_TIPO_DOCTO_DOCTO_CONTABLE', 'DC',
               'Documento contable de retenciones — conector 142882'),
    VarCritica('SIESA_ID_CIA', '1',
               'F_CIA de TODO payload. Si queda en 8215 (el tenant Connekta) '
               'no hay POST que pase'),

    # ── Guard condicional: solo dispara si la API no trae el dato ────────────
    VarCritica('SIESA_PUNTO_ENVIO_DEFAULT', None,
               'Fallback de punto de envío para 142943, solo cuando '
               'API_v2_Ventas_Pedidos no devuelve el campo',
               condicional=True),
)

#: Estados posibles. `ok` no se reporta — el health solo muestra lo que hay que
#: arreglar, y una lista donde todo aparece es una lista que nadie lee.
OK = 'ok'
VACIA = 'VACIA'
FALTA = 'FALTA'


def estado(var: VarCritica) -> str:
    """`ok` | `VACIA` | `FALTA` para una variable del catálogo.

    El orden de las dos preguntas importa. Si se preguntara primero "¿está?" y
    después "¿tiene valor?", una variable declarada vacía se reportaría como
    presente — que es exactamente lo que pasó con `SIESA_TIPO_DOCTO_AJUSTE`
    durante dos meses.
    """
    crudo = os.environ.get(var.nombre)
    if crudo is not None and not crudo.strip():
        return VACIA                      # declarada en blanco: siempre mal
    if crudo is None:
        if var.default or var.condicional:
            return OK
        return FALTA
    return OK


def valor_efectivo(var: VarCritica) -> Optional[str]:
    """Lo que el gateway va a usar de verdad — no lo que dice la variable.

    Devuelve `None` cuando no hay valor utilizable. Existe para que el health
    muestre el default heredado en vez de un hueco: ver `SIESA_TIPO_DOCTO_AJUSTE`
    vacío y a la vez `ADI` como valor efectivo es lo que hace obvio que el
    problema es la declaración, no la falta.
    """
    crudo = os.environ.get(var.nombre)
    if crudo and crudo.strip():
        return crudo.strip()
    return var.default


def problemas() -> List[dict]:
    """Las variables que están mal, con qué rompe cada una. Vacío = todo bien.

    Es la fuente única para el health Y para la validación de arranque del
    gateway. Que sean el mismo llamado es el punto: dos listas divergieron una
    vez y la que se quedó corta fue la que nadie estaba mirando.
    """
    salida = []
    for var in VARS_CRITICAS:
        e = estado(var)
        if e == OK:
            continue
        salida.append({
            'variable': var.nombre,
            'estado': e,
            'rompe': var.rompe,
            'valor_efectivo': valor_efectivo(var),
            'detalle': (
                f'{var.nombre} está DECLARADA CON VALOR VACÍO. El string vacío '
                f'desactiva el default ({var.default!r}) sin avisar — borrala '
                f'del todo o ponele un valor.'
                if e == VACIA else
                f'{var.nombre} no está configurada y no tiene default usable.'
            ),
        })
    return salida


def nombres_faltantes() -> List[str]:
    """Solo los nombres — para el log de arranque, que no tiene espacio."""
    return [p['variable'] for p in problemas()]


__all__ = [
    'VarCritica', 'VARS_CRITICAS', 'OK', 'VACIA', 'FALTA',
    'estado', 'valor_efectivo', 'problemas', 'nombres_faltantes',
]
