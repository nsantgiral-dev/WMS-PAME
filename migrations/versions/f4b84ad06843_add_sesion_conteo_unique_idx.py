"""add partial unique index on sesiones_conteo to prevent duplicate active CC1

Revision ID: f4b84ad06843
Revises: 01cd3ed5ad51
Create Date: 2026-07-17

Prevents race condition where API + scheduler create duplicate CC1 sessions
for the same (producto, ubicacion, almacen). CC2/CC3 (es_segundo_conteo=True)
are excluded — multiple verification counts are valid.
"""
from alembic import op

revision = 'f4b84ad06843'
down_revision = '01cd3ed5ad51'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'ix_sesion_conteo_activa_unica',
        'sesiones_conteo',
        ['producto_id', 'ubicacion_id', 'almacen_id'],
        unique=True,
        postgresql_where="estado IN ('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO') "
                         "AND es_segundo_conteo = false",
    )


def downgrade():
    op.drop_index('ix_sesion_conteo_activa_unica', table_name='sesiones_conteo')
