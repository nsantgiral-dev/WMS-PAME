"""add kardex_movimientos + stock_diario tables

Revision ID: 6849d59497dd
Revises: b21ee491624d
Create Date: 2026-07-23

Kardex transaccional de Siesa (T470+T350+T120) para reconstrucción
de stock diario y cálculo de velocity censurada.
"""
from alembic import op
import sqlalchemy as sa

revision = '6849d59497dd'
down_revision = 'b21ee491624d'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'kardex_movimientos',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('fecha', sa.Date, nullable=False),
        sa.Column('tipo_docto', sa.String(10)),
        sa.Column('bodega', sa.String(10), nullable=False),
        sa.Column('referencia', sa.String(30), nullable=False),
        sa.Column('concepto', sa.Integer, nullable=False),
        sa.Column('naturaleza', sa.Integer, nullable=False),
        sa.Column('cantidad', sa.Numeric(14, 4), nullable=False),
        sa.Column('costo_promedio', sa.Numeric(14, 4), default=0),
        sa.Column('descargado_en', sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index('ix_kardex_fecha', 'kardex_movimientos', ['fecha'])
    op.create_index('ix_kardex_bodega', 'kardex_movimientos', ['bodega'])
    op.create_index('ix_kardex_referencia', 'kardex_movimientos', ['referencia'])
    op.create_index('ix_kardex_ref_bod_fecha', 'kardex_movimientos',
                     ['referencia', 'bodega', 'fecha'])

    op.create_table(
        'stock_diario',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('referencia', sa.String(30), nullable=False),
        sa.Column('bodega', sa.String(10), nullable=False),
        sa.Column('fecha', sa.Date, nullable=False),
        sa.Column('stock_cierre', sa.Numeric(14, 4), default=0),
        sa.Column('tuvo_stock', sa.Boolean, default=False),
    )
    op.create_index('ix_stock_diario_ref_bod', 'stock_diario',
                     ['referencia', 'bodega', 'fecha'], unique=True)


def downgrade():
    op.drop_table('stock_diario')
    op.drop_table('kardex_movimientos')
