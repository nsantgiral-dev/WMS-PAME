"""add bonificacion fields to items_recepcion

Revision ID: z7a8b9c0d1e2
Revises: y6z7a8b9c0d1
Create Date: 2026-04-21

"""
from alembic import op
import sqlalchemy as sa

revision = 'z7a8b9c0d1e2'
down_revision = 'y6z7a8b9c0d1'
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
