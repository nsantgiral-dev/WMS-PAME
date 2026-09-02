"""
Quién declaró que los números del WMS cuadran con el mundo, y contra qué.

No es una bitácora: es **el único dato del sistema que no se puede fabricar
desde adentro**. Todo lo demás —host, compañía, documentos de hoy, montos
coherentes entre sí— lo hereda igual una copia de la base.

Se guarda `huella_config` a propósito: una declaración vale para el ambiente
en el que se hizo. Si el host o la compañía cambian, la declaración anterior
deja de decir nada y el estado vuelve a ALARMA — aunque antes estuviera en
verde. Ver `app/services/ambiente.py`.
"""
from app.extensions import db


class DeclaracionAmbiente(db.Model):
    __tablename__ = 'declaraciones_ambiente'

    id = db.Column(db.Integer, primary_key=True)

    declarado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'),
                              nullable=False)
    #: `index=True` para que el modelo declare lo mismo que la migración
    #: m010 — `estado()` ordena por esta columna para tomar la última.
    #: Sin esto, `flask db check` reporta deriva entre esquema y modelos.
    declarado_en = db.Column(db.DateTime, nullable=False, index=True)

    #: Hash de host + compañía al momento de declarar. Si cambia, la
    #: declaración caduca sola.
    huella_config = db.Column(db.String(32), nullable=False)
    host = db.Column(db.String(200), nullable=False)
    id_compania = db.Column(db.String(20), nullable=False)

    #: Qué se cuadró — «existencia de PAPELSP9218 en NB1», no «revisé».
    concepto = db.Column(db.String(200), nullable=False)
    cifra_wms = db.Column(db.String(60), nullable=False)
    cifra_externa = db.Column(db.String(60), nullable=False)

    #: De dónde salió la cifra de afuera. **No puede ser el propio WMS**: el
    #: error que esto existe para impedir es medir el sistema contra sí mismo.
    fuente_externa = db.Column(db.String(200), nullable=False)

    notas = db.Column(db.Text)

    usuario = db.relationship('Usuario', foreign_keys=[declarado_por],
                              lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'declarado_por': self.declarado_por,
            'declarado_por_nombre': (self.usuario.nombre if self.usuario
                                     else None),
            'declarado_en': (self.declarado_en.isoformat()
                             if self.declarado_en else None),
            'host': self.host,
            'id_compania': self.id_compania,
            'concepto': self.concepto,
            'cifra_wms': self.cifra_wms,
            'cifra_externa': self.cifra_externa,
            'fuente_externa': self.fuente_externa,
            'notas': self.notas,
        }
