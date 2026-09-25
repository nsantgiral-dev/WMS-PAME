"""
Cuándo se le pregunta (y se le postea) a Siesa — UNA ventana, **configurable**
(tanda 2 · H, decisión del dueño 2026-09-25).

**La ventana no es una regla de producción.** «Siesa no opera después de
~8 p. m.» (Regla 14) se midió en **Siesa QA**; en temporada la operación
trabaja hasta las 12 a. m. y la facturación no puede quedar bloqueada por
reloj. Lo que protege de verdad cuando Siesa no responde no es la hora: es el
circuit breaker (`connekta_circuit_breaker`), el precheck del cierre de caja
(`documento_fiscal.siesa_disponible_para_facturar`) y la jerarquía «no sé ≠
no» del POST.

- **`SIESA_VENTANA`** = `"HH:MM-HH:MM"` (hora de Bogotá). Sugerida para QA:
  `06:00-19:30` (lo medido allá). Producción: **sin la variable**.
- **Sin la variable: sin restricción (24 h).**
- **Ilegible: sin restricción, y declarado** (`estado()` →
  `/api/health/siesa` → `ventana_siesa.problema`). No se adivina un horario
  que frene la facturación.
- Una ventana que cruza la medianoche (`18:00-01:00`) se entiende así.

Quién pregunta (siempre por acá, nunca con una hora propia):

- `ventana_abierta(momento)` — la pregunta, una función. Los crons, la DLQ
  (`siesa_job_service.dlq_puede_postear`, que solo agrega «en simulación no
  aplica»), el cierre de caja y la emisión fiscal, la vista previa de la
  liquidación.
- `solo_en_ventana_siesa(fn)` — envuelve un job de APScheduler que habla con
  Siesa: con la ventana configurada y fuera de ella, no corre y lo dice.
- La DLQ, con la ventana configurada y fuera de ella, solo procesa los tipos
  que no van a Siesa (`TIPOS_SIN_SIESA`). Sin ventana, un POST que falla de
  noche sigue el backoff normal y la jerarquía de excepciones.

Trinquete: `tests/test_ventana_siesa.py` — todo `add_job` está clasificado,
nadie más declara una ventana ni lee `SIESA_VENTANA`.
"""
import functools
import logging
import os
from datetime import time

from app.utils.fecha import TZ_BOGOTA, ahora_bogota

logger = logging.getLogger(__name__)

#: La variable. Sin ella, Siesa se consulta a cualquier hora.
VAR_VENTANA = 'SIESA_VENTANA'

#: Lo medido en Siesa QA (Regla 14): la sugerida para ese ambiente.
VENTANA_SUGERIDA_QA = '06:00-19:30'

#: Tipos de `siesa_jobs` que no hablan con Siesa: el DLQ los procesa a
#: cualquier hora (un correo de alerta no espera a mañana).
TIPOS_SIN_SIESA = ('ALERTA_EMAIL',)


#: Solo para tests: fija la respuesta cuando NO se pasa `momento` (el reloj
#: real). La suite corre a cualquier hora —Railway en UTC, de noche en
#: Bogotá— y un DLQ que no procesa de noche rompería el build cinco horas al
#: día (la lección de `hoy_operativo`). Un `momento` explícito siempre manda.
_RELOJ_FIJO = {'abierta': None}


def _hhmm(s: str) -> time:
    h, m = s.strip().split(':')
    h, m = int(h), int(m)
    return time(h, m)


def leer(crudo) -> tuple:
    """`(ventana | None, problema | None)` de un valor de `SIESA_VENTANA`.
    Vacío → `(None, None)`: sin restricción. Ilegible → `(None, problema)`:
    sin restricción y declarado."""
    crudo = (crudo or '').strip()
    if not crudo:
        return None, None
    try:
        a, b = crudo.split('-')
        ini, fin = _hhmm(a), _hhmm(b)
    except (ValueError, TypeError) as e:
        return None, (f'{VAR_VENTANA}={crudo!r} no se entiende ({e}; se espera '
                      f'"HH:MM-HH:MM"): Siesa se consulta sin restricción de horario')
    if ini == fin:
        return None, (f'{VAR_VENTANA}={crudo!r}: inicio igual al fin — Siesa se consulta '
                      f'sin restricción de horario')
    return (ini, fin), None


def ventana(env=None):
    """La ventana configurada `(inicio, fin)`, o `None` = sin restricción."""
    env = os.environ if env is None else env
    return leer(env.get(VAR_VENTANA))[0]


def estado(env=None) -> dict:
    """Lo que publica `/api/health/siesa`."""
    env = os.environ if env is None else env
    crudo = (env.get(VAR_VENTANA) or '').strip()
    v, problema = leer(crudo)
    return {'variable': VAR_VENTANA, 'valor': crudo or None,
            'configurada': v is not None, 'sin_restriccion': v is None,
            'ventana': texto_de(v), 'problema': problema,
            'sugerida_qa': VENTANA_SUGERIDA_QA}


def _dentro(v, t: time) -> bool:
    ini, fin = v
    if ini < fin:
        return ini <= t <= fin
    return t >= ini or t <= fin          # cruza la medianoche


def ventana_abierta(momento=None) -> bool:
    """¿Se le puede hablar a Siesa ahora? Sin ventana configurada, siempre.
    `momento`: naive en hora de Bogotá, o consciente de zona (se juzga en
    Bogotá: 01:30 UTC son las 20:30 del día anterior allá)."""
    if momento is None and _RELOJ_FIJO['abierta'] is not None:
        return _RELOJ_FIJO['abierta']
    v = ventana()
    if v is None:
        return True
    momento = momento or ahora_bogota()
    if momento.tzinfo is not None:
        momento = momento.astimezone(TZ_BOGOTA)
    return _dentro(v, momento.time())


def texto_de(v) -> str:
    if v is None:
        return 'sin restricción de horario'
    return f'{v[0].strftime("%H:%M")}–{v[1].strftime("%H:%M")} Bogotá'


def texto_ventana() -> str:
    return texto_de(ventana())


def solo_en_ventana_siesa(fn):
    """Envuelve un job que habla con Siesa: con la ventana configurada y fuera
    de ella, no corre."""
    @functools.wraps(fn)
    def _envuelto(*args, **kwargs):
        if not ventana_abierta():
            logger.info('[VENTANA_SIESA] %s omitido: fuera de %s',
                        getattr(fn, '__name__', 'job'), texto_ventana())
            return {'omitido': f'fuera de la ventana de Siesa ({texto_ventana()})'}
        return fn(*args, **kwargs)
    _envuelto._ventana_siesa = True
    return _envuelto
