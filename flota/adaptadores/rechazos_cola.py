"""
Los registros del conductor que la cola mandó y el servidor rechazó.

Tres escritores y un lector, y ninguno más:

```
anotar(...)             el rechazo, en su propia transacción (@idempotente)
resolver(usuario, clave) la misma clave entró: `resuelto` (commitea quien llama)
marcar(...)             el conductor pidió ayuda o descartó en su teléfono
cerrar(...)             control de flota lo dio por atendido, con motivo
abiertos(...)           lo que la bandeja muestra como pendiente
```

**La anotación va en su propia transacción a propósito.** `@idempotente` hace
`rollback` de todo lo que el registro rechazado alcanzó a escribir —la clave no
se gasta, las fotos no quedan colgadas—; la anotación tiene que sobrevivir a
ese rollback, porque es lo único que le cuenta a control de flota que un
tanqueo pagado no entró.
"""
from datetime import datetime
from typing import List, Optional

from app.extensions import db
from flota.adaptadores.modelos import (ESTADO_RECHAZO_COLA, OPERACION_IDEMPOTENTE,
                                       RechazoCola)
from flota.dominio.errores import ErrorFlota


class RechazoInvalido(ErrorFlota):
    """El pedido sobre un rechazo no se puede aceptar."""


#: Lo que el conductor puede hacer sobre su rechazo desde el teléfono.
ACCIONES_DEL_CONDUCTOR = ('ayuda', 'descartar')


def _placa(datos) -> Optional[str]:
    crudo = datos['placa'] if isinstance(datos, dict) and 'placa' in datos else None
    placa = (str(crudo).strip().upper() if crudo is not None else '') or None
    return placa[:20] if placa else None


def _vehiculo_id(placa):
    if not placa:
        return None
    from app.models.vehiculo import Vehiculo

    v = Vehiculo.query.filter_by(placa=placa).first()
    return v.id if v is not None else None


def texto_del_rechazo(cuerpo, status) -> str:
    """El texto que el servidor dio, o uno que diga que no dio ninguno."""
    if isinstance(cuerpo, dict):
        for k in ('error', 'detalle'):
            if k in cuerpo and cuerpo[k]:
                return str(cuerpo[k])
    return f'El servidor lo rechazó (código {status}) sin decir por qué.'


def anotar(*, usuario_id: int, clave: str, operacion: str, datos: dict,
           status: int, cuerpo_respuesta, ts_dispositivo: Optional[datetime]) -> None:
    """Anota (o re-anota) un rechazo, en una transacción propia.

    Nunca levanta hacia quien la llama: el que manda el 409 al teléfono no
    puede convertirse en un 500 porque la anotación falló. Si falla, se deja
    escrito en el log y el teléfono igual conserva la operación.
    """
    import logging

    if operacion not in OPERACION_IDEMPOTENTE:
        return
    ahora = datetime.utcnow()
    try:
        fila = RechazoCola.query.filter_by(usuario_id=usuario_id, clave=clave).first()
        mensaje = texto_del_rechazo(cuerpo_respuesta, status)
        if fila is None:
            placa = _placa(datos)
            db.session.add(RechazoCola(
                clave=clave, operacion=operacion, usuario_id=usuario_id,
                placa=placa, vehiculo_id=_vehiculo_id(placa),
                status_http=int(status), mensaje=mensaje, intentos=1,
                primer_ts=ahora, ultimo_ts=ahora, ts_dispositivo=ts_dispositivo,
                estado='abierto'))
        else:
            fila.intentos = (fila.intentos or 0) + 1
            fila.ultimo_ts = ahora
            fila.status_http = int(status)
            fila.mensaje = mensaje
            # Un reintento que vuelve a fallar reabre lo que el conductor había
            # descartado: si lo volvió a mandar, no lo había descartado.
            if fila.estado in ('descartado', 'resuelto'):
                fila.estado = 'abierto'
        db.session.commit()
    except Exception:
        db.session.rollback()
        logging.getLogger(__name__).exception(
            'no se pudo anotar el rechazo %s de %s', clave, usuario_id)


def resolver(usuario_id: int, clave: str) -> None:
    """La misma clave entró: el rechazo queda `resuelto`.

    **Sin commit**: lo hace quien llama (`@idempotente`), en el mismo commit
    que anota la respuesta del registro que por fin entró.
    """
    fila = RechazoCola.query.filter_by(usuario_id=usuario_id, clave=clave).first()
    if fila is not None and fila.estado != 'cerrado':
        fila.estado = 'resuelto'


def cerrado_por_flota(usuario_id: int, clave: str) -> Optional[RechazoCola]:
    """El rechazo de esta clave, si control de flota lo CERRÓ (2026-09-29).

    Cerrar es «lo registré a mano» o «no va»: un «Reintentar» posterior del
    conductor con la misma clave lo duplicaría (el tanqueo del conductor no
    lleva número de recibo). La clave cerrada ya no ejecuta nada.
    """
    fila = RechazoCola.query.filter_by(usuario_id=usuario_id, clave=clave).first()
    return fila if fila is not None and fila.estado == 'cerrado' else None


def de_otro(usuario_id: int, clave: str) -> bool:
    """¿La clave es de un rechazo de OTRA persona? La pregunta del derecho."""
    fila = RechazoCola.query.filter_by(clave=clave).first()
    return fila is not None and fila.usuario_id != usuario_id


def marcar(*, usuario_id: int, clave: str, accion: str, operacion: Optional[str],
           datos: dict, mensaje: Optional[str],
           ts_dispositivo: Optional[datetime]) -> RechazoCola:
    """El conductor pidió ayuda o descartó el registro en su teléfono.

    Si el servidor no tenía la fila —el rechazo fue anterior a esta tabla, o la
    anotación falló— se crea con lo que el teléfono sabe: el aviso vale más que
    la forma exacta de cómo se perdió.
    """
    if accion not in ACCIONES_DEL_CONDUCTOR:
        raise RechazoInvalido(
            f'Acción desconocida: {accion!r}. Las válidas son '
            f'{", ".join(ACCIONES_DEL_CONDUCTOR)}.')
    ahora = datetime.utcnow()
    fila = RechazoCola.query.filter_by(usuario_id=usuario_id, clave=clave).first()
    if fila is None:
        if operacion not in OPERACION_IDEMPOTENTE:
            raise RechazoInvalido(
                'Para avisar un registro que el servidor no conoce hace falta '
                'decir qué era (recibo, daño, inspección o tanqueo).')
        placa = _placa(datos)
        fila = RechazoCola(
            clave=clave, operacion=operacion, usuario_id=usuario_id,
            placa=placa, vehiculo_id=_vehiculo_id(placa), status_http=0,
            mensaje=(mensaje or 'El teléfono dice que no se pudo registrar.').strip(),
            intentos=0, primer_ts=ahora, ultimo_ts=ahora,
            ts_dispositivo=ts_dispositivo, estado='abierto')
        db.session.add(fila)
    if accion == 'ayuda':
        fila.pidio_ayuda_ts = ahora
        if fila.estado == 'descartado':
            fila.estado = 'abierto'
    elif fila.estado == 'abierto':
        # Un registro que no entró y el conductor decide dejar ir: queda en la
        # bitácora, no solo en esta fila (quién, cuándo, qué era).
        from app.services.bitacora import registrar_accion

        db.session.flush()
        registrar_accion(
            'DESCARTAR', fila, usuario_id=usuario_id,
            motivo='El conductor lo descartó en su teléfono',
            antes={'estado': fila.estado, 'operacion': fila.operacion,
                   'placa': fila.placa, 'mensaje': fila.mensaje},
            despues={'estado': 'descartado'})
        fila.estado = 'descartado'
    db.session.commit()
    return fila


def cerrar(*, rechazo_id: int, usuario_id: int, motivo: str) -> RechazoCola:
    """Control de flota lo da por atendido. Motivo obligatorio."""
    motivo = (motivo or '').strip()
    if not motivo:
        raise RechazoInvalido(
            'Cerrar un registro que no entró exige escribir qué se hizo: sin '
            'eso, el pendiente desaparece sin rastro.')
    fila = db.session.get(RechazoCola, rechazo_id)
    if fila is None:
        raise LookupError(f'No existe el rechazo {rechazo_id}')
    if fila.estado == 'resuelto':
        raise RechazoInvalido('Ese registro ya entró: no hay nada que cerrar.')
    fila.estado = 'cerrado'
    fila.cerrado_ts = datetime.utcnow()
    fila.cerrado_por_usuario_id = usuario_id
    fila.cierre_motivo = motivo
    db.session.commit()
    return fila


def abiertos(dias_descartados: int = 7) -> List[RechazoCola]:
    """Lo que la bandeja muestra: los abiertos, y los descartados recientes.

    Un descartado se muestra una semana: el conductor dijo «no importa», pero
    control de flota tiene que poder ver que un registro no entró. Después de
    la semana sigue en la tabla; la bandeja deja de pedir trabajo con él.
    """
    from datetime import timedelta

    desde = datetime.utcnow() - timedelta(days=dias_descartados)
    return (RechazoCola.query
            .filter(db.or_(RechazoCola.estado == 'abierto',
                           db.and_(RechazoCola.estado == 'descartado',
                                   RechazoCola.ultimo_ts >= desde)))
            .order_by(RechazoCola.primer_ts, RechazoCola.id).all())


__all__ = ['anotar', 'resolver', 'marcar', 'cerrar', 'abiertos', 'de_otro',
           'texto_del_rechazo', 'RechazoInvalido', 'ACCIONES_DEL_CONDUCTOR',
           'ESTADO_RECHAZO_COLA']
