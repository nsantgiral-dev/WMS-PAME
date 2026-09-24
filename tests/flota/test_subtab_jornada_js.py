"""La pestaña 🕒 Jornada de Flota está cableada: `flotaSubtab('jornada')`
esconde la bandeja y la analítica, muestra `#flota-jornada` y pide el resumen
de la jornada. Lo escribieron dos agentes por separado; esto prueba el cable."""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[2]

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '', dataset: {},
  classList: { toggle() {}, add() {}, remove() {} }, addEventListener() {} });
const pedidos = [];
const ctx = { console, window: {}, localStorage: { getItem: () => null, setItem() {} },
  document: { getElementById: el, createElement: () => el('_x'), querySelectorAll: () => [] },
  OPERARIO: { rol: 'control_flota' }, setTimeout, clearTimeout,
  get: (url) => { pedidos.push(url); return Promise.resolve({ conductores: [], desde: '2026-09-17', hasta: '2026-09-23' }); },
  flotaBandejaCargar: () => { pedidos.push('BANDEJA'); return Promise.resolve(); },
  flotaCargarAnalitica: () => { pedidos.push('ANALITICA'); return Promise.resolve(); } };
vm.createContext(ctx);
for (const f of ['util.js', 'flota_analitica.js', 'flota_jornada.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
// La bandeja y la analítica reales no son lo que se prueba acá.
vm.runInContext(`flotaBandejaCargar = () => { get('BANDEJA'); return Promise.resolve(); };
                 flotaCargarAnalitica = () => { get('ANALITICA'); return Promise.resolve(); };`, ctx);
(async () => {
  await vm.runInContext("flotaSubtab('jornada')", ctx);
  await new Promise(r => setTimeout(r, 30));
  console.log(JSON.stringify({ pedidos,
    jornada: (els['flota-jornada'] || {}).style, contenido: (els['flota-contenido'] || {}).style,
    analitica: (els['flota-analitica'] || {}).style }));
})();
"""


def test_la_pestana_jornada_pide_el_resumen_y_se_muestra_sola():
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(RAIZ / 'app' / 'static' / 'pwa')],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert any(u.startswith('/api/jornada/resumen') for u in d['pedidos']), d['pedidos']
    assert 'BANDEJA' not in d['pedidos'] and 'ANALITICA' not in d['pedidos']
    assert d['jornada'].get('display') == 'block'
    assert d['contenido'].get('display') == 'none'
    assert d['analitica'].get('display') == 'none'


def test_la_pestana_esta_en_el_html():
    html = (RAIZ / 'app' / 'static' / 'pwa' / 'index.html').read_text(encoding='utf-8')
    assert 'onclick="flotaSubtab(\'jornada\')"' in html
    assert 'id="flota-jornada"' in html
