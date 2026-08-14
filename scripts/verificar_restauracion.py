#!/usr/bin/env python
"""
Verifica que un respaldo restaurado sirve. **Solo lee.**

Un respaldo que existe no es un respaldo: es un archivo. Lo que hace falta
saber es si, cuando haga falta, se puede volver a operar desde él — y eso solo
se sabe restaurándolo una vez y mirándolo.

Este script es la segunda mitad de esa prueba. La primera —restaurar en una
base NUEVA, nunca encima de producción— se hace en Railway.

## Cómo se usa

    # 1. Foto de producción (solo lectura, no toca nada)
    venv/bin/python scripts/verificar_restauracion.py --foto produccion.json

    # 2. Restaurar el respaldo en una base APARTE (en Railway)

    # 3. Comparar la copia contra la foto
    DATABASE_URL='postgresql://…copia…' \\
        venv/bin/python scripts/verificar_restauracion.py --contra produccion.json

## Qué se considera «restauración probada»

1. **Todas las tablas están** — una que falte se ve como cero filas, y cero
   filas se lee igual que «esa parte del negocio no se usó».
2. **Las filas cuadran.** Se tolera que la copia tenga MENOS en tablas
   operativas —el respaldo es de un momento anterior— pero no que falten en
   las analíticas ni en las maestras: ésas no se regeneran.
3. **La cabeza de migraciones coincide.** Una copia en otra revisión no la
   levanta la app: `flask db upgrade` puede fallar o migrar a ciegas.
4. **La memoria analítica no está vacía.** Es la única parte irrecuperable —
   sin las 26 semanas de referencia el CUSUM queda ciego ~6 meses.

Lo que este script NO prueba: que la aplicación arranque contra la copia. Eso
es apuntar `DATABASE_URL` a la copia y abrir `/api/health/ping`. Vale la pena
hacerlo — un esquema íntegro con la app caída sigue siendo una noche perdida.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERDE, ROJO, AMAR, GRIS, FIN = '\033[92m', '\033[91m', '\033[93m', '\033[90m', '\033[0m'

#: Sin esto, la copia arranca pero el negocio pierde la memoria. Coinciden con
#: `PROTEGIDAS_ANALITICAS` del reset — el corte no las toca y una restauración
#: tampoco las puede perder.
IRRECUPERABLES = (
    'serie_vigia', 'alarma_vigia', 'kardex_movimientos', 'stock_diario',
    'juicios_temporada', 'precios_realizados',
)

#: Se pueden volver a cargar desde Siesa. Que la copia tenga menos no es un
#: fallo del respaldo.
REGENERABLES = ('pedidos_siesa', 'stock_siesa', 'siesa_jobs',
                'movimientos_inventario', 'ubicaciones_huerfanas')


def _destino():
    from urllib.parse import urlparse
    url = os.getenv('DATABASE_URL', '')
    if not url:
        from app import create_app
        url = create_app().config.get('SQLALCHEMY_DATABASE_URI', '')
    u = urlparse(url)
    return u.hostname or 'local', (u.path or '').lstrip('/') or '(archivo)'


def _radiografia():
    """`{tablas: {nombre: filas}, head: str}` — todo por lectura."""
    from sqlalchemy import text

    from app import create_app
    from app.extensions import db

    app = create_app()
    with app.app_context():
        tablas = {}
        for nombre in sorted(db.metadata.tables):
            try:
                tablas[nombre] = db.session.execute(
                    text(f'SELECT count(*) FROM {nombre}')).scalar()
            except Exception:
                tablas[nombre] = None      # None = la tabla NO existe
        try:
            head = db.session.execute(
                text('SELECT version_num FROM alembic_version')).scalar()
        except Exception:
            head = None
    return {'tablas': tablas, 'head': head}


def _comparar(foto: dict, copia: dict) -> int:
    problemas, avisos = [], []

    if foto.get('head') != copia.get('head'):
        problemas.append(
            f'cabeza de migraciones distinta: la foto dice {foto.get("head")!r} '
            f'y la copia {copia.get("head")!r} — la app no levanta sobre eso')

    for nombre, n_foto in sorted(foto['tablas'].items()):
        n_copia = copia['tablas'].get(nombre)
        if n_copia is None:
            problemas.append(f'{nombre}: NO EXISTE en la copia')
            continue
        if n_foto is None:
            continue
        if n_copia == n_foto:
            continue
        falta = n_foto - n_copia
        if falta <= 0:
            avisos.append(f'{nombre}: la copia tiene {-falta} fila(s) MÁS '
                          f'(el respaldo es posterior a la foto)')
        elif nombre in IRRECUPERABLES:
            problemas.append(f'{nombre}: faltan {falta} de {n_foto} — '
                             f'IRRECUPERABLE, no se regenera desde Siesa')
        elif nombre in REGENERABLES:
            avisos.append(f'{nombre}: faltan {falta} de {n_foto} '
                          f'(se recarga desde Siesa)')
        else:
            problemas.append(f'{nombre}: faltan {falta} de {n_foto}')

    for nombre in IRRECUPERABLES:
        if copia['tablas'].get(nombre) == 0 and foto['tablas'].get(nombre):
            problemas.append(f'{nombre}: VACÍA en la copia — es la memoria que '
                             f'no se puede reconstruir')

    print()
    if avisos:
        print(f'  {AMAR}Diferencias tolerables:{FIN}')
        for a in avisos:
            print(f'    · {a}')
        print()
    if problemas:
        print(f'  {ROJO}La copia NO sirve para volver a operar:{FIN}')
        for p in problemas:
            print(f'    · {p}')
        print(f'\n  {GRIS}Un respaldo que no restaura es un archivo. '
              f'Arreglar esto ANTES del corte.{FIN}\n')
        return 1

    print(f'  {VERDE}RESTAURACIÓN VERIFICADA{FIN} — la copia tiene todo lo que '
          f'la foto tenía.')
    print(f'  {GRIS}Falta un paso que este script no hace: apuntar la app a la '
          f'copia y abrir /api/health/ping.\n'
          f'  Un esquema íntegro con la app caída sigue siendo una noche '
          f'perdida.{FIN}\n')
    return 0


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--foto', metavar='ARCHIVO',
                   help='Guarda la radiografía de la base actual. Solo lee.')
    p.add_argument('--contra', metavar='ARCHIVO',
                   help='Compara la base actual contra una foto previa.')
    args = p.parse_args()

    host, base = _destino()
    print()
    print('  VERIFICACIÓN DE RESTAURACIÓN')
    print('  ' + '─' * 54)
    print(f'  {AMAR}Leyendo:{FIN} {host} · base {base}')
    print(f'  {GRIS}Este script NO escribe nada.{FIN}')

    if not args.foto and not args.contra:
        p.error('elegí --foto (guardar) o --contra (comparar)')

    actual = _radiografia()
    total = sum(v for v in actual['tablas'].values() if v)
    print(f'  {len(actual["tablas"])} tablas · {total:,} filas · '
          f'head {actual["head"]}')

    if args.foto:
        with open(args.foto, 'w') as f:
            json.dump(actual, f, indent=2)
        print(f'\n  {VERDE}Foto guardada en {args.foto}{FIN}')
        print(f'  {GRIS}Ahora restaurá el respaldo en una base APARTE y corré:\n'
              f"    DATABASE_URL='…copia…' {sys.argv[0]} --contra {args.foto}{FIN}\n")
        return 0

    with open(args.contra) as f:
        foto = json.load(f)
    return _comparar(foto, actual)


if __name__ == '__main__':
    sys.exit(main())
