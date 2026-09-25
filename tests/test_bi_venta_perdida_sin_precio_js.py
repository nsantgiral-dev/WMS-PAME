"""El tablero BI no pinta $0 por un agotado sin precio de venta.

El servidor ya no suma los agotados sin precio y lo declara (`sin_precio`,
`total_es_cota_inferior`; ver `metricas/venta_perdida.py`). La pantalla
multiplicaba `cantidad × precio_venta_capturado` y un `null` salía «$0».
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
const ctx = { console, window: {}, document: { getElementById: () => null } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/tablero_bi.js', 'utf8'), ctx);
const f = vm.runInContext('_biSinPrecioHtml', ctx);
console.log(JSON.stringify({
  con: f({ sin_precio: { eventos: 3, unidades: '<b>7</b>' } }),
  sin: f({ sin_precio: { eventos: 0, unidades: 0 } }),
  nada: f({}),
}));
"""


def test_el_total_se_declara_piso_si_faltan_precios():
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(RAIZ / 'app' / 'static' / 'pwa')],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'piso' in d['con'] and '3 agotado' in d['con']
    assert '&lt;b&gt;7&lt;/b&gt;' in d['con'], 'las unidades van escapadas'
    assert d['sin'] == '' and d['nada'] == ''


def test_la_fila_sin_precio_no_dice_cero():
    src = (RAIZ / 'app' / 'static' / 'pwa' / 'tablero_bi.js').read_text(encoding='utf-8')
    # La fila sin precio dice «sin precio» (en gris desde 2026-09-24), nunca un monto.
    assert "ev.precio_venta_capturado == null ? '<span style=\"color:var(--tx3);font-weight:400;\">sin precio</span>'" in src
