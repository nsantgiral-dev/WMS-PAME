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
    tarea_picking_id = db.Column(db.Integer, db.ForeignKey('tareas_picking.id'), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)

    # Copia de TareaPicking.referencia_documento — no hay FK real a pedidos_siesa,
    # la correlación en todo el proyecto es por igualdad de string (ver picking.py).
    pedido_siesa_ref = db.Column(db.String(50))
    # 'PEDIDO' | 'TRASLADO', copiado de la tarea. Un traslado bloqueado no es
    # venta perdida — se excluye en la agregación de la métrica 5.
    tipo_documento = db.Column(db.String(30))

    cantidad_faltante = db.Column(db.Integer, nullable=False)
    precio_venta_capturado = db.Column(db.Numeric(12, 2), nullable=False)
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
            'producto_id': self.producto_id,
            'almacen_id': self.almacen_id,
            'pedido_siesa_ref': self.pedido_siesa_ref,
            'tipo_documento': self.tipo_documento,
            'cantidad_faltante': self.cantidad_faltante,
            'precio_venta_capturado': float(self.precio_venta_capturado) if self.precio_venta_capturado is not None else None,
            'categoria_producto': self.categoria_producto,
            'clasificacion_abc': self.clasificacion_abc,
            'creado_en': self.creado_en.isoformat() if self.creado_en else None,
        }
