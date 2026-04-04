from datetime import datetime
from app.extensions import db


class RutaDespacho(db.Model):
    """
    Manifiesto de cargue: agrupa los bultos que salen en un mismo vehículo.
    Ciclo: EN_CARGUE → EN_TRANSITO → ENTREGADA
    El conductor y el vehículo se asignan dinámicamente por viaje.
    """
    __tablename__ = 'rutas_despacho'

    id              = db.Column(db.Integer, primary_key=True)
    conductor_id    = db.Column(db.Integer, db.ForeignKey('conductores.id'), nullable=False)
    vehiculo_id     = db.Column(db.Integer, db.ForeignKey('vehiculos.id'), nullable=True)
    tipo_ruta       = db.Column(db.String(20), nullable=False)   # Urbana | Municipal
    estado          = db.Column(db.String(20), default='EN_CARGUE')  # EN_CARGUE | EN_TRANSITO | ENTREGADA
    notas           = db.Column(db.Text)
    fecha_creacion  = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_cierre    = db.Column(db.DateTime)    # cuando pasa a EN_TRANSITO
    fecha_entregada = db.Column(db.DateTime)    # cuando pasa a ENTREGADA

    bultos = db.relationship('Bulto', backref='ruta', lazy=True)

    def total_bultos(self):
        return len(self.bultos)

    def total_planificados(self):
        """Bultos asignados pero aún no confirmados físicamente."""
        return sum(1 for b in self.bultos if b.estado == 'PENDIENTE')

    def total_confirmados(self):
        """Bultos confirmados con scan físico."""
        return sum(1 for b in self.bultos if b.estado == 'CARGADO')

    def pedidos(self):
        """Lista de números de pedido únicos en esta ruta."""
        return list({b.tarea.numero_pedido_siesa for b in self.bultos if b.tarea})

    def to_dict(self, include_bultos=False):
        c = self.conductor
        v = self.vehiculo
        d = {
            'id':                 self.id,
            'conductor_id':       self.conductor_id,
            'conductor_nombre':   c.nombre if c else '',
            'conductor_cedula':   c.cedula if c else '',
            'vehiculo_id':        self.vehiculo_id,
            'vehiculo_placa':     v.placa  if v else '',
            'vehiculo_tipo':      v.tipo   if v else '',
            'vehiculo_capacidad': float(v.capacidad_kg) if v and v.capacidad_kg else None,
            'tipo_ruta':          self.tipo_ruta,
            'estado':             self.estado,
            'notas':              self.notas or '',
            'total_bultos':       self.total_bultos(),
            'total_planificados': self.total_planificados(),
            'total_confirmados':  self.total_confirmados(),
            'pedidos':            self.pedidos(),
            'fecha_creacion':     self.fecha_creacion.isoformat(),
            'fecha_cierre':       self.fecha_cierre.isoformat() if self.fecha_cierre else None,
            'fecha_entregada':    self.fecha_entregada.isoformat() if self.fecha_entregada else None,
        }
        if include_bultos:
            grupos = {}
            for b in self.bultos:
                destino = (b.tarea.municipio or b.tarea.cliente or 'Sin destino') if b.tarea else 'Sin destino'
                grupos.setdefault(destino, []).append({
                    'id':            b.id,
                    'codigo_barras': b.codigo_barras,
                    'tipo':          b.tipo,
                    'numero':        b.numero,
                    'total':         b.total,
                    'estado':        b.estado,  # PENDIENTE (planificado) | CARGADO (confirmado)
                    'numero_pedido': b.tarea.numero_pedido_siesa if b.tarea else '',
                    'cliente':       b.tarea.cliente or '' if b.tarea else '',
                })
            d['manifiesto'] = [
                {'destino': dest, 'bultos': blts}
                for dest, blts in sorted(grupos.items())
            ]
        return d
