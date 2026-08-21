from datetime import datetime
from app.extensions import db


class EstadoEntrega:
    """Qué pasó en la puerta del cliente. **Un solo catálogo.**

    Hasta el 2026-08-13 esta clase estaba definida **dos veces** —acá y en
    `services/ruta_service.py`— con los mismos tres valores y distinto nombre
    para la tupla (`TODOS` / `VALIDOS`). Agregar un estado a una y no a la otra
    era cuestión de tiempo.
    """
    ENTREGADO = 'ENTREGADO'
    PARCIAL = 'PARCIAL'
    RECHAZADO = 'RECHAZADO'
    #: **La mercancía se entregó y el cliente no pagó.** No es un rechazo:
    #: `RECHAZADO` significa que los bultos vuelven al camión, y acá no vuelven.
    #:
    #: Existía como motivo dentro de `RECHAZADO` y el modelo se contradecía —
    #: el estado afirmaba una cosa y el motivo la negaba. Mientras fue
    #: excepcional se podía vivir con eso; el control «si no paga completo, no
    #: se entrega» lo vuelve cotidiano, y entonces **cada consumidor del estado
    #: tendría que acordarse de mirar el motivo**. Ya pasó una vez, con la
    #: lista de reingreso de bodega.
    ENTREGADO_SIN_PAGO = 'ENTREGADO_SIN_PAGO'

    TODOS = (ENTREGADO, PARCIAL, RECHAZADO, ENTREGADO_SIN_PAGO)
    #: Alias del nombre que usaba la copia de `ruta_service`. Se conserva para
    #: no romper llamadores; es la misma tupla, no otra lista.
    VALIDOS = TODOS

    #: Lo que el conductor puede mandar. `ENTREGADO_SIN_PAGO` **no está**: no
    #: es un botón, lo deriva el servidor del motivo del rechazo. La pregunta
    #: que se le hace al conductor sigue siendo «¿volvió la mercancía?», que
    #: es la que sabe contestar — no «¿qué estado contable corresponde?».
    ACEPTADOS_DEL_CONDUCTOR = (ENTREGADO, PARCIAL, RECHAZADO)

    #: Estados en los que la mercancía **no volvió al camión**. La lista de
    #: reingreso de bodega se define por acá y no por el estado del bulto.
    SIN_RETORNO = (ENTREGADO, PARCIAL, ENTREGADO_SIN_PAGO)


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

    #: Por qué no se entregó, tipificado. `NULL` = se confirmó antes de que se
    #: preguntara, que no es lo mismo que «no hubo motivo».
    #: El catálogo vive en `services/motivos_rechazo.py` — incluido el caso que
    #: hoy es invisible: no pagó **y se quedó con la mercancía**, donde el
    #: estado dice RECHAZADO pero el inventario no volvió.
    motivo_rechazo = db.Column(db.String(30), nullable=True)

    #: Decisión del admin sobre el `motivo_descuento` que el conductor
    #: declaró en campo (lo que el cliente dijo, sin verificar). `None` =
    #: pendiente — el RC queda bloqueado hasta que se decida (Regla 0: ante
    #: dato sin verificar, no se asume). `True` = el cliente sí tenía derecho
    #: al descuento, sigue el flujo normal (RC neto + DC de retención).
    #: `False` = no tenía derecho — el RC sigue bloqueado hasta que el monto
    #: usado alcance el valor completo de la factura (el admin lo corrige a
    #: mano cuando el cliente paga la diferencia; no hay un segundo estado de
    #: "ya pagó el resto" en el WMS a propósito).
    retencion_confirmada = db.Column(db.Boolean, nullable=True)
    retencion_confirmada_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    retencion_confirmada_en = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        # Un recaudo por parada. Lo comprueba `ruta_service.py:978` con un
        # `.first()` sin bloqueo, y hay DOS escritores —`confirmar_parada` y
        # `forzar_cierre_ruta`—, así que no hace falta concurrencia exótica.
        # Con dos filas, `total_recaudado()` suma las dos, la liquidación
        # emite dos RC, y el congelamiento del monto tras el RC se decide con
        # un `.first()` sin `order_by`: dispara o no según el orden del heap.
        db.Index('uq_recaudo_por_parada', 'ruta_id', 'tarea_id', unique=True),
        db.CheckConstraint(
            "modo_pantalla IS NULL OR modo_pantalla IN ('CREDITO','DINAMICO','LIBRE')",
            name='ck_recaudo_modo_pantalla'),
        db.CheckConstraint(
            "motivo_rechazo IS NULL OR motivo_rechazo IN ("
            "'CLIENTE_CERRADO','DIRECCION_ERRADA','NO_PIDIO','MERCANCIA_AVERIADA',"
            "'FUERA_DE_HORARIO','NO_PAGO','NO_PAGO_SE_QUEDO')",
            name='ck_recaudo_motivo_rechazo'),
        db.CheckConstraint(
            "estado_entrega IN ('ENTREGADO','PARCIAL','RECHAZADO','ENTREGADO_SIN_PAGO')",
            name='ck_recaudo_estado_entrega'),
        # El invariante que da sentido al estado nuevo: si la mercancía se
        # quedó con el cliente, el estado tiene que decirlo. Sin esto, la
        # combinación vieja —RECHAZADO + NO_PAGO_SE_QUEDO— podría volver a
        # entrar por cualquier camino que no pase por `confirmar_parada`, y el
        # modelo se contradiría otra vez sin que nadie lo note.
        db.CheckConstraint(
            "motivo_rechazo IS NULL OR motivo_rechazo <> 'NO_PAGO_SE_QUEDO' "
            "OR estado_entrega = 'ENTREGADO_SIN_PAGO'",
            name='ck_recaudo_sin_pago_no_es_rechazo'),
    )

    # Idempotencia Siesa — flags independientes por conector
    siesa_rc_triggered  = db.Column(db.Boolean, default=False)   # 142888 ReciboCaja
    siesa_nc_triggered  = db.Column(db.Boolean, default=False)   # 142946 NotaFactura
    siesa_dc_triggered  = db.Column(db.Boolean, default=False)   # 142882 DocumentoContable
    #: Las cuentas PUC de retención **ya enviadas**, en JSON.
    #:
    #: `siesa_dc_triggered` es un booleano y las retenciones son N: un cliente
    #: con retefuente + reteIVA + ICA genera tres documentos distintos. Con una
    #: sola bandera, el primer job la enciende y los otros dos se declaran
    #: idempotentes **sin enviar nada** — el tablero cuenta tres completados y
    #: en Siesa hay uno.
    siesa_dc_pucs = db.Column(db.Text, nullable=True)

    # Trazabilidad
    confirmado_por  = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    editado_por     = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    editado_en      = db.Column(db.DateTime)

    fecha_confirmacion = db.Column(db.DateTime, default=datetime.utcnow)

    # ── Retenciones ya enviadas, por cuenta PUC ──────────────────────────
    def pucs_enviadas(self) -> set:
        import json
        try:
            return set(json.loads(self.siesa_dc_pucs or '[]'))
        except (ValueError, TypeError):
            # Un JSON ilegible se trata como «no sé qué se envió», y ante no
            # saber NO se reenvía: un documento contable duplicado es un ajuste
            # manual en el ERP. Regla 0.
            return {'__ILEGIBLE__'}

    def marcar_puc_enviada(self, cuenta_puc: str):
        import json
        pucs = self.pucs_enviadas() | {cuenta_puc}
        self.siesa_dc_pucs = json.dumps(sorted(pucs))
        self.siesa_dc_triggered = True

    def desmarcar_puc(self, cuenta_puc: str):
        """Revierte ante fallo explícito — la otra mitad del pre-flag."""
        import json
        pucs = self.pucs_enviadas() - {cuenta_puc}
        self.siesa_dc_pucs = json.dumps(sorted(pucs))
        self.siesa_dc_triggered = bool(pucs)
    fecha_creacion     = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    ruta              = db.relationship('RutaDespacho', backref='recaudos', lazy=True)
    tarea             = db.relationship('TareaPacking', backref='recaudo_entrega', uselist=False, lazy=True)
    usuario_confirmador = db.relationship('Usuario', foreign_keys=[confirmado_por], lazy=True)
    usuario_editor      = db.relationship('Usuario', foreign_keys=[editado_por], lazy=True)
    usuario_retencion   = db.relationship('Usuario', foreign_keys=[retencion_confirmada_por], lazy=True)

    def to_dict(self, include_foto=False):
        d = {
            'id':                    self.id,
            'ruta_id':               self.ruta_id,
            'tarea_id':              self.tarea_id,
            'estado_entrega':        self.estado_entrega,
            'forma_pago':            self.forma_pago or '',
            'modo_pantalla':         self.modo_pantalla,
            'motivo_rechazo':        self.motivo_rechazo,
            'monto_cobrado':         float(self.monto_cobrado) if self.monto_cobrado else 0,
            'observaciones':         self.observaciones or '',
            'bultos_rechazados_ids': self.bultos_rechazados_ids or [],
            'items_entregados':      self.items_entregados or [],
            'causal_devolucion':     self.causal_devolucion or '',
            'motivo_descuento':      self.motivo_descuento or '',
            'monto_descuento':       float(self.monto_descuento) if self.monto_descuento else 0,
            'retencion_confirmada':  self.retencion_confirmada,
            'retencion_confirmada_por': self.retencion_confirmada_por,
            'retencion_confirmada_en': self.retencion_confirmada_en.isoformat() if self.retencion_confirmada_en else None,
            'siesa_rc_triggered':    self.siesa_rc_triggered or False,
            'siesa_nc_triggered':    self.siesa_nc_triggered or False,
            'siesa_dc_triggered':    self.siesa_dc_triggered or False,
            # `siesa_triggered` en cada línea se guardó en True al ENCOLAR el
            # job (`registrar_cobro_recaudo`), no al confirmarse el envío —
            # un DC que Siesa rechaza (job 482, recaudo 22, PD1425, ruta 23,
            # 2026-08-21) se mostraba con ✓ en pantalla aunque nunca llegó a
            # Siesa. `pucs_enviadas()` sí es la marca real (pre-flag antes
            # del POST, revertida si falla) — se recalcula acá para que el
            # dato que sale de este modelo sea el mismo para cualquier
            # pantalla que lo consuma, no una copia que puede quedar vieja.
            'retenciones_detalle':   [
                {**rd, 'siesa_triggered': rd.get('puc') in self.pucs_enviadas()}
                for rd in (self.retenciones_detalle or [])
            ],
            'confirmado_por':        self.confirmado_por,
            'editado_por':           self.editado_por,
            'editado_en':            self.editado_en.isoformat() if self.editado_en else None,
            'fecha_confirmacion':    self.fecha_confirmacion.isoformat(),
        }
        if include_foto:
            d['foto_entrega'] = self.foto_entrega or ''
        return d
