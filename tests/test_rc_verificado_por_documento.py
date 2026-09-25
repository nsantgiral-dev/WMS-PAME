"""
«¿El recibo entró?» se contesta por el DOCUMENTO, y el pre-flag solo se baja
con prueba positiva de que no entró (P0-4, 2026-09-25).

## El caso

Tras un POST de recibo de caja que falla, el ejecutor preguntaba
`esta_saldada` (saldo ≤ $0,5). Un RC de contado con retención sale **neto** y
el DC va después: tras un recibo que SÍ entró, la factura conserva el saldo de
la retención → «no está saldada» → «no entró» → se bajaba la bandera → la cola
mandaba el **segundo recibo**. Es el incidente RC-00002744 por otra puerta.

## La clase

*Se revierte un pre-flag (Regla 6) sin prueba positiva de que el documento no
entró.* Los handlers revertían ante cualquier excepción que no fuera un
timeout de lectura: un 502 de un proxy, una conexión cortada a mitad de la
respuesta, un JSON ilegible sobre un 200. En todos esos el documento pudo
haber entrado.

## Ahora

- El gateway distingue: `ConnektaNoEnviado` (4xx, `codigo != 0`, 429,
  circuito abierto, payload inválido) es la **única** prueba de que no entró.
  Todo lo demás es «no sé».
- El RC guarda el saldo de antes del POST en el job y, ante «no sé», compara:
  si bajó por el monto del recibo, entró. Si no, FALLIDO sin reintento,
  `SIN_VERIFICAR`, y una persona lo resuelve (`resolver-rc`).
- Trinquete AST: en `siesa_job_service`, toda reversión de un pre-flag dentro
  de un `except` está en un `except ConnektaNoEnviado`.
"""
import ast
import json
import pathlib
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════════════════════
# 1 · El gateway dice cuál es cuál
# ═════════════════════════════════════════════════════════════════════════════

def _gw():
    from app.services.connekta_gateway import ConnektaGateway
    gw = ConnektaGateway()
    gw.ikey, gw.itoken = 'k', 't'
    gw.modo_simulacion = False
    gw.modo_ensayo = False
    return gw


def _resp(status, cuerpo=None):
    r = MagicMock()
    r.status_code = status
    r.ok = 200 <= status < 300
    r.json.return_value = cuerpo if cuerpo is not None else {}
    r.text = json.dumps(cuerpo or {})
    r.headers = {}
    return r


class TestElGatewayClasifica:

    @pytest.mark.parametrize('status,cuerpo', [
        (400, {'codigo': 1, 'mensaje': 'el documento de cruce no existe'}),
        (422, {'error': 'x'}),
        (429, {}),
        (200, {'codigo': 1, 'mensaje': 'rechazado', 'detalle': 'x'}),
    ])
    def test_siesa_dijo_que_no_es_prueba_de_que_no_entro(self, app, status, cuerpo):
        from app.services.connekta_gateway import ConnektaNoEnviado, ConnektaRechazado
        with patch('app.services.connekta_gateway.requests.post',
                   return_value=_resp(status, cuerpo)):
            with pytest.raises(ConnektaRechazado) as e:
                _gw()._post('142888', 'API_v1_ReciboCaja', {})
        assert isinstance(e.value, ConnektaNoEnviado)

    @pytest.mark.parametrize('status', [500, 502, 504])
    def test_un_5xx_no_prueba_nada(self, app, status):
        from app.services.connekta_gateway import ConnektaNoEnviado
        with patch('app.services.connekta_gateway.requests.post',
                   return_value=_resp(status, {'x': 1})):
            with pytest.raises(Exception) as e:
                _gw()._post('142888', 'API_v1_ReciboCaja', {})
        assert not isinstance(e.value, ConnektaNoEnviado), (
            'un 502/504 de un proxy puede venir de una petición que SÍ se procesó')

    def test_una_conexion_cortada_no_prueba_nada(self, app):
        import requests
        from app.services.connekta_gateway import ConnektaNoEnviado
        with patch('app.services.connekta_gateway.requests.post',
                   side_effect=requests.exceptions.ChunkedEncodingError('cortada')):
            with pytest.raises(Exception) as e:
                _gw()._post('142888', 'API_v1_ReciboCaja', {})
        assert not isinstance(e.value, ConnektaNoEnviado)

    def test_el_circuito_abierto_y_el_payload_invalido_no_salieron(self):
        from app.services.connekta_gateway import (ConnektaCircuitOpenError, ConnektaNoEnviado,
                                                   ConnektaPayloadInvalido)
        assert issubclass(ConnektaCircuitOpenError, ConnektaNoEnviado)
        assert issubclass(ConnektaPayloadInvalido, ConnektaNoEnviado)
        assert issubclass(ConnektaPayloadInvalido, ValueError), 'los que ya lo atrapaban así'

    def test_la_forma_de_pago_sin_medio_no_sale(self, app):
        from app.services.connekta_gateway import ConnektaPayloadInvalido
        gw = _gw()
        gw.tipo_docto_recibo_caja = 'RC'
        with patch('app.services.connekta_gateway.requests.post') as post:
            with pytest.raises(ConnektaPayloadInvalido):
                gw.trigger_recibo_caja('900', '001', 1000, 'TRUEQUE', 'FEW', '1')
        post.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El recibo, por el documento
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def recaudo(db, almacen):
    import uuid
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
                     consec_docto_pedido_siesa=100)
    db.session.add(t)
    db.session.flush()
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='ENTREGADO',
                       forma_pago='EFECTIVO', monto_cobrado=978991.6,
                       # 2026-09-25 03:30 UTC = 24 de septiembre en Bogotá.
                       fecha_confirmacion=datetime(2026, 9, 25, 3, 30))
    db.session.add(r)
    db.session.commit()
    return r


def _job(db, r, monto=978991.6):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob.encolar('RECIBO_CAJA', {
        'recaudo_id': r.id, 'tercero_nit': '900', 'sucursal': '001', 'monto': monto,
        'forma_pago': 'EFECTIVO', 'tipo_docto_fe': 'FEW', 'consec_fe': '1416'},
        referencia_tipo='RecaudoEntrega', referencia_id=r.id)
    db.session.commit()
    return j


def _fila(db_, cr):
    return [{'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': '100',
             'f353_total_db': 1000000, 'f353_total_cr': cr}]


class TestElReciboSeVerificaPorElDocumento:

    def test_rc_00002744_con_retencion_el_saldo_que_queda_no_dice_que_no_entro(
            self, app, db, recaudo):
        """El caso: factura $1.000.000, RC neto $978.991,60 (la retención de
        $21.008,40 va después en el DC). El POST se corta; el recibo SÍ entró:
        la factura queda con $21.008,40 de saldo. La pregunta vieja («¿saldada?»)
        contestaba que no y la cola mandaba el segundo recibo."""
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja.side_effect = Exception('connection reset by peer')
            mc.get_cxc_general.side_effect = [_fila(db, 0), _fila(db, 978991.6)]
            res = _ejecutar_job(job)
        assert res == {'timeout_pero_exitoso': True, 'verificado_por': 'saldo'}
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_triggered is True
        assert recaudo.siesa_rc_resultado == 'ENVIADO'

    def test_el_saldo_de_antes_queda_escrito_en_el_job(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja.return_value = {'codigo': 0}
            mc.get_cxc_general.return_value = _fila(db, 0)
            _ejecutar_job(job)
        assert json.loads(job.payload)['saldo_antes_rc'] == 1000000

    def test_saldo_igual_es_no_se_sabe(self, app, db, recaudo):
        from app.services.connekta_gateway import ConnektaResultadoDesconocido
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja.side_effect = Exception('HTTP 504')
            mc.get_cxc_general.return_value = _fila(db, 0)
            with pytest.raises(ConnektaResultadoDesconocido, match='Liquidación'):
                _ejecutar_job(job)
        db.session.refresh(recaudo)
        assert recaudo.siesa_rc_triggered is True

    def test_sin_saldo_de_antes_no_hay_prueba(self, app, db, recaudo):
        from app.services.connekta_gateway import ConnektaResultadoDesconocido
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja.side_effect = Exception('timeout')
            mc.get_cxc_general.side_effect = [Exception('caída'), _fila(db, 978991.6)]
            with pytest.raises(ConnektaResultadoDesconocido):
                _ejecutar_job(job)

    def test_un_rechazo_explicito_baja_la_bandera_y_reintenta(self, app, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services.connekta_gateway import ConnektaRechazado
        from app.services.siesa_job_service import _run_dlq_jobs
        job = _job(db, recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc, \
                patch('app.services.siesa_job_service._crear_alerta_admin'):
            mc.trigger_recibo_caja.side_effect = ConnektaRechazado('codigo=1')
            mc.get_cxc_general.return_value = _fila(db, 0)
            _run_dlq_jobs()
        db.session.refresh(recaudo)
        job = db.session.get(SiesaJob, job.id)
        assert recaudo.siesa_rc_triggered is False
        assert job.estado == 'PENDIENTE' and job.intentos == 1

    def test_el_no_se_sabe_va_a_fallido_sin_reintento_y_no_cuenta_como_llegado(
            self, app, db, recaudo):
        from app.models.siesa_job import SiesaJob
        from app.services import politica_cobro as pc
        from app.services.siesa_job_service import _run_dlq_jobs
        job = _job(db, recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc, \
                patch('app.services.siesa_job_service._crear_alerta_admin'):
            mc.trigger_recibo_caja.side_effect = Exception('HTTP 502')
            mc.get_cxc_general.return_value = _fila(db, 0)
            _run_dlq_jobs()
        job = db.session.get(SiesaJob, job.id)
        db.session.refresh(recaudo)
        assert job.estado == 'FALLIDO' and job.intentos == 0
        assert recaudo.siesa_rc_resultado == 'SIN_VERIFICAR'
        assert pc.rc_llego_a_siesa(recaudo) is False


class TestElRCLlevaElDiaDelCobro:
    """P2 — Regla 5: `F357_FECHA_RECAUDO` y `F358_FECHA_CONSIGNACION` son el
    día (Bogotá) en que el conductor cobró, no el del envío del DLQ."""

    def test_el_ejecutor_pasa_el_dia_bogota_de_la_confirmacion(self, app, db, recaudo):
        from app.services.siesa_job_service import _ejecutar_job
        job = _job(db, recaudo)
        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.trigger_recibo_caja.return_value = {'codigo': 0}
            mc.get_cxc_general.return_value = _fila(db, 0)
            _ejecutar_job(job)
        assert mc.trigger_recibo_caja.call_args.kwargs['fecha_recaudo'] == '20260924'

    def test_el_payload_lleva_la_fecha_del_cobro_y_el_documento_la_de_hoy(self, app):
        gw = _gw()
        gw.modo_ensayo = True
        gw.tipo_docto_recibo_caja = 'RC'
        out = gw.trigger_recibo_caja('900', '001', 1000, 'TRANSFERENCIA', 'FEW', '1',
                                     referencia_pago='1234', fecha_recaudo='20260920')
        p = out['payload']
        caja = p['Caja'][0] if isinstance(p.get('Caja'), list) else p.get('Caja')
        header = next(v[0] for k, v in p.items() if isinstance(v, list) and v
                      and isinstance(v[0], dict) and 'F357_FECHA_RECAUDO' in v[0])
        assert header['F357_FECHA_RECAUDO'] == '20260920'
        assert header['F350_FECHA'] != '20260920'
        assert caja['F358_FECHA_CONSIGNACION'] == '20260920'

    def test_una_fecha_ilegible_cae_al_dia_del_documento(self, app):
        gw = _gw()
        gw.modo_ensayo = True
        gw.tipo_docto_recibo_caja = 'RC'
        p = gw.trigger_recibo_caja('900', '001', 1000, 'EFECTIVO', 'FEW', '1',
                                   fecha_recaudo='2026-09-20')['payload']
        header = next(v[0] for k, v in p.items() if isinstance(v, list) and v
                      and isinstance(v[0], dict) and 'F357_FECHA_RECAUDO' in v[0])
        assert header['F357_FECHA_RECAUDO'] == header['F350_FECHA']


# ═════════════════════════════════════════════════════════════════════════════
# 3 · El trinquete de clase
# ═════════════════════════════════════════════════════════════════════════════

#: Reversiones fuera de `except ConnektaNoEnviado` que se aceptan, con su
#: porqué. **Solo encoge.**
REVERSIONES_DECLARADAS = {
    'siesa_job_service.py::_ejecutar_con_preflag':
        'ENTRADA_OC, TRASLADO_AVERIAS y AJUSTE_CONTEO (frente de inventario): '
        'revierte ante toda excepción que no sea un timeout. Misma clase; '
        'pendiente de ese frente para no cambiarle el reintento de los ajustes.',
}

_TIPOS_QUE_PRUEBAN = {'ConnektaNoEnviado', 'ConnektaRechazado', 'ConnektaPayloadInvalido',
                      'ConnektaCircuitOpenError'}


def _revierte(nodo) -> bool:
    for n in ast.walk(nodo):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if (isinstance(t, ast.Attribute) and t.attr.startswith('siesa_')
                        and t.attr.endswith('triggered')
                        and isinstance(n.value, ast.Constant) and n.value.value is False):
                    return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == 'desmarcar_puc':
            return True
    return False


def _nombre_tipo(t):
    if t is None:
        return None
    if isinstance(t, ast.Tuple):
        return {_nombre_tipo(e) for e in t.elts}
    return t.id if isinstance(t, ast.Name) else getattr(t, 'attr', None)


def reversiones(src: str, archivo: str) -> dict:
    """{sitio: prueba?} para todo `except` que revierte un pre-flag."""
    out = {}
    arbol = ast.parse(src)
    for fn in ast.walk(arbol):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for h in ast.walk(fn):
            if isinstance(h, ast.ExceptHandler) and any(_revierte(s) for s in h.body):
                tipo = _nombre_tipo(h.type)
                tipos = tipo if isinstance(tipo, set) else {tipo}
                out[f'{archivo}::{fn.name}:{h.lineno}'] = bool(tipos) and tipos <= _TIPOS_QUE_PRUEBAN
    return out


class TestSoloUnaPruebaBajaLaBandera:

    def test_ningun_except_revierte_sin_prueba(self):
        src = (RAIZ / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        sitios = reversiones(src, 'siesa_job_service.py')
        malos = sorted(s for s, ok in sitios.items()
                       if not ok and s.rsplit(':', 1)[0] not in REVERSIONES_DECLARADAS)
        assert not malos, (
            f'\n{malos}: bajan el pre-flag ante una excepción que no prueba que el '
            'documento no entró. Solo `except ConnektaNoEnviado` puede (4xx, '
            '`codigo != 0`, 429, circuito abierto). Un 5xx o una conexión cortada '
            'es «no sé»: la bandera queda y se declara (Regla 3).')

    def test_el_inventario_solo_encoge_y_dice_por_que(self):
        src = (RAIZ / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        sitios = {s.rsplit(':', 1)[0] for s, ok in reversiones(src, 'siesa_job_service.py').items()
                  if not ok}
        assert set(REVERSIONES_DECLARADAS) <= sitios | set(), 'inventario viejo'
        assert len(REVERSIONES_DECLARADAS) <= 1
        assert all(len(v) > 40 for v in REVERSIONES_DECLARADAS.values())

    def test_piso(self):
        """RC, DC y las dos NC revierten bajo prueba: un escáner roto daría 0."""
        src = (RAIZ / 'app' / 'services' / 'siesa_job_service.py').read_text(encoding='utf-8')
        sitios = reversiones(src, 'siesa_job_service.py')
        assert sum(1 for ok in sitios.values() if ok) >= 4

    def test_meta(self):
        src = ("def a(r):\n    try:\n        x()\n    except Exception:\n"
               "        r.siesa_rc_triggered = False\n        raise\n"
               "def b(r):\n    try:\n        x()\n    except ConnektaNoEnviado:\n"
               "        r.siesa_rc_triggered = False\n        raise\n"
               "def c(r):\n    try:\n        x()\n    except:\n        r.desmarcar_puc('1')\n"
               "def d(r):\n    if ensayo:\n        r.siesa_nc_triggered = False\n"
               "def e(r):\n    try:\n        x()\n    except (ConnektaRechazado, ValueError):\n"
               "        r.siesa_dc_triggered = False\n"
               "def f(r):\n    '''except Exception: r.siesa_rc_triggered = False'''\n")
        s = {k.split('::')[1].split(':')[0]: v for k, v in reversiones(src, 'x.py').items()}
        assert s == {'a': False, 'b': True, 'c': False, 'e': False}
