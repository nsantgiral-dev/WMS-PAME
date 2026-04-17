"""merge heads: add_codigo_barras + add_cond_pago_siesa

Revision ID: s2t3u4v5w6x7
Revises: 27fbcf49ac17, r1s2t3u4v5w6
Create Date: 2026-04-17

"""
from alembic import op
import sqlalchemy as sa

revision = 's2t3u4v5w6x7'
down_revision = ('27fbcf49ac17', 'r1s2t3u4v5w6')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
