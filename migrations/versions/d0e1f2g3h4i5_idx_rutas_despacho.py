"""idx rutas_despacho conductor_id, estado, fecha_creacion

Revision ID: d0e1f2g3h4i5
Revises: c2d3e4f5g6h7
Create Date: 2026-04-25 00:00:00.000000

"""
from alembic import op

revision = 'd0e1f2g3h4i5'
down_revision = 'c2d3e4f5g6h7'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rutas_despacho_conductor_id "
        "ON rutas_despacho (conductor_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rutas_despacho_estado "
        "ON rutas_despacho (estado)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rutas_despacho_fecha_creacion "
        "ON rutas_despacho (fecha_creacion DESC)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_rutas_despacho_conductor_estado "
        "ON rutas_despacho (conductor_id, estado)"
    )


def downgrade():
    op.drop_index('ix_rutas_despacho_conductor_estado', table_name='rutas_despacho')
    op.drop_index('ix_rutas_despacho_fecha_creacion', table_name='rutas_despacho')
    op.drop_index('ix_rutas_despacho_estado', table_name='rutas_despacho')
    op.drop_index('ix_rutas_despacho_conductor_id', table_name='rutas_despacho')
