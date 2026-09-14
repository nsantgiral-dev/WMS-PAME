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
    evento = EventoStockAgotado(
        tarea_picking_id=tarea.id,
        producto_id=tarea.producto_id,
        almacen_id=tarea.almacen_id,
        pedido_siesa_ref=tarea.referencia_documento,
        tipo_documento=tarea.tipo_documento,
        cantidad_faltante=cantidad_faltante,
        precio_venta_capturado=producto.precio_venta or 0,
        categoria_producto=producto.categoria,
        clasificacion_abc=producto.clasificacion_abc,
    )
    db.session.add(evento)
    return evento
