from datetime import datetime
from app.extensions import db


class TareaPacking(db.Model):
    __tablename__ = 'tareas_packing'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)

    # Referencia al pedido de Siesa
    numero_pedido_siesa = db.Column(db.String(50), nullable=False)
    # Componentes separados requeridos por el gateway (F430_ID_TIPO_DOCTO / F430_CONSEC_DOCTO)
    tipo_docto_pedido_siesa = db.Column(db.String(20))
    consec_docto_pedido_siesa = db.Column(db.String(30))
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)

    # Empacador asignado
    empacador_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Estado
    estado = db.Column(db.String(30), default='PENDIENTE', nullable=False)
    # PENDIENTE → EN_PROCESO → VERIFICADO → CARGADO → CANCELADO

    # Resultado verificación
    verificacion_exitosa = db.Column(db.Boolean, default=False)
    observaciones = db.Column(db.Text)

    # Trigger a Siesa
    siesa_triggered = db.Column(db.Boolean, default=False)
    siesa_response = db.Column(db.Text)
    siesa_triggered_at = db.Column(db.DateTime)

    # Destino — copiado de PedidoSiesa al crear la tarea
    cliente    = db.Column(db.String(200))
    municipio  = db.Column(db.String(100))

    # Tiempos
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_inicio = db.Column(db.DateTime)
    fecha_verificado = db.Column(db.DateTime)
    fecha_despachado = db.Column(db.DateTime)
    fecha_cargado = db.Column(db.DateTime)

    # Relaciones
    empacador = db.relationship('Usuario', backref='tareas_packing', lazy=True)
    almacen = db.relationship('Almacen', backref='tareas_packing', lazy=True)
    items = db.relationship('ItemPacking', backref='tarea', lazy=True,
                            cascade='all, delete-orphan')

    def total_items(self):
        return len(self.items)

    def items_verificados(self):
        return sum(1 for i in self.items if i.verificado)

    def tiene_diferencias(self):
        return any(i.tiene_diferencia() for i in self.items)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'numero_pedido_siesa': self.numero_pedido_siesa,
            'tipo_docto_pedido_siesa': self.tipo_docto_pedido_siesa,
            'consec_docto_pedido_siesa': self.consec_docto_pedido_siesa,
            'almacen_id': self.almacen_id,
            'empacador_id': self.empacador_id,
            'estado': self.estado,
            'verificacion_exitosa': self.verificacion_exitosa,
            'observaciones': self.observaciones,
            'siesa_triggered': self.siesa_triggered,
            'siesa_triggered_at': self.siesa_triggered_at.isoformat() if self.siesa_triggered_at else None,
            'cliente': self.cliente or '',
            'municipio': self.municipio or '',
            'total_items': self.total_items(),
            'items_verificados': self.items_verificados(),
            'tiene_diferencias': self.tiene_diferencias(),
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_inicio': self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            'fecha_verificado': self.fecha_verificado.isoformat() if self.fecha_verificado else None,
            'fecha_despachado': self.fecha_despachado.isoformat() if self.fecha_despachado else None,
            'fecha_cargado': self.fecha_cargado.isoformat() if self.fecha_cargado else None,
            'items': [i.to_dict() for i in self.items],
            'bultos': [{'id': b.id, 'codigo_barras': b.codigo_barras, 'tipo': b.tipo, 'numero': b.numero, 'total': b.total} for b in self.bultos],
            'siesa_response': self.siesa_response or ''
        }


class ItemPacking(db.Model):
    __tablename__ = 'items_packing'

    id = db.Column(db.Integer, primary_key=True)
    tarea_id = db.Column(db.Integer, db.ForeignKey('tareas_packing.id'), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)

    # Lo que debía venir del picking
    cantidad_esperada = db.Column(db.Integer, nullable=False)

    # Lo que el empacador realmente encontró
    cantidad_real = db.Column(db.Integer, default=0)

    # Estado verificación
    verificado = db.Column(db.Boolean, default=False)
    lote = db.Column(db.String(50))
    observacion = db.Column(db.String(200))

    # Relaciones
    producto = db.relationship('Producto', backref='items_packing', lazy=True)

    def tiene_diferencia(self):
        return self.verificado and self.cantidad_real != self.cantidad_esperada

    def diferencia(self):
        return self.cantidad_real - self.cantidad_esperada

    def to_dict(self):
        return {
            'id': self.id,
            'tarea_id': self.tarea_id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'cantidad_esperada': self.cantidad_esperada,
            'cantidad_real': self.cantidad_real,
            'verificado': self.verificado,
            'tiene_diferencia': self.tiene_diferencia(),
            'diferencia': self.diferencia(),
            'lote': self.lote,
            'observacion': self.observacion
        }