"""El bloque «Retenidos por cartera» del tablero (`cartera.js`), en Node con
`util.js` real: escapa lo que pinta, en los `onclick` solo viajan posiciones,
no ofrece decidir sin el permiso que el servidor confirma, ni a quien inició
el pedido, y se esconde ante un 403."""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0]; const respuesta = JSON.parse(args[1]);
const els = {};
const el = (id) => (els[id] = els[id] || { id, style: {}, innerHTML: '' });
const ctx = { console, document: { getElementById: el },
  get: (url) => respuesta.status ? Promise.reject(Object.assign(new Error('x'),
                                   { status: respuesta.status }))
                                 : Promise.resolve(respuesta.cuerpo),
  post: () => Promise.resolve({}), alerta() {} };
el('cartera-bloque');
vm.createContext(ctx);
for (const f of ['util.js', 'cartera.js'])
  vm.runInContext(fs.readFileSync(base + '/' + f, 'utf8'), ctx);
(async () => {
  await vm.runInContext('carteraCargarBloque()', ctx);
  console.log(JSON.stringify({ html: els['cartera-bloque'].innerHTML,
                               display: els['cartera-bloque'].style.display }));
})();
"""

MALO = '<img src=x onerror=alert(1)>'


def _render(respuesta):
    if not shutil.which('node'):
        pytest.skip('sin node')
    r = subprocess.run(['node', '-e', _ARNES, '--', str(RAIZ / 'app' / 'static' / 'pwa'),
                        json.dumps(respuesta)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _ret(**k):
    base = {'id': 7, 'pedido': 'PD1', 'cliente': MALO, 'nit': '900', 'valor': 1000,
            'cond_pago': 'C04', 'compuerta': 'G1', 'antiguedad_horas': 80,
            'motivos': [{'codigo': 'MORA', 'texto': MALO, 'retiene': True}],
            'iniciado_por': {'id': 5},
            # Lo que el servidor permite hacer con ella (`retencion_publica`).
            'acciones': ['autorizar', 'convertir_contado', 'reevaluar']}
    base.update(k)
    return base


def test_escapa_y_en_el_onclick_solo_viaja_la_posicion():
    d = _render({'cuerpo': {'retenciones': [_ret()], 'puede_autorizar': True,
                            'usuario_id': 1}})
    assert '<img' not in d['html'] and '&lt;img' in d['html']
    assert 'carteraAutorizar(0)' in d['html'] and 'carteraConvertir(0)' in d['html']
    assert 'con más de 3 días' in d['html']


def test_sin_el_permiso_no_hay_botones_de_decidir():
    d = _render({'cuerpo': {'retenciones': [_ret()], 'puede_autorizar': False,
                            'usuario_id': 1}})
    assert 'carteraAutorizar' not in d['html'] and 'carteraReevaluar(0)' in d['html']
    assert 'carteraLoteVer' not in d['html']


def test_quien_inicio_el_pedido_no_ve_el_boton_de_autorizarlo():
    d = _render({'cuerpo': {'retenciones': [_ret()], 'puede_autorizar': True,
                            'usuario_id': 5}})
    assert 'carteraAutorizar' not in d['html'] and 'Usted lo inició' in d['html']


def test_con_acuerdo_vigente_solo_se_ofrece_contado():
    """Decisión del dueño: acuerdo de pago vigente → solo contado. El panel
    no ofrece «Autorizar crédito» (el servidor tampoco lo acepta) y lo dice."""
    d = _render({'cuerpo': {'retenciones': [_ret(
        motivos=[{'codigo': 'ACUERDO_VIGENTE', 'texto': 'acuerdo', 'retiene': True}],
        acciones=['convertir_contado', 'reevaluar'])], 'puede_autorizar': True,
        'usuario_id': 1}})
    assert 'carteraAutorizar' not in d['html'] and 'carteraConvertir(0)' in d['html']
    assert 'solo puede salir de contado' in d['html']


def test_el_lote_solo_con_su_permiso():
    d = _render({'cuerpo': {'retenciones': [], 'puede_autorizar': False,
                            'puede_autorizar_lote': True, 'usuario_id': 1}})
    assert 'carteraLoteVer()' in d['html'] and 'Ningún pedido retenido' in d['html']


def test_un_403_esconde_el_bloque():
    assert _render({'status': 403})['display'] == 'none'


def test_la_compuerta_se_dice_en_palabras():
    """E2E 2026-09-25: el bloque pintaba «G1» / «EMISION». El texto viene del
    servidor (`Compuerta.PALABRAS`); un servidor viejo sin el campo dice
    «retenido», nunca el código."""
    d = _render({'cuerpo': {'retenciones': [_ret(compuerta='EMISION', compuerta_texto='retenido al facturar'),
                                            _ret(id=8, compuerta='G1')],
                            'puede_autorizar': False, 'usuario_id': 1}})
    assert 'retenido al facturar' in d['html']
    assert 'EMISION' not in d['html'] and '· G1' not in d['html']


def test_el_servidor_publica_la_compuerta_en_palabras():
    from app.models.cartera import Compuerta
    assert set(Compuerta.PALABRAS) == set(Compuerta.TODAS)
