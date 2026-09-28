"""
Validación de la plata (2026-09-26) — P2/P3 menores, cada uno con su caso:

- La salud avisa si no hay liquidador o líder de cartera activos.
- Cartera: un faltante de retorno es DEUDA declarada en RECHAZADO y en
  PARCIAL por igual (decisión pendiente del dueño sobre quién lo paga).
- «No entró» al resolver un RC limpia la marca de «cobro de otro mes».
- La retención que espera un RC que espera su NC no se reprograma cada 2 min.
- El resumen de la retención PARCIAL dice ruta, pedido y conductor.
"""
import json
from datetime import datetime

import pytest

from tests.test_cartera_retencion import _jwt, _recaudo, _tarea, _usuario
from tests.test_e2e_total_20260925 import _faltante_total


class TestLosRolesDeLaPlata:

    def test_sin_liquidador_ni_lider_se_avisa(self, db):
        from app.services.permisos_liquidacion import quien_opera_la_plata
        d = quien_opera_la_plata()
        assert d['por_rol'] == {'liquidador': 0, 'lider_cartera': 0}
        assert len(d['avisos']) == 2
        _usuario(db, 'liquidador')
        _usuario(db, 'lider_cartera')
        assert quien_opera_la_plata()['avisos'] == []

    def test_la_salud_lo_publica(self, app, client, db):
        r = client.get('/api/health/siesa', headers=_jwt(app, _usuario(db, 'admin')))
        d = r.get_json()
        assert d['roles_de_la_plata']['por_rol']['liquidador'] == 0
        assert any('liquidador' in a for a in d['advertencias'])


class TestElFaltanteDeRetornoEsDeuda:

    def _fila(self, t, saldo):
        return {'f353_id_tipo_docto_cruce': t.fe_tipo, 'f353_consec_docto_cruce': t.fe_consec,
                'f353_total_db': saldo, 'f353_total_cr': 0, 'f353_fecha': '20260901',
                'f353_fecha_vcto': '20260930', 'f201_id_sucursal': '001'}

    def test_rechazada_contada_en_cero(self, db, almacen):
        from app.services import cartera_service as cs
        t, r, _d = _faltante_total(db, almacen)
        [e] = cs.clasificar_filas([self._fila(t, 152_320)])
        assert e['clase'] == cs.DEUDA and e.get('faltante_de_retorno') is True
        assert float(e['cuenta']) == 152_320

    def test_rechazada_sin_contar_sigue_devuelta(self, db, almacen):
        from app.models.devolucion_cliente import DevolucionCliente
        from app.services import cartera_service as cs
        t, r, d = _faltante_total(db, almacen)
        d.estado = 'ABIERTA'
        db.session.commit()
        [e] = cs.clasificar_filas([self._fila(t, 152_320)])
        assert e['clase'] == cs.DEVUELTA and not e.get('faltante_de_retorno')


class TestResolverRcNoEntroLimpiaLaMarca:

    def test_no_entro(self, db, almacen):
        from app.services.liquidacion_service import LiquidacionService
        t = _tarea(db, almacen, '003-PD-7001', cond='C02', valor=10_000)
        r = _recaudo(db, t, estado='ENTREGADO', monto=10_000)
        r.siesa_rc_triggered = True
        r.rc_cobro_otro_mes = datetime(2026, 8, 31).date()
        db.session.commit()
        LiquidacionService.resolver_recibo_sin_verificar(r.id, usuario_id=1, entro=False,
                                                         motivo='no está')
        db.session.refresh(r)
        assert r.rc_cobro_otro_mes is None and r.siesa_rc_triggered is False


class TestLaRetencionQueEsperaUnaNcNoGiraCadaDosMinutos:

    def test_espera_lo_mismo_que_el_rc(self, db, almacen):
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        t = _tarea(db, almacen, '003-PD-7002', cond='C02', valor=10_000)
        r = _recaudo(db, t, estado='PARCIAL', monto=5_000)
        r.motivo_descuento, r.retencion_confirmada = 'RETEFUENTE_2.5', True
        SiesaJob.encolar(tipo='RECIBO_CAJA', payload={'recaudo_id': r.id, 'depende_de_nc': True},
                         referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        dc = SiesaJob.encolar(tipo='DOCUMENTO_CONTABLE_RET',
                              payload={'recaudo_id': r.id, 'cuenta_puc': '13551501',
                                       'tipo_retencion': 'RETEFUENTE_2.5'},
                              referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        db.session.commit()
        with pytest.raises(sjs.DependenciaPendiente) as e:
            sjs._ejecutar_job(dc)
        assert e.value.espera_minutos == 30 and 'nota crédito' in str(e.value)


class TestElAvisoDeLaRetencionParcialDiceDonde:

    def test_ruta_pedido_y_conductor(self):
        from app.services import devolucion_ruta as dr
        a = {'sin_contar_48h': [], 'sin_contar_24h': [], 'nc_sin_aprobar_3d': [],
             'nc_anuladas': [], 'rc_esperando_nc_48h': [],
             'retencion_parcial_sin_contar_24h': [
                 {'ruta_id': 12, 'pedido': 'PD1003', 'conductor': 'Ana'}]}
        [linea] = dr.lineas_de_aviso(a)
        assert 'ruta 12 · PD1003 · Ana' in linea
