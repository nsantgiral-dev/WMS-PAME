"""
Compras — lo que el comprador decidió (tanda F, 2026-09-27).

La clase: **la bandeja no guardaba lo que el comprador decidió.** Entre «Copiar
OC» y que la OC apareciera en el espejo de Siesa (30 min con el cron, nunca si
está apagado) la línea seguía ahí: dos personas pedían dos veces, el lunes
siguiente volvía sin rastro, y no quedaba quién decidió ni contra qué número.

Ahora (`app/services/compras_decisiones.py`, tabla `decision_compra`):
  · «Ya se pidió» (OC de Siesa n.º, cantidad) cuenta como «en camino»
    (`compras_fuentes.en_camino`, fuente `DECISION_WMS`) hasta que aparece la
    OC en el espejo o pasan 7 días (y se dice);
  · «Posponer hasta <fecha>» y «No pedir (motivo)» sacan la línea hasta su
    fecha, salvo que se vuelva urgente;
  · cada decisión guarda quién, cuándo y la línea como se vio; se deshace con
    motivo (bitácora ANULAR); «No pedir» deja un DESCARTAR;
  · escriben admin, jefe de almacén y compras; el gerente mira.

Trinquetes AST: solo `compras_decisiones` escribe una decisión y solo
`compras_fuentes._decisiones_en_camino` suma lo pedido. Meta-tests y pisos.
"""
import ast
import json
import pathlib
from datetime import timedelta

import pytest

from app.utils.fecha import dia_operativo
from tests.test_compras_bandeja import _cab, _mundo, lt_de_este_mundo

RAIZ = pathlib.Path(__file__).resolve().parent.parent
URL = '/api/compras/bandeja/decision'


def _hoy():
    return dia_operativo()


@pytest.fixture
def mundo(app, db, monkeypatch):
    monkeypatch.delenv('ROP_CICLO_NACIONAL_DIAS', raising=False)
    monkeypatch.delenv('COMPRAS_PEDIDO_EN_CAMINO_DIAS', raising=False)
    lt_de_este_mundo(monkeypatch)
    _mundo(db)


def _bandeja():
    from app.services import compras_bandeja
    return compras_bandeja.bandeja()


def _lineas(b):
    return {l['referencia']: l for p in b['proveedores'] for l in p['lineas']}


def _decidir(client, cab, **cuerpo):
    return client.post(URL, json=cuerpo, headers=cab)


class TestYaSePidio:

    def test_cuenta_como_en_camino_y_la_bandeja_lo_dice(self, app, db, client, almacen, mundo):
        antes = _lineas(_bandeja())['URG']
        assert antes['urgencia'] == 'URGENTE'
        cab = _cab(app, db, almacen, 'compras')
        r = _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=antes['pedir_unidades'],
                     oc_siesa='003-OC-500', cantidad_propuesta=antes['pedir_unidades'],
                     urgencia='URGENTE', foto={'pedir_unidades': antes['pedir_unidades'],
                                               'urgencia': 'URGENTE', 'ajeno': 'no se guarda'})
        assert r.status_code == 201, r.get_json()
        d = r.get_json()['decision']
        assert d['usuario_nombre'] == 'compras' and d['vigente'] is True
        assert d['foto'] == {'pedir_unidades': antes['pedir_unidades'], 'urgencia': 'URGENTE'}

        from app.services.compras_fuentes import en_camino
        ec = en_camino(['URG'])
        assert ec['por_sku']['URG'] == antes['pedir_unidades']
        assert ec['detalle']['URG']['decision_pedido']['estado'] == 'CUENTA'
        assert ec['declaracion']['fuentes']['DECISION_WMS']['unidades'] == antes['pedir_unidades']

        # Pedido hasta el nivel objetivo: la posición queda un ciclo de compra
        # por encima del punto de pedido y la línea sale de la bandeja. No se
        # pide dos veces; lo decidido queda a la vista.
        b = _bandeja()
        assert 'URG' not in _lineas(b), 'lo pedido ya cuenta: no se pide dos veces'
        assert [x['referencia'] for x in b['decisiones']['recientes']] == ['URG']
        from app.services.compras_bandeja import explicar_sku
        e = explicar_sku('URG')
        assert e['motivo'] == 'YA_CUBIERTO' and e['decision']['accion'] == 'PEDIDO'

    def test_pedir_de_menos_deja_la_linea_con_lo_que_falta(self, app, db, client, almacen, mundo):
        antes = _lineas(_bandeja())['URG']
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=20)
        l = _lineas(_bandeja())['URG']
        assert l['decision']['accion'] == 'PEDIDO' and l['decision']['cantidad'] == 20
        assert l['porque']['ya_pedido'] == 20
        assert l['pedir_unidades'] == antes['pedir_unidades'] - 20

    def test_cuando_aparece_la_oc_la_cuenta_el_espejo(self, app, db, client, almacen, mundo):
        from app.models.compras_fuentes import OcLineaSiesa
        from app.services.compras_fuentes import en_camino
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=100)
        db.session.add(OcLineaSiesa(
            rowid_linea=7777, co='003', tipo_docto='OC', consec_docto=501, fecha_oc=_hoy(),
            estado_oc=1, referencia='URG', bodega='NB1', factor=1, cant_pedida=100,
            cant_entrada=0, pendiente_base=100, fecha_entrega=_hoy(), abierta=True))
        db.session.commit()
        ec = en_camino(['URG'])
        assert ec['por_sku']['URG'] == 100.0, 'una vez: la OC, no la OC más la decisión'
        assert ec['detalle']['URG']['decision_pedido']['estado'] == 'OC_EN_SIESA'
        assert ec['declaracion']['fuentes']['DECISION_WMS']['cubiertos_por_oc'] == 1

    def test_la_oc_escrita_la_reconoce_aunque_sea_de_antes(self, app, db, client, almacen, mundo):
        from app.models.compras_fuentes import OcLineaSiesa
        from app.services.compras_fuentes import en_camino
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=100, oc_siesa='OC-502')
        db.session.add(OcLineaSiesa(
            rowid_linea=7778, co='003', tipo_docto='OC', consec_docto=502,
            fecha_oc=_hoy() - timedelta(days=2), estado_oc=1, referencia='URG', bodega='NB1',
            factor=1, cant_pedida=100, cant_entrada=0, pendiente_base=100,
            fecha_entrega=_hoy(), abierta=True))
        db.session.commit()
        assert en_camino(['URG'])['detalle']['URG']['decision_pedido']['estado'] == 'OC_EN_SIESA'

    def test_cuenta_hasta_lead_time_mas_dos_sigmas(self, app, db, client, almacen, mundo):
        """P1-C: con el lead time de este mundo (5 ± 2) cuenta 9 días; no 7
        fijos. Se calcula al leer: lo que diga la columna no manda."""
        from app.models.decision_compra import DecisionCompra
        from app.services.compras_decisiones import dias_pedido_en_camino
        from app.services.compras_fuentes import en_camino
        assert dias_pedido_en_camino()['dias'] == 9
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=100)
        d = DecisionCompra.query.one()
        d.dia = _hoy() - timedelta(days=8)
        d.vigente_hasta = _hoy() - timedelta(days=5)
        db.session.commit()
        assert en_camino(['URG'])['por_sku']['URG'] == 100.0

    def test_vencido_sin_oc_deja_de_contar_y_la_linea_vuelve_marcada(self, app, db, client,
                                                                    almacen, mundo):
        from app.models.decision_compra import DecisionCompra
        from app.services.compras_fuentes import en_camino
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=100, oc_siesa='OC-77')
        d = DecisionCompra.query.one()
        d.dia = _hoy() - timedelta(days=12)
        db.session.commit()
        ec = en_camino(['URG'])
        assert 'URG' not in ec['por_sku'] or ec['por_sku']['URG'] == 0
        dec = ec['declaracion']['fuentes']['DECISION_WMS']
        assert dec['vencidos_sin_oc'] == 1 and 'Revise si la orden se hizo' in dec['nota']
        l = _lineas(_bandeja())['URG']
        assert l['pedido_sin_llegar']['oc_siesa'] == 'OC-77'
        assert l['pedido_sin_llegar']['estado'] == 'VENCIDO_SIN_OC'

    def test_no_pedir_despues_de_pedir_deja_de_contar(self, app, db, client, almacen, mundo):
        from app.services.compras_fuentes import en_camino
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=100)
        assert en_camino(['URG'])['por_sku']['URG'] == 100.0
        _decidir(client, cab, referencia='URG', accion='DESCARTADO', motivo_codigo='PRECIO')
        assert en_camino(['URG'])['por_sku'].get('URG', 0) == 0


class TestPosponerYNoPedir:

    def test_posponer_la_saca_hasta_la_fecha(self, app, db, client, almacen, mundo):
        cab = _cab(app, db, almacen, 'compras')
        hasta = (_hoy() + timedelta(days=5)).isoformat()
        r = _decidir(client, cab, referencia='SEM', accion='POSPUESTO', hasta=hasta,
                     urgencia='ESTA_SEMANA')
        assert r.status_code == 201
        b = _bandeja()
        assert 'SEM' not in _lineas(b)
        assert 'SEM' in b['decisiones']['ocultas_por_decision']
        assert b['resumen']['ocultas_por_decision'] == 1
        from app.services.compras_bandeja import explicar_sku
        assert explicar_sku('SEM')['motivo'] == 'DECIDIDO'

    def test_si_se_vuelve_urgente_vuelve_a_la_bandeja(self, app, db, client, almacen, mundo):
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='POSPUESTO',
                 hasta=(_hoy() + timedelta(days=5)).isoformat(), urgencia='ESTA_SEMANA')
        l = _lineas(_bandeja())['URG']
        assert l['decision_escalo'] is True and l['decision']['accion'] == 'POSPUESTO'

    def test_no_pedir_exige_motivo_y_deja_bitacora(self, app, db, client, almacen, mundo):
        from app.models.bitacora import BitacoraAccion
        cab = _cab(app, db, almacen, 'compras')
        assert _decidir(client, cab, referencia='SEM', accion='DESCARTADO').status_code == 400
        assert _decidir(client, cab, referencia='SEM', accion='DESCARTADO',
                        motivo_codigo='OTRO').status_code == 400, 'con «otro», el detalle'
        r = _decidir(client, cab, referencia='SEM', accion='DESCARTADO',
                     motivo_codigo='DESCONTINUADO', motivo='lo reemplaza el SEM2')
        assert r.status_code == 201
        assert r.get_json()['decision']['motivo'] == 'Producto descontinuado — lo reemplaza el SEM2'
        b = BitacoraAccion.query.filter_by(accion='DESCARTAR', entidad='DecisionCompra').one()
        assert 'descontinuado' in b.motivo
        assert 'SEM' not in _lineas(_bandeja())

    @pytest.mark.parametrize('dias', [0, -1, 91])
    def test_la_fecha_de_posponer_tiene_limites(self, app, db, client, almacen, dias):
        cab = _cab(app, db, almacen, 'compras')
        r = _decidir(client, cab, referencia='X', accion='POSPUESTO',
                     hasta=(_hoy() + timedelta(days=dias)).isoformat())
        assert r.status_code == 400

    @pytest.mark.parametrize('cuerpo', [
        {'referencia': 'X', 'accion': 'PEDIDO', 'cantidad': 0},
        {'referencia': 'X', 'accion': 'PEDIDO', 'cantidad': 2.5},
        {'referencia': 'X', 'accion': 'PEDIDO'},
        {'referencia': 'X', 'accion': 'COMPRAR', 'cantidad': 3},
        {'referencia': '', 'accion': 'PEDIDO', 'cantidad': 3},
    ])
    def test_lo_invalido_es_400(self, app, db, client, almacen, cuerpo):
        assert client.post(URL, json=cuerpo, headers=_cab(app, db, almacen, 'compras')).status_code == 400


class TestLaUltimaDecisionManda:

    def test_pedir_despues_de_posponer_la_devuelve_con_lo_pedido(self, app, db, client, almacen,
                                                                 mundo):
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='POSPUESTO',
                 hasta=(_hoy() + timedelta(days=5)).isoformat(), urgencia='URGENTE')
        assert 'URG' not in _lineas(_bandeja())
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=20)
        l = _lineas(_bandeja())['URG']
        assert l['decision']['accion'] == 'PEDIDO'


class TestLoDecididoNoSePierde:
    """P1-D: toda decisión vigente se ve (no solo 14 días) y una pospuesta
    cuyo faltante crece vuelve marcada."""

    def test_una_pospuesta_vieja_sigue_en_ya_decidido(self, app, db, client, almacen, mundo):
        from app.models.decision_compra import DecisionCompra
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='SEM', accion='POSPUESTO',
                 hasta=(_hoy() + timedelta(days=60)).isoformat())
        d = DecisionCompra.query.one()
        d.dia = _hoy() - timedelta(days=30)
        db.session.commit()
        b = _bandeja()
        assert 'SEM' not in _lineas(b)
        assert [x['referencia'] for x in b['decisiones']['recientes']] == ['SEM']
        from app.services.compras_bandeja import explicar_sku
        assert explicar_sku('SEM')['motivo'] == 'DECIDIDO'

    def test_pospuesta_urgente_vuelve_si_el_faltante_crece(self, app, db, client, almacen, mundo):
        cab = _cab(app, db, almacen, 'compras')
        antes = _lineas(_bandeja())['URG']
        _decidir(client, cab, referencia='URG', accion='POSPUESTO', urgencia='URGENTE',
                 cantidad_propuesta=int(antes['pedir_unidades'] / 3),
                 hasta=(_hoy() + timedelta(days=5)).isoformat())
        l = _lineas(_bandeja())['URG']
        assert l['decision_escalo'] is True
        assert l['decision_reaparece']['motivo'] == 'FALTANTE_CRECIO'
        assert 'cuando se decidió faltaban' in l['decision_reaparece']['texto']

    def test_pospuesta_urgente_sin_cambio_no_vuelve(self, app, db, client, almacen, mundo):
        cab = _cab(app, db, almacen, 'compras')
        antes = _lineas(_bandeja())['URG']
        _decidir(client, cab, referencia='URG', accion='POSPUESTO', urgencia='URGENTE',
                 cantidad_propuesta=antes['pedir_unidades'],
                 hasta=(_hoy() + timedelta(days=5)).isoformat())
        assert 'URG' not in _lineas(_bandeja())


class TestDeshacerYDobleToque:

    def test_deshacer_exige_motivo_y_la_linea_vuelve(self, app, db, client, almacen, mundo):
        from app.models.bitacora import BitacoraAccion
        cab = _cab(app, db, almacen, 'compras')
        d = _decidir(client, cab, referencia='SEM', accion='POSPUESTO',
                     hasta=(_hoy() + timedelta(days=5)).isoformat()).get_json()['decision']
        url = f'{URL}/{d["id"]}/anular'
        assert client.post(url, json={'motivo': '  '}, headers=cab).status_code == 400
        r = client.post(url, json={'motivo': 'me equivoqué de producto'}, headers=cab)
        assert r.status_code == 200 and r.get_json()['decision']['anulada'] is True
        assert BitacoraAccion.query.filter_by(accion='ANULAR', entidad='DecisionCompra').count() == 1
        assert 'SEM' in _lineas(_bandeja())
        assert client.post(url, json={'motivo': 'otra vez'}, headers=cab).status_code == 409

    def test_el_doble_toque_no_crea_dos(self, app, db, client, almacen, mundo):
        from app.models.decision_compra import DecisionCompra
        cab = _cab(app, db, almacen, 'compras')
        a = _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=50)
        b = _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=50)
        assert a.status_code == 201 and b.status_code == 200
        assert b.get_json()['decision']['repetida'] is True
        assert DecisionCompra.query.count() == 1


class TestQuienDecide:

    @pytest.mark.parametrize('rol', ['compras', 'admin', 'jefe_almacen'])
    def test_escriben(self, app, db, client, almacen, rol):
        r = _decidir(client, _cab(app, db, almacen, rol), referencia='X', accion='PEDIDO', cantidad=3)
        assert r.status_code == 201

    @pytest.mark.parametrize('rol', ['gerente', 'conductor', 'supervisor', 'operario'])
    def test_no_escriben(self, app, db, client, almacen, rol):
        r = _decidir(client, _cab(app, db, almacen, rol), referencia='X', accion='PEDIDO', cantidad=3)
        assert r.status_code == 403

    def test_la_bandeja_dice_quien_puede(self, app, db, client, almacen, mundo):
        for rol, puede in (('compras', True), ('gerente', False)):
            d = client.get('/api/compras/bandeja', headers=_cab(app, db, almacen, rol)).get_json()
            assert d['puede_decidir'] is puede, rol

    def test_la_lista_blanca_de_escritura_no_tiene_al_gerente(self):
        from app.routes._auth_helpers import Roles
        assert Roles.GERENTE not in Roles.COMPRAS_ESCRITURA
        assert set(Roles.COMPRAS_ESCRITURA) <= set(Roles.COMPRAS_ROLES)


# ─────────────────────────────────────────────────────────────────────────────
# La pantalla
# ─────────────────────────────────────────────────────────────────────────────

class TestLaPantalla:

    def _render(self, tmp_path, d, **extra):
        from tests.test_compras_bandeja_js import _node
        return _node(tmp_path, pintar=dict({'html': f'cmpBandejaHtml({json.dumps(d)}, null)'}, **extra))

    def test_quien_puede_ve_los_tres_botones_y_el_formulario(self, app, db, client, almacen,
                                                             mundo, tmp_path):
        from tests.test_compras_bandeja_js import _json, _limpio
        d = _json(dict(_bandeja(), puede_decidir=True))
        k = 0
        out = self._render(
            tmp_path, d,
            form=(f'(() => {{ cmpBandejaHtml({json.dumps(d)}, null); cmpYaSePidio({k}); '
                  f'return document.getElementById("cmp-dec-{k}").innerHTML; }})()'),
            cuerpo=(f'(() => {{ cmpBandejaHtml({json.dumps(d)}, null); cmpNoPedir({k}); '
                    f'return JSON.stringify(cmpDecisionCuerpo({k})); }})()'))
        t = _limpio(out['html'])
        assert 'Ya se pidió' in t and 'Posponer' in t and 'No pedir' in t
        assert 'onclick="cmpYaSePidio(0)"' in out['html']
        f = _limpio(out['form'])
        assert 'Número de la OC en Siesa' in f and 'Unidades que pidió' in f and 'Guardar' in f
        cuerpo = json.loads(out['cuerpo'])
        primera = d['proveedores'][0]['lineas'][0]
        assert cuerpo['accion'] == 'DESCARTADO' and cuerpo['referencia'] == primera['referencia']
        assert cuerpo['cantidad_propuesta'] == primera['pedir_unidades']
        assert cuerpo['foto']['pedir_unidades'] == primera['pedir_unidades']

    def test_el_gerente_no_ve_botones(self, app, db, mundo, tmp_path):
        from tests.test_compras_bandeja_js import _json
        d = _json(dict(_bandeja(), puede_decidir=False))
        out = self._render(tmp_path, d)
        assert 'cmpYaSePidio' not in out['html'] and 'cmpDeshacerDecision' not in out['html']

    def test_lo_decidido_se_pinta_escapado(self, app, db, client, almacen, mundo, tmp_path):
        from tests.test_compras_bandeja_js import MALO, _json, _limpio
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='SEM', accion='DESCARTADO', motivo_codigo='OTRO',
                 motivo=MALO, foto={'pedir_unidades': 83})
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=40, oc_siesa='OC-9')
        d = _json(dict(_bandeja(), puede_decidir=True))
        out = self._render(tmp_path, d)
        assert '<img' not in out['html']
        t = _limpio(out['html'])
        assert 'Ya decidido estos días (2)' in t
        assert 'No se pide, decidió compras' in t and 'la bandeja proponía 83' in t
        assert 'Pedido por compras' in t and '40 u · OC OC-9' in t
        assert 'fuera de la bandeja por una decisión vigente' in t
        assert 'onclick="cmpDeshacerDecision(0)"' in out['html']


# ─────────────────────────────────────────────────────────────────────────────
# Trinquetes
# ─────────────────────────────────────────────────────────────────────────────

def _funciones(arbol):
    out = [('<modulo>', arbol)]

    def visitar(nodo, pref):
        for h in ast.iter_child_nodes(nodo):
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((f'{pref}{h.name}', h))
                visitar(h, f'{pref}{h.name}.')
            elif isinstance(h, ast.ClassDef):
                visitar(h, f'{pref}{h.name}.')
            else:
                visitar(h, pref)
    visitar(arbol, '')
    return out


def _propios(fn):
    pila = list(fn.body)
    while pila:
        n = pila.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield n
        pila.extend(ast.iter_child_nodes(n))


def _usos(src, archivo):
    """[(archivo, funcion, forma)]: `crea` (DecisionCompra(...)), `anula`
    (asignar .anulada_en), `suma` (llamar pedidos_sin_oc o leer
    .cantidad_decidida)."""
    salida = []
    for q, fn in _funciones(ast.parse(src)):
        for n in _propios(fn):
            if isinstance(n, ast.Call):
                f = n.func
                nombre = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)
                if nombre == 'DecisionCompra':
                    salida.append((archivo, q, 'crea'))
                if nombre == 'pedidos_sin_oc':
                    salida.append((archivo, q, 'suma'))
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Attribute) and t.attr == 'anulada_en':
                        salida.append((archivo, q, 'anula'))
            if (isinstance(n, ast.Attribute) and n.attr == 'cantidad_decidida'
                    and isinstance(n.ctx, ast.Load)):
                salida.append((archivo, q, 'suma'))
    return salida


PERMITIDOS = {
    'crea': {('app/services/compras_decisiones.py', 'registrar')},
    'anula': {('app/services/compras_decisiones.py', 'anular')},
    'suma': {('app/services/compras_fuentes.py', '_decisiones_en_camino'),
             ('app/services/compras_decisiones.py', 'registrar'),
             ('app/services/compras_decisiones.py', 'a_dict'),
             ('app/services/compras_decisiones.py', 'pedidos_sin_oc')},
}


def _todos():
    salida, n = [], 0
    for base in ('app', 'flota'):
        for p in sorted((RAIZ / base).rglob('*.py')):
            n += 1
            salida += _usos(p.read_text(encoding='utf-8'), str(p.relative_to(RAIZ)))
    return salida, n


class TestUnaDecisionUnDueno:

    def test_solo_su_dueno_crea_anula_y_suma(self):
        usos, _n = _todos()
        malos = sorted({u for u in usos if (u[0], u[1]) not in PERMITIDOS[u[2]]})
        assert not malos, (f'{malos}: crean o anulan una decisión de compra, o suman lo '
                           'pedido, fuera de compras_decisiones / en_camino.')

    def test_el_inventario_solo_encoge(self):
        assert sum(len(v) for v in PERMITIDOS.values()) <= 6

    def test_piso(self):
        usos, n = _todos()
        assert n >= 200
        vistos = {(a, f) for a, f, _ in usos}
        assert set().union(*PERMITIDOS.values()) <= vistos, \
            f'el escáner dejó de ver a los dueños: {set().union(*PERMITIDOS.values()) - vistos}'


class TestElDetectorMuerde:

    def test_ve_crear_anular_y_sumar(self):
        src = ('def a(): return DecisionCompra(referencia="X")\n'
               'def b(d): d.anulada_en = 1\n'
               'def c(): return cd.pedidos_sin_oc()\n'
               'def e(d): return d.cantidad_decidida + 1\n')
        assert sorted(_usos(src, 'x.py')) == [('x.py', 'a', 'crea'), ('x.py', 'b', 'anula'),
                                               ('x.py', 'c', 'suma'), ('x.py', 'e', 'suma')]

    def test_no_marca_lo_sano(self):
        src = ('def f(d):\n'
               '    """DecisionCompra(...) y d.cantidad_decidida en un docstring"""\n'
               '    # d.anulada_en = 1\n'
               '    return {"cantidad_decidida": 3, "x": d.anulada_en}\n')
        assert _usos(src, 'x.py') == []


class TestLaPantallaDiceLoQueVolvio:

    def test_lo_pedido_que_no_llego_vuelve_marcado(self, app, db, client, almacen, mundo,
                                                   tmp_path):
        from app.models.decision_compra import DecisionCompra
        from tests.test_compras_bandeja_js import _json, _limpio, _node
        cab = _cab(app, db, almacen, 'compras')
        _decidir(client, cab, referencia='URG', accion='PEDIDO', cantidad=100, oc_siesa='OC-77')
        d = DecisionCompra.query.one()
        d.dia = _hoy() - timedelta(days=12)
        db.session.commit()
        b = _json(dict(_bandeja(), puede_decidir=True))
        l = _lineas(b)['URG']
        import json as _j
        out = _node(tmp_path, pintar={'html': f'cmpLineaHtml({_j.dumps(l)}, 0, 0, 0)'})
        t = _limpio(out['html'])
        assert 'Ya se pidió el ' in t and '(OC OC-77)' in t and '100 u' in t
        assert 'No ha llegado ni aparece en Siesa: confírmelo con el proveedor' in t

    def test_la_pospuesta_que_crecio_dice_por_que_volvio(self, app, db, client, almacen, mundo,
                                                         tmp_path):
        from tests.test_compras_bandeja_js import _json, _limpio, _node
        cab = _cab(app, db, almacen, 'compras')
        antes = _lineas(_bandeja())['URG']
        _decidir(client, cab, referencia='URG', accion='POSPUESTO', urgencia='URGENTE',
                 cantidad_propuesta=int(antes['pedir_unidades'] / 3),
                 hasta=(_hoy() + timedelta(days=5)).isoformat())
        l = _lineas(_json(_bandeja()))['URG']
        import json as _j
        t = _limpio(_node(tmp_path, pintar={'html': f'cmpLineaHtml({_j.dumps(l)}, 0, 0, 0)'})['html'])
        assert 'Pospuesto por compras' in t and 'cuando se decidió faltaban' in t
