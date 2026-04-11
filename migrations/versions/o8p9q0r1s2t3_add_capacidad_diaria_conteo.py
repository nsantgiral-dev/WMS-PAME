"""add capacidad_diaria_conteo to usuarios

Revision ID: o8p9q0r1s2t3
Revises: n7o8p9q0r1s2
Create Date: 2026-04-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'o8p9q0r1s2t3'
down_revision = 'n7o8p9q0r1s2'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('usuarios',
        sa.Column('capacidad_diaria_conteo', sa.Integer(), nullable=False,
                  server_default='15'))


def downgrade():
    op.drop_column('usuarios', 'capacidad_diaria_conteo')
