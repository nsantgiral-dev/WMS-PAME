"""
La liquidación diaria a escala (validación de la plata, 2026-09-26).

- **La retención que no salió tiene salida** (P1-4): `politica_cobro.dc_pendiente`
  / `documentos_pendientes` conocen el DC; la señal y el resumen diario lo
  avisan cuando el recibo ya salió y la retención no.
- **«Enviar todo a Siesa» en Liquidación** (P1-6): la misma operación que el
  botón de la planilla, para quien liquida, con lo que sale y lo que no.
- **El detalle lee cada factura una vez y en paralelo** (P1-6): en serie, 25
  paradas con Siesa lenta eran 25 lecturas en fila.
- **La planilla no ofrece botones de plata** a quien no los puede usar, ni
  «Liquidar Ruta» a una ruta en tránsito (P2-7/9).
"""
import threading
import time
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_cartera_retencion import _jwt, _recaudo, _tarea, _usuario
from tests.test_sin_codigos_en_pantalla import _node, visible


class TestLaRetencionQueFalta:

    def _rec(self, db, almacen, **kw):
        t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 10**6}', cond='C02', valor=100_000)
        r = _recaudo(db, t, estado=kw.get('estado', 'ENTREGADO'), monto=97_500, descuento=2_500)
        r.motivo_descuento = 'RETEFUENTE_2.5'
        r.retencion_confirmada = True
        r.siesa_rc_triggered = kw.get('rc', True)
        db.session.commit()
        return r

    def test_confirmada_sin_documento_es_pendiente(self, db, almacen):
        from app.services import politica_cobro as pc
        r = self._rec(db, almacen)
        assert pc.dc_pendiente(r) == pc.DC_LISTO and 'DC' in pc.documentos_pendientes(r)
        claves = [s['clave'] for s in __import__('app.services.senales_ruta', fromlist=['x'])
                  .senales_de_recaudo(r)]
        assert 'retencion_sin_documento' in claves

    def test_en_cola_o_enviada_ya_no(self, db, almacen):
        from app.models.siesa_job import SiesaJob
        from app.services import politica_cobro as pc
        r = self._rec(db, almacen)
        SiesaJob.encolar(tipo='DOCUMENTO_CONTABLE_RET', payload={'recaudo_id': r.id,
                                                                  'cuenta_puc': '13551501'},
                         referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        db.session.commit()
        assert pc.dc_pendiente(r) is None

    def test_parcial_sin_devolucion_amarrada_espera(self, db, almacen):
        from app.services import politica_cobro as pc
        r = self._rec(db, almacen, estado='PARCIAL')
        assert pc.dc_pendiente(r) == pc.DC_ESPERA_DEVOLUCION
        assert 'DC' not in pc.documentos_pendientes(r)

    def test_el_resumen_diario_la_nombra_con_ruta_pedido_y_conductor(self, db, almacen):
        from app.services import politica_cobro as pc
        r = self._rec(db, almacen)
        [linea] = pc.lineas_de_aviso_dc()
        assert f'ruta {r.ruta_id}' in linea and r.tarea.numero_pedido_siesa in linea

    def test_antes_del_recibo_no_es_aviso(self, db, almacen):
        from app.services import politica_cobro as pc
        self._rec(db, almacen, rc=False)
        assert pc.lineas_de_aviso_dc() == []


class TestLasFacturasEnParalelo:

    def test_cada_factura_una_vez_y_con_tope(self):
        from app.services import liquidacion_service as ls
        vivas, maximo, llamadas = [0], [0], []
        candado = threading.Lock()

        class Lento:
            def get_rowids_factura(self, tipo, consec):
                with candado:
                    vivas[0] += 1
                    maximo[0] = max(maximo[0], vivas[0])
                    llamadas.append((tipo, consec))
                time.sleep(0.05)
                with candado:
                    vivas[0] -= 1
                if consec == '13':
                    raise RuntimeError('Siesa no respondió')
                return [{'f470_rowid': consec}]

        facturas = {('FEW', str(i)) for i in range(25)}
        with patch('app.services.connekta_gateway.connekta', Lento()):
            t0 = time.monotonic()
            res = ls.leer_facturas_en_paralelo(facturas)
            dt = time.monotonic() - t0
        assert sorted(llamadas) == sorted(facturas)
        assert 2 <= maximo[0] <= ls.PARALELO_FACTURAS
        assert isinstance(res[('FEW', '13')], RuntimeError)       # el fallo se declara
        assert res[('FEW', '0')] == [{'f470_rowid': '0'}]
        assert dt < 25 * 0.05                                       # no en serie


class TestLaPlanilla:

    def test_trae_los_permisos_de_plata(self, app, client, db, almacen):
        t = _tarea(db, almacen, f'003-PD-{uuid.uuid4().int % 10**6}', cond='C02', valor=10_000)
        r = _recaudo(db, t, estado='ENTREGADO', monto=10_000)
        for rol, puede in (('liquidador', True), ('lider_cartera', False), ('gerente', False)):
            d = client.get(f'/api/rutas/{r.ruta_id}/planilla',
                           headers=_jwt(app, _usuario(db, rol=rol))).get_json()
            assert d['permisos']['liquidar'] is puede, rol

    def test_sin_permiso_no_hay_botones_de_plata(self, tmp_path):
        base = {'ruta': {'id': 7, 'estado': 'ENTREGADA'}, 'paradas': [], 'sin_gestionar': 0,
                'total_recaudado': 0, 'totales_por_forma': {}, 'estado_financiero': 'PENDIENTE'}
        for permiso, estado, ve in ((True, 'ENTREGADA', True), (False, 'ENTREGADA', False),
                                    (True, 'EN_TRANSITO', False)):
            d = {**base, 'ruta': {'id': 7, 'estado': estado}, 'permisos': {'liquidar': permiso}}
            out = _node(tmp_path, ['util.js', 'rutas.js'], {'/planilla': d}, """
                await _cargarPlanilla(7);
                return { html: document.getElementById('modal-planilla-body').innerHTML };
            """)
            assert ('Liquidar Ruta' in visible(out['html'])) is ve, (permiso, estado)


class TestEnviarTodoEnLiquidacion:

    def test_dice_que_sale_y_que_no(self, tmp_path):
        det = {'ruta': {'id': 3, 'estado_financiero': 'LIQUIDADA', 'conductor_nombre': 'Ana'},
               'permisos': {'liquidar': True},
               'recaudos': [
                   {'id': 1, 'numero_pedido': 'PD1', 'cliente': 'A', 'estado_entrega': 'ENTREGADO',
                    'forma_pago': 'EFECTIVO', 'monto_cobrado': 1, 'documentos_pendientes': ['RC', 'DC'],
                    'senales': [], 'rc_en_cola': False},
                   {'id': 2, 'numero_pedido': 'PD2', 'cliente': 'B', 'estado_entrega': 'ENTREGADO',
                    'forma_pago': 'EFECTIVO', 'monto_cobrado': 1, 'documentos_pendientes': [],
                    'decision_retencion': 'PENDIENTE', 'senales': []}]}
        for permiso in (True, False):
            det['permisos']['liquidar'] = permiso
            out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
                _liqDetalleRuta = DET; _liqRenderDetalle();
                return { html: document.getElementById('liq-modal-body').innerHTML };
            """, globales={'DET': det})
            v = visible(out['html'])
            assert ('Enviar todo a Siesa' in v) is permiso
            if permiso:
                assert 'PD1: recibo de caja, retención' in v
                assert 'PD2: la retención espera que alguien la confirme' in v
