from datetime import datetime
from app.extensions import db


class EstadoRecepcion:
    ABIERTA    = 'ABIERTA'
    EN_PROCESO = 'EN_PROCESO'
    CONFIRMADA = 'CONFIRMADA'
    CANCELADA  = 'CANCELADA'


class RecepcionMercancia(db.Model):
    __tablename__ = 'recepciones'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)

    # Orden de compra de Siesa
    numero_oc_siesa = db.Column(db.String(50), nullable=False)
    # Componentes separados requeridos por el gateway (f420_id_co_docto / f420_id_tipo_docto / f420_consec_docto)
    co_oc_siesa = db.Column(db.String(20))
    tipo_docto_oc_siesa = db.Column(db.String(20))
    consec_docto_oc_siesa = db.Column(db.String(30))
    proveedor_codigo = db.Column(db.String(50))
    proveedor_nombre = db.Column(db.String(200))
    cond_pago_siesa = db.Column(db.String(20))
    sucursal_prov_siesa = db.Column(db.String(10))
    num_remision_prov = db.Column(db.String(12))

    # Almacén y recepcionista
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    recepcionista_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Estado
    # ABIERTA → EN_PROCESO → CONFIRMADA → CANCELADA
    estado = db.Column(db.String(30), default='ABIERTA', nullable=False)

    # Tipo de recepción
    es_parcial = db.Column(db.Boolean, default=False)
    tiene_excesos = db.Column(db.Boolean, default=False)
    tiene_cross_dock = db.Column(db.Boolean, default=False)

    # Trigger a Siesa
    siesa_triggered = db.Column(db.Boolean, default=False)
    siesa_response = db.Column(db.Text)
    siesa_triggered_at = db.Column(db.DateTime)

    # Observaciones
    observaciones = db.Column(db.Text)

    # Tiempos
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_inicio = db.Column(db.DateTime)
    fecha_confirmacion = db.Column(db.DateTime)

    # Relaciones
    almacen = db.relationship('Almacen', backref='recepciones', lazy=True)
    recepcionista = db.relationship('Usuario', backref='recepciones', lazy=True)
    items = db.relationship('ItemRecepcion', backref='recepcion',
                            lazy=True, cascade='all, delete-orphan')

    def total_items(self):
        return len(self.items)

    def items_escaneados(self):
        return sum(1 for i in self.items if i.cantidad_recibida > 0)

    __table_args__ = (
        # Una recepción activa por OC. El endpoint dice ser idempotente
        # (`routes/siesa.py:1141`) y la idempotencia vive en un `.first()`
        # sin lock: dos recepcionistas abriendo la misma OC desde el listado
        # crean dos, cada una escanea parte del físico, y salen dos entradas
        # 142948 con doble suma de inventario.
        db.Index('uq_recepcion_oc_activa', 'numero_oc_siesa', 'co_oc_siesa',
                 unique=True,
                 postgresql_where=db.text("estado <> 'CANCELADA'"),
                 sqlite_where=db.text("estado <> 'CANCELADA'")),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'numero_oc_siesa': self.numero_oc_siesa,
            'co_oc_siesa': self.co_oc_siesa,
            'tipo_docto_oc_siesa': self.tipo_docto_oc_siesa,
            'consec_docto_oc_siesa': self.consec_docto_oc_siesa,
            'proveedor_codigo': self.proveedor_codigo,
            'proveedor_nombre': self.proveedor_nombre,
            'cond_pago_siesa': self.cond_pago_siesa,
            'sucursal_prov_siesa': self.sucursal_prov_siesa,
            'num_remision_prov': self.num_remision_prov,
            'almacen_id': self.almacen_id,
            'recepcionista_id': self.recepcionista_id,
            'estado': self.estado,
            'es_parcial': self.es_parcial,
            'tiene_excesos': self.tiene_excesos,
            'tiene_cross_dock': self.tiene_cross_dock,
            'siesa_triggered': self.siesa_triggered,
            'siesa_triggered_at': self.siesa_triggered_at.isoformat() if self.siesa_triggered_at else None,
            'observaciones': self.observaciones,
            'total_items': self.total_items(),
            'items_escaneados': self.items_escaneados(),
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_inicio': self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            'fecha_confirmacion': self.fecha_confirmacion.isoformat() if self.fecha_confirmacion else None,
            'items': [i.to_dict() for i in self.items]
        }


class ItemRecepcion(db.Model):
    __tablename__ = 'items_recepcion'

    id = db.Column(db.Integer, primary_key=True)
    recepcion_id = db.Column(db.Integer, db.ForeignKey('recepciones.id'), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)

    # Lo que dice la OC de Siesa
    cantidad_ordenada = db.Column(db.Integer, nullable=False)
    tolerancia_exceso_pct = db.Column(db.Float, default=0.0)

    # Lo que escaneó el recepcionista (recepción ciega)
    cantidad_recibida = db.Column(db.Integer, default=0)   # siempre en unidades sueltas
    empaques_escaneados = db.Column(db.Integer, default=0) # cajas/pacas contadas

    # Decisión de ubicación: INVENTARIO | CROSS_DOCK
    #
    # El comentario original declaraba además un tercer valor, 'BLOQUEADO', que
    # NINGÚN camino de código escribió nunca — un casillero hecho y vacío que
    # prometía «esta mercancía está retenida». Se retira del comentario en vez
    # de dejarlo: un valor que solo existe en la documentación hace que el
    # siguiente lector construya sobre algo que no está.
    #
    # La avería, que es lo que ese valor parecía querer representar, NO se
    # modela como destino del ítem: un ítem puede llegar parcialmente roto, así
    # que vive en `cantidad_averiada` y el reparto lo hace
    # `confirmar_recepcion`.
    destino = db.Column(db.String(20), default='INVENTARIO')
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)
    ubicacion_cross_dock_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)

    # Estado del ítem
    ingresado_inventario = db.Column(db.Boolean, default=False)

    #: De lo recibido, cuánto llegó AVERIADO. Es un subconjunto de
    #: `cantidad_recibida`, no una resta: la mercancía entró físicamente y entró
    #: a la OC en Siesa (lo que viaja al 142948 es `cantidad_recibida`).
    #:
    #: Se cuenta porque el proceso del negocio lo presupone: «si dan nota
    #: crédito se da de baja, o si no se devuelve» — una NC reversa una entrada,
    #: y devolver exige tenerlo. Los dos desenlaces dan por hecho que entró.
    #:
    #: Quien prefiera NO ingresarla sigue teniendo el camino de siempre:
    #: contar de menos. Esta columna agrega una capacidad, no quita la otra.
    cantidad_averiada = db.Column(db.Integer, default=0, nullable=False,
                                  server_default='0')
    #: Por qué llegó averiado. Sin esto la avería es un número sin historia y la
    #: auxiliar de compras no tiene con qué reclamarle al proveedor.
    motivo_averia = db.Column(db.String(200))

    # Tipo de ítem: OC = viene en la orden | BONIFICACION = adicional del proveedor ($0)
    tipo = db.Column(db.String(20), default='OC', nullable=False)
    motivo_siesa = db.Column(db.String(10))  # código motivo Siesa, ej. '04'

    # Lote y vencimiento
    lote = db.Column(db.String(50))
    fecha_vencimiento = db.Column(db.Date)

    # Idempotencia de escaneo: guarda el scan_id (UUID generado por el cliente)
    # del último escaneo aplicado a este ítem. Un reintento con el MISMO
    # scan_id (wifi de andén que cortó justo al volver la respuesta) no vuelve
    # a sumar — ver RecepcionService.escanear_producto.
    ultimo_scan_id = db.Column(db.String(64), nullable=True)

    # Relaciones
    producto = db.relationship('Producto', backref='items_recepcion', lazy=True)
    ubicacion = db.relationship('Ubicacion', foreign_keys=[ubicacion_id], lazy=True)
    ubicacion_cross_dock = db.relationship('Ubicacion',
                                           foreign_keys=[ubicacion_cross_dock_id], lazy=True)

    __table_args__ = (
        # La avería no puede exceder lo recibido: si lo hiciera, el reparto de
        # `confirmar_recepcion` dejaría cantidad negativa en el destino bueno y
        # el CHECK `ck_cantidad_no_negativa` de UbicacionProducto reventaría
        # DESPUÉS, con la recepción a medio confirmar.
        db.CheckConstraint('cantidad_averiada >= 0',
                           name='ck_item_recepcion_averiada_no_negativa'),
        db.CheckConstraint('cantidad_averiada <= cantidad_recibida',
                           name='ck_item_recepcion_averiada_subconjunto'),
        # `ConteoService.procesos_en_curso` (recepción sin EntradaOC). m031.
        db.Index('ix_items_recepcion_producto', 'producto_id'),
    )

    def cantidad_buena(self):
        """Lo recibido que NO está averiado. Por construcción, lo bueno más lo
        averiado es siempre lo recibido: el reparto no puede perder unidades."""
        return self.cantidad_recibida - (self.cantidad_averiada or 0)

    def cantidad_maxima_permitida(self):
        if self.tipo == 'BONIFICACION':
            return 999999
        return int(self.cantidad_ordenada * (1 + self.tolerancia_exceso_pct / 100))

    def es_exceso(self):
        if self.tipo == 'BONIFICACION':
            return False
        return self.cantidad_recibida > self.cantidad_maxima_permitida()

    def es_faltante(self):
        if self.tipo == 'BONIFICACION':
            return False
        return self.cantidad_recibida < self.cantidad_ordenada

    def diferencia(self):
        if self.tipo == 'BONIFICACION':
            return 0
        return self.cantidad_recibida - self.cantidad_ordenada

    def to_dict(self):
        return {
            'id': self.id,
            'recepcion_id': self.recepcion_id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'clasificacion_abc': self.producto.clasificacion_abc if self.producto else None,
            'cantidad_ordenada': self.cantidad_ordenada,
            'cantidad_recibida': self.cantidad_recibida,
            'cantidad_averiada': self.cantidad_averiada or 0,
            'cantidad_buena': self.cantidad_buena(),
            'motivo_averia': self.motivo_averia,
            'empaques_escaneados': self.empaques_escaneados or 0,
            'cantidad_maxima_permitida': self.cantidad_maxima_permitida(),
            'tolerancia_exceso_pct': self.tolerancia_exceso_pct,
            'es_exceso': self.es_exceso(),
            'es_faltante': self.es_faltante(),
            'diferencia': self.diferencia(),
            'destino': self.destino,
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_cross_dock_id': self.ubicacion_cross_dock_id,
            'ingresado_inventario': self.ingresado_inventario,
            'lote': self.lote,
            'fecha_vencimiento': self.fecha_vencimiento.isoformat() if self.fecha_vencimiento else None,
            'tipo': self.tipo,
            'motivo_siesa': self.motivo_siesa,
            'factor_conversion': (self.producto.factor_conversion or 1) if self.producto else 1,
            'unidad_empaque': (self.producto.unidad_empaque or '') if self.producto else '',
        }