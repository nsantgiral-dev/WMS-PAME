"""add serie_vigia + alarma_vigia tables

Revision ID: a7c3f82e9b14
Revises: 6849d59497dd
Create Date: 2026-07-24

CUSUM tabular bilateral para detección de corrimientos operativos.
Series semanales (despachos, facturación, facturas únicas por C.O.) +
alarmas con dos bandas (AVISO h=3.0 / ALARMA h=4.5) y regla anti-silencio.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a7c3f82e9b14'
down_revision = '6849d59497dd'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'serie_vigia',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('serie', sa.String(50), nullable=False),
        sa.Column('semana', sa.Date(), nullable=False),
        sa.Column('valor', sa.Numeric(14, 2), nullable=False),
        sa.Column('registros', sa.Integer(), default=0),
    )
    op.create_index('ix_serie_vigia_serie_semana', 'serie_vigia',
                    ['serie', 'semana'], unique=True)

    op.create_table(
        'alarma_vigia',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('serie', sa.String(50), nullable=False),
        sa.Column('semana', sa.Date(), nullable=False),
        sa.Column('tipo', sa.String(10), nullable=False),
        sa.Column('severidad', sa.String(10), nullable=False,
                  server_default='ALARMA'),
        sa.Column('s_valor', sa.Numeric(10, 4), nullable=False),
        sa.Column('mu_ref', sa.Numeric(14, 2), nullable=True),
        sa.Column('sigma_ref', sa.Numeric(14, 2), nullable=True),
        sa.Column('causa', sa.Text(), nullable=True),
        sa.Column('responsable_id', sa.Integer(),
                  sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('cerrada', sa.Boolean(), default=False),
        sa.Column('fecha_cierre', sa.DateTime(), nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(),
                  server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table('alarma_vigia')
    op.drop_index('ix_serie_vigia_serie_semana', table_name='serie_vigia')
    op.drop_table('serie_vigia')
