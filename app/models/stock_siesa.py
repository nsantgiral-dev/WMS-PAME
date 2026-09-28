from datetime import datetime
from app.extensions import db


class StockSiesa(db.Model):
    """Snapshot del inventario de Siesa por bodega y producto.
    Se actualiza periódicamente desde la API. Persiste en PostgreSQL
    para sobrevivir deploys (no depende de cache en memoria).

    Solo la escribe `inventario_siesa_service._guardar_stock_en_bd`, y solo
    con una lectura COMPLETA: lo reportado se escribe y lo que no vino queda
    en cero con `ausente_desde` (2026-09-27; antes era acumulativa y un
    agotado conservaba su último positivo para siempre)."""
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
    #: Primer momento en que una lectura COMPLETA de Siesa no trajo esta fila
    #: (m051comprasa). Desde ahí la fila vale cero: la consulta de existencias
    #: solo trae existencia > 0, así que no venir en una lectura completa es
    #: «Siesa no tiene». NULL = Siesa la reporta. Nada se borra: una fila en
    #: cero es «se sabe que no hay»; su ausencia sería «no se sabe».
    ausente_desde = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('bodega', 'codigo_siesa', name='uq_stock_siesa_bodega_codigo'),
    )
