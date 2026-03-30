"""campos tipo_docto y consec_docto para packing y recepcion

Revision ID: 3f9a1c7d2e85
Revises: e2808116ca1a
Create Date: 2026-03-28 10:00:00.000000

Motivo: el gateway Connekta necesita tipo_docto y consec_docto por separado
para construir los bodies oficiales de los conectores 142945 (RemisionPedido)
y 142948 (EntradaOC). Antes sólo se almacenaba el número combinado.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3f9a1c7d2e85'
down_revision = 'e2808116ca1a'
branch_labels = None
depends_on = None


def upgrade():
    # tareas_packing — componentes del pedido Siesa para trigger_despacho (conector 142945)
    op.add_column('tareas_packing',
        sa.Column('tipo_docto_pedido_siesa', sa.String(20), nullable=True))
    op.add_column('tareas_packing',
        sa.Column('consec_docto_pedido_siesa', sa.String(30), nullable=True))

    # recepciones — componentes de la OC Siesa para confirmar_entrada_compras (conector 142948)
    op.add_column('recepciones',
        sa.Column('co_oc_siesa', sa.String(20), nullable=True))
    op.add_column('recepciones',
        sa.Column('tipo_docto_oc_siesa', sa.String(20), nullable=True))
    op.add_column('recepciones',
        sa.Column('consec_docto_oc_siesa', sa.String(30), nullable=True))


def downgrade():
    op.drop_column('recepciones', 'consec_docto_oc_siesa')
    op.drop_column('recepciones', 'tipo_docto_oc_siesa')
    op.drop_column('recepciones', 'co_oc_siesa')
    op.drop_column('tareas_packing', 'consec_docto_pedido_siesa')
    op.drop_column('tareas_packing', 'tipo_docto_pedido_siesa')
