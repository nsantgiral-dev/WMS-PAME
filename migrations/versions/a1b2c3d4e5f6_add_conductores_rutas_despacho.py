"""add modulo milla-cero: vehiculos, rutas_maestras, conductores, rutas_despacho

Revision ID: a1b2c3d4e5f6
Revises: k4l5m6n7o8p9
Create Date: 2026-04-08

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'k4l5m6n7o8p9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'vehiculos',
        sa.Column('id',             sa.Integer(),       nullable=False),
        sa.Column('placa',          sa.String(10),      nullable=False),
        sa.Column('tipo',           sa.String(30),      nullable=False),
        sa.Column('capacidad_kg',   sa.Numeric(10, 2),  nullable=True),
        sa.Column('activo',         sa.Boolean(),       nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(),      nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('placa')
    )

    op.create_table(
        'rutas_maestras',
        sa.Column('id',             sa.Integer(),      nullable=False),
        sa.Column('nombre',         sa.String(100),    nullable=False),
        sa.Column('tipo_ruta',      sa.String(20),     nullable=False),
        sa.Column('activa',         sa.Boolean(),      nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(),     nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('nombre')
    )

    op.create_table(
        'rutas_maestras_paradas',
        sa.Column('id',              sa.Integer(),     nullable=False),
        sa.Column('ruta_maestra_id', sa.Integer(),     nullable=False),
        sa.Column('municipio',       sa.String(100),   nullable=False),
        sa.Column('orden',           sa.Integer(),     nullable=False),
        sa.ForeignKeyConstraint(['ruta_maestra_id'], ['rutas_maestras.id']),
        sa.PrimaryKeyConstraint('id')
    )

    op.create_table(
        'conductores',
        sa.Column('id',             sa.Integer(),     nullable=False),
        sa.Column('nombre',         sa.String(100),   nullable=False),
        sa.Column('cedula',         sa.String(20),    nullable=False),
        sa.Column('telefono',       sa.String(20),    nullable=True),
        sa.Column('activo',         sa.Boolean(),     nullable=True),
        sa.Column('usuario_id',     sa.Integer(),     nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(),    nullable=True),
        sa.ForeignKeyConstraint(['usuario_id'], ['usuarios.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('cedula')
    )

    op.create_table(
        'rutas_despacho',
        sa.Column('id',               sa.Integer(),   nullable=False),
        sa.Column('conductor_id',     sa.Integer(),   nullable=False),
        sa.Column('vehiculo_id',      sa.Integer(),   nullable=True),
        sa.Column('ruta_maestra_id',  sa.Integer(),   nullable=True),
        sa.Column('tipo_ruta',        sa.String(20),  nullable=False),
        sa.Column('fecha_programada', sa.Date(),      nullable=True),
        sa.Column('estado',           sa.String(20),  nullable=True),
        sa.Column('notas',            sa.Text(),      nullable=True),
        sa.Column('fecha_creacion',   sa.DateTime(),  nullable=True),
        sa.Column('fecha_cierre',     sa.DateTime(),  nullable=True),
        sa.Column('fecha_entregada',  sa.DateTime(),  nullable=True),
        sa.ForeignKeyConstraint(['conductor_id'],    ['conductores.id']),
        sa.ForeignKeyConstraint(['vehiculo_id'],     ['vehiculos.id']),
        sa.ForeignKeyConstraint(['ruta_maestra_id'], ['rutas_maestras.id']),
        sa.PrimaryKeyConstraint('id')
    )

    op.add_column('bultos',
        sa.Column('ruta_despacho_id', sa.Integer(),
                  sa.ForeignKey('rutas_despacho.id'), nullable=True))


def downgrade():
    op.drop_column('bultos', 'ruta_despacho_id')
    op.drop_table('rutas_despacho')
    op.drop_table('conductores')
    op.drop_table('rutas_maestras_paradas')
    op.drop_table('rutas_maestras')
    op.drop_table('vehiculos')
