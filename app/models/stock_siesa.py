from datetime import datetime
from app.extensions import db


class StockSiesa(db.Model):
    """Snapshot del inventario de Siesa por bodega y producto.
    Se actualiza periódicamente desde la API. Persiste en PostgreSQL
    para sobrevivir deploys (no depende de cache en memoria)."""
    __tablename__ = 'stock_siesa'

    id = db.Column(db.Integer, primary_key=True)
    bodega = db.Column(db.String(20), nullable=False, index=True)
    codigo_siesa = db.Column(db.String(60), nullable=False)
    existencia = db.Column(db.Float, default=0)
    comprometido = db.Column(db.Float, default=0)
    salida_sin_conf = db.Column(db.Float, default=0)
    descripcion = db.Column(db.String(200), default='')
    unidad_medida = db.Column(db.String(10), default='UND')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('bodega', 'codigo_siesa', name='uq_stock_siesa_bodega_codigo'),
    )
