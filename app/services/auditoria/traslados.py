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


def _mas_nueva(fechas):
    """La fecha del miembro más nuevo de un hallazgo agregado. Si alguno no
    tiene fecha, `None`: el agregado cuenta como vigente (Regla 0)."""
    fechas = list(fechas)
    if not fechas or any(f is None for f in fechas):
        return None
    return max(fechas)


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
    # Desde `ck_traslado_cadena_no_crece` (migración m013, 2026-08-19) la
    # violación **no se puede escribir**: el CHECK la rechaza en el commit. Por
    # eso el detector ciego ya no puede construirla persistiendo una fila, y
    # apunta al test que prueba que la base la rechaza — que es estrictamente
    # más fuerte que probar que el auditor la ve después.
    #
    # El invariante se conserva porque **sigue cubriendo las filas escritas
    # antes del CHECK**, que la migración no reescribe: son las que este
    # invariante venía reportando y que alguien tiene que corregir con el
    # conteo físico en la mano.
    detector_ciego='tests/flujo/test_flujo_traslados.py::TestElDetectorNoEstaCiego::test_la_base_impide_enviar_mas_de_lo_aprobado',
    # Las filas anteriores al CHECK de m013 son las únicas que pueden violarlo:
    # huella del defecto, no un error de hoy.
    defecto_corregido=('d5e78970', '2026-08-20T06:57:39-05:00'),
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
                    fecha=it.solicitud.fecha_creacion if it.solicitud else None,
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
    detector_ciego='tests/flujo/test_flujo_traslados.py::TestElDetectorNoEstaCiego::test_ve_un_despacho_sin_salida_en_siesa',
)
def todo_despacho_tiene_su_salida_en_siesa(ctx=None):
    """Una solicitud que pasó de `EN_TRANSITO` en adelante movió inventario
    físico. Sin `siesa_salida_consec`, ese movimiento no existe en el ERP."""
    return [
        Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            fecha=s.fecha_creacion,
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
    detector_ciego='tests/flujo/test_flujo_traslados.py::TestElDetectorNoEstaCiego::test_ve_la_mercancia_atrapada_en_la_bodega_puente',
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
            fecha=s.fecha_creacion,
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
    detector_ciego='tests/flujo/test_flujo_traslados.py::TestElDetectorNoEstaCiego::test_ve_un_error_de_siesa_sin_resolver',
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
            # Agregado: vigente si ALGUNO de sus miembros lo es (la fecha del
            # más nuevo). Mostrar de más, no de menos.
            fecha=_mas_nueva(x.fecha_creacion for x in ss),
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
    detector_ciego='tests/flujo/test_flujo_traslados.py::TestElDetectorNoEstaCiego::test_ve_una_cancelada_que_ya_movio_inventario',
)
def una_cancelada_no_movio_inventario(ctx=None):
    """Cancelar después de despachar deja las dos bases en desacuerdo, y el
    desacuerdo es del lado peor: el WMS cree que no pasó nada."""
    return [
        Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            fecha=s.fecha_creacion,
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
    detector_ciego='tests/flujo/test_flujo_traslados.py::TestElDetectorNoEstaCiego::test_ve_una_entrada_sin_salida',
)
def no_hay_entrada_sin_salida(ctx=None):
    return [
        Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            fecha=s.fecha_creacion,
            detalle=f'entrada {s.siesa_entrada_consec} sin salida previa',
            datos={'estado': s.estado},
        )
        for s in _solicitudes()
        if s.siesa_entrada_consec and not s.siesa_salida_consec
    ]


#: Días en tránsito a partir de los cuales un traslado deja de parecerse a la
#: operación normal. Medido el 2026-09-17 sobre los traslados ENTREGADA de
#: producción que tienen las dos fechas: **promedio 7,6 días, máximo 15,3**.
#: Un umbral de 2 o 3 días marcaría como problema la mitad de los despachos
#: sanos — y un canal donde casi todo es aviso deja de leerse.
DIAS_EN_TRANSITO_ANORMAL = 16


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
    atascado hace semanas se ve igual que uno de ayer.

    Antes emitía un hallazgo por CADA traslado en vuelo. Medido el 2026-09-17:
    19 en tránsito, 19 hallazgos, todos los días. Eso no es un detector: es
    ruido que entierra al que sí está atascado.

    Ahora emite dos cosas distintas:

      · uno por cada traslado que pasó el umbral — esos son los que alguien
        tiene que mirar;
      · UNO SOLO, agregado, por los que no tienen fecha de despacho. No es la
        misma pregunta: «no sé hace cuánto salió» es un problema del registro,
        no del traslado, y 19 líneas repitiéndolo no agregan información.

    El sin-fecha tiene causa conocida y arreglada hacia adelante: hasta el
    2026-09-16 el despacho por cierre de packing no escribía `fecha_despacho`
    (solo lo hacía el botón del admin). Los 19 que hoy están así salieron por
    ese camino y **no se pueden recuperar** — no hay de dónde deducir la fecha
    sin inventarla.
    """
    from app.utils.fecha import ahora_bogota, dia_operativo_de
    hoy = ahora_bogota().date()
    out = []
    sin_fecha, nacidos = [], []
    for s in _solicitudes(('EN_TRANSITO',)):
        if not s.fecha_despacho:
            sin_fecha.append(s.codigo or f'traslado#{s.id}')
            nacidos.append(s.fecha_creacion)
            continue
        dias = (hoy - dia_operativo_de(s.fecha_despacho)).days
        if dias < DIAS_EN_TRANSITO_ANORMAL:
            continue
        out.append(Hallazgo(
            referencia=s.codigo or f'traslado#{s.id}',
            fecha=s.fecha_creacion,
            detalle=f'{dias} día(s) en tránsito',
            datos={'origen': s.bodega_origen_siesa,
                   'destino': s.bodega_destino_siesa,
                   'dias': dias, 'umbral': DIAS_EN_TRANSITO_ANORMAL},
        ))
    if sin_fecha:
        out.append(Hallazgo(
            referencia='sin-fecha-de-despacho',
            detalle=(f'{len(sin_fecha)} traslado(s) en tránsito sin '
                     f'`fecha_despacho`: no se puede saber hace cuánto salieron. '
                     f'Causa conocida —el despacho por cierre de packing no la '
                     f'escribía hasta el 2026-09-16— y sin arreglo retroactivo.'),
            datos={'codigos': sin_fecha[:20], 'total': len(sin_fecha)},
            fecha=_mas_nueva(nacidos),
        ))
    return out


# ── Frontera: recepción → dictamen (averías) ─────────────────────────────

@invariante(
    codigo='TRA-31',
    flujo='traslados',
    frontera='recepción → dictamen de avería',
    consecuencia='Mercancía declarada averiada que llegó al CD y nadie '
                 'dictaminó. Hasta que alguien decida, el sync la repuebla '
                 'desde la existencia de Siesa como stock VENDIBLE — o sea '
                 'que se está ofreciendo para venta.',
    severidad=AVISA,
)
def toda_averia_recibida_termina_dictaminada(ctx=None):
    """El limbo propio del flujo de averías, y la razón por la que no es
    cosmético.

    ## Qué pasa mientras nadie dictamina

    `confirmar_recepcion` no escribe stock en el destino —ningún traslado lo
    hace, el sync lo repuebla desde Siesa— y el ETS 173079 ya metió esas
    unidades en la existencia normal de la bodega en el ERP. Así que la
    mercancía que alguien declaró rota aparece en el pool vendible del CD y
    se puede despachar a un cliente.

    El dictamen es lo que la saca de ahí: confirmada va a la zona de averías
    (que el FEFO no toca) y sale el documento a AV1; no averiada se queda,
    pero porque alguien lo decidió.

    ## Por qué no tiene umbral de días

    Regla 13: ningún número sin base y sin fecha. Hoy en producción hay **cero**
    traslados de clase AVERIAS —el flujo se acaba de construir— así que
    cualquier umbral que escribiera sería una opinión disfrazada de medición.
    TRA-30 tiene 16 días porque se midieron 71 traslados entregados; acá no hay
    qué medir todavía.

    Mientras tanto se reportan **todos** los pendientes, en UN hallazgo
    agregado con sus edades. Con cero averías esto no emite nada, así que no
    ensucia el canal; cuando haya volumen, la lista de edades es exactamente
    el dato con el que alguien podrá fijar el umbral con base.

    ## Por qué agregado y no uno por traslado

    Misma lección que TRA-30: 19 hallazgos diarios repitiendo lo mismo no son
    un detector, son ruido que entierra al que importa. La pregunta acá es una
    sola —«¿hay averías sin decidir, y desde cuándo?»— y se contesta una vez.
    """
    from app.models.traslado import ClaseTraslado
    from app.utils.fecha import ahora_bogota, dia_operativo_de

    hoy = ahora_bogota().date()
    pendientes = []
    for s in _solicitudes(('ENTREGADA',)):
        if s.clase_traslado != ClaseTraslado.AVERIAS:
            continue
        # `is not None` y no truthiness: `False` («no estaba averiada») es un
        # dictamen dado, no un pendiente. Con `if s.averia_veredicto:` este
        # invariante gritaría para siempre sobre traslados ya resueltos.
        if s.averia_veredicto is not None:
            continue
        dias = (hoy - dia_operativo_de(s.fecha_entrega)).days if s.fecha_entrega else None
        pendientes.append({
            '_nacio': s.fecha_creacion,
            'codigo': s.codigo or f'traslado#{s.id}',
            'origen': s.bodega_origen_siesa,
            'destino': s.bodega_destino_siesa,
            'dias_desde_entrega': dias,
        })

    if not pendientes:
        return []

    nacidos = [p.pop('_nacio') for p in pendientes]
    _con_dias = [p['dias_desde_entrega'] for p in pendientes
                 if p['dias_desde_entrega'] is not None]
    _viejo = max(_con_dias) if _con_dias else None
    return [Hallazgo(
        referencia='averias-sin-dictaminar',
        detalle=(
            f'{len(pendientes)} traslado(s) de averías recibido(s) en el CD y '
            f'sin dictaminar'
            + (f' — el más viejo lleva {_viejo} día(s)' if _viejo is not None else '')
            + '. Hasta que alguien decida, esas unidades cuentan como vendibles.'),
        datos={'total': len(pendientes), 'pendientes': pendientes[:20],
               'dias_mas_viejo': _viejo},
        fecha=_mas_nueva(nacidos),
    )]
