"""El botón de navegación, EJECUTADO — y sobre todo, el botón que NO se pinta.

El pedido era explícito: *«Degradá con honestidad: sin coordenada ni dirección,
NO pintes un botón que abre Waze en el centro de Bogotá — mostrá que no se
sabe.»* Eso no se puede verificar leyendo el archivo: un detector de texto vería
`waze.com` dentro de `_condBloqueNavegacion` y no sabría si el `href` se pinta
siempre o solo cuando hay punto. Es exactamente la clase que ya costó una vez
en `test_reconciliacion_por_bodega` — el identificador estaba, el pintado no.

Así que se ejecuta el `rutas.js` real en Node y se mira lo que devuelve la
función, en los tres estados que la Regla 4 exige distinguir:

    geo == null      → nadie capturó nada todavía
    geo sin lat      → se capturó y no se pudo elegir un punto, con su motivo
    geo con punto    → Waze + Maps, con cuántas visitas lo sostienen

Node no siempre está; cuando falta el test se salta **declarándolo**, mismo
criterio que `tests/test_frontend_integrity.py`. Un salto anunciado no es una
cobertura fingida.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
RUTAS_JS = RAIZ / 'app' / 'static' / 'pwa' / 'rutas.js'

# El arnés vive acá y no en un `.js` suelto a propósito: es andamio de este
# test, no código del PWA. Un archivo más en `app/static/pwa/` entraría en el
# grafo de llamadas que vigila `test_frontend_integrity.py`.
HARNESS = r"""
import fs from 'node:fs';
import vm from 'node:vm';

const RUTASJS = process.argv[2];
const PARADA = JSON.parse(fs.readFileSync(process.argv[3], 'utf-8'));

const ctx = {
  console,
  document: { getElementById: () => null, querySelector: () => null,
              createElement: () => ({ style: {} }), addEventListener() {} },
  window: { location: { origin: 'http://test' }, addEventListener() {} },
  navigator: { onLine: true, userAgent: 'node' },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
  fetch: async () => ({ ok: true, json: async () => ({}) }),
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(RUTASJS, 'utf-8'), ctx, { filename: 'rutas.js' });

if (typeof ctx._condBloqueNavegacion !== 'function') {
  throw new Error('no existe _condBloqueNavegacion');
}
process.stdout.write(JSON.stringify({ html: ctx._condBloqueNavegacion(PARADA) }));
"""


def _bloque(tmp_path, geo) -> str:
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    harness = tmp_path / 'harness.mjs'
    harness.write_text(HARNESS, encoding='utf-8')
    parada = tmp_path / 'parada.json'
    parada.write_text(json.dumps(
        {'cliente': 'PAPELERIA LA 5', 'municipio': 'Neiva', 'geo': geo}),
        encoding='utf-8')
    proc = subprocess.run(['node', str(harness), str(RUTAS_JS), str(parada)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'el render reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)['html']


class TestSinCoordenadaNoHayBoton:
    """El lado que el pedido nombra: **no se pinta un botón que no sirve.**

    Un conductor que abre Waze una vez y aterriza en el centro del municipio no
    vuelve a tocar ese botón nunca, aunque después empiece a servir. Se gasta
    la confianza en el gesto, que es lo único que hace que el dato se acumule.
    """

    def test_sin_geo_no_aparece_ningun_enlace_de_navegacion(self, tmp_path):
        html = _bloque(tmp_path, None)
        assert 'waze.com' not in html
        assert 'google.com/maps' not in html
        assert 'href' not in html

    def test_sin_geo_la_pantalla_dice_que_no_se_sabe(self, tmp_path):
        """Y dice cómo se arregla: sin eso, «no sabemos» es una queja."""
        html = _bloque(tmp_path, None)
        assert 'No sabemos dónde queda' in html
        assert 'Estoy aquí' in html

    def test_un_maestro_sin_punto_dice_su_motivo(self, tmp_path):
        """«Nadie capturó nada» y «las capturas no coinciden» se arreglan
        distinto — uno hablando con el conductor, el otro mirando si hay dos
        clientes con el mismo nombre. Colapsarlos deja al que lee sin saber
        qué hacer."""
        html = _bloque(tmp_path, {
            'lat': None, 'lon': None, 'precision_m': None, 'fuente': 'sin_dato',
            'motivo_sin_maestro': 'capturas_dispersas',
            'capturas_consideradas': 0, 'capturas_descartadas': 4,
            'elegido_en': '2026-09-02T10:00:00'})
        assert 'waze.com' not in html
        assert 'dos clientes con el mismo nombre' in html

    def test_precision_insuficiente_no_se_confunde_con_sin_capturas(self, tmp_path):
        html = _bloque(tmp_path, {
            'lat': None, 'lon': None, 'precision_m': None, 'fuente': 'sin_dato',
            'motivo_sin_maestro': 'precision_insuficiente',
            'capturas_consideradas': 0, 'capturas_descartadas': 2,
            'elegido_en': None})
        assert 'precisión suficiente' in html
        assert 'no se capturó ninguna' not in html


class TestConCoordenadaSiHayBoton:
    """El gemelo. Sin esto, «nunca se pinta un botón» sería un detector
    perfecto y un producto inútil — que es la forma exacta de los seis guards
    en verde del 2026-08-15."""

    GEO = {'lat': 2.9273, 'lon': -75.2819, 'precision_m': 18.0,
           'fuente': 'gps_conductor', 'motivo_sin_maestro': None,
           'capturas_consideradas': 3, 'capturas_descartadas': 1,
           'elegido_en': '2026-09-02T10:00:00'}

    def test_waze_y_maps_llevan_la_coordenada_exacta(self, tmp_path):
        html = _bloque(tmp_path, self.GEO)
        assert 'https://waze.com/ul?ll=2.9273,-75.2819&navigate=yes' in html
        assert ('https://www.google.com/maps/dir/?api=1&'
                'destination=2.9273,-75.2819') in html

    def test_el_punto_viaja_con_cuantas_visitas_lo_sostienen(self, tmp_path):
        """«3 visitas ±18 m» y «1 visita ±90 m» son maestros muy distintos, y
        el conductor es el único que puede juzgar cuál creerle."""
        html = _bloque(tmp_path, self.GEO)
        assert '3 visita' in html
        assert '±18 m' in html

    def test_no_se_pinta_el_mensaje_de_que_no_se_sabe(self, tmp_path):
        html = _bloque(tmp_path, self.GEO)
        assert 'No sabemos dónde queda' not in html
