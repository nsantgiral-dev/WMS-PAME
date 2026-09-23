from datetime import datetime
from app.extensions import db


class EstadoConteo:
    PENDIENTE       = 'PENDIENTE'
    EN_PROCESO      = 'EN_PROCESO'
    BLOQUEADO       = 'BLOQUEADO'
    MATCH           = 'MATCH'
    DESCUADRE       = 'DESCUADRE'
    SEGUNDO_CONTEO  = 'SEGUNDO_CONTEO'
    TERCER_CONTEO   = 'TERCER_CONTEO'   # CC1 esperando CC3 porque CC1≠CC2
    AJUSTANDO       = 'AJUSTANDO'       # transición: lock liberado, Siesa en vuelo
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
    #: **Contra qué se comparó de verdad.** `'SIESA'` | `'WMS'` | `None`.
    #:
    #: El nombre `existencia_siesa` promete procedencia y no la tenía: cuando
    #: Siesa no respondía, el servicio caía al stock del WMS **con solo un
    #: WARNING** y lo guardaba en la misma columna. El propio docstring de
    #: `comparar_conteo` lo admitía — «compara contra existencia_siesa (que
    #: ahora almacena stock WMS)».
    #:
    #: Y el ajuste que sale a Siesa es un DELTA: `fisica − existencia_siesa`.
    #: Si esa base fue el número del WMS, Siesa queda en
    #: `siesa_real + (fisica − wms)` en vez de en `fisica`. Es decir:
    #: **precisamente cuando WMS y Siesa discrepan —la única razón para
    #: contar— el ajuste empeora el descuadre.**
    #:
    #: Sin esta columna ningún invariante podía comprobarlo: el defecto no era
    #: solo indetectable, era inauditable.
    fuente_existencia = db.Column(db.String(10), nullable=True)
    #: **La foto de Siesa tomada al contar** (2026-09-23). Las cuatro juntas o
    #: ninguna — `ConteoService.consultar_foto_siesa` no devuelve media foto.
    #:
    #: `existencia_siesa` sola no es contra qué comparar en una tienda:
    #: `f400_cant_existencia_1` todavía incluye la venta POS que Siesa no ha
    #: acumulado (`f400_cant_pos_1`). La mercancía ya se fue del estante, así
    #: que un conteo honesto sale «corto» exactamente en esa cantidad; si se
    #: ajusta, la acumulación del POS la vuelve a descontar al día siguiente.
    #: Doble descuento. La base es el **teórico**: `existencia − cant_pos`.
    #:
    #: Y la foto se guarda —no se vuelve a leer al aprobar— porque el ajuste es
    #: un DELTA medido en un instante: `cantidad_fisica − teorico_siesa`. Leer
    #: la existencia en otro instante mezcla dos momentos, y cualquier
    #: movimiento entre medio se cuela en el ajuste (hasta con el signo
    #: invertido).
    #:
    #: `salida_sin_conf_siesa` va aparte porque es la que decide si el delta se
    #: puede creer: el POS pendiente vive dentro de ella (medido: 600/600 filas
    #: con POS en Siesa QA, 2026-09-23). Si hay salidas sin confirmar que NO son
    #: POS, no se sabe si esa mercancía ya salió del estante y el ajuste no se
    #: aprueba — ver `ConteoService.motivo_bloqueo_ajuste`.
    cant_pos_siesa = db.Column(db.Integer, nullable=True)
    salida_sin_conf_siesa = db.Column(db.Integer, nullable=True)
    teorico_siesa = db.Column(db.Integer, nullable=True)
    foto_siesa_at = db.Column(db.DateTime, nullable=True)
    #: **La foto de Siesa tomada al ABRIR la tarea** (2026-09-23, m029). La de
    #: arriba es la del cierre: se lee al confirmar. El físico se cuenta en el
    #: intervalo entre las dos, así que si Siesa se movió en el medio —un
    #: cliente paga en caja una unidad que ya estaba contada, una unidad se
    #: vende antes de que el contador llegue al estante, el POS se acumula—
    #: el físico y la foto del cierre miden instantes distintos y el conteo
    #: fabrica un sobrante o un faltante que no existe.
    #:
    #: Con las dos fotos se sabe: si existencia, POS o salida sin confirmar
    #: cambiaron entre la apertura y el cierre, el conteo está CONTAMINADO y se
    #: recuenta (`ConteoService.movimiento_durante_conteo`). Sin la de inicio
    #: —Siesa no respondió al abrir, o la sesión es anterior a la columna— no
    #: se sabe, y el conteo sirve para decidir MATCH o segundo conteo pero no
    #: para ajustar (`ConteoService.motivo_bloqueo_ajuste`). Las cuatro juntas
    #: o ninguna, igual que la del cierre.
    existencia_inicio_siesa = db.Column(db.Integer, nullable=True)
    cant_pos_inicio_siesa = db.Column(db.Integer, nullable=True)
    salida_sin_conf_inicio_siesa = db.Column(db.Integer, nullable=True)
    foto_inicio_at = db.Column(db.DateTime, nullable=True)
    #: Los conteos que se descartaron por movimiento durante el conteo, en JSON
    #: (lista). El recuento se hace sobre la MISMA sesión —no se crea otra: la
    #: cadena CC1 → CC2 → CC3 es de un hijo por padre y la cola del Conteo
    #: Definitivo cuelga de ella—, así que lo que se contó y se descartó queda
    #: acá para auditoría: físico, las dos fotos, quién y cuándo.
    conteos_descartados = db.Column(db.Text, nullable=True)
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
    almacen = db.relationship('Almacen', backref='sesiones_conteo', lazy=True)
    producto = db.relationship('Producto', backref='sesiones_conteo', lazy=True)
    operario = db.relationship('Usuario', foreign_keys=[operario_id],
                               backref='conteos_asignados', lazy=True)
    segundo_operario = db.relationship('Usuario', foreign_keys=[segundo_operario_id],
                                       backref='segundos_conteos', lazy=True)
    aprobador = db.relationship('Usuario', foreign_keys=[aprobador_id],
                                backref='ajustes_aprobados', lazy=True)
    editor = db.relationship('Usuario', foreign_keys=[editado_por],
                             backref='conteos_editados', lazy=True)
    # Relación padre → hijo (segundo conteo generado por este).
    # uselist=False: cada sesión tiene máximo un hijo directo.
    # remote_side=[id] en el backref desambigua la dirección self-referencial.
    hijo_conteo = db.relationship(
        'SesionConteo',
        foreign_keys=[sesion_origen_id],
        backref=db.backref('sesion_padre', remote_side=[id], lazy='select'),
        uselist=False,
        lazy='select',
    )

    __table_args__ = (
        # El índice parcial que impide dos sesiones activas del mismo
        # (producto, ubicación, almacén) — creado por
        # `f4b84ad06843_add_sesion_conteo_unique_idx` para una carrera real
        # entre la API y el scheduler.
        #
        # Se declara acá con el MISMO predicado que la migración. Si aun así
        # `flask db check` lo sigue reportando, no es deriva: es que
        # autogenerate no sabe comparar predicados parciales. Eso se declara
        # en `tests/test_deriva_esquema.py` como excepción conocida, con su
        # motivo — no se silencia sin decir por qué.
        db.Index('ix_sesion_conteo_activa_unica',
                 'producto_id', 'ubicacion_id', 'almacen_id',
                 unique=True,
                 postgresql_where=db.text(
                     "estado IN ('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO') "
                     "AND es_segundo_conteo = false"),
                 #: **`sqlite_where` también, o el índice se vuelve TOTAL en
                 #: los tests.** Sin él, SQLite crea un único sobre
                 #: (producto, ubicación, almacén) sin predicado: dos sesiones
                 #: CERRADAS del mismo hueco —el historial normal— colisionan,
                 #: y 13 tests se caen sobre operación legítima.
                 #: Es la misma precaución que llevan los cuatro índices de
                 #: m012; acá se me pasó y la suite lo dijo enseguida.
                 sqlite_where=db.text(
                     "estado IN ('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO') "
                     "AND es_segundo_conteo = 0")),
    )

    def lista_conteos_descartados(self) -> list:
        """`conteos_descartados` leído. Un JSON ilegible no se oculta: se
        devuelve tal cual, marcado, para que la auditoría lo vea."""
        if not self.conteos_descartados:
            return []
        import json
        try:
            valor = json.loads(self.conteos_descartados)
        except (TypeError, ValueError):
            return [{'ilegible': self.conteos_descartados}]
        return valor if isinstance(valor, list) else [valor]

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
            'almacen_nombre': self.almacen.nombre if self.almacen else None,
            'bodega_siesa_id': self.almacen.bodega_siesa_id if self.almacen else None,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'maneja_lote': self.maneja_lote,
            'operario_id': self.operario_id,
            'estado': self.estado,
            'existencia_siesa': self.existencia_siesa,
            'fuente_existencia': self.fuente_existencia,
            'cant_pos_siesa': self.cant_pos_siesa,
            'salida_sin_conf_siesa': self.salida_sin_conf_siesa,
            'teorico_siesa': self.teorico_siesa,
            'foto_siesa_at': self.foto_siesa_at.isoformat() if self.foto_siesa_at else None,
            'existencia_inicio_siesa': self.existencia_inicio_siesa,
            'cant_pos_inicio_siesa': self.cant_pos_inicio_siesa,
            'salida_sin_conf_inicio_siesa': self.salida_sin_conf_inicio_siesa,
            'foto_inicio_at': self.foto_inicio_at.isoformat() if self.foto_inicio_at else None,
            'conteos_descartados': self.lista_conteos_descartados(),
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
            # Por qué este ajuste no se puede aprobar, dicho por la MISMA
            # función que lo niega — la pantalla no reimplementa la regla.
            # Solo en DESCUADRE: es el único estado desde el que
            # `confirmar_ajuste` encola, y la pantalla solo lo pinta ahí. En
            # los demás la respuesta no significa nada, y desde m029 cuesta
            # una consulta de traslados por fila del listado.
            'bloqueo_ajuste': (_motivo_bloqueo_ajuste(self)
                               if self.estado == EstadoConteo.DESCUADRE else None),
            # Datos del segundo conteo (hijo) embebidos para evitar N+1.
            # Si CC1≠CC2, hijo_conteo.hijo_conteo es el CC3.
            'segundo_conteo': {
                'id': self.hijo_conteo.id,
                'estado': self.hijo_conteo.estado,
                'cantidad_fisica': self.hijo_conteo.cantidad_fisica,
                # CC1 y CC2 coinciden por DIFERENCIA contra su propia foto, no
                # por cantidad física — la pantalla necesita las dos para
                # decir lo mismo que el servicio.
                'diferencia': self.hijo_conteo.diferencia,
                'teorico_siesa': self.hijo_conteo.teorico_siesa,
                'operario_id': self.hijo_conteo.operario_id,
                'operario_nombre': (
                    self.hijo_conteo.operario.nombre
                    if self.hijo_conteo.operario else None
                ),
                'fecha_cierre': (
                    self.hijo_conteo.fecha_cierre.isoformat()
                    if self.hijo_conteo.fecha_cierre else None
                ),
                'tercer_conteo': {
                    'id': self.hijo_conteo.hijo_conteo.id,
                    'estado': self.hijo_conteo.hijo_conteo.estado,
                    'cantidad_fisica': self.hijo_conteo.hijo_conteo.cantidad_fisica,
                    'operario_id': self.hijo_conteo.hijo_conteo.operario_id,
                    'operario_nombre': (
                        self.hijo_conteo.hijo_conteo.operario.nombre
                        if self.hijo_conteo.hijo_conteo.operario else None
                    ),
                } if self.hijo_conteo.hijo_conteo else None,
            } if self.hijo_conteo else None,
        }


def _motivo_bloqueo_ajuste(sesion):
    """Import diferido: la política vive en el servicio, no en el modelo."""
    from app.services.conteo_service import ConteoService
    return ConteoService.motivo_bloqueo_ajuste(sesion)
