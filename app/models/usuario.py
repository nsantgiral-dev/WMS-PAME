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
    puede_usar_camara = db.Column(db.Boolean, default=True)
    # Capacidades operativas — independientes del rol base
    puede_picar = db.Column(db.Boolean, default=True)
    puede_empacar = db.Column(db.Boolean, default=False)
    puede_abastecer = db.Column(db.Boolean, default=False)  # abastecedor RESERVA→PICKING
    # Límite de conteos cíclicos intercalados por día (0 = sin límite)
    capacidad_diaria_conteo = db.Column(db.Integer, default=15, nullable=False)

    # Punto de venta (solo para rol='tienda')
    bodega_siesa_id = db.Column(db.String(20), nullable=True)      # ej. 'NC1'
    siesa_co_id = db.Column(db.String(20), nullable=True)          # ej. '003' — C.O. de la tienda
    nombre_punto_venta = db.Column(db.String(100), nullable=True)  # ej. 'Neiva Centro'
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        _cond = self.conductor_perfil[0] if getattr(self, 'conductor_perfil', None) else None
        return {
            'id': self.id,
            'nombre': self.nombre,
            'email': self.email,
            'rol': self.rol,
            'activo': self.activo,
            'almacen_id': self.almacen_id,
            'puede_usar_camara': self.puede_usar_camara if self.puede_usar_camara is not None else True,
            'puede_picar': self.puede_picar if self.puede_picar is not None else True,
            'puede_empacar': self.puede_empacar or False,
            'puede_abastecer': self.puede_abastecer or False,
            'capacidad_diaria_conteo': self.capacidad_diaria_conteo if self.capacidad_diaria_conteo is not None else 15,
            'bodega_siesa_id': self.bodega_siesa_id,
            'siesa_co_id': self.siesa_co_id,
            'nombre_punto_venta': self.nombre_punto_venta,
            'conductor_id': _cond.id if _cond else None,
            'conductor_cedula': _cond.cedula if _cond else None,
            'conductor_telefono': _cond.telefono if _cond else None,
        }