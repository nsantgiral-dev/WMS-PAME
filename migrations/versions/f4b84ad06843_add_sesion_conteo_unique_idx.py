"""add partial unique index on sesiones_conteo to prevent duplicate active CC1

Revision ID: f4b84ad06843
Revises: 01cd3ed5ad51
Create Date: 2026-07-17

Prevents race condition where API + scheduler create duplicate CC1 sessions
for the same (producto, ubicacion, almacen). CC2/CC3 (es_segundo_conteo=True)
are excluded — multiple verification counts are valid.

Pre-step: cancel duplicate active sessions (keep most recent per group).
"""
from alembic import op
from sqlalchemy import text

revision = 'f4b84ad06843'
down_revision = '01cd3ed5ad51'
branch_labels = None
depends_on = None


def upgrade():
    # Cancel duplicate active CC1 sessions before creating unique index.
    # For each (producto, ubicacion, almacen) group with duplicates,
    # keep the most recent (highest id) and set older ones to CANCELADO.
    op.execute(text("""
        UPDATE sesiones_conteo
        SET estado = 'CANCELADO'
        WHERE id IN (
            SELECT s.id
            FROM sesiones_conteo s
            INNER JOIN (
                SELECT producto_id, ubicacion_id, almacen_id, MAX(id) AS max_id
                FROM sesiones_conteo
                WHERE estado IN ('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO')
                  AND es_segundo_conteo = false
                GROUP BY producto_id, ubicacion_id, almacen_id
                HAVING COUNT(*) > 1
            ) dupes ON s.producto_id = dupes.producto_id
                   AND s.ubicacion_id = dupes.ubicacion_id
                   AND s.almacen_id = dupes.almacen_id
                   AND s.id < dupes.max_id
            WHERE s.estado IN ('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO')
              AND s.es_segundo_conteo = false
        )
    """))

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
