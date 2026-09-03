"""Emite el cuerpo de una migración DESDE `db.metadata`, no a mano.

    PYTHONPATH=. venv/bin/python scripts/emitir_migracion.py tabla_a tabla_b

Por qué existe: una transcripción manual de N columnas y M CHECK es donde se
pierde un constraint sin que nadie lo note, y un invariante que la base no
impone es una sugerencia. Es la misma técnica con la que se emitieron
`f10ta1cimientos` y `m017flotahallazgo`.

Lo que este script NO hace, y hay que hacerlo a mano después:
  · encadenar `down_revision` — el head único no es opcional, `releaseCommand`
    corre `flask db upgrade` en cada deploy y con dos heads el release falla;
  · los triggers de PostgreSQL, que ningún CHECK puede reemplazar porque miran
    otras filas;
  · los ALTER sobre tablas que ya existen (ensanchar un CHECK, agregar una
    columna): `create_table` no los cubre y el metadata no sabe qué había antes.
"""
import os
import sys

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ.setdefault('SYNC_SCHEDULER', 'false')
os.environ.setdefault('SECRET_KEY', 'x-de-32-bytes-o-mas-para-hmac-sha256-ok')

import sqlalchemy as sa
from alembic.autogenerate import api
from alembic.migration import MigrationContext
from alembic.operations import ops

from app import create_app
from app.extensions import db


def main(nombres):
    app = create_app()
    with app.app_context():
        import flota.adaptadores.modelos  # noqa: F401  registra las tablas
        faltan = [n for n in nombres if n not in db.metadata.tables]
        if faltan:
            # Regla 5: o funciona, o falla ruidosamente. Emitir una migración a
            # medias es peor que no emitirla — se despliega y falta una tabla.
            sys.exit(f'ABORTA: no están en metadata: {faltan}')

        operaciones = []
        for n in nombres:
            tabla = db.metadata.tables[n]
            operaciones.append(ops.CreateTableOp.from_table(tabla))
            for ix in sorted(tabla.indexes, key=lambda i: i.name or ''):
                operaciones.append(ops.CreateIndexOp.from_index(ix))

        motor = sa.create_engine('sqlite://')
        with motor.connect() as conn:
            ctx = MigrationContext.configure(conn)
            print(api.render_python_code(ops.UpgradeOps(ops=operaciones),
                                         migration_context=ctx))
        print('\n# ── downgrade, en orden inverso ──')
        for n in reversed(nombres):
            print(f"    op.drop_table('{n}')")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1:])
