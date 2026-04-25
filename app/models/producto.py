from datetime import datetime
from app.extensions import db

class Producto(db.Model):
    __tablename__ = 'productos'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)
    nombre = db.Column(db.String(200), nullable=False)
    descripcion = db.Column(db.Text)
    categoria = db.Column(db.String(100))
    unidad_medida = db.Column(db.String(20), default='UND')
    peso = db.Column(db.Float)
    volumen = db.Column(db.Float)
    precio_compra = db.Column(db.Float, default=0)
    precio_venta = db.Column(db.Float, default=0)
    stock_minimo = db.Column(db.Integer, default=0)
    stock_maximo = db.Column(db.Integer, default=0)
    punto_pedido = db.Column(db.Integer, default=0)
    clasificacion_abc = db.Column(db.String(1))
    codigo_siesa = db.Column(db.String(50), index=True)
    codigo_barras = db.Column(db.String(100))      # EAN/UPC de la unidad suelta
    codigo_barras_empaque = db.Column(db.String(100))  # EAN de la caja/paca/paquete
    unidad_empaque = db.Column(db.String(20))     # ej. 'CJA', 'PAC', 'PQT'
    factor_conversion = db.Column(db.Integer, default=1)  # unidades por empaque
    unidad_negocio_id = db.Column(db.String(10))  # Unidad de negocio Siesa p.ej. '001'=PAPELERIA
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relaciones
    ubicaciones = db.relationship('UbicacionProducto', backref='producto', lazy='subquery')
    movimientos = db.relationship('MovimientoInventario', backref='producto', lazy=True)

    @property
    def stock_total(self):
        """Fuente única de verdad: suma de todas las ubicaciones."""
        return sum(u.cantidad for u in self.ubicaciones if u.cantidad > 0)

    @property
    def stock_disponible(self):
        """Stock total menos reservado."""
        return sum(u.cantidad_disponible() for u in self.ubicaciones)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'descripcion': self.descripcion,
            'categoria': self.categoria,
            'unidad_medida': self.unidad_medida,
            'precio_compra': self.precio_compra,
            'precio_venta': self.precio_venta,
            'stock_minimo': self.stock_minimo,
            'stock_maximo': self.stock_maximo,
            'punto_pedido': self.punto_pedido,
            'clasificacion_abc': self.clasificacion_abc,
            'codigo_siesa': self.codigo_siesa,
            'codigo_barras': self.codigo_barras,
            'codigo_barras_empaque': self.codigo_barras_empaque,
            'unidad_empaque': self.unidad_empaque,
            'factor_conversion': self.factor_conversion or 1,
            'unidad_negocio_id': self.unidad_negocio_id,
            'stock_total': self.stock_total,
            'stock_disponible': self.stock_disponible,
            'activo': self.activo
        }