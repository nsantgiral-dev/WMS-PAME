"""Inventario Cíclico: un lugar para cada cosa (2026-09-24).

La pantalla tenía seis pestañas y el propio repo declaraba sus enredos
(CLAUDE.md, «Conteo: lo que encontró el recorrido end to end → Propuesto, no
hecho»). Este archivo fija la arquitectura nueva renderizando `conteo.js` en
Node con `util.js` REAL, y la política de palabras del servidor por AST.

| Antes | Ahora |
|---|---|
| 🧭 Líder · Conteos · ABC · 🗄️ Datos · 🎯 Definitivo · 📊 Estadísticas | 📥 Por decidir · 📋 Conteos · ⚙️ Plan ABC · 📊 Estadísticas · (🗄️ Datos, al final: no es del conteo) |
| Se aprobaba desde Conteos → Acción (modal), desde el resultado del definitivo y desde el tablero — y solo el tablero conocía el tope en pesos de quien mira | Una puerta: `liderAprobarAjuste` en 📥 Por decidir, con `no_puede_aprobar` por fila. Las otras llevan hasta allá |
| Bloqueados y «mercancía sin código» en Líder **y** en Definitivo (dos endpoints GET para la copia) | Solo en el tablero; `GET /bloqueados` y `GET /novedades` retirados |
| La cola de definitivos en su pestaña; el tablero solo nombraba el CC3 de las auditorías | Bloque «Conteos definitivos por contar» del tablero (todos los CC3 del almacén), con «Contar ahora» que abre el HUD |
| Estadísticas: `sin_veredicto: pendiente 3`, `Excluidos — sin_foto_siesa: 2`, `Clase A · DIARIO_ABC`, `SUPERA_TOPE` | Palabras de bodega que manda el servidor (`metricas.conteo.ETIQUETAS`) |
| Tarjetas con «Op #4» | El nombre (`to_dict.operario_nombre`) |
| «📍 SIESA-GENERAL» | «Buscalo en toda la bodega» (`Ubicacion.es_fisica`, servido como `ubicacion_fisica`) |
"""
import ast
import json
import pathlib
import re
import shutil
import subprocess

import pytest

from tests.test_frontend_integrity import _sin_comentarios, _trocear

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
METRICAS = RAIZ / 'app' / 'services' / 'metricas' / 'conteo.py'


def _node(programa, *args):
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', programa, '--', str(PWA), *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _texto(html):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)
                  .replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&'))


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Las pestañas
# ─────────────────────────────────────────────────────────────────────────────

class TestLasPestanas:

    def _tira(self):
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        ini = html.index('<div id="tab-inventario"')
        fin = html.index('id="inv-panel-conteos"', ini)
        return html[ini:fin]

    def test_cuatro_de_conteo_y_datos_al_final(self):
        orden = re.findall(r'id="inv-tab-(\w+)" onclick="invSubtab\(\'(\w+)\'\)"', self._tira())
        assert [a for a, b in orden if a == b] == [a for a, _ in orden], orden
        assert [a for a, _ in orden] == ['lider', 'conteos', 'abc', 'estadisticas', 'datos'], orden

    def test_no_hay_pestana_ni_panel_definitivo(self):
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        assert 'inv-tab-definitivo' not in html and 'inv-panel-definitivo' not in html
        # El KPI del dashboard lleva a donde ahora se cuentan.
        tarjeta = re.search(r'<div class="kpi-card" id="kpi-card-definitivos"[^>]*>', html).group(0)
        assert "invSubtab('lider')" in tarjeta

    def test_los_nombres_dicen_para_que_son(self):
        tira = _texto(self._tira())
        for nombre in ('Por decidir', 'Conteos', 'Plan ABC', 'Estadísticas'):
            assert nombre in tira, (nombre, tira)
        assert 'Líder' not in tira

    def test_un_enlace_viejo_a_definitivo_abre_por_decidir(self):
        r = _node(r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const els = {}; const el = id => (els[id] = els[id] || { id, style: {} });
const llamado = [];
const ctx = { console, window: {}, document: { getElementById: el } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
ctx.__l = llamado;
vm.runInContext('liderCargar = () => __l.push("lider");', ctx);
vm.runInContext("invSubtab('definitivo')", ctx);
console.log(JSON.stringify({ llamado, panel: els['inv-panel-lider'].style.display, sub: vm.runInContext('_INV_SUBTAB', ctx) }));
""")
        assert r == {'llamado': ['lider'], 'panel': 'block', 'sub': 'lider'}, r


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Una sola puerta para aprobar
# ─────────────────────────────────────────────────────────────────────────────

_URL_AJUSTAR = re.compile(r'/api/conteo/\$\{[^}]+\}/ajustar')


def _puertas_de_aprobar(fuentes):
    """Funciones del PWA que ARMAN la URL que aprueba un ajuste."""
    puertas = set()
    for nombre_archivo, src in fuentes.items():
        cuerpos, arranque = _trocear(src)
        for fn, cuerpo in cuerpos.items():
            if _URL_AJUSTAR.search(_sin_comentarios(cuerpo)):
                puertas.add(fn)
        if _URL_AJUSTAR.search(_sin_comentarios(arranque)):
            puertas.add(f'<arranque de {nombre_archivo}>')
    return puertas


class TestUnaSolaPuertaParaAprobar:

    def test_solo_el_tablero_aprueba(self):
        fuentes = {f.name: f.read_text(encoding='utf-8') for f in sorted(PWA.glob('*.js'))}
        assert _puertas_de_aprobar(fuentes) == {'liderAprobarAjuste'}

    def test_el_detector_ve_una_segunda_puerta(self):
        src = ('async function a(id) { await put(`/api/conteo/${id}/ajustar`, {}); }\n'
               'async function b(r) { return put(`/api/conteo/${r.raiz_id}/ajustar`); }\n'
               'function c() { /* `/api/conteo/${x}/ajustar` en un comentario no cuenta */ }\n')
        assert _puertas_de_aprobar({'x.js': src}) == {'a', 'b'}

    def test_el_modal_viejo_no_volvio(self):
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        js = (PWA / 'conteo.js').read_text(encoding='utf-8')
        for viejo in ('modal-conteo-ajuste', 'conteoConfirmarAjuste', 'defAprobarAjuste',
                      'conteoAbrirAjuste'):
            assert viejo not in html and not re.search(rf'function {viejo}\b', js), viejo


_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], modo = args[1] || '';
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, value: '', innerHTML: '', options: [] });
const llamadas = { put: [], confirmar: [], texto: [], definitivo: [] };
const ctx = { console, window: {}, document: { getElementById: el },
  OPERARIO: { puede_usar_camara: false }, TAREA_ACTUAL: null,
  alerta: () => {}, liderCargar: async () => {},
  put: async (url) => { llamadas.put.push(url); return { mensaje: 'ok' }; },
  _modalConfirmar: async (msg, opts) => { llamadas.confirmar.push([msg, opts || {}]); return modo === 'acepta'; },
  _modalTexto: async (t, msg, opts) => { llamadas.texto.push([t, opts || {}]); return null; } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
ctx.__ll = llamadas;
vm.runInContext('liderCargar = async () => {}; defAbrirConteo = (id) => __ll.definitivo.push(id);', ctx);
const X = '<img src=x onerror=alert(1)>';
const texto = h => h.replace(/<[^>]+>/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/\s+/g, ' ');

// Conteos: una raíz contada con diferencia, sin nombre de operario y en la
// ubicación que no es un lugar.
const raiz = { id: 11, codigo: 'CC-11', tipo: 'DIARIO_ABC', estado: 'DESCUADRE', clasificacion_abc: 'B',
  producto_codigo: 'P1', producto_nombre: 'CUADERNO', ubicacion_codigo: 'SIESA-GENERAL', ubicacion_fisica: false,
  bodega_siesa_id: 'NB1', existencia_siesa: 10, teorico_siesa: 10, cantidad_fisica: 8, diferencia: -2,
  motivo_codigo: 'AJ-SAL', operario_id: 4, operario_nombre: null, bloqueo_ajuste: null,
  segundo_conteo: { id: 12, estado: 'DESCUADRE', cantidad_fisica: 8, diferencia: -2, teorico_siesa: 10,
    operario_id: 5, operario_nombre: null } };
const card = vm.runInContext('_renderCardAccion', ctx)(raiz);
const prog = vm.runInContext('_renderCardProgreso', ctx)({ ...raiz, id: 13, estado: 'EN_PROCESO', segundo_conteo: null });
const fisica = vm.runInContext('_renderCardProgreso', ctx)({ ...raiz, id: 14, estado: 'PENDIENTE', ubicacion_codigo: 'A-01-03', ubicacion_fisica: true, operario_id: null, segundo_conteo: null });

// El resultado del conteo definitivo que deja un ajuste por decidir.
vm.runInContext('_defMostrarResultado', ctx)({ resultado: 'DESCUADRE', raiz_id: 11, mensaje: 'listo' });
const resultadoDef = els['def-modal-contenido'].innerHTML;

// El tablero.
const permisos = modo === 'sin-permisos' ? {} : { contar_definitivo: true, aprobar_ajuste: true, cancelar_conteo: true };
const vacio = vm.runInContext('liderTableroHtml', ctx)({ permisos, resumen: { decisiones_pendientes: 0, por_bloque: {} },
  decisiones: {}, hoy: {} });
const d = { almacen_id: 1, permisos, resumen: { decisiones_pendientes: 3, por_bloque: { definitivos: 1, ajustes: 2 } },
  decisiones: {
    definitivos: { total: 1, filas: [{ id: 31, producto_codigo: 'P9', producto_nombre: 'LAPIZ', ubicacion_codigo: 'SIESA-GENERAL', ubicacion_fisica: false, es_auditoria: true }] },
    ajustes: { total_descuadres: 2, aprobables: { total: 2, filas: [
      { id: 11, producto_codigo: 'P1', direccion: 'SALIDA', unidades: 2, valor: 4000, teorico: 10, contado: 8,
        decidio: 'El 2º conteo confirmó al 1º (Ana y Beto)', no_puede_aprobar: null },
      { id: 15, producto_codigo: 'P2', direccion: 'ENTRADA', unidades: 1, valor: 900000, teorico: 3, contado: 4,
        decidio: X, no_puede_aprobar: 'El ajuste vale $900.000 y supera tu tope de $50.000' }] },
      bloqueados: { total: 0, filas: [] } } },
  hoy: { por_persona: { filas: [{ operario_id: 4, nombre: null, cadenas: 1, conteos: 1 }] } } };
const tablero = vm.runInContext('liderTableroHtml', ctx)(d);
vm.runInContext('_LIDER_DATOS = ' + JSON.stringify(d), ctx);
const onclicks = [...tablero.matchAll(/onclick="([^"]*)"/g)].map(m => m[1]);

(async () => {
  for (const o of onclicks.filter(o => /liderContarDefinitivo|liderAprobarAjuste/.test(o))) await vm.runInContext(o, ctx);
  console.log(JSON.stringify({
    card: texto(card), cardHtml: card, prog: texto(prog), fisica: texto(fisica),
    resultadoDef: texto(resultadoDef), resultadoDefHtml: resultadoDef,
    vacio: texto(vacio), tablero: texto(tablero), tableroHtml: tablero, onclicks, llamadas,
    crudos: (tablero.match(/<img/g) || []).length,
  }));
})();
"""


@pytest.fixture(scope='module')
def r():
    return _node(_ARNES)


class TestLasOtrasPuertasLlevanAlTablero:

    def test_la_tarjeta_de_conteos_lleva_a_decidir(self, r):
        assert 'Decidir en 📥 Por decidir' in r['card']
        assert 'conteoIrADecidir()' in r['cardHtml'] and '/ajustar' not in r['cardHtml']

    def test_el_resultado_del_definitivo_no_aprueba(self, r):
        assert 'Ajustes esperando decisión' in r['resultadoDef']
        assert 'Aprobar ajuste y enviar' not in r['resultadoDef']
        assert 'defCerrarModal()' in r['resultadoDefHtml']


class TestElTableroEsElLugarDeDecidir:

    def test_contar_ahora_abre_el_hud_del_definitivo(self, r):
        assert 'Conteos definitivos por contar' in r['tablero'] and 'Contar ahora' in r['tablero']
        assert r['llamadas']['definitivo'] == [31]

    def test_la_fila_dice_que_se_conto_y_que_manda(self, r):
        assert 'Siesa decía 10 · se contaron 8' in r['tablero']
        assert 'El 2º conteo confirmó al 1º (Ana y Beto)' in r['tablero']

    def test_el_tope_reemplaza_al_boton_en_su_fila(self, r):
        assert 'supera tu tope' in r['tablero']
        aprobar = [o for o in r['onclicks'] if o.startswith('liderAprobarAjuste(')]
        assert aprobar == ['liderAprobarAjuste(11)'], aprobar

    def test_aprobar_pide_confirmar_y_dice_que_no_se_deshace(self, r):
        (msg, opts), = r['llamadas']['confirmar']
        assert 'No se deshace' in msg and 'Siesa decía 10 · se contaron 8' in msg
        assert opts.get('textoCancelar') == 'Volver'
        assert r['llamadas']['put'] == [], 'se envió a Siesa sin confirmar'

    def test_confirmado_manda_la_fila(self):
        assert _node(_ARNES, 'acepta')['llamadas']['put'] == ['/api/conteo/11/ajustar']

    def test_no_ajustar_esta_al_lado_de_aprobar(self, r):
        assert 'liderCancelarConteo(11)' in r['onclicks'] and 'liderCancelarConteo(15)' in r['onclicks']

    def test_sin_permiso_no_hay_botones(self):
        s = _node(_ARNES, 'sin-permisos')
        assert not [o for o in s['onclicks'] if o.startswith(('liderAprobarAjuste', 'liderContarDefinitivo',
                                                              'liderCancelarConteo'))], s['onclicks']
        assert 'Lo cuenta un supervisor' in s['tablero']

    def test_todo_llega_escapado(self, r):
        assert r['crudos'] == 0 and '&lt;img' in r['tableroHtml']


class TestEstadosVacios:

    def test_nada_pendiente_lo_dice(self, r):
        assert 'Nada pendiente ✓' in r['vacio']
        for vacio in ('Ningún conteo bloqueado ✓', 'Nada reportado ✓',
                      'Ningún conteo definitivo pendiente ✓', 'Ningún ajuste esperando ✓'):
            assert vacio in r['vacio'], vacio

    def test_conteos_vacios_dicen_algo_util(self):
        js = (PWA / 'conteo.js').read_text(encoding='utf-8')
        cuerpo = js[js.index('async function cargarConteos'):js.index('async function conteosMostrarFormManual')]
        assert 'Nada pendiente de contar ✓' in cuerpo
        assert 'Nada con diferencia ni esperando recuento ✓' in cuerpo


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Personas y lugares, no números y códigos
# ─────────────────────────────────────────────────────────────────────────────

class TestNombresYLugares:

    def test_sin_op_numero(self, r):
        for vista in ('card', 'prog', 'tablero'):
            assert not re.search(r'Op\s*#|#\s*4\b', r[vista]), (vista, r[vista])
        assert 'sin nombre registrado' in r['prog']
        assert 'Sin nombre registrado' in r['tablero']

    def test_ningun_fallback_a_numero_en_el_codigo(self):
        js = _sin_comentarios((PWA / 'conteo.js').read_text(encoding='utf-8'))
        assert not re.search(r"'(?:Op )?#'\s*\+", js)

    def test_ubicacion_que_no_es_un_lugar(self, r):
        for vista in ('card', 'prog', 'tablero'):
            assert 'Buscalo en toda la bodega' in r[vista], vista
            assert 'SIESA-GENERAL' not in r[vista], vista
        assert 'A-01-03' in r['fisica'] and 'lo toma el próximo libre' in r['fisica']

    def test_el_servidor_manda_nombre_y_si_la_ubicacion_es_fisica(self, app, db):
        """`to_dict` y la vista ciega llevan lo que la pantalla necesita para
        no inventarlo: el nombre y `ubicacion_fisica` (de `Ubicacion.es_fisica`)."""
        from datetime import datetime
        from werkzeug.security import generate_password_hash
        from app.models.almacen import Almacen
        from app.models.conteo import SesionConteo
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        from app.models.usuario import Usuario
        alm = Almacen(nombre='IA', codigo='IA1')
        db.session.add(alm)
        db.session.flush()
        u = Usuario(nombre='Ana Contadora', email='ana@ia.test', rol='operario', activo=True,
                    password_hash=generate_password_hash('x'), almacen_id=alm.id)
        gen = Ubicacion(codigo=Ubicacion.CODIGO_GENERAL, almacen_id=alm.id)
        p = Producto(codigo='IA-P1', nombre='Lápiz')
        db.session.add_all([u, gen, p])
        db.session.flush()
        s = SesionConteo(codigo='IA-1', tipo='MANUAL', ubicacion_id=gen.id, almacen_id=alm.id,
                         producto_id=p.id, estado='PENDIENTE', operario_id=u.id,
                         fecha_creacion=datetime.utcnow())
        db.session.add(s)
        db.session.commit()
        d = s.to_dict()
        assert d['operario_nombre'] == 'Ana Contadora'
        assert d['ubicacion_fisica'] is False
        assert s.to_dict_operario()['ubicacion_fisica'] is False


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Estadísticas en palabras de bodega
# ─────────────────────────────────────────────────────────────────────────────

def _claves_que_escribe(src: str) -> set:
    """Toda clave LITERAL que el módulo de métricas agrupa y la pantalla pinta,
    leída por AST (un detector de texto se atrapa en sus propios docstrings):

    · `excl['x'] += 1` y los dict literales de `'excluidos': {...}` / `excl = {...}`;
    · lo que devuelve `motivo_sin_veredicto` (las claves de `sin_veredicto`);
    · las claves de `_CLAVES_BLOQUEO` (el `por_motivo` de los bloqueados);
    · los literales de `grupo = ...` (los grupos de la exactitud) y el `or '?'`;
    · los literales dentro de un subíndice de `esperan_aprobacion[...]`.
    """
    arbol = ast.parse(src)
    claves = set()

    def _literales(nodo):
        return {n.value for n in ast.walk(nodo) if isinstance(n, ast.Constant) and isinstance(n.value, str)}

    def _dicts(nodo):
        out = set()
        for n in ast.walk(nodo):
            if isinstance(n, ast.Dict):
                out |= {k.value for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        return out

    for n in ast.walk(arbol):
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == 'excl':
            claves |= {c for c in _literales(n.slice)}
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == 'esperan_aprobacion':
            claves |= {c for c in _literales(n.slice) if c.isupper()}
        if isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if isinstance(k, ast.Constant) and k.value == 'excluidos':
                    claves |= _dicts(v)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in ('excl', 'grupo')
                                              for t in n.targets):
            nombre = next(t.id for t in n.targets if isinstance(t, ast.Name))
            claves |= _dicts(n.value) if nombre == 'excl' else _literales(n.value)
        if isinstance(n, ast.FunctionDef) and n.name == 'motivo_sin_veredicto':
            claves |= {r.value.value for r in ast.walk(n)
                       if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)}
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_CLAVES_BLOQUEO'
                                              for t in n.targets):
            claves |= {e.elts[1].value for e in n.value.elts}
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            claves |= {v.value for v in n.values if isinstance(v, ast.Constant) and v.value == '?'}
    return claves


class TestLasClavesTienenPalabras:

    def test_toda_clave_que_el_reporte_escribe_tiene_su_texto(self):
        from app.services import conteo_politica as politica
        from app.services.metricas.conteo import ETIQUETAS
        claves = _claves_que_escribe(METRICAS.read_text(encoding='utf-8'))
        claves |= {'OTRO', politica.SIN_COSTO, politica.SUPERA_TOPE}
        assert len(claves) >= 30, f'piso: el escáner dejó de ver claves ({sorted(claves)})'
        faltan = sorted(c for c in claves if c not in ETIQUETAS)
        assert faltan == [], f'claves sin palabras de bodega en metricas.conteo.ETIQUETAS: {faltan}'

    def test_el_escaner_ve_una_clave_nueva(self):
        src = ("def f(c):\n    excl = Counter()\n    excl['clave_nueva'] += 1\n"
               "    grupo = 'NUEVO' if c else 'OTRO_GRUPO'\n"
               "    return {'excluidos': {'otra_mas': 1} if c else {}}\n"
               "def motivo_sin_veredicto(r):\n    return 'nuevo_motivo'\n"
               "_CLAVES_BLOQUEO = (('frase', 'CLAVE_BLOQ'),)\n")
        assert _claves_que_escribe(src) == {'clave_nueva', 'NUEVO', 'OTRO_GRUPO', 'otra_mas',
                                            'nuevo_motivo', 'CLAVE_BLOQ'}

    def test_ninguna_etiqueta_es_la_clave(self):
        from app.services.metricas.conteo import ETIQUETAS
        assert not [k for k, v in ETIQUETAS.items() if not v or v == k or '_' in v]


_ARNES_EST = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], rutaReporte = args[1];
const ctx = { console, window: {}, document: { getElementById: () => null } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
const d = JSON.parse(fs.readFileSync(rutaReporte, 'utf8'));
const html = vm.runInContext('_ceRender', ctx)(d);
console.log(JSON.stringify({ texto: html.replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ') }));
"""

#: Una clave cruda: `snake_case` o `MAYÚSCULAS_CON_GUION`.
_CLAVE_CRUDA = re.compile(r'(?<![\w-])(?:[a-z]+(?:_[a-z]+)+|[A-Z]{2,}(?:_[A-Z]+)+|OTROS|OTRO|FALTANTE)(?![\w-])')


def _reporte_con_todas_las_claves():
    """Un reporte con la FORMA real y, en cada contenedor, todas las claves
    de `ETIQUETAS` — lo peor que la pantalla puede recibir."""
    from app.services.metricas.conteo import ETIQUETAS
    todas = {k: 1 for k in ETIQUETAS if k not in ('DIARIO_ABC', 'OTROS', '?')}
    m = {'numerador': 1, 'denominador': 2, 'porcentaje': None, 'excluidos': todas,
         'sin_porcentaje_por': 'muestra chica'}
    return {
        'parametros': {'desde': '2026-09-01', 'hasta': '2026-09-24'}, 'fuente': 'solo base del WMS',
        'etiquetas': dict(ETIQUETAS),
        'carga_cobertura': {'al_dia_operativo': '2026-09-24', 'nota': 'foto de hoy', 'por_almacen': [],
                            'filas': [], 'excluidos': todas},
        'rezago': {'pendiente': {'0-2': 1}, 'en_curso': {'0-2': 1}, 'total_pendiente': 1,
                   'total_en_curso': 1, 'excluidos': todas},
        'volumen': {'unidad': 'cadena', 'iniciadas': 3, 'cerradas': 1, 'skus_cerrados': 1, 'a_cc2': m,
                    'a_cc3': m, 'sin_veredicto': todas, 'recuentos': m, 'recuentos_propios': 0,
                    'por_semana': [], 'excluidos': todas},
        'ajustes': {'cantidad': 1, 'valor': {'etiqueta': 'estimado'}, 'excluidos': todas,
                    'bloqueados_hoy': {'total': 1, 'por_motivo': todas, 'descuadres_aprobables': 1,
                                       'aprobables_por_motivo': todas},
                    'motivos_auditoria_picking': {'por_motivo': todas}},
        'exactitud': {'definicion': 'OK / (OK + ERROR)', 'min_n': 30,
                      'por_clase': {'A': {'DIARIO_ABC': m, 'OTROS': m}, '?': {'OTROS': m}},
                      'excluidos': todas,
                      'con_tolerancia': {'definicion': 'ASCM', 'por_clase': {'B': {'DIARIO_ABC': m}},
                                         'tolerancia_vigente': {}, 'primer_conteo_fuera': {},
                                         'excluidos': todas}},
        'productos_problema': {'por_diferencia': [], 'por_ajustes': [], 'por_recuentos': []},
        'por_operario': {'nota': 'solo volumen', 'filas': [{'operario_id': 4, 'nombre': None,
                                                            'cadenas': 1, 'conteos': 1}],
                         'excluidos': todas},
    }


def _render_estadisticas(tmp_path, reporte):
    ruta = tmp_path / 'reporte.json'
    ruta.write_text(json.dumps(reporte), encoding='utf-8')
    return _node(_ARNES_EST, str(ruta))['texto']


class TestEstadisticasSinClavesCrudas:

    def test_ninguna_clave_llega_cruda(self, tmp_path):
        texto = _render_estadisticas(tmp_path, _reporte_con_todas_las_claves())
        crudas = sorted(set(_CLAVE_CRUDA.findall(texto)))
        assert crudas == [], (crudas, texto[:600])
        for palabras in ('sin foto de Siesa al contar', 'Clase A · plan ABC',
                         'sin clase · manual, watchdog y auditorías', 'superan el tope automático',
                         'Sin nombre registrado'):
            assert palabras in texto, palabras
        assert '#4' not in texto

    def test_el_detector_muerde_sin_etiquetas(self, tmp_path):
        """Sin el vocabulario del servidor la pantalla cae a la clave con
        espacios: el detector tiene que verla igual en los grupos y motivos."""
        rep = _reporte_con_todas_las_claves()
        rep['etiquetas'] = {}
        texto = _render_estadisticas(tmp_path, rep)
        assert 'sin foto siesa' in texto and 'Clase A · diario abc' in texto

    def test_el_detector_de_claves_crudas_muerde(self):
        assert _CLAVE_CRUDA.findall('Excluidos — sin_foto_siesa: 2 · Clase A · DIARIO_ABC · OTROS') == [
            'sin_foto_siesa', 'DIARIO_ABC', 'OTROS']
        assert _CLAVE_CRUDA.findall('sin foto de Siesa · plan ABC · 0-2 d') == []


class TestElReporteRealTraeSusPalabras:

    def test_cada_clave_del_reporte_tiene_texto_propio(self, mundo, tienda):
        """Sobre el mundo del tablero (bloqueados, definitivos, ajustes,
        auditorías, rechazos): toda clave que el reporte agrupa está en
        `ETIQUETAS`, no en la palabra suelta de respaldo."""
        from app.services.metricas.conteo import ETIQUETAS, calcular_estadisticas_conteo
        rep = calcular_estadisticas_conteo(almacen_id=tienda['almacen'].id)
        vistas = set()

        def _recorrer(nodo, padre=None):
            if isinstance(nodo, dict):
                for k, v in nodo.items():
                    if padre in ('excluidos', 'sin_veredicto', 'por_motivo', 'aprobables_por_motivo'):
                        vistas.add(k)
                    _recorrer(v, k)
            elif isinstance(nodo, list):
                for v in nodo:
                    _recorrer(v, padre)
        _recorrer({k: v for k, v in rep.items() if k != 'etiquetas'})
        for porc in (rep['exactitud'], rep['exactitud']['con_tolerancia']):
            for cl, grupos in porc['por_clase'].items():
                vistas |= set(grupos)
        assert len(vistas) >= 3, f'piso: el mundo no produjo claves ({vistas})'
        assert sorted(k for k in vistas if k not in ETIQUETAS) == []
        assert all(rep['etiquetas'][k] == ETIQUETAS[k] for k in vistas)


from tests.test_conteo_teorico_pos import siesa, tienda  # noqa: E402,F401 (fixtures)
from tests.test_tablero_lider_conteo import mundo  # noqa: E402,F401 (fixture)
