from datetime import datetime
from app.extensions import db


class RecaudoEntrega(db.Model):
    """
    Captura de pago y estado de entrega por factura (TareaPacking) en una ruta.
    Unidad de pago = pedido/factura, NO bulto físico.
    """
    __tablename__ = 'recaudos_entrega'

    id              = db.Column(db.Integer, primary_key=True)
    ruta_id         = db.Column(db.Integer, db.ForeignKey('rutas_despacho.id'), nullable=False)
    tarea_id        = db.Column(db.Integer, db.ForeignKey('tareas_packing.id'), nullable=False)

    # Estado de la parada
    estado_entrega  = db.Column(db.String(20), nullable=False)
    # ENTREGADO | PARCIAL | RECHAZADO

    # Recaudo
    forma_pago      = db.Column(db.String(30))   # EFECTIVO | TRANSFERENCIA | CHEQUE | CREDITO | EXENTO
    monto_cobrado   = db.Column(db.Numeric(12, 2), default=0)
    observaciones   = db.Column(db.Text)

    # Foto evidencia — JPEG base64, máx ~800KB (1.1MB raw)
    foto_entrega    = db.Column(db.Text)

    # IDs de bultos rechazados (para reingreso)
    bultos_rechazados_ids = db.Column(db.JSON, default=list)

    # Trazabilidad
    confirmado_por  = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    editado_por     = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    editado_en      = db.Column(db.DateTime)

    fecha_confirmacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_creacion     = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    ruta              = db.relationship('RutaDespacho', backref='recaudos', lazy=True)
    tarea             = db.relationship('TareaPacking', backref='recaudo_entrega', uselist=False, lazy=True)
    usuario_confirmador = db.relationship('Usuario', foreign_keys=[confirmado_por], lazy=True)
    usuario_editor      = db.relationship('Usuario', foreign_keys=[editado_por], lazy=True)

    def to_dict(self):
        return {
            'id':                    self.id,
            'ruta_id':               self.ruta_id,
            'tarea_id':              self.tarea_id,
            'estado_entrega':        self.estado_entrega,
            'forma_pago':            self.forma_pago or '',
            'monto_cobrado':         float(self.monto_cobrado) if self.monto_cobrado else 0,
            'observaciones':         self.observaciones or '',
            'foto_entrega':          self.foto_entrega or '',
            'bultos_rechazados_ids': self.bultos_rechazados_ids or [],
            'confirmado_por':        self.confirmado_por,
            'editado_por':           self.editado_por,
            'editado_en':            self.editado_en.isoformat() if self.editado_en else None,
            'fecha_confirmacion':    self.fecha_confirmacion.isoformat(),
        }
