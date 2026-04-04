from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db


class Usuario(db.Model):
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    rol = db.Column(db.String(50), default='operario')
    activo = db.Column(db.Boolean, default=True)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=True)
    puede_usar_camara = db.Column(db.Boolean, default=False)
    # Capacidades operativas — independientes del rol base
    puede_picar = db.Column(db.Boolean, default=True)
    puede_empacar = db.Column(db.Boolean, default=False)
    # Punto de venta (solo para rol='tienda')
    bodega_siesa_id = db.Column(db.String(20), nullable=True)      # ej. 'TP1'
    nombre_punto_venta = db.Column(db.String(100), nullable=True)  # ej. 'Tienda Centro'
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'nombre': self.nombre,
            'email': self.email,
            'rol': self.rol,
            'activo': self.activo,
            'almacen_id': self.almacen_id,
            'puede_usar_camara': self.puede_usar_camara or False,
            'puede_picar': self.puede_picar if self.puede_picar is not None else True,
            'puede_empacar': self.puede_empacar or False,
            'bodega_siesa_id': self.bodega_siesa_id,
            'nombre_punto_venta': self.nombre_punto_venta,
        }