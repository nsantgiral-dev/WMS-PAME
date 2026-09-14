"""
Tests directos de ConnektaCircuitBreaker (app/services/connekta_circuit_breaker.py),
extraído de ConnektaGateway el 2026-09-09. No requiere Flask ni DB — es la
máquina de estados en aislamiento, sin las propiedades de compatibilidad que
sí prueba tests/test_circuit_breaker.py y tests/test_siesa_circuit_breaker.py
contra ConnektaGateway.
"""
import time

from app.services.connekta_circuit_breaker import ConnektaCircuitBreaker


class TestTransicionesBasicas:
    def test_arranca_cerrado(self):
        cb = ConnektaCircuitBreaker()
        assert cb.state == 'CLOSED'
        assert cb.consumir_permiso() is True

    def test_abre_al_llegar_al_umbral(self):
        cb = ConnektaCircuitBreaker(failure_threshold=3, window_seconds=60, probe_interval=1)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == 'OPEN'
        assert cb.consumir_permiso() is False

    def test_fallos_fuera_de_ventana_no_cuentan(self):
        cb = ConnektaCircuitBreaker(failure_threshold=2, window_seconds=0.05, probe_interval=1)
        cb.record_failure()
        time.sleep(0.1)
        cb.record_failure()
        assert cb.state == 'CLOSED', 'el primer fallo ya venció, no debería sumar'

    def test_probe_exitoso_cierra_el_circuit(self):
        cb = ConnektaCircuitBreaker(failure_threshold=1, window_seconds=60, probe_interval=0.05)
        cb.record_failure()
        assert cb.state == 'OPEN'
        time.sleep(0.1)
        assert cb.consumir_permiso() is True, 'ya pasó el intervalo de probe'
        assert cb.state == 'HALF_OPEN'
        cb.record_success()
        assert cb.state == 'CLOSED'
        assert cb.failures == []

    def test_probe_fallido_vuelve_a_abrir(self):
        """El caso que motivó la extracción: un probe que falla NO debe dejar
        el breaker atascado en HALF_OPEN para siempre."""
        cb = ConnektaCircuitBreaker(failure_threshold=1, window_seconds=60, probe_interval=0.05)
        cb.record_failure()
        time.sleep(0.1)
        cb.consumir_permiso()  # transición a HALF_OPEN, gasta el probe
        assert cb.state == 'HALF_OPEN'
        cb.record_failure()
        assert cb.state == 'OPEN', 'un probe fallido debe reabrir, no quedar atascado'

    def test_consumir_permiso_en_half_open_solo_una_vez(self):
        cb = ConnektaCircuitBreaker(failure_threshold=1, window_seconds=60, probe_interval=0.05)
        cb.record_failure()
        time.sleep(0.1)
        assert cb.consumir_permiso() is True    # gasta el único probe
        assert cb.consumir_permiso() is False   # ya no hay más permisos en HALF_OPEN


class TestOnTripCallback:
    def test_se_llama_solo_al_transicionar_a_open(self):
        llamadas = []
        cb = ConnektaCircuitBreaker(failure_threshold=2, window_seconds=60, probe_interval=1)
        cb.record_failure(on_trip=lambda: llamadas.append(1))
        assert llamadas == [], 'todavía no llega al umbral'
        cb.record_failure(on_trip=lambda: llamadas.append(1))
        assert llamadas == [1], 'debe dispararse exactamente al abrir'
        cb.record_failure(on_trip=lambda: llamadas.append(1))
        assert llamadas == [1], 'ya estaba OPEN, no debe repetirse'

    def test_sin_callback_no_revienta(self):
        cb = ConnektaCircuitBreaker(failure_threshold=1, window_seconds=60, probe_interval=1)
        cb.record_failure()  # on_trip=None por defecto


class TestSnapshot:
    def test_snapshot_refleja_el_estado(self):
        cb = ConnektaCircuitBreaker(failure_threshold=5, window_seconds=300, probe_interval=60)
        s = cb.snapshot()
        assert s == {
            'state': 'CLOSED', 'failures_recent': 0,
            'failure_threshold': 5, 'opened_at': None,
        }
        cb.record_failure()
        assert cb.snapshot()['failures_recent'] == 1

    def test_snapshot_cuando_abierto_trae_opened_at(self):
        cb = ConnektaCircuitBreaker(failure_threshold=1, window_seconds=60, probe_interval=60)
        cb.record_failure()
        s = cb.snapshot()
        assert s['state'] == 'OPEN'
        assert s['opened_at'] is not None
