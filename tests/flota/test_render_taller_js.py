"""
La pantalla de taller y garantía, EJECUTADA — no leída.

Un aserto de substring sobre `flota.js` pasa con la función entera
desconectada, o con el bloque devolviendo `''` siempre. Ya pasó en este repo:
un `|| true` anuló el pintado de `app.js` y sobrevivió a la suite; se descubrió
mutándolo. Acá el `flota.js` real corre en un `vm` con DOM mínimo y `fetch`
sembrado, y se mira **lo que quedó pintado**.

Se prueban dos cosas distintas:

1. **El bloque de salud** — los tres campos nuevos. Reusa el arnés de
   `test_render_salud_js.py` en vez de copiarlo por cuarta vez (regla 0).
2. **El expediente de taller** — con su propio arnés, porque lo que hay que leer
   no es el valor devuelto sino el HTML que quedó en `#flota-recibo`.

## Lo que se verifica y por qué no es cosmético

· **La propuesta de garantía se pinta con las DOS dimensiones.** Es la
  contracara de que la búsqueda use «o» en vez de «y»: un renglón que solo
  dijera «vigente» podría estar vigente por fecha y pasado de kilómetros, y
  quien llama al taller necesita ver cuál de las dos cubre.
· **`sin_dato` no se pinta como si estuviera bien.** Ni como vencida: las dos
  mentirían en direcciones opuestas y la primera es la cara — dejar de reclamar
  una garantía que sí cubría.
· **El formulario del trabajo no tiene campos de vencimiento.** No pueden
  existir: se calculan. Si aparecieran, la regla 6 aplicada a la garantía se
  rompe en la pantalla y el servidor sería lo único que la sostiene.
· **La propuesta no bloquea nada.** El botón de abrir la orden sigue ahí.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.flota.test_render_salud_js import _pintar, _sano

RAIZ = Path(__file__).resolve().parents[2]
FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'


# ══════════════════════════════════════════════════════════════════════════
# 1 — El bloque de salud
# ══════════════════════════════════════════════════════════════════════════

def _taller(**extra):
    """Una flota **sin una sola visita al taller**: el estado de hoy."""
    base = {'ot_abiertas': 0, 'trabajos_sin_factura': 0,
            'garantias_vigentes': 0}
    base.update(extra)
    return _sano(**base)


class TestElTallerNoGritaCuandoNoHayNada:

    def test_sin_ordenes_no_pinta_nada(self, tmp_path):
        """La disciplina de los otros bloques. Un tablero que siempre muestra
        algo se deja de mirar — la lección de los 639 avisos conocidos."""
        assert _pintar(tmp_path, _taller()) == ''

    def test_un_health_sin_los_campos_nuevos_no_revienta(self, tmp_path):
        """Un despliegue a medias no puede dejar al administrador sin tablero."""
        assert _pintar(tmp_path, _sano()) == ''


class TestLosTresCamposSeVenYNoSeSuman:

    def test_las_ordenes_abiertas_se_ven(self, tmp_path):
        html = _pintar(tmp_path, _taller(ot_abiertas=3))
        assert '3 orden(es) de trabajo abiertas' in html

    def test_y_se_publican_SIN_umbral(self, tmp_path):
        """Regla 13: no hay una sola medición de cuánto dura una visita, así que
        no se dice si tres es mucho. Un techo escrito hoy sería a ojo."""
        html = _pintar(tmp_path, _taller(ot_abiertas=3))
        assert 'no hay una sola medición' in html

    def test_los_trabajos_sin_factura_tienen_su_propia_linea(self, tmp_path):
        """**El precio de que la orden no lleve valor.** Si se sumaran a las
        órdenes abiertas, el número que dice «la factura no llegó» quedaría
        escondido detrás del que dice «el camión está adentro»."""
        html = _pintar(tmp_path, _taller(ot_abiertas=3, trabajos_sin_factura=5))
        assert '3 orden(es) de trabajo abiertas' in html
        assert '5 trabajo(s) de taller sin factura recibida' in html

    def test_las_garantias_vivas_se_publican_como_dato(self, tmp_path):
        """Es el campo que dice si la fase está haciendo algo: en cero durante
        meses significa que la búsqueda que evita pagar dos veces no tiene sobre
        qué pronunciarse."""
        html = _pintar(tmp_path, _taller(garantias_vigentes=4))
        assert '4 reparación(es) todavía en garantía' in html
        assert 'se paga dos veces' in html

    def test_cero_garantias_no_ocupa_espacio(self, tmp_path):
        assert _pintar(tmp_path, _taller(garantias_vigentes=0)) == ''

    def test_ninguna_palabra_del_bloque_imputa_nada_a_nadie(self, tmp_path):
        """Regla 2 en la línea que alguien lee. Un camión que entra al taller
        no es culpa de quien lo manejaba."""
        html = _pintar(tmp_path, _taller(ot_abiertas=3, trabajos_sin_factura=5,
                                         garantias_vigentes=4)).lower()
        for palabra in ('culpable', 'responsable', 'negligencia', 'descuido',
                        'conductor'):
            assert palabra not in html


# ══════════════════════════════════════════════════════════════════════════
# 2 — El expediente de taller
# ══════════════════════════════════════════════════════════════════════════

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const FLOTAJS = process.argv[2];
const GUION = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

function elemento(id) {
  return {
    id, innerHTML: '', textContent: '', value: '', style: {}, disabled: false,
    dataset: {}, checked: false,
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
  alerta: () => {},
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
  // `FLOTA_PLACA` se declara con `let`: no es propiedad del objeto global del
  // contexto, así que asignarla desde fuera crearía otra variable y la pantalla
  // seguiría viendo `null`. Se asigna corriendo un script en el mismo contexto.
  vm.runInContext('FLOTA_PLACA = ' + JSON.stringify(GUION.placa) + ';', ctx);
  // `OPERARIO` lo declara `app.js` con `let` y esta pantalla lo lee para saber
  // si quien mira DECIDE o solo REGISTRA. Sin asignarlo, `flotaDecide()` da
  // `false` y el arnés probaría para siempre la pantalla del que no decide —
  // silenciosamente, y con los tests en verde.
  vm.runInContext('OPERARIO = ' + JSON.stringify(GUION.operario) + ';', ctx);
  await ctx.flotaRenderTaller();
  // El sistema elegido se simula moviendo el `value` del selector y volviendo a
  // llamar al filtro, que es exactamente lo que hace el `onchange`.
  if (GUION.sistema) {
    doc.getElementById('ot-sistema').value = GUION.sistema;
    ctx.flotaTallerSistemaCambio();
  }
  process.stdout.write(JSON.stringify({
    html: doc.getElementById('flota-recibo').innerHTML || '',
    garantias: doc.getElementById('ot-garantias').innerHTML || '',
  }));
})().catch((e) => { console.error((e && e.stack) || e); process.exit(1); });
"""


def _correr(tmp_path, payload, sistema=None, rol='admin') -> dict:
    """Corre `flotaRenderTaller()` del `flota.js` real y devuelve lo pintado.

    `rol` por defecto es `admin` —el que decide— porque es lo que este archivo
    venía probando antes de que el eje existiera. Dejarlo en el que NO decide
    habría hecho pasar los tests viejos midiendo otra pantalla.
    """
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'placa': 'TGZ653', 'sistema': sistema,
                             'operario': {'rol': rol, 'nombre': 'quien sea'},
                             'rutas': {'/flota/ordenes/': payload}}),
                 encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(FLOTA_JS), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'la pantalla reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)


def _garantia(**extra):
    base = {
        'intervencion_id': 7, 'orden_trabajo_id': 3, 'sistema': 'embrague',
        'descripcion': 'cambio de kit de embrague',
        'garantia_declarada': 'si', 'garantia_meses': 6, 'garantia_km': 10000,
        'hasta_fecha': '2026-09-05', 'hasta_km': 110000, 'km_actual': 105000,
        'gasto_id': None, 'por_fecha': True, 'por_km': True,
        'vigente': True, 'cubre': True,
    }
    base.update(extra)
    return base


def _orden(**extra):
    base = {
        'id': 3, 'vehiculo_id': 1, 'tipo': 'correctiva', 'estado': 'abierta',
        'taller': 'Taller Los Andes', 'descripcion': 'ruido al embragar',
        'lectura_id': 11, 'km': 100000, 'hallazgo_id': None,
        'abierta_ts': '2026-03-05T19:00:00Z', 'abierta_por_usuario_id': 2,
        'cerrada_ts': None, 'motivo_cierre': None,
        'intervenciones': [], 'sin_factura': 0,
    }
    base.update(extra)
    return base


def _payload(**extra):
    base = {
        'placa': 'TGZ653', 'dia': '2026-07-01', 'km_actual': 105000,
        'ordenes': [], 'abiertas': 0, 'garantias_vigentes': [],
        'tipos': ['correctiva', 'preventiva'],
        'sistemas': ['motor', 'transmision', 'embrague', 'frenos', 'otro'],
        'garantia_declarada': ['si', 'no', 'sin_dato'],
        'categorias_de_taller': ['mantenimiento', 'repuesto', 'llanta'],
        'origenes_costo': ['tarjeta_convenio', 'credito_proveedor',
                           'efectivo_conductor', 'sin_dato'],
    }
    base.update(extra)
    return base


class TestLaPropuestaDeGarantiaEsLoQueValeLaPlata:

    def test_con_una_garantia_viva_del_sistema_elegido_se_pinta(self, tmp_path):
        r = _correr(tmp_path, _payload(garantias_vigentes=[_garantia()]),
                    sistema='embrague')
        assert 'todavía en garantía' in r['garantias']
        assert 'embrague' in r['garantias']

    def test_con_otro_sistema_elegido_NO_se_pinta(self, tmp_path):
        """La otra dirección. Sin esto, un filtro degenerado en «todas» pasaría
        el test de arriba y propondría reclamar el embrague por unos frenos —
        que es cómo un aviso deja de creerse."""
        r = _correr(tmp_path, _payload(garantias_vigentes=[_garantia()]),
                    sistema='frenos')
        assert r['garantias'] == ''

    def test_sin_garantias_vivas_no_ocupa_espacio(self, tmp_path):
        r = _correr(tmp_path, _payload(), sistema='embrague')
        assert r['garantias'] == ''

    def test_dice_explicitamente_que_NO_bloquea(self, tmp_path):
        """El plan es literal: **no bloquea, propone**. Si el renglón se leyera
        como una prohibición, alguien dejaría el camión roto en patio por un
        dato que puede estar mal levantado."""
        r = _correr(tmp_path, _payload(garantias_vigentes=[_garantia()]),
                    sistema='embrague')
        assert 'No bloquea nada' in r['garantias']

    def test_y_el_boton_de_abrir_la_orden_SIGUE_ahi(self, tmp_path):
        """La propiedad, no la frase: con la propuesta en pantalla, abrir la
        orden sigue siendo posible."""
        r = _correr(tmp_path, _payload(garantias_vigentes=[_garantia()]),
                    sistema='embrague')
        assert 'flotaAbrirOT()' in r['html']

    def test_pinta_las_DOS_dimensiones_aunque_una_sola_cubra(self, tmp_path):
        """La contracara del `o`: vigente por fecha y pasada de kilómetros es un
        caso real, y quien llama al taller tiene que verlo."""
        r = _correr(tmp_path, _payload(garantias_vigentes=[
            _garantia(por_km=False, km_actual=150000)]), sistema='embrague')
        assert '2026-09-05' in r['garantias']
        assert '110000' in r['garantias']
        assert 'vencida hasta 110000 km' in r['garantias']

    def test_una_dimension_sin_declarar_se_dice_asi_y_no_como_vencida(
            self, tmp_path):
        r = _correr(tmp_path, _payload(garantias_vigentes=[
            _garantia(hasta_km=None, garantia_km=None, por_km='sin_dato')]),
            sistema='embrague')
        assert 'sin plazo por kilómetros' in r['garantias']
        assert 'vencida hasta' not in r['garantias']

    def test_sin_odometro_dice_que_NO_SE_PUDO_revisar(self, tmp_path):
        """`sin_dato` no se pinta como si estuviera bien ni como vencida. Se
        dice qué falta para poder juzgarla, que es lo único accionable."""
        r = _correr(tmp_path, _payload(km_actual=None, garantias_vigentes=[
            _garantia(por_km='sin_dato', km_actual=None)]), sistema='embrague')
        assert 'no se pudo revisar' in r['garantias']


class TestElExpedienteDeOrdenes:

    def test_sin_ordenes_lo_dice_con_palabras(self, tmp_path):
        r = _correr(tmp_path, _payload())
        assert 'no ha entrado al taller' in r['html']

    def test_una_orden_abierta_muestra_sus_gestos(self, tmp_path):
        r = _correr(tmp_path, _payload(ordenes=[_orden()], abiertas=1))
        assert 'Taller Los Andes' in r['html']
        assert 'flotaCerrarOT(3' in r['html']
        assert 'flotaAnularOT(3)' in r['html']

    def test_una_orden_sin_trabajos_explica_por_que_no_se_puede_cerrar(
            self, tmp_path):
        """La regla se dice en la pantalla y no solo en el 409: cerrar es lo que
        saca el renglón rojo del tablero, o sea el camino barato de la regla
        11."""
        r = _correr(tmp_path, _payload(ordenes=[_orden()]))
        assert 'sin registro de qué se hizo' in r['html']
        assert 'se anula con motivo escrito' in r['html']

    def test_un_trabajo_sin_factura_sale_marcado(self, tmp_path):
        r = _correr(tmp_path, _payload(ordenes=[_orden(
            estado='cerrada', cerrada_ts='2026-03-10T22:00:00Z',
            sin_factura=1, intervenciones=[_garantia(gasto_id=None)])]))
        assert 'sin factura recibida' in r['html']

    def test_y_uno_facturado_NO(self, tmp_path):
        """La otra dirección: un marcador incondicional pasaría el de arriba y
        dejaría todo el expediente en amarillo para siempre."""
        r = _correr(tmp_path, _payload(ordenes=[_orden(
            estado='cerrada', cerrada_ts='2026-03-10T22:00:00Z',
            sin_factura=0, intervenciones=[_garantia(gasto_id=42)])]))
        assert 'sin factura recibida' not in r['html']
        assert 'factura registrada' in r['html']

    def test_sin_dato_de_garantia_se_dice_como_no_se_pregunto(self, tmp_path):
        """**Regla 4 en la línea que alguien lee.** Una factura que no dice nada
        no es una factura sin garantía."""
        r = _correr(tmp_path, _payload(ordenes=[_orden(intervenciones=[
            _garantia(garantia_declarada='sin_dato', hasta_fecha=None,
                      hasta_km=None, por_fecha='sin_dato', por_km='sin_dato',
                      vigente='sin_dato', cubre=False)])]))
        assert 'no se preguntó' in r['html']
        assert 'no es lo mismo que no tenerla' in r['html']

    def test_declarada_no_se_dice_distinto(self, tmp_path):
        r = _correr(tmp_path, _payload(ordenes=[_orden(intervenciones=[
            _garantia(garantia_declarada='no', hasta_fecha=None, hasta_km=None,
                      por_fecha='sin_dato', por_km='sin_dato',
                      vigente='sin_dato', cubre=False)])]))
        assert 'no trae garantía' in r['html']


class TestElFormularioNoDejaTeclearElVencimiento:
    """Regla 6 aplicada a la garantía, sostenida también en la pantalla. Si los
    campos derivados existieran acá, el servidor sería lo único que la sostiene
    — y una pantalla que ofrece escribir algo que el servidor rechaza es un
    formulario que devuelve 400 sin que nadie entienda por qué."""

    def test_el_formulario_del_trabajo_pide_los_PLAZOS(self, tmp_path):
        r = _correr(tmp_path, _payload(ordenes=[_orden()]))
        assert 'Meses de garantía' in r['html']
        assert 'Kilómetros de garantía' in r['html']

    def test_y_NO_pide_ninguna_fecha_de_vencimiento(self, tmp_path):
        r = _correr(tmp_path, _payload(ordenes=[_orden()]))
        for prohibido in ('garantia_hasta_fecha', 'garantia_hasta_km',
                          'Vence el', 'Vencimiento'):
            assert prohibido not in r['html']

    def test_lo_dice_en_la_pantalla_y_no_solo_en_el_codigo(self, tmp_path):
        r = _correr(tmp_path, _payload(ordenes=[_orden()]))
        assert 'no se escriben' in r['html']

    def test_el_estado_de_la_garantia_no_trae_opcion_marcada(self, tmp_path):
        """Mismo criterio que el estado del tanque: un valor puesto por inercia
        no produce un error — produce un dato inventado que nadie desmiente.
        «No sé» tiene que ser una respuesta que haya que elegir."""
        r = _correr(tmp_path, _payload(ordenes=[_orden()]))
        assert '<option value="" selected>— elegí una —</option>' in r['html']
        assert 'no es «no trae»' in r['html']


class TestElFormularioDeFactura:

    def _cerrada_con_pendiente(self):
        return _payload(ordenes=[_orden(
            estado='cerrada', cerrada_ts='2026-03-10T22:00:00Z', sin_factura=1,
            intervenciones=[_garantia(gasto_id=None)])])

    def test_aparece_solo_cuando_hay_trabajos_sin_facturar(self, tmp_path):
        r = _correr(tmp_path, self._cerrada_con_pendiente())
        assert 'Llegó la factura de esta visita' in r['html']

    def test_no_aparece_si_ya_esta_todo_facturado(self, tmp_path):
        r = _correr(tmp_path, _payload(ordenes=[_orden(
            estado='cerrada', cerrada_ts='2026-03-10T22:00:00Z', sin_factura=0,
            intervenciones=[_garantia(gasto_id=42)])]))
        assert 'Llegó la factura de esta visita' not in r['html']

    def test_los_trabajos_van_con_casilla_y_no_asumidos(self, tmp_path):
        """Una factura del taller puede cubrir dos de los tres trabajos, y darlos
        todos por cubiertos dejaría el tercero contado como facturado sin que
        nadie lo mirara."""
        r = _correr(tmp_path, self._cerrada_con_pendiente())
        assert 'type="checkbox"' in r['html']
        assert 'fa-3-i-7' in r['html']

    def test_NO_pide_kilometraje_y_explica_por_que(self, tmp_path):
        """La factura se digita treinta días después, en una oficina, sin el
        vehículo delante. Pedirle el odómetro produce un número inventado o uno
        que el servidor rechaza por retroceso."""
        r = _correr(tmp_path, self._cerrada_con_pendiente())
        assert 'fa-3-km' not in r['html']
        assert 'El kilometraje no se pide' in r['html']
        assert 'entró al taller' in r['html']


class TestLasURLVanEnterasYNoConcatenadas:
    """El trinquete de rutas huérfanas mide adyacencia sobre el TEXTO del PWA:
    con la URL armada por concatenación, `/flota/ordenes/${id}/${verbo}` no
    contiene ningún `/anular` y el endpoint se declara sin consumidor. Ya destapó
    exactamente esto con `/aplazar` y con la de tanqueos.

    Se verifica sobre el archivo y no sobre lo pintado a propósito: lo que se
    persigue es la forma de la constante, que no llega al HTML."""

    def test_las_cinco_estan_escritas_enteras(self):
        js = FLOTA_JS.read_text(encoding='utf-8')
        for trozo in ('/flota/ordenes/${placa}', '/flota/ordenes/${id}/cerrar',
                      '/flota/ordenes/${id}/anular',
                      '/flota/ordenes/${id}/factura',
                      '/flota/ordenes/${id}/intervenciones'):
            assert trozo in js, f'{trozo} dejó de estar escrita entera'

    def test_ninguna_se_arma_con_el_verbo_por_variable(self):
        js = FLOTA_JS.read_text(encoding='utf-8')
        assert '/flota/ordenes/${id}/${' not in js


class TestLaPantallaNoOfreceLoQueElBackendNiega:
    """El gesto que termina en 403 no se pinta — y lo que sigue siendo suyo, sí.

    Desde el 2026-09-09 abrir, cerrar y anular una orden son `DECIDE_FLOTA`
    (gestión). Esconder el botón **no es el control de acceso** —ese vive en el
    backend y lo ejerce la matriz rol × endpoint por HTTP—; es que dejarle a la
    vista un gesto que el sistema le va a negar **enseña a ignorar los errores**,
    que es la misma razón por la que a este rol se le esconden las otras
    pestañas (`especialista-control-flota.md:18`).

    Las dos direcciones, porque un `flotaDecide()` que devolviera `false` a todo
    el mundo dejaría la mitad de arriba en verde.
    """

    def _con_una_abierta(self, tmp_path, rol):
        o = _orden(estado='abierta', intervenciones=[])
        return _correr(tmp_path, {'ordenes': [o], 'sistemas': ['frenos'],
                                  'tipos': ['correctiva'], 'garantias': []},
                       rol=rol)['html']

    def test_control_de_flota_no_ve_los_verbos_de_decision(self, tmp_path):
        html = self._con_una_abierta(tmp_path, 'control_flota')
        for gesto in ('Abrir orden', 'Volvió del taller', 'Anular'):
            assert gesto not in html, f'le ofreció «{gesto}» y el backend lo niega'

    def test_y_le_dice_a_quien_le_toca_en_vez_de_dejar_un_hueco(self, tmp_path):
        """Un formulario que desaparece sin explicación es indistinguible de uno
        que se rompió. El gesto que reemplaza al botón es «escalá»."""
        html = self._con_una_abierta(tmp_path, 'control_flota')
        assert 'lo decide gestión' in html

    def test_pero_SIGUE_viendo_lo_que_es_registro(self, tmp_path):
        """La mitad que un recorte de permisos se lleva por delante sin que
        nadie se entere: registrar qué se hizo sigue siendo suyo."""
        html = self._con_una_abierta(tmp_path, 'control_flota')
        assert 'Registrar un trabajo de esta visita' in html

    def test_gestion_SI_los_ve(self, tmp_path):
        html = self._con_una_abierta(tmp_path, 'admin')
        for gesto in ('Abrir orden', 'Volvió del taller', 'Anular'):
            assert gesto in html, f'a gestión le falta «{gesto}»'
