"""add conductores y rutas_despacho — modulo milla cero

Revision ID: a1b2c3d4e5f6
Revises: da4f2d7948b4
Create Date: 2026-04-01 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'da4f2d7948b4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'conductores',
        sa.Column('id',             sa.Integer(),     nullable=False),
        sa.Column('nombre',         sa.String(100),   nullable=False),
        sa.Column('cedula',         sa.String(20),    nullable=False),
        sa.Column('telefono',       sa.String(20),    nullable=True),
        sa.Column('placa',          sa.String(10),    nullable=True),
        sa.Column('activo',         sa.Boolean(),     nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(),    nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cedula')
    )

    op.create_table(
        'rutas_despacho',
        sa.Column('id',               sa.Integer(),   nullable=False),
        sa.Column('conductor_id',     sa.Integer(),   nullable=False),
        sa.Column('tipo_ruta',        sa.String(20),  nullable=False),
        sa.Column('estado',           sa.String(20),  nullable=True),
        sa.Column('notas',            sa.Text(),      nullable=True),
        sa.Column('fecha_creacion',   sa.DateTime(),  nullable=True),
        sa.Column('fecha_cierre',     sa.DateTime(),  nullable=True),
        sa.Column('fecha_entregada',  sa.DateTime(),  nullable=True),
        sa.ForeignKeyConstraint(['conductor_id'], ['conductores.id']),
        sa.PrimaryKeyConstraint('id')
    )

    op.add_column(
        'bultos',
        sa.Column('ruta_despacho_id', sa.Integer(), sa.ForeignKey('rutas_despacho.id'), nullable=True)
    )


def downgrade():
    op.drop_column('bultos', 'ruta_despacho_id')
    op.drop_table('rutas_despacho')
    op.drop_table('conductores')
