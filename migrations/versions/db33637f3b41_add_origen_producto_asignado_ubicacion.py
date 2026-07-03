"""add origen and producto_asignado_id to ubicaciones

Revision ID: db33637f3b41
Revises: d2e3f4g5h6i7
Create Date: 2026-07-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'db33637f3b41'
down_revision = 'd2e3f4g5h6i7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'ubicaciones',
        sa.Column('origen', sa.String(10), nullable=False, server_default='MANUAL'),
    )
    op.add_column(
        'ubicaciones',
        sa.Column('producto_asignado_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_ubicaciones_producto_asignado',
        'ubicaciones', 'productos',
        ['producto_asignado_id'], ['id'],
    )
    op.create_index(
        'ix_ubicaciones_producto_asignado',
        'ubicaciones', ['producto_asignado_id'],
        unique=False,
    )


def downgrade():
    op.drop_index('ix_ubicaciones_producto_asignado', table_name='ubicaciones')
    op.drop_constraint('fk_ubicaciones_producto_asignado', 'ubicaciones', type_='foreignkey')
    op.drop_column('ubicaciones', 'producto_asignado_id')
    op.drop_column('ubicaciones', 'origen')
