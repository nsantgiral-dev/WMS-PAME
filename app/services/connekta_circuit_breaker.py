"""
Circuit breaker de Connekta/Siesa — máquina de estados aislada del resto del
gateway (2026-09-09, extraída de `ConnektaGateway`, paso 2 de la deuda de
tamaño — ver `_fmt_valor`/`app/utils/siesa_formato.py` para el paso 1).

CLOSED (normal) → OPEN (N fallos en la ventana) → HALF_OPEN (un probe) → CLOSED.

`ConnektaGateway` conserva las mismas propiedades (`_cb_state`,
`_cb_failures`, `_CB_FAILURE_THRESHOLD`, etc.) y los mismos métodos
(`_cb_record_failure`, `_cb_record_success`, `_cb_consumir_permiso`,
`circuit_state`) delegando a una instancia de esta clase — ningún caller
externo cambia, incluido `siesa_job_service.py` (lee `connekta._cb_state`
directo) y los tests que mutan el estado a mano para forzar escenarios
(`gw._cb_state = 'OPEN'`, `gw._CB_PROBE_INTERVAL = 0.1`, etc.).

La alerta por correo cuando el circuit abre (`_cb_trip_alert`) se queda en
`ConnektaGateway` — es lógica de negocio (crea un `SiesaJob` de tipo
`ALERTA_EMAIL`), no parte genérica del circuit breaker. Se conecta acá vía
el parámetro `on_trip` de `record_failure`.
"""
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_TZ_BOGOTA = ZoneInfo('America/Bogota')


class ConnektaCircuitBreaker:

    def __init__(self, failure_threshold: int = 5, window_seconds: int = 300,
                 probe_interval: int = 60):
        self.lock = threading.Lock()
        self.state = 'CLOSED'                 # CLOSED | OPEN | HALF_OPEN
        self.failures = []                    # timestamps de fallos recientes
        self.opened_at = None                 # cuándo se abrió el circuit
        self.last_probe = time.monotonic()    # monotonic timestamp del último probe
        self.failure_threshold = failure_threshold
        self.window_seconds = window_seconds
        self.probe_interval = probe_interval

    def record_failure(self, on_trip=None):
        """Registra un fallo. Si alcanza el threshold, trip a OPEN.

        `on_trip` se llama TODAVÍA DENTRO del lock, igual que antes de la
        extracción — no es un descuido, es preservar el orden exacto que ya
        tenía `ConnektaGateway._cb_record_failure`.
        """
        now = time.monotonic()
        with self.lock:
            self.failures.append(now)
            # Limpiar fallos fuera de la ventana
            cutoff = now - self.window_seconds
            self.failures = [t for t in self.failures if t > cutoff]

            # Un probe que falla vuelve a OPEN. Sin esto el estado se quedaba
            # en HALF_OPEN —donde TODO se niega— y el breaker no volvía a
            # intentar nunca: la caída de Siesa se convertía en una caída
            # permanente del gateway hasta reiniciar el proceso.
            #
            # Es el camino NORMAL de un circuit breaker: abre, prueba, sigue
            # caído. Que ese camino lo trabara volvía inútil todo el mecanismo.
            if self.state == 'HALF_OPEN':
                self.state = 'OPEN'
                logger.warning(
                    '[CONNEKTA CB] probe falló — vuelve a OPEN, reintento en %ds',
                    self.probe_interval)
                return

            if len(self.failures) >= self.failure_threshold and self.state == 'CLOSED':
                self.state = 'OPEN'
                self.opened_at = datetime.now(_TZ_BOGOTA).isoformat()
                logger.critical(
                    '[CONNEKTA CB] CIRCUIT OPEN — %d fallos en %ds. '
                    'Siesa no disponible. DLQ pausado. Probe cada %ds.',
                    len(self.failures), self.window_seconds, self.probe_interval
                )
                if on_trip:
                    on_trip()

    def record_success(self):
        """Registra un éxito. Si estamos en HALF_OPEN, cierra el circuit."""
        with self.lock:
            if self.state == 'HALF_OPEN':
                self.state = 'CLOSED'
                self.failures.clear()
                self.opened_at = None
                logger.info('[CONNEKTA CB] CIRCUIT CLOSED — Siesa recuperado. DLQ reanudado.')
            elif self.state == 'CLOSED':
                # Éxito en operación normal — limpiar fallos acumulados
                self.failures.clear()

    def consumir_permiso(self) -> bool:
        """Pide permiso para UNA llamada HTTP. **Consume estado.**

        Se llamaba `_cb_should_allow`: un nombre de pregunta para un método que
        MUTA — transiciona OPEN → HALF_OPEN y gasta el único probe permitido.
        Con ese nombre, `_get()` la llamaba dos veces y un comentario decía
        "redundante para claridad".

        No era redundante: la primera llamada gastaba el probe y devolvía True,
        la segunda veía HALF_OPEN y devolvía False. **La llamada HTTP nunca
        salía**, y el breaker quedaba en HALF_OPEN para siempre.

        Se llama EXACTAMENTE UNA VEZ por intento.
        """
        with self.lock:
            if self.state == 'CLOSED':
                return True
            if self.state == 'OPEN':
                # ¿Ya pasó el intervalo de probe?
                now = time.monotonic()
                if now - self.last_probe >= self.probe_interval:
                    self.state = 'HALF_OPEN'
                    self.last_probe = now
                    logger.info('[CONNEKTA CB] HALF_OPEN — enviando probe a Siesa')
                    return True
                return False
            # HALF_OPEN — ya se permitió una llamada, bloquear las demás
            return False

    def snapshot(self) -> dict:
        """Estado actual del circuit breaker para health check y dashboard."""
        with self.lock:
            now = time.monotonic()
            cutoff = now - self.window_seconds
            recent = len([t for t in self.failures if t > cutoff])
            return {
                'state': self.state,
                'failures_recent': recent,
                'failure_threshold': self.failure_threshold,
                'opened_at': self.opened_at,
            }


__all__ = ['ConnektaCircuitBreaker']
