"""
La demanda de compras calculada por el worker (m052comprasg, tanda G).

Una fila por nivel de servicio, reescrita en cada recálculo: la venta de un año,
la demanda del horizonte de cada SKU, su lead time y su régimen —lo caro del
ROP, 30 a 60 s a volumen de producción—. La bandeja SOLO lee esto y le suma
al leer lo que cambia durante el día (existencias, OCs, contenedores y lo que
decidió el comprador). Ningún request lo calcula: ver `compras_rop`.

Se reescribe, no se acumula (una fila de ~6 MB por recálculo, varias por día,
sería una tabla que crece sin que nadie la lea). `sello` dice con qué datos se
calculó; si no coincide con el de ahora, la bandeja muestra esta con su fecha
y «recalculando».
"""
from app.extensions import db


class CalculoRop(db.Model):
    __tablename__ = 'compras_rop_calculo'

    id = db.Column(db.Integer, primary_key=True)
    nivel_servicio = db.Column(db.Numeric(6, 4), nullable=False, unique=True)
    dia = db.Column(db.Date, nullable=True)
    sello = db.Column(db.Text, nullable=True)
    calculado_en = db.Column(db.DateTime, nullable=True)
    duracion_s = db.Column(db.Float, nullable=True)
    skus = db.Column(db.Integer, nullable=True)
    #: JSON de `ArmadorService.calcular_demanda_rop`. NULL si nunca se pudo calcular.
    demanda = db.Column(db.Text, nullable=True)
    #: El último recálculo que falló (el cálculo guardado, si lo hay, sigue siendo el de antes).
    error = db.Column(db.String(500), nullable=True)
    error_en = db.Column(db.DateTime, nullable=True)
