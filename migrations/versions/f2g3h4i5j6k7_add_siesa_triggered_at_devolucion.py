"""add siesa_triggered_at to tareas_devolucion

Revision ID: f2g3h4i5j6k7
Revises: e1f2g3h4i5j6
Create Date: 2026-04-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'f2g3h4i5j6k7'
down_revision = 'e1f2g3h4i5j6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('tareas_devolucion',
        sa.Column('siesa_triggered_at', sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_column('tareas_devolucion', 'siesa_triggered_at')
