"""
La pantalla de Liquidación dice la verdad sobre la plata (2026-09-25).

- Las rutas entregadas y sin liquidar de días ANTERIORES aparecen en
  Pendientes (`rutas_atrasadas`, de `rezago_liquidacion`): la pantalla abre en
  «hoy» y una ruta de ayer desaparecía.
- El botón «Enviar a Siesa» de la planilla sale de la política
  (`pendiente_siesa`): sobre una devolución contada en cero (FALTANTE_TOTAL)
  quedaba visible para siempre invitando a mandar una NC que no va a existir.
- «RC ✓» solo con el recibo confirmado; uno sin verificar se ve y se resuelve.
- Registrar el cobro lo ofrece solo a quien puede liquidar (lo dice el
  servidor, con las mismas funciones que cortan con 403).
- La vista previa del RC de una PARCIAL no resta la retención otra vez.

Todo en Node con `util.js` real y respuestas del servidor.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from tests.test_e2e_total_20260925 import _faltante_total
from tests.test_cartera_retencion import _jwt, _usuario
from tests.test_sin_codigos_en_pantalla import _node, codigos_crudos, visible


def _ruta_entregada(db, almacen, dias_atras=0, liquidada=False):
    from app.models.conductor import Conductor
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    c = Conductor(nombre='C', cedula=f'C{uuid.uuid4().hex[:8]}', activo=True)
    db.session.add(c)
    db.session.flush()
    fecha = (datetime.utcnow() - timedelta(days=dias_atras)).date()
    ruta = RutaDespacho(conductor_id=c.id, tipo_ruta='Urbana', estado='ENTREGADA',
                        fecha_programada=fecha,
                        estado_financiero='LIQUIDADA' if liquidada else 'PENDIENTE',
                        fecha_entregada=datetime.utcnow() - timedelta(days=dias_atras))
    db.session.add(ruta)
    db.session.flush()
    t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                     almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                     consec_docto_pedido_siesa=12)
    db.session.add(t)
    db.session.flush()
    db.session.add(RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='ENTREGADO',
                                  forma_pago='EFECTIVO', monto_cobrado=10000))
    db.session.commit()
    return ruta


class TestLasRutasAtrasadasAparecen:

    def test_el_dashboard_de_hoy_trae_la_ruta_de_hace_tres_dias(self, app, client, db, almacen):
        vieja = _ruta_entregada(db, almacen, dias_atras=3)
        hoy = _ruta_entregada(db, almacen, dias_atras=0)
        d = client.get('/api/rutas/liquidacion/dashboard',
                       headers=_jwt(app, _usuario(db))).get_json()
        assert [r['id'] for r in d['rutas']] == [hoy.id]
        [a] = d['rutas_atrasadas']
        assert a['id'] == vieja.id and a['atrasada'] is True and a['dias_rezago'] == 3
        # No suman a los totales del rango.
        assert d['resumen']['total_recaudado'] == 10000

    def test_la_pantalla_la_pinta_en_pendientes(self, app, client, db, almacen, tmp_path):
        _ruta_entregada(db, almacen, dias_atras=3)
        d = client.get('/api/rutas/liquidacion/dashboard',
                       headers=_jwt(app, _usuario(db))).get_json()
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqDashboard = DASH;
            liqCargarPendientes();
            return { html: document.getElementById('liq-lista-pendientes').innerHTML };
        """, globales={'DASH': d})
        txt = visible(out['html'])
        assert 'Atrasada · 3 días' in txt, txt
        assert 'Sin rutas por liquidar' not in txt


def _con_bulto(db, t, r):
    from app.models.bulto import Bulto
    db.session.add(Bulto(tarea_id=t.id, codigo_barras=f'B-{uuid.uuid4().hex[:8]}', tipo='Caja',
                         numero=1, total=1, estado='RECHAZADO', ruta_despacho_id=r.ruta_id))
    db.session.commit()


class TestElBotonDeEnviarNoPrometeUnaNC:

    def test_faltante_total_no_deja_nada_por_enviar(self, app, client, db, almacen):
        t, r, _d = _faltante_total(db, almacen, liquidada=True)
        _con_bulto(db, t, r)
        d = client.get(f'/api/rutas/{r.ruta_id}/planilla',
                       headers=_jwt(app, _usuario(db))).get_json()
        [p] = d['paradas']
        assert p['pendiente_siesa'] == []

    def test_la_planilla_no_pinta_el_boton_y_si_con_la_nc_pendiente(self, app, client, db,
                                                                    almacen, tmp_path):
        t, r, dev = _faltante_total(db, almacen, liquidada=True)
        _con_bulto(db, t, r)

        def pintar():
            d = client.get(f'/api/rutas/{r.ruta_id}/planilla',
                           headers=_jwt(app, _usuario(db))).get_json()
            return _node(tmp_path, ['util.js', 'rutas.js'], {'/planilla': d}, f"""
                await _cargarPlanilla({r.ruta_id});
                return {{ html: document.getElementById('modal-planilla-body').innerHTML }};
            """)['html']
        assert 'Enviar a Siesa' not in pintar()
        dev.estado = 'ABIERTA'          # sin contar: la NC sí va a salir
        db.session.commit()
        assert 'Enviar a Siesa' in pintar()

    def test_una_devolucion_sin_contar_si(self, db, almacen):
        from app.models.devolucion_cliente import DevolucionCliente
        from app.services import politica_cobro as pc
        _t, r, d = _faltante_total(db, almacen, liquidada=True)
        d.estado = 'ABIERTA'
        db.session.commit()
        assert pc.documentos_pendientes(r) == ['NC']


def _detalle(client, app, db, ruta_id, usuario=None):
    return client.get(f'/api/rutas/{ruta_id}/liquidacion-detalle',
                      headers=_jwt(app, usuario or _usuario(db))).get_json()


class TestLaTarjetaDeLaParada:

    def _pintar(self, tmp_path, det):
        return _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqDetalleRuta = DET;
            _liqRenderDetalle();
            return { html: document.getElementById('liq-modal-body').innerHTML };
        """, globales={'DET': det})

    def test_un_recibo_sin_verificar_se_dice_y_no_como_listo(self, app, client, db, almacen,
                                                             tmp_path):
        ruta = _ruta_entregada(db, almacen, liquidada=True)
        from app.models.recaudo_entrega import RecaudoEntrega
        rec = RecaudoEntrega.query.filter_by(ruta_id=ruta.id).one()
        rec.siesa_rc_triggered = True
        rec.siesa_rc_resultado = 'SIN_VERIFICAR'
        db.session.commit()
        det = _detalle(client, app, db, ruta.id)
        [x] = det['recaudos']
        assert x['rc_sin_verificar'] is True and x['rc_llego'] is False
        txt = visible(self._pintar(tmp_path, det)['html'])
        assert 'RC ✓' not in txt
        assert 'Recibo de caja sin verificar' in txt and 'Sí está en Siesa' in txt
        assert not codigos_crudos(self._pintar(tmp_path, det)['html'])

    def test_quien_no_liquida_no_ve_registrar_cobro(self, app, client, db, almacen, tmp_path):
        ruta = _ruta_entregada(db, almacen, liquidada=True)
        jefe = _usuario(db, rol='jefe_almacen', email=f'jefe_{uuid.uuid4().hex[:4]}@t.co')
        det = _detalle(client, app, db, ruta.id, usuario=jefe)
        assert det['permisos']['liquidar'] is False
        txt = visible(self._pintar(tmp_path, det)['html'])
        assert 'Registrar Cobro' not in txt
        assert 'lo registra quien liquida' in txt
        det_admin = _detalle(client, app, db, ruta.id)
        assert det_admin['permisos']['liquidar'] is True
        assert 'Registrar Cobro' in visible(self._pintar(tmp_path, det_admin)['html'])


class TestLaVistaPreviaDelRC:

    def _preview(self, tmp_path, resta, monto=580000, ret=12604.9):
        pv = {'retenciones_disponibles': [{'tipo': 'RETEFUENTE_2.5', 'nombre': 'RF',
                                           'puc': '13551501', 'monto_estimado': ret}],
              'rc_resta_retencion': resta}
        return _node(tmp_path, ['util.js', 'liquidacion.js'], {}, f"""
            const panel = document.getElementById('liq-cobro-panel-5');
            panel.dataset.preview = JSON.stringify(PV);
            document.getElementById('liq-monto-5').value = '{monto}';
            document.querySelectorAll = (sel) => sel.includes('liq-ret-check-5')
              ? [{{ value: 'RETEFUENTE_2.5' }}] : [];
            _liqActualizarBloqueoRetencion = () => {{}};
            liqPreviewCobro(5);
            return {{ html: document.getElementById('liq-preview-5').innerHTML }};
        """, globales={'PV': pv})

    def test_parcial_no_resta_la_retencion_otra_vez(self, tmp_path):
        txt = visible(self._preview(tmp_path, resta=False)['html'])
        assert 'RC por $580.000' in txt, txt
        assert 'ya viene neto' in txt

    def test_entregado_si(self, tmp_path):
        txt = visible(self._preview(tmp_path, resta=True)['html'])
        assert 'RC por $567.395' in txt, txt


class TestElDesgloseRotulaLaCondicion:

    def test_la_declarada_es_la_del_pedido_y_la_de_cobro_dice_su_fuente(self, app, client, db,
                                                                          almacen):
        from app.models.packing import TareaPacking
        from app.models.recaudo_entrega import RecaudoEntrega
        ruta = _ruta_entregada(db, almacen)
        rec = RecaudoEntrega.query.filter_by(ruta_id=ruta.id).one()
        t = db.session.get(TareaPacking, rec.tarea_id)
        t.cond_pago = 'C04'          # el pedido declara crédito a 30
        t.cond_pago_fe = 'C02'       # la factura salió a 1 día (contado)
        db.session.commit()
        d = client.get('/api/rutas/liquidacion/desglose',
                       headers=_jwt(app, _usuario(db))).get_json()['recaudos']
        assert d['condicion_declarada'] == {'crédito real (C04)': 1}
        assert d['condicion_de_cobro'] == {'contado (C02, de la factura)': 1}
