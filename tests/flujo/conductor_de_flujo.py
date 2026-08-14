"""
Lleva UN pedido por todo el WMS, usando los servicios reales.

## Por qué no inserta filas

Un arnés que escribe `TareaPacking(...)` directo prueba que la base acepta esas
filas — nada más. Es lo que ya hacen los 1897 tests, cada uno fabricando su
propio estado coherente, y por eso ninguno puede ver un defecto de frontera.

Acá cada etapa se avanza **llamando al servicio que la operación llama**:
`PickingService.confirmar_picking`, `PackingService.crear_desde_picking`,
`RutaService.confirmar_parada`. Si una etapa deja de producir lo que la
siguiente espera, se rompe acá igual que se rompería en la bodega.

## El flujo real, no el que yo deduje

Confirmado con operaciones el 2026-08-13:

  · **el picking SIEMPRE va primero** — no hay pedidos que entren directo a
    empacar (`VTA-21` lo vigila);
  · **el picking parcial se puede y ocurre** — así que el camino por defecto de
    este arnés recoge de menos, no completo. Un arnés que solo ejerce el caso
    feliz verifica el flujo que nadie tiene.
"""
import uuid
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Flujo:
    """Lo que el arnés fue produciendo. Cada campo es la salida de una etapa."""
    pedido: str
    almacen_id: int
    usuario_id: int
    producto_ids: List[int] = field(default_factory=list)
    pickings: List[int] = field(default_factory=list)
    packing_id: Optional[int] = None
    bultos: List[int] = field(default_factory=list)
    ruta_id: Optional[int] = None
    recaudo_id: Optional[int] = None


def _sufijo():
    return uuid.uuid4().hex[:6]


def sembrar_catalogo(db, almacen, n=2, con_stock=50, unidad_negocio='001'):
    """Productos con stock en una ubicación de PICKING.

    Devuelve `(productos, ubicacion)`. El stock es lo que hace que
    `crear_tareas` encuentre de dónde recoger — sin él el picking nace
    bloqueado y el arnés no avanza.
    """
    from app.models.inventario import UbicacionProducto
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion

    ub = Ubicacion(codigo=f'PIK-FLU-{_sufijo()}', almacen_id=almacen.id,
                   tipo_zona='PICKING', stock_minimo=0, stock_maximo=999,
                   secuencia_ruteo=1, activo=True)
    db.session.add(ub)
    db.session.flush()

    productos = []
    for i in range(n):
        s = _sufijo()
        # `unidad_negocio_id`: los traslados la exigen por producto — Siesa NO
        # la hereda de la bodega. Sin ella, `aprobar_solicitud` aborta. El arnés
        # chocó con ese guard la primera vez, que es exactamente lo que se
        # espera de un arnés que usa el camino real.
        p = Producto(codigo=f'FLU{i}{s}', nombre=f'PRODUCTO DE FLUJO {i}',
                     codigo_siesa=f'SIE{i}{s}', unidad_negocio_id=unidad_negocio)
        db.session.add(p)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=p.id,
                                         cantidad=con_stock, reservado=0,
                                         bloqueado=0))
        productos.append(p)
    db.session.commit()
    return productos, ub


def sembrar_pedido(db, productos, cliente='CLIENTE DE FLUJO', cantidad=10,
                   cond_pago='C02'):
    """Las líneas de pedido tal como las deja `pedidos_sync_service`.

    Se escriben directo **a propósito**: la etapa anterior es Siesa, y lo que
    el arnés verifica es de acá en adelante. Lo que sí se respeta es la forma
    exacta que produce el sync, incluido `producto_id` resuelto por
    `codigo_siesa`.
    """
    from app.models.pedido_siesa import PedidoSiesa

    numero = f'PED-FLU-{_sufijo()}'
    for p in productos:
        db.session.add(PedidoSiesa(
            tipo_docto='PD', consec_docto=int(uuid.uuid4().int % 100000),
            centro_op='003', bodega='NB1', numero_pedido=numero,
            item_codigo=p.codigo_siesa, item_descripcion=p.nombre,
            item_id_siesa=p.codigo_siesa, cliente=cliente, municipio='NEIVA',
            estado_siesa=3, cantidad_pedida=cantidad, cantidad_remisionada=0,
            cantidad_pendiente=cantidad, producto_id=p.id))
    db.session.commit()
    return numero


def hacer_picking(db, flujo, cantidad_pedida, recoger):
    """Crea las tareas de picking y las confirma.

    `recoger < cantidad_pedida` ejerce el **picking parcial**, que operaciones
    confirmó como caso real. Es el camino por defecto del arnés.
    """
    from app.services.picking_service import PickingService

    for pid in flujo.producto_ids:
        tareas = PickingService.crear_tareas(
            producto_id=pid, cantidad=cantidad_pedida,
            almacen_id=flujo.almacen_id, referencia_documento=flujo.pedido,
            tipo_documento='PEDIDO')
        for t in tareas:
            PickingService.iniciar_picking(t.id, flujo.usuario_id)
            PickingService.confirmar_picking(t.id, recoger, flujo.usuario_id)
            flujo.pickings.append(t.id)
    db.session.commit()
    return flujo.pickings


def hacer_packing(db, flujo, bultos=1):
    """Arma el packing desde el picking y lo cierra con sus bultos."""
    from app.services.packing_service import PackingService

    tarea = PackingService.crear_desde_picking(
        tareas_picking_ids=flujo.pickings,
        numero_pedido_siesa=flujo.pedido,
        almacen_id=flujo.almacen_id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='1')
    flujo.packing_id = tarea.id
    PackingService.iniciar(tarea.id, flujo.usuario_id)
    for item in tarea.items:
        PackingService.escanear_item(tarea.id, item.producto_id,
                                     item.cantidad_esperada)
    PackingService.confirmar_packing(tarea.id)
    PackingService.cerrar_packing(tarea.id, [{'tipo': 'Caja', 'cantidad': bultos}],
                                  flujo.usuario_id)
    db.session.commit()

    from app.models.bulto import Bulto
    flujo.bultos = [b.id for b in Bulto.query.filter_by(tarea_id=tarea.id).all()]
    return tarea


def hacer_ruta(db, flujo, conductor_id):
    """Programa la ruta, le carga los bultos y la pone en tránsito."""
    from app.models.bulto import Bulto
    from app.models.ruta_despacho import RutaDespacho

    ruta = RutaDespacho(conductor_id=conductor_id, tipo_ruta='Urbana',
                        estado='EN_TRANSITO')
    db.session.add(ruta)
    db.session.flush()
    for b in Bulto.query.filter(Bulto.id.in_(flujo.bultos)).all():
        b.ruta_despacho_id = ruta.id
    db.session.commit()
    flujo.ruta_id = ruta.id
    return ruta


def hacer_entrega(db, flujo, **datos):
    """Confirma la parada con el servicio real, no escribiendo el recaudo."""
    from app.services.ruta_service import RutaService

    base = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
            'monto_cobrado': 1000}
    base.update(datos)
    recaudo_id, _ = RutaService.confirmar_parada(
        flujo.ruta_id, flujo.packing_id, flujo.usuario_id, base)
    flujo.recaudo_id = recaudo_id
    db.session.commit()
    return recaudo_id


def flujo_completo(db, almacen, usuario_id, conductor_id,
                   cantidad_pedida=10, recoger=7, **entrega):
    """Pedido → picking parcial → packing → bultos → ruta → entrega.

    `recoger=7` de `10` por defecto: el caso real, no el feliz.
    """
    productos, _ = sembrar_catalogo(db, almacen)
    pedido = sembrar_pedido(db, productos, cantidad=cantidad_pedida)
    flujo = Flujo(pedido=pedido, almacen_id=almacen.id, usuario_id=usuario_id,
                  producto_ids=[p.id for p in productos])
    hacer_picking(db, flujo, cantidad_pedida, recoger)
    hacer_packing(db, flujo)
    hacer_ruta(db, flujo, conductor_id)
    hacer_entrega(db, flujo, **entrega)
    return flujo


# ── Traslados ────────────────────────────────────────────────────────────

def flujo_traslado(db, almacen, solicitante_id, aprobador_id, operario_id,
                   cantidad=10, aprobar=8, recibir=None, modo='EN_TRANSITO'):
    """Solicitud → aprobación → picking → despacho (STS) → recepción (ETS).

    Igual que el de venta: cada etapa se avanza con el servicio real. Y por
    defecto **recorta en cada paso** —10 solicitadas, 8 aprobadas, y lo enviado
    es lo que el picking encuentre— porque el recorte es el caso normal, no la
    excepción.
    """
    from app.services.traslado_service import TrasladoService

    productos, _ = sembrar_catalogo(db, almacen, n=2)
    items = [{'producto_id': p.id, 'cantidad_solicitada': cantidad} for p in productos]

    sol = TrasladoService.crear_solicitud(
        solicitante_id=solicitante_id, bodega_destino='PC1',
        nombre_punto_venta='PITALITO CENTRO', items=items,
        bodega_origen='NB1')
    TrasladoService.enviar_solicitud(sol.id)

    from app.models.traslado import ItemSolicitudTraslado
    aprobados = [{'id': it.id, 'cantidad_aprobada': aprobar}
                 for it in ItemSolicitudTraslado.query.filter_by(solicitud_id=sol.id).all()]
    TrasladoService.aprobar_solicitud(sol.id, aprobador_id,
                                      items_aprobados=aprobados,
                                      operario_id=operario_id)
    db.session.commit()
    return sol
