"""conductor: vincular usuario_id para login en PWA

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'f6g7h8i9j0k1'
down_revision = 'e5f6g7h8i9j0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('conductores',
        sa.Column('usuario_id', sa.Integer(),
                  sa.ForeignKey('usuarios.id', ondelete='SET NULL'),
                  nullable=True))
    op.create_index('ix_conductores_usuario_id', 'conductores', ['usuario_id'], unique=True)


def downgrade():
    op.drop_index('ix_conductores_usuario_id', table_name='conductores')
    op.drop_column('conductores', 'usuario_id')
