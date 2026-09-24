"""Inventario Cíclico sin enredos — lo que ve cada persona (2026-09-23).

Revisión de la pantalla como la lee un jefe de bodega. Lo que se arregló y
este archivo fija, renderizando `conteo.js` en Node con `util.js` REAL:

| Enredo | Ahora |
|---|---|
| Tarjetas con `DESCUADRE`, `SEGUNDO_CONTEO`, `CC2 / CC3`, `AJ-SAL`, «Omitir CC2» | Palabras de bodega (las del servidor en el tablero) |
| La tarjeta resuelta por el conteo definitivo escondía su cifra | Se ve «definitivo» con la cantidad que manda el ajuste |
| «Confirmar ajuste» activo con `bloqueo_ajuste` del servidor → error | El botón no ofrece aprobar; el motivo está arriba |
| Modal de aprobar: la cifra de Siesa rotulada «WMS»; «se usará el 2do conteo» con definitivo; «Observaciones» que no viajaban | Rotulado «Siesa»; dice que manda el definitivo; el campo muerto no está |
| «Aprobar ajuste ahora → Siesa» del Definitivo sin confirmación | Pide confirmar, como el tablero |
| «Cancelar conteo» con un botón «Cancelar» que cerraba el diálogo | «Cancelar conteo» / «Volver» |
| Tablero: «Por motivo: NO_ENCONTRADO 1», «CC2», «No se puede aprobar · MOVIMIENTO_DURANTE_CONTEO» | El texto que ya manda el servidor |
| «Recogido sin despachar» vivía en la pestaña Definitivo; el servidor manda a buscarlo en «Inventario Cíclico» | Está en 🧭 Líder, y el mensaje del servidor lo dice |
| `MOVIMIENTO_CONTINUO` tenía texto en la pantalla vieja y no en el tablero | Todo motivo de bloqueo tiene su texto |
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

# Códigos internos que un jefe de bodega no tiene por qué leer.
JERGA = ('DESCUADRE', 'SEGUNDO_CONTEO', 'TERCER_CONTEO', 'EN_PROCESO', 'AJUSTADO',
         'CC1', 'CC2', 'CC3', 'AJ-SAL', 'AJ-ENT', 'Omitir CC2', 'MOVIMIENTO_CONTINUO',
         'NO_ENCONTRADO', 'MOVIMIENTO_DURANTE_CONTEO')

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0], modo = args[1] || '';
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, value: '', disabled: false, innerHTML: '', textContent: '', title: '', options: [] });
const llamadas = { put: [], confirmar: [], texto: [] };
const ctx = { console, window: {}, document: { getElementById: el },
  OPERARIO: { puede_usar_camara: false }, TAREA_ACTUAL: null,
  alerta: () => {}, cargarConteos: async () => {}, cargarConteoStats: async () => {},
  cargarConteoDefinitivos: () => {},
  put: async (url, body) => { llamadas.put.push(url); return { mensaje: 'ok' }; },
  _modalConfirmar: async (msg, opts) => { llamadas.confirmar.push(opts || {}); return modo === 'acepta'; },
  _modalTexto: async (t, msg, opts) => { llamadas.texto.push([t, opts || {}]); return null; } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
if (modo === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
// Los helpers de la pantalla se leen del contexto después de cargar conteo.js:
// el stub de defCerrarModal no puede ir antes (la declaración lo pisaría).
vm.runInContext('defCerrarModal = () => {};', ctx);
const X = '<img src=x onerror=alert(1)>';
const texto = h => h.replace(/<[^>]+>/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/\s+/g, ' ');
const raiz = { id: 11, codigo: 'CC-11', tipo: 'DIARIO_ABC', clasificacion_abc: 'A', estado: 'DESCUADRE',
  producto_codigo: 'PAPELSP9218', producto_nombre: 'CUADERNO', ubicacion_codigo: 'A-01', bodega_siesa_id: 'NB1',
  existencia_siesa: 3570, teorico_siesa: 3570, cantidad_fisica: 3560, diferencia: -12, motivo_codigo: 'AJ-SAL',
  operario_id: 4, bloqueo_ajuste: null, no_sale_solo: null, valor_ajuste: 48000,
  segundo_conteo: { id: 12, estado: 'DESCUADRE', cantidad_fisica: 3565, diferencia: -5, teorico_siesa: 3570,
    tercer_conteo: { id: 13, estado: 'DESCUADRE', cantidad_fisica: 3558 } } };
const card = vm.runInContext('_renderCardAccion', ctx);
const conDef = card(raiz);
const bloq = card({ ...raiz, id: 14, bloqueo_ajuste: 'Hubo ventas mientras se contaba' });
const espera = card({ ...raiz, id: 15, estado: 'SEGUNDO_CONTEO', segundo_conteo: { id: 16, estado: 'PENDIENTE' } });
const tercero = card({ ...raiz, id: 17, estado: 'TERCER_CONTEO',
  segundo_conteo: { ...raiz.segundo_conteo, tercer_conteo: { id: 18, estado: 'PENDIENTE' } } });
const progreso = vm.runInContext('_renderCardProgreso', ctx)({ ...raiz, estado: 'EN_PROCESO', segundo_conteo: null });
const resuelto = vm.runInContext('_renderCardResuelto', ctx)({ ...raiz, estado: 'AJUSTADO' });

vm.runInContext('conteoAbrirAjuste', ctx)(raiz);
const modalDef = els['conteo-ajuste-info'].innerHTML;
vm.runInContext('conteoAbrirAjuste', ctx)({ ...raiz, segundo_conteo: { id: 12, estado: 'DESCUADRE', cantidad_fisica: 3560, diferencia: -12, teorico_siesa: 3570 } });
const modalIgual = els['conteo-ajuste-info'].innerHTML;
vm.runInContext('conteoAbrirAjuste', ctx)({ ...raiz, bodega_siesa_id: X, ubicacion_codigo: X });
const modalX = els['conteo-ajuste-info'].innerHTML;

const tablero = vm.runInContext('liderTableroHtml', ctx)({
  permisos: { reabrir_cancelar_bloqueado: true, resolver_novedad: true, aprobar_ajuste: true, recontar: true, cancelar_conteo: true },
  almacen_id: 1, resumen: { por_bloque: {} },
  decisiones: {
    bloqueados: { total: 2, por_motivo: { MOVIMIENTO_CONTINUO: 1, NO_ENCONTRADO: 1 }, filas: [
      { id: 21, producto_codigo: 'P1', motivo_bloqueo: 'NO_ENCONTRADO', motivo_texto: 'No lo encontró', nivel: 'CC2' },
      { id: 22, producto_codigo: 'P2', motivo_bloqueo: 'MOVIMIENTO_CONTINUO', motivo_texto: 'Se vendía mientras se contaba', nivel: 'CC3' }] },
    ajustes: { total_descuadres: 1, aprobables: { total: 0, filas: [] },
      bloqueados: { total: 1, por_motivo: { MOVIMIENTO_DURANTE_CONTEO: 1 }, filas: [
        { id: 14, producto_codigo: 'P3', motivo_clave: 'MOVIMIENTO_DURANTE_CONTEO', motivo: 'ventas POS',
          accion: { tipo: 'RECONTAR', texto: 'Recontar en un momento sin ventas' } }] } },
    rechazados_siesa: { total: 1, total_sistema: 1, filas: [{ id: 30, producto_codigo: 'P4', motivo_codigo: 'AJ-SAL', unidades: 4, intentos: 3 }] } },
  hoy: {} });

(async () => {
  await vm.runInContext('defAprobarAjuste', ctx)(11);
  await vm.runInContext('conteoCancelar', ctx)(11);
  console.log(JSON.stringify({
    conDef: texto(conDef), bloq: texto(bloq), bloqHtml: bloq, espera: texto(espera), tercero: texto(tercero),
    progreso: texto(progreso), resuelto: texto(resuelto),
    modalDef: texto(modalDef), modalIgual: texto(modalIgual),
    modalCrudos: (modalX.match(/<img/g) || []).length, modalEscapados: (modalX.match(/&lt;img/g) || []).length,
    tablero: texto(tablero), llamadas,
  }));
})();
"""


def _render(modo=''):
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), modo],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.fixture(scope='module')
def r():
    return _render()


def _sin_jerga(texto):
    return [j for j in JERGA if re.search(r'(?<![\w-])' + re.escape(j) + r'(?![\w-])', texto)]


class TestLasTarjetasHablanEnPalabrasDeBodega:

    @pytest.mark.parametrize('vista', ['conDef', 'bloq', 'espera', 'tercero', 'progreso', 'resuelto'])
    def test_sin_codigos_internos(self, r, vista):
        assert _sin_jerga(r[vista]) == [], (vista, r[vista])

    def test_el_detector_de_jerga_muerde(self):
        """Si el detector no viera nada, el test de arriba sería verde siempre."""
        assert _sin_jerga('Estado DESCUADRE · CC2 · AJ-SAL') == ['DESCUADRE', 'CC2', 'AJ-SAL']
        assert _sin_jerga('Contado con diferencia · 2º conteo · faltante') == []

    def test_los_estados_se_leen(self, r):
        assert 'Contado con diferencia' in r['conDef']
        assert 'Esperando el 2º conteo' in r['espera']
        assert 'Esperando el conteo definitivo' in r['tercero']
        assert 'Contándose' in r['progreso']
        assert 'Ajustado en Siesa' in r['resuelto']
        assert 'faltante' in r['conDef']

    def test_la_cadena_resuelta_por_el_definitivo_muestra_su_cifra(self, r):
        """Antes la tarjeta en DESCUADRE mostraba solo el 2º conteo (3565) y
        escondía el definitivo (3558), que es el que manda el ajuste."""
        assert '3558' in r['conDef'] and 'definitivo' in r['conDef'], r['conDef']
        assert 'Manda el conteo definitivo' in r['conDef']

    def test_con_bloqueo_del_servidor_no_se_ofrece_aprobar(self, r):
        assert 'No se puede aprobar' in r['bloq']
        assert 'Hubo ventas mientras se contaba' in r['bloq']
        assert 'conteoAbrirAjusteId(' not in r['bloqHtml']

    def test_sin_bloqueo_si_se_ofrece(self, r):
        assert 'Revisar y aprobar ajuste' in r['conDef']

    def test_cancelar_dice_que_cancela(self, r):
        assert '✕ Cancelar' in r['conDef']


class TestElModalDeAprobarDiceLoQuePasa:

    def test_la_cifra_de_siesa_no_se_rotula_wms(self, r):
        assert 'WMS' not in r['modalDef'] and 'Siesa 3570' in r['modalDef'], r['modalDef']

    def test_con_definitivo_dice_que_manda_el_definitivo(self, r):
        assert 'Definitivo 3558' in r['modalDef'], r['modalDef']
        assert 'manda el conteo definitivo' in r['modalDef']
        assert '2do conteo como referencia' not in r['modalDef']

    def test_con_1_y_2_iguales_lo_dice(self, r):
        assert '2º conteo 3560' in r['modalIgual'] and 'coinciden' in r['modalIgual'], r['modalIgual']

    def test_el_motivo_va_en_palabras(self, r):
        assert 'Ajuste por faltante' in r['modalDef']
        assert 'Concepto 603' not in r['modalDef']

    def test_bodega_y_ubicacion_pasan_por_esc(self, r):
        assert r['modalCrudos'] == 0 and r['modalEscapados'] >= 3, r

    def test_el_arnes_muerde_con_esc_roto(self):
        assert _render('esc-roto')['modalCrudos'] >= 3

    def test_no_hay_campo_de_observaciones_que_no_viaja(self):
        """PUT /api/conteo/<id>/ajustar no recibe observaciones: el campo
        recogía un texto que se perdía sin avisar."""
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        assert 'conteo-ajuste-obs' not in html
        js = (PWA / 'conteo.js').read_text(encoding='utf-8')
        assert 'conteo-ajuste-obs' not in js


class TestLasAccionesQueNoSeDeshacenPidenConfirmar:

    def test_aprobar_desde_el_definitivo_pide_confirmacion(self, r):
        assert r['llamadas']['confirmar'], 'defAprobarAjuste no pidió confirmación'
        assert r['llamadas']['put'] == [], 'se envió a Siesa sin confirmar'

    def test_confirmado_si_envia_la_raiz(self):
        assert _render('acepta')['llamadas']['put'] == ['/api/conteo/11/ajustar']

    def test_cancelar_conteo_no_tiene_dos_botones_cancelar(self, r):
        titulo, opts = r['llamadas']['texto'][0]
        assert titulo == 'Cancelar conteo'
        assert opts.get('textoCancelar') == 'Volver' and opts.get('textoConfirmar') == 'Cancelar conteo'


class TestElTableroNoMuestraClaves:

    def test_sin_codigos_internos(self, r):
        assert _sin_jerga(r['tablero']) == [], r['tablero']

    def test_por_motivo_con_el_texto_del_servidor(self, r):
        assert 'No lo encontró: 1' in r['tablero']
        assert 'Recontar en un momento sin ventas: 1' in r['tablero']

    def test_nivel_en_palabras(self, r):
        assert 'en el 2º conteo' in r['tablero'] and 'en el conteo definitivo' in r['tablero']

    def test_rechazo_en_palabras(self, r):
        assert 'faltante de 4 und' in r['tablero']


class TestRecogidoSinDespacharEstaDondeLoMandaBuscarElServidor:

    def _panel(self, html, pid):
        ini = html.index(f'id="{pid}"')
        # hasta el siguiente panel de Inventario Cíclico
        sig = min(i for i in (html.find('id="inv-panel-', ini + 5), len(html)) if i > 0)
        return html[ini:sig]

    def test_la_lista_vive_en_el_panel_lider(self):
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        assert 'id="inv-recogido-lista"' in self._panel(html, 'inv-panel-lider')
        assert 'id="inv-recogido-lista"' not in self._panel(html, 'inv-panel-definitivo')
        assert html.count('id="inv-recogido-lista"') == 1

    def test_el_tablero_la_carga(self):
        js = (PWA / 'conteo.js').read_text(encoding='utf-8')
        cuerpo = js[js.index('async function liderCargar'):js.index('function _liderFila')]
        assert 'cargarConteoRecogidoSinDespachar(almId)' in cuerpo, 'el tablero no carga lo recogido de SU almacén'

    def test_el_mensaje_del_servidor_nombra_la_pestana(self):
        from app.services.conteo_service import ConteoService
        assert 'Líder' in ConteoService.DONDE_DECLARAR_REGRESO
        assert 'Recogido sin despachar' in ConteoService.DONDE_DECLARAR_REGRESO

    def test_inventario_abre_en_lider(self):
        js = (PWA / 'conteo.js').read_text(encoding='utf-8')
        assert "let _INV_SUBTAB = 'lider';" in js
        html = (PWA / 'index.html').read_text(encoding='utf-8')
        assert re.search(r'<div id="inv-panel-lider">', html)
        assert re.search(r'<div id="inv-panel-conteos" style="display:none;">', html)


def test_todo_motivo_de_bloqueo_tiene_su_texto():
    """`MOVIMIENTO_CONTINUO` lo pone el sistema y no estaba en el texto del
    tablero: el líder leía la clave. Se recorre la clase, no una lista a mano,
    para que un motivo nuevo sin texto se ponga rojo acá."""
    from app.models.conteo import MotivoBloqueoConteo
    from app.services.tablero_lider_conteo import MOTIVO_BLOQUEO_TEXTO
    motivos = {v for k, v in vars(MotivoBloqueoConteo).items()
               if k.isupper() and isinstance(v, str)}
    motivos |= set(MotivoBloqueoConteo.VALIDOS)
    assert len(motivos) >= 7, f'piso: el recorrido de la clase se rompió ({motivos})'
    faltan = sorted(m for m in motivos if m not in MOTIVO_BLOQUEO_TEXTO)
    assert faltan == [], faltan
