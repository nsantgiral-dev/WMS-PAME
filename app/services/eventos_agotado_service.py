"""
Registro del evento "agotado real" — una sola responsabilidad: dejar
constancia del hecho con su contexto completo en el momento en que ocurre.

No decide cuándo dispararse (eso lo decide el caller, hoy
PickingService.reportar_problema() cuando motivo='FALTANTE') ni sabe nada
de cómo se agregan las métricas de BI que consumen este evento — solo
escribe. Las funciones de agregación de app/services/metricas/ leen
EventoStockAgotado directamente, sin pasar por este módulo.
"""
from app.extensions import db
from app.models.evento_stock_agotado import EventoStockAgotado
from app.models.picking import TareaPicking


def registrar_evento_agotado(tarea: TareaPicking, cantidad_faltante: int) -> EventoStockAgotado | None:
    """Agrega el evento a la sesión sin comitear — el caller controla la transacción."""
    if cantidad_faltante <= 0:
        return None

    producto = tarea.producto
    # «Sin precio» se declara como `None`, no como $0: `precio_venta` no lo
    # puebla ninguna sincronización (ver costo_service) y ningún producto se
    # vende a cero. Un 0 acá se suma en la venta perdida como si fuera dato.
    precio = producto.precio_venta
    evento = EventoStockAgotado(
        tarea_picking_id=tarea.id,
        tarea_codigo=tarea.codigo,
        cantidad_solicitada=tarea.cantidad_solicitada,
        producto_id=tarea.producto_id,
        almacen_id=tarea.almacen_id,
        pedido_siesa_ref=tarea.referencia_documento,
        tipo_documento=tarea.tipo_documento,
        cantidad_faltante=cantidad_faltante,
        precio_venta_capturado=precio if precio and precio > 0 else None,
        categoria_producto=producto.categoria,
        clasificacion_abc=producto.clasificacion_abc,
    )
    db.session.add(evento)
    return evento
