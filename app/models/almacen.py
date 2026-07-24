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
    # Siesa/Connekta — construye payloads dinámicamente por CD
    bodega_siesa_id = db.Column(db.String(20))   # ej. 'NB1', 'NB2'
    centro_op_siesa = db.Column(db.String(20))   # ej. '003', '004'

    # Bodega/CO donde la fusión Layout↔Picking está activa (ver layout_service.py
    # asignar_producto): al asignar un SKU a un hueco real ahí, se traspasa stock
    # desde SIESA-GENERAL. Las demás bodegas aún no tienen su layout físico
    # organizado — hardcodeado a propósito (YAGNI), único lugar a tocar si se
    # habilita otra bodega más adelante.
    _BODEGA_FUSION_LAYOUT = 'NB1'
    _CO_FUSION_LAYOUT = '003'

    @property
    def tiene_fusion_layout_activa(self) -> bool:
        return (self.bodega_siesa_id == self._BODEGA_FUSION_LAYOUT
                and self.centro_op_siesa == self._CO_FUSION_LAYOUT)

    # Relaciones
    ubicaciones = db.relationship('Ubicacion', backref='almacen', lazy=True)
    usuarios = db.relationship('Usuario', backref='almacen', lazy=True)
    clasificaciones_abc = db.relationship('ProductoClasificacionABC',
                                          backref='almacen', lazy=True,
                                          cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'direccion': self.direccion,
            'ciudad': self.ciudad,
            'activo': self.activo,
            'bodega_siesa_id': self.bodega_siesa_id,
            'centro_op_siesa': self.centro_op_siesa,
        }
