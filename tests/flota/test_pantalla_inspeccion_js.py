"""La pantalla de inspección del conductor, EJECUTADA — no leída.

## Por qué se ejecuta

Un test que busque `'no_apto'` dentro del texto de `flota.js` pasa con la
función entera desconectada, o con el formulario devolviendo `''` siempre. Ya
pasó en este repo: un aserto de substring sobre `app.js` sobrevivió a anular el
pintado con `|| true`, y se descubrió mutándolo.

Acá el `flota.js` real corre en un `vm` de Node con un DOM mínimo, se le siembra
la lista que devuelve el servidor, y se mira **lo que quedó pintado**.

## Lo que se afirma, y por qué es lo que hay que afirmar

**Ningún control nace marcado** (regla 1). No es una preferencia de UI: un
`checked` en «Bien» convierte la pantalla en una fábrica de evidencia falsa de
seguridad — el conductor baja, aprieta Terminar, y el registro afirma que
veintiocho ítems se miraron. Esa evidencia se usa después frente a una
aseguradora.

Y **lo que no se toca no se manda**: el servidor lo escribe como `sin_dato` y la
inspección sale `incompleta`. Que la pantalla mande `optimo` por omisión sería
la misma regla rota un nivel más adentro, donde ningún CHECK la ve.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'

# Arnés propio, hermano del de `test_render_salud_js.py`. Se duplica el andamio
# a propósito: aquel pinta un bloque sin estado y este necesita **sembrar
# globales antes de llamar**, y un arnés compartido que sirva a los dos es uno
# que nadie puede cambiar sin romper el otro.
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const FLOTAJS = process.argv[2];
const GUION = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

function elemento(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', style: {}, disabled: false,
    dataset: {},
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
// Lo último que se mandó por la red. Es lo que permite mirar el PAYLOAD REAL
// que arma la pantalla, en vez de reimplementar el armado en el test — que es
// el error que deja una mutación viva: el test verifica su propia copia.
let ULTIMO = null;
const ctx = {
  console, document: doc,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  window: { location: { origin: 'http://test' }, addEventListener() {},
            matchMedia: () => ({ matches: false, addEventListener() {} }) },
  navigator: { onLine: true },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  AbortController: globalThis.AbortController,
  get: async () => { throw new Error('sin red en este arnés'); },
  horaColombia: (x) => String(x),
  alerta: () => {},
  API: '', TOKEN: 'token-de-prueba',
  // Devuelve `true`: el arnés quiere ver qué se manda CON huecos, que es el
  // caso que la regla 1 gobierna.
  confirm: () => true,
  fetch: async (url, opts) => {
    ULTIMO = { url, body: opts && opts.body };
    return { ok: true, json: async () => ({
      veredicto: 'incompleta', hallazgos: [], habilita_despacho: false }) };
  },
  ultimoEnvio: () => ULTIMO,
};
ctx.globalThis = ctx;
ctx.window.document = doc;
vm.createContext(ctx);
// `util.js` va PRIMERO y se carga de verdad, no se stubbea: `esc()` es lo
// que este arnés tiene que poder ver fallar.
vm.runInContext(fs.readFileSync(FLOTAJS.replace(/[^/]+$/, 'util.js'), 'utf-8'),
                ctx, { filename: 'util.js' });
vm.runInContext(fs.readFileSync(FLOTAJS, 'utf-8'), ctx, { filename: 'flota.js' });

(async () => {
  // La semilla asigna a las globales que `flota.js` declara con `let`: viven en
  // el ámbito léxico del contexto, así que un script posterior las ve.
  vm.runInContext(GUION.semilla, ctx, { filename: 'semilla.js' });
  const salida = await vm.runInContext(`(async () => (${GUION.expr}))()`, ctx,
                                       { filename: 'expr.js' });
  process.stdout.write(JSON.stringify({ salida: salida === undefined ? null : salida }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _catalogo_falso(n_bloqueantes=2, n_resto=2):
    """Una lista del día con la forma EXACTA que devuelve el endpoint.

    Se escribe a mano y no se importa del servidor a propósito: si el contrato
    cambia, este arnés sigue pintando la forma vieja y el test verde mentiría.
    Lo que impide esa deriva es `TestElContratoEsElDelEndpoint`, que compara los
    campos contra los que `_json_item` publica de verdad.
    """
    items = []
    for i in range(n_bloqueantes):
        items.append({'item_id': 100 + i, 'nombre': f'Bloqueante {i}',
                      'gesto': f'gesto bloqueante {i}', 'criticidad': 'bloqueante',
                      'periodicidad': 'diaria', 'orden_mostrado': len(items) + 1,
                      'bloqueante': True, 'dias_de_plazo': 0})
    for i in range(n_resto):
        items.append({'item_id': 200 + i, 'nombre': f'Menor {i}',
                      'gesto': f'gesto menor {i}', 'criticidad': 'menor',
                      'periodicidad': 'diaria', 'orden_mostrado': len(items) + 1,
                      'bloqueante': False, 'dias_de_plazo': 30})
    return {'placa': 'INE300', 'dia': '2026-09-02', 'plantilla': 'camion_v1',
            'items': items, 'bloqueantes': n_bloqueantes,
            'ya_respondidas_hoy': []}


def _correr(tmp_path, expr, semilla_extra='', lista=None):
    """Corre `expr` contra el `flota.js` real con la lista ya sembrada."""
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    lista = _catalogo_falso() if lista is None else lista
    semilla = (
        f'FLOTA_PLACA = "INE300";\n'
        f'FLOTA_INSP = {json.dumps(lista)};\n'
        f'FLOTA_INSP_RESP = {{}};\n'
        f'FLOTA_INSP_NOTA = {{}};\n'
        f'FLOTA_INSP_INICIO = Date.now();\n'
        f'{semilla_extra}\n'
    )
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'semilla': semilla, 'expr': expr}), encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(FLOTA_JS), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'la pantalla reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)['salida']


def _html(tmp_path, semilla_extra='', lista=None):
    return _correr(tmp_path, 'flotaCondInspeccionHTML()', semilla_extra, lista)


class TestNingunControlNaceMarcado:
    """**Regla 1, en el único lugar donde el conductor la puede romper gratis.**

    Un default optimista acá no lo atrapa ningún CHECK: la fila que llega al
    servidor dice `optimo` y es una respuesta válida. El único sitio donde se
    puede impedir es en la pantalla que no la pone.
    """

    def test_la_lista_recien_pintada_no_tiene_ningun_boton_marcado(self, tmp_path):
        html = _html(tmp_path)
        assert html.count('btn-flota ok') == 0, (
            'un control nació marcado: el conductor puede apretar Terminar sin '
            'mirar nada y el registro afirmaría que miró')

    def test_no_hay_checked_ni_selected_en_el_formulario(self, tmp_path):
        """Las otras dos formas de poner un default: un radio con `checked` y
        un `<option selected>`. Se miran sobre el HTML pintado y no sobre el
        fuente, así que también atrapan el que se construya por concatenación."""
        html = _html(tmp_path)
        assert 'checked' not in html
        assert 'selected' not in html

    def test_arranca_diciendo_que_faltan_TODOS(self, tmp_path):
        html = _html(tmp_path)
        assert 'Faltan 4 de 4' in html

    def test_al_marcar_uno_queda_marcado_SOLO_ese(self, tmp_path):
        """La otra dirección: si nada se pudiera marcar, el test de arriba
        pasaría con la pantalla inservible."""
        html = _html(tmp_path, semilla_extra='FLOTA_INSP_RESP = {100: "optimo"};')
        assert html.count('btn-flota ok') == 1

    def test_marcar_es_lo_que_mueve_el_contador(self, tmp_path):
        """Se ejecuta el gesto real —`flotaCondMarcarItem`— y se lee el
        contador que quedó en el DOM, no el que devuelve una función pura."""
        texto = _correr(
            tmp_path,
            'flotaCondMarcarItem(100, "optimo"), '
            'document.getElementById("insp-faltan").textContent',
            semilla_extra='flotaCondInspeccionPintar();')
        assert texto == 'Faltan 3 de 4'


class TestLoQueSeVeAlAbrir:

    def test_el_gesto_va_en_pantalla(self, tmp_path):
        """Sin el gesto la criticidad es decorativa: «revisar frenos» se
        contesta de memoria a la tercera semana."""
        html = _html(tmp_path)
        assert 'gesto bloqueante 0' in html
        assert 'gesto menor 1' in html

    def test_respeta_el_orden_que_mando_el_servidor(self, tmp_path):
        """La pantalla no reordena ni agrupa. El barajado del servidor existe
        para que los ítems se lean; reordenarlos acá lo anularía sin que ningún
        test del backend se enterara."""
        html = _html(tmp_path)
        posiciones = [html.index(f'Bloqueante {i}') for i in range(2)]
        posiciones += [html.index(f'Menor {i}') for i in range(2)]
        assert posiciones == sorted(posiciones)

    def test_dice_cuantos_deciden_si_el_camion_sale(self, tmp_path):
        html = _html(tmp_path)
        assert '4 ítems · 2 bloqueantes' in html

    def test_pide_el_kilometraje(self, tmp_path):
        """Regla 3: sin odómetro no se persiste ningún evento de flota."""
        html = _html(tmp_path)
        assert 'id="insp-km"' in html

    def test_el_boton_de_enviar_sella_su_placa(self, tmp_path):
        """El defecto que cruzó las fotos de dos camiones. Un veredicto `apto`
        en el expediente equivocado es evidencia falsa sobre un vehículo que
        nadie miró."""
        html = _html(tmp_path)
        assert 'data-placa="INE300"' in html

    def test_lo_ya_respondido_hoy_se_muestra(self, tmp_path):
        """No bloquea la segunda inspección —dos turnos en un día son reales—
        pero sin esto la pantalla invita a repetir hasta que dé apto."""
        lista = _catalogo_falso()
        lista['ya_respondidas_hoy'] = [{
            'veredicto': 'no_apto', 'items_esperados': 28, 'items_sin_dato': 0,
            'segundos_llenado': 140}]
        html = _html(tmp_path, lista=lista)
        assert 'no_apto' in html
        assert 'no se borran' in html

    def test_sin_inspecciones_previas_no_ocupa_espacio(self, tmp_path):
        """La otra dirección: el bloque solo aparece cuando hay algo que decir."""
        assert 'ya se inspeccionó' not in _html(tmp_path)


class TestMarcarUnaFallaNoCuestaNada:
    """Regla 11 aplicada al costo de ser honesto: marcar la falla tiene que ser
    lo más fácil que se pueda hacer. Si cuesta un campo obligatorio, lo que se
    paga con ese precio es un `optimo`."""

    def test_la_nota_aparece_solo_al_marcar_mal_y_es_OPCIONAL(self, tmp_path):
        html = _html(tmp_path, semilla_extra='FLOTA_INSP_RESP = {100: "no_apto"};')
        assert 'id="insp-nota-100"' in html
        assert 'opcional' in html

    def test_marcar_bien_no_pide_nada(self, tmp_path):
        html = _html(tmp_path, semilla_extra='FLOTA_INSP_RESP = {100: "optimo"};')
        assert 'insp-nota-100' not in html

    def test_dice_el_plazo_que_va_a_correr_y_quien_lo_cierra(self, tmp_path):
        """El plazo sale del servidor (regla 6). Y decir que lo cierra otro es
        lo que hace visible la asimetría: el conductor reporta, no cierra."""
        html = _html(tmp_path, semilla_extra='FLOTA_INSP_RESP = {200: "no_apto"};')
        assert '30 día(s) de plazo' in html
        assert 'no vos' in html

    def test_cambiar_de_mal_a_bien_borra_la_nota(self, tmp_path):
        """Una nota de un ítem que terminó en `optimo` no se puede mandar: el
        CHECK de la base rechaza el hallazgo, pero la nota viajaría igual y
        quedaría describiendo un daño que el registro dice que no existe."""
        resto = _correr(
            tmp_path,
            'flotaCondMarcarItem(100, "optimo"), JSON.stringify(FLOTA_INSP_NOTA)',
            semilla_extra=('FLOTA_INSP_RESP = {100: "no_apto"};\n'
                           'FLOTA_INSP_NOTA = {100: "la trasera derecha"};\n'
                           'flotaCondInspeccionPintar();'))
        assert json.loads(resto) == {}


class TestLoQueNoSeTocaViajaComoAUSENCIA:
    """El punto donde la regla 1 se rompería sin que ningún CHECK la vea.

    Si la pantalla mandara `optimo` por omisión, el servidor recibiría una
    respuesta válida y escribiría `apto`. No hay constraint que distinga eso de
    una inspección de verdad: **el único lugar donde se puede impedir es acá**.
    """

    def _envio(self, tmp_path, marcado):
        """Aprieta «Terminar inspección» de verdad y devuelve lo que salió.

        **Se ejecuta `flotaCondGuardarInspeccion`, no una copia del armado.**
        Reimplementar el filtro en el test lo dejaría verde con la pantalla
        mandando `optimo` por omisión — la mutación sobreviviría y el arnés
        diría que está protegido.
        """
        crudo = _correr(
            tmp_path,
            '(await flotaCondGuardarInspeccion(), JSON.stringify(ultimoEnvio()))',
            semilla_extra=(
                f'FLOTA_INSP_RESP = {json.dumps(marcado)};\n'
                'flotaCondInspeccionPintar();\n'
                'document.getElementById("insp-km").value = "12000";\n'
                'document.getElementById("insp-obs").value = "";\n'
                'document.getElementById("insp-guardar").dataset.placa = "INE300";\n'))
        envio = json.loads(crudo)
        assert envio is not None, 'la pantalla no llegó a mandar nada'
        return envio['url'], json.loads(envio['body'])

    def test_los_no_contestados_NO_van_en_el_payload(self, tmp_path):
        url, cuerpo = self._envio(tmp_path, {'100': 'optimo'})
        assert url == '/flota/inspeccion'
        assert [r['item_id'] for r in cuerpo['respuestas']] == [100]
        assert cuerpo['placa'] == 'INE300' and cuerpo['km'] == 12000

    def test_los_contestados_SI_van_con_lo_que_se_marco(self, tmp_path):
        """La otra dirección: un filtro escrito al revés dejaría el payload
        vacío siempre y el test de arriba pasaría igual."""
        _url, cuerpo = self._envio(tmp_path, {'100': 'optimo', '200': 'no_apto'})
        enviados = cuerpo['respuestas']
        assert {r['item_id'] for r in enviados} == {100, 200}
        assert {r['respuesta'] for r in enviados} == {'optimo', 'no_apto'}

    def test_el_reloj_viaja_en_el_payload(self, tmp_path):
        """Regla 11: `segundos_llenado` se manda medido. Si la pantalla dejara
        de mandarlo, el servidor devolvería 400 y nadie sabría por qué."""
        _url, cuerpo = self._envio(tmp_path, {'100': 'optimo'})
        assert 'segundos_llenado' in cuerpo
        assert isinstance(cuerpo['segundos_llenado'], int)
        assert cuerpo['segundos_llenado'] >= 0

    def test_el_aviso_de_huecos_dice_QUE_significa_incompleta(self):
        """Un «¿confirmás?» pelado enseña a apretar Aceptar. El aviso tiene que
        decir que `incompleta` no es «casi apto» sino «no se sabe»."""
        js = FLOTA_JS.read_text(encoding='utf-8')
        i = js.index('async function flotaCondGuardarInspeccion')
        cuerpo = js[i:i + 3000]
        assert 'INCOMPLETA' in cuerpo
        assert 'no habilita despacho' in cuerpo
        assert 'bloqueantes' in cuerpo, (
            'el aviso no distingue un hueco cualquiera de un bloqueante en '
            'blanco, que es el que decide si el camión sale')


class TestLasURLsVanEnTERAS:
    """El trinquete de rutas huérfanas mide adyacencia sobre el texto del PWA.

    Con la URL armada por concatenación —`'/flota/inspeccion/' + verbo`— el
    endpoint se declara sin consumidor, y esa no es una falla del guard: una URL
    que solo existe en tiempo de ejecución no se puede auditar leyendo el repo.
    Ya destapó el caso de los tres botones del hallazgo en la tanda anterior.
    """

    def test_las_dos_urls_estan_escritas_completas(self):
        js = FLOTA_JS.read_text(encoding='utf-8')
        assert "'/flota/inspeccion/items/'" in js
        assert "'/flota/inspeccion'" in js

    def test_el_reloj_de_la_regla_11_se_manda_medido(self):
        """`segundos_llenado` no se estima ni se redondea a un valor cómodo: es
        el único dato que distingue mirar de marcar."""
        js = FLOTA_JS.read_text(encoding='utf-8')
        i = js.index('async function flotaCondGuardarInspeccion')
        cuerpo = js[i:i + 3000]
        assert 'segundos_llenado: Math.max(0, Math.round((Date.now() - FLOTA_INSP_INICIO)' in cuerpo


class TestElContratoEsElDelEndpoint:
    """El arnés siembra una lista escrita a mano. Si el endpoint cambiara sus
    campos, la pantalla se rompería en producción con todos los tests en verde
    — que es exactamente la forma de fallo que un doble de prueba introduce."""

    def test_los_campos_sembrados_son_los_que_publica_el_servidor(self, db):
        from flota.adaptadores import catalogo
        from flota.api.inspecciones import _json_item

        catalogo.sembrar(db)
        from flota.adaptadores.modelos import ItemInspeccion

        real = _json_item(ItemInspeccion.query.first(), 1)
        falso = _catalogo_falso()['items'][0]
        assert set(real) == set(falso), (
            'el doble de prueba y el endpoint dejaron de tener la misma forma: '
            'la pantalla se rompería en producción con la suite en verde')
