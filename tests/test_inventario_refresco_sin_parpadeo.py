"""Inventario Cíclico no parpadea: una política de refresco (2026-09-24).

«En Estadísticas la interfaz parpadea cada tanto.» La causa real:
`TIMER_ADMIN` (app.js) llama cada 30 s a `cargarAdmin(true)` →
`cargarInventario()` → `invSubtab(_INV_SUBTAB)` → el cargador de la pestaña
activa, y TODOS empezaban con `el.innerHTML = 'Cargando…'`. El panel se vaciaba,
el área scrolleable se encogía (el scroll saltaba arriba) y se volvía a pintar.
Estadísticas, además, volvía a pedir un reporte que nadie había cambiado; Datos
devolvía la fecha del kardex a 2024-01-01; y la respuesta de un tick lento podía
pisar a la del botón «Actualizar» con otros filtros.

La clase no es «Estadísticas parpadea»: es **un refresco periódico que vacía o
pisa lo que se está viendo**. La política vive en `invCargarPanel` (conteo.js)
y la usa cada cargador del módulo (Líder, Conteos, ABC, Datos, Definitivo,
Estadísticas y las listas de abajo):

| Regla | Test |
|---|---|
| El tick solo refresca la pestaña visible y viva; Estadísticas no se re-pide | `TestElTickNoTocaEstadisticas` |
| Con datos pintados, recargar nunca deja el panel vacío ni «Cargando…» | `TestRecargarNoVaciaElPanel` (las seis pestañas) |
| Una respuesta que llega con la pestaña oculta no pinta | `TestUnaPestanaOcultaNoSePinta` |
| La respuesta vieja no pisa a la nueva | `TestLaRespuestaViejaNoPisa` |
| Salir de la pestaña o de Inventario limpia sus intervalos | `TestLosIntervalosSeLimpianAlSalir` |
| Si el refresco falla, lo visto se queda y se declara | `TestSiElRefrescoFallaLoVistoSeQueda` |

El arnés corre `util.js`, `conteo.js` y `kardex.js` REALES en Node, más
`cargarAdmin` y `tab` extraídos LITERALES de `app.js`, con timers falsos
controlables y un DOM mínimo sembrado con los ids de `index.html`. Cada escritura
de `innerHTML` queda registrada, así que «el panel nunca quedó vacío» se mide
sobre la historia, no sobre el estado final. `TestLasMutacionesMuerden` rompe
cada regla en una copia del PWA y exige que su test se ponga rojo.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

# El timer de app.js, tal cual. Si cambia allá, este arnés tiene que cambiar.
TICK_ADMIN = 'setInterval(() => cargarAdmin(true), 30000)'

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], escenario = args[1];

// ── Timers falsos ────────────────────────────────────────────────────────
let ahora = 0, sigId = 1;
const timers = new Map();
const fakeSetInterval = (fn, ms) => { const id = sigId++; timers.set(id, { fn, ms, next: ahora + ms, rep: true }); return id; };
const fakeSetTimeout = (fn, ms) => { const id = sigId++; timers.set(id, { fn, ms: ms || 0, next: ahora + (ms || 0), rep: false }); return id; };
const fakeClear = (id) => { timers.delete(id); };
const drenar = async () => { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); };
async function avanzar(ms) {
  const fin = ahora + ms;
  for (;;) {
    let proximo = null;
    for (const [id, t] of timers) if (t.next <= fin && (!proximo || t.next < proximo[1].next)) proximo = [id, t];
    if (!proximo) break;
    const [id, t] = proximo;
    ahora = t.next;
    if (t.rep) t.next += t.ms; else timers.delete(id);
    t.fn();
    await drenar();
  }
  ahora = fin;
}

// ── DOM mínimo: los ids de index.html existen; los que pinta un innerHTML aparecen ──
const html = fs.readFileSync(base + '/index.html', 'utf8');
const presentes = new Set([...html.matchAll(/\sid="([^"$]+)"/g)].map(m => m[1]));
const main = { id: 'admin-main', scrollTop: 500 };
const els = {};
function nodo(id) {
  const n = { id, style: {}, value: '', options: [], textContent: '', parentElement: main, _html: '', escrituras: [] };
  Object.defineProperty(n, 'innerHTML', {
    get() { return n._html; },
    set(v) {
      n._html = String(v);
      n.escrituras.push(n._html);
      for (const m of n._html.matchAll(/\sid="([^"]+)"/g)) presentes.add(m[1]);
      // Un contenido corto encoge el área scrolleable: el navegador sube el scroll.
      if (n._html.length < 400) main.scrollTop = 0;
    },
  });
  return n;
}
const $ = (id) => presentes.has(id) ? (els[id] = els[id] || nodo(id)) : null;
const documento = { getElementById: $, querySelectorAll: () => [], hidden: false };

// ── get() diferido: la prueba decide cuándo y en qué orden responde ──
const pedidos = [];
const get = (url) => new Promise((resolver, rechazar) => pedidos.push({ url, resolver, rechazar, listo: false }));
function pendientes(patron) { return pedidos.filter(p => !p.listo && p.url.includes(patron)); }
async function responder(patron, datos, cual = 'todos') {
  let ps = pendientes(patron);
  if (cual === 'primero') ps = ps.slice(0, 1);
  if (cual === 'ultimo') ps = ps.slice(-1);
  ps.forEach(p => { p.listo = true; typeof datos === 'function' ? p.resolver(datos(p.url)) : p.resolver(datos); });
  await drenar();
  return ps.length;
}
async function fallar(patron) {
  const ps = pendientes(patron);
  ps.forEach(p => { p.listo = true; p.rechazar(new Error('sin red')); });
  await drenar();
  return ps.length;
}
const cuantos = (patron) => pedidos.filter(p => p.url.includes(patron)).length;

const ctx = { console, window: { scrollY: 0 }, document: documento, get,
  setInterval: fakeSetInterval, setTimeout: fakeSetTimeout, clearInterval: fakeClear, clearTimeout: fakeClear,
  alerta: () => {}, cargarDashboard: async () => {}, URLSearchParams, encodeURIComponent };
vm.createContext(ctx);
vm.runInContext('var TAB = "tab-dashboard";', ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/kardex.js', 'utf8'), ctx);
// cargarAdmin y tab, LITERALES de app.js.
const app = fs.readFileSync(base + '/app.js', 'utf8');
function extraer(firma) {
  const i = app.indexOf(firma);
  if (i < 0) throw new Error('no está en app.js: ' + firma);
  let j = app.indexOf('{', i), prof = 0;
  for (; j < app.length; j++) { if (app[j] === '{') prof++; else if (app[j] === '}' && --prof === 0) break; }
  return app.slice(i, j + 1);
}
vm.runInContext(extraer('async function cargarAdmin(') + '\n' + extraer('function tab(id)'), ctx);
vm.runInContext('_INV_ALMACENES = [{ id: 1, nombre: "NB1" }, { id: 2, nombre: "NC1" }];', ctx);
const js = (c) => vm.runInContext(c, ctx);
$('lider-almacen').options = [{}, {}]; $('lider-almacen').value = '1';
$('inv-abc-almacen').value = '1';

// ── Respuestas del servidor ──────────────────────────────────────────────
const largo = 'x'.repeat(600);
const tablero = (nombre) => ({ almacen: nombre, al_dia_operativo: '2026-09-24', permisos: {}, decisiones: {},
  resumen: { por_bloque: {}, decisiones_pendientes: 0 }, hoy: { dia: '2026-09-24', por_persona: {} }, fuente: largo });
const metrica = { numerador: 1, denominador: 2, porcentaje: null, sin_porcentaje_por: 'n chico' };
const estadisticas = (fuente) => ({ parametros: { desde: '2026-08-27', hasta: '2026-09-24' }, fuente: fuente + largo,
  carga_cobertura: { filas: [], por_almacen: [] }, rezago: { pendiente: {}, en_curso: {} },
  volumen: { a_cc2: metrica, a_cc3: metrica, recuentos: metrica, por_semana: [] },
  ajustes: { valor: {}, bloqueados_hoy: { por_motivo: {} }, motivos_auditoria_picking: { por_motivo: {} } },
  exactitud: { por_clase: {} }, productos_problema: { por_diferencia: [], por_ajustes: [], por_recuentos: [] },
  por_operario: { filas: [] } });
const sesion = { id: 7, codigo: 'CC-7', estado: 'PENDIENTE', producto_codigo: 'P1', producto_nombre: 'CUADERNO ' + largo, ubicacion_codigo: 'A-01' };
const RESPUESTAS = [
  ['/api/conteo/lider/tablero', (u) => tablero(u.includes('almacen_id=2') ? 'DOS' : 'UNO')],
  ['/api/conteo/recogido-sin-despachar', { pedidos: [{ pedido: 'PD1', almacen_nombre: largo, productos: [] }] }],
  ['/api/conteo/estadisticas', () => estadisticas('REPORTE')],
  ['/api/conteo/stats', { pendientes: 1 }],
  ['/api/conteo/definitivos', { pendientes: [sesion], total: 1 }],
  ['/api/conteo/abc/resumen', { distribucion_abc: {}, plan: {}, fuente: largo }],
  ['/api/conteo/?', { sesiones: [sesion], total: 1, total_paginas: 1 }],
  ['/api/kardex/descargar/estado', { en_curso: false, resultado: null }],
  ['/api/kardex/salud', { veredicto: 'AL_DIA', confiable: true, problemas: [] }],
];
// Datos pide estado y salud a la vez (`Promise.all`): se responden juntos.
async function responderKardex(estado) {
  await responder('/api/kardex/descargar/estado', estado);
  await responder('/api/kardex/salud', { veredicto: 'AL_DIA', confiable: true, problemas: [] });
}
async function responderTodo() {
  for (const [patron, datos] of RESPUESTAS) await responder(patron, datos);
}
const CONTENIDO = { lider: 'inv-lider-contenido', conteos: 'inv-conteos-lista', abc: 'inv-abc-resumen',
  datos: 'inv-datos-container', estadisticas: 'inv-estadisticas-contenido' };
const vacioOCargando = (h) => !h.trim() || /Cargando/.test(h);
const pollsVivos = () => [...timers.values()].filter(t => t.rep && t.ms === 10000).length;

const esc = {};
(async () => {
  js('TAB = "tab-inventario"');
  js(args[2]);   // el timer de app.js, literal (TICK_ADMIN)

  if (escenario === 'tick_estadisticas') {
    js('invSubtab("estadisticas")');
    await responderTodo();
    const el = $('inv-estadisticas-contenido');
    const antes = el.escrituras.length;
    main.scrollTop = 500;
    await avanzar(95000);                 // tres ticks
    await responderTodo();
    esc.pedidos = cuantos('/api/conteo/estadisticas');
    esc.escrituras_despues = el.escrituras.slice(antes);
    esc.scroll = main.scrollTop;
    esc.pintado = /REPORTE/.test(el.innerHTML);
  }

  if (escenario === 'tick_lider') {
    js('invSubtab("lider")');
    await responderTodo();
    const el = $('inv-lider-contenido');
    const antes = el.escrituras.length;
    const pintado = el.innerHTML;
    main.scrollTop = 500;
    await avanzar(30000);
    esc.pidio_de_nuevo = pendientes('/api/conteo/lider/tablero').length;
    esc.mientras_espera_igual = el.innerHTML === pintado;
    await responder('/api/conteo/lider/tablero', tablero('REFRESCADO'));
    await responderTodo();
    esc.escrituras_despues = el.escrituras.slice(antes);
    esc.refrescado = /REFRESCADO/.test(el.innerHTML);
    esc.scroll = main.scrollTop;
  }

  if (escenario === 'recargar_cada_pestana') {
    const subtabs = js('Object.keys(INV_SUBTABS)');
    esc.subtabs = subtabs;
    esc.por_pestana = {};
    for (const s of subtabs) {
      const el = $(CONTENIDO[s]);
      if (!el) { esc.por_pestana[s] = { sin_contenedor: true }; continue; }
      js(`invSubtab(${JSON.stringify(s)})`);
      await responderTodo();
      const antes = el.escrituras.length;
      const primero = el.innerHTML;
      js(`invSubtab(${JSON.stringify(s)})`);            // volver a entrar = recargar
      const durante = el.innerHTML;
      await responderTodo();
      esc.por_pestana[s] = {
        pinto_datos: !vacioOCargando(primero),
        durante_igual: durante === primero,
        escrituras: el.escrituras.slice(antes).map(h => vacioOCargando(h) ? 'VACIO' : 'DATOS'),
      };
    }
  }

  if (escenario === 'oculta') {
    js('invSubtab("lider")');
    await responderTodo();
    const el = $('inv-lider-contenido');
    const pintado = el.innerHTML;
    await avanzar(30000);                   // el tick pide el tablero…
    js('invSubtab("estadisticas")');        // …y el líder se va a Estadísticas
    await responder('/api/conteo/lider/tablero', tablero('TARDIO'));
    esc.lider_sin_tocar = el.innerHTML === pintado;
    esc.datos_lider = js('_LIDER_DATOS && _LIDER_DATOS.almacen');
    await responderTodo();
    // Otra pestaña del admin: el tick no pide nada de Inventario.
    const n = pedidos.length;
    js('TAB = "tab-dashboard"');
    await avanzar(30000);
    esc.pedidos_inventario_fuera = pedidos.slice(n).filter(p => !p.url.includes('/definitivos')).length;
    // Navegador en segundo plano: tampoco.
    js('TAB = "tab-inventario"'); js('invSubtab("lider")'); await responderTodo();
    documento.hidden = true;
    const m = cuantos('/api/conteo/lider/tablero');
    await avanzar(30000);
    esc.pedidos_con_hidden = cuantos('/api/conteo/lider/tablero') - m;
  }

  if (escenario === 'vieja_no_pisa') {
    js('invSubtab("lider")');
    await responderTodo();
    $('lider-almacen').value = '1'; js('liderCargar()');
    $('lider-almacen').value = '2'; js('liderCargar()');
    const ps = pendientes('/api/conteo/lider/tablero');
    ps[1].listo = true; ps[1].resolver(tablero('NUEVA')); await drenar();
    ps[0].listo = true; ps[0].resolver(tablero('VIEJA')); await drenar();
    await responderTodo();
    esc.lider = /NUEVA/.test($('inv-lider-contenido').innerHTML) && !/VIEJA/.test($('inv-lider-contenido').innerHTML);
    esc.lider_datos = js('_LIDER_DATOS.almacen');
    // Estadísticas: el tick no pide, pero dos «Actualizar» con filtros distintos sí.
    js('invSubtab("estadisticas")');
    await responderTodo();
    $('ce-clase').value = 'A'; js('conteoEstCargar()');
    $('ce-clase').value = 'B'; js('conteoEstCargar()');
    const es = pendientes('/api/conteo/estadisticas');
    esc.qs = es.map(p => p.url);
    es[1].listo = true; es[1].resolver(estadisticas('FILTRO_B')); await drenar();
    es[0].listo = true; es[0].resolver(estadisticas('FILTRO_A')); await drenar();
    const h = $('inv-estadisticas-contenido').innerHTML;
    esc.estadisticas = /FILTRO_B/.test(h) && !/FILTRO_A/.test(h);
    // Y los filtros elegidos siguen ahí al salir y volver.
    const filtros = $('inv-estadisticas-filtros').escrituras.length;
    js('invSubtab("lider")'); await responderTodo();
    js('invSubtab("estadisticas")');
    esc.filtros_reescritos = $('inv-estadisticas-filtros').escrituras.length - filtros;
    esc.clase_conservada = $('ce-clase').value;
    esc.pide_con_filtro = pendientes('/api/conteo/estadisticas').every(p => p.url.includes('clase=B'));
    await responderTodo();
  }

  if (escenario === 'intervalos') {
    const kardexCorriendo = { en_curso: true, resultado: null };
    js('invSubtab("datos")');
    await responderKardex(kardexCorriendo);
    await responderTodo();
    esc.poll_en_datos = pollsVivos();
    js('invSubtab("lider")'); await responderTodo();
    esc.poll_tras_cambiar_subtab = pollsVivos();
    for (let i = 0; i < 5; i++) {
      js('invSubtab("datos")');
      await responderKardex(kardexCorriendo);
      js('invSubtab("abc")');
      js('invSubtab("datos")');
      await responderKardex(kardexCorriendo);
    }
    await responderTodo();
    esc.poll_tras_idas_y_vueltas = pollsVivos();
    js('tab("tab-dashboard")');
    esc.poll_tras_salir_de_inventario = pollsVivos();
    js('tab("tab-inventario")'); js('invSubtab("datos")');
    await responderKardex(kardexCorriendo);
    await responderTodo();
    js('invSalir()');
    esc.poll_tras_invSalir = pollsVivos();
    // Un sondeo que termina con la pestaña oculta no repinta Datos.
    js('invSubtab("datos")');
    await responderKardex(kardexCorriendo);
    const datos = $('inv-datos-container');
    const antes = datos.escrituras.length;
    js('TAB = "tab-inventario"; _INV_SUBTAB = "lider"');   // oculto sin pasar por invSubtab
    await avanzar(10000);
    await responderKardex({ en_curso: false, resultado: null });
    await responderTodo();
    esc.datos_repintado_oculto = datos.escrituras.length - antes;
  }

  if (escenario === 'falla_con_datos') {
    js('invSubtab("lider")');
    await responderTodo();
    const el = $('inv-lider-contenido');
    const antes = el.escrituras.length;
    await avanzar(30000);
    await fallar('/api/conteo/lider/tablero');
    esc.sigue_lo_visto = /UNO/.test(el.innerHTML);
    esc.declara = /No se pudo actualizar/.test(el.innerHTML);
    esc.alguna_vacia = el.escrituras.slice(antes).some(vacioOCargando);
    // Sin datos previos, el error sí se dice como error.
    $('lider-almacen').value = '2'; js('liderCargar()');
    await fallar('/api/conteo/lider/tablero');
    esc.error_sin_datos = /sin red/.test(el.innerHTML) && !/UNO/.test(el.innerHTML);
    await responderTodo();
  }

  console.log(JSON.stringify(esc));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
"""


def _correr(escenario, pwa=PWA):
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa), escenario, TICK_ADMIN],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope='module')
def resultados():
    return {e: _correr(e) for e in ESCENARIOS}


ESCENARIOS = ('tick_estadisticas', 'tick_lider', 'recargar_cada_pestana', 'oculta',
              'vieja_no_pisa', 'intervalos', 'falla_con_datos')


# ─────────────────────────────────────────────────────────────────────────────
# Las propiedades
# ─────────────────────────────────────────────────────────────────────────────

def _tick_estadisticas(r):
    return r['pintado'] and r['pedidos'] == 1 and r['escrituras_despues'] == [] and r['scroll'] == 500


def _tick_lider(r):
    return (r['pidio_de_nuevo'] == 1 and r['mientras_espera_igual'] and r['refrescado']
            and not any('Cargando' in h or not h.strip() for h in r['escrituras_despues'])
            and r['scroll'] == 500)


def _recargar_cada_pestana(r):
    return all(p.get('pinto_datos') and p.get('durante_igual') and 'VACIO' not in p.get('escrituras', ['VACIO'])
               for p in r['por_pestana'].values())


def _oculta(r):
    return (r['lider_sin_tocar'] and r['datos_lider'] == 'UNO'
            and r['pedidos_inventario_fuera'] == 0 and r['pedidos_con_hidden'] == 0)


def _vieja_no_pisa(r):
    return r['lider'] and r['lider_datos'] == 'NUEVA' and r['estadisticas']


def _intervalos(r):
    return (r['poll_en_datos'] == 1 and r['poll_tras_cambiar_subtab'] == 0
            and r['poll_tras_idas_y_vueltas'] <= 1 and r['poll_tras_salir_de_inventario'] == 0
            and r['poll_tras_invSalir'] == 0 and r['datos_repintado_oculto'] == 0)


def _falla_con_datos(r):
    return r['sigue_lo_visto'] and r['declara'] and not r['alguna_vacia'] and r['error_sin_datos']


class TestElTickNoTocaEstadisticas:
    """El caso reportado: tres ticks de 30 s sobre Estadísticas."""

    def test_no_vuelve_a_pedir_el_reporte(self, resultados):
        assert resultados['tick_estadisticas']['pedidos'] == 1

    def test_no_reescribe_el_panel_ni_mueve_el_scroll(self, resultados):
        r = resultados['tick_estadisticas']
        assert r['pintado']
        assert r['escrituras_despues'] == [], r['escrituras_despues'][:1]
        assert r['scroll'] == 500

    def test_el_arnes_usa_el_timer_real_de_app_js(self):
        assert TICK_ADMIN in (PWA / 'app.js').read_text(encoding='utf-8')


class TestRecargarNoVaciaElPanel:
    """Una pestaña viva (Líder) SÍ se refresca con el tick — sin vaciarse."""

    def test_el_tick_refresca_el_lider_en_su_lugar(self, resultados):
        r = resultados['tick_lider']
        assert r['pidio_de_nuevo'] == 1, 'el Líder es de datos vivos: el tick debe refrescarlo'
        assert r['mientras_espera_igual'], 'mientras llega la respuesta, se sigue viendo lo anterior'
        assert r['refrescado']
        assert not any('Cargando' in h or not h.strip() for h in r['escrituras_despues'])
        assert r['scroll'] == 500

    def test_ninguna_pestana_se_vacia_al_recargar(self, resultados):
        """La clase, en las seis pestañas de Inventario: volver a entrar con los
        mismos parámetros nunca escribe «Cargando…» ni un panel vacío."""
        r = resultados['recargar_cada_pestana']
        assert set(r['subtabs']) == {'lider', 'conteos', 'abc', 'datos', 'estadisticas'}, (
            'Pestaña nueva en INV_SUBTABS: agregale contenedor y respuesta a este arnés')
        for s, p in r['por_pestana'].items():
            assert p.get('pinto_datos'), f'{s}: el arnés no llegó a pintar datos ({p})'
            assert p['durante_igual'], f'{s}: recargar vació el panel antes de la respuesta'
            assert 'VACIO' not in p['escrituras'], f'{s}: {p["escrituras"]}'
            assert p['escrituras'], f'{s}: la recarga no repintó nada'


class TestUnaPestanaOcultaNoSePinta:

    def test_la_respuesta_que_llega_tarde_a_una_pestana_oculta_no_pinta(self, resultados):
        r = resultados['oculta']
        assert r['lider_sin_tocar']
        assert r['datos_lider'] == 'UNO', 'los botones del tablero leerían una fila que no se ve'

    def test_en_otra_pestana_del_admin_el_tick_no_pide_inventario(self, resultados):
        assert resultados['oculta']['pedidos_inventario_fuera'] == 0

    def test_con_el_navegador_en_segundo_plano_no_pide(self, resultados):
        assert resultados['oculta']['pedidos_con_hidden'] == 0


class TestLaRespuestaViejaNoPisa:

    def test_lider(self, resultados):
        r = resultados['vieja_no_pisa']
        assert r['lider'] and r['lider_datos'] == 'NUEVA'

    def test_estadisticas_con_filtros_distintos(self, resultados):
        r = resultados['vieja_no_pisa']
        assert any('clase=A' in u for u in r['qs']) and any('clase=B' in u for u in r['qs'])
        assert r['estadisticas']

    def test_los_filtros_se_conservan(self, resultados):
        r = resultados['vieja_no_pisa']
        assert r['filtros_reescritos'] == 0
        assert r['clase_conservada'] == 'B'
        assert r['pide_con_filtro']


class TestLosIntervalosSeLimpianAlSalir:

    def test_el_sondeo_del_kardex_vive_solo_en_datos(self, resultados):
        r = resultados['intervalos']
        assert r['poll_en_datos'] == 1
        assert r['poll_tras_cambiar_subtab'] == 0

    def test_entrar_y_salir_no_acumula(self, resultados):
        assert resultados['intervalos']['poll_tras_idas_y_vueltas'] <= 1

    def test_salir_de_inventario_lo_apaga(self, resultados):
        r = resultados['intervalos']
        assert r['poll_tras_salir_de_inventario'] == 0
        assert r['poll_tras_invSalir'] == 0

    def test_el_sondeo_que_termina_con_datos_oculto_no_repinta(self, resultados):
        assert resultados['intervalos']['datos_repintado_oculto'] == 0

    def test_pararTimers_apaga_lo_de_inventario(self):
        app = (PWA / 'app.js').read_text(encoding='utf-8')
        cuerpo = app[app.index('function pararTimers()'):]
        cuerpo = cuerpo[:cuerpo.index('\n}\n')]
        assert 'invSalir()' in cuerpo


class TestSiElRefrescoFallaLoVistoSeQueda:

    def test_los_datos_se_quedan_y_se_declara(self, resultados):
        r = resultados['falla_con_datos']
        assert r['sigue_lo_visto'] and r['declara']
        assert not r['alguna_vacia']

    def test_sin_datos_previos_el_error_se_dice(self, resultados):
        assert resultados['falla_con_datos']['error_sin_datos']


# ─────────────────────────────────────────────────────────────────────────────
# Meta: cada regla, rota en una copia del PWA, pone rojo su escenario
# ─────────────────────────────────────────────────────────────────────────────

# (archivo, texto a quitar, reemplazo, escenario que debe caer, propiedad)
MUTACIONES = {
    'el_tick_vuelve_a_invSubtab': (
        'conteo.js', '  if (desdeTimer) return invRefrescoPeriodico();\n', '', 'tick_estadisticas', _tick_estadisticas),
    'estadisticas_viva': (
        'conteo.js', "panel: 'inv-panel-estadisticas', vivo: false", "panel: 'inv-panel-estadisticas', vivo: true",
        'tick_estadisticas', _tick_estadisticas),
    'siempre_cargando': (
        'conteo.js', 'const conDatos = est.el === el && est.html !== null && est.params === params;',
        'const conDatos = false;', 'recargar_cada_pestana', _recargar_cada_pestana),
    'tick_lider_con_cargando': (
        'conteo.js', 'const conDatos = est.el === el && est.html !== null && est.params === params;',
        'const conDatos = false;', 'tick_lider', _tick_lider),
    'sin_numero_de_carga': (
        'conteo.js', '  if (mio !== est.seq) return false;           // llegó después de una más nueva\n', '',
        'vieja_no_pisa', _vieja_no_pisa),
    'pinta_oculta': (
        'conteo.js', '  if (!invPanelVisible(o.subtab)) return false; // oculta: al volver, recarga\n', '',
        'oculta', _oculta),
    'tick_ignora_visibilidad': (
        'conteo.js', "  if (typeof document !== 'undefined' && document.hidden) return;\n", '',
        'oculta', _oculta),
    'invSubtab_no_limpia': (
        'conteo.js', '  invLimpiarIntervalos(nombre);\n', '', 'intervalos', _intervalos),
    'tab_no_sale_de_inventario': (
        'app.js', "  if (TAB === 'tab-inventario' && id !== 'tab-inventario' && typeof invSalir === 'function') invSalir();\n",
        '', 'intervalos', _intervalos),
    'error_vacia_lo_visto': (
        'conteo.js', '    if (conDatos) el.innerHTML = _invAvisoViejo(err) + est.html;\n    else {',
        '    {', 'falla_con_datos', _falla_con_datos),
}


class TestLasMutacionesMuerden:

    def test_sano_cumple_todas(self, resultados):
        for nombre, prop in (('tick_estadisticas', _tick_estadisticas), ('tick_lider', _tick_lider),
                             ('recargar_cada_pestana', _recargar_cada_pestana), ('oculta', _oculta),
                             ('vieja_no_pisa', _vieja_no_pisa), ('intervalos', _intervalos),
                             ('falla_con_datos', _falla_con_datos)):
            assert prop(resultados[nombre]), (nombre, resultados[nombre])

    @pytest.mark.parametrize('mutacion', sorted(MUTACIONES))
    def test_la_mutacion_pone_rojo(self, mutacion, tmp_path):
        archivo, quitar, poner, escenario, prop = MUTACIONES[mutacion]
        copia = tmp_path / 'pwa'
        shutil.copytree(PWA, copia, ignore=shutil.ignore_patterns('*.png', '*.jpg', '*.svg', 'vendor'))
        src = (copia / archivo).read_text(encoding='utf-8')
        assert src.count(quitar) == 1, f'la mutación {mutacion} ya no encuentra su texto: actualizala'
        (copia / archivo).write_text(src.replace(quitar, poner), encoding='utf-8')
        assert quitar not in (copia / archivo).read_text(encoding='utf-8')
        if not shutil.which('node'):
            pytest.skip('sin node')
        r = subprocess.run(['node', '-e', _ARNES, '--', str(copia), escenario, TICK_ADMIN],
                           capture_output=True, text=True, timeout=60)
        rompio = r.returncode != 0 or not prop(json.loads(r.stdout.strip().splitlines()[-1]))
        assert rompio, f'{mutacion}: el escenario {escenario} siguió verde con la regla rota'

    def test_piso_de_mutaciones(self):
        assert len(MUTACIONES) >= 10
