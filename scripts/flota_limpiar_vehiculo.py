#!/usr/bin/env python
"""
Borra el rastro de flota de UN vehículo. Nada más.

Por qué existe y no se usa `reset_transaccional.py`:

    Ese script es el **acta de corte** — se corre una vez, el día del go-live, y
    vacía TODA la operación: picking, packing, recepciones, rutas de despacho,
    movimientos de inventario, jobs de Siesa, pedidos. Usarlo para borrar diez
    custodias de prueba de un vehículo se lleva por delante semanas de ensayo de
    los otros módulos.

    Además, sin `--ejecutar` sale en el simulacro antes de tocar nada, así que
    el comando "para limpiar el THP696" no limpia nada; y con `--ejecutar`
    limpia mucho más de lo que se pidió. Las dos formas de equivocarse.

Qué NO toca, nunca:

    · `flota_ficha_tecnica` y `flota_documento_vehiculo` — el levantamiento de
      campo. Media mañana recorriendo vehículos con la foto del tablero y la
      medida de llanta en la mano. Borrarlo obliga a hacerlo dos veces, y la
      segunda nadie la hace.
    · Cualquier tabla que no sea de flota.
    · Cualquier vehículo que no sea el indicado.

Orden de borrado, y no es intercambiable:

    fotos → custodias → lecturas → archivos del volumen

    Los archivos van **al final**: borrarlos antes dejaría filas apuntando a
    nada, que es peor que un archivo de más — un hueco silencioso contra disco
    ocupado. La misma regla que ya está escrita en `reset_transaccional`.

El trigger append-only de `flota_lectura_odometro` bloquea DELETE en PostgreSQL
a propósito: una lectura no se edita ni se borra, se corrige con otra. Acá se
desactiva explícitamente y se restaura en un `finally` — un borrado declarado,
no un rodeo silencioso. Si quedara desactivado, la tabla dejaría de estar
protegida para siempre y nadie se enteraría.

Uso:
    python scripts/flota_limpiar_vehiculo.py --placa THP696            # simula
    python scripts/flota_limpiar_vehiculo.py --placa THP696 --ejecutar # borra
"""
import argparse
import os
import sys

VERDE, AMAR, ROJO, GRIS, FIN = (
    '\033[92m', '\033[93m', '\033[91m', '\033[90m', '\033[0m')


class VehiculoNoExiste(Exception):
    """La placa no está en el maestro. No se inventa un id."""


def _ids(db, sql, **params):
    from sqlalchemy import text
    return [r[0] for r in db.session.execute(text(sql), params)]


def _en(ids):
    """Lista para un `IN (...)`. Solo enteros — vienen de la propia base."""
    return ','.join(str(int(i)) for i in ids)


def contar(db, placa):
    """Qué hay que borrar de este vehículo. No borra nada.

    Devuelve `(vehiculo_id, {'fotos': [...], 'custodias': [...], 'lecturas': [...]})`.

    Las fotos cuelgan de una paternidad polimórfica: no hay FK que seguir, se
    resuelven por `(entidad_tipo, entidad_id)`. Por eso se enumeran los padres
    primero — un `DELETE ... WHERE vehiculo_id` no existe para esa tabla.
    """
    fila = db.session.execute(
        __import__('sqlalchemy').text(
            'SELECT id FROM vehiculos WHERE upper(placa) = :p'),
        {'p': (placa or '').strip().upper()}).fetchone()
    if fila is None:
        raise VehiculoNoExiste(f'No existe vehículo con placa {placa}')
    vid = fila[0]

    custodias = _ids(db, 'SELECT id FROM flota_custodia WHERE vehiculo_id = :v', v=vid)
    lecturas = _ids(db, 'SELECT id FROM flota_lectura_odometro WHERE vehiculo_id = :v', v=vid)

    fotos = []
    if custodias:
        fotos += _ids(db,
                      "SELECT id FROM flota_foto WHERE entidad_tipo IN "
                      "('custodia_inicio','custodia_fin') AND entidad_id IN "
                      f"({_en(custodias)})")
    if lecturas:
        fotos += _ids(db,
                      "SELECT id FROM flota_foto WHERE entidad_tipo = 'odometro' "
                      f"AND entidad_id IN ({_en(lecturas)})")

    return vid, {'fotos': fotos, 'custodias': custodias, 'lecturas': lecturas}


def borrar(db, vid, objetivo):
    """Ejecuta el borrado en el orden que no deja referencias colgadas.

    El trigger append-only se desactiva DECLARADO y se restaura en `finally`:
    si una excepción lo dejara apagado, `flota_lectura_odometro` quedaría sin su
    invariante y el próximo `DELETE` accidental no encontraría resistencia.
    """
    from sqlalchemy import text

    def _borrar(tabla, ids):
        if ids:
            db.session.execute(text(f'DELETE FROM {tabla} WHERE id IN ({_en(ids)})'))

    es_pg = db.engine.dialect.name == 'postgresql'
    _borrar('flota_foto', objetivo['fotos'])
    _borrar('flota_custodia', objetivo['custodias'])

    if objetivo['lecturas']:
        if es_pg:
            db.session.execute(text(
                'ALTER TABLE flota_lectura_odometro DISABLE TRIGGER USER'))
        try:
            _borrar('flota_lectura_odometro', objetivo['lecturas'])
        finally:
            if es_pg:
                db.session.execute(text(
                    'ALTER TABLE flota_lectura_odometro ENABLE TRIGGER USER'))
    db.session.commit()


def verificar(db, vid):
    """Cuenta de nuevo. Un borrado que no se verifica es una afirmación."""
    from sqlalchemy import text

    c = db.session.execute(text(
        'SELECT count(*) FROM flota_custodia WHERE vehiculo_id = :v'), {'v': vid}).scalar()
    l = db.session.execute(text(
        'SELECT count(*) FROM flota_lectura_odometro WHERE vehiculo_id = :v'), {'v': vid}).scalar()
    return {'custodias': c, 'lecturas': l}


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--placa', required=True, help='Placa del vehículo a limpiar.')
    p.add_argument('--ejecutar', action='store_true',
                   help='Borra de verdad. Sin esto solo cuenta.')
    p.add_argument('--fotos', action='store_true',
                   help='Borra también los archivos que queden huérfanos.')
    args = p.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app import create_app
    from app.extensions import db

    app = create_app()
    with app.app_context():
        try:
            vid, objetivo = contar(db, args.placa)
        except VehiculoNoExiste as e:
            print(f'\n  {ROJO}{e}{FIN}\n')
            return 2

        print()
        print(f'  LIMPIEZA DE FLOTA — {args.placa.strip().upper()}')
        print('  ' + '─' * 54)
        if not args.ejecutar:
            print(f'  {AMAR}SIMULACRO — nada se borra. Usá --ejecutar.{FIN}')
        print()
        print(f'    {len(objetivo["fotos"]):>6}  fotos')
        print(f'    {len(objetivo["custodias"]):>6}  custodias')
        print(f'    {len(objetivo["lecturas"]):>6}  lecturas de odómetro')
        print(f'\n  {VERDE}Se conservan: ficha técnica y documentos.{FIN}')

        if not args.ejecutar:
            print(f'\n  {AMAR}Simulacro terminado. Nada se tocó.{FIN}\n')
            return 0

        borrar(db, vid, objetivo)
        quedan = verificar(db, vid)
        print(f'\n  {VERDE}Borrado.{FIN}')
        print(f'  Quedan: {quedan["custodias"]} custodias, {quedan["lecturas"]} lecturas.')
        if quedan['custodias'] or quedan['lecturas']:
            print(f'  {ROJO}No quedó limpio — revisá el trigger append-only.{FIN}\n')
            return 1

        if args.fotos:
            from scripts.reset_transaccional import _limpiar_fotos_huerfanas
            print(f'\n  {VERDE}Archivos huérfanos:{FIN} {_limpiar_fotos_huerfanas(db)}')
        print()
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
