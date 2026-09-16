"""La fila de recepción, EJECUTADA — no leída.

El bloque de avería se pinta dentro de un template anidado en `${...}`, que es
exactamente la forma que un detector de texto sobre el archivo lee mal. Acá el
`recepcion.js` real corre en Node y se mira lo que quedó en el DOM.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const ITEMS = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const ctx = {
  console,
  document: { getElementById: () => null, querySelector: () => null,
              querySelectorAll: () => [], addEventListener() {},
              createElement: () => ({ style: {} }) },
  window: { location: { origin: 'http://test' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/recepcion.js', 'utf-8'), ctx);
if (typeof ctx.renderItemsRecepcion !== 'function') {
  throw new Error('no existe renderItemsRecepcion');
}
process.stdout.write(ctx.renderItemsRecepcion(ITEMS));
"""


def _render(tmp_path, items):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'; h.write_text(HARNESS, encoding='utf-8')
    d = tmp_path / 'items.json'; d.write_text(json.dumps(items), encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(PWA), str(d)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'el render reventó:\n{proc.stderr}'
    return proc.stdout


def _item(**kw):
    base = {'producto_id': 7, 'producto_codigo': 'PROD-001',
            'producto_nombre': 'Resma Carta', 'cantidad_ordenada': 100,
            'cantidad_recibida': 100, 'cantidad_averiada': 0,
            'motivo_averia': None, 'tipo': 'OC', 'destino': 'INVENTARIO'}
    base.update(kw); return [base]


def test_la_averia_se_pinta_con_su_motivo(tmp_path):
    html = _render(tmp_path, _item(cantidad_averiada=20,
                                   motivo_averia='Cajas mojadas'))
    assert '20 averiada' in html, html
    assert 'Cajas mojadas' in html
    assert 'zona de averías' in html


def test_sin_averia_no_se_pinta_el_bloque(tmp_path):
    """Dirección contraria: no puede ensuciar toda la lista con «0 averiadas»."""
    html = _render(tmp_path, _item(cantidad_averiada=0))
    assert 'averiada' not in html.replace('Declarar avería', '')


def test_el_boton_aparece_solo_con_algo_contado(tmp_path):
    con = _render(tmp_path, _item(cantidad_recibida=10))
    sin = _render(tmp_path, _item(cantidad_recibida=0))
    assert 'recepAbrirAveria' in con
    assert 'recepAbrirAveria' not in sin, (
        'ofrece declarar avería sobre algo que todavía no se contó')


def test_el_motivo_va_escapado(tmp_path):
    """Texto libre que escribe el operario, pintado dentro de un template
    anidado — el sitio exacto donde el escapado se pierde sin que nadie note."""
    html = _render(tmp_path, _item(
        cantidad_averiada=3,
        motivo_averia='<img src=x onerror=alert(1)>'))
    assert '<img src=x' not in html, html
    assert '&lt;img' in html
