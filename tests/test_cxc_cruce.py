"""
Una sola forma de encontrar la fila de cartera de una factura.

Estaba escrita **dos veces, con claves distintas**, y las dos citaban la misma
verificación en vivo del 2026-08-11:

    liquidacion_service.py   matcheaba contra el PEDIDO   ← el correcto
    siesa_job_service.py     matcheaba contra la FACTURA  ← nunca encontraba nada

Y la segunda decide, tras un POST que lanzó excepción, si el recibo de caja
**sí entró**. Al no encontrar nunca la fila respondía «no entró», el job
revertía la bandera y la cola reenviaba: **segundo recibo de caja**. Que es
exactamente el incidente RC-00002744 que la Regla 3 existe para prevenir.

Dos implementaciones de la misma pregunta divergen. Ya pasó con la condición de
pago y con el fallback de días; acá costaba un documento financiero duplicado.
"""
import pytest

from app.services import cxc_cruce as cx

_FILA = {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': '100',
         'f353_total_db': 1500000, 'f353_total_cr': 0}


class TestSeBuscaPorElPedido:

    def test_encuentra_por_el_pedido(self):
        assert cx.fila_de_la_factura([_FILA], 'PD', 100) is _FILA

    def test_no_encuentra_por_la_factura(self):
        """El defecto exacto. `FE`/`5020` es la factura, y los campos de cruce
        traen el pedido."""
        assert cx.fila_de_la_factura([_FILA], 'FE', 5020) is None

    def test_el_consecutivo_se_compara_como_texto(self):
        """Siesa devuelve `'100'` y la tarea guarda `100`. Comparar tipos
        distintos no matchearía nunca — y el fallo sería silencioso."""
        assert cx.fila_de_la_factura([_FILA], 'PD', '100') is _FILA
        assert cx.fila_de_la_factura([_FILA], ' PD ', 100) is _FILA

    def test_sin_pedido_no_inventa_una_fila(self):
        assert cx.fila_de_la_factura([_FILA], '', 100) is None
        assert cx.fila_de_la_factura([_FILA], 'PD', None) is None


class TestNoSaberTieneSuPropioValor:
    """`None` es el caso que antes se colapsaba a `False`."""

    def test_saldada_devuelve_True(self):
        f = dict(_FILA, f353_total_cr=1500000)
        assert cx.esta_saldada([f], 'PD', 100) is True

    def test_con_saldo_devuelve_False(self):
        assert cx.esta_saldada([_FILA], 'PD', 100) is False

    def test_sin_fila_devuelve_None_y_no_False(self):
        """La diferencia que costaba el recibo duplicado: `False` significa
        «tiene saldo, reintentá»; acá lo correcto es «no sé»."""
        assert cx.esta_saldada([], 'PD', 100) is None
        assert cx.esta_saldada([_FILA], 'PD', 999) is None

    def test_tolera_centavos(self):
        f = dict(_FILA, f353_total_cr=1499999.7)
        assert cx.esta_saldada([f], 'PD', 100) is True


class TestLosDosSitiosUsanLaMismaFuncion:
    """Lo que impide que vuelvan a divergir."""

    @staticmethod
    def _fuente(rel):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[1] / rel).read_text(encoding='utf-8')

    @pytest.mark.parametrize('archivo', [
        'app/services/liquidacion_service.py',
        'app/services/siesa_job_service.py',
    ])
    def test_ninguno_rearma_la_busqueda_a_mano(self, archivo):
        fuente = self._fuente(archivo)
        assert 'cxc_cruce' in fuente, f'{archivo} dejó de usar la política común'
        assert "r.get('f353_id_tipo_docto_cruce'" not in fuente, (
            f'\n{archivo} volvió a armar el match de cartera por su cuenta. '
            f'Así fue como las dos versiones divergieron: una buscaba por el '
            f'pedido y la otra por la factura, y la segunda decidía si un '
            f'recibo de caja se reenviaba.')
