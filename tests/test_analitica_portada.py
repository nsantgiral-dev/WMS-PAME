"""📈 Analítica → 🎯 ¿Cómo vamos? — la portada (2026-09-24).

Lo que esta suite exige, en el orden en que un gerente lo leería:

1. **Una cifra, una fuente.** Llega a caja y ciclo de caja son EXACTAMENTE los
   del 🧭 Recorrido con los mismos filtros; plata en riesgo y venta perdida son
   la suma de las fugas de 💸 Fugas. Si divergen, la gerencia vuelve a tener
   dos números para el mismo hecho — el defecto que la portada existe para
   cerrar.
2. **Contra la meta.** Seis indicadores con meta, umbral, dueño y la marca de
   provisional, en el catálogo (`analitica_kpi.METRICAS`); el semáforo es UNA
   función (`analitica_kpi.semaforo`). Sin dato es gris, nunca rojo; un piso
   nunca es verde.
3. **Nace útil y lo dice.** Con el KPI diario apagado, las dos que dependen de
   él salen «sin dato» con la razón, y la portada lo avisa UNA vez con cómo se
   enciende (admin: botón de recálculo).
4. **Qué hacer.** Cada tarjeta trae su porqué y su lista de acciones, con
   botones que pasan posiciones y llevan a la pantalla que resuelve.
5. **La pantalla** (Node, `util.js` real): todo dato escapado, `onclick` solo
   con posiciones, sin letra < 12 px ni hex, una columna en el teléfono sin
   partir «$121.000 al menos», y el shell abre en la portada con Diagnóstico
   solo para admin.
"""
import json
import pathlib
import re
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from tests.test_analitica_recorrido import (  # noqa: F401 (fixture)
    _conductor, _entregar, _hasta_despacho, _tok, _usuario, mundo)
from tests.test_analitica_fugas import _agotado, _job_fallido

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
X = '<img src=x onerror=alert(1)>'


def _kpi():
    from app.services import analitica_kpi
    return analitica_kpi


def _port():
    from app.services import analitica_portada
    return analitica_portada


def _rango():
    from app.utils.fecha import dia_operativo
    hoy = dia_operativo()
    return hoy - timedelta(days=29), hoy


def _tarjetas(p):
    return {t['clave']: t for t in p['tarjetas']}


@pytest.fixture
def mundo_portada(db, mundo):
    """El mundo del recorrido + agotados (con y sin precio), un recibo de caja
    trabado, una nota crédito trabada (no es plata en riesgo) y una ruta
    entregada con plata cobrada que nadie liquidó."""
    from app.models.ruta_despacho import RutaDespacho
    alm = mundo['almacen']
    _agotado(db, alm, mundo['oper'], precio=12000, categoria='CUADERNOS')
    _agotado(db, alm, mundo['oper'], precio=None, categoria='ESCRITURA')
    mundo['job_rc'] = _job_fallido(db, 'RECIBO_CAJA', {'monto': 85000})
    mundo['job_nc'] = _job_fallido(db, 'NOTA_CREDITO_FACTURA', {})
    cond_u, cond = _conductor(db, alm)
    f = _hasta_despacho(db, alm, mundo['oper'], 1520, 'CLIENTE CALLE', cond, n=1)
    _entregar(db, f, cond_u, monto_cobrado=60000)
    r = db.session.get(RutaDespacho, f.ruta_id)
    r.estado = 'ENTREGADA'
    r.fecha_entregada = datetime.utcnow()
    db.session.commit()
    mundo['ruta_calle'] = f.ruta_id
    return mundo


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Una cifra, una fuente
# ─────────────────────────────────────────────────────────────────────────────

class TestUnaCifraUnaFuente:

    def test_llega_a_caja_y_ciclo_son_los_del_recorrido(self, mundo_portada):
        from app.services.analitica_recorrido import recorrido
        d, h = _rango()
        g = recorrido({'desde': d, 'hasta': h, 'almacen_id': None})['guia']
        t = _tarjetas(_port().portada(d, h))
        assert t['llega_a_caja']['periodo']['valor'] == g['valor_sin_fuga_cerrados']['tasa']
        assert t['llega_a_caja']['periodo']['n'] == g['valor_sin_fuga_cerrados']['pedidos_base']
        assert t['ciclo_caja']['periodo']['valor'] == g['ciclo_caja']['mediana_dias']
        assert t['ciclo_caja']['periodo']['n'] == g['ciclo_caja']['n']

    def test_con_almacen_tambien(self, mundo_portada):
        from app.services.analitica_recorrido import recorrido
        d, h = _rango()
        alm = mundo_portada['almacen'].id
        g = recorrido({'desde': d, 'hasta': h, 'almacen_id': alm})['guia']
        t = _tarjetas(_port().portada(d, h, alm))
        assert t['llega_a_caja']['periodo']['valor'] == g['valor_sin_fuga_cerrados']['tasa']

    def test_plata_en_riesgo_es_la_suma_de_sus_fugas(self, mundo_portada):
        from app.services.analitica_fugas import calcular_fugas
        d, h = _rango()
        f = {x['clave']: x for x in calcular_fugas(d, h)['fugas']}
        t = _tarjetas(_port().portada(d, h))['plata_en_riesgo']
        # Documentos trabados: solo el recibo de caja ($85.000) es plata; la
        # nota crédito trabada es un descuadre y no entra.
        # «Cobrado sin recibo en Siesa» (2026-09-25): lo cobrado en rutas ya
        # liquidadas cuyo recibo no consta. No se solapa con la calle (rutas
        # sin liquidar) ni con los trabados (el RC FALLIDO se cuenta allá).
        esperado = (f['plata_en_la_calle']['pesos'] + 85000.0
                    + (f['entregado_sin_pago']['pesos'] or 0)
                    + (f['credito_no_autorizado']['pesos'] or 0)
                    + (f['cobrado_sin_recibo']['pesos'] or 0))
        assert t['periodo']['valor'] == esperado
        assert f['documentos_trabados']['casos'] == 2, 'el mundo tiene RC y NC trabados'
        comp = {c['clave']: c for c in t['periodo']['componentes']}
        assert comp['documentos_trabados']['casos'] == 1
        assert comp['plata_en_la_calle']['pesos'] == 60000.0
        assert t['periodo']['es_piso'] is True, 'el entregado sin pago no tiene valor'

    def test_venta_perdida_es_la_de_fugas(self, mundo_portada):
        from app.services.analitica_fugas import calcular_fugas
        d, h = _rango()
        f = {x['clave']: x for x in calcular_fugas(d, h)['fugas']}['venta_perdida']
        t = _tarjetas(_port().portada(d, h))['venta_perdida']
        assert t['periodo']['valor'] == f['pesos'] == 36000.0
        assert t['periodo']['es_piso'] is True

    def test_el_resumen_del_catalogo_da_lo_mismo_que_la_portada(self, app, client,
                                                                mundo_portada):
        d, h = _rango()
        r = client.get(f'/api/analitica/resumen?desde={d}&hasta={h}',
                       headers=_tok(app, mundo_portada['admin'])).get_json()
        cat = {m['clave']: m for m in r['metricas']}
        port = _tarjetas(r['portada'])
        for clave in ('llega_a_caja', 'ciclo_caja', 'plata_en_riesgo'):
            assert cat[clave]['periodo']['valor'] == port[clave]['periodo']['valor'], clave


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Contra la meta
# ─────────────────────────────────────────────────────────────────────────────

class TestLasMetas:

    def test_los_seis_tienen_meta_umbral_dueno_y_son_provisionales(self, app):
        k = _kpi()
        for clave in _port().INDICADORES:
            m = k.METRICAS[clave]
            assert m.meta is not None and m.umbral_amarillo is not None, clave
            assert m.dueno.strip(), clave
            assert m.meta_provisional is True, clave
            # El amarillo queda del lado malo de la meta.
            if m.direccion == k.SUBE_ES_BUENO:
                assert m.umbral_amarillo < m.meta, clave
            else:
                assert m.umbral_amarillo > m.meta, clave

    def test_metas_declaradas(self, app):
        k = _kpi()
        assert k.METRICAS['ciclo_caja'].meta == 2.0
        assert k.METRICAS['llega_a_caja'].meta == 0.95
        assert k.METRICAS['fill_rate'].meta == 0.92
        assert k.METRICAS['plata_en_riesgo'].meta == 0.0

    def test_las_de_cohorte_no_se_guardan_por_dia(self, app, db, almacen):
        from app.models.analitica_kpi import AnaliticaKpiDiario
        from app.utils.fecha import dia_operativo
        k = _kpi()
        k.calcular_y_guardar_dia(dia_operativo() - timedelta(days=1))
        guardadas = {f.metrica for f in AnaliticaKpiDiario.query.all()}
        cohorte = {c for c, m in k.METRICAS.items() if m.agregacion == k.COHORTE}
        assert cohorte == {'llega_a_caja', 'ciclo_caja', 'plata_en_riesgo'}
        assert not guardadas & cohorte
        assert guardadas, 'las diarias sí se guardan'

    def test_la_serie_de_una_de_cohorte_lo_dice(self, app, db):
        d, h = _rango()
        puntos = _kpi().serie('ciclo_caja', h - timedelta(days=2), h)
        assert all(p['valor'] is None and 'en vivo' in p['motivo'] for p in puntos)


class TestElSemaforo:

    @pytest.mark.parametrize('clave,valor,nivel', [
        ('llega_a_caja', 0.96, 'verde'), ('llega_a_caja', 0.95, 'verde'),
        ('llega_a_caja', 0.92, 'amarillo'), ('llega_a_caja', 0.5, 'rojo'),
        ('ciclo_caja', 1.5, 'verde'), ('ciclo_caja', 2.0, 'verde'),
        ('ciclo_caja', 2.5, 'amarillo'), ('ciclo_caja', 4.0, 'rojo'),
        ('plata_en_riesgo', 0, 'verde'), ('plata_en_riesgo', 500000, 'amarillo'),
        ('plata_en_riesgo', 2000000, 'rojo'),
    ])
    def test_contra_la_meta(self, app, clave, valor, nivel):
        assert _kpi().semaforo(clave, valor)['nivel'] == nivel

    def test_sin_dato_es_gris_nunca_rojo(self, app):
        for clave in _port().INDICADORES:
            s = _kpi().semaforo(clave, None)
            assert s['nivel'] == 'sin_dato' and s['texto'] == 'Sin dato'

    def test_sin_meta(self, app):
        assert _kpi().semaforo('pedidos_despachados', 10)['nivel'] == 'sin_meta'

    def test_un_piso_nunca_es_verde(self, app):
        s = _kpi().semaforo('venta_perdida', 0, dias=30, es_piso=True)
        assert s['nivel'] == 'amarillo' and s['por_piso'] is True
        # Un piso que ya pasa el umbral es rojo igual: lo que falta solo empeora.
        assert _kpi().semaforo('plata_en_riesgo', 5e6, es_piso=True)['nivel'] == 'rojo'

    def test_la_meta_por_dia_escala_con_el_periodo(self, app):
        k = _kpi()
        assert k.semaforo('venta_perdida', 150000, dias=1)['nivel'] == 'rojo'
        assert k.semaforo('venta_perdida', 150000, dias=30)['nivel'] == 'amarillo'
        assert k.semaforo('venta_perdida', 150000, dias=30)['umbral_amarillo'] == 3000000

    def test_ninguna_tarjeta_sin_dato_es_roja(self, mundo_portada):
        d, h = _rango()
        for t in _port().portada(d, h)['tarjetas']:
            if t['periodo']['valor'] is None:
                assert t['semaforo']['nivel'] == 'sin_dato', t['clave']
            for s in t['tendencia']:
                if s['valor'] is None:
                    assert s['semaforo'] == 'sin_dato', t['clave']

    def test_el_recorrido_usa_el_mismo_semaforo(self, app, client, mundo_portada):
        r = client.get('/api/analitica/recorrido',
                       headers=_tok(app, mundo_portada['admin'])).get_json()
        g = r['guia']
        assert r['semaforos']['llega_a_caja'] == _kpi().semaforo(
            'llega_a_caja', g['valor_sin_fuga_cerrados']['tasa'])
        assert r['semaforos']['ciclo_caja'] == _kpi().semaforo(
            'ciclo_caja', g['ciclo_caja']['mediana_dias'])

    def test_el_recorrido_pregunta_por_la_cifra_de_lo_cerrado(self, app, mundo_portada,
                                                              monkeypatch):
        """Con el mundo, cohorte y cerrados caen en el mismo color: el test de
        arriba no vería que el recorrido le pase al semáforo la tasa de toda la
        cohorte. Este mira los argumentos."""
        from app.services import analitica_kpi
        from app.services.analitica_recorrido import recorrido
        vistos = {}
        real = analitica_kpi.semaforo
        def _espia(clave, valor, **kw):
            vistos[clave] = valor
            return real(clave, valor, **kw)
        monkeypatch.setattr(analitica_kpi, 'semaforo', _espia)
        d, h = _rango()
        g = recorrido({'desde': d, 'hasta': h, 'almacen_id': None})['guia']
        assert vistos['llega_a_caja'] == g['valor_sin_fuga_cerrados']['tasa']
        assert vistos['llega_a_caja'] != g['valor_sin_fuga']['tasa'], 'el mundo las distingue'
        assert vistos['ciclo_caja'] == g['ciclo_caja']['mediana_dias']


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Nace útil y lo dice
# ─────────────────────────────────────────────────────────────────────────────

class TestElKpiApagado:

    def test_las_del_kpi_salen_sin_dato_y_con_la_razon(self, mundo_portada, monkeypatch):
        monkeypatch.delenv('ANALITICA_KPI', raising=False)
        d, h = _rango()
        p = _port().portada(d, h)
        t = _tarjetas(p)
        for clave in ('fill_rate', 'exactitud_inventario'):
            assert t[clave]['periodo']['valor'] is None
            assert t[clave]['periodo']['sin_calcular'] is True
            assert t[clave]['origen'] == 'kpi_diario'
        assert p['kpi_diario']['encendido'] is False
        assert 'ANALITICA_KPI=true' in p['kpi_diario']['como_encender']
        # Las en vivo sí tienen número aunque el cron esté apagado.
        assert t['llega_a_caja']['periodo']['valor'] is not None
        assert t['plata_en_riesgo']['periodo']['valor'] is not None

    def test_tendencia_de_ocho_ventanas_de_siete_dias(self, mundo_portada):
        d, h = _rango()
        p = _port().portada(d, h)
        assert len(p['semanas']) == 8
        assert p['semanas'][-1]['hasta'] == h.isoformat()
        for s in p['semanas']:
            a, b = (datetime.fromisoformat(s['desde']), datetime.fromisoformat(s['hasta']))
            assert (b - a).days == 6
        assert all(len(t['tendencia']) == 8 for t in p['tarjetas'])

    def test_la_ultima_ventana_coincide_con_su_propia_medicion(self, mundo_portada):
        d, h = _rango()
        t = _tarjetas(_port().portada(d, h))['venta_perdida']
        assert t['tendencia'][-1]['valor'] == 36000.0


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Por qué y qué hacer
# ─────────────────────────────────────────────────────────────────────────────

class TestQueHacer:

    def test_plata_en_riesgo_trae_liquidar_ver_y_reintentar(self, mundo_portada):
        d, h = _rango()
        acc = _tarjetas(_port().portada(d, h))['plata_en_riesgo']['por_que']['que_hacer']
        tipos = {a['tipo'] for a in acc}
        assert {'liquidar_ruta', 'ver_pedido', 'reintentar'} <= tipos, acc
        liq = next(a for a in acc if a['tipo'] == 'liquidar_ruta')
        assert liq['ruta_id'] == mundo_portada['ruta_calle'] and liq['pesos'] == 60000.0
        rc = next(a for a in acc if a['tipo'] == 'reintentar')
        assert rc['job_id'] == mundo_portada['job_rc'].id and rc['destino'] == 'liquidacion'
        assert all(a.get('job_id') != mundo_portada['job_nc'].id for a in acc)
        # Sin valor primero (como el detalle de Fugas), después de mayor a menor.
        con = [a['pesos'] for a in acc if a['pesos'] is not None]
        assert con == sorted(con, reverse=True)

    def test_ciclo_trae_tramos_y_pedidos(self, mundo_portada):
        d, h = _rango()
        pq = _tarjetas(_port().portada(d, h))['ciclo_caja']['por_que']
        assert len(pq['tramos']) == 6
        grupos = {a.get('grupo') for a in pq['que_hacer']}
        assert grupos == {'esperando', 'lentos'}
        assert all(a['tipo'] == 'ver_pedido' and a['pedido_clave'] for a in pq['que_hacer'])

    def test_llega_a_caja_dice_donde_se_pierde(self, mundo_portada):
        d, h = _rango()
        pq = _tarjetas(_port().portada(d, h))['llega_a_caja']['por_que']
        motivos = {p['motivo'] for p in pq['perdidas']}
        assert any('cancelado' in m.lower() for m in motivos), motivos
        assert all(a['tipo'] == 'ver_pedido' for a in pq['que_hacer'])

    def test_un_porque_roto_no_tumba_la_cifra(self, mundo_portada, monkeypatch):
        port = _port()

        def _roto(m):
            raise RuntimeError('boom')
        monkeypatch.setitem(port._POR_QUE, 'ciclo_caja', _roto)
        d, h = _rango()
        t = _tarjetas(port.portada(d, h))['ciclo_caja']
        assert t['periodo']['valor'] is not None
        assert 'boom' in t['por_que']['error'] and t['por_que']['que_hacer'] == []


class TestElEndpoint:

    def test_solo_gestion(self, app, client, mundo):
        assert client.get('/api/analitica/resumen',
                          headers=_tok(app, mundo['oper'])).status_code == 403

    def test_forma(self, app, client, mundo_portada):
        r = client.get('/api/analitica/resumen', headers=_tok(app, mundo_portada['admin']))
        assert r.status_code == 200
        p = r.get_json()['portada']
        assert [t['clave'] for t in p['tarjetas']] == list(_port().INDICADORES)
        for t in p['tarjetas']:
            assert {'periodo', 'semaforo', 'variacion', 'tendencia', 'por_que', 'dueno',
                    'meta', 'umbral_amarillo', 'meta_provisional', 'origen'} <= set(t)
        assert set(p['conteo']) == {'verde', 'amarillo', 'rojo', 'sin_dato'}
        assert sum(p['conteo'].values()) == 6

    def test_basura_es_400(self, app, client, mundo):
        h = _tok(app, mundo['admin'])
        for qs in ('desde=ayer', 'almacen_id=x', 'hasta=2999-01-01'):
            assert client.get(f'/api/analitica/resumen?{qs}', headers=h).status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# 5 · La pantalla — Node con util.js real
# ─────────────────────────────────────────────────────────────────────────────

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], modo = args[1] || '', datos = JSON.parse(fs.readFileSync(args[2], 'utf8'));
const rol = args[3] || 'admin';
const els = {};
const mkEl = (id) => (els[id] = els[id] || { id, innerHTML: '', value: '', style: {}, dataset: {},
  options: [], selectedIndex: -1, classList: { toggle(){}, add(){}, remove(){} }, scrollIntoView(){} });
const llamadas = [];
const ctx = { console, window: {}, localStorage: { getItem: () => null, setItem(){} },
  document: { getElementById: (id) => mkEl(id), querySelectorAll: () => [], querySelector: () => null },
  setTimeout, clearTimeout, confirm: () => true };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(`var OPERARIO = { rol: '${rol}' };
  async function get(u){ return globalThis.__resp(u); }
  async function post(u, b){ globalThis.__llamadas.push(['post', u, b]); return {}; }
  function alerta(){} function tab(t){ globalThis.__llamadas.push(['tab', t]); }
  function liqAbrirRuta(id){ globalThis.__llamadas.push(['liqAbrirRuta', id]); }
  function liqSubtab(s){ globalThis.__llamadas.push(['liqSubtab', s]); }`, ctx);
ctx.__llamadas = llamadas;
ctx.__resp = (u) => {
  if (u.startsWith('/api/almacenes')) return [];
  if (u.startsWith('/api/analitica/resumen')) return datos.resumen;
  if (u.startsWith('/api/analitica/salud')) return datos.salud;
  if (u.includes('/recorrido/pedido/')) { llamadas.push(['pedido', u]); return { pedido: {}, etapas: [], eventos: [] }; }
  return {};
};
for (const f of ['analitica.js', 'analitica_portada.js', 'analitica_recorrido.js', 'analitica_fugas.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
vm.runInContext('function anSaludCargar(el){el.innerHTML="salud"} function anBitacoraCargar(el){el.innerHTML="bitacora"} function anDiagnosticoCargar(el){el.innerHTML="diagnostico"} function anRecorridoCargar(el){el.innerHTML="recorrido"; return Promise.resolve();}', ctx);
if (modo === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
(async () => {
  await vm.runInContext('cargarAnalitica', ctx)();
  await new Promise(r => setTimeout(r, 20));
  const shell = els['tab-analitica'].innerHTML;
  const abrio = vm.runInContext('_AN_SUBTAB', ctx);
  const portada = vm.runInContext('anPortHtml', ctx)(datos.resumen);
  const confianza = vm.runInContext('anPortConfianzaHtml', ctx)();
  const tarjetas = datos.resumen.portada.tarjetas;
  const detalles = tarjetas.map((t, i) => vm.runInContext('anPortDetalleHtml', ctx)(t, i));
  const todo = portada + confianza + detalles.join('');
  // Las acciones: plata en riesgo (índice 2) y su lista.
  const iPlata = tarjetas.findIndex(t => t.clave === 'plata_en_riesgo');
  const acc = tarjetas[iPlata].por_que.que_hacer;
  for (let k = 0; k < acc.length; k++) await vm.runInContext('anPortAccion', ctx)(iPlata, k);
  await new Promise(r => setTimeout(r, 20));
  // Un destino fuera de la lista blanca no se abre.
  tarjetas[3].por_que.que_hacer = [{ tipo: 'ir_tab', tab: 'tab-usuarios' }];
  await vm.runInContext('anPortAccion', ctx)(3, 0);
  // Diagnóstico para un no admin cae en la portada.
  vm.runInContext("anSubtab('diagnostico')", ctx);
  const trasDiag = vm.runInContext('_AN_SUBTAB', ctx);
  vm.runInContext("anSubtab('salud')", ctx);
  const trasSalud = vm.runInContext('_AN_SUBTAB', ctx);
  const handlers = [...new Set([...todo.matchAll(/onclick="([A-Za-z_$][\w$]*)\(/g)].map(m => m[1]))];
  const onclickConDato = [...todo.matchAll(/onclick="([^"]*)"/g)].map(m => m[1])
    .filter(a => !/^[A-Za-z_$][\w$]*\((-?\d+(,-?\d+)*)?\)$/.test(a));
  const spark = vm.runInContext('anPortSparkline', ctx)({ nombre: 'x', unidad: 'pesos', semaforo: { meta: null },
    tendencia: [{ valor: 1, semaforo: 'rojo' }, { valor: null }, { valor: null }, { valor: 3, semaforo: 'verde' },
                { valor: 4, semaforo: 'amarillo' }, { valor: 5, semaforo: 'rojo' }, { valor: null }, { valor: 2, semaforo: 'verde' }] });
  console.log(JSON.stringify({
    crudos: (todo.match(/<img/g) || []).length,
    escapados: (todo.match(/&lt;img/g) || []).length,
    handlers, sinDefinir: handlers.filter(h => typeof ctx[h] !== 'function'), onclickConDato,
    fontMenor: /font-size:\s*(\d|1[01])px/.test(todo + shell),
    hex: (todo.match(/(?:color|background|border|fill|stroke)[^;"]*#[0-9a-fA-F]{3,8}\b/g) || []),
    abrio, trasDiag, trasSalud,
    shellTabs: ['¿Cómo vamos?', 'Recorrido', 'Fugas', 'Diagnóstico'].map(t => shell.indexOf(t)),
    avisoKpi: (portada.match(/salen del cálculo diario/g) || []).length,
    botonRecalcular: portada.includes('anPortRecalcular()'),
    pisoUnaVez: (portada.match(/«al menos»:/g) || []).length,
    alMenosSinPartir: /white-space:nowrap[^>]*>\$[\d.]+<span[^>]*> al menos/.test(portada),
    unaColumna: portada.includes('minmax(min(100%,270px),1fr)'),
    sinDatoRojo: tarjetas.filter(t => t.periodo.valor === null).some(t => {
      const i = portada.indexOf(`data-clave="${t.clave}"`);
      const fin = portada.indexOf('class="an-port-tarjeta"', i + 1);
      const trozo = portada.slice(i, fin < 0 ? undefined : fin);
      return trozo.includes('--err-'); }),
    frase: portada.includes('De cada $100 que cerraron'),
    confianza, llamadas,
    spark: { circulos: (spark.match(/<circle/g) || []).length, tramos: (spark.match(/<polyline/g) || []).length,
             huecos: (spark.match(/<line /g) || []).length },
  }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


@pytest.fixture
def datos_js(app, client, db, mundo_portada, tmp_path):
    """Las respuestas REALES de /resumen y /salud, con dato hostil adentro."""
    from app.models.packing import TareaPacking
    from app.models.pedido_historia import PedidoHistoria
    for m in (PedidoHistoria, TareaPacking):
        for x in m.query.all():
            x.cliente = X
    db.session.commit()
    h = _tok(app, mundo_portada['admin'])
    res = client.get('/api/analitica/resumen', headers=h).get_json()
    sal = client.get('/api/analitica/salud', headers=h).get_json()
    for t in res['portada']['tarjetas']:
        t['nombre'] = X + t['nombre']
        t['dueno'] = X
        for a in t['por_que'].get('que_hacer', []):
            a['titulo'] = X
            a['detalle'] = X
    sal['global']['razones'].append({'nivel': 'critico', 'texto': X})
    p = tmp_path / 'datos.json'
    p.write_text(json.dumps({'resumen': res, 'salud': sal}), encoding='utf-8')
    return p


def _render(datos, modo='', rol='admin'):
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), modo, str(datos), rol],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLaPantalla:

    def test_ningun_dato_llega_crudo(self, datos_js):
        r = _render(datos_js)
        assert r['crudos'] == 0, r
        assert r['escapados'] >= 20, f'piso: el arnés dejó de pintar ({r["escapados"]})'

    def test_el_arnes_muerde_con_esc_roto(self, datos_js):
        assert _render(datos_js, 'esc-roto')['crudos'] >= 20

    def test_botones_existen_y_solo_llevan_posiciones(self, datos_js):
        r = _render(datos_js)
        assert r['sinDefinir'] == [], r['sinDefinir']
        assert {'anPortAbrir', 'anPortAccion', 'anPortRecalcular'} <= set(r['handlers'])
        assert r['onclickConDato'] == [], r['onclickConDato']

    def test_legible(self, datos_js):
        r = _render(datos_js)
        assert r['fontMenor'] is False
        assert r['hex'] == [], r['hex']

    def test_abre_en_la_portada_y_diagnostico_es_de_admin(self, datos_js):
        adm = _render(datos_js, rol='admin')
        assert adm['abrio'] == 'portada'
        assert all(i >= 0 for i in adm['shellTabs']) and adm['shellTabs'] == sorted(adm['shellTabs'])
        ger = _render(datos_js, rol='gerente')
        assert ger['shellTabs'][3] == -1, 'un gerente no ve Diagnóstico'
        assert ger['trasDiag'] == 'portada' and ger['trasSalud'] == 'portada'
        assert adm['trasSalud'] == 'diagnostico', 'la pestaña vieja de Salud lleva a Diagnóstico'

    def test_el_kpi_apagado_se_dice_una_vez(self, datos_js):
        adm = _render(datos_js, rol='admin')
        assert adm['avisoKpi'] == 1 and adm['botonRecalcular'] is True
        ger = _render(datos_js, rol='gerente')
        assert ger['avisoKpi'] == 1 and ger['botonRecalcular'] is False

    def test_sin_dato_en_gris_y_el_piso_una_vez(self, datos_js):
        r = _render(datos_js)
        assert r['sinDatoRojo'] is False
        assert r['pisoUnaVez'] == 1
        assert r['alMenosSinPartir'] is True, '«$X al menos» en un solo renglón'
        assert r['unaColumna'] is True
        assert r['frase'] is True

    def test_la_confianza_en_palabras(self, datos_js):
        r = _render(datos_js)
        assert 'Confianza del dato' in r['confianza']
        assert 'Ver por qué' in r['confianza']

    def test_las_acciones_llevan_a_la_pantalla_que_resuelve(self, datos_js):
        r = _render(datos_js)
        ll = r['llamadas']
        assert ['tab', 'tab-liquidacion'] in ll
        assert any(x[0] == 'liqAbrirRuta' and isinstance(x[1], int) for x in ll), ll
        assert ['liqSubtab', 'jobs'] in ll
        assert any(x[0] == 'pedido' for x in ll), 'ver pedido abre su recorrido'
        assert ['tab', 'tab-usuarios'] not in ll, 'fuera de la lista blanca no se abre'

    def test_la_tendencia_corta_en_los_huecos(self, datos_js):
        s = _render(datos_js)['spark']
        assert s['circulos'] == 5, 'un punto por semana con dato'
        assert s['huecos'] == 3, 'una marca gris por semana sin dato'
        assert s['tramos'] == 1, 'solo el tramo 3-4-5 tiene más de un punto seguido'


# ─────────────────────────────────────────────────────────────────────────────
# Integración: script, service worker, permisos, deuda
# ─────────────────────────────────────────────────────────────────────────────

class TestEnLaApp:

    def _leer(self, nombre):
        return (PWA / nombre).read_text(encoding='utf-8')

    def test_scripts_en_orden_y_cacheados(self):
        html = self._leer('index.html')
        i_shell = html.index('/static/pwa/analitica.js?v=')
        i_port = html.index('/static/pwa/analitica_portada.js?v=')
        i_diag = html.index('/static/pwa/analitica_diagnostico.js?v=')
        assert i_shell < i_port < i_diag
        sw = self._leer('sw.js')
        for f in ('analitica_portada.js', 'analitica_diagnostico.js'):
            assert f"'/static/pwa/{f}'" in sw

    def test_la_portada_es_la_primera_vista(self):
        js = self._leer('analitica.js')
        m = re.search(r'const AN_VISTAS = \{\s*(\w+):', js)
        assert m and m.group(1) == 'portada'
        assert "const AN_VISTA_INICIAL = 'portada'" in js

    def test_resumen_salio_de_la_deuda(self):
        from tests.test_frontend_integrity import DEUDA_SIN_UI
        for ruta in ('/api/analitica/resumen', '/api/analitica/metricas',
                     '/api/analitica/kpi/recalcular'):
            assert ruta not in DEUDA_SIN_UI, ruta

    def test_nav_distingue_operacion_de_analitica(self):
        html = self._leer('index.html')
        assert "onclick=\"tab('tab-dashboard')\"" in html
        dash = re.search(r"tab\('tab-dashboard'\)\"[^>]*>([^<]+)<", html).group(1)
        anal = re.search(r"tab\('tab-analitica'\)\"[^>]*>([^<]+)<", html).group(1)
        assert dash.strip() != anal.strip()
        assert dash.split()[0] != anal.split()[0], 'íconos distintos'
