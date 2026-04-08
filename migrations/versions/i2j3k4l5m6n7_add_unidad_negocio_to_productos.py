"""add unidad_negocio_id to productos

Revision ID: i2j3k4l5m6n7
Revises: h1i2j3k4l5m6
Create Date: 2026-04-07

Cambios:
- productos: +unidad_negocio_id (String 10) — código de unidad de negocio Siesa
  para f441_id_un_movto en requisiciones de traslado 174646
  Valores: 001=PAPELERIA, 002=TECNOLOGIA, 003=ARTE, 004=ASEO, 005=BELLEZA,
           006=FARMACIA, 007=DESECHABLES, 008=JUGUETERIA, 009=FIESTA,
           010=NAVIDAD, 011=HOGAR, 012=MASCOTAS, 013=FERRETERIA,
           014=IMPRESION, 99=ADMON
"""
from alembic import op
import sqlalchemy as sa

revision = 'i2j3k4l5m6n7'
down_revision = 'h1i2j3k4l5m6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('productos', sa.Column('unidad_negocio_id', sa.String(10), nullable=True))


def downgrade():
    op.drop_column('productos', 'unidad_negocio_id')
