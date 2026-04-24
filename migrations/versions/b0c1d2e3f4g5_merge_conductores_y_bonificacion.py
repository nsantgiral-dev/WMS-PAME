"""merge conductores_rutas_despacho y bonificacion_item_recepcion

Revision ID: b0c1d2e3f4g5
Revises: a1b2c3d4e5f6, a1b2c3d4e5f7
Create Date: 2026-04-24

La BD de Railway aplicó a1b2c3d4e5f6 (conductores/rutas) y a1b2c3d4e5f7
(bonificacion recepcion) como dos cabezas paralelas en deploys distintos.
Este merge las unifica en un único head para que flask db upgrade funcione.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'b0c1d2e3f4g5'
down_revision = ('a1b2c3d4e5f6', 'a1b2c3d4e5f7')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
