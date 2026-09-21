"""El manifiesto de ruta, EJECUTADO, cuando la planilla responde 403.

    get('/api/rutas/' + id + '/planilla').catch(() => ({ paradas: [] })),

`GET /<id>/planilla` exige `_es_admin_o_jefe`. `GET /<id>` no. Un supervisor
abre el manifiesto —el encabezado carga— y el detalle le da 403, que ese
`.catch` convertía en una lista vacía. La pantalla entonces **afirmaba**:

    Sin paradas registradas          ← y arriba: «· 0 pedidos»

Medido el 2026-09-21 contra producción, ruta 31 (EN_CARGUE): `admin` y
`jefe_almacen` ven `/planilla` 200 con **3 paradas**; `supervisor` recibe 403 y
la pantalla le dice que no hay ninguna. La pestaña Rutas no está oculta para
supervisor (`_TABS_OCULTAS_SUPERVISOR`) y el botón «📋 Ver» se le ofrece con
solo tener bultos, así que es un camino que alguien recorre.

## La clase, que es lo que importa

**Un `no sé` pintado como un hecho.** Es peor que un error: un error se
reporta, y «no hay paradas» se cree. Es la misma forma que
`tiene_cuenta_pwa` cerró el mismo día en la lista de conductores —ahí la
ausencia la producía una redacción de privacidad, acá un 403— y la misma que
la Regla 0 pide declarar en vez de rellenar.

Tres estados, tres mensajes: hay paradas · no se pudo leer · no hay ninguna.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

RUTA = {'id': 31, 'ruta_maestra_nombre': 'Neiva Norte', 'conductor_nombre': 'Victor',
        'tipo_ruta': 'URBANA', 'estado': 'EN_CARGUE', 'total_bultos': 9}

PARADAS = [
    {'numero_pedido': 'PD1450', 'cliente': 'Cliente A', 'municipio': 'Neiva',
     'bultos': [], 'recaudo': None},
    {'numero_pedido': 'PD1451', 'cliente': 'Cliente B', 'municipio': 'Neiva',
     'bultos': [], 'recaudo': None},
    {'numero_pedido': 'PD1452', 'cliente': 'Cliente C', 'municipio': 'Rivera',
     'bultos': [], 'recaudo': None},
]

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const CASO = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

const nodos = new Map();
let ultimoModal = null;
function nodo(id) {
  return {
    _id: id, _html: '', textContent: '', value: '',
    style: { cssText: '', display: '' }, classList: { add(){}, remove(){} },
    appendChild() {}, addEventListener() {}, remove() {},
    querySelector: () => null, querySelectorAll: () => [],
    get id() { return this._id; },
    set id(v) { this._id = v; nodos.set(v, this); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
  };
}
const GUARDADO = { 'wms_operario': JSON.stringify({ id: 1, nombre: 'X', rol: CASO.rol }) };
let avisos = [];
const ctx = {
  console,
  document: {
    getElementById: (id) => nodos.get(id) || null,
    createElement: () => { ultimoModal = nodo(null); return ultimoModal; },
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {},
    body: { appendChild() {}, style: {}, classList: { add(){}, remove(){} } },
    documentElement: { style: {} },
  },
  window: { location: { origin: 'http://t', href: '' }, addEventListener() {},
            matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true, vibrate() {}, serviceWorker: { register: () => Promise.resolve() } },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  localStorage: {
    getItem: (k) => (k in GUARDADO ? GUARDADO[k] : null),
    setItem(k, v) { GUARDADO[k] = String(v); }, removeItem(k) { delete GUARDADO[k]; },
  },
  AbortController,
};
ctx.globalThis = ctx; ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(PWA + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(PWA + '/app.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(PWA + '/rutas.js', 'utf-8'), ctx);

// El stub va DESPUÉS y DENTRO del contexto: `app.js` declara su propio
// `get` y `alerta`, y un stub puesto en `ctx` antes queda sombreado.
vm.runInContext(`
  globalThis.__avisos = [];
  alerta = (m, t) => { globalThis.__avisos.push([m, t]); };
  get = async (url) => {
    const CASO = ${JSON.stringify(CASO)};
    if (url.endsWith('/planilla')) {
      if (CASO.planilla_status) {
        const e = new Error('Solo admin o jefe puede ver la planilla');
        e.status = CASO.planilla_status;
        throw e;
      }
      return { paradas: CASO.paradas };
    }
    return { ruta: CASO.ruta };
  };
`, ctx);

await ctx.rutaVerManifiesto(31);
process.stdout.write(JSON.stringify({
  html: ultimoModal ? ultimoModal.innerHTML : '',
  avisos: ctx.__avisos || [],
}));
"""


def _manifiesto(tmp_path, rol, planilla_status=None, paradas=None):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'; h.write_text(HARNESS)
    caso = {'rol': rol, 'ruta': RUTA, 'paradas': paradas if paradas is not None else PARADAS,
            'planilla_status': planilla_status}
    d = tmp_path / 'caso.json'; d.write_text(json.dumps(caso))
    r = subprocess.run(['node', str(h), str(PWA), str(d)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f'el manifiesto REVENTÓ:\n{r.stderr}'
    out = json.loads(r.stdout)
    # Sin esto, un fallo del arnés se lee como «la pantalla no miente».
    assert not out['avisos'], (
        f'la pantalla cayó al catch — el arnés no ejercitó el render: '
        f'{out["avisos"]}')
    assert out['html'], 'el modal quedó vacío'
    return out['html']


MIENTE = 'Sin paradas registradas'


class TestUnNoSeNoSePintaComoUnHecho:

    def test_con_403_no_afirma_que_no_hay_paradas(self, tmp_path):
        """EL test. Es el caso real: supervisor, ruta 31, tres paradas."""
        html = _manifiesto(tmp_path, 'supervisor', planilla_status=403)
        assert MIENTE not in html, (
            'a supervisor la pantalla le afirma que una ruta con 3 paradas no '
            'tiene ninguna')
        assert 'permiso' in html, (
            'tampoco le dice por qué no las ve: un hueco silencioso es la '
            'misma mentira sin texto')

    def test_tampoco_cuenta_cero_pedidos_en_el_encabezado(self, tmp_path):
        """La misma afirmación vivía dos veces: el cuerpo y el contador del
        encabezado. Arreglar una sola deja la pantalla mintiendo en voz baja."""
        html = _manifiesto(tmp_path, 'supervisor', planilla_status=403)
        assert '0 pedidos' not in html
        assert 'sin dato' in html

    def test_un_fallo_que_no_es_permiso_tambien_se_declara(self, tmp_path):
        """500, red caída, timeout — cualquiera producía la misma lista vacía.
        El mensaje cambia; lo que no cambia es que no se afirma la ausencia."""
        html = _manifiesto(tmp_path, 'admin', planilla_status=500)
        assert MIENTE not in html
        assert 'No se pudo cargar' in html
        assert 'permiso' not in html, (
            'un 500 no es un problema de permisos: decirlo manda a investigar '
            'al lado equivocado')


class TestLosOtrosDosEstadosSiguenIntactos:
    """Detector en las dos direcciones. Si la pantalla dejara de decir «Sin
    paradas registradas» NUNCA, este arreglo sería el defecto nuevo: una ruta
    de verdad vacía es un problema operativo que hay que ver."""

    def test_con_paradas_las_pinta(self, tmp_path):
        html = _manifiesto(tmp_path, 'admin')
        for p in PARADAS:
            assert p['numero_pedido'] in html
        assert '3 pedidos' in html
        assert MIENTE not in html

    def test_una_ruta_de_verdad_vacia_sigue_diciendolo(self, tmp_path):
        html = _manifiesto(tmp_path, 'admin', paradas=[])
        assert MIENTE in html, (
            'la pantalla dejó de avisar de una ruta sin paradas: eso sí es un '
            'hecho y hay que verlo')
        assert 'permiso' not in html and 'No se pudo cargar' not in html
        assert '0 pedidos' in html


def test_el_catch_ya_no_fabrica_la_lista_vacia():
    """Por texto sobre UNA línea concreta, no sobre el archivo: el arreglo
    puede revertirse sin tocar el render, y entonces todos los tests de arriba
    seguirían verdes contra un `dp.ok` que nunca es falso."""
    fuente = (PWA / 'rutas.js').read_text(encoding='utf-8')
    assert "'/planilla').catch(() => ({ paradas: [] }))" not in fuente, (
        'volvió el `.catch` que convierte un 403 en «no hay paradas»')
    assert "e => ({ ok: false, status: e && e.status, paradas: [] })" in fuente, (
        'se perdió la distinción entre «falló» y «está vacío»')
