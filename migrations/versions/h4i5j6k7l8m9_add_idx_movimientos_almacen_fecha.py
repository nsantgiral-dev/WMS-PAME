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
    # dashboard_service.movimientos_recientes() filtra por almacen_id y ordena por fecha DESC.
    # Sin índice, cada llamada hace seq-scan sobre toda la tabla (crece con cada movimiento).
    # CONCURRENTLY: no bloquea writes durante la construcción del índice.
    op.execute(
        'CREATE INDEX CONCURRENTLY IF NOT EXISTS '
        'idx_movimientos_almacen_fecha '
        'ON movimientos_inventario (almacen_id, fecha DESC)'
    )


def downgrade():
    op.execute('DROP INDEX IF EXISTS idx_movimientos_almacen_fecha')
