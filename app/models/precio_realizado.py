"""
PrecioRealizado — el precio al que de verdad se vende, por SKU y por C.O.

NO es una lista de precios. Es `valor / cantidad` sobre las ventas reales del
export: neto de descuentos, de la escalera clandestina y de todo lo que ocurre
entre el precio publicado y el que se cobra.

El margen que sale de aquí es el que se obtiene. El de una lista es el que se
cree obtener.

DOBLE USO, y el segundo no estaba previsto:
  1. Cu del newsvendor — reemplaza el margen supuesto por margen medido.
  2. La dispersión del precio realizado de un MISMO SKU entre C.O. es, medida,
     la escalera de precios que existe y nunca se decidió formalmente. Ese
     número no sirve al newsvendor: sirve a la política de precios, que hoy se
     toma a ciegas.
"""
from datetime import datetime
from app.extensions import db


class PrecioRealizado(db.Model):
    __tablename__ = 'precios_realizados'

    id = db.Column(db.Integer, primary_key=True)

    referencia = db.Column(db.String(30), nullable=False)
    # C.O. o canal. NULL = agregado de toda la red para ese SKU.
    centro_operacion = db.Column(db.String(10), nullable=True)
    # Etiqueta del periodo cubierto por el export ('2025-26', 'TOTAL', ...)
    periodo = db.Column(db.String(20), nullable=False, default='TOTAL')

    valor_total = db.Column(db.Numeric(16, 2), nullable=False, default=0)
    cantidad_total = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    precio_realizado = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    lineas = db.Column(db.Integer, default=0)

    fecha_carga = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_precio_realizado_ref_co_per', 'referencia',
                 'centro_operacion', 'periodo', unique=True),
    )

    def to_dict(self):
        return {
            'referencia': self.referencia,
            'centro_operacion': self.centro_operacion,
            'periodo': self.periodo,
            'precio_realizado': float(self.precio_realizado or 0),
            'cantidad_total': float(self.cantidad_total or 0),
            'valor_total': float(self.valor_total or 0),
            'lineas': self.lineas,
        }
