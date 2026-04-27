"""add fecha_procesando to siesa_jobs for accurate stuck-job detection

Revision ID: i3j4k5l6m7n8
Revises: h4i5j6k7l8m9
Branch_labels: None
depends_on: None

fecha_creacion reflects when the job was enqueued, not when it entered PROCESANDO.
Without fecha_procesando, a job backlogged for hours that just started PROCESANDO
is immediately reset to PENDIENTE on the next DLQ cycle — risking duplicate Siesa calls.
"""
from alembic import op
import sqlalchemy as sa

revision = 'i3j4k5l6m7n8'
down_revision = 'h4i5j6k7l8m9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'siesa_jobs',
        sa.Column('fecha_procesando', sa.DateTime(), nullable=True)
    )


def downgrade():
    op.drop_column('siesa_jobs', 'fecha_procesando')
