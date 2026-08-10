"""
Registrar corridas de sync sin poder tumbar el sync.

Regla que gobierna todo este archivo: **anotar no puede romper lo anotado.** Un
sync de catálogo son ~17.000 peticiones a Siesa; abortarlo porque falló un
INSERT de auditoría cambiaría un problema de registro por uno de operación.

Pero el silencio tampoco sirve —regla 5— así que un fallo al anotar se loguea
como `CRITICAL` y además queda visible del lado del lector: si la memoria dice
que corrió y la tabla no tiene fila, `estado_persistido()` lo declara en vez de
elegir una de las dos versiones.
"""
import json
import logging
from datetime import datetime

from app.extensions import db
from app.models.registro_sync import TIPOS, RegistroSync

logger = logging.getLogger(__name__)


def abrir(tipo: str):
    """Anota que una corrida empezó. Devuelve el id, o `None` si no se pudo.

    El id se pasa a `cerrar_*`. Si es `None` el cierre no hace nada — la corrida
    sigue igual, solo queda sin registro, y eso se ve como un hueco en la tabla.
    """
    if tipo not in TIPOS:
        logger.critical('[REGISTRO_SYNC] tipo desconocido: %r — no se anota', tipo)
        return None
    try:
        r = RegistroSync(tipo=tipo, inicio=datetime.utcnow())
        db.session.add(r)
        db.session.commit()
        return r.id
    except Exception as e:
        db.session.rollback()
        logger.critical('[REGISTRO_SYNC] no se pudo abrir %s: %s', tipo, e)
        return None


def _cerrar(registro_id, ok: bool, resultado=None, error=None):
    if registro_id is None:
        return
    try:
        r = db.session.get(RegistroSync, registro_id)
        if r is None:
            logger.critical('[REGISTRO_SYNC] desapareció el registro %s', registro_id)
            return
        r.fin = datetime.utcnow()
        r.ok = ok
        if resultado is not None:
            r.resultado = json.dumps(resultado, default=str)[:20000]
        if error is not None:
            r.error = str(error)[:4000]
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.critical('[REGISTRO_SYNC] no se pudo cerrar %s: %s', registro_id, e)


def cerrar_ok(registro_id, resultado=None):
    _cerrar(registro_id, True, resultado=resultado)


def cerrar_error(registro_id, error):
    _cerrar(registro_id, False, error=error)


def ultimo(tipo: str):
    """La última corrida de ese tipo, o `None` si no hay ninguna."""
    try:
        r = (RegistroSync.query
             .filter_by(tipo=tipo)
             .order_by(RegistroSync.inicio.desc())
             .first())
        return r.to_dict() if r else None
    except Exception as e:
        # Devolver `None` acá sería decir "nunca corrió" cuando la verdad es
        # "no pude leer" — el defecto que este módulo existe para eliminar.
        logger.error('[REGISTRO_SYNC] no se pudo leer %s: %s', tipo, e)
        return {'_error_lectura': str(e)[:200]}


def ultimo_ok(tipo: str):
    """La última corrida **exitosa**. Es la que contesta «¿ya se cargó?».

    Distinta de `ultimo()` a propósito: si el catálogo se sincronizó bien el
    lunes y falló el martes, `ultimo()` dice «fallo» y `ultimo_ok()` dice
    «el lunes». Las dos son verdad y responden preguntas distintas.
    """
    try:
        r = (RegistroSync.query
             .filter_by(tipo=tipo, ok=True)
             .order_by(RegistroSync.inicio.desc())
             .first())
        return r.to_dict() if r else None
    except Exception as e:
        logger.error('[REGISTRO_SYNC] no se pudo leer ok de %s: %s', tipo, e)
        return {'_error_lectura': str(e)[:200]}


def estado_persistido(tipo: str, en_memoria_corrio: bool = False):
    """Lo que la TABLA sabe de ese tipo, más la contradicción si la hay.

    `en_memoria_corrio` es lo que dice el dict del servicio. Si la memoria
    afirma que corrió y la tabla no tiene ninguna fila, **no se elige una de las
    dos versiones**: se declaran ambas. Elegir sería inventar.
    """
    ult = ultimo(tipo)
    ok = ultimo_ok(tipo)
    salida = {
        'ultima_corrida': ult,
        'ultima_exitosa': ok,
        'alguna_vez_ok': bool(ok and not ok.get('_error_lectura')),
    }
    if en_memoria_corrio and ult is None:
        salida['inconsistencia'] = (
            'el proceso dice que corrió pero no hay fila en registros_sync — '
            'el registro falló (ver logs CRITICAL) o la tabla se limpió'
        )
    return salida
