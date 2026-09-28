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


#: El ambiente que se sella explícitamente al arrancar (P1-5 a, 2026-09-26).
AMBIENTE_PRODUCCION = 'production'


def es_produccion(nombre) -> bool:
    return _mismo(nombre, AMBIENTE_PRODUCCION)


def veredicto(proceso, sello_ambiente, hay_historia: bool) -> tuple:
    """`(postea, sellar, motivo)`. **La política, una función** (P1-5,
    2026-09-26): la leen `puede_postear` (que decide y sella) y `estado` (que
    solo muestra).

    - Sin ambiente en el proceso: postea, no sella (local/tests, declarado).
    - Base sin sello y **sin historia** de `siesa_jobs`: la primera vez que
      alguien va a postear, se sella con su ambiente.
    - Base sin sello **con historia**: NO postea. Una base que ya habló con
      Siesa sin sello puede ser una copia de otro ambiente; adoptarla exige
      sellarla a propósito (admin, Siesa → Recuperación → Re-sellar).
    - Sello del mismo ambiente: postea. De otro: no postea.
    """
    if proceso is None:
        return True, False, ''
    if sello_ambiente is None:
        if hay_historia:
            return False, False, (
                f'La base no tiene sello de ambiente y ya tiene historia de envíos a '
                f'Siesa: puede ser una copia de otro ambiente. No se postea hasta '
                f'sellarla a propósito (admin: Siesa → Recuperación → Re-sellar, '
                f'escribiendo «{proceso}»).')
        return True, True, ''
    if _mismo(sello_ambiente, proceso):
        return True, False, ''
    return False, False, (f'La base está sellada «{sello_ambiente}» y este proceso es '
                          f'«{proceso}»: no se postea a Siesa (¿una copia de otro '
                          f'ambiente restaurada acá?).')


def _leer_en(conn):
    """`(ambiente del sello | None, hay_historia)` por una conexión PROPIA: la
    decisión no toca la sesión del llamador (antes `_decidir` hacía commit y
    rollback de `db.session`, y un rollback se llevaba los cambios pendientes
    de quien iba a postear — P3, 2026-09-26)."""
    from sqlalchemy import select
    from app.models.sello_ambiente import SelloAmbiente
    from app.models.siesa_job import SiesaJob
    t, jobs = SelloAmbiente.__table__, SiesaJob.__table__
    fila = conn.execute(select(t.c.ambiente).order_by(t.c.id.desc()).limit(1)).first()
    historia = conn.execute(select(jobs.c.id).limit(1)).first() is not None
    return (fila.ambiente if fila else None), historia


def _sellar_en(conn, ambiente, por, motivo):
    from app.models.sello_ambiente import SelloAmbiente
    conn.execute(SelloAmbiente.__table__.insert().values(
        ambiente=ambiente, sellado_en=datetime.utcnow(), sellado_por=por, motivo=motivo))


def estado() -> dict:
    """Lo que ven `/api/health/siesa` y el panel de recuperación. **Solo
    lee**: no sella."""
    proceso = ambiente_del_proceso()
    try:
        from app.extensions import db
        sello = _leer()
        sello_d = sello.to_dict() if sello else None
        from app.models.siesa_job import SiesaJob
        historia = db.session.query(SiesaJob.id).first() is not None
        error = None
    except Exception as e:     # tabla ausente, base caída
        sello_d, historia, error = None, False, str(e)[:200]
    base = {'proceso': proceso, 'sello': sello_d, 'hay_historia': historia,
            'puede_resellar': proceso is not None}
    if proceso is None:
        return {**base, 'coincide': None, 'bloquea': False,
                'texto': (f'Este proceso no tiene {VAR_AMBIENTE}: no se verifica a qué '
                          'ambiente pertenece la base (local o tests).')}
    if error:
        return {**base, 'coincide': None, 'bloquea': True,
                'texto': f'No se pudo leer el sello de la base ({error}): no se postea.'}
    postea, sellar, motivo = veredicto(proceso, sello_d['ambiente'] if sello_d else None,
                                       historia)
    if sello_d is None:
        return {**base, 'coincide': None, 'bloquea': not postea,
                'texto': motivo or ('La base todavía no tiene sello y no tiene historia: '
                                    f'el primer envío a Siesa la sellará «{proceso}».')}
    return {**base, 'coincide': postea, 'bloquea': not postea,
            'texto': (f'Base sellada «{sello_d["ambiente"]}», proceso «{proceso}».'
                      + ('' if postea else
                         ' NO COINCIDEN: ni la DLQ ni ningún POST salen a Siesa. '
                         'Si esta base es una copia que se adoptó a propósito, '
                         're-séllela (admin: Siesa → Recuperación).'))}


def puede_postear(usar_cache: bool = True) -> tuple:
    """`(True, '')` o `(False, motivo)`. Sella la base si no tiene sello, no
    tiene historia y el proceso declara su ambiente (el primer uso)."""
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
        with db.engine.begin() as conn:
            sello, historia = _leer_en(conn)
            postea, sellar, motivo = veredicto(proceso, sello, historia)
            if sellar:
                _sellar_en(conn, proceso, 'sistema',
                           'primer envío a Siesa sobre una base sin historia')
                logger.warning('[SELLO] Base sellada «%s» (primer uso, sin historia).', proceso)
        return postea, motivo
    except Exception as e:
        return False, f'No se pudo leer el sello de la base ({str(e)[:200]}): no se postea.'


def sellar_en_arranque(app=None) -> str:
    """P1-5 a (2026-09-26): **producción se sella explícitamente al arrancar**,
    no esperando al primer envío de la DLQ. Así toda copia que se tome de
    producción —desde su primer arranque con esta versión— lleva el sello
    `production`, y un QA que la restaure no postea sus jobs.

    Solo el proceso de producción, solo si la base no tiene sello, y nunca con
    el candado local puesto. Nunca levanta (el arranque no depende de esto):
    devuelve qué hizo. Conexión propia."""
    proceso = ambiente_del_proceso()
    if not es_produccion(proceso):
        return 'no es producción'
    if app is not None and app.config.get('CANDADO_PRODUCCION_LOCAL'):
        return 'candado local'
    try:
        from app.extensions import db
        with db.engine.begin() as conn:
            sello, _historia = _leer_en(conn)
            if sello is not None:
                return f'ya sellada «{sello}»'
            _sellar_en(conn, proceso, 'arranque',
                       'producción se sella al arrancar (P1-5, 2026-09-26)')
        logger.warning('[SELLO] Base de producción sellada al arrancar.')
        _olvidar_cache()
        return 'sellada'
    except Exception as e:     # tabla aún sin migrar, base caída: el primer envío lo intenta
        logger.warning('[SELLO] No se pudo sellar al arrancar: %s', e)
        return f'error: {str(e)[:120]}'


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
