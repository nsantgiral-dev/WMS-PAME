"""
Las pantallas dicen en qué va la emisión fiscal (H2b, 2026-09-26), en Node con
`util.js` real.

- Una caja cerrada y en la cola de Siesa no es «⚠ Reintentar Siesa»: no ofrece
  «Limpiar bultos» ni reintentar, y tocarla no vuelve a cerrar.
- Una caja con documento sin confirmar la resuelve el administrador.
- Reintentar el envío no reimprime las etiquetas.
- La cola de pedidos: «en cola» es EN PROCESO; «✓ Despachado» solo si puede
  salir; una factura sin remisión va a ERROR.
- El muelle ofrece bajar del camión un bulto cargado que no puede salir.
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
const nodos = {};
const nodo = (id) => (nodos[id] ||= { id, style: {}, classList: { add() {}, remove() {}, toggle() {} },
  remove() {}, innerHTML: '', textContent: '', value: '', querySelector: () => null,
  appendChild() {}, focus() {}, scrollIntoView() {} });
const ctx = {
  console,
  document: { getElementById: (id) => nodo(id), querySelector: () => null, querySelectorAll: () => [],
              addEventListener() {}, createElement: () => nodo('_x'), body: { appendChild() {} } },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {}, userAgent: 'node' },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
};
ctx.globalThis = ctx; ctx.window = ctx;
ctx.location = { origin: 'http://t' }; ctx.addEventListener = () => {};
vm.createContext(ctx);
for (const a of ['util.js', 'modal.js', 'app.js', 'packing.js', 'rutas.js'])
  vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf-8'), ctx, { filename: a });
const out = { alertas: [], posts: [], etiquetas: 0 };
Object.assign(ctx, { __G: G, __out: out });
vm.runInContext(`
  OPERARIO = { rol: 'empacador', puede_usar_camara: false };
  alerta = (m, t) => __out.alertas.push([String(m), t]);
  post = async (u, b) => { __out.posts.push(u); return { bultos: [1], estado_siesa: 'EN_COLA' }; };
  get = async (u) => __G.detalle;
  put = async () => ({});
  empImprimirEtiquetas = () => { __out.etiquetas += 1; };
  empCargarTareas = async () => {};
`, ctx);
if (G.tareas) {
  vm.runInContext('EMP_TAREAS_ALL = __G.tareas; EMP_FILTRO_TIPO = "PEDIDO"; empRenderListaTareas();', ctx);
  out.lista = nodos['emp-lista'].innerHTML;
}
if (G.detalle) { await ctx.empIniciarHUD(G.detalle.id); }
if (G.reintentar) { await ctx.empReintentarSiesa(G.reintentar); }
if (G.pedidos) out.grupos = G.pedidos.map(p => ctx.pedidoGrupo(p));
if (G.grupo) out.grupo = ctx._htmlGrupoRuta(G.grupo, 0, 1, 7);
process.stdout.write(JSON.stringify(out));
"""


def _correr(tmp_path, **g):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'h.mjs'
    h.write_text(_ARNES, encoding='utf-8')
    f = tmp_path / 'g.json'
    f.write_text(json.dumps(g), encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(f)], capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _caja(i, emision, **kw):
    return {'id': i, 'estado': 'VERIFICADO', 'siesa_triggered': False, 'tipo_documento': 'PEDIDO',
            'numero_pedido_siesa': f'PD{i}', 'total_items': 1, 'items_verificados': 1,
            'bultos': [{'tipo': 'Caja'}], 'estado_emision': emision, **kw}


class TestLaCajaEnColaNoEsUnError:

    def test_la_lista(self, tmp_path):
        r = _correr(tmp_path, tareas=[_caja(11, 'EN_COLA')])
        html = r['lista']
        assert 'En cola de facturación' in html
        assert 'Reintentar Siesa' not in html and 'Limpiar bultos' not in html
        assert 'empIniciarHUD(11)' not in html

    def test_fallido_ofrece_reintentar_y_limpiar(self, tmp_path):
        html = _correr(tmp_path, tareas=[_caja(12, 'FALLIDO')])['lista']
        assert 'Reintentar Siesa' in html and 'Limpiar bultos' in html
        assert 'empIniciarHUD(12)' in html

    @pytest.mark.parametrize('emision', ['SIN_VERIFICAR', 'REMISION_SIN_FACTURA',
                                         'FACTURA_SIN_REMISION'])
    def test_con_documento_lo_resuelve_el_administrador(self, tmp_path, emision):
        html = _correr(tmp_path, tareas=[_caja(13, emision)])['lista']
        assert 'lo resuelve el administrador' in html
        assert 'Limpiar bultos' not in html and 'empIniciarHUD(13)' not in html

    def test_un_servidor_viejo_sin_el_campo_sigue_como_antes(self, tmp_path):
        c = _caja(14, None)
        del c['estado_emision']
        html = _correr(tmp_path, tareas=[c])['lista']
        assert 'Reintentar Siesa' in html

    def test_tocarla_no_la_vuelve_a_cerrar(self, tmp_path):
        r = _correr(tmp_path, detalle=_caja(15, 'EN_COLA'))
        assert r['posts'] == []
        assert any('cola de facturación' in a[0] for a in r['alertas'])

    def test_reintentar_no_reimprime_las_etiquetas(self, tmp_path):
        r = _correr(tmp_path, reintentar=_caja(16, 'FALLIDO'))
        assert r['posts'] == ['/api/packing/16/cerrar']
        assert r['etiquetas'] == 0


class TestLaColaDePedidos:

    def test_grupos(self, tmp_path):
        r = _correr(tmp_path, pedidos=[
            {'packing_estado': 'VERIFICADO', 'estado_emision': 'EN_COLA'},
            {'siesa_triggered': True, 'despachable': True, 'estado_emision': 'DESPACHADO'},
            {'siesa_triggered': True, 'despachable': False,
             'estado_emision': 'FACTURA_SIN_REMISION'},
            {'packing_estado': 'VERIFICADO', 'estado_emision': 'FALLIDO'},
            {'packing_estado': 'VERIFICADO', 'estado_emision': None, 'picking_iniciado': True},
        ])
        assert r['grupos'] == [1, 2, 3, 3, 1]


class TestElMuelleBajaLoQueNoPuedeSalir:

    def _bulto(self, despachable):
        return {'id': 5, 'estado': 'CARGADO', 'codigo_barras': 'PD1-01', 'numero_pedido': 'PD1',
                'cliente': 'X', 'tipo': 'Caja', 'numero': 1, 'total': 1,
                'despachable': despachable}

    def test_cargado_que_no_puede_salir(self, tmp_path):
        html = _correr(tmp_path, grupo={'destino': 'Neiva', 'bultos': [self._bulto(False)]})['grupo']
        assert 'muelleBajarCargado(5)' in html and 'Bajar del camión' in html

    def test_cargado_que_puede_salir_no(self, tmp_path):
        html = _correr(tmp_path, grupo={'destino': 'Neiva', 'bultos': [self._bulto(True)]})['grupo']
        assert 'muelleBajarCargado' not in html
