"""
DEPRECATED (2026-07-28) — reemplazado por DevolucionCliente
(app/models/devolucion_cliente.py). No crear filas nuevas: el flujo reactivo
de reconciliación que las generaba (crear_tareas_desde_discrepancias, en
devolucion_service.py) ya no se invoca — ver inventario_siesa_service.py.
Se conserva sin borrar por el histórico ya persistido y por los SiesaJob con
referencia_tipo='TareaDevolucion' de trabajos ya completados.

TareaDevolucion — representa una devolución física pendiente de ubicar en bodega.

El WMS NO crea notas crédito ni toca finanzas. Solo gestiona el movimiento físico:
Siesa ya procesó el dinero → el recepcionista ubica la caja en el estante.

Estados: PENDIENTE → EN_PROCESO → COMPLETADO | DESCARTADO
"""
from datetime import datetime
from app.extensions import db


class EstadoDevolucion:
    PENDIENTE   = 'PENDIENTE'
    EN_PROCESO  = 'EN_PROCESO'
    COMPLETADO  = 'COMPLETADO'
    DESCARTADO  = 'DESCARTADO'


class TareaDevolucion(db.Model):
    __tablename__ = 'tareas_devolucion'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)

    # Producto y cantidad diferencial (Siesa - WMS)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    cantidad_diferencia = db.Column(db.Integer, nullable=False)  # unidades a ubicar

    # Ubicación donde el recepcionista pone la mercancía
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)
    es_averiado = db.Column(db.Boolean, default=False)

    # Recepcionista asignado
    recepcionista_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Estado
    estado = db.Column(db.String(20), default='PENDIENTE', nullable=False)
    # PENDIENTE → EN_PROCESO → COMPLETADO | DESCARTADO

    # Trazabilidad: de qué reconciliación surgió
    origen_reconciliacion = db.Column(db.String(50))  # timestamp de la reconciliación

    # Idempotencia: no crear duplicados si ya hay tarea PENDIENTE para mismo producto
    idempotency_key = db.Column(db.String(64), unique=True)

    # Observaciones del recepcionista
    observaciones = db.Column(db.Text)

    # Idempotencia con Siesa — P4: evita traslado NB1→AV1 duplicado en reintento DLQ
    siesa_triggered    = db.Column(db.Boolean, default=False, nullable=False, server_default='false')
    siesa_triggered_at = db.Column(db.DateTime, nullable=True)

    # Tiempos
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_completado = db.Column(db.DateTime)

    # Relaciones
    producto = db.relationship('Producto', backref='tareas_devolucion', lazy=True)
    ubicacion = db.relationship('Ubicacion', backref='tareas_devolucion', lazy=True)
    recepcionista = db.relationship('Usuario', backref='tareas_devolucion', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'almacen_id': self.almacen_id,
            'cantidad_diferencia': self.cantidad_diferencia,
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'es_averiado': self.es_averiado,
            'siesa_triggered': self.siesa_triggered,   # averiado: indica si Siesa fue notificado
            'siesa_triggered_at': self.siesa_triggered_at.isoformat() if self.siesa_triggered_at else None,
            'recepcionista_id': self.recepcionista_id,
            'estado': self.estado,
            'observaciones': self.observaciones,
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_completado': self.fecha_completado.isoformat() if self.fecha_completado else None,
        }
