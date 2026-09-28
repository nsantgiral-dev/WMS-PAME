"""Ausencias declaradas del personal (m051asignacion, 2026-09-27).

Una incapacidad, unas vacaciones o un permiso se declaran con **fecha de
regreso**: la persona no recibe trabajo desde `desde` hasta el día anterior a
`regreso`. Sin fecha de regreso la ausencia sigue vigente hasta que alguien la
cierre (Regla 0: no se adivina el regreso).

No se borra: una ausencia equivocada se **anula** (quién, cuándo, por qué). La
política —qué significa estar disponible— vive en `app/services/presencia.py`;
esto es solo el registro.
"""
from datetime import datetime

from app.extensions import db


class MotivoAusencia:
    INCAPACIDAD = 'INCAPACIDAD'
    VACACIONES = 'VACACIONES'
    PERMISO = 'PERMISO'
    CALAMIDAD = 'CALAMIDAD'
    OTRO = 'OTRO'
    VALIDOS = (INCAPACIDAD, VACACIONES, PERMISO, CALAMIDAD, OTRO)
    TEXTO = {
        INCAPACIDAD: 'Incapacidad', VACACIONES: 'Vacaciones', PERMISO: 'Permiso',
        CALAMIDAD: 'Calamidad', OTRO: 'Ausencia',
    }


class AusenciaUsuario(db.Model):
    __tablename__ = 'ausencias_usuario'

    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False, index=True)
    motivo = db.Column(db.String(20), nullable=False)
    #: Primer día ausente (día operativo de Bogotá).
    desde = db.Column(db.Date, nullable=False)
    #: Primer día de vuelta. NULL = sin fecha: sigue ausente hasta que se cierre.
    regreso = db.Column(db.Date, nullable=True)
    nota = db.Column(db.String(300), nullable=True)
    registrada_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    registrada_en = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    anulada_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    anulada_en = db.Column(db.DateTime, nullable=True)
    motivo_anulacion = db.Column(db.String(300), nullable=True)

    usuario = db.relationship('Usuario', foreign_keys=[usuario_id])

    def cubre(self, dia) -> bool:
        """¿Esta ausencia deja a la persona fuera el día operativo `dia`?"""
        if self.anulada_en is not None:
            return False
        if dia < self.desde:
            return False
        return self.regreso is None or dia < self.regreso

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'usuario_id': self.usuario_id,
            'motivo': self.motivo,
            'motivo_texto': MotivoAusencia.TEXTO.get(self.motivo, self.motivo),
            'desde': self.desde.isoformat() if self.desde else None,
            'regreso': self.regreso.isoformat() if self.regreso else None,
            'nota': self.nota,
            'registrada_por_id': self.registrada_por_id,
            'registrada_en': self.registrada_en.isoformat() if self.registrada_en else None,
            'anulada_en': self.anulada_en.isoformat() if self.anulada_en else None,
        }
