"""El modal de empaque ambiguo de picking, EJECUTADO en Node con `util.js` real.

`_modalAmbiguedadPicking` metía `e.producto_codigo` crudo dentro de
`onclick="_elegirEmpaquePicking('…', …)"`. `esc()` no protege ahí y no puede:
el navegador decodifica las entidades del atributo antes de correr el JS, así
que un `&#39;` vuelve a ser `'` y rompe la cadena igual (CLAUDE.md, «Todo dato
que se pinta va con esc()»). El arreglo es pasar la posición y buscar el dato.

Este arnés hace lo que hace el navegador: toma el `onclick` del HTML generado,
le **decodifica las entidades**, y lo ejecuta. Así el test mide el ataque real
y no la forma del texto.
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
const caso = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const traza = { posts: [], alertas: [], alertasNativas: [], quitados: 0, html: '' };
const cuerpo = { appendChild(m) { traza.html = m.innerHTML; traza.modal = m; } };
const ctx = {
  console,
  document: { getElementById: () => null, querySelector: () => null,
              querySelectorAll: () => [], addEventListener() {}, body: cuerpo,
              createElement: () => ({ style: {}, remove() { traza.quitados++; } }) },
  window: { location: { origin: 'http://test' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  alert: (m) => traza.alertasNativas.push(String(m)),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/picking.js', 'utf-8'), ctx);
// Lo que picking.js toma de app.js en tiempo de ejecución.
vm.runInContext(`
  TAREA_ACTUAL = { id: 41 };
  postConReintento = async (url, body) => { __traza.posts.push({ url, body }); return {}; };
  alerta = (m, t) => __traza.alertas.push([m, t]);
  beepOk = () => {}; beepError = () => {};
  _actualizarContadorPicking = () => {};
`, Object.assign(ctx, { __traza: traza }));

ctx._modalAmbiguedadPicking(caso.codigo, caso.empaques);

// Lo que hace el navegador con onclick="…": decodifica entidades y ejecuta.
const decodificar = (s) => s.replace(/&#39;/g, "'").replace(/&quot;/g, '"')
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const onclicks = [...traza.html.matchAll(/onclick="([^"]*)"/g)].map(m => decodificar(m[1]));
traza.onclicks = onclicks;
if (caso.clic != null) {
  const elBoton = { closest: () => traza.modal };
  ctx.__boton = elBoton;
  try {
    vm.runInContext(`(function(){ ${onclicks[caso.clic]} }).call(__boton)`, ctx);
  } catch (e) { traza.error = String(e); }
  await new Promise(r => setTimeout(r, 10));
}
delete traza.modal;
process.stdout.write(JSON.stringify(traza));
"""


def _correr(tmp_path, codigo, empaques, clic=None):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    d = tmp_path / 'caso.json'
    d.write_text(json.dumps({'codigo': codigo, 'empaques': empaques, 'clic': clic}),
                 encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(PWA), str(d)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'el arnés reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)


def _emp(**kw):
    base = {'producto_codigo': 'PAPELSP9218', 'producto_nombre': 'Resma carta',
            'referencia_item': 'PAPELSP9218', 'unidad_medida': 'CAJA',
            'factor_conversion': 12}
    base.update(kw)
    return base


ATAQUE = "X');alert('pwn');('"


class TestElDatoNoViajaEnElOnclick:

    def test_un_codigo_con_comilla_no_ejecuta_nada(self, tmp_path):
        """El caso del defecto: el código del producto con una comilla."""
        t = _correr(tmp_path, '7701234', [_emp(producto_codigo=ATAQUE)], clic=0)
        assert 'error' not in t, t.get('error')
        assert t['alertasNativas'] == [], 'el código del producto se ejecutó como JS'
        assert t['posts'][0]['body']['codigo'] == ATAQUE, 'el dato no llegó intacto'

    def test_una_unidad_con_comilla_tampoco(self, tmp_path):
        t = _correr(tmp_path, '7701234', [_emp(unidad_medida="CJ');alert('u');('")], clic=0)
        assert 'error' not in t and t['alertasNativas'] == []

    def test_ningun_dato_del_empaque_esta_dentro_del_onclick(self, tmp_path):
        """La propiedad, no el síntoma: el onclick solo lleva la posición."""
        t = _correr(tmp_path, '7701234',
                    [_emp(producto_codigo='COD-UNICO-1', unidad_medida='UNIDAD-RARA')])
        botones = [o for o in t['onclicks'] if 'Ambiguo' in o]
        assert botones, t['onclicks']
        for o in botones:
            assert 'COD-UNICO-1' not in o and 'UNIDAD-RARA' not in o, o

    def test_el_codigo_escaneado_y_el_nombre_se_pintan_escapados(self, tmp_path):
        t = _correr(tmp_path, '<b>77</b>', [_emp(producto_nombre='<img src=x onerror=alert(1)>')])
        assert '<img src=x' not in t['html'] and '&lt;img' in t['html']
        assert '<b>77</b>' not in t['html'] and '&lt;b&gt;77' in t['html']


class TestElModalHaceLoMismoQueAntes:

    def test_elige_el_empaque_del_boton_tocado(self, tmp_path):
        emps = [_emp(unidad_medida='UND', factor_conversion=1),
                _emp(unidad_medida='CAJA', factor_conversion=12)]
        t = _correr(tmp_path, '7701234', emps, clic=1)
        assert t['posts'] == [{'url': '/api/mobile/escanear', 'body': {
            'tarea_id': 41, 'tipo': 'PICKING', 'codigo': 'PAPELSP9218', 'cantidad': 12}}]
        assert t['alertas'][-1] == ['+12 und — CAJA', 'exito']
        assert t['quitados'] == 1, 'el modal no se cerró'

    def test_sin_producto_codigo_usa_la_referencia(self, tmp_path):
        t = _correr(tmp_path, '7701234',
                    [_emp(producto_codigo=None, referencia_item='REF-9')], clic=0)
        assert t['posts'][0]['body']['codigo'] == 'REF-9'

    def test_el_factor_llega_como_numero(self, tmp_path):
        """Antes viajaba como literal numérico dentro del onclick."""
        t = _correr(tmp_path, '7701234', [_emp(factor_conversion='24')], clic=0)
        assert t['posts'][0]['body']['cantidad'] == 24

    def test_cancelar_sigue_cerrando(self, tmp_path):
        t = _correr(tmp_path, '7701234', [_emp()], clic=1)   # [0]=empaque, [1]=Cancelar
        assert t['posts'] == [] and t['quitados'] == 1
