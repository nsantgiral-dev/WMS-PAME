"""
Integración de liquidación (2026-09-27): las costuras entre L1 (encolar sin
Siesa, el ejecutor resuelve), L2/L3 (acta de caja, transferencias vistas en el
banco) y L4 (parcial pagada de menos, retenciones de la puerta).

## Lo que solo se ve al juntarlos

1. **Liquidar no le pregunta nada a Siesa.** `liquidar_ruta` (L2) encola en la
   misma acción con `liquidar_ruta_siesa`, que desde L1 no lee Siesa. Se mide
   en ejecución: 15 rutas por la puerta HTTP, con la red reventando.
2. **Lo que le falta a una ruta para liquidarse es UNA lista**
   (`RutaService.lo_que_falta_para_liquidar`): la puerta la exige y la pantalla
   la pinta por ruta con su acción. Antes el acta (L2) y la parcial sin
   autorizar (L4) frenaban cada una por su lado y quien liquida se enteraba de
   a una, al pulsar.
3. **«Plata que falta» no se cuenta dos veces.** El faltante del acta es del
   conductor (`contado − declarado`); la parcial pagada de menos es del
   cliente (`esperado_en_caja − cobrado`). El acta solo informa la segunda, y
   con la misma función que la frena (`politica_cobro.cobrado_de_menos`) —
   antes tenía su propia cuenta, que restaba la retención dos veces.

Trinquetes AST (con meta-tests y pisos): `liquidar_ruta` no llama a las
políticas por su cuenta (las lee de la lista); la lista llama a las tres;
`caja_conductor` no mide «pagó de menos» con otra vara.
"""
import ast
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_caja_conductor import _conductor, _ruta
from tests.test_cartera_retencion import _jwt, _usuario
from tests.test_parcial_y_retenciones import _parcial_valorizada
from tests.test_sin_codigos_en_pantalla import _node, codigos_crudos, visible

RAIZ = Path(__file__).resolve().parents[1]
RUTA_SERVICE = RAIZ / 'app' / 'services' / 'ruta_service.py'
CAJA = RAIZ / 'app' / 'services' / 'caja_conductor.py'


def _entregada(db, m):
    m.ruta.estado = 'ENTREGADA'
    db.session.commit()
    return m.ruta


# ═════════════════════════════════════════════════════════════════════════════
# 1 · Liquidar 15 rutas no le pregunta nada a Siesa
# ═════════════════════════════════════════════════════════════════════════════

class TestLiquidarNoLePreguntaASiesa:

    def test_quince_rutas_por_la_puerta_con_la_red_reventando(self, app, client, db, almacen,
                                                               monkeypatch):
        """Cada ruta: efectivo + transferencia (+ un rechazo en una de cada
        tres, forzado con motivo), con su acta. El gateway real fuera de
        simulación, `_get`/`_post` y `requests` reventando **y anotando** (una
        lectura que traga la excepción dejaría el test en verde)."""
        import requests
        from app.models.ruta_despacho import RutaDespacho
        from app.models.siesa_job import SiesaJob
        from app.services import caja_conductor as cc
        from app.services.connekta_gateway import ConnektaGateway, connekta

        rutas = []
        liquidador = _usuario(db, 'liquidador')
        for i in range(15):
            c, _ = _conductor(db)
            paradas = [('ENTREGADO', 'EFECTIVO', 50000 + i, 50000 + i),
                       ('ENTREGADO', 'TRANSFERENCIA_BBVA', 20000, 20000)]
            if i % 3 == 0:
                paradas.append(('RECHAZADO', 'EFECTIVO', 0, 30000))
            ruta, recs = _ruta(db, almacen, c, paradas)
            for r in recs:
                r.tarea.fe_tipo, r.tarea.fe_consec = 'FE', str(17000 + i)
                if r.estado_entrega == 'RECHAZADO':
                    r.motivo_rechazo = 'CLIENTE_CERRADO'
            db.session.commit()
            cc.registrar_acta(c.id, 50000 + i, liquidador.id)
            rutas.append(ruta.id)

        preguntas = []

        def _no(*a, **k):
            preguntas.append(str(a[1:2] or a[:1])[:80])
            raise AssertionError('liquidar le preguntó a Siesa')
        monkeypatch.setattr(connekta, 'modo_simulacion', False)
        monkeypatch.setattr(ConnektaGateway, '_get', _no)
        monkeypatch.setattr(ConnektaGateway, '_post', _no)
        monkeypatch.setattr(requests, 'get', _no)
        monkeypatch.setattr(requests, 'post', _no)
        h = _jwt(app, liquidador)
        with patch('app.services.siesa_job_service.disparar_dlq_inmediato') as dlq:
            for rid in rutas:
                res = client.post(f'/api/rutas/{rid}/liquidar', headers=h,
                                  json={'motivo_devoluciones': 'rechazo de prueba, bodega lo cuenta mañana'})
                assert res.status_code == 200, res.get_json()
                assert res.get_json()['siesa_error'] is None, res.get_json()
        assert preguntas == [], f'liquidar le preguntó a Siesa: {preguntas}'
        assert all(db.session.get(RutaDespacho, rid).estado_financiero == 'LIQUIDADA' for rid in rutas)
        # Todo lo que estaba listo quedó en la cola, sin salir en el request.
        assert SiesaJob.query.filter_by(tipo='RECIBO_CAJA').count() == 30
        assert dlq.call_count >= 1


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Lo que falta para liquidar: una lista, la puerta y la pantalla
# ═════════════════════════════════════════════════════════════════════════════

class TestLoQueFaltaParaLiquidar:

    def test_la_parcial_sin_autorizar_la_devolucion_y_la_caja_juntas(self, db, almacen, producto):
        from app.services.ruta_service import RutaService
        m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        ruta = _entregada(db, m)
        faltas = RutaService.lo_que_falta_para_liquidar(ruta)
        assert [f['codigo'] for f in faltas] == [
            'credito_no_autorizado', 'devoluciones_sin_contar', 'caja_sin_acta']
        cna = faltas[0]
        assert cna['paradas'] == [{'recaudo_id': rec.id, 'pedido': m.tarea.numero_pedido_siesa,
                                   'diferencia': 30000.0}]
        assert cna['accion'] == 'abrir_ruta' and cna['forzable'] is None
        assert faltas[1]['forzable'] == 'motivo' and faltas[2]['forzable'] == 'admin'
        assert faltas[2]['conductor_id'] == m.conductor.id
        for f in faltas:
            assert f['error'].startswith(f['codigo'] + ':')
            assert not f['texto'].startswith(f['codigo'])

    def test_la_puerta_levanta_lo_mismo_que_la_lista_dice(self, db, almacen, producto):
        from app.services.ruta_service import RutaService
        m, _rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        ruta = _entregada(db, m)
        [primera, *_] = RutaService.lo_que_falta_para_liquidar(ruta)
        with pytest.raises(ValueError) as e:
            RutaService.liquidar_ruta(ruta.id, usuario_id=_usuario(db, 'liquidador').id)
        assert str(e.value) == primera['error']
        assert 'faltan $30,000' in str(e.value)

    def test_autorizada_la_parcial_queda_lo_forzable(self, db, almacen, producto):
        from app.services.liquidacion_service import LiquidacionService
        from app.services.ruta_service import RutaService
        m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        ruta = _entregada(db, m)
        LiquidacionService.autorizar_credito(rec.id, _usuario(db, 'lider_cartera').id,
                                             'cliente de confianza, paga el lunes')
        assert [f['codigo'] for f in RutaService.lo_que_falta_para_liquidar(ruta)] == [
            'devoluciones_sin_contar', 'caja_sin_acta']

    def test_una_ruta_en_camino_solo_dice_eso(self, db, almacen):
        from app.services.ruta_service import RutaService
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 1000, 1000)],
                        estado='EN_TRANSITO')
        [f] = RutaService.lo_que_falta_para_liquidar(ruta)
        assert f['codigo'] == 'ruta_sin_cerrar' and f['accion'] == 'pedir_cierre'

    def test_una_ruta_lista_no_tiene_nada_y_liquida(self, db, almacen):
        from app.services.ruta_service import RutaService
        c, _ = _conductor(db)
        ruta, _ = _ruta(db, almacen, c, [('ENTREGADO', 'TRANSFERENCIA_BBVA', 1000, 1000)])
        assert RutaService.lo_que_falta_para_liquidar(ruta) == []
        with patch('app.services.siesa_job_service.disparar_dlq_inmediato'):
            assert RutaService.liquidar_ruta(ruta.id, usuario_id=_usuario(db).id)['ok']

    def test_el_tablero_y_el_detalle_la_traen(self, app, client, db, almacen, producto):
        m, _rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        ruta = _entregada(db, m)
        h = _jwt(app, _usuario(db, 'liquidador'))
        d = client.get('/api/rutas/liquidacion/dashboard', headers=h).get_json()
        todas = d['rutas'] + d['rutas_atrasadas']
        [rd] = [x for x in todas if x['id'] == ruta.id]
        assert [f['codigo'] for f in rd['falta_para_liquidar']] == [
            'credito_no_autorizado', 'devoluciones_sin_contar', 'caja_sin_acta']
        det = client.get(f'/api/rutas/{ruta.id}/liquidacion-detalle', headers=h).get_json()
        assert [f['codigo'] for f in det['falta_para_liquidar']] == [
            'credito_no_autorizado', 'devoluciones_sin_contar', 'caja_sin_acta']

    def test_la_tarjeta_la_pinta_con_su_accion_y_sin_codigos(self, tmp_path, db, almacen,
                                                             producto):
        from app.services.ruta_service import RutaService
        m, _rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        ruta = _entregada(db, m)
        r = {'id': ruta.id, 'conductor_id': m.conductor.id, 'conductor_nombre': 'Ana <b>',
             'estado': 'ENTREGADA', 'estado_financiero': 'PENDIENTE', 'total_paradas': 1,
             'falta_para_liquidar': RutaService.lo_que_falta_para_liquidar(ruta),
             'caja': {'estado': 'FALTA', 'efectivo': 40000}}
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqDashboard = { permisos: { recibir_caja: true }, rutas: [] };
            const con = _liqRutaCard(R, false);
            _liqDashboard = { permisos: { recibir_caja: false }, rutas: [] };
            return { con, sin: _liqRutaCard(R, false) };
        """, globales={'R': r})
        txt = visible(out['con'])
        assert 'Para liquidar faltan 3 cosas' in txt
        assert 'faltan $30.000 sin autorizar' in txt
        assert 'Recibir la caja' in txt and 'Abrir la ruta para resolverlo' in txt
        assert 'Llegó el camión' in txt
        assert f'liqAbrirCajaConductor({m.conductor.id})' in out['con']
        assert 'Recibir la caja' not in visible(out['sin'])
        assert not [c for c in codigos_crudos(out['con'])
                    if c in ('caja_sin_acta', 'credito_no_autorizado', 'devoluciones_sin_contar')]
        assert 'Falta recibir la caja' not in txt, 'la caja no se dice dos veces en la tarjeta'


# ── Trinquete: la puerta no juzga por su cuenta ─────────────────────────────

#: Las políticas que deciden cada punto de la lista.
POLITICAS = {'credito_no_autorizado', 'pendientes_de_conteo', 'exigir_acta_para_liquidar'}

#: Llamadas a una política que `liquidar_ruta` hace por su cuenta, con su
#: porqué. **Solo encoge.**
LLAMADAS_DECLARADAS = {
    # Necesita el acta (el objeto) para devolver su id y para forzar sin ella
    # con motivo; la lista usa la MISMA función para el aviso.
    'exigir_acta_para_liquidar':
        'la puerta necesita el acta que devuelve (su id, o forzar con motivo)',
}


def _llamadas(fuente, funcion):
    arbol = ast.parse(fuente)
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == funcion:
            nombres = set()
            for m in ast.walk(n):
                if isinstance(m, ast.Call):
                    f = m.func
                    nombres.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', ''))
            return nombres
    raise AssertionError(f'{funcion} no está: el detector se desincronizó')


class TestLaPuertaLeeLaLista:

    def test_liquidar_no_llama_a_las_politicas_por_su_cuenta(self):
        llamadas = _llamadas(RUTA_SERVICE.read_text(encoding='utf-8'), 'liquidar_ruta')
        assert 'lo_que_falta_para_liquidar' in llamadas
        propias = (llamadas & POLITICAS) - set(LLAMADAS_DECLARADAS)
        assert not propias, (
            f'liquidar_ruta juzga por su cuenta {sorted(propias)}: agréguelo a '
            f'`lo_que_falta_para_liquidar`, que la pantalla también lee.')

    def test_la_lista_llama_a_las_tres(self):
        llamadas = _llamadas(RUTA_SERVICE.read_text(encoding='utf-8'),
                             'lo_que_falta_para_liquidar')
        assert POLITICAS <= llamadas

    def test_el_inventario_solo_encoge(self):
        assert set(LLAMADAS_DECLARADAS) <= POLITICAS
        assert len(LLAMADAS_DECLARADAS) <= 1
        assert all(len(v) > 20 for v in LLAMADAS_DECLARADAS.values())

    def test_meta_ve_la_llamada_y_no_el_docstring(self):
        fuente = ('def liquidar_ruta(r):\n'
                  '    """credito_no_autorizado(r) en el docstring no cuenta."""\n'
                  '    RutaService.lo_que_falta_para_liquidar(r)\n'
                  '    _cp.credito_no_autorizado(r)\n')
        assert 'credito_no_autorizado' in _llamadas(fuente, 'liquidar_ruta')
        sana = fuente.replace('    _cp.credito_no_autorizado(r)\n', '')
        assert 'credito_no_autorizado' not in _llamadas(sana, 'liquidar_ruta')

    def test_piso(self):
        assert len(_llamadas(RUTA_SERVICE.read_text(encoding='utf-8'), 'liquidar_ruta')) >= 8


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Una plata que falta se cuenta una vez
# ═════════════════════════════════════════════════════════════════════════════

class TestUnaPlataQueFaltaSeCuentaUnaVez:

    def test_la_parcial_pagada_de_menos_no_es_faltante_del_conductor(self, db, almacen,
                                                                      producto):
        from app.services import caja_conductor as cc
        from app.services import politica_cobro as pc
        m, rec = _parcial_valorizada(db, almacen, producto, cobrado=40000)
        _entregada(db, m)
        e = cc.esperado_de_entrega(m.conductor.id)
        assert e['efectivo'] == 40000
        [menor] = e['cobro_menor_que_factura']
        assert menor['diferencia'] == pc.faltante_de_la_parcial(rec)['diferencia'] == 30000
        assert menor['credito_no_autorizado'] is True
        acta = cc.registrar_acta(m.conductor.id, 40000, _usuario(db, 'liquidador').id)
        assert float(acta.diferencia) == 0, 'la plata del cliente no entra al faltante del conductor'

    def test_la_retencion_rechazada_la_ven_igual_las_dos(self, db, almacen):
        """El acta tenía su propia cuenta: restaba `monto_descuento` aunque la
        retención estuviera rechazada (el cliente la debía) y la dejaba fuera.
        Con la misma vara, aparece."""
        from app.services import caja_conductor as cc
        from app.services import politica_cobro as pc
        c, _ = _conductor(db)
        _, [r] = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 97500, 100000)])
        r.motivo_descuento, r.monto_descuento, r.retencion_confirmada = 'RETEFUENTE_2.5', 2500, False
        db.session.commit()
        [menor] = cc.esperado_de_entrega(c.id)['cobro_menor_que_factura']
        assert menor['diferencia'] == pc.cobrado_de_menos(r)['diferencia'] == 2500
        assert menor['credito_no_autorizado'] is False

    def test_la_retencion_confirmada_no_es_cobro_de_menos(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        _, [r] = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 97500, 100000)])
        r.motivo_descuento, r.monto_descuento, r.retencion_confirmada = 'RETEFUENTE_2.5', 2500, True
        db.session.commit()
        assert cc.esperado_de_entrega(c.id)['cobro_menor_que_factura'] == []


class TestLaCajaNoMideConOtraVara:
    """`caja_conductor` informa «el cliente pagó de menos» con la política de
    L4 (`cobrado_de_menos`), no con `esperado_en_caja` a mano."""

    VARA_PROPIA = {'esperado_en_caja'}

    def _llamadas_modulo(self, fuente):
        return {(m.func.attr if isinstance(m.func, ast.Attribute) else getattr(m.func, 'id', ''))
                for m in ast.walk(ast.parse(fuente)) if isinstance(m, ast.Call)}

    def test_el_modulo(self):
        llamadas = self._llamadas_modulo(CAJA.read_text(encoding='utf-8'))
        assert not (llamadas & self.VARA_PROPIA)
        assert 'cobrado_de_menos' in llamadas

    def test_meta(self):
        assert self.VARA_PROPIA & self._llamadas_modulo('x = _pc.esperado_en_caja(r, t)\n')
        assert not (self.VARA_PROPIA & self._llamadas_modulo('"""esperado_en_caja(r)"""\n'))

    def test_piso(self):
        assert len(self._llamadas_modulo(CAJA.read_text(encoding='utf-8'))) >= 30


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Validación de la liquidación integrada (2026-09-29): las actas
# ═════════════════════════════════════════════════════════════════════════════

class TestAnularNoBorraElFaltante:
    """P2: anular un acta (incluso confirmada por el conductor) y volver a
    registrarla sin diferencia hacía desaparecer el faltante del número de
    gerencia. Ahora va aparte, con quién la anuló, cuándo, por qué y qué era."""

    def test_el_faltante_anulado_se_ve(self, db, almacen):
        from app.services import caja_conductor as cc
        c, u = _conductor(db)
        _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        liq = _usuario(db, 'liquidador')
        a = cc.registrar_acta(c.id, 25000, liq.id, motivo_diferencia='Dijo que le faltó')
        cc.responder_conductor(a.id, u.id, True)
        with pytest.raises(ValueError):
            cc.anular_acta(a.id, liq.id, '  ')
        cc.anular_acta(a.id, liq.id, 'Recontamos y sí estaba')
        cc.registrar_acta(c.id, 30000, liq.id)
        [d] = cc.diferencias_por_conductor()
        assert d['faltante'] == 0 and d['actas'] == 1
        assert d['faltante_anulado'] == 5000
        [x] = d['anuladas']
        assert (x['acta_id'], x['diferencia'], x['estado_antes'], x['anulada_por'], x['motivo']) == (
            a.id, -5000, 'CONFIRMADA', liq.nombre, 'Recontamos y sí estaba')
        assert x['anulada_en']

    def test_la_pantalla_lo_pinta(self, tmp_path):
        lista = [{'conductor': 'Ana <b>', 'actas': 1, 'faltante': 0, 'sobrante': 0,
                  'faltante_anulado': 5000,
                  'anuladas': [{'acta_id': 7, 'diferencia': -5000, 'estado_antes': 'CONFIRMADA',
                                'anulada_por': 'Liq <i>', 'motivo': 'Recontamos'}]}]
        out = _node(tmp_path, ['util.js', 'liquidacion.js'], {}, """
            _liqRenderDiferenciasCaja(L);
            return { html: document.getElementById('liq-diferencias-caja').innerHTML };
        """, globales={'L': lista})
        txt = visible(out['html'])
        assert 'Acta #7 anulada con faltante de $5.000' in txt
        assert 'Confirmada por el conductor' in txt and 'la anuló Liq <i>' in txt
        assert '<i>' not in out['html'].replace('&lt;i&gt;', '')


class TestActaComplementaria:
    """P2: un acta que cubre dos rutas, una ya liquidada, y a la otra le entra
    efectivo después: antes pedía «anule el acta» (imposible) y la única salida
    era liquidar sin acta. Ahora la diferencia se recibe en un acta
    complementaria."""

    def _mundo(self, db, almacen):
        from app.services import caja_conductor as cc
        c, _ = _conductor(db)
        r1, _ = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 30000)])
        r2, [p2] = _ruta(db, almacen, c, [('ENTREGADO', 'EFECTIVO', 30000, 40000)])
        liq = _usuario(db, 'liquidador')
        a = cc.registrar_acta(c.id, 60000, liq.id)
        return c, r1, r2, p2, a, liq

    def test_la_diferencia_entra_en_un_acta_complementaria(self, db, almacen):
        from app.services import caja_conductor as cc
        from app.services.ruta_service import RutaService
        c, r1, r2, p2, a, liq = self._mundo(db, almacen)
        r1.estado_financiero = 'LIQUIDADA'
        p2.monto_cobrado = 40000                      # parada corregida después
        db.session.commit()
        with pytest.raises(cc.CajaSinActa, match='acta complementaria') as e:
            cc.exigir_acta_para_liquidar(r2)
        assert 'Anule' not in str(e.value)
        with pytest.raises(ValueError, match='ya liquidadas'):
            cc.anular_acta(a.id, liq.id, 'x')
        e = cc.esperado_de_entrega(c.id)
        assert e['efectivo'] == 10000
        assert [(x['ruta_id'], x['efectivo'], x['complementaria_de']) for x in e['rutas']] == [
            (r2.id, 10000, a.id)]
        assert c.id in [x['conductor_id'] for x in cc.conductores_por_recibir()]
        comp = cc.registrar_acta(c.id, 7000, liq.id, motivo_diferencia='Trajo 7.000')
        db.session.refresh(r2)
        assert r2.entrega_caja_id == a.id, 'la ruta sigue ligada al acta que la recibió'
        assert float(comp.diferencia) == -3000
        assert cc.exigir_acta_para_liquidar(r2).id == a.id
        assert 'caja_sin_acta' not in [f['codigo'] for f in RutaService.lo_que_falta_para_liquidar(r2)]
        assert cc.esperado_de_entrega(c.id)['rutas'] == []
        assert cc.diferencias_por_conductor()[0]['faltante'] == 3000

    def test_si_el_acta_se_puede_anular_sigue_pidiendo_anularla(self, db, almacen):
        from app.services import caja_conductor as cc
        c, r1, r2, p2, a, liq = self._mundo(db, almacen)
        p2.monto_cobrado = 40000
        db.session.commit()
        with pytest.raises(cc.CajaSinActa, match='Anule el acta'):
            cc.exigir_acta_para_liquidar(r2)
        assert cc.esperado_de_entrega(c.id)['rutas'] == []

    def test_anular_la_complementaria_vuelve_a_pedirla(self, db, almacen):
        from app.services import caja_conductor as cc
        c, r1, r2, p2, a, liq = self._mundo(db, almacen)
        r1.estado_financiero = 'LIQUIDADA'
        p2.monto_cobrado = 40000
        db.session.commit()
        comp = cc.registrar_acta(c.id, 10000, liq.id)
        cc.anular_acta(comp.id, liq.id, 'Contamos mal')
        db.session.refresh(r2)
        assert r2.entrega_caja_id == a.id
        assert [x['efectivo'] for x in cc.esperado_de_entrega(c.id)['rutas']] == [10000]
