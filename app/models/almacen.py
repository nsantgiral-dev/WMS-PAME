from datetime import datetime
from app.extensions import db

class Almacen(db.Model):
    __tablename__ = 'almacenes'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    direccion = db.Column(db.String(200))
    ciudad = db.Column(db.String(100))
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    ubicaciones = db.relationship('Ubicacion', backref='almacen', lazy=True)
    usuarios = db.relationship('Usuario', backref='almacen', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'direccion': self.direccion,
            'ciudad': self.ciudad,
            'activo': self.activo
        }