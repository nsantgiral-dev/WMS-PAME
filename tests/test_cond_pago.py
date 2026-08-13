"""
Un pedido sin condición de pago no es un pedido de contado.

Hasta el 2026-08-13, `f430_id_cond_pago` vacío se leía como CONTADO en dos
sitios independientes:

  · `ruta_service._valor_y_cond_pago`: `es_contado = (not cond_pago) or ...`
    → con vacío daba **True**.
  · `connekta_gateway.trigger_factura_desde_remision`: cae a
    `SIESA_COND_PAGO_VENTAS` (obligado — Connekta V2 colapsa con null).

El primero llega a la pantalla del conductor. `rutas.js:1908` solo muestra el
modo CRÉDITO cuando `es_contado === false` **confirmado**, así que con el campo
vacío mostraba «Valor a Cobrar»: **se le pedía al conductor cobrarle a un
cliente cuya condición de pago nadie conocía.**

## Por qué el vacío no puede colapsar a contado

Significa tres cosas que no se distinguen: maestro incompleto, consulta fallida,
o cliente realmente de contado. El lado conservador acá no es cobrar — es no
afirmar. Regla 0.

Hoy el daño es acotado. Con la factura diferida al momento de la liquidación
—diseño en evaluación— sería: cliente de crédito leído como contado, sin
factura, sin CxC y sin consumir cupo. Un crédito otorgado por un campo vacío.

## Y sobre cómo se mide si esto pasa

Se estuvo discutiendo con conteos de facturas de otro sistema («de 382.812,
prácticamente ninguna tiene condición vacía»). **Ese conteo no puede
detectarlo**: el fallback rellena el campo antes de emitir, así que toda
factura sale con condición. Es la huella del fallback, no su ausencia.

Lo que sí lo detecta es el rastro que deja `registrar_ausencia`, y la alerta
que el gateway encola.
"""
import pytest

from app.services import cond_pago as cp


class TestElVacioNoEsContado:

    def test_vacio_es_ausente_no_contado(self):
        assert cp.clasificar('', 'C01') == cp.AUSENTE
        assert cp.clasificar(None, 'C01') == cp.AUSENTE
        assert cp.clasificar('   ', 'C01') == cp.AUSENTE

    def test_el_codigo_de_contado_es_contado(self):
        assert cp.clasificar('C01', 'C01') == cp.CONTADO

    @pytest.mark.parametrize('cond', ['C02', '30D', 'C30', 'CREDITO60'])
    def test_cualquier_otra_es_credito(self, cond):
        assert cp.clasificar(cond, 'C01') == cp.CREDITO

    def test_C02_es_credito_aunque_sea_de_un_dia(self):
        """Para Siesa, C02 es crédito a un día — no contado. Es la distinción
        que decide si un pedido de ruta se retiene por mora."""
        assert cp.clasificar('C02', 'C01') == cp.CREDITO


class TestLaPantallaRecibeNoSe:
    """`None` es el valor que antes se colapsaba a `True`."""

    def test_ausente_devuelve_None(self):
        assert cp.es_contado_o_none('', 'C01') is None

    def test_contado_devuelve_True(self):
        assert cp.es_contado_o_none('C01', 'C01') is True

    def test_credito_devuelve_False(self):
        assert cp.es_contado_o_none('30D', 'C01') is False

    def test_None_no_es_False(self):
        """Distinción que importa en el JS: `es_contado === false` dispara el
        modo CRÉDITO. `None` no debe disparar CRÉDITO —no sabemos que lo
        sea— sino caer al campo libre."""
        assert cp.es_contado_o_none('', 'C01') is not False


class TestUnaSolaPolitica:
    """Anti-divergencia: la lectura del vacío estaba escrita dos veces, las dos
    hacia contado. Un fix en un solo sitio reproduce el defecto."""

    from pathlib import Path
    _RAIZ = Path(__file__).resolve().parents[1]
    _ARCHIVOS = ['app/services/ruta_service.py',
                 'app/services/connekta_gateway.py']

    def test_nadie_vuelve_a_leer_el_vacio_por_su_cuenta(self):
        import re
        culpables = []
        for rel in self._ARCHIVOS:
            texto = (self._RAIZ / rel).read_text(encoding='utf-8')
            for n, linea in enumerate(texto.split('\n'), 1):
                if linea.strip().startswith('#'):
                    continue
                # `not cond_pago` o `not _cond_pago_siesa` en una expresión que
                # decide contado: la forma exacta del defecto original.
                if re.search(r'not\s+_?cond_pago\w*\s*\)?\s*(or|and|if)', linea):
                    culpables.append(f'{rel}:{n}  {linea.strip()[:70]}')
        assert not culpables, (
            '\nVuelven a interpretar la condición vacía por su cuenta:\n'
            + '\n'.join(f'  · {c}' for c in culpables)
            + '\n\nUsar `services/cond_pago.py`.')

    def test_los_dos_consumidores_importan_el_modulo(self):
        faltan = [r for r in self._ARCHIVOS
                  if 'cond_pago as' not in (self._RAIZ / r).read_text(encoding='utf-8')]
        assert not faltan, f'no consumen la política compartida: {faltan}'


class TestRutaServiceNoAfirmaContado:
    """El defecto medido, en el sitio donde estaba."""

    def test_es_contado_es_None_cuando_siesa_no_trae_condicion(self, monkeypatch):
        from app.services import ruta_service as rs
        from app.services.connekta_gateway import connekta

        class _Tarea:
            id = 1
            rm_tipo, rm_consec = 'RS', 7
            tipo_docto_pedido_siesa = 'PD'
            consec_docto_pedido_siesa = '999'

        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **kw: [{'f350_id_tipo_docto': 'FEW',
                                           'f350_consec_docto': '1'}], raising=False)
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [], raising=False)
        # Siesa responde, pero sin condición de pago — el caso real.
        monkeypatch.setattr(connekta, 'get_pedido_cabecera',
                            lambda *a, **k: {'f430_id_cond_pago': ''}, raising=False)

        _, es_contado, _ = rs.RutaService._valor_y_cond_pago(_Tarea())
        assert es_contado is None, (
            'con condición vacía volvió a afirmar contado — la pantalla del '
            'conductor le pediría cobrar sin saber si el cliente es de crédito')

    def test_es_contado_es_True_con_condicion_de_contado(self, monkeypatch):
        from app.services import ruta_service as rs
        from app.services.connekta_gateway import connekta

        class _Tarea:
            id = 1
            rm_tipo, rm_consec = 'RS', 7
            tipo_docto_pedido_siesa = 'PD'
            consec_docto_pedido_siesa = '999'

        monkeypatch.setattr(connekta, 'modo_simulacion', False, raising=False)
        monkeypatch.setattr(connekta, 'get_detalle_factura',
                            lambda **kw: [{'f350_id_tipo_docto': 'FEW',
                                           'f350_consec_docto': '1'}], raising=False)
        monkeypatch.setattr(connekta, 'get_rowids_factura', lambda *a, **k: [], raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'get_pedido_cabecera',
                            lambda *a, **k: {'f430_id_cond_pago': 'C01'}, raising=False)

        _, es_contado, _ = rs.RutaService._valor_y_cond_pago(_Tarea())
        assert es_contado is True
