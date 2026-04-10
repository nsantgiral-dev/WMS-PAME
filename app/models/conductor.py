from datetime import datetime
from app.extensions import db


class Conductor(db.Model):
    __tablename__ = 'conductores'

    id             = db.Column(db.Integer, primary_key=True)
    nombre         = db.Column(db.String(100), nullable=False)
    cedula         = db.Column(db.String(20), unique=True, nullable=False)
    telefono       = db.Column(db.String(20))
    activo         = db.Column(db.Boolean, default=True)
    disponible     = db.Column(db.Boolean, default=True)   # False cuando está en ruta activa
    # Cuenta de login vinculada — permite al conductor acceder a la PWA
    usuario_id     = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    rutas   = db.relationship('RutaDespacho', backref='conductor', lazy=True)
    usuario = db.relationship('Usuario', backref='conductor_perfil', lazy=True)

    def to_dict(self):
        return {
            'id':             self.id,
            'nombre':         self.nombre,
            'cedula':         self.cedula,
            'telefono':       self.telefono or '',
            'activo':         self.activo,
            'disponible':     self.disponible,
            'usuario_id':     self.usuario_id,
            'usuario_email':  self.usuario.email if self.usuario else None,
            'fecha_creacion': self.fecha_creacion.isoformat(),
        }
