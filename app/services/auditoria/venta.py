"""
Invariantes del flujo de venta: pedido → picking → packing → bultos → ruta →
entrega → liquidación.

Cada uno vigila **una frontera**. La pregunta siempre es la misma: lo que entró
a esta etapa, ¿es lo que salió de la anterior?

Están escritos como consultas sobre lo que ya hay en la base, no sobre un
objeto que alguien les pase. Eso permite correrlos contra un flujo sintético en
CI y contra los datos reales con el mismo código.
"""
from app.extensions import db
from app.services.auditoria.base import AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante


def _norm(s):
    """Para comparar nombres sin pelear con mayúsculas ni espacios dobles."""
    return ' '.join((s or '').upper().split())


# ── Frontera 1: Siesa → PedidoSiesa ──────────────────────────────────────

@invariante(
    codigo='VTA-01',
    flujo='venta',
    frontera='Siesa → pedido',
    consecuencia='El operario ve «Producto None» al empacar y nadie sabe qué '
                 'ítem es. El pedido no se puede alistar completo.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_un_item_sin_producto_local',
)
def toda_linea_del_pedido_tiene_producto_local(ctx=None):
    """`pedidos_sync_service.py:145` — `producto_id = prod.id if prod else None`.

    Un ítem que Siesa manda y el catálogo local no tiene queda en `None`, **sin
    contador y sin alerta**. Y en `packing_service.py:188` eso se convierte en
    `f'Producto {item.producto_id}'` → literalmente «Producto None» en la
    pantalla del empacador.

    Lo que confirma que es un descuido y no una decisión: el sync de códigos de
    barras, el de empaques, temporada e inventario **todos cuentan sus
    `sin_producto`**. El de pedidos —el único que alimenta picking y packing—
    es el que no lo cuenta.
    """
    from app.models.pedido_siesa import PedidoSiesa
    filas = (PedidoSiesa.query
             .filter(PedidoSiesa.producto_id.is_(None))
             .filter(PedidoSiesa.cantidad_pendiente > 0)
             .order_by(PedidoSiesa.id.desc()).limit(500).all())
    return [
        Hallazgo(
            referencia=f'{p.numero_pedido} / {p.item_codigo}',
            detalle=f'«{p.item_descripcion or "sin descripción"}» no existe en '
                    f'el catálogo local del WMS',
            datos={'item_codigo': p.item_codigo, 'cliente': p.cliente,
                   'cantidad_pendiente': p.cantidad_pendiente},
        ) for p in filas
    ]


@invariante(
    codigo='VTA-02',
    flujo='venta',
    frontera='Siesa → pedido',
    consecuencia='El pedido dice un producto y el operario busca otro. Se '
                 'alista lo que no era y el cliente recibe lo que no pidió.',
    severidad=AVISA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_un_nombre_que_no_coincide',
)
def el_nombre_del_pedido_coincide_con_el_del_catalogo(ctx=None):
    """Siesa llama al ítem de una forma y el WMS de otra.

    `PedidoSiesa.item_descripcion` es como lo llama Siesa; `Producto.nombre` es
    lo que ve el operario en picking y packing. Los dos están en la base desde
    siempre y **nadie los compara nunca**.

    Es `AVISA` y no `BLOQUEA` porque el `codigo_siesa` es el que manda: si
    coincide, el ítem físico es el correcto aunque el rótulo difiera. Lo que se
    rompe es la confianza del operario, que compara contra el papel.
    """
    from app.models.pedido_siesa import PedidoSiesa
    from app.models.producto import Producto

    filas = (db.session.query(PedidoSiesa, Producto)
             .join(Producto, PedidoSiesa.producto_id == Producto.id)
             .filter(PedidoSiesa.cantidad_pendiente > 0)
             .order_by(PedidoSiesa.id.desc()).limit(1000).all())
    out = []
    for p, prod in filas:
        a, b = _norm(p.item_descripcion), _norm(prod.nombre)
        if not a or not b or a == b:
            continue
        # Uno contenido en el otro es truncamiento, no divergencia: Siesa corta
        # la descripción a 200 y algunos maestros la traen abreviada.
        if a in b or b in a:
            continue
        out.append(Hallazgo(
            referencia=f'{p.numero_pedido} / {p.item_codigo}',
            detalle=f'Siesa: «{p.item_descripcion}» · WMS: «{prod.nombre}»',
            datos={'item_codigo': p.item_codigo},
        ))
    return out


# ── Frontera 2: pedido → picking ─────────────────────────────────────────

@invariante(
    codigo='VTA-10',
    flujo='venta',
    frontera='pedido → picking',
    consecuencia='Se pickea más de lo que el cliente pidió. El sobrante sale '
                 'del inventario y no está en ninguna factura.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_que_se_pickeo_de_mas',
)
def no_se_pickea_mas_de_lo_pedido(ctx=None):
    """El picking parcial está permitido —confirmado con operaciones— así que
    la desigualdad es en un solo sentido: recoger de menos es normal, recoger
    de más no.
    """
    from app.models.picking import TareaPicking
    filas = (TareaPicking.query
             .filter(TareaPicking.cantidad_recogida > TareaPicking.cantidad_solicitada)
             .order_by(TareaPicking.id.desc()).limit(500).all())
    return [
        Hallazgo(
            referencia=t.codigo or f'picking#{t.id}',
            detalle=f'recogió {t.cantidad_recogida} de {t.cantidad_solicitada} '
                    f'solicitadas',
            datos={'producto_id': t.producto_id, 'estado': t.estado},
        ) for t in filas
    ]


# ── Frontera 3: picking → packing ────────────────────────────────────────

@invariante(
    codigo='VTA-20',
    flujo='venta',
    frontera='picking → packing',
    consecuencia='Se empacó más de lo que el picking entregó. Sale del almacén '
                 'algo que el inventario todavía cree tener.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElBordeEntrePickingYPacking::test_ve_que_se_empaco_mas_de_lo_que_el_picking_entrego',
)
def lo_empacado_no_supera_lo_que_el_picking_entrego(ctx=None):
    """Se compara contra `cantidad_esperada`, **el snapshot que el propio
    packing guardó** al crearse desde el picking.

    La primera versión sumaba `TareaPicking.cantidad_recogida` en vivo, y eso
    daba falsos positivos: `reabrir_picking` **pone la cantidad recogida en
    cero** (`picking_service.py:519`). Un pedido pickeado, empacado y con su
    picking reabierto después aparecía como «empacado 7 > recogido 3» sin que
    nadie hubiera empacado de más.

    Comparar un valor mutable contra un consumo histórico es medir dos momentos
    distintos. El snapshot no se reescribe.
    """
    from app.models.packing import ItemPacking, TareaPacking
    filas = (db.session.query(ItemPacking, TareaPacking)
             .join(TareaPacking, ItemPacking.tarea_id == TareaPacking.id)
             .filter(ItemPacking.cantidad_real > ItemPacking.cantidad_esperada)
             .order_by(ItemPacking.id.desc()).limit(500).all())
    return [
        Hallazgo(
            referencia=f'{t.numero_pedido_siesa or t.codigo} / producto#{i.producto_id}',
            detalle=f'empacado {i.cantidad_real} > lo que entregó el picking '
                    f'({i.cantidad_esperada})',
            datos={'packing': t.codigo, 'estado': t.estado},
        ) for i, t in filas
    ]


@invariante(
    codigo='VTA-22',
    flujo='venta',
    frontera='picking → packing',
    consecuencia='El picking dice hoy una cantidad distinta de la que el '
                 'packing consumió. Suele ser una reapertura posterior; si no '
                 'lo es, una de las dos cifras está mal.',
    severidad=AVISA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElBordeEntrePickingYPacking::test_pero_sí_lo_declara_como_aviso',
)
def el_picking_actual_coincide_con_lo_que_el_packing_consumio(ctx=None):
    """`AVISA` y no `BLOQUEA` **a propósito**.

    `reabrir_picking` pone la cantidad recogida en cero, así que una diferencia
    acá es lo esperable después de una reapertura legítima. Lo que no es
    esperable es que sea frecuente: si lo es, alguien está reabriendo pickings
    de pedidos ya empacados y eso sí hay que mirarlo.

    Vive separado de VTA-20 para que el bloqueante siga siendo preciso. Mezclar
    un caso legítimo con uno grave dentro de la misma severidad es lo que
    entrena a ignorar el canal.
    """
    from app.models.packing import ItemPacking, TareaPacking
    from app.models.picking import TareaPicking

    recogido = {}
    for doc, prod, cant in (
            db.session.query(TareaPicking.referencia_documento,
                             TareaPicking.producto_id,
                             db.func.sum(TareaPicking.cantidad_recogida))
            .filter(TareaPicking.referencia_documento.isnot(None))
            .group_by(TareaPicking.referencia_documento,
                      TareaPicking.producto_id).all()):
        recogido[(doc, prod)] = cant or 0

    out = []
    for i, t in (db.session.query(ItemPacking, TareaPacking)
                 .join(TareaPacking, ItemPacking.tarea_id == TareaPacking.id)
                 .order_by(ItemPacking.id.desc()).limit(3000).all()):
        clave = (t.numero_pedido_siesa, i.producto_id)
        if clave not in recogido:
            continue                      # sin picking: lo mira VTA-21
        if recogido[clave] != (i.cantidad_esperada or 0):
            out.append(Hallazgo(
                referencia=f'{t.numero_pedido_siesa} / producto#{i.producto_id}',
                detalle=f'el packing consumió {i.cantidad_esperada}; el picking '
                        f'hoy suma {recogido[clave]}',
                datos={'packing': t.codigo},
            ))
    return out


@invariante(
    codigo='VTA-21',
    flujo='venta',
    frontera='picking → packing',
    consecuencia='Se empacó un pedido que nunca pasó por picking. Operaciones '
                 'confirmó que el picking SIEMPRE va primero — salvo que se '
                 'haya creado a mano, que hoy no deja rastro.',
    severidad=AVISA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElBordeEntrePickingYPacking::test_un_packing_sin_picking_avisa_pero_no_bloquea',
)
def todo_packing_viene_de_un_picking(ctx=None):
    """Confirmado con operaciones el 2026-08-13: «siempre primero picking».

    **`AVISA` y no `BLOQUEA`**: `PackingService.crear_manual` existe, no exige
    picking previo y **no marca el packing de ninguna forma**. Un packing
    manual legítimo es hoy indistinguible de un picking perdido, y no se puede
    bloquear sobre una pregunta que el modelo no sabe responder.

    Para que vuelva a ser bloqueante hace falta que `crear_manual` deje su
    origen escrito. Mientras tanto, esto sirve para contar cuántos hay y
    decidir si el camino manual se sigue usando.
    """
    from app.models.packing import TareaPacking
    from app.models.picking import TareaPicking

    con_picking = {r[0] for r in db.session.query(
        TareaPicking.referencia_documento).distinct()
        .filter(TareaPicking.referencia_documento.isnot(None)).all()}
    filas = (TareaPacking.query
             .filter(TareaPacking.numero_pedido_siesa.isnot(None))
             .order_by(TareaPacking.id.desc()).limit(1000).all())
    return [
        Hallazgo(
            referencia=t.codigo or f'packing#{t.id}',
            detalle=f'el pedido {t.numero_pedido_siesa} no tiene ninguna tarea '
                    f'de picking',
            datos={'estado': t.estado},
        ) for t in filas if t.numero_pedido_siesa not in con_picking
    ]


# ── Frontera 4: packing → bultos ─────────────────────────────────────────

@invariante(
    codigo='VTA-30',
    flujo='venta',
    frontera='packing → bultos',
    consecuencia='Una tarea despachada sin bultos: la mercancía salió y no hay '
                 'nada que rastrear ni que reingresar si vuelve.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_un_despacho_sin_bultos',
)
def toda_tarea_despachada_tiene_bultos(ctx=None):
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking

    con_bultos = {r[0] for r in db.session.query(Bulto.tarea_id).distinct().all()}
    filas = (TareaPacking.query
             .filter(TareaPacking.estado == 'DESPACHADO')
             .order_by(TareaPacking.id.desc()).limit(1000).all())
    return [
        Hallazgo(
            referencia=t.codigo or f'packing#{t.id}',
            detalle='marcada DESPACHADO y no tiene ningún bulto',
            datos={'pedido': t.numero_pedido_siesa},
        ) for t in filas if t.id not in con_bultos
    ]


# ── Frontera 5: bultos → ruta ────────────────────────────────────────────

@invariante(
    codigo='VTA-40',
    flujo='venta',
    frontera='bultos → ruta',
    consecuencia='Un bulto entregado sin ruta: nadie sabe quién lo llevó ni a '
                 'qué liquidación pertenece.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_un_bulto_entregado_sin_ruta',
)
def todo_bulto_entregado_pertenece_a_una_ruta(ctx=None):
    from app.models.bulto import Bulto, EstadoBulto
    filas = (Bulto.query
             .filter(Bulto.estado.in_((EstadoBulto.ENTREGADO, EstadoBulto.RECHAZADO)))
             .filter(Bulto.ruta_despacho_id.is_(None))
             .order_by(Bulto.id.desc()).limit(500).all())
    return [
        Hallazgo(
            referencia=b.codigo_barras or f'bulto#{b.id}',
            detalle=f'estado {b.estado} sin ruta asignada',
            datos={'tarea_id': b.tarea_id},
        ) for b in filas
    ]


# ── Frontera 6: ruta → recaudo ───────────────────────────────────────────

@invariante(
    codigo='VTA-50',
    flujo='venta',
    frontera='ruta → recaudo',
    consecuencia='El estado dice que los bultos volvieron y los bultos dicen '
                 'que no. Bodega va a buscar mercancía que no está.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_la_contradiccion_entre_parada_y_bultos',
)
def el_estado_de_la_parada_concuerda_con_el_de_sus_bultos(ctx=None):
    """La contradicción que costó el cuarto estado.

    `RECHAZADO` significa «los bultos vuelven al camión». Si la parada dice eso
    y sus bultos están `ENTREGADO`, uno de los dos miente — y el que se cree es
    el estado, porque es el que lee la lista de reingreso.

    El caso legítimo de bultos entregados con parada rechazada ya no existe:
    desde `m006entregadosinpago` eso es `ENTREGADO_SIN_PAGO`.
    """
    from app.models.bulto import Bulto, EstadoBulto
    from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega

    out = []
    for r in (RecaudoEntrega.query
              .filter(RecaudoEntrega.estado_entrega == EstadoEntrega.RECHAZADO)
              .order_by(RecaudoEntrega.id.desc()).limit(500).all()):
        bultos = Bulto.query.filter_by(ruta_despacho_id=r.ruta_id,
                                       tarea_id=r.tarea_id).all()
        if bultos and all(b.estado == EstadoBulto.ENTREGADO for b in bultos):
            out.append(Hallazgo(
                referencia=f'ruta#{r.ruta_id}/tarea#{r.tarea_id}',
                detalle='parada RECHAZADA pero todos sus bultos figuran '
                        'ENTREGADOS',
                datos={'motivo': r.motivo_rechazo},
            ))
    return out


@invariante(
    codigo='VTA-51',
    flujo='venta',
    frontera='ruta → recaudo',
    consecuencia='Se le pidió al conductor cobrar sobre una parada cuya '
                 'condición de pago nadie conocía.',
    severidad=OBSERVA,
)
def las_paradas_en_modo_libre_se_pueden_contar(ctx=None):
    """`LIBRE` es donde el conductor elige sin restricción.

    No es un defecto —ante condición desconocida, no afirmar es lo correcto—
    pero hay que poder contarlo: es la superficie por donde una parada de
    contado se puede registrar como crédito.
    """
    from app.services.cond_pago import LIBRE
    from app.models.recaudo_entrega import RecaudoEntrega
    filas = (RecaudoEntrega.query
             .filter(RecaudoEntrega.modo_pantalla == LIBRE)
             .order_by(RecaudoEntrega.id.desc()).limit(500).all())
    return [
        Hallazgo(
            referencia=f'ruta#{r.ruta_id}/tarea#{r.tarea_id}',
            detalle=f'el conductor eligió sin restricción · '
                    f'{r.estado_entrega} / {r.forma_pago or "sin forma de pago"}',
            datos={'monto': float(r.monto_cobrado or 0)},
        ) for r in filas
    ]


# ── Frontera 7: recaudo → Siesa ──────────────────────────────────────────

@invariante(
    codigo='VTA-60',
    flujo='venta',
    frontera='recaudo → Siesa',
    consecuencia='La ruta se liquidó y el cobro nunca llegó al ERP. La factura '
                 'queda abierta: es la cartera fantasma.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_un_cobro_que_no_llego_a_siesa',
)
def un_cobro_registrado_llego_a_siesa(ctx=None):
    """Una parada de contado con monto cobrado, en una ruta ya liquidada, tiene
    que haber disparado su recibo de caja."""
    from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho

    liquidadas = {r.id for r in RutaDespacho.query.filter(
        RutaDespacho.estado_financiero == EstadoFinancieroRuta.LIQUIDADA).all()}
    if not liquidadas:
        return []
    filas = (RecaudoEntrega.query
             .filter(RecaudoEntrega.ruta_id.in_(liquidadas))
             .filter(RecaudoEntrega.monto_cobrado > 0)
             .filter(db.or_(RecaudoEntrega.siesa_rc_triggered.is_(False),
                            RecaudoEntrega.siesa_rc_triggered.is_(None)))
             .filter(RecaudoEntrega.estado_entrega.in_(
                 (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL)))
             .order_by(RecaudoEntrega.id.desc()).limit(500).all())
    return [
        Hallazgo(
            referencia=f'ruta#{r.ruta_id}/tarea#{r.tarea_id}',
            detalle=f'cobró {r.monto_cobrado} y no se disparó el recibo de caja',
            datos={'forma_pago': r.forma_pago, 'estado': r.estado_entrega},
        ) for r in filas
    ]


@invariante(
    codigo='VTA-61',
    flujo='venta',
    frontera='recaudo → Siesa',
    consecuencia='La factura de esa parada queda en Elaboración con el '
                 'inventario ya descargado.',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_venta.py::TestElDetectorNoEstaCiego::test_ve_una_parada_de_contado',
)
def ninguna_parada_de_ruta_declara_contado(ctx=None):
    """Probado en producción el 2026-08-13: una FE de contado no se aprueba sin
    recaudo, y en ruta el dinero llega horas después.

    Se mira la condición **anotada en la tarea**, que es la que el pedido trajo.
    """
    from app.models.packing import TareaPacking
    from app.services import cond_pago as cp
    from app.services.connekta_gateway import connekta

    filas = (TareaPacking.query
             .filter(TareaPacking.cond_pago.isnot(None))
             .order_by(TareaPacking.id.desc()).limit(1000).all())
    return [
        Hallazgo(
            referencia=t.codigo or f'packing#{t.id}',
            detalle=f'el pedido declara condición «{t.cond_pago}», que es CONTADO',
            datos={'pedido': t.numero_pedido_siesa},
        ) for t in filas
        if cp.clasificar(t.cond_pago, connekta.cond_pago_ventas) == cp.CONTADO
    ]
