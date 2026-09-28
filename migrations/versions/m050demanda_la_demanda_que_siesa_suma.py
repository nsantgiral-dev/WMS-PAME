"""La venta diaria que Siesa ya sumó, y los días que una lectura cubrió

Revision ID: m050demanda
Revises: m049tardia
Create Date: 2026-09-27

Aditiva, sin backfill. Ver `app/models/demanda_siesa.py` y CLAUDE.md
«Compras: de dónde sale la demanda (2026-09-27)».
"""
import sqlalchemy as sa
from alembic import op

revision = 'm050demanda'
down_revision = 'm050inv2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'demanda_dia_siesa',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('fecha', sa.Date(), nullable=False),
        sa.Column('bodega', sa.String(10), nullable=False),
        sa.Column('referencia', sa.String(50), nullable=False),
        sa.Column('vendido', sa.Numeric(16, 4), nullable=False),
        sa.Column('devuelto', sa.Numeric(16, 4), nullable=False),
        sa.Column('lineas', sa.Integer(), nullable=True),
        sa.Column('registro_id', sa.Integer(), nullable=True),
        sa.Column('actualizada_en', sa.DateTime(), nullable=False),
    )
    op.create_index('uq_demanda_dia_siesa', 'demanda_dia_siesa',
                    ['fecha', 'bodega', 'referencia'], unique=True)
    op.create_index('ix_demanda_dia_siesa_ref_fecha', 'demanda_dia_siesa',
                    ['referencia', 'fecha'])
    op.create_table(
        'demanda_dia_cubierto',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('fecha', sa.Date(), nullable=False),
        sa.Column('registro_id', sa.Integer(), nullable=True),
        sa.Column('consulta', sa.String(120), nullable=True),
        sa.Column('leido_en', sa.DateTime(), nullable=False),
    )
    op.create_index('uq_demanda_dia_cubierto_fecha', 'demanda_dia_cubierto',
                    ['fecha'], unique=True)


def downgrade():
    op.drop_index('uq_demanda_dia_cubierto_fecha', table_name='demanda_dia_cubierto')
    op.drop_table('demanda_dia_cubierto')
    op.drop_index('ix_demanda_dia_siesa_ref_fecha', table_name='demanda_dia_siesa')
    op.drop_index('uq_demanda_dia_siesa', table_name='demanda_dia_siesa')
    op.drop_table('demanda_dia_siesa')
