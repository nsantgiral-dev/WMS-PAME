"""
Modelos para gestión de compras — acuerdos marco + memoria de precios.

Tres ramas de compra:
  1. SKU con acuerdo vigente → OC al precio pactado (70-80% del volumen)
  2. SKU núcleo A sin acuerdo → despachador RFQ, comparador, humano decide
  3. Cola C → precio de lista del proveedor preferente, sin cotizar

El acuerdo marco convierte la negociación en algoritmo:
  se negocia 1 vez por trimestre, se ejecuta N veces sin fricción.
"""
from datetime import datetime, date
from app.extensions import db
from app.utils.fecha import dia_operativo as _dia_operativo


class Proveedor(db.Model):
    """Maestro de proveedores."""
    __tablename__ = 'proveedores'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)  # Código Siesa
    nombre = db.Column(db.String(200), nullable=False)
    nit = db.Column(db.String(20))
    contacto_nombre = db.Column(db.String(100))
    contacto_telefono = db.Column(db.String(20))
    contacto_whatsapp = db.Column(db.String(20))  # Para despachador RFQ
    pais = db.Column(db.String(20), default='COLOMBIA')  # COLOMBIA, CHINA
    condicion_pago_default = db.Column(db.String(20))  # C01=contado, C30=30 días, etc.
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'codigo': self.codigo, 'nombre': self.nombre,
            'nit': self.nit, 'pais': self.pais,
            'condicion_pago_default': self.condicion_pago_default,
            'activo': self.activo,
        }


class AcuerdoMarco(db.Model):
    """Precio negociado con vigencia — la negociación convertida en algoritmo."""
    __tablename__ = 'acuerdos_marco'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    proveedor_id = db.Column(db.Integer, db.ForeignKey('proveedores.id'), nullable=False)
    precio_unitario = db.Column(db.Numeric(12, 2), nullable=False)
    moneda = db.Column(db.String(5), default='COP')  # COP, USD
    condicion_pago = db.Column(db.String(20))  # C01, C30, C60
    volumen_comprometido = db.Column(db.Integer)  # unidades por trimestre
    vigencia_desde = db.Column(db.Date, nullable=False)
    vigencia_hasta = db.Column(db.Date, nullable=False)
    negociado_por = db.Column(db.String(100))  # Quién negoció
    notas = db.Column(db.Text)
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    producto = db.relationship('Producto', backref='acuerdos_marco', lazy='select')
    proveedor = db.relationship('Proveedor', backref='acuerdos', lazy='select')

    __table_args__ = (
        db.Index('ix_acuerdo_prod_prov', 'producto_id', 'proveedor_id'),
    )

    @property
    def vigente(self):
        hoy = _dia_operativo()
        return self.activo and self.vigencia_desde <= hoy <= self.vigencia_hasta

    @property
    def dias_para_vencer(self):
        return (self.vigencia_hasta - _dia_operativo()).days

    def to_dict(self):
        return {
            'id': self.id,
            'producto_id': self.producto_id,
            'proveedor_id': self.proveedor_id,
            'proveedor_nombre': self.proveedor.nombre if self.proveedor else None,
            'precio_unitario': float(self.precio_unitario),
            'moneda': self.moneda,
            'condicion_pago': self.condicion_pago,
            'vigencia_desde': self.vigencia_desde.isoformat(),
            'vigencia_hasta': self.vigencia_hasta.isoformat(),
            'vigente': self.vigente,
            'dias_para_vencer': self.dias_para_vencer,
        }


class PrecioProveedor(db.Model):
    """Memoria de cotizaciones — el historial que hoy vive en WhatsApp."""
    __tablename__ = 'precios_proveedor'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    proveedor_id = db.Column(db.Integer, db.ForeignKey('proveedores.id'), nullable=False)
    precio_unitario = db.Column(db.Numeric(12, 2), nullable=False)
    moneda = db.Column(db.String(5), default='COP')
    cantidad_cotizada = db.Column(db.Integer)  # Para qué volumen se cotizó
    condicion_pago = db.Column(db.String(20))
    fuente = db.Column(db.String(20), default='COTIZACION')
    # COTIZACION, FACTURA, ACUERDO_MARCO, LISTA_PRECIOS
    cotizado_por = db.Column(db.String(100))
    # `default=_dia_operativo` y no `date.today`: sin parentesis, el guard de
    # fechas no lo veia. En Railway `date.today()` es UTC, asi que una
    # cotizacion registrada despues de las 7 p.m. quedaba fechada MAÑANA — y la
    # jerarquia de costos la compara contra la vigencia de los acuerdos.
    fecha = db.Column(db.Date, nullable=False, default=_dia_operativo)
    vigente = db.Column(db.Boolean, default=True)

    producto = db.relationship('Producto', backref='precios_historicos', lazy='select')
    proveedor = db.relationship('Proveedor', backref='precios', lazy='select')

    __table_args__ = (
        db.Index('ix_precio_prod_prov_fecha', 'producto_id', 'proveedor_id', 'fecha'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'producto_id': self.producto_id,
            'proveedor_id': self.proveedor_id,
            'proveedor_nombre': self.proveedor.nombre if self.proveedor else None,
            'precio_unitario': float(self.precio_unitario),
            'moneda': self.moneda,
            'cantidad_cotizada': self.cantidad_cotizada,
            'fuente': self.fuente,
            'fecha': self.fecha.isoformat(),
            'vigente': self.vigente,
        }
