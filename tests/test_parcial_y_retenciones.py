"""
Parciales y retenciones (L4 de la auditoría de liquidación, 2026-09-27).

## P1-5 · Una PARCIAL pagada de menos era crédito que nadie autorizó

`trato_de_cobro` daba CONTADO a toda PARCIAL con monto > 0: el cliente se
quedaba con $70 k, pagaba $40 k, el RC salía por $40 k y los $30 k quedaban en
cartera sin que nadie lo decidiera ni lo viera. **La clase:** *una parada se
juzga contra lo que declaró el conductor y no contra lo que el cliente se
quedó*. Ahora, con lo que se quedó valorizable (`politica_cobro.esperado_en_caja`,
la vara de la reconciliación — una política), la diferencia por encima del
residuo es CREDITO_NO_AUTORIZADO hasta que el líder de cartera (o el admin) la
autorice o se corrija el monto.

## P1-4 · El RC de una PARCIAL esperaba la nota crédito sin límite visible

No se quita la espera (NC → RC, Regla 7: quitarla exige probarlo en Siesa QA).
Se hace visible: `RC_ESPERA_NC_HORAS` (24 h por defecto; eran 48 fijas), con
quién la destraba, en el mensaje del envío y en el resumen diario.

## P2-4 · La puerta aceptaba lo que un cliente no puede descontar

Las autorretenciones (obligación de la empresa) y la retención bancaria
(la aplica el banco) no reducen lo que paga el cliente: `aplica_en_puerta`
en el catálogo; ni el conductor las ve, ni la oficina las confirma, ni sale
una NI contra la factura. Base mínima configurable como **aviso**
(`RETENCION_BASE_MINIMA_PESOS`, sin default).

Trinquetes AST con inventario que solo encoge, meta-tests y pisos.
"""
import ast
import json
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tests.test_devolucion_vuelve import _dev, _fila, _gw, _mundo, _parcial, _usuario

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'


# ═════════════════════════════════════════════════════════════════════════════
# Mundo: una PARCIAL cuyo «lo que se quedó» se puede valorizar
# ═════════════════════════════════════════════════════════════════════════════

def _parcial_valorizada(db, almacen, producto, cobrado, cond=None, datos=None):
    """Factura $100.000 (10 × $10.000); vuelven 3 → el cliente se quedó con
    $70.000. La devolución amarrada a la factura (lo que hace recepción)."""
    from app.services import devolucion_ruta as dr
    m = _mundo(db, almacen, producto)
    m.tarea.valor_factura = 100000
    if cond:
        m.tarea.cond_pago = m.tarea.cond_pago_fe = cond
    db.session.commit()
    _parcial(m, producto, entregado=7, datos={'monto_cobrado': cobrado, **(datos or {})})
    rec, dev = _dev(m)
    dr.vincular_a_factura(dev, gateway=_gw([_fila(producto)]))
    db.session.commit()
    return m, rec


class TestLaParcialPagadaDeMenos:

    def test_pagar_menos_de_lo_que_se_quedo_es_credito_no_autorizado(self, db, almacen, producto):
        from app.services import cond_pago as cp
        from app.services import politica_cobro as pc
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        assert pc.faltante_de_la_parcial(rec) == {
            'esperado': 70000.0, 'cobrado': 40000.0, 'diferencia': 30000.0, 'tope': 100.0}
        assert cp.trato_de_cobro(rec) == cp.TRATO_NO_AUTORIZADO
        assert cp.credito_no_autorizado(rec) is True

    def test_pagar_lo_que_se_quedo_es_contado(self, db, almacen, producto):
        from app.services import cond_pago as cp
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=70000)
        assert cp.trato_de_cobro(rec) == cp.TRATO_CONTADO

    def test_el_residuo_de_redondeo_no_es_credito(self, db, almacen, producto):
        from app.services import cond_pago as cp
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=69950)
        assert cp.trato_de_cobro(rec) == cp.TRATO_CONTADO

    def test_sin_poder_valorizar_no_se_inventa_un_faltante(self, db, almacen, producto):
        """Regla 0: sin la factura anotada (o lo devuelto sin precio) no hay
        vara contra la cual decir «faltó»: sigue CONTADO por lo cobrado."""
        from app.services import cond_pago as cp
        from app.services import politica_cobro as pc
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        rec.tarea.valor_factura = None
        db.session.commit()
        assert pc.faltante_de_la_parcial(rec) is None
        assert cp.trato_de_cobro(rec) == cp.TRATO_CONTADO
        # Ni siquiera con un descuento declarado: sin la factura, «lo que se
        # quedó» es lo que el conductor dijo, y eso no mide ningún faltante.
        _m2, rec2 = _parcial_valorizada(db, almacen, producto, cobrado=40000, datos={
            'motivo_descuento': 'RETEFUENTE_2.5', 'monto_descuento': 2000})
        rec2.tarea.valor_factura = None
        rec2.retencion_confirmada = False
        db.session.commit()
        assert pc.faltante_de_la_parcial(rec2) is None

    def test_el_credito_real_no_se_marca(self, db, almacen, producto):
        """Un cliente de crédito real (C04, 30 días) que paga una parte: el
        resto es su crédito, no uno sin autorizar."""
        from app.services import cond_pago as cp
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000, cond='C04')
        assert cp.trato_de_cobro(rec) == cp.TRATO_CONTADO

    def test_la_retencion_declarada_explica_la_diferencia(self, db, almacen, producto):
        from app.services import cond_pago as cp
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=68000, datos={
            'motivo_descuento': 'RETEFUENTE_2.5', 'monto_descuento': 2000})
        assert cp.trato_de_cobro(rec) == cp.TRATO_CONTADO

    def test_la_retencion_rechazada_no_la_explica(self, db, almacen, producto):
        from app.services import cond_pago as cp
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=68000, datos={
            'motivo_descuento': 'RETEFUENTE_2.5', 'monto_descuento': 2000})
        rec.retencion_confirmada = False
        db.session.commit()
        assert cp.trato_de_cobro(rec) == cp.TRATO_NO_AUTORIZADO

    def test_una_autorretencion_tampoco_la_explica(self, db, almacen, producto):
        from app.services import cond_pago as cp
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=68000, datos={
            'motivo_descuento': 'AUTORRETENCION_ICA_NEIVA_3X1000', 'monto_descuento': 2000})
        assert cp.trato_de_cobro(rec) == cp.TRATO_NO_AUTORIZADO

    def test_la_liquidacion_no_emite_el_recibo_y_lo_dice(self, db, almacen, producto):
        from app.models.siesa_job import SiesaJob
        from app.services.liquidacion_service import _procesar_recaudo
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        r = _procesar_recaudo(rec, 'notas')
        db.session.commit()
        assert r['credito_no_autorizado'] == 1 and r['rc'] == 0
        assert SiesaJob.query.filter_by(tipo='RECIBO_CAJA', referencia_id=rec.id).count() == 0
        [msg] = r['errores']
        assert 'faltan $30,000' in msg and 'líder de cartera' in msg

    def test_autorizada_sale_el_recibo_por_lo_cobrado(self, db, almacen, producto):
        from app.models.siesa_job import SiesaJob
        from app.services import cond_pago as cp
        from app.services.liquidacion_service import LiquidacionService, _procesar_recaudo
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        lider = _usuario(db, 'lider_cartera')
        LiquidacionService.autorizar_credito(rec.id, lider.id, 'cliente de confianza, paga el lunes')
        assert cp.trato_de_cobro(rec) == cp.TRATO_CONTADO
        r = _procesar_recaudo(rec, 'notas')
        db.session.commit()
        assert r['credito_no_autorizado'] == 0 and r['rc'] == 1
        [job] = SiesaJob.query.filter_by(tipo='RECIBO_CAJA', referencia_id=rec.id).all()
        assert job.get_payload()['monto'] == 40000

    def test_el_detalle_trae_las_cifras(self, db, almacen, producto):
        from app.services.liquidacion_service import LiquidacionService
        m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        d = LiquidacionService.preparar_detalle_ruta(m.ruta.id)
        [rd] = [x for x in d['recaudos'] if x['id'] == rec.id]
        assert rd['credito_no_autorizado'] is True
        assert rd['parcial_pagada_de_menos']['diferencia'] == 30000.0

    def test_la_pantalla_dice_cuanto_falta(self, db, almacen, producto, tmp_path):
        from tests.test_sin_codigos_en_pantalla import _node, visible
        from app.services.liquidacion_service import LiquidacionService
        m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        d = LiquidacionService.preparar_detalle_ruta(m.ruta.id)
        [rd] = [x for x in d['recaudos'] if x['id'] == rec.id]
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqDetalleRuta = { permisos: { autorizar_credito: true } };
            return { html: _liqBloqueCreditoNoAutorizado(7, REC) };
        """, globales={'REC': json.loads(json.dumps(rd, default=str))})
        txt = visible(out['html'])
        assert 'PAGÓ DE MENOS' in txt and '30.000' in txt and 'Autorizar como crédito' in txt


# ═════════════════════════════════════════════════════════════════════════════
# P1-4 · El recibo que espera su nota crédito, con tope visible
# ═════════════════════════════════════════════════════════════════════════════

def _rc_esperando(db, almacen, producto, horas):
    from app.models.siesa_job import SiesaJob
    m = _mundo(db, almacen, producto)
    _parcial(m, producto, entregado=7)
    rec, dev = _dev(m)
    job = SiesaJob.encolar('RECIBO_CAJA', {'recaudo_id': rec.id, 'depende_de_nc': True,
                                           'monto': 1000, 'tipo_docto_fe': 'FEW',
                                           'consec_fe': '500'},
                           referencia_tipo='RecaudoEntrega', referencia_id=rec.id)
    job.fecha_creacion = datetime.utcnow() - timedelta(hours=horas)
    db.session.commit()
    return m, rec, dev, job


class TestElReciboQueEsperaSuNota:

    def test_el_tope_por_defecto_es_24_h_y_se_configura(self, monkeypatch):
        from app.services import devolucion_ruta as dr
        monkeypatch.delenv('RC_ESPERA_NC_HORAS', raising=False)
        assert dr.horas_rc_esperando_nc() == 24
        monkeypatch.setenv('RC_ESPERA_NC_HORAS', '6')
        assert dr.horas_rc_esperando_nc() == 6
        monkeypatch.setenv('RC_ESPERA_NC_HORAS', 'mañana')
        assert dr.horas_rc_esperando_nc() == 24

    def test_antes_del_tope_no_avisa_despues_si_con_responsable(self, db, almacen, producto,
                                                                monkeypatch):
        from app.services import devolucion_ruta as dr
        monkeypatch.delenv('RC_ESPERA_NC_HORAS', raising=False)
        _rc_esperando(db, almacen, producto, horas=20)
        assert dr.avisos()['rc_esperando_nc_48h'] == []
        m, rec, dev, job = _rc_esperando(db, almacen, producto, horas=30)
        [a] = dr.avisos()['rc_esperando_nc_48h']
        assert a['job_id'] == job.id and a['devolucion'] == dev.codigo and a['vencido']
        [linea] = [x for x in dr.lineas_de_aviso() if 'nota crédito hace más' in x]
        assert '24 h' in linea and m.tarea.numero_pedido_siesa in linea
        assert 'Responde: bodega' in linea and 'Llegó el camión' in linea

    def test_el_envio_en_espera_dice_cuanto_lleva_y_quien_lo_destraba(self, db, almacen,
                                                                       producto, monkeypatch):
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        monkeypatch.delenv('RC_ESPERA_NC_HORAS', raising=False)
        _m, _rec, dev, job = _rc_esperando(db, almacen, producto, horas=30)
        with patch('app.services.connekta_gateway.connekta') as mc:
            with pytest.raises(DependenciaPendiente) as e:
                _ejecutar_job(job)
        assert f'la devolución {dev.codigo}' in str(e.value) and '30 h' in str(e.value)
        assert 'La destraba bodega' in str(e.value)
        mc.trigger_recibo_caja.assert_not_called()

    def test_sigue_esperando_la_nota(self, db, almacen, producto):
        """El tope avisa; no suelta el recibo antes de la NC (Regla 7)."""
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        _m, _rec, _dev_, job = _rc_esperando(db, almacen, producto, horas=500)
        with patch('app.services.connekta_gateway.connekta') as mc:
            with pytest.raises(DependenciaPendiente):
                _ejecutar_job(job)
        mc.trigger_recibo_caja.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# P2-4 · Lo que un cliente no puede descontar, y la base mínima
# ═════════════════════════════════════════════════════════════════════════════

NO_EN_PUERTA = {'RETEFUENTE_1.5', 'AUTORRETENCION_ICA_NEIVA_3X1000',
                'AUTORRETENCION_ICA_NEIVA_3.5X1000', 'AUTORRETENCION_ICA_NEIVA_4.5X1000',
                'AUTORRETENCION_ICA_NEIVA_8X1000', 'AUTORRETENCION_ICA_PITALITO_4X1000'}


class TestLaPuertaDeLasRetenciones:

    def test_el_catalogo_marca_las_seis(self):
        from app.services.liquidacion_service import CATALOGO_RETENCIONES
        assert {k for k, v in CATALOGO_RETENCIONES.items()
                if not v['aplica_en_puerta']} == NO_EN_PUERTA
        assert all(v.get('no_en_puerta') for k, v in CATALOGO_RETENCIONES.items()
                   if not v['aplica_en_puerta']), 'cada una dice por qué'

    def test_el_conductor_no_las_ve(self):
        from app.services.politica_cobro import catalogo_de_la_puerta
        tipos = {r['tipo'] for r in catalogo_de_la_puerta()}
        assert tipos and not (tipos & NO_EN_PUERTA)

    def test_ni_la_nota_ni_la_confirmacion(self, db, almacen, producto):
        from app.services import politica_cobro as pc
        from app.services.liquidacion_service import LiquidacionService
        _m, rec = _parcial_valorizada(db, almacen, producto, cobrado=68000, datos={
            'motivo_descuento': 'AUTORRETENCION_ICA_NEIVA_3X1000', 'monto_descuento': 2000})
        with pytest.raises(ValueError, match='No se puede confirmar.*autorretención'):
            LiquidacionService.confirmar_retencion(rec.id, 1, True)
        rec.retencion_confirmada = True          # aunque alguien la forzara
        with pytest.raises(pc.RetencionNoAplicable, match='autorretención'):
            pc.exigir_retencion_aplicable(rec, 'AUTORRETENCION_ICA_NEIVA_3X1000')
        with pytest.raises(pc.RetencionNoAplicable, match='banco'):
            pc.exigir_retencion_aplicable(rec, 'RETEFUENTE_1.5')

    def test_la_pantalla_no_ofrece_si_le_correspondia(self, db, almacen, producto, tmp_path):
        from tests.test_sin_codigos_en_pantalla import _node, visible
        from app.services.liquidacion_service import LiquidacionService
        m, rec = _parcial_valorizada(db, almacen, producto, cobrado=68000, datos={
            'motivo_descuento': 'RETEFUENTE_1.5', 'monto_descuento': 2000})
        d = LiquidacionService.preparar_detalle_ruta(m.ruta.id)
        [rd] = [x for x in d['recaudos'] if x['id'] == rec.id]
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqDetalleRuta = { permisos: { confirmar_retencion: true } };
            return { html: _liqBloqueRetencion(7, REC, null) };
        """, globales={'REC': json.loads(json.dumps(rd, default=str))})
        txt = visible(out['html'])
        assert 'Sí le correspondía' not in txt and 'No le correspondía' in txt
        assert 'banco' in txt


class TestLaBaseMinimaEsUnAviso:

    def test_sin_configurar_no_hay_aviso(self, monkeypatch):
        from app.services import politica_cobro as pc
        monkeypatch.delenv('RETENCION_BASE_MINIMA_PESOS', raising=False)
        assert pc.bases_minimas() == ({}, [])
        assert pc.aviso_base_minima('RETEFUENTE_2.5', 10) is None

    def test_configurada_avisa_por_debajo(self, monkeypatch):
        from app.services import politica_cobro as pc
        monkeypatch.setenv('RETENCION_BASE_MINIMA_PESOS', '{"RETEFUENTE_2.5": 1344000}')
        assert 'por debajo de la base mínima' in pc.aviso_base_minima('RETEFUENTE_2.5', 180000)
        assert pc.aviso_base_minima('RETEFUENTE_2.5', 2000000) is None
        assert pc.aviso_base_minima('RETEIVA', 10) is None
        assert pc.aviso_base_minima('RETEFUENTE_2.5', None) is None

    @pytest.mark.parametrize('crudo,esperado', [
        ('{no es json', 'ilegible'), ('[1, 2]', 'objeto'),
        ('{"INVENTADA": 5}', 'catálogo'), ('{"RETEIVA": -3}', 'positiva'),
        ('{"RETEIVA": "x"}', 'número')])
    def test_lo_ilegible_se_ignora_y_se_declara(self, monkeypatch, crudo, esperado):
        from app.services import politica_cobro as pc
        monkeypatch.setenv('RETENCION_BASE_MINIMA_PESOS', crudo)
        bases, problemas = pc.bases_minimas()
        assert bases == {} and esperado in ' '.join(problemas)

    def test_no_bloquea_el_cobro(self, db, almacen, producto, monkeypatch):
        """El aviso no traba: la oficina decide."""
        from app.services.liquidacion_service import LiquidacionService
        monkeypatch.setenv('RETENCION_BASE_MINIMA_PESOS', '{"RETEFUENTE_2.5": 99999999}')
        m, rec = _parcial_valorizada(db, almacen, producto, cobrado=68000, datos={
            'motivo_descuento': 'RETEFUENTE_2.5', 'monto_descuento': 2000})
        with patch('app.services.connekta_gateway.connekta.get_rowids_factura',
                   return_value=[{'f470_vlr_bruto': 84034, 'f470_vlr_imp': 15966,
                                  'f470_vlr_neto': 100000, 'f470_rowid': '700'}]):
            d = LiquidacionService.preparar_detalle_ruta(m.ruta.id)
        [rd] = [x for x in d['recaudos'] if x['id'] == rec.id]
        assert 'por debajo de la base mínima' in rd['aviso_base_minima']
        assert '84.034' in rd['aviso_base_minima'] or '84,034' in rd['aviso_base_minima']
        LiquidacionService.confirmar_retencion(rec.id, 1, True)   # no bloquea


# ═════════════════════════════════════════════════════════════════════════════
# Trinquetes (AST)
# ═════════════════════════════════════════════════════════════════════════════

def _nombre(c):
    f = c.func
    return f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)


def _fn(src, nombre):
    return next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == nombre)


def contado_sin_mirar_la_parcial(src: str) -> bool:
    """¿`trato_de_cobro` devuelve CONTADO por «pagó algo» sin preguntar antes
    si una PARCIAL pagó de menos?"""
    fn = _fn(src, 'trato_de_cobro')
    for n in ast.walk(fn):
        if not isinstance(n, ast.If):
            continue
        if not any(isinstance(x, ast.Compare) and isinstance(x.left, ast.Name)
                   and x.left.id == 'monto' for x in ast.walk(n.test)):
            continue
        # La rama «pagó algo»: su primer return CONTADO tiene que venir después
        # de preguntar por la parcial.
        pregunta = [c.lineno for c in ast.walk(ast.Module(body=n.body, type_ignores=[]))
                    if isinstance(c, ast.Call) and _nombre(c) == 'parcial_pagada_de_menos']
        contado = [r.lineno for r in ast.walk(ast.Module(body=n.body, type_ignores=[]))
                   if isinstance(r, ast.Return) and isinstance(r.value, ast.Name)
                   and r.value.id == 'TRATO_CONTADO']
        if contado and not (pregunta and min(pregunta) < min(contado)):
            return True
    return False


#: Recorridos del catálogo que NO distinguen lo que un cliente puede
#: descontar, con su porqué. **Solo encoge.**
CATALOGO_SIN_FILTRO = {
    'app/services/liquidacion_service.py::<module>':
        'las vistas derivadas RETENCION_PUC / RETENCION_TASA (cuenta y tasa de cada tipo)',
}


def recorridos_del_catalogo(fuentes: dict) -> dict:
    """{archivo::función: menciona aplica_en_puerta?} para todo recorrido de
    `CATALOGO_RETENCIONES` (`.items()`, `.keys()`, `.values()` o iterarlo)."""
    out = {}
    for archivo, src in fuentes.items():
        arbol = ast.parse(src)
        padres = {}
        for n in ast.walk(arbol):
            for h in ast.iter_child_nodes(n):
                padres[h] = n
        for n in ast.walk(arbol):
            recorre = False
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr in ('items', 'keys', 'values') \
                    and isinstance(n.func.value, ast.Name) \
                    and n.func.value.id == 'CATALOGO_RETENCIONES':
                recorre = True
            if isinstance(n, (ast.For, ast.comprehension)) and isinstance(n.iter, ast.Name) \
                    and n.iter.id == 'CATALOGO_RETENCIONES':
                recorre = True
            if not recorre:
                continue
            fn, p = None, padres.get(n)
            while p is not None:
                if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = p
                    break
                p = padres.get(p)
            clave = f'{archivo}::{fn.name if fn else "<module>"}'
            ambito = fn if fn is not None else n
            filtra = any(isinstance(x, ast.Constant) and x.value == 'aplica_en_puerta'
                         for x in ast.walk(ambito))
            out[clave] = out.get(clave, True) and filtra
    return out


def _fuentes():
    return {str(p.relative_to(RAIZ)): p.read_text(encoding='utf-8') for p in APP.rglob('*.py')}


class TestTrinquetes:

    def test_el_contado_pregunta_por_la_parcial(self):
        src = (APP / 'services' / 'cond_pago.py').read_text(encoding='utf-8')
        assert not contado_sin_mirar_la_parcial(src), (
            'trato_de_cobro vuelve a dar CONTADO a una PARCIAL que pagó de menos '
            '(`parcial_pagada_de_menos`): crédito que nadie autorizó (P1-5)')

    def test_meta_del_contado(self):
        mal = ("def trato_de_cobro(r):\n    if not x and monto > 0:\n"
               "        return TRATO_CONTADO\n")
        bien = ("def trato_de_cobro(r):\n    if not x and monto > 0:\n"
                "        if parcial_pagada_de_menos(r) is not None:\n"
                "            return TRATO_NO_AUTORIZADO\n        return TRATO_CONTADO\n")
        tarde = ("def trato_de_cobro(r):\n    if not x and monto > 0:\n"
                 "        return TRATO_CONTADO\n        parcial_pagada_de_menos(r)\n")
        assert contado_sin_mirar_la_parcial(mal) is True
        assert contado_sin_mirar_la_parcial(bien) is False
        assert contado_sin_mirar_la_parcial(tarde) is True

    def test_ningun_recorrido_del_catalogo_ignora_la_puerta(self):
        r = recorridos_del_catalogo(_fuentes())
        malos = [s for s, ok in r.items() if not ok and s not in CATALOGO_SIN_FILTRO]
        assert not malos, (
            f'\n{malos}: una lista de retenciones que no distingue lo que un cliente puede '
            f'descontar (`aplica_en_puerta`): una autorretención o la bancaria vuelven a '
            f'ofrecerse o a emitirse contra la factura del cliente (P2-4).')

    def test_el_inventario_solo_encoge(self):
        r = recorridos_del_catalogo(_fuentes())
        sobran = [s for s in CATALOGO_SIN_FILTRO if r.get(s, True)]
        assert not sobran, f'{sobran} ya filtra (o ya no recorre): sáquelo del inventario'

    def test_piso(self):
        r = recorridos_del_catalogo(_fuentes())
        assert len(r) >= 4, f'el detector se quedó ciego ({r})'
        assert r.get('app/services/politica_cobro.py::catalogo_de_la_puerta') is True

    def test_meta_del_catalogo(self):
        src = ("A = [k for k, v in CATALOGO_RETENCIONES.items()]\n"
               "def ofrece():\n    '''aplica_en_puerta en el docstring también es texto'''\n"
               "    return [k for k in CATALOGO_RETENCIONES]\n"
               "def filtra():\n    return [k for k, v in CATALOGO_RETENCIONES.items()"
               " if v.get('aplica_en_puerta')]\n")
        r = recorridos_del_catalogo({'x.py': src})
        assert r['x.py::filtra'] is True and r['x.py::<module>'] is False
        # El docstring es una constante con otro valor: no cuenta como filtro.
        assert r['x.py::ofrece'] is False

    def test_nadie_mas_lee_las_variables_nuevas(self):
        """`RC_ESPERA_NC_HORAS` y `RETENCION_BASE_MINIMA_PESOS`: una función cada una."""
        dueños = {'RC_ESPERA_NC_HORAS': 'app/services/devolucion_ruta.py',
                  'RETENCION_BASE_MINIMA_PESOS': 'app/services/politica_cobro.py'}
        for archivo, src in _fuentes().items():
            for n in ast.walk(ast.parse(src)):
                if isinstance(n, ast.Call) and _nombre(n) in ('get', 'getenv') and n.args \
                        and isinstance(n.args[0], ast.Constant) and n.args[0].value in dueños:
                    assert archivo == dueños[n.args[0].value], (
                        f'{archivo} lee {n.args[0].value}: una política, una función')
