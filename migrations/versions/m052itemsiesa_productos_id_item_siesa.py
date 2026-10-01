"""El número del ítem de Siesa (`f120_id`) en el producto

Revision ID: m052itemsiesa
Revises: m051liqcaja
Create Date: 2026-10-01

Aditiva, sin backfill: la llena el sync de catálogo en su próxima vuelta
(`siesa_sync_service`). Con ella el sync sigue un ítem aunque Siesa le cambie
la referencia (PAPELSP8985 → P197_006, producción 2026-10-01).
"""
import sqlalchemy as sa
from alembic import op

revision = 'm052itemsiesa'
down_revision = 'm051liqcaja'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('productos', sa.Column('id_item_siesa', sa.Integer(), nullable=True))
    op.create_index('ix_productos_id_item_siesa', 'productos', ['id_item_siesa'])


def downgrade():
    op.drop_index('ix_productos_id_item_siesa', table_name='productos')
    op.drop_column('productos', 'id_item_siesa')
