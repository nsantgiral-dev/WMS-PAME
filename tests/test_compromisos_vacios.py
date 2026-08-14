"""
Una lista vacía que significaba tres cosas.

`get_compromisos_pedido` devolvía `[]` en tres situaciones distintas:

    modo simulación          → []
    parámetros inválidos     → []
    CUALQUIER excepción      → []      ← red caída, timeout, 429, Siesa fuera

Y `DespachoParialService` lee la lista vacía como **«la automatización de Siesa
ya procesó el pedido completo»**, así que marca la tarea `DESPACHADO` y
`siesa_triggered = True`.

Sin remisión. Sin factura.

**Mercancía saliendo del centro de distribución sin respaldo fiscal, en verde en
el tablero.** Y la guarda `if tarea.siesa_triggered` bloqueaba el reintento para
siempre: el pedido quedaba en ese estado sin forma de recuperarlo desde la app.

Se dispara con cualquier caída del ERP durante un cierre de empaque.

## Es la misma regla de siempre

`ConnektaPaginacionError` ya existía en este archivo por el mismo motivo, y
`get_factura_desde_pedido` ya fallaba rápido ante error de red. Esta consulta
era la inconsistente — y la que más costaba.
"""
from unittest.mock import patch

import pytest

from app.services.connekta_gateway import CompromisosNoDisponibles, ConnektaGateway


def _gw():
    g = ConnektaGateway()
    g.modo_simulacion = False
    g._cb_state = 'CLOSED'
    return g


class TestNoPoderPreguntarNoEsNoHaber:

    def test_un_fallo_de_red_levanta_en_vez_de_devolver_vacio(self):
        gw = _gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('timeout')):
            with pytest.raises(CompromisosNoDisponibles):
                gw.get_compromisos_pedido('PD', 1234, f430_rowid=99)

    def test_el_mensaje_dice_por_que_no_se_marca_el_despacho(self):
        """Quien lo lea en un log tiene que entender la consecuencia, no solo
        que algo falló."""
        gw = _gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('timeout')):
            with pytest.raises(CompromisosNoDisponibles, match='No se marca el despacho'):
                gw.get_compromisos_pedido('PD', 1234, f430_rowid=99)

    def test_una_respuesta_vacia_de_verdad_sigue_siendo_vacia(self):
        """El caso legítimo: Siesa respondió y no hay compromisos pendientes.
        Eso SÍ significa que el pedido ya se procesó."""
        gw = _gw()
        with patch.object(gw, '_get', return_value={'detalle': {'Table': []}}):
            assert gw.get_compromisos_pedido('PD', 1234, f430_rowid=99) == []

    def test_el_mapa_de_rowids_no_vuelve_a_taparla(self):
        """`get_pedido_rowid_map` la llama por dentro. Si atrapara la excepción
        devolvería un mapa vacío, que es la misma mentira un nivel arriba."""
        gw = _gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('timeout')):
            with pytest.raises(CompromisosNoDisponibles):
                gw.get_pedido_rowid_map('PD', 1234, f430_rowid=99)


class TestElDespachoNoSeMarcaSinDocumento:

    def test_la_rama_de_compromisos_vacios_sigue_documentada(self):
        """La rama que marca DESPACHADO con `'244328-AUTO'` solo es correcta
        mientras la consulta distinga «no hay» de «no pude preguntar»."""
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'despacho_parcial_service.py').read_text(encoding='utf-8')
        i = fuente.find('if not compromisos_siesa:')
        assert i != -1
        contexto = fuente[max(0, i - 1200):i]
        assert 'no pude preguntar' in contexto, (
            'la rama perdió la advertencia de por qué es segura')

    def test_la_consulta_no_puede_volver_a_devolver_vacio_ante_error(self):
        """Trinquete sobre la causa. Si alguien reintroduce el `return []` en el
        `except`, esta rama vuelve a marcar despachos sin documento fiscal."""
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'connekta_gateway.py').read_text(encoding='utf-8')
        i = fuente.find('def get_compromisos_pedido')
        j = fuente.find('\n    def ', i + 10)
        cuerpo = fuente[i:j]
        assert 'raise CompromisosNoDisponibles' in cuerpo
        assert 'logger.warning' not in cuerpo, (
            'volvió a degradarse a warning: un fallo que marca un despacho sin '
            'factura no es un warning')
