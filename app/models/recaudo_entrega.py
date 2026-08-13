from datetime import datetime
from app.extensions import db


class EstadoEntrega:
    ENTREGADO = 'ENTREGADO'
    PARCIAL = 'PARCIAL'
    RECHAZADO = 'RECHAZADO'
    TODOS = (ENTREGADO, PARCIAL, RECHAZADO)


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

    # Detalle de referencias en entrega parcial
    # [{"codigo": "REF001", "nombre": "Papel A4", "pedido": 10, "entregado": 7, "devuelto": 3}]
    items_entregados = db.Column(db.JSON, nullable=True)

    # ── Liquidación Siesa ────────────────────────────────────────────
    # Causal de devolución DIAN para 142946 (NotaFactura)
    causal_devolucion = db.Column(db.String(10), nullable=True)
    # Tipo de descuento/retención (RETEFUENTE | RETEIVA | ICA | OTRO)
    motivo_descuento  = db.Column(db.String(30), nullable=True)
    # Monto del descuento/retención aplicado
    monto_descuento   = db.Column(db.Numeric(12, 2), default=0)

    # Retenciones detalladas (reemplaza motivo_descuento para multi-retención)
    # [{tipo, puc, tasa, monto, base, siesa_triggered, job_id}]
    retenciones_detalle = db.Column(db.JSON, nullable=True)

    #: En qué modo estaba la pantalla del conductor al confirmar esta parada.
    #: `LIBRE` es el caso de riesgo: elige forma de pago sin restricción,
    #: incluido CREDITO en una parada de contado. `NULL` = se confirmó antes de
    #: que esto se midiera, que NO es lo mismo que LIBRE.
    #: Mismo CHECK que la migración — si viviera solo allá, `create_all()` no
    #: lo tendría y ningún test lo ejercitaría.
    modo_pantalla = db.Column(db.String(12), nullable=True)

    __table_args__ = (
        db.CheckConstraint(
            "modo_pantalla IS NULL OR modo_pantalla IN ('CREDITO','DINAMICO','LIBRE')",
            name='ck_recaudo_modo_pantalla'),
    )

    # Idempotencia Siesa — flags independientes por conector
    siesa_rc_triggered  = db.Column(db.Boolean, default=False)   # 142888 ReciboCaja
    siesa_nc_triggered  = db.Column(db.Boolean, default=False)   # 142946 NotaFactura
    siesa_dc_triggered  = db.Column(db.Boolean, default=False)   # 142882 DocumentoContable

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

    def to_dict(self, include_foto=False):
        d = {
            'id':                    self.id,
            'ruta_id':               self.ruta_id,
            'tarea_id':              self.tarea_id,
            'estado_entrega':        self.estado_entrega,
            'forma_pago':            self.forma_pago or '',
            'modo_pantalla':         self.modo_pantalla,
            'monto_cobrado':         float(self.monto_cobrado) if self.monto_cobrado else 0,
            'observaciones':         self.observaciones or '',
            'bultos_rechazados_ids': self.bultos_rechazados_ids or [],
            'items_entregados':      self.items_entregados or [],
            'causal_devolucion':     self.causal_devolucion or '',
            'motivo_descuento':      self.motivo_descuento or '',
            'monto_descuento':       float(self.monto_descuento) if self.monto_descuento else 0,
            'siesa_rc_triggered':    self.siesa_rc_triggered or False,
            'siesa_nc_triggered':    self.siesa_nc_triggered or False,
            'siesa_dc_triggered':    self.siesa_dc_triggered or False,
            'retenciones_detalle':   self.retenciones_detalle or [],
            'confirmado_por':        self.confirmado_por,
            'editado_por':           self.editado_por,
            'editado_en':            self.editado_en.isoformat() if self.editado_en else None,
            'fecha_confirmacion':    self.fecha_confirmacion.isoformat(),
        }
        if include_foto:
            d['foto_entrega'] = self.foto_entrega or ''
        return d
