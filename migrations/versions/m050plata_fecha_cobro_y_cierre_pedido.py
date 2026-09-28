"""Fecha real del cobro y cierre de ruta pedido por la oficina

Revision ID: m050plata
Revises: m049tardia
Create Date: 2026-09-26

Aditiva, nullable, sin backfill (validación de la plata).

recaudos_entrega
- fecha_cobro (datetime, UTC): cuándo se cobró de verdad — la hora del
  teléfono corregida por el desfase medido, o la del servidor en la primera
  confirmación; la oficina la declara en su formulario. No la reescribe una
  re-confirmación. `politica_cobro.fechas_del_recibo` la lee.

rutas_despacho
- cierre_pedido_en / cierre_pedido_por_id: la oficina le pidió al conductor
  cerrar una ruta que lleva más de un día en camino.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm050plata'
down_revision = 'm049tardia'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recaudos_entrega') as b:
        b.add_column(sa.Column('fecha_cobro', sa.DateTime(), nullable=True))
    with op.batch_alter_table('rutas_despacho') as b:
        b.add_column(sa.Column('cierre_pedido_en', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('cierre_pedido_por_id', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('rutas_despacho') as b:
        b.drop_column('cierre_pedido_por_id')
        b.drop_column('cierre_pedido_en')
    with op.batch_alter_table('recaudos_entrega') as b:
        b.drop_column('fecha_cobro')
