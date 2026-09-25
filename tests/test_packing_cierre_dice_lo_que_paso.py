"""El cierre de caja dice lo que pasó: encolado no es «Siesa procesó», retenido
por cartera no es «Error Siesa», y con Siesa caído no se factura.

**El caso** (auditoría «operación diaria por rol», 2026-09-25):

- `packing.js` decía «Siesa procesó la factura» tras todo cierre exitoso,
  aunque la respuesta solo hubiera encolado el job (`siesa_triggered: false`).
- Una caja retenida por cartera en el cierre (G2) volvía como 400 con el texto
  de cartera y el reintento la pintaba como «Error Siesa: …»; en la cola de
  pedidos caía en la pestaña ERROR SIESA con su insignia roja.
- Un cierre encolado sin señal y retenido por cartera dejaba al empacador
  frente a «Reintentando automáticamente…» para siempre: la cola offline no
  distinguía un rechazo definitivo de una falla pasajera.

**La clase**: *la pantalla afirma un desenlace que el servidor no afirmó*.
Una función decide el texto (`empMensajeCierre`), el servidor declara
`estado_siesa` y `estado_cierre`, y un trinquete prohíbe que el PWA vuelva a
escribir «procesó» por su cuenta.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'


# ═════════════════════════════════════════════════════════════════════════════
# 1 · El servidor declara el desenlace
# ═════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def empacador(db, almacen):
    from app.models.usuario import Usuario
    u = Usuario(nombre='Emp', email='emp-cierre@t.co', rol='empacador',
                password_hash='x', almacen_id=almacen.id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


@pytest.fixture
def token_emp(app, empacador):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return create_access_token(identity=str(empacador.id))


@pytest.fixture
def tarea(db, almacen):
    from app.models.packing import TareaPacking
    t = TareaPacking(codigo='PK-DICE', estado='VERIFICADO', almacen_id=almacen.id,
                     numero_pedido_siesa='PD77', tipo_documento='PEDIDO')
    db.session.add(t)
    db.session.commit()
    return t


class _Closer:
    def __init__(self, resultado):
        self.resultado = resultado

    def ejecutar_cierre(self, *a, **k):
        return self.resultado


def _con_closer(resultado):
    return patch('app.services.closing.factory.PackingCloserFactory.get',
                 return_value=_Closer(resultado))


def _cerrar(client, token, tarea_id):
    return client.post(f'/api/packing/{tarea_id}/cerrar',
                       json={'bultos': [{'tipo': 'Caja', 'cantidad': 1}]},
                       headers={'Authorization': f'Bearer {token}'})


class TestElServidorDeclaraElDesenlace:

    def test_retenido_por_cartera_es_409_con_su_estado(self, client, token_emp, tarea):
        from app.services.closing.base import CierreResult, RETENIDO_CARTERA
        with _con_closer(CierreResult(exitoso=False, mensaje='x', estado=RETENIDO_CARTERA,
                                      error='Retenido por cartera: mora de 40 días.',
                                      retencion_id=9)):
            r = _cerrar(client, token_emp, tarea.id)
        assert r.status_code == 409
        d = r.get_json()
        assert d['estado_cierre'] == 'RETENIDO_CARTERA' and d['retenido_por_cartera'] is True
        assert d['retencion_id'] == 9 and 'mora' in d['error']

    def test_siesa_caido_es_503(self, client, token_emp, tarea):
        from app.services.closing.base import CierreResult, SIESA_NO_DISPONIBLE
        with _con_closer(CierreResult(exitoso=False, mensaje='x', estado=SIESA_NO_DISPONIBLE,
                                      error='Siesa no está disponible: no se puede facturar.')):
            r = _cerrar(client, token_emp, tarea.id)
        assert r.status_code == 503
        assert r.get_json()['estado_cierre'] == 'SIESA_NO_DISPONIBLE'

    def test_un_error_del_empaque_sigue_siendo_400(self, client, token_emp, tarea):
        from app.services.closing.base import CierreResult
        with _con_closer(CierreResult(exitoso=False, mensaje='x', error='El packing debe estar VERIFICADO')):
            r = _cerrar(client, token_emp, tarea.id)
        assert r.status_code == 400 and 'estado_cierre' not in r.get_json()

    def test_encolado_no_se_declara_confirmado(self, db, tarea):
        from app.services.closing.base import CierreResult
        from app.services.packing_service import PackingService
        with _con_closer(CierreResult(exitoso=True, mensaje='encolado')):
            d = PackingService.cerrar_packing_resultado(tarea.id, [], 0)
        assert d['estado_siesa'] == 'EN_COLA'
        assert 'procesó' not in d['mensaje'] and 'confirmó' not in d['mensaje']

    def test_el_precheck_sin_siesa_se_marca_no_disponible(self, db, almacen, monkeypatch):
        from app.models.packing import TareaPacking
        from app.services.closing.base import MotivoSiesaNoDisponible
        from app.services.closing.pedido_closer import PedidoPackingCloser
        from app.services.connekta_gateway import connekta
        t = TareaPacking(codigo='PK-CAIDO', estado='VERIFICADO', almacen_id=almacen.id,
                         numero_pedido_siesa='PD78', tipo_docto_pedido_siesa='PD',
                         consec_docto_pedido_siesa='78')
        db.session.add(t)
        db.session.commit()
        monkeypatch.setattr(connekta, 'modo_simulacion', False)

        def _boom(*a, **k):
            raise Exception('Connekta no respondió')
        monkeypatch.setattr(connekta, 'get_estado_pedido', _boom)
        monkeypatch.setattr(connekta, 'get_factura_desde_pedido', _boom)
        err = PedidoPackingCloser()._precheck_siesa(t)
        assert isinstance(err, MotivoSiesaNoDisponible)
        assert err.startswith('Siesa no está disponible: no se puede facturar')
        res = PedidoPackingCloser().ejecutar_cierre(t.id, [{'tipo': 'Caja', 'cantidad': 1}], 0)
        assert res.estado == 'SIESA_NO_DISPONIBLE'

    def test_la_cola_offline_saca_el_retenido_y_deja_siesa_caido(self, client, token_emp, tarea):
        from app.services.closing.base import CierreResult, RETENIDO_CARTERA, SIESA_NO_DISPONIBLE
        cola = [{'accion': 'cerrar_packing', 'tarea_id': tarea.id, '_qid': 'q1',
                 'bultos': [{'tipo': 'Caja', 'cantidad': 1}]}]
        h = {'Authorization': f'Bearer {token_emp}'}
        with _con_closer(CierreResult(exitoso=False, mensaje='x', estado=RETENIDO_CARTERA,
                                      error='Retenido por cartera: sin cupo.')):
            e = client.post('/api/mobile/sync', json={'cola': cola}, headers=h).get_json()['resultados'][0]
        assert e['definitivo'] is True and e['estado_cierre'] == 'RETENIDO_CARTERA'
        with _con_closer(CierreResult(exitoso=False, mensaje='x', estado=SIESA_NO_DISPONIBLE,
                                      error='Siesa no está disponible.')):
            e = client.post('/api/mobile/sync', json={'cola': cola}, headers=h).get_json()['resultados'][0]
        assert e['definitivo'] is False and e['estado_cierre'] == 'SIESA_NO_DISPONIBLE'


# ═════════════════════════════════════════════════════════════════════════════
# 2 · La pantalla, ejecutada
# ═════════════════════════════════════════════════════════════════════════════

_ARNES = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const G = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const nodo = () => ({ style: {}, classList: { add() {}, remove() {} }, remove() {}, innerHTML: '',
                      querySelector: () => null, appendChild() {} });
const guardado = {};
const ctx = {
  console,
  document: { getElementById: () => nodo(), querySelector: () => null, querySelectorAll: () => [],
              addEventListener() {}, createElement: () => nodo(), body: { appendChild() {} } },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: (k) => guardado[k] ?? null, setItem: (k, v) => { guardado[k] = v; },
                  removeItem: (k) => { delete guardado[k]; } },
};
ctx.globalThis = ctx; ctx.window = ctx;
ctx.location = { origin: 'http://t' }; ctx.addEventListener = () => {};
vm.createContext(ctx);
for (const a of ['util.js', 'app.js', 'packing.js']) vm.runInContext(fs.readFileSync(PWA + '/' + a, 'utf-8'), ctx, { filename: a });
const out = { mensajes: [], grupos: [], alertas: [], cb: [] };
Object.assign(ctx, { __G: G, __out: out });
for (const c of G.mensajes) out.mensajes.push(ctx.empMensajeCierre(c.data, c.status));
for (const p of G.pedidos) out.grupos.push(ctx.pedidoGrupo(p));
if (G.sync) {
  vm.runInContext(`
    alerta = (m, t) => __out.alertas.push([String(m), t]);
    post = async () => __G.sync.respuesta;
    COLA_OFFLINE = __G.sync.cola;
    onSyncRechazo_cerrar_packing = (x) => __out.cb.push(['rechazo', x._qid]);
    onSyncPendiente_cerrar_packing = (x) => __out.cb.push(['pendiente', x._qid]);
  `, ctx);
  await ctx.syncOffline();
  out.cola = vm.runInContext('COLA_OFFLINE.map(x => x._qid)', ctx);
}
process.stdout.write(JSON.stringify(out));
"""


def _pantalla(tmp_path, mensajes=(), pedidos=(), sync=None):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    h = tmp_path / 'h.mjs'
    h.write_text(_ARNES, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'mensajes': list(mensajes), 'pedidos': list(pedidos), 'sync': sync}),
                 encoding='utf-8')
    p = subprocess.run(['node', str(h), str(PWA), str(g)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


class TestLaPantallaNoAfirmaDeMas:

    def test_los_cuatro_desenlaces(self, tmp_path):
        t = _pantalla(tmp_path, mensajes=[
            {'data': {'bultos': [1, 2], 'estado_siesa': 'EN_COLA'}, 'status': 200},
            {'data': {'bultos': [1], 'estado_siesa': 'CONFIRMADO'}, 'status': 200},
            {'data': {'error': 'Retenido por cartera: mora de 40 días.', 'estado_cierre': 'RETENIDO_CARTERA'}, 'status': 409},
            {'data': {'error': 'Siesa no está disponible: no se puede facturar. Timeout.', 'estado_cierre': 'SIESA_NO_DISPONIBLE'}, 'status': 503},
        ])
        cola, conf, ret, caido = t['mensajes']
        assert 'en cola' in cola['texto'] and 'procesó' not in cola['texto'] and 'confirmó' not in cola['texto']
        assert 'confirmó' in conf['texto']
        assert ret['texto'].startswith('Retenido por cartera: mora de 40 días.')
        assert ret['tipo'] == 'advertencia' and 'Siesa' not in ret['texto']
        assert caido['texto'].startswith('Siesa no está disponible: no se puede facturar')
        assert caido['tipo'] == 'error'

    def test_una_respuesta_vieja_sin_estado_siesa_no_dice_procesado(self, tmp_path):
        t = _pantalla(tmp_path, mensajes=[{'data': {'bultos': [1]}, 'status': 200}])
        assert 'procesó' not in t['mensajes'][0]['texto']

    def test_retenido_por_cartera_no_cae_en_error_siesa(self, tmp_path):
        t = _pantalla(tmp_path, pedidos=[
            {'packing_estado': 'VERIFICADO', 'retencion_cartera': {'resumen': 'mora'}},
            {'packing_estado': 'VERIFICADO', 'cartera_liberada': True},
            {'packing_estado': 'VERIFICADO'},
            {'siesa_triggered': True, 'retencion_cartera': {'resumen': 'vieja'}},
        ])
        assert t['grupos'] == [4, 4, 3, 2]

    def test_la_cola_offline_saca_lo_definitivo_y_avisa_a_su_dueno(self, tmp_path):
        t = _pantalla(tmp_path, sync={
            'cola': [{'_qid': 'a', 'accion': 'cerrar_packing'}, {'_qid': 'b', 'accion': 'cerrar_packing'}],
            'respuesta': {'sincronizados': 0, 'resultados': [
                {'_qid': 'a', 'accion': 'cerrar_packing', 'exito': False, 'definitivo': True,
                 'estado_cierre': 'RETENIDO_CARTERA', 'error': 'Retenido por cartera: x'},
                {'_qid': 'b', 'accion': 'cerrar_packing', 'exito': False, 'definitivo': False,
                 'estado_cierre': 'SIESA_NO_DISPONIBLE', 'error': 'Siesa no está disponible'},
            ]}})
        assert t['cola'] == ['b'], 'lo rechazado para siempre siguió en la cola'
        assert ['rechazo', 'a'] in t['cb'] and ['pendiente', 'b'] in t['cb']


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Trinquete: ninguna cadena del PWA afirma que Siesa procesó
# ═════════════════════════════════════════════════════════════════════════════

def _sin_comentarios(src):
    from tests.test_frontend_integrity import _sin_comentarios as f
    return f(src)


AFIRMA_PROCESADO = re.compile(r'Siesa\s+proces[óo]|Error\s+Siesa:\s*\$\{')


def afirmaciones(fuente: str):
    """Sitios donde el PWA afirma, en una cadena, que Siesa procesó (o pinta
    cualquier falla como «Error Siesa: ${…}»). Comentarios no cuentan."""
    return [m.group(0) for m in AFIRMA_PROCESADO.finditer(_sin_comentarios(fuente))]


class TestTrinqueteNadieAfirmaProcesado:

    def test_ningun_modulo_lo_afirma(self):
        sitios = {f.name: afirmaciones(f.read_text(encoding='utf-8'))
                  for f in sorted(PWA.glob('*.js'))}
        sitios = {k: v for k, v in sitios.items() if v}
        assert not sitios, (
            f'El PWA vuelve a afirmar un desenlace de Siesa por su cuenta: {sitios}. '
            f'El texto lo decide `empMensajeCierre` con lo que el servidor declara.')

    def test_meta_ve_las_formas(self):
        assert afirmaciones("alerta(`${n} piezas — Siesa procesó la factura`)")
        assert afirmaciones("alerta(`Error Siesa: ${e.message}`)")

    def test_meta_no_marca_comentarios_ni_lo_sano(self):
        assert afirmaciones('// Siesa procesó la factura\n/* Siesa procesó */\n'
                            "const x = 'quedó en cola para Siesa';") == []

    def test_piso(self):
        assert len(list(PWA.glob('*.js'))) >= 20
