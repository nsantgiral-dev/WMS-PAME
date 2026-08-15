"""
Bodega → Centro de Operación. **Una sola copia, una sola función.**

## Por qué existe este archivo

El 2026-08-14 la relación bodega↔CO estaba escrita como diccionario literal en
**tres** módulos, con el mismo nombre (`_BODEGA_CO_MAP`) y tres contenidos
distintos:

    app/routes/tienda_oc.py          10 bodegas   ← completo
    app/routes/traslados.py           9 bodegas   ← sin FP1
    app/services/traslado_service.py  8 bodegas   ← sin FP1 ni NS2

El trinquete (`tests/test_bodegas_coherentes.py`) estaba en verde con las tres
divergiendo, porque **leía solo la primera** — y su docstring afirmaba que era
«el único sitio del código con las 10». Era el único *completo*, no el único que
*existía*. Un guard que mide una copia cuando la propiedad es «todas coinciden»
es un guard en verde sobre algo roto.

Lo que costaba la copia de 8: `_BODEGA_CO_MAP.get('NS2')` devuelve `None`, y
`transferencia_transito_entrada` hace `co_destino or self.centro_op` — así que
el ETS 173079 hacia NS2 o FP1 se armaba con **CO 003** y `bodega_entrada` NS2.
Siesa valida `CO(bodega_entrada) == CO(documento)` (errores 46089/46090): el
documento se rechaza y la mercancía se queda en la bodega de tránsito, que es
justo el limbo que los invariantes de traslado existen para detectar.

## Por qué la tabla `almacenes` no bastaba

`almacenes.centro_op_siesa` es la autoridad real —la lee `_co_de_bodega` en el
gateway— pero la migración que iba a poblarla quedó como no-op:

    migrations/versions/c1d2e3f4g5h6_set_centro_op_siesa_almacenes.py
    \"\"\"stub: centro_op_siesa ya manejado por dict en código\"\"\"

Ese es el día en que el maestro dejó de ser el maestro, sin que nadie lo
declarara. Todo lo demás es consecuencia.

## Cómo se resuelve acá

`co_de_bodega()` **lee `almacenes` primero** y cae a `BODEGA_CO` si esa fila no
existe o no tiene CO. Cuando las dos fuentes discrepan lo grita en el log.

Es un paso intermedio a propósito, no el destino: mientras el log salga limpio
en producción, `almacenes` puede quedar como única autoridad y `BODEGA_CO`
pasar a ser solo el valor certificado contra el que se contrasta el alta. Poner
el fail-fast hoy, sin haber medido qué almacenes están mal cargados, sería
romper traslados que hoy funcionan para arreglar dos que no — que es la lección
de «validar contra producción real» al revés.
"""
import logging

logger = logging.getLogger(__name__)

#: Bodega → CO. Fuente: maestro de Siesa + `CO PAME.xlsx`, certificado por el
#: consultor. Estático: no cambia salvo que entre una bodega nueva.
#:
#: NO incluye las bodegas de servicio (`AV1` averías, `TRA1` tránsito, `BC99`
#: contratación) ni las duplicadas de Siesa (`FD1`, `ND1`, `PD1`). El CO 999
#: (ADMINISTRATIVO) es contable y no lleva almacén en el WMS.
BODEGA_CO = {
    'NS1': '001',   # NEIVA SUR PRINCIPAL
    'NS2': '001',   # NEIVA SUR FUNDACIÓN — parqueo de licitaciones, no es PV
    'NC1': '002',   # NEIVA CENTRO
    'NB1': '003',   # NEIVA BODEGA CD
    'PC1': '004',   # PITALITO CENTRO
    'PT1': '005',   # PITALITO TERMINAL
    'FC1': '006',   # FLORENCIA CENTRO
    'FN1': '007',   # FERIA NEIVA — es Santa Lucía Plaza, opera todo el año
    'FP1': '008',   # FERIA PITALITO
    'FF1': '009',   # FERIA FLORENCIA
}


def co_de_bodega(bodega_siesa_id, por_defecto=None):
    """CO Siesa de una bodega. **El único sitio que contesta esta pregunta.**

    Orden: `almacenes.centro_op_siesa` (autoridad) → `BODEGA_CO` (certificado)
    → `por_defecto`.

    Devuelve `None` (o `por_defecto`) si la bodega es desconocida en las dos
    fuentes. **No levanta excepción**: hoy ese caso ya produce un documento que
    Siesa rechaza, y convertirlo en un 500 a días de un corte cambia el modo de
    fallo de un flujo que no se ha medido. Lo que sí hace es dejarlo en el log
    como `CRITICAL`, que es lo que hoy no pasaba.

    Args:
        bodega_siesa_id: código de bodega Siesa ('NB1', 'NS2', ...).
        por_defecto: qué devolver si no se pudo resolver.
    """
    if not bodega_siesa_id:
        return por_defecto

    bodega = str(bodega_siesa_id).strip().upper()
    certificado = BODEGA_CO.get(bodega)

    co_almacen = None
    try:
        from app.models.almacen import Almacen
        alm = Almacen.query.filter_by(bodega_siesa_id=bodega).first()
        if alm and alm.centro_op_siesa:
            co_almacen = str(alm.centro_op_siesa).strip()
    except Exception as e:
        # Sin contexto de aplicación, sin tablas todavía, o consulta imposible.
        # No es motivo para no contestar: para eso está el certificado.
        logger.debug('[BODEGAS] no se pudo leer almacenes para %s: %s', bodega, e)

    if co_almacen and certificado and co_almacen != certificado:
        # La divergencia importa más que cuál gana: significa que alguien cargó
        # un almacén a mano con el CO equivocado, y eso factura contra otra sede.
        logger.critical(
            '[BODEGAS] %s: almacenes dice CO=%s y el maestro certificado dice '
            'CO=%s. Gana almacenes. Corregir la fila de almacenes o el maestro '
            '— mientras discrepen, los documentos de esa bodega son sospechosos.',
            bodega, co_almacen, certificado,
        )

    if co_almacen:
        return co_almacen

    if certificado:
        if bodega != 'NB1':
            # NB1 coincide con el default del gateway, así que su ausencia no
            # cambiaba nada. Para el resto, esta rama es exactamente el caso que
            # antes devolvía el CO equivocado.
            logger.warning(
                '[BODEGAS] %s no tiene almacén con centro_op_siesa — se usa el '
                'maestro certificado (CO=%s). Configurar el almacén.',
                bodega, certificado,
            )
        return certificado

    logger.critical(
        '[BODEGAS] bodega %s desconocida: no está en almacenes ni en el maestro '
        'certificado. Todo documento Siesa que la use va a salir con el CO '
        'equivocado o va a ser rechazado.', bodega,
    )
    return por_defecto
