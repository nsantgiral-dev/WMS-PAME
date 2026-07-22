"""add retenciones_detalle JSON to recaudos_entrega

Revision ID: d41591071f7b
Revises: f4b84ad06843
Create Date: 2026-07-21

Stores per-retention tracking with individual siesa_triggered flags.
Replaces motivo_descuento String(30) which can't track multiple retentions.
Old field kept for backward compatibility.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd41591071f7b'
down_revision = 'f4b84ad06843'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'recaudos_entrega',
        sa.Column('retenciones_detalle', sa.JSON(), nullable=True),
    )


def downgrade():
    op.drop_column('recaudos_entrega', 'retenciones_detalle')
