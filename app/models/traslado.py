"""
Traslados entre Bodega Principal y Puntos de Venta.

Máquina de estados:
  BORRADOR → ENVIADA → EN_PICKING → EN_PACKING → PREPARADO → EN_TRANSITO → ENTREGADA
                     ↘ RECHAZADA                                          ↘ REVERTIDA
           ↘ CANCELADA  (tienda: BORRADOR/ENVIADA; admin: hasta PREPARADO)

  BORRADOR:    Tienda arma la solicitud
  ENVIADA:     Tienda envía al admin bodega
  EN_PICKING:  Admin aprueba; operario recoge ítems con TareaPicking
  EN_PACKING:  Picking confirmado + RIT 174646 disparada; operario verifica empaque
  PREPARADO:   Packing confirmado + Compromisos 174720 disparados; listo para despachar
  EN_TRANSITO: Admin despacha con 174930; mercancía en camino al PV
  ENTREGADA:   Tienda confirma recepción; ETS 173079 disparada
  REVERTIDA:   Admin revierte un traslado EN_TRANSITO; unidades devueltas al inventario origen
"""
from datetime import datetime
from app.extensions import db
from app.models.picking import TareaPicking


class EstadoTraslado:
    BORRADOR    = 'BORRADOR'
    ENVIADA     = 'ENVIADA'
    EN_PICKING  = 'EN_PICKING'
    EN_PACKING  = 'EN_PACKING'
    PREPARADO   = 'PREPARADO'
    EN_TRANSITO = 'EN_TRANSITO'
    ENTREGADA   = 'ENTREGADA'
    RECHAZADA   = 'RECHAZADA'
    CANCELADA   = 'CANCELADA'
    REVERTIDA   = 'REVERTIDA'


class SolicitudTraslado(db.Model):
    __tablename__ = 'solicitudes_traslado'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(30), unique=True, nullable=False)  # ST-20260403-001

    # Bodegas Siesa
    bodega_origen_siesa = db.Column(db.String(20), nullable=False)   # 'NB1'
    bodega_destino_siesa = db.Column(db.String(20), nullable=False)  # 'TP1'
    nombre_punto_venta = db.Column(db.String(100))

    # Estado
    estado = db.Column(db.String(30), default='BORRADOR', nullable=False)

    # DIRECTA → 173066 una sola pasada
    # EN_TRANSITO → 173076 (salida) + 173079 (entrada)
    modo_transferencia = db.Column(db.String(20), default='EN_TRANSITO')
    bodega_transito_siesa = db.Column(db.String(20))  # solo si EN_TRANSITO

    # Usuarios
    solicitante_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    aprobador_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    operario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Siesa — consecutivos de los documentos creados
    siesa_requisicion_consec = db.Column(db.Integer)  # 174646
    siesa_salida_consec = db.Column(db.Integer)       # 173076 o 173066
    siesa_entrada_consec = db.Column(db.Integer)      # 173079
    siesa_error = db.Column(db.Text)                  # último error de Siesa (para debug)
    #: ¿El 174720 registró los compromisos sobre la RIT? **Decide qué conector
    #: despacha**, y por eso no puede vivir en `siesa_error`.
    #:
    #: El 174930 no manda cantidades: Siesa las toma de lo comprometido en la
    #: RIT. Si el 174720 no entró, ahí siguen las **originales del 174646**, y
    #: el STS sale por lo pedido en vez de por lo empacado. El 173076 sí lleva
    #: `cantidad_enviada`, que es lo real.
    #:
    #: **Se enciende DESPUÉS del POST, no antes.** Es al revés de la Regla 6 a
    #: propósito: acá la bandera no evita un duplicado, **abre una compuerta**.
    #: Un pre-flag dejaría la puerta abierta ante un crash entre el POST y el
    #: commit, y despacharíamos sobre una suposición. Ante la duda, el lado
    #: barato es reenviar el 174720 (reafirma las mismas cantidades sobre la
    #: misma RIT); el caro es mandar a Siesa un STS por cantidades que nadie
    #: empacó.
    siesa_compromisos_ok = db.Column(db.Boolean, default=False,
                                     server_default='false', nullable=False)
    inventario_descontado = db.Column(db.Boolean, default=False, server_default='false')

    # Timestamps
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_envio = db.Column(db.DateTime)
    fecha_aprobacion = db.Column(db.DateTime)
    fecha_despacho = db.Column(db.DateTime)
    fecha_entrega = db.Column(db.DateTime)

    observaciones = db.Column(db.Text)
    motivo_rechazo = db.Column(db.String(200))

    # Relationships
    solicitante = db.relationship('Usuario', foreign_keys=[solicitante_id],
                                  backref='solicitudes_traslado_creadas')
    aprobador = db.relationship('Usuario', foreign_keys=[aprobador_id],
                                backref='solicitudes_traslado_aprobadas')
    operario = db.relationship('Usuario', foreign_keys=[operario_id],
                               backref='solicitudes_traslado_asignadas')
    items = db.relationship('ItemSolicitudTraslado', backref='solicitud',
                            lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        # El error de Siesa es relevante (requiere acción) solo cuando no hay
        # consecutivo de cierre — significa que el movimiento nunca llegó a Siesa.
        consec_cierre = (self.siesa_entrada_consec if self.modo_transferencia == 'EN_TRANSITO'
                         else self.siesa_salida_consec)

        # ENTREGADA **no** silencia el error, y esa era la trampa (2026-08-14):
        # `confirmar_recepcion` pone ENTREGADA aunque el 173079 haya fallado
        # —el estado describe el hecho físico, la mercancía llegó—, así que un
        # traslado con la entrada nunca registrada en Siesa se pintaba en verde
        # y sin aviso. Es exactamente el limbo que los invariantes de traslado
        # existen para detectar: el stock no falta ni sobra, está en la bodega
        # puente, y nadie reclama.
        #
        # Las otras tres sí callan con razón: un traslado RECHAZADO, CANCELADO
        # o REVERTIDO no debe tener documento de cierre en Siesa, así que un
        # error viejo ahí no pide ninguna acción.
        #
        # El `not consec_cierre` sigue siendo la guarda que evita el ruido: si
        # el consecutivo existe, el movimiento llegó y no se avisa nada.
        estados_sin_cierre_esperado = (
            EstadoTraslado.RECHAZADA, EstadoTraslado.CANCELADA,
            EstadoTraslado.REVERTIDA,
        )
        siesa_necesita_atencion = (
            bool(self.siesa_error) and
            not consec_cierre and
            self.estado not in estados_sin_cierre_esperado
        )
        return {
            'id': self.id,
            'codigo': self.codigo,
            'bodega_origen_siesa': self.bodega_origen_siesa,
            'bodega_destino_siesa': self.bodega_destino_siesa,
            'nombre_punto_venta': self.nombre_punto_venta,
            'estado': self.estado,
            'modo_transferencia': self.modo_transferencia,
            'bodega_transito_siesa': self.bodega_transito_siesa,
            'solicitante_id': self.solicitante_id,
            'solicitante_nombre': self.solicitante.nombre if self.solicitante else None,
            'aprobador_id': self.aprobador_id,
            'aprobador_nombre': self.aprobador.nombre if self.aprobador else None,
            'operario_id': self.operario_id,
            'operario_nombre': self.operario.nombre if self.operario else None,
            'siesa_requisicion_consec': self.siesa_requisicion_consec,
            'siesa_salida_consec': self.siesa_salida_consec,
            'siesa_entrada_consec': self.siesa_entrada_consec,
            'siesa_error': self.siesa_error if siesa_necesita_atencion else None,
            'siesa_necesita_atencion': siesa_necesita_atencion,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_envio': self.fecha_envio.isoformat() if self.fecha_envio else None,
            'fecha_aprobacion': self.fecha_aprobacion.isoformat() if self.fecha_aprobacion else None,
            'fecha_despacho': self.fecha_despacho.isoformat() if self.fecha_despacho else None,
            'fecha_entrega': self.fecha_entrega.isoformat() if self.fecha_entrega else None,
            'observaciones': self.observaciones,
            'motivo_rechazo': self.motivo_rechazo,
            'items': [i.to_dict() for i in self.items],
            'total_items': len(self.items),
            'picking_progreso': self._picking_progreso(),
            'packing_info': self._packing_info(),
        }

    def _picking_progreso(self):
        """Progreso de TareasPicking — solo relevante en EN_PICKING/PREPARADO.
        [M9] Single query with conditional count instead of 2 separate COUNT queries.
        """
        if self.estado not in (EstadoTraslado.EN_PICKING, EstadoTraslado.PREPARADO):
            return None
        from sqlalchemy import func as _func, case as _case
        from app.extensions import db as _db
        row = _db.session.query(
            _func.count().label('total'),
            _func.count(_case((TareaPicking.estado == 'COMPLETADO', 1))).label('completadas'),
        ).select_from(TareaPicking).filter(
            TareaPicking.referencia_documento == self.codigo,
            TareaPicking.tipo_documento == 'TRASLADO',
        ).first()
        total, completadas = row.total, row.completadas
        if total == 0:
            return {'total': 0, 'completadas': 0, 'sin_tareas': True}
        return {
            'total': total,
            'completadas': completadas,
            'sin_tareas': False,
            'porcentaje': round(completadas / total * 100),
        }

    def _packing_info(self):
        """TareaPacking activa — relevante en EN_PACKING y PREPARADO (despacho pendiente)."""
        if self.estado not in (EstadoTraslado.EN_PACKING, EstadoTraslado.PREPARADO):
            return None
        tareas = self.tareas_packing
        if not tareas:
            return None
        t = tareas[0]
        return {
            'id': t.id,
            'codigo': t.codigo,
            'estado': t.estado,
            'empacador': t.empacador.nombre if t.empacador else None,
        }


class ItemSolicitudTraslado(db.Model):
    __tablename__ = 'items_solicitud_traslado'

    id = db.Column(db.Integer, primary_key=True)
    solicitud_id = db.Column(db.Integer, db.ForeignKey('solicitudes_traslado.id'), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    producto_codigo_siesa = db.Column(db.String(50))

    cantidad_solicitada = db.Column(db.Integer, nullable=False)
    cantidad_aprobada = db.Column(db.Integer)    # admin ajusta al aprobar
    cantidad_enviada = db.Column(db.Integer, default=0)   # picking confirmado
    cantidad_recibida = db.Column(db.Integer, default=0)  # recepción en tienda

    disponible_siesa = db.Column(db.Integer)  # snapshot en bodega origen al crear

    producto = db.relationship('Producto', backref='items_traslado', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'solicitud_id': self.solicitud_id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'producto_codigo_siesa': self.producto_codigo_siesa,
            'cantidad_solicitada': self.cantidad_solicitada,
            'cantidad_aprobada': self.cantidad_aprobada,
            'cantidad_enviada': self.cantidad_enviada,
            'cantidad_recibida': self.cantidad_recibida,
            'disponible_siesa': self.disponible_siesa,
        }
