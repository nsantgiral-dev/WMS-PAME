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
desactiva explícitamente y se vuelve a activar en el mismo bloque — un borrado
declarado, no un rodeo silencioso.

Uso:
    python scripts/flota_limpiar_vehiculo.py --placa THP696            # simula
    python scripts/flota_limpiar_vehiculo.py --placa THP696 --ejecutar # borra
"""
import argparse
import os
import sys

VERDE, AMAR, ROJO, GRIS, FIN = (
    '\033[92m', '\033[93m', '\033[91m', '\033[90m', '\033[0m')


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
    from sqlalchemy import text

    from app import create_app
    from app.extensions import db

    placa = args.placa.strip().upper()
    app = create_app()
    with app.app_context():
        veh = db.session.execute(
            text('SELECT id, placa FROM vehiculos WHERE upper(placa) = :p'),
            {'p': placa}).fetchone()
        if veh is None:
            print(f'\n  {ROJO}No existe vehículo con placa {placa}.{FIN}\n')
            return 2
        vid = veh[0]

        print()
        print(f'  LIMPIEZA DE FLOTA — {veh[1]}')
        print('  ' + '─' * 54)
        if not args.ejecutar:
            print(f'  {AMAR}SIMULACRO — nada se borra. Usá --ejecutar.{FIN}')
        print()

        custodias = [r[0] for r in db.session.execute(text(
            'SELECT id FROM flota_custodia WHERE vehiculo_id = :v'), {'v': vid})]
        lecturas = [r[0] for r in db.session.execute(text(
            'SELECT id FROM flota_lectura_odometro WHERE vehiculo_id = :v'), {'v': vid})]

        # Las fotos cuelgan de una paternidad polimórfica: no hay FK que seguir,
        # se resuelve por (entidad_tipo, entidad_id). Por eso se enumeran los
        # padres primero — un `DELETE ... WHERE vehiculo_id` no existe acá.
        fotos = []
        if custodias:
            fotos += [r[0] for r in db.session.execute(text(
                "SELECT id FROM flota_foto WHERE entidad_tipo IN "
                "('custodia_inicio','custodia_fin') AND entidad_id = ANY(:ids)"
                if db.engine.dialect.name == 'postgresql' else
                "SELECT id FROM flota_foto WHERE entidad_tipo IN "
                "('custodia_inicio','custodia_fin') AND entidad_id IN "
                f"({','.join(str(i) for i in custodias)})"),
                {'ids': custodias} if db.engine.dialect.name == 'postgresql' else {})]
        if lecturas:
            fotos += [r[0] for r in db.session.execute(text(
                "SELECT id FROM flota_foto WHERE entidad_tipo = 'odometro' "
                "AND entidad_id = ANY(:ids)"
                if db.engine.dialect.name == 'postgresql' else
                "SELECT id FROM flota_foto WHERE entidad_tipo = 'odometro' "
                f"AND entidad_id IN ({','.join(str(i) for i in lecturas)})"),
                {'ids': lecturas} if db.engine.dialect.name == 'postgresql' else {})]

        print(f'    {len(fotos):>6}  fotos')
        print(f'    {len(custodias):>6}  custodias')
        print(f'    {len(lecturas):>6}  lecturas de odómetro')
        print(f'\n  {VERDE}Se conservan: ficha técnica y documentos de {veh[1]}.{FIN}')

        if not args.ejecutar:
            print(f'\n  {AMAR}Simulacro terminado. Nada se tocó.{FIN}\n')
            return 0

        def _borrar(tabla, ids):
            if not ids:
                return
            db.session.execute(text(f'DELETE FROM {tabla} WHERE id IN '
                                    f'({",".join(str(i) for i in ids)})'))

        es_pg = db.engine.dialect.name == 'postgresql'
        _borrar('flota_foto', fotos)
        _borrar('flota_custodia', custodias)

        # El trigger append-only bloquea DELETE en PostgreSQL. Se desactiva
        # DECLARADO y se restaura en el mismo bloque: si esto se rodeara en
        # silencio, la próxima vez nadie sabría que la tabla estaba protegida.
        if lecturas:
            if es_pg:
                db.session.execute(text(
                    'ALTER TABLE flota_lectura_odometro DISABLE TRIGGER USER'))
            try:
                _borrar('flota_lectura_odometro', lecturas)
            finally:
                if es_pg:
                    db.session.execute(text(
                        'ALTER TABLE flota_lectura_odometro ENABLE TRIGGER USER'))

        db.session.commit()
        print(f'\n  {VERDE}Borrado.{FIN}')

        # Verificación: contar de nuevo. Un borrado que no se verifica es una
        # afirmación, no un hecho.
        quedan = db.session.execute(text(
            'SELECT count(*) FROM flota_custodia WHERE vehiculo_id = :v'),
            {'v': vid}).scalar()
        quedan_l = db.session.execute(text(
            'SELECT count(*) FROM flota_lectura_odometro WHERE vehiculo_id = :v'),
            {'v': vid}).scalar()
        print(f'  Quedan: {quedan} custodias, {quedan_l} lecturas.')
        if quedan or quedan_l:
            print(f'  {ROJO}No quedó limpio — revisá el trigger append-only.{FIN}\n')
            return 1

        if args.fotos:
            from scripts.reset_transaccional import _limpiar_fotos_huerfanas
            print(f'\n  {VERDE}Archivos huérfanos:{FIN} '
                  f'{_limpiar_fotos_huerfanas(db)}')
        print()
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
