"""add observaciones_bloqueo to tareas_picking

Revision ID: q0r1s2t3u4v5
Revises: p9q0r1s2t3u4
Create Date: 2026-04-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'q0r1s2t3u4v5'
down_revision = 'p9q0r1s2t3u4'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tareas_picking',
        sa.Column('observaciones_bloqueo', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('tareas_picking', 'observaciones_bloqueo')
