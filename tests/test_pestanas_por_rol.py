"""Una pestaña que el shell le muestra a un rol carga sin que el servidor se la niegue.

**Qué pasaba** (validación e2e 2026-09-26): el gerente entraba al shell de
admin con TODAS las pestañas (`app.js`: `esAdmin` incluye `gerente`; solo se
le escondía ⛔ Cartera). Muelle decía «Sin permiso» y se re-pedía cada 8 s,
Bodega salía en error, Siesa hacía 7 lecturas en 403, Reposición 403. Jefe de
almacén y supervisor: Siesa con 5-6 lecturas en 403. Y la cola del conteo
definitivo (`actualizarBadgeDefinitivos`, en cada tick de 30 s y en toda
pestaña) daba 403 a gerente, liquidador, líder de cartera y control de flota.

**La clase:** una pestaña visible cuya carga el servidor niega. Se escondía
con listas escritas a ojo (`_TABS_OCULTAS_SUPERVISOR`, solo para un rol) y
`test_permisos_por_pantalla` declara a mano qué rol abre qué módulo —declaraba
solo `admin` para casi todos—, así que nadie cruzaba lo que el shell muestra
contra lo que el servidor deja leer.

**Ahora:** `_TABS_OCULTAS_POR_ROL` en `app.js`, un solo lugar para todos los
roles de gestión. **El trinquete es medido, no declarado:** para cada rol que
entra al shell de admin, Node corre `mostrarSegunRol` con el `app.js` real y el
SHELL de `sw.js` cargado entero, toma las pestañas que quedan visibles, entra a
cada una con `cargarAdmin()` y registra cada GET. Después cada URL se pide con
el token de ese rol: **cero 403**. Una pestaña nueva, un GET nuevo o un guard
que se estrecha entran solos. Y un sondeo que recibe 403 se detiene (Muelle).

**Lo que NO mide:** los GET que dependen de un clic dentro de la pestaña
(sub-pestañas, abrir un detalle, p. ej. «Jobs» de Reposición para el gerente);
los que se arman con un id que en la base vacía no existe (dan 404, no 403).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_cartera_retencion import _jwt

PWA = Path(__file__).resolve().parent.parent / 'app' / 'static' / 'pwa'

#: Todo rol que entra al shell de admin (`mostrarSegunRol`: `esAdmin`).
ROLES_SHELL = ('admin', 'gerente', 'jefe_almacen', 'supervisor',
               'liquidador', 'lider_cartera', 'control_flota')

_ARNES = r"""
// Uso: node arnes.js <pwa_dir> <rol>  → {visibles, gets: {tab: [url]}, errores}
const fs = require('fs'), vm = require('vm');
const [PWA, ROL, ALM] = process.argv.slice(1).filter(a => a !== '--');
process.on('unhandledRejection', () => {}); process.on('uncaughtException', () => {});
const sw = fs.readFileSync(PWA + '/sw.js', 'utf8');
const SHELL = [...sw.match(/const SHELL = \[([^\]]+)\]/)[1].matchAll(/'\/static\/pwa\/([a-z_]+\.js)'/g)].map(m => m[1]);
const html = fs.readFileSync(PWA + '/index.html', 'utf8');
const navs = [...html.matchAll(/<div[^>]*class="nav-tab[^"]*"[^>]*onclick="([^"]*)"[^>]*>/g)]
  .map(m => ({ onclick: m[1], style: {}, classList: { toggle() {}, add() {}, remove() {} },
               getAttribute(k) { return k === 'onclick' ? this.onclick : null; } }));
function el(id) {
  return { id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, className: '', disabled: false, checked: false,
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, removeEventListener() {},
    appendChild(c) { return c; }, setAttribute() {}, getAttribute() { return null; }, removeAttribute() {}, remove() {},
    focus() {}, blur() {}, click() {}, insertBefore() {}, scrollIntoView() {}, closest() { return null; }, contains() { return false; },
    insertAdjacentHTML() {}, getBoundingClientRect() { return { top: 0, left: 0, width: 360, height: 10 }; },
    get parentNode() { return el('p'); }, get parentElement() { return el('p'); }, get children() { return []; },
    getContext() { return null; }, options: [], files: [] };
}
const els = {};
const doc = { body: el('body'), head: el('head'), documentElement: el('html'),
  getElementById(id) { if (!(id in els)) els[id] = el(id); return els[id]; },
  querySelector() { return null; },
  querySelectorAll(sel) {
    if (sel === '.nav-tab') return navs;
    if (sel.startsWith('.nav-tab[onclick*="')) return navs.filter(n => n.onclick.includes(sel.split('"')[1]));
    return [];
  },
  getElementsByClassName() { return []; }, addEventListener() {},
  createElement() { return el('n'); }, createTextNode(t) { return { textContent: t }; } };
let llamadas = [];
const reg = (m, u) => llamadas.push({ metodo: m, url: String(u).replace(/^https?:\/\/[^/]+/, '') });
const ctx = {
  console: { log() {}, info() {}, debug() {}, warn() {}, error() {} },
  document: doc, window: { location: { origin: 'http://t', hash: '', href: 'http://t/', search: '' }, addEventListener() {}, scrollTo() {},
    matchMedia: () => ({ matches: false, addEventListener() {} }), innerWidth: 1200 },
  navigator: { onLine: true, serviceWorker: null }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, requestAnimationFrame: () => 0,
  setImmediate, queueMicrotask, AbortController, URLSearchParams, Intl, Date, Math, JSON, Promise, Blob: class {}, URL,
  fetch: async (u, o) => { reg((o && o.method) || 'GET', u); return { ok: true, status: 200, json: async () => ({}), text: async () => '{}', blob: async () => ({}), headers: { get: () => null } }; },
  alert() {}, confirm: () => true, prompt: () => 'x', Chart: function () { return { destroy() {}, update() {} }; },
  __reg: reg,
};
ctx.globalThis = ctx; ctx.window.document = doc; ctx.self = ctx.window; ctx.location = ctx.window.location;
vm.createContext(ctx);
const errores = [];
for (const a of SHELL) {
  try { vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf8'), ctx, { filename: a }); }
  catch (e) { errores.push('carga ' + a + ': ' + String(e).slice(0, 200)); }
}
vm.runInContext(`
  get = async (u) => { __reg('GET', u); return {}; };
  post = async (u) => { __reg('POST', u); return {}; };
  put = async (u) => { __reg('PUT', u); return {}; };
  del = async (u) => { __reg('DELETE', u); return {}; };
  alerta = () => {}; pantalla = () => {}; actualizarUI = () => {};
  OPERARIO = { rol: ${JSON.stringify(ROL)}, nombre: 'x', almacen_id: ${ALM}, almacen_bodega_siesa_id: 'NB1' };
  TOKEN = 't'; ALMACEN_ID = ${ALM};
  __cargarAdmin = cargarAdmin; cargarAdmin = async () => {};
`, ctx);
(async () => {
  try { vm.runInContext(`mostrarSegunRol(${JSON.stringify(ROL)})`, ctx); } catch (e) { errores.push('mostrarSegunRol: ' + String(e).slice(0, 200)); }
  const visibles = navs.filter(n => n.style.display !== 'none').map(n => (n.onclick.match(/tab\('([^']+)'\)/) || [])[1]).filter(Boolean);
  const gets = {};
  for (const t of visibles) {
    llamadas = [];
    try { await vm.runInContext(`(async () => { TAB = ${JSON.stringify(t)}; await __cargarAdmin(); })()`, ctx); }
    catch (e) { errores.push(t + ': ' + String(e).slice(0, 160)); }
    for (let i = 0; i < 8; i++) await new Promise((r) => setImmediate(r));
    gets[t] = [...new Set(llamadas.filter((l) => l.metodo === 'GET').map((l) => l.url))];
  }
  process.stdout.write(JSON.stringify({ visibles, gets, errores }));
})();
"""


def pestanas(rol, almacen_id=1, pwa=PWA):
    """{visibles, gets: {pestaña: [url]}, errores} de `app.js` real."""
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    p = subprocess.run(['node', '-e', _ARNES, '--', str(pwa), rol, str(almacen_id)],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _usuario_de(db, rol, almacen):
    from app.models.usuario import Usuario
    u = Usuario(email=f'{rol}@pestanas.test', nombre=rol, rol=rol, activo=True, almacen_id=almacen.id)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def negadas(app, client, db, almacen, rol, pwa=PWA):
    d = pestanas(rol, almacen.id, pwa)
    assert not d['errores'], d['errores']
    cab = _jwt(app, _usuario_de(db, rol, almacen))
    out = []
    for tab, urls in d['gets'].items():
        for url in urls:
            if client.get(url, headers=cab, follow_redirects=True).status_code == 403:
                out.append((tab, url))
    return d, out


class TestNingunaPestanaVisibleCargaEn403:

    @pytest.mark.parametrize('rol', ROLES_SHELL)
    def test_cero_403(self, app, client, db, almacen, rol):
        d, neg = negadas(app, client, db, almacen, rol)
        assert d['visibles'], rol
        assert not neg, f'{rol} ve pestañas cuya carga el servidor le niega: {neg}'


class TestElTrinqueteSeMide:
    """Un arnés que se desincroniza devuelve cero pestañas y cero GET, y un
    cero se lee igual que «todo carga»."""

    def test_piso_el_admin_ve_y_carga(self, almacen):
        d = pestanas('admin', almacen.id)
        assert not d['errores'], d['errores']
        assert len(d['visibles']) >= 15, d['visibles']
        assert sum(len(v) for v in d['gets'].values()) >= 40, d['gets']
        assert '/api/muelle/listos' in d['gets']['tab-muelle']

    def test_cada_rol_de_plata_ve_solo_lo_suyo(self, almacen):
        assert pestanas('liquidador', almacen.id)['visibles'] == ['tab-liquidacion']
        assert pestanas('control_flota', almacen.id)['visibles'] == ['tab-flota']

    @pytest.mark.parametrize('rol,linea', [
        ('gerente', "  gerente:      ['tab-bodega', 'tab-connekta', 'tab-muelle'],\n"),
        ('jefe_almacen', "  jefe_almacen: ['tab-connekta'],\n"),
    ])
    def test_sin_su_fila_el_trinquete_muerde(self, app, client, db, almacen, tmp_path, rol, linea):
        """Mutación: se le quita a `app.js` (copia en tmp) la fila del rol y el
        trinquete tiene que ver los 403 que la fila evitaba."""
        copia = tmp_path / 'pwa'
        shutil.copytree(PWA, copia, ignore=shutil.ignore_patterns('*.png', '*.jpg', '*.svg', '*.woff*'))
        src = (copia / 'app.js').read_text(encoding='utf-8')
        assert src.count(linea) == 1, 'la fila cambió: actualice la mutación'
        (copia / 'app.js').write_text(src.replace(linea, ''), encoding='utf-8')
        _, neg = negadas(app, client, db, almacen, rol, pwa=copia)
        assert any(t == 'tab-connekta' for t, _ in neg), neg

    def test_roles_supervision_es_el_del_servidor(self):
        from app.routes._auth_helpers import Roles
        js = (PWA / 'app.js').read_text(encoding='utf-8')
        m = re.search(r"const _ROLES_SUPERVISION = \[([^\]]+)\]", js)
        assert m, 'falta _ROLES_SUPERVISION en app.js'
        assert sorted(re.findall(r"'([a-z_]+)'", m.group(1))) == sorted(Roles.SUPERVISION)


class TestUnSondeoQueRecibe403SeDetiene:

    def _muelle(self, tmp_path, status):
        from tests.test_sin_codigos_en_pantalla import _node
        js = f"""
          let n = 0;
          setTimeout = () => {{ n++; return 7; }};
          TAB = 'tab-muelle'; MUELLE_TIMER = null; RUTA_ACTIVA_ID = null;
          cargarRutaSelector = async () => {{}};
          cargarMuelleSinRuta = async () => {{
            const e = new Error('Sin permiso'); e.status = {status}; throw e; }};
          await cargarMuelle();
          return {{n, html: document.getElementById('lista-muelle').innerHTML}};
        """
        return _node(tmp_path, ['util.js', 'rutas.js'], {}, js)

    def test_con_403_no_se_vuelve_a_pedir(self, tmp_path):
        out = self._muelle(tmp_path, 403)
        assert out['n'] == 0, out
        assert 'Sin permiso' in out['html']

    def test_con_otro_error_sigue_reintentando(self, tmp_path):
        assert self._muelle(tmp_path, 500)['n'] == 1
