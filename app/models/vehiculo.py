from datetime import datetime
from app.extensions import db


class Vehiculo(db.Model):
    """
    Catálogo de flota propia. La placa se asigna al viaje (RutaDespacho),
    no al conductor — los conductores no tienen vehículo fijo.
    """
    __tablename__ = 'vehiculos'

    id             = db.Column(db.Integer, primary_key=True)
    placa          = db.Column(db.String(10), unique=True, nullable=False)
    tipo           = db.Column(db.String(30), nullable=False)   # NHR | Turbo | Moto | Van | Camión
    capacidad_kg   = db.Column(db.Numeric(10, 2), nullable=True)
    activo         = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    rutas = db.relationship('RutaDespacho', backref='vehiculo', lazy=True)

    def to_dict(self):
        return {
            'id':             self.id,
            'placa':          self.placa,
            'tipo':           self.tipo,
            'capacidad_kg':   float(self.capacidad_kg) if self.capacidad_kg is not None else None,
            'activo':         self.activo,
            'fecha_creacion': self.fecha_creacion.isoformat(),
        }
