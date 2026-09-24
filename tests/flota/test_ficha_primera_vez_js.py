"""P0 — la primera ficha técnica de un vehículo no se podía crear.

## El defecto

`flotaAbrirFicha` armaba el formulario con `d.ficha.capacidad_tanque_fuente`.
Cuando el vehículo **todavía no tiene ficha**, el servidor responde
`{existe: false, ficha: null}` —a propósito: `null` no es «ficha vacía», es «no
hay ficha»— y `null.capacidad_tanque_fuente` revienta con `TypeError`.

El `try` de la función envolvía solo el `get`, así que el error salía por
arriba, el `innerHTML` nunca se asignaba y el modal se quedaba en «Cargando…»
para siempre. **Ningún vehículo nuevo podía recibir su primera ficha**, y sin
ficha no hay capacidad de tanque (detector de sobre-tanqueo ciego), ni
posiciones de llanta, ni preventivo.

La clase no es «un campo mal leído»: es **leer un campo de un objeto que el
contrato declara que puede ser `null`**. La función ya tenía el alias correcto
(`const f = d.ficha || {}`) y lo usaba en todos los campos menos en uno.

## Por qué en Node y no un grep

Un test que busque `d.ficha.capacidad` en el texto pasa el día que alguien lo
escriba de otra forma equivalente (`d['ficha'].capacidad…`). Acá se ejecuta el
`flota.js` real con `util.js` real y se mira lo que quedó pintado.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'

#: Arnés mínimo: carga los archivos, siembra `get`, llama una función y
#: devuelve el `innerHTML` de los elementos que se le pidan.
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const GUION = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'));

function elemento(id) {
  return {
    id, innerHTML: '', textContent: '', style: {}, disabled: false, value: '',
    checked: false, dataset: {}, open: false,
    classList: { add() {}, remove() {}, toggle() { return false; },
                 contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {}, remove() {},
    insertAdjacentHTML(pos, html) { this.innerHTML = html + this.innerHTML; },
    insertBefore() {},
    get parentNode() { return elemento('padre-' + id); },
  };
}
const elementos = {};
const doc = {
  body: elemento('body'), head: elemento('head'),
  documentElement: elemento('html'),
  getElementById(id) {
    if (!(id in elementos)) elementos[id] = elemento(id);
    return elementos[id];
  },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, createElement(t) { return elemento(t); },
};
const pedidas = [];
const enviados = [];
const ctx = {
  console, document: doc,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  window: { location: { origin: 'http://test' }, addEventListener() {},
            matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  AbortController: globalThis.AbortController,
  OPERARIO: GUION.operario || null,
  API: '', TOKEN: 't',
  get: async (ruta) => {
    pedidas.push(String(ruta));
    for (const [trozo, payload] of Object.entries(GUION.rutas)) {
      if (String(ruta).includes(trozo)) {
        if (payload && payload.__error__) throw new Error(payload.__error__);
        return payload;
      }
    }
    throw new Error('ruta no sembrada: ' + ruta);
  },
  fetch: async (url, opts) => {
    enviados.push({ url: String(url), metodo: (opts || {}).method || 'GET',
                    cuerpo: (opts || {}).body || null });
    return { ok: true, status: 200, json: async () => (GUION.respuesta_post || {}) };
  },
  prompt: () => (GUION.prompt === undefined ? 'motivo de prueba' : GUION.prompt),
  confirm: () => true,
  alerta: (m, t) => { enviados.push({ alerta: String(m), tipo: t }); },
  horaColombia: (x) => String(x),
};
ctx.globalThis = ctx;
ctx.window.document = doc;
vm.createContext(ctx);
for (const a of GUION.archivos) {
  vm.runInContext(fs.readFileSync(a, 'utf-8'), ctx, { filename: a });
}
for (const [k, v] of Object.entries(GUION.globales || {})) {
  vm.runInContext(`${k} = ${JSON.stringify(v)};`, ctx);
}

(async () => {
  const salida = { pasos: [] };
  for (const paso of GUION.pasos) {
    if (typeof ctx[paso.fn] !== 'function') {
      throw new Error('no existe la función ' + paso.fn);
    }
    const r = await ctx[paso.fn](...(paso.args || []));
    salida.pasos.push(typeof r === 'string' ? r : null);
  }
  salida.html = {};
  for (const id of (GUION.leer || [])) {
    salida.html[id] = elementos[id] ? elementos[id].innerHTML : null;
  }
  salida.pedidas = pedidas;
  salida.enviados = enviados;
  process.stdout.write(JSON.stringify(salida));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""

#: Los cuatro archivos de la pestaña Flota, en el orden de `index.html`.
ARCHIVOS_FLOTA = [str(PWA / 'util.js'), str(PWA / 'flota.js'),
                  str(PWA / 'flota_analitica.js'), str(PWA / 'flota_bandeja.js')]
#: Solo lo que `flotaAbrirFicha` necesita: el defecto vive en `flota.js`.
ARCHIVOS_FICHA = [str(PWA / 'util.js'), str(PWA / 'flota.js')]


def correr(tmp_path, guion: dict) -> dict:
    """Corre el guion en Node. Revienta el test si el JS revienta."""
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    guion = dict(guion)
    guion.setdefault('archivos', ARCHIVOS_FLOTA)
    guion.setdefault('rutas', {})
    h = tmp_path / 'arnes.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'guion.json'
    g.write_text(json.dumps(guion), encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'el JS reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)


_SIN_FICHA = {'placa': 'NUE001', 'existe': False, 'ficha': None,
              'atributos_sin_dato': None, 'completa': None}


def _abrir(tmp_path, respuesta) -> str:
    r = correr(tmp_path, {
        'archivos': ARCHIVOS_FICHA,
        'rutas': {'/flota/vehiculo/NUE001/ficha': respuesta},
        'pasos': [{'fn': 'flotaAbrirFicha', 'args': ['NUE001']}],
        'leer': ['flota-recibo'],
    })
    return r['html']['flota-recibo']


class TestLaPrimeraFichaSePuedeCrear:

    def test_un_vehiculo_sin_ficha_muestra_el_formulario(self, tmp_path):
        """EL P0. Antes: TypeError y «Cargando…» para siempre."""
        html = _abrir(tmp_path, _SIN_FICHA)
        assert 'Cargando' not in html
        assert 'todavía no tiene ficha' in html
        assert 'Guardar ficha' in html
        assert 'fi-km_inicial' in html

    def test_la_procedencia_del_tanque_nace_en_sin_dato(self, tmp_path):
        """Regla 1 del módulo aplicada al formulario: ningún control nace
        marcado con una respuesta. Sin ficha, la fuente de la capacidad del
        tanque tiene que arrancar en `sin_dato`, no en la primera opción que
        el navegador elija."""
        html = _abrir(tmp_path, _SIN_FICHA)
        i = html.index('id="fi-capacidad_tanque_fuente"')
        bloque = html[i:i + 600]
        assert 'value="sin_dato" selected' in bloque

    def test_no_pinta_null_ni_undefined(self, tmp_path):
        html = _abrir(tmp_path, _SIN_FICHA)
        for basura in ('undefined', 'null', 'NaN'):
            assert basura not in html, basura

    def test_con_ficha_existente_conserva_la_procedencia_guardada(self, tmp_path):
        """La otra mitad: arreglar el `null` no puede borrar lo que sí hay."""
        ficha = {'km_inicial': 1000, 'posiciones_llanta': 4,
                 'capacidad_tanque_galones': 15.0,
                 'capacidad_tanque_fuente': 'manual_fabricante'}
        html = _abrir(tmp_path, {'placa': 'NUE001', 'existe': True,
                                 'ficha': ficha, 'completa': False,
                                 'atributos_sin_dato': ['combustible']})
        i = html.index('id="fi-capacidad_tanque_fuente"')
        assert 'value="manual_fabricante" selected' in html[i:i + 900]
        assert 'combustible' in html

    def test_la_placa_del_boton_va_escapada(self, tmp_path):
        """`data-placa` salía sin `esc()`. Una placa viene del maestro y hoy
        no trae comillas, pero el atributo es el que decide sobre qué vehículo
        se guarda la ficha: se escapa como cualquier dato."""
        r = correr(tmp_path, {
            'archivos': ARCHIVOS_FICHA,
            'rutas': {'/ficha': dict(_SIN_FICHA, placa='A"B')},
            'pasos': [{'fn': 'flotaAbrirFicha', 'args': ['A"B']}],
            'leer': ['flota-recibo'],
        })
        assert 'data-placa="A&quot;B"' in r['html']['flota-recibo']
