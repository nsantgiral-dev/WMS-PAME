"""add inventario_descontado flag to solicitudes_traslado

Revision ID: a1b2c3d4e5f6
Revises: z7a8b9c0d1e2
Create Date: 2026-07-17

Guard de idempotencia para evitar doble decremento de stock WMS
cuando ejecutar_cierre se reintenta tras Gunicorn timeout.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f6'
down_revision = 'z7a8b9c0d1e2'
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
