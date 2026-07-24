#!/usr/bin/env python
"""
Registra el SHA-256 de los TXT del backtest original en docs/canon_florencia.json.

Se corre UNA VEZ, sobre los MISMOS archivos que alimentaron la corrida
certificada. A partir de ahí la prueba de reproducción puede distinguir un
fallo de tubería de un simple cambio de insumos — que es la diferencia entre
una alarma que vale oro y una que hace perder un día.

Uso:
    venv/bin/python scripts/registrar_canon_insumos.py ventas_2025.txt ventas_2026.txt

Sin --forzar no sobreescribe un registro existente: el canon se fija una vez.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERDE, ROJO, GRIS, FIN = '\033[92m', '\033[91m', '\033[90m', '\033[0m'


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('archivos', nargs='+', help='TXT del backtest original')
    p.add_argument('--forzar', action='store_true',
                   help='Sobreescribe un registro previo (rompe la cadena de custodia)')
    p.add_argument('--canon', help='Ruta al canon (default: docs/canon_florencia.json)')
    args = p.parse_args()

    from app.services.vigia_service import sha256_archivo, CANON_PATH
    ruta = args.canon or CANON_PATH

    with open(ruta, encoding='utf-8') as f:
        canon = json.load(f)

    if canon.get('insumos', {}).get('registrado') and not args.forzar:
        print(f'\n  {ROJO}El canon ya tiene insumos registrados.{FIN}')
        for a in canon['insumos']['archivos']:
            print(f"    {GRIS}{a['archivo']}  {a['sha256'][:16]}...{FIN}")
        print(f'\n  Usa --forzar solo si sabes por qué cambias la referencia.\n')
        return 1

    registrados = []
    for path in args.archivos:
        if not os.path.isfile(path):
            print(f'  {ROJO}No existe: {path}{FIN}')
            return 1
        h = sha256_archivo(path)
        registrados.append({
            'archivo': os.path.basename(path),
            'sha256': h,
            'bytes': os.path.getsize(path),
        })
        print(f'  {VERDE}✓{FIN} {os.path.basename(path)}  {GRIS}{h[:16]}...{FIN}')

    canon['insumos'] = {
        '_nota': canon.get('insumos', {}).get('_nota'),
        'registrado': True,
        'archivos': registrados,
    }

    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(canon, f, indent=2, ensure_ascii=False)
        f.write('\n')

    print(f'\n  {VERDE}Registrados {len(registrados)} insumos en {ruta}{FIN}')
    print(f'  {GRIS}Commitea el canon: la cadena de custodia vive en el repo.{FIN}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
