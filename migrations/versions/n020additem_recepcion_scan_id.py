"""add ultimo_scan_id to items_recepcion

Revision ID: n020additemscanid
Revises: m019eventosstockagotado
Create Date: 2026-09-15

"""
from alembic import op
import sqlalchemy as sa

revision = 'n020additemscanid'
down_revision = 'm019eventosstockagotado'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('items_recepcion',
        sa.Column('ultimo_scan_id', sa.String(length=64), nullable=True)
    )


def downgrade():
    op.drop_column('items_recepcion', 'ultimo_scan_id')
