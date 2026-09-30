"""El modal ✏ de Conteos reasigna: el botón decía «Reasignar operario /
corregir conteo» y el modal solo tenía la cantidad.

## El defecto (visto en QA el 2026-09-30)

Un 2º conteo (PAPELSP7879) quedó en la cola sin dueño. El supervisor abrió ✏
para dárselo a Operario 2 y el modal solo ofrecía «Cantidad física corregida»,
que en un conteo sin contar no aplica. El servidor sí aceptaba `operario_id`
en `PUT /api/conteo/<id>/editar` (`ConteoService.reasignar_operario`: puesto,
presencia y doble ciego); la pantalla no lo mandaba nunca.

## Ahora

En PENDIENTE y EN_PROCESO (los reasignables) el modal muestra «Quién lo
cuenta», con la misma lista del conteo manual (`/api/asignacion/candidatos`),
y esconde la cantidad. Guardar manda `operario_id` solo si cambió (`null` =
a la cola). En un conteo ya contado el selector no aparece.

El arnés corre `conteo.js` con `util.js` real en Node y un `get`/`put` falsos.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]

_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const base = process.argv.slice(1).filter(a => a !== '--')[0];
const elementos = {};
const el = (id) => (elementos[id] = elementos[id] || { id, style: {}, value: '', disabled: false, innerHTML: '', textContent: '', title: '' });
const pedidos = []; const puts = []; const alertas = [];
const ctx = { console, URLSearchParams, window: {}, globalThis: {}, document: { getElementById: el },
  get: async (u) => { pedidos.push(u); return {
    disponibles: [{ id: 27, nombre: 'Operario 2 <b>' }],
    no_disponibles: [{ id: 30, nombre: 'Ausente', presencia: 'AUSENTE', presencia_texto: 'Incapacidad' }] }; },
  put: async (u, b) => { puts.push([u, b]); return { cambios: ['ok'] }; },
  alerta: (m, t) => alertas.push([m, t]), cargarConteos: async () => {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
const abrir = vm.runInContext('conteoAbrirEdicion', ctx);
const guardar = vm.runInContext('conteoGuardarEdicion', ctx);
const espera = () => new Promise(r => setTimeout(r, 0));
(async () => {
  const out = {};
  // 1 · CC2 pendiente sin dueño: el selector aparece, la cantidad no.
  const cc2 = { id: 26843, estado: 'PENDIENTE', almacen_id: 1, operario_id: null,
    producto_codigo: 'PAPELSP7879', cantidad_fisica: null, no_se_corrige_cantidad: 'Es un CC2' };
  abrir(cc2);
  el('conteo-edit-motivo').value = 'apurado';
  out.guardarAntesDeCargar = { puts: puts.length };
  await guardar();
  out.guardarAntesDeCargar.alerta = alertas.slice(-1)[0];
  out.guardarAntesDeCargar.puts = puts.length;
  await espera(); await espera();
  out.pedido = pedidos.slice(-1)[0];
  out.selectorVisible = el('conteo-edit-operario-bloque').style.display !== 'none';
  out.cantidadVisible = el('conteo-edit-cantidad-bloque').style.display !== 'none';
  out.opciones = el('conteo-edit-operario').innerHTML;
  // Sin cambiar a nadie: no se manda nada.
  el('conteo-edit-motivo').value = 'dar a Operario 2';
  el('conteo-edit-operario').value = '';
  await guardar();
  out.sinCambio = { puts: puts.length, alerta: alertas.slice(-1)[0] };
  // Elegir a Operario 2.
  el('conteo-edit-motivo').value = 'dar a Operario 2';
  el('conteo-edit-operario').value = '27';
  await guardar();
  out.reasignar = puts.slice(-1)[0];
  // 2 · Con dueño, devolverlo a la cola manda null.
  abrir({ ...cc2, operario_id: 27, operario_nombre: 'Operario 2' });
  await espera(); await espera();
  el('conteo-edit-motivo').value = 'a la cola';
  el('conteo-edit-operario').value = '';
  await guardar();
  out.aLaCola = puts.slice(-1)[0];
  // 3 · Ya contado: sin selector, con cantidad.
  abrir({ id: 41, estado: 'DESCUADRE', almacen_id: 1, operario_id: 2, cantidad_fisica: 12, no_se_corrige_cantidad: null });
  out.contado = { selector: el('conteo-edit-operario-bloque').style.display !== 'none',
                  cantidad: el('conteo-edit-cantidad-bloque').style.display !== 'none' };
  el('conteo-edit-motivo').value = 'error';
  el('conteo-edit-cantidad').value = '11';
  await guardar();
  out.corregir = puts.slice(-1)[0];
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _correr():
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_un_conteo_sin_contar_ofrece_quien_lo_cuenta_y_no_la_cantidad():
    o = _correr()
    assert o['selectorVisible'] and not o['cantidadVisible']
    assert o['pedido'] == '/api/asignacion/candidatos?tipo=CONTEO&almacen_id=1'
    assert 'Operario 2 &lt;b&gt;' in o['opciones'], 'el nombre va escapado'
    assert 'disabled>Ausente — Incapacidad' in o['opciones']


def test_guardar_antes_de_que_cargue_la_lista_no_manda_nada():
    o = _correr()
    assert o['guardarAntesDeCargar']['puts'] == 0
    assert 'cargue' in o['guardarAntesDeCargar']['alerta'][0]


def test_sin_cambiar_a_nadie_no_se_manda():
    o = _correr()
    assert o['sinCambio']['puts'] == 0
    assert 'No cambió nada' in o['sinCambio']['alerta'][0]


def test_elegir_una_persona_manda_su_id():
    o = _correr()
    url, body = o['reasignar']
    assert url == '/api/conteo/26843/editar'
    assert body == {'motivo_edicion': 'dar a Operario 2', 'operario_id': 27}


def test_quitarle_el_dueno_lo_devuelve_a_la_cola():
    o = _correr()
    assert o['aLaCola'][1] == {'motivo_edicion': 'a la cola', 'operario_id': None}


def test_un_conteo_ya_contado_no_se_reasigna_desde_aca():
    o = _correr()
    assert o['contado'] == {'selector': False, 'cantidad': True}
    assert o['corregir'][1] == {'motivo_edicion': 'error', 'cantidad_fisica': 11}
