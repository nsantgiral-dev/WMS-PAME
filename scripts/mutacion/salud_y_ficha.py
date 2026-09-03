"""¿Mueren las mutaciones sobre los doce campos pintados y la capacidad del tanque?"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

MUT = [
    ('M1 el PUT vuelve a ignorar la capacidad (defecto original)',
     'flota/api/ficha.py',
     "    'capacidad_tanque_galones', 'capacidad_tanque_fuente',\n)",
     ')',
     'tests/flota/test_vocabularios_completos.py'),
    ('M2 el detector de sobre-tanqueo se apaga',
     'flota/dominio/costos.py',
     'def excede_capacidad(',
     'def excede_capacidad_APAGADO(*a, **k):\n    return None\n\n\ndef excede_capacidad(',
     'tests/flota/test_vocabularios_completos.py'),
    ('M3 el tablero deja de decir que los datos son de QA',
     'app/static/pwa/flota.js',
     '  if (h.datos_reales === false) {',
     '  if (false) {',
     'tests/flota/test_render_salud_js.py'),
    ('M4 el cierre forzado vuelve a ser invisible',
     'app/static/pwa/flota.js',
     '  if (h.custodias_cerradas_forzadas > 0) {',
     '  if (false) {',
     'tests/flota/test_render_salud_js.py'),
    ('M5 la cobertura de fichas se pinta siempre, aunque esté completa',
     'app/static/pwa/flota.js',
     '  if (h.vehiculos_activos > 0 && h.fichas_completas < h.vehiculos_activos) {',
     '  if (h.vehiculos_activos > 0) {',
     'tests/flota/test_render_salud_js.py'),
]


def correr(objetivo):
    r = subprocess.run(['venv/bin/python', '-m', 'pytest', objetivo, '-q',
                        '-p', 'no:randomly', '--no-header'],
                       cwd=RAIZ, capture_output=True, text=True,
                       env={'PATH': os.environ['PATH'], 'TZ': 'UTC',
                            'HOME': str(Path.home())})
    s = r.stdout or ''
    cola = s.strip().split('\n')[-1] if s.strip() else ''
    # Los tres estados, y el tercero se decide por lo que REALMENTE corrió.
    #
    # La primera versión daba «no se juzgó» ante cualquier `skipped` en la
    # salida, y eso es demasiado grueso: un archivo con dos tests saltados por
    # una razón legítima —los hay— no se podría juzgar nunca, y la mutación
    # figuraría como no evaluada para siempre. El arnés que persigue verdes
    # falsos produciría un «no sé» falso, que es el mismo error con otro signo.
    #
    # La regla correcta mira si hubo veredicto: si algo falló, la mutación
    # murió; si algo pasó y nada falló, sobrevivió; si no corrió nada, no se
    # juzgó.
    if 'failed' in cola:
        return 1, cola
    if 'no tests ran' in cola or 'error' in cola.lower() or 'passed' not in cola:
        return 0, 'NO SE JUZGÓ: ' + cola
    return 0, cola


fallos = []
for etiqueta, archivo, viejo, nuevo, objetivo in MUT:
    ruta = RAIZ / archivo
    respaldo = ruta.read_text()
    if viejo not in respaldo:
        print(f'  ⚠ {etiqueta}: el texto a mutar NO existe'); fallos.append(etiqueta); continue
    ruta.write_text(respaldo.replace(viejo, nuevo, 1))
    try:
        cod, cola = correr(objetivo)
    finally:
        ruta.write_text(respaldo)
    if cod == 0:
        print(f'  ✗ {"SE SALTÓ  " if cola.startswith("NO SE") else "SOBREVIVIÓ"}  {etiqueta}')
        fallos.append(etiqueta)
    else:
        print(f'  ✓ muerta      {etiqueta}')

print()
print(f'MUTACIONES MUERTAS: {len(MUT) - len(fallos)}/{len(MUT)}')
sys.exit(1 if fallos else 0)
