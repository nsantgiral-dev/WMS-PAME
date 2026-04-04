"""supervisor_guard — trazabilidad de excepciones de picking

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2026-04-03 22:00:00.000000

Cambios en sesiones_conteo:
- tarea_picking_id: FK a la tarea que originó la auditoría (nullable)
- aprobador_id: FK al usuario admin que aprobó el ajuste a Siesa
- idempotency_key: clave única por ajuste enviado a Siesa (ADJ-{id}-{ts})
"""
from alembic import op
import sqlalchemy as sa


revision = 'd4e5f6g7h8i9'
down_revision = 'c3d4e5f6g7h8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('sesiones_conteo',
        sa.Column('tarea_picking_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_sesiones_conteo_tarea_picking_id',
        'sesiones_conteo', 'tareas_picking',
        ['tarea_picking_id'], ['id']
    )

    op.add_column('sesiones_conteo',
        sa.Column('aprobador_id', sa.Integer(), nullable=True))
    op.create_foreign_key(
        'fk_sesiones_conteo_aprobador_id',
        'sesiones_conteo', 'usuarios',
        ['aprobador_id'], ['id']
    )

    op.add_column('sesiones_conteo',
        sa.Column('idempotency_key', sa.String(80), nullable=True))
    op.create_unique_constraint(
        'uq_sesiones_conteo_idempotency_key',
        'sesiones_conteo', ['idempotency_key']
    )


def downgrade():
    op.drop_constraint('uq_sesiones_conteo_idempotency_key', 'sesiones_conteo', type_='unique')
    op.drop_column('sesiones_conteo', 'idempotency_key')
    op.drop_constraint('fk_sesiones_conteo_aprobador_id', 'sesiones_conteo', type_='foreignkey')
    op.drop_column('sesiones_conteo', 'aprobador_id')
    op.drop_constraint('fk_sesiones_conteo_tarea_picking_id', 'sesiones_conteo', type_='foreignkey')
    op.drop_column('sesiones_conteo', 'tarea_picking_id')
