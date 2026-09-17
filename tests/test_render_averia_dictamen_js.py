"""La tarjeta del traslado, EJECUTADA — no leída.

El bloque de dictamen se pinta dentro de un template anidado en `${...}` y su
condición es `s.averia_veredicto === null`, no `!s.averia_veredicto`. Un
detector de texto sobre el archivo no distingue una de otra, y la diferencia
es la que importa: con truthiness, un veredicto `false` («no estaba averiada»,
una decisión tomada) volvería a pedir el dictamen de algo ya resuelto.

Acá corre el `traslados.js` real en Node y se mira el HTML que quedó.

**Punto ciego declarado:** si `node` no está en el entorno, esto se salta y sale
verde sin haber corrido nada. Es el mismo trato que `test_render_recepcion_averia_js.py`
y existe porque el build de Railway no tiene Node. Un `-rs` en la corrida lo
muestra; en local, donde sí hay Node, corre de verdad.
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
const S = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
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
vm.runInContext(fs.readFileSync(process.argv[2] + '/traslados.js', 'utf-8'), ctx);
if (typeof ctx._renderTrasladoCard !== 'function') {
  throw new Error('no existe _renderTrasladoCard');
}
process.stdout.write(ctx._renderTrasladoCard(S));
"""


def _render(tmp_path, solicitud):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS)
    d = tmp_path / 's.json'
    d.write_text(json.dumps(solicitud))
    r = subprocess.run(['node', str(h), str(PWA), str(d)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _solicitud(**kw):
    base = {
        'id': 7, 'codigo': 'ST-AVE-7', 'estado': 'ENTREGADA',
        'bodega_origen_siesa': 'NC1', 'bodega_destino_siesa': 'NB1',
        'nombre_punto_venta': 'Neiva Centro', 'fecha_creacion': None,
        'items': [], 'total_items': 0, 'siesa_error': None,
        'es_averia': True, 'averia_evidencia': 'fotos del punto',
        'averia_veredicto': None, 'averia_veredicto_nombre': None,
        'averia_veredicto_nota': None, 'picking_progreso': None,
        'operario_nombre': None, 'modo_transferencia': 'EN_TRANSITO',
        'siesa_salida_consec': 1, 'siesa_entrada_consec': 2,
    }
    base.update(kw)
    return base


def test_sin_dictaminar_pide_el_dictamen(tmp_path):
    html = _render(tmp_path, _solicitud())
    assert 'Avería sin dictaminar' in html
    assert 'trasDictaminarAveria(7, true)' in html
    assert 'trasDictaminarAveria(7, false)' in html


def test_muestra_la_evidencia_que_dejo_el_punto(tmp_path):
    """Quien dictamina no estuvo en el punto. Sin la evidencia a la vista,
    decide sobre nada."""
    html = _render(tmp_path, _solicitud())
    assert 'fotos del punto' in html


def test_un_veredicto_FALSE_no_vuelve_a_pedir_el_dictamen(tmp_path):
    """EL test de este archivo.

    `false` es una decisión tomada: alguien miró y dijo que no estaba averiada.
    Con `if (!s.averia_veredicto)` el bloque volvería a aparecer y cualquiera
    podría «dictaminarla» de nuevo — sobre algo ya resuelto, y contra un
    backend que lo va a rechazar por congelado. La pantalla estaría ofreciendo
    una acción imposible.
    """
    html = _render(tmp_path, _solicitud(averia_veredicto=False,
                                        averia_veredicto_nombre='Supervisor CD',
                                        averia_veredicto_nota='llegó sellada'))
    assert 'Avería sin dictaminar' not in html
    assert 'trasDictaminarAveria' not in html
    assert 'NO averiada' in html
    assert 'llegó sellada' in html


def test_un_veredicto_TRUE_muestra_el_resultado_y_no_los_botones(tmp_path):
    html = _render(tmp_path, _solicitud(averia_veredicto=True,
                                        averia_veredicto_nombre='Supervisor CD'))
    assert 'trasDictaminarAveria' not in html
    assert 'Confirmada como averiada' in html
    assert 'Supervisor CD' in html


def test_antes_de_llegar_no_se_ofrece_dictaminar(tmp_path):
    """Dictaminar antes de recibir sería opinar sobre mercancía que todavía no
    se contó. El backend lo rechaza; la pantalla no debería ni ofrecerlo."""
    for estado in ('BORRADOR', 'ENVIADA', 'EN_PICKING', 'EN_TRANSITO'):
        html = _render(tmp_path, _solicitud(estado=estado))
        assert 'trasDictaminarAveria' not in html, estado


def test_un_traslado_normal_no_muestra_nada_de_averias(tmp_path):
    """La otra dirección del detector: el bloque no puede aparecer donde no
    corresponde. Son 174 traslados normales en producción contra 0 averías."""
    html = _render(tmp_path, _solicitud(es_averia=False, averia_evidencia=None))
    assert 'Avería' not in html
    assert 'trasDictaminarAveria' not in html
