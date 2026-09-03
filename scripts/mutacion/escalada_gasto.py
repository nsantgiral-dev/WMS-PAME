"""¿El guard de autoridad de gastos muere cuando se lo rompe?

Tres formas de romperlo, y las tres tienen que poner la suite roja:
  M1 · el guard no se llama (la escalada original vuelve)
  M2 · la autoridad se cree en vez de comprobarse contra la base
  M3 · una categoría desconocida degrada al rol más amplio
"""
import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
OBJ = 'tests/flota/test_escalada_gasto.py'

MUT = [
    ('M1 el guard de autoridad no se llama',
     'flota/adaptadores/gastos.py',
     '    _exigir_autoridad(categoria, registrado_por_usuario_id)',
     '    pass  # _exigir_autoridad'),
    ('M2 la autoridad se cree, no se comprueba',
     'flota/adaptadores/gastos.py',
     '    u = Usuario.query.get(usuario_id)\n'
     '    if u is None or not u.activo or u.rol not in permitidos:',
     '    u = Usuario.query.get(usuario_id)\n'
     '    if False:'),
    ('M3 una categoria desconocida degrada al rol mas amplio',
     'flota/dominio/costos.py',
     '    return categoria not in CATEGORIAS_DE_CAMPO',
     '    return categoria in (\'soat\', \'rtm\')'),
]


def correr():
    r = subprocess.run(['venv/bin/python', '-m', 'pytest', OBJ, '-q',
                        '-p', 'no:randomly', '--no-header'],
                       cwd=RAIZ, capture_output=True, text=True,
                       env={'PATH': os.environ['PATH'], 'TZ': 'UTC',
                            'HOME': str(Path.home())})
    s = r.stdout or ''
    # Un test saltado sale con exit 0 igual que uno que paso: el arnes que caza
    # verdes falsos no puede producir uno.
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
