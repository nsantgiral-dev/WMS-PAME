"""Conteo: actividad real de la sesión y regreso al estante de lo recogido

Revision ID: m033ciclo
Revises: m032tol
Create Date: 2026-09-23

## Para qué

«Ninguna cadena de conteo queda trabada ni pierde trabajo»:

- `sesiones_conteo.ultima_actividad_at` — el último escaneo o total tecleado.
  El barrido de zombis liberaba por `fecha_inicio` (2 h desde que se abrió) y
  borraba lo contado aunque el operario siguiera escaneando. Ahora libera por
  inactividad, y lo parcial queda en `conteos_descartados`.
- `tareas_picking.devuelto_estante_at` / `_por_id` / `_nota` — el líder
  declara que la mercancía de un pedido recogido (empaque cancelado sin
  remisión, o nunca empacado) volvió al estante. Es la fecha que le faltaba a
  la guarda de «mercancía en proceso» del conteo para dejar de excluir el SKU.

## Sin backfill

Nulas en todo el histórico: una sesión sin actividad registrada se juzga por
su `fecha_inicio` (lo mismo que antes), y un picking sin declaración sigue
contando como en proceso (Regla 0: no se inventa que la mercancía volvió).
"""
import sqlalchemy as sa
from alembic import op

revision = 'm033ciclo'
down_revision = 'm032tol'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.add_column(sa.Column('ultima_actividad_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('tareas_picking') as batch:
        batch.add_column(sa.Column('devuelto_estante_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('devuelto_estante_por_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('devuelto_estante_nota', sa.Text(), nullable=True))
        batch.create_foreign_key('fk_tareas_picking_devuelto_estante_por',
                                 'usuarios', ['devuelto_estante_por_id'], ['id'])


def downgrade():
    with op.batch_alter_table('tareas_picking') as batch:
        batch.drop_constraint('fk_tareas_picking_devuelto_estante_por', type_='foreignkey')
        batch.drop_column('devuelto_estante_nota')
        batch.drop_column('devuelto_estante_por_id')
        batch.drop_column('devuelto_estante_at')
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.drop_column('ultima_actividad_at')
