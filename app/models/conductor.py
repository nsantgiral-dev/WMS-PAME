from datetime import datetime
from app.extensions import db


class Conductor(db.Model):
    __tablename__ = 'conductores'

    id             = db.Column(db.Integer, primary_key=True)
    nombre         = db.Column(db.String(100), nullable=False)
    cedula         = db.Column(db.String(20), unique=True, nullable=False)
    telefono       = db.Column(db.String(20))
    activo         = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    rutas = db.relationship('RutaDespacho', backref='conductor', lazy=True)

    def to_dict(self):
        return {
            'id':             self.id,
            'nombre':         self.nombre,
            'cedula':         self.cedula,
            'telefono':       self.telefono or '',
            'activo':         self.activo,
            'fecha_creacion': self.fecha_creacion.isoformat(),
        }
