"""¿Cuáles CHECK de flota los EJERCE la suite de PostgreSQL, y cuáles solo cuenta?

Por cada CheckConstraint de los modelos se lo quita de la metadata antes de que
el fixture `esquema` haga el `create_all`, y se corre la suite de PostgreSQL.
Si sigue verde, ese constraint no decide nada ahí: existe, se cuenta, y nadie
comprobó que rechace.

`test_todos_los_check_quedaron_en_la_base` se deselecciona a propósito: cuenta,
no ejerce, y se caería con CUALQUIER mutación — es exactamente la proxy que se
quiere separar de la propiedad.
"""
import json
import os
import subprocess
import sys

RAIZ = str(__import__('pathlib').Path(__file__).resolve().parents[2])
SCRATCH = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(RAIZ, 'venv/bin/python')
PG_URL = os.environ['PGURL']
SALIDA = os.path.join(SCRATCH, 'resultado_mutar_checks.json')

DESEL = ('tests/flota/test_constraints_postgres.py::TestElEsquemaSeCreaEnPostgres'
         '::test_todos_los_check_quedaron_en_la_base')


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
        if not (n.startswith("flota_") or n.endswith("_geo")): continue
        for c in t.constraints:
            if isinstance(c, CheckConstraint): r.append(f"{n}::{c.name}")
    print("JSON"+json.dumps(r))
'''], cwd=RAIZ, capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': RAIZ})
    for l in out.stdout.splitlines():
        if l.startswith('JSON'):
            return json.loads(l[4:])
    print(out.stdout, out.stderr)
    raise SystemExit('no pude listar')


def correr(objetivo):
    env = {**os.environ, 'TZ': 'UTC',
           'PYTHONPATH': RAIZ + ':' + SCRATCH,
           'FLOTA_TEST_PG_URL': PG_URL}
    if objetivo:
        env['MUTAR_CHECK'] = objetivo
    else:
        env.pop('MUTAR_CHECK', None)
    r = subprocess.run(
        [PY, '-m', 'pytest', 'tests/flota/test_constraints_postgres.py', '-q',
         '-p', 'no:randomly', '-p', 'mutplugin_checks', '-m', 'postgres',
         '--deselect', DESEL],
        cwd=RAIZ, capture_output=True, text=True, env=env)
    return r.returncode, r.stdout[-400:]


if __name__ == '__main__':
    rc, salida = correr(None)
    print('CONTROL (sin mutar):', 'VERDE' if rc == 0 else 'ROJO')
    if rc != 0:
        print(salida)
        raise SystemExit('el control no esta verde; el arnes no mide nada')

    objetivos = listar()
    print(f'{len(objetivos)} CHECK a mutar\n')
    res = {}
    for i, o in enumerate(objetivos, 1):
        rc, _ = correr(o)
        res[o] = 'MUERE' if rc != 0 else 'SOBREVIVE'
        print(f'[{i:3}/{len(objetivos)}] {res[o]:10} {o}', flush=True)

    viven = [o for o, v in res.items() if v == 'SOBREVIVE']
    print()
    print(f'CHECK QUE LA SUITE DE POSTGRES NO EJERCE: {len(viven)} de {len(objetivos)}')
    for o in viven:
        print('   ·', o)
    with open(SALIDA, 'w') as f:
        json.dump(res, f, indent=1)
