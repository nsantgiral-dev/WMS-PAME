"""Re-confirmar una parada no pierde la hora del teléfono anterior (QA e2e
2026-09-24, menor).

`confirmar_parada` pisa `ts_dispositivo`/`ts_desfase_s`/`via_cola` con los de
la nueva confirmación. Lo que decía antes quedaba solo en… ningún lado: el
EDITAR de la bitácora guardaba montos y estados, no la hora. Ahora esos tres
campos entran al EDITAR como el resto (`_CAMPOS_PARADA`).
"""
from datetime import datetime, timedelta

from tests.test_qa_e2e_flota_20260924 import _DATA_URL, _ruta_con_parada, mundo  # noqa: F401


def _confirmar(ruta, t, m, ts):
    from app.services.ruta_service import RutaService
    RutaService.confirmar_parada(ruta.id, t.id, m.u_cond.id, {
        'version_formulario': 3, 'estado_entrega': 'RECHAZADO',
        'motivo_rechazo': 'CLIENTE_CERRADO', 'observaciones': 'cerrado',
        'monto_cobrado': 0, 'foto_entrega': _DATA_URL,
        'geo': {'fuente': 'sin_dato', 'motivo': 'sin_senal'},
        'ts_dispositivo': ts.isoformat() + 'Z', 'ts_envio': ts.isoformat() + 'Z',
        'via_cola': True})


def test_el_editar_guarda_la_hora_anterior(db, mundo):  # noqa: F811
    from app.models.bitacora import BitacoraAccion
    ruta, t = _ruta_con_parada(db, mundo, estado='EN_TRANSITO')
    primera = datetime.utcnow() - timedelta(hours=3)
    _confirmar(ruta, t, mundo, primera)
    _confirmar(ruta, t, mundo, datetime.utcnow())
    [ed] = BitacoraAccion.query.filter_by(accion='EDITAR', entidad='RecaudoEntrega').all()
    assert 'ts_dispositivo' in ed.antes, ed.antes
    assert ed.antes['ts_dispositivo'].startswith(primera.isoformat()[:16]), ed.antes
    assert ed.antes['ts_dispositivo'] != ed.despues['ts_dispositivo']
