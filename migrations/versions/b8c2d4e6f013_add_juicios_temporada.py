"""add juicios_temporada — la lista paralela de la fundadora, persistida

Revision ID: b8c2d4e6f013
Revises: d3f7a1c5e8b2
Create Date: 2026-07-25

Vivía en localStorage. Un juicio humano registrado no es una estimación
volátil: es el dato que en enero de 2027 permite comparar lo que ella pidió,
lo que el modelo pidió y lo que se vendió — el único backtest que puede
validar el modelo ante quien decide.

Guarda también el Q* del modelo en el momento del juicio: comparar después
contra un modelo que ya cambió no probaría nada.
"""
from alembic import op
import sqlalchemy as sa

revision = 'b8c2d4e6f013'
down_revision = 'd3f7a1c5e8b2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'juicios_temporada',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('temporada', sa.String(10), nullable=False),
        sa.Column('referencia', sa.String(30), nullable=False),
        sa.Column('cantidad_juicio', sa.Integer(), nullable=False),
        sa.Column('autor_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('autor_nombre', sa.String(120), nullable=True),
        sa.Column('nota', sa.Text(), nullable=True),
        sa.Column('q_modelo', sa.Integer(), nullable=True),
        sa.Column('costo_unitario', sa.Numeric(14, 2), nullable=True),
        sa.Column('distribucion', sa.String(60), nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(), nullable=True),
        sa.Column('fecha_actualizacion', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_juicio_temporada_ref', 'juicios_temporada',
                    ['temporada', 'referencia'], unique=True)


def downgrade():
    op.drop_index('ix_juicio_temporada_ref', table_name='juicios_temporada')
    op.drop_table('juicios_temporada')
