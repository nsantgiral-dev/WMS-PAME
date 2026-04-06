"""
ProductoClasificacionABC — Clasificación ABC por almacén.

Replica la lógica de t122_mc_items_bodegas de Siesa: el ABC vive en la
intersección ítem × bodega, no en el maestro global del ítem.

Llave única: (producto_id, almacen_id) — permite upsert limpio.
"""
from datetime import datetime
from app.extensions import db


class ProductoClasificacionABC(db.Model):
    __tablename__ = 'producto_clasificacion_abc'
    __table_args__ = (
        db.UniqueConstraint('producto_id', 'almacen_id',
                            name='uq_producto_almacen_abc'),
    )

    id            = db.Column(db.Integer, primary_key=True)
    producto_id   = db.Column(db.Integer, db.ForeignKey('productos.id'),
                              nullable=False, index=True)
    almacen_id    = db.Column(db.Integer, db.ForeignKey('almacenes.id'),
                              nullable=False, index=True)
    clasificacion = db.Column(db.String(1), nullable=False)   # 'A' | 'B' | 'C'
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow,
                              onupdate=datetime.utcnow)

    producto = db.relationship('Producto', backref=db.backref(
        'clasificaciones_abc', lazy=True))

    def to_dict(self):
        return {
            'producto_id':   self.producto_id,
            'almacen_id':    self.almacen_id,
            'clasificacion': self.clasificacion,
            'updated_at':    self.updated_at.isoformat() if self.updated_at else None,
        }
