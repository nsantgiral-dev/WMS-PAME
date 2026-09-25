"""
Cuándo se le pregunta (y se le postea) a Siesa — UNA ventana (P2, 2026-09-25).

Había cuatro: 7:00–20:00 (Salud), 6:00–20:00 (cartera), 7:00–19:30 (fotos,
barrido de cartera), 7:00–21:00 (pedidos, en la Salud). Y crons que iban a
Siesa a las 2:00, 2:30, 3:00 y 5:55, o las 24 horas: la sincronización de
empaques y la de ubicaciones fallaban de madrugada sin registro, y un recibo
de caja que el DLQ intentaba a las 20:30 amanecía FALLIDO (Regla 14: Siesa no
opera después de ~8 p. m., timeout de 30 s).

`VENTANA` es 06:00–19:30 Bogotá: desde el primer cron de la mañana hasta
media hora antes de que Siesa deje de responder (un POST tarda 30–60 s; una
foto, minutos). **Siesa caído o fuera de ventana = no se le habla** (decisión
del dueño 2026-09-25: sin contingencia).

- `ventana_abierta(momento)` — la pregunta, una función.
- `solo_en_ventana_siesa(fn)` — envuelve un job de APScheduler que habla con
  Siesa: fuera de ventana no corre y lo dice (`{'omitido': …}`).
- El DLQ, fuera de ventana, solo procesa los tipos que no van a Siesa
  (`TIPOS_SIN_SIESA`).

Trinquete: `tests/test_ventana_siesa.py` — todo `add_job` está clasificado
(habla con Siesa → envuelto; no habla → con su porqué) y nadie más declara
una ventana de Siesa.
"""
import functools
import logging
from datetime import time

from app.utils.fecha import ahora_bogota

logger = logging.getLogger(__name__)

#: 06:00–19:30 Bogotá. Inicio y fin incluidos.
VENTANA = (time(6, 0), time(19, 30))

#: Tipos de `siesa_jobs` que no hablan con Siesa: el DLQ los procesa a
#: cualquier hora (un correo de alerta no espera a mañana).
TIPOS_SIN_SIESA = ('ALERTA_EMAIL',)


#: Solo para tests: fija la respuesta cuando NO se pasa `momento` (el reloj
#: real). La suite corre a cualquier hora —Railway en UTC, de noche en
#: Bogotá— y un DLQ que no procesa de noche rompería el build cinco horas al
#: día (la lección de `hoy_operativo`). Un `momento` explícito siempre manda.
_RELOJ_FIJO = {'abierta': None}


def ventana_abierta(momento=None) -> bool:
    """¿Se le puede hablar a Siesa ahora? `momento` en hora de Bogotá."""
    if momento is None and _RELOJ_FIJO['abierta'] is not None:
        return _RELOJ_FIJO['abierta']
    momento = momento or ahora_bogota()
    return VENTANA[0] <= momento.time() <= VENTANA[1]


def texto_ventana() -> str:
    return f'{VENTANA[0].strftime("%H:%M")}–{VENTANA[1].strftime("%H:%M")} Bogotá'


def solo_en_ventana_siesa(fn):
    """Envuelve un job que habla con Siesa: fuera de la ventana no corre."""
    @functools.wraps(fn)
    def _envuelto(*args, **kwargs):
        if not ventana_abierta():
            logger.info('[VENTANA_SIESA] %s omitido: fuera de %s',
                        getattr(fn, '__name__', 'job'), texto_ventana())
            return {'omitido': f'fuera de la ventana de Siesa ({texto_ventana()})'}
        return fn(*args, **kwargs)
    _envuelto._ventana_siesa = True
    return _envuelto
