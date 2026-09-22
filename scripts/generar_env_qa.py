"""Arma `.env.qa` leyendo el ambiente QA de Railway.

`.env.qa` no se commitea —`.gitignore` lo cubre— así que sin este script la
receta para reconstruirlo se pierde con la sesión que lo armó. Eso ya costó:
durante semanas «no existe `.env.qa`» figuró como un pendiente del usuario
cuando las credenciales estaban en Railway todo el tiempo y el CLI estaba
instalado y autenticado.

    venv/bin/python scripts/generar_env_qa.py            # simulacro
    venv/bin/python scripts/generar_env_qa.py --escribir

## Dos valores NO salen de Railway, y por qué

- **`DATABASE_URL`** → la URL **pública** de la Postgres de QA
  (`RAILWAY_TCP_PROXY_DOMAIN` del servicio `Postgres`). La variable del
  servicio web apunta a `postgres.railway.internal`, que solo resuelve dentro
  del contenedor.

- **`MODO_ENSAYO=true`** → en el ambiente QA de Railway esta variable **está
  ausente**, o sea que los POST contra Siesa QA son reales. Acá nace frenado:
  cada script que quiera disparar de verdad lo desactiva DENTRO de su proceso
  (`os.environ['MODO_ENSAYO']='false'` antes de crear la app), nunca editando
  el archivo. Un `.env` es un estado que sobrevive a la sesión; una variable
  de proceso, no.

## Lo que se descarta

`PORT` y todo lo que empieza con `RAILWAY_`: son de la plataforma. Y
`FLOTA_FOTOS_DIR`, que apunta a `/data` — el punto de montaje del volumen, que
fuera del contenedor no existe.

Los valores se escriben **entre comillas simples**: hay al menos uno con
espacios (un nombre propio), y sin comillas `source .env.qa` lo interpreta
como un comando.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / '.env.qa'
FUERA = {'PORT', 'DATABASE_URL', 'FLOTA_FOTOS_DIR'}


def _vars(servicio: str) -> dict:
    r = subprocess.run(
        ['railway', 'variables', '--environment', 'QA',
         '--service', servicio, '--json'],
        capture_output=True, text=True, cwd=RAIZ)
    if r.returncode != 0:
        sys.exit(f'railway falló para {servicio}: {r.stderr.strip()[:200]}')
    return json.loads(r.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--escribir', action='store_true')
    args = ap.parse_args()

    web = _vars('WMS-PAME')
    pg = _vars('Postgres')

    dominio, puerto = pg.get('RAILWAY_TCP_PROXY_DOMAIN'), pg.get('RAILWAY_TCP_PROXY_PORT')
    if not dominio or not puerto:
        sys.exit('El servicio Postgres de QA no publica proxy TCP: sin URL pública.')
    db = (f"postgresql://{pg.get('PGUSER','postgres')}:{pg['POSTGRES_PASSWORD']}"
          f"@{dominio}:{puerto}/{pg.get('PGDATABASE','railway')}")

    def cita(v):
        return "'" + str(v).replace("'", "'\\''") + "'"

    lineas = [
        '# Generado por scripts/generar_env_qa.py desde el ambiente QA de Railway.',
        '# NO se commitea. Regenerar con:  venv/bin/python scripts/generar_env_qa.py --escribir',
        '#',
        '# DATABASE_URL usa la URL PUBLICA de la Postgres de QA; la de Railway',
        '# apunta al host interno, que no resuelve fuera del contenedor.',
        '# MODO_ENSAYO nace en true aunque en Railway esté ausente: acá los POST',
        '# se desactivan DENTRO del proceso que los quiera, no editando el archivo.',
        '',
        f'DATABASE_URL={cita(db)}',
        "MODO_ENSAYO='true'",
        '',
    ]
    n = 0
    for k in sorted(web):
        if k in FUERA or k == 'MODO_ENSAYO' or k.startswith('RAILWAY_'):
            continue
        lineas.append(f'{k}={cita(web[k])}')
        n += 1

    print(f'origen  : ambiente QA de Railway (servicios WMS-PAME + Postgres)')
    print(f'destino : {DESTINO}')
    print(f'base QA : {dominio}:{puerto}')
    print(f'variables: {n} de Railway + DATABASE_URL + MODO_ENSAYO')
    if not args.escribir:
        print('\nSimulacro: no se escribió nada. Usá --escribir.')
        return
    DESTINO.write_text('\n'.join(lineas) + '\n', encoding='utf-8')
    print(f'\nEscrito. Verificá que git lo ignore:  git check-ignore -v .env.qa')


if __name__ == '__main__':
    main()
