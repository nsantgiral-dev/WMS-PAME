from datetime import datetime
from app.extensions import db


class EstadoPicking:
    PENDIENTE = 'PENDIENTE'
    EN_PROCESO = 'EN_PROCESO'
    COMPLETADO = 'COMPLETADO'
    CANCELADO = 'CANCELADO'
    BLOQUEADO = 'BLOQUEADO'


class TareaPicking(db.Model):
    __tablename__ = 'tareas_picking'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)

    # Qué recoger
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    cantidad_solicitada = db.Column(db.Integer, nullable=False)
    cantidad_recogida = db.Column(db.Integer, default=0)

    # Dónde recoger (definido por FEFO)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    lote = db.Column(db.String(50))
    fecha_vencimiento = db.Column(db.Date)

    # A quién
    operario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Conteo de empaques escaneados (cajas/pacas) — las unidades van en cantidad_recogida
    empaques_escaneados = db.Column(db.Integer, default=0)

    # Estado
    estado = db.Column(db.String(20), default='PENDIENTE', nullable=False)
    prioridad = db.Column(db.Integer, default=1)
    motivo_bloqueo = db.Column(db.String(50))  # UBICACION_VACIA|FALTANTE|MERCANCIA_AVERIADA|PRODUCTO_INCORRECTO
    observaciones_bloqueo = db.Column(db.Text)

    # Referencia origen
    referencia_documento = db.Column(db.String(50))
    tipo_documento = db.Column(db.String(30))

    # Tiempos
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_inicio = db.Column(db.DateTime)
    fecha_completado = db.Column(db.DateTime)

    # Relaciones
    producto = db.relationship('Producto', backref='tareas_picking', lazy=True)
    ubicacion = db.relationship('Ubicacion', backref='tareas_picking', lazy=True)
    operario = db.relationship('Usuario', backref='tareas_picking', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'producto_id': self.producto_id,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'cantidad_solicitada': self.cantidad_solicitada,
            'cantidad_recogida': self.cantidad_recogida,
            'empaques_escaneados': self.empaques_escaneados or 0,
            'factor_conversion': self.producto.factor_conversion or 1 if self.producto else 1,
            'unidad_empaque': self.producto.unidad_empaque or '' if self.producto else '',
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'almacen_id': self.almacen_id,
            'lote': self.lote,
            'fecha_vencimiento': self.fecha_vencimiento.isoformat() if self.fecha_vencimiento else None,
            'operario_id': self.operario_id,
            'estado': self.estado,
            'prioridad': self.prioridad,
            'referencia_documento': self.referencia_documento,
            'tipo_documento': self.tipo_documento,
            'motivo_bloqueo': self.motivo_bloqueo,
            'observaciones_bloqueo': self.observaciones_bloqueo,
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_inicio': self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            'fecha_completado': self.fecha_completado.isoformat() if self.fecha_completado else None
        }
