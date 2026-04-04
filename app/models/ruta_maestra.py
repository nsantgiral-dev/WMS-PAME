from datetime import datetime
from app.extensions import db


class RutaMaestra(db.Model):
    """
    Catálogo fijo de rutas geográficas.
    Una ruta maestra se despacha N veces — cada despacho es un RutaDespacho.
    Las paradas definen los municipios y su secuencia de cargue (LIFO: orden 1 = primera entrega = última en cargar).
    """
    __tablename__ = 'rutas_maestras'

    id             = db.Column(db.Integer, primary_key=True)
    nombre         = db.Column(db.String(100), unique=True, nullable=False)
    tipo_ruta      = db.Column(db.String(20), nullable=False)  # Urbana | Municipal
    activa         = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    paradas   = db.relationship(
        'RutaMaestraParada', backref='ruta_maestra', lazy=True,
        order_by='RutaMaestraParada.orden', cascade='all, delete-orphan'
    )
    despachos = db.relationship('RutaDespacho', backref='ruta_maestra', lazy=True)

    def municipios(self):
        return [p.municipio for p in self.paradas]

    def to_dict(self, include_paradas=True):
        d = {
            'id':             self.id,
            'nombre':         self.nombre,
            'tipo_ruta':      self.tipo_ruta,
            'activa':         self.activa,
            'fecha_creacion': self.fecha_creacion.isoformat(),
        }
        if include_paradas:
            d['paradas'] = [{'id': p.id, 'municipio': p.municipio, 'orden': p.orden}
                            for p in self.paradas]
        return d


class RutaMaestraParada(db.Model):
    """
    Parada de una ruta maestra.
    orden 1 = primera entrega (última en cargar — LIFO).
    """
    __tablename__ = 'rutas_maestras_paradas'

    id              = db.Column(db.Integer, primary_key=True)
    ruta_maestra_id = db.Column(db.Integer, db.ForeignKey('rutas_maestras.id'), nullable=False)
    municipio       = db.Column(db.String(100), nullable=False)
    orden           = db.Column(db.Integer, nullable=False)
