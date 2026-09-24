"""
Contado contraentrega vs crédito real (2026-09-24).

REGLA DEL DUEÑO: las facturas de ruta salen en Siesa como «crédito» corto solo
por lo que tarda la ruta; en la calle se cobran CONTRAENTREGA. Toda factura con
≤ 15 días de crédito se trata en el WMS como contado (el conductor la cobra, la
liquidación la exige). > 15 días es crédito real y se entrega sin cobrar.

Lo que había: `cobra_en_la_puerta` cobraba SOLO con C01 o exactamente
`SIESA_COND_PAGO_RUTA`. Una C03 (8 días) salía «💳 no se cobra», la forma de
pago quedaba CREDITO sola, la liquidación no hacía nada y la reconciliación la
sacaba del denominador. EXENTO pasaba en contado, un ENTREGADO con $0 pasaba, y
sin condición («no sé») la guarda no actuaba.

La CLASE, no el caso: *una decisión de cobro tomada fuera de la política* —
comparando `forma_pago == 'CREDITO'` o leyendo un código crudo—. El caso C03
es una instancia; los trinquetes de abajo cubren la clase, con inventario que
solo encoge, meta-tests y piso.
"""
import ast
import json
import pathlib
import subprocess
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
from app.services import cond_pago as cp
from app.services.ruta_service import RutaService

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_V3 = {'version_formulario': cp.VERSION_FORMULARIO_CONTADO}
_FOTO = 'data:image/jpeg;base64,' + 'A' * 40


@pytest.fixture(autouse=True)
def _tabla_limpia(monkeypatch):
    """Ningún test hereda una sobreescritura del entorno ni la caché de la
    consulta dinámica de otro test."""
    for v in ('COBRO_CONTRAENTREGA_MAX_DIAS', 'SIESA_COND_PAGO_DIAS',
              'CONNEKTA_CONSULTA_COND_PAGO'):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setitem(cp._CACHE_CONSULTA, 'tabla', None)
    monkeypatch.setitem(cp._CACHE_CONSULTA, 'en', None)
    monkeypatch.setitem(cp._CACHE_CONSULTA, 'error', None)


# ═════════════════════════════════════════════════════════════════════════════
# Helpers de mundo
# ═════════════════════════════════════════════════════════════════════════════

def _conductor(db):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    s = uuid.uuid4().hex[:6]
    u = Usuario(email=f'cc_{s}@test.com', nombre='Conductor CC', rol='conductor', activo=True)
    u.set_password('x')
    db.session.add(u); db.session.flush()
    c = Conductor(usuario_id=u.id, nombre='Conductor CC', cedula=f'CC{s}', activo=True)
    db.session.add(c); db.session.flush()
    return c


def _ruta(db, almacen, cond=None, valor=None, conductor=None, cliente='Tienda Uno',
          estado='EN_TRANSITO', tipo_documento='PEDIDO', ruta=None):
    """Una parada en una ruta: la tarea con su condición ANOTADA (como la deja
    `_valor_y_cond_pago`) y dos bultos. `ruta` permite varias paradas."""
    from app.models.bulto import Bulto
    from app.models.packing import TareaPacking
    from app.models.ruta_despacho import RutaDespacho
    conductor = conductor or _conductor(db)
    if ruta is None:
        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado=estado,
                            fecha_cierre=datetime.utcnow() - timedelta(hours=1))
        db.session.add(ruta); db.session.flush()
    s = uuid.uuid4().hex[:6]
    t = TareaPacking(codigo=f'PK-CC-{s}', estado='DESPACHADO', almacen_id=almacen.id,
                     tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1,
                     numero_pedido_siesa=f'PED-CC-{s}', cliente=cliente, municipio='Neiva',
                     cond_pago=cond, valor_factura=valor, tipo_documento=tipo_documento)
    db.session.add(t); db.session.flush()
    for i in (1, 2):
        db.session.add(Bulto(tarea_id=t.id, ruta_despacho_id=ruta.id,
                             codigo_barras=f'B-{uuid.uuid4().hex[:8]}', tipo='CAJA',
                             numero=i, total=2, estado='CARGADO'))
    db.session.commit()
    return ruta, t, conductor


def _confirmar(ruta, t, c, **datos):
    base = {'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': 0}
    base.update(datos)
    return RutaService.confirmar_parada(ruta.id, t.id, c.usuario_id, base)


def _recaudo(ruta, t):
    return RecaudoEntrega.query.filter_by(ruta_id=ruta.id, tarea_id=t.id).first()


def _procesar(recaudo, db):
    from app.services.liquidacion_service import _procesar_recaudo
    with patch('app.services.liquidacion_service._obtener_tercero',
               return_value=('900123456', '001')):
        r = _procesar_recaudo(recaudo, 'test contado')
    db.session.commit()
    return r


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La política
# ═════════════════════════════════════════════════════════════════════════════

class TestLaTablaSaleDelPDF:
    """La columna «Dias Vcto», nunca la descripción."""

    @pytest.mark.parametrize('codigo,dias', [
        ('C01', 0), ('C02', 1), ('C03', 8), ('C04', 30), ('C05', 45), ('C06', 60),
        ('C07', 75), ('C08', 90), ('C09', 120), ('P01', 30), ('P02', 45), ('P03', 60),
        ('P04', 75), ('P05', 90), ('P06', 120), ('P07', 180),
        # La descripción miente: «40 DIAS», «210 DIAS», «240 DIAS» → 180.
        ('P08', 180), ('P09', 180), ('P10', 180),
    ])
    def test_dias_de_cada_codigo(self, codigo, dias):
        assert cp.dias_credito(codigo) == dias

    def test_el_pdf_dice_lo_mismo_que_la_copia(self):
        """Si alguien actualiza el PDF y no la copia (o al revés), rojo. Lee el
        PDF con `pdftotext` cuando está disponible."""
        import shutil
        pdf = _RAIZ / 'docs' / 'siesa-specs' / 'Condiciones de pago.pdf'
        if not shutil.which('pdftotext') or not pdf.exists():
            pytest.skip('sin pdftotext o sin el PDF')
        texto = subprocess.run(['pdftotext', '-layout', str(pdf), '-'],
                               capture_output=True, text=True, check=True).stdout
        import re
        leidos = {}
        for linea in texto.splitlines():
            m = re.match(r'^\s*([CP]\d{2})\s+.+?\s+(\d+)\s+\d+\s+[\d,]+\s', linea)
            if m:
                leidos[m.group(1)] = int(m.group(2))
        assert leidos == cp.DIAS_POR_CODIGO_PDF

    def test_normaliza_el_codigo(self):
        assert cp.dias_credito(' c03 ') == 8


class TestCobroContraentrega:

    @pytest.mark.parametrize('dias', [0, 1, 8, 15])
    def test_hasta_el_umbral_se_cobra(self, monkeypatch, dias):
        monkeypatch.setenv('SIESA_COND_PAGO_DIAS', json.dumps({'XX': dias}))
        r = cp.cobro_contraentrega('XX')
        assert r == {'cobrar': True, 'dias': dias, 'codigo': 'XX', 'origen': cp.MAESTRO,
                     'umbral': 15}

    @pytest.mark.parametrize('dias', [16, 30])
    def test_pasado_el_umbral_es_credito_real(self, monkeypatch, dias):
        monkeypatch.setenv('SIESA_COND_PAGO_DIAS', json.dumps({'XX': dias}))
        r = cp.cobro_contraentrega('XX')
        assert r['cobrar'] is False and r['origen'] == cp.MAESTRO

    @pytest.mark.parametrize('codigo', ['C01', 'C02', 'C03'])
    def test_c01_c02_c03_se_cobran(self, codigo):
        assert cp.cobro_contraentrega(codigo)['cobrar'] is True

    @pytest.mark.parametrize('codigo', ['C04', 'C05', 'C09', 'P01', 'P08', 'P10'])
    def test_c04_en_adelante_y_las_p_no(self, codigo):
        assert cp.cobro_contraentrega(codigo)['cobrar'] is False

    @pytest.mark.parametrize('codigo', [None, '', '   '])
    def test_ausente_se_cobra_y_se_declara(self, codigo):
        r = cp.cobro_contraentrega(codigo)
        assert r['cobrar'] is True and r['origen'] == cp.SUPUESTO_AUSENTE and r['dias'] is None

    def test_desconocido_se_cobra_y_se_declara(self):
        r = cp.cobro_contraentrega('Z99')
        assert r['cobrar'] is True and r['origen'] == cp.SUPUESTO_DESCONOCIDO

    def test_sin_maestro_se_cobra_y_se_declara(self, monkeypatch):
        monkeypatch.setattr(cp, 'tabla_dias', lambda: ({}, 'CODIGO', []))
        r = cp.cobro_contraentrega('C04')
        assert r['cobrar'] is True and r['origen'] == cp.SIN_MAESTRO

    def test_solo_un_credito_conocido_no_se_cobra(self):
        """La propiedad entera, sobre todo el universo de entradas."""
        for c in list(cp.DIAS_POR_CODIGO_PDF) + [None, '', 'Z9']:
            r = cp.cobro_contraentrega(c)
            if not r['cobrar']:
                assert r['origen'] == cp.MAESTRO and r['dias'] > r['umbral']


class TestElUmbral:

    def test_configurable(self, monkeypatch):
        monkeypatch.setenv('COBRO_CONTRAENTREGA_MAX_DIAS', '30')
        assert cp.cobro_contraentrega('C04')['cobrar'] is True
        assert cp.cobro_contraentrega('C05')['cobrar'] is False

    @pytest.mark.parametrize('malo', ['', 'quince', '-3', '1.5'])
    def test_invalido_cae_a_15_y_se_declara(self, monkeypatch, malo):
        monkeypatch.setenv('COBRO_CONTRAENTREGA_MAX_DIAS', malo)
        umbral, problema = cp.umbral_dias()
        assert umbral == 15 and problema
        assert problema in cp.estado_politica()['problemas']


class TestLaTablaSeSobreescribe:

    def test_json_invalido_se_ignora_entero_y_se_declara(self, monkeypatch):
        monkeypatch.setenv('SIESA_COND_PAGO_DIAS', '{no es json')
        assert cp.dias_credito('C03') == 8
        assert any('JSON' in p for p in cp.estado_politica()['problemas'])

    def test_una_entrada_invalida_no_tumba_las_otras(self, monkeypatch):
        monkeypatch.setenv('SIESA_COND_PAGO_DIAS', '{"C10": 20, "C11": "x", "C12": -1}')
        assert cp.dias_credito('C10') == 20
        assert cp.dias_credito('C11') is None and cp.dias_credito('C12') is None
        assert len(cp.estado_politica()['problemas']) == 2
        assert cp.tabla_dias()[1] == 'ENV'


class TestLaConsultaDinamicaManda:
    """Gancho para `CONNEKTA_CONSULTA_COND_PAGO` (sin default). Solo GET."""

    def _con_consulta(self, monkeypatch, filas):
        from app.services.connekta_gateway import connekta
        monkeypatch.setenv('CONNEKTA_CONSULTA_COND_PAGO', 'papeleriamedellin_WMS_CondPago')
        monkeypatch.setattr(type(connekta), '_get',
                            lambda self, *a, **k: {'detalle': {'Datos': filas}})

    def test_si_responde_manda_y_se_declaran_las_diferencias(self, monkeypatch):
        self._con_consulta(monkeypatch, [{'f208_id': 'C03', 'f208_dias_vcto': 20},
                                         {'f208_id': 'C02', 'f208_dias_vcto': 1}])
        r = cp.refrescar_tabla_desde_siesa(forzar=True)
        assert r['filas'] == 2 and r['error'] is None
        assert cp.cobro_contraentrega('C03')['cobrar'] is False   # manda Siesa
        estado = cp.estado_politica()
        assert estado['fuente_tabla'] == 'CONSULTA'
        assert {'codigo': 'C03', 'siesa': 20, 'copia': 8} in estado['divergencias_con_siesa']

    def test_si_falla_queda_la_copia_y_se_declara(self, monkeypatch):
        from app.services.connekta_gateway import connekta
        monkeypatch.setenv('CONNEKTA_CONSULTA_COND_PAGO', 'x')

        def _explota(self, *a, **k):
            raise RuntimeError('401')
        monkeypatch.setattr(type(connekta), '_get', _explota)
        r = cp.refrescar_tabla_desde_siesa(forzar=True)
        assert '401' in r['error']
        assert cp.tabla_dias()[1] == 'CODIGO'
        assert cp.cobro_contraentrega('C03')['cobrar'] is True

    def test_sin_configurar_no_sale_a_la_red(self, monkeypatch):
        from app.services.connekta_gateway import connekta

        def _no(self, *a, **k):
            raise AssertionError('no debía consultar')
        monkeypatch.setattr(type(connekta), '_get', _no)
        assert cp.refrescar_tabla_desde_siesa() == {'configurada': False}


class TestLaEtiquetaDelConductor:

    def test_contado(self):
        assert cp.etiqueta_conductor(cp.cobro_contraentrega('C02'))['texto'] == \
            'Contado contraentrega · C02 (1 día) — cobrá al entregar'
        assert '(8 días)' in cp.etiqueta_conductor(cp.cobro_contraentrega('C03'))['texto']

    def test_credito_real(self):
        assert cp.etiqueta_conductor(cp.cobro_contraentrega('C04'))['texto'] == \
            'Crédito 30 días — no se cobra'

    def test_supuesto_avisa(self):
        e = cp.etiqueta_conductor(cp.cobro_contraentrega(None))
        assert e['tono'] == 'warn' and 'sin condición de pago' in e['texto'].lower()


# ═════════════════════════════════════════════════════════════════════════════
# 9 · El vencimiento de la FE (decisión del dueño, 2026-09-24)
# ═════════════════════════════════════════════════════════════════════════════

class TestVencimientoFE:

    @pytest.mark.parametrize('codigo,dias', [
        ('C02', 15), ('C03', 15), ('C01', 15), ('C04', 30), ('C05', 45),
        (None, 15), ('', 15), ('Z9', 15), ('P08', 180),
    ])
    def test_contado_la_ventana_credito_sus_dias(self, codigo, dias):
        esperado = (date(2026, 9, 24) + timedelta(days=dias)).strftime('%Y%m%d')
        assert cp.vencimiento_fe('20260924', codigo) == esperado
        assert cp.vencimiento_fe(date(2026, 9, 24), codigo) == esperado

    def test_sigue_al_umbral(self, monkeypatch):
        monkeypatch.setenv('COBRO_CONTRAENTREGA_MAX_DIAS', '10')
        assert cp.vencimiento_fe('20260924', 'C02') == '20261004'

    def test_la_fe_de_ruta_lo_usa(self, db, monkeypatch):
        """El payload real del 142943 lleva el vencimiento de la política,
        calculado sobre la fecha Bogotá con la que sale `F350_FECHA`."""
        from app.services.connekta_gateway import connekta
        capturado = {}
        monkeypatch.setattr(type(connekta), '_post',
                            lambda self, c, n, payload, **k: capturado.setdefault('p', payload) and {'codigo': 0})
        monkeypatch.setattr(connekta, 'modo_simulacion', True, raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02', raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'punto_envio_default', '001', raising=False)
        cab = {'f200_id_pedido_fact': '900', 'f430_id_cond_pago': 'C04',
               'f461_id_punto_envio': '001'}
        resp = connekta.trigger_factura_desde_remision('RM', 5, cab)
        cuota = capturado['p']['CuotasCxC'][0]
        hoy = capturado['p']['Doctoventascomercial'][0]['F350_FECHA']
        assert cuota['F353_FECHA_VCTO'] == cp.vencimiento_fe(hoy, 'C04')
        assert cuota['F353_FECHA_VCTO'] == (datetime.strptime(hoy, '%Y%m%d')
                                            + timedelta(days=30)).strftime('%Y%m%d')
        assert resp['cond_pago_emitida'] == 'C04'

        cab['f430_id_cond_pago'] = ''      # sin condición → sale en C02 → +15
        capturado.clear()
        connekta.trigger_factura_desde_remision('RM', 6, cab)
        assert capturado['p']['CuotasCxC'][0]['F353_FECHA_VCTO'] == cp.vencimiento_fe(hoy, 'C02')


#: `F353_FECHA_VCTO` que NO es el vencimiento de una FE emitida por el WMS.
#: Inventario que solo encoge; cada entrada dice por qué.
F353_FUERA_DE_LA_POLITICA = {
    ('app/services/connekta_liquidacion_gateway.py', 'trigger_nota_factura_crear_cruzar'):
        'NC 251126: el cruce lleva el vencimiento REAL de la factura que cruza '
        '(`get_vencimiento_factura`), no uno nuevo',
    ('app/services/connekta_liquidacion_gateway.py', 'trigger_documento_contable'):
        'DC 142882 de retención: documento contable que cruza hoy, no una factura',
}


def _sitios_f353():
    sitios = []
    for f in sorted((_RAIZ / 'app').rglob('*.py')):
        arbol = ast.parse(f.read_text(encoding='utf-8'))
        for fn in ast.walk(arbol):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for d in ast.walk(fn):
                if isinstance(d, ast.Dict):
                    for k in d.keys:
                        if isinstance(k, ast.Constant) and str(k.value).upper() == 'F353_FECHA_VCTO':
                            sitios.append((f.relative_to(_RAIZ).as_posix(), fn))
    return sitios


def _usa_vencimiento_fe(fn) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == 'vencimiento_fe' for n in ast.walk(fn))


class TestNingunVencimientoSeArmaFueraDeLaPolitica:

    def test_toda_fe_usa_vencimiento_fe(self):
        malos = [(a, fn.name) for a, fn in _sitios_f353()
                 if (a, fn.name) not in F353_FUERA_DE_LA_POLITICA and not _usa_vencimiento_fe(fn)]
        assert not malos, f'F353_FECHA_VCTO armado fuera de cond_pago.vencimiento_fe: {malos}'

    def test_el_inventario_solo_encoge(self):
        vivos = {(a, fn.name) for a, fn in _sitios_f353()}
        muertos = set(F353_FUERA_DE_LA_POLITICA) - vivos
        assert not muertos, f'entradas que ya no existen — borrarlas: {muertos}'
        assert len(F353_FUERA_DE_LA_POLITICA) <= 2

    def test_cada_excepcion_dice_por_que(self):
        assert all(len(v) > 30 for v in F353_FUERA_DE_LA_POLITICA.values())

    def test_piso(self):
        """Hoy: 238925 y 142943 (FE) + NC + DC. Un escáner roto daría cero."""
        assert len(_sitios_f353()) >= 4

    def test_el_detector_ve_un_mas_30(self):
        fn = ast.parse("def f():\n    v = core._fecha_bogota_mas(30)\n"
                       "    return {'F353_FECHA_VCTO': v}\n").body[0]
        assert not _usa_vencimiento_fe(fn)
        fn2 = ast.parse("def f():\n    v = _cp.vencimiento_fe(h, c)\n"
                        "    return {'F353_FECHA_VCTO': v}\n").body[0]
        assert _usa_vencimiento_fe(fn2)


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El snapshot
# ═════════════════════════════════════════════════════════════════════════════

class TestElSnapshot:

    def test_la_fe_manda_sobre_el_pedido_y_se_declara(self, db, almacen):
        _r, t, _c = _ruta(db, almacen, cond='C02')
        r = cp.anotar_en_tarea(t, cond_fe='C04')
        assert r['difiere_del_pedido'] is True and r['cobrar'] is False
        assert (t.cond_pago_fe, t.dias_credito, t.cobro_contraentrega, t.clasif_origen) == \
            ('C04', 30, False, cp.MAESTRO)
        assert t.clasif_en is not None

    def test_el_pedido_cambiado_en_siesa_no_pisa_lo_anotado(self, db, almacen):
        """Condición cambiada en Siesa después de cargar: la primera lectura
        del pedido queda, y si la FE se leyó, manda la FE."""
        _r, t, _c = _ruta(db, almacen, cond='C02')
        cp.anotar_en_tarea(t, cond_fe='C02')
        cp.anotar_en_tarea(t, cond_pedido='C05')
        assert t.cond_pago == 'C02' and cp.cobro_de_tarea(t)['cobrar'] is True

    def test_sin_dato_no_se_inventa(self, db, almacen):
        _r, t, _c = _ruta(db, almacen)
        assert cp.anotar_desde_historia(t) is False
        assert t.cond_pago is None and t.cobro_contraentrega is None

    def test_al_emitir_la_fe_se_anota_la_condicion_emitida(self, db, almacen):
        from app.services.despacho_parcial_service import DespachoParialService
        _r, t, _c = _ruta(db, almacen)
        t.estado = 'VERIFICADO'
        db.session.commit()
        DespachoParialService._persistir_resultado(
            t, 'RM-1', {'codigo': 0, 'cond_pago_emitida': 'C03', 'cond_pago_pedido': 'C03'})
        assert (t.cond_pago, t.cond_pago_fe, t.dias_credito, t.cobro_contraentrega) == \
            ('C03', 'C03', 8, True)

    def test_en_ensayo_no_se_anota_la_fe(self, db, almacen):
        from app.services.despacho_parcial_service import DespachoParialService
        _r, t, _c = _ruta(db, almacen)
        DespachoParialService._persistir_resultado(
            t, 'RM-1', {'modo_ensayo': True, 'cond_pago_emitida': 'C04'})
        assert t.cond_pago_fe is None

    def test_la_lista_de_paradas_lleva_la_clasificacion(self, db, almacen):
        ruta, t, _c = _ruta(db, almacen, cond='C03')
        d = RutaService.listar_paradas(ruta.id)
        p = d['paradas'][0]
        assert p['cobro_contraentrega'] is True and p['dias_credito'] == 8
        assert p['clasif_origen'] == cp.MAESTRO and p['modo_pago'] != 'CREDITO'
        assert p['cobro_etiqueta']['texto'].startswith('Contado contraentrega · C03')
        assert d['formas_que_no_cobran'] == ['CREDITO', 'EXENTO']
        assert d['version_formulario'] >= cp.VERSION_FORMULARIO_CONTADO
        assert db.session.get(type(t), t.id).cobro_contraentrega is True   # persistido

    def test_la_lista_toma_la_condicion_de_la_fe_sin_llamada_extra(self, db, almacen, monkeypatch):
        from app.services.connekta_gateway import connekta
        ruta, t, _c = _ruta(db, almacen, cond='C02')
        monkeypatch.setattr('app.services.fe_resolver.resolver_fe_o_none', lambda _t: ('FEW', '9'))
        monkeypatch.setattr(type(connekta), 'get_rowids_factura', lambda self, *a, **k: [
            {'f470_vlr_neto': 1000, 'f470_vlr_bruto': 840, 'f470_vlr_imp': 160,
             'f461_id_cond_pago': 'C05', 'f120_referencia': 'X', 'f470_cant_base': 1}])
        p = RutaService.listar_paradas(ruta.id)['paradas'][0]
        assert p['cond_pago_fe'] == 'C05' and p['cobro_contraentrega'] is False
        assert p['cond_pago_difiere'] is True and p['modo_pago'] == 'CREDITO'

    def test_el_recaudo_congela_solo_lo_leido(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, cond='C02')
        _confirmar(ruta, t, c, monto_cobrado=100)
        assert _recaudo(ruta, t).cobro_contraentrega is True
        ruta2, t2, c2 = _ruta(db, almacen)
        _confirmar(ruta2, t2, c2, monto_cobrado=100)
        assert _recaudo(ruta2, t2).cobro_contraentrega is None, 'un supuesto no se congela'


# ═════════════════════════════════════════════════════════════════════════════
# 4 · La guarda del servidor
# ═════════════════════════════════════════════════════════════════════════════

class TestLaGuardaDelServidor:

    @pytest.mark.parametrize('cond', ['C01', 'C02', 'C03', None, 'Z9'])
    @pytest.mark.parametrize('forma', ['CREDITO', 'EXENTO'])
    def test_contado_no_acepta_credito_ni_exento(self, db, almacen, cond, forma):
        ruta, t, c = _ruta(db, almacen, cond=cond)
        with pytest.raises(ValueError, match='contado contraentrega'):
            _confirmar(ruta, t, c, forma_pago=forma, **_V3)
        with pytest.raises(ValueError, match='contado contraentrega'):
            _confirmar(ruta, t, c, estado_entrega='PARCIAL', forma_pago=forma,
                       observaciones='parte', **_V3)

    @pytest.mark.parametrize('forma', ['CREDITO', 'EXENTO'])
    def test_credito_real_si(self, db, almacen, forma):
        ruta, t, c = _ruta(db, almacen, cond='C04')
        _confirmar(ruta, t, c, forma_pago=forma, **_V3)
        assert _recaudo(ruta, t).forma_pago == forma

    def test_entregado_de_contado_con_cero_se_rechaza(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, cond='C02')
        with pytest.raises(ValueError, match='No pagó'):
            _confirmar(ruta, t, c, monto_cobrado=0, **_V3)

    def test_entregado_que_no_alcanza_el_valor_se_rechaza(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, cond='C02', valor=50000)
        with pytest.raises(ValueError, match='no alcanza'):
            _confirmar(ruta, t, c, monto_cobrado=30000, **_V3)

    def test_el_descuento_declarado_cuenta(self, db, almacen):
        """Retención en la puerta: lo cobrado + el descuento = la factura."""
        ruta, t, c = _ruta(db, almacen, cond='C02', valor=50000)
        _confirmar(ruta, t, c, monto_cobrado=48750, motivo_descuento='RETEFUENTE_2.5',
                   monto_descuento=1250, **_V3)
        assert float(_recaudo(ruta, t).monto_cobrado) == 48750

    def test_la_tolerancia_de_redondeo(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, cond='C02', valor=50000)
        _confirmar(ruta, t, c, monto_cobrado=49950, **_V3)

    def test_parcial_de_contado_con_plata_pasa(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, cond='C03', valor=50000)
        _confirmar(ruta, t, c, estado_entrega='PARCIAL', monto_cobrado=20000,
                   observaciones='devolvió dos', **_V3)
        assert _recaudo(ruta, t).estado_entrega == 'PARCIAL'

    @pytest.mark.parametrize('motivo', ['NO_PAGO', 'CLIENTE_CERRADO'])
    def test_no_pago_siempre_disponible(self, db, almacen, motivo):
        """Una parada en la calle no se traba: aunque el select conserve un
        CREDITO de un render anterior."""
        ruta, t, c = _ruta(db, almacen, cond='C02')
        _confirmar(ruta, t, c, estado_entrega='RECHAZADO', motivo_rechazo=motivo,
                   observaciones='no pagó', forma_pago='CREDITO', **_V3)
        assert _recaudo(ruta, t).estado_entrega == EstadoEntrega.RECHAZADO

    def test_no_pago_se_quedo_siempre_disponible(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, cond='C02')
        _confirmar(ruta, t, c, estado_entrega='RECHAZADO', motivo_rechazo='NO_PAGO_SE_QUEDO',
                   observaciones='se la llevó', foto_entrega=_FOTO,
                   geo={'fuente': 'sin_dato', 'motivo': 'sin_senal'}, **_V3)
        assert _recaudo(ruta, t).estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO

    def test_formulario_viejo_c03_credito_entra_como_senal(self, db, almacen):
        """Un ítem de la cola de un PWA viejo no se traba: la regla nueva no
        lo rechaza, y llega a la liquidación como crédito no autorizado."""
        ruta, t, c = _ruta(db, almacen, cond='C03')
        _confirmar(ruta, t, c, forma_pago='CREDITO')
        assert cp.credito_no_autorizado(_recaudo(ruta, t)) is True

    def test_formulario_viejo_conserva_la_regla_de_antes(self, db, almacen, monkeypatch):
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        monkeypatch.setattr(connekta, 'cond_pago_ruta', 'C02', raising=False)
        ruta, t, c = _ruta(db, almacen, cond='C02')
        with pytest.raises(ValueError, match='se cobra en la entrega'):
            _confirmar(ruta, t, c, forma_pago='CREDITO')

    def test_reconfirmar_no_escapa_de_la_guarda(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, cond='C02', valor=1000)
        _confirmar(ruta, t, c, monto_cobrado=1000, **_V3)
        with pytest.raises(ValueError, match='contado contraentrega'):
            _confirmar(ruta, t, c, forma_pago='CREDITO', **_V3)
        assert _recaudo(ruta, t).forma_pago == 'EFECTIVO'

    def test_el_admin_confirmando_a_mano_tambien(self, db, almacen, usuario_admin):
        ruta, t, _c = _ruta(db, almacen, cond='C02')
        with pytest.raises(ValueError, match='contado contraentrega'):
            RutaService.confirmar_parada(ruta.id, t.id, usuario_admin.id, {
                'estado_entrega': 'ENTREGADO', 'forma_pago': 'CREDITO', **_V3})

    def test_traslado_no_se_cobra(self, db, almacen):
        ruta, t, c = _ruta(db, almacen, tipo_documento='TRASLADO')
        assert cp.cobro_de_tarea(t)['origen'] == cp.NO_APLICA
        _confirmar(ruta, t, c, forma_pago='CREDITO', **_V3)

    def test_offline_no_va_a_siesa(self, db, almacen, monkeypatch):
        """La confirmación decide con lo anotado: si Siesa no responde, igual."""
        from app.services.connekta_gateway import connekta

        def _caido(self, *a, **k):
            raise RuntimeError('sin señal')
        for m in ('get_pedido_cabecera', 'get_rowids_factura', '_get'):
            monkeypatch.setattr(type(connekta), m, _caido)
        ruta, t, c = _ruta(db, almacen, cond='C03', valor=1000)
        with pytest.raises(ValueError, match='contado contraentrega'):
            _confirmar(ruta, t, c, forma_pago='CREDITO', **_V3)
        _confirmar(ruta, t, c, monto_cobrado=1000, **_V3)

    def test_mismo_cliente_dos_condiciones(self, db, almacen):
        """Dos facturas del mismo cliente en la misma ruta: C02 se cobra, C04 no."""
        ruta, t1, c = _ruta(db, almacen, cond='C02', cliente='Doña Rosa')
        _r, t2, _c = _ruta(db, almacen, cond='C04', cliente='Doña Rosa', ruta=ruta, conductor=c)
        paradas = {p['tarea_id']: p for p in RutaService.listar_paradas(ruta.id)['paradas']}
        assert paradas[t1.id]['cobro_contraentrega'] is True
        assert paradas[t2.id]['cobro_contraentrega'] is False
        with pytest.raises(ValueError):
            _confirmar(ruta, t1, c, forma_pago='CREDITO', **_V3)
        _confirmar(ruta, t2, c, forma_pago='CREDITO', **_V3)


# ═════════════════════════════════════════════════════════════════════════════
# 5 · La liquidación
# ═════════════════════════════════════════════════════════════════════════════

def _recaudo_directo(db, almacen, cond, forma, monto, estado='ENTREGADO', items=None):
    """Un recaudo como lo dejaría un formulario viejo o un POST a mano."""
    ruta, t, c = _ruta(db, almacen, cond=cond, valor=100000, estado='ENTREGADA')
    r = RecaudoEntrega(ruta_id=ruta.id, tarea_id=t.id, estado_entrega=estado,
                       forma_pago=forma, monto_cobrado=monto, items_entregados=items,
                       confirmado_por=c.usuario_id, fecha_confirmacion=datetime.utcnow())
    db.session.add(r); db.session.commit()
    return ruta, t, r


class TestLaLiquidacion:

    @pytest.mark.parametrize('forma,monto', [('CREDITO', 0), ('EXENTO', 0), ('EFECTIVO', 0)])
    def test_contado_sin_plata_es_error_nunca_credito(self, db, almacen, forma, monto):
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C02', forma, monto)
        res = _procesar(r, db)
        assert res['credito'] == 0 and res['rc'] == 0
        assert res['credito_no_autorizado'] == 1
        assert res['errores'] and res['errores'][0].startswith('credito_no_autorizado')

    def test_credito_real_va_al_contador(self, db, almacen):
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C04', 'CREDITO', 0)
        res = _procesar(r, db)
        assert res['credito'] == 1 and not res['errores']

    def test_parcial_contado_sin_plata_igual_arma_la_devolucion(self, db, almacen, producto):
        from app.models.devolucion_cliente import DevolucionCliente
        items = [{'codigo': producto.codigo, 'cantidad_pedida': 5, 'cantidad_entregada': 3,
                  'cantidad_devuelta': 2}]
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0, 'PARCIAL', items)
        with patch('app.services.connekta_gateway.connekta.get_rowids_factura', return_value=[{
                'f120_referencia': producto.codigo_siesa, 'f470_cant_base': 5,
                'f470_vlr_neto': 100000, 'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
                'f470_rowid': '1'}]):
            res = _procesar(r, db)
        assert res['credito_no_autorizado'] == 1 and res['rc'] == 0
        assert DevolucionCliente.query.filter_by(recaudo_entrega_id=r.id).first() is not None

    def test_la_ruta_no_se_liquida(self, db, almacen):
        ruta, _t, _r = _recaudo_directo(db, almacen, 'C03', 'CREDITO', 0)
        with pytest.raises(ValueError, match='credito_no_autorizado'):
            RutaService.liquidar_ruta(ruta.id)

    def test_liquidacion_masiva_declara_con_codigo(self, db, almacen):
        from app.models.conductor import Conductor
        from app.services.liquidacion_service import LiquidacionService
        ruta, t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0)
        # Segunda parada, de crédito real, en la misma ruta.
        _r2, t2, _c2 = _ruta(db, almacen, cond='C04', ruta=ruta,
                             conductor=db.session.get(Conductor, ruta.conductor_id))
        db.session.add(RecaudoEntrega(ruta_id=ruta.id, tarea_id=t2.id, estado_entrega='ENTREGADO',
                                      forma_pago='CREDITO', monto_cobrado=0,
                                      fecha_confirmacion=datetime.utcnow()))
        db.session.commit()
        with patch('app.services.liquidacion_service._obtener_tercero',
                   return_value=('900', '001')):
            res = LiquidacionService.liquidar_ruta_siesa(ruta.id)
        assert res['credito_no_autorizado'] == 1 and res['credito_omitidos'] == 1
        assert [e['codigo'] for e in res['errores']] == ['credito_no_autorizado']

    def test_registrar_cobro_explica(self, db, almacen):
        from app.services.liquidacion_service import LiquidacionService
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C02', 'EXENTO', 0)
        with pytest.raises(ValueError, match='credito_no_autorizado'):
            LiquidacionService.registrar_cobro_recaudo(r.id)

    def test_preview_no_propone_recibo_y_lo_marca(self, db, almacen):
        from app.services.liquidacion_service import LiquidacionService
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0)
        p = LiquidacionService.preview_acciones_recaudo(r.id)
        assert p['credito_no_autorizado'] is True and 'RECIBO_CAJA' not in p['acciones_pendientes']

    def test_contado_con_plata_sigue_su_camino(self, db, almacen):
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C02', 'EFECTIVO', 100000)
        res = _procesar(r, db)
        assert res['rc'] == 1 and not res['errores']


class TestAutorizarComoCredito:

    def test_razon_obligatoria(self, db, almacen, usuario_admin):
        from app.services.liquidacion_service import LiquidacionService
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0)
        with pytest.raises(ValueError):
            LiquidacionService.autorizar_credito(r.id, usuario_admin.id, '   ')

    def test_autoriza_deja_bitacora_y_destraba(self, db, almacen, usuario_admin):
        from app.models.bitacora import BitacoraAccion
        from app.services.liquidacion_service import LiquidacionService
        ruta, _t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0)
        LiquidacionService.autorizar_credito(r.id, usuario_admin.id, 'cliente institucional, lo aprobó gerencia')
        r = db.session.get(RecaudoEntrega, r.id)
        assert r.credito_autorizado_por == usuario_admin.id and r.credito_autorizado_en
        fila = BitacoraAccion.query.filter_by(entidad='RecaudoEntrega', entidad_id=r.id).one()
        assert fila.accion == 'EDITAR' and 'gerencia' in fila.motivo
        assert cp.credito_no_autorizado(r) is False
        assert _procesar(r, db)['credito'] == 1
        RutaService.liquidar_ruta(ruta.id)

    def test_no_autoriza_lo_que_no_hace_falta(self, db, almacen, usuario_admin):
        from app.services.liquidacion_service import LiquidacionService
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C04', 'CREDITO', 0)
        with pytest.raises(ValueError, match='nada que autorizar'):
            LiquidacionService.autorizar_credito(r.id, usuario_admin.id, 'por si acaso')

    def test_endpoint_solo_admin(self, app, client, db, almacen, jwt_token, jwt_token_admin):
        ruta, _t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0)
        url = f'/api/rutas/{ruta.id}/recaudos/{r.id}/autorizar-credito'
        assert client.post(url, json={'razon': 'x'},
                           headers={'Authorization': f'Bearer {jwt_token}'}).status_code == 403
        assert client.post(url, json={},
                           headers={'Authorization': f'Bearer {jwt_token_admin}'}).status_code == 400
        res = client.post(url, json={'razon': 'lo aprobó gerencia'},
                          headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert res.status_code == 200 and res.get_json()['recaudo']['credito_autorizado_por']


# ═════════════════════════════════════════════════════════════════════════════
# 6 · El despacho informa
# ═════════════════════════════════════════════════════════════════════════════

class TestElDespachoInforma:

    def test_cerrar_ruta_informa_supuestas_y_escribe_el_snapshot(self, db, almacen, monkeypatch):
        from app.models.packing import TareaPacking
        monkeypatch.setattr(RutaService, '_reconocer_advertencias_flota',
                            staticmethod(lambda *a, **k: []))
        ruta, t, _c = _ruta(db, almacen, estado='EN_CARGUE')
        from app.models.bulto import Bulto
        for b in Bulto.query.filter_by(ruta_despacho_id=ruta.id):
            b.estado = 'CARGADO'
        db.session.commit()
        r = RutaService.cerrar_ruta(ruta.id)
        claves = [x['clave'] for x in r.informe_cobro]
        assert claves == ['cobro_supuesto']
        assert db.session.get(TareaPacking, t.id).clasif_origen == cp.SUPUESTO_AUSENTE

    def test_los_traslados_no_entran_al_informe(self, db, almacen):
        ruta, _t, _c = _ruta(db, almacen, tipo_documento='TRASLADO')
        assert RutaService._informe_de_cobro(ruta, consultar_cartera=False) == []

    def test_contado_documental_se_informa(self, db, almacen, monkeypatch):
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'cond_pago_ventas', 'C01', raising=False)
        ruta, _t, _c = _ruta(db, almacen, cond='C01')
        assert [x['clave'] for x in RutaService._informe_de_cobro(ruta, False)] == ['fe_contado']


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Los reportes leen la política
# ═════════════════════════════════════════════════════════════════════════════

class TestLosReportes:

    def test_desglose(self, client, db, almacen, jwt_token_admin):
        _recaudo_directo(db, almacen, 'C03', 'CREDITO', 0)
        _recaudo_directo(db, almacen, 'C04', 'CREDITO', 0)
        d = client.get('/api/rutas/liquidacion/desglose',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'}).get_json()
        assert d['por_dias_credito']['conteo'] == {'C03 (8 días) · contado': 1,
                                                    'C04 (30 días) · crédito real': 1}
        assert d['credito_no_autorizado']['total'] == 1
        assert d['credito_no_autorizado']['valor_sin_cobrar'] == 100000.0

    def test_fuga(self, db, almacen):
        from app.services import analitica_fugas as af
        from app.utils.fecha import dia_operativo
        _recaudo_directo(db, almacen, 'C02', 'EXENTO', 0)
        _recaudo_directo(db, almacen, 'C04', 'CREDITO', 0)
        hoy = dia_operativo()
        res = af._credito_no_autorizado(hoy, hoy, af._Ctx(None))
        assert len(res.casos) == 1 and res.casos[0].pesos == 100000.0
        assert res.casos[0].motivo == 'Registrado EXENTO'

    def test_reconciliacion_cuenta_el_supuesto(self, db, almacen):
        from app.services.reconciliacion_ruta import reconciliar
        ruta, _t, _r = _recaudo_directo(db, almacen, None, 'EFECTIVO', 100000)
        rep = reconciliar(ruta.id)
        assert rep['columnas']['debian_cobrarse']['n'] == 1 and rep['paradas_sin_condicion'] == 1

    def test_reconciliacion_saca_el_autorizado(self, db, almacen, usuario_admin):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.reconciliacion_ruta import reconciliar
        ruta, _t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0)
        assert reconciliar(ruta.id)['columnas']['debian_cobrarse']['n'] == 1
        LiquidacionService.autorizar_credito(r.id, usuario_admin.id, 'gerencia')
        assert reconciliar(ruta.id)['columnas']['debian_cobrarse']['n'] == 0


class TestLaAuditoriaLoVe:

    def test_vta62_ve_un_credito_no_autorizado(self, db, almacen):
        """Detector ciego de VTA-62: se rompe a propósito y se exige que lo vea."""
        from app.services import auditoria
        _recaudo_directo(db, almacen, 'C03', 'CREDITO', 0)
        r = auditoria.auditar('venta')
        v = next(x for x in r['resultados'] if x['codigo'] == 'VTA-62')
        assert v['total'] == 1

    def test_vta62_no_ve_el_credito_real_ni_el_autorizado(self, db, almacen, usuario_admin):
        from app.services import auditoria
        from app.services.liquidacion_service import LiquidacionService
        _recaudo_directo(db, almacen, 'C04', 'CREDITO', 0)
        _ruta_, _t, r = _recaudo_directo(db, almacen, 'C02', 'CREDITO', 0)
        LiquidacionService.autorizar_credito(r.id, usuario_admin.id, 'gerencia')
        res = auditoria.auditar('venta')
        assert next(x for x in res['resultados'] if x['codigo'] == 'VTA-62')['total'] == 0


# ═════════════════════════════════════════════════════════════════════════════
# 8 · Trinquetes de la CLASE
# ═════════════════════════════════════════════════════════════════════════════

_NO_COBRAN = {'CREDITO', 'EXENTO'}
_POLITICA = 'app/services/cond_pago.py'


def _menciona_forma(n) -> bool:
    for x in ast.walk(n):
        nombre = x.id if isinstance(x, ast.Name) else x.attr if isinstance(x, ast.Attribute) else None
        if nombre and ('forma' in nombre.lower() or nombre == 'fp'):
            return True
    return False


def _es_literal_no_cobra(n) -> bool:
    if isinstance(n, ast.Constant) and n.value in _NO_COBRAN:
        return True
    if isinstance(n, ast.Attribute) and n.attr in _NO_COBRAN:
        return True
    if isinstance(n, (ast.Tuple, ast.List, ast.Set)):
        return any(_es_literal_no_cobra(e) for e in n.elts)
    return False


def comparaciones_de_forma(arbol) -> list:
    """`(funcion, linea)` de toda comparación de una forma de pago contra
    CREDITO/EXENTO: `==`, `!=`, `in`, `not in`, con literal o `FormaPago.X`."""
    salida = []

    def visitar(n, fn):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = n.name
        if isinstance(n, ast.Compare):
            lados = [n.left] + n.comparators
            if any(_es_literal_no_cobra(x) for x in lados) and \
                    any(_menciona_forma(x) for x in lados if not _es_literal_no_cobra(x)):
                salida.append((fn, n.lineno))
        for h in ast.iter_child_nodes(n):
            visitar(h, fn)
    visitar(arbol, '<modulo>')
    return salida


def _archivos():
    for base in ('app', 'flota'):
        for f in sorted((_RAIZ / base).rglob('*.py')):
            rel = f.relative_to(_RAIZ).as_posix()
            if rel != _POLITICA:
                yield rel, ast.parse(f.read_text(encoding='utf-8'))


#: Comparaciones `forma_pago == 'CREDITO'` que NO deciden cobro ni documentos.
#: Solo encoge. Cada una dice por qué.
FORMA_FUERA_DE_LA_POLITICA = {
    ('app/routes/rutas.py', 'liquidacion_desglose'):
        'reporte: lista y cuenta las paradas marcadas CRÉDITO (cada una trae '
        'además `credito_no_autorizado` de la política)',
    ('app/routes/rutas.py', 'liquidacion_dashboard'):
        'reporte: suma el monto declarado CRÉDITO en su propio renglón del tablero',
}


class TestNingunaFormaDePagoDecideCobroFueraDeLaPolitica:

    def test_ninguna_nueva(self):
        encontrados = {(rel, fn) for rel, arbol in _archivos()
                       for fn, _l in comparaciones_de_forma(arbol)}
        nuevos = encontrados - set(FORMA_FUERA_DE_LA_POLITICA)
        assert not nuevos, (
            f'`forma_pago` comparada contra CREDITO/EXENTO fuera de cond_pago: {nuevos}. '
            'Usar cond_pago.trato_de_cobro / credito_no_autorizado / forma_no_cobra.')

    def test_el_inventario_solo_encoge(self):
        encontrados = {(rel, fn) for rel, arbol in _archivos()
                       for fn, _l in comparaciones_de_forma(arbol)}
        assert not (set(FORMA_FUERA_DE_LA_POLITICA) - encontrados), 'borrar las que ya no existen'
        assert len(FORMA_FUERA_DE_LA_POLITICA) <= 2

    def test_cada_una_dice_por_que(self):
        assert all(len(v) > 30 for v in FORMA_FUERA_DE_LA_POLITICA.values())

    def test_piso(self):
        """Si el escáner se rompe devuelve cero y se lee como «todo bien»."""
        total = sum(len(comparaciones_de_forma(a)) for _r, a in _archivos())
        assert total >= 2

    @pytest.mark.parametrize('codigo', [
        "def f(r):\n    return r.forma_pago == 'CREDITO'",
        "def f(fp):\n    return fp in ('CREDITO', 'EXENTO')",
        "def f(x):\n    return FormaPago.EXENTO != x.forma_pago",
        "def f(r):\n    return (r.forma_pago or '').upper() not in ['EXENTO']",
    ])
    def test_ve_las_cuatro_escrituras(self, codigo):
        assert comparaciones_de_forma(ast.parse(codigo)) == [('f', 2)]

    @pytest.mark.parametrize('codigo', [
        "def f(m):\n    return m in ('CREDITO', 'DINAMICO', 'LIBRE')",   # modo, no forma
        "def f(r):\n    '''forma_pago == 'CREDITO' en un docstring'''\n    return 1",
        "def f(r):\n    # r.forma_pago == 'CREDITO'\n    return 1",
    ])
    def test_no_marca_lo_sano(self, codigo):
        assert comparaciones_de_forma(ast.parse(codigo)) == []

    def test_la_politica_si_puede(self):
        arbol = ast.parse((_RAIZ / _POLITICA).read_text(encoding='utf-8'))
        assert comparaciones_de_forma(arbol), 'la política misma decide — debe verse'


_FUNCIONES_DE_COBRO = {'cobro_contraentrega', 'cobro_de_tarea', 'cobro_de_recaudo',
                       'anotar_en_tarea', 'trato_de_cobro', 'credito_no_autorizado',
                       'cobra_en_la_puerta'}


def lecturas_crudas(arbol) -> list:
    """Funciones que leen `.cond_pago`/`.cond_pago_fe` (atributo o getattr) y
    no llaman a ninguna función de la política de cobro."""
    salida = []
    for fn in ast.walk(arbol):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        lee = any(isinstance(n, ast.Attribute) and n.attr in ('cond_pago', 'cond_pago_fe')
                  and isinstance(n.ctx, ast.Load) for n in ast.walk(fn))
        lee = lee or any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                         and n.func.id == 'getattr' and len(n.args) >= 2
                         and isinstance(n.args[1], ast.Constant)
                         and n.args[1].value in ('cond_pago', 'cond_pago_fe')
                         for n in ast.walk(fn))
        if not lee:
            continue
        llama = {(n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, 'id', None))
                 for n in ast.walk(fn) if isinstance(n, ast.Call)}
        if not (llama & _FUNCIONES_DE_COBRO):
            salida.append(fn.name)
    return salida


#: Lecturas crudas de la condición que NO deciden cobro. Solo encoge.
COND_CRUDA_SIN_POLITICA = {
    ('app/models/packing.py', 'to_dict'):
        'serialización: publica los códigos tal como están anotados',
    ('app/services/auditoria/venta.py', 'ninguna_parada_de_ruta_declara_contado'):
        'VTA-61: contado DOCUMENTAL (¿Siesa aprueba la FE?), otra pregunta — '
        'usa `clasificar`, no la política de cobro',
}


class TestNingunaCondicionCrudaDecideCobro:

    def test_ninguna_nueva(self):
        encontrados = {(rel, fn) for rel, arbol in _archivos() for fn in lecturas_crudas(arbol)}
        nuevos = encontrados - set(COND_CRUDA_SIN_POLITICA)
        assert not nuevos, (f'leen cond_pago sin pasar por la política de cobro: {nuevos}')

    def test_el_inventario_solo_encoge(self):
        encontrados = {(rel, fn) for rel, arbol in _archivos() for fn in lecturas_crudas(arbol)}
        assert not (set(COND_CRUDA_SIN_POLITICA) - encontrados)
        assert len(COND_CRUDA_SIN_POLITICA) <= 2
        assert all(len(v) > 30 for v in COND_CRUDA_SIN_POLITICA.values())

    def test_piso(self):
        """Hoy leen la condición al menos cinco funciones (to_dict, VTA-61,
        desglose, listar_paradas, _valor_y_cond_pago). Un escáner roto daría
        cero y se leería como «nadie la lee»."""
        total = 0
        for _rel, arbol in _archivos():
            for fn in ast.walk(arbol):
                if isinstance(fn, ast.FunctionDef) and any(
                        isinstance(n, ast.Attribute) and n.attr in ('cond_pago', 'cond_pago_fe')
                        for n in ast.walk(fn)):
                    total += 1
        assert total >= 5

    def test_ve_una_lectura_cruda(self):
        codigo = "def f(t):\n    return t.cond_pago in ('C01', 'C02')\n"
        assert lecturas_crudas(ast.parse(codigo)) == ['f']
        codigo = "def f(t):\n    return getattr(t, 'cond_pago', None) == 'C01'\n"
        assert lecturas_crudas(ast.parse(codigo)) == ['f']

    def test_no_marca_la_que_pasa_por_la_politica(self):
        codigo = "def f(t):\n    c = t.cond_pago\n    return cp.cobro_de_tarea(t)['cobrar']\n"
        assert lecturas_crudas(ast.parse(codigo)) == []


# ═════════════════════════════════════════════════════════════════════════════
# 3 · La pantalla (Node, util.js real)
# ═════════════════════════════════════════════════════════════════════════════

def _extraer(texto, cabeza, fin='\n}\n'):
    i = texto.index(cabeza)
    j = texto.index(fin, i)
    return texto[i:j + len(fin)]


def _correr_js(tmp_path, cuerpo):
    js = (_RAIZ / 'app' / 'static' / 'pwa' / 'rutas.js').read_text(encoding='utf-8')
    util = (_RAIZ / 'app' / 'static' / 'pwa' / 'util.js').read_text(encoding='utf-8')
    prog = '\n'.join([
        util,
        _extraer(js, 'const _FORMAS_PAGO_COBRO = [', '];\n'),
        "let _COND_FORMAS_NO_COBRAN = ['CREDITO', 'EXENTO'];",
        _extraer(js, 'function _condEsCreditoReal('),
        _extraer(js, 'function _condFormasPago('),
        _extraer(js, 'function _condAvisoCobro('),
        cuerpo,
    ])
    f = tmp_path / 'cc.js'
    f.write_text(prog, encoding='utf-8')
    return json.loads(subprocess.run(['node', str(f)], capture_output=True, text=True,
                                     check=True).stdout)


class TestLaPantallaDelConductor:

    def test_el_select_de_contado_no_ofrece_credito_ni_exento(self, tmp_path):
        out = _correr_js(tmp_path, """
const v = (p) => _condFormasPago(p).map(f => f.v);
console.log(JSON.stringify({
  contado: v({cobro_contraentrega: true, modo_pago: 'DINAMICO'}),
  supuesto: v({cobro_contraentrega: true, clasif_origen: 'SUPUESTO_AUSENTE', modo_pago: 'LIBRE'}),
  credito: v({cobro_contraentrega: false, modo_pago: 'CREDITO'}),
  cache_vieja_credito: v({modo_pago: 'CREDITO'}),
  cache_vieja_libre: v({modo_pago: 'LIBRE'}),
}));
""")
        for k in ('contado', 'supuesto', 'cache_vieja_libre'):
            assert 'CREDITO' not in out[k] and 'EXENTO' not in out[k], k
            assert 'EFECTIVO' in out[k]
        assert 'CREDITO' in out['credito'] and 'CREDITO' in out['cache_vieja_credito']

    def test_el_aviso_viene_del_servidor_y_se_escapa(self, tmp_path):
        out = _correr_js(tmp_path, """
const a = _condAvisoCobro({cobro_etiqueta: {texto: '<img src=x onerror=1>', tono: 'ok'}});
const b = _condAvisoCobro({cobro_contraentrega: true});
console.log(JSON.stringify({a, b, pintado: esc(a.texto)}));
""")
        assert out['a']['tono'] == 'ok'
        assert '<img' not in out['pintado']
        assert out['b']['tono'] == 'warn'

    def test_el_render_usa_el_filtro_y_esc(self):
        js = (_RAIZ / 'app' / 'static' / 'pwa' / 'rutas.js').read_text(encoding='utf-8')
        cuerpo = _extraer(js, 'function _condRenderFormParada(')
        assert '_condFormasPago(p).map(' in cuerpo
        assert '_FORMAS_PAGO_COBRO.map(' not in cuerpo
        assert cuerpo.count('${esc(avisoCobro.texto)}') == 2

    def test_el_formulario_declara_la_version_3(self):
        js = (_RAIZ / 'app' / 'static' / 'pwa' / 'rutas.js').read_text(encoding='utf-8')
        assert f'const COND_VERSION_FORMULARIO = {cp.VERSION_FORMULARIO_CONTADO};' in js

    def test_el_muelle_dice_el_informe_de_cobro(self, tmp_path):
        js = (_RAIZ / 'app' / 'static' / 'pwa' / 'rutas.js').read_text(encoding='utf-8')
        prog = _extraer(js, 'function _rutaAvisoCobro(') + """
console.log(JSON.stringify({
  nada: _rutaAvisoCobro({}),
  algo: _rutaAvisoCobro({informe_cobro: [{clave: 'cobro_supuesto'}, {clave: 'cobro_supuesto'},
                                         {clave: 'fe_saldada'}]}),
}));
"""
        f = tmp_path / 'aviso.js'
        f.write_text(prog, encoding='utf-8')
        out = json.loads(subprocess.run(['node', str(f)], capture_output=True, text=True,
                                        check=True).stdout)
        assert out['nada'] == ''
        assert '2 sin condición' in out['algo'] and '1 ya pagada' in out['algo']
        assert js.count('_rutaAvisoCobro(d)') == 3   # la definición y los dos despachos
