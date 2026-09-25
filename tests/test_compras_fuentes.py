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

def fila_oc(rowid, ref='PROD-001', pedida=100, entrada=0, *, bodega='NB1',
            estado=1, consec=10, co='003', fecha='2026-09-01T00:00:00', prov='800123',
            nit='800123', razon='Proveedor Uno SAS', precio=5000, factor=1,
            moneda='COP', obsequio=0, ts_parcial=None, ts_cumplido=None,
            unidad='UND', entrega='2026-09-20T00:00:00', cia=1, **extra):
    """Una línea con la forma REAL de `API_v2_Compras_Ordenes`.

    `pedida`/`entrada` son unidades de INVENTARIO y van en `f421_cant_*`; las
    `f421_cant_*_base` son la unidad de la LÍNEA = inventario ÷ factor.
    Verificado en vivo el 2026-09-25 (OC 003-OC-28: PQ, factor 12, pedida 36,
    pedida_base 3). La versión anterior de este fixture fijaba la relación al
    revés, y con eso escondía que `pendiente_de_linea` contaba paquetes como
    unidades."""
    def _linea(v):
        return v / factor if (v is not None and factor) else None
    f = {
        'f420_rowid': 1000 + consec, 'f420_id_cia': cia, 'f420_id_co': co,
        'f420_id_tipo_docto': 'OC',
        'f420_consec_docto': consec, 'f420_fecha': fecha, 'f420_ind_estado': estado,
        'f420_id_moneda_docto': moneda, 'f420_tasa_conv': 1.0,
        'f420_fecha_ts_parcial': ts_parcial, 'f420_fecha_ts_cumplido': ts_cumplido,
        'f200_id_prov': prov, 'f200_nit_prov': nit, 'f200_razon_social_prov': razon,
        'f202_id_sucursal_prov': '001', 'f120_referencia': ref, 'f150_id': bodega,
        'f421_rowid': rowid, 'f421_id_cia': cia, 'f421_id_co_movto': co,
        'f421_ind_estado': 1,
        'f421_ind_obsequio': obsequio, 'f421_id_unidad_medida': unidad,
        'f421_factor': factor,
        'f421_cant_pedida': pedida, 'f421_cant_entrada': entrada,
        'f421_cant_pedida_base': _linea(pedida), 'f421_cant_entrada_base': _linea(entrada),
        'f421_precio_unitario': precio, 'f421_fecha_entrega': entrega,
    }
    f.update(extra)
    return f


#: Líneas reales de Siesa QA (2026-09-25, `API_v2_Compras_Ordenes` estados 1
#: y 2), copiadas campo por campo de la respuesta cruda: las abiertas de
#: PAPELSP6948 y PAPELSP6741. En camino esperado, verificado en vivo:
#: 432 y 5.509.
LINEAS_REALES = [
    # (f421_rowid, consec, ref, bodega, unidad, factor, pedida, pedida_base, entrada, entrada_base, precio, vlr_bruto, entrega, estado)
    (280, 11, 'PAPELSP6741', 'NS1', 'UND', 1.0, 10.0, 10.0, 0.0, 0.0, 916.8, 9168.0, '2024-08-20', 1),
    (1371, 24, 'PAPELSP6741', 'NS1', 'UND', 1.0, 20.0, 20.0, 11.0, 11.0, 1160.0, 23200.0, '2026-06-20', 2),
    (365, 32, 'PAPELSP6741', 'NB1', 'PQ', 10.0, 30.0, 3.0, 0.0, 0.0, 3650.0, 10950.0, '2026-04-21', 1),
    (1345, 33, 'PAPELSP6741', 'NB1', 'PQ', 10.0, 60.0, 6.0, 0.0, 0.0, 7650.0, 45900.0, '2026-04-23', 1),
    (1356, 42, 'PAPELSP6741', 'NB1', 'PQ', 10.0, 200.0, 20.0, 0.0, 0.0, 5000.0, 100000.0, '2026-05-27', 1),
    (1357, 43, 'PAPELSP6741', 'NB1', 'PQ', 10.0, 200.0, 20.0, 0.0, 0.0, 5000.0, 100000.0, '2026-05-27', 1),
    (1372, 48, 'PAPELSP6741', 'NB1', 'UND', 1.0, 500.0, 500.0, 0.0, 0.0, 1390.0, 695000.0, '2026-06-24', 1),
    (1373, 49, 'PAPELSP6741', 'NB1', 'UND', 1.0, 500.0, 500.0, 0.0, 0.0, 1390.0, 695000.0, '2026-06-24', 1),
    (1374, 50, 'PAPELSP6741', 'NB1', 'UND', 1.0, 500.0, 500.0, 0.0, 0.0, 1390.0, 695000.0, '2026-06-24', 1),
    (1375, 51, 'PAPELSP6741', 'NB1', 'UND', 1.0, 500.0, 500.0, 0.0, 0.0, 1000.0, 500000.0, '2026-06-24', 1),
    (1376, 52, 'PAPELSP6741', 'NB1', 'PQ', 10.0, 3000.0, 300.0, 0.0, 0.0, 1200.0, 360000.0, '2026-06-23', 1),
    (358, 28, 'PAPELSP6948', 'NB1', 'PQ', 12.0, 36.0, 3.0, 0.0, 0.0, 1250.0, 3750.0, '2026-04-20', 1),
    (359, 29, 'PAPELSP6948', 'NB1', 'PQ', 12.0, 48.0, 4.0, 0.0, 0.0, 1250.0, 5000.0, '2026-04-20', 1),
    (360, 30, 'PAPELSP6948', 'NB1', 'PQ', 12.0, 60.0, 5.0, 0.0, 0.0, 1250.0, 6250.0, '2026-04-21', 1),
    (1352, 39, 'PAPELSP6948', 'NB1', 'PQ', 12.0, 288.0, 24.0, 0.0, 0.0, 13650.0, 327600.0, '2026-04-23', 1),
    (1354, 41, 'PAPELSP6948', 'NB1', 'UND', 1.0, 600.0, 600.0, 600.0, 600.0, 13250.0, 7950000.0, '2026-04-25', 2),
]


def fila_real(t):
    """Una tupla de `LINEAS_REALES` como la devuelve Siesa (los números tal
    cual; nada derivado por el fixture)."""
    (rowid, consec, ref, bodega, unidad, factor, pedida, pedida_b, entrada, entrada_b,
     precio, bruto, entrega, estado) = t
    f = fila_oc(rowid, ref=ref, bodega=bodega, consec=consec, estado=estado, factor=factor,
                unidad=unidad, precio=precio, entrega=f'{entrega}T00:00:00')
    f.update({'f421_cant_pedida': pedida, 'f421_cant_pedida_base': pedida_b,
              'f421_cant_entrada': entrada, 'f421_cant_entrada_base': entrada_b,
              'f421_vlr_bruto': bruto})
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

    def test_en_unidad_de_inventario(self):
        from app.services.compras_fuentes import pendiente_de_linea
        assert pendiente_de_linea(fila_oc(1, pedida=120, entrada=20)) == (Decimal(100), 'INVENTARIO')

    def test_la_linea_real_en_paquetes_cuenta_unidades(self):
        """OC 003-OC-28 tal como la devolvió Siesa QA: 3 PQ de 12 = 36 UND.
        La versión anterior restaba las `_base` y daba 3 (×12 de menos)."""
        from app.services.compras_fuentes import pendiente_de_linea
        oc28 = fila_real(LINEAS_REALES[11])
        assert oc28['f421_cant_pedida_base'] == 3 and oc28['f421_factor'] == 12
        assert pendiente_de_linea(oc28) == (Decimal(36), 'INVENTARIO')

    def test_el_valor_bruto_confirma_la_unidad_de_cada_campo(self):
        """Lo que prueba cuál campo es cuál, en los datos crudos: el valor
        bruto es `_base` × precio (el precio es por PQ), y `pedida` =
        `pedida_base` × factor en TODAS las líneas reales."""
        for t in LINEAS_REALES:
            f = fila_real(t)
            assert abs(f['f421_vlr_bruto'] - f['f421_cant_pedida_base'] * f['f421_precio_unitario']) < 1
            assert abs(f['f421_cant_pedida'] - f['f421_cant_pedida_base'] * f['f421_factor']) < 0.01

    def test_sin_las_de_inventario_usa_la_base_por_el_factor(self):
        from app.services.compras_fuentes import pendiente_de_linea
        f = fila_oc(1, pedida=None, entrada=None, f421_cant_pedida_base=10,
                    f421_cant_entrada_base=4, f421_factor=12)
        assert pendiente_de_linea(f) == (Decimal(72), 'FACTOR')

    def test_parcial_en_paquetes(self):
        """2 PQ de 12 pedidos-3 = una entrada de 1 PQ: faltan 24 UND."""
        from app.services.compras_fuentes import pendiente_de_linea
        f = fila_oc(1, pedida=36, entrada=12, factor=12, unidad='PQ')
        assert f['f421_cant_entrada_base'] == 1
        assert pendiente_de_linea(f)[0] == 24

    def test_sin_base_ni_factor_no_inventa(self):
        from app.services.compras_fuentes import pendiente_de_linea
        f = fila_oc(1, pedida=None, entrada=None, f421_factor=None)
        assert pendiente_de_linea(f) == (None, 'SIN_UNIDAD_BASE')

    def test_un_exceso_no_deja_negativo(self):
        from app.services.compras_fuentes import pendiente_de_linea
        assert pendiente_de_linea(fila_oc(1, pedida=10, entrada=12))[0] == 0


# ══════════════════════════════════════════════════════════════════════════════
# 2 · el espejo de OCs
# ══════════════════════════════════════════════════════════════════════════════

class TestSyncDeOcs:

    def test_barrido_completo_escribe_y_cierra_lo_que_no_aparece(self, app, db, producto):
        from app.models.acuerdo_marco import Proveedor
        from app.services import registro_sync_service as reg
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(99, consec=1)]}))
        assert _linea(99).abierta
        r = _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida=50, entrada=10)],
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
        gw = SiesaFalsa({filtro: [fila_oc(7, estado=3, entrada=100,
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

    def test_cero_ocs_abiertas_no_cierra_todo(self, app, db, producto):
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1)]}))
        r = _sync(SiesaFalsa({}))
        assert r['paginacion_completa'] is False and 'CERO' in r['motivo_incompleta']
        assert _linea(1).abierta is True

    def test_nada_se_borra(self, app, db, producto):
        from app.models.compras_fuentes import OcLineaSiesa
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1), fila_oc(2, consec=12)]}))
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(3, consec=13)]}))
        assert OcLineaSiesa.query.count() == 3 and not _linea(1).abierta


#: Filas reales de `API_v2_Proveedores` (Siesa QA, 2026-09-25): una por
#: SUCURSAL, las dos compañías. COLFONDOS es tipo 0019 (el sync viejo lo metía
#: como proveedor); YIWU es USD en la compañía 1 y COP en la 2 (con las dos
#: mezcladas «ganaba la última»); DISPAPELES tiene dos sucursales con el mismo
#: `f200_rowid` (y eso no es una página repetida).
PROVEEDORES_QA = [
    {'f200_id_cia': 1, 'f200_id': '800227940', 'f200_nit': '800227940', 'f200_rowid': 2, 'f202_id_sucursal': '001', 'f202_descripcion_sucursal': 'FONDO DE PENSIONES COLFONDOS', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0019', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 1, 'f200_id': '1', 'f200_nit': '1', 'f200_rowid': 198, 'f202_id_sucursal': '001', 'f202_descripcion_sucursal': 'YIWU CTC IMP & EXP.CO LTDA', 'f202_ind_estado': 1, 'f202_id_moneda': 'USD', 'f202_id_tipo_prov': '0002', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 1, 'f200_id': '860028580', 'f200_nit': '860028580', 'f200_rowid': 8336, 'f202_id_sucursal': '001', 'f202_descripcion_sucursal': 'DISPAPELES SAS', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0001', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 1, 'f200_id': '860028580', 'f200_nit': '860028580', 'f200_rowid': 8336, 'f202_id_sucursal': '002', 'f202_descripcion_sucursal': 'DISPAPELES SAS', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0001', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 1, 'f200_id': '36312375', 'f200_nit': '36312375', 'f200_rowid': 74, 'f202_id_sucursal': '001', 'f202_descripcion_sucursal': 'GOMEZ NARVAEZ ANGELA MARIA', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0018', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 1, 'f200_id': '36312375', 'f200_nit': '36312375', 'f200_rowid': 74, 'f202_id_sucursal': '002', 'f202_descripcion_sucursal': 'GOMEZ NARVAEZ ANGELA MARIA', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0017', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 1, 'f200_id': '1124850590', 'f200_nit': '1124850590', 'f200_rowid': 236, 'f202_id_sucursal': '001', 'f202_descripcion_sucursal': 'PORTILLA NARVAEZ JOSE LUIS', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0016', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 2, 'f200_id': '800227940', 'f200_nit': '800227940', 'f200_rowid': 9916, 'f202_id_sucursal': '001', 'f202_descripcion_sucursal': 'FONDO DE PENSIONES COLFONDOS', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0019', 'f202_id_cond_pago': 'P01'},
    {'f200_id_cia': 2, 'f200_id': '1', 'f200_nit': '1', 'f200_rowid': 10026, 'f202_id_sucursal': '001', 'f202_descripcion_sucursal': 'YIWU CTC IMP & EXP.CO LTDA', 'f202_ind_estado': 1, 'f202_id_moneda': 'COP', 'f202_id_tipo_prov': '0002', 'f202_id_cond_pago': 'P01'},
]
FILTRO_PROVEEDORES = 'f202_ind_estado = 1'


def _sync_proveedores(filas=None):
    from app.services.compras_oc_sync import sincronizar_proveedores
    return sincronizar_proveedores(
        gateway=SiesaFalsa({FILTRO_PROVEEDORES: PROVEEDORES_QA if filas is None else filas}),
        pausa_s=0)


class TestProveedoresDelMaestro:

    def test_sin_tipos_solo_los_de_las_ocs_y_de_la_compania_propia(self, app, db, monkeypatch):
        from app.models.acuerdo_marco import Proveedor
        monkeypatch.delenv('COMPRAS_TIPOS_PROVEEDOR', raising=False)
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, prov='1', nit='1', razon='YIWU'),
                                       fila_oc(2, consec=12, prov='860028580',
                                               razon='DISPAPELES SAS')]}))
        r = _sync_proveedores()
        assert r['ok'] and r['otra_compania'] == 2 and r['proveedores_nuevos'] == 0
        assert r['paginacion_completa'] is True, 'dos sucursales no son una página repetida'
        assert {p.codigo for p in Proveedor.query.all()} == {'1', '860028580'}
        yiwu = Proveedor.query.filter_by(codigo='1').one()
        assert yiwu.moneda == 'USD', 'la de la compañía 1, no «la última fila» (COP en la 2)'
        assert yiwu.tipo_proveedor == '0002' and yiwu.nombre == 'YIWU'
        assert Proveedor.query.filter_by(codigo='860028580').one().moneda == 'COP'

    def test_con_tipos_crea_los_de_mercancia_y_no_eps_ni_otros_tipos(self, app, db, monkeypatch):
        from app.models.acuerdo_marco import Proveedor
        monkeypatch.setenv('COMPRAS_TIPOS_PROVEEDOR', '0001, 0002,0018')
        r = _sync_proveedores()
        codigos = {p.codigo for p in Proveedor.query.all()}
        assert codigos == {'1', '860028580', '36312375'}
        assert '800227940' not in codigos and '1124850590' not in codigos
        g = Proveedor.query.filter_by(codigo='36312375').one()
        assert g.tipo_proveedor == '0017,0018' and g.fuente == 'SIESA_PROVEEDORES'
        assert r['fuera_de_tipo'] == 2 and r['tipos'] == ['0001', '0002', '0018']

    def test_el_sync_viejo_se_desactiva_con_bitacora(self, app, db, monkeypatch):
        from app.models.acuerdo_marco import Proveedor
        from app.models.bitacora import BitacoraAccion
        monkeypatch.setenv('COMPRAS_TIPOS_PROVEEDOR', '0018')
        db.session.add_all([
            Proveedor(codigo='800227940', nombre='COLFONDOS', fuente='SIESA_TERCEROS', activo=True),
            Proveedor(codigo='36312375', nombre='GOMEZ', fuente='SIESA_TERCEROS', activo=True),
            Proveedor(codigo='555', nombre='A mano', fuente='MANUAL', activo=True)])
        db.session.commit()
        r = _sync_proveedores()
        assert r['desactivados_sync_viejo'] == 1
        assert Proveedor.query.filter_by(codigo='800227940').one().activo is False
        g = Proveedor.query.filter_by(codigo='36312375').one()
        assert g.activo is True and g.fuente == 'SIESA_PROVEEDORES' and g.nombre == 'GOMEZ'
        assert Proveedor.query.filter_by(codigo='555').one().activo is True
        b = BitacoraAccion.query.filter_by(accion='DESACTIVAR', entidad='Proveedor').one()
        assert b.antes == {'activo': True} and 'compañía 1' in b.motivo

    def test_barrido_incompleto_no_desactiva(self, app, db, monkeypatch):
        from app.models.acuerdo_marco import Proveedor
        from app.services.compras_oc_sync import sincronizar_proveedores
        monkeypatch.delenv('COMPRAS_TIPOS_PROVEEDOR', raising=False)
        db.session.add(Proveedor(codigo='800227940', nombre='X', fuente='SIESA_TERCEROS', activo=True))
        db.session.commit()
        r = sincronizar_proveedores(gateway=SiesaFalsa({}, falla={(FILTRO_PROVEEDORES, 1)}),
                                    pausa_s=0)
        assert r['paginacion_completa'] is False
        assert Proveedor.query.filter_by(codigo='800227940').one().activo is True

    def test_la_misma_sucursal_dos_veces_si_es_paginacion_inestable(self, app, db):
        r = _sync_proveedores([PROVEEDORES_QA[2]] * 2)
        assert r['paginacion_completa'] is False

    def test_una_linea_de_oc_de_otra_compania_no_se_escribe(self, app, db, producto):
        r = _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1), fila_oc(2, consec=12, cia=2)]}))
        assert r['otra_compania'] == 1 and _linea(2) is None and _linea(1) is not None

    def test_la_moneda_del_proveedor_cubre_la_oc_que_no_la_trae(self, app, db, producto):
        from app.models.acuerdo_marco import Proveedor
        from app.services.compras_fuentes import lineas_precio_oc
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, prov='1', moneda='')]}))
        assert lineas_precio_oc(['PROD-001'])[0]['moneda_fuente'] == 'SUPUESTA_COP'
        Proveedor.query.filter_by(codigo='1').one().moneda = 'USD'
        db.session.commit()
        l = lineas_precio_oc(['PROD-001'])[0]
        assert l['moneda'] == 'USD' and l['moneda_fuente'] == 'PROVEEDOR'


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
            fila_oc(1, pedida=100, entrada=30),                  # 70 NB1
            fila_oc(2, consec=12, pedida=10, entrada=0, bodega='NC1'),
            fila_oc(3, consec=13, pedida=40, entrada=0, bodega='AV1'),
            fila_oc(4, consec=14, pedida=50, entrada=0, bodega='TRA1'),
        ]}))
        r = en_camino()
        assert r['por_sku'] == {'PROD-001': 80.0}
        assert r['declaracion']['lineas_fuera_de_lista_blanca'] == 2

    def test_las_lineas_reales_de_siesa_qa(self, app, db):
        """Las abiertas de PAPELSP6948 y PAPELSP6741 tal como las devolvió
        Siesa QA el 2026-09-25: 432 y 5.509 unidades, verificado en vivo. Con
        la lectura vieja (las `_base`) daban 36 y 2.849."""
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({
            ABIERTAS_1: [fila_real(t) for t in LINEAS_REALES if t[-1] == 1],
            ABIERTAS_2: [fila_real(t) for t in LINEAS_REALES if t[-1] == 2]}))
        r = en_camino(['PAPELSP6948', 'PAPELSP6741'])
        assert r['por_sku'] == {'PAPELSP6948': 432.0, 'PAPELSP6741': 5509.0}

    def test_las_ocs_viejas_se_suman_y_se_declaran_con_su_peso(self, app, db, monkeypatch):
        """PAPELSP6741 tiene una OC de NS1 con entrega 2024-08-20: vieja. Se suma
        (decisión del dueño) y se declara con su peso."""
        from app.services import compras_fuentes
        monkeypatch.delenv('COMPRAS_OC_VENCIDA_DIAS', raising=False)
        monkeypatch.delenv('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS', raising=False)
        monkeypatch.setattr('app.utils.fecha.dia_operativo', lambda: date(2026, 9, 25))
        _sync(SiesaFalsa({
            ABIERTAS_1: [fila_real(t) for t in LINEAS_REALES if t[-1] == 1],
            ABIERTAS_2: [fila_real(t) for t in LINEAS_REALES if t[-1] == 2]}))
        r = compras_fuentes.en_camino(['PAPELSP6948', 'PAPELSP6741'])
        assert r['por_sku'] == {'PAPELSP6948': 432.0, 'PAPELSP6741': 5509.0}
        v = r['declaracion']['ocs_vencidas']
        # Con 90 días, todo lo de abril–junio de 2026 y la de 2024 está vencido:
        # 432 + 5.509 − 9 (la OC 24 de NS1, entrega 2026-06-20, también) = todo.
        assert v['dias'] == 90 and v['dias_fuente'] == 'DEFAULT_DECLARADO'
        assert v['unidades'] == 5941.0 and v['pct_unidades'] == 100.0
        assert v['entrega_mas_vieja'] == '2024-08-20' and 'Se suma igual' in v['nota']
        assert r['detalle']['PAPELSP6741']['oc_vencida'] == 5509.0
        assert r['declaracion']['corte_antiguedad']['lineas_excluidas'] == 0

    def test_con_umbral_propio_solo_la_de_2024_es_vieja(self, app, db, monkeypatch):
        from app.services import compras_fuentes
        monkeypatch.setenv('COMPRAS_OC_VENCIDA_DIAS', '365')
        monkeypatch.delenv('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS', raising=False)
        monkeypatch.setattr('app.utils.fecha.dia_operativo', lambda: date(2026, 9, 25))
        _sync(SiesaFalsa({ABIERTAS_1: [fila_real(t) for t in LINEAS_REALES if t[-1] == 1]}))
        r = compras_fuentes.en_camino(['PAPELSP6741'])
        v = r['declaracion']['ocs_vencidas']
        assert v['dias_fuente'] == 'CONFIGURADO' and v['lineas'] == 1 and v['unidades'] == 10.0
        assert r['por_sku']['PAPELSP6741'] == 5500.0, 'se suma igual'

    def test_el_corte_opcional_no_suma_las_mas_viejas_y_lo_dice(self, app, db, monkeypatch):
        from app.services import compras_fuentes
        monkeypatch.setenv('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS', '365')
        monkeypatch.setattr('app.utils.fecha.dia_operativo', lambda: date(2026, 9, 25))
        _sync(SiesaFalsa({ABIERTAS_1: [fila_real(t) for t in LINEAS_REALES if t[-1] == 1]}))
        r = compras_fuentes.en_camino(['PAPELSP6741'])
        c = r['declaracion']['corte_antiguedad']
        assert r['por_sku']['PAPELSP6741'] == 5490.0
        assert c['dias'] == 365 and c['lineas_excluidas'] == 1 and c['unidades_excluidas'] == 10.0
        assert 'NO se suman' in c['nota']

    def test_una_variable_ilegible_no_corta_y_se_declara(self, app, db, monkeypatch):
        from app.services import compras_fuentes
        monkeypatch.setenv('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS', 'un año')
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida=10, entrega='2020-01-01T00:00:00')]}))
        r = compras_fuentes.en_camino()
        assert r['por_sku'] == {'PROD-001': 10.0}
        assert any('COMPRAS_OC_EXCLUIR_MAS_DE_DIAS' in p
                   for p in r['declaracion']['problemas_de_configuracion'])

    def test_pedir_av1_no_la_suma_y_lo_declara(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(3, pedida=40, bodega='AV1')]}))
        r = en_camino(bodegas=['AV1', 'NB1'])
        assert r['por_sku'] == {} and r['declaracion']['bodegas_ignoradas_no_operadas'] == ['AV1']

    def test_cerrada_no_cuenta(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1)]}))
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(2, ref='PROD-XX', consec=12)]}))
        assert 'PROD-001' not in en_camino()['por_sku']

    def test_sin_unidad_base_no_se_suma_y_se_declara(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida=None, entrada=None,
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
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida=100, consec=10)]}))
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
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida=100)]}))
        _item(db, producto, _contenedor(db), 20)
        r = en_camino()
        assert r['por_sku'] == {'PROD-001': 120.0}
        assert r['detalle']['PROD-001']['solapamiento_posible'] is True

    def test_contenedor_a_bodega_no_operada_no_cuenta(self, app, db, producto):
        from app.services.compras_fuentes import en_camino
        _item(db, producto, _contenedor(db), 20, bodega='AV1')
        assert en_camino()['por_sku'] == {}

    def test_una_fuente_que_revienta_se_declara_no_suma_cero(self, app, db, producto,
                                                               monkeypatch):
        """Del motor: «viene 0» y «no sé qué viene» empujan la compra al mismo
        lado. La fuente que falla va a `fuentes_con_error`; la otra sigue."""
        from app.services import compras_fuentes
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida=70)]}))

        def _revienta(*a, **k):
            raise RuntimeError('tabla de contenedores ilegible')
        monkeypatch.setattr(compras_fuentes, '_contenedores_en_camino', _revienta)
        r = compras_fuentes.en_camino()
        assert r['por_sku'] == {'PROD-001': 70.0}
        d = r['declaracion']
        assert d['completo'] is False
        assert d['fuentes_con_error'][0]['fuente'] == 'IMPORTACION'
        assert 'IMPORTACION' not in d['fuentes'] and d['fuentes']['OC_SIESA']['refs'] == 1

    def test_si_revientan_las_ocs_el_contenedor_que_cita_una_se_suma(self, app, db,
                                                                     producto, monkeypatch):
        """Sin saber qué OCs siguen abiertas, el ítem que cita su OC no se puede
        descontar: se suma (contar de más achica el déficit, Regla 0) y se dice."""
        from app.services import compras_fuentes
        _item(db, producto, _contenedor(db), 25, oc='003-OC-10')

        def _revienta(*a, **k):
            raise RuntimeError('espejo ilegible')
        monkeypatch.setattr(compras_fuentes, '_pendiente_de_ocs_abiertas', _revienta)
        r = compras_fuentes.en_camino()
        assert r['por_sku'] == {'PROD-001': 25.0}
        assert r['declaracion']['contenedor_oc_no_verificable'] == 1
        assert r['declaracion']['completo'] is False

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
        _sync(SiesaFalsa({ABIERTAS_1: [fila_oc(1, pedida=70),
                                       fila_oc(2, consec=12, pedida=500, bodega='AV1')]}))
        r = ArmadorService.rop_dual()
        despues = r['china']['items'][0]
        assert despues['en_transito'] == 70 and despues['posicion'] == antes['posicion'] + 70
        assert despues['deficit'] == max(0, antes['deficit'] - 70)
        # Integración: la declaración de lo que viene sale en `insumo_en_camino`
        # (el nombre del motor; antes había dos claves para lo mismo).
        assert r['insumo_en_camino']['lineas_fuera_de_lista_blanca'] == 1
        assert r['insumo_en_camino']['hay_dato'] is True


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


#: Las 24 OCs de DISPAPELES (860028580) con entrada, tal como las devolvió
#: Siesa QA el 2026-09-25: (co, consec, f420_fecha, estado, ts_parcial,
#: ts_cumplido).
OCS_DISPAPELES_QA = [
    ('003', 47, '2026-05-28', 3, None, '2026-05-28T16:04:26.093'),
    ('002', 1, '2026-06-19', 3, None, '2026-06-19T11:12:18.847'),
    ('001', 22, '2026-06-19', 3, None, '2026-06-19T15:15:13.133'),
    ('001', 23, '2026-06-19', 3, None, '2026-06-19T15:15:34.207'),
    ('001', 19, '2026-06-19', 2, '2026-06-19T15:16:42.25', None),
    ('001', 24, '2026-06-19', 2, '2026-06-19T15:39:43.153', None),
    ('003', 53, '2026-06-23', 3, None, '2026-06-23T14:20:46.36'),
    ('003', 54, '2026-06-23', 3, None, '2026-06-23T14:36:53.987'),
    ('003', 55, '2026-07-06', 3, None, '2026-07-06T14:31:51.083'),
    ('003', 56, '2026-07-27', 3, '2026-07-27T15:57:45.483', '2026-07-27T15:57:45.98'),
    ('003', 58, '2026-07-27', 3, '2026-07-27T16:19:25.56', '2026-07-27T16:19:25.667'),
    ('003', 59, '2026-07-31', 3, '2026-07-31T15:31:54.727', '2026-07-31T15:31:54.967'),
    ('003', 60, '2026-07-31', 3, '2026-07-31T16:01:08.033', '2026-07-31T16:01:08.363'),
    ('003', 61, '2026-07-31', 3, None, '2026-07-31T16:18:41.98'),
    ('003', 62, '2026-07-31', 3, None, '2026-07-31T16:26:23.903'),
    ('003', 63, '2026-07-31', 3, None, '2026-07-31T16:35:54.293'),
    ('003', 64, '2026-07-31', 3, None, '2026-07-31T16:39:32.563'),
    ('003', 65, '2026-09-02', 3, None, '2026-09-02T11:58:12.977'),
    ('003', 66, '2026-09-04', 2, '2026-09-04T11:22:15.9', None),
    ('003', 67, '2026-09-09', 3, None, '2026-09-09T14:44:56.247'),
    ('003', 68, '2026-09-10', 3, None, '2026-09-10T10:51:13.99'),
    ('003', 69, '2026-09-17', 3, None, '2026-09-17T17:07:05.873'),
    ('003', 70, '2026-09-17', 3, None, '2026-09-17T17:48:22.243'),
    ('003', 71, '2026-09-17', 3, None, '2026-09-17T18:05:57.057'),
]


def _oc_real(db, rowid, co, consec, fecha, estado, ts_parcial, ts_cumplido,
             prov='860028580'):
    from app.models.compras_fuentes import OcLineaSiesa
    from app.services.compras_oc_sync import _dt
    db.session.add(OcLineaSiesa(
        rowid_linea=rowid, co=co, tipo_docto='OC', consec_docto=consec,
        fecha_oc=date.fromisoformat(fecha), estado_oc=estado, proveedor_codigo=prov,
        moneda='COP', referencia='PROD-001', bodega='NB1', abierta=estado != 3,
        fecha_parcial=_dt(ts_parcial), fecha_cumplido=_dt(ts_cumplido)))
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

    def test_ocs_registradas_al_recibir_no_miden_al_proveedor(self, app, db):
        """DISPAPELES en Siesa QA: 24 OCs con entrada, las 24 el MISMO día de la
        OC (creada y cumplida con minutos de diferencia: se digitó al recibir).
        Antes salía «0 ± 0 días, MEDIDO, confianza ALTA». Ahora se cuentan
        aparte y, sin otra muestra, cae al default declarado."""
        from app.services.compras_fuentes import (lead_time, lead_times_por_proveedor,
                                                  observaciones_lead_time, LT_NACIONAL_DIAS)
        for i, oc in enumerate(OCS_DISPAPELES_QA):
            _oc_real(db, 5000 + i, *oc)
        obs = observaciones_lead_time()
        assert obs['por_oc'] == []
        assert obs['descartadas']['oc_registrada_al_recibir'] == 24
        assert obs['descartadas_por_proveedor']['860028580'] == {'oc_registrada_al_recibir': 24}
        r = lead_time(proveedor='860028580', origen='NACIONAL', observaciones=obs)
        assert r['nivel'] == 'DEFAULT' and r['fuente'] == 'DEFAULT_CONSERVADOR'
        assert r['lt_dias'] == LT_NACIONAL_DIAS and r['confianza'] == 'NINGUNA'
        assert r['descartadas_al_recibir'] == 24 and 'mismo día' in r['nota']
        fila = lead_times_por_proveedor(obs)
        assert [(f['proveedor'], f['n_proveedor'], f['n_descartadas_al_recibir'])
                for f in fila] == [('860028580', 0, 24)]

    def test_un_dia_si_es_una_observacion(self, app, db):
        from app.services.compras_fuentes import observaciones_lead_time
        _oc_cumplida(db, 1, 1, 1)
        _oc_cumplida(db, 2, 2, 0)
        obs = observaciones_lead_time()
        assert [o['dias'] for o in obs['por_oc']] == [1]
        assert obs['descartadas'] == {'oc_registrada_al_recibir': 1}

    def test_las_validas_siguen_midiendo_aunque_haya_descartadas(self, app, db):
        from app.services.compras_fuentes import lead_time
        for i, d in enumerate((0, 0, 0, 3, 4, 5)):
            _oc_cumplida(db, i + 1, i + 1, d)
        r = lead_time(proveedor='800123', origen='NACIONAL')
        assert r['nivel'] == 'PROVEEDOR' and r['n'] == 3 and r['lt_medido'] == 4
        assert r['descartadas_al_recibir'] == 3

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
        # Integración con el motor (D9): con 3 a 5 contenedores se usa el MAYOR
        # entre lo medido (100 ± 10) y el conservador (105 ± 15). Antes esta
        # prueba esperaba 100: lo medido reemplazaba al conservador desde 3.
        assert r['nivel'] == 'ORIGEN' and r['n'] == 3 and r['fuente'] == 'PARCIAL'
        assert r['lt_medido'] == 100 and r['lt_medio'] == 105 and r['sigma_lt'] == 15

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
        # La de USD es más vieja: su conversión se prueba aparte (D4).
        _oc_precio(db, 3, date(2026, 7, 1), 9, moneda='USD')
        _oc_precio(db, 4, date(2026, 9, 2), 0, obsequio=1)
        _oc_precio(db, 5, date(2026, 9, 3), 5000, estado=9)          # anulada
        p = precios_oc(['PROD-001'])['PROD-001']
        assert p['costo'] == 1000 and p['fecha_costo'] == '2026-08-01'

    def test_la_linea_real_en_paquetes_vale_su_valor_bruto(self, app, db):
        """OC 003-OC-28: precio 1.250 por PQ de 12 → 104,17 por unidad, por 36
        unidades = 3.750, el `f421_vlr_bruto` que mandó Siesa."""
        from app.services.compras_fuentes import lineas_precio_oc
        _sync(SiesaFalsa({ABIERTAS_1: [fila_real(LINEAS_REALES[11])]}))
        l = lineas_precio_oc(['PAPELSP6948'])[0]
        assert l['cantidad_base'] == 36
        assert round(l['precio_base'] * l['cantidad_base'], 2) == 3750

    def test_una_oc_en_usd_se_nacionaliza_con_a_cop(self, app, db, producto, monkeypatch):
        """Integración con D4: la moneda se lleva a pesos con UNA política
        (`costo_service.a_cop`, la de acuerdos y cotizaciones), declarada en
        `conversion`. Antes las OCs en USD se excluían sin más."""
        from app.services.compras_fuentes import precios_oc
        monkeypatch.setenv('TRM_COP_USD', '4000')
        monkeypatch.setenv('FACTOR_NACIONALIZACION', '1.5')
        _oc_precio(db, 1, date(2026, 8, 1), 1000)
        _oc_precio(db, 2, date(2026, 9, 1), 24, moneda='USD', factor=12)   # 2 USD/u
        p = precios_oc(['PROD-001'])['PROD-001']
        assert p['costo'] == 2 * 4000 * 1.5 and p['moneda'] == 'USD'
        assert p['conversion']['trm'] == 4000 and p['fecha_costo'] == '2026-09-01'

    def test_otra_moneda_se_excluye_declarada(self, app, db, producto):
        from app.services.compras_fuentes import precios_oc
        from app.services.costo_service import resolver_costos
        _oc_precio(db, 1, date(2026, 9, 1), 9, moneda='EUR')
        p = precios_oc(['PROD-001'])['PROD-001']
        assert p['costo'] is None and p['lineas_otra_moneda'] == 1
        assert 'EUR' in p['motivo_exclusion']
        assert resolver_costos(['PROD-001'])['PROD-001']['fuente'] != 'OC_SIESA'

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

    def test_la_deriva_compara_la_oc_contra_el_acuerdo(self, app, db, producto):
        """Integración: el enchufe del motor (`precios_de_compra_recibidos`)
        lee las OCs de Siesa, y `detectar_deriva` es el ÚNICO comparador OC ↔
        acuerdo (se retiró `compras_fuentes.precio_oc_vs_acuerdo`, que hacía lo
        mismo sin cantidades ni moneda). Con cantidad: el impacto es plata."""
        from app.models.acuerdo_marco import AcuerdoMarco, Proveedor
        from app.models.compras_fuentes import OcLineaSiesa
        from app.services.compras_inteligencia_service import ComprasInteligenciaService
        from app.utils.fecha import dia_operativo
        hoy_operativo = dia_operativo()
        prov = Proveedor(codigo='800123', nombre='P')
        db.session.add(prov)
        db.session.flush()
        db.session.add(AcuerdoMarco(producto_id=producto.id, proveedor_id=prov.id,
                                    precio_unitario=1000, vigencia_desde=hoy_operativo - timedelta(days=30),
                                    vigencia_hasta=hoy_operativo + timedelta(days=30)))
        db.session.add(OcLineaSiesa(
            rowid_linea=1, co='003', tipo_docto='OC', consec_docto=1, fecha_oc=hoy_operativo,
            estado_oc=1, referencia='PROD-001', precio_unitario=1050, factor=1,
            cant_pedida=10, cant_pedida_base=10, moneda='COP', ind_obsequio=0, proveedor_codigo='800123',
            abierta=True))
        db.session.commit()
        r = ComprasInteligenciaService.detectar_deriva()
        assert r['fuente_precio_compra'] == 'OC_SIESA' and r['total'] == 1
        d = r['derivas'][0]
        assert d['diferencia_pct'] == 5.0 and d['cantidad'] == 10
        assert r['impacto_estimado_cop'] == 500 and d['mismo_proveedor'] is True
        assert d['oc'] == '003-OC-1' and r['nota'] is None


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


#: Filas reales de `API_v2_ItemsCriterios` filtrada por `f125_id_plan =
#: ''P03''` (Siesa QA, 2026-09-25).
CRITERIOS_P03_QA = [
    {'f120_ts': '2024-12-05T17:53:38.39', 'f120_id_cia': 1, 'f120_id': 1, 'f120_rowid': 2, 'f120_referencia': 'PAPELSP01', 'f120_descripcion': 'ABACO DE MADERA BEADS AROUND', 'f125_ts': '2024-07-05T14:36:58.09', 'f125_id_plan': 'P03', 'f105_descripcion': 'MARCA', 'f125_id_criterio_mayor': 'M009', 'f106_descripcion': 'IHO NEGOCIOS SAS'},
    {'f120_ts': '2024-01-26T11:34:11.91', 'f120_id_cia': 1, 'f120_id': 355, 'f120_rowid': 357, 'f120_referencia': 'PAPELSP016', 'f120_descripcion': 'ADAPTA FOAM MOUSE Y SUAVIDAD 280ML', 'f125_ts': '2025-01-14T11:57:37.633', 'f125_id_plan': 'P03', 'f105_descripcion': 'MARCA', 'f125_id_criterio_mayor': 'M001', 'f106_descripcion': 'NORMA'},
    {'f120_ts': '2023-12-12T10:02:23.43', 'f120_id_cia': 1, 'f120_id': 958, 'f120_rowid': 956, 'f120_referencia': 'FIESTSF104', 'f120_descripcion': 'BASE PARA GLOBO QUIROND PLASTICO JD02-7', 'f125_ts': '2024-12-06T11:03:57.697', 'f125_id_plan': 'P03', 'f105_descripcion': 'MARCA', 'f125_id_criterio_mayor': 'M003', 'f106_descripcion': 'QUIROND'},
]
FILTRO_P03 = "f125_id_plan = ''P03''"


@pytest.fixture
def productos_p03(db):
    from app.models.producto import Producto
    ps = [Producto(codigo=r, nombre=r, codigo_siesa=r, activo=True)
          for r in ('PAPELSP016', 'FIESTSF104')]
    db.session.add_all(ps)
    db.session.commit()
    return ps


def _leer(gw, **kw):
    from app.services.maestro_compras_carga import leer_marca_siesa
    return leer_marca_siesa(gateway=gw, pausa_s=0, **kw)


class TestMarcaDesdeSiesa:

    def test_sin_criterio_no_lee_nada(self, app, db, monkeypatch):
        from app.services.maestro_compras_carga import leer_marca_siesa, marca_desde_siesa
        monkeypatch.delenv('SIESA_CRITERIO_MARCA', raising=False)
        gw = SiesaFalsa()
        assert 'SIESA_CRITERIO_MARCA' in leer_marca_siesa(gateway=gw)['omitido']
        assert 'SIESA_CRITERIO_MARCA' in marca_desde_siesa()['omitido']
        assert not gw.llamadas

    def test_lee_items_criterios_filtrando_el_plan_con_comillas(self, app, db, monkeypatch):
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        gw = SiesaFalsa({FILTRO_P03: CRITERIOS_P03_QA})
        r = _leer(gw)
        assert r['ok'] and r['items'] == 3 and r['criterios_distintos'] == 3
        assert gw.llamadas[0][0] == 'API_v2_ItemsCriterios'
        assert gw.llamadas[0][1]['parametros'] == FILTRO_P03
        assert all(c[0] != '238920' for c in gw.llamadas), 'el 238920 es un plano de importación'

    def test_la_vista_previa_lee_lo_guardado_nunca_siesa(self, app, db, monkeypatch, productos_p03):
        from app.services.maestro_compras_carga import marca_desde_siesa
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        assert 'sin_lectura' in marca_desde_siesa()
        _leer(SiesaFalsa({FILTRO_P03: CRITERIOS_P03_QA}))
        prev = marca_desde_siesa()
        assert prev['resumen']['validas'] == 2 and prev['fuera_del_catalogo'] == 1
        cambios = {v['codigo']: v['cambios'] for v in prev['validas']}
        assert cambios['PAPELSP016'] == {'marca_siesa': {'antes': None, 'despues': 'NORMA'},
                                         'marca_codigo': {'antes': None, 'despues': 'M001'}}

    def test_aplicar_guarda_el_nombre_y_el_codigo_aparte(self, app, db, monkeypatch, productos_p03):
        from app.services.maestro_compras_carga import marca_desde_siesa
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        _leer(SiesaFalsa({FILTRO_P03: CRITERIOS_P03_QA}))
        r = marca_desde_siesa(aplicar_=True)
        assert r['escritas'] == 2
        p = productos_p03[1]
        db.session.refresh(p)
        assert p.marca_siesa == 'QUIROND' and p.marca_codigo == 'M003'
        assert p.marca_fuente == 'SIESA_CRITERIOS'

    def test_una_lectura_incompleta_no_se_guarda_ni_se_aplica(self, app, db, monkeypatch,
                                                               productos_p03):
        from app.models.compras_fuentes import MarcaSiesaLectura
        from app.services.maestro_compras_carga import marca_desde_siesa
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        p1 = [dict(CRITERIOS_P03_QA[0], f120_rowid=i, f120_referencia=f'X{i}') for i in range(100)]
        p2 = [dict(CRITERIOS_P03_QA[0], f120_rowid=50, f120_referencia='X50')]
        r = _leer(SiesaFalsa({FILTRO_P03: lambda pag: p1 if pag == 1 else p2}))
        assert r['ok'] is False and 'repetido' in r['motivo_incompleta']
        assert MarcaSiesaLectura.query.count() == 0
        assert 'sin_lectura' in marca_desde_siesa()

    def test_el_tope_de_paginas_es_incompleta(self, app, db, monkeypatch):
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        monkeypatch.setenv('COMPRAS_MARCA_MAX_PAGINAS', '1')
        llenas = [dict(CRITERIOS_P03_QA[0], f120_rowid=i) for i in range(100)]
        r = _leer(SiesaFalsa({FILTRO_P03: llenas * 3}))
        assert r['ok'] is False and 'tope' in r['motivo_incompleta']

    def test_si_siesa_ignora_el_filtro_los_otros_planes_no_entran(self, app, db, monkeypatch):
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        otro = dict(CRITERIOS_P03_QA[0], f125_id_plan='P01', f125_id_criterio_mayor='L10 ',
                    f106_descripcion='JUGUETERIA')
        r = _leer(SiesaFalsa({FILTRO_P03: CRITERIOS_P03_QA + [otro]}))
        assert r['ok'] and r['items'] == 3 and r['otro_plan'] == 1

    def test_campos_desconocidos_no_se_adivinan(self, app, db, monkeypatch):
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        r = _leer(SiesaFalsa({FILTRO_P03: [{'otro_campo': 1, 'f120_rowid': 1}]}))
        assert r['ok'] is False and 'No se adivina' in r['motivo_incompleta']

    def test_una_lectura_de_otro_plan_no_vale(self, app, db, monkeypatch, productos_p03):
        from app.services.maestro_compras_carga import marca_desde_siesa
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        _leer(SiesaFalsa({FILTRO_P03: CRITERIOS_P03_QA}))
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P02')
        assert 'P03' in marca_desde_siesa()['sin_lectura']

    def test_el_boton_no_lee_siesa_dentro_del_request(self, app, db, monkeypatch):
        from app.services import fotos_siesa_service, maestro_compras_carga as m
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        monkeypatch.setattr(fotos_siesa_service, 'ventana_abierta', lambda *a: True)
        lanzados = []
        monkeypatch.setattr(m, 'leer_marca_siesa', lambda *a, **k: pytest.fail('dentro del request'))
        r = m.disparar_lectura_marca(app, lanzar=lanzados.append)
        assert r['ok'] and r['codigo'] == 202 and len(lanzados) == 1

    def test_con_una_lectura_en_curso_no_arranca_otra(self, app, db, monkeypatch):
        from app.services import fotos_siesa_service, registro_sync_service as reg
        from app.services.maestro_compras_carga import disparar_lectura_marca
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        monkeypatch.setattr(fotos_siesa_service, 'ventana_abierta', lambda *a: True)
        reg.abrir('compras_marca')
        r = disparar_lectura_marca(app, lanzar=lambda fn: pytest.fail('no debía lanzar'))
        assert r['codigo'] == 409 and 'en curso' in r['error']

    def test_fuera_de_la_ventana_no_lee(self, app, db, monkeypatch):
        from app.services import fotos_siesa_service
        from app.services.maestro_compras_carga import disparar_lectura_marca
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        monkeypatch.setattr(fotos_siesa_service, 'ventana_abierta', lambda *a: False)
        r = disparar_lectura_marca(app, lanzar=lambda fn: pytest.fail('no debía lanzar'))
        assert r['codigo'] == 409 and 'Regla 14' in r['error']

    def test_el_armador_reconoce_la_marca_china_por_codigo(self, app, db, producto, monkeypatch):
        """El nombre va a `marca_siesa` y `MARCAS_CHINA` son códigos: sin
        `marca_codigo` el cruce se perdía al guardar el nombre."""
        from app.services.armador_service import ArmadorService
        from app.services.kardex_service import KardexService
        monkeypatch.setattr(KardexService, 'demanda_descensurada', lambda *a, **k: {
            'PROD-001': {'d_avg': 2.0, 'sigma_d': 0.0, 'dias_con_stock': 300,
                         'dias_ventana': 360, 'demanda_neta': 600,
                         'factor_censura': 1.0, 'censurado': False}})
        producto.marca_siesa, producto.marca_codigo = 'QUIROND', 'M003'
        db.session.commit()
        r = ArmadorService.rop_dual()
        assert [i['referencia'] for i in r['china']['items']] == ['PROD-001']


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

    def test_marca_leer_y_estado(self, client, token_compras, monkeypatch):
        from app.services import fotos_siesa_service, maestro_compras_carga as m
        monkeypatch.setenv('SIESA_CRITERIO_MARCA', 'P03')
        monkeypatch.setattr(fotos_siesa_service, 'ventana_abierta', lambda *a: True)
        lanzados = []
        orig = m.disparar_lectura_marca
        monkeypatch.setattr(m, 'disparar_lectura_marca',
                            lambda app: orig(app, lanzar=lanzados.append))
        r = client.post('/api/compras/fuentes/marca-siesa/leer', headers=token_compras, json={})
        assert r.status_code == 202 and 'codigo' not in r.get_json() and len(lanzados) == 1
        e = client.get('/api/compras/fuentes/marca-siesa/estado', headers=token_compras)
        assert e.status_code == 200 and e.get_json()['plan_configurado'] == 'P03'
        v = client.get('/api/compras/fuentes/marca-siesa/vista-previa', headers=token_compras)
        assert v.status_code == 200 and 'sin_lectura' in v.get_json()

    def test_marca_leer_sin_plan_es_400(self, client, token_compras, monkeypatch):
        monkeypatch.delenv('SIESA_CRITERIO_MARCA', raising=False)
        r = client.post('/api/compras/fuentes/marca-siesa/leer', headers=token_compras, json={})
        assert r.status_code == 400

    def test_health_declara_las_dos_fuentes(self, client, jwt_token_admin):
        r = client.get('/api/health/siesa', headers={'Authorization': f'Bearer {jwt_token_admin}'})
        d = r.get_json()
        assert d['compras_oc']['encendido'] is False and 'cron_en_este_proceso' in d['compras_oc']
        assert d['kardex_auto']['encendido'] is False
