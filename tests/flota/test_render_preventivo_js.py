"""La pantalla del preventivo, EJECUTADA — no leída.

Un test que buscara `'tareas_vencidas'` dentro del texto de `flota.js` pasa con
la función entera desconectada, o con el bloque devolviendo `''` siempre. Ya
pasó en este repo: un aserto de substring sobre `app.js` sobrevivió a anular el
pintado con `|| true`, y se descubrió mutándolo.

Acá el `flota.js` real corre en un `vm` con DOM mínimo y `fetch` sembrado, y se
mira **lo que quedó pintado**.

## Qué se afirma

1. Que los cuatro contadores del health llegan al tablero, **separados**: un
   solo total no dice a quién llamar.
2. Que una flota sin nada que hacer **no pinta nada**. Un tablero que siempre
   muestra algo se deja de mirar — la lección de los 639 avisos conocidos.
3. Que la **procedencia se ve al lado del intervalo**, y que una fuente blanda
   se distingue de una documental. Es la decisión 2 del plan, y si la pantalla
   la deja de pintar, el número vuelve a leerse como verificado.
4. Que `sin_dato` **no se dibuja como si estuviera bien**.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'

# Arnés propio, como el de `test_render_salud_js.py`. Duplicar el andamio es más
# barato que un arnés compartido que nadie pueda cambiar sin romper el otro —
# y este además necesita dos cosas que aquél no tiene: pasar argumentos a la
# función y leer lo que quedó en un elemento del DOM en vez del retorno.
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const FLOTAJS = process.argv[2];
const GUION = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

function elemento(id) {
  return {
    id, innerHTML: '', textContent: '', style: {}, disabled: false,
    classList: { add() {}, remove() {}, toggle() { return false; },
                 contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {}, remove() {},
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
const ctx = {
  console, document: doc,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  window: { location: { origin: 'http://test' }, addEventListener() {},
            matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  AbortController: globalThis.AbortController,
  get: async (ruta) => {
    for (const [trozo, payload] of Object.entries(GUION.rutas)) {
      if (String(ruta).includes(trozo)) return payload;
    }
    throw new Error('ruta no sembrada: ' + ruta);
  },
  horaColombia: (x) => String(x),
};
ctx.globalThis = ctx;
ctx.window.document = doc;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(FLOTAJS, 'utf-8'), ctx, { filename: 'flota.js' });

(async () => {
  if (typeof ctx[GUION.fn] !== 'function') {
    throw new Error('no existe la función ' + GUION.fn);
  }
  for (const [k, v] of Object.entries(GUION.globales || {})) ctx[k] = v;
  const salida = await ctx[GUION.fn](...(GUION.args || []));
  const html = GUION.leer
    ? (elementos[GUION.leer] ? elementos[GUION.leer].innerHTML : '')
    : (salida || '');
  process.stdout.write(JSON.stringify({ html }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _correr(tmp_path, guion: dict) -> str:
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps(guion), encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(FLOTA_JS), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'el render reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)['html']


def _salud(tmp_path, health: dict) -> str:
    return _correr(tmp_path, {'fn': 'flotaBloqueSalud',
                              'rutas': {'/flota/health': health}})


def _sano(**extra) -> dict:
    """Una flota sin nada que reportar. Los campos del preventivo en cero."""
    base = {
        'documentos_vencidos': 0, 'documentos_no_encontrados': 0,
        'vehiculos_sin_lectura': 0, 'lecturas_sin_foto': 0,
        'fichas_con_ancla_incoherente': 0, 'lecturas_correccion_30d': 0,
        'salto_km_maximo_30d': {'delta_km': None, 'nota': 'sin lecturas'},
        'hallazgos_abiertos': 0, 'hallazgos_vencidos': 0,
        'vehiculos_sin_inspeccion_hoy': 0, 'inspecciones_incompletas_hoy': 0,
        'segundos_llenado_30d': {'n': 0, 'minimo': None, 'mediana': None,
                                 'nota': 'ninguna inspección'},
        'tareas_vencidas': 0, 'tareas_por_vencer': 0,
        'tareas_sin_linea_base': 0, 'tareas_sin_intervalo': 0,
        'km_dia_por_vehiculo': [],
    }
    base.update(extra)
    return base


def _plan(tmp_path, tareas, ritmo=None) -> str:
    payload = {
        'placa': 'TGZ653', 'tareas': tareas, 'dias_aviso': 15,
        'piden_taller': 0,
        'ritmo': ritmo or {'km_dia': 'sin_dato', 'marca': 'sin_dato', 'n': 0,
                           'dias': 'sin_dato', 'motivo': 'menos de dos lecturas'},
    }
    return _correr(tmp_path, {
        'fn': 'flotaRenderPreventivo', 'leer': 'flota-recibo',
        'globales': {'FLOTA_PLACA': 'TGZ653'},
        'rutas': {'/flota/preventivo/': payload}})


def _tarea(**extra) -> dict:
    base = {'plan_id': 1, 'tipo': 'distribucion',
            'nombre': 'correa o cadena de distribución', 'estado': 'al_dia',
            'intervalo_km': 60000, 'proximo_km': 115000, 'km_restante': 5000,
            'dias_estimados': 50, 'fuente': 'manual_fabricante',
            'fuente_blanda': False, 'origen': 'ficha',
            'ultima_ejecucion_km': 55000, 'nota': None}
    base.update(extra)
    return base


# ══════════════════════════════════════════════════════════════════════════
# El bloque de salud
# ══════════════════════════════════════════════════════════════════════════

class TestNoGritaCuandoNoHayNadaQueHacer:

    def test_una_flota_sana_no_pinta_nada(self, tmp_path):
        assert _salud(tmp_path, _sano()) == ''

    def test_una_lista_de_ritmos_vacia_no_ocupa_espacio(self, tmp_path):
        """`[]` es «no hay vehículos activos», no un ritmo de cero."""
        assert _salud(tmp_path, _sano(km_dia_por_vehiculo=[])) == ''


class TestLosCuatroContadoresVanSeparados:
    """Cada uno se corrige llamando a una persona distinta: al taller, al que
    consigue el repuesto, al que sabe cuándo se hizo, y al concesionario."""

    def test_las_vencidas_salen_en_rojo(self, tmp_path):
        html = _salud(tmp_path, _sano(tareas_vencidas=2))
        assert '2 tarea(s) de mantenimiento VENCIDAS' in html
        assert 'var(--red)' in html

    def test_y_dicen_que_el_kilometraje_lo_puso_el_fabricante(self, tmp_path):
        """No hay umbral acá, y la pantalla lo dice: es lo que hace que nadie
        venga a «ajustar el número» dentro de tres meses."""
        html = _salud(tmp_path, _sano(tareas_vencidas=1))
        assert 'fabricante' in html

    def test_las_por_vencer_salen_aparte(self, tmp_path):
        html = _salud(tmp_path, _sano(tareas_por_vencer=3))
        assert '3 tarea(s) llegan al cambio pronto' in html

    def test_las_sin_linea_base_se_explican_como_NO_SE_SABE(self, tmp_path):
        """Regla 4: no están al día ni vencidas."""
        html = _salud(tmp_path, _sano(tareas_sin_linea_base=5))
        assert '5 tarea(s) sin saber cuándo se hizo la última vez' in html
        assert 'no están al día ni vencidas' in html.lower()

    def test_las_sin_intervalo_dicen_que_NO_SE_PUDIERON_MIRAR(self, tmp_path):
        """El campo que impide que los otros tres se apaguen sin que nadie lo
        note. «No es que estén bien: es que no se pudieron mirar»."""
        html = _salud(tmp_path, _sano(tareas_sin_intervalo=4))
        assert '4 tarea(s) sin intervalo declarado' in html
        assert 'no es que estén bien' in html.lower()

    def test_las_cuatro_a_la_vez_producen_cuatro_lineas(self, tmp_path):
        html = _salud(tmp_path, _sano(
            tareas_vencidas=1, tareas_por_vencer=2,
            tareas_sin_linea_base=3, tareas_sin_intervalo=4))
        for trozo in ('1 tarea(s) de mantenimiento VENCIDAS',
                      '2 tarea(s) llegan al cambio pronto',
                      '3 tarea(s) sin saber cuándo',
                      '4 tarea(s) sin intervalo declarado'):
            assert trozo in html


class TestElRitmoSePintaComoHechoYNoComoAlarma:
    """Regla 13. Es el número que permite fijar el único umbral de la fase con
    dato dentro de un mes."""

    def test_se_pinta_con_su_marca_y_su_cantidad_de_lecturas(self, tmp_path):
        html = _salud(tmp_path, _sano(km_dia_por_vehiculo=[
            {'placa': 'TGZ653', 'km_dia': '112.5', 'marca': 'declarada',
             'n': 8, 'dias': '30.0', 'motivo': None}]))
        assert 'TGZ653: 112.5 km/día (declarada, 8 lecturas)' in html
        assert 'var(--tx2)' in html

    def test_no_se_compara_entre_vehiculos_y_lo_dice(self, tmp_path):
        html = _salud(tmp_path, _sano(km_dia_por_vehiculo=[
            {'placa': 'TGZ653', 'km_dia': '112.5', 'marca': 'declarada',
             'n': 8, 'dias': '30.0', 'motivo': None}]))
        assert 'No se compara' in html

    def test_sin_dato_NO_se_pinta_como_cero(self, tmp_path):
        """«No es cero: es que no se puede calcular todavía»."""
        html = _salud(tmp_path, _sano(km_dia_por_vehiculo=[
            {'placa': 'TGZ653', 'km_dia': 'sin_dato', 'marca': 'sin_dato',
             'n': 1, 'dias': 'sin_dato', 'motivo': 'menos de dos lecturas'}]))
        assert 'sin dato' in html
        assert 'No es cero' in html


# ══════════════════════════════════════════════════════════════════════════
# La pantalla del plan
# ══════════════════════════════════════════════════════════════════════════

class TestLaProcedenciaSeVeAlLadoDelNumero:
    """Decisión 2 del plan. Si la pantalla la deja de pintar, el intervalo
    vuelve a leerse como si alguien lo hubiera verificado."""

    def test_una_fuente_documental_sale_con_el_intervalo(self, tmp_path):
        html = _plan(tmp_path, [_tarea()])
        assert 'cada 60.000 km' in html
        assert 'fuente: manual_fabricante' in html

    def test_una_fuente_BLANDA_se_marca_como_no_documental(self, tmp_path):
        """Un intervalo dicho por el taller suele ser el correcto; lo que no
        tiene es con qué demostrarlo, y eso es lo que hay que ver."""
        html = _plan(tmp_path, [_tarea(fuente='estimado', fuente_blanda=True)])
        assert 'fuente: estimado — no es documental' in html
        assert 'var(--yellow)' in html

    def test_y_una_documental_NO_lleva_esa_marca(self, tmp_path):
        """La dirección contraria: una pantalla que marcara todo pasaría el
        test de arriba y no distinguiría nada."""
        html = _plan(tmp_path, [_tarea(fuente='manual_fabricante',
                                       fuente_blanda=False)])
        assert 'no es documental' not in html


class TestLosEstadosSePintanConSuPalabra:

    def test_una_vencida_sale_en_rojo_y_con_la_palabra_entera(self, tmp_path):
        html = _plan(tmp_path, [_tarea(estado='vencida', km_restante=-5000,
                                       dias_estimados='sin_dato')])
        assert 'VENCIDA' in html
        assert 'var(--red)' in html

    def test_sin_intervalo_no_se_pinta_como_si_estuviera_bien(self, tmp_path):
        html = _plan(tmp_path, [_tarea(
            tipo='aceite_motor', nombre='aceite de motor',
            estado='sin_intervalo', intervalo_km='sin_dato',
            proximo_km='sin_dato', km_restante='sin_dato',
            dias_estimados='sin_dato', fuente='sin_dato', fuente_blanda=True,
            ultima_ejecucion_km='sin_dato')])
        assert 'sin intervalo declarado' in html
        assert 'la ficha no dice cada cuántos kilómetros toca' in html
        assert 'var(--yellow)' in html

    def test_sin_linea_base_dice_que_no_hay_contra_que_comparar(self, tmp_path):
        html = _plan(tmp_path, [_tarea(
            estado='sin_linea_base', proximo_km='sin_dato',
            km_restante='sin_dato', dias_estimados='sin_dato',
            ultima_ejecucion_km='sin_dato')])
        assert 'nunca se registró una ejecución' in html
        assert 'nunca registrada' in html

    def test_los_dias_son_UNOS_N_y_nunca_una_fecha(self, tmp_path):
        """Es una proyección sobre el ritmo medido, no un vencimiento."""
        html = _plan(tmp_path, [_tarea(km_restante=500, dias_estimados=5)])
        assert 'unos 5 día(s)' in html
        assert 'faltan 500 km' in html

    def test_sin_ritmo_medido_se_DICE_que_no_se_sabe_en_cuantos_dias(
            self, tmp_path):
        """No se calla: callarlo haría creer que el número no hacía falta."""
        html = _plan(tmp_path, [_tarea(km_restante=500,
                                       dias_estimados='sin_dato')])
        assert 'sin ritmo medido' in html


class TestElPlanExplicaSuUnicoUmbral:

    def test_la_ventana_de_anticipacion_se_nombra_en_pantalla(self, tmp_path):
        """Un umbral que la interfaz no nombra es un umbral que nadie discute."""
        html = _plan(tmp_path, [_tarea()])
        assert '<b>15 días</b>' in html
        assert 'los kilómetros los pone el fabricante' in html

    def test_el_ritmo_sin_dato_sale_con_su_motivo(self, tmp_path):
        html = _plan(tmp_path, [_tarea()])
        assert 'Ritmo de uso: <b>sin dato</b>' in html
        assert 'menos de dos lecturas' in html

    def test_el_ritmo_medido_sale_con_n_y_dias(self, tmp_path):
        html = _plan(tmp_path, [_tarea()], ritmo={
            'km_dia': 112.5, 'marca': 'declarada', 'n': 8, 'dias': 30.0,
            'motivo': None})
        assert '112.5 km/día' in html
        assert '8 lecturas sobre 30 días' in html

    def test_un_vehiculo_sin_tareas_lo_explica_en_vez_de_quedar_vacio(
            self, tmp_path):
        """Una pantalla en blanco se lee como «cargando» o como «está todo
        bien». Ninguna de las dos es cierta."""
        html = _plan(tmp_path, [])
        # El corte de línea del template cae en medio de la frase, así que se
        # busca el trozo que no lo cruza. Un aserto sobre la frase entera
        # fallaría por el sangrado y no por el contenido.
        assert 'no tiene ninguna tarea de' in html
        assert 'ficha técnica' in html
