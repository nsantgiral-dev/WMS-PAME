"""add siesa_co_id to usuarios

Revision ID: k4l5m6n7o8p9
Revises: j3k4l5m6n7o8
Create Date: 2026-04-08

"""
from alembic import op
import sqlalchemy as sa

revision = 'k4l5m6n7o8p9'
down_revision = 'j3k4l5m6n7o8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('usuarios', sa.Column('siesa_co_id', sa.String(20), nullable=True))


def downgrade():
    op.drop_column('usuarios', 'siesa_co_id')
