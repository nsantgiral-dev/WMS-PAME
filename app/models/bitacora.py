"""
BitacoraAccion — lo que alguien eliminó, canceló, anuló, reabrió o editó.

Fase 0 de analítica (2026-09-24). La analítica tiene que poder reconstruir el
recorrido pedido → caja y ver **todo lo que se elimina, cancela o edita, con
quién, cuándo y por qué**. Antes de esta tabla esas tres preguntas se
contestaban de memoria: `cancelar_picking` recibía el motivo y lo tiraba,
`reabrir_picking` borraba al operario, reintentar un job FALLIDO borraba el
error que lo había hecho fallar, y un borrado físico no dejaba nada.

## Solo agregar

Una fila por acción. No se actualiza ni se borra: el historial ES el dato.

## Sin claves foráneas, a propósito

`entidad_id`, `usuario_id` y `almacen_id` son enteros sueltos, y
`entidad_codigo` guarda el código legible (`PK-...`, `PD1352`, `ST-...`).
La tabla está en `PROTEGIDAS_ANALITICAS` del acta de corte: sobrevive al
reset transaccional, y las filas a las que apunta **no**. Con una FK hacia
una tabla OPERATIVA el corte fallaría (o la cascada se llevaría la
bitácora); sin el código legible, un id suelto de una tarea borrada no le
dice nada a nadie.
"""
from datetime import datetime

from app.extensions import db


class BitacoraAccion(db.Model):
    __tablename__ = 'bitacora_acciones'

    id = db.Column(db.Integer, primary_key=True)

    #: Instante técnico, UTC naive — como todo `created_at` del WMS.
    ocurrido_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    #: El día que alguien LEE: Bogotá (Regla 5). Se calcula al escribir, no al
    #: consultar, para que filtrar por día no dependa de convertir en SQL.
    dia_operativo = db.Column(db.Date, nullable=False)

    #: Vocabulario cerrado: `app.services.bitacora.ACCIONES`.
    accion = db.Column(db.String(20), nullable=False)
    entidad = db.Column(db.String(40), nullable=False)
    entidad_id = db.Column(db.Integer, nullable=True)
    entidad_codigo = db.Column(db.String(80), nullable=True)

    #: `None` = lo hizo el sistema (un cron, una cascada sin usuario).
    usuario_id = db.Column(db.Integer, nullable=True)
    motivo = db.Column(db.Text, nullable=True)

    #: Solo los campos que la acción cambia (o la fila entera, si se borra).
    antes = db.Column(db.JSON, nullable=True)
    despues = db.Column(db.JSON, nullable=True)

    almacen_id = db.Column(db.Integer, nullable=True)
    #: Ruta HTTP o proceso que ejecutó la acción.
    origen = db.Column(db.String(120), nullable=True)

    __table_args__ = (
        db.Index('ix_bitacora_dia', 'dia_operativo'),
        db.Index('ix_bitacora_entidad', 'entidad', 'entidad_id'),
        db.Index('ix_bitacora_accion', 'accion'),
        db.Index('ix_bitacora_usuario', 'usuario_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'ocurrido_en': self.ocurrido_en.isoformat() if self.ocurrido_en else None,
            'dia_operativo': self.dia_operativo.isoformat() if self.dia_operativo else None,
            'accion': self.accion,
            'entidad': self.entidad,
            'entidad_id': self.entidad_id,
            'entidad_codigo': self.entidad_codigo,
            'usuario_id': self.usuario_id,
            'motivo': self.motivo,
            'antes': self.antes,
            'despues': self.despues,
            'almacen_id': self.almacen_id,
            'origen': self.origen,
        }
