"""add stock_siesa table — snapshot persistente de inventario Siesa por bodega

Revision ID: d2e3f4g5h6i7
Revises: c1d2e3f4g5h6
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = 'd2e3f4g5h6i7'
down_revision = 'c1d2e3f4g5h6'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'stock_siesa',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bodega', sa.String(20), nullable=False, index=True),
        sa.Column('codigo_siesa', sa.String(60), nullable=False),
        sa.Column('existencia', sa.Float(), server_default='0'),
        sa.Column('comprometido', sa.Float(), server_default='0'),
        sa.Column('salida_sin_conf', sa.Float(), server_default='0'),
        sa.Column('descripcion', sa.String(200), server_default=''),
        sa.Column('unidad_medida', sa.String(10), server_default='UND'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('bodega', 'codigo_siesa', name='uq_stock_siesa_bodega_codigo'),
    )


def downgrade():
    op.drop_table('stock_siesa')
