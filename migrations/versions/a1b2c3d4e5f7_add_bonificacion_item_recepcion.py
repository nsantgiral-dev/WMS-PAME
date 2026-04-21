"""add bonificacion fields to items_recepcion

Revision ID: a1b2c3d4e5f7
Revises: a2b3c4d5e6f7
Create Date: 2026-04-21

"""
from alembic import op
import sqlalchemy as sa

revision = 'a1b2c3d4e5f7'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('items_recepcion',
        sa.Column('tipo', sa.String(20), nullable=False, server_default='OC'))
    op.add_column('items_recepcion',
        sa.Column('motivo_siesa', sa.String(10), nullable=True))


def downgrade():
    op.drop_column('items_recepcion', 'motivo_siesa')
    op.drop_column('items_recepcion', 'tipo')
