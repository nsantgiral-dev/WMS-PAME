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
  · valor y costo → `demanda_fuentes.valor_realizado`
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
    #: Valor SIN impuesto y costo promedio de lo vendido y lo devuelto ese día
    #: (m052comprasc). NULL = la consulta registrada no los trae: no se sabe,
    #: no es cero. `lineas_sin_*` cuenta las líneas que Siesa trajo sin valor
    #: (o sin costo): con alguna, el precio del día no es el precio.
    valor_vendido = db.Column(db.Numeric(18, 4))
    valor_devuelto = db.Column(db.Numeric(18, 4))
    costo_vendido = db.Column(db.Numeric(18, 4))
    costo_devuelto = db.Column(db.Numeric(18, 4))
    lineas_sin_valor = db.Column(db.Integer)
    lineas_sin_costo = db.Column(db.Integer)
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


class DemandaVentanaLectura(db.Model):
    """Dónde quedó la lectura de cada PERÍODO del histórico (m052comprasc).

    Un período es una consulta con fechas fijas (`demanda_fuentes.
    ventanas_historicas`), ordenada por fecha descendente: su numeración no
    cambia de un día a otro, así que la lectura se retoma en `orden_hasta + 1`
    en vez de volver a la página 1. Antes de retomar se relee la fila
    `orden_hasta` y se exige que sea el ancla guardada (y el mismo total); si
    no, el período se movió (un documento anulado o fechado atrás) y se relee
    entero.

    OPERATIVA (se vacía en el corte) y REGENERABLE: es solo un marcador."""
    __tablename__ = 'demanda_ventana_lectura'

    id = db.Column(db.Integer, primary_key=True)
    consulta = db.Column(db.String(120), nullable=False)
    desde = db.Column(db.Date)
    fin = db.Column(db.Date)
    total_filas = db.Column(db.Integer)
    #: La última fila de un día entero ya guardado: la lectura sigue en la +1.
    orden_hasta = db.Column(db.Integer)
    #: El primer día del tramo ya cubierto (lo cubierto va de acá al `hasta`
    #: del período; la lectura avanza hacia atrás).
    cubierto_desde = db.Column(db.Date)
    ancla_fecha = db.Column(db.Date)
    ancla_bodega = db.Column(db.String(10))
    ancla_referencia = db.Column(db.String(50))
    completa = db.Column(db.Boolean, nullable=False, default=False)
    #: 401: la consulta no está registrada en Siesa (o sin permiso).
    sin_registrar = db.Column(db.Boolean, nullable=False, default=False)
    motivo = db.Column(db.String(500))
    paginas = db.Column(db.Integer, default=0)
    registro_id = db.Column(db.Integer)
    leida_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index('uq_demanda_ventana_lectura_consulta', 'consulta', unique=True),
    )
