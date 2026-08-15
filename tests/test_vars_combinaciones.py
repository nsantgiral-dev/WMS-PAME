"""Dos variables que por separado están bien y juntas arman una bomba."""
import pytest
from app.services import vars_criticas as vc


def _nombres(monkeypatch, url, skip):
    monkeypatch.setenv('CONNEKTA_URL', url)
    monkeypatch.setenv('SKIP_FE_CHECK', skip)
    return {p['variable'] for p in vc._combinaciones_peligrosas()}


CLAVE = 'SKIP_FE_CHECK + CONNEKTA_URL'


class TestSkipFeCheckContraProduccion:
    """El guard anti-duplicado apagado contra Siesa real.

    `get_factura_desde_pedido` devuelve `[]` sin preguntar, y sus tres
    llamadores vivos leen eso como «no hay factura previa, seguí».

    Hoy es inofensivo porque `CONNEKTA_URL` apunta a QA. **El primer paso del
    go-live es moverla a producción**, y ahí el guard queda apagado sin que
    nadie lo note: dos variables que tienen que moverse juntas y nada las ataba.
    """

    def test_qa_con_skip_no_alarma(self, monkeypatch):
        assert CLAVE not in _nombres(
            monkeypatch, 'https://serviciosqa.siesacloud.com', 'true')

    def test_produccion_con_skip_ALARMA(self, monkeypatch):
        assert CLAVE in _nombres(
            monkeypatch, 'https://servicios.siesacloud.com', 'true')

    def test_produccion_sin_skip_no_alarma(self, monkeypatch):
        assert CLAVE not in _nombres(
            monkeypatch, 'https://servicios.siesacloud.com', 'false')

    def test_entra_en_la_lista_que_mira_el_health(self, monkeypatch):
        """No sirve de nada si `problemas()` no la incluye — es la lista que
        leen el health y la validación de arranque."""
        monkeypatch.setenv('CONNEKTA_URL', 'https://servicios.siesacloud.com')
        monkeypatch.setenv('SKIP_FE_CHECK', 'true')
        assert CLAVE in {p['variable'] for p in vc.problemas()}

    def test_el_detalle_dice_qué_hacer(self, monkeypatch):
        monkeypatch.setenv('CONNEKTA_URL', 'https://servicios.siesacloud.com')
        monkeypatch.setenv('SKIP_FE_CHECK', 'true')
        p = next(x for x in vc.problemas() if x['variable'] == CLAVE)
        assert 'ANTES de cambiar CONNEKTA_URL' in p['detalle']
