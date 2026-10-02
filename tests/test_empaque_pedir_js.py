"""Pedir por paquete o por unidad (2026-10-02), en Node con `util.js` real.

Decisiones del dueño que se prueban acá:
1. Si el producto viene en paquete la casilla ARRANCA en paquetes; si no
   alcanza uno completo, en unidades. Siempre se ve cuántas unidades trae el
   paquete y, en vivo, cuántas suman los elegidos.
2. Se pueden pedir unidades sueltas junto con los paquetes; lo disponible se
   valida sobre el total en unidades.

La pantalla manda paquetes + sueltas + el total; el servidor recalcula
(`empaque_producto.unidades_de_linea`, probado en `test_empaque_una_politica`).
"""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0]; const guion = args[1];
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '', value: '', textContent: '' });
const alertas = []; const posts = [];
const ctx = { console, els, alertas, posts,
  document: { getElementById: el, createElement: () => ({ style: {} }) },
  alerta: (m, t) => alertas.push([String(m), t]),
  get: () => Promise.resolve({}),
  post: (url, body) => { posts.push([url, body]); return Promise.resolve({ id: 99 }); },
  postConReintento: () => Promise.resolve({}),
  _modalConfirmar: () => Promise.resolve(true),
  tiendaSubtab: () => {}, OPERARIO: { bodega_siesa_id: 'PC1', nombre_punto_venta: 'Pitalito' },
  frescuraStockHtml: () => '', frescuraStock: () => ({}),
};
vm.createContext(ctx);
for (const f of args.slice(2)) vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
(async () => {
  const salida = await vm.runInContext(guion, ctx);
  console.log(JSON.stringify(salida === undefined ? null : salida));
})().catch(e => { console.error(e.stack || e); process.exit(1); });
"""

PQ12 = {'unidad': 'PQ', 'factor': 12, 'fuente': 'PRODUCTO_EMPAQUES'}
MALO = '<img src=x onerror=alert(1)>'


def correr(guion, archivos=('util.js',)):
    if not shutil.which('node'):
        pytest.skip('sin node')
    envuelto = f'(async () => {{ {guion} }})()'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), envuelto, *archivos],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLosTextos:
    def test_disponible_en_unidades_y_paquetes(self):
        d = correr(f"""const e = {json.dumps(PQ12)};
          return [empaqueTexto(1368, e), empaqueTexto(29, e), empaqueTexto(5, e), empaqueTexto(1368, null),
                  empaqueResumen(2, 5, e), empaqueResumen(3, 0, e), empaqueResumen(0, 5, e), empaqueEtiqueta(e)];""")
        assert d == ['1.368 und · 114 PQ×12', '29 und · 2 PQ×12 + 5 und', '5 und · no alcanza un PQ×12',
                     '1.368 und', '2 PQ + 5 und = 29 und', '3 PQ = 36 und', '5 und', 'PQ × 12 und']

    def test_la_linea_guardada_dice_como_se_pidio(self):
        d = correr(f"""const it = {{cantidad_solicitada: 29, empaque: {json.dumps(PQ12)},
                        pedido_como: {{paquetes: 2, sueltas: 5, factor: 12, unidad: 'PQ'}}}};
          return [empaqueDeLinea(it), empaqueDeLinea(it, 24),
                  empaqueDeLinea({{cantidad_solicitada: 30, empaque: {json.dumps(PQ12)}, pedido_como: null}})];""")
        assert d == ['2 PQ + 5 und = 29 und', '24 und · 2 PQ×12', '30 und · 2 PQ×12 + 6 und']


class TestLasCasillas:
    def test_arranca_en_paquetes_si_alcanza_uno(self):
        h = correr(f"return empaqueCasillasHtml('qty-A', {json.dumps(PQ12)}, 1368, null);")
        assert 'id="qty-A-pq"' in h and 'id="qty-A-und"' in h
        assert 'value="1" id="qty-A-pq"' in h and 'value="0" id="qty-A-und"' in h
        assert 'PQ × 12 und' in h and '= 12 und' in h and 'no alcanza' not in h

    def test_arranca_en_unidades_si_no_alcanza_un_paquete(self):
        h = correr(f"return empaqueCasillasHtml('qty-B', {json.dumps(PQ12)}, 5, null);")
        assert 'value="0" id="qty-B-pq"' in h and 'value="1" id="qty-B-und"' in h
        assert 'no alcanza un paquete completo' in h and '= 1 und' in h

    def test_sin_paquete_una_casilla_como_siempre(self):
        h = correr("return empaqueCasillasHtml('qty-C', null, 50, null);")
        assert 'id="qty-C-und"' in h and 'qty-C-pq' not in h and 'min="1"' in h

    def test_el_total_en_vivo_y_la_lectura(self):
        d = correr(f"""const e = {json.dumps(PQ12)};
          els['qty-D-pq'] = {{ value: '2' }}; els['qty-D-und'] = {{ value: '5' }};
          els['qty-D-total'] = {{ textContent: '' }};
          empaqueActualizarTotal('qty-D', 12);
          return [els['qty-D-total'].textContent, empaqueLeer('qty-D', e)];""")
        assert d == ['= 29 und', {'paquetes': 2, 'sueltas': 5, 'total': 29}]

    def test_la_unidad_se_escapa(self):
        h = correr(f"return empaqueCasillasHtml('qty-E', {{unidad: {json.dumps(MALO)}, factor: 12}}, 100, null);")
        assert '<img' not in h and '&lt;img' in h


def _stock():
    return [{'codigo_siesa': 'DORI07', 'nombre': 'BOLIGRAFO DORICOLOR 07', 'producto_id': 7,
             'disponible': 1368, 'empaque': PQ12},
            {'codigo_siesa': 'SUELTO', 'nombre': 'BORRADOR', 'producto_id': 8,
             'disponible': 40, 'empaque': None}]


class TestPedirEnLaTienda:
    def _con_stock(self, resto):
        return correr(f"""_TIENDA_STOCK = {json.dumps(_stock())}; _TIENDA_STOCK_ESTADO = 'ok';
          els['tienda-stock-lista'] = {{ innerHTML: '' }};
          {resto}""", ('util.js', 'tienda.js'))

    def test_la_lista_muestra_el_paquete_y_arranca_en_paquetes(self):
        h = self._con_stock("tiendaRenderStock(); return els['tienda-stock-lista'].innerHTML;")
        assert '1.368 und · 114 PQ×12' in h
        assert 'value="1" id="qty-DORI07-pq"' in h and 'PQ × 12 und' in h
        assert 'id="qty-SUELTO-und"' in h and 'qty-SUELTO-pq' not in h

    def test_paquetes_y_sueltas_al_carrito_y_al_servidor(self):
        d = self._con_stock("""
          els['qty-DORI07-pq'] = { value: '2' }; els['qty-DORI07-und'] = { value: '5' };
          tiendaAgregarCarrito('DORI07', 'BOLIGRAFO', 1368, 7);
          els['qty-SUELTO-und'] = { value: '3' };
          tiendaAgregarCarrito('SUELTO', 'BORRADOR', 40, 8);
          const carrito = _TIENDA_CARRITO.map(c => [c.cantidad, c.paquetes, c.sueltas]);
          await tiendaEnviarSolicitud(null);
          return { carrito, items: posts[0][1].items, alertas };""")
        assert d['carrito'] == [[29, 2, 5], [3, 0, 3]]
        assert d['items'][0] == {'producto_id': 7, 'cantidad_solicitada': 29, 'paquetes': 2,
                                 'sueltas': 5, 'disponible_siesa': 1368}
        assert d['items'][1] == {'producto_id': 8, 'cantidad_solicitada': 3, 'disponible_siesa': 40}

    def test_mas_de_lo_disponible_se_dice_y_no_entra(self):
        d = self._con_stock("""
          els['qty-DORI07-pq'] = { value: '200' }; els['qty-DORI07-und'] = { value: '0' };
          tiendaAgregarCarrito('DORI07', 'BOLIGRAFO', 1368, 7);
          return { n: _TIENDA_CARRITO.length, alertas };""")
        assert d['n'] == 0
        assert d['alertas'][0][1] == 'error'
        assert '200 PQ = 2.400 und' in d['alertas'][0][0] and '1.368 und' in d['alertas'][0][0]


class TestAprobarYCards:
    def test_aprobar_arranca_en_lo_pedido_y_manda_paquetes(self):
        d = correr(f"""const it = {{ id: 5, cantidad_solicitada: 29, empaque: {json.dumps(PQ12)},
                         pedido_como: {{paquetes: 2, sueltas: 5, factor: 12, unidad: 'PQ'}},
                         disponible_siesa: 100 }};
          const h = _trasCasillasAprobar(it, 'apr-');
          els['apr-5-pq'] = {{ value: '1' }}; els['apr-5-und'] = {{ value: '0' }};
          return [h, _trasLeerAprobado(it, 'apr-')];""", ('util.js', 'traslados.js'))
        h, lectura = d
        assert 'value="2" id="apr-5-pq"' in h and 'value="5" id="apr-5-und"' in h
        assert lectura == {'cantidad_aprobada': 12, 'paquetes': 1, 'sueltas': 0}

    def test_aprobar_sin_paquete_admite_cero(self):
        d = correr("""const it = { id: 6, cantidad_solicitada: 4, empaque: null };
          const h = _trasCasillasAprobar(it, 'apr-');
          els['apr-6-und'] = { value: '0' };
          return [h, _trasLeerAprobado(it, 'apr-')];""", ('util.js', 'traslados.js'))
        assert 'min="0"' in d[0] and 'value="4"' in d[0]
        assert d[1] == {'cantidad_aprobada': 0}

    def test_la_tarjeta_del_admin_dice_como_se_pidio(self):
        h = correr(f"""return _renderTrasladoCard({{ estado: 'ENVIADA', items: [
            {{ producto_codigo: 'DORI07', producto_nombre: 'BOLIGRAFO DORICOLOR 07', cantidad_solicitada: 29, cantidad_enviada: 0,
               empaque: {json.dumps(PQ12)}, pedido_como: {{paquetes: 2, sueltas: 5, factor: 12, unidad: 'PQ'}} }}] }});""",
                   ('util.js', 'traslados.js'))
        assert 'BOLIGRAFO DORICOLOR 07</span>' in h and '· DORI07</span>' in h
        assert '2 PQ + 5 und = 29 und' in h


class TestElHudDePicking:
    def test_paquetes_desde_las_unidades(self):
        d = correr("return [_pickingTextoPaquetes(2, 5, 29, 12, 'PQ'), _pickingTextoPaquetes(0, 0, 24, 12, 'PQ')];",
                   ('util.js', 'picking.js'))
        assert d == ['+5 und · de 2 PQ + 5 und', 'de 2 PQ']
