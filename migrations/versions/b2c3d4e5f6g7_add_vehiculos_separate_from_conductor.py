"""add vehiculos — separar vehículo del conductor (flota dinámica)

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2026-04-03 19:30:00.000000

Cambios:
- Crea tabla 'vehiculos' (catálogo de flota propia)
- Agrega 'vehiculo_id' FK a 'rutas_despacho'
- Elimina columna 'placa' de 'conductores' (la placa es del vehículo, no del conductor)
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6g7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Crear tabla de vehículos
    op.create_table(
        'vehiculos',
        sa.Column('id',             sa.Integer(),      nullable=False),
        sa.Column('placa',          sa.String(10),     nullable=False),
        sa.Column('tipo',           sa.String(30),     nullable=False),
        sa.Column('capacidad_kg',   sa.Numeric(10, 2), nullable=True),
        sa.Column('activo',         sa.Boolean(),      nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(),     nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('placa'),
    )

    # 2. Agregar vehiculo_id a rutas_despacho (nullable primero para datos existentes)
    op.add_column(
        'rutas_despacho',
        sa.Column('vehiculo_id', sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        'fk_rutas_despacho_vehiculo_id',
        'rutas_despacho', 'vehiculos',
        ['vehiculo_id'], ['id']
    )

    # 3. Eliminar columna placa de conductores
    op.drop_column('conductores', 'placa')


def downgrade():
    op.add_column('conductores', sa.Column('placa', sa.String(10), nullable=True))
    op.drop_constraint('fk_rutas_despacho_vehiculo_id', 'rutas_despacho', type_='foreignkey')
    op.drop_column('rutas_despacho', 'vehiculo_id')
    op.drop_table('vehiculos')
