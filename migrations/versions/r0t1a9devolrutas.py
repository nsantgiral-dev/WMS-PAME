"""add_recaudo_entrega_id_devolucion_cliente

Revision ID: r0t1a9devolrutas
Revises: s001registrosync
Create Date: 2026-08-12

Conecta el flujo de Liquidación de rutas con el módulo de Devoluciones:
cuando una entrega Parcial/Rechazada se liquida ("Liquidar en WMS"), en vez
de crear la NC directo (250696, sin cruce automático de cartera), se arma
una DevolucionCliente ABIERTA con este campo apuntando al RecaudoEntrega
que la originó. La recepcionista confirma en Devoluciones (251126, con
cruce automático) — eso dispara la NC real y, vía el bridge en
siesa_job_service.py, también marca siesa_nc_triggered=True en el
RecaudoEntrega, destrabando el RECIBO_CAJA que dependía de esa NC.

NULL = devolución armada desde cero por la recepcionista (flujo original,
sin cambios). No-NULL = se originó en una liquidación de ruta.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'r0t1a9devolrutas'
down_revision = 's001registrosync'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('devoluciones_cliente',
                   sa.Column('recaudo_entrega_id', sa.Integer(),
                             sa.ForeignKey('recaudos_entrega.id'), nullable=True))


def downgrade():
    op.drop_column('devoluciones_cliente', 'recaudo_entrega_id')
