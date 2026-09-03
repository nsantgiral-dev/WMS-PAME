"""¿El trinquete 5 reparado de verdad mata lo que antes sobrevivía?"""
import os, subprocess, sys
from pathlib import Path
RAIZ = Path(__file__).resolve().parents[2]
JS = RAIZ / 'app/static/pwa/flota.js'
OBJ = 'tests/flota/test_trinquetes_flota.py::TestTrinqueteEndpointsSinConsumidor'

MUT = [
    # (etiqueta, viejo, nuevo)  — se rompe la invocación REAL y se deja prosa.
    ('la URL de aplazar solo queda en un comentario',
     "  aplazar:    (id) => `/flota/hallazgos/${id}/aplazar`,",
     "  aplazar:    (id) => `/flota/hallazgos/${id}/` + 'apla' + 'zar',"),
    ('el limpiador devuelve vacío (limpia de más)',
     None, None),   # se muta el helper, no el JS
]

def correr():
    r = subprocess.run(['venv/bin/python','-m','pytest',OBJ,'-q','-p','no:randomly','--no-header'],
                       cwd=RAIZ, capture_output=True, text=True,
                       env={'PATH': os.environ['PATH'], 'TZ':'UTC', 'HOME': str(Path.home())})
    s = r.stdout or ''
    if 'no tests ran' in s or 'error' in s.lower() and 'passed' not in s:
        return 0, 'NO SE JUZGÓ: ' + s[-200:]
    return r.returncode, s[-200:]

fallos = []
# M1 · sobre el JS
etiqueta, viejo, nuevo = MUT[0]
src = JS.read_text()
if viejo not in src:
    print(f'  ⚠ {etiqueta}: el texto a mutar no existe'); fallos.append(etiqueta)
else:
    JS.write_text(src.replace(viejo, nuevo, 1))
    try: cod, cola = correr()
    finally: JS.write_text(src)
    print(('  ✓ muerta      ' if cod else '  ✗ SOBREVIVIÓ  ') + etiqueta)
    if cod == 0: fallos.append(etiqueta)

# M2 · el limpiador que limpia de más tiene que romper sus propios tests
T = RAIZ / 'tests/flota/test_trinquetes_flota.py'
src = T.read_text()
viejo2 = "    salida = []\n    i, n = 0, len(fuente)"
nuevo2 = "    return ''\n    salida = []\n    i, n = 0, len(fuente)"
if viejo2 not in src:
    print('  ⚠ M2: no se encontró el cuerpo del limpiador'); fallos.append('M2')
else:
    T.write_text(src.replace(viejo2, nuevo2, 1))
    try:
        r = subprocess.run(['venv/bin/python','-m','pytest',
                            'tests/flota/test_trinquetes_flota.py::TestElLimpiadorDeComentariosSeMide',
                            '-q','-p','no:randomly','--no-header'], cwd=RAIZ,
                           capture_output=True, text=True,
                           env={'PATH': os.environ['PATH'], 'TZ':'UTC','HOME':str(Path.home())})
    finally: T.write_text(src)
    ok = r.returncode != 0
    print(('  ✓ muerta      ' if ok else '  ✗ SOBREVIVIÓ  ') + 'el limpiador devuelve vacío (limpia de más)')
    if not ok: fallos.append('M2')

print()
sys.exit(1 if fallos else 0)
