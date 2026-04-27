"""idx bultos ruta_despacho_id+estado para queries de cargue y sugeridos

Revision ID: e1f2g3h4i5j6
Revises: d0e1f2g3h4i5
Create Date: 2026-04-25 00:00:00.000000

"""
from alembic import op

revision = 'e1f2g3h4i5j6'
down_revision = 'd0e1f2g3h4i5'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_bultos_ruta_estado "
        "ON bultos (ruta_despacho_id, estado)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_bultos_tarea_id "
        "ON bultos (tarea_id)"
    )


def downgrade():
    op.drop_index('ix_bultos_tarea_id', table_name='bultos')
    op.drop_index('ix_bultos_ruta_estado', table_name='bultos')
