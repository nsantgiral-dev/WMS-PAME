"""add items_entregados to recaudos_entrega — detalle de referencias en entrega parcial

Revision ID: f8e9d0c1b2a3
Revises: k1l2m3n4o5p6
Create Date: 2026-06-01

Permite registrar a nivel de referencia cuántas unidades se entregaron
y cuántas se devolvieron en una parada PARCIAL.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f8e9d0c1b2a3'
down_revision = 'k1l2m3n4o5p6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'recaudos_entrega',
        sa.Column('items_entregados', sa.JSON, nullable=True),
    )


def downgrade():
    op.drop_column('recaudos_entrega', 'items_entregados')
