"""add codigo_barras to productos

Revision ID: 27fbcf49ac17
Revises: a1b2c3d4e5f6
Create Date: 2026-04-15
"""
from alembic import op
import sqlalchemy as sa

revision = '27fbcf49ac17'
down_revision = 'q0r1s2t3u4v5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('productos', sa.Column('codigo_barras', sa.String(100), nullable=True))
    op.create_index('ix_productos_codigo_barras', 'productos', ['codigo_barras'], unique=False)


def downgrade():
    op.drop_index('ix_productos_codigo_barras', table_name='productos')
    op.drop_column('productos', 'codigo_barras')
