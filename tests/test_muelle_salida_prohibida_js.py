"""El muelle separa «prohibido salir» de «no se sabe», y el panel de avisos dice
qué falta — pintados en Node con `util.js` real (2026-09-27).

- `rutaCuadroAdvertencias` (rutas.js): tres cuadros según la `gravedad` que
  manda el servidor; con algo prohibido y quien no puede autorizar, solo
  «Entendido» (no hay campo de motivo que aprender a llenar con «ok»); con un
  administrador, el motivo pide el largo mínimo.
- `_modalTexto` (modal.js) no deja confirmar un motivo más corto que `minimo`.
- `flotaAvisosHtml` (flota.js): «qué falta» con el servicio, todo escapado.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

PWA = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa'

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const G = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
function nodo() {
  const hijos = {};
  return { innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
    classList: { add() {}, remove() {} }, addEventListener() {}, appendChild() {},
    focus() {}, select() {}, remove() {},
    querySelector(sel) { if (!hijos[sel]) hijos[sel] = nodo(); return hijos[sel]; } };
}
const LLAMADAS = [];
const RESP = G.respuestas || [];
const ctx = {
  console,
  document: { getElementById: () => nodo(), createElement: () => nodo(),
              querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
              body: { appendChild: (n) => { ctx.__ultimo = n; } } },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  alerta() {}, TOKEN: 'x', API: '',
  fetch: async (url, o) => { LLAMADAS.push(JSON.parse(o.body || '{}'));
    const r = RESP.shift(); return { ok: r.status < 300, status: r.status, json: async () => r.json }; },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const a of G.archivos) vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf-8'), ctx, { filename: a });
if (G.guion) {
  ctx._modalTexto = async (t, m, o) => { ctx.__modal = { tipo: 'texto', t, m, o }; return G.guion.motivo; };
  ctx._modalConfirmar = async (m, o) => { ctx.__modal = { tipo: 'confirmar', m, o }; return true; };
}
ctx.__LLAMADAS = LLAMADAS;
const salida = await vm.runInContext(`(async () => (${G.expr}))()`, ctx);
process.stdout.write(JSON.stringify(salida === undefined ? null : salida));
"""


def _correr(tmp_path, archivos, expr, **extra):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'archivos': archivos, 'expr': expr, **extra}), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)], capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0, p.stderr[-800:]
    return json.loads(p.stdout)


_409 = {
    'error': 'El vehículo no puede salir: la ley lo prohíbe.',
    'salida_prohibida': True, 'puede_autorizar': False, 'motivo_minimo': 20,
    'advertencias_flota': [
        {'clave': 'soat_vencido', 'texto': 'SOAT vencido desde <b>x</b>', 'gravedad': 'prohibido',
         'bloquea': True},
        {'clave': 'rtm_sin_registro', 'texto': 'Revisión sin cargar', 'gravedad': 'no_se_sabe',
         'bloquea': False},
        {'clave': 'en_taller', 'texto': 'Tiene una orden de taller abierta',
         'gravedad': 'advertencia', 'bloquea': False},
    ]}


class TestElCuadroDelMuelle:

    def test_tres_cuadros_y_nada_crudo(self, tmp_path):
        c = _correr(tmp_path, ['util.js', 'rutas.js'],
                    f'rutaCuadroAdvertencias({json.dumps(_409)}, "despachar")')
        h = c['html']
        assert h.index('Prohibido salir') < h.index('No se sabe') < h.index('Advertencias')
        assert 'ruta-adv-prohibido' in h and 'ruta-adv-no-se-sabe' in h
        assert '&lt;b&gt;x&lt;/b&gt;' in h and '<b>x</b>' not in h
        assert c['prohibido'] is True and c['puedeAutorizar'] is False
        assert 'Pídale a un administrador' in h
        assert '#' not in h.split('class=')[0], 'sin colores quemados'

    def test_sin_nada_prohibido_se_reconoce_con_motivo(self, tmp_path):
        d = dict(_409, salida_prohibida=False, advertencias_flota=_409['advertencias_flota'][1:])
        c = _correr(tmp_path, ['util.js', 'rutas.js'],
                    f'rutaCuadroAdvertencias({json.dumps(d)}, "despachar")')
        assert c['prohibido'] is False and 'Puede despachar igual' in c['html']
        assert 'Prohibido salir' not in c['html']


class TestElFlujoDelMuelle:

    def test_quien_no_autoriza_solo_ve_entendido_y_no_reenvia(self, tmp_path):
        s = _correr(tmp_path, ['util.js', 'rutas.js'],
                    '_rutaPostConFlota("/api/rutas/1/cerrar", "despachar")'
                    '.then(r => ({ r: r.r, llamadas: __LLAMADAS.length, modal: __modal.tipo }))',
                    respuestas=[{'status': 409, 'json': _409}], guion={'motivo': 'ok'})
        assert s == {'r': None, 'llamadas': 1, 'modal': 'confirmar'}

    def test_el_admin_escribe_el_motivo_con_su_largo_minimo(self, tmp_path):
        d = dict(_409, puede_autorizar=True)
        motivo = 'Va al CDA a renovar la revisión hoy mismo'
        s = _correr(tmp_path, ['util.js', 'rutas.js'],
                    '_rutaPostConFlota("/api/rutas/1/cerrar", "despachar")'
                    '.then(r => ({ status: r.r.status, cuerpo: __LLAMADAS[1], '
                    'minimo: __modal.o.minimo, tipo: __modal.tipo }))',
                    respuestas=[{'status': 409, 'json': d}, {'status': 200, 'json': {'ok': True}}],
                    guion={'motivo': motivo})
        assert s == {'status': 200, 'cuerpo': {'motivo_advertencias': motivo},
                     'minimo': 20, 'tipo': 'texto'}


class TestElModalExigeElLargo:

    def test_no_confirma_un_motivo_corto(self, tmp_path):
        expr = '''(async () => {
          const p = _modalTexto('t', 'm', { minimo: 20 });
          const ov = __ultimo;
          ov.querySelector('#_mt-input').value = 'ok';
          ov.querySelector('#_mt-si').onclick();
          const error = ov.querySelector('#_mt-error').textContent;
          ov.querySelector('#_mt-input').value = 'un motivo de verdad bastante largo';
          ov.querySelector('#_mt-si').onclick();
          return { error, valor: await p };
        })()'''
        s = _correr(tmp_path, ['util.js', 'modal.js'], expr)
        assert 'al menos 20' in s['error']
        assert s['valor'] == 'un motivo de verdad bastante largo'


class TestElPanelDeAvisos:

    def test_que_falta_con_el_servicio_y_escapado(self, tmp_path):
        d = {'avisos': [{'canal': 'correo', 'telefono': 'correo', 'estado': 'entregado_al_proveedor',
                         'parametros': json.dumps({'asunto': 'Flota <script>'}), 'simulado': False}],
             'sin_confirmar_6h': 0,
             'barrido': {'servicio': 'WMS-Worker', 'ultimo_inicio': '2026-09-27T11:00:00'},
             'que_falta': [{'servicio': 'WMS-Worker', 'grave': True,
                            'texto': 'falta FLOTA_AVISOS=true <img>'}]}
        h = _correr(tmp_path, ['util.js', 'flota.js'], f'flotaAvisosHtml({json.dumps(d)})')
        assert '<b>WMS-Worker</b>: falta FLOTA_AVISOS=true &lt;img&gt;' in h
        assert 'Flota &lt;script&gt;' in h and '<script>' not in h
        assert 'aceptado por el correo' in h
        assert '27/09/2026' in h and '06:00' in h

    def test_sin_nada_que_falte_lo_dice(self, tmp_path):
        h = _correr(tmp_path, ['util.js', 'flota.js'],
                    'flotaAvisosHtml({ avisos: [], que_falta: [], sin_confirmar_6h: 0 })')
        assert 'Los avisos están configurados' in h and 'Ninguno todavía' in h
