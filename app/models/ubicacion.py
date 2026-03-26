from datetime import datetime
from app.extensions import db

class Ubicacion(db.Model):
    __tablename__ = 'ubicaciones'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    zona = db.Column(db.String(50))
    pasillo = db.Column(db.String(10))
    estante = db.Column(db.String(10))
    nivel = db.Column(db.String(10))
    tipo = db.Column(db.String(30), default='estanteria')
    capacidad_maxima = db.Column(db.Integer)
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    productos = db.relationship('UbicacionProducto', backref='ubicacion', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'almacen_id': self.almacen_id,
            'zona': self.zona,
            'pasillo': self.pasillo,
            'estante': self.estante,
            'nivel': self.nivel,
            'tipo': self.tipo,
            'capacidad_maxima': self.capacidad_maxima,
            'activo': self.activo
        }