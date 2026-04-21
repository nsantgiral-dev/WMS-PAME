"""
Cuarentena Fail-Fast de ubicaciones con prefijo no reconocido.

El sync nocturno de Siesa solo acepta ubicaciones con prefijos certificados:
  PIK-* → PICKING | RES-* → RESERVA | AVE-* → AVERIAS

Cualquier código que NO arranque con esos prefijos va aquí en vez de
clasificarse como GENERAL.  El jefe de bodega debe corregir el nombre
en Siesa Enterprise y esperar al próximo sync.

NUNCA crear ubicaciones GENERAL automáticamente — podría asignar picking
a estanterías de alto riesgo o áreas de tránsito no autorizadas.
"""
from datetime import datetime
from app.extensions import db


class UbicacionHuerfana(db.Model):
    __tablename__ = 'ubicaciones_huerfanas'

    id = db.Column(db.Integer, primary_key=True)

    # Datos crudos recibidos de Siesa (sin transformar)
    codigo_siesa = db.Column(db.String(50), nullable=False)
    bodega_id = db.Column(db.String(20), nullable=False)     # f150_id de Siesa
    descripcion = db.Column(db.String(200))                  # f155_descripcion
    activo_siesa = db.Column(db.Boolean)                     # f155_ind_estado

    # Metadatos de cuarentena
    fecha_detectada = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    fecha_ultima_vez = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    veces_detectada = db.Column(db.Integer, default=1, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('codigo_siesa', 'bodega_id', name='uq_huerfana_codigo_bodega'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'codigo_siesa': self.codigo_siesa,
            'bodega_id': self.bodega_id,
            'descripcion': self.descripcion,
            'activo_siesa': self.activo_siesa,
            'fecha_detectada': self.fecha_detectada.isoformat() if self.fecha_detectada else None,
            'fecha_ultima_vez': self.fecha_ultima_vez.isoformat() if self.fecha_ultima_vez else None,
            'veces_detectada': self.veces_detectada,
        }
