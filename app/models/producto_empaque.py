from datetime import datetime
from app.extensions import db


class ProductoEmpaque(db.Model):
    """
    Catálogo de niveles de empaque por producto.

    Una fila por cada código de barras que representa un empaque.
    Un mismo producto puede tener múltiples empaques:
      - UND (factor=1)  → EAN-13 individual
      - CAJ (factor=12) → DUN-14 caja de 12
      - PACA (factor=1240) → código de paca (o LPN generado)

    origen='SIESA_GS1' → vino del worker nocturno (query 28 + 35)
    origen='WMS_LPN'   → creado manualmente en recepción para pacas sin código
    """
    __tablename__ = 'producto_empaques'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id', ondelete='CASCADE'), nullable=False)
    referencia_item = db.Column(db.String(50), nullable=False)  # codigo_siesa — para re-sync
    codigo_barras = db.Column(db.String(50), nullable=False)    # lo que lee el scanner
    unidad_medida = db.Column(db.String(20), nullable=False)    # CAJ, PACA, PAQ, UND
    factor_conversion = db.Column(db.Integer, nullable=False)   # UNDs base que contiene
    origen = db.Column(db.String(20), nullable=False, default='SIESA_GS1')
    activo = db.Column(db.Boolean, nullable=False, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relaciones
    producto = db.relationship('Producto', backref='empaques', lazy=True)
    lpns = db.relationship('LPN', backref='empaque', lazy=True)

    @classmethod
    def buscar_por_barcode(cls, codigo_barras: str):
        """Lookup principal: dado un código escaneado, retorna el empaque activo."""
        return cls.query.filter_by(
            codigo_barras=codigo_barras.strip(),
            activo=True
        ).first()

    @classmethod
    def buscar_todos_por_barcode(cls, codigo_barras: str):
        """Retorna todos los empaques que coinciden con ese código (para detectar ambigüedad)."""
        return cls.query.filter_by(
            codigo_barras=codigo_barras.strip(),
            activo=True
        ).all()

    def to_dict(self):
        return {
            'id': self.id,
            'producto_id': self.producto_id,
            'referencia_item': self.referencia_item,
            'codigo_barras': self.codigo_barras,
            'unidad_medida': self.unidad_medida,
            'factor_conversion': self.factor_conversion,
            'origen': self.origen,
            'activo': self.activo,
        }
