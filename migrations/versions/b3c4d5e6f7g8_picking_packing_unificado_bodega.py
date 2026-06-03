"""picking_packing_unificado: merge de heads + bodega_origen_siesa + campos ST

Revision ID: b3c4d5e6f7g8
Revises: 27fbcf49ac17, a1b2c3d4e5f7, b9c0d1e2f3g4, f2g3h4i5j6k7, f8e9d0c1b2a3, r1s2t3u4v5w6
Create Date: 2026-06-03

Consolida los 6 heads sueltos y aplica los campos necesarios para el módulo
unificado de picking/packing (PD + ST):

  tareas_picking:
    + bodega_origen_siesa  VARCHAR(20) — scoping multi-bodega (NB1, NC1, NS1…)

  tareas_packing:
    + tipo_documento       VARCHAR(20) NOT NULL DEFAULT 'PEDIDO'
    + referencia_doc       VARCHAR(50) nullable
    + solicitud_id         INTEGER FK nullable → solicitudes_traslado
    + tienda_destino       VARCHAR(100) nullable
    + bodega_origen_siesa  VARCHAR(20) nullable
    ~ numero_pedido_siesa  → nullable=True
"""
from alembic import op
import sqlalchemy as sa


revision = 'b3c4d5e6f7g8'
down_revision = ('27fbcf49ac17', 'a1b2c3d4e5f7', 'b9c0d1e2f3g4',
                 'f2g3h4i5j6k7', 'f8e9d0c1b2a3', 'r1s2t3u4v5w6')
branch_labels = None
depends_on = None


def upgrade():
    # ── tareas_picking: scoping por bodega ──────────────────────────────────
    op.add_column(
        'tareas_picking',
        sa.Column('bodega_origen_siesa', sa.String(20), nullable=True),
    )
    op.execute("UPDATE tareas_picking SET bodega_origen_siesa = 'NB1'")

    # ── tareas_packing: soporte para ST ─────────────────────────────────────
    op.add_column(
        'tareas_packing',
        sa.Column('tipo_documento', sa.String(20), nullable=False,
                  server_default='PEDIDO'),
    )
    op.add_column(
        'tareas_packing',
        sa.Column('referencia_doc', sa.String(50), nullable=True),
    )
    op.add_column(
        'tareas_packing',
        sa.Column('solicitud_id', sa.Integer,
                  sa.ForeignKey('solicitudes_traslado.id', ondelete='SET NULL'),
                  nullable=True),
    )
    op.add_column(
        'tareas_packing',
        sa.Column('tienda_destino', sa.String(100), nullable=True),
    )
    op.add_column(
        'tareas_packing',
        sa.Column('bodega_origen_siesa', sa.String(20), nullable=True),
    )

    # Backfill: todos los packing existentes son PEDIDO de NB1
    op.execute("UPDATE tareas_packing SET bodega_origen_siesa = 'NB1'")
    op.execute(
        "UPDATE tareas_packing SET referencia_doc = numero_pedido_siesa "
        "WHERE referencia_doc IS NULL AND numero_pedido_siesa IS NOT NULL"
    )

    # numero_pedido_siesa: aflojar NOT NULL (incompatible con tareas ST)
    op.alter_column(
        'tareas_packing',
        'numero_pedido_siesa',
        existing_type=sa.String(50),
        nullable=True,
    )


def downgrade():
    op.alter_column(
        'tareas_packing',
        'numero_pedido_siesa',
        existing_type=sa.String(50),
        nullable=False,
    )
    op.drop_column('tareas_packing', 'bodega_origen_siesa')
    op.drop_column('tareas_packing', 'tienda_destino')
    op.drop_column('tareas_packing', 'solicitud_id')
    op.drop_column('tareas_packing', 'referencia_doc')
    op.drop_column('tareas_packing', 'tipo_documento')
    op.drop_column('tareas_picking', 'bodega_origen_siesa')
