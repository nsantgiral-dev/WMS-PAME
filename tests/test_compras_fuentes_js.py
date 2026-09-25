"""Compras → 🧾 Fuentes (`compras_fuentes.js`), en Node con `util.js` real:
escapa lo que pinta, los `onclick` llevan solo posiciones o palabras fijas,
`null` es «sin dato» y no 0, y «Aplicar» exige haber visto la vista previa."""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
MALO = '<img src=x onerror=alert(1)>'

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0]; const r = JSON.parse(args[1]);
const els = {}; const alertas = []; const subidas = [];
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '', value: '',
                                           files: r.archivo ? [{name: 'x.csv'}] : [] });
const ctx = { console, document: { getElementById: el }, alertas, subidas,
  get: (url) => url.includes('contenedores') ? Promise.resolve(r.contenedores)
                                            : Promise.resolve(r.estado),
  post: () => Promise.resolve({}), put: () => Promise.resolve({}),
  alerta: (m, t) => alertas.push([m, t]), confirm: () => true,
  FormData: class { append() {} },
  subirArchivoConProgreso: (url) => { subidas.push(url); return Promise.resolve(r.previa || {}); },
  conBotonOcupado: async (ev, fn) => { await fn(); } };
el('fuentes-container'); el('fuentes-tipo').value = 'ORIGEN_MARCA';
vm.createContext(ctx);
for (const f of ['util.js', 'compras_fuentes.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
(async () => {
  await vm.runInContext('fuentesCargar()', ctx);
  let aplicarSinPrevia = null;
  if (r.probarAplicar) {
    await vm.runInContext('fuentesAplicar({})', ctx);
    aplicarSinPrevia = subidas.slice();
    await vm.runInContext('fuentesPrevia({})', ctx);
    await vm.runInContext('fuentesAplicar({})', ctx);
  }
  console.log(JSON.stringify({ html: els['fuentes-container'].innerHTML,
    resultado: (els['fuentes-resultado'] || {}).innerHTML, alertas, subidas, aplicarSinPrevia }));
})();
"""


def _render(r):
    if not shutil.which('node'):
        pytest.skip('sin node')
    p = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), json.dumps(r)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _estado():
    return {
        'compras_oc': {'encendido': False, 'lineas_abiertas': 3,
                       'lineas_abiertas_con_pendiente': None,
                       'proveedores_por_fuente': {MALO: 1},
                       'sync_abiertas': {'ultima_utc': None, 'nota': MALO}},
        'en_camino': {'skus': 1, 'unidades': 70, 'declaracion': {'lineas_sin_unidad_base': 2},
                      'top': [{'referencia': MALO, 'oc': 70, 'contenedores': 0, 'total': 70,
                               'solapamiento_posible': True}]},
        'lead_time': {'nacional': {'lt_dias': 5, 'sigma_lt': 2, 'n': 0,
                                   'fuente': 'DEFAULT_CONSERVADOR'},
                      'por_proveedor': [{'proveedor': MALO, 'n_proveedor': 3, 'lt_dias': 10,
                                         'sigma_lt': 2, 'nivel': 'PROVEEDOR'}],
                      'observaciones': 3, 'descartadas': {}},
        'kardex_auto': {'encendido': False, 'ventana_efectiva': ['07:00:00', '07:55:00'],
                        'ultima_descarga': {'estado': MALO}},
    }


def _contenedores():
    return {'contenedores': [{'id': 44, 'numero': MALO, 'proveedor': MALO,
                              'estado': 'NAVEGANDO'}]}


def test_escapa_todo_lo_que_pinta():
    d = _render({'estado': _estado(), 'contenedores': _contenedores()})
    assert '<img' not in d['html'] and '&lt;img' in d['html']


def test_los_onclick_llevan_posiciones_no_datos():
    import re
    d = _render({'estado': _estado(), 'contenedores': _contenedores()})
    llamadas = re.findall(r'onclick="([^"]*)"', d['html'])
    assert 'fuentesContEstado(event, 0)' in llamadas and 'fuentesContItems(event, 0)' in llamadas
    assert all('44' not in c and 'img' not in c for c in llamadas)


def test_null_es_sin_dato_y_no_cero():
    d = _render({'estado': _estado(), 'contenedores': _contenedores()})
    assert 'con pendiente sin dato' in d['html']


def test_declara_lo_que_no_suma_y_el_doble_posible():
    d = _render({'estado': _estado(), 'contenedores': _contenedores()})
    assert 'sin unidad base: no se suman' in d['html'] and 'posible doble' in d['html']


def test_aplicar_exige_la_vista_previa():
    d = _render({'estado': _estado(), 'contenedores': _contenedores(), 'archivo': True,
                 'probarAplicar': True,
                 'previa': {'resumen': {'filas': 1, 'validas': 1}, 'escritas': 1,
                            'validas': [{'codigo': MALO, 'cambios': {'origen': {'antes': None, 'despues': MALO}}}]}})
    assert d['aplicarSinPrevia'] == [], 'sin vista previa no se sube nada'
    assert d['subidas'] == ['/api/compras/fuentes/carga/vista-previa',
                            '/api/compras/fuentes/carga/aplicar']
    assert '<img' not in d['resultado'] and '&lt;img' in d['resultado']
