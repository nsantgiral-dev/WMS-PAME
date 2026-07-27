"""precios_realizados — precio al que de verdad se vende, por SKU y C.O.

Revision ID: e7b2d5a83c14
Revises: c4a9e1f70b28
Create Date: 2026-07-27

No es una lista de precios: es valor/cantidad sobre las ventas reales del
export. Neto de descuentos y de la escalera clandestina — el margen que sale de
aquí es el que se obtiene, no el que se cree obtener.

Sustituye el margen SUPUESTO en Cu. Y de paso, la dispersión del precio de un
mismo SKU entre C.O. es la primera medición de la escalera de precios que
existe y nunca se decidió.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e7b2d5a83c14'
down_revision = 'c4a9e1f70b28'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'precios_realizados',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('referencia', sa.String(30), nullable=False),
        sa.Column('centro_operacion', sa.String(10), nullable=True),
        sa.Column('periodo', sa.String(20), nullable=False, server_default='TOTAL'),
        sa.Column('valor_total', sa.Numeric(16, 2), nullable=False, server_default='0'),
        sa.Column('cantidad_total', sa.Numeric(14, 4), nullable=False, server_default='0'),
        sa.Column('precio_realizado', sa.Numeric(14, 4), nullable=False, server_default='0'),
        sa.Column('lineas', sa.Integer(), nullable=True),
        sa.Column('fecha_carga', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_precio_realizado_ref_co_per', 'precios_realizados',
                    ['referencia', 'centro_operacion', 'periodo'], unique=True)


def downgrade():
    op.drop_index('ix_precio_realizado_ref_co_per', table_name='precios_realizados')
    op.drop_table('precios_realizados')
