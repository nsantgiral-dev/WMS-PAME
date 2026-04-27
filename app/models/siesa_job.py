import json
from datetime import datetime
from app.extensions import db


# Backoff exponencial: intento 1 → 5min, 2 → 15min, 3 → 45min, 4 → 120min, 5 → 180min
# 5 intentos cubre caídas de Siesa de hasta ~6h sin intervención manual.
_BACKOFF_MINUTOS = [5, 15, 45, 120, 180]


class SiesaJob(db.Model):
    """
    Dead Letter Queue para jobs asíncronos hacia Siesa.

    Todo conector POST que no puede bloquearse al operario pasa por aquí.
    El scheduler lo procesa cada 5 minutos. Si falla 3 veces, estado=FALLIDO
    y aparece alerta roja en el dashboard del admin.

    Garantiza que WMS local y Siesa nunca queden desincronizados sin que
    el administrador lo sepa.
    """
    __tablename__ = 'siesa_jobs'

    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(50), nullable=False)
    payload = db.Column(db.Text, nullable=False)           # JSON serializado
    referencia_tipo = db.Column(db.String(50), nullable=True)
    referencia_id = db.Column(db.Integer, nullable=True)

    estado = db.Column(db.String(20), nullable=False, default='PENDIENTE')
    intentos = db.Column(db.Integer, nullable=False, default=0)
    max_intentos = db.Column(db.Integer, nullable=False, default=5)
    proximo_intento = db.Column(db.DateTime, nullable=True)  # None = procesar inmediatamente
    resultado = db.Column(db.Text, nullable=True)
    error_ultimo = db.Column(db.Text, nullable=True)

    creado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_completado = db.Column(db.DateTime, nullable=True)

    @classmethod
    def encolar(cls, tipo: str, payload: dict,
                 referencia_tipo: str = None, referencia_id: int = None,
                 creado_por_id: int = None) -> 'SiesaJob':
        """Crea un job en la cola. No hace commit — el caller lo hace."""
        job = cls(
            tipo=tipo,
            payload=json.dumps(payload, ensure_ascii=False),
            referencia_tipo=referencia_tipo,
            referencia_id=referencia_id,
            creado_por_id=creado_por_id,
            estado='PENDIENTE',
            intentos=0,
        )
        db.session.add(job)
        return job

    def get_payload(self) -> dict:
        return json.loads(self.payload)

    def marcar_completado(self, resultado: dict):
        self.estado = 'COMPLETADO'
        self.resultado = json.dumps(resultado, ensure_ascii=False)
        self.fecha_completado = datetime.utcnow()

    def marcar_fallo(self, error: str):
        from datetime import timedelta
        self.intentos += 1
        self.error_ultimo = str(error)[:2000]

        if self.intentos >= self.max_intentos:
            self.estado = 'FALLIDO'
            self.proximo_intento = None
        else:
            # Backoff exponencial
            minutos = _BACKOFF_MINUTOS[min(self.intentos - 1, len(_BACKOFF_MINUTOS) - 1)]
            self.proximo_intento = datetime.utcnow() + timedelta(minutes=minutos)
            self.estado = 'PENDIENTE'

    def to_dict(self):
        return {
            'id': self.id,
            'tipo': self.tipo,
            'referencia_tipo': self.referencia_tipo,
            'referencia_id': self.referencia_id,
            'estado': self.estado,
            'intentos': self.intentos,
            'max_intentos': self.max_intentos,
            'error_ultimo': self.error_ultimo,
            'proximo_intento': self.proximo_intento.isoformat() if self.proximo_intento else None,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_completado': self.fecha_completado.isoformat() if self.fecha_completado else None,
        }
