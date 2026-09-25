"""
Parada tardía: el formulario de la oficina (tanda 2 · B) y el aviso de la
retención de una PARCIAL sin contar (tanda 2 · D). 2026-09-25.

**B — la clase:** *una parada de una ruta cerrada que nadie puede registrar
con lo que de verdad pasó*. La única salida era «cerrar lo que falta» (todo
rechazado) o esperar la cola del conductor. Ahora:

- Liquidación muestra las paradas sin gestionar por ruta (cliente, valor, hace
  cuánto) y no liquida hasta resolverlas; el texto guía pide primero al
  conductor que abra la app con señal.
- Quien liquida (`puede_liquidar`) las registra con el formulario: mismas
  validaciones que el conductor (contado/crédito, comprobante, evidencia de
  «no pagó y se quedó»), motivo obligatorio, FORZAR en la bitácora y la parada
  marcada `registrada_por_oficina` (señal en Liquidación y en la jornada).
- Lo que el teléfono mande después **no pisa** lo registrado: queda en
  `version_conductor` con `diferencia_conductor`.
- Más de 24 h sin gestionar → resumen diario.

**D:** la retención de una PARCIAL espera el conteo de su devolución (se queda
así); más de 24 h → resumen diario y señal en Liquidación.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from tests.test_cartera_retencion import _jwt, _usuario
from tests.test_parada_tardia import mundo  # noqa: F401 — fixture
from tests.test_sin_codigos_en_pantalla import _node, codigos_crudos, visible

FOTO = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=='


def _cerrar(db, f, uc):
    from app.services.ruta_service import RutaService
    RutaService.entregar_ruta(f.ruta_id, {'bultos': []}, uc.id)


def _url(f):
    return f'/api/rutas/{f.ruta_id}/paradas/{f.packing_id}/confirmar'


def _detalle(client, app, f, u):
    return client.get(f'/api/rutas/{f.ruta_id}/liquidacion-detalle', headers=_jwt(app, u))


def _oficina(**k):
    base = {'motivo_tardia': 'El conductor lo confirmó por teléfono', 'version_formulario': 4,
            'observaciones': 'Registrada por la oficina'}
    base.update(k)
    return base


# ═════════════════════════════════════════════════════════════════════════════
# B.1 · Liquidación muestra lo que falta y no liquida
# ═════════════════════════════════════════════════════════════════════════════

class TestLiquidacionMuestraLoQueFalta:
    def test_el_detalle_trae_las_paradas_sin_gestionar(self, app, client, db, mundo):
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        r = _detalle(client, app, f, ad)
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        [p] = d['paradas_sin_gestionar']
        assert p['tarea_id'] == f.packing_id and 'cliente' in p and 'valor' in p
        assert p['horas'] is not None and p['horas'] >= 0
        assert p['items'] and all(it['cantidad_pedida'] for it in p['items'])
        assert p['cobro_contraentrega'] is True       # C02: se cobra al entregar
        assert d['permisos']['registrar_parada_tardia'] is True
        assert d['formulario_oficina']['version_formulario'] >= 4

    def test_el_dashboard_cuenta_las_que_faltan(self, app, client, db, mundo):
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo().isoformat()
        d = client.get(f'/api/rutas/liquidacion/dashboard?fecha_desde={hoy}&fecha_hasta={hoy}',
                       headers=_jwt(app, ad)).get_json()
        filas = d['rutas'] + d['rutas_atrasadas']
        assert next(x for x in filas if x['id'] == f.ruta_id)['paradas_sin_gestionar'] == 1

    def test_liquidar_se_niega_y_guia(self, db, mundo):
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        with pytest.raises(ValueError, match='Pídale al conductor que abra la app con señal'):
            RutaService.liquidar_ruta(f.ruta_id, usuario_id=ad.id)


# ═════════════════════════════════════════════════════════════════════════════
# B.3 · El formulario de la oficina: mismas validaciones, motivo, bitácora
# ═════════════════════════════════════════════════════════════════════════════

class TestLaOficinaRegistra:
    def test_entregado_en_efectivo_queda_marcado_y_en_la_bitacora(self, app, client, db, mundo):
        from app.models.bitacora import BitacoraAccion
        from app.models.recaudo_entrega import RecaudoEntrega
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json=_oficina(estado_entrega='ENTREGADO', forma_pago='EFECTIVO',
                                      monto_cobrado=0))
        # Contado sin plata: la MISMA guarda del conductor.
        assert r.status_code == 400 and 'se cobra al entregar' in r.get_json()['error']
        from app.models.packing import TareaPacking
        valor = float(db.session.get(TareaPacking, f.packing_id).valor_factura or 0)
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json=_oficina(estado_entrega='ENTREGADO', forma_pago='EFECTIVO',
                                      monto_cobrado=valor or 1000))
        assert r.status_code == 200, r.get_json()
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.registrada_por_oficina is True
        [b] = BitacoraAccion.query.filter_by(accion='FORZAR').all()
        assert b.despues['registrada_por_oficina'] is True
        assert b.motivo == 'El conductor lo confirmó por teléfono'
        assert b.usuario_id == ad.id
        # Ya no falta nada en la liquidación, y la señal está.
        d = _detalle(client, app, f, ad).get_json()
        assert d['paradas_sin_gestionar'] == []
        [x] = d['recaudos']
        assert 'registrada_por_oficina' in [s['clave'] for s in x['senales']]

    def test_sin_motivo_no_entra(self, app, client, db, mundo):
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json={**_oficina(estado_entrega='RECHAZADO', motivo_rechazo='NO_PAGO'),
                              'motivo_tardia': '  '})
        assert r.status_code == 400 and 'motivo' in r.get_json()['error'].lower()

    def test_credito_sobre_contado_se_rechaza_como_al_conductor(self, app, client, db, mundo):
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json=_oficina(estado_entrega='ENTREGADO', forma_pago='CREDITO',
                                      monto_cobrado=0))
        assert r.status_code == 400 and 'contado contraentrega' in r.get_json()['error']

    def test_transferencia_sin_referencia_se_rechaza(self, app, client, db, mundo):
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        from app.models.packing import TareaPacking
        valor = float(db.session.get(TareaPacking, f.packing_id).valor_factura or 1000)
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json=_oficina(estado_entrega='ENTREGADO', forma_pago='TRANSFERENCIA_BBVA',
                                      monto_cobrado=valor))
        assert r.status_code == 400 and 'referencia' in r.get_json()['error']

    def test_no_pago_y_se_quedo_exige_foto_y_no_pide_gps(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        sin_foto = client.post(_url(f), headers=_jwt(app, ad),
                               json=_oficina(estado_entrega='RECHAZADO',
                                             motivo_rechazo='NO_PAGO_SE_QUEDO'))
        assert sin_foto.status_code == 400 and 'foto' in sin_foto.get_json()['error']
        r = client.post(_url(f), headers=_jwt(app, ad),
                        json=_oficina(estado_entrega='RECHAZADO', motivo_rechazo='NO_PAGO_SE_QUEDO',
                                      foto_entrega=FOTO))
        assert r.status_code == 200, r.get_json()
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.estado_entrega == 'ENTREGADO_SIN_PAGO' and rec.registrada_por_oficina

    def test_el_jefe_de_almacen_no_la_registra(self, app, client, db, mundo):
        f, uc, _ad = mundo
        _cerrar(db, f, uc)
        jefe = _usuario(db, rol='jefe_almacen')
        r = client.post(_url(f), headers=_jwt(app, jefe),
                        json=_oficina(estado_entrega='RECHAZADO', motivo_rechazo='NO_PAGO'))
        assert r.status_code == 400 and 'quien liquida' in r.get_json()['error']
        assert _detalle(client, app, f, jefe).get_json()['permisos']['registrar_parada_tardia'] is False

    def test_con_la_ruta_en_camino_la_oficina_no_registra(self, db, mundo):
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        with pytest.raises(ValueError, match='solo con la ruta ya cerrada'):
            RutaService.confirmar_parada(f.ruta_id, f.packing_id, ad.id,
                                         {'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO',
                                          'observaciones': 'x'},
                                         motivo_tardia='x', por_oficina=True)


# ═════════════════════════════════════════════════════════════════════════════
# B.4 · Lo del teléfono después no pisa lo de la oficina
# ═════════════════════════════════════════════════════════════════════════════

def _registrada_no_pago(app, client, db, f, uc, ad):
    _cerrar(db, f, uc)
    r = client.post(_url(f), headers=_jwt(app, ad),
                    json=_oficina(estado_entrega='RECHAZADO', motivo_rechazo='NO_PAGO'))
    assert r.status_code == 200, r.get_json()


class TestLaVersionDelConductor:
    def test_si_difiere_no_pisa_y_queda_para_revisar(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        f, uc, ad = mundo
        _registrada_no_pago(app, client, db, f, uc, ad)
        r = client.post(_url(f), headers=_jwt(app, uc),
                        json={'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                              'monto_cobrado': 50000, 'via_cola': True, 'version_formulario': 4})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['version_conductor']['difiere'] is True
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.estado_entrega == 'RECHAZADO' and float(rec.monto_cobrado or 0) == 0
        assert rec.diferencia_conductor is True
        assert rec.version_conductor['estado_entrega'] == 'ENTREGADO'
        assert 'estado_entrega' in rec.version_conductor['campos_que_difieren']
        d = _detalle(client, app, f, ad).get_json()
        claves = [s['clave'] for s in d['recaudos'][0]['senales']]
        assert 'diferencia_con_el_conductor' in claves

    def test_si_coincide_queda_anotado_que_la_confirmo(self, app, client, db, mundo):
        from app.models.recaudo_entrega import RecaudoEntrega
        f, uc, ad = mundo
        _registrada_no_pago(app, client, db, f, uc, ad)
        r = client.post(_url(f), headers=_jwt(app, uc),
                        json={'estado_entrega': 'RECHAZADO', 'motivo_rechazo': 'NO_PAGO',
                              'forma_pago': 'EFECTIVO', 'observaciones': 'no pagó',
                              'via_cola': True})
        assert r.status_code == 200
        rec = RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()
        assert rec.diferencia_conductor is False and rec.version_conductor_en is not None
        d = _detalle(client, app, f, ad).get_json()
        claves = [s['clave'] for s in d['recaudos'][0]['senales']]
        assert 'registrada_por_oficina' not in claves
        assert 'diferencia_con_el_conductor' not in claves

    def test_el_servicio_tampoco_deja_pisarla(self, app, client, db, mundo):
        """Un guard en la ruta protege la ruta; en el servicio, la operación."""
        from app.services.ruta_service import RutaService
        f, uc, ad = mundo
        _registrada_no_pago(app, client, db, f, uc, ad)
        with pytest.raises(ValueError, match='la registró la oficina'):
            RutaService.confirmar_parada(f.ruta_id, f.packing_id, uc.id,
                                         {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO',
                                          'monto_cobrado': 1},
                                         motivo_tardia='La confirmación llegó por la cola')

    def test_la_jornada_la_nombra(self, app, client, db, mundo):
        from app.models.conductor import Conductor
        from app.utils.fecha import dia_operativo
        f, uc, ad = mundo
        _registrada_no_pago(app, client, db, f, uc, ad)
        c = Conductor.query.filter_by(usuario_id=uc.id).one()
        r = client.get(f'/api/jornada?dia={dia_operativo().isoformat()}&conductor_id={c.id}',
                       headers=_jwt(app, ad))
        assert r.status_code == 200, r.get_json()
        eventos = [e for e in r.get_json()['eventos'] if e['tipo'].startswith('parada')]
        assert [e['tipo'] for e in eventos] == ['parada_oficina']
        assert eventos[0]['detalle']['registrada_por_oficina'] is True


# ═════════════════════════════════════════════════════════════════════════════
# B.5 · Más de 24 h sin gestionar → resumen diario
# ═════════════════════════════════════════════════════════════════════════════

class TestElResumenLaAvisa:
    def test_mas_de_un_dia(self, db, mundo):
        from app.services import parada_tardia as pt
        f, uc, _ad = mundo
        _cerrar(db, f, uc)
        assert pt.lineas_de_aviso() == []
        manana = datetime.utcnow() + timedelta(hours=25)
        [linea] = pt.lineas_de_aviso(ahora=manana)
        assert '1 parada(s) sin gestionar' in linea and str(f.ruta_id) in linea

    def test_entra_al_correo(self, db, monkeypatch):
        from app.services import alertas_service as al
        from app.services import parada_tardia as pt
        monkeypatch.setattr(pt, 'lineas_de_aviso', lambda ahora=None: ['LINEA-PARADAS'])
        hoy = datetime.utcnow()
        assert 'LINEA-PARADAS' in al.avisos_sin_canal(hoy - timedelta(days=1), hoy)


# ═════════════════════════════════════════════════════════════════════════════
# B · La pantalla (Node, util.js real)
# ═════════════════════════════════════════════════════════════════════════════

class TestLaPantalla:
    def _pintar(self, tmp_path, det, extra=''):
        return _node(tmp_path, ['util.js', 'rutas.js', 'liquidacion.js'], {}, """
            _liqDetalleRuta = DET;
            _liqRenderDetalle();
            const html = document.getElementById('liq-modal-body').innerHTML;
            """ + extra + """
            return { html, extra: typeof EXTRA === 'undefined' ? '' : JSON.stringify(EXTRA) };
        """, globales={'DET': det})

    def test_el_bloque_guia_y_no_deja_liquidar(self, app, client, db, mundo, tmp_path):
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        det = _detalle(client, app, f, ad).get_json()
        det['paradas_sin_gestionar'][0]['cliente'] = '<img src=x onerror=alert(1)>'
        out = self._pintar(tmp_path, det)
        txt = visible(out['html'])
        assert 'Paradas sin gestionar (1)' in txt
        assert 'pídale al conductor que abra la app con señal' in txt.lower()
        assert 'Registrar desde la oficina' in txt
        assert '🔒 Liquidar — faltan 1 parada por gestionar' in txt
        assert 'LIQUIDAR EN WMS' not in txt
        assert '<img src=x' not in out['html']
        assert f'liqAbrirParadaTardia({f.ruta_id}, 0)' in out['html']
        assert not codigos_crudos(out['html'])

    def test_quien_no_liquida_no_ve_el_boton(self, app, client, db, mundo, tmp_path):
        f, uc, _ad = mundo
        _cerrar(db, f, uc)
        det = _detalle(client, app, f, _usuario(db, rol='jefe_almacen')).get_json()
        txt = visible(self._pintar(tmp_path, det)['html'])
        assert 'Registrar desde la oficina' not in txt
        assert 'La registra quien liquida la ruta' in txt

    def test_el_formulario_arma_lo_que_valida_el_servidor(self, app, client, db, mundo, tmp_path):
        f, uc, ad = mundo
        _cerrar(db, f, uc)
        det = _detalle(client, app, f, ad).get_json()
        out = self._pintar(tmp_path, det, """
            liqAbrirParadaTardia(1, 0);
            const form = document.getElementById('liq-tardia-0').innerHTML;
            const p = _liqDetalleRuta.paradas_sin_gestionar[0];
            document.getElementById('liq-tardia-res-0').value = 'NO_PAGO_SE_QUEDO';
            const sinMotivo = _liqDatosTardia(p, 0);
            document.getElementById('liq-tardia-motivo-0').value = 'Llamó el cliente';
            const seQuedo = _liqDatosTardia(p, 0);
            document.getElementById('liq-tardia-res-0').value = 'PARCIAL';
            document.getElementById('liq-tardia-forma-0').value = 'EFECTIVO';
            document.getElementById('liq-tardia-monto-0').value = '5000';
            document.getElementById('liq-tardia-it-0-0').value = '3';
            const parcial = _liqDatosTardia(p, 0);
            var EXTRA = { form, sinMotivo, seQuedo, parcial };
        """)
        e = __import__('json').loads(out['extra'])
        assert 'CREDITO' not in e['form'] and 'EXENTO' not in e['form']   # contado
        assert e['sinMotivo'] is None
        assert e['seQuedo']['estado_entrega'] == 'RECHAZADO'
        assert e['seQuedo']['motivo_rechazo'] == 'NO_PAGO_SE_QUEDO'
        assert e['seQuedo']['motivo_tardia'] == 'Llamó el cliente'
        assert e['seQuedo']['version_formulario'] == det['formulario_oficina']['version_formulario']
        assert e['parcial']['estado_entrega'] == 'PARCIAL' and e['parcial']['monto_cobrado'] == 5000
        assert e['parcial']['items_entregados'][0]['cantidad_entregada'] == 3


# ═════════════════════════════════════════════════════════════════════════════
# D · La retención de una PARCIAL espera el conteo
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def parcial_con_retencion(db, almacen):
    from app.models.conductor import Conductor
    from app.models.devolucion_cliente import DevolucionCliente
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    c = Conductor(nombre='C', cedula=f'C{uuid.uuid4().hex[:8]}', activo=True)
    db.session.add(c)
    db.session.flush()
    ruta = RutaDespacho(conductor_id=c.id, tipo_ruta='Urbana', estado='ENTREGADA',
                        fecha_entregada=datetime.utcnow())
    db.session.add(ruta)
    db.session.flush()
    t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                     almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                     consec_docto_pedido_siesa=77, numero_pedido_siesa='PD77')
    db.session.add(t)
    db.session.flush()
    hace = datetime.utcnow() - timedelta(hours=30)
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='PARCIAL',
                       forma_pago='EFECTIVO', monto_cobrado=9000, motivo_descuento='RETEIVA',
                       monto_descuento=300, retencion_confirmada=True, fecha_confirmacion=hace)
    db.session.add(r)
    db.session.flush()
    d = DevolucionCliente(codigo=f'DEVC-{uuid.uuid4().hex[:6]}', tarea_packing_id=t.id,
                          tipo_docto_fe='FEW', consec_fe='9', almacen_id=almacen.id,
                          estado='EN_CAMION', recaudo_entrega_id=r.id, fecha_creacion=hace)
    db.session.add(d)
    db.session.commit()
    return r, d


class TestLaRetencionDeUnaParcialEsperaElConteo:
    def test_se_ve_y_pasadas_24h_va_al_resumen(self, db, parcial_con_retencion):
        from app.services import devolucion_ruta as dr
        from app.services import senales_ruta as sr
        r, d = parcial_con_retencion
        esp = dr.retencion_esperando_conteo(r)
        assert esp['devolucion'] == d.id and esp['vencida'] is True and esp['horas'] >= 29
        a = dr.avisos()
        assert [x['recaudo_id'] for x in a['retencion_parcial_sin_contar_24h']] == [r.id]
        assert any('retención(es) de entregas parciales' in x for x in dr.lineas_de_aviso(a))
        [s] = [x for x in sr.senales_de_recaudo(r) if x['clave'] == 'retencion_espera_conteo']
        assert d.codigo in s['texto'] and 'pídale a bodega' in s['texto']

    def test_antes_de_24h_se_ve_pero_no_avisa(self, db, parcial_con_retencion):
        from app.services import devolucion_ruta as dr
        r, _d = parcial_con_retencion
        antes = r.fecha_confirmacion + timedelta(hours=2)
        assert dr.retencion_esperando_conteo(r, ahora=antes)['vencida'] is False
        assert dr.avisos(ahora=antes)['retencion_parcial_sin_contar_24h'] == []

    @pytest.mark.parametrize('cambio', ['rechazada', 'dc_salio', 'contada', 'sin_retencion',
                                        'entregado'])
    def test_lo_que_no_espera(self, db, parcial_con_retencion, cambio):
        from app.services import devolucion_ruta as dr
        r, d = parcial_con_retencion
        if cambio == 'rechazada':
            r.retencion_confirmada = False
        elif cambio == 'dc_salio':
            r.siesa_dc_triggered = True
        elif cambio == 'contada':
            d.estado = 'CONFIRMADA'
        elif cambio == 'sin_retencion':
            r.motivo_descuento = None
        else:
            r.estado_entrega = 'ENTREGADO'
        db.session.commit()
        assert dr.retencion_esperando_conteo(r) is None
        assert dr.avisos()['retencion_parcial_sin_contar_24h'] == []

    def test_una_retencion_pendiente_de_decidir_tambien_espera(self, db, parcial_con_retencion):
        from app.services import devolucion_ruta as dr
        r, _d = parcial_con_retencion
        r.retencion_confirmada = None
        db.session.commit()
        assert dr.retencion_esperando_conteo(r) is not None
