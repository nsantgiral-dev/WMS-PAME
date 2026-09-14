"""add puede_organizar_layout to usuarios

Revision ID: m018puedeorganizarlayout
Revises: m017disponiblesiesapicking
Create Date: 2026-09-07

Cambios:
  usuarios.puede_organizar_layout (Boolean, default False)
    Operarios/empacadores que también pueden crear cuerpo/hueco y asignar
    SKU en Layout (PICKING). No es un rol nuevo — misma capacidad adicional
    sobre el rol base que ya usan puede_picar/puede_empacar/puede_abastecer.
    Solo cubre crear-cuerpo y asignar-SKU: editar, reclasificar, eliminar e
    importar Excel siguen exclusivos de admin/jefe_almacen (ver
    _puede_organizar_layout en app/routes/_auth_helpers.py).
"""
from alembic import op
import sqlalchemy as sa

revision = 'm018puedeorganizarlayout'
down_revision = 'm017disponiblesiesapicking'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'usuarios',
        sa.Column('puede_organizar_layout', sa.Boolean(), nullable=True, server_default='false'),
    )


def downgrade():
    op.drop_column('usuarios', 'puede_organizar_layout')
