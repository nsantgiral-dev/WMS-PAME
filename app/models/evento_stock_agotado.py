"""
Snapshot de un agotado real, capturado en el momento en que ocurre.

Disparado desde `PickingService.reportar_problema()` cuando una TareaPicking
se bloquea con `motivo_bloqueo='FALTANTE'` — es decir, cuando un operario ya
caminó a la ubicación y el stock disponible no alcanzó para la cantidad
solicitada. Es el único punto del WMS donde ese hecho es observable en vivo;
sin capturarlo ahí, no hay forma de reconstruirlo después.

Evento hacia adelante: el histórico previo a la existencia de esta tabla no
es recuperable, y no se intenta backfillear.

`precio_venta_capturado`, `categoria_producto` y `clasificacion_abc` se
denormalizan (copiados del producto en el momento del evento) a propósito:
la métrica de venta perdida necesita el precio y la categoría vigentes
CUANDO se perdió la venta, no los actuales — comparar un snapshot histórico
contra un valor mutable de hoy mide dos momentos distintos y da un número
que no cuadra con nada (mismo error de fondo que ya costó el bug VTA-20 de
la auditoría de invariantes).
"""
from datetime import datetime

from app.extensions import db


class EventoStockAgotado(db.Model):
    __tablename__ = 'eventos_stock_agotado'

    id = db.Column(db.Integer, primary_key=True)
    # Nullable con SET NULL (m034agotado): esta tabla es PROTEGIDA_ANALITICA y
    # `tareas_picking` es OPERATIVA. Con la FK NOT NULL sin `ondelete` el acta
    # de corte no podía vaciar las tareas del ensayo. La tarea se va; el evento
    # se queda con lo que necesitaba de ella (`tarea_codigo`,
    # `cantidad_solicitada`, más pedido/producto/faltante ya copiados).
    tarea_picking_id = db.Column(
        db.Integer, db.ForeignKey('tareas_picking.id', ondelete='SET NULL'),
        nullable=True)
    tarea_codigo = db.Column(db.String(50))
    cantidad_solicitada = db.Column(db.Integer)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)

    # Copia de TareaPicking.referencia_documento — no hay FK real a pedidos_siesa,
    # la correlación en todo el proyecto es por igualdad de string (ver picking.py).
    pedido_siesa_ref = db.Column(db.String(50))
    # Copiado de la tarea (ver app/models/tipo_documento.py). Un traslado
    # bloqueado no es venta perdida — se excluye en la agregación de la métrica 5.
    tipo_documento = db.Column(db.String(30))

    cantidad_faltante = db.Column(db.Integer, nullable=False)
    #: `None` = SIN PRECIO, no $0. `Producto.precio_venta` no lo puebla ninguna
    #: sincronización; el `or 0` de antes hacía que la venta perdida sumara
    #: ceros que parecían dato (Regla 0). La métrica los cuenta aparte.
    precio_venta_capturado = db.Column(db.Numeric(12, 2), nullable=True)
    categoria_producto = db.Column(db.String(100))
    clasificacion_abc = db.Column(db.String(1))

    creado_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    producto = db.relationship('Producto', lazy=True)
    tarea_picking = db.relationship('TareaPicking', lazy=True)

    # Declarados también acá (no solo en la migración): los tests construyen
    # la base con create_all() desde los modelos — un índice que solo viviera
    # en la migración nunca existiría en esas bases.
    __table_args__ = (
        db.Index('ix_eventos_stock_agotado_creado_en', 'creado_en'),
        db.Index('ix_eventos_stock_agotado_producto_almacen',
                 'producto_id', 'almacen_id', 'creado_en'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'tarea_picking_id': self.tarea_picking_id,
            'tarea_codigo': self.tarea_codigo,
            'cantidad_solicitada': self.cantidad_solicitada,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'almacen_id': self.almacen_id,
            'pedido_siesa_ref': self.pedido_siesa_ref,
            'tipo_documento': self.tipo_documento,
            'cantidad_faltante': self.cantidad_faltante,
            'precio_venta_capturado': float(self.precio_venta_capturado) if self.precio_venta_capturado is not None else None,
            'sin_precio': self.precio_venta_capturado is None,
            'categoria_producto': self.categoria_producto,
            'clasificacion_abc': self.clasificacion_abc,
            'creado_en': self.creado_en.isoformat() if self.creado_en else None,
        }
