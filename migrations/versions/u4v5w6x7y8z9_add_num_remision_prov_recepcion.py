"""add num_remision_prov to recepciones

Revision ID: u4v5w6x7y8z9
Revises: t3u4v5w6x7y8
Create Date: 2026-04-17

"""
from alembic import op
import sqlalchemy as sa

revision = 'u4v5w6x7y8z9'
down_revision = 't3u4v5w6x7y8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('recepciones',
        sa.Column('num_remision_prov', sa.String(length=12), nullable=True)
    )


def downgrade():
    op.drop_column('recepciones', 'num_remision_prov')
