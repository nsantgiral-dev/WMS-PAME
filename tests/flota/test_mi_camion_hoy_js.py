"""«Mi camión hoy» — la pantalla del conductor, EJECUTADA en Node con `util.js` real.

## Lo que se afirma

1. **Un botón por estado.** Sin turno → «Recibir». Turno sin inspección de hoy →
   «Inspeccionar». Inspeccionado → una franja (placa · semáforo · «Más»). Lo que
   espera en la cola del teléfono cuenta: un recibo tomado sin señal ya es un
   turno abierto para la pantalla.
2. **El semáforo es UNA línea** cuando hay algo, y no aparece cuando no hay nada.
3. **Ningún código crudo** en lo que el conductor lee (`no_apto`, `soat`,
   `efectivo_conductor`…), y nada pintado con hex ni bajo 12 px.
4. **Ningún control nace marcado**: ni la gravedad del daño, ni «lleno», ni con
   qué se pagó.
5. **La cola sin señal**: la operación se guarda con sus fotos ANTES de salir; un
   reenvío lleva **la misma clave** (el servidor la usa para no duplicar, ver
   `test_cola_del_conductor.py`); la cola sale en orden y se detiene en la
   primera que no sale; un rechazo no se pierde.
6. **El kilometraje se pide una vez**: inspección, daño y tanqueo lo heredan.
7. **La fecha del tanqueo es la de Bogotá**, también a las 9 p. m.

## Lo que NO se afirma

Que las rutas se vean sin bajar en un 390×844: Node no hace layout. Se mide un
proxy —cuántos controles tiene la tarjeta en cada estado, y que no use los
`btn-flota` de 54 px apilados— y se declara acá que es un proxy.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const GUION = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

const nodos = new Map();
function nodo(id) {
  const n = {
    _id: id, _html: '', textContent: '', value: '', disabled: false, dataset: {},
    style: { cssText: '', display: '' }, files: null,
    classList: { _c: new Set(), add(c) { this._c.add(c); }, remove(c) { this._c.delete(c); },
                 contains(c) { return this._c.has(c); } },
    appendChild() {}, addEventListener() {}, remove() {}, focus() {}, click() {},
    get id() { return this._id; },
    set id(v) { this._id = v; nodos.set(v, this); },
    get innerHTML() { return this._html; },
    set innerHTML(v) {
      this._html = String(v);
      for (const m of this._html.matchAll(/id="([a-zA-Z0-9_-]+)"/g)) {
        if (!nodos.has(m[1])) nodos.set(m[1], nodo(m[1]));
        const pl = new RegExp('id="' + m[1] + '"[^>]*data-placa="([^"]*)"').exec(this._html);
        if (pl) nodos.get(m[1]).dataset.placa = pl[1];
        // El `value="…"` de un input, como lo leería el navegador.
        const va = new RegExp('id="' + m[1] + '"[^>]*\\svalue="([^"]*)"').exec(this._html);
        if (va) nodos.get(m[1]).value = va[1];
      }
    },
  };
  return n;
}
// `_condDB` en memoria, con la misma interfaz que el de rutas.js.
const ALMACEN = {};
const condDB = GUION.sin_almacen ? undefined : {
  async get(k) { return k in ALMACEN ? JSON.parse(JSON.stringify(ALMACEN[k])) : null; },
  async set(k, v) { ALMACEN[k] = JSON.parse(JSON.stringify(v)); },
};
// La red: una respuesta por llamada, en orden. 'SIN_RED' lanza.
const RESPUESTAS = GUION.respuestas || [];
const ENVIOS = [];
const ALERTAS = [];
const ctx = {
  console,
  document: {
    getElementById: (id) => { if (!nodos.has(id)) nodos.set(id, nodo(id)); return nodos.get(id); },
    createElement: () => nodo(null),
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener() {}, body: { appendChild() {}, style: {} },
  },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  alerta: (m, t) => ALERTAS.push([m, t]), TOKEN: 'x', API: '',
  confirm: () => true, horaColombia: (x) => String(x),
  get: async (url) => {
    if (GUION.get && url in GUION.get) return JSON.parse(JSON.stringify(GUION.get[url]));
    const e = new Error('Sin señal'); throw e;
  },
  fetch: async (url, opts) => {
    ENVIOS.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
    const r = RESPUESTAS.length ? RESPUESTAS.shift() : 'SIN_RED';
    if (r === 'SIN_RED') throw new TypeError('Failed to fetch');
    return { ok: r.status >= 200 && r.status < 300, status: r.status,
             json: async () => r.json || {} };
  },
  _condDB: condDB,
  crypto: { randomUUID: (() => { let i = 0; return () => 'clave-' + (++i); })() },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(PWA + '/util.js', 'utf-8'), ctx, { filename: 'util.js' });
vm.runInContext(fs.readFileSync(PWA + '/flota.js', 'utf-8'), ctx, { filename: 'flota.js' });
ctx.__nodos = nodos; ctx.__ALMACEN = ALMACEN; ctx.__ENVIOS = ENVIOS; ctx.__ALERTAS = ALERTAS;
(async () => {
  vm.runInContext(GUION.semilla || '', ctx, { filename: 'semilla.js' });
  const salida = await vm.runInContext(`(async () => (${GUION.expr}))()`, ctx, { filename: 'expr.js' });
  process.stdout.write(JSON.stringify({ salida: salida === undefined ? null : salida }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _correr(tmp_path, expr, semilla='', respuestas=None, get=None, sin_almacen=False):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'semilla': semilla, 'expr': expr,
                             'respuestas': respuestas or [], 'get': get or {},
                             'sin_almacen': sin_almacen}), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f'la pantalla reventó:\n{p.stderr}'
    return json.loads(p.stdout)['salida']


def _turno(**extra):
    """La forma EXACTA de `GET /flota/conductor/mi-turno` (ver el contrato abajo)."""
    d = {
        'conductor': {'id': 7, 'nombre': 'Yesid'},
        'estado_vehiculo': {
            'hallazgos_abiertos': 0, 'hallazgos_vencidos': 0, 'hallazgo_peor': None,
            'inspeccion_de_hoy': {'hecha': False, 'veredicto': None, 'habilita_despacho': None},
            'preventivo_vencido': [], 'documentos_vencidos': [],
        },
        'rendimiento': None, 'origen': 'ruta', 'vehiculo_id': 3, 'placa': 'THP696',
        'requiere_confirmacion': True, 'odometro_actual': 120500,
        'tiene_turno_abierto': False,
        'candidatos': [{'vehiculo_id': 3, 'placa': 'THP696', 'tipo': 'NHR',
                        'ocupado_por': None}],
    }
    for k, v in extra.items():
        if k == 'inspeccion':
            d['estado_vehiculo']['inspeccion_de_hoy'] = v
        elif k in d['estado_vehiculo']:
            d['estado_vehiculo'][k] = v
        else:
            d[k] = v
    return d


def _tarjeta(tmp_path, d, cola=None, rechazos=None):
    return _correr(tmp_path, f'flotaCondTarjetaHTML({json.dumps(d)}, '
                             f'{json.dumps(cola or [])}, {json.dumps(rechazos or [])})')


def _visible(html):
    """Lo que el conductor LEE: sin etiquetas ni atributos."""
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', html or ''))


def _botones(html):
    return len(re.findall(r'<button\b', html))


# ═════════════════════════════════════════════════════════════════════════
# 1 · Un botón por estado
# ═════════════════════════════════════════════════════════════════════════

class TestUnBotonPorEstado:

    def test_sin_turno_el_boton_es_recibir(self, tmp_path):
        html = _tarjeta(tmp_path, _turno())
        assert 'Recibir el camión' in html
        assert 'onclick="flotaCondAbrirRecibo()"' in html
        assert html.count('btn-primary') == 1
        assert 'Inspeccionar' not in html

    def test_con_turno_y_sin_inspeccion_el_boton_es_inspeccionar(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(tiene_turno_abierto=True, origen='custodia'))
        assert 'onclick="flotaCondInspeccion()"' in html
        assert html.count('btn-primary') == 1
        assert 'Recibir' not in html

    def test_inspeccionado_se_encoge_a_una_franja(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(
            tiene_turno_abierto=True, origen='custodia',
            inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True}))
        assert 'flota-hoy-franja' in html
        assert 'THP696' in html
        assert 'onclick="flotaCondMasAbrir()"' in html
        assert 'btn-primary' not in html, 'la franja no lleva botón principal'
        assert _botones(html) == 1, 'la franja es placa · semáforo · «Más», nada más'

    def test_ninguna_tarjeta_apila_los_botones_de_54px(self, tmp_path):
        """PROXY del «las rutas se ven sin bajar en 390×844» (Node no hace
        layout): la tarjeta no usa los `btn-flota` de 54 px de alto que se
        apilaban seis, y tiene pocos controles en cada estado."""
        estados = [
            _turno(),
            _turno(tiene_turno_abierto=True, origen='custodia'),
            _turno(tiene_turno_abierto=True, origen='custodia',
                   inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True}),
        ]
        for d, tope in zip(estados, (3, 2, 1)):
            html = _tarjeta(tmp_path, d)
            assert 'btn-flota' not in html
            assert _botones(html) <= tope, (d['tiene_turno_abierto'], _botones(html))

    def test_la_hoja_mas_trae_los_cuatro_gestos_y_no_el_odometro(self, tmp_path):
        html = _correr(tmp_path, 'flotaCondHojaHTML(D, [])',
                       semilla=f'var D = {json.dumps(_turno(tiene_turno_abierto=True))};')
        for fn in ('flotaCondReportarDano', 'flotaCondTanquear',
                   'flotaCondAbrirEntrega', 'flotaCondMisReportes'):
            assert f'onclick="{fn}()"' in html, fn
        assert 'Odómetro' not in html and 'Odometro' not in html
        assert 'Corrección' not in html

    def test_el_conductor_ya_no_tiene_el_gesto_del_odometro(self):
        js = (PWA / 'flota.js').read_text(encoding='utf-8')
        assert 'function flotaCondOdometro' not in js
        assert 'flotaAbrirTanqueo' not in js


class TestLaColaCuentaParaElEstado:
    """El recibo tomado sin señal ya es un turno abierto para el conductor."""

    def _op(self, que, creado='2099-01-01T12:00:00Z'):
        return {'clave': 'k-' + que, 'tipo': 'traspaso', 'que': que, 'placa': 'THP696',
                'creado': creado, 'cuerpo': {'km': 120600}}

    def test_un_recibo_pendiente_pide_inspeccionar(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(), cola=[self._op('recibo')])
        assert 'onclick="flotaCondInspeccion()"' in html
        assert 'pendiente de sincronizar' in html
        assert 'recibo' in _visible(html)

    def test_una_entrega_pendiente_vuelve_a_pedir_recibir(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(tiene_turno_abierto=True, origen='custodia'),
                        cola=[self._op('entrega')])
        assert 'onclick="flotaCondAbrirRecibo()"' in html

    def test_un_rechazo_que_nadie_vio_queda_en_la_tarjeta(self, tmp_path):
        rech = [{'que': 'recibo', 'placa': 'THP696', 'creado': 'x',
                 'mensaje': 'el odómetro no puede retroceder'}]
        html = _tarjeta(tmp_path, _turno(), rechazos=rech)
        assert 'el odómetro no puede retroceder' in html
        assert 'flotaColaDescartarRechazo(0)' in html


# ═════════════════════════════════════════════════════════════════════════
# 2 · El semáforo es una línea
# ═════════════════════════════════════════════════════════════════════════

class TestElSemaforo:

    def _franja(self, tmp_path, **estado):
        return _tarjeta(tmp_path, _turno(
            tiene_turno_abierto=True, origen='custodia',
            inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True},
            **estado))

    def test_un_papel_vencido_sale_en_una_linea_roja_con_su_nombre(self, tmp_path):
        html = self._franja(tmp_path, documentos_vencidos=[
            {'tipo': 'soat', 'vencio': '2026-09-01'}])
        sem = re.findall(r'<span class="flota-sem[^"]*">(.*?)</span>', html)
        assert len(sem) == 1
        assert 'flota-sem-rojo' in html
        assert 'SOAT vencido' in sem[0]

    def test_un_dano_abierto_es_amarillo(self, tmp_path):
        html = self._franja(tmp_path, hallazgos_abiertos=2)
        assert 'flota-sem-amarillo' in html
        assert '2 daño(s) abierto(s)' in html

    def test_lo_mas_grave_va_primero_y_el_resto_se_cuenta(self, tmp_path):
        html = self._franja(tmp_path, hallazgos_abiertos=1, documentos_vencidos=[
            {'tipo': 'soat', 'vencio': '2026-09-01'}, {'tipo': 'rtm', 'vencio': '2026-08-01'}],
            preventivo_vencido=[{'tarea': 'Correa', 'faltan_km': -300}])
        sem = re.search(r'● ([^<]*)', html).group(1)
        assert sem.startswith('SOAT vencido')
        assert '+2' in sem

    def test_sin_avisos_no_hay_semaforo(self, tmp_path):
        """Una línea que siempre aparece se deja de leer."""
        html = self._franja(tmp_path)
        assert '●' not in html

    def test_no_bloquea(self, tmp_path):
        """Informa, no bloquea: con todo vencido el botón sigue ahí."""
        html = _tarjeta(tmp_path, _turno(documentos_vencidos=[
            {'tipo': 'soat', 'vencio': '2026-01-01'}], hallazgos_vencidos=3))
        assert 'Recibir el camión' in html
        assert 'disabled' not in html

    def test_lo_que_escribio_una_persona_llega_escapado(self, tmp_path):
        d = _turno(tiene_turno_abierto=True, hallazgo_peor={
            'criticidad': 'mayor', 'descripcion': '<img src=x onerror=alert(1)>',
            'vencido': False}, hallazgos_abiertos=1)
        html = _correr(tmp_path, 'flotaCondHojaHTML(D, [])', semilla=f'var D = {json.dumps(d)};')
        assert '<img src=x' not in html
        assert '&lt;img' in html


# ═════════════════════════════════════════════════════════════════════════
# 3 · Palabras, no códigos; legible al sol
# ═════════════════════════════════════════════════════════════════════════

CODIGOS = re.compile(r'\b[a-z]+_[a-z_]+\b')


def _pantallas(tmp_path):
    """Todo lo que el conductor lee en esta pantalla, en sus estados."""
    d_malo = _turno(tiene_turno_abierto=True, origen='custodia',
                    inspeccion={'hecha': True, 'veredicto': 'no_apto', 'habilita_despacho': False},
                    documentos_vencidos=[{'tipo': 'tarjeta_propiedad', 'vencio': '2026-01-01'},
                                         {'tipo': 'poliza_rc', 'vencio': '2026-01-01'}],
                    hallazgos_vencidos=1,
                    hallazgo_peor={'criticidad': 'bloqueante', 'descripcion': 'Freno',
                                   'vencido': True})
    semilla = (f'var D = {json.dumps(d_malo)};\n'
               f'FLOTA_COND = D; FLOTA_PLACA = "THP696";\n'
               f'FLOTA_DANO = {{foto: null, sinFoto: true, gravedad: null}};\n'
               f'FLOTA_TQ = {{foto: null, tanque: null, origen: null,'
               f' estados: ["lleno", "parcial"], origenes: ["tarjeta_convenio", '
               f'"credito_proveedor", "efectivo_conductor"]}};\n')
    return _correr(tmp_path, (
        '[flotaCondTarjetaHTML(D, [], []), flotaCondHojaHTML(D, []), flotaDanoHTML(),'
        ' flotaTanqueoHTML(), flotaCondInspeccionResultadoHTML({estado: "hecho",'
        ' datos: {veredicto: "no_apto", hallazgos: [1], habilita_despacho: false}}),'
        ' flotaCondInspeccionResultadoHTML({estado: "en_cola"}),'
        ' flotaMensajeDeError({error: "No", tu_rol: "conductor", roles_permitidos: ["control_flota", "jefe_almacen"]})]'),
        semilla=semilla)


class TestNingunCodigoCrudo:

    def test_lo_que_se_lee_no_tiene_codigos(self, tmp_path):
        for html in _pantallas(tmp_path):
            vis = _visible(html)
            crudos = [c for c in CODIGOS.findall(vis)]
            assert not crudos, f'códigos crudos a la vista: {crudos}\n{vis[:400]}'

    def test_los_codigos_salen_en_palabras(self, tmp_path):
        todo = ' '.join(_visible(h) for h in _pantallas(tmp_path))
        for palabra in ('Tarjeta de propiedad', 'Póliza de responsabilidad civil',
                        'no apto', 'Mi plata (efectivo)', 'Tarjeta de la empresa',
                        'control de flota', 'jefe de almacén', 'No puede salir'):
            assert palabra in todo, palabra

    def test_el_detector_de_codigos_muerde(self):
        assert CODIGOS.findall(_visible('<b>efectivo_conductor</b> y no_apto'))
        assert not CODIGOS.findall(_visible('<button onclick="x(\'no_apto\')">no apto</button>'))


class TestLegibleAlSol:

    def test_nada_pintado_con_hex_ni_bajo_12px(self, tmp_path):
        for html in _pantallas(tmp_path):
            assert not re.search(r'color\s*:\s*#', html), 'color de texto a mano'
            assert not re.search(r'background\s*:\s*#', html), 'fondo a mano'
            for px in re.findall(r'font-size\s*:\s*(\d+)px', html):
                assert int(px) >= 12

    def test_la_tarjeta_usa_clases_que_existen(self):
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        js = (PWA / 'flota.js').read_text(encoding='utf-8')
        usadas = {c for g in re.findall(r'class="([a-z0-9 -]+)"', js) for c in g.split()
                  if c.startswith('flota-')}
        definidas = set(re.findall(r'\.(flota-[a-z0-9-]+)', html))
        assert usadas <= definidas, sorted(usadas - definidas)


# ═════════════════════════════════════════════════════════════════════════
# 4 · Ningún control nace marcado
# ═════════════════════════════════════════════════════════════════════════

class TestNadaNaceMarcado:

    def test_el_dano_empieza_por_la_foto(self, tmp_path):
        html = _correr(tmp_path, 'flotaDanoHTML()', semilla=(
            'FLOTA_PLACA = "THP696"; FLOTA_DANO = {foto: null, sinFoto: false, gravedad: null};'))
        assert 'Foto del daño' in html and 'No puedo tomarla' in html
        assert 'flotaDanoGravedad' not in html, 'la gravedad va después de la foto'

    def test_la_gravedad_no_viene_elegida(self, tmp_path):
        html = _correr(tmp_path, 'flotaDanoHTML()', semilla=(
            'FLOTA_PLACA = "THP696"; FLOTA_DANO = {foto: null, sinFoto: true, gravedad: null};'))
        assert html.count('flotaDanoGravedad(') == 3
        assert 'flota-opcion ok' not in html
        assert 'selected' not in html and 'checked' not in html

    def test_por_la_puerta_tampoco_nace_elegida(self, tmp_path):
        """Por el GESTO, no por la pieza: abrir el daño desde la hoja «Más»
        deja la gravedad sin elegir. Un test que siembra `FLOTA_DANO` a mano
        quedaba en verde con el opener sembrando «mayor»."""
        s = _correr(tmp_path, '(async () => { await flotaCondReportarDano(); flotaDanoSinFoto();'
                              ' return {g: FLOTA_DANO.gravedad,'
                              ' html: document.getElementById("flota-recibo").innerHTML}; })()',
                    semilla=f'FLOTA_COND = {json.dumps(_turno(tiene_turno_abierto=True))};')
        assert s['g'] is None
        assert 'flota-opcion ok' not in s['html']
        assert s['html'].count('flotaDanoGravedad(') == 3

    def test_elegir_marca_solo_esa(self, tmp_path):
        html = _correr(tmp_path, '(flotaDanoGravedad("mayor"), flotaDanoHTML())', semilla=(
            'FLOTA_PLACA = "THP696"; FLOTA_DANO = {foto: null, sinFoto: true, gravedad: null};'))
        assert html.count('flota-opcion ok') == 1

    def test_el_tanqueo_no_trae_lleno_ni_pago_marcados(self, tmp_path):
        html = _correr(tmp_path, 'flotaTanqueoHTML()', semilla=(
            'FLOTA_PLACA = "THP696"; FLOTA_TQ = {foto: null, tanque: null, origen: null,'
            ' estados: ["lleno", "parcial"], origenes: ["tarjeta_convenio", "efectivo_conductor"]};'))
        assert 'flota-opcion ok' not in html
        assert 'selected' not in html and 'checked' not in html

    def test_el_tanqueo_del_conductor_no_ofrece_no_se(self, tmp_path):
        """Está frente al surtidor con el recibo en la mano."""
        html = _correr(tmp_path, '(await flotaCondTanquear(), document.getElementById("flota-recibo").innerHTML)',
                       semilla='FLOTA_COND = {placa: "THP696"};',
                       get={'/flota/vocabulario': {'estados_tanque': ['lleno', 'parcial', 'sin_dato'],
                                                   'origenes_costo': ['tarjeta_convenio', 'sin_dato'],
                                                   'categorias': [], 'categorias_con_periodo': [],
                                                   'categorias_de_campo': []}})
        assert "'sin_dato'" not in html
        assert 'Lo llené' in html and 'Tarjeta de la empresa' in html


# ═════════════════════════════════════════════════════════════════════════
# 5 · La cola sin señal
# ═════════════════════════════════════════════════════════════════════════

FOTO = {'dataUrl': 'data:image/jpeg;base64,AAAA', 'ancho': 800, 'alto': 600}


def _registrar(expr_extra=''):
    return ('(async () => { const r = await flotaColaRegistrar("traspaso", "recibo", "THP696",'
            f' {{placa: "THP696", km: 1, fotos_inicio: [flotaFotoPayload({json.dumps(FOTO)}, "evidencia_estado", "frontal")]}});'
            f' {expr_extra} return {{r, cola: await _condDB.get("flota_cola"), envios: __ENVIOS}}; }})()')


class TestLaColaSinSenal:

    def test_sin_senal_queda_guardada_con_sus_fotos(self, tmp_path):
        s = _correr(tmp_path, _registrar(), respuestas=['SIN_RED'])
        assert s['r']['estado'] == 'en_cola'
        assert len(s['cola']) == 1
        op = s['cola'][0]
        assert op['cuerpo']['fotos_inicio'][0]['data_url'].startswith('data:image/jpeg'), (
            'la foto quedó solo en memoria: al cerrar la pantalla se pierde')
        assert op['cuerpo']['clave_idempotencia'] == op['clave']
        assert op['cuerpo']['ts_dispositivo']

    def test_el_reenvio_lleva_la_misma_clave(self, tmp_path):
        """La mitad del cliente del «un reenvío no duplica»: la otra mitad —el
        servidor que reconoce la clave— está en `test_cola_del_conductor`."""
        s = _correr(tmp_path, _registrar('await flotaColaSincronizar();'),
                    respuestas=['SIN_RED', {'status': 201, 'json': {'custodia_id': 9}}])
        claves = [e['body']['clave_idempotencia'] for e in s['envios']]
        assert len(claves) == 2 and claves[0] == claves[1]
        assert s['cola'] == []

    def test_con_senal_sale_y_no_queda_nada(self, tmp_path):
        s = _correr(tmp_path, _registrar(), respuestas=[{'status': 201, 'json': {'custodia_id': 9}}])
        assert s['r']['estado'] == 'hecho'
        assert s['cola'] == []
        assert s['envios'][0]['url'] == '/flota/custodia/traspaso'

    def test_la_cola_sale_en_orden_y_se_detiene(self, tmp_path):
        """Una entrega no puede llegar antes que el recibo del mismo turno."""
        semilla = ('ALMACEN_INICIAL = [' + ','.join(
            f'{{clave: "k{i}", tipo: "traspaso", que: "{q}", placa: "THP696", creado: "x",'
            f' cuerpo: {{km: {i}, clave_idempotencia: "k{i}"}}}}'
            for i, q in ((1, 'recibo'), (2, 'entrega'))) + '];')
        s = _correr(tmp_path, '(async () => { await _condDB.set("flota_cola", ALMACEN_INICIAL);'
                              ' await flotaColaSincronizar();'
                              ' return {cola: await _condDB.get("flota_cola"), envios: __ENVIOS}; })()',
                    semilla=semilla, respuestas=['SIN_RED'])
        assert len(s['envios']) == 1, 'siguió mandando detrás de una que no salió'
        assert [o['clave'] for o in s['cola']] == ['k1', 'k2']

    def test_un_rechazo_en_segundo_plano_no_se_pierde(self, tmp_path):
        semilla = ('ALMACEN_INICIAL = [{clave: "k1", tipo: "hallazgo", que: "dano", placa: "THP696",'
                   ' creado: "x", cuerpo: {km: 1}}];')
        s = _correr(tmp_path, '(async () => { await _condDB.set("flota_cola", ALMACEN_INICIAL);'
                              ' await flotaColaSincronizar();'
                              ' return {cola: await _condDB.get("flota_cola"),'
                              ' rech: await _condDB.get("flota_cola_rechazos")}; })()',
                    semilla=semilla,
                    respuestas=[{'status': 409, 'json': {'error': 'el odómetro retrocede'}}])
        assert s['cola'] == []
        assert s['rech'][0]['mensaje'] == 'el odómetro retrocede'

    def test_un_500_se_queda_para_reintentar(self, tmp_path):
        s = _correr(tmp_path, _registrar(), respuestas=[{'status': 503, 'json': {}}])
        assert s['r']['estado'] == 'en_cola'
        assert s['cola'][0]['ultimo_error']

    def test_un_rechazo_con_el_formulario_abierto_se_dice_ahi(self, tmp_path):
        s = _correr(tmp_path, _registrar('const rech = await _condDB.get("flota_cola_rechazos");'
                                         ' r.rech = rech;'),
                    respuestas=[{'status': 409, 'json': {'error': 'ya lo tiene Ana'}}])
        assert s['r']['estado'] == 'rechazado'
        assert s['r']['mensaje'] == 'ya lo tiene Ana'
        assert not s['r']['rech'], 'se anotó dos veces: en el formulario y en la tarjeta'
        assert s['cola'] == []

    def test_sin_almacen_y_sin_senal_no_finge_que_guardo(self, tmp_path):
        s = _correr(tmp_path, '(async () => (await flotaColaRegistrar("hallazgo", "dano", "THP696",'
                              ' {placa: "THP696"})).estado)()',
                    respuestas=['SIN_RED'], sin_almacen=True)
        assert s == 'perdido'


# ═════════════════════════════════════════════════════════════════════════
# 6 · El kilometraje, una vez por turno
# ═════════════════════════════════════════════════════════════════════════

class TestElKmSeHereda:

    def test_la_inspeccion_hereda_el_del_ultimo_registro(self, tmp_path):
        html = _correr(tmp_path, 'flotaCondKmHTML("insp", flotaCondKmConocido("THP696"))',
                       semilla=f'FLOTA_COND = {json.dumps(_turno())};')
        assert '120.500 km' in html
        assert 'id="insp-km"' in html and 'value="120500"' in html
        assert 'Cambió' in html

    def test_lo_de_la_cola_manda_sobre_el_servidor(self, tmp_path):
        """El recibo se tomó sin señal: su km es el último que se leyó."""
        html = _correr(tmp_path, 'flotaCondKmHTML("hz", flotaCondKmConocido("THP696"))',
                       semilla=(f'FLOTA_COND = {json.dumps(_turno())};'
                                'FLOTA_COLA_ITEMS = [{que: "recibo", placa: "THP696",'
                                ' cuerpo: {km: 120777}}];'))
        assert 'value="120777"' in html

    def test_sin_dato_se_pide(self, tmp_path):
        """Regla 4: sin lecturas no es 0 km, es «no sé» — y entonces se pide."""
        html = _correr(tmp_path, 'flotaCondKmHTML("tq", flotaCondKmConocido("THP696"))',
                       semilla=f'FLOTA_COND = {json.dumps(_turno(odometro_actual="sin_dato"))};')
        assert 'Cambió' not in html and 'value=' not in html

    def test_el_dano_manda_el_km_heredado(self, tmp_path):
        s = _correr(tmp_path, (
            '(async () => { flotaAbrirModal("x", "THP696");'
            ' document.getElementById("flota-recibo").innerHTML = flotaDanoHTML();'
            ' document.getElementById("hz-km").value = "120500";'
            ' document.getElementById("hz-desc").value = "";'
            ' await flotaCondGuardarDano(); return __ENVIOS; })()'),
            semilla=(f'FLOTA_COND = {json.dumps(_turno(tiene_turno_abierto=True))};'
                     'FLOTA_PLACA = "THP696";'
                     f'FLOTA_DANO = {{foto: {json.dumps(FOTO)}, sinFoto: false, gravedad: "menor"}};'),
            respuestas=[{'status': 201, 'json': {'id': 1}}])
        cuerpo = s[0]['body']
        assert s[0]['url'] == '/flota/hallazgos'
        assert cuerpo['km'] == 120500 and cuerpo['criticidad'] == 'menor'
        assert len(cuerpo['fotos']) == 1, 'el daño salió sin su foto'
        assert cuerpo['descripcion'], 'con foto y sin texto la descripción no puede ir vacía'


# ═════════════════════════════════════════════════════════════════════════
# 7 · La fecha del tanqueo es la de Bogotá
# ═════════════════════════════════════════════════════════════════════════

class TestLaFechaEsDeBogota:

    def test_a_las_9_de_la_noche_sigue_siendo_hoy(self, tmp_path):
        # 2026-09-25 02:00 UTC = 2026-09-24 21:00 en Bogotá.
        s = _correr(tmp_path, 'flotaHoyBogota(new Date("2026-09-25T02:00:00Z"))')
        assert s == '2026-09-24'

    def test_el_tanqueo_manda_la_fecha_de_hoy_y_sus_cinco_datos(self, tmp_path):
        s = _correr(tmp_path, (
            '(async () => { flotaAbrirModal("x", "THP696");'
            ' document.getElementById("flota-recibo").innerHTML = flotaTanqueoHTML();'
            ' document.getElementById("tq-galones").value = "10.5";'
            ' document.getElementById("tq-valor").value = "150000";'
            ' document.getElementById("tq-estacion").value = "Terpel";'
            ' await flotaCondGuardarTanqueo(); return {envios: __ENVIOS, hoy: flotaHoyBogota()}; })()'),
            semilla=(f'FLOTA_COND = {json.dumps(_turno(tiene_turno_abierto=True))};'
                     'FLOTA_PLACA = "THP696";'
                     f'FLOTA_TQ = {{foto: {json.dumps(FOTO)}, tanque: "lleno",'
                     ' origen: "efectivo_conductor", estados: ["lleno"], origenes: []};'),
            respuestas=[{'status': 201, 'json': {'id': 3}}])
        cuerpo = s['envios'][0]['body']
        assert s['envios'][0]['url'] == '/flota/tanqueos'
        assert cuerpo['fecha'] == s['hoy']
        assert (cuerpo['galones'], cuerpo['valor'], cuerpo['tanque'], cuerpo['origen_costo'],
                cuerpo['estacion'], cuerpo['km']) == ('10.5', '150000', 'lleno',
                                                      'efectivo_conductor', 'Terpel', 120500)
        assert cuerpo['fotos'][0]['clase'] == 'foto_dato'


# ═════════════════════════════════════════════════════════════════════════
# 8 · El recibo guiado
# ═════════════════════════════════════════════════════════════════════════

class TestElReciboGuiado:

    SEMILLA = ('FLOTA_PLACA = "THP696"; FLOTA_FOTOS = {}; FLOTA_FOTO_TABLERO = null;'
               'FLOTA_REC = {pasos: ["tablero", "frontal", "lateral_izq", "llanta_3"],'
               ' i: 0, km: "", saltados: {}};')

    def test_un_angulo_por_pantalla_con_contador_y_guia(self, tmp_path):
        html = _correr(tmp_path, '(FLOTA_REC.i = 2, flotaRecHTML())', semilla=self.SEMILLA)
        assert '3/4' in html
        assert 'Costado izquierdo' in html
        assert 'mirando el vehículo de frente' in html
        assert 'capture="environment"' in html
        assert 'Saltar' in html

    def test_la_llanta_dice_cual_es(self, tmp_path):
        html = _correr(tmp_path, '(FLOTA_REC.i = 3, flotaRecHTML())', semilla=self.SEMILLA)
        assert 'trasera izquierda' in html

    def test_el_tablero_no_se_salta_y_pide_el_km(self, tmp_path):
        html = _correr(tmp_path, 'flotaRecHTML()', semilla=self.SEMILLA)
        assert 'id="rec-km"' in html
        assert 'Saltar' not in html

    def test_saltar_registra_faltante_y_avanza(self, tmp_path):
        html = _correr(tmp_path, '(FLOTA_REC.i = 1, flotaRecSaltar(), flotaRecSaltar(),'
                                 ' flotaRecSaltar(), flotaRecHTML())', semilla=self.SEMILLA)
        assert 'Listo para recibir' in html
        assert html.count('— falta') == 4, 'el tablero y los tres ángulos saltados'
        assert 'quedan registradas como faltantes' in html
        assert 'data-placa="THP696"' in html


# ═════════════════════════════════════════════════════════════════════════
# El contrato del doble de prueba
# ═════════════════════════════════════════════════════════════════════════

def test_el_turno_sembrado_tiene_la_forma_del_endpoint():
    """Si `mi_turno` gana o pierde una clave, estos tests estarían pintando una
    forma muerta con la suite en verde."""
    import ast
    fuente = (RAIZ / 'flota' / 'api' / 'conductor.py').read_text(encoding='utf-8')
    arbol = ast.parse(fuente)
    fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef) and n.name == 'mi_turno')
    # El `return jsonify({...}), 200` del camino feliz: el diccionario que
    # trae `tiene_turno_abierto` (los otros dos returns son errores).
    dics = [n for n in ast.walk(fn) if isinstance(n, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == 'tiene_turno_abierto'
                                                    for k in n.keys)]
    assert len(dics) == 1
    claves = {k.value for k in dics[0].keys if isinstance(k, ast.Constant)}
    assert claves == set(_turno()), (claves ^ set(_turno()))
    fn2 = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)
               and n.name == '_estado_del_vehiculo')
    ret2 = [n for n in ast.walk(fn2) if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)][-1]
    claves2 = {k.value for k in ret2.value.keys if isinstance(k, ast.Constant)}
    assert claves2 == set(_turno()['estado_vehiculo'])
