from datetime import datetime
from app.extensions import db


class EstadoBulto:
    PENDIENTE  = 'PENDIENTE'
    CARGADO    = 'CARGADO'
    ENTREGADO  = 'ENTREGADO'
    RECHAZADO  = 'RECHAZADO'


class Bulto(db.Model):
    """
    Pieza física de un pedido empacado.
    Un TareaPacking genera N Bultos al cerrar (Caja, Bolsa, Rollo, Plancha, Estiba).
    El Muelle trabaja bulto por bulto — scan-to-truck obligatorio.
    """
    __tablename__ = 'bultos'

    id              = db.Column(db.Integer, primary_key=True)
    tarea_id        = db.Column(db.Integer, db.ForeignKey('tareas_packing.id'), nullable=False)
    codigo_barras   = db.Column(db.String(30), unique=True, nullable=False)  # PD1125-01
    tipo            = db.Column(db.String(20), nullable=False)               # Caja, Bolsa, Rollo, Plancha, Estiba
    numero          = db.Column(db.Integer, nullable=False)                  # 1, 2, 3 …
    total           = db.Column(db.Integer, nullable=False)                  # total piezas del pedido
    estado             = db.Column(db.String(20), default='PENDIENTE')   # PENDIENTE → CARGADO → ENTREGADO | RECHAZADO
    ruta_despacho_id   = db.Column(db.Integer, db.ForeignKey('rutas_despacho.id'), nullable=True)
    fecha_cargado      = db.Column(db.DateTime)
    fecha_entrega      = db.Column(db.DateTime)
    motivo_rechazo     = db.Column(db.String(100))  # Cliente rechazó / Dirección incorrecta / Averiado / No había nadie
    fecha_creacion     = db.Column(db.DateTime, default=datetime.utcnow)

    tarea = db.relationship('TareaPacking', backref='bultos', lazy=True)

    def to_dict(self):
        t = self.tarea
        return {
            'id':                self.id,
            'tarea_id':          self.tarea_id,
            'codigo_barras':     self.codigo_barras,
            'tipo':              self.tipo,
            'numero':            self.numero,
            'total':             self.total,
            'estado':            self.estado,
            'ruta_despacho_id':  self.ruta_despacho_id,
            'fecha_cargado':     self.fecha_cargado.isoformat() if self.fecha_cargado else None,
            'fecha_entrega':     self.fecha_entrega.isoformat() if self.fecha_entrega else None,
            'motivo_rechazo':    self.motivo_rechazo,
            'numero_pedido':     t.numero_pedido_siesa if t else '',
            'cliente':           t.cliente or '' if t else '',
            'municipio':         t.municipio or '' if t else '',
        }
