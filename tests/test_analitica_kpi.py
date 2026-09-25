"""
Capa semántica y KPI diario de la analítica (Fase 1, m037kpi).

`app/services/analitica_kpi.py`, `app/routes/analitica_kpi.py`,
`analitica_kpi_diario`.

## Qué protege

1. **Cada métrica mide lo que dice**, contra un mundo armado con los servicios
   reales donde se puede (bitácora, historia de pedidos, fotos de Siesa con una
   Siesa falsa, la cola de jobs, el mundo dorado del conteo, el registro de
   agotados) y con filas del modelo donde el servicio real exige un flujo
   entero (despachos, paradas, rutas: igual que sus propios tests).
2. **«No sabemos» ≠ 0**: sin fuente, antes de su primer registro o con la
   corrida incompleta, AUSENTE con motivo; la base lo impide por CHECK.
3. **El upsert es idempotente** y un AUSENTE nunca pisa un valor guardado:
   después del acta de corte, recalcular «descubriría» que no hay datos.
4. **El recálculo absorbe lo tardío**: los últimos N días se vuelven a medir.
5. **El día es el de Bogotá**, también entre las 19:00 y la medianoche.
6. **El cron nace apagado**, vive en `_scheduler_pesados` y toma su lock del
   registro.
7. **El CUSUM es el de Vigía**, no una copia (AST).
"""
import ast
import importlib.util
import json
import pathlib
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from flask_jwt_extended import create_access_token

from app.utils.fecha import TZ_BOGOTA, inicio_del_dia_utc
# El mundo dorado del conteo (servicios reales) y sus fixtures.
from tests.test_conteo_teorico_pos import siesa, tienda  # noqa: F401
from tests.test_estadisticas_conteo import dorado  # noqa: F401
from tests.test_fotos_siesa import _V, _C, _Siesa, _cxc, _venta

RAIZ = pathlib.Path(__file__).resolve().parents[1]
SERVICIO = RAIZ / 'app' / 'services' / 'analitica_kpi.py'

#: Un martes. `_venta` de los tests de fotos fecha sus facturas este día.
D = date(2026, 9, 22)


def _kpi():
    from app.services import analitica_kpi
    return analitica_kpi


def _utc(dia, hora_bogota, minuto=0):
    """El instante UTC naive (como lo guardan las columnas) de una hora de Bogotá."""
    return inicio_del_dia_utc(dia) + timedelta(hours=hora_bogota, minutes=minuto)


def _calc(clave, dia=D, almacen_id=None):
    return _kpi().calcular(clave, dia, almacen_id)


# ─────────────────────────────────────────────────────────────────────────────
# Constructores del mundo
# ─────────────────────────────────────────────────────────────────────────────

def _despacho(db, almacen, cuando, codigo, valor=Decimal('1000')):
    from app.models.packing import TareaPacking
    t = TareaPacking(codigo=codigo, estado='DESPACHADO', almacen_id=almacen.id,
                     numero_pedido_siesa=codigo, tipo_documento='PEDIDO',
                     valor_factura=valor, fecha_despachado=cuando)
    db.session.add(t)
    db.session.commit()
    return t


def _otro_almacen(db, codigo='ALM-NS1', bodega='NS1'):
    from app.models.almacen import Almacen
    a = Almacen(codigo=codigo, nombre=codigo, bodega_siesa_id=bodega, activo=True)
    db.session.add(a)
    db.session.commit()
    return a


def _conductor(db):
    from app.models.conductor import Conductor
    c = Conductor.query.filter_by(cedula='999').first()
    if c is None:
        c = Conductor(nombre='Conductor KPI', cedula='999')
        db.session.add(c)
        db.session.commit()
    return c


def _ruta(db, entregada=None, estado_financiero='PENDIENTE', liquidada_en=None,
          creada=None):
    from app.models.ruta_despacho import RutaDespacho
    r = RutaDespacho(conductor_id=_conductor(db).id, tipo_ruta='Urbana', estado='ENTREGADA',
                     estado_financiero=estado_financiero, fecha_entregada=entregada,
                     liquidada_en=liquidada_en,
                     fecha_creacion=creada or (entregada or datetime(2026, 9, 1)))
    db.session.add(r)
    db.session.commit()
    return r


def _parada(db, almacen, ruta, estado, cuando, valor=Decimal('500'), codigo=None):
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    t = TareaPacking(codigo=codigo or f'PK-{estado}-{cuando.isoformat()}-{valor}',
                     estado='DESPACHADO', almacen_id=almacen.id, tipo_documento='PEDIDO',
                     valor_factura=valor, fecha_despachado=cuando)
    db.session.add(t)
    db.session.flush()
    rec = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega=estado,
                         fecha_confirmacion=cuando)
    db.session.add(rec)
    db.session.commit()
    return rec


def _fila_pedido(rowid, *, pedida, remisionada, fecha_pedido, fecha_entrega,
                 consec=1502, bodega='NB1', co='003'):
    """Una fila de `API_v2_Ventas_Pedidos`, como la lee el sync."""
    return {
        'f431_rowid': rowid, 'f430_id_co': co, 'f430_id_tipo_docto': 'PD',
        'f430_consec_docto': consec, 'f150_id': bodega, 'f120_referencia': f'SKU{rowid}',
        'f120_id': rowid, 'f200_id_pedido_fact': '900', 'f200_razon_social_pedido_fact': 'CLI',
        'f015_id_depto_pe': '41', 'f015_id_ciudad_pe': '001', 'f200_id_pedido_vend': 'V1',
        'f430_id_cond_pago': 'C02', 'f430_id_fecha': f'{fecha_pedido.isoformat()}T00:00:00',
        'f430_fecha_entrega': f'{fecha_entrega.isoformat()}T00:00:00',
        'f431_cant1_pedida': pedida, 'f431_cant1_remisionada': remisionada,
        'f431_cant1_comprometida': pedida, 'f431_vlr_neto': 1000.0, 'f430_ind_estado': 3,
    }


# ─────────────────────────────────────────────────────────────────────────────
# El catálogo
# ─────────────────────────────────────────────────────────────────────────────

class TestCatalogo:

    def test_cada_metrica_se_define_completa(self, app):
        k = _kpi()
        claves = [m['clave'] for m in k.catalogo()]
        assert len(claves) == len(set(claves))
        for m in k.catalogo():
            for campo in ('nombre', 'mide', 'unidad', 'dueno', 'fuente'):
                assert m[campo] and m[campo].strip(), (m['clave'], campo)
            assert m['direccion'] in (k.SUBE_ES_BUENO, k.BAJA_ES_BUENO)
            assert m['direccion_buena'] in ('↑', '↓')
            assert m['agregacion'] in k.AGREGACIONES
            if m['agregacion'] == k.TASA:
                assert m['unidad'] == 'proporcion', m['clave']
            assert callable(k.METRICAS[m['clave']].calcular)

    def test_estan_las_que_pide_la_fase_1(self, app):
        pedidas = {'pedidos_despachados', 'valor_despachado', 'fill_rate', 'venta_perdida',
                   'conteos_cerrados', 'exactitud_inventario', 'ajustes_valor',
                   'rutas_sin_liquidar', 'entregado_sin_pago', 'rechazos_ruta',
                   'jobs_siesa_fallidos', 'cancelaciones', 'ventas_facturadas',
                   'cartera_abierta', 'cartera_vencida'}
        assert pedidas <= set(_kpi().METRICAS)

    def test_la_exactitud_hereda_el_n_minimo_del_conteo(self, app):
        from app.services.metricas.conteo import MIN_N_EXACTITUD
        assert _kpi().METRICAS['exactitud_inventario'].min_n == MIN_N_EXACTITUD


# ─────────────────────────────────────────────────────────────────────────────
# Cada métrica contra su mundo
# ─────────────────────────────────────────────────────────────────────────────

class TestDespachos:

    def test_cuenta_el_dia_de_bogota_y_declara_el_valor_faltante(self, db, almacen):
        # 20:30 del 22 en Bogotá = 01:30 UTC del 23: es del 22.
        _despacho(db, almacen, _utc(D, 20, 30), 'PD-NOCHE', Decimal('1500'))
        _despacho(db, almacen, _utc(D, 10), 'PD-SIN-VALOR', None)
        _despacho(db, almacen, _utc(D + timedelta(days=1), 0, 30), 'PD-MANANA')
        r = _calc('pedidos_despachados')
        assert (r.estado, r.valor, r.n) == ('OK', 2, 2)
        v = _calc('valor_despachado')
        assert v.estado == 'INCOMPLETO' and v.valor == 1500.0
        assert 'cota inferior' in v.motivo and v.detalle['sin_valor_factura'] == 1

    def test_por_almacen_y_total(self, db, almacen):
        otro = _otro_almacen(db)
        _despacho(db, almacen, _utc(D, 9), 'PD-A')
        _despacho(db, otro, _utc(D, 9), 'PD-B', Decimal('250'))
        assert _calc('valor_despachado', almacen_id=otro.id).valor == 250.0
        assert _calc('valor_despachado').valor == 1250.0
        assert _calc('pedidos_despachados', almacen_id=almacen.id).valor == 1

    def test_antes_del_primer_despacho_no_hay_cero(self, db, almacen):
        _despacho(db, almacen, _utc(D, 9), 'PD-1')
        antes = _calc('pedidos_despachados', D - timedelta(days=1))
        assert antes.estado == 'AUSENTE' and antes.valor is None
        assert 'primer registro' in antes.motivo
        despues = _calc('pedidos_despachados', D + timedelta(days=1))
        assert (despues.estado, despues.valor) == ('OK', 0), \
            'después del primer registro, un día sin despachos SÍ es cero'


class TestFillRate:
    """Sobre la historia (servicio real `registrar_barrido`), no sobre lo pendiente."""

    INICIO = D - timedelta(days=5)

    def _barrido(self, filas, dia, completo=True):
        from app.services.pedidos_historia import registrar_barrido
        r = registrar_barrido(filas, completo, ahora=_utc(dia, 10), co_barrido='003')
        from app.extensions import db
        db.session.commit()
        return r

    def test_sin_historia_es_ausente(self, db):
        r = _calc('fill_rate')
        assert r.estado == 'AUSENTE' and 'sesgo de supervivencia' in r.motivo

    def test_lo_cumplido_cuenta_y_lo_abierto_tambien(self, db):
        fp = self.INICIO
        a = _fila_pedido(1, pedida=10, remisionada=0, fecha_pedido=fp, fecha_entrega=D)
        b = _fila_pedido(2, pedida=10, remisionada=0, fecha_pedido=fp, fecha_entrega=D)
        self._barrido([a, b], self.INICIO)
        a2 = dict(a, f431_cant1_remisionada=10)       # la línea A se cumple…
        b2 = dict(b, f431_cant1_remisionada=4)        # …la B va en 4 de 10
        self._barrido([a2, b2], D - timedelta(days=1))
        # Al día siguiente A ya no viene (pedidos_siesa la habría borrado).
        self._barrido([b2], D)
        r = _calc('fill_rate')
        assert r.estado == 'OK'
        assert (r.numerador, r.denominador, r.n) == (14.0, 20.0, 2)
        assert r.valor == pytest.approx(0.7)

    def test_desenlace_desconocido_y_pedido_antes_de_la_historia(self, db):
        fp = self.INICIO
        viejo = _fila_pedido(3, pedida=5, remisionada=0, fecha_pedido=fp - timedelta(days=9),
                             fecha_entrega=D, consec=900)
        a = _fila_pedido(1, pedida=10, remisionada=10, fecha_pedido=fp, fecha_entrega=D)
        c = _fila_pedido(4, pedida=8, remisionada=0, fecha_pedido=fp, fecha_entrega=D,
                         consec=1600)
        self._barrido([viejo, a, c], self.INICIO)
        self._barrido([viejo], self.INICIO + timedelta(days=1))   # C desaparece
        r = _calc('fill_rate')
        assert r.estado == 'INCOMPLETO'
        assert (r.numerador, r.denominador) == (10.0, 10.0), \
            'solo A: C desapareció sin saber cómo, y la vieja no es de la historia'
        exc = r.detalle['excluidas']
        assert exc['desenlace_desconocido'] == 1 and exc['pedidas_antes_de_la_historia'] == 1

    def test_antes_de_la_historia_no_hay_numero(self, db):
        self._barrido([_fila_pedido(1, pedida=1, remisionada=0, fecha_pedido=D,
                                    fecha_entrega=D)], D)
        r = _calc('fill_rate', D - timedelta(days=3))
        assert r.estado == 'AUSENTE' and 'antes de que empezara' in r.motivo

    def test_por_bodega(self, db, almacen):
        otro = _otro_almacen(db)
        fp = self.INICIO
        self._barrido([
            _fila_pedido(1, pedida=10, remisionada=10, fecha_pedido=fp, fecha_entrega=D),
            _fila_pedido(2, pedida=10, remisionada=0, fecha_pedido=fp, fecha_entrega=D,
                         bodega='NS1')], self.INICIO)
        assert _calc('fill_rate', almacen_id=almacen.id).valor == 1.0
        assert _calc('fill_rate', almacen_id=otro.id).valor == 0.0
        assert _calc('fill_rate').valor == 0.5


class TestVentaPerdida:

    def _agotado(self, db, almacen, producto, ub, cuando, faltante, precio):
        from app.models.picking import TareaPicking
        from app.services.eventos_agotado_service import registrar_evento_agotado
        producto.precio_venta = precio
        t = TareaPicking(codigo=f'TP-{cuando.isoformat()}-{faltante}', producto_id=producto.id,
                         cantidad_solicitada=faltante, ubicacion_id=ub.id,
                         almacen_id=almacen.id, tipo_documento='PEDIDO_SIESA',
                         referencia_documento='PD1')
        db.session.add(t)
        db.session.flush()
        ev = registrar_evento_agotado(t, faltante)
        ev.creado_en = cuando
        db.session.commit()

    def test_sin_precio_es_cota_inferior(self, db, almacen, producto, ub_picking):
        self._agotado(db, almacen, producto, ub_picking, _utc(D, 11), 3, Decimal('100'))
        self._agotado(db, almacen, producto, ub_picking, _utc(D, 12), 2, None)
        r = _calc('venta_perdida')
        assert r.estado == 'INCOMPLETO' and r.valor == 300.0 and r.n == 2
        assert r.detalle['sin_precio'] == {'eventos': 1, 'unidades': 2}

    def test_antes_del_primer_evento_es_ausente(self, db, almacen, producto, ub_picking):
        self._agotado(db, almacen, producto, ub_picking, _utc(D, 11), 3, Decimal('100'))
        assert _calc('venta_perdida', D - timedelta(days=1)).estado == 'AUSENTE'
        assert _calc('venta_perdida', D + timedelta(days=1)).valor == 0


class TestConteo:
    """El mundo dorado de `test_estadisticas_conteo` (servicios reales): el KPI
    diario tiene que dar lo mismo que el reporte, porque ES el reporte."""

    def test_mismos_numeros_que_el_reporte(self, db, dorado, tienda):
        from app.utils.fecha import dia_operativo
        hoy = dia_operativo()
        cerr = _calc('conteos_cerrados', hoy)
        assert (cerr.estado, cerr.valor) == ('OK', 6)
        ex = _calc('exactitud_inventario', hoy)
        assert ex.estado == 'AUSENTE' and '< 30' in ex.motivo, \
            'con n=6 no se publica la tasa del día…'
        assert (ex.numerador, ex.denominador) == (3.0, 6.0), '…pero se guarda para el período'
        aj = _calc('ajustes_valor', hoy)
        assert aj.estado == 'OK' and aj.valor == 5500.0 and aj.n == 2
        assert (aj.detalle['entradas'], aj.detalle['salidas']) == (4000.0, 1500.0)
        assert _calc('conteos_cerrados', hoy, tienda['almacen'].id).valor == 6

    def test_la_tasa_del_periodo_suma_numeradores(self, app):
        k = _kpi()
        dias = [{'dia': f'2026-09-{d:02d}', 'estado': 'AUSENTE', 'valor': None, 'n': 10,
                 'numerador': 8.0, 'denominador': 10.0, 'en_curso': False}
                for d in range(1, 5)]
        agg = k.agregar('exactitud_inventario', dias)
        assert agg['valor'] == pytest.approx(0.8) and agg['denominador'] == 40
        assert agg['estado'] == 'OK', 'una tasa de muestra chica SÍ se midió ese día'


class TestRutasSinLiquidar:

    def test_reconstruye_el_cierre_de_un_dia_pasado(self, db):
        _ruta(db, entregada=_utc(D - timedelta(days=2), 15))                      # atrasada
        _ruta(db, entregada=_utc(D - timedelta(days=2), 15), estado_financiero='LIQUIDADA',
              liquidada_en=_utc(D + timedelta(days=1), 9))                           # tarde
        _ruta(db, entregada=_utc(D, 16))                                             # del día
        _ruta(db, entregada=_utc(D + timedelta(days=1), 16))                         # futura
        r = _calc('rutas_sin_liquidar')
        assert (r.estado, r.valor) == ('OK', 2), 'las dos entregadas el 20 sin liquidar al 22'
        assert r.detalle['sin_liquidar_total'] == 3
        # Al 24: la del 20 sigue; la liquidada el 23 sale; la del 22 y la del 23
        # ya tienen un día o más de rezago.
        assert _calc('rutas_sin_liquidar', D + timedelta(days=2)).valor == 3

    def test_liquidada_sin_fecha_no_se_adivina(self, db):
        _ruta(db, entregada=_utc(D - timedelta(days=3), 10), estado_financiero='LIQUIDADA')
        r = _calc('rutas_sin_liquidar')
        assert r.estado == 'INCOMPLETO' and r.detalle['desconocidas'] == 1
        # Cuando ya existe una liquidación con fecha anterior al día, se sabe.
        _ruta(db, entregada=_utc(D - timedelta(days=4), 10), estado_financiero='LIQUIDADA',
              liquidada_en=_utc(D - timedelta(days=1), 10))
        assert _calc('rutas_sin_liquidar').estado == 'OK'

    def test_para_hoy_es_el_mismo_universo_que_la_alerta(self, db):
        from app.services.rezago_liquidacion import (rutas_entregadas_sin_liquidar,
                                                     rutas_sin_liquidar_al_cierre)
        from app.utils.fecha import dia_operativo
        for dias in (0, 1, 3):
            _ruta(db, entregada=datetime.utcnow() - timedelta(days=dias))
        _ruta(db, entregada=datetime.utcnow() - timedelta(days=2),
              estado_financiero='LIQUIDADA', liquidada_en=datetime.utcnow())
        hoy = rutas_sin_liquidar_al_cierre(dia_operativo())
        assert sorted(f['ruta_id'] for f in hoy['rutas']) == \
            sorted(r.id for r in rutas_entregadas_sin_liquidar())

    def test_no_se_abre_por_almacen(self, db, almacen):
        r = _calc('rutas_sin_liquidar', almacen_id=almacen.id)
        assert r.estado == 'AUSENTE' and 'no se abre por almacén' in r.motivo


class TestParadas:

    def test_entregado_sin_pago_y_rechazos(self, db, almacen):
        ruta = _ruta(db, entregada=_utc(D, 18))
        _parada(db, almacen, ruta, 'ENTREGADO_SIN_PAGO', _utc(D, 21), Decimal('700'))
        _parada(db, almacen, ruta, 'ENTREGADO_SIN_PAGO', _utc(D, 11), None)
        _parada(db, almacen, ruta, 'RECHAZADO', _utc(D, 12))
        _parada(db, almacen, ruta, 'ENTREGADO', _utc(D, 13))
        esp = _calc('entregado_sin_pago')
        assert esp.estado == 'INCOMPLETO' and esp.valor == 700.0 and esp.n == 2
        rech = _calc('rechazos_ruta')
        assert (rech.numerador, rech.denominador) == (1.0, 4.0)
        assert rech.valor == pytest.approx(0.25) and rech.estado == 'OK'
        vacio = _calc('rechazos_ruta', D + timedelta(days=1))
        assert vacio.estado == 'AUSENTE' and vacio.denominador == 0, \
            'sin paradas no hay tasa: no es 0 % de rechazo'


class TestJobs:

    def _job(self, db, tipo, cuando, fallar=0):
        from app.models.siesa_job import SiesaJob
        j = SiesaJob.encolar(tipo, {'x': 1})
        db.session.flush()
        j.fecha_creacion = cuando
        for _ in range(fallar):
            j.marcar_fallo('Siesa dijo que no')
        db.session.commit()
        return j

    def test_cohorte_del_dia_sin_correos(self, db):
        self._job(db, 'RECIBO_CAJA', _utc(D, 9), fallar=5)
        self._job(db, 'AJUSTE_CONTEO', _utc(D, 22), fallar=5)       # 22:00 Bogotá: es del 22
        self._job(db, 'ENTRADA_OC', _utc(D, 10)).marcar_completado({'ok': 1})
        db.session.commit()
        self._job(db, 'ALERTA_EMAIL', _utc(D, 11), fallar=5)
        r = _calc('jobs_siesa_fallidos')
        assert (r.estado, r.valor, r.n) == ('OK', 2, 3)
        assert r.detalle['fallidos_por_tipo'] == {'RECIBO_CAJA': 1, 'AJUSTE_CONTEO': 1}

    def test_con_jobs_en_cola_el_numero_no_es_final(self, db):
        self._job(db, 'RECIBO_CAJA', _utc(D, 9), fallar=5)
        self._job(db, 'RECIBO_CAJA', _utc(D, 9), fallar=1)            # reintentando
        r = _calc('jobs_siesa_fallidos')
        assert r.estado == 'INCOMPLETO' and r.valor == 1 and 'cola' in r.motivo


class TestCancelaciones:

    def test_cuenta_bajas_de_la_bitacora(self, db, almacen, monkeypatch):
        from app.services.bitacora import registrar_accion
        import app.services.bitacora as bit
        monkeypatch.setattr(bit, 'dia_operativo_de', lambda _m: D, raising=False)
        registrar_accion('CANCELAR', 'TareaPicking', 1, motivo='x', almacen_id=almacen.id)
        registrar_accion('ELIMINAR', 'Bulto', 2, almacen_id=almacen.id)
        registrar_accion('ANULAR', 'JuicioTemporada', 3)
        registrar_accion('EDITAR', 'RecaudoEntrega', 4, almacen_id=almacen.id)
        db.session.commit()
        from app.models.bitacora import BitacoraAccion
        for a in BitacoraAccion.query.all():
            a.dia_operativo = D
        db.session.commit()
        total = _calc('cancelaciones')
        # Anular un juicio de temporada no es una baja del flujo pedido → caja:
        # va a `tecnicas` (2026-09-24, `ENTIDADES_DE_NEGOCIO`). Antes sumaba.
        assert (total.valor, total.detalle['sin_almacen']) == (2, 0)
        assert total.detalle['por_accion'] == {'CANCELAR': 1, 'ELIMINAR': 1}
        assert total.detalle['tecnicas'] == {'JuicioTemporada': 1}
        assert _calc('cancelaciones', almacen_id=almacen.id).valor == 2
        assert _calc('cancelaciones', D - timedelta(days=1)).estado == 'AUSENTE'


class TestFotos:
    """Con `fotografiar_*` reales y la Siesa falsa de los tests de fotos."""

    def test_ventas_de_una_corrida_completa(self, db, almacen):
        from app.services import fotos_siesa_service as f
        f.fotografiar_ventas('003', D, _Siesa({_V: [[
            _venta(1, neto=74500.0), _venta(2, doc=1450, neto=500.0),
            _venta(3, doc=1451, neto=999.0, estado=9)]]}))
        r = _calc('ventas_facturadas', almacen_id=almacen.id)      # NB1 → CO 003
        assert (r.estado, r.valor, r.n) == ('OK', 75000.0, 2), 'la anulada no suma'
        total = _calc('ventas_facturadas')
        assert total.estado == 'INCOMPLETO' and total.valor == 75000.0
        assert '001' in total.detalle['faltan_cos'], 'los otros CO no tienen foto: cota'

    def test_corrida_fallida_es_ausente_no_cero(self, db, almacen):
        from app.services import fotos_siesa_service as f
        f.fotografiar_ventas('003', D, _Siesa({_V: [RuntimeError('Siesa caída')]}))
        r = _calc('ventas_facturadas', almacen_id=almacen.id)
        assert r.estado == 'AUSENTE' and r.valor is None

    def test_cartera_abierta_y_vencida(self, db):
        from app.services import fotos_siesa_service as f
        f.fotografiar_cartera(D, _Siesa({_C: [[
            _cxc(1, 1000.0, 250.0, vcto='2026-09-20'),           # vencida: 750
            _cxc(2, 500.0, 0.0, vcto='2026-10-20', consec=21)]]}))
        ab = _calc('cartera_abierta')
        assert (ab.estado, ab.valor, ab.n) == ('OK', 1250.0, 2)
        ve = _calc('cartera_vencida')
        assert (ve.estado, ve.valor, ve.n) == ('OK', 750.0, 1)
        assert _calc('cartera_abierta', D - timedelta(days=1)).estado == 'AUSENTE'


# ─────────────────────────────────────────────────────────────────────────────
# «No sabemos» ≠ 0
# ─────────────────────────────────────────────────────────────────────────────

class TestAusenteNoEsCero:

    def test_un_mundo_vacio_no_da_ningun_cero(self, db, almacen):
        k = _kpi()
        k.recalcular_rango(D, D, hoy=D + timedelta(days=1))
        from app.models.analitica_kpi import AnaliticaKpiDiario
        filas = AnaliticaKpiDiario.query.all()
        assert filas, 'el recálculo no escribió nada'
        for f in filas:
            assert f.estado == 'AUSENTE' and f.valor is None and f.motivo, f.to_dict()

    def test_la_base_rechaza_un_ausente_con_valor(self, db):
        from sqlalchemy.exc import IntegrityError
        from app.models.analitica_kpi import AnaliticaKpiDiario
        db.session.add(AnaliticaKpiDiario(dia_operativo=D, metrica='x', estado='AUSENTE',
                                          valor=0, motivo='m', calculado_en=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_la_base_exige_motivo_si_no_es_ok(self, db):
        from sqlalchemy.exc import IntegrityError
        from app.models.analitica_kpi import AnaliticaKpiDiario
        db.session.add(AnaliticaKpiDiario(dia_operativo=D, metrica='x', estado='INCOMPLETO',
                                          valor=1, calculado_en=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_un_error_al_calcular_es_ausente(self, db, monkeypatch):
        k = _kpi()

        def revienta(*_a):
            raise RuntimeError('boom')
        monkeypatch.setitem(k.METRICAS, 'pedidos_despachados',
                            k.METRICAS['pedidos_despachados'].__class__(
                                **{**k.METRICAS['pedidos_despachados'].__dict__,
                                   'calcular': revienta}))
        r = k.calcular('pedidos_despachados', D)
        assert r.estado == 'AUSENTE' and 'boom' in r.motivo


# ─────────────────────────────────────────────────────────────────────────────
# Upsert, recálculo, acta de corte
# ─────────────────────────────────────────────────────────────────────────────

def _fila(clave, dia=D, almacen_id=None):
    from app.models.analitica_kpi import AnaliticaKpiDiario
    return AnaliticaKpiDiario.query.filter_by(dia_operativo=dia, almacen_id=almacen_id,
                                              metrica=clave).first()


class TestUpsert:

    def test_idempotente(self, db, almacen):
        from app.models.analitica_kpi import AnaliticaKpiDiario
        _despacho(db, almacen, _utc(D, 9), 'PD-1')
        k = _kpi()
        r1 = k.recalcular_rango(D, D, hoy=D + timedelta(days=1))
        n1 = AnaliticaKpiDiario.query.count()
        antes = {(f.metrica, f.almacen_id): (f.estado, f.valor) for f in
                 AnaliticaKpiDiario.query.all()}
        r2 = k.recalcular_rango(D, D, hoy=D + timedelta(days=1))
        assert AnaliticaKpiDiario.query.count() == n1
        assert r1['filas']['nueva'] == n1 and r2['filas']['nueva'] == 0
        despues = {(f.metrica, f.almacen_id): (f.estado, f.valor) for f in
                   AnaliticaKpiDiario.query.all()}
        assert antes == despues
        assert _fila('pedidos_despachados').valor == 1
        assert _fila('pedidos_despachados', almacen_id=almacen.id).valor == 1
        assert _fila('rutas_sin_liquidar', almacen_id=almacen.id) is None, \
            'una métrica sin desglose no escribe filas por almacén'

    def test_el_total_es_unico_aunque_almacen_sea_null(self, db):
        from sqlalchemy.exc import IntegrityError
        from app.models.analitica_kpi import AnaliticaKpiDiario
        for _ in range(2):
            db.session.add(AnaliticaKpiDiario(dia_operativo=D, metrica='x', estado='OK',
                                              valor=1, calculado_en=datetime.utcnow()))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_el_dia_en_curso_no_se_guarda(self, db):
        with pytest.raises(ValueError, match='día en curso'):
            _kpi().recalcular_rango(D, D, hoy=D)


class TestRecalculoDeDiasTardios:

    def test_el_cron_vuelve_a_medir_los_ultimos_dias(self, db, almacen, monkeypatch):
        monkeypatch.setenv('ANALITICA_KPI', 'true')
        monkeypatch.setenv('ANALITICA_KPI_DIAS', '3')
        k = _kpi()
        _despacho(db, almacen, _utc(D, 9), 'PD-1')
        k.correr_kpi(hoy=D + timedelta(days=1))
        assert _fila('pedidos_despachados').valor == 1
        # Llega tarde: un despacho del 22 que se registró después.
        _despacho(db, almacen, _utc(D, 18), 'PD-TARDE')
        k.correr_kpi(hoy=D + timedelta(days=3))
        assert _fila('pedidos_despachados').valor == 2
        # Fuera de la ventana ya no se toca.
        _despacho(db, almacen, _utc(D, 19), 'PD-MUY-TARDE')
        k.correr_kpi(hoy=D + timedelta(days=10))
        assert _fila('pedidos_despachados').valor == 2

    def test_calcula_ayer_y_los_n_anteriores(self, db, monkeypatch):
        monkeypatch.setenv('ANALITICA_KPI', 'true')
        monkeypatch.setenv('ANALITICA_KPI_DIAS', '2')
        r = _kpi().correr_kpi(hoy=D)
        assert [d['dia'] for d in r['dias']] == ['2026-09-19', '2026-09-20', '2026-09-21']


class TestActaDeCorte:

    def _mod(self, nombre):
        spec = importlib.util.spec_from_file_location(nombre, RAIZ / 'scripts' / f'{nombre}.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_protegida_e_irrecuperable(self):
        reset = self._mod('reset_transaccional')
        assert 'analitica_kpi_diario' in reset.PROTEGIDAS_ANALITICAS
        assert 'analitica_kpi_diario' not in reset.OPERATIVAS
        assert 'analitica_kpi_diario' in self._mod('verificar_restauracion').IRRECUPERABLES

    def test_sin_claves_foraneas(self, app):
        from app.extensions import db
        assert not db.metadata.tables['analitica_kpi_diario'].foreign_keys

    def test_recalcular_despues_del_corte_no_borra_lo_medido(self, db, almacen):
        from app.models.packing import TareaPacking
        k = _kpi()
        _despacho(db, almacen, _utc(D, 9), 'PD-1')
        k.recalcular_rango(D, D, hoy=D + timedelta(days=1))
        assert _fila('pedidos_despachados').estado == 'OK'
        TareaPacking.query.delete()                 # el corte vacía las operativas
        db.session.commit()
        r = k.recalcular_rango(D, D, hoy=D + timedelta(days=1))
        assert r['filas']['conservada'] > 0
        f = _fila('pedidos_despachados')
        assert (f.estado, float(f.valor)) == ('OK', 1.0), \
            'un AUSENTE pisó un valor guardado: el corte borró la historia del KPI'


# ─────────────────────────────────────────────────────────────────────────────
# El día de Bogotá
# ─────────────────────────────────────────────────────────────────────────────

class TestDiaDeBogota:

    def test_el_cron_a_las_23_30_de_bogota_calcula_el_ayer_de_bogota(self, db, monkeypatch):
        import app.utils.fecha as fecha
        monkeypatch.setenv('ANALITICA_KPI', 'true')
        monkeypatch.setenv('ANALITICA_KPI_DIAS', '0')
        # 23:30 del 22 en Bogotá = 04:30 UTC del 23.
        monkeypatch.setattr(fecha, 'ahora_bogota',
                            lambda: datetime(2026, 9, 22, 23, 30, tzinfo=TZ_BOGOTA))
        r = _kpi().correr_kpi()
        assert [d['dia'] for d in r['dias']] == ['2026-09-21'], \
            'en UTC ya es el 23: «ayer» habría sido el 22, que todavía no terminó'

    def test_la_franja_de_la_noche_es_del_mismo_dia(self, db, almacen):
        for h, m in ((19, 0), (21, 15), (23, 59)):
            _despacho(db, almacen, _utc(D, h, m), f'PD-{h}{m}')
        assert _calc('pedidos_despachados').valor == 3
        assert _calc('pedidos_despachados', D + timedelta(days=1)).valor == 0


# ─────────────────────────────────────────────────────────────────────────────
# El cron y el lock
# ─────────────────────────────────────────────────────────────────────────────

class TestCron:

    def test_nace_apagado(self, db, almacen, monkeypatch):
        from app.models.analitica_kpi import AnaliticaKpiDiario
        monkeypatch.delenv('ANALITICA_KPI', raising=False)
        _despacho(db, almacen, _utc(D, 9), 'PD-1')
        r = _kpi().correr_kpi(hoy=D + timedelta(days=1))
        assert 'nace apagado' in r['omitido']
        assert AnaliticaKpiDiario.query.count() == 0

    def test_esta_en_los_pesados(self):
        arbol = ast.parse((RAIZ / 'app' / '__init__.py').read_text(encoding='utf-8'))
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Assign) and len(nodo.targets) == 1
                    and getattr(nodo.targets[0], 'id', None) == '_scheduler_pesados'):
                literales = {c.value for c in ast.walk(nodo.value)
                             if isinstance(c, ast.Constant)}
                assert 'app.services.analitica_kpi' in literales
                assert '[ANALITICA_KPI]' in literales
                return
        pytest.fail('no se encontró _scheduler_pesados')

    def test_el_interruptor_vive_en_la_funcion_que_escribe(self):
        """Uno solo: en `correr_kpi`. El scheduler no lo consulta."""
        arbol = ast.parse(SERVICIO.read_text(encoding='utf-8'))
        quien = {n.name for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)
                 and any(isinstance(c, ast.Call) and getattr(c.func, 'id', '') == 'encendido'
                         for c in ast.walk(n))}
        assert quien == {'correr_kpi', 'estado', 'init_scheduler'} or \
            quien == {'correr_kpi', 'estado'} or 'correr_kpi' in quien
        init = next(n for n in ast.walk(arbol)
                    if isinstance(n, ast.FunctionDef) and n.name == 'init_scheduler')
        ifs = [n for n in ast.walk(init) if isinstance(n, ast.If)
               and any(isinstance(c, ast.Call) and getattr(c.func, 'id', '') == 'encendido'
                       for c in ast.walk(n.test))]
        assert not ifs, 'un segundo interruptor en el scheduler: dos sitios donde apagar lo mismo'

    def test_publica_su_frescura_en_health(self, db, almacen, monkeypatch):
        fuente = (RAIZ / 'app' / 'routes' / 'health.py').read_text(encoding='utf-8')
        assert "resultado['analitica_kpi'] = _kpi.estado()" in fuente
        assert "'[ANALITICA_KPI]' in _activos" in fuente
        _kpi().recalcular_rango(D, D, hoy=D + timedelta(days=1))
        e = _kpi().estado(dias=3, hoy=D + timedelta(days=2))
        assert e['encendido'] is False and e['ultimo_calculo']
        assert '2026-09-22' in e['totales_por_dia']
        assert e['dias_sin_calcular'] == ['2026-09-21', '2026-09-23']


class TestLock:

    def test_clave_del_registro(self):
        from app.utils import lock
        assert lock.LOCK_ANALITICA_KPI == 2021
        assert list(lock._claves_fijas().values()).count(2021) == 1

    def test_recalcular_toma_el_lock_del_registro(self):
        arbol = ast.parse(SERVICIO.read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == 'recalcular_rango')
        tomas = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                 and getattr(c.func, 'id', '') == 'advisory_lock']
        assert tomas and getattr(tomas[0].args[0], 'id', '') == 'LOCK_ANALITICA_KPI'

    def test_sin_lock_no_escribe(self, db, almacen, monkeypatch):
        from app.models.analitica_kpi import AnaliticaKpiDiario
        from app.utils import lock

        class _Ocupado:
            tomado = False

            def liberar(self):
                pass
        monkeypatch.setattr(lock, 'tomar_lock_de_sesion', lambda *a, **k: _Ocupado())
        with pytest.raises(_kpi().RecalculoEnCurso):
            _kpi().recalcular_rango(D, D, hoy=D + timedelta(days=1))
        assert AnaliticaKpiDiario.query.count() == 0


# ─────────────────────────────────────────────────────────────────────────────
# El CUSUM es el de Vigía
# ─────────────────────────────────────────────────────────────────────────────

def _recursiones_cusum(fuente: str) -> list:
    """`max(0, s ± z − k)`: la recursión del CUSUM tabular. Por AST — un
    comentario o un docstring que la describa no cuenta."""
    hallazgos = []
    for n in ast.walk(ast.parse(fuente)):
        if not (isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'max'
                and len(n.args) == 2 and isinstance(n.args[0], ast.Constant)
                and n.args[0].value == 0 and isinstance(n.args[1], ast.BinOp)):
            continue
        nombres = {x.id for x in ast.walk(n.args[1]) if isinstance(x, ast.Name)}
        if any(x.startswith('s_') or x.startswith('s') and len(x) <= 2 for x in nombres) \
                and any('K' in x or x == 'k' for x in nombres):
            hallazgos.append(n.lineno)
    return hallazgos


class TestElCusumEsElDeVigia:

    def test_la_recursion_vive_solo_en_vigia(self):
        sitios = {}
        for raiz in (RAIZ / 'app', RAIZ / 'flota'):
            for p in raiz.rglob('*.py'):
                h = _recursiones_cusum(p.read_text(encoding='utf-8'))
                if h:
                    sitios[str(p.relative_to(RAIZ))] = h
        assert list(sitios) == ['app/services/vigia_service.py'], sitios
        assert len(sitios['app/services/vigia_service.py']) == 2, \
            'piso: S+ y S− de cusum_bilateral — si da 0, el escáner se rompió'

    def test_la_analitica_la_importa_y_la_llama(self):
        arbol = ast.parse(SERVICIO.read_text(encoding='utf-8'))
        importa = any(isinstance(n, ast.ImportFrom) and n.module == 'app.services.vigia_service'
                      and any(a.name == 'cusum_bilateral' for a in n.names)
                      for n in ast.walk(arbol))
        llama = any(isinstance(n, ast.Call) and getattr(n.func, 'id', '') == 'cusum_bilateral'
                    for n in ast.walk(arbol))
        assert importa and llama
        assert not _recursiones_cusum(SERVICIO.read_text(encoding='utf-8'))
        fuente = SERVICIO.read_text(encoding='utf-8')
        assert '1.4826' not in fuente, 'la referencia robusta de Vigía copiada acá'

    def test_vigia_tambien_la_usa(self):
        arbol = ast.parse((RAIZ / 'app' / 'services' / 'vigia_service.py').read_text(
            encoding='utf-8'))
        fn = next(n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == 'ejecutar_cusum')
        assert any(isinstance(c, ast.Call) and getattr(c.func, 'id', '') == 'cusum_bilateral'
                   for c in ast.walk(fn))

    def test_el_detector_ve_una_copia(self):
        copia = 'def f(z):\n    s_plus = 0\n    s_plus = max(0, s_plus + z - CUSUM_K)\n'
        assert _recursiones_cusum(copia) == [3]

    def test_el_detector_no_marca_lo_sano(self):
        sano = ('def f(a, b):\n    """max(0, s_plus + z - CUSUM_K)"""\n'
                '    # max(0, s_plus + z - k)\n    return max(0, a - b)\n')
        assert _recursiones_cusum(sano) == []


class TestAlertaDeCambio:

    def _sembrar(self, db, semanas, valor_por_semana, desde_lunes, faltan=()):
        from app.models.analitica_kpi import AnaliticaKpiDiario
        for s in range(semanas):
            for d in range(7):
                dia = desde_lunes + timedelta(weeks=s, days=d)
                if dia in faltan:
                    continue
                db.session.add(AnaliticaKpiDiario(
                    dia_operativo=dia, metrica='pedidos_despachados', estado='OK',
                    valor=valor_por_semana(s), n=1, calculado_en=datetime.utcnow()))
        db.session.commit()

    LUNES = date(2026, 5, 4)

    def test_detecta_el_desplome_con_la_funcion_de_vigia(self, db):
        # 14 semanas estables (con ruido) y 4 en que se despacha la mitad.
        ruido = [10, 11, 9, 10, 12, 9, 10, 11, 10, 9, 11, 10, 12, 10]
        self._sembrar(db, 18, lambda s: ruido[s] if s < 14 else 5, self.LUNES)
        hasta = self.LUNES + timedelta(weeks=18, days=-1)
        a = _kpi().alerta_de_cambio('pedidos_despachados', hasta - timedelta(days=27), hasta,
                                    hoy=hasta + timedelta(days=3))
        assert a['disponible'] and a['semanas_usadas'] == 18
        assert (a['alarma'], a['severidad'], a['lectura']) == ('BAJA', 'ALARMA', 'desfavorable')
        assert a['alarmas_en_periodo']

    def test_sin_historia_suficiente_lo_dice(self, db):
        self._sembrar(db, 5, lambda s: 10, self.LUNES)
        hasta = self.LUNES + timedelta(weeks=5, days=-1)
        a = _kpi().alerta_de_cambio('pedidos_despachados', hasta, hasta,
                                    hoy=hasta + timedelta(days=1))
        assert a['disponible'] is False and 'historia insuficiente' in a['motivo']

    def test_una_semana_con_hueco_no_entra(self, db):
        faltan = {self.LUNES + timedelta(weeks=3, days=2)}
        self._sembrar(db, 9, lambda s: 10, self.LUNES, faltan=faltan)
        hasta = self.LUNES + timedelta(weeks=9, days=-1)
        a = _kpi().alerta_de_cambio('pedidos_despachados', hasta, hasta,
                                    hoy=hasta + timedelta(days=1))
        assert a['semanas_excluidas'] >= 1 and a['semanas_usadas'] == 8

    def test_la_semana_en_curso_no_entra(self, db):
        self._sembrar(db, 9, lambda s: 10, self.LUNES)
        hoy = self.LUNES + timedelta(weeks=8, days=3)      # miércoles de la novena
        a = _kpi().alerta_de_cambio('pedidos_despachados', hoy, hoy, hoy=hoy)
        assert a['semanas_usadas'] == 8


# ─────────────────────────────────────────────────────────────────────────────
# Serie y resumen
# ─────────────────────────────────────────────────────────────────────────────

class TestSerieYResumen:

    def test_serie_marca_el_dia_en_curso_y_los_huecos(self, db, almacen):
        _despacho(db, almacen, _utc(D, 9), 'PD-1')
        _despacho(db, almacen, _utc(D + timedelta(days=2), 9), 'PD-2')
        k = _kpi()
        k.recalcular_rango(D, D, hoy=D + timedelta(days=2))
        s = k.serie('pedidos_despachados', D, D + timedelta(days=2), hoy=D + timedelta(days=2))
        assert [p['estado'] for p in s] == ['OK', 'AUSENTE', 'INCOMPLETO']
        assert 'sin calcular' in s[1]['motivo']
        assert s[2]['en_curso'] and s[2]['valor'] == 1 and 'día en curso' in s[2]['motivo']

    def test_resumen_compara_con_el_periodo_anterior(self, db, almacen):
        k = _kpi()
        for i in range(4):
            dia = D - timedelta(days=i)
            for j in range(1 if i >= 2 else 3):
                _despacho(db, almacen, _utc(dia, 9, j), f'PD-{i}-{j}')
        k.recalcular_rango(D - timedelta(days=3), D, hoy=D + timedelta(days=1))
        res = {m['clave']: m for m in k.resumen(D - timedelta(days=1), D,
                                                hoy=D + timedelta(days=1))}
        m = res['pedidos_despachados']
        assert (m['periodo']['valor'], m['anterior']['valor']) == (6, 2)
        assert m['variacion']['absoluta'] == 4 and m['variacion']['relativa'] == 2
        assert m['variacion']['favorable'] is True and m['variacion']['comparable'] is True
        assert m['alerta']['disponible'] is False
        vp = res['venta_perdida']
        assert vp['periodo']['valor'] is None and vp['variacion']['comparable'] is False

    def test_nivel_toma_el_ultimo_dia(self, app):
        puntos = [{'dia': '2026-09-01', 'estado': 'OK', 'valor': 5, 'n': 1},
                  {'dia': '2026-09-02', 'estado': 'OK', 'valor': 7, 'n': 2},
                  {'dia': '2026-09-03', 'estado': 'AUSENTE', 'valor': None, 'n': None,
                   'denominador': None, 'motivo': 'x'}]
        agg = _kpi().agregar('cartera_abierta', puntos)
        assert agg['valor'] == 7 and agg['dia_del_valor'] == '2026-09-02'
        assert agg['estado'] == 'INCOMPLETO'


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _usuario(db, almacen, rol, email):
    from app.models.usuario import Usuario
    u = Usuario(nombre=rol, email=email, rol=rol, almacen_id=almacen.id, activo=True)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _h(app, u):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


class TestEndpoints:

    def test_roles(self, app, client, db, almacen, jwt_token, usuario_admin):
        op = {'Authorization': f'Bearer {jwt_token}'}
        sup = _h(app, _usuario(db, almacen, 'supervisor', 'sup-kpi@test.com'))
        for url in ('/api/analitica/metricas', '/api/analitica/resumen',
                    '/api/analitica/serie?metrica=pedidos_despachados'):
            assert client.get(url, headers=op).status_code == 403, url
            assert client.get(url, headers=sup).status_code == 200, url
        cuerpo = {'desde': '2026-09-01', 'hasta': '2026-09-02'}
        assert client.post('/api/analitica/kpi/recalcular', json=cuerpo,
                           headers=sup).status_code == 403, 'recalcular es solo admin'
        assert client.post('/api/analitica/kpi/recalcular', json=cuerpo,
                           headers=_h(app, usuario_admin)).status_code == 200

    @pytest.mark.parametrize('qs', [
        'metrica=inventada', 'metrica=pedidos_despachados&desde=ayer',
        'metrica=pedidos_despachados&desde=2026-09-10&hasta=2026-09-01',
        'metrica=pedidos_despachados&hasta=2999-01-01',
        'metrica=pedidos_despachados&almacen_id=x',
        'metrica=pedidos_despachados&almacen_id=99999',
        'metrica=pedidos_despachados&desde=2020-01-01&hasta=2026-09-01',
    ])
    def test_serie_valida(self, app, client, db, usuario_admin, qs):
        r = client.get(f'/api/analitica/serie?{qs}', headers=_h(app, usuario_admin))
        assert r.status_code == 400, (qs, r.get_json())

    def test_forma(self, app, client, db, almacen, usuario_admin):
        from app.utils.fecha import dia_operativo
        h = _h(app, usuario_admin)
        cat = client.get('/api/analitica/metricas', headers=h).get_json()
        assert len(cat['metricas']) == len(_kpi().METRICAS)
        assert 'kpi_diario' in cat['meta']['fuentes']
        hoy = dia_operativo()
        desde = (hoy - timedelta(days=2)).isoformat()
        s = client.get(f'/api/analitica/serie?metrica=pedidos_despachados&desde={desde}'
                       f'&almacen_id={almacen.id}', headers=h).get_json()
        assert len(s['serie']) == 3 and s['serie'][-1]['en_curso'] is True
        assert s['serie'][-1]['estado'] != 'OK'
        assert s['meta']['almacen_id'] == almacen.id and s['meta']['calculado_en']
        res = client.get(f'/api/analitica/resumen?desde={desde}', headers=h).get_json()
        assert {m['clave'] for m in res['metricas']} == set(_kpi().METRICAS)
        for m in res['metricas']:
            assert {'periodo', 'anterior', 'variacion', 'alerta'} <= set(m)

    def test_recalcular_valida_e_idempotente(self, app, client, db, almacen, usuario_admin):
        from app.utils.fecha import dia_operativo
        h = _h(app, usuario_admin)
        hoy = dia_operativo()
        url = '/api/analitica/kpi/recalcular'
        for cuerpo in ({}, {'desde': '2026-09-01'}, {'desde': 'x', 'hasta': '2026-09-01'},
                       {'desde': hoy.isoformat(), 'hasta': hoy.isoformat()},
                       {'desde': '2026-01-01', 'hasta': '2026-06-01'},
                       {'desde': '2026-09-02', 'hasta': '2026-09-01'}):
            assert client.post(url, json=cuerpo, headers=h).status_code == 400, cuerpo
        ayer = (hoy - timedelta(days=1)).isoformat()
        r1 = client.post(url, json={'desde': ayer, 'hasta': ayer}, headers=h).get_json()
        r2 = client.post(url, json={'desde': ayer, 'hasta': ayer}, headers=h).get_json()
        assert r1['filas']['nueva'] > 0 and r2['filas']['nueva'] == 0

    def test_recalcular_ocupado_es_409(self, app, client, db, usuario_admin, monkeypatch):
        from app.utils import lock
        from app.utils.fecha import dia_operativo

        class _Ocupado:
            tomado = False

            def liberar(self):
                pass
        monkeypatch.setattr(lock, 'tomar_lock_de_sesion', lambda *a, **k: _Ocupado())
        ayer = (dia_operativo() - timedelta(days=1)).isoformat()
        r = client.post('/api/analitica/kpi/recalcular', json={'desde': ayer, 'hasta': ayer},
                        headers=_h(app, usuario_admin))
        assert r.status_code == 409
