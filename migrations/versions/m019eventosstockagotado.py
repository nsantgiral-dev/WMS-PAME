"""Tabla eventos_stock_agotado — snapshot de agotados reales

Revision ID: m019eventosstockagotado
Revises: m018puedeorganizarlayout
Create Date: 2026-09-08

Alimenta el tablero BI (métricas "SKUs no vendidos por agotado" y "venta
perdida por agotados $"). Se dispara desde PickingService.reportar_problema()
cuando una TareaPicking se bloquea con motivo_bloqueo='FALTANTE' — el único
punto del WMS donde un agotado real (ya con operario físicamente en la
ubicación) es observable en vivo.

Evento hacia adelante, sin backfill: el histórico previo a este deploy no
tiene forma de reconstruirse (nadie capturó el momento exacto en que un
pedido no pudo asignarse por falta de stock antes de esta tabla), y no se
intenta simular uno.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm019eventosstockagotado'
down_revision = 'm018puedeorganizarlayout'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'eventos_stock_agotado',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('tarea_picking_id', sa.Integer(), nullable=False),
        sa.Column('producto_id', sa.Integer(), nullable=False),
        sa.Column('almacen_id', sa.Integer(), nullable=False),
        sa.Column('pedido_siesa_ref', sa.String(length=50), nullable=True),
        sa.Column('tipo_documento', sa.String(length=30), nullable=True),
        sa.Column('cantidad_faltante', sa.Integer(), nullable=False),
        sa.Column('precio_venta_capturado', sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column('categoria_producto', sa.String(length=100), nullable=True),
        sa.Column('clasificacion_abc', sa.String(length=1), nullable=True),
        sa.Column('creado_en', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['tarea_picking_id'], ['tareas_picking.id']),
        sa.ForeignKeyConstraint(['producto_id'], ['productos.id']),
        sa.ForeignKeyConstraint(['almacen_id'], ['almacenes.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_eventos_stock_agotado_creado_en',
                     'eventos_stock_agotado', ['creado_en'])
    # La agregación real siempre filtra por producto+almacen+rango de fecha
    # (métricas 4 y 5 del tablero) — sin este índice compuesto cada consulta
    # escanea la tabla completa.
    op.create_index('ix_eventos_stock_agotado_producto_almacen',
                     'eventos_stock_agotado',
                     ['producto_id', 'almacen_id', 'creado_en'])


def downgrade():
    op.drop_index('ix_eventos_stock_agotado_producto_almacen',
                   table_name='eventos_stock_agotado')
    op.drop_index('ix_eventos_stock_agotado_creado_en',
                   table_name='eventos_stock_agotado')
    op.drop_table('eventos_stock_agotado')
