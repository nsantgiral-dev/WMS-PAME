"""add inventario_descontado flag to solicitudes_traslado

Revision ID: 01cd3ed5ad51
Revises: d9e0f1a2b3c4
Create Date: 2026-07-17

Guard de idempotencia para evitar doble decremento de stock WMS
cuando ejecutar_cierre se reintenta tras Gunicorn timeout.
"""
from alembic import op
import sqlalchemy as sa

revision = '01cd3ed5ad51'
down_revision = 'd9e0f1a2b3c4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'solicitudes_traslado',
        sa.Column('inventario_descontado', sa.Boolean(),
                  server_default='false', nullable=True),
    )


def downgrade():
    op.drop_column('solicitudes_traslado', 'inventario_descontado')
