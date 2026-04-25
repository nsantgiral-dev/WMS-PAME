"""idx productos codigo_siesa

Revision ID: c2d3e4f5g6h7
Revises: b1c2d3e4f5g6
Create Date: 2026-04-25 00:00:00.000000

"""
from alembic import op

revision = 'c2d3e4f5g6h7'
down_revision = 'b1c2d3e4f5g6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_productos_codigo_siesa', 'productos', ['codigo_siesa'])


def downgrade():
    op.drop_index('ix_productos_codigo_siesa', table_name='productos')
