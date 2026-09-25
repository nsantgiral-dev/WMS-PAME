"""Decisiones de plata o inventario con el modal de la app, y errores que no se
van solos.

**El caso** (auditoría «operación diaria por rol», 2026-09-25):

- `alerta()` borraba todo aviso a los 2,5 s, también los de error: el motivo
  de un rechazo desaparecía antes de poder leerlo.
- Unos 60 `prompt()`/`confirm()` nativos. En el teléfono son un diálogo del
  sistema operativo que corta el texto largo, no se puede estilizar y un
  toque distraído contesta «Aceptar». Los que deciden plata o inventario
  —liquidar una ruta, forzarla con devoluciones sin contar, facturar una
  remisión, declarar una avería, cerrar una llegada con faltantes, autorizar
  un crédito— pasaron al modal propio (`_modalConfirmar`, `_modalTexto`,
  `_modalCantidad`).

**Trinquete**: el JS sin comentarios no llama `confirm(`/`prompt(` fuera del
inventario de abajo, por (archivo, función), con su porqué. **Solo encoge.**
Y las funciones que deciden plata o inventario no pueden volver a él.
"""
import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from tests.test_frontend_integrity import _sin_comentarios

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

_NATIVO = re.compile(r'(?<![\w.$])(confirm|prompt)\s*\(')
_FUNCION = re.compile(r'(?m)^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(')

#: (archivo, función) → (cuántos, por qué). **Solo encoge.** Vacío desde el
#: 2026-09-25: el modal pasó a `modal.js` (flota lo necesitaba sin `app.js`)
#: y los 18 de flota, los 3 de analítica/compras y las dos búsquedas de código
#: (recepción, tienda) lo usan.
DIALOGOS_NATIVOS_DECLARADOS: dict = {}

#: Las que deciden plata o inventario: nunca más un diálogo nativo.
DECIDEN_PLATA_O_INVENTARIO = [
    ('app.js', 'facturarRemisionExistente'), ('app.js', 'iniciarDespachoDesdeSiesa'),
    ('app.js', 'siesaFacturarRMManual'), ('app.js', 'siesaDescartarJob'),
    ('liquidacion.js', 'liqMotivoDevolucionesSinContar'),
    ('rutas.js', 'muelleConfirmarCargueCompleto'), ('rutas.js', 'muelleDesasignar'),
    ('rutas.js', 'rutaCerrar'), ('rutas.js', 'rutaEntregar'), ('rutas.js', 'rutaLiquidar'),
    ('rutas.js', 'rutaLiquidarSiesa'), ('rutas.js', 'condCerrarRuta'),
    ('reposicion.js', 'repReintentarTodosFallidos'),
    ('recepcion.js', 'recepAbrirAveria'), ('recepcion.js', '_seleccionarProductoManual'),
    ('recepcion.js', 'recLlegadaCerrar'), ('recepcion.js', 'marcarNCAprobada'),
    ('recepcion.js', 'confirmarDevolucionCliente'),
    ('tienda.js', 'tiendaAveriasEnviar'), ('tienda.js', 'tiendaValidarAveria'),
    ('traslados.js', 'trasDictaminarAveria'),
    ('cartera.js', '_carteraMotivo'), ('cartera.js', 'carteraAutorizar'),
    ('cartera.js', 'carteraCerrarLiberado'),
    ('kardex.js', 'kardexReconstruirForzar'),
    # Flota (2026-09-25): el tanqueo sin foto del recibo es plata; cerrar una
    # orden decide si el daño queda abierto.
    ('flota.js', 'flotaCondGuardarTanqueo'), ('flota.js', 'flotaCerrarOT'),
    ('flota.js', 'flotaCorregirKm'), ('flota_bandeja.js', 'flotaBandejaDecidir'),
]


def dialogos(fuente: str) -> Counter:
    """{función: cuántos confirm/prompt nativos} del fuente sin comentarios."""
    codigo = _sin_comentarios(fuente)
    fns = [(m.start(), m.group(1)) for m in _FUNCION.finditer(codigo)]
    c = Counter()
    for m in _NATIVO.finditer(codigo):
        nombre = ''
        for pos, n in fns:
            if pos < m.start():
                nombre = n
        c[nombre] += 1
    return c


def _todos() -> dict:
    out = {}
    for f in sorted(PWA.glob('*.js')):
        for nombre, n in dialogos(f.read_text(encoding='utf-8')).items():
            out[(f.name, nombre)] = n
    return out


class TestTrinqueteDialogosNativos:

    def test_no_crece(self):
        malos = {k: n for k, n in _todos().items()
                 if n > DIALOGOS_NATIVOS_DECLARADOS.get(k, (0, ''))[0]}
        assert not malos, (
            f'confirm()/prompt() nativos nuevos: {malos}. Use _modalConfirmar / _modalTexto '
            f'/ _modalCantidad (modal.js) o declárelo con su porqué.')

    def test_solo_encoge(self):
        hoy = _todos()
        viejos = {k: v[0] for k, v in DIALOGOS_NATIVOS_DECLARADOS.items() if hoy.get(k, 0) < v[0]}
        assert not viejos, f'bajaron o desaparecieron, actualice el inventario: {viejos}'

    def test_cada_entrada_dice_por_que(self):
        for k, (n, motivo) in DIALOGOS_NATIVOS_DECLARADOS.items():
            assert n >= 1 and isinstance(motivo, str) and len(motivo) > 15, k

    def test_plata_e_inventario_no_vuelven_al_nativo(self):
        hoy = _todos()
        for k in DECIDEN_PLATA_O_INVENTARIO:
            assert k not in hoy, f'{k} decide plata o inventario y volvió a un diálogo nativo'
            assert k not in DIALOGOS_NATIVOS_DECLARADOS

    def test_las_funciones_de_la_lista_existen(self):
        """Una lista de nombres que ya no existen protege nada."""
        for archivo, nombre in DECIDEN_PLATA_O_INVENTARIO:
            src = (PWA / archivo).read_text(encoding='utf-8')
            assert re.search(rf'function {re.escape(nombre)}\(', src), (archivo, nombre)

    # ── meta ────────────────────────────────────────────────────────────────

    def test_meta_ve_las_formas(self):
        src = ("async function a() {\n  if (!confirm('x')) return;\n  const m = prompt('y');\n}\n"
               "function b() { return window.confirm ? 1 : 0; }\n")
        assert dialogos(src) == Counter({'a': 2})

    def test_meta_no_marca_lo_sano(self):
        src = ("// if (!confirm('x')) return;\n/* prompt('y') */\n"
               "async function c() { await _modalConfirmar('¿?'); await _modalTexto('t', 'm'); "
               "obj.confirm('z'); miconfirm('w'); }\n")
        assert dialogos(src) == Counter()

    def test_piso(self):
        """Con el inventario en cero, un escáner roto también da cero: que
        recorra de verdad el PWA — funciones vistas y usos del modal propio."""
        fuentes = [_sin_comentarios(f.read_text(encoding='utf-8')) for f in PWA.glob('*.js')]
        funciones = sum(len(_FUNCION.findall(c)) for c in fuentes)
        modales = sum(len(re.findall(r'\b_modal(?:Confirmar|Texto|Cantidad)\s*\(', c)) for c in fuentes)
        assert funciones >= 1000, f'solo {funciones} funciones en el PWA: ¿se rompió el lector?'
        assert modales >= 60, f'solo {modales} usos del modal propio: ¿se rompió el lector?'

    def test_el_modal_vive_en_la_capa_base(self):
        """`modal.js` define los tres y nadie más: si vuelve a `app.js`, flota
        (que se prueba sin `app.js`) se queda sin modal."""
        for f in PWA.glob('*.js'):
            for nombre in ('_modalConfirmar', '_modalTexto', '_modalCantidad'):
                define = re.search(rf'(?m)^function {nombre}\(', f.read_text(encoding='utf-8'))
                assert bool(define) is (f.name == 'modal.js'), (f.name, nombre)


# ═════════════════════════════════════════════════════════════════════════════
# alerta(): el error se queda
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
const fs = require('fs'), vm = require('vm');
const hijos = [];
function nodo(tag) {
  const n = { tag, style: {}, dataset: {}, children: [], textContent: '', id: '',
              attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
              appendChild(c) { c.parent = this; this.children.push(c); return c; },
              remove() { if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); } };
  return n;
}
const body = nodo('body');
const timers = [];
const ctx = { console, window: { location: { origin: 'http://t' }, addEventListener() {} },
  document: { body, createElement: nodo,
              getElementById: (id) => body.children.find(c => c.id === id) || null,
              querySelector: () => null, querySelectorAll: () => [], addEventListener() {} },
  localStorage: { getItem: () => null, setItem() {} }, navigator: { onLine: true },
  setTimeout: (f, ms) => { timers.push({ f, ms }); return timers.length; }, clearTimeout() {},
  setInterval: () => 0, clearInterval() {} };
ctx.globalThis = ctx; ctx.location = ctx.window.location; ctx.addEventListener = () => {};
vm.createContext(ctx);
for (const f of ['util.js', 'app.js']) vm.runInContext(fs.readFileSync(process.argv[1] + '/' + f, 'utf8'), ctx);
const casos = JSON.parse(process.argv[2]);
for (const [m, t] of casos) ctx.alerta(m, t);
const pila = body.children.find(c => c.id === 'alertas-pila');
const antes = pila.children.map(c => ({ texto: c.children[0].textContent, boton: c.children.length > 1,
                                        role: c.attrs.role }));
const idsTimers = timers.map(t => t.ms);
timers.forEach(t => t.f());
const despues = pila.children.map(c => c.children[0].textContent);
console.log(JSON.stringify({ antes, despues, timers: idsTimers }));
"""


def _alertas(casos):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    p = subprocess.run(['node', '-e', _ARNES, str(PWA), json.dumps(casos)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


class TestElErrorSeQueda:

    def test_el_error_no_se_va_solo_y_el_exito_si(self):
        t = _alertas([['Rechazado: falta la foto', 'error'], ['Listo', 'exito']])
        assert [a['texto'] for a in t['antes']] == ['Rechazado: falta la foto', 'Listo']
        assert t['antes'][0]['boton'] and t['antes'][0]['role'] == 'alert'
        assert not t['antes'][1]['boton']
        assert t['despues'] == ['Rechazado: falta la foto'], 'el error se fue solo'

    def test_el_mismo_error_no_se_apila_y_caben_tres(self):
        t = _alertas([['A', 'error'], ['A', 'error'], ['B', 'error'], ['C', 'error'], ['D', 'error']])
        assert t['despues'] == ['B', 'C', 'D']

    def test_el_mismo_error_dos_veces_es_uno(self):
        assert _alertas([['A', 'error'], ['A', 'error']])['despues'] == ['A']
