"""stub: centro_op_siesa ya manejado por dict en código

Revision ID: c1d2e3f4g5h6
Revises: b3c4d5e6f7g8
Create Date: 2026-06-11

La BD quedó en esta revisión tras un deploy parcial.
El mapeo bodega→CO se hardcodeó en _BODEGA_CO_MAP; esta migración es no-op.
"""
from alembic import op

revision = 'c1d2e3f4g5h6'
down_revision = 'b3c4d5e6f7g8'
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
