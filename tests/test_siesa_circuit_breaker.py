"""
Tests del Circuit Breaker de ConnektaGateway.
Valida: trip, recovery, fail-fast, DLQ pausa, ventana deslizante.
"""
import os
import time
import pytest

os.environ.setdefault('CONNEKTA_MODO_SIMULACION', 'true')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('SYNC_SCHEDULER', 'false')

from app.services.connekta_gateway import ConnektaGateway, ConnektaCircuitOpenError


def _make_cb():
    """Gateway con circuit breaker configurado para tests rápidos."""
    gw = ConnektaGateway()
    gw.modo_simulacion = False
    gw.modo_ensayo = False
    gw.ikey = 'test-key'            # evita lazy-loading que revierte modo_simulacion
    gw.itoken = 'test-token'
    gw._CB_FAILURE_THRESHOLD = 3    # trip con 3 fallos (rápido para tests)
    gw._CB_WINDOW_SECONDS = 10      # ventana de 10s
    gw._CB_PROBE_INTERVAL = 1       # probe cada 1s
    return gw


class TestCircuitBreakerTrip:

    def test_closed_initially(self):
        gw = _make_cb()
        assert gw._cb_state == 'CLOSED'
        assert gw.circuit_state()['state'] == 'CLOSED'

    def test_trip_after_threshold(self):
        gw = _make_cb()
        for _ in range(3):
            gw._cb_record_failure()
        assert gw._cb_state == 'OPEN'

    def test_no_trip_below_threshold(self):
        gw = _make_cb()
        for _ in range(2):
            gw._cb_record_failure()
        assert gw._cb_state == 'CLOSED'

    def test_failures_outside_window_dont_count(self):
        gw = _make_cb()
        gw._CB_WINDOW_SECONDS = 0.1  # 100ms window
        gw._cb_record_failure()
        gw._cb_record_failure()
        time.sleep(0.15)  # esperar que salgan de la ventana
        gw._cb_record_failure()  # solo 1 fallo en ventana
        assert gw._cb_state == 'CLOSED'

    def test_success_resets_failures(self):
        gw = _make_cb()
        gw._cb_record_failure()
        gw._cb_record_failure()
        gw._cb_record_success()
        gw._cb_record_failure()
        # Solo 1 fallo después del reset
        assert gw._cb_state == 'CLOSED'


class TestCircuitBreakerFailFast:

    def test_open_blocks_calls(self):
        gw = _make_cb()
        gw._CB_PROBE_INTERVAL = 600  # no probe durante test
        for _ in range(3):
            gw._cb_record_failure()
        assert gw._cb_state == 'OPEN'
        assert gw._cb_consumir_permiso() is False

    def test_open_get_returns_none(self):
        """_get() returns None when circuit is OPEN."""
        gw = _make_cb()
        gw._CB_PROBE_INTERVAL = 600
        for _ in range(3):
            gw._cb_record_failure()
        result = gw._get('API_test')
        assert result is None

    def test_open_post_raises_circuit_error(self):
        """_post() raises ConnektaCircuitOpenError when circuit is OPEN."""
        gw = _make_cb()
        gw._CB_PROBE_INTERVAL = 600
        for _ in range(3):
            gw._cb_record_failure()
        with pytest.raises(ConnektaCircuitOpenError):
            gw._post('142888', 'test', {})


class TestCircuitBreakerRecovery:

    def test_open_transitions_to_half_open(self):
        gw = _make_cb()
        gw._CB_PROBE_INTERVAL = 0.1  # 100ms
        for _ in range(3):
            gw._cb_record_failure()
        assert gw._cb_state == 'OPEN'

        time.sleep(0.15)
        allowed = gw._cb_consumir_permiso()
        assert allowed is True
        assert gw._cb_state == 'HALF_OPEN'

    def test_half_open_success_closes(self):
        gw = _make_cb()
        gw._CB_PROBE_INTERVAL = 0.1
        for _ in range(3):
            gw._cb_record_failure()
        time.sleep(0.15)
        gw._cb_consumir_permiso()  # transition to HALF_OPEN
        gw._cb_record_success()
        assert gw._cb_state == 'CLOSED'

    def test_half_open_failure_reopens(self):
        """Un probe que falla vuelve a OPEN.

        ESTE TEST ESTUVO FALSEADO hasta el 2026-08-04. Hacía:

            gw._cb_record_failure()
            # El state machine: HALF_OPEN + failure no cambia a OPEN
            # directamente ...
            # Forzamos para el test:
            gw._cb_state = 'OPEN'   # manual for clarity
            assert gw._cb_state == 'OPEN'

        Asignaba el estado a mano y después lo afirmaba: no verificaba una sola
        línea del código. Y el comentario NOMBRABA EL BUG — el autor lo vio, lo
        escribió, y forzó el valor para que el test pasara.

        El breaker no reabría, así que una caída de Siesa lo dejaba trabado en
        HALF_OPEN —donde todo se niega— hasta reiniciar el proceso. Meses en
        verde bajo un test que se llama exactamente como la propiedad rota.
        """
        gw = _make_cb()
        gw._CB_PROBE_INTERVAL = 0.1
        for _ in range(3):
            gw._cb_record_failure()
        time.sleep(0.15)
        assert gw._cb_consumir_permiso() is True
        assert gw._cb_state == 'HALF_OPEN'

        gw._cb_record_failure()
        assert gw._cb_state == 'OPEN', (
            'el probe fallido dejó el breaker trabado en HALF_OPEN')

    def test_half_open_blocks_concurrent(self):
        """Solo una llamada permitida en HALF_OPEN, las demás bloqueadas."""
        gw = _make_cb()
        gw._CB_PROBE_INTERVAL = 0.1
        for _ in range(3):
            gw._cb_record_failure()
        time.sleep(0.15)
        assert gw._cb_consumir_permiso() is True   # primera: HALF_OPEN, permitida
        assert gw._cb_consumir_permiso() is False   # segunda: HALF_OPEN, bloqueada


class TestCircuitBreakerState:

    def test_circuit_state_dict(self):
        gw = _make_cb()
        state = gw.circuit_state()
        assert 'state' in state
        assert 'failures_recent' in state
        assert 'failure_threshold' in state
        assert state['state'] == 'CLOSED'
        assert state['failures_recent'] == 0

    def test_circuit_state_when_open(self):
        gw = _make_cb()
        for _ in range(3):
            gw._cb_record_failure()
        state = gw.circuit_state()
        assert state['state'] == 'OPEN'
        assert state['failures_recent'] >= 3
        assert state['opened_at'] is not None


class TestCircuitBreakerException:

    def test_exception_type(self):
        err = ConnektaCircuitOpenError('test')
        assert isinstance(err, Exception)
        assert 'test' in str(err)

    def test_distinguishable_from_regular_exception(self):
        try:
            raise ConnektaCircuitOpenError('circuit open')
        except ConnektaCircuitOpenError:
            pass  # expected
        except Exception:
            pytest.fail('ConnektaCircuitOpenError should be catchable specifically')
