from datetime import datetime
from app.extensions import db

class UbicacionProducto(db.Model):
    __tablename__ = 'ubicaciones_productos'

    id = db.Column(db.Integer, primary_key=True)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    cantidad = db.Column(db.Integer, default=0, nullable=False)
    reservado = db.Column(db.Integer, default=0)
    bloqueado = db.Column(db.Integer, default=0)
    lote = db.Column(db.String(50))
    fecha_vencimiento = db.Column(db.Date)
    fecha_ingreso = db.Column(db.DateTime, default=datetime.utcnow)
    row_version = db.Column(db.Integer, default=1, nullable=False)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.CheckConstraint('cantidad >= 0', name='ck_cantidad_no_negativa'),
        db.CheckConstraint('reservado >= 0', name='ck_reservado_no_negativo'),
        db.UniqueConstraint('ubicacion_id', 'producto_id', 'lote', name='uq_ubicacion_producto_lote'),
    )

    def cantidad_disponible(self):
        return max(0, self.cantidad - self.reservado - self.bloqueado)

    def to_dict(self):
        return {
            'id': self.id,
            'ubicacion_id': self.ubicacion_id,
            'producto_id': self.producto_id,
            'cantidad': self.cantidad,
            'reservado': self.reservado,
            'bloqueado': self.bloqueado,
            'disponible': self.cantidad_disponible(),
            'lote': self.lote,
            'fecha_vencimiento': self.fecha_vencimiento.isoformat() if self.fecha_vencimiento else None,
            'fecha_ingreso': self.fecha_ingreso.isoformat() if self.fecha_ingreso else None
        }


class MovimientoInventario(db.Model):
    __tablename__ = 'movimientos_inventario'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    tipo = db.Column(db.String(30), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)
    saldo_antes = db.Column(db.Integer)
    saldo_despues = db.Column(db.Integer)
    motivo = db.Column(db.String(200))
    numero_documento = db.Column(db.String(50))
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    idempotency_key = db.Column(db.String(64), unique=True)
    siesa_sync = db.Column(db.String(20), default='PENDIENTE')
    fecha = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'producto_id': self.producto_id,
            'almacen_id': self.almacen_id,
            'tipo': self.tipo,
            'cantidad': self.cantidad,
            'saldo_antes': self.saldo_antes,
            'saldo_despues': self.saldo_despues,
            'motivo': self.motivo,
            'numero_documento': self.numero_documento,
            'siesa_sync': self.siesa_sync,
            'fecha': self.fecha.isoformat()
        }