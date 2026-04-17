from datetime import datetime
from app.extensions import db


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
    cantidad_recibida = db.Column(db.Integer, default=0)

    # Decisión de ubicación
    # INVENTARIO, CROSS_DOCK, BLOQUEADO
    destino = db.Column(db.String(20), default='INVENTARIO')
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)
    ubicacion_cross_dock_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)

    # Estado del ítem
    ingresado_inventario = db.Column(db.Boolean, default=False)

    # Lote y vencimiento
    lote = db.Column(db.String(50))
    fecha_vencimiento = db.Column(db.Date)

    # Relaciones
    producto = db.relationship('Producto', backref='items_recepcion', lazy=True)
    ubicacion = db.relationship('Ubicacion', foreign_keys=[ubicacion_id], lazy=True)
    ubicacion_cross_dock = db.relationship('Ubicacion',
                                           foreign_keys=[ubicacion_cross_dock_id], lazy=True)

    def cantidad_maxima_permitida(self):
        return int(self.cantidad_ordenada * (1 + self.tolerancia_exceso_pct / 100))

    def es_exceso(self):
        return self.cantidad_recibida > self.cantidad_maxima_permitida()

    def es_faltante(self):
        return self.cantidad_recibida < self.cantidad_ordenada

    def diferencia(self):
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
            'fecha_vencimiento': self.fecha_vencimiento.isoformat() if self.fecha_vencimiento else None
        }