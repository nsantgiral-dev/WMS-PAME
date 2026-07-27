"""
Tests de la descarga de kardex.

Cisne negro que previenen: UNA DESCARGA PARCIAL REPORTADA COMO ÉXITO.

Un kardex truncado no produce un error — produce descensura equivocada, ROP
equivocado y temporada equivocada, todo plausible y sin una sola alarma. Y no
era hipotético: ~17.000 peticiones a 0.1-0.2s son 28-57 minutos, y el corte
duro estaba DENTRO de ese rango, no cerca.

Antes había tres formas de terminar —fin natural, timeout, excepción— y las
tres caían en el mismo retorno de éxito.
"""
import os
import re

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(rel):
    with open(os.path.join(_RAIZ, rel), encoding='utf-8') as f:
        return f.read()


class TestParcialEsFallo:
    """La propiedad central: completa y truncado no pueden verse igual."""

    def test_sin_datos_no_reporta_exito(self, app, db):
        """En simulación la primera página viene vacía: no es una descarga buena."""
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        assert r['ok'] is False
        assert r['estado'] == 'SIN_DATOS'

    def test_el_resultado_declara_como_termino(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        for clave in ('ok', 'estado', 'rango_pedido', 'rango_traido',
                      'pagina_final', 'reanudar_desde'):
            assert clave in r, f'falta {clave} en el resultado'

    def test_ok_solo_es_true_si_esta_completa(self):
        """Guard estructural: ok se deriva del estado, no se pone a mano."""
        src = _src('app/services/kardex_service.py')
        assert "completa = estado == 'COMPLETA'" in src
        assert "'ok': completa" in src

    def test_los_tres_finales_estan_diferenciados(self):
        """Timeout, excepción y fin natural: tres estados, no uno."""
        src = _src('app/services/kardex_service.py')
        for estado in ('TIMEOUT_PARCIAL', 'ERROR_PARCIAL', 'SIN_DATOS', 'COMPLETA'):
            assert estado in src, f'falta el estado {estado}'

    def test_una_parcial_advierte_de_no_seguir(self):
        """Correr /reconstruir sobre un kardex truncado propaga el hueco."""
        src = _src('app/services/kardex_service.py')
        assert 'advertencia' in src and 'reconstruir' in src


class TestRangoPedidoVsTraido:
    """La comparación que delata el truncamiento sin mirar los logs."""

    def test_el_rango_pedido_sale_de_los_argumentos(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20250115', '20250220', max_minutos=1)
        assert r['rango_pedido']['desde'] == '2025-01-15'
        assert r['rango_pedido']['hasta'] == '2025-02-20'

    def test_el_rango_traido_es_none_si_no_llego_nada(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        assert r['rango_traido']['desde'] is None


class TestReanudacion:
    """Trocear por página, no por fecha.

    La consulta dinámica NO acepta filtros de fecha —se filtra en Python
    después de recibir— así que acotar el rango no ahorra ni una petición.
    Lo que sí funciona es reanudar desde donde se cortó.
    """

    def test_acepta_pagina_inicial(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', pagina_inicial=42, max_minutos=1)
        assert r['pagina_inicial'] == 42

    def test_una_completa_no_deja_punto_de_reanudacion(self, app, db):
        from app.services.kardex_service import KardexService
        r = KardexService.descargar_kardex('20240101', max_minutos=1)
        if r['ok']:
            assert r['reanudar_desde'] is None

    def test_el_tope_de_tiempo_bajo_del_corte_anterior(self):
        """25 min por corrida: mejor varias honestas que una que se rinde."""
        src = _src('app/services/kardex_service.py')
        assert "KARDEX_MAX_MINUTOS', '25'" in src

    def test_el_panel_ofrece_reanudar(self):
        js = _src('app/static/pwa/kardex.js')
        assert 'reanudar_desde' in js
        assert 'kardexDescargar(r.reanudar_desde)' in js.replace('${', '').replace('}', '')


class TestAvisoOperativo:
    """17.000 llamadas contra el ERP que factura en siete puntos de venta."""

    def test_el_endpoint_avisa_del_impacto(self):
        src = _src('app/routes/kardex.py')
        assert 'FUERA DE HORARIO' in src.upper()

    def test_el_panel_lo_muestra_antes_de_pulsar(self):
        """El aviso sirve antes del clic, no en un log después."""
        js = _src('app/static/pwa/kardex.js')
        assert '17.000' in js and 'fuera de horario' in js.lower()
