"""La cola del conductor (rutas.js): ninguna confirmación se pierde, ningún
rechazo queda trabado en silencio, y el cierre de ruta no se adelanta.

**El caso** (auditoría «operación diaria por rol», 2026-09-25):

- Con señal débil `navigator.onLine` dice «conectado» y el envío muere en el
  camino. `condGuardarParada` decía «Error de conexión» y la entrega —fotos,
  cobro, lo que volvió— se perdía: el conductor tenía que rehacerla.
- `condSyncQueue` dejaba en la cola, para siempre, todo lo que el servidor
  rechazaba: «siguen en cola», reintentándose cada vez, sin que nadie supiera
  qué parada era ni por qué.
- `condCerrarRuta` cerraba con confirmaciones todavía en el teléfono: la ruta
  pasaba a ENTREGADA y cada confirmación rezagada llegaba después a «la ruta
  debe estar EN_TRANSITO». La entrega se perdía por el orden.

**La clase**: *una operación del conductor cuyo destino depende de cómo falló
el envío, sin que el conductor lo vea*. El contrato es el de la cola de flota
(`flotaColaEnviarUna`): hecho · rechazado (sale y queda anotado) · sin red /
reintentar (se queda, con sus intentos y su error).

Se ejecuta rutas.js de verdad en Node, con `util.js` real y una IndexedDB en
memoria que implementa lo que `_condDB` usa.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

_ARNES = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const G = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

// ── IndexedDB en memoria: lo justo que usa _condDB ─────────────────────
function idb() {
  const stores = { cache: new Map(), queue: new Map() };
  let auto = 1;
  const pedir = (fn) => { const r = {}; setTimeout(() => {
    try { r.result = fn(); r.onsuccess && r.onsuccess({ target: r }); }
    catch (e) { r.error = e; r.onerror && r.onerror(e); } }, 0); return r; };
  const db = {
    objectStoreNames: { contains: () => true },
    transaction(nombre) {
      const st = stores[nombre];
      const tx = { objectStore: () => ({
        get: (k) => pedir(() => st.get(k) && structuredClone(st.get(k))),
        put: (v) => pedir(() => { const k = nombre === 'cache' ? v.k : v.id; st.set(k, structuredClone(v)); return k; }),
        add: (v) => pedir(() => { const id = auto++; st.set(id, structuredClone({ ...v, id })); return id; }),
        getAll: () => pedir(() => [...st.values()].map(x => structuredClone(x))),
        delete: (k) => pedir(() => { st.delete(k); }),
      }) };
      setTimeout(() => tx.oncomplete && tx.oncomplete(), 3);
      return tx;
    },
  };
  return { open: () => { const r = {}; setTimeout(() => { r.result = db; r.onsuccess({ target: r }); }, 0); return r; },
           _stores: stores };
}
const base = idb();

const nodos = new Map();
function nodo(id) {
  return { id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
           classList: { add() {}, remove() {} }, addEventListener() {}, appendChild() {},
           querySelector: () => null, focus() {}, remove() {} };
}
const traza = { alertas: [], envios: [], modales: [] };
let respuestas = G.respuestas || {};
const ctx = {
  console, structuredClone, AbortController,
  document: { getElementById: (id) => { if (!nodos.has(id)) nodos.set(id, nodo(id)); return nodos.get(id); },
              querySelector: () => null, querySelectorAll: () => [], addEventListener() {},
              createElement: () => nodo(null), body: { appendChild() {} } },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: G.enLinea !== false },
  indexedDB: base,
  localStorage: { getItem: () => null, setItem() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  TOKEN: 'x', API: '',
  fetch: async (url, op) => {
    traza.envios.push({ url, cuerpo: JSON.parse(op.body) });
    let r = respuestas[url];
    if (Array.isArray(r)) r = r.length > 1 ? r.shift() : r[0];
    if (!r || r === 'sin_red') throw new TypeError('Failed to fetch');
    return { ok: r.status < 300, status: r.status, json: async () => r.body || {} };
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
for (const a of ['util.js', 'rutas.js']) vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf-8'), ctx, { filename: a });
Object.assign(ctx, { __traza: traza, __G: G });
vm.runInContext(`
  alerta = (m, t) => __traza.alertas.push([String(m), t]);
  _modalConfirmar = async (m, o) => { __traza.modales.push(String(m)); return __G.confirmaModal !== false; };
  condAbrirParadas = async () => {};
  cargarRutasConductor = async () => {};
  condVolverAParadas = async () => {};
  _COND_RUTA_ACTIVA = { id: 7 };
  _COND_PARADAS = [{ tarea_id: 11, cliente: 'Papelería Luna', bultos: [] },
                   { tarea_id: 12, cliente: 'Colegio Sol', bultos: [] }];
`, ctx);

const payload = (t) => ({ version_formulario: 4, estado_entrega: 'ENTREGADO', forma_pago: 'EFECTIVO',
                          monto_cobrado: 1000, foto_entrega: 'data:x', ts_dispositivo: '2026-09-25T15:00:00Z' });
for (const paso of G.pasos) {
  if (paso.enLinea !== undefined) ctx.navigator.onLine = paso.enLinea;
  if (paso.respuestas) respuestas = paso.respuestas;
  if (paso.hacer === 'confirmar') {
    const p = vm.runInContext(`_COND_PARADAS.find(x => x.tarea_id === ${paso.tarea})`, ctx);
    await ctx._condMandarConfirmacion(7, p, payload(paso.tarea), null, null);
  } else if (paso.hacer === 'sync') {
    await ctx.condSyncQueue();
  } else if (paso.hacer === 'cerrar') {
    await ctx.condCerrarRuta();
  } else if (paso.hacer === 'descartar') {
    await ctx.condDescartarRechazo(0);
  }
}
const salida = {
  ...traza,
  cola: [...base._stores.queue.values()],
  rechazos: (base._stores.cache.get('cond_rechazos') || {}).v || [],
  html_rechazos: nodos.has('cond-rechazos') ? nodos.get('cond-rechazos').innerHTML : '',
  sync_status: nodos.has('cond-sync-status') ? nodos.get('cond-sync-status').textContent : '',
  ruta_activa: vm.runInContext('_COND_RUTA_ACTIVA', ctx),
  parada11: vm.runInContext('JSON.stringify((_COND_PARADAS.find(x => x.tarea_id === 11) || {}).recaudo || null)', ctx),
};
process.stdout.write(JSON.stringify(salida));
"""

CONF11 = '/api/rutas/7/paradas/11/confirmar'
CONF12 = '/api/rutas/7/paradas/12/confirmar'
CERRAR = '/api/rutas/7/entregar'
OK = {'status': 200, 'body': {'ok': True}}


def _correr(tmp_path, pasos, respuestas=None, en_linea=True, confirma_modal=True):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'h.mjs'
    h.write_text(_ARNES, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'pasos': pasos, 'respuestas': respuestas or {},
                             'enLinea': en_linea, 'confirmaModal': confirma_modal}),
                 encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


class TestNingunaConfirmacionSePierde:

    def test_senal_debil_guarda_en_la_cola(self, tmp_path):
        """navigator.onLine dice que sí, el envío muere: antes «Error de conexión»
        y la entrega perdida. Ahora queda en la cola con su payload entero."""
        t = _correr(tmp_path, [{'hacer': 'confirmar', 'tarea': 11}],
                    respuestas={CONF11: 'sin_red'})
        assert len(t['cola']) == 1, t
        assert t['cola'][0]['payload']['foto_entrega'] == 'data:x'
        assert t['cola'][0]['cliente'] == 'Papelería Luna'
        assert any('guardada en el teléfono' in a[0] for a in t['alertas']), t['alertas']
        assert json.loads(t['parada11'])['_en_cola'] is True

    def test_servidor_caido_tambien_se_guarda(self, tmp_path):
        t = _correr(tmp_path, [{'hacer': 'confirmar', 'tarea': 11}],
                    respuestas={CONF11: {'status': 502}})
        assert len(t['cola']) == 1

    def test_rechazo_directo_no_se_encola_y_se_dice(self, tmp_path):
        t = _correr(tmp_path, [{'hacer': 'confirmar', 'tarea': 11}],
                    respuestas={CONF11: {'status': 400, 'body': {'error': 'Falta la foto'}}})
        assert t['cola'] == []
        assert ['Falta la foto', 'error'] in t['alertas']

    def test_sin_senal_se_guarda(self, tmp_path):
        t = _correr(tmp_path, [{'hacer': 'confirmar', 'tarea': 11}], en_linea=False)
        assert len(t['cola']) == 1 and t['envios'] == []


class TestLaColaNoTrabaNadaEnSilencio:

    def test_un_rechazo_sale_de_la_cola_y_queda_anotado(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'sync', 'respuestas': {CONF11: {'status': 400, 'body': {'error': 'Monto inválido'}}}},
        ], respuestas={CONF11: 'sin_red'})
        assert t['cola'] == [], 'el rechazo siguió en la cola'
        assert len(t['rechazos']) == 1 and t['rechazos'][0]['mensaje'] == 'Monto inválido'
        assert 'Papelería Luna' in t['html_rechazos'] and 'Entendido' in t['html_rechazos']
        assert any(a[1] == 'error' for a in t['alertas'])

    def test_entendido_lo_quita(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'sync', 'respuestas': {CONF11: {'status': 400, 'body': {'error': 'x'}}}},
            {'hacer': 'descartar'},
        ], respuestas={CONF11: 'sin_red'})
        assert t['rechazos'] == []

    def test_sin_red_se_queda_con_intentos_y_error_visible(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'sync'},
        ], respuestas={CONF11: 'sin_red'})
        assert len(t['cola']) == 1
        assert t['cola'][0]['intentos'] == 1 and t['cola'][0]['ultimo_error']
        assert 'sin salir' in t['sync_status']

    def test_llega_con_señal_y_sale(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'sync', 'respuestas': {CONF11: OK}},
        ], respuestas={CONF11: 'sin_red'})
        assert t['cola'] == [] and t['rechazos'] == []
        enviado = [e for e in t['envios'] if e['url'] == CONF11][-1]['cuerpo']
        assert enviado['via_cola'] is True and enviado['ts_envio']

    def test_uno_que_no_sale_no_frena_al_otro(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'confirmar', 'tarea': 12},
            {'hacer': 'sync', 'respuestas': {CONF11: 'sin_red', CONF12: OK}},
        ], respuestas={CONF11: 'sin_red', CONF12: 'sin_red'})
        assert [x['tareaId'] for x in t['cola']] == [11]


class TestElCierreNoSeAdelanta:

    def test_con_pendientes_y_senal_no_cierra(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'cerrar'},
        ], respuestas={CONF11: 'sin_red', CERRAR: OK})
        assert CERRAR not in [e['url'] for e in t['envios']], 'cerró con una confirmación en el teléfono'
        assert any('sin enviar' in a[0] and 'Papelería Luna' in a[0] for a in t['alertas']), t['alertas']
        assert t['ruta_activa'] is not None

    def test_con_pendientes_que_si_salen_cierra(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'cerrar', 'respuestas': {CONF11: OK, CERRAR: OK}},
        ], respuestas={CONF11: 'sin_red'})
        urls = [e['url'] for e in t['envios']]
        assert urls.index(CERRAR) > max(i for i, u in enumerate(urls) if u == CONF11)
        assert t['ruta_activa'] is None

    def test_con_un_rechazo_sin_leer_no_cierra(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'sync', 'respuestas': {CONF11: {'status': 400, 'body': {'error': 'x'}}}},
            {'hacer': 'cerrar', 'respuestas': {CERRAR: OK}},
        ], respuestas={CONF11: 'sin_red'})
        assert CERRAR not in [e['url'] for e in t['envios']]

    def test_sin_senal_el_cierre_espera_a_sus_confirmaciones(self, tmp_path):
        """Encolado detrás; en la sincronización, mientras la confirmación no
        sale, el cierre tampoco."""
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'cerrar'},
            {'hacer': 'sync', 'enLinea': True, 'respuestas': {CONF11: 'sin_red', CERRAR: OK}},
        ], en_linea=False)
        assert CERRAR not in [e['url'] for e in t['envios']]
        assert sorted(x['tipo'] for x in t['cola']) == ['cerrar', 'confirmar']
        assert any('confirmación(es) guardadas' in m for m in t['modales'])

    def test_si_la_confirmacion_se_rechaza_el_cierre_encolado_tampoco_sale(self, tmp_path):
        t = _correr(tmp_path, [
            {'hacer': 'confirmar', 'tarea': 11},
            {'hacer': 'cerrar'},
            {'hacer': 'sync', 'enLinea': True,
             'respuestas': {CONF11: {'status': 400, 'body': {'error': 'x'}}, CERRAR: OK}},
        ], en_linea=False)
        assert CERRAR not in [e['url'] for e in t['envios']]
        assert t['cola'] == []
        assert {r['tipo'] for r in t['rechazos']} == {'confirmar', 'cerrar'}

    def test_cierre_sin_pendientes_pregunta_con_el_modal_propio(self, tmp_path):
        t = _correr(tmp_path, [{'hacer': 'cerrar'}], respuestas={CERRAR: OK})
        assert t['modales'] and CERRAR in [e['url'] for e in t['envios']]

    def test_cancelar_el_modal_no_cierra(self, tmp_path):
        t = _correr(tmp_path, [{'hacer': 'cerrar'}], respuestas={CERRAR: OK}, confirma_modal=False)
        assert CERRAR not in [e['url'] for e in t['envios']]


class TestElServidorCierraUnaVez:
    """Un cierre que llegó y cuya respuesta se perdió vuelve desde la cola: el
    segundo `/entregar` no puede ser un «rechazo» sobre una ruta cerrada."""

    def test_entregar_dos_veces_es_idempotente(self, client, db, usuario_admin, jwt_token_admin):
        from app.models.conductor import Conductor
        from app.models.ruta_despacho import RutaDespacho
        c = Conductor(nombre='Victor', cedula='V-cola-1', activo=True, disponible=True)
        db.session.add(c)
        db.session.flush()
        r = RutaDespacho(conductor_id=c.id, tipo_ruta='URBANA', estado='ENTREGADA')
        db.session.add(r)
        db.session.commit()
        res = client.post(f'/api/rutas/{r.id}/entregar', json={'bultos': []},
                          headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert res.status_code == 200, res.get_json()
        assert res.get_json()['ya_cerrada'] is True
