"""Todo mapa de estados de entrega del PWA cubre TODOS los estados del modelo.

**La clase** (QA e2e 2026-09-24, P1 en producción): *un mapa de estados en JS
que no cubre `EstadoEntrega.TODOS`*. `_condRenderParadas` tenía un mapa local
con ENTREGADO/PARCIAL/RECHAZADO; una parada ENTREGADO_SIN_PAGO (el cuarto
estado, 2026-08-13) daba `EST_C[est] === undefined` → `TypeError` al leer
`badgeBg`. La lista del conductor quedaba en «Cargando paradas...» y con ella
se iba el botón «Cerrar Ruta». El manifiesto (`rutaVerManifiesto`) tenía el
mismo mapa incompleto: «Error cargando manifiesto».

Tres cosas se exigen:

1. **Cobertura.** Todo objeto literal del PWA con dos o más claves que sean
   estados de entrega es un «mapa de estados» y tiene que tener TODOS los
   valores de `EstadoEntrega.TODOS` — leídos del modelo **por AST**, no
   copiados acá. Excepciones en `MAPAS_PARCIALES_DECLARADOS`, con su motivo;
   la lista solo encoge.
2. **Estado desconocido → neutro.** `estiloEntrega()` devuelve un estilo para
   cualquier cosa (y las dos pantallas lo usan): el estado que el servidor
   agregue mañana se pinta gris, no revienta.
3. **Las dos pantallas pintan con cada estado del modelo**, en Node con
   `util.js` real.

Meta-tests: el detector ve el mapa incompleto, no marca un ternario ni un mapa
de una clave ni un comentario, y tiene piso.
"""
import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
MODELO = RAIZ / 'app' / 'models' / 'recaudo_entrega.py'


# ═════════════════════════════════════════════════════════════════════════
# Los estados, leídos del modelo por AST
# ═════════════════════════════════════════════════════════════════════════

def estados_del_modelo(fuente=None):
    """Los valores de `EstadoEntrega.TODOS`, resolviendo los nombres de la clase."""
    arbol = ast.parse(fuente if fuente is not None else MODELO.read_text(encoding='utf-8'))
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef) and nodo.name == 'EstadoEntrega':
            consts, todos = {}, None
            for st in nodo.body:
                if isinstance(st, ast.Assign) and len(st.targets) == 1 \
                        and isinstance(st.targets[0], ast.Name):
                    nombre, v = st.targets[0].id, st.value
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        consts[nombre] = v.value
                    elif nombre == 'TODOS' and isinstance(v, (ast.Tuple, ast.List)):
                        todos = v
            assert todos is not None, 'EstadoEntrega ya no declara TODOS como tupla'
            valores = []
            for e in todos.elts:
                if isinstance(e, ast.Name) and e.id in consts:
                    valores.append(consts[e.id])
                elif isinstance(e, ast.Constant) and isinstance(e.value, str):
                    valores.append(e.value)
                else:
                    raise AssertionError(f'TODOS tiene un elemento que no sé resolver: {ast.dump(e)}')
            return tuple(valores)
    raise AssertionError('no encontré la clase EstadoEntrega')


# ═════════════════════════════════════════════════════════════════════════
# El detector de mapas de estados en el JS
# ═════════════════════════════════════════════════════════════════════════

def _sin_comentarios(js):
    """Blanquea `//` y `/* */` respetando cadenas y plantillas (mismo largo)."""
    out, i, n = list(js), 0, len(js)
    while i < n:
        c = js[i]
        if c in '\'"`':
            j = i + 1
            while j < n and js[j] != c:
                j += 2 if js[j] == '\\' else 1
            i = j + 1
            continue
        if js.startswith('//', i):
            j = js.find('\n', i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = ' '
            i = j
            continue
        if js.startswith('/*', i):
            j = js.find('*/', i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != '\n':
                    out[k] = ' '
            i = j
            continue
        i += 1
    return ''.join(out)


def _llave_que_abre(js, pos):
    prof = 0
    for k in range(pos - 1, -1, -1):
        if js[k] == '}':
            prof += 1
        elif js[k] == '{':
            if prof == 0:
                return k
            prof -= 1
    return -1


_FUNCION = re.compile(r'(?:async\s+)?function\s+(\w+)\s*\(')


def _funcion_en(js, pos):
    ultima = None
    for m in _FUNCION.finditer(js, 0, pos):
        ultima = m.group(1)
    return ultima or '<global>'


def mapas_de_estado(fuentes, estados):
    """[(archivo, funcion, claves)] de todo objeto literal con ≥2 claves que son
    estados de entrega. Una clave es `{` o `,` + nombre (con o sin comillas) + `:`:
    un ternario `? 'ENTREGADO' :` no lo es."""
    alt = '|'.join(sorted(map(re.escape, estados), key=len, reverse=True))
    clave = re.compile(r'[{,]\s*([\'"]?)(' + alt + r')\1\s*:')
    hallados = []
    for archivo, texto in fuentes.items():
        js = _sin_comentarios(texto)
        por_objeto = {}
        for m in clave.finditer(js):
            abre = _llave_que_abre(js, m.start(2))
            por_objeto.setdefault(abre, set()).add(m.group(2))
        for abre, claves in sorted(por_objeto.items()):
            if len(claves) >= 2:
                hallados.append((archivo, _funcion_en(js, abre), frozenset(claves)))
    return hallados


def _fuentes_pwa():
    return {p.name: p.read_text(encoding='utf-8') for p in sorted(PWA.glob('*.js'))}


#: Mapas con claves de estado que A PROPÓSITO no tienen todos. Solo encoge.
MAPAS_PARCIALES_DECLARADOS = {
    ('rutas.js', 'condSelEstado'): (
        'Colores de los DOS botones que el conductor elige (✓ Entregado / ✗ '
        'Rechazado). Se indexa con la lista literal de los botones, nunca con un '
        'estado que llegue del servidor: PARCIAL y ENTREGADO_SIN_PAGO no son '
        'botones (los traduce el servidor, `ACEPTADOS_DEL_CONDUCTOR`).'),
}

PISO_MAPAS = 3


class TestEstadosDelModelo:
    def test_lee_los_cuatro_estados(self):
        est = estados_del_modelo()
        assert {'ENTREGADO', 'PARCIAL', 'RECHAZADO', 'ENTREGADO_SIN_PAGO'} <= set(est), est

    def test_un_estado_nuevo_en_el_modelo_se_ve(self):
        fuente = MODELO.read_text(encoding='utf-8').replace(
            'TODOS = (ENTREGADO, PARCIAL, RECHAZADO, ENTREGADO_SIN_PAGO)',
            "DEVUELTO_A_SEDE = 'DEVUELTO_A_SEDE'\n    TODOS = (ENTREGADO, PARCIAL, RECHAZADO, "
            'ENTREGADO_SIN_PAGO, DEVUELTO_A_SEDE)')
        assert 'DEVUELTO_A_SEDE' in estados_del_modelo(fuente)


class TestTodoMapaDeEstadosCubreElModelo:
    def test_ningun_mapa_incompleto_sin_declarar(self):
        estados = set(estados_del_modelo())
        malos = []
        for archivo, funcion, claves in mapas_de_estado(_fuentes_pwa(), estados):
            falta = estados - claves
            if falta and (archivo, funcion) not in MAPAS_PARCIALES_DECLARADOS:
                malos.append(f'{archivo}::{funcion} no tiene {sorted(falta)}')
        assert not malos, (
            'Mapa de estados de entrega incompleto — con el estado que falta, '
            '`MAPA[est]` es undefined y la pantalla revienta:\n  ' + '\n  '.join(malos))

    def test_la_lista_de_declarados_solo_encoge(self):
        estados = set(estados_del_modelo())
        vivos = {(a, f) for a, f, c in mapas_de_estado(_fuentes_pwa(), estados)
                 if estados - c}
        sobran = set(MAPAS_PARCIALES_DECLARADOS) - vivos
        assert not sobran, f'declarados que ya no existen (sacalos): {sorted(sobran)}'

    def test_toda_declaracion_dice_por_que(self):
        for k, motivo in MAPAS_PARCIALES_DECLARADOS.items():
            assert len(motivo.strip()) > 40, k

    def test_piso_de_mapas_medidos(self):
        n = len(mapas_de_estado(_fuentes_pwa(), estados_del_modelo()))
        assert n >= PISO_MAPAS, f'el detector encontró {n} mapas: ¿se rompió?'


class TestElDetectorMuerde:
    EST = ('ENTREGADO', 'PARCIAL', 'RECHAZADO', 'ENTREGADO_SIN_PAGO')

    def _m(self, js):
        return mapas_de_estado({'x.js': js}, self.EST)

    def test_ve_el_mapa_del_defecto(self):
        js = ('function _condRenderParadas(d){ const EST_C = {\n'
              " ENTREGADO: { label: 'ENTREGADO' },\n PARCIAL: { label: 'P' },\n"
              " RECHAZADO: { label: 'R' },\n}; }")
        [(a, f, claves)] = self._m(js)
        assert f == '_condRenderParadas' and 'ENTREGADO_SIN_PAGO' not in claves

    def test_ve_las_claves_entre_comillas(self):
        assert self._m("const M = {'ENTREGADO': 1, \"RECHAZADO\": 2};")

    def test_no_marca_un_ternario(self):
        assert not self._m("const x = a ? 'ENTREGADO' : b ? 'RECHAZADO' : 'PARCIAL';")

    def test_no_marca_un_mapa_de_una_clave(self):
        assert not self._m("const x = { ENTREGADO: 1, OTRA: 2 };")

    def test_no_marca_un_comentario(self):
        assert not self._m("// const M = { ENTREGADO: 1, RECHAZADO: 2 };\n"
                           "/* { PARCIAL: 1, RECHAZADO: 2 } */")

    def test_separa_dos_objetos(self):
        js = "const A = { ENTREGADO: 1, PARCIAL: 2 }; const B = { RECHAZADO: 1, ENTREGADO_SIN_PAGO: 2 };"
        assert len(self._m(js)) == 2

    def test_las_claves_anidadas_cuentan_para_su_objeto(self):
        js = "const M = { ENTREGADO: { a: `${x}` }, PARCIAL: { b: 1 } };"
        [(_a, _f, claves)] = self._m(js)
        assert claves == {'ENTREGADO', 'PARCIAL'}


# ═════════════════════════════════════════════════════════════════════════
# Las dos pantallas pintan con cada estado del modelo y con uno desconocido
# ═════════════════════════════════════════════════════════════════════════

_ARNES = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const G = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const nodos = new Map();
const cuerpo = [];
function nodo(id) {
  return { id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, className: '',
           classList: { add() {}, remove() {}, contains() { return false; } },
           addEventListener() {}, appendChild() {}, focus() {}, remove() {} };
}
const alertas = [];
const ctx = {
  console, document: {
    getElementById: (id) => { if (!nodos.has(id)) nodos.set(id, nodo(id)); return nodos.get(id); },
    querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
    createElement: () => nodo(null), body: { appendChild: (n) => cuerpo.push(n) } },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  alerta(m) { alertas.push(m); }, TOKEN: 'x', API: '', confirm: () => true,
  get: async (url) => url.endsWith('/planilla') ? { paradas: G.paradas }
                                                : { ruta: { id: 1, conductor_nombre: 'Ana', tipo_ruta: 'Urbana' } },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const a of ['util.js', 'rutas.js']) {
  vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf-8'), ctx, { filename: a });
}
const salida = { alertas };
vm.runInContext(`_COND_RUTA_ACTIVA = { id: 1 }; _COND_PARADAS = ${JSON.stringify(G.paradas)};`, ctx);
vm.runInContext(`_condRenderParadas({ facturas_gestionadas: ${G.paradas.length} })`, ctx);
salida.lista = nodos.get('cond-contenido').innerHTML;
await vm.runInContext('rutaVerManifiesto(1)', ctx);
salida.manifiesto = cuerpo.map(n => n.innerHTML).join('');
salida.neutro = vm.runInContext("JSON.stringify(estiloEntrega('ESTADO_DE_MANANA'))", ctx);
process.stdout.write(JSON.stringify(salida));
"""


def _parada(i, estado):
    return {'tarea_id': i, 'cliente': f'Cliente {estado}', 'municipio': 'Neiva',
            'numero_pedido': f'PD{i}', 'bultos': [{'id': 1}], 'bultos_detalle': [],
            'recaudo': {'estado_entrega': estado, 'forma_pago': None,
                        'monto_cobrado': 0, 'observaciones': ''}}


def _pintar(tmp_path, estados):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'a.mjs'
    h.write_text(_ARNES, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'paradas': [_parada(i, e) for i, e in enumerate(estados)]}),
                 encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f'reventó:\n{p.stderr[-800:]}'
    return json.loads(p.stdout)


class TestLasPantallasPintanCadaEstado:
    def test_la_lista_del_conductor_pinta_todos_los_estados_y_cerrar_ruta(self, tmp_path):
        estados = estados_del_modelo()
        s = _pintar(tmp_path, estados)
        for e in estados:
            assert f'Cliente {e}' in s['lista'], e
        assert 'Cerrar Ruta' in s['lista']

    def test_el_manifiesto_pinta_todos_los_estados(self, tmp_path):
        estados = estados_del_modelo()
        s = _pintar(tmp_path, estados)
        assert not s['alertas'], s['alertas']
        for e in estados:
            assert f'Cliente {e}' in s['manifiesto'], e

    def test_un_estado_desconocido_cae_a_neutro(self, tmp_path):
        s = _pintar(tmp_path, ['ESTADO_DE_MANANA'])
        assert 'Cliente ESTADO_DE_MANANA' in s['lista']
        assert 'Cliente ESTADO_DE_MANANA' in s['manifiesto']
        assert not s['alertas'], s['alertas']
        neutro = json.loads(s['neutro'])
        assert neutro['badgeBg'] and neutro['label'] != 'ESTADO_DE_MANANA'

    def test_sin_pago_no_se_pinta_como_rechazado(self, tmp_path):
        """Los bultos de ENTREGADO_SIN_PAGO se quedaron con el cliente: pintarlo
        rojo «RECHAZADO» diría que volvieron."""
        s = _pintar(tmp_path, ['ENTREGADO_SIN_PAGO'])
        assert 'RECHAZADO' not in s['lista'] and 'Rechazado' not in s['manifiesto']
