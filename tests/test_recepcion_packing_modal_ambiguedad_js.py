"""Los modales de empaque ambiguo de recepción y packing, EJECUTADOS en Node con
`util.js` real.

Hermanos del de picking (`tests/test_picking_modal_ambiguedad_js.py`, commit
50139cf), con el mismo defecto: `_modalAmbiguedadRecepcion` y
`_modalAmbiguedadPackingEmp` metían el código escaneado crudo y los datos del
empaque dentro de `onclick="fn('…', …)"`. `esc()` no protege ahí y no puede: el
navegador decodifica las entidades del atributo antes de correr el JS, así que
un `&#39;` vuelve a ser `'` y rompe la cadena igual (CLAUDE.md, «Todo dato que
se pinta va con esc()»). El arreglo es pasar la posición y buscar el dato.

El arnés hace lo que hace el navegador: toma el `onclick` del HTML generado, le
**decodifica las entidades**, y lo ejecuta. Así el test mide el ataque real y
no la forma del texto. Lo que se observa es el POST al servidor, no la función
intermedia: el payload tiene que ser el de antes.
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
const traza = { posts: [], alertas: [], alertasNativas: [], flashes: [], quitados: 0, html: '' };
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
Object.assign(ctx, { __traza: traza });
vm.runInContext(fs.readFileSync(process.argv[2] + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(process.argv[2] + '/' + caso.archivo, 'utf-8'), ctx);
// Lo que el módulo toma de app.js en tiempo de ejecución.
vm.runInContext(caso.preparar, ctx);

ctx[caso.modal](caso.codigo, caso.empaques);

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

# Cada modal con lo que su módulo toma de app.js y lo que el servidor contesta.
RECEPCION = {
    'archivo': 'recepcion.js',
    'modal': '_modalAmbiguedadRecepcion',
    'preparar': """
      RECEPCION_ACTUAL = { id: 7, items: [] };
      generarScanId = () => 'SCAN-1';
      postConReintento = async (url, body) => { __traza.posts.push({ url, body }); return { item: { producto_id: body.producto_id } }; };
      alerta = (m, t) => __traza.alertas.push([m, t]);
      beepOk = () => {}; beepError = () => {};
      guardarOffline = () => {};
      renderItemsRecepcion = () => '';
    """,
}
PACKING = {
    'archivo': 'packing.js',
    'modal': '_modalAmbiguedadPackingEmp',
    'preparar': """
      EMP_TAREA = { id: 41 };
      EMP_ITEMS = [];
      postConReintento = async (url, body) => { __traza.posts.push({ url, body }); return { producto_id: 5, cantidad_actual: 12 }; };
      alerta = (m, t) => __traza.alertas.push([m, t]);
      empFlash = (c, m) => __traza.flashes.push([c, m]);
      empRenderHUDItem = () => {};
      get = async () => ({});
    """,
}
MODALES = {'recepcion': RECEPCION, 'packing': PACKING}

# Qué función nueva ocupa el onclick en cada modal (los botones de empaque).
NUEVA = {'recepcion': '_elegirEmpaqueAmbiguoRecepcion',
         'packing': '_elegirEmpaqueAmbiguoPacking'}


def _correr(tmp_path, cual, codigo, empaques, clic=None):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    d = tmp_path / 'caso.json'
    d.write_text(json.dumps({**MODALES[cual], 'codigo': codigo,
                             'empaques': empaques, 'clic': clic}),
                 encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(PWA), str(d)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'el arnés reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)


def _emp(**kw):
    base = {'producto_id': 5, 'producto_codigo': 'PAPELSP9218',
            'producto_nombre': 'Resma carta', 'referencia_item': 'PAPELSP9218',
            'unidad_medida': 'CAJA', 'factor_conversion': 12}
    base.update(kw)
    return base


ATAQUE = "X');alert('pwn');('"

# Lo que llega al servidor, por modal, para el empaque tocado.
def _post_esperado(cual, codigo_escaneado, e):
    if cual == 'recepcion':
        return {'url': '/api/recepcion/7/escanear', 'body': {
            'producto_id': e['producto_id'], 'cantidad': e['factor_conversion'],
            'es_empaque': False, 'es_bonificacion': False, 'scan_id': 'SCAN-1'}}
    return {'url': '/api/mobile/escanear', 'body': {
        'tarea_id': 41, 'tipo': 'PACKING',
        'codigo': e['producto_codigo'] or codigo_escaneado,
        'cantidad': e['factor_conversion']}}


@pytest.fixture(params=['recepcion', 'packing'])
def cual(request):
    return request.param


class TestElDatoNoViajaEnElOnclick:

    def test_un_codigo_escaneado_con_comilla_no_ejecuta_nada(self, tmp_path, cual):
        """El código escaneado iba crudo en los dos onclick (y en el encabezado)."""
        t = _correr(tmp_path, cual, ATAQUE, [_emp(producto_codigo='')], clic=0)
        assert 'error' not in t, t.get('error')
        assert t['alertasNativas'] == [], 'el código escaneado se ejecutó como JS'
        assert len(t['posts']) == 1, t
        if cual == 'packing':   # sin producto_codigo, packing manda el escaneado
            assert t['posts'][0]['body']['codigo'] == ATAQUE, 'el dato no llegó intacto'

    def test_un_codigo_de_producto_con_comilla_no_ejecuta_nada(self, tmp_path, cual):
        t = _correr(tmp_path, cual, '7701234', [_emp(producto_codigo=ATAQUE)], clic=0)
        assert 'error' not in t, t.get('error')
        assert t['alertasNativas'] == []
        assert len(t['posts']) == 1, t
        if cual == 'packing':
            assert t['posts'][0]['body']['codigo'] == ATAQUE, 'el dato no llegó intacto'

    def test_una_unidad_con_comilla_tampoco(self, tmp_path, cual):
        unidad = "CJ');alert('u');('"
        t = _correr(tmp_path, cual, '7701234', [_emp(unidad_medida=unidad)], clic=0)
        assert 'error' not in t, t.get('error')
        assert t['alertasNativas'] == []
        assert len(t['posts']) == 1, t

    def test_ningun_dato_esta_dentro_del_onclick(self, tmp_path, cual):
        """La propiedad, no el síntoma: el onclick solo lleva la posición."""
        t = _correr(tmp_path, cual, 'ESCANEO-UNICO-9',
                    [_emp(producto_id=90817, producto_codigo='COD-UNICO-1',
                          unidad_medida='UNIDAD-RARA', factor_conversion=4711)])
        botones = [o for o in t['onclicks'] if NUEVA[cual] in o]
        assert len(botones) == 1, t['onclicks']
        for o in t['onclicks']:
            for dato in ('ESCANEO-UNICO-9', 'COD-UNICO-1', 'UNIDAD-RARA', '4711', '90817'):
                assert dato not in o, (dato, o)

    def test_el_codigo_escaneado_y_el_nombre_se_pintan_escapados(self, tmp_path, cual):
        t = _correr(tmp_path, cual, '<b>77</b>',
                    [_emp(unidad_medida='<img src=x onerror=alert(1)>')])
        assert '<img src=x' not in t['html'] and '&lt;img' in t['html']
        assert '<b>77</b>' not in t['html'] and '&lt;b&gt;77' in t['html']


class TestElModalHaceLoMismoQueAntes:

    def test_elige_el_empaque_del_boton_tocado(self, tmp_path, cual):
        emps = [_emp(producto_id=3, unidad_medida='UND', factor_conversion=1),
                _emp(producto_id=5, unidad_medida='CAJA', factor_conversion=12)]
        t = _correr(tmp_path, cual, '7701234', emps, clic=1)
        assert 'error' not in t, t.get('error')
        assert t['posts'] == [_post_esperado(cual, '7701234', emps[1])]
        if cual == 'recepcion':
            assert t['alertas'][-1] == ['CAJA × 12 UND registrada', 'exito']
        else:
            assert t['flashes'][-1] == ['verde', 'CAJA — 12 und']
        assert t['quitados'] == 1, 'el modal no se cerró'

    def test_el_factor_y_el_id_llegan_como_numero(self, tmp_path, cual):
        """Antes viajaban como literales numéricos dentro del onclick."""
        t = _correr(tmp_path, cual, '7701234',
                    [_emp(producto_id='8', factor_conversion='24')], clic=0)
        cuerpo = t['posts'][0]['body']
        assert cuerpo['cantidad'] == 24 and isinstance(cuerpo['cantidad'], int)
        if cual == 'recepcion':
            assert cuerpo['producto_id'] == 8

    def test_cancelar_sigue_cerrando(self, tmp_path, cual):
        t = _correr(tmp_path, cual, '7701234', [_emp()], clic=1)   # [0]=empaque, [1]=Cancelar
        assert t['posts'] == [] and t['quitados'] == 1


class TestPackingSinProductoCodigo:

    def test_usa_el_codigo_escaneado(self, tmp_path):
        """`productoCodigo || codigoBarras`: con código vacío, va el escaneado."""
        t = _correr(tmp_path, 'packing', '7701234', [_emp(producto_codigo=None)], clic=0)
        assert t['posts'][0]['body']['codigo'] == '7701234'
