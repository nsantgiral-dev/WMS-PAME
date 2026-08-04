from datetime import datetime
from app.extensions import db


class EstadoLPN:
    """Los estados de un LPN, con nombre.

    Era el único modelo del repo que asignaba su estado como literal suelto
    —`self.estado = 'CONSUMIDO'`— mientras los demás (EstadoPicking,
    EstadoPacking, EstadoConteo, EstadoRecepcion...) tienen su clase. Un literal
    repartido en ocho archivos es un typo esperando: `'CONSUMIDO '` con espacio
    no falla en ningún lado, simplemente deja de coincidir en los filtros.
    """

    ACTIVO    = 'ACTIVO'
    CONSUMIDO = 'CONSUMIDO'

    TODOS = (ACTIVO, CONSUMIDO)


class LPN(db.Model):
    """
    License Plate Number — instancia física de un empaque en el almacén.

    Un LPN = una paca/caja física con su etiqueta impresa.
    Nace en recepción, viaja en traslados, muere cuando el empaque se abre.

    Estados:
      ACTIVO      → paca en el almacén, disponible para picking/traslado
      EN_TRANSITO → asignado a un traslado, viajando entre ubicaciones
      CONSUMIDO   → paca abierta, su contenido pasó a inventario como UNDs sueltas

    Regla de oro: factor_conversion es inmutable después de creación.
    El factor es una verdad física de ese empaque — no cambia aunque
    se actualice el catálogo. Si la paca tiene 1240, siempre tendrá 1240.
    """
    __tablename__ = 'lpn'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)  # LPN-0000001
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    empaque_id = db.Column(db.Integer, db.ForeignKey('producto_empaques.id'), nullable=True)
    factor_conversion = db.Column(db.Integer, nullable=False)   # copia inmutable al nacer
    cantidad_actual = db.Column(db.Integer, nullable=False)     # UNDs reales (puede < factor si parcial)
    estado = db.Column(db.String(20), nullable=False, default=EstadoLPN.ACTIVO)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=True)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)
    recepcion_id = db.Column(db.Integer, db.ForeignKey('recepciones.id'), nullable=True)
    traslado_id = db.Column(db.Integer, db.ForeignKey('solicitudes_traslado.id'), nullable=True)
    creado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_consumo = db.Column(db.DateTime, nullable=True)
    notas = db.Column(db.Text, nullable=True)

    # Relaciones
    producto = db.relationship('Producto', backref='lpns', lazy=True)
    ubicacion = db.relationship('Ubicacion', backref='lpns', lazy=True)
    creado_por = db.relationship('Usuario', backref='lpns_creados', lazy=True)

    @classmethod
    def generar_codigo(cls):
        """Genera el próximo código LPN secuencial: LPN-0000001, LPN-0000002...
        Uses MAX(id)+1 as baseline — safe because `codigo` column is UNIQUE:
        if a concurrent insert causes a collision, the caller gets IntegrityError
        and can retry (or use a UUID-based fallback).
        """
        from sqlalchemy import text as _text
        # Lock-free: use advisory lock scoped to this table to serialize code generation
        db.session.execute(_text('SELECT pg_advisory_xact_lock(:k)'), {'k': 3001})
        ultimo = db.session.query(db.func.max(cls.id)).scalar() or 0
        return f'LPN-{(ultimo + 1):07d}'

    @classmethod
    def buscar_activos_por_producto(cls, producto_id: int, almacen_id: int):
        """Todos los LPNs disponibles de un producto en un almacén."""
        from sqlalchemy.orm import selectinload
        return cls.query.options(
            selectinload(cls.producto),
            selectinload(cls.ubicacion),
        ).filter_by(
            producto_id=producto_id,
            almacen_id=almacen_id,
            estado=EstadoLPN.ACTIVO
        ).all()

    def consumir(self):
        """Marca el LPN como consumido (paca abierta). Llama a commit() externamente."""
        self.estado = EstadoLPN.CONSUMIDO
        self.fecha_consumo = datetime.utcnow()

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'producto_id': self.producto_id,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'empaque_id': self.empaque_id,
            'factor_conversion': self.factor_conversion,
            'cantidad_actual': self.cantidad_actual,
            'estado': self.estado,
            'almacen_id': self.almacen_id,
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'recepcion_id': self.recepcion_id,
            'traslado_id': self.traslado_id,
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_consumo': self.fecha_consumo.isoformat() if self.fecha_consumo else None,
            'notas': self.notas,
        }
