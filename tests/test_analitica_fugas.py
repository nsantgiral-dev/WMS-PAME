"""
Analítica → 💸 Fugas (`app/services/analitica_fugas.py`,
`GET /api/analitica/fugas[/<clave_fuga>]`, `analitica_fugas.js`).

## Qué protege

1. **Sin valor ≠ 0.** Un caso sin precio o costo no suma y se cuenta aparte;
   una fuga donde ningún caso tiene valor sale con `pesos = None`, nunca 0.
2. **Período anterior de igual duración.**
3. **AV1 y TRA1 nunca son vendibles**: el limbo mira solo bodegas de servicio;
   el caso medido del Armador (`NB1 100 + AV1 40 + TRA1 25`) da 65, no 165.
4. **«Fugas del período» suma solo fugas con valor conocido** y dice cuántas
   quedan fuera. Una fuga neta negativa (sobrantes) no le resta al total.

## El mundo

Cada fuga se arma con el servicio que la operación llama — `reportar_problema`
(agotado), la cadena de conteo real con su job, `confirmar_parada` y
`entregar_ruta` (el arnés de flujo), `cancelar_picking`/`reabrir_picking`/
`PackingService.cancelar` (bitácora), `fotografiar_stock` (limbo) y
`SiesaJob.marcar_fallo` (documentos). Lo único que se escribe a mano es lo que
en la operación escribe **otro** proceso: `valor_factura` (la foto de ventas
o la pantalla del conductor) y el costo de la foto de NB1 (InvFecha).
"""
import json
import pathlib
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from tests.flujo import conductor_de_flujo as cf
from tests.test_conteo_teorico_pos import SKU, siesa, tienda  # noqa: F401 (fixtures)
from tests.test_estadisticas_conteo import (_abrir_y_contar, _abrir_y_contar_cc1,
                                            _hueco, _nuevo_cc1, _poner)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SERVICIO = RAIZ / 'app' / 'services' / 'analitica_fugas.py'


def _svc():
    from app.services import analitica_fugas
    return analitica_fugas


def _hoy():
    from app.utils.fecha import dia_operativo
    return dia_operativo()


def _rango():
    hoy = _hoy()
    return hoy - timedelta(days=29), hoy


def _fugas(almacen_id=None):
    d, h = _rango()
    r = _svc().calcular_fugas(d, h, almacen_id)
    return r, {f['clave']: f for f in r['fugas']}


def _tok(app, usuario):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


def _usuario(db, almacen, rol, email):
    from app.models.usuario import Usuario
    u = Usuario.query.filter_by(email=email).first()
    if not u:
        u = Usuario(nombre=f'Persona {rol}', email=email, rol=rol, activo=True,
                    almacen_id=almacen.id)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()
    return u


# ─────────────────────────────────────────────────────────────────────────────
# Constructores de casos (servicios reales)
# ─────────────────────────────────────────────────────────────────────────────

def _agotado(db, almacen, operario, precio=None, categoria=None, tipo='PEDIDO',
             encontrada=2, cantidad=5):
    """Un picking que el operario reporta FALTANTE: escribe el evento de agotado
    y deja la tarea BLOQUEADA con lo encontrado recogido."""
    from app.services.picking_service import PickingService
    productos, _ = cf.sembrar_catalogo(db, almacen, n=1, con_stock=cantidad)
    p = productos[0]
    p.precio_venta = precio or 0
    p.categoria = categoria
    db.session.commit()
    tareas = PickingService.crear_tareas(producto_id=p.id, cantidad=cantidad,
                                         almacen_id=almacen.id,
                                         referencia_documento='PD9001' if tipo != 'TRASLADO' else 'ST-1',
                                         tipo_documento=tipo)
    t = tareas[0]
    PickingService.iniciar_picking(t.id, operario.id)
    PickingService.reportar_problema(t.id, operario.id, 'FALTANTE', cantidad_encontrada=encontrada)
    db.session.commit()
    return t


def _entrega(db, almacen, actores, valor_factura=None, **datos):
    flujo = cf.flujo_completo(db, almacen, actores['op'].id, actores['cond'].id, **datos)
    from app.models.packing import TareaPacking
    t = db.session.get(TareaPacking, flujo.packing_id)
    if valor_factura is not None:
        t.valor_factura = valor_factura
    db.session.commit()
    return flujo, t


@pytest.fixture
def actores(db, almacen):
    return {'op': _usuario(db, almacen, 'operario', 'op-fugas@test.com'),
            'cond': _usuario(db, almacen, 'conductor', 'cond-fugas@test.com'),
            'sup': _usuario(db, almacen, 'supervisor', 'sup-fugas@test.com'),
            'admin': _usuario(db, almacen, 'admin', 'admin-fugas@test.com')}


def _job_fallido(db, tipo, payload, dias_atras=0, **ref):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob.encolar(tipo, payload, **ref)
    db.session.flush()
    while j.estado != 'FALLIDO':
        j.marcar_fallo('Siesa rechazó')
    if dias_atras:
        j.fecha_creacion = datetime.utcnow() - timedelta(days=dias_atras)
    db.session.commit()
    return j


def _stock(db, bodega, codigo, existencia):
    from app.models.stock_siesa import StockSiesa
    db.session.add(StockSiesa(bodega=bodega, codigo_siesa=codigo, existencia=existencia,
                              updated_at=datetime.utcnow()))
    db.session.commit()


class _SinSiesa:
    modo_simulacion = False

    def _get(self, *a, **k):
        raise AssertionError('la foto sin costo no debería consultar Siesa')


def _fotografiar(*bodegas):
    from app.services import fotos_siesa_service as f
    dia = f.dia_operativo()
    for b in bodegas:
        c = f.fotografiar_stock(b, dia, _SinSiesa(), costo=False)
        assert c['completa'], c
    return dia


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Venta perdida
# ─────────────────────────────────────────────────────────────────────────────

class TestVentaPerdida:

    def test_con_precio_suma_sin_precio_se_declara(self, db, almacen, actores):
        _agotado(db, almacen, actores['op'], precio=2000, categoria='Cuadernos')   # 3 × 2000
        _agotado(db, almacen, actores['op'], precio=None, categoria='Lápices')     # 3 sin precio
        _agotado(db, almacen, actores['op'], precio=9999, tipo='TRASLADO')          # no es venta
        _, f = _fugas()
        vp = f['venta_perdida']
        assert vp['casos'] == 2
        assert vp['pesos'] == 6000.0
        assert vp['unidades'] == 6
        assert vp['sin_valor'] == {'casos': 1, 'unidades': 3}
        assert vp['es_cota_inferior'] is True
        assert {m['motivo'] for m in vp['por_motivo']} == {'Cuadernos', 'Lápices'}

    def test_lee_la_misma_politica_que_el_tablero(self, db, almacen, actores):
        """El total de la fuga es el de `calcular_venta_perdida`: una política."""
        from app.services.metricas.venta_perdida import calcular_venta_perdida
        _agotado(db, almacen, actores['op'], precio=1500)
        _agotado(db, almacen, actores['op'], precio=None)
        _agotado(db, almacen, actores['op'], precio=700, tipo='TRASLADO')
        d, h = _rango()
        tablero = calcular_venta_perdida(almacen.id, d, h)
        _, f = _fugas(almacen.id)
        assert f['venta_perdida']['pesos'] == tablero['venta_perdida_total']
        assert f['venta_perdida']['sin_valor']['casos'] == tablero['sin_precio']['eventos']

    def test_solo_sin_precio_es_none_no_cero(self, db, almacen, actores):
        _agotado(db, almacen, actores['op'], precio=None)
        _, f = _fugas()
        assert f['venta_perdida']['pesos'] is None
        assert f['venta_perdida']['aporte_al_total'] is None
        assert 'precio' in f['venta_perdida']['fuera_del_total_por']


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Faltantes de inventario (la cadena de conteo real)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def ajustes(db, siesa, tienda, monkeypatch):
    """Tres cadenas ajustadas: faltante de 2 a $1.500, sobrante de 1 a $1.000 y
    faltante de 1 sin costo (aprobado por supervisión: sin costo no sale solo)."""
    from app.models.siesa_job import SiesaJob
    from app.services.connekta_gateway import ConnektaGateway
    from app.services.conteo_service import ConteoService
    from app.services.siesa_job_service import _ejecutar_job
    monkeypatch.setattr(ConnektaGateway, 'enviar_ajuste_inventario',
                        lambda self, **kw: {'codigo': 0})
    a, b, sup = tienda['a'], tienda['b'], tienda['supervisor']
    ids = {}

    def _cadena(nombre, n, fisico, costo, aprobar=False):
        _poner(siesa, 10, costo=costo)
        cc1 = _nuevo_cc1(tienda, _hueco(db, tienda, n))
        r = _abrir_y_contar_cc1(cc1, a, fisico)
        r2 = _abrir_y_contar(r['segundo_conteo_id'], b, fisico)
        if aprobar:
            ConteoService.confirmar_ajuste(cc1, sup.id)
        else:
            assert r2['auto_encolado'] is True, r2
        job = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=cc1).one()
        _ejecutar_job(job)
        ids[nombre] = cc1

    _cadena('faltante', 71, 8, 1500)
    _cadena('sobrante', 72, 11, 1000)
    _cadena('sin_costo', 73, 9, None, aprobar=True)
    db.session.expire_all()
    return ids


class TestFaltantesDeInventario:

    def test_neto_con_los_dos_lados_y_sin_costo_aparte(self, ajustes, tienda):
        _, f = _fugas()
        fi = f['faltantes_inventario']
        assert fi['casos'] == 3
        assert fi['pesos'] == 2000.0, 'faltantes 3.000 − sobrantes 1.000'
        assert fi['extra']['faltantes']['pesos'] == 3000.0
        assert fi['extra']['sobrantes']['pesos'] == 1000.0
        assert fi['sin_valor']['casos'] == 1
        assert fi['unidades'] == 2, 'unidades netas perdidas: 2 − 1 + 1'
        por = {m['motivo']: m for m in fi['por_motivo']}
        assert por['Faltante']['casos'] == 2 and por['Sobrante']['pesos'] == -1000.0

    def test_es_el_mismo_valor_que_las_estadisticas(self, ajustes, tienda):
        from app.services.metricas import conteo as mc
        d, h = _rango()
        bloque, _ = mc._ajustes(mc._cargar_cadenas(None), d, h)
        _, f = _fugas()
        # Mismo número, mirado desde la plata: el reporte de conteo da el neto
        # del inventario (entradas − salidas), la fuga lo que se perdió.
        assert f['faltantes_inventario']['pesos'] == -bloque['valor']['neto']
        assert f['faltantes_inventario']['extra']['neto_perdido_pesos'] == -bloque['valor']['neto']

    def test_neto_negativo_no_resta_del_total(self, db, siesa, tienda, monkeypatch):
        from app.models.siesa_job import SiesaJob
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.siesa_job_service import _ejecutar_job
        monkeypatch.setattr(ConnektaGateway, 'enviar_ajuste_inventario',
                            lambda self, **kw: {'codigo': 0})
        _poner(siesa, 10, costo=1000)
        cc1 = _nuevo_cc1(tienda, _hueco(db, tienda, 81))
        r = _abrir_y_contar_cc1(cc1, tienda['a'], 13)
        _abrir_y_contar(r['segundo_conteo_id'], tienda['b'], 13)
        _ejecutar_job(SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=cc1).one())
        _job_fallido(db, 'RECIBO_CAJA', {'monto': 5000})
        res, f = _fugas()
        assert f['faltantes_inventario']['pesos'] == -3000.0
        assert f['faltantes_inventario']['aporte_al_total'] == 0.0
        assert res['resumen']['total_pesos'] == 5000.0


# ─────────────────────────────────────────────────────────────────────────────
# 3, 4, 5 · Ruta: rechazos, entregado sin pago, plata en la calle
# ─────────────────────────────────────────────────────────────────────────────

def _foto_precio(db, pedido_clave, referencia, cantidad, neto):
    from app.models.fotos_siesa import FotoVentaLinea
    import random
    db.session.add(FotoVentaLinea(
        f470_rowid=random.randint(1, 10 ** 9), run_id='t', completa=True,
        dia_operativo=_hoy(), co='003', pedido_clave=pedido_clave, referencia=referencia,
        cantidad=cantidad, vlr_neto=neto, estado_docto=1))
    db.session.commit()


@pytest.fixture
def ruta(db, almacen, actores):
    """Cinco paradas, una por forma de terminar."""
    from app.models.packing import TareaPacking
    from app.models.producto import Producto
    from app.services.ruta_service import RutaService
    rech = dict(forma_pago=None, monto_cobrado=0, observaciones='no estaba')
    out = {}
    out['rechazo'] = _entrega(db, almacen, actores, valor_factura=50000,
                              estado_entrega='RECHAZADO', motivo_rechazo='CLIENTE_CERRADO', **rech)
    out['rechazo_sin_valor'] = _entrega(db, almacen, actores, valor_factura=None,
                                        estado_entrega='RECHAZADO', motivo_rechazo='NO_PIDIO', **rech)

    # Parcial: el packing trae 7 de cada producto; vuelven 2 del primero.
    def _parcial(con_precio):
        productos, _ = cf.sembrar_catalogo(db, almacen)
        pedido = cf.sembrar_pedido(db, productos)
        fl = cf.Flujo(pedido=pedido, almacen_id=almacen.id, usuario_id=actores['op'].id,
                      producto_ids=[p.id for p in productos])
        cf.hacer_picking(db, fl, 10, 7)
        cf.hacer_packing(db, fl)
        cf.hacer_ruta(db, fl, actores['cond'].id)
        t = db.session.get(TareaPacking, fl.packing_id)
        # La clave la escribe `crear_desde_picking` (Fase 0); la del arnés
        # sale de un consecutivo fijo, así que se vuelve única por parada.
        t.pedido_clave = f'{t.pedido_clave or "003-PD-1"}-{fl.packing_id}'
        db.session.commit()
        items = [{'codigo': p.codigo, 'cantidad_pedida': 7,
                  'cantidad_entregada': 5 if i == 0 else 7} for i, p in enumerate(productos)]
        if con_precio:
            _foto_precio(db, t.pedido_clave, productos[0].codigo_siesa, 7, 7000)
        cf.hacer_entrega(db, fl, estado_entrega='PARCIAL', forma_pago='EFECTIVO',
                         monto_cobrado=500, items_entregados=items,
                         observaciones='devolvió dos')
        return fl, t

    out['parcial'] = _parcial(True)
    out['parcial_sin_precio'] = _parcial(False)
    out['sin_pago'] = _entrega(db, almacen, actores, valor_factura=80000,
                               estado_entrega='RECHAZADO', motivo_rechazo='NO_PAGO_SE_QUEDO', **rech)
    fl, t = _entrega(db, almacen, actores, valor_factura=31000,
                     estado_entrega='ENTREGADO', forma_pago='EFECTIVO', monto_cobrado=30000)
    RutaService.entregar_ruta(fl.ruta_id, {}, actores['sup'].id)
    out['calle'] = (fl, t)
    assert Producto.query.count() >= 5
    return out


class TestRechazosEnRuta:

    def test_total_y_parcial_valorizados_sin_precio_aparte(self, ruta):
        _, f = _fugas()
        r = f['devoluciones_ruta']
        assert r['casos'] == 4
        assert r['pesos'] == 52000.0, 'rechazo 50.000 + parcial 2 × 1.000'
        assert r['sin_valor']['casos'] == 2
        from app.services import motivos_rechazo
        motivos = {m['motivo']: m for m in r['por_motivo']}
        assert motivos[motivos_rechazo.etiqueta('CLIENTE_CERRADO')]['pesos'] == 50000.0
        assert motivos[motivos_rechazo.etiqueta('NO_PIDIO')]['sin_valor'] == 1
        assert motivos['Entrega parcial']['casos'] == 2 and motivos['Entrega parcial']['pesos'] == 2000.0

    def test_cada_caso_trae_su_nota_credito_y_su_clave(self, ruta):
        d, h = _rango()
        det = _svc().detalle_fuga('devoluciones_ruta', d, h)
        assert all(c['detalle']['nota_credito'] for c in det['casos'])
        assert any(c['pedido_clave'] for c in det['casos'])

    def test_el_sin_pago_no_es_rechazo(self, ruta):
        _, f = _fugas()
        sp = f['entregado_sin_pago']
        assert sp['casos'] == 1 and sp['pesos'] == 80000.0
        assert sp['por_motivo'][0]['motivo'] == '0–2 días'


class TestPlataEnLaCalle:

    def test_ruta_entregada_sin_liquidar_con_lo_cobrado(self, ruta):
        _, f = _fugas()
        p = f['plata_en_la_calle']
        assert p['casos'] == 1
        assert p['pesos'] == 30000.0
        assert p['extra']['cartera_abierta_por_no_liquidar']['pesos'] == 31000.0
        assert p['por_motivo'][0]['motivo'] == 'Al día'

    def test_liquidada_sale(self, db, ruta):
        from app.models.ruta_despacho import RutaDespacho
        r = db.session.get(RutaDespacho, ruta['calle'][0].ruta_id)
        r.estado_financiero = 'LIQUIDADA'
        db.session.commit()
        _, f = _fugas()
        assert f['plata_en_la_calle']['casos'] == 0


# ─────────────────────────────────────────────────────────────────────────────
# 6 · Mercancía en limbo
# ─────────────────────────────────────────────────────────────────────────────

class TestMercanciaEnLimbo:

    def _mundo(self, db):
        from app.models.fotos_siesa import FotoStockDiaria
        _stock(db, 'NB1', 'SKU-X', 100)
        _stock(db, 'AV1', 'SKU-X', 40)
        _stock(db, 'TRA1', 'SKU-Y', 25)
        dia = _fotografiar('NB1', 'AV1', 'TRA1')
        nb1 = FotoStockDiaria.query.filter_by(dia_operativo=dia, bodega='NB1',
                                              codigo_siesa='SKU-X').one()
        nb1.costo_prom_uni = 500
        db.session.commit()

    def test_solo_bodegas_de_servicio_nunca_vendible(self, db):
        """El caso medido del Armador: 100 + 40 + 25 → lo que está en limbo es 65."""
        self._mundo(db)
        _, f = _fugas()
        lim = f['mercancia_en_limbo']
        assert lim['unidades'] == 65.0
        assert lim['casos'] == 2
        assert lim['pesos'] == 20000.0, '40 × costo de NB1'
        assert lim['sin_valor']['casos'] == 1, 'TRA1 sin costo en NB1'
        assert {m['motivo'] for m in lim['por_motivo']} == {'Averías (AV1)', 'En tránsito (TRA1)'}

    def test_las_bodegas_de_limbo_no_son_operadas(self):
        from app.services.inventario_siesa_service import _BODEGAS_PV
        assert set(_svc().bodegas_de_limbo()) == {'AV1', 'TRA1'}
        assert not set(_svc().bodegas_de_limbo()) & set(_BODEGAS_PV)

    def test_sin_foto_es_no_sabemos(self, db):
        res, f = _fugas()
        lim = f['mercancia_en_limbo']
        assert lim['sin_dato'] and lim['pesos'] is None and lim['casos'] is None
        assert lim['estado'] == 'sin_dato', 'no saber no es rojo'
        assert any(x['clave'] == 'mercancia_en_limbo' for x in res['resumen']['fuera_del_total'])

    def test_con_almacen_no_suma(self, db, almacen):
        self._mundo(db)
        _, f = _fugas(almacen.id)
        assert f['mercancia_en_limbo']['aporte_al_total'] is None
        assert 'almacén' in f['mercancia_en_limbo']['fuera_del_total_por']


# ─────────────────────────────────────────────────────────────────────────────
# 7 · Trabajo perdido
# ─────────────────────────────────────────────────────────────────────────────

class TestTrabajoPerdido:

    def test_solo_lo_que_ya_se_habia_hecho(self, db, almacen, actores):
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        # Recogió 2 y se reabrió: trabajo perdido.
        t1 = _agotado(db, almacen, actores['op'], encontrada=2)
        PickingService.reabrir_picking(t1.id, actores['sup'].id, motivo='se equivocó de hueco')
        # Recogió 3 y se canceló: trabajo perdido.
        t2 = _agotado(db, almacen, actores['op'], encontrada=3)
        PickingService.cancelar_picking(t2.id, motivo='cliente anuló', usuario_id=actores['sup'].id)
        # Nadie la había tocado: no es trabajo perdido.
        productos, _ = cf.sembrar_catalogo(db, almacen, n=1)
        t3 = PickingService.crear_tareas(producto_id=productos[0].id, cantidad=2,
                                         almacen_id=almacen.id, referencia_documento='PD9002',
                                         tipo_documento='PEDIDO')[0]
        PickingService.cancelar_picking(t3.id, motivo='duplicada', usuario_id=actores['sup'].id)
        # Empaque empezado y cancelado; y uno sin empezar.
        for empezar in (True, False):
            productos, _ = cf.sembrar_catalogo(db, almacen, n=1)
            pedido = cf.sembrar_pedido(db, productos, cantidad=4)
            fl = cf.Flujo(pedido=pedido, almacen_id=almacen.id, usuario_id=actores['op'].id,
                          producto_ids=[p.id for p in productos])
            cf.hacer_picking(db, fl, 4, 4)
            tp = PackingService.crear_desde_picking(
                tareas_picking_ids=fl.pickings, numero_pedido_siesa=pedido,
                almacen_id=almacen.id, tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='9')
            if empezar:
                PackingService.iniciar(tp.id, actores['op'].id)
                PackingService.escanear_item(tp.id, productos[0].id, 4)
            PackingService.cancelar(tp.id, motivo='pedido anulado', usuario_id=actores['sup'].id)
        db.session.commit()

        _, f = _fugas()
        tp_ = f['trabajo_perdido']
        assert tp_['casos'] == 3
        assert tp_['unidades'] == 2 + 3 + 4
        assert tp_['pesos'] is None, 'es trabajo, no mercancía: sin valor, nunca $0'
        assert tp_['sin_valor']['casos'] == 3
        d, h = _rango()
        det = _svc().detalle_fuga('trabajo_perdido', d, h)
        ques = sorted(c['detalle']['que'] for c in det['casos'])
        assert ques == ['Empaque cancelado', 'Picking cancelado', 'Picking reabierto']
        assert all(c['detalle']['lo_tiro'] == 'Persona supervisor' for c in det['casos'])


# ─────────────────────────────────────────────────────────────────────────────
# 8 · Documentos trabados
# ─────────────────────────────────────────────────────────────────────────────

class TestDocumentosTrabados:

    def test_con_valor_sin_valor_y_lo_que_no_es_documento(self, db):
        from app.models.siesa_job import SiesaJob
        _job_fallido(db, 'RECIBO_CAJA', {'monto': 12345})
        _job_fallido(db, 'TRASLADO_AVERIAS', {'x': 1})
        _job_fallido(db, 'ALERTA_EMAIL', {'para': 'x'})
        SiesaJob.encolar('RECIBO_CAJA', {'monto': 999})          # pendiente: no está trabado
        db.session.commit()
        _, f = _fugas()
        doc = f['documentos_trabados']
        assert doc['casos'] == 2
        assert doc['pesos'] == 12345.0
        assert doc['sin_valor']['casos'] == 1
        assert doc['extra']['fallidos_hoy'] == 2
        assert {m['motivo'] for m in doc['por_motivo']} == {'Recibo de caja', 'Traslado a averías'}

    def test_despacho_toma_el_valor_de_la_factura(self, db, almacen, actores):
        flujo, t = _entrega(db, almacen, actores, valor_factura=44000)
        _job_fallido(db, 'DESPACHO_F470', {'tarea_id': t.id},
                     referencia_tipo='TareaPacking', referencia_id=t.id)
        _, f = _fugas(almacen.id)
        assert f['documentos_trabados']['pesos'] == 44000.0


# ─────────────────────────────────────────────────────────────────────────────
# Reglas transversales
# ─────────────────────────────────────────────────────────────────────────────

class TestPeriodoAnteriorDeIgualDuracion:

    @pytest.mark.parametrize('desde,hasta,esperado', [
        (date(2026, 9, 1), date(2026, 9, 30), (date(2026, 8, 2), date(2026, 8, 31))),
        (date(2026, 9, 24), date(2026, 9, 24), (date(2026, 9, 23), date(2026, 9, 23))),
        (date(2026, 3, 1), date(2026, 3, 7), (date(2026, 2, 22), date(2026, 2, 28))),
    ])
    def test_misma_cantidad_de_dias_inmediatamente_antes(self, desde, hasta, esperado):
        ant = _svc().periodo_anterior(desde, hasta)
        assert ant == esperado
        assert (ant[1] - ant[0]) == (hasta - desde)
        assert ant[1] + timedelta(days=1) == desde

    def test_el_borde_del_periodo_anterior(self, db):
        """Rango de 30 días: el anterior empieza 30 días antes de `desde`."""
        d, h = _rango()
        from app.utils.fecha import inicio_del_dia_utc
        _job_fallido(db, 'RECIBO_CAJA', {'monto': 100})                       # actual
        j_ini = _job_fallido(db, 'RECIBO_CAJA', {'monto': 7})                  # primer día anterior
        j_ini.fecha_creacion = inicio_del_dia_utc(d - timedelta(days=30)) + timedelta(hours=1)
        j_fuera = _job_fallido(db, 'RECIBO_CAJA', {'monto': 5000})             # un día antes: fuera
        j_fuera.fecha_creacion = inicio_del_dia_utc(d - timedelta(days=31)) + timedelta(hours=1)
        db.session.commit()
        _, f = _fugas()
        t = f['documentos_trabados']['tendencia']
        assert t['anterior'] == {'pesos': 7.0, 'casos': 1, 'unidades': 1}
        assert t['direccion'] == 'sube' and t['delta_pesos'] == 93.0


class TestElTotalSoloSumaLoConocido:

    def test_total_declara_lo_que_queda_fuera(self, db, almacen, actores):
        _job_fallido(db, 'RECIBO_CAJA', {'monto': 1000})
        _agotado(db, almacen, actores['op'], precio=None)       # venta perdida sin valor
        res, f = _fugas()
        r = res['resumen']
        assert r['total_pesos'] == 1000.0
        fuera = {x['clave'] for x in r['fuera_del_total']}
        assert 'venta_perdida' in fuera
        assert 'mercancia_en_limbo' in fuera, 'sin foto: no sabemos, fuera del total'
        assert r['fugas_fuera_del_total'] == len(r['fuera_del_total'])
        assert r['es_cota_inferior'] is True
        suman = sum(x['aporte_al_total'] for x in res['fugas'] if x['aporte_al_total'] is not None)
        assert r['total_pesos'] == round(suman, 2)

    def test_sin_valor_relevante_va_adelante(self):
        def _f(clave, pesos, casos, sv=0, sin_dato=None):
            return {'clave': clave, 'pesos': pesos, 'casos': casos,
                    'sin_valor': {'casos': sv}, 'sin_dato': sin_dato}
        orden = [f['clave'] for f in _svc().ordenar([
            _f('chica', 10.0, 2), _f('grande', 900.0, 4), _f('vacia', 0.0, 0),
            _f('sinvalor_mucho', None, 5, 5), _f('sinvalor_poco', None, 1, 1),
            _f('sindato', None, None, None, 'sin foto')])]
        assert orden[:2] == ['sinvalor_mucho', 'sindato'] or orden[:2] == ['sindato', 'sinvalor_mucho']
        assert orden[2:4] == ['grande', 'chica']
        assert orden[4:] == ['sinvalor_poco', 'vacia']

    def test_totales_sin_valor_no_es_cero(self):
        s = _svc()
        solo_sin = s.totales(s.Resultado([s.Caso('x', None, 3, None, 'm')]))
        assert solo_sin['pesos'] is None and solo_sin['sin_valor']['casos'] == 1
        vacio = s.totales(s.Resultado([]))
        assert vacio['pesos'] == 0.0 and vacio['casos'] == 0, 'sin casos sí es cero: no hubo fuga'

    def test_meta_y_frescura(self, db):
        res, _ = _fugas()
        m = res['meta']
        assert m['calculado_en'].endswith('Z')
        assert set(m['fuentes']) >= {'base_wms', 'bitacora', 'foto_stock_servicio'}
        assert m['completa'] is False, 'sin foto de AV1/TRA1 la fuente no está completa'
        assert m['periodo_anterior']['hasta'] == (date.fromisoformat(m['desde']) - timedelta(days=1)).isoformat()


class TestCeroSiesa:

    def test_no_consulta_siesa(self, db, almacen, actores, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway

        def _prohibido(self, *a, **k):
            raise AssertionError('la analítica no habla con Siesa')
        monkeypatch.setattr(ConnektaGateway, '_get', _prohibido)
        monkeypatch.setattr(ConnektaGateway, '_post', _prohibido)
        _agotado(db, almacen, actores['op'], precio=100)
        res, _ = _fugas()
        assert len(res["fugas"]) == 10


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestEndpoint:

    def test_operario_recibe_403(self, app, client, db, actores):
        for url in ('/api/analitica/fugas', '/api/analitica/fugas/venta_perdida'):
            assert client.get(url, headers=_tok(app, actores['op'])).status_code == 403

    @pytest.mark.parametrize('rol', ['sup', 'admin'])
    def test_gestion_lo_ve(self, app, client, db, actores, rol):
        r = client.get('/api/analitica/fugas', headers=_tok(app, actores[rol]))
        assert r.status_code == 200, r.get_json()
        d = r.get_json()
        assert [f['clave'] for f in d['fugas']] and len(d["fugas"]) == 10
        assert set(d) == {'meta', 'resumen', 'fugas'}
        hoy = _hoy()
        assert d['meta']['hasta'] == hoy.isoformat()
        assert d['meta']['desde'] == (hoy - timedelta(days=29)).isoformat()

    @pytest.mark.parametrize('qs', ['desde=ayer', 'hasta=2026-13-01', 'desde=2026-09-10&hasta=2026-09-01',
                                    'almacen_id=abc', 'almacen_id=999999',
                                    'desde=2024-01-01&hasta=2026-01-01'])
    def test_basura_es_400(self, app, client, db, actores, qs):
        r = client.get('/api/analitica/fugas?' + qs, headers=_tok(app, actores['sup']))
        assert r.status_code == 400, (qs, r.get_json())
        r = client.get('/api/analitica/fugas/venta_perdida?' + qs, headers=_tok(app, actores['sup']))
        assert r.status_code == 400

    def test_fuga_desconocida_es_404(self, app, client, db, actores):
        r = client.get('/api/analitica/fugas/no_existe', headers=_tok(app, actores['sup']))
        assert r.status_code == 404 and 'venta_perdida' in r.get_json()['claves']

    @pytest.mark.parametrize('qs', ['page=0', 'page=x', 'per_page=-1'])
    def test_paginacion_invalida_es_400(self, app, client, db, actores, qs):
        r = client.get('/api/analitica/fugas/venta_perdida?' + qs, headers=_tok(app, actores['sup']))
        assert r.status_code == 400

    def test_detalle_paginado_sin_valor_primero(self, app, client, db, almacen, actores):
        _agotado(db, almacen, actores['op'], precio=100)
        _agotado(db, almacen, actores['op'], precio=None)
        _agotado(db, almacen, actores['op'], precio=900)
        h = _tok(app, actores['sup'])
        d = client.get('/api/analitica/fugas/venta_perdida?per_page=2', headers=h).get_json()
        assert d['total'] == 3 and len(d['casos']) == 2 and d['pagina'] == 1
        assert d['casos'][0]['sin_valor'] is True
        assert d['casos'][1]['pesos'] == 2700.0
        d2 = client.get('/api/analitica/fugas/venta_perdida?per_page=2&page=2', headers=h).get_json()
        assert [c['pesos'] for c in d2['casos']] == [300.0]
        assert d['fuga']['clave'] == 'venta_perdida'
        big = client.get('/api/analitica/fugas/venta_perdida?per_page=5000', headers=h).get_json()
        assert big['por_pagina'] == _svc().POR_PAGINA_MAX

    def test_filtra_por_almacen(self, app, client, db, almacen, actores):
        from app.models.almacen import Almacen
        otro = Almacen(codigo='OTRO', nombre='Otro almacén', activo=True)
        db.session.add(otro)
        db.session.commit()
        _agotado(db, almacen, actores['op'], precio=100)
        h = _tok(app, actores['sup'])
        a = client.get(f'/api/analitica/fugas?almacen_id={almacen.id}', headers=h).get_json()
        b = client.get(f'/api/analitica/fugas?almacen_id={otro.id}', headers=h).get_json()
        va = next(f for f in a['fugas'] if f['clave'] == 'venta_perdida')
        vb = next(f for f in b['fugas'] if f['clave'] == 'venta_perdida')
        assert va['casos'] == 1 and vb['casos'] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Cada fuga lee su política, no una copia
# ─────────────────────────────────────────────────────────────────────────────

class TestUnaPoliticaUnaFuncion:

    def test_cada_fuga_declara_su_definicion(self):
        for f in _svc().FUGAS.values():
            assert f.fn.__doc__ and len(f.fn.__doc__) > 200, f.clave
            assert f.definicion and f.dimension_motivo

    def test_las_politicas_se_importan_no_se_copian(self):
        fuente = SERVICIO.read_text(encoding='utf-8')
        for nombre in ('filtros_venta_perdida', '_ajustes', 'rutas_entregadas_sin_liquidar',
                       'dias_de_rezago', 'filas_vigentes', '_BODEGAS_SERVICIO',
                       'se_pueden_contar_los_traslados_en_vuelo', 'motivos_rechazo'):
            assert nombre in fuente, nombre
        assert "'AV1'" not in fuente.split('_TEXTO_BODEGA')[0], 'la lista de bodegas no se copia'


# ─────────────────────────────────────────────────────────────────────────────
# La pantalla — render real en Node con util.js real
# ─────────────────────────────────────────────────────────────────────────────

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], modo = args[1] || '';
const nodos = {};
const ctx = { console, window: {},
  document: { getElementById: (id) => (nodos[id] = nodos[id] || { innerHTML: '', id }) } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
// Shell mínimo con las firmas del contrato (analitica.js lo escribe otro agente).
vm.runInContext(`
  function anPesos(n) { return (n === null || n === undefined) ? 'sin dato' : '$' + Math.round(n).toLocaleString('es-CO'); }
  function anNum(n) { return (n === null || n === undefined) ? '—' : String(n); }
  function anPct(x, n) { return n ? Math.round(100 * x / n) + ' % de ' + n : '—'; }
  function anFrescura(meta) { return '<small>' + esc(meta.calculado_en || '') + '</small>'; }
  var PEDIDOS = [];
  function get(url) { PEDIDOS.push(url); return Promise.resolve(RESPUESTA_DETALLE); }
  function anCargarPanel(o) { o.el.innerHTML = o.html(DATOS); return Promise.resolve(); }
  function alerta() {}
`, ctx);
vm.runInContext(fs.readFileSync(base + '/analitica_fugas.js', 'utf8'), ctx);
if (modo === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
const X = '<img src=x onerror=alert(1)>';
const grupo = { almacen: X, ciudad: X, motivo: X, pesos: 10, casos: 1, unidades: 1, sin_valor: 0 };
const fuga = (clave, pesos, extra) => Object.assign({
  clave: X, titulo: X, definicion: X, unidad: X, dimension_motivo: X, pesos, casos: 2, unidades: 3,
  sin_valor: { casos: 1, unidades: 1 }, es_cota_inferior: true, sin_dato: null,
  aporte_al_total: pesos, fuera_del_total_por: X, faltan: [X], estado: 'critico',
  tendencia: { direccion: 'sube', delta_pesos: 5, delta_casos: 1, anterior: { pesos: 1, casos: 1, unidades: 1 } },
  por_almacen: [grupo], por_motivo: [grupo], extra: {} }, extra || {});
const DATOS = {
  meta: { almacen: X, calculado_en: X, fuentes: {} },
  resumen: { total_pesos: 1000, total_anterior_pesos: 10, fugas_que_suman: 1, fugas_fuera_del_total: 2,
    fuera_del_total: [{ clave: X, titulo: X, por: X, casos: 1 }], es_cota_inferior: true, casos: 5 },
  fugas: [fuga('a', null, { aporte_al_total: null }), fuga('b', 1000),
          fuga('c', null, { sin_dato: X, casos: null, unidades: null, aporte_al_total: null })],
};
const caso = (clave) => ({ referencia: X, pesos: null, sin_valor: true, unidades: 2, almacen: X, motivo: X, dia: X,
  pedido_clave: clave, detalle: { cliente: X, producto: X, que: X, lo_tiro: X, error: X, nota_credito: X } });
const RESPUESTA_DETALLE = { meta: { calculado_en: X }, fuga: DATOS.fugas[0], total: 60, pagina: 1, por_pagina: 25,
  casos: [caso(X), Object.assign(caso(null), { pesos: 500, sin_valor: false })] };
ctx.DATOS = DATOS; ctx.RESPUESTA_DETALLE = RESPUESTA_DETALLE;
const el = { innerHTML: '' };
(async () => {
  await vm.runInContext('anFugasCargar', ctx)(el, { almacen_id: '3', desde: '2026-09-01', hasta: '2026-09-30' });
  const resumen = el.innerHTML;
  vm.runInContext('anFugasAbrir', ctx)(0);
  await new Promise(r => setTimeout(r, 10));
  const detalle = nodos['an-fugas-detalle'].innerHTML;
  const html = resumen + detalle;
  const handlers = [...new Set([...html.matchAll(/onclick="([A-Za-z_$][\w$]*)\(/g)].map(m => m[1]))];
  const sinDefinir = handlers.filter(h => typeof ctx[h] !== 'function');
  const anchos = [...html.matchAll(/(?:^|[;"\s])(?:min-)?width:\s*(\d+)px/g)].map(m => +m[1]);
  // Todo onclick es `funcion(<entero o nada>)`: cualquier otra cosa lleva un dato.
  const onclickConDato = [...html.matchAll(/onclick="([^"]*)"/g)].map(m => m[1])
    .filter(a => !/^[A-Za-z_$][\w$]*\(-?\d*\)$/.test(a));
  console.log(JSON.stringify({
    crudos: (html.match(/<img/g) || []).length,
    escapados: (html.match(/&lt;img/g) || []).length,
    handlers, sinDefinir, anchoMax: Math.max(0, ...anchos), onclickConDato,
    pedidos: vm.runInContext('PEDIDOS', ctx),
    sinValorComoCero: /\$0\b/.test(resumen),
    dicenSinValor: (resumen.match(/Sin valor/g) || []).length,
    dicenSinDato: (resumen.match(/Sin dato/g) || []).length,
    quedanFuera: resumen.includes('quedan fuera por no tener valor'),
    botonesRecorrido: (detalle.match(/anFugasVerRecorrido\(/g) || []).length,
    conClave: (detalle.match(/data-pedido-clave="&lt;img/g) || []).length,
    paginacion: detalle.includes('anFugasPagina(1)'),
  }));
})();
"""


def _render(modo=''):
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa), modo],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLaPantalla:

    def test_ningun_texto_llega_crudo(self):
        r = _render()
        assert r['crudos'] == 0, r
        assert r['escapados'] >= 40, f'piso: el arnés dejó de pintar ({r})'

    def test_el_arnes_muerde_con_esc_roto(self):
        assert _render('esc-roto')['crudos'] >= 40

    def test_sin_valor_se_dice_nunca_cero(self):
        r = _render()
        assert not r['sinValorComoCero']
        assert r['dicenSinValor'] >= 1 and r['dicenSinDato'] >= 1
        assert r['quedanFuera']

    def test_los_botones_existen_y_no_llevan_datos(self):
        r = _render()
        assert r['sinDefinir'] == [], r
        assert set(r['handlers']) >= {'anFugasAbrir', 'anFugasVerRecorrido', 'anFugasPagina',
                                      'anFugasCerrarDetalle'}
        assert r['onclickConDato'] == [], 'un onclick solo lleva una posición'

    def test_el_detalle_pide_la_fuga_con_los_filtros(self):
        r = _render()
        assert r['pedidos'] and r['pedidos'][0].startswith('/api/analitica/fugas/')
        assert 'almacen_id=3' in r['pedidos'][0] and 'desde=2026-09-01' in r['pedidos'][0]
        assert 'page=1' in r['pedidos'][0]

    def test_recorrido_solo_donde_hay_clave(self):
        r = _render()
        assert r['botonesRecorrido'] == 1
        assert r['conClave'] >= 1
        assert r['paginacion']

    def test_cabe_en_un_celular(self):
        assert _render()['anchoMax'] <= 360

    def test_se_carga_despues_y_esta_en_el_shell(self):
        html = (RAIZ / 'app' / 'static' / 'pwa' / 'index.html').read_text(encoding='utf-8')
        assert re.search(r'<script defer src="/static/pwa/analitica_fugas\.js\?v=\w+"></script>', html)
        assert html.index('analitica_fugas.js') > html.index('flota_analitica.js')
        sw = (RAIZ / 'app' / 'static' / 'pwa' / 'sw.js').read_text(encoding='utf-8')
        assert "'/static/pwa/analitica_fugas.js'" in sw
