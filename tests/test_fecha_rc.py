"""
Las fechas del recibo de caja (142888) — regla del dueño, tanda 2 (2026-09-25).

- `F357_FECHA_RECAUDO`: el día (Bogotá) del cobro si es el MISMO MES que
  `F350_FECHA` (el día del envío). Si el mes cambió, `F357 = F350` y el día
  real del cobro va escrito en `F350_NOTAS`.
- `F358_FECHA_CONSIGNACION`: siempre el día real del cobro (la del banco).
- El recibo afectado queda marcado (`rc_cobro_otro_mes`) para el aviso de
  rutas que cruzan de mes.

**La clase:** una fecha del recibo decidida fuera de la política. Una función
(`politica_cobro.fechas_del_recibo`) y un trinquete AST: ninguna de las dos
claves se escribe con un valor que no salga de ella.

**No probado contra Siesa real:** la prueba de fin de mes en QA está escrita
en CLAUDE.md («La fecha del recibo de caja»).
"""
import ast
import json
import pathlib
from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services.politica_cobro import fechas_del_recibo

RAIZ = pathlib.Path(__file__).resolve().parent.parent


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La política
# ═════════════════════════════════════════════════════════════════════════════

class TestLaPolitica:
    def test_mismo_mes_lleva_el_dia_del_cobro(self):
        f = fechas_del_recibo('20260920', '20260925')
        assert (f['f350'], f['f357'], f['f358']) == ('20260925', '20260920', '20260920')
        assert f['cruza_mes'] is False and f['nota'] is None

    def test_el_mes_cambio_el_recaudo_va_con_el_documento_y_la_nota_lo_dice(self):
        f = fechas_del_recibo('20260930', '20261002')
        assert f['f350'] == '20261002'
        assert f['f357'] == '20261002'
        assert f['f358'] == '20260930'
        assert f['cruza_mes'] is True
        assert '30/09/2026' in f['nota'] and '02/10/2026' in f['nota']

    def test_cambio_de_anio(self):
        f = fechas_del_recibo('20251231', '20260102')
        assert f['cruza_mes'] is True and f['f357'] == '20260102' and f['f358'] == '20251231'

    def test_ultimo_dia_del_mes_mismo_dia(self):
        f = fechas_del_recibo('20260930', '20260930')
        assert f['cruza_mes'] is False and f['f357'] == '20260930'

    @pytest.mark.parametrize('malo', [None, '', '2026-09-20', '20260231', 'x', 20260920.5])
    def test_sin_fecha_de_cobro_todo_con_el_documento_y_declarado(self, malo):
        f = fechas_del_recibo(malo, '20260925')
        assert f['f357'] == f['f358'] == '20260925'
        assert f['sin_fecha_cobro'] is True and f['cruza_mes'] is False

    def test_un_cobro_posterior_no_se_registra_en_el_futuro(self):
        f = fechas_del_recibo('20260927', '20260925')
        assert f['f357'] == '20260925'

    def test_documento_ilegible_no_se_adivina(self):
        with pytest.raises(ValueError):
            fechas_del_recibo('20260920', '2026-09-25')


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El payload del 142888
# ═════════════════════════════════════════════════════════════════════════════

def _payload(monkeypatch, forma, fecha_recaudo, hoy='20261002', notas='Liq ruta 7'):
    from app.services.connekta_gateway import ConnektaGateway
    monkeypatch.setattr(ConnektaGateway, '_fecha_hoy_bogota', staticmethod(lambda: hoy))
    gw = ConnektaGateway()
    gw.ikey, gw.itoken = 'k', 't'
    gw.modo_simulacion = False
    gw.modo_ensayo = True
    gw.tipo_docto_recibo_caja = 'RC'
    p = gw.trigger_recibo_caja('900', '001', 1000, forma, 'FEW', '1', notas=notas,
                               referencia_pago='12345', fecha_recaudo=fecha_recaudo)['payload']
    header = next(v[0] for v in p.values() if isinstance(v, list) and v
                  and isinstance(v[0], dict) and 'F357_FECHA_RECAUDO' in v[0])
    caja = next(v[0] for v in p.values() if isinstance(v, list) and v
                and isinstance(v[0], dict) and 'F358_FECHA_CONSIGNACION' in v[0])
    return header, caja


class TestElPayload:
    def test_transferencia_que_cruza_de_mes(self, app, monkeypatch):
        h, c = _payload(monkeypatch, 'TRANSFERENCIA', '20260930')
        assert h['F350_FECHA'] == '20261002'
        assert h['F357_FECHA_RECAUDO'] == '20261002'
        assert c['F358_FECHA_CONSIGNACION'] == '20260930'
        assert h['F350_NOTAS'].startswith('Cobrado por el conductor el 30/09/2026')
        assert h['F350_NOTAS'].endswith('Liq ruta 7')
        # La referencia bancaria no se contamina con la nota de la fecha.
        assert c['F358_REFERENCIA_OTROS'] == '12345'

    def test_transferencia_del_mismo_mes(self, app, monkeypatch):
        h, c = _payload(monkeypatch, 'TRANSFERENCIA', '20260920', hoy='20260925')
        assert h['F357_FECHA_RECAUDO'] == '20260920'
        assert c['F358_FECHA_CONSIGNACION'] == '20260920'
        assert h['F350_NOTAS'] == 'Liq ruta 7'

    def test_efectivo_que_cruza_de_mes_no_inventa_consignacion(self, app, monkeypatch):
        h, c = _payload(monkeypatch, 'EFECTIVO', '20260930')
        assert h['F357_FECHA_RECAUDO'] == '20261002'
        assert c['F358_FECHA_CONSIGNACION'] == ''
        assert '30/09/2026' in h['F350_NOTAS']

    def test_las_notas_no_pasan_de_2000(self, app, monkeypatch):
        h, _c = _payload(monkeypatch, 'EFECTIVO', '20260930', notas='x' * 3000)
        assert len(h['F350_NOTAS']) == 2000
        assert h['F350_NOTAS'].startswith('Cobrado por el conductor')


# ═════════════════════════════════════════════════════════════════════════════
# 3 · El ejecutor marca el recibo y el aviso lo lista
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def recaudo_septiembre(db, almacen):
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
                     consec_docto_pedido_siesa=100, numero_pedido_siesa='PD100')
    db.session.add(t)
    db.session.flush()
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega='ENTREGADO',
                       forma_pago='EFECTIVO', monto_cobrado=1000,
                       # 2026-09-30 21:00 Bogotá = 2026-10-01 02:00 UTC.
                       fecha_confirmacion=datetime(2026, 10, 1, 2, 0))
    db.session.add(r)
    db.session.commit()
    return r


def _job_rc(db, r):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob.encolar('RECIBO_CAJA', {
        'recaudo_id': r.id, 'tercero_nit': '900', 'sucursal': '001', 'monto': 1000,
        'forma_pago': 'EFECTIVO', 'tipo_docto_fe': 'FEW', 'consec_fe': '1'},
        referencia_tipo='RecaudoEntrega', referencia_id=r.id)
    db.session.commit()
    return j


def _ejecutar(job, hoy, efecto):
    from app.services.siesa_job_service import _ejecutar_job
    with patch('app.services.connekta_gateway.connekta') as mc, \
            patch('app.utils.fecha.fecha_hoy_bogota', return_value=hoy):
        mc.trigger_recibo_caja.side_effect = efecto
        mc.get_cxc_general.return_value = [
            {'f353_id_tipo_docto_cruce': 'PD', 'f353_consec_docto_cruce': '100',
             'f353_total_db': 1000, 'f353_total_cr': 0}]
        try:
            _ejecutar_job(job)
        finally:
            pass
        return mc


class TestElReciboQuedaMarcado:
    def test_cobro_de_septiembre_enviado_en_octubre(self, db, recaudo_septiembre):
        from app.services import rezago_liquidacion as rz
        job = _job_rc(db, recaudo_septiembre)
        mc = _ejecutar(job, '20261002', lambda **k: {'codigo': 0})
        assert mc.trigger_recibo_caja.call_args.kwargs['fecha_recaudo'] == '20260930'
        db.session.refresh(recaudo_septiembre)
        assert recaudo_septiembre.rc_cobro_otro_mes == date(2026, 9, 30)
        lista = rz.recibos_de_otro_mes(hoy=date(2026, 10, 2))
        assert [x['recaudo_id'] for x in lista] == [recaudo_septiembre.id]
        assert lista[0]['cobrado_el'] == '2026-09-30' and lista[0]['pedido'] == 'PD100'
        assert rz.diagnostico(hoy=date(2026, 10, 2))['recibos_de_otro_mes'] == lista
        # Dos meses después ya no se lista: ese cierre pasó.
        assert rz.recibos_de_otro_mes(hoy=date(2026, 12, 2)) == []

    def test_del_mismo_mes_no_se_marca(self, db, recaudo_septiembre):
        job = _job_rc(db, recaudo_septiembre)
        _ejecutar(job, '20260930', lambda **k: {'codigo': 0})
        db.session.refresh(recaudo_septiembre)
        assert recaudo_septiembre.rc_cobro_otro_mes is None

    def test_un_rechazo_no_deja_la_marca(self, db, recaudo_septiembre):
        from app.services.connekta_gateway import ConnektaRechazado
        job = _job_rc(db, recaudo_septiembre)

        def _no(**k):
            raise ConnektaRechazado('codigo=1')
        with pytest.raises(ConnektaRechazado):
            _ejecutar(job, '20261002', _no)
        db.session.refresh(recaudo_septiembre)
        assert recaudo_septiembre.rc_cobro_otro_mes is None
        assert recaudo_septiembre.siesa_rc_triggered is False

    def test_el_correo_de_rutas_los_nombra(self, db, recaudo_septiembre, monkeypatch):
        from app.services import alertas_service as al
        enviados = []
        monkeypatch.setattr(al, '_enviar_email_con_dlq',
                            lambda asunto, html, texto, clave: enviados.append(texto))
        al._enviar_alerta_rutas_sin_liquidar(
            [], [{'ruta_id': 3, 'dias': 2, 'estado_financiero': 'PENDIENTE'}],
            [{'ruta_id': 7, 'pedido': 'PD100', 'cobrado_el': '2026-09-30'}])
        assert 'FECHADOS EN OTRO MES' in enviados[0] and 'PD100' in enviados[0]
        assert 'día real del cobro en sus notas' in enviados[0]


# ═════════════════════════════════════════════════════════════════════════════
# 4 · El trinquete: ninguna fecha del recibo fuera de la política
# ═════════════════════════════════════════════════════════════════════════════

CLAVES = ('F357_FECHA_RECAUDO', 'F358_FECHA_CONSIGNACION')


def _valor_ok(v) -> bool:
    if isinstance(v, ast.Constant) and v.value == '':
        return True        # el ancho fijo del plano (sin consignación)
    return (isinstance(v, ast.Subscript) and isinstance(v.value, ast.Name)
            and v.value.id == 'fechas_rc')


def fechas_fuera_de_la_politica(src: str) -> tuple:
    """`(total, malos)`: escrituras de las dos claves; malas = valor que no sale
    de `fechas_rc[...]` (o '' del ancho fijo), o función que no llama a
    `fechas_del_recibo`."""
    total, malos = 0, []
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        llama = any(isinstance(n, ast.Call) and getattr(n.func, 'id', getattr(n.func, 'attr', None))
                    == 'fechas_del_recibo' for n in ast.walk(fn))
        for n in ast.walk(fn):
            sitios = []
            if isinstance(n, ast.Dict):
                sitios = [(v, n.lineno) for k, v in zip(n.keys, n.values)
                          if isinstance(k, ast.Constant) and k.value in CLAVES]
            elif isinstance(n, ast.Assign):
                sitios = [(n.value, n.lineno) for t in n.targets
                          if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
                          and t.slice.value in CLAVES]
            for v, linea in sitios:
                total += 1
                if not _valor_ok(v) or not (llama or (isinstance(v, ast.Constant) and v.value == '')):
                    malos.append(f'{fn.name}:{linea}')
    return total, malos


class TestNingunaFechaDelReciboFueraDeLaPolitica:
    def test_app(self):
        total, malos = 0, []
        for p in sorted((RAIZ / 'app').rglob('*.py')):
            t, m = fechas_fuera_de_la_politica(p.read_text(encoding='utf-8'))
            total += t
            malos += [f'{p.relative_to(RAIZ)}::{x}' for x in m]
        assert total >= 3, 'el escáner no encontró las fechas del 142888: se rompió'
        assert not malos, (f'{malos}: una fecha del recibo de caja que no sale de '
                           '`politica_cobro.fechas_del_recibo`.')

    def test_meta_ve_una_fecha_propia(self):
        src = ("def rc(x):\n    fechas_rc = fechas_del_recibo(a, b)\n"
               "    h = {'F357_FECHA_RECAUDO': fecha_cobro}\n"
               "    caja['F358_FECHA_CONSIGNACION'] = hoy\n"
               "def otro():\n    return {'F357_FECHA_RECAUDO': fechas_rc['f357']}\n")
        total, malos = fechas_fuera_de_la_politica(src)
        assert total == 3 and len(malos) == 3

    def test_meta_lo_sano_no_marca(self):
        src = ("def rc(x):\n    '''F357_FECHA_RECAUDO: fecha_cobro'''\n"
               "    fechas_rc = fechas_del_recibo(a, b)\n"
               "    # 'F358_FECHA_CONSIGNACION': hoy\n"
               "    h = {'F357_FECHA_RECAUDO': fechas_rc['f357'], 'F358_FECHA_CONSIGNACION': ''}\n"
               "    caja['F358_FECHA_CONSIGNACION'] = fechas_rc['f358']\n")
        assert fechas_fuera_de_la_politica(src) == (3, [])
