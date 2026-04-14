"""add motivo_bloqueo to tareas_picking

Revision ID: p9q0r1s2t3u4
Revises: o8p9q0r1s2t3
Create Date: 2026-04-14
"""
from alembic import op
import sqlalchemy as sa

revision = 'p9q0r1s2t3u4'
down_revision = 'o8p9q0r1s2t3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tareas_picking',
        sa.Column('motivo_bloqueo', sa.String(50), nullable=True))


def downgrade():
    op.drop_column('tareas_picking', 'motivo_bloqueo')
