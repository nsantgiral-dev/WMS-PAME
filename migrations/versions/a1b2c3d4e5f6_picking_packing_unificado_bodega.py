"""picking_packing_unificado: bodega_origen_siesa en picking, campos ST en packing

Revision ID: a1b2c3d4e5f6
Revises: z7a8b9c0d1e2
Create Date: 2026-06-03

Habilita el módulo unificado de picking y packing para Pedidos (PD) y
Requisiciones/Traslados (ST):

  tareas_picking:
    + bodega_origen_siesa  VARCHAR(20)  — scoping multi-bodega (NB1, NC1, NS1…)

  tareas_packing:
    + tipo_documento       VARCHAR(20)  — 'PEDIDO' | 'TRASLADO'
    + referencia_doc       VARCHAR(50)  — 'PD1307' o 'ST-20260603-001'
    + solicitud_id         INTEGER FK   — solo si TRASLADO
    + tienda_destino       VARCHAR(100) — nombre display en UI
    + bodega_origen_siesa  VARCHAR(20)  — scoping igual que picking
    ~ numero_pedido_siesa  → nullable=True (era NOT NULL, incompatible con ST)
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'z7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    # ── tareas_picking: scoping por bodega ──────────────────────────────────
    op.add_column(
        'tareas_picking',
        sa.Column('bodega_origen_siesa', sa.String(20), nullable=True),
    )
    # Backfill: todos los registros existentes son de NB1
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

    # Backfill packing existente: todos son PEDIDO de NB1
    op.execute("UPDATE tareas_packing SET bodega_origen_siesa = 'NB1'")
    op.execute(
        "UPDATE tareas_packing SET referencia_doc = numero_pedido_siesa "
        "WHERE referencia_doc IS NULL AND numero_pedido_siesa IS NOT NULL"
    )

    # numero_pedido_siesa: aflojar NOT NULL para que tareas ST puedan tenerlo NULL
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
