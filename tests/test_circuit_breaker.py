"""
El circuit breaker se RECUPERA. Ese es su único propósito.

Un breaker que abre y no vuelve a cerrar es peor que no tener breaker: convierte
una caída temporal de Siesa en una caída permanente del gateway, hasta que
alguien reinicie el proceso.

Auditoría del 2026-08-04, disparada por un hallazgo del review. Había **dos
trabas independientes**, y cualquiera de las dos sola bastaba para dejar la
integración muerta:

  1. **`_get()` pedía permiso dos veces.** El método transiciona OPEN →
     HALF_OPEN y gasta el único probe permitido; la primera llamada lo gastaba y
     devolvía True, la segunda veía HALF_OPEN y devolvía False. **La llamada
     HTTP nunca salía.** Un comentario en el código decía "redundante para
     claridad" — el nombre `_cb_should_allow` sonaba a pregunta y era una orden.

  2. **Un probe que fallaba no volvía a OPEN.** `_cb_record_failure` solo
     transicionaba `if state == 'CLOSED'`. Desde HALF_OPEN no hacía nada, y en
     HALF_OPEN todo se niega. Este es el CAMINO NORMAL de un breaker —abre,
     prueba, Siesa sigue caída— y era el que lo trababa.

El review encontró la primera. La segunda salió de recorrer la máquina de
estados entera preguntando "¿desde acá se sale?".
"""
import time
from unittest.mock import patch

import pytest

from app.services.connekta_gateway import ConnektaGateway


@pytest.fixture
def gw():
    g = ConnektaGateway()
    g.modo_simulacion = False          # el breaker solo actúa fuera de simulación
    g._cb_state = 'CLOSED'
    g._cb_failures = []
    g._cb_opened_at = None
    g._cb_last_probe = time.monotonic() - 10_000   # probe disponible ya
    return g


def _abrir(gw):
    """Lleva el breaker a OPEN por la puerta normal: fallos consecutivos."""
    for _ in range(gw._CB_FAILURE_THRESHOLD):
        gw._cb_record_failure()
    assert gw._cb_state == 'OPEN'


class TestLaMaquinaDeEstadosSale:
    """De todo estado se sale. Esa es la propiedad."""

    def test_de_closed_se_permite(self, gw):
        assert gw._cb_consumir_permiso() is True
        assert gw._cb_state == 'CLOSED'

    def test_los_fallos_abren(self, gw):
        _abrir(gw)
        assert gw._cb_opened_at is not None

    def test_open_niega_hasta_que_toque_el_probe(self, gw):
        _abrir(gw)
        gw._cb_last_probe = time.monotonic()      # recién probado
        assert gw._cb_consumir_permiso() is False
        assert gw._cb_state == 'OPEN'

    def test_pasado_el_intervalo_sale_un_probe(self, gw):
        _abrir(gw)
        gw._cb_last_probe = time.monotonic() - gw._CB_PROBE_INTERVAL - 1
        assert gw._cb_consumir_permiso() is True
        assert gw._cb_state == 'HALF_OPEN'

    def test_el_probe_exitoso_cierra(self, gw):
        _abrir(gw)
        gw._cb_last_probe = time.monotonic() - gw._CB_PROBE_INTERVAL - 1
        gw._cb_consumir_permiso()
        gw._cb_record_success()
        assert gw._cb_state == 'CLOSED'
        assert gw._cb_failures == []

    def test_EL_PROBE_FALLIDO_VUELVE_A_OPEN(self, gw):
        """LA traba. Sin esto el estado se queda en HALF_OPEN, donde todo se
        niega, y el breaker no vuelve a intentar jamás.

        Es el camino normal: Siesa caída, el probe sale, Siesa sigue caída.
        """
        _abrir(gw)
        gw._cb_last_probe = time.monotonic() - gw._CB_PROBE_INTERVAL - 1
        gw._cb_consumir_permiso()
        assert gw._cb_state == 'HALF_OPEN'

        gw._cb_record_failure()
        assert gw._cb_state == 'OPEN', (
            'el probe fallido dejó el breaker en HALF_OPEN: la caída de Siesa '
            'se volvió una caída permanente del gateway')

    def test_y_despues_del_segundo_intervalo_vuelve_a_probar(self, gw):
        """La consecuencia de lo anterior: sigue reintentando indefinidamente."""
        _abrir(gw)
        for _ in range(3):
            gw._cb_last_probe = time.monotonic() - gw._CB_PROBE_INTERVAL - 1
            assert gw._cb_consumir_permiso() is True, 'dejó de reintentar'
            gw._cb_record_failure()
            assert gw._cb_state == 'OPEN'

    def test_half_open_deja_pasar_uno_solo(self, gw):
        """Diez workers no pueden mandar diez probes a un Siesa que se cae."""
        _abrir(gw)
        gw._cb_last_probe = time.monotonic() - gw._CB_PROBE_INTERVAL - 1
        assert gw._cb_consumir_permiso() is True
        assert gw._cb_consumir_permiso() is False
        assert gw._cb_consumir_permiso() is False


class TestElProbeLlegaAHacerLaLlamada:
    """La otra traba: el permiso se gastaba y el HTTP nunca salía."""

    def test_get_pide_permiso_una_sola_vez(self, gw):
        with patch.object(gw, '_cb_consumir_permiso', return_value=True) as permiso, \
             patch('requests.get') as req:
            req.return_value.status_code = 200
            req.return_value.json.return_value = {'Table': []}
            gw._get('API_v2_Items')
        assert permiso.call_count == 1, (
            f'pidió permiso {permiso.call_count} veces: cada llamada de más '
            f'gasta un probe y traba el breaker en HALF_OPEN')

    def test_con_el_probe_disponible_el_get_SALE_A_LA_RED(self, gw):
        """El síntoma exacto que se arregló: probe consumido, HTTP nunca hecho."""
        _abrir(gw)
        gw._cb_last_probe = time.monotonic() - gw._CB_PROBE_INTERVAL - 1
        with patch('requests.get') as req:
            req.return_value.status_code = 200
            req.return_value.json.return_value = {'Table': []}
            gw._get('API_v2_Items')
            assert req.called, (
                'el probe se consumió sin llegar a hacer la llamada HTTP — '
                'el breaker queda en HALF_OPEN para siempre')

    def test_y_ese_get_exitoso_cierra_el_circuito(self, gw):
        """De punta a punta: caída → probe → Siesa vuelve → gateway operativo."""
        _abrir(gw)
        gw._cb_last_probe = time.monotonic() - gw._CB_PROBE_INTERVAL - 1
        with patch('requests.get') as req:
            req.return_value.status_code = 200
            req.return_value.json.return_value = {'Table': []}
            gw._get('API_v2_Items')
        assert gw._cb_state == 'CLOSED', (
            'Siesa volvió y el gateway sigue bloqueado')

    def test_en_simulacion_no_se_gasta_el_probe(self, gw):
        """Simular no toca la red: pedir permiso ahí gastaría el probe sin
        averiguar nada sobre el estado real de Siesa."""
        gw.modo_simulacion = True
        _abrir(gw)
        antes = gw._cb_state
        with patch.object(gw, '_cb_consumir_permiso') as permiso:
            gw._get('API_v2_Items')
        assert permiso.call_count == 0
        assert gw._cb_state == antes


class TestElNombreNoMiente:
    """TRINQUETE — el bug nació del nombre.

    `_cb_should_allow` se lee como una consulta pura. Con ese nombre, llamarla
    dos veces parece inofensivo, y el propio código lo comentaba como
    "redundante para claridad". Era una orden disfrazada de pregunta.
    """

    def test_el_metodo_declara_que_consume(self):
        from pathlib import Path

        fuente = (Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'connekta_gateway.py').read_text(encoding='utf-8')
        assert 'def _cb_should_allow' not in fuente, (
            'volvió el nombre de pregunta para un método que muta estado')
        assert 'def _cb_consumir_permiso' in fuente

    def test_no_se_llama_mas_de_una_vez_por_camino(self):
        """Dos llamadas en la misma función son, por construcción, un probe
        gastado de más."""
        import ast
        from pathlib import Path

        fuente = (Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'connekta_gateway.py').read_text(encoding='utf-8')
        arbol = ast.parse(fuente)
        malas = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            n = sum(1 for x in ast.walk(nodo)
                    if isinstance(x, ast.Call)
                    and getattr(x.func, 'attr', '') == '_cb_consumir_permiso')
            if n > 1:
                malas.append(f'{nodo.name} ({n} veces)')
        assert not malas, (
            '\nPiden permiso al breaker más de una vez — cada llamada de más '
            'gasta un probe:\n' + '\n'.join(f'  · {m}' for m in malas))
