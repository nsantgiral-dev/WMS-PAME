"""
El acta de entrega de caja del conductor (m051liqcaja, 2026-09-27).

Hasta acá el WMS cuadraba la plata **consigo mismo** —cobros registrados contra
recibos de caja— y nunca contra la plata física: «liquidar» era un clic sin
arqueo, y un conductor que cobraba $2,1 M y entregaba $2,0 M producía tres
números perfectos. El acta es el único dato de la cadena que no produce ningún
sistema: lo cuenta una persona.

Un acta por conductor y entrega: cubre sus rutas ENTREGADAS que todavía no
tenían acta (`rutas_despacho.entrega_caja_id`) y los gastos que pagó con el
efectivo del recaudo (`entregas_caja_gastos`). La política vive en
`services/caja_conductor.py` y **solo ella escribe estas filas**.
"""
from datetime import datetime

from app.extensions import db


class EstadoEntregaCaja:
    #: Registrada por quien recibió la plata; falta la palabra del conductor.
    PENDIENTE_CONDUCTOR = 'PENDIENTE_CONDUCTOR'
    #: El conductor la confirmó en su teléfono.
    CONFIRMADA = 'CONFIRMADA'
    #: El conductor dijo que no está de acuerdo (con su comentario).
    OBJETADA = 'OBJETADA'
    #: Quien liquida declaró que el conductor no la confirmó (con motivo).
    SIN_CONFIRMAR = 'SIN_CONFIRMAR'
    #: Sin efecto (con motivo, en la bitácora). Sus rutas vuelven a estar sin acta.
    ANULADA = 'ANULADA'

    TODOS = (PENDIENTE_CONDUCTOR, CONFIRMADA, OBJETADA, SIN_CONFIRMAR, ANULADA)
    #: Las que cuentan: todo menos la anulada.
    VIGENTES = (PENDIENTE_CONDUCTOR, CONFIRMADA, OBJETADA, SIN_CONFIRMAR)


class EntregaCaja(db.Model):
    __tablename__ = 'entregas_caja'

    id = db.Column(db.Integer, primary_key=True)
    conductor_id = db.Column(db.Integer, db.ForeignKey('conductores.id'), nullable=False)
    #: El día operativo (Bogotá) en que se recibió la plata.
    dia = db.Column(db.Date, nullable=False)
    estado = db.Column(db.String(20), nullable=False)

    #: Efectivo que el conductor declaró haber cobrado en las rutas del acta.
    esperado_efectivo = db.Column(db.Numeric(14, 2), nullable=False)
    #: Lo que contó quien recibió.
    contado_efectivo = db.Column(db.Numeric(14, 2), nullable=False)
    #: Gastos pagados con el efectivo del recaudo: declarados y aceptados.
    gastos_declarados = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    gastos_aceptados = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    #: contado + gastos aceptados − esperado. Negativo = faltante a cargo del
    #: conductor; positivo = sobrante. Nunca se «ajusta al peso».
    diferencia = db.Column(db.Numeric(14, 2), nullable=False)
    motivo_diferencia = db.Column(db.Text, nullable=True)
    #: La foto de lo que se recibió: rutas, paradas y gastos con sus valores.
    #: Sobrevive a la anulación (las filas vivas se sueltan; esto no).
    detalle = db.Column(db.JSON, nullable=True)

    registrada_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    registrada_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    conductor_respuesta_en = db.Column(db.DateTime, nullable=True)
    conductor_comentario = db.Column(db.Text, nullable=True)
    sin_confirmar_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    sin_confirmar_en = db.Column(db.DateTime, nullable=True)
    sin_confirmar_motivo = db.Column(db.Text, nullable=True)

    anulada_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    anulada_en = db.Column(db.DateTime, nullable=True)
    anulada_motivo = db.Column(db.Text, nullable=True)

    conductor = db.relationship('Conductor', lazy=True)
    gastos = db.relationship('EntregaCajaGasto', backref='entrega', lazy=True)

    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PENDIENTE_CONDUCTOR','CONFIRMADA','OBJETADA','SIN_CONFIRMAR','ANULADA')",
            name='ck_entrega_caja_estado'),
        db.CheckConstraint('contado_efectivo >= 0', name='ck_entrega_caja_contado'),
        # Una diferencia sin motivo es un faltante sin explicación: la base no
        # la acepta aunque un camino futuro se salte la política.
        db.CheckConstraint(
            "diferencia = 0 OR (motivo_diferencia IS NOT NULL AND "
            "length(trim(motivo_diferencia)) > 0)",
            name='ck_entrega_caja_diferencia_con_motivo'),
        db.Index('ix_entregas_caja_conductor_dia', 'conductor_id', 'dia'),
    )

    def to_dict(self) -> dict:
        def _f(v):
            return float(v) if v is not None else None

        def _iso(v):
            return v.isoformat() if v else None
        return {
            'id': self.id,
            'conductor_id': self.conductor_id,
            'conductor': self.conductor.nombre if self.conductor else None,
            'dia': _iso(self.dia),
            'estado': self.estado,
            'esperado_efectivo': _f(self.esperado_efectivo),
            'contado_efectivo': _f(self.contado_efectivo),
            'gastos_declarados': _f(self.gastos_declarados),
            'gastos_aceptados': _f(self.gastos_aceptados),
            'diferencia': _f(self.diferencia),
            'motivo_diferencia': self.motivo_diferencia,
            'detalle': self.detalle or {},
            'registrada_por_id': self.registrada_por_id,
            'registrada_en': _iso(self.registrada_en),
            'conductor_respuesta_en': _iso(self.conductor_respuesta_en),
            'conductor_comentario': self.conductor_comentario,
            'sin_confirmar_en': _iso(self.sin_confirmar_en),
            'sin_confirmar_motivo': self.sin_confirmar_motivo,
            'anulada_en': _iso(self.anulada_en),
            'anulada_motivo': self.anulada_motivo,
        }


class EntregaCajaGasto(db.Model):
    """Un gasto pagado con el efectivo del recaudo, decidido en un acta.

    Un gasto se legaliza **una vez**: `gasto_id` es único. Al anular el acta la
    fila se borra (y el gasto vuelve a quedar por legalizar); lo decidido queda
    en `EntregaCaja.detalle` y en la bitácora.
    """
    __tablename__ = 'entregas_caja_gastos'

    id = db.Column(db.Integer, primary_key=True)
    entrega_id = db.Column(db.Integer, db.ForeignKey('entregas_caja.id'), nullable=False)
    gasto_id = db.Column(db.Integer, db.ForeignKey('flota_gasto.id'), nullable=False, unique=True)
    valor = db.Column(db.Numeric(14, 2), nullable=False)
    aceptado = db.Column(db.Boolean, nullable=False)
    motivo = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.CheckConstraint(
            "aceptado OR (motivo IS NOT NULL AND length(trim(motivo)) > 0)",
            name='ck_entrega_caja_gasto_rechazo_con_motivo'),
    )
