"""
La venta diaria que Siesa ya sumó — «Siesa calcula, el WMS decide» (m050demanda).

Una fila por (día, bodega, referencia) con lo vendido (concepto 501 en salida)
y lo devuelto (502 en entrada), leída de la consulta dinámica que agrega la
T470 en Siesa (`demanda_fuentes.SQL_VENTAS_DIA`). No es el kardex movimiento a
movimiento: son las sumas que el motor de compras necesita, calculadas donde
están los datos.

**Un día vale solo si una lectura COMPLETA lo cubrió** (`DemandaDiaCubierto`).
La consulta devuelve solo filas con venta o devolución; un día cubierto sin
fila para un SKU es un cero verdadero («Siesa leyó ese día y ese SKU no se
vendió»), y un día NO cubierto es un hueco («no se sabe»). Confundir los dos
es fabricar ceros (Regla 0), por eso la cobertura es su propia tabla y no se
infiere de las fechas que aparecen.

Nada se borra: una fila que Siesa deja de reportar para un día que una lectura
completa volvió a cubrir (una factura anulada) queda en cero, con el
`registro_id` de esa lectura.

Quién la lee y quién la escribe — nadie más (`tests/test_demanda_fuentes.py`):
  · escribe  → `demanda_fuentes._guardar_lectura`
  · cobertura → `demanda_fuentes.cobertura_siesa`
  · numerador → `kardex_service.serie_demanda`
"""
from datetime import datetime

from app.extensions import db


class DemandaDiaSiesa(db.Model):
    __tablename__ = 'demanda_dia_siesa'

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False)
    bodega = db.Column(db.String(10), nullable=False)
    referencia = db.Column(db.String(50), nullable=False)
    vendido = db.Column(db.Numeric(16, 4), nullable=False, default=0)
    devuelto = db.Column(db.Numeric(16, 4), nullable=False, default=0)
    lineas = db.Column(db.Integer)
    registro_id = db.Column(db.Integer)              # registros_sync de la lectura
    actualizada_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index('uq_demanda_dia_siesa', 'fecha', 'bodega', 'referencia', unique=True),
        db.Index('ix_demanda_dia_siesa_ref_fecha', 'referencia', 'fecha'),
    )


class DemandaDiaCubierto(db.Model):
    """Los días que una lectura COMPLETA de Siesa cubrió. Sin fila = hueco."""
    __tablename__ = 'demanda_dia_cubierto'

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False)
    registro_id = db.Column(db.Integer)
    consulta = db.Column(db.String(120))
    leido_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index('uq_demanda_dia_cubierto_fecha', 'fecha', unique=True),
    )
