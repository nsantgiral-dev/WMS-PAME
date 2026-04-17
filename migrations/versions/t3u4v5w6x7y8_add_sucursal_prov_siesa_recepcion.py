"""add sucursal_prov_siesa to recepciones

Revision ID: t3u4v5w6x7y8
Revises: s2t3u4v5w6x7
Create Date: 2026-04-17

"""
from alembic import op
import sqlalchemy as sa

revision = 't3u4v5w6x7y8'
down_revision = 's2t3u4v5w6x7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('recepciones',
        sa.Column('sucursal_prov_siesa', sa.String(length=10), nullable=True)
    )


def downgrade():
    op.drop_column('recepciones', 'sucursal_prov_siesa')
