"""add cond_pago_siesa to recepciones

Revision ID: r1s2t3u4v5w6
Revises: q0r1s2t3u4v5
Create Date: 2026-04-17

"""
from alembic import op
import sqlalchemy as sa

revision = 'r1s2t3u4v5w6'
down_revision = 'q0r1s2t3u4v5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('recepciones',
        sa.Column('cond_pago_siesa', sa.String(length=20), nullable=True)
    )


def downgrade():
    op.drop_column('recepciones', 'cond_pago_siesa')
