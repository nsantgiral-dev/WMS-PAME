"""La demanda de compras calculada por el worker (tanda G)

Revision ID: m052comprasg
Revises: m051comprasf
Create Date: 2026-09-29

Aditiva, sin backfill: la primera fila la escribe el primer recálculo. Ver
`app/models/compras_rop.py` y CLAUDE.md «La validación de B/E/F y la tanda G».
"""
import sqlalchemy as sa
from alembic import op

revision = 'm052comprasg'
down_revision = 'm051comprasf'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'compras_rop_calculo',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('nivel_servicio', sa.Numeric(6, 4), nullable=False),
        sa.Column('dia', sa.Date(), nullable=True),
        sa.Column('sello', sa.Text(), nullable=True),
        sa.Column('calculado_en', sa.DateTime(), nullable=True),
        sa.Column('duracion_s', sa.Float(), nullable=True),
        sa.Column('skus', sa.Integer(), nullable=True),
        sa.Column('demanda', sa.Text(), nullable=True),
        sa.Column('error', sa.String(500), nullable=True),
        sa.Column('error_en', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('nivel_servicio', name='uq_compras_rop_calculo_nivel'),
    )


def downgrade():
    op.drop_table('compras_rop_calculo')
