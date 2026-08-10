"""
En qué ambiente estamos: **una función**, no cinco.

El 2026-08-10, en la pantalla `Admin → Siesa`, se veía a la vez:

  · arriba, banner rojo: «DATOS DE PRUEBA — Siesa apunta al ambiente QA»
  · al centro, en verde y 28px: «PRODUCCIÓN · Listo para operar»

Los dos salían del mismo backend. El banner miraba el host de Connekta; el
panel miraba solo si había credenciales. Había **cinco** cálculos de lo mismo:
`health/ping`, `health/siesa` (dos veces: `modo` y `siesa_destino`),
`flota.medicion.ambiente()` y `app.js::cargarConnekta()`. Tres miraban el host,
dos no — y una de las que no, pintaba el verde grande.

**El destino manda sobre el modo.** Tener credenciales reales y POSTs
habilitados no es producción si los documentos aterrizan en el Siesa de
pruebas. Un «PRODUCCIÓN» en verde sobre QA es evidencia falsa de la peor clase:
la que se mira de reojo para decidir si ya se puede operar.
"""
import re
from pathlib import Path

import pytest

from app.services.connekta_gateway import connekta

_RAIZ = Path(__file__).resolve().parents[1]


@pytest.fixture
def qa(monkeypatch):
    monkeypatch.setattr(connekta, 'url_get_dinamico',
                        'https://serviciosqa.siesacloud.com/api/x', raising=False)


@pytest.fixture
def real(monkeypatch):
    monkeypatch.setattr(connekta, 'url_get_dinamico',
                        'https://servicios.siesacloud.com/api/x', raising=False)


class TestElDestinoMandaSobreElModo:

    def test_credenciales_reales_contra_QA_NO_es_produccion(self, qa, monkeypatch):
        """El caso exacto de la captura: todo verde, apuntando a pruebas."""
        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'modo_ensayo', False, raising=False)
        monkeypatch.delenv('WMS_ENSAYO', raising=False)
        assert connekta.modo_datos() == 'datos_de_prueba'

    def test_contra_el_host_real_si_es_produccion(self, real, monkeypatch):
        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'modo_ensayo', False, raising=False)
        monkeypatch.delenv('WMS_ENSAYO', raising=False)
        assert connekta.modo_datos() == 'produccion'

    @pytest.mark.parametrize('host', [
        'https://serviciosqa.siesacloud.com',
        'https://test.siesacloud.com',
        'https://dev.siesacloud.com',
        'https://wspapeleriamedpru.siesacloud.com:8043',
    ])
    def test_reconoce_las_formas_de_nombrar_pruebas(self, monkeypatch, host):
        monkeypatch.setattr(connekta, 'url_get_dinamico', host, raising=False)
        assert connekta.apunta_a_pruebas is True

    def test_WMS_ENSAYO_gana_sobre_produccion(self, real, monkeypatch):
        """Credenciales reales y datos ficticios a la vez — un ensayo con
        vestuario. Sin esto el banner se apaga justo cuando más se necesita."""
        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'modo_ensayo', False, raising=False)
        monkeypatch.setenv('WMS_ENSAYO', 'true')
        assert connekta.modo_datos() == 'ensayo'

    def test_sin_credenciales_es_simulacion(self, real, monkeypatch):
        monkeypatch.setattr(connekta, 'modo_simulacion', True, raising=False)
        monkeypatch.delenv('WMS_ENSAYO', raising=False)
        assert connekta.modo_datos() == 'simulacion'


class TestTodosLeenLaMisma:

    def test_estado_expone_modo_datos_y_el_host(self, qa):
        e = connekta.estado()
        assert e['modo_datos'] == 'datos_de_prueba'
        assert e['apunta_a_pruebas'] is True
        assert 'serviciosqa' in e['siesa_host']

    def test_flota_no_calcula_lo_suyo(self, qa):
        from flota.adaptadores.medicion import MedidorSQL
        assert MedidorSQL().ambiente() == 'datos_de_prueba'

    def test_flota_no_afirma_datos_reales_sobre_QA(self, qa):
        from flota.adaptadores.medicion import MedidorSQL
        assert MedidorSQL().datos_reales() is False


class TestNadieMasCalculaElAmbiente:
    """Anti-divergencia. El defecto no fue un bug: fue el mismo cálculo escrito
    cinco veces. Un fix aplicado a una copia reproduce el incidente."""

    #: El patrón que delata un cálculo propio: la tupla de sufijos de pruebas.
    _HUELLA = re.compile(r"'qa'\s*,\s*'test'\s*,\s*'dev'\s*,\s*'pru'")

    def test_solo_el_gateway_conoce_la_lista_de_hosts_de_prueba(self):
        culpables = []
        for py in list((_RAIZ / 'app').rglob('*.py')) + list((_RAIZ / 'flota').rglob('*.py')):
            if py.name == 'connekta_gateway.py':
                continue                     # la fuente única
            if self._HUELLA.search(py.read_text(encoding='utf-8')):
                culpables.append(str(py.relative_to(_RAIZ)))
        assert not culpables, (
            '\nVuelven a calcular el ambiente por su cuenta:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nUsar `connekta.modo_datos()` / `connekta.apunta_a_pruebas`.')

    def test_el_detector_no_esta_ciego(self):
        fuente = (_RAIZ / 'app' / 'services' / 'connekta_gateway.py').read_text(encoding='utf-8')
        assert self._HUELLA.search(fuente), (
            'el patrón ya no encuentra la lista ni en el gateway — dejó de medir')


class TestLaPantallaNoPintaVerdeSobrePruebas:
    """El defecto era visible, no teórico: 28px en verde diciendo PRODUCCIÓN."""

    _APP = _RAIZ / 'app' / 'static' / 'pwa' / 'app.js'

    def test_el_panel_decide_por_modo_datos(self):
        fuente = self._APP.read_text(encoding='utf-8')
        i = fuente.find('async function cargarConnekta()')
        assert i != -1
        bloque = fuente[i:i + 3000]
        assert "d.modo_datos === 'produccion'" in bloque, (
            'el verde de PRODUCCIÓN volvió a decidirse sin mirar el host')
        assert "d.modo_datos === 'datos_de_prueba'" in bloque

    def test_ante_campo_ausente_no_asume_produccion(self):
        """Backend viejo o campo faltante: un verde de más es peor que un «no
        sé». Regla 0 aplicada al banner."""
        fuente = self._APP.read_text(encoding='utf-8')
        i = fuente.find('async function cargarConnekta()')
        assert 'AMBIENTE DESCONOCIDO' in fuente[i:i + 3000]
