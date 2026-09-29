"""
Las pantallas de la caja y del banco, pintadas en Node con `util.js` real y
respuestas reales del servidor (m051liqcaja, 2026-09-27).

- Liquidación → «Caja por recibir»: el conductor con su efectivo, el botón a
  quien recibe la caja (el gerente ve y no toca), datos escapados.
- El acta: los totales cuadran a la vista mientras se teclea (con puntos de
  miles), la diferencia pide motivo, un gasto aceptado cuenta.
- «Transferencias»: la cola con su comprobante a pedido; verificar solo a quien
  puede; el banco en palabras.
- La cabecera: un universo y «Total cobrado»; una ruta liquidada con algo
  pendiente en Siesa sigue en la lista.
- El teléfono del conductor: lo que entrega y el acta para confirmar.
"""
import pytest

from tests.test_caja_conductor import _conductor, _gasto, _ruta, recibir_caja_de
from tests.test_cartera_retencion import _jwt, _usuario
from tests.test_sin_codigos_en_pantalla import _node, codigos_crudos, visible


def _dash(app, client, db, rol='liquidador'):
    return client.get('/api/rutas/liquidacion/dashboard',
                      headers=_jwt(app, _usuario(db, rol))).get_json()


class TestCajaPorRecibir:

    def _pintar(self, app, client, db, tmp_path, rol):
        d = client.get('/api/rutas/caja/por-recibir', headers=_jwt(app, _usuario(db, rol))).get_json()
        return _node(tmp_path, ['util.js', 'liquidacion.js'], {'/caja/por-recibir': d}, """
            await liqCargarCaja();
            return { html: document.getElementById('liq-caja').innerHTML };
        """)['html']

    def test_el_liquidador_recibe_y_el_gerente_mira(self, app, client, db, almacen, tmp_path):
        c, _ = _conductor(db)
        c.nombre = 'Ana <script>'
        db.session.commit()
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 2100000, 2100000),
                               ('ENTREGADO', 'TRANSFERENCIA_BBVA', 50000, 50000)])
        html = self._pintar(app, client, db, tmp_path, 'liquidador')
        txt = visible(html)
        assert 'Caja por recibir' in txt and '$2.100.000 en efectivo' in txt
        assert '<script>' not in html and 'Ana &lt;script&gt;' in html
        assert 'liqAbrirCaja(0)' in html and 'se verifican en el banco' in txt
        assert not codigos_crudos(html)
        assert 'liqAbrirCaja(' not in self._pintar(app, client, db, tmp_path, 'gerente')

    def test_sin_caja_no_pinta_nada(self, app, client, db, almacen, tmp_path):
        assert self._pintar(app, client, db, tmp_path, 'liquidador') == ''


class TestElActa:

    def _abrir(self, app, client, db, tmp_path, conductor_id, contado, gastos=None, rol='liquidador'):
        d = client.get(f'/api/rutas/caja/conductor/{conductor_id}',
                       headers=_jwt(app, _usuario(db, rol))).get_json()
        decide = ''.join(f'liqActaGasto({j}, {v});' for j, v in (gastos or []))
        return _node(tmp_path, ['util.js', 'liquidacion.js'], {'/caja/conductor/': d}, f"""
            await liqAbrirCajaConductor({conductor_id});
            {decide}
            document.getElementById('liq-acta-contado').value = {contado!r};
            liqActaRecalcular();
            return {{ form: document.getElementById('liq-modal-body').innerHTML,
                     resumen: document.getElementById('liq-acta-resumen').innerHTML,
                     motivo: document.getElementById('liq-acta-motivo-box').style.display }};
        """)

    def test_los_totales_cuadran_a_la_vista(self, app, client, db, almacen, tmp_path):
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 2100000, 2100000)])
        out = self._abrir(app, client, db, tmp_path, c.id, '2.000.000')
        r = visible(out['resumen'])
        assert 'Contado $2.000.000' in r and 'Efectivo esperado $2.100.000' in r
        assert 'Faltan $100.000' in r and out['motivo'] == 'block'
        assert 'inputmode="numeric"' in out['form']
        assert not codigos_crudos(out['form'])

    def test_cuadra_sin_pedir_motivo(self, app, client, db, almacen, tmp_path):
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 50000, 50000)])
        out = self._abrir(app, client, db, tmp_path, c.id, '50000')
        assert 'Cuadra' in visible(out['resumen']) and out['motivo'] == 'none'

    def test_el_gasto_aceptado_cuenta(self, app, client, db, almacen, tmp_path):
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 1200000, 1200000)])
        _gasto(db, u, 180000)
        out = self._abrir(app, client, db, tmp_path, c.id, '1020000', gastos=[(0, 1)])
        r = visible(out['resumen'])
        assert 'Gastos aceptados $180.000' in r and 'Cuadra' in r
        assert 'Combustible' in visible(out['form']) and 'combustible' not in codigos_crudos(out['form'])

    def test_el_gerente_no_tiene_boton(self, app, client, db, almacen, tmp_path):
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 50000, 50000)])
        out = self._abrir(app, client, db, tmp_path, c.id, '50000', rol='gerente')
        assert 'liqActaRegistrar' not in out['form']


class TestTransferencias:

    def _pintar(self, app, client, db, tmp_path, rol):
        d = client.get('/api/rutas/transferencias/por-verificar',
                       headers=_jwt(app, _usuario(db, rol))).get_json()
        return _node(tmp_path, ['util.js', 'liquidacion.js'], {'/transferencias/por-verificar': d}, """
            await liqCargarTransferencias();
            return { html: document.getElementById('liq-lista-transferencias').innerHTML };
        """)['html']

    def test_la_cola(self, app, client, db, almacen, tmp_path):
        c, _ = _conductor(db)
        _, [r] = _ruta(db, almacen, c, [('ENTREGADO', 'TRANSFERENCIA_BBVA', 50000, 50000)])
        r.referencia_pago = '8877'
        r.foto_comprobante = 'data:image/jpeg;base64,AAAA'
        db.session.commit()
        html = self._pintar(app, client, db, tmp_path, 'lider_cartera')
        txt = visible(html)
        assert 'BBVA' in txt and 'ref. 8877' in txt and '$50.000' in txt
        assert 'Por verificar en el banco' in txt and 'liqVerComprobante(' in html
        assert 'liqVerificarTransferencia(0, 1)' in html
        assert not codigos_crudos(html)
        assert 'liqVerificarTransferencia(' not in self._pintar(app, client, db, tmp_path, 'gerente')

    def test_el_comprobante_se_pide_y_se_pinta(self, app, client, db, almacen, tmp_path):
        c, _ = _conductor(db)
        _, [r] = _ruta(db, almacen, c, [('ENTREGADO', 'TRANSFERENCIA_BBVA', 50000, 50000)])
        r.foto_comprobante = 'data:image/jpeg;base64,AAAA'
        db.session.commit()
        f = client.get(f'/api/rutas/recaudos/{r.id}/comprobante',
                       headers=_jwt(app, _usuario(db, 'liquidador'))).get_json()
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {'/comprobante': f}, f"""
            await liqVerComprobante({r.id}, 'caja-x');
            return {{ html: document.getElementById('caja-x').innerHTML }};
        """)
        assert '<img src="data:image/jpeg;base64,AAAA"' in out['html']


class TestLaCabecera:

    def test_un_universo_y_total_cobrado(self, app, client, db, almacen, tmp_path):
        c, _ = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000),
                               ('ENTREGADO', 'CHEQUE', 20000, 20000)])
        d = _dash(app, client, db)
        r = d['resumen']
        assert r['total_cobrado'] == 50000 and r['total_cheque'] == 20000 and r['cuadra'] is True
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqRenderKpis(DASH.resumen);
            return { k: document.getElementById('liq-kpis').innerHTML,
                     u: document.getElementById('liq-universo').innerHTML };
        """, globales={'DASH': d})
        k = visible(out['k'])
        assert 'Total cobrado $50.000' in k and 'Cheque $20.000' in k
        assert 'de la lista' in visible(out['u'])

    def test_liquidada_con_algo_pendiente_sigue_en_la_lista(self, app, client, db, almacen,
                                                           tmp_path, monkeypatch):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService
        monkeypatch.setattr(LiquidacionService, 'liquidar_ruta_siesa',
                            staticmethod(lambda rid, admin_id=None: {'errores': []}))
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        recibir_caja_de(db, ruta.id)
        RutaService.liquidar_ruta(ruta.id, usuario_id=1)
        d = _dash(app, client, db)
        [x] = [x for x in d['rutas'] if x['id'] == ruta.id]
        assert x['falta_en_siesa'] and x['caja']['estado'] == 'RECIBIDA'
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqDashboard = DASH;
            liqCargarPendientes();
            return { html: document.getElementById('liq-lista-pendientes').innerHTML };
        """, globales={'DASH': d})
        txt = visible(out['html'])
        assert 'Liquidada · falta en Siesa: recibo de caja (1)' in txt
        assert 'Caja recibida · cuadró' in txt
        assert d['resumen']['liquidadas_falta_siesa'] == 1


class TestElTelefonoDelConductor:

    def test_lo_que_entrega_y_el_acta(self, app, client, db, almacen, tmp_path):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 100000, 100000)])
        cc.registrar_acta(c.id, 90000, _usuario(db, 'liquidador').id,
                          motivo_diferencia='Dice <b>que</b> dio vueltas')
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 40000, 40000)], estado='EN_TRANSITO')
        d = client.get('/api/rutas/caja/mi-resumen', headers=_jwt(app, u)).get_json()
        out = _node(tmp_path, ['util.js', 'modal.js', 'rutas.js'], {}, """
            return { html: _condBloqueMiCaja(D) };
        """, globales={'D': d})
        html = out['html']
        txt = visible(html)
        assert 'Faltan $10.000' in txt and 'Estoy de acuerdo' in txt
        assert 'Usted entrega en la oficina' in txt and '$40.000 en efectivo' in txt
        assert '<b>que</b>' not in html
        assert 'condResponderActa(0, 1)' in html

    def test_el_resumen_al_cerrar_sin_senal(self, tmp_path):
        paradas = [{'recaudo': {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 30000}},
                   {'recaudo': {'estado_entrega': 'ENTREGADO', 'forma_pago': 'TRANSFERENCIA_BBVA', 'monto_cobrado': 20000}},
                   {'recaudo': {'estado_entrega': 'RECHAZADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 5000}}]
        out = _node(tmp_path, ['util.js', 'modal.js', 'rutas.js'], {}, """
            return { html: _condResumenEntrega(P) };
        """, globales={'P': paradas})
        txt = visible(out['html'])
        assert '$30.000 en efectivo' in txt and '1 transferencia(s) por $20.000' in txt


class TestElChequeSoloSiExisteEnSiesa:
    """P2-2: CHEQUE se ofrecía en la puerta sin medio en Siesa (el recibo
    quedaba en un bucle). Solo aparece con `SIESA_MEDIO_PAGO_CHEQUE`."""

    @pytest.mark.parametrize('valor,habilitado', [(None, False), ('', False), ('  ', False),
                                                  ('CHQ', True)])
    def test_la_politica(self, monkeypatch, valor, habilitado):
        from app.services import medios_pago
        if valor is None:
            monkeypatch.delenv('SIESA_MEDIO_PAGO_CHEQUE', raising=False)
        else:
            monkeypatch.setenv('SIESA_MEDIO_PAGO_CHEQUE', valor)
        assert medios_pago.cheque_habilitado() is habilitado
        assert ('CHEQUE' in medios_pago.formas_ofrecidas(['EFECTIVO', 'CHEQUE'])) is habilitado

    @pytest.mark.parametrize('habilitado', [False, True])
    def test_el_select_del_conductor(self, tmp_path, habilitado):
        out = _node(tmp_path, ['util.js', 'modal.js', 'rutas.js'], {}, f"""
            _COND_CHEQUE_HABILITADO = {str(habilitado).lower()};
            return {{ contado: _condFormasPago({{cobro_contraentrega: true}}).map(f => f.v).join(','),
                     credito: _condFormasPago({{cobro_contraentrega: false}}).map(f => f.v).join(',') }};
        """)
        for k in ('contado', 'credito'):
            assert ('CHEQUE' in out[k].split(',')) is habilitado, out

    def test_el_gateway_sin_el_medio_no_arma_el_recibo(self, app, monkeypatch):
        """El mapa lleva CHEQUE (integración de liquidación, 2026-09-27); sin
        `SIESA_MEDIO_PAGO_CHEQUE` su valor es vacío y el RC no se arma."""
        from app.services.connekta_gateway import ConnektaGateway, connekta
        assert 'CHEQUE' in connekta._forma_pago_map
        monkeypatch.setitem(connekta._forma_pago_map, 'CHEQUE', None)
        with pytest.raises(ValueError, match='sin medio de pago Siesa'):
            connekta.trigger_recibo_caja('900123', '001', 1000.0, 'CHEQUE', 'FE', '1',
                                         cuenta_cxc='13050501', unidad_negocio='99')
        capturado = {}
        monkeypatch.setattr(ConnektaGateway, '_post',
                            lambda self, c, n, payload: capturado.setdefault('p', payload) or {'codigo': 0})
        monkeypatch.setitem(connekta._forma_pago_map, 'CHEQUE', 'CHQ')
        monkeypatch.setattr(connekta, 'tipo_docto_recibo_caja', connekta.tipo_docto_recibo_caja or 'RC')
        connekta.trigger_recibo_caja('900123', '001', 1000.0, 'CHEQUE', 'FE', '1',
                                     cuenta_cxc='13050501', unidad_negocio='99')
        assert capturado['p']['Caja'][0]['F358_ID_MEDIOS_PAGO'] == 'CHQ'

    def test_el_gateway_lo_lee_de_la_variable(self, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        monkeypatch.setenv('SIESA_MEDIO_PAGO_CHEQUE', 'CHQ')
        assert ConnektaGateway()._forma_pago_map['CHEQUE'] == 'CHQ'
        monkeypatch.delenv('SIESA_MEDIO_PAGO_CHEQUE')
        assert not ConnektaGateway()._forma_pago_map['CHEQUE']

    @pytest.mark.parametrize('habilitado', [False, True])
    def test_confirmar_parada_no_acepta_cheque_sin_medio(self, app, db, almacen, monkeypatch,
                                                          habilitado):
        from app.services.ruta_service import RutaService
        if habilitado:
            monkeypatch.setenv('SIESA_MEDIO_PAGO_CHEQUE', 'CHQ')
        else:
            monkeypatch.delenv('SIESA_MEDIO_PAGO_CHEQUE', raising=False)
        c, u = _conductor(db)
        ruta, [r] = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 1000, 1000)],
                          estado='EN_TRANSITO')
        tarea_id = r.tarea_id
        db.session.delete(r)
        db.session.commit()
        datos = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'CHEQUE', 'monto_cobrado': 1000,
                 'referencia_pago': '445566', 'foto_comprobante': 'data:image/jpeg;base64,/9j/4AAQ'}
        if habilitado:
            RutaService.confirmar_parada(ruta.id, tarea_id, u.id, datos)
        else:
            with pytest.raises(ValueError, match='cheque no está habilitado'):
                RutaService.confirmar_parada(ruta.id, tarea_id, u.id, datos)

    def test_la_lista_de_paradas_lo_dice(self, app, client, db, almacen, monkeypatch):
        monkeypatch.setenv('SIESA_MEDIO_PAGO_CHEQUE', 'CHQ')
        c, u = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 1000, 1000)], estado='EN_TRANSITO')
        d = client.get(f'/api/rutas/{ruta.id}/paradas', headers=_jwt(app, _usuario(db, 'admin'))).get_json()
        assert d['cheque_habilitado'] is True


class TestLaCabeceraPorServicio:

    def test_los_medios_parten_el_total(self, db, almacen):
        from app.services import liquidacion_tablero as lt
        c, _ = _conductor(db)
        r1, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000),
                                       ('ENTREGADO', 'TRANSFERENCIA_BBVA', 20000, 20000),
                                       ('ENTREGADO', 'TARJETA', 5000, 5000),
                                       ('ENTREGADO', 'CHEQUE', 7000, 7000)])
        r2, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 1000, 1000)])
        res = lt.resumen([r1], [r2])
        assert res['total_cobrado'] == 63000 and res['cuadra'] is True
        assert (res['total_efectivo'], res['total_bancario'], res['total_tarjeta'],
                res['total_cheque']) == (31000, 20000, 5000, 7000)
        assert res['pendientes'] == 2 and '1 atrasada' in res['universo']
