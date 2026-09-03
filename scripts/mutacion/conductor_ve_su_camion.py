"""¿Muere lo que hace que el conductor se entere del estado de su camión?"""
import os, subprocess, sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
OBJ = 'tests/flota/test_el_conductor_ve_su_camion.py'

MUT = [
    ('M1 mi-turno deja de traer el estado (el defecto original)',
     'flota/api/conductor.py',
     "        'estado_vehiculo': _estado_del_vehiculo(turno.vehiculo_id),",
     "        'estado_vehiculo': None,"),
    ('M2 el peor hallazgo se elige por orden de llegada, no por gravedad',
     'flota/api/conductor.py',
     "            {'criticidad': abiertos[0].criticidad,",
     "            {'criticidad': abiertos[-1].criticidad,"),
    ('M3 sin inspeccion se afirma que NO habilita (en vez de no saber)',
     'flota/api/conductor.py',
     "else {'hecha': False, 'veredicto': None,\n                                      'habilita_despacho': None}",
     "else {'hecha': False, 'veredicto': None,\n                                      'habilita_despacho': False}"),
    ('M4 los documentos vencidos no se miran',
     'flota/api/conductor.py',
     "        and (d.fecha_vencimiento - hoy).days < 0]",
     "        and False]"),
    ('M5 el 403 deja de decir que rol haria falta',
     'flota/api/_permisos.py',
     "                    'roles_permitidos': sorted(roles),",
     "                    'roles_permitidos': [],"),
]


def correr():
    r = subprocess.run(['venv/bin/python', '-m', 'pytest', OBJ, '-q',
                        '-p', 'no:randomly', '--no-header'],
                       cwd=RAIZ, capture_output=True, text=True,
                       env={'PATH': os.environ['PATH'], 'TZ': 'UTC',
                            'HOME': str(Path.home())})
    cola = (r.stdout or '').strip().split('\n')[-1]
    if 'failed' in cola:
        return 1, cola
    if 'no tests ran' in cola or 'passed' not in cola:
        return 0, 'NO SE JUZGÓ: ' + cola
    return 0, cola


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
