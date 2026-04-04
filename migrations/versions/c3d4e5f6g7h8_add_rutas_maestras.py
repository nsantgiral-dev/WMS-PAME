"""add rutas_maestras — plantilla fija + programación de viajes

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-04-03 21:00:00.000000

Cambios:
- Crea tabla 'rutas_maestras' (catálogo fijo de rutas geográficas)
- Crea tabla 'rutas_maestras_paradas' (paradas en orden LIFO)
- Agrega 'ruta_maestra_id' y 'fecha_programada' a 'rutas_despacho'
- Estado PROGRAMADO incorporado al modelo (no requiere cambio DDL — es un valor)
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6g7h8'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'rutas_maestras',
        sa.Column('id',             sa.Integer(),    nullable=False),
        sa.Column('nombre',         sa.String(100),  nullable=False),
        sa.Column('tipo_ruta',      sa.String(20),   nullable=False),
        sa.Column('activa',         sa.Boolean(),    nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(),   nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('nombre'),
    )

    op.create_table(
        'rutas_maestras_paradas',
        sa.Column('id',              sa.Integer(),    nullable=False),
        sa.Column('ruta_maestra_id', sa.Integer(),    nullable=False),
        sa.Column('municipio',       sa.String(100),  nullable=False),
        sa.Column('orden',           sa.Integer(),    nullable=False),
        sa.ForeignKeyConstraint(['ruta_maestra_id'], ['rutas_maestras.id']),
        sa.PrimaryKeyConstraint('id'),
    )

    op.add_column('rutas_despacho',
        sa.Column('ruta_maestra_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_rutas_despacho_ruta_maestra_id',
        'rutas_despacho', 'rutas_maestras',
        ['ruta_maestra_id'], ['id']
    )

    op.add_column('rutas_despacho',
        sa.Column('fecha_programada', sa.Date(), nullable=True))


def downgrade():
    op.drop_column('rutas_despacho', 'fecha_programada')
    op.drop_constraint('fk_rutas_despacho_ruta_maestra_id', 'rutas_despacho', type_='foreignkey')
    op.drop_column('rutas_despacho', 'ruta_maestra_id')
    op.drop_table('rutas_maestras_paradas')
    op.drop_table('rutas_maestras')
