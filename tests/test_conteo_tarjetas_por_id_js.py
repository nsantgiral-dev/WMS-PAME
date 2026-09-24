"""Las tarjetas de Conteos pasan el id, no el JSON de la sesión, y la cantidad
se deshabilita con el motivo del servidor.

## El defecto

`conteoAbrirEdicion(${JSON.stringify(s).replace(/"/g,'&quot;')})` y su gemelo
de ajuste metían la sesión entera en el `onclick`. El navegador decodifica las
entidades del atributo ANTES de correr el JS: un dato que ya traía `&quot;`
(nombre de producto de Siesa, motivo de edición escrito por un admin) volvía a
ser `"`, cerraba la cadena del JSON y lo que siguiera se ejecutaba.

Y el modal de edición decidía en JS qué estados bloqueaban la cantidad
(`AJUSTADO || CANCELADO`), una copia que no coincidía con el servidor. Ahora
muestra `no_se_corrige_cantidad`, que sale de la misma función que lo niega.

El arnés corre `conteo.js` con `util.js` real en Node, toma cada `onclick` del
HTML generado, le decodifica las entidades como el navegador y lo ejecuta.
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
const elementos = {};
const el = (id) => (elementos[id] = elementos[id] || { id, style: {}, value: '', disabled: false, innerHTML: '', textContent: '', title: '' });
const ctx = { console, window: {}, globalThis: {}, document: { getElementById: el } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
const MALO = 'x&quot;});globalThis.PWNED=1;({&quot;a&quot;:&quot;';
const s = { id: 41, codigo: 'CC1-X', estado: 'DESCUADRE', producto_codigo: MALO, producto_nombre: MALO,
  ubicacion_codigo: MALO, motivo_edicion: MALO, editado_en: '2026-09-23', cantidad_fisica: 7,
  existencia_siesa: 10, diferencia: -3, bodega_siesa_id: 'NB1', segundo_conteo: null,
  no_se_corrige_cantidad: null };
const p = { ...s, id: 42, estado: 'PENDIENTE', cantidad_fisica: null, no_se_corrige_cantidad: 'Un conteo en PENDIENTE <b>no</b>' };
const abiertos = [];
Object.assign(ctx, { __abiertos: abiertos });
vm.runInContext('__origE = conteoAbrirEdicion; conteoAbrirEdicion = (x) => __abiertos.push(["editar", x]); conteoAbrirAjuste = (x) => __abiertos.push(["ajustar", x]);', ctx);
const html = vm.runInContext('_renderCardAccion', ctx)(s) + vm.runInContext('_renderCardProgreso', ctx)(p);
const dec = (t) => t.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const onclicks = [...html.matchAll(/onclick="([^"]*)"/g)].map(m => dec(m[1]));
const deApertura = onclicks.filter(o => /conteoAbrir/.test(o));
for (const o of deApertura) vm.runInContext(o, ctx);
console.log(JSON.stringify({
  deApertura,
  pwned: ctx.globalThis.PWNED === 1 || ctx.PWNED === 1,
  abiertos: abiertos.map(([k, x]) => [k, x.id, x.producto_nombre === MALO]),
  datoEnOnclick: deApertura.some(o => o.includes('x"') || o.includes('producto')),
}));
// El modal de edición: la cantidad la habilita el servidor.
const abrir = vm.runInContext('__origE', ctx);
abrir(p);
const bloqueado = { disabled: el('conteo-edit-cantidad').disabled, info: el('conteo-edit-info').innerHTML };
abrir({ ...s, estado: 'CANCELADO' });
const libre = { disabled: el('conteo-edit-cantidad').disabled };
console.log(JSON.stringify({ bloqueado, libre }));
"""


def _correr():
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    lineas = r.stdout.strip().splitlines()
    return json.loads(lineas[-2]), json.loads(lineas[-1])


def test_los_botones_llevan_solo_el_id():
    tarjetas, _ = _correr()
    assert tarjetas['deApertura'], 'el arnés no encontró los botones'
    assert not tarjetas['datoEnOnclick'], tarjetas['deApertura']
    assert not tarjetas['pwned']


def test_abren_la_misma_sesion_con_el_dato_intacto():
    tarjetas, _ = _correr()
    assert ['editar', 41, True] in tarjetas['abiertos']
    assert ['editar', 42, True] in tarjetas['abiertos']


def test_la_cantidad_la_habilita_el_servidor():
    """El JS ya no decide por estado: un CANCELADO sin motivo del servidor
    queda editable en la pantalla (el servidor igual lo rechaza si cambia),
    y un PENDIENTE con motivo queda bloqueado mostrando el porqué, escapado."""
    _, modal = _correr()
    assert modal['bloqueado']['disabled'] is True
    assert 'PENDIENTE &lt;b&gt;no&lt;/b&gt;' in modal['bloqueado']['info']
    assert modal['libre']['disabled'] is False
