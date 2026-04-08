"""tabla siesa_mapeo_unidades para f441_id_un_movto

Revision ID: j3k4l5m6n7o8
Revises: i2j3k4l5m6n7
Create Date: 2026-04-07

Cambios:
- nueva tabla siesa_mapeo_unidades: tipo_inv_siesa (unique) → unidad_negocio_id
  Permite al administrador mantener el mapeo ERP sin tocar código.
  El sync nocturno la consulta para poblar productos.unidad_negocio_id.
"""
from alembic import op
import sqlalchemy as sa

revision = 'j3k4l5m6n7o8'
down_revision = 'i2j3k4l5m6n7'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'siesa_mapeo_unidades',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tipo_inv_siesa', sa.String(50), nullable=False, unique=True),
        sa.Column('unidad_negocio_id', sa.String(10), nullable=False),
        sa.Column('descripcion', sa.String(200), nullable=True),
    )


def downgrade():
    op.drop_table('siesa_mapeo_unidades')
