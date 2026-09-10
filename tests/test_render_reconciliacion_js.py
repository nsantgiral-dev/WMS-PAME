"""Los renders del PWA, EJECUTADOS — no leídos.

Lo que había antes en `test_reconciliacion_por_bodega.py` era un detector de
texto: recortaba el cuerpo de `verReconciliacion()` del archivo y exigía que
las cadenas `cobertura_pct`, `cuadre_pct` y `no_comparable` aparecieran ahí
dentro. Un auditor de mutación dejó los tres identificadores en su sitio,
anuló el pintado (`|| true` en el ternario que arma el bloque) y el test siguió
en verde: **12 passed**. Un assert que finge cobertura es peor que ninguno,
porque el hueco queda tapado.

Acá el `app.js` real se ejecuta en Node dentro de un `vm` con un DOM mínimo y
un `fetch` sembrado, se llama la función de render y se mira **lo que quedó
pintado**. Los números de cada payload están elegidos para que ninguno pueda
salir por casualidad de otro camino.

Node no siempre está (la imagen de Railway sí lo trae — `nixpacks.toml`
instala `nodejs`); cuando falta, el test se salta declarándolo, mismo criterio
que `tests/test_frontend_integrity.py`. Un salto que se anuncia no es una
cobertura fingida.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
APP_JS = RAIZ / 'app' / 'static' / 'pwa' / 'app.js'


# El arnés vive acá y no en un `.js` suelto a propósito: es andamio de este
# test, no código del PWA. Un archivo JS más en `app/static/pwa/` entraría en
# el grafo de llamadas que vigila `test_frontend_integrity.py`.
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const APPJS = process.argv[2];
const GUION = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

function elemento(id) {
  return {
    id, innerHTML: '', textContent: '', style: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {}, remove() {},
  };
}

const elementos = {};
const doc = {
  body: elemento('body'),
  head: elemento('head'),
  documentElement: elemento('html'),
  getElementById(id) {
    if (!(id in elementos)) elementos[id] = elemento(id);
    return elementos[id];
  },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  createElement(t) { return elemento(t); },
};

const store = {};
const ctx = {
  console,
  localStorage: {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  },
  document: doc,
  window: {
    location: { origin: 'http://test', href: '', reload() {} },
    addEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {} }),
  },
  navigator: { onLine: true, serviceWorker: { register: () => Promise.resolve() } },
  setTimeout, clearTimeout,
  // Algunos renders corren dentro de un setInterval de 8 s: se capturan los
  // callbacks y se disparan a mano en vez de esperarlos.
  setInterval: (fn) => { ctx.__intervalos.push(fn); return ctx.__intervalos.length; },
  clearInterval: () => {},
  AbortController: globalThis.AbortController,
  fetch: async (url) => {
    const ruta = String(url).replace('http://test', '');
    let cuerpo = {};
    for (const [trozo, payload] of Object.entries(GUION.rutas)) {
      if (ruta.includes(trozo)) { cuerpo = payload; break; }
    }
    return { ok: true, status: 200, json: async () => cuerpo,
             text: async () => JSON.stringify(cuerpo) };
  },
  __intervalos: [],
};
ctx.globalThis = ctx;
ctx.window.document = doc;
vm.createContext(ctx);
// `util.js` va primero y se carga de verdad: `esc()` es lo que este arnés
// tiene que poder ver fallar, y un stub lo volvería un test del stub.
vm.runInContext(fs.readFileSync(APPJS.replace(/[^/]+$/, 'util.js'), 'utf-8'),
                ctx, { filename: 'util.js' });
vm.runInContext(fs.readFileSync(APPJS, 'utf-8'), ctx, { filename: 'app.js' });

(async () => {
  if (typeof ctx[GUION.fn] !== 'function') {
    throw new Error('no existe la función de render ' + GUION.fn);
  }
  await ctx[GUION.fn]();
  for (const fn of ctx.__intervalos) await fn();
  const leido = {};
  for (const id of GUION.elementos) {
    const el = doc.getElementById(id);
    leido[id] = (el.innerHTML || '') + '\n' + (el.textContent || '');
  }
  process.stdout.write(JSON.stringify(leido));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _render(tmp_path, fn: str, rutas: dict, elementos: list) -> dict:
    """Ejecuta `fn()` del `app.js` real con `rutas` sembradas y devuelve lo pintado."""
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    harness = tmp_path / 'harness.mjs'
    harness.write_text(HARNESS, encoding='utf-8')
    guion = tmp_path / 'guion.json'
    guion.write_text(json.dumps(
        {'fn': fn, 'rutas': rutas, 'elementos': elementos}), encoding='utf-8')
    proc = subprocess.run(
        ['node', str(harness), str(APP_JS), str(guion)],
        capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f'el render {fn}() reventó al ejecutarse:\n{proc.stderr}')
    return json.loads(proc.stdout)


def _reconciliar(tmp_path, resultado: dict) -> dict:
    salida = _render(
        tmp_path, 'verReconciliacion',
        {'reconciliacion-estado': {'en_curso': False, 'ultimo_error': None,
                                   'ultimo_resultado': resultado},
         '/api/siesa/reconciliacion': {'iniciado': True}},
        ['inv-resultado', 'panel-reconciliacion'])
    return {'veredicto': salida['inv-resultado'],
            'panel': salida['panel-reconciliacion']}


def _payload(**extra) -> dict:
    """Un resultado de reconciliación con números que no salen por casualidad."""
    base = {
        'sin_diferencias': False,
        'total_discrepancias': 0,
        'discrepancias': [],
        'cobertura_pct': 42.9,
        'total_productos_wms': 3,
        'total_productos_siesa': 7,
        'cuadre_pct': 66.7,
        'skus_comparados': 3,
        'skus_cuadran': 2,
        'bodegas_comparadas': ['NB1'],
        'total_no_comparable': 1,
        'total_no_comparable_esperado': 0,
        'total_no_comparable_imprevisto': 1,
        'no_comparable': {
            'almacenes_sin_bodega_siesa': [],
            'bodegas_wms_sin_datos_siesa': [],
            'bodegas_siesa_sin_almacen_wms': [],
            'skus_siesa_sin_producto_wms': 0,
        },
        'por_bodega': [{
            'bodega': 'NB1', 'comparable': True, 'motivo': None,
            'skus_wms': 3, 'skus_siesa': 3,
            'unidades_wms': 16, 'unidades_siesa': 19,
            'denominador': 3, 'cuadran': 2, 'cuadre_pct': 66.7,
            'discrepancias': 0,
        }],
    }
    base.update(extra)
    return base


class TestElRenderPintaLaCobertura:
    """El backend calculaba `cobertura_pct` y lo publicaba; el render no lo
    pintaba. Un denominador calculado y escondido no existe para quien mira la
    pantalla."""

    def test_la_cobertura_sale_con_su_valor_y_su_denominador(self, tmp_path):
        salida = _reconciliar(tmp_path, _payload())
        assert '42.9%' in salida['panel'], (
            'la cobertura no se pintó: ' + salida['panel'])
        # El porcentaje sin sus dos términos no se puede interpretar.
        assert '3 productos' in salida['panel']
        assert 'de 7' in salida['panel']

    def test_el_cuadre_sale_por_bodega_con_su_denominador(self, tmp_path):
        salida = _reconciliar(tmp_path, _payload())
        assert 'NB1' in salida['panel']
        assert '2/3' in salida['panel']
        assert '66.7%' in salida['panel']

    def test_el_incomparable_imprevisto_se_pinta_y_nombra_la_bodega(
            self, tmp_path):
        salida = _reconciliar(tmp_path, _payload(no_comparable={
            'almacenes_sin_bodega_siesa': [
                {'almacen': 'TALLER', 'skus': 4, 'unidades': 7}],
            'bodegas_wms_sin_datos_siesa': [],
            'bodegas_siesa_sin_almacen_wms': [
                {'bodega': 'BC99', 'skus_siesa': 2, 'unidades_siesa': 5,
                 'esperado': False, 'justificacion': None,
                 'contraparte_wms': None}],
            'skus_siesa_sin_producto_wms': 6,
        }, total_no_comparable=12, total_no_comparable_imprevisto=12))
        panel = salida['panel']
        assert 'TALLER' in panel
        assert 'BC99' in panel
        assert '6 SKU' in panel, 'los SKU sin producto en el catálogo no se ven'

    def test_el_veredicto_verde_dice_su_denominador(self, tmp_path):
        salida = _reconciliar(tmp_path, _payload(
            sin_diferencias=True, total_no_comparable=0,
            total_no_comparable_imprevisto=0,
            skus_cuadran=3, cuadre_pct=100.0,
            por_bodega=[dict(_payload()['por_bodega'][0],
                             cuadran=3, cuadre_pct=100.0, discrepancias=0)]))
        assert 'Sin diferencias' in salida['veredicto']
        assert '3/3' in salida['veredicto'], (
            'el verde sin su denominador es el número que ya mintió una vez')

    def test_las_diferencias_se_pintan_con_los_dos_lados(self, tmp_path):
        salida = _reconciliar(tmp_path, _payload(
            total_discrepancias=1,
            discrepancias=[{
                'bodega': 'NB1', 'producto_id': 1, 'codigo': 'SKU1',
                'nombre': 'Cuaderno', 'stock_wms': 6, 'stock_siesa': 10,
                'diferencia': -4, 'diferencia_abs': 4, 'estado': 'SIESA_MAYOR',
            }]))
        panel = salida['panel']
        assert 'Cuaderno' in panel
        assert 'WMS: 6' in panel
        assert 'Siesa: 10' in panel


class TestElRenderDistingueLoPrevistoDeLoImprevisto:
    """Si la pantalla pintara igual «AV1 tiene averías, como siempre» que
    «apareció una bodega con saldo que nadie esperaba», quien mira no tendría
    cómo saber cuál de las dos exige levantarse de la silla."""

    def test_av1_esperado_no_se_pinta_como_alarma(self, tmp_path):
        salida = _reconciliar(tmp_path, _payload(
            sin_diferencias=True,
            total_no_comparable=1,
            total_no_comparable_esperado=1,
            total_no_comparable_imprevisto=0,
            skus_cuadran=3, cuadre_pct=100.0,
            no_comparable={
                'almacenes_sin_bodega_siesa': [],
                'bodegas_wms_sin_datos_siesa': [],
                'bodegas_siesa_sin_almacen_wms': [{
                    'bodega': 'AV1', 'skus_siesa': 1, 'unidades_siesa': 2,
                    'esperado': True,
                    'justificacion': 'Averías CDI — contraparte en cuarentena',
                    'contraparte_wms': {
                        'tipo': 'UBICACION', 'comparable': False,
                        'descripcion': 'Ubicación AVERIADOS (cuarentena) dentro de NB1',
                        'unidades': 2, 'skus': 1},
                }],
                'skus_siesa_sin_producto_wms': 0,
            }))
        # Verde y con AV1 igualmente visible: no descalificar no es esconder.
        assert 'Sin diferencias' in salida['veredicto']
        assert 'AV1' in salida['panel']
        assert 'previsto' in salida['panel'].lower(), (
            'AV1 se pinta igual que un hallazgo imprevisto: ' + salida['panel'])
        assert 'Averías CDI' in salida['panel'], (
            'la justificación de la exención no se ve — una excepción que no '
            'se explica en la pantalla es una excepción silenciosa')

    def test_la_tabla_por_bodega_marca_la_fila_prevista(self, tmp_path):
        salida = _reconciliar(tmp_path, _payload(por_bodega=[
            {'bodega': 'NB1', 'comparable': True, 'motivo': None,
             'skus_wms': 3, 'skus_siesa': 3, 'unidades_wms': 16,
             'unidades_siesa': 16, 'denominador': 3, 'cuadran': 3,
             'cuadre_pct': 100.0, 'discrepancias': 0},
            {'bodega': 'AV1', 'comparable': False,
             'motivo': 'BODEGA_SIESA_SIN_ALMACEN_WMS', 'esperado': True,
             'skus_wms': 0, 'skus_siesa': 1, 'unidades_wms': 0,
             'unidades_siesa': 2, 'denominador': 0, 'cuadran': 0,
             'cuadre_pct': None, 'discrepancias': 0},
            {'bodega': 'BC99', 'comparable': False,
             'motivo': 'BODEGA_SIESA_SIN_ALMACEN_WMS', 'esperado': False,
             'skus_wms': 0, 'skus_siesa': 1, 'unidades_wms': 0,
             'unidades_siesa': 7, 'denominador': 0, 'cuadran': 0,
             'cuadre_pct': None, 'discrepancias': 0},
        ]))
        panel = salida['panel']
        assert 'no comparable (previsto)' in panel, (
            'AV1 se pinta igual que BC99 en la tabla, que es lo primero que se '
            'mira: ' + panel)
        # Y la de BC99 sigue siendo el «no comparable» pelado.
        assert panel.count('no comparable') == 2

    def test_lo_imprevisto_se_pinta_como_lo_que_es(self, tmp_path):
        salida = _reconciliar(tmp_path, _payload(no_comparable={
            'almacenes_sin_bodega_siesa': [],
            'bodegas_wms_sin_datos_siesa': [],
            'bodegas_siesa_sin_almacen_wms': [{
                'bodega': 'BC99', 'skus_siesa': 1, 'unidades_siesa': 7,
                'esperado': False, 'justificacion': None,
                'contraparte_wms': None}],
            'skus_siesa_sin_producto_wms': 0,
        }))
        assert 'BC99' in salida['panel']
        assert 'no se puede comparar' in salida['panel'].lower()
        assert 'no se pueden comparar' in salida['veredicto']


_AUD_BASE = {
    'invariantes_corridos': 39,
    'invariantes_rotos': 0,
    'hallazgos_totales': 0,
    'bloqueantes': 0,
    'errores': [],
    'consultas_truncadas': [],
    'resultados': [{
        'codigo': 'VTA-01', 'flujo': 'venta', 'frontera': 'sync→packing',
        'severidad': 'BLOQUEA', 'consecuencia': 'sale sin documento fiscal',
        'error': None, 'hallazgos': [], 'total': 0, 'truncado': False,
    }],
    'nota': 'corrido sobre datos reales',
}


class TestElPanelDeAuditoriaPintaLasConsultasTruncadas:
    """`auditar()` devuelve `consultas_truncadas` (`auditoria/base.py:207`) y el
    panel nunca lo pintaba. `movimientos_inventario` es la tabla más grande y su
    tope de 20.000 es el más fácil de alcanzar: **un «0 hallazgos» parcial se
    lee como limpio**, que es exactamente el modo de fallo que este canal
    existe para evitar."""

    def _panel(self, tmp_path, aud):
        return _render(tmp_path, 'cargarAuditoriaFlujo',
                       {'/api/auditoria/flujo': aud},
                       ['auditoria-flujo'])['auditoria-flujo']

    def test_una_consulta_truncada_se_ve(self, tmp_path):
        panel = self._panel(tmp_path, dict(
            _AUD_BASE,
            consultas_truncadas=['app.services.auditoria.inventario:movimientos:20000'],
        ))
        assert 'movimientos:20000' in panel, (
            'el tope alcanzado no llega a la pantalla: «0 hallazgos» se va a '
            'leer como limpio. Panel: ' + panel)
        assert 'parcial' in panel.lower()

    def test_sin_topes_no_inventa_una_advertencia(self, tmp_path):
        """La otra dirección: en una corrida completa no puede aparecer un
        aviso de universo parcial. Un canal que avisa siempre no avisa."""
        panel = self._panel(tmp_path, dict(_AUD_BASE, consultas_truncadas=[]))
        assert '20000' not in panel
        assert 'parcial' not in panel.lower()
        assert 'VTA-01' in panel     # el panel sí se pintó


class TestElPanelDeRecuperacionPintaLosSemaforos:
    """`/api/siesa/monitor` calcula cuatro semáforos y `modulos` **no se pinta
    en ninguna pantalla del PWA** (`grep modulos app/static/pwa/*.js` daba
    cero). Alinear las claves del semáforo sin pintarlo habría sido arreglar el
    gemelo muerto: la luz correcta, en un tablero que nadie ve."""

    _MONITOR = {
        'pendientes': 3,
        'fallidos': 1,
        'modulos': {
            'productos': {'estado': 'VERDE', 'detalle': {}},
            'pedidos': {'estado': 'AMARILLO', 'detalle': {}},
            'inventario': {'estado': 'GRIS', 'detalle': {}},
            'reconciliacion': {'estado': 'ROJO',
                               'detalle': {'ultimo_error': 'Connekta 500'}},
        },
        'connekta': {},
    }

    def _panel(self, tmp_path, monitor=None, fallidos=None):
        return _render(
            tmp_path, 'siesaRecuperacionCargar',
            {'/api/siesa/monitor': monitor if monitor is not None else self._MONITOR,
             '/api/siesa/jobs-fallidos': fallidos if fallidos is not None
             else {'total': 1, 'por_tipo': {}, 'jobs': []}},
            ['siesa-recuperacion'])['siesa-recuperacion']

    def test_los_cuatro_modulos_se_pintan_con_su_color(self, tmp_path):
        panel = self._panel(tmp_path)
        for modulo in ('productos', 'pedidos', 'inventario', 'reconciliacion'):
            assert modulo in panel.lower(), (
                f'{modulo} no llega a la pantalla: ' + panel)
        assert 'VERDE' in panel.upper() or 'verde' in panel

    def test_una_reconciliacion_fallida_se_ve_roja_y_con_su_error(self, tmp_path):
        panel = self._panel(tmp_path)
        assert 'Connekta 500' in panel, (
            'el error del módulo no se pinta: un rojo sin motivo manda a leer '
            'logs, que es de donde este panel existe para sacar a alguien')

    def test_la_cola_dlq_muestra_sus_numeros(self, tmp_path):
        """Las dos casillas leían `monitor.pendientes` / `monitor.fallidos`, que
        el endpoint no devolvía: mostraban `—` desde siempre."""
        panel = self._panel(tmp_path)
        assert '>3<' in panel.replace(' ', ''), (
            'los jobs pendientes siguen sin número: ' + panel)
        assert '—' not in panel.split('Jobs pendientes')[1][:200]

    def test_un_monitor_caido_no_tumba_el_panel(self, tmp_path):
        """La otra dirección: el panel de recuperación es lo que se mira cuando
        algo se rompió, y no puede romperse con ello."""
        panel = self._panel(tmp_path, monitor={})
        assert 'Recuperación Siesa' in panel
