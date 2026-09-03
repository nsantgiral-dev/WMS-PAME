"""¿El trinquete de la escalada de custodia mata la vuelta al defecto?"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
OBJ = 'tests/flota/test_traspaso_t1.py'

MUT = [
    ('M1 mismo_custodio vuelve a salir del cuerpo del request (el defecto)',
     'flota/adaptadores/traspaso.py',
     '        (es_el_custodio_actual or _pide_gestion)\n        and vigente is not None',
     '        vigente is not None'),
    ('M2 el atajo se le da a cualquiera',
     'flota/adaptadores/traspaso.py',
     '        (es_el_custodio_actual or _pide_gestion)',
     '        (True or _pide_gestion)'),
    ('M3 quien_pide se cree en vez de mirarse',
     'flota/adaptadores/traspaso.py',
     '    _pide_gestion = quien_pide == QuienPide.ADMIN_ZONA',
     '    _pide_gestion = True'),
]


def correr():
    r = subprocess.run(['venv/bin/python', '-m', 'pytest', OBJ, '-q',
                        '-p', 'no:randomly', '--no-header'],
                       cwd=RAIZ, capture_output=True, text=True,
                       env={'PATH': os.environ['PATH'], 'TZ': 'UTC',
                            'HOME': str(Path.home())})
    s = r.stdout or ''
    if 'skipped' in s or 'no tests ran' in s:
        return 0, 'NO SE JUZGÓ: ' + s[-160:]
    return r.returncode, s[-160:]


fallos = []
for etiqueta, archivo, viejo, nuevo in MUT:
    ruta = RAIZ / archivo
    respaldo = ruta.read_text()
    if viejo not in respaldo:
        print(f'  ⚠ {etiqueta}: el texto a mutar NO existe'); fallos.append(etiqueta); continue
    ruta.write_text(respaldo.replace(viejo, nuevo, 1))
    try:
        cod, cola = correr()
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
