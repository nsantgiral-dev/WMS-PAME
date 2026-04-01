from datetime import datetime
from app.extensions import db


class PedidoSiesa(db.Model):
    """
    Read Model local de pedidos Siesa — se sincroniza en background cada 5 min.
    El WMS lee de aquí, nunca de Connekta en tiempo real.
    """
    __tablename__ = 'pedidos_siesa'

    id = db.Column(db.Integer, primary_key=True)

    # Clave natural Siesa: tipo_docto + consec_docto + co + bodega
    tipo_docto   = db.Column(db.String(10), nullable=False)
    consec_docto = db.Column(db.Integer, nullable=False)
    centro_op    = db.Column(db.String(10), nullable=False)
    bodega       = db.Column(db.String(10), nullable=False)

    # Datos del pedido
    numero_pedido    = db.Column(db.String(20), nullable=False)
    item_codigo      = db.Column(db.String(50))
    item_descripcion = db.Column(db.String(200))
    item_id_siesa    = db.Column(db.String(50))
    cliente          = db.Column(db.String(200))
    municipio        = db.Column(db.String(100))   # nombre resuelto desde código DANE
    fecha_entrega    = db.Column(db.String(20))
    estado_siesa     = db.Column(db.Integer)

    cantidad_pedida      = db.Column(db.Float, default=0)
    cantidad_remisionada = db.Column(db.Float, default=0)
    cantidad_pendiente   = db.Column(db.Float, default=0)

    # Enriquecimiento WMS
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=True)

    # Control de sync
    sync_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('tipo_docto', 'consec_docto', 'centro_op', 'bodega', 'item_codigo',
                            name='uq_pedido_siesa_item'),
    )

    def to_dict(self):
        from app.models.producto import Producto
        prod = Producto.query.get(self.producto_id) if self.producto_id else None
        return {
            'numero_pedido':      self.numero_pedido,
            'tipo_docto':         self.tipo_docto,
            'consec_docto':       self.consec_docto,
            'centro_op':          self.centro_op,
            'bodega':             self.bodega,
            'item_codigo':        self.item_codigo,
            'item_descripcion':   self.item_descripcion,
            'item_id_siesa':      self.item_id_siesa,
            'cliente':            self.cliente,
            'municipio':          self.municipio or '',
            'fecha_entrega':      self.fecha_entrega,
            'estado':             self.estado_siesa,
            'cantidad_pedida':    self.cantidad_pedida,
            'cantidad_remisionada': self.cantidad_remisionada,
            'cantidad_pendiente': self.cantidad_pendiente,
            'producto_id':        self.producto_id,
            'producto_nombre_wms': prod.nombre if prod else None,
        }
