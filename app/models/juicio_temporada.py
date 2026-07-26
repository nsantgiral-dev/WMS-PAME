"""
JuicioTemporada — la lista paralela de la fundadora, registrada.

NO es una estimación del sistema: es un juicio humano, con autor y fecha. Por
eso vive en el servidor y no en localStorage — un caché borrado, otro
navegador o la laptop equivocada en la sala del comité lo evaporarían.

Su valor real llega después: en enero de 2027, comparando lo que ella pidió,
lo que el modelo pidió y lo que de verdad se vendió, este es el dataset que
valida o refuta el modelo ante la única persona cuya aprobación importa.

Se guarda el Q* del modelo del momento junto al juicio: sin eso, comparar
después sería contra un modelo que ya cambió.
"""
from datetime import datetime
from app.extensions import db


class JuicioTemporada(db.Model):
    __tablename__ = 'juicios_temporada'

    id = db.Column(db.Integer, primary_key=True)

    # Temporada que se está decidiendo (ej. '2026-27'), no la de la historia
    temporada = db.Column(db.String(10), nullable=False)
    referencia = db.Column(db.String(30), nullable=False)

    # El juicio humano
    cantidad_juicio = db.Column(db.Integer, nullable=False)
    autor_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    autor_nombre = db.Column(db.String(120))  # quién lo dictó, si no es el usuario logueado
    nota = db.Column(db.Text)

    # Foto del modelo en el momento del juicio — para poder comparar después
    q_modelo = db.Column(db.Integer)
    costo_unitario = db.Column(db.Numeric(14, 2))
    distribucion = db.Column(db.String(60))

    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow,
                                    onupdate=datetime.utcnow)

    __table_args__ = (
        db.Index('ix_juicio_temporada_ref', 'temporada', 'referencia', unique=True),
    )

    def to_dict(self):
        return {
            'temporada': self.temporada,
            'referencia': self.referencia,
            'cantidad_juicio': self.cantidad_juicio,
            'autor_nombre': self.autor_nombre,
            'nota': self.nota,
            'q_modelo': self.q_modelo,
            'costo_unitario': float(self.costo_unitario) if self.costo_unitario else None,
            'distribucion': self.distribucion,
            'fecha': self.fecha_actualizacion.isoformat() if self.fecha_actualizacion else None,
        }
