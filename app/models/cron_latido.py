"""
El latido de cada cron: lo que corrió DE VERDAD y cuándo (P1-11, 2026-09-25).

`SCHEDULERS_ACTIVOS` se escribe al arrancar, en memoria del proceso: la web no
ve los crons del worker, y un cron registrado que revienta en cada corrida
seguía figurando «activo». Esta tabla la escribe el decorador `con_latido`
(`app/services/cron_latido.py`) en cada corrida, en el proceso que la corrió.
`/api/health/siesa` y 🩺 Salud del dato la leen.
"""
from app.extensions import db


class CronLatido(db.Model):
    __tablename__ = 'cron_latido'

    id = db.Column(db.Integer, primary_key=True)
    #: El `id` del job en APScheduler (o el nombre que declara el decorador).
    nombre = db.Column(db.String(80), nullable=False)
    #: Qué servicio de Railway lo corrió (`RAILWAY_SERVICE_NAME`), o `local`.
    servicio = db.Column(db.String(60), nullable=False)
    ultimo_inicio = db.Column(db.DateTime, nullable=True)
    ultimo_fin = db.Column(db.DateTime, nullable=True)
    ultimo_ok = db.Column(db.Boolean, nullable=True)
    ultimo_ok_en = db.Column(db.DateTime, nullable=True)
    ultimo_error = db.Column(db.Text, nullable=True)
    corridas = db.Column(db.Integer, nullable=False, default=0, server_default='0')
    fallos_seguidos = db.Column(db.Integer, nullable=False, default=0, server_default='0')

    __table_args__ = (
        db.UniqueConstraint('nombre', 'servicio', name='uq_cron_latido_nombre_servicio'),
    )

    def to_dict(self):
        def _iso(v):
            return v.isoformat() if v else None
        return {
            'nombre': self.nombre, 'servicio': self.servicio,
            'ultimo_inicio': _iso(self.ultimo_inicio), 'ultimo_fin': _iso(self.ultimo_fin),
            'ultimo_ok': self.ultimo_ok, 'ultimo_ok_en': _iso(self.ultimo_ok_en),
            'ultimo_error': self.ultimo_error, 'corridas': self.corridas,
            'fallos_seguidos': self.fallos_seguidos,
        }
