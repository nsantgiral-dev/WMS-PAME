from datetime import datetime
from app.extensions import db


class EstadoReposicion:
    PENDIENTE  = 'PENDIENTE'
    EN_PROCESO = 'EN_PROCESO'
    COMPLETADA = 'COMPLETADA'
    CANCELADA  = 'CANCELADA'


class TareaReposicion(db.Model):
    """
    Cola de reabastecimiento RESERVA → PICKING.

    Nace cuando el inventario de una ubicación PICKING cae bajo stock_minimo.
    El Abastecedor la ejecuta: escanea el LPN en RESERVA, confirma, y el sistema:
      1. Marca el LPN como CONSUMIDO
      2. Suma las UNDs al inventario local de la ubicación PICKING
      3. Dispara el job asíncrono al conector 173076 (tránsito salida en Siesa)

    Estados:
      PENDIENTE   → esperando que un abastecedor la tome
      EN_PROCESO  → abastecedor asignado, en camino a RESERVA
      COMPLETADA  → LPN roto, unidades en PICKING, Siesa notificado
      CANCELADA   → jefe de almacén canceló (sin LPN disponible, etc.)
    """
    __tablename__ = 'tareas_reposicion'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)   # REP-0000001

    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    cantidad_unidades = db.Column(db.Integer, nullable=False)  # UNDs base a reponer

    ubicacion_reserva_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=False)
    ubicacion_picking_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=False)

    # LPN seleccionado para romper (puede asignarse en el momento de tomar la tarea)
    lpn_id = db.Column(db.Integer, db.ForeignKey('lpn.id'), nullable=True)

    abastecedor_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    estado = db.Column(db.String(20), nullable=False, default='PENDIENTE')

    # Resultado
    unidades_movidas = db.Column(db.Integer, nullable=True)
    siesa_job_id = db.Column(db.String(50), nullable=True)
    siesa_enviado = db.Column(db.Boolean, nullable=False, default=False)

    notas = db.Column(db.Text, nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_inicio = db.Column(db.DateTime, nullable=True)
    fecha_completada = db.Column(db.DateTime, nullable=True)

    # Relaciones
    producto = db.relationship('Producto', backref='tareas_reposicion', lazy=True)
    almacen = db.relationship('Almacen', backref='tareas_reposicion', lazy=True)
    ubicacion_reserva = db.relationship('Ubicacion', foreign_keys=[ubicacion_reserva_id], lazy=True)
    ubicacion_picking = db.relationship('Ubicacion', foreign_keys=[ubicacion_picking_id], lazy=True)
    lpn = db.relationship('LPN', backref='tarea_reposicion', lazy=True)
    abastecedor = db.relationship('Usuario', backref='tareas_reposicion', lazy=True)

    @classmethod
    def generar_codigo(cls):
        """Serialize code generation with advisory lock to avoid race conditions."""
        from sqlalchemy import text as _text
        db.session.execute(_text('SELECT pg_advisory_xact_lock(:k)'), {'k': 3002})
        ultimo = db.session.query(db.func.max(cls.id)).scalar() or 0
        return f'REP-{(ultimo + 1):07d}'

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'tipo': 'REPOSICION',
            'estado': self.estado,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'almacen_id': self.almacen_id,
            'almacen_nombre': self.almacen.nombre if self.almacen else None,
            'bodega_siesa_id': self.almacen.bodega_siesa_id if self.almacen else None,
            'cantidad_unidades': self.cantidad_unidades,
            'ubicacion_reserva': self.ubicacion_reserva.codigo if self.ubicacion_reserva else None,
            'ubicacion_reserva_id': self.ubicacion_reserva_id,
            'ubicacion_picking': self.ubicacion_picking.codigo if self.ubicacion_picking else None,
            'ubicacion_picking_id': self.ubicacion_picking_id,
            'lpn_id': self.lpn_id,
            'lpn_codigo': self.lpn.codigo if self.lpn else None,
            'lpn_cantidad': self.lpn.cantidad_actual if self.lpn else None,
            'abastecedor_id': self.abastecedor_id,
            'unidades_movidas': self.unidades_movidas,
            'siesa_enviado': self.siesa_enviado,
            'notas': self.notas,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_completada': self.fecha_completada.isoformat() if self.fecha_completada else None,
        }
