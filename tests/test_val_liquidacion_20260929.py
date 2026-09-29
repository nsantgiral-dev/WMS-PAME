"""
Validación crítica de la liquidación completa (01d1ba41..99734a79, 2026-09-29).

Cada test reproduce un hallazgo P1 con `xfail(strict=True)`: se pone verde
(y por lo tanto rojo por `strict`) el día que se arregle.
"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tests._envio_liq import cartera_en


@pytest.fixture
def parada_transferencia(db, almacen):
    """Una parada de contado pagada por transferencia, en una ruta entregada."""
    from app.models.conductor import Conductor
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import RutaDespacho
    c = Conductor(nombre='C', cedula=f'C{uuid.uuid4().hex[:8]}', activo=True)
    db.session.add(c)
    db.session.flush()
    ruta = RutaDespacho(conductor_id=c.id, tipo_ruta='Urbana', estado='ENTREGADA')
    db.session.add(ruta)
    db.session.flush()
    t = TareaPacking(codigo=f'PK-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                     almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
                     consec_docto_pedido_siesa=1502, numero_pedido_siesa='PD1502',
                     cliente='FERRETERÍA X')
    db.session.add(t)
    db.session.flush()
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='ENTREGADO',
                       forma_pago='TRANSFERENCIA', monto_cobrado=50000,
                       referencia_pago='REF12345')
    db.session.add(r)
    db.session.commit()
    return r


def _mc():
    mc = MagicMock()
    mc.trigger_recibo_caja.return_value = {'codigo': 0}
    return mc


class TestElTopeDeEsperaCuentaDesdeQueSeEsperaASiesa:
    """P1 · `envio_liquidacion._esperar_o_declarar` mide el tope de 24 h desde
    `job.fecha_creacion`. Un recibo que esperó legítimamente —la verificación
    del banco (tesorería lo ve al otro día) o la nota crédito de su devolución
    (bodega cuenta mañana)— tiene más de 24 h de creado cuando por fin le toca
    leer la cartera. La PRIMERA vez que Siesa titubea (circuito abierto, un
    timeout) ya no espera: `DatoQueFalta` → FALLIDO sin reintento, con un
    mensaje que dice «lleva 30 h así» (es falso: llevaba 30 h esperando al
    banco) y manda a «avisarle a sistemas». El recibo no sale solo, la retención
    que lo espera cae detrás (`_rc_vivo is None` → ErrorDeterminista) y la
    factura queda abierta en Siesa con la plata ya en la empresa.

    «Siesa caído = se para todo» dice ESPERAR, no declarar a la primera."""

    def test_verificado_en_el_banco_tras_30_h_y_siesa_titubea_una_vez(
            self, app, db, parada_transferencia):
        from app.services import verificacion_banco as vb
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        from app.models.siesa_job import SiesaJob
        r = parada_transferencia
        job = SiesaJob.encolar('RECIBO_CAJA', {
            'recaudo_id': r.id, 'tipo_docto_fe': 'FE', 'consec_fe': '17062',
            'forma_pago': 'TRANSFERENCIA', 'monto': 50000},
            referencia_tipo='RecaudoEntrega', referencia_id=r.id)
        db.session.commit()
        # Encolado ayer; esperó al banco (DependenciaPendiente sin tope) toda la
        # noche. Hoy tesorería lo vio.
        job.fecha_creacion = datetime.utcnow() - timedelta(hours=30)
        db.session.commit()
        vb.verificar(r.id, usuario_id=None, encontrada=True)
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, Exception('timeout de lectura'))
            # Hoy: Siesa titubea UNA vez. Lo correcto es esperar sin gastar
            # intento; hoy se declara «falta un dato» y el recibo muere.
            with pytest.raises(DependenciaPendiente):
                _ejecutar_job(job)
        mc.trigger_recibo_caja.assert_not_called()
