"""
Re-validación de compras A (2026-09-29): lo que faltaba para que la lectura
completa de Siesa se use bien en todos los procesos y en todas las pantallas.

1. **P1-R1 · la memoria no tapa una lectura más nueva de la base.** En
   producción solo el worker lee Siesa; la web se quedaba con la completa que
   cargó al arrancar (`test_val_compras_ac_20260929.py`). Acá, la otra puerta
   de la misma clase: la pantalla de Pedir (traslados y tienda).
2. **El reintento de las 06:00** — solo si hoy no hay una lectura completa.
3. **`CARGA_FISICA_AUTOMATICA` y el ensayo sin escribir** — cuántas ubicaciones
   pondría en cero la carga, por bodega, con unidades.
4. **El cero automático es solo del espejo (SIESA-GENERAL).** Un hueco real
   con stock que Siesa da en cero no se pisa: va a la reconciliación.
5. **El tablero de movimientos** muestra el cero de la carga como salida con
   su saldo anterior, no «+0» verde (Node, con `util.js` real).
"""
import json
import pathlib
import shutil
import subprocess
from datetime import datetime, time, timedelta
from unittest.mock import patch

import pytest

from app.services import inventario_siesa_service as iss
from tests.test_existencias_verdaderas import (SiesaExistencias, cache_limpio,  # noqa: F401
                                               refrescar, universo)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'
MALO = '<img src=x onerror=alert(1)>'


def _correr_registro(dias):
    """Mueve la última lectura completa de la base `dias` hacia atrás."""
    from app.extensions import db
    from app.models.registro_sync import RegistroSync
    hace = datetime.utcnow() - timedelta(days=dias)
    RegistroSync.query.filter_by(tipo='existencias_siesa').update({'inicio': hace, 'fin': hace})
    db.session.commit()
    return hace


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La pantalla de Pedir ve la lectura más nueva de la base
# ═════════════════════════════════════════════════════════════════════════════

class TestLaPantallaDePedirVeLaUltimaCompleta:

    def test_obtener_stock_bodega_se_renueva_con_la_de_la_base(self, db, cache_limpio):
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))         # el worker, hoy
        from app.services import registro_sync_service as reg
        fin = reg.ultimo_ok('existencias_siesa')['fin']
        # La web tiene en memoria la de ayer, con otro contenido.
        cache_limpio.update(ts=cache_limpio['ts'] - timedelta(days=1),
                            data={'NB1': {'VIEJO': {'existencia': 9}}})
        inv, meta = iss.obtener_stock_bodega('NB1')
        assert 'VIEJO' not in inv and len(inv) == 80
        assert meta == {'fuente': 'siesa', 'actualizado_en': fin}

    def test_sin_lectura_completa_no_dice_siesa(self, db, cache_limpio):
        cache_limpio.update(data={'NB1': {'X': {'existencia': 1}}}, completa=False,
                            degradado=True, ts=datetime.utcnow())
        _, meta = iss.obtener_stock_bodega('NB1')
        assert meta['fuente'] != 'siesa'

    def test_la_cache_de_traslados_se_renueva_al_cambiar_la_lectura(self, db, cache_limpio,
                                                                     monkeypatch):
        from app.services.traslado_service import TrasladoService
        monkeypatch.setattr('app.services.traslado_service.connekta.modo_simulacion', False)
        TrasladoService.invalidar_cache_stock()
        vistas = []
        monkeypatch.setattr(iss, 'obtener_stock_bodega',
                            lambda bod, forzar=False: (vistas.append(bod) or {},
                                                       {'fuente': 'siesa', 'actualizado_en': None}))
        # Vacía para no depender de productos: se usa el fallback, pero la
        # pregunta es si la caché de 5 min se consulta o se renueva.
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))
        marca = TrasladoService._marca_lectura_siesa()
        assert marca
        TrasladoService._stock_siesa_cache['NB1'] = {'data': {'items': ['de ayer']},
                                                     'ts': __import__('time').time(),
                                                     'lectura': marca}
        assert TrasladoService.get_stock_disponible('NB1') == {'items': ['de ayer']}
        assert vistas == []
        # El worker guarda una completa nueva: la caché no la tapa.
        TrasladoService._stock_siesa_cache['NB1']['lectura'] = 'otra-lectura'
        TrasladoService.get_stock_disponible('NB1')
        assert vistas == ['NB1']
        TrasladoService.invalidar_cache_stock()


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El reintento de las 06:00
# ═════════════════════════════════════════════════════════════════════════════

class TestElReintento:

    def test_la_hora(self, monkeypatch):
        monkeypatch.delenv('INV_SIESA_REINTENTO', raising=False)
        assert iss.hora_de_reintento() == time(6, 0)
        monkeypatch.setenv('INV_SIESA_REINTENTO', '05:45')
        assert iss.hora_de_reintento() == time(5, 45)
        monkeypatch.setenv('INV_SIESA_REINTENTO', '')
        assert iss.hora_de_reintento() is None
        monkeypatch.setenv('INV_SIESA_REINTENTO', 'a las seis')
        assert iss.hora_de_reintento() is None

    def test_con_la_completa_de_hoy_no_lee(self, app, db, cache_limpio, monkeypatch):
        leidas = []
        monkeypatch.setattr(iss, '_ejecutar_lectura_programada', lambda a: leidas.append(1))
        refrescar(SiesaExistencias(universo()))
        assert iss.hay_completa_de_hoy()
        iss._ejecutar_reintento(app)
        assert leidas == []

    def test_sin_lectura_completa_lee(self, app, db, cache_limpio, monkeypatch):
        leidas = []
        monkeypatch.setattr(iss, '_ejecutar_lectura_programada', lambda a: leidas.append(1))
        iss._ejecutar_reintento(app)
        assert leidas == [1]

    def test_con_la_completa_de_ayer_lee(self, app, db, cache_limpio, monkeypatch):
        """La de las 04:30 salió incompleta: la última completa es de antes."""
        leidas = []
        monkeypatch.setattr(iss, '_ejecutar_lectura_programada', lambda a: leidas.append(1))
        refrescar(SiesaExistencias(universo()))
        hace = _correr_registro(2)
        cache_limpio['ts'] = hace
        assert not iss.hay_completa_de_hoy()
        iss._ejecutar_reintento(app)
        assert leidas == [1]


# ═════════════════════════════════════════════════════════════════════════════
# 3 · El interruptor y el ensayo sin escribir
# ═════════════════════════════════════════════════════════════════════════════

def _mundo(db, almacen):
    """NB1 con: el espejo (SIESA-GENERAL) con un SKU que Siesa no reporta (12),
    un hueco PICKING con otro SKU que Siesa no reporta (7), una zona de averías
    (3) y 80 SKU que Siesa sí reporta."""
    from app.models.inventario import UbicacionProducto
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    gen = Ubicacion(codigo=Ubicacion.CODIGO_GENERAL, almacen_id=almacen.id, zona='GENERAL',
                    activo=True)
    pik = Ubicacion(codigo='PIK-01', almacen_id=almacen.id, zona='PICKING',
                    tipo_zona='PICKING', activo=True)
    ave = Ubicacion(codigo='AVE-01', almacen_id=almacen.id, zona='AVERIAS',
                    tipo_zona='AVERIAS', activo=True)
    fantasma = Producto(codigo='FANT', nombre='fantasma', codigo_siesa='FANT', activo=True)
    hueco = Producto(codigo='HUECO', nombre='hueco', codigo_siesa='HUECO', activo=True)
    roto = Producto(codigo='ROTO', nombre='roto', codigo_siesa='ROTO', activo=True)
    db.session.add_all([gen, pik, ave, fantasma, hueco, roto])
    for i in range(80):
        db.session.add(Producto(codigo=f'SKU{i:03d}', nombre='x', codigo_siesa=f'SKU{i:03d}',
                                activo=True))
    db.session.flush()
    db.session.add_all([
        UbicacionProducto(ubicacion_id=gen.id, producto_id=fantasma.id, cantidad=12),
        UbicacionProducto(ubicacion_id=pik.id, producto_id=hueco.id, cantidad=7),
        UbicacionProducto(ubicacion_id=ave.id, producto_id=roto.id, cantidad=3),
    ])
    db.session.commit()
    return gen, pik, ave, fantasma, hueco, roto


class TestElEnsayoNoEscribe:

    def test_el_interruptor(self, monkeypatch):
        monkeypatch.delenv('CARGA_FISICA_AUTOMATICA', raising=False)
        assert iss.carga_fisica_automatica() is True
        monkeypatch.setenv('CARGA_FISICA_AUTOMATICA', 'false')
        assert iss.carga_fisica_automatica() is False
        monkeypatch.setenv('CARGA_FISICA_AUTOMATICA', ' TRUE ')
        assert iss.carga_fisica_automatica() is True

    def test_apagada_la_carga_de_las_7_no_corre(self, app, db, monkeypatch):
        corrio = []
        monkeypatch.setattr(iss, '_run_carga_inicial', lambda *a, **k: corrio.append(a))
        monkeypatch.setattr(iss, 'motivo_carga_bloqueada', lambda bod: '')
        monkeypatch.setattr(iss, '_avisar_cargas_no_escritas', lambda filas: None)
        monkeypatch.setenv('CARGA_FISICA_AUTOMATICA', 'true')
        iss._ejecutar_carga_fisica_diaria(app)
        assert corrio, 'encendida, la carga corre (si no, el test de apagada no prueba nada)'
        corrio.clear()
        monkeypatch.setenv('CARGA_FISICA_AUTOMATICA', 'false')
        iss._ejecutar_carga_fisica_diaria(app)
        assert corrio == []

    def test_dice_cuanto_pondria_en_cero_sin_tocar_nada(self, db, almacen, cache_limpio):
        from app.models.inventario import MovimientoInventario, UbicacionProducto
        gen, pik, ave, fantasma, hueco, roto = _mundo(db, almacen)
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))
            e = iss.ensayo_carga_fisica('NB1')
        assert e['escribiria'] is True and e['motivo'] is None
        assert (e['a_cero'], e['unidades_a_cero']) == (1, 12.0)
        assert (e['a_reconciliar'], e['unidades_a_reconciliar']) == (1, 7.0)
        assert e['lectura_de_siesa']['completa']
        db.session.expire_all()
        assert UbicacionProducto.query.filter_by(ubicacion_id=gen.id).one().cantidad == 12
        assert MovimientoInventario.query.count() == 0

    def test_sin_lectura_completa_lo_dice(self, db, almacen, cache_limpio):
        e = iss.ensayo_carga_fisica(almacen.bodega_siesa_id)
        assert 'ninguna lectura completa' in e['motivo'] and 'a_cero' not in e

    def test_se_ve_en_el_panel_de_admin(self, app, client, db, almacen, cache_limpio):
        from tests.test_cartera_retencion import _jwt, _usuario
        _mundo(db, almacen)
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))
            r = client.get('/api/siesa/carga-fisica',
                           headers=_jwt(app, _usuario(db, rol='admin')))
        assert r.status_code == 200
        d = r.get_json()
        assert d['automatica'] is True
        nb1 = next(b for b in d['bodegas'] if b['bodega'] == 'NB1')
        assert nb1['ensayo']['a_cero'] == 1 and nb1['ensayo']['a_reconciliar'] == 1

    def test_el_panel_pinta_el_ensayo(self):
        js = (PWA / 'app.js').read_text(encoding='utf-8')
        assert 'b.ensayo' in js and 'unidades_a_cero' in js and 'd.automatica' in js


# ═════════════════════════════════════════════════════════════════════════════
# 4 · El cero automático es solo del espejo
# ═════════════════════════════════════════════════════════════════════════════

class TestElCeroEsSoloDelEspejo:

    def test_la_carga_no_pisa_el_hueco_real_y_lo_declara(self, app, db, almacen, cache_limpio):
        from app.models.inventario import UbicacionProducto
        from app.services import registro_sync_service as reg
        gen, pik, ave, fantasma, hueco, roto = _mundo(db, almacen)
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))
            iss._run_carga_inicial(app, 'NB1')
        db.session.expire_all()
        cant = {(r.ubicacion_id, r.producto_id): r.cantidad for r in UbicacionProducto.query}
        assert cant[(gen.id, fantasma.id)] == 0, 'el espejo sí se pone en cero'
        assert cant[(pik.id, hueco.id)] == 7, 'el hueco real no se pisa'
        assert cant[(ave.id, roto.id)] == 3, 'las averías no las administra el sync'
        res = reg.ultimo('stock')['resultado']
        assert res['huecos_a_reconciliar'] == 1 and res['puestos_en_cero'] == 1

    def test_el_hueco_aparece_en_la_reconciliacion(self, app, db, almacen, cache_limpio):
        gen, pik, ave, fantasma, hueco, roto = _mundo(db, almacen)
        with patch.object(iss.connekta, 'bodega', 'NB1'):
            refrescar(SiesaExistencias(universo()))
            iss._run_carga_inicial(app, 'NB1')
            r = iss._calcular_reconciliacion(iss._descargar_inventario_multibodega_para_reconciliar())
        d = [x for x in r['discrepancias'] if x['producto_id'] == hueco.id]
        assert d and d[0]['estado'] == 'SOLO_WMS' and d[0]['stock_wms'] == 7

    def test_el_plan_separa_espejo_y_huecos(self, db, almacen):
        gen, pik, ave, fantasma, hueco, roto = _mundo(db, almacen)
        plan = iss.plan_de_ceros(almacen.id, gen.id, {})
        assert [r.producto_id for r in plan['a_cero']] == [fantasma.id]
        assert [r.producto_id for r in plan['a_reconciliar']] == [hueco.id]
        # Lo que Siesa reporta con existencia no va a ninguna de las dos.
        plan = iss.plan_de_ceros(almacen.id, gen.id,
                                 {'FANT': {'existencia': 1}, 'HUECO': {'existencia': 2}})
        assert plan == {'a_cero': [], 'a_reconciliar': [],
                        'unidades_a_cero': 0.0, 'unidades_a_reconciliar': 0.0}
        # Las exclusiones (operación activa, ajuste reciente) se respetan.
        assert iss.plan_de_ceros(almacen.id, gen.id, {}, {fantasma.id})['a_cero'] == []
        assert iss.plan_de_ceros(almacen.id, gen.id, {}, (), {gen.id})['a_cero'] == []


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Pantallas (Node, con util.js real)
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0]; const r = JSON.parse(args[1]);
function extraer(archivo, nombre) {
  const src = fs.readFileSync(base + '/' + archivo, 'utf8');
  const i = src.indexOf('function ' + nombre + '(');
  if (i < 0) throw new Error('no está ' + nombre);
  let j = src.indexOf('{', src.indexOf(')', i)), n = 0;
  for (let k = j; k < src.length; k++) {
    if (src[k] === '{') n++;
    else if (src[k] === '}' && --n === 0) return src.slice(i, k + 1);
  }
}
const els = {};
const ctx = { console, document: { getElementById: (id) => (els[id] = els[id] || { innerHTML: '' }) } };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(extraer('app.js', 'movimientoMostrado'), ctx);
vm.runInContext(extraer('app.js', 'movimientos'), ctx);
vm.runInContext(extraer('traslados.js', 'frescuraStock'), ctx);
vm.runInContext(extraer('traslados.js', 'frescuraStockHtml'), ctx);
ctx.movimientos(r.movs);
const out = { html: els['movimientos-recientes'].innerHTML,
  mostrados: r.movs.map(m => ctx.movimientoMostrado(m, new Set(['ENTRADA', 'CARGA_INICIAL_SIESA']))),
  frescura: r.metas.map(m => ctx.frescuraStock(m, r.ahora)),
  frescuraHtml: r.metas.map(m => ctx.frescuraStockHtml(m)) };
console.log(JSON.stringify(out));
"""


def _node(movs=(), metas=(), ahora=None):
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = {'movs': list(movs), 'metas': list(metas), 'ahora': ahora}
    p = subprocess.run(['node', '-e', _ARNES, '--', str(PWA), json.dumps(r)],
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _mov(**kw):
    base = {'tipo': 'CARGA_INICIAL_SIESA', 'cantidad': 0, 'fecha': '2026-09-29T12:00:00',
            'numero_documento': 'CARGA-SIESA'}
    base.update(kw)
    return base


class TestElTableroDeMovimientos:

    def test_el_cero_de_la_carga_es_una_salida_con_su_saldo(self):
        d = _node([_mov(saldo_antes=12, saldo_despues=0)])
        m = d['mostrados'][0]
        assert (m['signo'], m['cantidad']) == ('-', 12) and m['color'] == 'var(--err-tx)'
        assert '12 → 0' in m['detalle']
        assert '+0' not in d['html'] and '-12' in d['html']

    def test_una_carga_que_sube_es_entrada(self):
        m = _node([_mov(cantidad=5, saldo_antes=3, saldo_despues=8)])['mostrados'][0]
        assert (m['signo'], m['cantidad']) == ('+', 5)

    def test_sin_saldos_la_regla_de_siempre(self):
        d = _node([_mov(tipo='ENTRADA', cantidad=4), _mov(tipo='SALIDA', cantidad=2)])
        assert [(m['signo'], m['cantidad']) for m in d['mostrados']] == [('+', 4), ('-', 2)]

    def test_escapa(self):
        d = _node([_mov(tipo=MALO, numero_documento=MALO, saldo_antes=1, saldo_despues=0)])
        assert '<img' not in d['html']


class TestLaFechaDeLaLecturaEnPedir:

    AHORA = 1790000000000          # 2026-09-21T13:33:20Z

    def test_dice_de_cuando_es_con_la_hora_de_bogota(self):
        f = _node(metas=[{'fuente': 'siesa', 'actualizado_en': '2026-09-21T09:31:00'}],
                  ahora=self.AHORA)['frescura'][0]
        assert f['antiguo'] is False
        assert '04:31' in f['tiempo'] and '21' in f['tiempo'], f

    def test_mas_de_21_horas_o_sin_completa_se_avisa(self):
        d = _node(metas=[{'fuente': 'siesa', 'actualizado_en': '2026-09-20T09:31:00'},
                         {'fuente': 'siesa_bd_snapshot', 'actualizado_en': '2026-09-21T09:31:00'},
                         {'fuente': 'siesa', 'actualizado_en': None}],
                  ahora=self.AHORA)
        assert [f['antiguo'] for f in d['frescura']] == [True, True, True]

    def test_escapa_y_sin_fuente_no_pinta(self):
        d = _node(metas=[{'fuente': MALO, 'actualizado_en': None}, {'fuente': None}])
        assert '<img' not in d['frescuraHtml'][0] and d['frescuraHtml'][1] == ''

    def test_las_dos_pantallas_usan_la_misma_funcion(self):
        tienda = (PWA / 'tienda.js').read_text(encoding='utf-8')
        trasl = (PWA / 'traslados.js').read_text(encoding='utf-8')
        assert 'frescuraStockHtml(_TIENDA_STOCK_META)' in tienda
        assert 'frescuraStockHtml(_AP_STOCK_META)' in trasl
        assert 'minutos > 15' not in tienda
