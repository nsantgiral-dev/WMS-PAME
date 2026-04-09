"""add operario_id y estado PREPARADO a solicitudes_traslado

Revision ID: l5m6n7o8p9q0
Revises: k4l5m6n7o8p9
Create Date: 2026-04-09

"""
from alembic import op
import sqlalchemy as sa

revision = 'l5m6n7o8p9q0'
down_revision = 'k4l5m6n7o8p9'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'solicitudes_traslado',
        sa.Column('operario_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True)
    )


def downgrade():
    op.drop_column('solicitudes_traslado', 'operario_id')
