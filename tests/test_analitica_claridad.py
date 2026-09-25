"""Analítica: arreglos de claridad sin rediseño (2026-09-24).

La clase: **una pantalla que pinta un hueco del dato como si fuera una noticia
del negocio** — «sin base para comparar» en rojo, un «$0» donde no hay valor,
«sin motivo» sobre una liquidación que nadie tiene que justificar, «FALTANTE»
en vez de «Faltante en la ubicación», «10.0» y «DESPACHO_F470» en la línea de
tiempo, «⚠ Fuente: incompleta» tres veces sin decir cuál.

Cada caso con su test, del servidor y de la pantalla (Node con `util.js` real).
"""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'


def _node(script, *args):
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', script, '--', str(PWA), *args],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# ─────────────────────────────────────────────────────────────────────────────
# Fugas: lo que no se sabe va en gris y aparte
# ─────────────────────────────────────────────────────────────────────────────

class TestFugasEstado:

    def _t(self, casos=1, pesos=10.0, sin_dato=None):
        return {'casos': casos, 'pesos': pesos, 'sin_dato': sin_dato}

    def test_los_cinco_estados(self, app):
        from app.services.analitica_fugas import estado_de
        assert estado_de(self._t(sin_dato='sin foto'), {'direccion': 'sin_base'}) == 'sin_dato'
        assert estado_de(self._t(casos=0), {'direccion': 'igual'}) == 'ok'
        assert estado_de(self._t(), {'direccion': 'sin_base'}) == 'sin_base'
        assert estado_de(self._t(), {'direccion': 'sube'}) == 'critico'
        assert estado_de(self._t(), {'direccion': 'baja'}) == 'advertencia'
        assert estado_de(self._t(), {'direccion': 'igual'}) == 'advertencia'

    def test_no_saber_nunca_es_critico(self, app):
        from app.services.analitica_fugas import estado_de
        for tend in ('sin_base', 'sube', 'baja'):
            assert estado_de(self._t(sin_dato='x'), {'direccion': tend}) != 'critico'
        assert estado_de(self._t(), {'direccion': 'sin_base'}) != 'critico'

    def test_fuentes_con_la_convencion_del_shell(self, app, db):
        """Toda fuente incompleta dice su nombre y su motivo: `anFrescura` los
        pinta; sin ellos salía «⚠ Fuente: incompleta» tres veces."""
        from app.services.analitica_fugas import calcular_fugas
        from app.utils.fecha import dia_operativo
        from datetime import timedelta
        h = dia_operativo()
        m = calcular_fugas(h - timedelta(days=29), h)['meta']
        for clave, f in m['fuentes'].items():
            assert f.get('nombre'), clave
            assert {'completa', 'motivo', 'actualizado_en'} <= set(f), clave
            if f['completa'] is False:
                assert f['motivo'], clave
        assert m['completa'] is False, 'sin foto de AV1/TRA1 la fuente sigue incompleta'


_ARNES_FUGAS = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const ctx = { console, window: {}, document: { getElementById: () => ({ innerHTML: '' }) },
  localStorage: { getItem: () => null, setItem(){} } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/analitica.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/analitica_fugas.js', 'utf8'), ctx);
const r = (e) => vm.runInContext(e, ctx);
console.log(JSON.stringify({
  sinBase: r(`anFugasTendencia({ direccion: 'sin_base' })`),
  porCasos: r(`anFugasTendencia({ direccion: 'sube', delta_pesos: null, delta_casos: 1, anterior: { pesos: 0, casos: 0 } })`),
  porPesos: r(`anFugasTendencia({ direccion: 'sube', delta_pesos: 5000, delta_casos: 1, anterior: { pesos: 1000, casos: 1 } })`),
  pildoras: ['sin_base', 'sin_dato', 'critico'].map(e => r(`anFugasPildora('${e}')`)),
  valorSinDato: r(`anFugasValor({ sin_dato: 'x' })`),
  frescura: r(`anFrescura({ calculado_en: null, fuentes: { a: { nombre: 'Foto <b>', completa: false, motivo: 'no corrió' }, b: { nombre: 'Bitácora', completa: false, motivo: 'empieza hoy' }, c: { nombre: 'WMS', completa: true } } })`),
  frescuraOk: r(`anFrescura({ calculado_en: null, fuentes: { c: { nombre: 'WMS', completa: true } } })`),
}));
"""


class TestFugasPantalla:

    def test_sin_base_y_sin_dato_en_gris(self):
        d = _node(_ARNES_FUGAS)
        assert '--err-' not in d['sinBase'] and 'sin base para comparar' in d['sinBase']
        sin_base, sin_dato, critico = d['pildoras']
        assert '--err-' not in sin_base and '--err-' not in sin_dato
        assert '--err-' in critico and 'Crece' in critico
        assert '--err-' not in d['valorSinDato']

    def test_compara_en_la_misma_moneda(self):
        d = _node(_ARNES_FUGAS)
        assert 'vs. 0 caso(s)' in d['porCasos'] and '$0' not in d['porCasos']
        assert '$5.000' in d['porPesos'] and 'vs. $1.000' in d['porPesos']

    def test_la_frescura_es_una_linea_que_nombra_cada_fuente(self):
        d = _node(_ARNES_FUGAS)
        f = d['frescura']
        assert f.startswith('<details') and '2 fuentes incompletas' in f
        assert 'Foto &lt;b&gt;' in f and 'no corrió' in f and 'empieza hoy' in f
        assert 'WMS' not in f, 'la completa no se nombra'
        assert 'fuentes completas' in d['frescuraOk']


# ─────────────────────────────────────────────────────────────────────────────
# Bitácora: palabras, no códigos; liquidar y bloquear no «deben» un motivo
# ─────────────────────────────────────────────────────────────────────────────

class TestBitacora:

    def test_motivo_legible(self, app):
        from app.services.analitica_salud import motivo_legible
        assert motivo_legible('BLOQUEAR', 'FALTANTE') == 'Faltante en la ubicación'
        assert motivo_legible('BLOQUEAR', 'UBICACION_VACIA') == 'Ubicación vacía'
        assert motivo_legible('BLOQUEAR', 'algo raro') == 'algo raro'
        assert motivo_legible('CANCELAR', 'FALTANTE') == 'FALTANTE', 'un texto escrito va tal cual'
        assert motivo_legible('CANCELAR', None) is None

    def test_la_lista_trae_la_etiqueta_y_si_pide_motivo(self, app, client, db,
                                                          jwt_token_admin, usuario_admin):
        from app.services.bitacora import registrar_accion
        registrar_accion('BLOQUEAR', 'TareaPicking', 1, usuario_id=usuario_admin.id,
                         motivo='FALTANTE')
        registrar_accion('LIQUIDAR', 'RutaDespacho', 2, usuario_id=usuario_admin.id)
        registrar_accion('CANCELAR', 'TareaPicking', 3, usuario_id=usuario_admin.id)
        db.session.commit()
        acc = client.get('/api/analitica/bitacora',
                         headers={'Authorization': f'Bearer {jwt_token_admin}'}).get_json()['acciones']
        por = {a['accion']: a for a in acc}
        assert por['BLOQUEAR']['motivo_legible'] == 'Faltante en la ubicación'
        assert por['LIQUIDAR']['pide_motivo'] is False
        assert por['BLOQUEAR']['pide_motivo'] is False
        assert por['CANCELAR']['pide_motivo'] is True


_ARNES_BIT = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const ctx = { console, window: {}, document: { getElementById: () => ({ innerHTML: '' }) } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/analitica_bitacora.js', 'utf8'), ctx);
const fila = (a) => vm.runInContext('_anBitFila', ctx)(a, 0, false);
const html = vm.runInContext('anBitHtml', ctx)({ filtros: {}, acciones: [], total: 0,
  patrones: { total: 8, sin_motivo: 1, sin_motivo_base: 4, por_persona: [], por_accion: [], por_hora: [] } });
console.log(JSON.stringify({
  liquidar: fila({ accion: 'LIQUIDAR', frase: 'Ana liquidó la ruta R-1', motivo: null, pide_motivo: false }),
  bloquear: fila({ accion: 'BLOQUEAR', frase: 'Ana bloqueó el picking', motivo: 'FALTANTE', motivo_legible: 'Faltante en la ubicación', pide_motivo: false }),
  cancelar: fila({ accion: 'CANCELAR', frase: 'Ana canceló', motivo: null, pide_motivo: true }),
  kpis: html,
}));
"""


class TestBitacoraPantalla:

    def test_sin_codigos_y_sin_motivo_solo_donde_se_pide(self):
        d = _node(_ARNES_BIT)
        assert 'sin motivo' not in d['liquidar']
        assert 'Faltante en la ubicación' in d['bloquear'] and 'FALTANTE' not in d['bloquear']
        assert 'sin motivo' in d['cancelar']

    def test_la_tasa_sin_motivo_usa_la_base_que_pide_motivo(self):
        d = _node(_ARNES_BIT)
        assert '25 % de 4' in d['kpis'], 'uno de cuatro que piden motivo, no uno de ocho'


# ─────────────────────────────────────────────────────────────────────────────
# Tablero BI: sin valor ≠ $0, filas con pedido y cliente
# ─────────────────────────────────────────────────────────────────────────────

class TestVentaPerdidaPorCategoria:

    def test_declara_los_sin_precio_por_categoria(self, app, db, almacen, usuario):
        from tests.test_analitica_fugas import _agotado
        from app.services.metricas.venta_perdida import calcular_venta_perdida
        from app.utils.fecha import dia_operativo
        _agotado(db, almacen, usuario, precio=None, categoria='ESCRITURA')
        _agotado(db, almacen, usuario, precio=1000, categoria='CUADERNOS')
        hoy = dia_operativo()
        r = calcular_venta_perdida(almacen.id, hoy, hoy)
        assert r['por_categoria']['ESCRITURA'] == 0.0
        assert r['sin_precio_por_categoria'] == {'ESCRITURA': 1}


_ARNES_BI = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const els = {};
const el = (id) => (els[id] = els[id] || { id, innerHTML: '', value: '', style: {}, textContent: '' });
const X = '<img src=x onerror=alert(1)>';
const ctx = { console, window: {}, document: { getElementById: el }, ALMACEN_ID: 1,
  Chart: function () { this.destroy = () => {}; } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/tablero_bi.js', 'utf8'), ctx);
ctx.get = async (u) => {
  if (u.includes('pedidos-despachados/detalle')) return { items: [
    { codigo: 'PACK-1', numero_pedido_siesa: 'PD1509', cliente: X, valor_factura: null, total_items: 2, fecha_despachado: null },
    { codigo: 'PACK-2', pedido_clave: '003-PD-1', cliente: '', valor_factura: 5000, total_items: 1, fecha_despachado: null }],
    total: 2, page: 1, per_page: 50 };
  if (u.includes('pedidos-despachados')) return { pedidos: 5, lineas: 9, unidades: 84, valor_total: 0, sin_valor_factura: 5, por_dia: {} };
  if (u.includes('venta-perdida/detalle')) return { items: [], total: 0, page: 1, per_page: 50 };
  if (u.includes('venta-perdida')) return { venta_perdida_total: 36000, por_categoria: { [X]: 0, CUADERNOS: 36000 },
    sin_precio_por_categoria: { [X]: 1 }, sin_precio: { eventos: 1, unidades: 3 }, por_dia: {} };
  return {};
};
(async () => {
  await vm.runInContext(`(async()=>{_BI_SUBTAB='despachados'; await cargarBI();})()`, ctx);
  const desp = els['bi-kpi'].innerHTML + els['bi-detalle-lista'].innerHTML;
  await vm.runInContext(`(async()=>{_BI_SUBTAB='venta_perdida'; await cargarBI();})()`, ctx);
  const vp = els['bi-desglose'].innerHTML;
  const r = (e) => vm.runInContext(e, ctx);
  console.log(JSON.stringify({ desp, vp,
    parcial: r(`biValorDespachado({ pedidos: 5, sin_valor_factura: 2, valor_total: 100000 })`),
    completo: r(`biValorDespachado({ pedidos: 5, sin_valor_factura: 0, valor_total: 100000 })`),
    crudos: ((desp + vp).match(/<img/g) || []).length }));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


class TestTableroBI:

    def test_sin_valor_no_es_cero(self):
        d = _node(_ARNES_BI)
        valor = d['desp'].split('Valor total')[0].rsplit('kpi-valor">', 1)[-1]
        assert valor.startswith('sin valor'), valor
        assert 'sin valor' in d['desp']
        assert d['parcial'] == '$100.000 al menos' and d['completo'] == '$100.000'

    def test_filas_con_pedido_y_cliente(self):
        d = _node(_ARNES_BI)
        assert 'Pedido PD1509' in d['desp'] and 'Pedido 003-PD-1' in d['desp']
        assert 'cliente sin dato' in d['desp']

    def test_categoria_sin_precio_y_escapada(self):
        d = _node(_ARNES_BI)
        assert 'sin precio (1)' in d['vp']
        assert d['crudos'] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Línea de tiempo: números formateados, no «10.0»
# ─────────────────────────────────────────────────────────────────────────────

_ARNES_CIFRAS = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const ctx = { console, window: {}, document: { getElementById: () => ({ innerHTML: '' }) },
  localStorage: { getItem: () => null, setItem(){} } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/analitica.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/analitica_recorrido.js', 'utf8'), ctx);
const pedido = vm.runInContext('anRecPedidoHtml', ctx)({ pedido: { numero: 'PD1', estado: 'EN_CURSO' }, etapas: [],
  acciones_bitacora: 0, eventos: [{ en: null, titulo: 'Siesa aprobó el pedido', detalle: null, tipo: 'marca',
    lineas: ['SIE0da1cbd', '<b>'], cifras: [{ etiqueta: 'líneas', valor: 2, formato: 'num' },
    { etiqueta: 'unidades pedidas', valor: 20.0, formato: 'num' }, { etiqueta: 'valor', valor: 100000.0, formato: 'pesos' },
    { etiqueta: 'cobrado', valor: null, formato: 'pesos' }] }] });
console.log(JSON.stringify({ pedido }));
"""


class TestLineaDeTiempo:

    def test_cifras_formateadas(self):
        p = _node(_ARNES_CIFRAS)['pedido']
        assert 'valor $100.000' in p and 'unidades pedidas 20' in p and 'cobrado sin dato' in p
        assert '20.0' not in p and '100000' not in p
        assert 'SIE0da1cbd' not in p.split('title=')[0], 'los códigos de ítem van en el title'
        assert '&lt;b&gt;' in p
