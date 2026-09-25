"""
¿Esta base es de este ambiente? — el sello (P0-9, auditoría 2026-09-25).

## El caso

`docs/flujo_qa_produccion.md` describe cómo se recarga QA con una copia de
producción. La copia trae sus `siesa_jobs` PENDIENTE, y la DLQ de QA —con
`SKIP_FE_CHECK=true`, contra el Siesa compartido (antes del corte los dos
ambientes hablan con `serviciosqa`)— los ejecuta: remisiones, facturas,
recibos de caja de producción emitidos desde QA. `vars_criticas` trata
`serviciosqa` como inofensivo, así que ningún guard lo frena.

Ningún dato de la base distingue un original de su copia (ver `ambiente.py`).
Lo que sí se puede es **marcarla**: la primera vez que un proceso va a hablar
con Siesa sobre una base sin sello, la sella con su `RAILWAY_ENVIRONMENT_NAME`.
Desde ahí, un proceso de otro ambiente no postea.

## Tres respuestas, como toda lectura que decide un POST

| Proceso | Sello | ¿Postea? |
|---|---|---|
| sin `RAILWAY_ENVIRONMENT_NAME` (local, tests, un script) | cualquiera | **sí**, declarado: no hay ambiente contra qué comparar |
| con ambiente | ausente | **sí**, y lo sella con el suyo (primer uso) |
| con ambiente | el mismo | sí |
| con ambiente | otro | **no** (`AmbienteNoCoincide`) |
| con ambiente | no se pudo leer | **no**: no saber no es coincidir (Regla 0) |

Dos paredes: la DLQ no arranca su ciclo (los jobs quedan PENDIENTE, intactos)
y `ConnektaGateway._post` levanta antes de salir a la red (cubre los POST
inline de traslados, conteo, etc.). La respuesta se guarda 60 s por proceso.

## Lo que NO cubre

- **La transición**: una copia tomada ANTES de que producción corriera esta
  versión no trae sello, y el primer proceso de QA que la use la sella `QA`.
  Producción tiene que desplegar esto y correr un ciclo de DLQ antes de que se
  tome el próximo respaldo para QA.
- Un proceso sin la variable postea sobre cualquier base (Railway la inyecta
  siempre; fuera de Railway no hay a quién preguntarle).

Re-sellar (adoptar una copia a propósito) es `resellar()`, desde
`POST /api/health/sello-ambiente`: admin, motivo y el nombre del ambiente
escrito, con bitácora.
"""
import logging
import os
import time
from datetime import datetime

logger = logging.getLogger(__name__)

#: La inyecta Railway en cada proceso. La misma que usa `alertas_service`.
VAR_AMBIENTE = 'RAILWAY_ENVIRONMENT_NAME'

_TTL_S = 60.0
_cache = {'ts': 0.0, 'respuesta': None}


from app.services.connekta_gateway import ConnektaNoEnviado  # noqa: E402


class AmbienteNoCoincide(ConnektaNoEnviado):
    """La base está sellada para otro ambiente, o su sello no se pudo leer.
    No es un fallo de Siesa: no se salió a la red. Por eso es un
    `ConnektaNoEnviado` (tanda 2, 2026-09-25): quien tenía un pre-flag puesto
    lo puede bajar — el documento no existe en ningún Siesa."""


def ambiente_del_proceso():
    nombre = (os.getenv(VAR_AMBIENTE) or '').strip()
    return nombre or None


def _mismo(a: str, b: str) -> bool:
    return (a or '').strip().lower() == (b or '').strip().lower()


def _leer():
    from app.models.sello_ambiente import SelloAmbiente
    return SelloAmbiente.query.order_by(SelloAmbiente.id.desc()).first()


def estado() -> dict:
    """Lo que ve `/api/health/siesa`. **Solo lee**: no sella."""
    proceso = ambiente_del_proceso()
    try:
        sello = _leer()
        sello_d = sello.to_dict() if sello else None
        error = None
    except Exception as e:     # tabla ausente, base caída
        sello_d, error = None, str(e)[:200]
    if proceso is None:
        return {'proceso': None, 'sello': sello_d, 'coincide': None, 'bloquea': False,
                'texto': (f'Este proceso no tiene {VAR_AMBIENTE}: no se verifica a qué '
                          'ambiente pertenece la base (local o tests).')}
    if error:
        return {'proceso': proceso, 'sello': None, 'coincide': None, 'bloquea': True,
                'texto': f'No se pudo leer el sello de la base ({error}): no se postea.'}
    if sello_d is None:
        return {'proceso': proceso, 'sello': None, 'coincide': None, 'bloquea': False,
                'texto': ('La base todavía no tiene sello: el primer envío a Siesa '
                          f'la sellará «{proceso}».')}
    coincide = _mismo(sello_d['ambiente'], proceso)
    return {'proceso': proceso, 'sello': sello_d, 'coincide': coincide,
            'bloquea': not coincide,
            'texto': (f'Base sellada «{sello_d["ambiente"]}», proceso «{proceso}».'
                      + ('' if coincide else
                         ' NO COINCIDEN: ni la DLQ ni ningún POST salen a Siesa. '
                         'Si esta base es una copia que se adoptó a propósito, '
                         're-séllela desde el health (admin).'))}


def puede_postear(usar_cache: bool = True) -> tuple:
    """`(True, '')` o `(False, motivo)`. Sella la base si no tiene sello y el
    proceso declara su ambiente (el primer uso)."""
    ahora = time.monotonic()
    if usar_cache and _cache['respuesta'] is not None and ahora - _cache['ts'] < _TTL_S:
        return _cache['respuesta']
    respuesta = _decidir()
    _cache.update(ts=ahora, respuesta=respuesta)
    return respuesta


def _decidir() -> tuple:
    proceso = ambiente_del_proceso()
    if proceso is None:
        return True, ''
    try:
        from app.extensions import db
        from app.models.sello_ambiente import SelloAmbiente
        sello = _leer()
        if sello is None:
            db.session.add(SelloAmbiente(ambiente=proceso, sellado_en=datetime.utcnow(),
                                         sellado_por='sistema',
                                         motivo='primer envío a Siesa sobre esta base'))
            db.session.commit()
            logger.warning('[SELLO] Base sellada «%s» (primer uso).', proceso)
            return True, ''
        if _mismo(sello.ambiente, proceso):
            return True, ''
        return False, (f'La base está sellada «{sello.ambiente}» y este proceso es '
                       f'«{proceso}»: no se postea a Siesa (¿una copia de otro '
                       f'ambiente restaurada acá?).')
    except Exception as e:
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass
        return False, f'No se pudo leer el sello de la base ({str(e)[:200]}): no se postea.'


def exigir_para_postear():
    ok, motivo = puede_postear()
    if not ok:
        logger.error('[SELLO] %s', motivo)
        raise AmbienteNoCoincide(motivo)


def resellar(ambiente_escrito: str, motivo: str, usuario_id: int) -> dict:
    """Adoptar esta base para el ambiente del proceso, a propósito."""
    from app.extensions import db
    from app.models.sello_ambiente import SelloAmbiente
    from app.services.bitacora import motivo_obligatorio, registrar_accion
    motivo = motivo_obligatorio(motivo, 're-sellar la base')
    proceso = ambiente_del_proceso()
    if proceso is None:
        raise ValueError(f'Este proceso no tiene {VAR_AMBIENTE}: no hay ambiente que sellar.')
    if not _mismo(ambiente_escrito, proceso):
        raise ValueError(f'Escriba el nombre del ambiente de este proceso («{proceso}») '
                         'para confirmar.')
    anterior = _leer()
    fila = SelloAmbiente(ambiente=proceso, sellado_en=datetime.utcnow(),
                         sellado_por=f'usuario:{usuario_id}', motivo=motivo,
                         ambiente_anterior=anterior.ambiente if anterior else None)
    db.session.add(fila)
    db.session.flush()
    registrar_accion('EDITAR', fila, usuario_id=usuario_id, motivo=motivo,
                     antes={'ambiente': anterior.ambiente if anterior else None},
                     despues={'ambiente': proceso})
    db.session.commit()
    _cache.update(ts=0.0, respuesta=None)
    return fila.to_dict()


def _olvidar_cache():
    """Para tests."""
    _cache.update(ts=0.0, respuesta=None)
