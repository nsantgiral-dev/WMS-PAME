"""add edicion admin conteos

Revision ID: n7o8p9q0r1s2
Revises: m6n7o8p9q0r1
Create Date: 2026-04-10
"""
from alembic import op
import sqlalchemy as sa

revision = 'n7o8p9q0r1s2'
down_revision = 'm6n7o8p9q0r1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sesiones_conteo', sa.Column('editado_por', sa.Integer(),
                  sa.ForeignKey('usuarios.id'), nullable=True))
    op.add_column('sesiones_conteo', sa.Column('editado_en', sa.DateTime(), nullable=True))
    op.add_column('sesiones_conteo', sa.Column('motivo_edicion', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('sesiones_conteo', 'motivo_edicion')
    op.drop_column('sesiones_conteo', 'editado_en')
    op.drop_column('sesiones_conteo', 'editado_por')
