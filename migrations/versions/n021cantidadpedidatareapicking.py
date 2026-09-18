"""`tareas_picking.cantidad_pedida` y `bloqueo_sin_stock` — trazabilidad del
backorder parcial de Siesa

Revision ID: n021cantidadpedidatareapicking
Revises: n020additemscanid
Create Date: 2026-09-18

PD1497 (2026-09-18): el pedido pidió 4, Siesa comprometió 2 y el WMS solo tenía
2 unidades. La tarea de picking quedó en 2 y el operario nunca supo que faltaban
2; tampoco quedó nada en Bodega → Auditoría, porque el resto se intentaba crear
como tarea bloqueada reservando stock y no había stock que reservar.

- `cantidad_pedida`: cuánto pedía la línea del pedido al crear la tarea. Es un
  snapshot: el HUD del operario compara contra esto ("pedido 4, Siesa
  comprometió 2") y no contra `cantidad_solicitada` de la tarea, que ya viene
  recortada. `NULL` = tarea que no pasó por ese cálculo (traslado, manual).
- `bloqueo_sin_stock`: la tarea nació BLOQUEADA sin congelar unidades en la
  ubicación (no había stock que congelar). Cancelarla, reabrirla o auditarla no
  debe restarle nada a `bloqueado`, que pertenece a otras tareas.
"""
from alembic import op
import sqlalchemy as sa

revision = 'n021cantidadpedidatareapicking'
down_revision = 'n020additemscanid'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'tareas_picking',
        sa.Column('cantidad_pedida', sa.Numeric(12, 2), nullable=True),
    )
    op.add_column(
        'tareas_picking',
        sa.Column('bloqueo_sin_stock', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade():
    op.drop_column('tareas_picking', 'bloqueo_sin_stock')
    op.drop_column('tareas_picking', 'cantidad_pedida')
