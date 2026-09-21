"""El conductor tiene que ENTERARSE de que la correa está vencida.

`preventivo_vencido` se calculaba, se serializaba y se testeaba — y ninguna
pantalla lo leía. `flotaCondEstado` leía `hallazgos_vencidos`,
`hallazgos_abiertos`, `hallazgo_peor`, `documentos_vencidos` e
`inspeccion_de_hoy`, y nunca `preventivo_vencido`. El dato llegaba al navegador
y se descartaba ahí.

El mismo día se había arreglado el `KeyError` que rompía ese campo con un 500
— o sea que el endpoint pasó de reventar a publicar un dato que nadie mostraba.
Arreglar el productor no sirve si el consumidor no existe.

El backend lo publica con su argumento escrito: «una correa que revienta en
motor de interferencia es motor nuevo, y `distribucion_km_cambio` llevaba meses
en la base sin un solo lector». Se le dio lector en el servidor y no en la
pantalla.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'

HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';
const PWA = process.argv[2];
const ESTADO = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));
const ctx = {
  console,
  document: { getElementById: () => null, querySelector: () => null,
              querySelectorAll: () => [], addEventListener() {},
              createElement: () => ({ style: {} }) },
  window: { location: { origin: 'http://t' }, addEventListener() {} },
  navigator: { onLine: true, vibrate() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  alerta: () => {}, TOKEN: 'x', API: '',
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(PWA + '/util.js', 'utf-8'), ctx);
vm.runInContext(fs.readFileSync(PWA + '/flota.js', 'utf-8'), ctx);
process.stdout.write(ctx.flotaCondEstado(ESTADO) || '');
"""


def _render(tmp_path, estado):
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS)
    d = tmp_path / 'e.json'
    d.write_text(json.dumps(estado))
    r = subprocess.run(['node', str(h), str(PWA), str(d)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


BASE = {'hallazgos_vencidos': 0, 'hallazgos_abiertos': 0, 'hallazgo_peor': None,
        'documentos_vencidos': [], 'inspeccion_de_hoy': {},
        'preventivo_vencido': []}


def test_una_tarea_vencida_aparece_con_su_nombre(tmp_path):
    html = _render(tmp_path, dict(BASE, preventivo_vencido=[
        {'tarea': 'Cambio de correa de distribución', 'faltan_km': -5000}]))
    assert 'correa de distribución' in html.lower(), (
        'el conductor no ve QUÉ venció: un contador dice cuántos, no cuál')
    assert 'VENCIDO' in html


def test_dice_cuantos_km_lleva_pasado(tmp_path):
    """El signo de `faltan_km` es el dato —negativo = ya pasó— pero al
    conductor se le muestra en valor absoluto con la palabra: «5.000 km
    pasado», no «-5.000 km»."""
    html = _render(tmp_path, dict(BASE, preventivo_vencido=[
        {'tarea': 'Correa', 'faltan_km': -5000}]))
    assert '5.000' in html, 'no se muestra hace cuántos km venció'
    assert '-5.000' not in html, (
        'se pinta el número negativo crudo: el signo es para el cálculo, no '
        'para la pantalla')


def test_varias_vencidas_salen_todas(tmp_path):
    html = _render(tmp_path, dict(BASE, preventivo_vencido=[
        {'tarea': 'Correa', 'faltan_km': -5000},
        {'tarea': 'Aceite', 'faltan_km': -1200}]))
    assert 'Correa' in html and 'Aceite' in html


def test_sin_km_igual_se_avisa(tmp_path):
    """`faltan_km` puede no venir. No saber hace cuánto venció no la vuelve
    menos vencida — callarla sería peor que decirla sin número."""
    html = _render(tmp_path, dict(BASE, preventivo_vencido=[
        {'tarea': 'Correa', 'faltan_km': None}]))
    assert 'Correa' in html and 'VENCIDO' in html


def test_sin_nada_vencido_no_inventa_una_linea(tmp_path):
    """La otra dirección. Un aviso que aparece sobre operación sana se vuelve
    ruido y el canal deja de leerse."""
    html = _render(tmp_path, BASE)
    assert 'VENCIDO' not in html


def test_el_bloque_sigue_mostrando_lo_de_antes(tmp_path):
    """Agregar el preventivo no puede haber tapado los daños ni los papeles."""
    html = _render(tmp_path, dict(
        BASE, hallazgos_vencidos=2,
        documentos_vencidos=[{'tipo': 'SOAT', 'vencio': '2026-01-01'}]))
    assert 'daño' in html.lower()
    assert 'SOAT' in html
