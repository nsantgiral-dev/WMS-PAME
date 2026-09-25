"""
Las dos puertas de recuperación del despacho que escriben en Siesa, con
guarda (P1-8, 2026-09-25).

- `facturar-rm-manual`: la remisión la DIGITA una persona y el WMS no la puede
  verificar contra Siesa (`f460_*` no existe en la API: no hay cómo preguntar
  si esa remisión es de este pedido). Una factura sobre la remisión equivocada
  se anula con nota crédito. Se exige la **confirmación explícita** —el
  documento repetido tal cual— y un motivo, y queda FORZAR en la bitácora.
- `/despacho_parcial/<id>/despachar` (declarada BORRAR en DEUDA_SIN_UI, sin
  la idempotencia de la vía viva): mientras exista, no corre a la vez que la
  cola de Siesa ni que otro clic — toma el lock de la DLQ.
"""
import contextlib
from unittest.mock import patch

from tests.test_cartera_retencion import _jwt, _usuario


def _tarea(db, almacen):
    import uuid
    from app.models.packing import TareaPacking
    t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='VERIFICADO',
                     almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                     consec_docto_pedido_siesa=31, numero_pedido_siesa=f'PD{uuid.uuid4().hex[:5]}')
    db.session.add(t)
    db.session.commit()
    return t


class TestFacturarSobreRemisionDigitada:

    def test_sin_repetir_el_documento_no_sale(self, app, client, db, almacen):
        t = _tarea(db, almacen)
        with patch('app.services.despacho_parcial_service.DespachoParialService.'
                   'facturar_rm_con_consec') as f:
            r = client.post(f'/api/despacho_parcial/{t.id}/facturar-rm-manual',
                            headers=_jwt(app, _usuario(db)),
                            json={'tipo_rm': 'rm', 'consec_rm': 1567, 'motivo': 'x'})
        assert r.status_code == 400 and r.get_json()['requiere_confirmacion'] == 'RM-1567'
        f.assert_not_called()

    def test_sin_motivo_no_sale(self, app, client, db, almacen):
        t = _tarea(db, almacen)
        with patch('app.services.despacho_parcial_service.DespachoParialService.'
                   'facturar_rm_con_consec') as f:
            r = client.post(f'/api/despacho_parcial/{t.id}/facturar-rm-manual',
                            headers=_jwt(app, _usuario(db)),
                            json={'tipo_rm': 'RM', 'consec_rm': 1567, 'confirmacion': 'RM-1567'})
        assert r.status_code == 400
        f.assert_not_called()

    def test_confirmado_y_con_motivo_queda_en_la_bitacora(self, app, client, db, almacen):
        from app.models.bitacora import BitacoraAccion
        from app.services.bitacora import FORZADO_FE_SOBRE_RM_DIGITADA
        t = _tarea(db, almacen)
        with patch('app.services.despacho_parcial_service.DespachoParialService.'
                   'facturar_rm_con_consec', return_value={'rm': 'RM-1567'}) as f:
            r = client.post(f'/api/despacho_parcial/{t.id}/facturar-rm-manual',
                            headers=_jwt(app, _usuario(db)),
                            json={'tipo_rm': 'RM', 'consec_rm': 1567,
                                  'confirmacion': 'rm-1567', 'motivo': 'la RM está en Siesa'})
        assert r.status_code == 200, r.get_json()
        f.assert_called_once()
        [b] = BitacoraAccion.query.filter_by(accion='FORZAR').all()
        assert b.despues['forzado'] == FORZADO_FE_SOBRE_RM_DIGITADA
        assert b.despues['remision'] == 'RM-1567'


class TestDespacharManualNoCorreConLaCola:

    def test_con_la_cola_ocupada_responde_409_sin_tocar_siesa(self, app, client, db, almacen):
        t = _tarea(db, almacen)

        @contextlib.contextmanager
        def _ocupado(*a, **k):
            yield False
        with patch('app.utils.lock.advisory_lock', _ocupado), \
                patch('app.services.despacho_parcial_service.DespachoParialService.'
                      'despachar_parcial') as d:
            r = client.post(f'/api/despacho_parcial/{t.id}/despachar',
                            headers=_jwt(app, _usuario(db)), json={'cantidades': {'X': 1}})
        assert r.status_code == 409
        d.assert_not_called()
