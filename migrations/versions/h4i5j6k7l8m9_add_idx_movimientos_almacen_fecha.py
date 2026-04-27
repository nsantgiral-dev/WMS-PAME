"""add index on movimientos_inventario(almacen_id, fecha DESC)

Revision ID: h4i5j6k7l8m9
Revises: g3h4i5j6k7l8
Create Date: 2026-04-27 00:00:00.000000

"""
from alembic import op

revision = 'h4i5j6k7l8m9'
down_revision = 'g3h4i5j6k7l8'
branch_labels = None
depends_on = None


def upgrade():
    # CONCURRENTLY eliminado: no puede correr dentro de la transacción de Alembic.
    # Durante el deploy no hay tráfico, el lock breve es aceptable.
    op.execute(
        'CREATE INDEX IF NOT EXISTS '
        'idx_movimientos_almacen_fecha '
        'ON movimientos_inventario (almacen_id, fecha DESC)'
    )


def downgrade():
    op.execute('DROP INDEX IF EXISTS idx_movimientos_almacen_fecha')
