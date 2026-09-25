"""Ningún código del modelo se pinta crudo (QA e2e 2026-09-24, P3).

**La clase:** *un código de vocabulario (`no_apto`, `ENTREGADO_SIN_PAGO`,
`efectivo_conductor`, `salio_sin_turno`, `` `entregar_ruta` ``) que llega a la
pantalla como se guarda*. El recorrido por rol los encontró en el muelle, la
jornada, la bandeja, la analítica de flota, la liquidación, el desglose y las
pestañas del expediente. Un jefe de bodega no lee `sin_dato`: lee «¿esto es un
error?».

El detector es de **forma**, no de lista: en el texto visible (sin etiquetas
ni atributos, entidades decodificadas) no puede haber ningún
`palabra_con_guiones_bajos`, ningún `MAYUSCULAS_CON_GUIONES` ni ningún
`` `identificador` ``. Así el código que alguien agregue mañana también se ve.

Cada pantalla se pinta en Node con `util.js` real y respuestas **reales** del
servidor, sobre un mundo que tiene los códigos que se escaparon (un vehículo
con papeles, gastos, taller, preventivo y ficha con «sin dato»; una parada
«no pagó y se quedó»; una inspección no apta; una ruta que salió sin turno).
Meta-tests: el detector ve cada forma y no marca lo sano; piso de pantallas.
"""
import html as _html
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.flota.test_ficha_primera_vez_js import ARCHIVOS_FICHA, EXPEDIENTE, correr
from tests.test_qa_e2e_flota_20260924 import (_DATA_URL, _auth, _ruta_con_parada,  # noqa: F401
                                              mundo, PWA)

# ═════════════════════════════════════════════════════════════════════════
# El detector
# ═════════════════════════════════════════════════════════════════════════

_SNAKE = re.compile(r'\b[a-z]+(?:_[a-z0-9]+)+\b')
_SCREAM = re.compile(r'\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b')
_TICK = re.compile(r'`[^`\s]+`')


def visible(html):
    """Lo que se LEE: sin etiquetas (con sus atributos), entidades decodificadas."""
    sin = re.sub(r'<[^>]*>', ' ', html or '')
    return re.sub(r'\s+', ' ', _html.unescape(sin))


def codigos_crudos(html):
    v = visible(html)
    return sorted(set(_SNAKE.findall(v) + _SCREAM.findall(v) + _TICK.findall(v)))


class TestElDetectorMuerde:
    def test_ve_las_tres_formas(self):
        assert codigos_crudos('<b>efectivo_conductor</b>') == ['efectivo_conductor']
        assert codigos_crudos('estado ENTREGADO_SIN_PAGO') == ['ENTREGADO_SIN_PAGO']
        assert '`cerro`' in codigos_crudos('Quién cerró: `cerro` no guarda')
        assert 'entregar_ruta' in codigos_crudos('Quién cerró: `entregar_ruta` no guarda')
        assert codigos_crudos('salió «no_apto»') == ['no_apto']

    def test_no_marca_lo_sano(self):
        assert not codigos_crudos('<option value="sin_dato">Sin dato</option>')
        assert not codigos_crudos('<button onclick="x(\'no_apto\')">no apta</button>')
        assert not codigos_crudos('SOAT · Revisión técnico-mecánica · EFECTIVO · 12:30 · $1.000')

    def test_decodifica_entidades(self):
        assert codigos_crudos('&lt;b&gt;no_apto') == ['no_apto']


def _node(tmp_path, archivos, rutas, js, globales=None):
    """Corre `js` (cuerpo async que devuelve un objeto de cadenas) con `get`
    sembrado por trozo de URL. Revienta el test si el JS revienta."""
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    arnes = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const G = JSON.parse(fs.readFileSync(process.argv[2], 'utf-8'));
function el(id) {
  return { id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {}, className: '',
    disabled: false, checked: false, open: false,
    classList: { add() {}, remove() {}, toggle() { return false; }, contains() { return false; } },
    querySelector() { return null; }, querySelectorAll() { return []; },
    addEventListener() {}, appendChild() {}, setAttribute() {}, remove() {}, focus() {},
    insertBefore() {}, scrollIntoView() {}, insertAdjacentHTML(p, h) { this.innerHTML = h + this.innerHTML; },
    get parentNode() { return el('padre-' + id); } };
}
const els = {};
const doc = { body: el('body'), head: el('head'), documentElement: el('html'),
  getElementById(id) { if (!(id in els)) els[id] = el(id); return els[id]; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, createElement(t) { return el(t); } };
const ctx = { console, document: doc,
  window: { location: { origin: 'http://t' }, addEventListener() {},
            matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true }, localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  AbortController: globalThis.AbortController, API: '', TOKEN: 't',
  OPERARIO: { rol: 'admin' }, alerta() {}, confirm: () => true, prompt: () => 'x',
  horaColombia: (x) => String(x),
  get: async (url) => {
    for (const [trozo, p] of Object.entries(G.rutas)) if (String(url).includes(trozo)) return p;
    throw new Error('ruta no sembrada: ' + url);
  },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }) };
ctx.globalThis = ctx;
ctx.window.document = doc;
vm.createContext(ctx);
for (const a of G.archivos) vm.runInContext(fs.readFileSync(a, 'utf-8'), ctx, { filename: a });
for (const [k, v] of Object.entries(G.globales || {})) vm.runInContext(`${k} = ${JSON.stringify(v)};`, ctx);
ctx.__els = els;
(async () => {
  const f = vm.runInContext(`(async () => { ${G.js} })`, ctx);
  const out = await f();
  process.stdout.write(JSON.stringify(out));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""
    h = tmp_path / 'arnes_codigos.mjs'
    h.write_text(arnes, encoding='utf-8')
    g = tmp_path / 'guion_codigos.json'
    g.write_text(json.dumps({'archivos': [str(PWA / a) for a in archivos], 'rutas': rutas,
                             'js': js, 'globales': globales or {}}, default=str),
                 encoding='utf-8')
    p = subprocess.run(['node', str(h), str(g)], capture_output=True, text=True, timeout=90)
    assert p.returncode == 0, f'el JS reventó:\n{p.stderr[-1500:]}'
    return json.loads(p.stdout)


# ═════════════════════════════════════════════════════════════════════════
# El muelle: advertencias de flota (texto del servidor)
# ═════════════════════════════════════════════════════════════════════════

class TestElMuelle:
    def test_la_advertencia_dice_inspeccion_no_apta_en_femenino(self, db, mundo, monkeypatch):  # noqa: F811
        from flota.adaptadores import inspecciones
        from app.services import senales_ruta
        ruta, _t = _ruta_con_parada(db, mundo)
        monkeypatch.setattr(inspecciones, 'del_dia',
                            lambda vid, dia: [SimpleNamespace(veredicto='no_apto')])
        adv = senales_ruta.advertencias_de_flota(ruta)
        textos = ' · '.join(a['texto'] for a in adv)
        # Integración 2026-09-24: desde las costuras el texto lo escribe la
        # política única de salida (`flota/dominio/salida.py`: «La inspección
        # de hoy salió no apta»), no `senales_ruta`. La propiedad es la misma:
        # en palabras y en femenino, nunca «no apto» ni el código.
        assert 'no apta' in textos, textos
        assert 'no apto' not in textos and 'no_apto' not in textos, textos
        assert not codigos_crudos(textos), textos


# ═════════════════════════════════════════════════════════════════════════
# El expediente del vehículo, con todo lo que tenía códigos
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture
def lleno(client, db, almacen):
    from flask_jwt_extended import create_access_token
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import FichaTecnica
    u = Usuario(email='lleno@codigos.test', nombre='Admin', rol='admin', activo=True,
                almacen_id=almacen.id)
    u.set_password('x')
    v = Vehiculo(placa='LLE001', tipo='NHR', activo=True)
    db.session.add_all([u, v])
    db.session.flush()
    db.session.add(FichaTecnica(
        vehiculo_id=v.id, posiciones_llanta=6, km_inicial=50000,
        km_inicial_ts=datetime(2026, 1, 1), distribucion='correa',
        distribucion_km_cambio=60000, distribucion_fuente='manual_fabricante',
        aceite_motor_spec='15W40'))
    db.session.commit()
    h = _auth(create_access_token(identity=str(u.id)))
    for d in ({'tipo': 'soat', 'estado': 'vigente', 'numero': 'S1', 'entidad': 'X',
               'fecha_expedicion': '2026-01-01', 'fecha_vencimiento': '2027-01-01'},
              {'tipo': 'poliza_rc', 'estado': 'no_encontrado'},
              {'tipo': 'tarjeta_propiedad', 'estado': 'vigente', 'numero': 'T1',
               'entidad': 'X', 'fecha_expedicion': '2020-01-01'}):
        assert client.post('/flota/vehiculo/LLE001/documentos', headers=h,
                           json=d).status_code == 201
    from app.utils.fecha import dia_operativo
    r = client.post('/flota/tanqueos', headers=h, json=dict(
        placa='LLE001', fecha=dia_operativo().isoformat(), valor='120000', galones='10',
        tanque='lleno', estacion='Terpel', km=50100, proveedor='Terpel',
        origen_costo='efectivo_conductor'))
    assert r.status_code == 201, r.get_json()
    assert client.post('/flota/preventivo/LLE001/sembrar', headers=h).status_code == 200
    rutas = {}
    for url in ('/api/almacenes/', '/api/rutas/conductores?activos=true',
                '/flota/custodia/activa/LLE001', '/flota/vehiculo/LLE001/ficha',
                '/flota/vehiculo/LLE001/documentos', '/flota/hallazgos/LLE001',
                '/flota/vocabulario', '/flota/gastos/LLE001', '/flota/ordenes/LLE001',
                '/flota/llantas/LLE001', '/flota/preventivo/LLE001', '/flota/odometro/dudosas'):
        r = client.get(url, headers=h)
        assert r.status_code == 200, (url, r.get_json())
        rutas[url] = r.get_json()
    return rutas


class TestElExpediente:
    @pytest.mark.parametrize('fn', EXPEDIENTE)
    def test_ninguna_pestana_pinta_codigos(self, tmp_path, lleno, fn):
        r = correr(tmp_path, {'archivos': ARCHIVOS_FICHA, 'operario': {'rol': 'admin'},
                              'rutas': lleno, 'pasos': [{'fn': fn, 'args': ['LLE001']}],
                              'leer': ['flota-recibo']})
        html = r['html']['flota-recibo']
        assert html
        assert not codigos_crudos(html), (fn, codigos_crudos(html))

    def test_los_codigos_salen_en_palabras(self, tmp_path, lleno):
        todo = ''
        for fn in ('flotaAbrirDocumentos', 'flotaAbrirGastos', 'flotaAbrirTaller',
                   'flotaAbrirFicha'):
            r = correr(tmp_path, {'archivos': ARCHIVOS_FICHA, 'operario': {'rol': 'admin'},
                                  'rutas': lleno, 'pasos': [{'fn': fn, 'args': ['LLE001']}],
                                  'leer': ['flota-recibo']})
            todo += visible(r['html']['flota-recibo'])
        for palabra in ('Póliza de responsabilidad civil', 'Tarjeta de propiedad',
                        'Efectivo del conductor', 'Aire acondicionado', 'Sin dato',
                        'Manual del fabricante'):
            assert palabra in todo, palabra


# ═════════════════════════════════════════════════════════════════════════
# La bandeja: una señal con su evidencia
# ═════════════════════════════════════════════════════════════════════════

class TestLaBandeja:
    def test_la_senal_salio_sin_turno_no_muestra_el_codigo(self, tmp_path, client, db, mundo):  # noqa: F811
        from flota.adaptadores.modelos import Custodia
        db.session.add(Custodia(vehiculo_id=mundo.vehiculo_id, custodio_tipo='sede',
                                custodio_sede_id=mundo.almacen.id,
                                registrado_por_usuario_id=mundo.u_admin.id,
                                inicio_ts=datetime.utcnow() - timedelta(hours=5), km_inicio=1000))
        db.session.commit()
        _ruta_con_parada(db, mundo, estado='EN_TRANSITO')
        b = client.get('/flota/bandeja', headers=_auth(mundo.t_flota)).get_json()
        assert any(s['clase'] == 'turno_de_la_ruta' for s in b['senales']), b['senales']
        out = _node(tmp_path, ['util.js', 'flota.js', 'flota_analitica.js', 'flota_bandeja.js'],
                    {}, 'return { s: flotaBandejaSenalesHtml(B), p: flotaBandejaPendientesHtml(B),'
                        ' h: flotaBandejaHoyHtml(B) };', globales={'B': b})
        for k, html in out.items():
            assert not codigos_crudos(html), (k, codigos_crudos(html))


# ═════════════════════════════════════════════════════════════════════════
# Liquidación: el detalle de la ruta y el desglose
# ═════════════════════════════════════════════════════════════════════════

class TestLaLiquidacion:
    def _ruta_sin_pago(self, db, mundo):
        from app.services.ruta_service import RutaService
        ruta, t = _ruta_con_parada(db, mundo, estado='EN_TRANSITO')
        RutaService.confirmar_parada(ruta.id, t.id, mundo.u_cond.id, {
            'version_formulario': 3, 'estado_entrega': 'RECHAZADO',
            'motivo_rechazo': 'NO_PAGO_SE_QUEDO', 'forma_pago': 'CREDITO',
            'observaciones': 'paga el viernes', 'monto_cobrado': 0,
            'foto_entrega': _DATA_URL, 'geo': {'fuente': 'sin_dato', 'motivo': 'sin_senal'}})
        return ruta

    def test_el_detalle_y_el_desglose_no_pintan_codigos(self, tmp_path, client, db, mundo):  # noqa: F811
        ruta = self._ruta_sin_pago(db, mundo)
        h = _auth(mundo.t_admin)
        det = client.get(f'/api/rutas/{ruta.id}/liquidacion-detalle', headers=h)
        des = client.get('/api/rutas/liquidacion/desglose', headers=h)
        assert det.status_code == 200 and des.status_code == 200, (det.get_json(), des.get_json())
        out = _node(tmp_path, ['util.js', 'liquidacion.js'],
                    {'/liquidacion-detalle': det.get_json(),
                     '/liquidacion/desglose': des.get_json()},
                    f"await liqAbrirRuta({ruta.id}); await liqCargarDesglose();"
                    " return { detalle: __els['liq-modal-body'].innerHTML,"
                    " desglose: __els['liq-desglose'].innerHTML };")
        assert 'Se quedó sin pagar' in visible(out['detalle'])
        assert 'Crédito no autorizado' in visible(out['desglose'])
        assert 'Paradas por condición de pago' in visible(out['desglose'])
        for k, html in out.items():
            assert not codigos_crudos(html), (k, codigos_crudos(html))


# ═════════════════════════════════════════════════════════════════════════
# La jornada del conductor
# ═════════════════════════════════════════════════════════════════════════

class TestLaJornada:
    def test_el_dia_con_una_parada_sin_pago_no_pinta_codigos(self, tmp_path, client, db, mundo):  # noqa: F811
        from app.utils.fecha import dia_operativo
        TestLaLiquidacion()._ruta_sin_pago(db, mundo)
        dia = dia_operativo().isoformat()
        h = _auth(mundo.t_flota)
        res = client.get(f'/api/jornada/resumen?desde={dia}&hasta={dia}', headers=h)
        jd = client.get(f'/api/jornada?dia={dia}&conductor_id={mundo.conductor_id}', headers=h)
        assert res.status_code == 200 and jd.status_code == 200, (res.get_json(), jd.get_json())
        r = res.get_json()
        i = next(k for k, f in enumerate(r['conductores'])
                 if f['conductor']['id'] == mundo.conductor_id)
        j = next(k for k, d in enumerate(r['conductores'][i]['dias']) if d['dia'] == dia)
        out = _node(tmp_path, ['util.js', 'flota_jornada.js'],
                    {'/api/jornada/resumen': r, '/api/jornada?': jd.get_json()},
                    "const c = document.getElementById('fj'); await flotaJornadaCargar(c);"
                    " const resumen = c.innerHTML;"
                    f" fjAbrirConductor({i}); const cond = c.innerHTML;"
                    f" await fjAbrirDia({i}, {j});"
                    " return { resumen, cond, dia: c.innerHTML };")
        # Integración 2026-09-24: «Lo que esta vista no puede ver» va UNA vez,
        # al pie de la lista de conductores (costuras), no en cada día. Lo que
        # este test protege es que diga en palabras quién cerró la ruta, sin
        # el nombre de una función (`entregar_ruta`).
        assert 'Quién cerró la ruta' in visible(out['resumen'])
        assert 'entregar_ruta' not in out['resumen'] + out['dia']
        for k, html in out.items():
            assert not codigos_crudos(html), (k, codigos_crudos(html))


    def test_el_preoperacional_dice_el_veredicto_en_palabras(self, tmp_path):
        """Integración 2026-09-24: el día pintaba `d.veredicto` tal cual
        («no_apto»). El servidor manda la palabra (`palabra_de_veredicto`) y un
        servidor viejo, sin ella, cae a `fjValor`."""
        from flota.dominio.inspeccion import palabra_de_veredicto
        ev = {'tipo': 'preoperacional', 'detalle': {
            'placa': 'ABC123', 'veredicto': 'no_apto', 'segundos_llenado': 40, 'items': 12,
            'veredicto_en_palabras': palabra_de_veredicto('no_apto')}}
        viejo = {'tipo': 'preoperacional', 'detalle': {
            'placa': 'ABC123', 'veredicto': 'no_apto', 'segundos_llenado': 40, 'items': 12}}
        out = _node(tmp_path, ['util.js', 'flota_jornada.js'], {},
                    'return { n: fjDetalleEvento(E), v: fjDetalleEvento(V) };',
                    globales={'E': ev, 'V': viejo})
        for k, html in out.items():
            assert 'no apta' in html, (k, html)
            assert not codigos_crudos(html), (k, codigos_crudos(html))


def test_piso_de_pantallas_medidas():
    """Si alguien borra pantallas del inventario, el detector mide menos."""
    assert len(EXPEDIENTE) >= 9


class TestLaAnaliticaDeFlota:
    def test_la_inspeccion_mas_rapida_dice_el_veredicto_en_palabras(self, tmp_path):
        # Integración 2026-09-24: el panel `flotaAnInspeccion` de Analítica se
        # retiró en las costuras; el tiempo de llenado vive en el diagnóstico
        # plegado (`flotaDiagLlenado`), que ahora también dice el veredicto.
        s = {'mediana': 90, 'n': 12,
             'minimo': {'segundos': 20, 'items': 14, 'veredicto': 'no_apto'}}
        out = _node(tmp_path, ['util.js', 'flota.js', 'flota_analitica.js',
                               'flota_bandeja.js'], {},
                    'return { p: flotaDiagLlenado(H) };', globales={'H': s})
        assert 'no apta' in visible(out['p'])
        assert not codigos_crudos(out['p']), codigos_crudos(out['p'])
