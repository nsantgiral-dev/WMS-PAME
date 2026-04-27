"""add check constraint cantidad >= 0 on ubicaciones_productos

Revision ID: b1c2d3e4f5g6
Revises: 5bdcedc43de0
Create Date: 2026-04-25 00:00:00.000000

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b1c2d3e4f5g6'
down_revision = '5bdcedc43de0'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_cantidad_no_negativa') THEN "
        "ALTER TABLE ubicaciones_productos ADD CONSTRAINT ck_cantidad_no_negativa CHECK (cantidad >= 0); "
        "END IF; "
        "END $$;"
    )


def downgrade():
    op.execute(
        'ALTER TABLE ubicaciones_productos '
        'DROP CONSTRAINT IF EXISTS ck_cantidad_no_negativa'
    )
