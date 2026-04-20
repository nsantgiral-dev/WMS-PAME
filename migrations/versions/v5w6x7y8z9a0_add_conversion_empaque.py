"""add conversion empaque a productos e items_recepcion

Revision ID: v5w6x7y8z9a0
Revises: u4v5w6x7y8z9
Create Date: 2026-04-20
"""
from alembic import op
import sqlalchemy as sa

revision = 'v5w6x7y8z9a0'
down_revision = 'u4v5w6x7y8z9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('productos', sa.Column('codigo_barras_empaque', sa.String(100), nullable=True))
    op.add_column('productos', sa.Column('unidad_empaque', sa.String(20), nullable=True))
    op.add_column('productos', sa.Column('factor_conversion', sa.Integer(), nullable=True, server_default='1'))
    op.add_column('items_recepcion', sa.Column('empaques_escaneados', sa.Integer(), nullable=True, server_default='0'))


def downgrade():
    op.drop_column('items_recepcion', 'empaques_escaneados')
    op.drop_column('productos', 'factor_conversion')
    op.drop_column('productos', 'unidad_empaque')
    op.drop_column('productos', 'codigo_barras_empaque')
