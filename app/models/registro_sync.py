"""
Qué sincronizaciones corrieron, en una tabla — no en la memoria del proceso.

Hasta el 2026-08-10 el estado de los tres syncs de arranque vivía en
diccionarios de módulo: `siesa_sync_service._sync_estado`,
`inventario_siesa_service._estado_carga` y `_estado_setup`, más el de códigos
de barras. Cada deploy de Railway los ponía en `None`.

El problema no es perder una estadística. Es que **`None` significaba dos cosas
distintas y el endpoint respondía lo mismo para las dos**: «nunca se corrió» y
«se corrió antes del último reinicio». Medido en producción el 2026-08-10:
`resultado_catalogo: null` con tres deploys el mismo día — imposible saber si el
catálogo estaba cargado o no.

Eso importa el día del corte. La secuencia de arranque es: sincronizar catálogo
→ códigos de barras → cargar stock inicial **una sola vez**. Si a mitad el
contenedor reinicia, la evidencia de qué llegó a correr desaparece, y la
pregunta «¿ya cargamos el stock?» pasa a contestarse de memoria. Cargarlo dos
veces duplica el inventario de arranque.

Una fila por corrida. Append-only: no se actualiza ni se borra, porque el
historial ES el dato — «se corrió tres veces y las tres fallaron» y «se corrió
una vez y salió bien» no pueden verse iguales.
"""
import json
from datetime import datetime

from app.extensions import db

#: Los tipos que se registran. Lista cerrada a propósito: un tipo libre haría
#: que un typo (`'catalogo '`) cree una serie paralela que nadie consulta.
TIPOS = ('catalogo', 'barcodes', 'stock', 'setup_inicial', 'reconciliacion')


class RegistroSync(db.Model):
    """Una corrida de sincronización. Sobrevive al reinicio del contenedor."""

    __tablename__ = 'registros_sync'

    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(40), nullable=False, index=True)
    inicio = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    fin = db.Column(db.DateTime)
    #: `None` = quedó abierta. **No es lo mismo que fallida**: una corrida
    #: abierta es un proceso que murió sin cerrar —reinicio, OOM, deploy a
    #: mitad— y eso es exactamente lo que hay que poder distinguir el día del
    #: corte. Un default a `False` la haría parecer un fallo conocido.
    ok = db.Column(db.Boolean)
    resultado = db.Column(db.Text)      # JSON del servicio, tal cual
    error = db.Column(db.Text)

    #: El mismo CHECK que crea la migración. Declarado **también acá** porque
    #: los tests construyen la base con `create_all()` desde el modelo: si el
    #: invariante viviera solo en la migración, ninguna prueba lo ejercitaría y
    #: una base nueva no lo tendría. Un guard que no está no da error — deja
    #: pasar.
    #:
    #: `IS NULL` / `IS NOT NULL` y no `= NULL`: una comparación con NULL da NULL
    #: y el CHECK la aprueba. Esa trampa ya costó una migración abortada.
    __table_args__ = (
        # Declarado con el nombre EXACTO que tiene en la base: existía en
        # migraciones y no en el modelo, y `flask db check` lo reportaba
        # como sobrante.
        db.Index('ix_registros_sync_tipo_inicio', 'tipo', 'inicio'),
        db.CheckConstraint(
            '(ok IS NULL AND fin IS NULL) OR (ok IS NOT NULL AND fin IS NOT NULL)',
            name='ck_registro_sync_cierre_completo'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'tipo': self.tipo,
            'inicio': self.inicio.isoformat() if self.inicio else None,
            'fin': self.fin.isoformat() if self.fin else None,
            'ok': self.ok,
            'estado': ('en_curso_o_interrumpida' if self.ok is None
                       else 'ok' if self.ok else 'fallo'),
            'resultado': json.loads(self.resultado) if self.resultado else None,
            'error': self.error,
        }
