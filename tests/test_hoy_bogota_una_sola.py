"""El día de Bogotá en el PWA: una función (`hoyBogota`, util.js), ninguna copia.

**El caso** (auditoría «operación diaria por rol», 2026-09-25): Rutas llenaba
sus filtros y la fecha de una ruta nueva con `new Date().toISOString()` — el
día UTC. Entre las 7 p. m. y la medianoche de Neiva ya es «mañana»: la lista
de rutas del turno de la tarde salía vacía y la ruta se programaba para el día
siguiente. Y había cinco copias de «el día de Bogotá» (liquidación, tablero,
flota, analítica, temporada), cada una con su propio respaldo.

**La clase**: *una fecha que alguien lee como día sale del reloj UTC, o de una
copia de la política*. Trinquete sobre el JS sin comentarios:

1. ningún `.toISOString()` (o `.toJSON()`) recortado a día —`slice(0, 10)`,
   `substring(0, 10)`, `substr(0, 10)`, `split('T')[0]`—;
2. ningún `Intl.DateTimeFormat('en-CA'…)` fuera de util.js (la forma de
   armar el día de Bogotá);
3. ninguna función `…HoyBogota` fuera de la única.

`new Date().toISOString()` entero sigue permitido: es un INSTANTE (la hora del
teléfono que viaja al servidor), no un día. Inventario de excepciones que solo
encoge: hoy vacío.

Lo que NO cubre: el día sacado en dos pasos (`const s = d.toISOString(); …
s.slice(0, 10)`) y `getDate()`/`getFullYear()` (hora LOCAL del teléfono, que en
Colombia es Bogotá: no es el defecto).
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_frontend_integrity import _sin_comentarios

RAIZ = Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

_DIA_DE_ISO = re.compile(
    r'\.to(?:ISOString|JSON)\(\s*\)\s*'
    r'(?:\.(?:slice|substring|substr)\(\s*0\s*,\s*10\s*\)'
    r'|\.split\(\s*[\'"]T[\'"]\s*\)\s*\[\s*0\s*\])')
_FORMATO_DIA = re.compile(r'DateTimeFormat\(\s*[\'"]en-CA[\'"]')
_COPIA = re.compile(r'function\s+(\w+HoyBogota|\w*hoyBogota\w+)\s*\(')

#: (archivo, forma) → por qué se tolera. **Solo encoge.**
EXCEPCIONES = {}


def formas(fuente: str, archivo: str = 'x.js'):
    """Las formas prohibidas del fuente, sin contar comentarios."""
    codigo = _sin_comentarios(fuente)
    salida = [('dia_de_iso', m.group(0)) for m in _DIA_DE_ISO.finditer(codigo)]
    if archivo != 'util.js':
        salida += [('formato_dia', m.group(0)) for m in _FORMATO_DIA.finditer(codigo)]
    salida += [('copia', m.group(1)) for m in _COPIA.finditer(codigo)]
    return salida


def _todas():
    out = {}
    for f in sorted(PWA.glob('*.js')):
        for forma, texto in formas(f.read_text(encoding='utf-8'), f.name):
            out.setdefault((f.name, forma), []).append(texto)
    return out


class TestUnaSolaFormaDelDia:

    def test_no_crece(self):
        nuevas = {k: v for k, v in _todas().items() if k not in EXCEPCIONES}
        assert not nuevas, (
            f'Un día armado por fuera de hoyBogota (util.js): {nuevas}. '
            f'`toISOString()` es el día UTC; después de las 7 p. m. es mañana.')

    def test_solo_encoge(self):
        viejas = set(EXCEPCIONES) - set(_todas())
        assert not viejas, f'ya no existen, sacalas: {viejas}'

    def test_util_tiene_la_unica(self):
        assert re.search(r'function hoyBogota\(', (PWA / 'util.js').read_text(encoding='utf-8'))

    # ── meta ────────────────────────────────────────────────────────────────

    def test_meta_ve_las_formas(self):
        src = ("const a = new Date().toISOString().slice(0, 10);\n"
               "const b = new Date().toISOString().split('T')[0];\n"
               "const c = x.toISOString()\n    .substring(0,10);\n"
               "const f = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Bogota'});\n"
               "function liqHoyBogota(f) {}\n")
        tipos = [t for t, _ in formas(src)]
        assert tipos.count('dia_de_iso') == 3 and 'formato_dia' in tipos and 'copia' in tipos

    def test_meta_no_marca_lo_sano(self):
        src = ("// const a = new Date().toISOString().slice(0, 10);\n"
               "/* x.toISOString().split('T')[0] */\n"
               "const ts = new Date().toISOString();\n"
               "const d = hoyBogota();\n"
               "const s = 'toISOString().slice(0, 10) en una cadena no llama nada'.length;\n")
        assert [t for t, _ in formas(src) if t != 'dia_de_iso'] == []
        assert formas("const ts = new Date().toISOString();\nhoyBogota(null, -1);\n") == []

    def test_piso(self):
        assert len(list(PWA.glob('*.js'))) >= 25


# ═════════════════════════════════════════════════════════════════════════════
# La función, ejecutada
# ═════════════════════════════════════════════════════════════════════════════

def _node(expr):
    if not shutil.which('node'):
        pytest.skip('node no disponible')
    js = ("const fs=require('fs'),vm=require('vm');const ctx={};vm.createContext(ctx);"
          f"vm.runInContext(fs.readFileSync({json.dumps(str(PWA / 'util.js'))},'utf8'),ctx);"
          f"console.log(JSON.stringify(vm.runInContext({json.dumps(expr)},ctx)));")
    p = subprocess.run(['node', '-e', js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


class TestHoyBogota:

    def test_de_noche_sigue_siendo_hoy(self):
        assert _node("hoyBogota(new Date('2026-09-25T02:30:00Z'))") == '2026-09-24'

    def test_de_dia(self):
        assert _node("hoyBogota(new Date('2026-09-24T15:00:00Z'))") == '2026-09-24'

    def test_desplazamiento(self):
        assert _node("hoyBogota(new Date('2026-09-25T02:30:00Z'), -29)") == '2026-08-26'

    def test_sin_intl_cae_a_utc_menos_5(self):
        assert _node("(() => { const I = Intl; Intl = undefined; "
                     "try { return hoyBogota(new Date('2026-09-25T02:30:00Z')); } "
                     "finally { Intl = I; } })()") == '2026-09-24'
