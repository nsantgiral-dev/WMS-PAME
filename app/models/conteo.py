from datetime import datetime
from app.extensions import db


class EstadoConteo:
    PENDIENTE       = 'PENDIENTE'
    EN_PROCESO      = 'EN_PROCESO'
    BLOQUEADO       = 'BLOQUEADO'
    MATCH           = 'MATCH'
    DESCUADRE       = 'DESCUADRE'
    SEGUNDO_CONTEO  = 'SEGUNDO_CONTEO'
    AJUSTANDO       = 'AJUSTANDO'   # transición: lock liberado, Siesa en vuelo
    AJUSTADO        = 'AJUSTADO'
    CANCELADO       = 'CANCELADO'


class SesionConteo(db.Model):
    __tablename__ = 'sesiones_conteo'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)

    # Origen de la tarea
    # DIARIO_ABC (generado automático desde Siesa) o MANUAL
    tipo = db.Column(db.String(20), default='DIARIO_ABC', nullable=False)
    clasificacion_abc = db.Column(db.String(1))  # A, B, C

    # Ubicación a contar
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)

    # Producto
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    producto_codigo_siesa = db.Column(db.String(50))
    maneja_lote = db.Column(db.Boolean, default=False)

    # Operario asignado
    operario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Estado
    # PENDIENTE → EN_PROCESO → MATCH → DESCUADRE → SEGUNDO_CONTEO → AJUSTADO → CANCELADO
    estado = db.Column(db.String(20), default='PENDIENTE', nullable=False)

    # Conteo — NUNCA exponer existencia_siesa al operario
    existencia_siesa = db.Column(db.Integer)  # Oculto — viene de Siesa en tiempo real
    cantidad_fisica = db.Column(db.Integer)   # Lo que contó el operario
    lote_id = db.Column(db.String(50))        # Obligatorio si maneja_lote=True

    # Diferencia calculada al conciliar
    diferencia = db.Column(db.Integer)
    motivo_codigo = db.Column(db.String(20))  # AJ-ENT o AJ-SAL

    # Segundo conteo (double-blind)
    es_segundo_conteo = db.Column(db.Boolean, default=False)
    sesion_origen_id = db.Column(db.Integer, db.ForeignKey('sesiones_conteo.id'), nullable=True)
    segundo_operario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Origen de la excepción (si vino de picking)
    tarea_picking_id = db.Column(db.Integer, db.ForeignKey('tareas_picking.id'), nullable=True)

    # Trigger a Siesa
    siesa_triggered   = db.Column(db.Boolean, default=False)
    siesa_response    = db.Column(db.Text)
    siesa_triggered_at = db.Column(db.DateTime)
    # Quién aprobó el ajuste (admin — nunca el operario que contó)
    aprobador_id      = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    # Clave de idempotencia — ADJ-{id}-{timestamp_ms}
    idempotency_key   = db.Column(db.String(80), unique=True, nullable=True)

    # Tiempos
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_inicio = db.Column(db.DateTime)
    fecha_cierre = db.Column(db.DateTime)

    # Auditoría de edición admin
    editado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    editado_en = db.Column(db.DateTime)
    motivo_edicion = db.Column(db.Text)

    # Relaciones
    ubicacion = db.relationship('Ubicacion', backref='sesiones_conteo', lazy=True)
    producto = db.relationship('Producto', backref='sesiones_conteo', lazy=True)
    operario = db.relationship('Usuario', foreign_keys=[operario_id],
                               backref='conteos_asignados', lazy=True)
    segundo_operario = db.relationship('Usuario', foreign_keys=[segundo_operario_id],
                                       backref='segundos_conteos', lazy=True)
    aprobador = db.relationship('Usuario', foreign_keys=[aprobador_id],
                                backref='ajustes_aprobados', lazy=True)
    editor = db.relationship('Usuario', foreign_keys=[editado_por],
                             backref='conteos_editados', lazy=True)
    # Relación padre → hijo (segundo conteo generado por este)
    # uselist=False: cada sesión tiene máximo un hijo directo.
    # remote_side=[id] en el backref desambigua la dirección self-referencial.
    hijo_conteo = db.relationship(
        'SesionConteo',
        foreign_keys=[sesion_origen_id],
        backref=db.backref('sesion_padre', remote_side=[id], lazy='select'),
        uselist=False,
        lazy='select',
    )

    def to_dict_operario(self):
        """Vista para el operario — SIN cantidad esperada (conteo ciego).
        No exponer es_segundo_conteo: el operario no debe saber si está verificando."""
        return {
            'id': self.id,
            'codigo': self.codigo,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'maneja_lote': self.maneja_lote,
            'estado': self.estado,
        }

    def to_dict(self):
        """Vista completa para supervisores."""
        return {
            'id': self.id,
            'codigo': self.codigo,
            'tipo': self.tipo,
            'clasificacion_abc': self.clasificacion_abc,
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'almacen_id': self.almacen_id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'maneja_lote': self.maneja_lote,
            'operario_id': self.operario_id,
            'estado': self.estado,
            'existencia_siesa': self.existencia_siesa,
            'cantidad_fisica': self.cantidad_fisica,
            'lote_id': self.lote_id,
            'diferencia': self.diferencia,
            'motivo_codigo': self.motivo_codigo,
            'es_segundo_conteo': self.es_segundo_conteo,
            'sesion_origen_id': self.sesion_origen_id,
            'tarea_picking_id':  self.tarea_picking_id,
            'siesa_triggered':   self.siesa_triggered,
            'siesa_triggered_at': self.siesa_triggered_at.isoformat() if self.siesa_triggered_at else None,
            'aprobador_id':      self.aprobador_id,
            'aprobador_nombre':  self.aprobador.nombre if self.aprobador else None,
            'idempotency_key':   self.idempotency_key,
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_inicio': self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            'fecha_cierre': self.fecha_cierre.isoformat() if self.fecha_cierre else None,
            'editado_por': self.editado_por,
            'editado_por_nombre': self.editor.nombre if self.editor else None,
            'editado_en': self.editado_en.isoformat() if self.editado_en else None,
            'motivo_edicion': self.motivo_edicion,
            # Datos del segundo conteo (hijo) para que el admin pueda comparar
            # sin necesitar un request adicional.
            'segundo_conteo': {
                'id': self.hijo_conteo.id,
                'estado': self.hijo_conteo.estado,
                'cantidad_fisica': self.hijo_conteo.cantidad_fisica,
                'operario_id': self.hijo_conteo.operario_id,
                'operario_nombre': (
                    self.hijo_conteo.operario.nombre
                    if self.hijo_conteo.operario else None
                ),
                'fecha_cierre': (
                    self.hijo_conteo.fecha_cierre.isoformat()
                    if self.hijo_conteo.fecha_cierre else None
                ),
            } if self.hijo_conteo else None,
        }