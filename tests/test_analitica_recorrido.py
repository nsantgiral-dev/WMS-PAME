"""
Analítica — 🧭 Recorrido del pedido (Fase 1, 2026-09-24).

El mundo se arma con los **servicios reales**, como el arnés de flujo: la
historia del pedido entra por `pedidos_historia.registrar_barrido` (lo que
escribe el sync), el picking por `PickingService`, el empaque por
`PackingService`, el despacho por la frontera del DLQ
(`DespachoParialService._persistir_resultado`, que es lo que deja el job al
terminar), la entrega por `RutaService.confirmar_parada` y la liquidación por
`RutaService.liquidar_ruta`. Un test que escribiera las filas a mano probaría
que la base las acepta, no que el embudo lee lo que la operación deja.

Casos que distinguen (cada uno cae en un lugar distinto del embudo):

    PD1501  completo, efectivo                    → COMPLETO_SIN_FUGA
    PD1502  picking cancelado con motivo          → FUGA en «recogido», con el motivo de la bitácora
    PD1503  rechazado en ruta (cliente cerrado)   → FUGA en «entregado»
    PD1504  no pagó y se quedó con la mercancía   → llega a «entregado», FUGA en «cobrado»
    PD1505  recogido 7 de 10, liquidado           → COMPLETO_CON_FUGA (recogido incompleto)
    PD1506  picking creado, nadie lo tocó         → EN_CURSO en «aprobado»
    PD1507  cumplido en Siesa sin pasar por WMS   → FUERA_DEL_WMS (fuera del denominador)
    PD1508  una línea sin valor                   → «sin valor», no suma
    PD1509  a crédito, liquidado                  → cobrado SIN MARCA, completo sin fuga
    PD1510  bloqueo de picking (ubicación vacía)  → DETENIDO en «recogido»
    PED-FLU empaque sin clave (arnés viejo)       → «sin enlazar», nunca descartado
"""
import json
import pathlib
import shutil
import subprocess
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from tests.flujo.conductor_de_flujo import (
    Flujo, hacer_picking, hacer_ruta, sembrar_catalogo, sembrar_pedido,
)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
VALOR_LINEA = 50000


# ─────────────────────────────────────────────────────────────────────────────
# El mundo
# ─────────────────────────────────────────────────────────────────────────────

def _usuario(db, rol, almacen, nombre=None):
    from app.models.usuario import Usuario
    from tests.flujo.conductor_de_flujo import _sufijo
    u = Usuario(nombre=nombre or f'{rol} recorrido', email=f'{rol}_{_sufijo()}@t.com',
                password_hash='x', rol=rol, almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _conductor(db, almacen):
    from app.models.conductor import Conductor
    from tests.flujo.conductor_de_flujo import _sufijo
    u = _usuario(db, 'conductor', almacen, 'Conductor Recorrido')
    c = Conductor(nombre='Conductor Recorrido', cedula=f'REC-{_sufijo()}',
                  usuario_id=u.id, activo=True, disponible=True)
    db.session.add(c)
    db.session.commit()
    return u, c


def _mock_connekta():
    from unittest.mock import MagicMock
    m = MagicMock()
    m.modo_simulacion = True
    m.get_detalle_factura.return_value = []
    m.get_rowids_factura.return_value = []
    m.get_pedido_cabecera.return_value = {'f430_id_co': '003'}
    return m


def sembrar_historia(db, productos, consec, cliente, valores=None, remisionada=0,
                     completa=False, ahora=None):
    """Las líneas del pedido como las lee el sync: pendientes en Siesa + su historia."""
    from app.models.pedido_siesa import PedidoSiesa
    from app.services.pedidos_historia import registrar_barrido
    valores = valores or [VALOR_LINEA] * len(productos)
    filas = []
    for i, p in enumerate(productos):
        if remisionada == 0:
            db.session.add(PedidoSiesa(
                tipo_docto='PD', consec_docto=consec, centro_op='003', bodega='NB1',
                numero_pedido=f'PD{consec}', item_codigo=p.codigo_siesa,
                item_descripcion=p.nombre, item_id_siesa=p.codigo_siesa, cliente=cliente,
                municipio='NEIVA', estado_siesa=3, cantidad_pedida=10,
                cantidad_remisionada=0, cantidad_pendiente=10, producto_id=p.id))
        filas.append({
            'f431_rowid': consec * 100 + i, 'f430_id_tipo_docto': 'PD',
            'f430_consec_docto': consec, 'f430_id_co': '003', 'f150_id': 'NB1',
            'f120_referencia': p.codigo_siesa, 'f120_id': p.codigo_siesa,
            'f200_razon_social_pedido_fact': cliente, 'f431_cant1_pedida': 10,
            'f431_cant1_remisionada': remisionada, 'f431_vlr_neto': valores[i],
            'f430_ind_estado': 3,
        })
    db.session.commit()
    r = registrar_barrido(filas, paginacion_completa=completa, ahora=ahora, co_barrido='003')
    assert 'error' not in r, r
    db.session.commit()


def _flujo(almacen, usuario, productos, consec):
    return Flujo(pedido=f'PD{consec}', almacen_id=almacen.id, usuario_id=usuario.id,
                 producto_ids=[p.id for p in productos])


def _empacar(db, flujo, consec):
    from app.models.bulto import Bulto
    from app.services.packing_service import PackingService
    tarea = PackingService.crear_desde_picking(
        tareas_picking_ids=flujo.pickings, numero_pedido_siesa=flujo.pedido,
        almacen_id=flujo.almacen_id, tipo_docto_pedido_siesa='PD',
        consec_docto_pedido_siesa=str(consec))
    flujo.packing_id = tarea.id
    PackingService.iniciar(tarea.id, flujo.usuario_id)
    for item in tarea.items:
        PackingService.escanear_item(tarea.id, item.producto_id, item.cantidad_esperada)
    PackingService.confirmar_packing(tarea.id)
    PackingService.cerrar_packing(tarea.id, [{'tipo': 'Caja', 'cantidad': 1}], flujo.usuario_id)
    db.session.commit()
    flujo.bultos = [b.id for b in Bulto.query.filter_by(tarea_id=tarea.id).all()]
    return tarea


def _despachar(db, flujo, consec):
    """La frontera del DLQ: lo que deja el job DESPACHO_F470 al terminar."""
    from app.models.packing import TareaPacking
    from app.services.despacho_parcial_service import DespachoParialService
    tarea = db.session.get(TareaPacking, flujo.packing_id)
    tarea.rm_tipo, tarea.rm_consec = 'RM', 9000 + consec
    DespachoParialService._persistir_resultado(tarea, f'RM-{9000 + consec}', {'codigo': 0})


def _entregar(db, flujo, conductor_u, **datos):
    from app.services.ruta_service import RutaService
    base = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 100000}
    base.update(datos)
    with patch('app.services.connekta_gateway.connekta', _mock_connekta()):
        rid, _ = RutaService.confirmar_parada(flujo.ruta_id, flujo.packing_id,
                                              conductor_u.id, base)
    db.session.commit()
    return rid


def _liquidar(db, flujo, admin):
    from app.services.ruta_service import RutaService
    with patch('app.services.connekta_gateway.connekta', _mock_connekta()):
        RutaService.liquidar_ruta(flujo.ruta_id, usuario_id=admin.id)
    db.session.commit()


def _hasta_despacho(db, almacen, oper, consec, cliente, conductor, recoger=10, n=2,
                    valores=None):
    productos, _ = sembrar_catalogo(db, almacen, n=n)
    sembrar_historia(db, productos, consec, cliente, valores=valores)
    f = _flujo(almacen, oper, productos, consec)
    hacer_picking(db, f, 10, recoger)
    _empacar(db, f, consec)
    _despachar(db, f, consec)
    hacer_ruta(db, f, conductor.id)
    return f


@pytest.fixture
def mundo(db, almacen):
    from app.services.picking_service import PickingService

    admin = _usuario(db, 'admin', almacen, 'Admin Recorrido')
    oper = _usuario(db, 'operario', almacen, 'Operaria Recorrido')
    cond_u, cond = _conductor(db, almacen)
    w = {'admin': admin, 'oper': oper, 'almacen': almacen}

    # PD1507 va primero: su barrido COMPLETO la saca como CUMPLIDO, y en ese
    # momento no hay otras líneas abiertas que el barrido pudiera tocar.
    p7, _ = sembrar_catalogo(db, almacen, n=1)
    sembrar_historia(db, p7, 1507, 'CLIENTE FUERA', remisionada=10, completa=True)

    f1 = _hasta_despacho(db, almacen, oper, 1501, 'CLIENTE COMPLETO', cond)
    _entregar(db, f1, cond_u)
    _liquidar(db, f1, admin)
    w['f1'] = f1

    p2, _ = sembrar_catalogo(db, almacen, n=2)
    sembrar_historia(db, p2, 1502, 'CLIENTE CANCELA')
    for pid in [p.id for p in p2]:
        for t in PickingService.crear_tareas(producto_id=pid, cantidad=10,
                                             almacen_id=almacen.id,
                                             referencia_documento='PD1502',
                                             tipo_documento='PEDIDO'):
            PickingService.cancelar_picking(t.id, motivo='El cliente anuló por teléfono',
                                            usuario_id=admin.id)
    db.session.commit()

    f3 = _hasta_despacho(db, almacen, oper, 1503, 'CLIENTE CERRADO', cond)
    _entregar(db, f3, cond_u, estado_entrega='RECHAZADO', forma_pago=None, monto_cobrado=0,
              motivo_rechazo='CLIENTE_CERRADO', observaciones='Local cerrado a las 3 p. m.')

    f4 = _hasta_despacho(db, almacen, oper, 1504, 'CLIENTE NO PAGA', cond)
    _entregar(db, f4, cond_u, estado_entrega='RECHAZADO', forma_pago=None, monto_cobrado=0,
              motivo_rechazo='NO_PAGO_SE_QUEDO', observaciones='Dijo que paga el viernes')

    f5 = _hasta_despacho(db, almacen, oper, 1505, 'CLIENTE PARCIAL', cond, recoger=7)
    _entregar(db, f5, cond_u, monto_cobrado=70000)
    _liquidar(db, f5, admin)

    p6, _ = sembrar_catalogo(db, almacen, n=1)
    sembrar_historia(db, p6, 1506, 'CLIENTE ESPERA')
    PickingService.crear_tareas(producto_id=p6[0].id, cantidad=10, almacen_id=almacen.id,
                                referencia_documento='PD1506', tipo_documento='PEDIDO')
    db.session.commit()

    p8, _ = sembrar_catalogo(db, almacen, n=2)
    sembrar_historia(db, p8, 1508, 'CLIENTE SIN VALOR', valores=[VALOR_LINEA, None])
    PickingService.crear_tareas(producto_id=p8[0].id, cantidad=10, almacen_id=almacen.id,
                                referencia_documento='PD1508', tipo_documento='PEDIDO')
    db.session.commit()

    f9 = _hasta_despacho(db, almacen, oper, 1509, 'CLIENTE CREDITO', cond, n=1)
    _entregar(db, f9, cond_u, forma_pago='CREDITO', monto_cobrado=0)
    _liquidar(db, f9, admin)
    w['f9'] = f9

    p10, _ = sembrar_catalogo(db, almacen, n=1)
    sembrar_historia(db, p10, 1510, 'CLIENTE BLOQUEADO')
    t10 = PickingService.crear_tareas(producto_id=p10[0].id, cantidad=10,
                                      almacen_id=almacen.id, referencia_documento='PD1510',
                                      tipo_documento='PEDIDO')[0]
    PickingService.iniciar_picking(t10.id, oper.id)
    PickingService.reportar_problema(t10.id, oper.id, 'UBICACION_VACIA', 0)
    db.session.commit()

    # El arnés viejo: número 'PED-FLU-…' → sin clave, ni en picking ni en empaque.
    pv, _ = sembrar_catalogo(db, almacen, n=1)
    numero = sembrar_pedido(db, pv)
    fv = Flujo(pedido=numero, almacen_id=almacen.id, usuario_id=oper.id,
               producto_ids=[p.id for p in pv])
    hacer_picking(db, fv, 10, 10)
    from app.services.packing_service import PackingService
    pk = PackingService.crear_desde_picking(tareas_picking_ids=fv.pickings,
                                            numero_pedido_siesa=numero,
                                            almacen_id=almacen.id)
    fv.packing_id = pk.id
    PackingService.iniciar(pk.id, oper.id)
    for item in pk.items:
        PackingService.escanear_item(pk.id, item.producto_id, item.cantidad_esperada)
    PackingService.confirmar_packing(pk.id)
    db.session.commit()
    assert pk.pedido_clave is None
    w['fv'] = fv
    return w


def _filtros(**kw):
    from app.services.analitica_recorrido import filtros_de
    return filtros_de({k: v for k, v in kw.items() if v is not None})


def _svc():
    from app.services import analitica_recorrido
    return analitica_recorrido


def _por_clave(pedidos):
    return {p.clave: p for p in pedidos}


def _etapa(d, e):
    return next(x for x in d['embudo'] if x['etapa'] == e)


# ─────────────────────────────────────────────────────────────────────────────
# Cada pedido cae donde tiene que caer
# ─────────────────────────────────────────────────────────────────────────────

class TestCadaPedidoCaeDondeDebe:

    def test_la_cadena_se_une_por_la_clave(self, mundo):
        from app.models.packing import TareaPacking
        pk = TareaPacking.query.get(mundo['f1'].packing_id)
        assert pk.pedido_clave == '003-PD-1501'

    def test_estados_finales(self, mundo):
        svc = _svc()
        ps, _ = svc.cohorte(*_rango())
        E = svc.EstadoFinal
        por = _por_clave(ps)
        esperado = {
            '003-PD-1501': E.COMPLETO_SIN_FUGA,
            '003-PD-1502': E.FUGA,
            '003-PD-1503': E.FUGA,
            '003-PD-1504': E.FUGA,
            '003-PD-1505': E.COMPLETO_CON_FUGA,
            '003-PD-1506': E.EN_CURSO,
            '003-PD-1507': E.FUERA_DEL_WMS,
            '003-PD-1508': E.EN_CURSO,
            '003-PD-1509': E.COMPLETO_SIN_FUGA,
            '003-PD-1510': E.DETENIDO,
        }
        assert {c: por[c].estado_final for c in esperado} == esperado

    def test_hasta_donde_llego_cada_uno(self, mundo):
        svc = _svc()
        por = _por_clave(svc.cohorte(*_rango())[0])
        ultimo = {c: svc.ETAPAS[p.ultimo] for c, p in por.items() if p.enlazado}
        assert ultimo['003-PD-1501'] == 'liquidado'
        assert ultimo['003-PD-1502'] == 'aprobado'
        assert ultimo['003-PD-1503'] == 'despachado'
        assert ultimo['003-PD-1504'] == 'entregado'    # la mercancía quedó con el cliente
        assert ultimo['003-PD-1510'] == 'aprobado'

    def test_el_motivo_de_la_cancelacion_sale_de_la_bitacora(self, mundo):
        p = _por_clave(_svc().cohorte(*_rango())[0])['003-PD-1502']
        assert p.fuga['etapa'] == 'recogido'
        assert 'El cliente anuló por teléfono' in p.fuga['motivo']

    def test_rechazo_y_sin_pago_llevan_su_motivo(self, mundo):
        por = _por_clave(_svc().cohorte(*_rango())[0])
        assert por['003-PD-1503'].fuga['etapa'] == 'entregado'
        assert 'Cliente cerrado' in por['003-PD-1503'].fuga['motivo']
        assert por['003-PD-1504'].fuga['etapa'] == 'cobrado'
        assert por['003-PD-1504'].fuga['codigo'] == 'ENTREGADO_SIN_PAGO'

    def test_el_bloqueo_detiene_con_su_motivo(self, mundo):
        p = _por_clave(_svc().cohorte(*_rango())[0])['003-PD-1510']
        assert p.detenido['etapa'] == 'recogido'
        assert 'Ubicación vacía' in p.detenido['motivo']

    def test_credito_pasa_por_cobrado_sin_marca(self, mundo):
        p = _por_clave(_svc().cohorte(*_rango())[0])['003-PD-1509']
        assert p.a_credito and 'cobrado' in p.implicitas
        assert p.marcas['cobrado'] is None and p.marcas['liquidado'] is not None

    def test_recogido_incompleto_es_perdida_parcial(self, mundo):
        p = _por_clave(_svc().cohorte(*_rango())[0])['003-PD-1505']
        assert [f['codigo'] for f in p.parciales] == ['RECOGIDO_INCOMPLETO']

    def test_una_linea_sin_valor_deja_el_pedido_sin_valor(self, mundo):
        por = _por_clave(_svc().cohorte(*_rango())[0])
        assert por['003-PD-1508'].valor is None
        assert por['003-PD-1508'].valor_motivo
        assert float(por['003-PD-1501'].valor) == 2 * VALOR_LINEA


def _rango():
    from app.utils.fecha import dia_operativo
    hoy = dia_operativo()
    return hoy - timedelta(days=2), hoy + timedelta(days=1), None


# ─────────────────────────────────────────────────────────────────────────────
# El embudo y la guía
# ─────────────────────────────────────────────────────────────────────────────

class TestElEmbudo:

    def _d(self):
        desde, hasta, _ = _rango()
        return _svc().recorrido(_filtros(desde=desde.isoformat(), hasta=hasta.isoformat()))

    def test_conteos_por_etapa(self, mundo):
        d = self._d()
        n = {e['etapa']: e['pedidos'] for e in d['embudo']}
        # 10 con clave; PD1507 salió por fuera del WMS pero SÍ fue aprobado.
        assert d['pedidos'] == 10
        assert n == {'aprobado': 10, 'recogido': 5, 'empacado': 5, 'despachado': 5,
                     'entregado': 4, 'cobrado': 3, 'liquidado': 3}

    def test_conversion_trae_su_denominador(self, mundo):
        e = _etapa(self._d(), 'entregado')
        assert e['conversion_anterior'] == {'tasa': 0.8, 'num': 4, 'n': 5}
        assert e['conversion_inicio']['n'] == 10

    def test_fugas_agrupadas_en_la_etapa_que_no_alcanzaron(self, mundo):
        d = self._d()
        assert [f['codigo'] for f in _etapa(d, 'recogido')['fugas']] == ['PICKING_CANCELADO']
        assert [f['codigo'] for f in _etapa(d, 'entregado')['fugas']] == ['RECHAZO_EN_RUTA']
        assert [f['codigo'] for f in _etapa(d, 'cobrado')['fugas']] == ['ENTREGADO_SIN_PAGO']
        assert [f['codigo'] for f in _etapa(d, 'recogido')['detenidos']] == ['BLOQUEO_PICKING']
        assert _etapa(d, 'recogido')['perdidas_parciales'][0]['codigo'] == 'RECOGIDO_INCOMPLETO'

    def test_el_valor_sin_dato_se_cuenta_aparte(self, mundo):
        ap = _etapa(self._d(), 'aprobado')
        assert ap['sin_valor'] == 1
        # 1501(2) 1502(2) 1503(2) 1504(2) 1505(2) 1506(1) 1507(1) 1509(1) 1510(1) líneas
        assert ap['valor'] == 14 * VALOR_LINEA

    def test_valor_sin_fuga_con_su_base(self, mundo):
        g = self._d()['guia']['valor_sin_fuga']
        # base: con valor y sin FUERA_DEL_WMS → 1501,1502,1503,1504,1505,1506,1509,1510
        assert g['pedidos_base'] == 8
        assert g['pedidos_sin_valor'] == 1
        assert g['pedidos_fuera_del_wms'] == 1
        assert g['valor_base'] == 13 * VALOR_LINEA
        assert g['valor_sin_fuga'] == 3 * VALOR_LINEA            # 1501 (2) + 1509 (1)
        assert g['tasa'] == round(3 / 13, 4)

    def test_la_tasa_sobre_cerrados_no_la_baja_lo_que_va_en_camino(self, mundo):
        g = self._d()['guia']['valor_sin_fuga_cerrados']
        # cerrados con valor: 1501,1502,1503,1504,1505,1509 → 11 líneas
        assert g['pedidos_base'] == 6
        assert g['tasa'] == round(3 / 11, 4)

    def test_ciclo_de_caja_n_y_sin_marca(self, mundo):
        c = self._d()['guia']['ciclo_caja']
        assert c['liquidados'] == 3 and c['n'] == 3 and c['sin_marca'] == 0
        assert c['mediana_dias'] is not None and c['mediana_dias'] >= 0

    def test_sin_enlazar_se_ve(self, mundo):
        s = self._d()['sin_enlazar']
        assert s['empaques'] == 1
        assert s['picking_sin_clave'] >= 1
        assert s['muestra'][0]['clave'].startswith('SIN-CLAVE-PK')
        assert {e['etapa']: e['pedidos'] for e in s['embudo']}['empacado'] == 1

    def test_meta_declara_frescura_y_corte(self, mundo):
        m = self._d()['meta']
        assert m['calculado_en'] and m['corte']
        f = m['fuentes']['historia_pedidos']
        assert f['completa'] is False and 'empieza' in f['motivo']   # historia nace hoy


class TestTiempos:
    """Con el reloj fijado en la frontera: las marcas se ajustan después del
    flujo real, porque el flujo corre en segundos."""

    def test_mediana_p90_y_ciclo(self, mundo, db):
        from app.models.packing import TareaPacking
        from app.models.pedido_historia import PedidoHistoria
        from app.models.picking import TareaPicking
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho

        t0 = datetime.utcnow() - timedelta(days=1)
        for flujo, horas_liq in ((mundo['f1'], 48), (mundo['f9'], 96)):
            clave = TareaPacking.query.get(flujo.packing_id).pedido_clave
            for h in PedidoHistoria.query.filter_by(pedido_clave=clave):
                h.primera_vez_vista_at = t0
            for t in TareaPicking.query.filter_by(pedido_clave=clave):
                t.fecha_creacion = t0 + timedelta(hours=1)
                t.fecha_completado = t0 + timedelta(hours=2)
            pk = TareaPacking.query.get(flujo.packing_id)
            pk.fecha_creacion = t0 + timedelta(hours=2)
            pk.fecha_verificado = t0 + timedelta(hours=3)
            pk.fecha_despachado = t0 + timedelta(hours=5)
            RecaudoEntrega.query.filter_by(tarea_id=pk.id).one().fecha_confirmacion = \
                t0 + timedelta(hours=24)
            RutaDespacho.query.get(flujo.ruta_id).liquidada_en = t0 + timedelta(hours=horas_liq)
        db.session.commit()
        desde, hasta, _ = _rango()
        d = _svc().recorrido(_filtros(desde=desde.isoformat(), hasta=hasta.isoformat()))
        rec = _etapa(d, 'recogido')['tiempo_desde_anterior']
        # 1501 y 1509 con 2 h; el resto sin marca de aprobado útil o con segundos
        assert rec['n'] >= 2 and rec['p90_horas'] is not None
        ciclo = d['guia']['ciclo_caja']
        dias = sorted([2.0, 4.0])
        assert ciclo['n'] == 3                      # + 1505, con segundos
        assert ciclo['p90_dias'] == dias[-1]

    def test_historia_posterior_al_wms_no_es_marca_de_aprobacion(self, mundo, db):
        from app.models.pedido_historia import PedidoHistoria
        for h in PedidoHistoria.query.filter_by(pedido_clave='003-PD-1501'):
            h.primera_vez_vista_at = datetime.utcnow() + timedelta(minutes=5)
        db.session.commit()
        p = _por_clave(_svc().cohorte(*_rango())[0])['003-PD-1501']
        assert p.entrada_fuente == 'WMS'
        assert p.marcas['aprobado'] is None and 'aprobado' in p.implicitas


class TestEstadistica:

    def test_percentil_por_rango_mas_cercano(self):
        svc = _svc()
        assert svc.percentil([], 90) is None
        assert svc.percentil([5], 90) == 5
        assert svc.percentil(list(range(1, 11)), 90) == 9
        assert svc.percentil(list(range(1, 11)), 50) == 5
        assert svc.mediana([1, 2, 3, 4]) == 2.5

    def test_sin_denominador_no_es_cero(self):
        assert _svc().tasa(0, 0) == {'tasa': None, 'num': 0, 'n': 0}
        r = _svc().resumen_tiempos([])
        assert r['n'] == 0 and r['mediana_horas'] is None and r['p90_horas'] is None


# ─────────────────────────────────────────────────────────────────────────────
# La cohorte es por día de Bogotá
# ─────────────────────────────────────────────────────────────────────────────

class TestCohorteBogota:

    def test_las_22h_de_bogota_son_ese_dia_y_no_el_siguiente(self, db, almacen):
        p, _ = sembrar_catalogo(db, almacen, n=1)
        # 2026-09-10 03:00 UTC = 2026-09-09 22:00 Bogotá
        sembrar_historia(db, p, 1601, 'NOCHE', ahora=datetime(2026, 9, 10, 3, 0))
        svc = _svc()
        dentro, _ = svc.cohorte(date(2026, 9, 9), date(2026, 9, 9))
        fuera, _ = svc.cohorte(date(2026, 9, 10), date(2026, 9, 10))
        assert [x.clave for x in dentro] == ['003-PD-1601']
        assert fuera == []


# ─────────────────────────────────────────────────────────────────────────────
# Drill-down y línea de tiempo
# ─────────────────────────────────────────────────────────────────────────────

class TestDrillDown:

    def test_en_etapa(self, mundo):
        desde, hasta, _ = _rango()
        d = _svc().pedidos_de_etapa('aprobado', _filtros(desde=desde.isoformat(),
                                                         hasta=hasta.isoformat()))
        claves = {f['clave'] for f in d['pedidos']}
        assert claves == {'003-PD-1506', '003-PD-1508', '003-PD-1510'}
        assert all(f['dias_en_etapa'] is not None for f in d['pedidos'])

    def test_fuga_y_llegaron(self, mundo):
        desde, hasta, _ = _rango()
        f = _filtros(desde=desde.isoformat(), hasta=hasta.isoformat())
        fuga = _svc().pedidos_de_etapa('entregado', f, vista='fuga')
        assert [x['clave'] for x in fuga['pedidos']] == ['003-PD-1503']
        assert 'Cliente cerrado' in fuga['pedidos'][0]['motivo']
        lleg = _svc().pedidos_de_etapa('liquidado', f, vista='llegaron')
        assert lleg['total'] == 3

    def test_paginado(self, mundo):
        desde, hasta, _ = _rango()
        f = _filtros(desde=desde.isoformat(), hasta=hasta.isoformat())
        d = _svc().pedidos_de_etapa('aprobado', f, vista='llegaron', pagina=2, por_pagina=4)
        assert d['total'] == 10 and len(d['pedidos']) == 4

    def test_linea_de_tiempo_completa(self, mundo):
        d = _svc().linea_de_tiempo('003-PD-1501')
        titulos = ' | '.join(e['titulo'] for e in d['eventos'])
        for esperado in ('Línea vista en Siesa', 'Picking completado', 'Empaque verificado',
                         'Despachado', 'Parada confirmada', 'liquidada'):
            assert esperado in titulos, titulos
        desp = next(e for e in d['eventos'] if e['titulo'].startswith('Despachado'))
        assert desp['documento']['documentos'] == ['RM-10501']
        liq = next(e for e in d['eventos'] if 'liquidada' in e['titulo'])
        assert liq['quien'] == 'Admin Recorrido'
        assert any(e['tipo'] == 'bitacora' and 'Liquidar' in e['titulo'] for e in d['eventos'])
        assert all(x['alcanzada'] for x in d['etapas'])

    def test_linea_de_tiempo_trae_la_cancelacion(self, mundo):
        d = _svc().linea_de_tiempo('003-PD-1502')
        canc = [e for e in d['eventos'] if e['tipo'] == 'bitacora']
        assert canc and all(e['detalle'] == 'El cliente anuló por teléfono' for e in canc)
        assert canc[0]['quien'] == 'Admin Recorrido'

    def test_linea_de_tiempo_del_empaque_sin_clave(self, mundo):
        from app.models.packing import TareaPacking
        pk = TareaPacking.query.get(mundo['fv'].packing_id)
        d = _svc().linea_de_tiempo(f'SIN-CLAVE-PK{pk.id}')
        assert d['pedido']['enlazado'] is False

    def test_pedido_desconocido(self, mundo):
        assert _svc().linea_de_tiempo('003-PD-99999') is None
        assert _svc().linea_de_tiempo('SIN-CLAVE-PKabc') is None


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints: rol, validación, forma
# ─────────────────────────────────────────────────────────────────────────────

def _tok(app, usuario):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


class TestEndpoints:

    URLS = ('/api/analitica/recorrido', '/api/analitica/recorrido/etapa/aprobado',
            '/api/analitica/recorrido/pedido/003-PD-1501')

    def test_solo_gestion(self, app, client, mundo):
        for url in self.URLS:
            assert client.get(url, headers=_tok(app, mundo['oper'])).status_code == 403
            assert client.get(url).status_code == 401

    @pytest.mark.parametrize('rol', ['admin', 'supervisor', 'jefe_almacen', 'gerente'])
    def test_gestion_entra(self, app, client, db, almacen, rol):
        u = _usuario(db, rol, almacen)
        r = client.get('/api/analitica/recorrido', headers=_tok(app, u))
        assert r.status_code == 200, r.get_json()

    @pytest.mark.parametrize('qs', ['desde=ayer', 'hasta=2026-13-01',
                                    'desde=2026-09-10&hasta=2026-09-01',
                                    'almacen_id=abc', 'desde=2024-01-01&hasta=2026-01-01'])
    def test_basura_es_400(self, app, client, mundo, qs):
        r = client.get(f'/api/analitica/recorrido?{qs}', headers=_tok(app, mundo['admin']))
        assert r.status_code == 400, qs

    def test_etapa_y_vista_desconocidas_son_400(self, app, client, mundo):
        h = _tok(app, mundo['admin'])
        assert client.get('/api/analitica/recorrido/etapa/volando', headers=h).status_code == 400
        assert client.get('/api/analitica/recorrido/etapa/aprobado?vista=x',
                          headers=h).status_code == 400
        assert client.get('/api/analitica/recorrido/etapa/aprobado?page=-1',
                          headers=h).status_code == 400

    def test_pedido_desconocido_404(self, app, client, mundo):
        r = client.get('/api/analitica/recorrido/pedido/003-PD-4040',
                       headers=_tok(app, mundo['admin']))
        assert r.status_code == 404

    def test_forma(self, app, client, mundo):
        d = client.get('/api/analitica/recorrido', headers=_tok(app, mundo['admin'])).get_json()
        assert [e['etapa'] for e in d['embudo']] == list(_svc().ETAPAS)
        assert set(d) >= {'pedidos', 'embudo', 'guia', 'sin_enlazar', 'meta', 'definiciones'}
        assert set(d['meta']) >= {'desde', 'hasta', 'almacen_id', 'calculado_en', 'fuentes'}

    def test_filtro_por_almacen(self, app, client, mundo, db):
        from app.models.almacen import Almacen
        otro = Almacen(codigo='ALM-OTRO', nombre='Otro', bodega_siesa_id='PC1', activo=True)
        db.session.add(otro)
        db.session.commit()
        h = _tok(app, mundo['admin'])
        propio = client.get(f'/api/analitica/recorrido?almacen_id={mundo["almacen"].id}',
                            headers=h).get_json()
        ajeno = client.get(f'/api/analitica/recorrido?almacen_id={otro.id}', headers=h).get_json()
        assert propio['pedidos'] == 10 and ajeno['pedidos'] == 0
        assert propio['meta']['almacen_id'] == mundo['almacen'].id


# ─────────────────────────────────────────────────────────────────────────────
# La pantalla — render real en Node con util.js real
# ─────────────────────────────────────────────────────────────────────────────

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], modo = args[1] || '', datos = JSON.parse(fs.readFileSync(args[2], 'utf8'));
const els = {};
const mkEl = (id) => (els[id] = els[id] || { id, innerHTML: '', value: '', style: {}, dataset: {},
  classList: { toggle(){}, add(){}, remove(){} }, querySelectorAll: () => [], appendChild(){} });
const ctx = { console, window: {}, localStorage: { getItem: () => null, setItem(){} },
  document: { getElementById: (id) => mkEl(id), querySelectorAll: () => [] },
  getComputedStyle: () => ({ getPropertyValue: () => '#888' }),
  setTimeout, clearTimeout };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
// Lo que viene de app.js se stubbea; util.js (esc) va de verdad.
vm.runInContext(`var API=''; var TOKEN='t'; var ALMACEN_ID=1;
  async function get(u){ return globalThis.__resp(u); } function alerta(){}`, ctx);
ctx.__resp = (u) => {
  if (u.startsWith('/api/almacenes')) return datos.almacenes;
  if (u.includes('/recorrido/pedido/')) return datos.pedido;
  if (u.includes('/recorrido/etapa/')) return datos.etapa;
  return datos.recorrido;
};
vm.runInContext(fs.readFileSync(base + '/analitica.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/analitica_recorrido.js', 'utf8'), ctx);
vm.runInContext('function anFugasCargar(el){el.innerHTML="fugas"} function anSaludCargar(el){el.innerHTML="salud"} function anBitacoraCargar(el){el.innerHTML="bitacora"}', ctx);
if (modo === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
(async () => {
  const html = {
    resumen: vm.runInContext('anRecHtml', ctx)(datos.recorrido),
    etapa: vm.runInContext('anRecEtapaHtml', ctx)(datos.etapa),
    pedido: vm.runInContext('anRecPedidoHtml', ctx)(datos.pedido),
  };
  await vm.runInContext('cargarAnalitica', ctx)();
  const shell = els['tab-analitica'] ? els['tab-analitica'].innerHTML : '';
  const todo = html.resumen + html.etapa + html.pedido;
  const handlers = [...new Set([...(todo + shell).matchAll(/onclick="([A-Za-z_$][\w$]*)\(/g)].map(m => m[1]))];
  const f = vm.runInContext('anFiltros', ctx)();
  console.log(JSON.stringify({
    crudos: (todo.match(/<img/g) || []).length,
    escapados: (todo.match(/&lt;img/g) || []).length,
    handlers, sinDefinir: handlers.filter(h => typeof ctx[h] !== 'function'),
    datoEnOnclick: /onclick="[^"]*\('[^"]*(<|&lt;)/.test(todo),
    shellTieneFiltros: ['an-f-almacen', 'an-f-desde', 'an-f-hasta'].every(i => shell.includes(i)),
    subtabs: ['Recorrido', 'Fugas', 'Salud', 'Bitácora'].map(t => shell.indexOf(t)),
    filtros: f,
    pesos: [vm.runInContext('anPesos', ctx)(null), vm.runInContext('anPesos', ctx)(1234567)],
    pct: [vm.runInContext('anPct', ctx)(0.5, 0), vm.runInContext('anPct', ctx)(0.82, 340)],
    frescura: vm.runInContext('anFrescura', ctx)(datos.recorrido.meta),
    sinValor: html.resumen.includes('sin valor'), sinEnlazar: html.resumen.includes('sin clave'),
    textos: ['Ciclo de caja', 'sin fuga', 'Aprobado en Siesa', 'Liquidado'].map(t => html.resumen.indexOf(t)),
    fontMenor: /font-size:\s*(\d|1[01])px/.test(todo + shell),
  }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture
def datos_pantalla(app, client, mundo, tmp_path, db):
    """Las respuestas REALES de los endpoints, con un dato hostil adentro."""
    from app.models.pedido_historia import PedidoHistoria
    from app.models.packing import TareaPacking
    X = '<img src=x onerror=alert(1)>'
    for h in PedidoHistoria.query.all():
        h.cliente = X
    for pk in TareaPacking.query.all():
        pk.cliente = X
    db.session.commit()
    h = _tok(app, mundo['admin'])
    d = {
        'recorrido': client.get('/api/analitica/recorrido', headers=h).get_json(),
        'etapa': client.get('/api/analitica/recorrido/etapa/aprobado?vista=llegaron',
                            headers=h).get_json(),
        'pedido': client.get('/api/analitica/recorrido/pedido/003-PD-1503',
                             headers=h).get_json(),
        'almacenes': [{'id': 1, 'nombre': X, 'codigo': X}],
    }
    d['recorrido']['embudo'][0]['fugas'].append({'codigo': X, 'motivo': X, 'pedidos': 1,
                                                 'valor': None, 'sin_valor': 1})
    d['pedido']['eventos'].append({'en': None, 'dia': None, 'etapa': None, 'titulo': X,
                                   'detalle': X, 'quien': X, 'documento': {'documentos': [X],
                                   'resultado': X}, 'tipo': 'bitacora'})
    p = tmp_path / 'datos.json'
    p.write_text(json.dumps(d), encoding='utf-8')
    return p


def _render(datos, modo=''):
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa), modo, str(datos)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLaPantalla:

    def test_ningun_dato_llega_crudo(self, datos_pantalla):
        r = _render(datos_pantalla)
        assert r['crudos'] == 0, r
        assert r['escapados'] >= 10, f'piso: el arnés dejó de pintar ({r})'

    def test_el_arnes_muerde_con_esc_roto(self, datos_pantalla):
        assert _render(datos_pantalla, 'esc-roto')['crudos'] >= 10

    def test_botones_llaman_funciones_que_existen_y_sin_datos_en_onclick(self, datos_pantalla):
        r = _render(datos_pantalla)
        assert r['sinDefinir'] == [], r
        assert {'anSubtab', 'anActualizar', 'anRecEtapa', 'anRecPedido'} <= set(r['handlers'])
        assert r['datoEnOnclick'] is False

    def test_shell_filtros_y_subpestanas_en_orden(self, datos_pantalla):
        r = _render(datos_pantalla)
        assert r['shellTieneFiltros']
        assert all(p >= 0 for p in r['subtabs']) and r['subtabs'] == sorted(r['subtabs'])
        f = r['filtros']
        assert set(f) == {'almacen_id', 'desde', 'hasta'}
        assert len(f['desde']) == 10 and len(f['hasta']) == 10 and f['almacen_id'] == ''

    def test_formatos_no_inventan_ceros(self, datos_pantalla):
        r = _render(datos_pantalla)
        assert r['pesos'][0] == 'sin dato'
        assert '—' in r['pct'][0] and '340' in r['pct'][1] and '82' in r['pct'][1]
        assert 'empieza' in r['frescura']             # la fuente incompleta se dice

    def test_resumen_declara_lo_que_no_sabe(self, datos_pantalla):
        r = _render(datos_pantalla)
        assert r['sinValor'] and r['sinEnlazar']
        assert all(p >= 0 for p in r['textos']), r['textos']
        assert r['fontMenor'] is False


# ─────────────────────────────────────────────────────────────────────────────
# El shell en la aplicación: pestaña, rol, orden, service worker
# ─────────────────────────────────────────────────────────────────────────────

class TestLaPestanaEnLaApp:

    def _leer(self, nombre):
        return (RAIZ / 'app' / 'static' / 'pwa' / nombre).read_text(encoding='utf-8')

    def test_la_ven_los_mismos_roles_que_el_servidor_deja_pasar(self):
        import re
        from app.routes._auth_helpers import Roles
        m = re.search(r"const _ROLES_ANALITICA = \[([^\]]+)\]", self._leer('app.js'))
        assert m, 'falta _ROLES_ANALITICA en app.js'
        assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == set(Roles.GESTION)

    def test_el_orden_de_tabs_es_el_del_nav(self):
        """`tab()` marca la activa por POSICIÓN: `TABS[i]` contra el i-ésimo
        `.nav-tab`. Una pestaña insertada en un sitio del HTML y en otro del
        arreglo resalta la equivocada — el guard de integridad compara
        conjuntos y no lo ve."""
        import re
        html_tabs = re.findall(r"class=\"nav-tab[^\"]*\" onclick=\"tab\('(tab-[^']+)'\)\"",
                               self._leer('index.html'))
        assert len(html_tabs) >= 20, html_tabs
        m = re.search(r"const TABS = \[([^\]]+)\]", self._leer('app.js'))
        assert re.findall(r"'(tab-[^']+)'", m.group(1)) == html_tabs

    def test_carga_al_entrar_y_nunca_por_el_timer(self):
        app_js = self._leer('app.js')
        assert "TAB === 'tab-analitica') { if (!desdeTimer) await cargarAnalitica(); }" in app_js

    def test_shell_y_vista_cargan_en_orden_y_se_cachean(self):
        html = self._leer('index.html')
        i_shell = html.index('/static/pwa/analitica.js?v=')
        i_vista = html.index('/static/pwa/analitica_recorrido.js?v=')
        assert html.index('/static/pwa/flota_analitica.js') < i_shell < i_vista
        sw = self._leer('sw.js')
        assert "'/static/pwa/analitica.js'" in sw and "'/static/pwa/analitica_recorrido.js'" in sw

    def test_contenedor_de_la_pestana(self):
        assert '<div id="tab-analitica" class="admin-content" style="display:none;">' \
            in self._leer('index.html')
