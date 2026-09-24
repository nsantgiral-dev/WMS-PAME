"""
Historia de cada línea de pedido que el WMS vio en Siesa (m034fotos).

`pedidos_siesa` es un read model de lo PENDIENTE: el sync borra una línea en
cuanto deja de tener pendiente (`pedidos_sync_service`), y no guarda cuándo la
vio por primera vez. Así, mañana no se puede contestar «¿cuánto tardó este
pedido desde que apareció hasta que salió?» ni «¿qué pedidos desaparecieron
sin remisionarse?». Siesa tampoco lo guarda de forma consultable: lo que no se
fotografía cada día no existe mañana.

**Solo se agrega y se actualiza — nunca se borra.** Está en
`PROTEGIDAS_ANALITICAS` del acta de corte y es IRRECUPERABLE en
`verificar_restauracion.py`. Sin FK hacia tablas operativas a propósito: el
corte las vacía, y una FK obligaría a borrar esta también.

La escribe `app/services/pedidos_historia.py` y nadie más.
"""
from datetime import datetime

from app.extensions import db


class MotivoSalidaPedido:
    """Por qué una línea dejó de verse como pendiente. **Un solo vocabulario.**

    · `CUMPLIDO`     se vio, en un barrido COMPLETO, con lo remisionado ≥ lo
                     pedido; o Siesa dijo estado 4 del pedido.
    · `DESAPARECIDO` un barrido COMPLETO ya no la trae y todavía no se sabe
                     por qué (el pedido dejó el estado 3: cumplido, anulado o
                     devuelto a aprobado).
    · `ANULADO`      Siesa dijo estado 9 del pedido.
    · `OTRO_ESTADO`  Siesa dijo otro estado (p. ej. volvió a 2).

    Ninguno se escribe desde un barrido incompleto: los pedidos de las páginas
    no leídas se leen igual que los que ya no existen (Regla 0).
    """
    CUMPLIDO = 'CUMPLIDO'
    DESAPARECIDO = 'DESAPARECIDO'
    ANULADO = 'ANULADO'
    OTRO_ESTADO = 'OTRO_ESTADO'
    TODOS = (CUMPLIDO, DESAPARECIDO, ANULADO, OTRO_ESTADO)


class PedidoHistoria(db.Model):
    __tablename__ = 'pedidos_historia'

    id = db.Column(db.Integer, primary_key=True)
    #: `f431_rowid` — el id real de la línea en Siesa. Dos líneas del mismo ítem
    #: en un pedido son dos filas, cosa que la clave de `pedidos_siesa` no ve.
    linea_rowid = db.Column(db.BigInteger, nullable=False)
    pedido_clave = db.Column(db.String(40), index=True)
    co = db.Column(db.String(10))
    tipo_docto = db.Column(db.String(10))
    consec_docto = db.Column(db.Integer)
    bodega = db.Column(db.String(10))
    item_codigo = db.Column(db.String(60))
    item_id_siesa = db.Column(db.String(50))
    cliente_id = db.Column(db.String(40))
    cliente = db.Column(db.String(200))
    municipio = db.Column(db.String(100))
    vendedor_id = db.Column(db.String(40))
    cond_pago = db.Column(db.String(10))
    fecha_pedido = db.Column(db.Date)
    fecha_entrega = db.Column(db.Date)

    #: Lo pedido la PRIMERA vez que se vio y la última. Si difieren, el pedido
    #: se editó en Siesa después de aparecer.
    cantidad_pedida_inicial = db.Column(db.Numeric(14, 4))
    cantidad_pedida = db.Column(db.Numeric(14, 4))
    cantidad_remisionada = db.Column(db.Numeric(14, 4))
    cantidad_comprometida = db.Column(db.Numeric(14, 4))
    vlr_neto = db.Column(db.Numeric(16, 2))
    estado_siesa = db.Column(db.Integer)

    #: UTC naive (como toda columna técnica) + el día Bogotá ya resuelto: la
    #: pregunta analítica es «qué día», y restar días UTC a días Bogotá es el
    #: defecto que ya costó cinco sitios (2026-09-23).
    primera_vez_vista_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    primer_dia_visto = db.Column(db.Date, nullable=False)
    ultima_vez_vista_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    ultimo_dia_visto = db.Column(db.Date, nullable=False)

    #: `NULL` mientras se sigue viendo. Se llena solo tras un barrido completo.
    salida_at = db.Column(db.DateTime)
    salida_dia = db.Column(db.Date)
    motivo_salida = db.Column(db.String(20))
    estado_siesa_salida = db.Column(db.Integer)
    #: Veces que volvió a aparecer después de haber salido (un pedido que
    #: Siesa devolvió a estado 3). La salida anterior se pierde; el contador no.
    reapariciones = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint('linea_rowid', name='uq_pedidos_historia_linea_rowid'),
        db.Index('ix_pedidos_historia_abiertas', 'salida_at'),
        db.CheckConstraint(
            "motivo_salida IS NULL OR motivo_salida IN "
            "('CUMPLIDO','DESAPARECIDO','ANULADO','OTRO_ESTADO')",
            name='ck_pedidos_historia_motivo'),
    )

    def to_dict(self):
        def _f(v):
            return float(v) if v is not None else None
        return {
            'linea_rowid': self.linea_rowid,
            'pedido_clave': self.pedido_clave,
            'item_codigo': self.item_codigo,
            'cliente': self.cliente,
            'cantidad_pedida_inicial': _f(self.cantidad_pedida_inicial),
            'cantidad_pedida': _f(self.cantidad_pedida),
            'cantidad_remisionada': _f(self.cantidad_remisionada),
            'primer_dia_visto': self.primer_dia_visto.isoformat() if self.primer_dia_visto else None,
            'ultimo_dia_visto': self.ultimo_dia_visto.isoformat() if self.ultimo_dia_visto else None,
            'salida_dia': self.salida_dia.isoformat() if self.salida_dia else None,
            'motivo_salida': self.motivo_salida,
            'estado_siesa_salida': self.estado_siesa_salida,
            'reapariciones': self.reapariciones,
        }
