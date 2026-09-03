"""Los CHECK de las tres últimas tandas (taller, preventivo, llantas): ¿los
ejerce ALGUIEN, aunque sea contra SQLite?

La suite de PostgreSQL ya se midió: no los toca. Acá se quita cada uno de la
metadata y se corren SUS PROPIAS suites contra SQLite. Si siguen verdes, ese
constraint no decide nada en ningún motor.
"""
import json
import os
import subprocess

RAIZ = str(__import__('pathlib').Path(__file__).resolve().parents[2])
SCRATCH = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(RAIZ, 'venv/bin/python')

SUITES = ['tests/flota/test_taller.py', 'tests/flota/test_llantas.py',
          'tests/flota/test_preventivo_nace.py', 'tests/flota/test_canon_preventivo.py',
          'tests/flota/test_garantia.py', 'tests/flota/test_endpoints_preventivo.py',
          'tests/flota/test_render_taller_js.py', 'tests/flota/test_render_llantas_js.py',
          'tests/flota/test_render_preventivo_js.py']

TABLAS = ('flota_orden_trabajo', 'flota_intervencion', 'flota_plan_tarea',
          'flota_ejecucion_tarea', 'flota_llanta', 'flota_montaje_llanta')


def listar():
    out = subprocess.run([PY, '-c', '''
import sys; sys.path.insert(0,".")
from app import create_app, db
import flota.adaptadores.modelos
from sqlalchemy import CheckConstraint
import json
app=create_app()
with app.app_context():
    r=[]
    for n,t in sorted(db.metadata.tables.items()):
        if n not in %r: continue
        for c in t.constraints:
            if isinstance(c, CheckConstraint): r.append(f"{n}::{c.name}")
    print("JSON"+json.dumps(r))
''' % (TABLAS,)], cwd=RAIZ, capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': RAIZ})
    for l in out.stdout.splitlines():
        if l.startswith('JSON'):
            return json.loads(l[4:])
    raise SystemExit(out.stdout + out.stderr)


def correr(objetivo):
    env = {**os.environ, 'TZ': 'UTC', 'PYTHONPATH': RAIZ + ':' + SCRATCH}
    if objetivo:
        env['MUTAR_CHECK'] = objetivo
    else:
        env.pop('MUTAR_CHECK', None)
    r = subprocess.run(
        [PY, '-m', 'pytest', *SUITES, '-q', '-p', 'no:randomly',
         '-p', 'mutplugin_checks', '-m', 'not postgres'],
        cwd=RAIZ, capture_output=True, text=True, env=env)
    cola = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''
    return r.returncode, cola


if __name__ == '__main__':
    rc, cola = correr(None)
    print('CONTROL:', 'VERDE' if rc == 0 else 'ROJO', '|', cola, flush=True)
    if rc != 0:
        raise SystemExit('control rojo')
    objetivos = listar()
    res = {}
    for i, o in enumerate(objetivos, 1):
        rc, cola = correr(o)
        # Un resumen sin `passed` (error de colección) no es una mutación cazada.
        juzgado = 'passed' in cola or 'failed' in cola
        res[o] = ('MUERE' if rc != 0 else 'SOBREVIVE') if juzgado else 'NO SE JUZGO'
        print(f'[{i:2}/{len(objetivos)}] {res[o]:12} {o:60} | {cola}', flush=True)
    viven = [k for k, v in res.items() if v == 'SOBREVIVE']
    print(f'\nSIN EJERCER NI EN SQLITE: {len(viven)} de {len(objetivos)}')
    for k in viven:
        print('  ·', k)
    with open(os.path.join(SCRATCH, 'resultado_sqlite.json'), 'w') as f:
        json.dump(res, f, indent=1)
