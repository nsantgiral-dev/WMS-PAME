"""La lista de conductores, EJECUTADA, con el usuario redactado.

`RutaService.listar_conductores` **borra** `usuario_email` para quien no es de
almacén — es un dato personal y la redacción está bien. Lo que estaba mal es
que la pantalla leía esa ausencia como un hecho:

    ${c.usuario_email ? '👤 …' : 'Sin cuenta PWA — no puede entrar a la app'}

`undefined` significaba las dos cosas a la vez — «no tiene cuenta» y «no te
puedo mostrar cuál es». Medido contra producción el 2026-09-21 ejecutando el
endpoint con las credenciales de cada rol: los **tres** conductores activos
tienen cuenta (`usuario_id` 9, 10, 25) y `supervisor` y `control_flota` —los
dos roles que administran flota— veían a los tres marcados «no puede entrar a
la app», con un botón «Crear cuenta PWA» que además les responde 403.

`jefe_almacen` los veía bien. Por eso nadie lo reportó: quien mira la pantalla
con permiso de almacén no ve el defecto.

El contrato ahora es `tiene_cuenta_pwa` —un booleano que no es dato personal y
no se redacta—. `usuario_id` también sobrevive hoy a la redacción, pero por
casualidad: el día que alguien lo quite por prolijidad, el defecto vuelve.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

#: Lo que ve un rol CON permiso de almacén.
CON_EMAIL = {'id': 3, 'nombre': 'Victor', 'cedula': '123', 'telefono': '300',
             'activo': True, 'disponible': True, 'usuario_id': 9,
             'tiene_cuenta_pwa': True, 'usuario_email': 'victor@pame.co',
             'fecha_creacion': '2026-01-01T00:00:00'}

#: Lo mismo, con el correo redactado — supervisor / control_flota / gerente.
REDACTADO = {'id': 3, 'nombre': 'Victor', 'activo': True, 'disponible': True,
             'usuario_id': 9, 'tiene_cuenta_pwa': True,
             'fecha_creacion': '2026-01-01T00:00:00'}

#: Un conductor que de verdad no tiene cuenta.
SIN_CUENTA = {'id': 4, 'nombre': 'Nuevo', 'activo': True, 'disponible': True,
              'usuario_id': None, 'tiene_cuenta_pwa': False,
              'usuario_email': None, 'fecha_creacion': '2026-01-01T00:00:00'}

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const CONDUCTORES = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const ROL = process.argv[4];

const nodos = new Map();
function nodo(id) {
  return {
    _id: id, _html: '', textContent: '', value: '', checked: false,
    style: { cssText: '', display: '' }, classList: { add(){}, remove(){}, toggle(){} },
    appendChild() {}, addEventListener() {}, remove() {}, focus() {}, click() {},
    querySelector: () => null, querySelectorAll: () => [],
    get id() { return this._id; },
    set id(v) { this._id = v; nodos.set(v, this); },
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = String(v);
      for (const m of this._html.matchAll(/id="([a-zA-Z0-9_-]+)"/g)) {
        if (!nodos.has(m[1])) nodos.set(m[1], nodo(m[1]));
      }
    },
  };
}
nodos.set('lista-conductores', nodo('lista-conductores'));

// OPERARIO se declara con `let` DENTRO de app.js y se inicializa desde
// localStorage. Sembrarlo por ahí es la vía REAL —es lo que hace el login—
// y evita el problema de que `ctx.OPERARIO = …` nunca toca la variable del
// módulo (ya costó una vez, ver test_render_tanqueo_del_conductor_js).
const GUARDADO = { 'wms_operario': JSON.stringify({ id: 1, nombre: 'X', rol: ROL }) };

const ctx = {
  console,
  document: {
    getElementById: (id) => nodos.get(id) || null,
    createElement: () => nodo(null),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, cookie: '',
    body: { appendChild() {}, style: {}, classList: { add(){}, remove(){} } },
    documentElement: { style: {} },
  },
  window: { location: { origin: 'http://t', href: '', reload() {} },
            addEventListener() {}, matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true, vibrate() {}, serviceWorker: { register: () => Promise.resolve() } },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  localStorage: {
    getItem: (k) => (k in GUARDADO ? GUARDADO[k] : null),
    setItem(k, v) { GUARDADO[k] = String(v); }, removeItem(k) { delete GUARDADO[k]; },
  },
  alerta: () => {},
  AbortController,
};
ctx.globalThis = ctx;
ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(PWA + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(PWA + '/app.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(PWA + '/rutas.js', 'utf-8'), ctx);

// El stub va DESPUÉS de cargar y DENTRO del contexto.
//
// `app.js` declara su propio `async function get(...)` — un stub puesto en
// `ctx` antes de cargarlo queda sombreado, corre el `get` de verdad y muere
// pidiendo `AbortController`. El `catch` de `cargarListaConductores` lo
// convierte en «Error cargando conductores», y entonces TODO test escrito
// como `assert 'X' not in html` pasa sin haber ejercitado nada.
//
// Tercera vez que un arnés de este repo reporta un defecto suyo como defecto
// del código. Por eso `_render` exige abajo que la pantalla no sea la de
// error, en vez de confiar en que se dibujó.
vm.runInContext(
  'get = async () => ({ conductores: ' + JSON.stringify(CONDUCTORES) + ' });', ctx);

await ctx.cargarListaConductores();
process.stdout.write(JSON.stringify({
  html: nodos.get('lista-conductores').innerHTML,
}));
"""


def _render(tmp_path, conductores, rol):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'; h.write_text(HARNESS)
    d = tmp_path / 'c.json'; d.write_text(json.dumps(conductores))
    r = subprocess.run(['node', str(h), str(PWA), str(d), rol],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f'la lista REVENTÓ en vez de dibujarse:\n{r.stderr}'
    html = json.loads(r.stdout)['html']
    # Sin esto, un fallo del arnés se lee como «la pantalla no miente»: el
    # `catch` pinta un div rojo y todos los `not in html` pasan vacíos.
    assert 'Error cargando conductores' not in html, (
        'la pantalla cayó al catch — el arnés no ejercitó el render')
    assert conductores == [] or 'border-radius:12px' in html, (
        'no se dibujó ninguna tarjeta de conductor')
    return html


MIENTE = 'Sin cuenta PWA'


class TestLaPantallaNoNiegaUnaCuentaQueExiste:

    @pytest.mark.parametrize('rol', ['supervisor', 'control_flota', 'gerente'])
    def test_con_el_correo_redactado_no_dice_que_no_puede_entrar(self, tmp_path, rol):
        """EL test. Es el caso REAL de producción: tres conductores activos,
        los tres con cuenta, y el rol que administra flota sin permiso para
        ver la dirección de correo."""
        html = _render(tmp_path, [REDACTADO], rol)
        assert MIENTE not in html, (
            f'a {rol} la pantalla le dice que un conductor CON cuenta '
            f'(usuario_id=9) «no puede entrar a la app»')

    def test_tampoco_le_ofrece_crearla(self, tmp_path):
        """El botón es la segunda mitad de la mentira: afirma que falta algo
        que está, y encima `POST /conductores/<id>/cuenta` es `_solo_admin`,
        así que a supervisor le responde 403."""
        html = _render(tmp_path, [REDACTADO], 'supervisor')
        assert 'Crear cuenta PWA' not in html

    def test_el_admin_sigue_viendo_el_correo(self, tmp_path):
        """El arreglo no puede costar el dato a quien sí puede verlo."""
        html = _render(tmp_path, [CON_EMAIL], 'admin')
        assert 'victor@pame.co' in html
        assert MIENTE not in html


class TestElAvisoSigueApareciendoCuandoEsCierto:
    """Un detector que nunca dispara prueba la mitad. Si la pantalla dejara de
    avisar NUNCA, este arreglo sería peor que el defecto: un conductor sin
    cuenta no puede trabajar y nadie se enteraría."""

    def test_un_conductor_sin_cuenta_sigue_marcado(self, tmp_path):
        html = _render(tmp_path, [SIN_CUENTA], 'admin')
        assert MIENTE in html, (
            'la pantalla dejó de avisar de un conductor que de verdad no '
            'puede entrar a la app')

    def test_y_al_admin_se_le_ofrece_crearla(self, tmp_path):
        html = _render(tmp_path, [SIN_CUENTA], 'admin')
        assert 'Crear cuenta PWA' in html

    def test_a_quien_no_puede_crearla_no_se_le_ofrece(self, tmp_path):
        """Misma doctrina que `mostrarSegunRol` (`app.js`): ofrecer un gesto
        que el backend va a negar con 403 enseña a ignorar errores."""
        html = _render(tmp_path, [SIN_CUENTA], 'supervisor')
        assert MIENTE in html, 'el aviso sí corresponde: no tiene cuenta'
        assert 'Crear cuenta PWA' not in html


class TestLosDosEnLaMismaLista:
    """Mezclados, que es como llegan: el aviso tiene que caer sobre uno y no
    sobre el otro. Un test con una sola fila no distingue «lee bien el campo»
    de «siempre dice lo mismo»."""

    def test_el_aviso_cae_solo_sobre_el_que_no_tiene(self, tmp_path):
        html = _render(tmp_path, [REDACTADO, SIN_CUENTA], 'supervisor')
        assert html.count(MIENTE) == 1, (
            f'el aviso aparece {html.count(MIENTE)} veces sobre una lista con '
            f'exactamente un conductor sin cuenta')
        assert 'Nuevo' in html and 'Victor' in html


def test_el_backend_manda_el_campo_que_la_pantalla_lee():
    """El arnés siembra el payload a mano. Si `to_dict` dejara de mandar
    `tiene_cuenta_pwa`, todos los tests de arriba seguirían verdes sobre un
    contrato que ya no existe — y la pantalla volvería a mentir."""
    import ast
    fuente = (RAIZ / 'app' / 'models' / 'conductor.py').read_text(encoding='utf-8')
    arbol = ast.parse(fuente)
    claves = set()
    for n in ast.walk(arbol):
        if isinstance(n, ast.FunctionDef) and n.name == 'to_dict':
            for d in ast.walk(n):
                if isinstance(d, ast.Dict):
                    claves |= {k.value for k in d.keys
                               if isinstance(k, ast.Constant)}
    assert 'tiene_cuenta_pwa' in claves, (
        'Conductor.to_dict dejó de mandar tiene_cuenta_pwa: la pantalla vuelve '
        'a tener que adivinar desde un campo redactado')


def test_la_redaccion_no_toca_el_hecho():
    """El otro lado: `listar_conductores` borra datos personales. Si algún día
    alguien agrega `tiene_cuenta_pwa` a esa lista de borrado, el defecto vuelve
    entero — y por AST, no por texto, porque el nombre aparece también en el
    comentario que explica por qué NO va ahí."""
    import ast
    fuente = (RAIZ / 'app' / 'services' / 'ruta_service.py').read_text(encoding='utf-8')
    borrados = set()
    for n in ast.walk(ast.parse(fuente)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'pop' and n.args
                and isinstance(n.args[0], ast.Constant)):
            borrados.add(n.args[0].value)
    assert 'usuario_email' in borrados, (
        'la redacción del correo desapareció: era correcta y protege un dato '
        'personal')
    assert 'tiene_cuenta_pwa' not in borrados, (
        'se está borrando el hecho junto con el dato personal: la pantalla '
        'vuelve a leer «no sé» como «no tiene»')
