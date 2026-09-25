"""Compras: las fuentes de datos (m046compras) — con fakes de Siesa.

Nada de esto consulta Siesa: la respuesta de `API_v2_Compras_Ordenes` se arma
con los nombres de campo de su contrato (`docs/siesa-specs/
API_v2_Compras_Ordenes.docx`, 89 campos). Lo que se prueba:

- el espejo de OCs: paginación con tamPag 100, cierre SOLO con barrido
  completo (página fallida, circuito abierto, tope, rowid repetido → no cierra),
  proveedores, historial con la fecha entre comillas;
- `en_camino`: lista blanca de bodegas, unidad base, contenedores sin contar
  dos veces, y que el armador lo suma en la posición;
- `lead_time`: proveedor → origen → default, con n y confianza;
- el precio de compra en la jerarquía de `costo_service` y contra el acuerdo;
- la carga de origen / marca / fichas con vista previa, y la marca de Siesa;
- el kardex automático: interruptor, ventana, reanudación, reconstrucción;
- los endpoints y sus permisos.
"""
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

import pytest

from app.utils.fecha import TZ_BOGOTA


# ══════════════════════════════════════════════════════════════════════════════
# Siesa falsa
# ══════════════════════════════════════════════════════════════════════════════

def fila_oc(rowid, ref='PROD-001', pedida_base=100, entrada_base=0, *, bodega='NB1',
            estado=1, consec=10, co='003', fecha='2026-09-01T00:00:00', prov='800123',
            nit='800123', razon='Proveedor Uno SAS', precio=5000, factor=1,
            moneda='COP', obsequio=0, ts_parcial=None, ts_cumplido=None, **extra):
    f = {
        'f420_rowid': 1000 + consec, 'f420_id_co': co, 'f420_id_tipo_docto': 'OC',
        'f420_consec_docto': consec, 'f420_fecha': fecha, 'f420_ind_estado': estado,
        'f420_id_moneda_docto': moneda, 'f420_tasa_conv': 1.0,
        'f420_fecha_ts_parcial': ts_parcial, 'f420_fecha_ts_cumplido': ts_cumplido,
        'f200_id_prov': prov, 'f200_nit_prov': nit, 'f200_razon_social_prov': razon,
        'f202_id_sucursal_prov': '001', 'f120_referencia': ref, 'f150_id': bodega,
        'f421_rowid': rowid, 'f421_id_co_movto': co, 'f421_ind_estado': 1,
        'f421_ind_obsequio': obsequio, 'f421_id_unidad_medida': 'UND',
        'f421_factor': factor,
        'f421_cant_pedida': pedida_base / (factor or 1) if pedida_base is not None else None,
        'f421_cant_entrada': entrada_base / (factor or 1) if entrada_base is not None else None,
        'f421_cant_pedida_base': pedida_base, 'f421_cant_entrada_base': entrada_base,
        'f421_precio_unitario': precio, 'f421_fecha_entrega': '2026-09-20T00:00:00',
    }
    f.update(extra)
    return f


class SiesaFalsa:
    modo_simulacion = False
    api_ordenes = 'API_v2_Compras_Ordenes'

    def __init__(self, por_filtro=None, falla=None, nula=None):
        self.por_filtro = por_filtro or {}
        self.falla = falla or set()      # {(filtro, pagina)}
        self.nula = nula or set()
        self.llamadas = []
        self.clasificacion = []

    def _get(self, nombre, params, url=None):
        self.llamadas.append((nombre, dict(params)))
        filtro = params.get('parametros')
        pag = int(params['paginacion'].split('|')[0].split('=')[1])
        if (filtro, pag) in self.falla:
            raise Exception('Read timed out')
        if (filtro, pag) in self.nula:
            return None
        filas = self.por_filtro.get(filtro, [])
        if callable(filas):
            filas = filas(pag)
            return {'detalle': {'Table': filas}}
        return {'detalle': {'Table': filas[(pag - 1) * 100: pag * 100]}}

    def get_clasificacion_items(self, pagina=1):
        return {'detalle': {'Table': self.clasificacion[(pagina - 1) * 100: pagina * 100]}}


ABIERTAS_1 = 'f420_ind_estado = 1'
ABIERTAS_2 = 'f420_ind_estado = 2'


def _sync(gw, **kw):
    from app.services.compras_oc_sync import sincronizar_ocs
    return sincronizar_ocs(gateway=gw, pausa_s=0, **kw)


def _linea(rowid):
    from app.models.compras_fuentes import OcLineaSiesa
    return OcLineaSiesa.query.filter_by(rowid_linea=rowid).first()


# ══════════════════════════════════════════════════════════════════════════════
# 1 · pendiente de una línea
# ══════════════════════════════════════════════════════════════════════════════

class TestPendienteDeLinea:

    def test_en_unidad_base(self):
        from app.services.compras_fuentes import pendiente_de_linea
        assert pendiente_de_linea(fila_oc(1, pedida_base=120, entrada_base=20)) == (Decimal(100), 'BASE')

    def test_sin_base_usa_el_factor(self):
        from app.services.compras_fuentes import pendiente_de_linea
        f = fila_oc(1, pedida_base=None, entrada_base=None,
                    f421_cant_pedida=10, f421_cant_entrada=4, f421_factor=12)
        assert pendiente_de_linea(f) == (Decimal(72), 'FACTOR')

    def test_sin_base_ni_factor_no_inventa(self):
        from app.services.compras_fuentes import pendiente_de_linea
        f = fila_oc(1, pedida_base=None, entrada_base=None, f421_factor=None)
        assert pendiente_de_linea(f) == (None, 'SIN_UNIDAD_BASE')

    def test_un_exceso_no_deja_negativo(self):
        from app.services.compras_fuentes import pendiente_de_linea
        assert pendiente_de_linea(fila_oc(1, pedida_base=10, entrada_base=12))[0] == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2 · el espejo de OCs
# ══════════════════════════════════════════════════════════════════════════════

class TestSyncDeOcs:

    def test_barrido_completo_escribe_y_cierra_lo_que_no_aparece(self, app, db, producto):
        from app.models.acuerdo_marco import Proveedor
        from app.services import registro_sync_service as reg
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(99, consec=1)]}))
        assert _linea(99).abierta
        r = _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida_base=50, entrada_base=10)],
                              ABIERTAS_2: [fila_oc(2, consec=11, estado=2)]}))
        assert r['paginacion_completa'] is True and r['lineas_cerradas'] == 1
        assert _linea(1).pendiente_base == 40 and _linea(1).abierta
        viejo = _linea(99)
        assert viejo.abierta is False and viejo.motivo_cierre == 'NO_APARECE_EN_ABIERTAS'
        p = Proveedor.query.filter_by(codigo='800123').one()
        assert p.fuente == 'SIESA_OC' and p.nombre == 'Proveedor Uno SAS'
        assert _linea(1).proveedor_id == p.id
        assert reg.ultimo_ok('compras_oc') is not None

    def test_una_pagina_que_falla_no_cierra_nada(self, app, db, producto):
        from app.services import registro_sync_service as reg
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(99, consec=1)]}))
        llenas = [fila_oc(i, consec=20 + i) for i in range(1, 101)]
        r = _sync(SiesaFalsa({ABIERTAS_1: llenas}, falla={(ABIERTAS_1, 2)}))
        assert r['paginacion_completa'] is False and r['cierre_omitido_por_incompleta']
        assert _linea(99).abierta is True, 'con barrido incompleto no se cierra lo no visto'
        assert _linea(1) is not None, 'lo que sí llegó se guarda'
        assert reg.ultimo('compras_oc')['ok'] is False

    def test_circuito_abierto_es_incompleta(self, app, db, producto):
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(99)]}))
        r = _sync(SiesaFalsa({}, nula={(ABIERTAS_2, 1)}))
        assert r['paginacion_completa'] is False and _linea(99).abierta

    def test_rowid_repetido_es_paginacion_inestable(self, app, db, producto):
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(9999, consec=1)]}))
        p1 = [fila_oc(i, consec=i) for i in range(1, 101)]
        p2 = [fila_oc(50, consec=50)]           # la 50 otra vez: orden inestable
        r = _sync(SiesaFalsa({ABIERTAS_1: lambda pag: p1 if pag == 1 else p2}))
        assert r['paginacion_completa'] is False
        assert 'repetido' in r['motivo_incompleta']
        assert _linea(9999).abierta

    def test_el_tope_de_paginas_es_incompleta(self, app, db, producto, monkeypatch):
        monkeypatch.setenv('COMPRAS_OC_MAX_PAGINAS', '1')
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(9999, consec=1)]}))
        r = _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(i, consec=i) for i in range(1, 101)]}))
        assert r['paginacion_completa'] is False and _linea(9999).abierta

    def test_tampag_100_y_los_dos_estados_abiertos(self, app, db):
        gw = SiesaFalsa()
        _sync(gw)
        assert {c[1]['parametros'] for c in gw.llamadas} == {ABIERTAS_1, ABIERTAS_2}
        assert all(c[1]['paginacion'].endswith('tamPag=100') for c in gw.llamadas)

    def test_historial_con_fecha_entre_comillas_y_cierra_como_cumplida(self, app, db, producto):
        from app.services.compras_oc_sync import sincronizar_historial
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(7)]}))
        filtro = "f420_ind_estado = 3 AND f420_fecha >= ''20260101''"
        gw = SiesaFalsa({filtro: [fila_oc(7, estado=3, entrada_base=100,
                                          ts_cumplido='2026-09-10T10:00:00')]})
        r = sincronizar_historial(gateway=gw, desde=date(2026, 1, 1), pausa_s=0)
        assert r['ok'] and gw.llamadas[0][1]['parametros'] == filtro
        l = _linea(7)
        assert l.abierta is False and l.motivo_cierre == 'CUMPLIDA_EN_HISTORIAL'
        assert l.fecha_cumplido == datetime(2026, 9, 10, 10, 0)

    def test_nace_apagado_y_no_toca_siesa(self, app, db, monkeypatch):
        from app.services.compras_oc_sync import correr
        monkeypatch.delenv('COMPRAS_OC_SYNC', raising=False)
        gw = SiesaFalsa()
        assert 'nace apagado' in correr(gateway=gw)['omitido'] and not gw.llamadas

    def test_fuera_de_la_ventana_no_toca_siesa(self, app, db, monkeypatch):
        from app.services.compras_oc_sync import correr
        monkeypatch.setenv('COMPRAS_OC_SYNC', 'true')
        gw = SiesaFalsa()
        r = correr(gateway=gw, reloj=lambda: datetime(2026, 9, 24, 21, 0, tzinfo=TZ_BOGOTA))
        assert 'ventana' in r['omitido'] and not gw.llamadas

    def test_proveedores_del_maestro_de_terceros(self, app, db):
        from app.models.acuerdo_marco import Proveedor
        from app.services.compras_oc_sync import sincronizar_proveedores_terceros
        gw = SiesaFalsa({'f200_ind_proveedor = 1 AND f200_ind_estado = 1': [
            {'f200_rowid': 1, 'f200_id': '900555', 'f200_nit': '900555',
             'f200_razon_social': 'Papeles del Sur'}]})
        r = sincronizar_proveedores_terceros(gateway=gw, pausa_s=0)
        assert r['proveedores_nuevos'] == 1
        assert Proveedor.query.filter_by(codigo='900555').one().fuente == 'SIESA_TERCEROS'

    def test_nada_se_borra(self, app, db, producto):
        from app.models.compras_fuentes import OcLineaSiesa
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1), fila_oc(2, consec=12)]}))
        _sync(SiesaFalsa({}))
        assert OcLineaSiesa.query.count() == 2


# ══════════════════════════════════════════════════════════════════════════════
# 3 · en camino
# ══════════════════════════════════════════════════════════════════════════════

def _contenedor(db, estado='NAVEGANDO'):
    from app.models.importacion import Contenedor
    c = Contenedor(numero='MSKU1', estado=estado)
    db.session.add(c)
    db.session.commit()
    return c


def _item(db, producto, c, cantidad, oc=None, estado=None, bodega=None):
    from app.models.importacion import ItemEnTransito
    db.session.add(ItemEnTransito(producto_id=producto.id, contenedor_id=c.id,
                                  cantidad=cantidad, estado=estado or c.estado,
                                  oc_referencia=oc, bodega_destino=bodega))
    db.session.commit()


class TestEnCamino:

    def test_suma_lo_pendiente_de_las_bodegas_operadas(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [
            fila_oc(1, pedida_base=100, entrada_base=30),                  # 70 NB1
            fila_oc(2, consec=12, pedida_base=10, entrada_base=0, bodega='NC1'),
            fila_oc(3, consec=13, pedida_base=40, entrada_base=0, bodega='AV1'),
            fila_oc(4, consec=14, pedida_base=50, entrada_base=0, bodega='TRA1'),
        ]}))
        r = en_camino()
        assert r['por_sku'] == {'PROD-001': 80.0}
        assert r['declaracion']['lineas_fuera_de_lista_blanca'] == 2

    def test_pedir_av1_no_la_suma_y_lo_declara(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(3, pedida_base=40, bodega='AV1')]}))
        r = en_camino(bodegas=['AV1', 'NB1'])
        assert r['por_sku'] == {} and r['declaracion']['bodegas_ignoradas_no_operadas'] == ['AV1']

    def test_cerrada_no_cuenta(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1)]}))
        _sync(SiesaFalsa({}))
        assert en_camino()['por_sku'] == {}

    def test_sin_unidad_base_no_se_suma_y_se_declara(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida_base=None, entrada_base=None,
                                               f421_factor=None)]}))
        r = en_camino()
        assert r['por_sku'] == {'PROD-001': 0.0}
        assert r['detalle']['PROD-001']['lineas_sin_unidad_base'] == 1
        assert r['declaracion']['lineas_sin_unidad_base'] == 1

    def test_contenedor_cuenta_segun_su_estado(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _item(db, producto, _contenedor(db, 'NAVEGANDO'), 30)
        _item(db, producto, _contenedor(db, 'EN_PRODUCCION'), 5)
        _item(db, producto, _contenedor(db, 'BORRADOR'), 1000)
        _item(db, producto, _contenedor(db, 'RECIBIDO'), 1000)
        assert en_camino()['por_sku'] == {'PROD-001': 35.0}

    def test_contenedor_que_cita_su_oc_abierta_no_se_suma_dos_veces(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida_base=100, consec=10)]}))
        _item(db, producto, _contenedor(db), 100, oc='003-OC-10')
        r = en_camino()
        assert r['por_sku'] == {'PROD-001': 100.0}
        assert r['declaracion']['contenedor_cubierto_por_su_oc'] == 1
        assert r['declaracion']['contenedor_cita_oc_cerrada'] == 0

    def test_contenedor_que_cita_una_oc_cerrada_no_se_suma(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _item(db, producto, _contenedor(db), 100, oc='003-OC-77')
        r = en_camino()
        assert r['por_sku'] == {} and r['declaracion']['contenedor_cita_oc_cerrada'] == 1

    def test_contenedor_sin_oc_sobre_oc_abierta_se_suma_y_se_marca(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida_base=100)]}))
        _item(db, producto, _contenedor(db), 20)
        r = en_camino()
        assert r['por_sku'] == {'PROD-001': 120.0}
        assert r['detalle']['PROD-001']['solapamiento_posible'] is True

    def test_contenedor_a_bodega_no_operada_no_cuenta(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _item(db, producto, _contenedor(db), 20, bodega='AV1')
        assert en_camino()['por_sku'] == {}

    def test_sin_sync_lo_declara(self, app, db):
        from app.services.compras_fuentes import en_camino
        fr = en_camino()['declaracion']['sync_oc']
        assert fr['nunca_corrio'] is True and fr['nota']


class TestElArmadorLoSuma:
    """La posición del armador incluye lo que viene en camino."""

    @pytest.fixture
    def demanda(self, monkeypatch):
        from app.services.kardex_service import KardexService
        monkeypatch.setattr(KardexService, 'demanda_descensurada', lambda *a, **k: {
            'PROD-001': {'d_avg': 6.0, 'sigma_d': 0.0, 'dias_con_stock': 300,
                         'dias_ventana': 360, 'demanda_neta': 1800,
                         'factor_censura': 1.0, 'censurado': False}})

    def test_posicion_suma_en_camino(self, app, db, producto, demanda):
        from app.models.stock_siesa import StockSiesa
        from app.services.armador_service import ArmadorService
        producto.origen = 'CHINA'
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='PROD-001', existencia=100,
                                  comprometido=0, salida_sin_conf=0))
        db.session.commit()
        antes = next(f for f in ArmadorService.rop_dual()['china']['items'])
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida_base=70),
                                       fila_oc(2, consec=12, pedida_base=500, bodega='AV1')]}))
        r = ArmadorService.rop_dual()
        despues = r['china']['items'][0]
        assert despues['en_transito'] == 70 and despues['posicion'] == antes['posicion'] + 70
        assert despues['deficit'] == max(0, antes['deficit'] - 70)
        assert r['en_camino']['lineas_fuera_de_lista_blanca'] == 1


# ══════════════════════════════════════════════════════════════════════════════
# 4 · lead time
# ══════════════════════════════════════════════════════════════════════════════

def _oc_cumplida(db, rowid, consec, dias, prov='800123', moneda='COP',
                 fecha_oc=date(2026, 6, 1)):
    from app.models.compras_fuentes import OcLineaSiesa
    db.session.add(OcLineaSiesa(
        rowid_linea=rowid, co='003', tipo_docto='OC', consec_docto=consec,
        fecha_oc=fecha_oc, estado_oc=3, proveedor_codigo=prov, moneda=moneda,
        referencia='PROD-001', bodega='NB1', abierta=False,
        fecha_parcial=datetime.combine(fecha_oc + timedelta(days=dias), time(10, 0))))
    db.session.commit()


class TestLeadTime:

    def test_sin_datos_es_el_default_declarado(self, app, db):
        from app.services.compras_fuentes import lead_time, LT_NACIONAL_DIAS
        r = lead_time(origen='NACIONAL')
        assert r['lt_dias'] == LT_NACIONAL_DIAS and r['fuente'] == 'DEFAULT_CONSERVADOR'
        assert r['confianza'] == 'NINGUNA' and r['nota']

    def test_proveedor_con_tres_ocs_usa_el_suyo(self, app, db):
        from app.services.compras_fuentes import lead_time
        for i, d in enumerate((8, 10, 12)):
            _oc_cumplida(db, i + 1, i + 1, d)
        r = lead_time(proveedor='800123', origen='NACIONAL')
        assert r['nivel'] == 'PROVEEDOR' and r['lt_dias'] == 10 and r['n'] == 3
        assert r['fuente'] == 'PARCIAL' and r['confianza'] == 'MEDIA'

    def test_proveedor_con_pocas_cae_al_origen(self, app, db):
        from app.services.compras_fuentes import lead_time
        _oc_cumplida(db, 1, 1, 30, prov='A')
        for i, d in enumerate((4, 6, 8)):
            _oc_cumplida(db, 10 + i, 10 + i, d, prov='B')
        r = lead_time(proveedor='A', origen='NACIONAL')
        assert r['nivel'] == 'ORIGEN' and 'A tiene 1' in r['nota']

    def test_la_recepcion_del_wms_manda_sobre_la_marca_de_siesa(self, app, db, almacen):
        from app.models.recepcion import RecepcionMercancia
        from app.services.compras_fuentes import observaciones_lead_time
        _oc_cumplida(db, 1, 5, 20)
        db.session.add(RecepcionMercancia(
            codigo='REC-1', numero_oc_siesa='OC5', co_oc_siesa='003',
            tipo_docto_oc_siesa='OC', consec_docto_oc_siesa='5', almacen_id=almacen.id,
            estado='CONFIRMADA', fecha_confirmacion=datetime(2026, 6, 8, 15, 0)))
        db.session.commit()
        o = observaciones_lead_time()['por_oc'][0]
        assert o['fuente_entrada'] == 'WMS_RECEPCION' and o['dias'] == 7

    def test_una_entrada_antes_de_la_oc_se_descarta(self, app, db):
        from app.services.compras_fuentes import observaciones_lead_time
        _oc_cumplida(db, 1, 1, -3)
        obs = observaciones_lead_time()
        assert obs['por_oc'] == [] and obs['descartadas']['entrada_antes_de_la_oc'] == 1

    def test_una_oc_en_usd_no_entra_al_pool_nacional(self, app, db):
        from app.services.compras_fuentes import lead_time
        for i, d in enumerate((60, 70, 80)):
            _oc_cumplida(db, i + 1, i + 1, d, moneda='USD', prov=f'P{i}')
        assert lead_time(origen='NACIONAL')['nivel'] == 'DEFAULT'

    def test_china_mide_los_contenedores_y_el_armador_lo_delega(self, app, db):
        from app.models.importacion import Contenedor
        from app.services.armador_service import ArmadorService
        for i in range(3):
            db.session.add(Contenedor(numero=f'C{i}', fecha_oc=date(2025, 1, 1),
                                      fecha_recepcion_cedi=date(2025, 1, 1) + timedelta(days=90 + i * 10)))
        db.session.commit()
        r = ArmadorService.calcular_sigma_lt_real()
        assert r['nivel'] == 'ORIGEN' and r['lt_medio'] == 100 and r['n'] == 3

    def test_el_rop_usa_el_lead_time_del_proveedor_habitual(self, app, db, producto, monkeypatch):
        from app.services.kardex_service import KardexService
        from app.services.armador_service import ArmadorService
        monkeypatch.setattr(KardexService, 'demanda_descensurada', lambda *a, **k: {
            'PROD-001': {'d_avg': 2.0, 'sigma_d': 0.0, 'dias_con_stock': 300,
                         'dias_ventana': 360, 'demanda_neta': 600,
                         'factor_censura': 1.0, 'censurado': False}})
        for i, d in enumerate((20, 20, 20)):
            _oc_cumplida(db, i + 1, i + 1, d)
        fila = ArmadorService.rop_dual()['nacional']['items'][0]
        assert fila['lt_dias'] == 20 and fila['lt_nivel'] == 'PROVEEDOR'
        assert fila['lt_proveedor'] == '800123'


# ══════════════════════════════════════════════════════════════════════════════
# 5 · precio de compra
# ══════════════════════════════════════════════════════════════════════════════

def _oc_precio(db, rowid, fecha, precio, *, factor=1, moneda='COP', obsequio=0,
               estado=3, prov='800123'):
    from app.models.compras_fuentes import OcLineaSiesa
    db.session.add(OcLineaSiesa(
        rowid_linea=rowid, co='003', tipo_docto='OC', consec_docto=rowid, fecha_oc=fecha,
        estado_oc=estado, referencia='PROD-001', precio_unitario=precio, factor=factor,
        moneda=moneda, ind_obsequio=obsequio, proveedor_codigo=prov, abierta=False))
    db.session.commit()


class TestPrecioDeCompra:

    def test_la_mas_reciente_en_cop_y_por_unidad_base(self, app, db, producto):
        from app.services.compras_fuentes import precios_oc
        _oc_precio(db, 1, date(2026, 5, 1), 1000)
        _oc_precio(db, 2, date(2026, 8, 1), 12000, factor=12)       # caja de 12
        _oc_precio(db, 3, date(2026, 9, 1), 9, moneda='USD')
        _oc_precio(db, 4, date(2026, 9, 2), 0, obsequio=1)
        _oc_precio(db, 5, date(2026, 9, 3), 5000, estado=9)          # anulada
        p = precios_oc(['PROD-001'])['PROD-001']
        assert p['costo'] == 1000 and p['fecha_costo'] == '2026-08-01'

    def test_a_igual_fecha_la_mas_alta(self, app, db, producto):
        from app.services.compras_fuentes import precios_oc
        _oc_precio(db, 1, date(2026, 8, 1), 1000)
        _oc_precio(db, 2, date(2026, 8, 1), 1100)
        assert precios_oc(['PROD-001'])['PROD-001']['costo'] == 1100

    def test_en_la_jerarquia_entre_cotizacion_y_kardex(self, app, db, producto):
        from app.models.acuerdo_marco import PrecioProveedor, Proveedor
        from app.services.costo_service import resolver_costos, FUENTES
        assert FUENTES.index('COTIZACION') < FUENTES.index('OC_SIESA') < FUENTES.index('KARDEX_PROMEDIO')
        _oc_precio(db, 1, date(2026, 8, 1), 1000)
        assert resolver_costos(['PROD-001'])['PROD-001']['fuente'] == 'OC_SIESA'
        prov = Proveedor(codigo='800123', nombre='P')
        db.session.add(prov)
        db.session.flush()
        db.session.add(PrecioProveedor(producto_id=producto.id, proveedor_id=prov.id,
                                       precio_unitario=1200))
        db.session.commit()
        assert resolver_costos(['PROD-001'])['PROD-001']['fuente'] == 'COTIZACION'

    def test_contra_el_acuerdo_marco(self, app, db, producto):
        from app.models.acuerdo_marco import AcuerdoMarco, Proveedor
        from app.services.compras_fuentes import precio_oc_vs_acuerdo
        from app.utils.fecha import dia_operativo
        hoy_operativo = dia_operativo()
        prov = Proveedor(codigo='800123', nombre='P')
        db.session.add(prov)
        db.session.flush()
        db.session.add(AcuerdoMarco(producto_id=producto.id, proveedor_id=prov.id,
                                    precio_unitario=1000, vigencia_desde=hoy_operativo - timedelta(days=30),
                                    vigencia_hasta=hoy_operativo + timedelta(days=30)))
        db.session.commit()
        _oc_precio(db, 1, hoy_operativo, 1050)
        d = precio_oc_vs_acuerdo()
        assert len(d) == 1 and d[0]['diferencia_pct'] == 5.0


# ══════════════════════════════════════════════════════════════════════════════
# 6 · carga de origen / marca / fichas
# ══════════════════════════════════════════════════════════════════════════════

class TestCargaMaestro:

    def test_la_vista_previa_no_escribe(self, app, db, producto):
        from app.services.maestro_compras_carga import vista_previa, filas_desde_texto
        r = vista_previa('ORIGEN_MARCA', filas_desde_texto('codigo;origen;marca\nPROD-001;china;M003\n'))
        assert r['resumen']['validas'] == 1
        assert r['validas'][0]['cambios']['origen'] == {'antes': None, 'despues': 'CHINA'}
        db.session.refresh(producto)
        assert producto.origen is None

    def test_aplicar_escribe_con_su_fuente(self, app, db, producto):
        from app.services.maestro_compras_carga import aplicar, filas_desde_texto
        r = aplicar('ORIGEN_MARCA', filas_desde_texto('referencia,origen,marca\nPROD-001,CHINA,m003\n'))
        db.session.refresh(producto)
        assert r['escritas'] == 1 and producto.origen == 'CHINA' and producto.marca_siesa == 'M003'
        assert producto.origen_fuente == 'CARGA_ARCHIVO' and producto.marca_fuente == 'CARGA_ARCHIVO'

    def test_filas_invalidas_con_su_motivo(self, app, db, producto):
        from app.services.maestro_compras_carga import vista_previa, filas_desde_texto
        r = vista_previa('ORIGEN_MARCA', filas_desde_texto(
            'codigo,origen\nNO-EXISTE,CHINA\nPROD-001,MARTE\nPROD-001,CHINA\nPROD-001,CHINA\n'))
        errores = [e for f in r['invalidas'] for e in f['errores']]
        assert any('no existe' in e for e in errores)
        assert any('MARTE' in e for e in errores)
        assert any('repetido' in e for e in errores)

    def test_una_celda_vacia_no_borra(self, app, db, producto):
        from app.services.maestro_compras_carga import aplicar, filas_desde_texto
        producto.marca_siesa = 'M009'
        db.session.commit()
        aplicar('ORIGEN_MARCA', filas_desde_texto('codigo,origen,marca\nPROD-001,NACIONAL,\n'))
        db.session.refresh(producto)
        assert producto.marca_siesa == 'M009' and producto.origen == 'NACIONAL'

    def test_sobrescribir_queda_en_la_bitacora(self, app, db, producto):
        from app.models.bitacora import BitacoraAccion
        from app.services.maestro_compras_carga import aplicar, filas_desde_texto
        producto.origen = 'NACIONAL'
        db.session.commit()
        aplicar('ORIGEN_MARCA', filas_desde_texto('codigo,origen\nPROD-001,CHINA\n'))
        b = BitacoraAccion.query.filter_by(accion='EDITAR', entidad='Producto').one()
        assert b.antes == {'origen': 'NACIONAL'} and b.despues == {'origen': 'CHINA'}

    def test_fichas_nuevas_nacen_estimadas(self, app, db, producto):
        from app.models.importacion import FichaImportacion
        from app.services.maestro_compras_carga import aplicar, filas_desde_texto
        r = aplicar('FICHAS', filas_desde_texto(
            'codigo;unidades_por_caja;cbm_por_caja;peso_kg_por_caja\nPROD-001;24;0,045;12,5\n'))
        f = FichaImportacion.query.filter_by(producto_id=producto.id).one()
        assert r['escritas'] == 1 and f.fuente == 'ESTIMADO' and f.moq_cajas == 1
        assert float(f.cbm_por_caja) == 0.045 and f.unidades_por_caja == 24

    def test_ficha_con_numero_ambiguo_o_fuera_de_rango(self, app, db, producto):
        from app.services.maestro_compras_carga import vista_previa, filas_desde_texto
        r = vista_previa('FICHAS', filas_desde_texto(
            'codigo,unidades_por_caja,cbm_por_caja,peso_kg_por_caja\n'
            'PROD-001,1.5,45,12\n'))
        errores = r['invalidas'][0]['errores']
        assert any('entero' in e for e in errores) and any('mayor que' in e for e in errores)

    def test_falta_una_columna_obligatoria(self, app, db, producto):
        from app.services.maestro_compras_carga import vista_previa, filas_desde_texto
        r = vista_previa('FICHAS', filas_desde_texto('codigo,unidades_por_caja\nPROD-001,2\n'))
        assert set(r['faltan_columnas']) == {'cbm_por_caja', 'peso_kg_por_caja'}

    def test_lee_excel(self, app, db, producto):
        import openpyxl
        from app.services.maestro_compras_carga import leer_archivo, vista_previa
        wb = openpyxl.Workbook()
        wb.active.append(['Código', 'Origen'])
        wb.active.append(['PROD-001', 'China'])
        buf = io.BytesIO()
        wb.save(buf)
        filas = leer_archivo('x.xlsx', buf.getvalue())
        assert vista_previa('ORIGEN_MARCA', filas)['resumen']['validas'] == 1

    def test_editar_un_sku_a_mano(self, app, db, producto):
        from app.services.maestro_compras_carga import editar_producto
        editar_producto('PROD-001', origen='IMPORTADO')
        db.session.refresh(producto)
        assert producto.origen == 'IMPORTADO' and producto.origen_fuente == 'MANUAL'


class TestMarcaDesdeSiesa:

    def test_sin_criterio_no_lee_nada(self, app, db, monkeypatch):
        from app.services.maestro_compras_carga import marca_desde_siesa
        monkeypatch.delenv('SIESA_CRITERIO_MARCA', raising=False)
        gw = SiesaFalsa()
        gw.get_clasificacion_items = lambda pagina=1: pytest.fail('no debía leer Siesa')
        assert 'SIESA_CRITERIO_MARCA' in marca_desde_siesa(gateway=gw)['omitido']

    def test_campos_desconocidos_se_devuelven_no_se_adivinan(self, app, db, monkeypatch):
        from app.services.maestro_compras_carga import marca_desde_siesa
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', '005')
        gw = SiesaFalsa()
        gw.clasificacion = [{'otro_campo': 1, 'item': 'X'}]
        r = marca_desde_siesa(gateway=gw)
        assert r['campos_no_reconocidos'] == ['item', 'otro_campo']

    def test_filtra_el_plan_y_aplica_con_su_fuente(self, app, db, producto, producto2, monkeypatch):
        from app.services.maestro_compras_carga import marca_desde_siesa
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', '005')
        gw = SiesaFalsa()
        gw.clasificacion = [
            {'f120_referencia': 'PROD-001', 'f125_id_plan': '005', 'f125_id_criterio_mayor': 'M003'},
            {'f120_referencia': 'PROD-002', 'f125_id_plan': '001', 'f125_id_criterio_mayor': 'LINEA'},
        ]
        prev = marca_desde_siesa(gateway=gw)
        assert prev['resumen']['validas'] == 1 and producto.marca_siesa is None
        marca_desde_siesa(gateway=gw, aplicar_=True)
        db.session.refresh(producto)
        db.session.refresh(producto2)
        assert producto.marca_siesa == 'M003' and producto.marca_fuente == 'SIESA_238920'
        assert producto2.marca_siesa is None


# ══════════════════════════════════════════════════════════════════════════════
# 7 · kardex automático
# ══════════════════════════════════════════════════════════════════════════════

def _reloj(h, m=0):
    return lambda: datetime(2026, 9, 24, h, m, tzinfo=TZ_BOGOTA)


@pytest.fixture
def kardex_falso(monkeypatch):
    """Descarga y reconstrucción falsas, a nivel de CLASE (ver CLAUDE.md,
    «parchear la clase, nunca la instancia»)."""
    from app.services.kardex_service import KardexService
    llamadas = {'descargar': [], 'reconstruir': 0, 'resultado': {'ok': True, 'estado': 'COMPLETA'}}

    def _descargar(fecha_desde, fecha_hasta=None, pagina_inicial=1, max_minutos=None):
        llamadas['descargar'].append({'desde': fecha_desde, 'pagina': pagina_inicial,
                                      'max_minutos': max_minutos})
        return dict(llamadas['resultado'])

    def _reconstruir(bodega=None):
        llamadas['reconstruir'] += 1
        return {'dias_generados': 5, 'referencias_procesadas': 1,
                'refs_sin_ancla': {'cantidad': 0}}
    monkeypatch.setattr(KardexService, 'descargar_kardex', staticmethod(_descargar))
    monkeypatch.setattr(KardexService, 'reconstruir_stock_diario', staticmethod(_reconstruir))
    monkeypatch.setenv('KARDEX_AUTO', 'true')
    return llamadas


class TestKardexAutomatico:

    def test_nace_apagado(self, app, db, kardex_falso, monkeypatch):
        from app.services.kardex_auto import ciclo
        monkeypatch.delenv('KARDEX_AUTO')
        assert 'nace apagado' in ciclo(reloj=_reloj(7, 10))['omitido']
        assert not kardex_falso['descargar']

    def test_fuera_de_la_ventana_no_descarga(self, app, db, kardex_falso):
        from app.services.kardex_auto import ciclo
        assert 'ventana' in ciclo(reloj=_reloj(10))['omitido']
        assert not kardex_falso['descargar']

    def test_la_corrida_no_pasa_el_fin_de_la_ventana(self, app, db, kardex_falso):
        from app.services.kardex_auto import ciclo
        ciclo(reloj=_reloj(7, 40))
        assert kardex_falso['descargar'][0]['max_minutos'] == 15

    def test_completa_reconstruye(self, app, db, kardex_falso):
        from app.services.kardex_auto import ciclo
        r = ciclo(reloj=_reloj(7, 2))
        assert r['reconstruccion']['ok'] and kardex_falso['reconstruir'] == 1
        assert kardex_falso['descargar'][0]['pagina'] == 1

    def test_parcial_no_reconstruye_y_la_siguiente_reanuda(self, app, db, kardex_falso):
        from app.services.kardex_auto import ciclo
        kardex_falso['resultado'] = {'ok': False, 'estado': 'TIMEOUT_PARCIAL', 'reanudar_desde': 431}
        r = ciclo(reloj=_reloj(7, 2))
        assert 'omitida' in r['reconstruccion'] and kardex_falso['reconstruir'] == 0
        ciclo(reloj=_reloj(7, 32))
        assert kardex_falso['descargar'][1]['pagina'] == 431

    def test_una_completa_de_hoy_no_se_repite(self, app, db, kardex_falso, monkeypatch):
        from app.services import kardex_auto
        from app.utils.fecha import ahora_bogota
        ahora = ahora_bogota().replace(hour=7, minute=2, second=0, microsecond=0)
        kardex_auto.ciclo(reloj=lambda: ahora)
        r = kardex_auto.ciclo(reloj=lambda: ahora.replace(minute=32))
        assert len(kardex_falso['descargar']) == 1 and 'omitida' in r['descarga']

    def test_con_una_en_curso_no_arranca(self, app, db, kardex_falso):
        from app.services import registro_sync_service as reg
        from app.services.kardex_auto import ciclo
        reg.abrir('kardex')
        assert 'en curso' in ciclo(reloj=_reloj(7, 2))['omitido']

    def test_una_interrumpida_empieza_de_la_pagina_1(self, app, db, kardex_falso):
        from app.models.registro_sync import RegistroSync
        from app.services.kardex_auto import ciclo
        db.session.add(RegistroSync(tipo='kardex', inicio=datetime.utcnow() - timedelta(hours=6)))
        db.session.commit()
        ciclo(reloj=_reloj(7, 2))
        assert kardex_falso['descargar'][0]['pagina'] == 1

    def test_la_ventana_se_recorta_a_la_de_siesa(self, app, monkeypatch):
        from app.services.kardex_auto import ventana
        monkeypatch.setenv('KARDEX_AUTO_VENTANA', '05:00-07:30')
        v = ventana()
        assert v['efectiva'] == (time(7, 0), time(7, 30)) and 'Recortada' in v['problema']
        monkeypatch.setenv('KARDEX_AUTO_VENTANA', '21:00-22:00')
        assert ventana()['efectiva'] is None
        monkeypatch.setenv('KARDEX_AUTO_VENTANA', 'basura')
        assert ventana()['efectiva'] == (time(7, 0), time(7, 55))


# ══════════════════════════════════════════════════════════════════════════════
# 8 · endpoints
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def token_compras(app, db, almacen):
    from flask_jwt_extended import create_access_token
    from app.models.usuario import Usuario
    u = Usuario(nombre='Compras', email='compras@test.com', rol='compras',
                almacen_id=almacen.id, activo=True)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


class TestEndpoints:

    def test_estado_para_compras_y_no_para_operario(self, client, token_compras, jwt_token):
        ok = client.get('/api/compras/fuentes/estado', headers=token_compras)
        assert ok.status_code == 200
        assert {'compras_oc', 'kardex_auto', 'en_camino', 'lead_time'} <= set(ok.get_json())
        no = client.get('/api/compras/fuentes/estado',
                        headers={'Authorization': f'Bearer {jwt_token}'})
        assert no.status_code == 403

    def test_carga_por_archivo(self, client, token_compras, producto, db):
        datos = {'tipo': 'ORIGEN_MARCA',
                 'archivo': (io.BytesIO(b'codigo,origen\nPROD-001,CHINA\n'), 'o.csv')}
        r = client.post('/api/compras/fuentes/carga/vista-previa', headers=token_compras,
                        data=datos, content_type='multipart/form-data')
        assert r.status_code == 200 and r.get_json()['resumen']['validas'] == 1
        datos['archivo'] = (io.BytesIO(b'codigo,origen\nPROD-001,CHINA\n'), 'o.csv')
        r = client.post('/api/compras/fuentes/carga/aplicar', headers=token_compras,
                        data=datos, content_type='multipart/form-data')
        assert r.status_code == 200 and r.get_json()['escritas'] == 1
        db.session.refresh(producto)
        assert producto.origen == 'CHINA'

    def test_archivo_de_formato_raro_es_400(self, client, token_compras):
        r = client.post('/api/compras/fuentes/carga/vista-previa', headers=token_compras,
                        data={'tipo': 'ORIGEN_MARCA', 'archivo': (io.BytesIO(b'x'), 'x.pdf')},
                        content_type='multipart/form-data')
        assert r.status_code == 400

    def test_editar_producto(self, client, token_compras, producto, db):
        r = client.put('/api/compras/fuentes/producto', headers=token_compras,
                       json={'codigo': 'PROD-001', 'marca': 'm175'})
        assert r.status_code == 200
        db.session.refresh(producto)
        assert producto.marca_siesa == 'M175' and producto.marca_fuente == 'MANUAL'
        r = client.put('/api/compras/fuentes/producto', headers=token_compras,
                       json={'codigo': 'PROD-001', 'origen': 'MARTE'})
        assert r.status_code == 400

    def test_contenedor_items_y_recibido(self, client, token_compras, producto, db):
        from app.services.compras_fuentes import en_camino
        c = _contenedor(db, 'NAVEGANDO')
        r = client.post(f'/api/compras/fuentes/contenedores/{c.id}/items', headers=token_compras,
                        json={'filas': [{'codigo': 'PROD-001', 'cantidad': 40}]})
        assert r.status_code == 201
        assert en_camino()['por_sku'] == {'PROD-001': 40.0}
        r = client.put(f'/api/compras/fuentes/contenedores/{c.id}/estado', headers=token_compras,
                       json={'estado': 'RECIBIDO'})
        assert r.status_code == 200 and r.get_json()['contenedor']['fecha_recepcion_cedi']
        assert en_camino()['por_sku'] == {}

    def test_items_invalidos_no_cargan_nada(self, client, token_compras, producto, db):
        from app.models.importacion import ItemEnTransito
        c = _contenedor(db)
        r = client.post(f'/api/compras/fuentes/contenedores/{c.id}/items', headers=token_compras,
                        json={'filas': [{'codigo': 'PROD-001', 'cantidad': 4},
                                        {'codigo': 'NADA', 'cantidad': 1}]})
        assert r.status_code == 400 and ItemEnTransito.query.count() == 0

    def test_sync_fuera_de_ventana_es_409(self, client, token_compras, monkeypatch):
        from app.services import fotos_siesa_service
        monkeypatch.setattr(fotos_siesa_service, 'ventana_abierta', lambda *a: False)
        r = client.post('/api/compras/fuentes/sync-oc', headers=token_compras, json={})
        assert r.status_code == 409 and 'Regla 14' in r.get_json()['error']

    def test_health_declara_las_dos_fuentes(self, client, jwt_token_admin):
        r = client.get('/api/health/siesa', headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d = r.get_json()
        assert d['compras_oc']['encendido'] is False and 'cron_en_este_proceso' in d['compras_oc']
        assert d['kardex_auto']['encendido'] is False
