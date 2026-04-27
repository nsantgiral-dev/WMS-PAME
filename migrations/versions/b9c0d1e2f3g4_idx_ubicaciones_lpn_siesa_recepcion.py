"""idx_ubicaciones_lpn_siesa_recepcion

Revision ID: b9c0d1e2f3g4
Revises: a8b9c0d1e2f3
Create Date: 2026-04-25

Índices adicionales identificados en ronda 2 de auditoría:
  - ubicaciones_productos: (producto_id), (ubicacion_id, producto_id), (almacen_id, tipo_zona)
  - lpn: (traslado_id, estado)
  - siesa_jobs: (tipo, referencia_tipo, referencia_id, estado)
  - recepciones: (numero_oc_siesa, estado)
  - pedidos_siesa: (numero_pedido)
  - rutas_despacho: (vehiculo_id)
  - sesiones_conteo: (producto_id, ubicacion_id, estado), (almacen_id, estado)
  - tareas_packing: (estado, siesa_triggered)
"""
from alembic import op


revision = 'b9c0d1e2f3g4'
down_revision = 'a8b9c0d1e2f4'
branch_labels = None
depends_on = None


def upgrade():
    # ── ubicaciones_productos ─────────────────────────────────────────────────
    op.create_index('ix_ubicaciones_productos_producto_id',
                    'ubicaciones_productos', ['producto_id'], unique=False)
    op.create_index('ix_ubicaciones_productos_ubicacion_producto',
                    'ubicaciones_productos', ['ubicacion_id', 'producto_id'], unique=False)

    # ── lpn ───────────────────────────────────────────────────────────────────
    op.create_index('ix_lpn_traslado_estado',
                    'lpn', ['traslado_id', 'estado'], unique=False)

    # ── siesa_jobs ────────────────────────────────────────────────────────────
    op.create_index('ix_siesa_jobs_tipo_ref_estado',
                    'siesa_jobs',
                    ['tipo', 'referencia_tipo', 'referencia_id', 'estado'],
                    unique=False)

    # ── recepciones ───────────────────────────────────────────────────────────
    op.create_index('ix_recepciones_oc_estado',
                    'recepciones', ['numero_oc_siesa', 'estado'], unique=False)

    # ── pedidos_siesa ─────────────────────────────────────────────────────────
    op.create_index('ix_pedidos_siesa_numero_pedido',
                    'pedidos_siesa', ['numero_pedido'], unique=False)

    # ── rutas_despacho ────────────────────────────────────────────────────────
    op.create_index('ix_rutas_despacho_vehiculo_id',
                    'rutas_despacho', ['vehiculo_id'], unique=False)

    # ── sesiones_conteo (compuestos adicionales) ──────────────────────────────
    op.create_index('ix_sesiones_conteo_producto_ubicacion_estado',
                    'sesiones_conteo',
                    ['producto_id', 'ubicacion_id', 'estado'], unique=False)
    op.create_index('ix_sesiones_conteo_almacen_estado',
                    'sesiones_conteo', ['almacen_id', 'estado'], unique=False)

    # ── tareas_packing (compuesto adicional) ──────────────────────────────────
    op.create_index('ix_tareas_packing_estado_siesa_triggered',
                    'tareas_packing', ['estado', 'siesa_triggered'], unique=False)


def downgrade():
    op.drop_index('ix_tareas_packing_estado_siesa_triggered',   table_name='tareas_packing')
    op.drop_index('ix_sesiones_conteo_almacen_estado',          table_name='sesiones_conteo')
    op.drop_index('ix_sesiones_conteo_producto_ubicacion_estado', table_name='sesiones_conteo')
    op.drop_index('ix_rutas_despacho_vehiculo_id',              table_name='rutas_despacho')
    op.drop_index('ix_pedidos_siesa_numero_pedido',             table_name='pedidos_siesa')
    op.drop_index('ix_recepciones_oc_estado',                   table_name='recepciones')
    op.drop_index('ix_siesa_jobs_tipo_ref_estado',              table_name='siesa_jobs')
    op.drop_index('ix_lpn_traslado_estado',                     table_name='lpn')
    op.drop_index('ix_ubicaciones_productos_ubicacion_producto', table_name='ubicaciones_productos')
    op.drop_index('ix_ubicaciones_productos_producto_id',       table_name='ubicaciones_productos')
