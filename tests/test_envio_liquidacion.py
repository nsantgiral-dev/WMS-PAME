"""
Un documento de la liquidación sale a Siesa **solo con lo que se leyó de su
fila de cartera, justo antes del POST** (auditoría de liquidación, P1-1, P1-2,
P2-5 — 2026-09-27).

## La clase

*Un documento financiero sale con un dato de cartera que nadie leyó de SU
fila*: el tercero vacío de una cabecera que falló, la UN global (`001` en
producción, cuando la cartera real es `99`), la cuenta de respaldo, o la fila
de OTRO centro de operación con el mismo número de factura (la numeración se
solapa: medido en producción el 2026-09-27, FE-17062 existe en el CO 003 y en
el CO 004, de clientes distintos).

Y su gemela: *encolar le pregunta a Siesa dentro del request* — 2–3 s por
parada medidos en producción; el PWA corta a los 25 s.

## Ahora

- Encolar no lee Siesa (`liquidacion_service`): el job lleva la parada.
- El ejecutor resuelve tercero, sucursal, cuenta, UN, CO y monto de la fila
  exacta (`envio_liquidacion.resolver_envio`, CO + tipo + consecutivo). Sin
  fila, espera (`DependenciaPendiente`); pasado el tope, `DatoQueFalta`.
- El gateway no sale sin esos tres datos (`exigir_datos_del_cruce`).

## Trinquetes (AST, con inventario que solo encoge)

1. Nada alcanzable desde `liquidar_ruta_siesa` lee Siesa (grafo de llamadas del
   módulo) — y, en ejecución, con el gateway real fuera de simulación.
2. Todo `trigger_recibo_caja` / `trigger_documento_contable` de `app/` está
   precedido por `resolver_envio` **en su mismo bloque** (inventario: los
   delegados del gateway).
3. Los conectores del RC y la retención piden `exigir_datos_del_cruce` y no
   leen la UN ni la cuenta globales.

Meta-tests (lo que ven, lo sano que no, docstrings), pisos y mutaciones.
"""
import ast
import json
import pathlib
import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from tests._envio_liq import cartera_en, fila_cartera, resolver_cola

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'
LIQ = APP / 'services' / 'liquidacion_service.py'
GW_LIQ = APP / 'services' / 'connekta_liquidacion_gateway.py'


# ═════════════════════════════════════════════════════════════════════════════
# Mundo
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def parada(db, almacen):
    """Una parada de contado de una ruta entregada, sin la FE anotada."""
    def _hacer(estado='ENTREGADO', monto=50000, pedido_clave=None, cliente='FERRETERÍA X',
               motivo=None, confirmada=None, items=None, consec=1502):
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
                         consec_docto_pedido_siesa=consec, numero_pedido_siesa=f'PD{consec}',
                         cliente=cliente, pedido_clave=pedido_clave)
        db.session.add(t)
        db.session.flush()
        r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega=estado,
                           forma_pago='EFECTIVO', monto_cobrado=monto,
                           motivo_descuento=motivo, retencion_confirmada=confirmada,
                           items_entregados=items)
        db.session.add(r)
        db.session.commit()
        return r
    return _hacer


def _job(db, tipo, r, **extra):
    from app.models.siesa_job import SiesaJob
    p = {'recaudo_id': r.id, 'tipo_docto_fe': 'FE', 'consec_fe': '17062',
         'forma_pago': 'EFECTIVO', 'monto': 50000, **extra}
    j = SiesaJob.encolar(tipo, p, referencia_tipo='RecaudoEntrega', referencia_id=r.id)
    db.session.commit()
    return j


def _mc():
    mc = MagicMock()
    mc.trigger_recibo_caja.return_value = {'codigo': 0}
    mc.trigger_documento_contable.return_value = {'codigo': 0}
    return mc


#: Las dos filas del solape medido en producción el 2026-09-27 (solo GET): el
#: mismo consecutivo en dos CO, de dos clientes.
FILA_003 = fila_cartera(tipo='FE', consec='17062', co='003', nit='1075000551', un='99',
                        cuenta='13050501', total_db=50000)
FILA_004 = fila_cartera(tipo='FE', consec='17062', co='004', nit='1080000790', un='01',
                        cuenta='13050599', total_db=90000)


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La lectura exacta del documento
# ═════════════════════════════════════════════════════════════════════════════

class TestLaLecturaExactaLlevaElCO:

    def _gw(self, monkeypatch, respuesta):
        from app.services.connekta_gateway import ConnektaGateway
        gw = ConnektaGateway()
        gw.modo_simulacion = False
        pedidos = []

        def _get(self, api, params=None, **kw):
            pedidos.append((api, params))
            return respuesta
        monkeypatch.setattr(ConnektaGateway, '_get', _get)
        return gw, pedidos

    def test_el_filtro_es_co_tipo_y_consecutivo(self, app, monkeypatch):
        gw, pedidos = self._gw(monkeypatch, {'detalle': {'Table': [FILA_004]}})
        assert gw.get_cxc_de_factura('004', 'FE', '17062') == [FILA_004]
        [(api, params)] = pedidos
        assert api == 'API_v2_CxC_General'
        f = params['parametros']
        assert "f353_id_co_cruce = ''004''" in f
        assert "f353_id_tipo_docto_cruce = ''FE''" in f
        assert 'f353_consec_docto_cruce = 17062' in f

    def test_no_traga_errores(self, app, monkeypatch):
        gw, _ = self._gw(monkeypatch, None)
        with pytest.raises(RuntimeError, match='no respondió'):
            gw.get_cxc_de_factura('003', 'FE', '1')

    def test_un_filtro_que_no_filtra_no_elige_a_ciegas(self, app, monkeypatch):
        gw, _ = self._gw(monkeypatch, {'detalle': {'Table': [FILA_003] * 20}})
        with pytest.raises(RuntimeError, match='no filtró'):
            gw.get_cxc_de_factura('003', 'FE', '17062')

    def test_sin_co_no_se_lee(self, app, monkeypatch):
        gw, pedidos = self._gw(monkeypatch, {'detalle': {'Table': []}})
        with pytest.raises(ValueError, match='CO'):
            gw.get_cxc_de_factura('', 'FE', '17062')
        assert pedidos == []

    def test_el_vencimiento_de_la_nc_tambien_lleva_el_co(self, app, monkeypatch):
        gw, pedidos = self._gw(monkeypatch, {'detalle': {'Table': [
            dict(FILA_003, f353_fecha_vcto='2026-10-12T00:00:00')]}})
        assert gw.get_vencimiento_factura('FE', '17062', co='003') == '20261012'
        assert "f353_id_co_cruce = ''003''" in pedidos[0][1]['parametros']


class TestLaFilaEsLaDelCO:

    def test_fila_unica_descarta_la_de_otro_co(self):
        from app.services import cxc_cruce
        assert cxc_cruce.fila_unica([FILA_004, FILA_003], '003', 'FE', '17062') is FILA_003
        assert cxc_cruce.fila_unica([FILA_004], '003', 'FE', '17062') is None

    def test_dos_filas_del_mismo_documento_no_se_eligen(self):
        from app.services import cxc_cruce
        with pytest.raises(cxc_cruce.FilaAmbigua):
            cxc_cruce.fila_unica([FILA_003, dict(FILA_003, f353_nro_cuota_cruce=1)],
                                 '003', 'FE', '17062')

    def test_la_busqueda_por_nit_tambien_mira_el_co(self):
        from app.services import cxc_cruce
        assert cxc_cruce.fila_de_la_factura([FILA_004, FILA_003], 'PD', 1, 'FE', 17062,
                                            co='003') is FILA_003
        assert cxc_cruce.esta_saldada([FILA_004], 'PD', 1, 'FE', 17062, co='003') is None

    def test_la_cartera_del_cliente_tambien_mira_el_co(self):
        """El consumo de cupo de cartera (`cartera_service._fila_en_cartera`)
        busca la FE del WMS en la cartera del NIT: un cliente que compra en
        dos CO puede tener el mismo consecutivo en los dos."""
        from app.services.cartera_service import _fila_en_cartera
        t = MagicMock(pedido_clave='003-PD-1502', tipo_docto_pedido_siesa='PD',
                      consec_docto_pedido_siesa=1502, fe_tipo='FE', fe_consec='17062')
        assert _fila_en_cartera(t, [FILA_004, FILA_003]) is FILA_003
        assert _fila_en_cartera(t, [FILA_004]) is None

    def test_el_co_de_la_factura_sale_de_la_clave_del_pedido(self):
        from app.services import cxc_cruce
        t = MagicMock(pedido_clave='004-PD-1502')
        assert cxc_cruce.co_de_la_factura(t, '003') == '004'
        assert cxc_cruce.co_de_la_factura(MagicMock(pedido_clave=None), '003') == '003'
        assert cxc_cruce.co_de_la_factura(MagicMock(pedido_clave=None), MagicMock()) is None


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El ejecutor resuelve de la fila; nunca con respaldo
# ═════════════════════════════════════════════════════════════════════════════

class TestElEjecutorResuelveDeLaFila:

    def test_el_recibo_sale_con_la_un_y_el_tercero_de_la_fila_no_del_payload(
            self, app, db, parada):
        """El payload trae lo que un encolador envenenado habría puesto (el
        tercero vacío, la UN global): sale con los de la fila de cartera."""
        from app.services.siesa_job_service import _ejecutar_job
        r = parada(pedido_clave='003-PD-1502')
        job = _job(db, 'RECIBO_CAJA', r, tercero_nit='', unidad_negocio='001',
                   cuenta_cxc='', co_factura='')
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, [FILA_004, FILA_003])
            _ejecutar_job(job)
        kw = mc.trigger_recibo_caja.call_args.kwargs
        assert (kw['tercero_nit'], kw['unidad_negocio'], kw['cuenta_cxc'], kw['co_factura']) == (
            '1075000551', '99', '13050501', '003')
        p = json.loads(job.payload)
        assert p['cruce_por'] == 'FACTURA' and p['saldo_antes_rc'] == 50000

    def test_la_retencion_tambien(self, app, db, parada):
        from app.services.siesa_job_service import _ejecutar_job
        r = parada(pedido_clave='004-PD-1502', motivo='RETEFUENTE_2.5', confirmada=True)
        r.siesa_rc_triggered = True
        db.session.commit()
        job = _job(db, 'DOCUMENTO_CONTABLE_RET', r, cuenta_puc='13551501',
                   tipo_retencion='RETEFUENTE_2.5', monto=1231, base_gravable=49244,
                   unidad_negocio='001')
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, [FILA_003, FILA_004], co='003')
            _ejecutar_job(job)
        kw = mc.trigger_documento_contable.call_args.kwargs
        assert (kw['tercero_nit'], kw['unidad_negocio'], kw['co_factura']) == (
            '1080000790', '01', '004'), 'el CO sale de la clave del pedido, no del global'

    def test_sin_fila_espera_y_no_postea(self, app, db, parada):
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        r = parada()
        job = _job(db, 'RECIBO_CAJA', r)
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, [])
            with pytest.raises(DependenciaPendiente, match='todavía no muestra la factura') as e:
                _ejecutar_job(job)
        assert e.value.espera_minutos == 10
        assert 'PD1502' in str(e.value) and 'FERRETERÍA X' in str(e.value)
        mc.trigger_recibo_caja.assert_not_called()
        db.session.refresh(r)
        assert r.siesa_rc_triggered is False

    def test_pasado_el_tope_se_declara_con_quien_lo_pone(self, app, db, parada):
        from app.services.siesa_job_service import DatoQueFalta, ErrorDeterminista, _ejecutar_job
        r = parada()
        # El tope cuenta desde la primera espera A SIESA de este envío (marca
        # en el payload), no desde que se encoló (validación 2026-09-29, P1).
        job = _job(db, 'RECIBO_CAJA', r, esperando_siesa_desde=(
            datetime.utcnow() - timedelta(hours=25)).isoformat(timespec='seconds'))
        job.fecha_creacion = datetime.utcnow() - timedelta(hours=40)
        db.session.commit()
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, [])
            with pytest.raises(DatoQueFalta, match='Enviar todo a Siesa') as e:
                _ejecutar_job(job)
        assert isinstance(e.value, ErrorDeterminista)
        assert str(e.value).startswith('DATO_QUE_FALTA: ')
        assert 'lleva 25 h esperando a Siesa' in str(e.value)
        assert 'se encoló 15 h antes de empezar a esperar a Siesa' in str(e.value)
        mc.trigger_recibo_caja.assert_not_called()

    def test_el_tope_cuenta_desde_la_primera_espera_a_siesa(self, app, db, parada):
        """Encolado hace 40 h (esperó al banco o a la NC): la primera falla de
        Siesa espera y deja la marca; una lectura buena la borra."""
        from app.models.siesa_job import SiesaJob
        from app.services.envio_liquidacion import MARCA_ESPERA_SIESA
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        r = parada()
        job = _job(db, 'RECIBO_CAJA', r)
        job.fecha_creacion = datetime.utcnow() - timedelta(hours=40)
        db.session.commit()
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, Exception('timeout de lectura'))
            with pytest.raises(DependenciaPendiente):
                _ejecutar_job(job)
        db.session.expire_all()
        marca = db.session.get(SiesaJob, job.id).get_payload()[MARCA_ESPERA_SIESA]
        assert abs((datetime.utcnow() - datetime.fromisoformat(marca)).total_seconds()) < 60
        # Segunda falla: la marca no se mueve.
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, Exception('timeout de lectura'))
            with pytest.raises(DependenciaPendiente):
                _ejecutar_job(db.session.get(SiesaJob, job.id))
        assert db.session.get(SiesaJob, job.id).get_payload()[MARCA_ESPERA_SIESA] == marca
        # Siesa contesta: sale, y la marca se borra.
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, [FILA_003])
            _ejecutar_job(db.session.get(SiesaJob, job.id))
        mc.trigger_recibo_caja.assert_called_once()
        assert MARCA_ESPERA_SIESA not in db.session.get(SiesaJob, job.id).get_payload()

    @pytest.mark.parametrize('faltante', ['f200_id', 'f253_id', 'f353_id_un_cruce'])
    def test_una_fila_sin_sus_datos_no_sale(self, app, db, parada, faltante):
        from app.services.siesa_job_service import DatoQueFalta, _ejecutar_job
        r = parada()
        job = _job(db, 'RECIBO_CAJA', r)
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, [dict(FILA_003, **{faltante: ''})])
            with pytest.raises(DatoQueFalta, match=faltante):
                _ejecutar_job(job)
        mc.trigger_recibo_caja.assert_not_called()

    def test_siesa_caido_espera_sin_gastar_intento(self, app, db, parada):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _run_dlq_jobs
        r = parada()
        job = _job(db, 'RECIBO_CAJA', r)
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, Exception('timeout de lectura'))
            _run_dlq_jobs()
        job = db.session.get(SiesaJob, job.id)
        assert job.estado == 'PENDIENTE' and job.intentos == 0 and job.proximo_intento
        assert 'no respondió' in job.error_ultimo
        mc.trigger_recibo_caja.assert_not_called()

    def test_el_monto_del_boton_masivo_lo_calcula_el_ejecutor(self, app, db, parada):
        """Encolado sin monto: el neto de Siesa (con la misma política que
        «Registrar cobro»); acotado contra lo declarado."""
        from app.services.siesa_job_service import _ejecutar_job
        r = parada(monto=58600)
        job = _job(db, 'RECIBO_CAJA', r, monto=None)
        with patch('app.services.connekta_gateway.connekta', _mc()) as mc:
            cartera_en(mc, [dict(FILA_003, f353_total_db=58600)])
            mc.get_rowids_factura.return_value = [{'f470_vlr_neto': 58600, 'f470_vlr_bruto': 49244,
                                                   'f470_vlr_imp': 9356}]
            _ejecutar_job(job)
        assert mc.trigger_recibo_caja.call_args.kwargs['monto'] == 58600
        assert json.loads(job.payload)['monto'] == 58600


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Encolar no le pregunta nada a Siesa — en ejecución
# ═════════════════════════════════════════════════════════════════════════════

class TestEncolarNoLeeSiesa:

    def test_enviar_todo_con_el_gateway_real_fuera_de_simulacion(self, app, db, parada,
                                                                monkeypatch, producto):
        """El gateway real, fuera de simulación, con su única puerta de lectura
        (`_get`) y la red reventando: «Enviar todo a Siesa» encola igual —
        contado, contado con retención, parcial y rechazo—."""
        import requests
        from app.services.connekta_gateway import ConnektaGateway, connekta
        from app.services.liquidacion_service import LiquidacionService
        monkeypatch.setattr(connekta, 'modo_simulacion', False)

        preguntas = []

        def _no(*a, **k):
            # Se anota ADEMÁS de levantar: varias lecturas del gateway tragan la
            # excepción y devuelven vacío (`get_pedido_cabecera`), y un
            # `AssertionError` tragado dejaría este test en verde.
            preguntas.append(a[1:2] or a[:1])
            raise AssertionError('liquidar le preguntó a Siesa')
        monkeypatch.setattr(ConnektaGateway, '_get', _no)
        monkeypatch.setattr(requests, 'get', _no)
        monkeypatch.setattr(requests, 'post', _no)
        base = parada()
        ruta_id = base.ruta_id
        from app.models.recaudo_entrega import RecaudoEntrega
        for i, kw in enumerate(({'motivo': 'RETEFUENTE_2.5', 'confirmada': True},
                                {'estado': 'PARCIAL', 'items': [{'codigo': producto.codigo,
                                                                 'cantidad_devuelta': 1}]},
                                {'estado': 'RECHAZADO', 'monto': 0})):
            otra = parada(consec=1600 + i, **kw)
            RecaudoEntrega.query.get(otra.id).ruta_id = ruta_id
        db.session.commit()
        with patch('app.services.siesa_job_service.disparar_dlq_inmediato'):
            res = LiquidacionService.liquidar_ruta_siesa(ruta_id)
        assert preguntas == [], f'liquidar le preguntó a Siesa: {preguntas}'
        assert res['rc_encolados'] == 3 and res['dc_encolados'] == 1
        assert res['errores'] == []


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Trinquete 1 — nada alcanzable desde «Enviar todo» lee Siesa (AST)
# ═════════════════════════════════════════════════════════════════════════════

#: Lo que lee Siesa (directo o por una función que lo hace).
LEE_SIESA = {'get_rowids_factura', 'get_pedido_cabecera', 'get_cxc_general',
             'get_cxc_de_factura', 'get_detalle_factura', 'get_vencimiento_factura',
             'resolver_fe', 'resolver_fe_o_none', 'vincular_a_factura', '_vincular_sin_romper',
             'leer_facturas_en_paralelo', 'leer_cruce', 'resolver_cruce', 'resolver_envio',
             '_get'}

#: Funciones alcanzables que leen Siesa, con su porqué. **Solo encoge.**
LECTURAS_DECLARADAS = {}

RAIZ_ENVIAR_TODO = 'liquidar_ruta_siesa'


def _nombre(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)


def _funciones(arbol):
    out = {}
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(n.name, n)
    return out


def _llamadas(fn):
    pila = list(ast.iter_child_nodes(fn))
    while pila:
        n = pila.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            yield n
        pila.extend(ast.iter_child_nodes(n))


def lecturas_alcanzables(src: str, raiz: str):
    """`(visitadas, {función: [lecturas]})`: lo que lee Siesa en el grafo de
    llamadas del módulo alcanzable desde `raiz`."""
    fns = _funciones(ast.parse(src))
    visitadas, pila, malas = set(), [raiz], {}
    while pila:
        nombre = pila.pop()
        if nombre in visitadas or nombre not in fns:
            continue
        visitadas.add(nombre)
        for c in _llamadas(fns[nombre]):
            n = _nombre(c)
            if n in LEE_SIESA:
                malas.setdefault(nombre, []).append(n)
            elif n in fns:
                pila.append(n)
    return visitadas, malas


class TestNadaAlcanzableDesdeEnviarTodoLeeSiesa:

    def test_el_modulo(self):
        visitadas, malas = lecturas_alcanzables(LIQ.read_text(encoding='utf-8'), RAIZ_ENVIAR_TODO)
        nuevas = {f: l for f, l in malas.items() if f not in LECTURAS_DECLARADAS}
        assert not nuevas, (
            f'\n{nuevas}: «Enviar todo a Siesa» vuelve a leer Siesa dentro del request '
            f'(2–3 s por parada; el PWA corta a los 25 s). Lo resuelve el ejecutor: '
            f'`envio_liquidacion.resolver_envio`.')

    def test_el_inventario_solo_encoge(self):
        _, malas = lecturas_alcanzables(LIQ.read_text(encoding='utf-8'), RAIZ_ENVIAR_TODO)
        sobran = set(LECTURAS_DECLARADAS) - set(malas)
        assert not sobran, f'{sobran} ya no lee Siesa: sáquelo del inventario'

    def test_piso(self):
        visitadas, _ = lecturas_alcanzables(LIQ.read_text(encoding='utf-8'), RAIZ_ENVIAR_TODO)
        assert {'_procesar_recaudo', '_encolar_recibo_caja', '_encolar_documento_contable',
                '_encolar_retencion', '_devolucion_de_ruta'} <= visitadas, (
            f'el grafo no llega a los encoladores ({sorted(visitadas)})')

    def test_meta_ve_la_lectura_de_una_hija_y_no_lo_que_no_se_alcanza(self):
        src = ('def liquidar_ruta_siesa():\n    _hija()\n'
               'def _hija():\n    """get_rowids_factura en el docstring no cuenta."""\n'
               '    # connekta.get_pedido_cabecera() en un comentario tampoco\n'
               '    return connekta.get_rowids_factura(1, 2)\n'
               'def _suelta():\n    connekta.get_cxc_general(1)\n')
        visitadas, malas = lecturas_alcanzables(src, 'liquidar_ruta_siesa')
        assert malas == {'_hija': ['get_rowids_factura']}
        assert '_suelta' not in visitadas

    def test_meta_ve_la_resolucion_de_la_fe(self):
        src = 'def liquidar_ruta_siesa():\n    t, c = resolver_fe(tarea)\n'
        assert lecturas_alcanzables(src, 'liquidar_ruta_siesa')[1] == {
            'liquidar_ruta_siesa': ['resolver_fe']}


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Trinquete 2 — todo POST de la liquidación pasa por `resolver_envio`
# ═════════════════════════════════════════════════════════════════════════════

TRIGGERS = ('trigger_recibo_caja', 'trigger_documento_contable')

#: Llamadas que no pasan por la resolución, con su porqué. **Solo encoge.**
TRIGGERS_DECLARADOS = {
    'app/services/connekta_gateway.py::trigger_recibo_caja':
        'delegado del gateway: reenvía la llamada al dominio de liquidación',
    'app/services/connekta_gateway.py::trigger_documento_contable':
        'delegado del gateway: reenvía la llamada al dominio de liquidación',
}


def _bloque_minimo(fn, linea):
    """El `if` más interno de `fn` cuyo cuerpo contiene la línea (o `fn`)."""
    mejor = fn
    for n in ast.walk(fn):
        if isinstance(n, ast.If) and n.body:
            ini, fin = n.body[0].lineno, max(getattr(x, 'end_lineno', x.lineno) for x in n.body)
            if ini <= linea <= fin and (mejor is fn or ini >= mejor.body[0].lineno):
                mejor = n
    return mejor


def triggers_sin_resolucion(fuentes: dict) -> dict:
    """{sitio: resuelto?} para toda llamada a un trigger de la liquidación: la
    resolución tiene que estar en el mismo bloque (`if job.tipo == …`) y antes."""
    out = {}
    for archivo, src in fuentes.items():
        arbol = ast.parse(src)
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for c in _llamadas(fn):
                if _nombre(c) not in TRIGGERS:
                    continue
                bloque = _bloque_minimo(fn, c.lineno)
                ok = any(_nombre(x) == 'resolver_envio' and x.lineno < c.lineno
                         for x in ast.walk(bloque) if isinstance(x, ast.Call))
                out[f'{archivo}::{fn.name}'] = out.get(f'{archivo}::{fn.name}', True) and ok
    return out


def _fuentes_app():
    return {p.relative_to(RAIZ).as_posix(): p.read_text(encoding='utf-8') for p in APP.rglob('*.py')}


class TestTodoPostDeLaLiquidacionPasaPorLaResolucion:

    def test_ningun_trigger_sin_resolver(self):
        sitios = triggers_sin_resolucion(_fuentes_app())
        malos = [s for s, ok in sitios.items() if not ok and s not in TRIGGERS_DECLARADOS]
        assert not malos, (
            f'\n{malos}: un recibo de caja o una retención sale sin resolver su fila de '
            f'cartera antes (`envio_liquidacion.resolver_envio`): el tercero, la cuenta y '
            f'la UN salen de otro lado — la UN global ya costó rechazos deterministas.')

    def test_el_inventario_solo_encoge(self):
        sitios = triggers_sin_resolucion(_fuentes_app())
        sobran = [s for s in TRIGGERS_DECLARADOS if sitios.get(s, True)]
        assert not sobran, f'{sobran} ya no está (o ya resuelve): sáquelo del inventario'

    def test_piso(self):
        sitios = triggers_sin_resolucion(_fuentes_app())
        assert sitios.get('app/services/siesa_job_service.py::_ejecutar_job') is True
        assert len(sitios) >= 3

    def test_meta_por_bloque(self):
        src = ('def f(job):\n'
               '    if job.tipo == "RC":\n'
               '        p = resolver_envio(job)\n'
               '        connekta.trigger_recibo_caja(**p)\n'
               '    if job.tipo == "DC":\n'
               '        connekta.trigger_documento_contable(**p)\n'
               'def g():\n    """resolver_envio en un docstring no resuelve."""\n'
               '    connekta.trigger_recibo_caja()\n'
               'def h():\n    connekta.trigger_recibo_caja()\n    resolver_envio(j)\n')
        s = triggers_sin_resolucion({'x.py': src})
        assert s == {'x.py::f': False, 'x.py::g': False, 'x.py::h': False}
        bien = ('def f(job):\n    if job.tipo == "RC":\n        p = resolver_envio(job)\n'
                '        connekta.trigger_recibo_caja(**p)\n')
        assert triggers_sin_resolucion({'x.py': bien}) == {'x.py::f': True}


# ═════════════════════════════════════════════════════════════════════════════
# 6 · Trinquete 3 — el gateway no rellena la cartera con los globales
# ═════════════════════════════════════════════════════════════════════════════

GLOBALES_DE_CARTERA = {'unidad_negocio', 'cxc_auxiliar'}


def rellenos_de_cartera(src: str) -> dict:
    """{conector: (exige los datos?, [globales leídos])} en los triggers."""
    out = {}
    for fn in ast.walk(ast.parse(src)):
        if not isinstance(fn, ast.FunctionDef) or fn.name not in TRIGGERS:
            continue
        exige = any(_nombre(c) == 'exigir_datos_del_cruce' for c in _llamadas(fn))
        leidos = sorted({n.attr for n in ast.walk(fn)
                         if isinstance(n, ast.Attribute) and n.attr in GLOBALES_DE_CARTERA
                         and isinstance(n.value, ast.Name) and n.value.id in ('core', 'self')})
        out[fn.name] = (exige, leidos)
    return out


class TestElGatewayNoRellenaLaCartera:

    def test_los_dos_conectores(self):
        r = rellenos_de_cartera(GW_LIQ.read_text(encoding='utf-8'))
        assert r == {'trigger_recibo_caja': (True, []),
                     'trigger_documento_contable': (True, [])}, (
            f'{r}: un conector de la liquidación volvió a rellenar la cartera con la UN o '
            f'la cuenta globales (o dejó de exigir los datos de la fila).')

    def test_meta(self):
        src = ('def trigger_recibo_caja(self, un):\n    core = self._core\n'
               '    u = un or core.unidad_negocio\n'
               'def trigger_documento_contable(self):\n'
               '    """core.cxc_auxiliar en el docstring no cuenta."""\n'
               '    exigir_datos_del_cruce("x", 1, 2, 3)\n')
        assert rellenos_de_cartera(src) == {
            'trigger_recibo_caja': (False, ['unidad_negocio']),
            'trigger_documento_contable': (True, [])}

    def test_en_ejecucion(self, app):
        from app.services.connekta_gateway import ConnektaGateway, ConnektaPayloadInvalido
        gw = ConnektaGateway()
        gw.unidad_negocio = '001'
        with patch.object(gw, '_post') as post:
            with pytest.raises(ConnektaPayloadInvalido):
                gw.trigger_documento_contable('900', '001', '13551501', 1, 1, 'FE', '1',
                                              cuenta_cxc='13050501', unidad_negocio='')
        post.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# 7 · La tarjeta del envío: pedido, palabras, y sin «Reintentar» si no sirve
# ═════════════════════════════════════════════════════════════════════════════

class TestLaTarjetaDelEnvio:

    def _fallido(self, db, r, error):
        from app.models.siesa_job import SiesaJob
        j = _job(db, 'RECIBO_CAJA', r)
        j.estado, j.error_ultimo = 'FALLIDO', error
        db.session.commit()
        return db.session.get(SiesaJob, j.id)

    def test_un_dato_que_falta_no_es_reintentable_y_dice_de_que_pedido_es(self, app, db, parada):
        from app.services.envio_liquidacion import describir_envio
        r = parada()
        j = self._fallido(db, r, 'DATO_QUE_FALTA: el pedido PD1502: la fila no trae la UN.')
        d = describir_envio(j)
        assert d['pedido'] == 'PD1502' and d['cliente'] == 'FERRETERÍA X'
        assert d['reintentable'] is False and d['que_falta'] is True
        assert d['mensaje'] == 'el pedido PD1502: la fila no trae la UN.'

    def test_un_rechazo_de_siesa_si_es_reintentable(self, app, db, parada):
        from app.services.envio_liquidacion import describir_envio
        r = parada()
        j = self._fallido(db, r, 'Siesa rechazó (codigo=1): período cerrado')
        assert describir_envio(j)['reintentable'] is True

    def test_un_recibo_sin_verificar_no_se_reintenta(self, app, db, parada):
        from app.services.envio_liquidacion import describir_envio
        r = parada()
        r.siesa_rc_triggered = True
        db.session.commit()
        j = self._fallido(db, r, 'SIN_VERIFICAR: el pedido PD1502: …')
        d = describir_envio(j)
        assert d['reintentable'] is False and d['sin_verificar'] is True

    def test_el_endpoint_no_ofrece_reintentar_lo_que_no_sirve(self, app, client, db, parada):
        from tests.test_cartera_retencion import _jwt, _usuario
        r = parada()
        self._fallido(db, r, 'DATO_QUE_FALTA: el pedido PD1502: la fila no trae la UN.')
        d = client.get('/api/reposicion/siesa-jobs?estado=FALLIDO&tipos=RECIBO_CAJA',
                       headers=_jwt(app, _usuario(db, 'liquidador'))).get_json()
        [j] = d['jobs']
        assert j['puede_reintentar'] is False and j['envio']['pedido'] == 'PD1502'

    def test_la_pantalla(self, app, client, db, parada, tmp_path):
        from tests.test_cartera_retencion import _jwt, _usuario
        from tests.test_sin_codigos_en_pantalla import _node, codigos_crudos, visible
        r = parada(cliente='<b>ACME</b>')
        self._fallido(db, r, 'DATO_QUE_FALTA: el pedido PD1502: la fila no trae la UN.')
        d = client.get('/api/reposicion/siesa-jobs?estado=FALLIDO&tipos=RECIBO_CAJA',
                       headers=_jwt(app, _usuario(db, 'liquidador'))).get_json()
        out = _node(tmp_path, ['util.js', 'liquidacion.js'],
                    {'/api/reposicion/siesa-jobs': d}, """
            const sel = document.getElementById('liq-filtro-job'); sel.value = 'FALLIDO';
            await liqCargarJobs();
            return { html: document.getElementById('liq-lista-jobs').innerHTML };
        """)
        html = out['html']
        assert 'Reintentar' not in html
        assert 'Pedido PD1502' in visible(html)
        assert 'DATO_QUE_FALTA' not in html
        assert '<b>ACME' not in html, 'el cliente llega sin escapar'
        assert '/5' in visible(html)
        assert not codigos_crudos(visible(html))
