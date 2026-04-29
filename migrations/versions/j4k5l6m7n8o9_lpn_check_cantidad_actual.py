"""lpn: CHECK constraint cantidad_actual >= 0

Revision ID: j4k5l6m7n8o9
Revises: i3j4k5l6m7n8
Create Date: 2026-04-28

Agrega constraint de base de datos para garantizar que LPN.cantidad_actual
nunca sea negativa. Complementa la validación a nivel de app code.
El invariante INV_LPN_CANTIDAD_ACTUAL queda reforzado en la capa de DB.
"""
from alembic import op

revision = 'j4k5l6m7n8o9'
down_revision = 'i3j4k5l6m7n8'
branch_labels = None
depends_on = None


def upgrade():
    op.create_check_constraint(
        'lpn_cantidad_actual_gte0',
        'lpn',
        'cantidad_actual >= 0'
    )


def downgrade():
    op.drop_constraint('lpn_cantidad_actual_gte0', 'lpn', type_='check')
