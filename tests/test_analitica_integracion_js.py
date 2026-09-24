"""Las vistas de 📈 Analítica se hablan entre sí: de una fuga al recorrido de su
pedido. Cada agente escribió su vista por separado; esto prueba el cable.

`anFugasVerRecorrido(i)` → `anRecorridoAbrir(clave)` → abre la sub-pestaña
🧭 Recorrido (`anSubtab` devuelve la carga) y, ya pintada, pide la línea de
tiempo `GET /api/analitica/recorrido/pedido/<clave>`.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '', dataset: {},
  classList: { toggle() {} }, scrollIntoView() {} });
const pedidos = [];
const ctx = { console, window: {}, localStorage: { getItem: () => null, setItem() {} },
  document: { getElementById: el },
  get: (url) => { pedidos.push(url); return Promise.resolve({}); } };
vm.createContext(ctx);
for (const f of ['util.js', 'analitica.js', 'analitica_recorrido.js', 'analitica_fugas.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
// El recorrido completo no es lo que se prueba acá: solo que el cable llegue.
vm.runInContext(`anRecorridoCargar = () => Promise.resolve(); anRecPedidoHtml = () => 'LINEA';`, ctx);
vm.runInContext(`AN_FUGAS.detalle = { casos: [{ pedido_clave: '003-PD-1502' }, { pedido_clave: null }] };`, ctx);
(async () => {
  vm.runInContext('anFugasVerRecorrido(0)', ctx);
  vm.runInContext('anFugasVerRecorrido(1)', ctx);
  await new Promise(r => setTimeout(r, 30));
  console.log(JSON.stringify({ pedidos, subtab: vm.runInContext('_AN_SUBTAB', ctx),
    detalle: (els['an-rec-detalle'] || {}).innerHTML || '' }));
})();
"""


def test_de_una_fuga_al_recorrido_de_su_pedido():
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(RAIZ / 'app' / 'static' / 'pwa')],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert d['subtab'] == 'recorrido'
    assert d['pedidos'] == ['/api/analitica/recorrido/pedido/003-PD-1502'], d['pedidos']
    assert d['detalle'] == 'LINEA'
