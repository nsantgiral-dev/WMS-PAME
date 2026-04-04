"""bulto: estados entregado/rechazado + motivo_rechazo + fecha_entrega

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-04-04
"""
from alembic import op
import sqlalchemy as sa

revision = 'e5f6g7h8i9j0'
down_revision = 'd4e5f6g7h8i9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('bultos', sa.Column('fecha_entrega', sa.DateTime(), nullable=True))
    op.add_column('bultos', sa.Column('motivo_rechazo', sa.String(100), nullable=True))


def downgrade():
    op.drop_column('bultos', 'motivo_rechazo')
    op.drop_column('bultos', 'fecha_entrega')
