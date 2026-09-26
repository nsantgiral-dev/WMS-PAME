"""Índice de pedidos_historia por cliente

Revision ID: m050inv2
Revises: m049tardia
Create Date: 2026-09-26

Aditiva. La compuerta de cartera busca los pedidos de un NIT en cada
evaluación (`cartera_service._claves_del_nit`); sin índice, cada despacho a
crédito recorría la tabla entera.
"""
from alembic import op

revision = 'm050inv2'
down_revision = 'm049tardia'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_pedidos_historia_cliente', 'pedidos_historia', ['cliente_id'])


def downgrade():
    op.drop_index('ix_pedidos_historia_cliente', table_name='pedidos_historia')
