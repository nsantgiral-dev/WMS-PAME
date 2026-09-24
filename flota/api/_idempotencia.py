"""
Reenvío sin duplicar — la clave que el celular pone al ENCOLAR.

El conductor registra el recibo, la inspección, el daño o el tanqueo muchas
veces sin señal. La pantalla lo guarda en la cola del teléfono (IndexedDB,
fotos incluidas) y lo manda cuando vuelve la red. Un reenvío es inevitable: la
respuesta se pierde, el teléfono no sabe si llegó, y lo vuelve a mandar.

Regla 9 del módulo: *«un timeout no significa que falló»*. Sin esto, el segundo
envío de un recibo **cierra la custodia recién abierta y abre otra** con los
mismos kilómetros, un daño se reporta dos veces y un tanqueo se paga dos veces
en el costo por kilómetro.

## Cómo

`@idempotente('traspaso', 'custodia_id')` va DEBAJO de `@exige` — primero se
decide si el usuario puede, después si ya lo hizo. El decorador:

1. Lee `clave_idempotencia` del cuerpo (o el header `Idempotency-Key`). Sin
   clave, no hace nada: el panel de escritorio sigue igual.
2. Si la clave ya existe y es de este usuario y esta operación, devuelve la
   respuesta guardada con `repetida: true` y **200**, sin ejecutar nada.
3. Si no, agrega la fila a la sesión ANTES de ejecutar. El adaptador hace su
   `commit` y la fila entra **en la misma transacción que el hecho**: o quedan
   los dos, o ninguno. Después anota el id y la respuesta.

Si la operación falla (4xx/5xx) se hace `rollback`: la clave no queda gastada y
el conductor puede reintentar con los datos corregidos.
"""
import json
from datetime import datetime, timezone
from functools import wraps

from flask import jsonify, request
from flask_jwt_extended import get_jwt_identity
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from flota.adaptadores.modelos import OperacionIdempotente

#: Largo máximo de la clave. Un UUID son 36; se deja holgura sin abrir la
#: puerta a que alguien guarde un texto arbitrario en una columna indexada.
LARGO_MAXIMO = 64


def _clave_del_pedido(datos):
    crudo = datos['clave_idempotencia'] if 'clave_idempotencia' in datos else None
    if crudo is None:
        crudo = request.headers.get('Idempotency-Key')
    if crudo is None:
        return None
    clave = str(crudo).strip()
    return clave or None


def _ts_dispositivo(datos):
    """La hora del teléfono, en UTC ingenuo. `None` si no vino o no se entiende.

    No decide nada: se guarda para ver cuánto tardó el registro en llegar.
    """
    crudo = datos['ts_dispositivo'] if 'ts_dispositivo' in datos else None
    if not crudo:
        return None
    try:
        ts = datetime.fromisoformat(str(crudo).replace('Z', '+00:00'))
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
    return ts


def _respuesta_previa(previa, usuario_id, operacion):
    if previa.usuario_id != usuario_id or previa.operacion != operacion:
        # No es un reintento: es otra persona u otro gesto con la misma clave.
        # Devolverle el resultado ajeno sería filtrarle datos; ejecutar sería
        # ignorar una clave que ya significa otra cosa.
        return jsonify({
            'error': 'Este registro ya se usó para otra cosa. Volvé a hacerlo '
                     'desde la pantalla: se genera uno nuevo.',
        }), 409
    cuerpo = json.loads(previa.respuesta) if previa.respuesta else {
        'entidad_id': previa.entidad_id}
    cuerpo['repetida'] = True
    return jsonify(cuerpo), 200


def idempotente(operacion, campo_id):
    """Decorador. `campo_id` es la clave del JSON de respuesta con el id creado."""

    def decorador(f):
        @wraps(f)
        def envoltura(*args, **kwargs):
            datos = request.get_json(silent=True) or {}
            clave = _clave_del_pedido(datos)
            if clave is None:
                return f(*args, **kwargs)
            if len(clave) > LARGO_MAXIMO:
                return jsonify({'error': 'Clave de reenvío inválida.'}), 400

            usuario_id = int(get_jwt_identity())
            previa = OperacionIdempotente.query.filter_by(clave=clave).first()
            if previa is not None:
                return _respuesta_previa(previa, usuario_id, operacion)

            fila = OperacionIdempotente(
                clave=clave, operacion=operacion, usuario_id=usuario_id,
                creado_ts=datetime.utcnow(), ts_dispositivo=_ts_dispositivo(datos))
            db.session.add(fila)
            try:
                resultado = f(*args, **kwargs)
            except IntegrityError:
                # Dos envíos con la misma clave al mismo tiempo: el segundo choca
                # contra el único. Lo que haya escrito el primero es la verdad.
                db.session.rollback()
                previa = OperacionIdempotente.query.filter_by(clave=clave).first()
                if previa is not None:
                    return _respuesta_previa(previa, usuario_id, operacion)
                raise
            except Exception:
                db.session.rollback()
                raise

            resp, status = (resultado if isinstance(resultado, tuple)
                            else (resultado, 200))
            if not 200 <= int(status) < 300:
                # Rechazada: nada de esta operación puede quedar, y la clave
                # tampoco — el reintento con datos corregidos tiene que poder
                # entrar.
                db.session.rollback()
                return resultado

            cuerpo = resp.get_json(silent=True) if hasattr(resp, 'get_json') else None
            if fila in db.session:
                fila.entidad_id = (cuerpo[campo_id] if isinstance(cuerpo, dict)
                                   and campo_id in cuerpo else None)
                fila.respuesta = json.dumps(cuerpo) if cuerpo is not None else None
            db.session.commit()
            return resultado

        # Marca legible desde el objeto vivo: el trinquete la busca en cada
        # endpoint que la cola del conductor reenvía, sin leer texto.
        envoltura.idempotente_operacion = operacion
        return envoltura

    return decorador


__all__ = ['idempotente']
