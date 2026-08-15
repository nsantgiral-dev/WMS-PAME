"""
Invariantes del flujo de traslados: solicitud → aprobación → picking → packing
→ despacho (STS) → recepción (ETS).

Es la cadena más larga del WMS y la que más conectores toca: **174646** (RIT),
**173076** (salida en tránsito), **174930** (STS desde RIT), **173079**
(entrada), y **173066** para el modo directo.

## Por qué el limbo es el riesgo propio de este flujo

En modo `EN_TRANSITO` la mercancía sale de la bodega origen a una **bodega
puente** (`TRA1`) y solo llega al destino cuando entra el ETS. Si el STS
disparó y el ETS no, el stock existe —está en tránsito— pero **no está en
ninguna bodega que alguien mire**. No falta, no sobra: está donde nadie
pregunta.

Y a diferencia de una venta, acá nadie reclama: el cliente que no recibió su
mercancía llama; una tienda que no recibió un traslado que no pidió, no.

## Las cantidades bajan, nunca suben

    solicitada ≥ aprobada ≥ enviada ≥ recibida

Cada paso puede recortar —el admin aprueba menos, el picker encuentra menos, la
tienda recibe menos— pero ninguno puede inventar. Una desigualdad al revés es
mercancía que apareció de la nada entre dos etapas.
"""
from app.extensions import db
from app.services.auditoria.base import _AUDITORIA_TRUNCADA,  AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante

_TERMINALES = ('ENTREGADA', 'RECHAZADA', 'CANCELADA', 'REVERTIDA')


def _solicitudes(estados=None, limite=1000):
    """Las solicitudes a auditar, **las más recientes primero**.

    Sin `order_by`, el `.limit()` se llenaba con las más viejas — y el predicado
    de cada invariante corre en Python sobre lo que ya volvió, así que en cuanto
    los traslados pasaran el tope la auditoría dejaba de ver los nuevos. El
    panel diría «0 hallazgos» porque dejó de mirar.
    """
    from app.models.traslado import SolicitudTraslado
    q = SolicitudTraslado.query
    if estados:
        q = q.filter(SolicitudTraslado.estado.in_(estados))
    filas = q.order_by(SolicitudTraslado.id.desc()).limit(limite).all()
    if len(filas) >= limite:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:{limite}')
    return filas


# ── Frontera: aprobación → picking → recepción (cantidades) ──────────────

@invariante(
    codigo='TRA-01',
    flujo='traslados',
    frontera='cantidades entre etapas',
    consecuencia='Se despachó o recibió más de lo aprobado: mercancía que '
                 'aparece de la nada entre dos etapas y descuadra las dos '
                 'bodegas a la vez.',
    severidad=BLOQUEA,
)
def las_cantidades_solo_bajan(ctx=None):
    """`solicitada ≥ aprobada ≥ enviada ≥ recibida`.

    Cada paso puede recortar; ninguno puede inventar. Se comparan solo los
    valores presentes: `aprobada = NULL` significa «todavía no se aprobó», no
    «se aprobó cero».
    """
    from app.models.traslado import ItemSolicitudTraslado
    out = []
    for it in ItemSolicitudTraslado.query.order_by(ItemSolicitudTraslado.id.desc()).limit(5000).all():
        sol = it.cantidad_solicitada or 0
        apr = it.cantidad_aprobada
        env = it.cantidad_enviada or 0
        rec = it.cantidad_recibida or 0
        cadena = [('solicitada', sol)]
        if apr is not None:
            cadena.append(('aprobada', apr))
        cadena += [('enviada', env), ('recibida', rec)]
        for (n1, v1), (n2, v2) in zip(cadena, cadena[1:]):
            if v2 > v1:
                out.append(Hallazgo(
                    referencia=f'solicitud#{it.solicitud_id} / '
                               f'{it.producto_codigo_siesa or it.producto_id}',
                    detalle=f'{n2}={v2} supera {n1}={v1}',
                    datos={'solicitud_id': it.solicitud_id},
                ))
                break
    return out


# ── Frontera: despacho → Siesa ───────────────────────────────────────────

@invariante(
    codigo='TRA-10',
    flujo='traslados',
    frontera='despacho → Siesa',
    consecuencia='El WMS dice despachado y Siesa no tiene el movimiento: el '
                 'stock sigue contado en la bodega origen, que ya no lo tiene.',
    severidad=BLOQUEA,
)
def todo_despacho_tiene_su_salida_en_siesa(ctx=None):
    """Una solicitud que pasó de `EN_TRANSITO` en adelante movió inventario
    físico. Sin `siesa_salida_consec`, ese movimiento no existe en el ERP."""
    return [
        Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            detalle=f'estado {s.estado} sin consecutivo de salida en Siesa'
                    + (f' · error: {s.siesa_error[:120]}' if s.siesa_error else ''),
            datos={'modo': s.modo_transferencia, 'origen': s.bodega_origen_siesa},
        )
        for s in _solicitudes(('EN_TRANSITO', 'ENTREGADA'))
        if not s.siesa_salida_consec
    ]


@invariante(
    codigo='TRA-11',
    flujo='traslados',
    frontera='recepción → Siesa',
    consecuencia='La mercancía quedó en la bodega puente: salió del origen y '
                 'nunca llegó al destino. No falta ni sobra — está donde nadie '
                 'pregunta.',
    severidad=BLOQUEA,
)
def nada_se_queda_en_la_bodega_puente(ctx=None):
    """El limbo propio de este flujo.

    En modo `EN_TRANSITO` el STS deja el stock en `TRA1` y solo el ETS lo pone
    en el destino. Una solicitud ENTREGADA sin `siesa_entrada_consec` dice que
    la tienda la recibió mientras el ERP la tiene en la bodega puente.

    Y nadie reclama: el cliente que no recibe su pedido llama; una tienda que
    no recibió un traslado que no pidió, no.
    """
    return [
        Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            detalle=f'ENTREGADA sin entrada en Siesa — el stock quedó en '
                    f'{s.bodega_transito_siesa or "la bodega de tránsito"}',
            datos={'destino': s.bodega_destino_siesa,
                   'salida': s.siesa_salida_consec},
        )
        for s in _solicitudes(('ENTREGADA',))
        if s.modo_transferencia == 'EN_TRANSITO' and not s.siesa_entrada_consec
    ]


@invariante(
    codigo='TRA-12',
    flujo='traslados',
    frontera='despacho → Siesa',
    consecuencia='Hay un error de Siesa registrado y nadie lo va a mirar: la '
                 'solicitud avanzó igual y el tablero la da por buena.',
    severidad=AVISA,
)
def un_error_de_siesa_no_se_queda_callado(ctx=None):
    """`siesa_error` con movimiento de cierre presente es ruido histórico —el
    reintento funcionó—. Sin cierre y sin estado terminal, es trabajo pendiente
    que nadie tiene asignado."""
    # Se agrupa por CAUSA, no por solicitud.
    #
    # La primera corrida devolvió 53 filas y ninguna se podía triar: cincuenta
    # y tres traslados distintos con —probablemente— dos o tres errores
    # distintos repetidos. Un hallazgo por fila convierte un problema en una
    # lista, y una lista larga se ignora.
    #
    # La firma es el mensaje sin los identificadores que cambian entre
    # documentos: lo que queda es la causa.
    import re as _re
    grupos = {}
    for s in _solicitudes():
        if not s.siesa_error or s.estado in _TERMINALES:
            continue
        cierre = (s.siesa_entrada_consec if s.modo_transferencia == 'EN_TRANSITO'
                  else s.siesa_salida_consec)
        if cierre:
            continue
        firma = _re.sub(r'\d+', 'N', (s.siesa_error or '')[:220])
        grupos.setdefault(firma, []).append(s)

    return [
        Hallazgo(
            referencia=f'{len(ss)} traslado(s) · ej. {ss[0].codigo or ss[0].id}',
            detalle=firma,
            datos={'codigos': [x.codigo for x in ss[:15]],
                   'estados': sorted({x.estado for x in ss})},
        )
        for firma, ss in sorted(grupos.items(), key=lambda kv: -len(kv[1]))
    ]


# ── Frontera: estado ↔ documentos ────────────────────────────────────────

@invariante(
    codigo='TRA-20',
    flujo='traslados',
    frontera='estado ↔ documentos',
    consecuencia='Una solicitud cancelada que ya movió inventario en Siesa: el '
                 'ERP tiene el movimiento y el WMS lo da por no ocurrido.',
    severidad=BLOQUEA,
)
def una_cancelada_no_movio_inventario(ctx=None):
    """Cancelar después de despachar deja las dos bases en desacuerdo, y el
    desacuerdo es del lado peor: el WMS cree que no pasó nada."""
    return [
        Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            detalle=f'{s.estado} pero tiene salida en Siesa '
                    f'({s.siesa_salida_consec})',
            datos={'entrada': s.siesa_entrada_consec},
        )
        for s in _solicitudes(('CANCELADA', 'RECHAZADA'))
        if s.siesa_salida_consec
    ]


@invariante(
    codigo='TRA-21',
    flujo='traslados',
    frontera='recepción → Siesa',
    consecuencia='Entró al destino algo que nunca salió del origen: el ETS sin '
                 'su STS duplica stock en el ERP.',
    severidad=BLOQUEA,
)
def no_hay_entrada_sin_salida(ctx=None):
    return [
        Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            detalle=f'entrada {s.siesa_entrada_consec} sin salida previa',
            datos={'estado': s.estado},
        )
        for s in _solicitudes()
        if s.siesa_entrada_consec and not s.siesa_salida_consec
    ]


@invariante(
    codigo='TRA-30',
    flujo='traslados',
    frontera='despacho → recepción',
    consecuencia='Traslados despachados que llevan días sin recibirse. Cada '
                 'uno es stock que el origen ya no tiene y el destino todavía '
                 'no cuenta.',
    severidad=OBSERVA,
)
def se_pueden_contar_los_traslados_en_vuelo(ctx=None):
    """No es un defecto —un traslado tarda— pero sin poder contarlos, uno
    atascado hace semanas se ve igual que uno de ayer."""
    from app.utils.fecha import ahora_bogota
    hoy = ahora_bogota().date()
    out = []
    for s in _solicitudes(('EN_TRANSITO',)):
        ref = s.fecha_despacho.date() if s.fecha_despacho else None
        dias = (hoy - ref).days if ref else None
        out.append(Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            detalle=(f'{dias} día(s) en tránsito' if dias is not None
                     else 'en tránsito sin fecha de despacho'),
            datos={'origen': s.bodega_origen_siesa,
                   'destino': s.bodega_destino_siesa},
        ))
    return out
