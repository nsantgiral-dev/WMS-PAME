"""add pedido_anulado_siesa to tareas_packing

Revision ID: a8b9c0d1e2f3
Revises: b0c1d2e3f4g5
Create Date: 2026-04-24

"""
from alembic import op
import sqlalchemy as sa

revision = 'a8b9c0d1e2f3'
down_revision = 'b0c1d2e3f4g5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tareas_packing',
        sa.Column('pedido_anulado_siesa', sa.Boolean(), nullable=True, server_default='false'))
    op.add_column('tareas_packing',
        sa.Column('pedido_estado_siesa_detectado', sa.String(10), nullable=True))


def downgrade():
    op.drop_column('tareas_packing', 'pedido_estado_siesa_detectado')
    op.drop_column('tareas_packing', 'pedido_anulado_siesa')
