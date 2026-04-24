"""merge todas las ramas paralelas en un único head

Revision ID: b0c1d2e3f4g5
Revises: a1b2c3d4e5f6, a1b2c3d4e5f7, a8b9c0d1e2f3
Create Date: 2026-04-24

Railway aplicó varias migraciones por ramas paralelas en deploys distintos
dejando múltiples filas en alembic_version. Este merge no-op las unifica.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b0c1d2e3f4g5'
down_revision = ('a1b2c3d4e5f6', 'a1b2c3d4e5f7', 'a8b9c0d1e2f3')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
