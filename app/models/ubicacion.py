from datetime import datetime
from app.extensions import db


class Ubicacion(db.Model):
    __tablename__ = 'ubicaciones'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    zona = db.Column(db.String(50))
    pasillo = db.Column(db.String(10))
    estante = db.Column(db.String(10))
    nivel = db.Column(db.String(10))
    tipo = db.Column(db.String(30), default='estanteria')

    # ── Campos maestros sincronizados desde Siesa (API_v2_Ubicaciones ID 43) ──
    # Siesa es el dueño — el WMS solo obedece. No editar manualmente.
    tipo_zona = db.Column(db.String(10), nullable=False, default='GENERAL')
    # PICKING = piso, unidades sueltas | RESERVA = alto, LPNs sellados
    # AVERIAS = productos dañados/en revisión | GENERAL = sin rol especial (legado)
    stock_minimo = db.Column(db.Integer, nullable=True)   # f152_cant_minima en Siesa
    stock_maximo = db.Column(db.Integer, nullable=True)   # f152_cant_maxima en Siesa
    secuencia_ruteo = db.Column(db.Integer, nullable=True)  # f152_secuencia — menor = primero

    # capacidad_maxima se mantiene por retrocompatibilidad; stock_maximo es el campo canónico
    capacidad_maxima = db.Column(db.Integer)

    # origen: SIESA (la crea/clasifica el sync nocturno) | MANUAL (la crea el jefe de
    # bodega desde el módulo de Layout). El sync nunca toca una ubicación MANUAL.
    origen = db.Column(db.String(10), nullable=False, default='MANUAL')

    # Slot fijo de PICKING: 1 SKU ↔ 1 ubicación PICKING por almacén. Solo se usa
    # cuando tipo_zona='PICKING' — en RESERVA/AVERIAS queda en None.
    producto_asignado_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=True)

    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    productos = db.relationship('UbicacionProducto', backref='ubicacion', lazy=True)
    producto_asignado = db.relationship('Producto', foreign_keys=[producto_asignado_id], lazy=True)

    @property
    def es_picking(self):
        return self.tipo_zona == 'PICKING'

    @property
    def es_reserva(self):
        return self.tipo_zona == 'RESERVA'

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'almacen_id': self.almacen_id,
            'zona': self.zona,
            'pasillo': self.pasillo,
            'estante': self.estante,
            'nivel': self.nivel,
            'tipo': self.tipo,
            'tipo_zona': self.tipo_zona,
            'stock_minimo': self.stock_minimo,
            'stock_maximo': self.stock_maximo,
            'secuencia_ruteo': self.secuencia_ruteo,
            'capacidad_maxima': self.capacidad_maxima,
            'origen': self.origen,
            'producto_asignado_id': self.producto_asignado_id,
            'producto_asignado_codigo': self.producto_asignado.codigo if self.producto_asignado else None,
            'activo': self.activo,
        }