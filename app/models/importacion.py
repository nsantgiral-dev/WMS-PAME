"""
Modelos para importaciones China — Armador de Contenedor.

Tres tablas:
  ficha_importacion: cubicaje por SKU (CBM, peso, unidades/caja, MOQ)
  contenedor: registro histórico y en curso (alimenta σ_LT)
  item_en_transito: SKUs navegando (evita duplicar pedidos)
"""
from datetime import datetime
from app.extensions import db


class FichaImportacion(db.Model):
    """Cubicaje y datos logísticos por SKU de origen China."""
    __tablename__ = 'ficha_importacion'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    unidades_por_caja = db.Column(db.Integer, nullable=False)
    cbm_por_caja = db.Column(db.Numeric(8, 4), nullable=False)
    peso_kg_por_caja = db.Column(db.Numeric(8, 2), nullable=False)
    moq_cajas = db.Column(db.Integer, default=1)
    proveedor_china = db.Column(db.String(100))  # M003 QUIROND, M009 IHO, etc.
    costo_fob_usd = db.Column(db.Numeric(10, 2))
    fuente = db.Column(db.String(20), nullable=False, default='ESTIMADO')
    # PACKING_LIST = verificado, AGENTE = del sourcing, ESTIMADO = excluido de armado auto
    fecha_ficha = db.Column(db.DateTime, default=datetime.utcnow)

    producto = db.relationship('Producto', backref='ficha_importacion', lazy='select')

    __table_args__ = (
        db.Index('ix_ficha_importacion_producto', 'producto_id', unique=True),
    )


class Contenedor(db.Model):
    """Registro de contenedores históricos y en curso. Alimenta σ_LT."""
    __tablename__ = 'contenedores'

    id = db.Column(db.Integer, primary_key=True)
    numero = db.Column(db.String(20))  # Número de contenedor (ej: MSKU1234567)
    tipo = db.Column(db.String(10), default='40STD')  # 40STD, 40HC
    proveedor = db.Column(db.String(100))  # M003 QUIROND, M009 IHO, etc.

    # Las 4 fechas — de aquí sale σ_LT
    fecha_oc = db.Column(db.Date)           # Fecha de orden de compra
    fecha_etd = db.Column(db.Date)          # Estimated Time of Departure (China)
    fecha_eta = db.Column(db.Date)          # Estimated Time of Arrival (Buenaventura)
    fecha_llegada_puerto = db.Column(db.Date)  # Llegada real a Buenaventura
    fecha_recepcion_cedi = db.Column(db.Date)  # Recepción en CDI Neiva

    cbm_facturado = db.Column(db.Numeric(8, 2))
    peso_kg = db.Column(db.Numeric(10, 2))
    valor_fob_usd = db.Column(db.Numeric(12, 2))
    valor_nacionalizado_cop = db.Column(db.Numeric(14, 0))

    estado = db.Column(db.String(20), default='BORRADOR')
    # BORRADOR, EN_PRODUCCION, NAVEGANDO, EN_PUERTO, NACIONALIZACION, EN_RUTA_CEDI, RECIBIDO
    notas = db.Column(db.Text)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def lead_time_real(self):
        """Lead time real OC → recepción CDI (en días). None si faltan fechas."""
        if self.fecha_oc and self.fecha_recepcion_cedi:
            return (self.fecha_recepcion_cedi - self.fecha_oc).days
        return None

    def to_dict(self):
        return {
            'id': self.id,
            'numero': self.numero,
            'tipo': self.tipo,
            'proveedor': self.proveedor,
            'fecha_oc': self.fecha_oc.isoformat() if self.fecha_oc else None,
            'fecha_etd': self.fecha_etd.isoformat() if self.fecha_etd else None,
            'fecha_eta': self.fecha_eta.isoformat() if self.fecha_eta else None,
            'fecha_llegada_puerto': self.fecha_llegada_puerto.isoformat() if self.fecha_llegada_puerto else None,
            'fecha_recepcion_cedi': self.fecha_recepcion_cedi.isoformat() if self.fecha_recepcion_cedi else None,
            'lead_time_real': self.lead_time_real,
            'cbm_facturado': float(self.cbm_facturado) if self.cbm_facturado else None,
            'peso_kg': float(self.peso_kg) if self.peso_kg else None,
            'valor_fob_usd': float(self.valor_fob_usd) if self.valor_fob_usd else None,
            'estado': self.estado,
        }


class ItemEnTransito(db.Model):
    """SKUs navegando — evita que el Armador duplique pedidos."""
    __tablename__ = 'items_en_transito'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    contenedor_id = db.Column(db.Integer, db.ForeignKey('contenedores.id'), nullable=True)
    cantidad = db.Column(db.Integer, nullable=False)
    estado = db.Column(db.String(20), default='NAVEGANDO')
    # NAVEGANDO, EN_PUERTO, NACIONALIZACION, EN_RUTA_CEDI, RECIBIDO
    fecha_registro = db.Column(db.DateTime, default=datetime.utcnow)

    producto = db.relationship('Producto', backref='en_transito', lazy='select')
    contenedor = db.relationship('Contenedor', backref='items', lazy='select')

    __table_args__ = (
        db.Index('ix_items_transito_prod_cont', 'producto_id', 'contenedor_id'),
    )
