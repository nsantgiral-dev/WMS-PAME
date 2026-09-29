"""Lo que el comprador decidió sobre una línea de la bandeja

Revision ID: m051comprasf
Revises: m052comprasc (re-encadenada en la integración final del 2026-09-29; se escribió sobre m050demanda)
Create Date: 2026-09-27

Aditiva, sin backfill. Ver `app/models/decision_compra.py` y CLAUDE.md
«Compras: la demanda del horizonte, «ya pedido» honesto y las decisiones del
comprador (2026-09-27)».
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051comprasf'
down_revision = 'm052comprasc'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'decision_compra',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('referencia', sa.String(50), nullable=False),
        sa.Column('accion', sa.String(12), nullable=False),
        sa.Column('cantidad_propuesta', sa.Numeric(16, 4), nullable=True),
        sa.Column('cantidad_decidida', sa.Numeric(16, 4), nullable=True),
        sa.Column('oc_siesa', sa.String(40), nullable=True),
        sa.Column('proveedor_codigo', sa.String(20), nullable=True),
        sa.Column('motivo', sa.String(300), nullable=True),
        sa.Column('urgencia_vista', sa.String(12), nullable=True),
        sa.Column('dia', sa.Date(), nullable=False),
        sa.Column('vigente_hasta', sa.Date(), nullable=False),
        sa.Column('usuario_id', sa.Integer(), nullable=True),
        sa.Column('usuario_nombre', sa.String(120), nullable=True),
        sa.Column('creada_en', sa.DateTime(), nullable=False),
        sa.Column('foto', sa.Text(), nullable=True),
        sa.Column('anulada_en', sa.DateTime(), nullable=True),
        sa.Column('anulada_por_id', sa.Integer(), nullable=True),
        sa.Column('anulada_motivo', sa.String(300), nullable=True),
        sa.CheckConstraint("accion IN ('PEDIDO', 'POSPUESTO', 'DESCARTADO')",
                           name='ck_decision_compra_accion'),
    )
    op.create_index('ix_decision_compra_ref_vigencia', 'decision_compra',
                    ['referencia', 'vigente_hasta'])
    op.create_index('ix_decision_compra_dia', 'decision_compra', ['dia'])


def downgrade():
    op.drop_index('ix_decision_compra_dia', table_name='decision_compra')
    op.drop_index('ix_decision_compra_ref_vigencia', table_name='decision_compra')
    op.drop_table('decision_compra')
