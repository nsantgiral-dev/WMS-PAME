"""
A qué ambiente pertenece ESTA base del WMS (P0-9, 2026-09-25).

Una fila. La escribe el proceso que va a hablar con Siesa la primera vez que
lo intenta sobre una base sin sello (`sello_ambiente.puede_postear`), con el
`RAILWAY_ENVIRONMENT_NAME` de ese proceso. Después, un proceso de otro ambiente
no postea sobre ella: una copia de producción restaurada en QA trae el sello
`production`, y la DLQ de QA deja sus `siesa_jobs` PENDIENTE sin tocarlos.

Ver `app/services/sello_ambiente.py`.
"""
from datetime import datetime

from app.extensions import db


class SelloAmbiente(db.Model):
    __tablename__ = 'sello_ambiente'

    id = db.Column(db.Integer, primary_key=True)
    #: El `RAILWAY_ENVIRONMENT_NAME` del proceso que selló (`production`, `QA`).
    ambiente = db.Column(db.String(40), nullable=False)
    sellado_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    #: `sistema` (primer uso) o el usuario que re-selló a mano.
    sellado_por = db.Column(db.String(80), nullable=False)
    motivo = db.Column(db.Text, nullable=True)
    #: Si se re-selló: el ambiente que tenía antes.
    ambiente_anterior = db.Column(db.String(40), nullable=True)

    def to_dict(self):
        return {
            'ambiente': self.ambiente,
            'sellado_en': self.sellado_en.isoformat() if self.sellado_en else None,
            'sellado_por': self.sellado_por,
            'motivo': self.motivo,
            'ambiente_anterior': self.ambiente_anterior,
        }
