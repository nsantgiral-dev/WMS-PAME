"""¿El trinquete de la ventana del CPK mata la vuelta al defecto original?"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
OBJ = 'tests/flota/test_cpk_frontera_de_mes.py'

MUT = [
    ('M1 la ventana vuelve a compararse en UTC (el defecto original)',
     'flota/adaptadores/gastos.py',
     'if desde <= _dia_operativo_de(l.ts) <= hasta]',
     'if desde <= l.ts.date() <= hasta]'),
    ('M2 la zona se reimplementa a mano en vez de usar el helper',
     'flota/adaptadores/gastos.py',
     '    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()',
     '    return (ts - __import__("datetime").timedelta(hours=5)).date()'),
    ('M3 el corrimiento se hace para el lado contrario',
     'flota/adaptadores/gastos.py',
     '    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()',
     '    return (ts + __import__("datetime").timedelta(hours=5)).date()'),
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
