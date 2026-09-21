"""La pantalla de tanqueo del conductor, EJECUTADA.

El 2026-09-21 se arregló que el conductor tuviera el botón de Tanqueo y no la
pantalla: reusaba `flotaRenderGastos`, cuya primera llamada exige un rol que él
no tiene. Se le dio `GET /flota/vocabulario` y un modo `soloTanqueo`.

**Ese arreglo estaba roto una línea más abajo, y nadie lo vio.**

`const cpk` se evaluaba SIEMPRE, aunque su resultado solo se use en el bloque
que el modo tanqueo omite. Con el payload del vocabulario `d.cpk` es
`undefined` —no `'sin_dato'`—, así que entraba a la rama del número y leía
`d.km_recorridos.toLocaleString()` sobre `undefined`: **TypeError**. Y
`flotaCondTanquear` llama sin `await` y sin `.catch`, así que moría como
rejection no atendida: el formulario no se dibujaba y no había mensaje.

O sea que el arreglo movió la falla del 403 al render y dejó al conductor
exactamente igual de trabado.

## Por qué no se vio

`test_el_conductor_puede_tanquear.py` prueba el endpoint —que devuelve las
cinco listas, que no filtra el CPK— y cruza los `@exige` por AST. Nada de eso
ejecuta el render.

Y `test_render_gastos_js.py` SÍ corre `flotaRenderGastos` en Node, pero
**siempre sin argumentos** y sembrando el payload de `/flota/gastos/`. La rama
nueva tenía cero cobertura de render.

Es el mismo patrón que el arreglo decía estar cerrando: se probó que el permiso
estaba bien, nunca que la pantalla se podía construir.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'

#: Lo que devuelve `GET /flota/vocabulario`, literal. Si el endpoint cambia de
#: forma, este diccionario tiene que cambiar con él — y el test de contrato de
#: abajo lo exige.
VOCABULARIO = {
    'categorias': ['combustible', 'llanta', 'peaje'],
    'categorias_con_periodo': ['soat'],
    'categorias_de_campo': ['tanqueo'],
    'origenes_costo': ['factura'],
    'estados_tanque': ['lleno', 'parcial'],
}

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const PAYLOAD = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const MODO = process.argv[4];   // 'tanqueo' | 'gastos'

// DOM suficiente para que el MODAL exista de verdad.
//
// El arnés anterior stubeaba `getElementById` y llamaba a la función de
// render directamente. Con eso no podía ver el defecto real: el botón del
// conductor no abría el modal, y `#flota-recibo` —que el modal crea— no
// existía. Se probaba la pieza, no el gesto.
const nodos = new Map();
function nodo(id) {
  return {
    _id: id, _html: '', textContent: '',
    style: { cssText: '', display: '' },
    appendChild() {}, addEventListener() {}, remove() {},
    get id() { return this._id; },
    set id(v) { this._id = v; nodos.set(v, this); },
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = String(v);
      // El HTML asignado crea sus hijos con id, como en el navegador.
      for (const m of this._html.matchAll(/id="([a-zA-Z0-9_-]+)"/g)) {
        if (!nodos.has(m[1])) nodos.set(m[1], nodo(m[1]));
      }
    },
  };
}
const ctx = {
  console,
  document: {
    getElementById: (id) => nodos.get(id) || null,
    createElement: () => nodo(null),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {},
    body: { appendChild() {}, style: {} },
  },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  alerta: () => {}, TOKEN: 'x', API: '',
  get: async (url) => {
    if (MODO === 'tanqueo' && !String(url).includes('/vocabulario')) {
      // Es el 403 real: el conductor no puede ver los gastos.
      throw new Error('Sin permiso para ver los gastos de un vehículo');
    }
    return PAYLOAD;
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(PWA + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(PWA + '/flota.js', 'utf-8'), ctx);

// Por la PUERTA, no por la pieza.
//
// `FLOTA_COND` se declara con `let` DENTRO del módulo, así que asignar
// `ctx.FLOTA_COND` desde fuera no toca esa variable: el módulo sigue viendo
// la suya, vacía. Hay que asignar dentro del mismo contexto — si no, el
// arnés reporta «el botón no hizo nada» cuando lo único que pasó es que
// nunca le llegó la placa.
if (MODO === 'tanqueo') {
  vm.runInContext("FLOTA_COND = { placa: 'ABC123' };", ctx);
  await ctx.flotaCondTanquear();
} else {
  await ctx.flotaAbrirGastos('ABC123');
}

// Si se pide, se simula GUARDAR un tanqueo: es el camino donde el panel se
// recarga, y donde el modo se perdía.
if (process.argv[5] === 'guardar') {
  await ctx.flotaRenderGastos();   // la recarga que hace flotaGuardarGasto
}

const modal = nodos.get('flota-modal');
const recibo = nodos.get('flota-recibo');
process.stdout.write(JSON.stringify({
  modal_visible: !!(modal && modal.style.display === 'block'),
  html: (recibo && recibo.innerHTML) || '',
}));
"""

def _abrir(tmp_path, payload, solo_tanqueo, recargar=False):
    """Dispara el GESTO completo y devuelve `(modal_visible, html)`."""
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS)
    d = tmp_path / 'p.json'
    d.write_text(json.dumps(payload))
    r = subprocess.run(
        ['node', str(h), str(PWA), str(d),
         'tanqueo' if solo_tanqueo else 'gastos'] + (['guardar'] if recargar else []),
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, (
        f'el gesto REVENTÓ en vez de dibujarse:\n{r.stderr}')
    d = json.loads(r.stdout)
    return d['modal_visible'], d['html']


def _render(tmp_path, payload, solo_tanqueo, recargar=False):
    """Solo el HTML, para los tests que miran contenido."""
    return _abrir(tmp_path, payload, solo_tanqueo, recargar)[1]


class TestElConductorVeElFormulario:

    def test_el_modal_se_abre(self, tmp_path):
        """EL defecto que dos arreglos seguidos no vieron.

        `flotaRenderGastos` escribe en `#flota-recibo`, que **solo existe
        dentro del modal**. El arreglo anterior llamaba al render directo y se
        saltaba la apertura: la primera vez el contenedor no existía y la
        escritura lanzaba `TypeError`; si el conductor había abierto antes otro
        modal de flota, el contenedor existía pero el modal estaba oculto y el
        formulario se dibujaba donde nadie lo ve.

        En los dos caminos el botón no hacía nada, y la llamada iba sin `await`
        ni `.catch`, así que moría como rejection no atendida: sin mensaje.
        """
        visible, _ = _abrir(tmp_path, VOCABULARIO, solo_tanqueo=True)
        assert visible, (
            'el modal no quedó visible: el formulario se dibuja en un '
            'contenedor que el conductor no puede ver')

    def test_se_dibuja_sin_reventar(self, tmp_path):
        """EL test. Con el payload del vocabulario —que NO trae cpk,
        km_recorridos ni rendimiento— la pantalla tiene que construirse."""
        html = _render(tmp_path, VOCABULARIO, solo_tanqueo=True)
        assert 'gs-cat' in html, 'no se dibujó el selector de categoría'
        assert 'Registrar' in html, 'no se dibujó el botón de registrar'

    def test_ofrece_los_estados_de_tanque(self, tmp_path):
        """Sin esto no se puede marcar «lleno», y sin dos llenos no hay
        ventana de rendimiento — el dato que la fase de gastos vino a medir."""
        html = _render(tmp_path, VOCABULARIO, solo_tanqueo=True)
        for estado in VOCABULARIO['estados_tanque']:
            assert estado in html, f'falta el estado de tanque {estado!r}'


class TestDespuesDeGuardarSigueAhi:
    """El camino de ÉXITO, que es donde el defecto sobrevivía.

    `flotaGuardarGasto` termina recargando el panel. Cuando el modo era un
    argumento, esa recarga lo perdía: el conductor guardaba su tanqueo, veía el
    toast verde, y acto seguido el panel se reemplazaba por «No se pudieron
    cargar los gastos: sin permiso para ver los gastos de un vehículo».

    El formulario desaparecía justo después de usarlo bien. Por eso el modo
    pasó a ser ESTADO del panel: lo decide quien abre, y la recarga lo hereda
    sin que nadie tenga que acordarse de pasarlo.
    """

    def test_el_formulario_sigue_despues_de_recargar(self, tmp_path):
        html = _render(tmp_path, VOCABULARIO, solo_tanqueo=True, recargar=True)
        assert 'gs-cat' in html, (
            'tras guardar, el panel perdió el formulario — la recarga se fue '
            'al listado de gastos que el conductor no puede ver')

    def test_la_recarga_no_pinta_el_error_de_permiso(self, tmp_path):
        html = _render(tmp_path, VOCABULARIO, solo_tanqueo=True, recargar=True)
        assert 'No se pudieron cargar los gastos' not in html, (
            'el panel se reemplazó por el 403 justo después de un tanqueo '
            'correcto')


class TestNoLeMuestraLoQueNoLeToca:
    """Arreglar el render no puede filtrar lo que el rol no debe ver."""

    def test_no_pinta_el_CPK(self, tmp_path):
        html = _render(tmp_path, VOCABULARIO, solo_tanqueo=True)
        assert 'por kilómetro' not in html, (
            'el CPK llegó a la pantalla del conductor — «está a un paso de '
            'leerse como una medida suya»')

    def test_no_pinta_el_rendimiento(self, tmp_path):
        html = _render(tmp_path, VOCABULARIO, solo_tanqueo=True)
        assert 'km/galón' not in html
        assert 'undefined' not in html, (
            'se coló un `undefined` en la pantalla: una clave que el payload '
            'del vocabulario no trae se está pintando igual')

    def test_no_afirma_nada_sobre_la_ficha_del_vehiculo(self, tmp_path):
        """El vocabulario no trae `capacidad_tanque_galones` —es por vehículo,
        no un catálogo—, así que la rama del `else` decía SIEMPRE «la ficha no
        dice cuántos galones caben», también cuando sí lo dice. Otra afirmación
        falsa, en la pantalla de quien tanquea."""
        html = _render(tmp_path, VOCABULARIO, solo_tanqueo=True)
        assert 'La ficha no dice' not in html, (
            'la pantalla afirma que la ficha no declara la capacidad, y en '
            'modo tanqueo simplemente no la pidió: no lo sabe')


class TestLaPantallaCompletaSigueIgual:
    """La otra dirección: el modo tanqueo no puede haber roto la pantalla de
    gestión, que es la que sí muestra el CPK."""

    PAYLOAD_COMPLETO = dict(
        VOCABULARIO,
        gastos=[], cpk=1500, cpk_motivo=None, cpk_marca='verificado',
        pesos_imputados=150000, km_recorridos=100, lecturas_en_ventana=2,
        rendimiento_km_galon=12.5, desde='2026-09-01', hasta='2026-09-30',
        capacidad_tanque_galones=40)

    def test_gestion_sigue_viendo_el_cpk(self, tmp_path):
        html = _render(tmp_path, self.PAYLOAD_COMPLETO, solo_tanqueo=False)
        assert 'por kilómetro' in html
        assert 'km/galón' in html

    def test_gestion_sigue_viendo_la_capacidad_de_la_ficha(self, tmp_path):
        html = _render(tmp_path, self.PAYLOAD_COMPLETO, solo_tanqueo=False)
        assert '40' in html and 'Tanque declarado' in html


def test_el_payload_de_prueba_coincide_con_lo_que_el_endpoint_devuelve():
    """Si el endpoint gana o pierde una clave y este diccionario no cambia, los
    tests de arriba estarían probando una pantalla contra un payload que ya no
    existe — verdes sobre una forma muerta."""
    import ast
    fuente = (RAIZ / 'flota' / 'api' / 'gastos.py').read_text(encoding='utf-8')
    arbol = ast.parse(fuente)
    fn = next(n for n in ast.walk(arbol)
              if isinstance(n, ast.FunctionDef) and n.name == 'vocabulario')
    claves = set()
    for nodo in ast.walk(fn):
        if isinstance(nodo, ast.Dict):
            claves |= {k.value for k in nodo.keys
                       if isinstance(k, ast.Constant)}
    assert claves == set(VOCABULARIO), (
        f'`GET /flota/vocabulario` devuelve {sorted(claves)} y este test '
        f'siembra {sorted(VOCABULARIO)}. La pantalla se está probando contra '
        f'un payload que el endpoint ya no manda.')
