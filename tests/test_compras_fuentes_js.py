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


_ARNES_EXPR = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const ctx = { console, document: { getElementById: () => null } };
vm.createContext(ctx);
for (const f of ['util.js', 'compras_fuentes.js'])
  vm.runInContext(fs.readFileSync(args[0] + '/' + f, 'utf8'), ctx);
ctx.__dato = JSON.parse(args[2]);
console.log(JSON.stringify(vm.runInContext(args[1], ctx)));
"""


def _expr(expr, dato):
    if not shutil.which('node'):
        pytest.skip('sin node')
    p = subprocess.run(['node', '-e', _ARNES_EXPR, '--', str(PWA), expr, json.dumps(dato)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_las_ocs_viejas_se_ven_con_su_peso():
    e = _estado()
    e['en_camino']['declaracion'].update({
        'ocs_vencidas': {'dias': 90, 'nota': '90 % de lo que viene es viejo ' + MALO},
        'corte_antiguedad': {'nota': None}, 'problemas_de_configuracion': [MALO]})
    e['en_camino']['top'][0]['oc_vencida'] = 70
    d = _render({'estado': e, 'contenedores': _contenedores()})
    assert '90 % de lo que viene es viejo' in d['html'] and 'Vieja' in d['html']
    assert '<img' not in d['html']


def test_el_lead_time_dice_las_ocs_registradas_al_recibir():
    e = _estado()
    e['lead_time']['descartadas'] = {'oc_registrada_al_recibir': 24}
    e['lead_time']['por_proveedor'][0]['n_descartadas_al_recibir'] = 24
    d = _render({'estado': e, 'contenedores': _contenedores()})
    assert 'OC registrada el día que entró la mercancía' in d['html']
    assert '(+24 al recibir)' in d['html']


def test_el_estado_de_la_lectura_de_marca_escapa_y_no_dice_cero():
    html = _expr('fuentesHtmlMarcaEstado(__dato)', {
        'plan_configurado': 'P03', 'en_curso': False,
        'ultima': {'inicio': None, 'ok': False, 'error': MALO, 'resultado': {'items': None}},
        'vigente': {'plan': MALO, 'leida_utc': None}, 'vigente_es_del_plan': False})
    assert '<img' not in html and '&lt;img' in html
    assert 'sin dato ítems' in html and 'volvé a leer' in html


def test_la_vista_previa_de_marca_sin_lectura_lo_dice():
    html = _expr('fuentesHtmlMarca(__dato)', {'sin_lectura': 'Todavía no hay ' + MALO})
    assert 'Todavía no hay' in html and '<img' not in html
