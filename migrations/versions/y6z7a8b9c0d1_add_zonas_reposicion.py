"""add zonas y tareas_reposicion

Revision ID: y6z7a8b9c0d1
Revises: x5y6z7a8b9c0
Create Date: 2026-04-20

Adds:
  - ubicaciones.tipo_zona  (RESERVA | PICKING | GENERAL — default GENERAL)
  - ubicaciones.stock_minimo / stock_maximo  (solo aplica a PICKING)
  - tareas_reposicion: cola de reabastecimiento RESERVA → PICKING
"""
from alembic import op
import sqlalchemy as sa

revision = 'y6z7a8b9c0d1'
down_revision = 'x5y6z7a8b9c0'
branch_labels = None
depends_on = None


def upgrade():
    # ── ubicaciones: campos sincronizados desde Siesa (API_v2_Ubicaciones ID 43) ──
    # Siesa es el dueño — el WMS es un espejo. No editar manualmente.
    op.add_column('ubicaciones', sa.Column('tipo_zona', sa.String(10), nullable=False, server_default='GENERAL'))
    op.add_column('ubicaciones', sa.Column('stock_minimo', sa.Integer, nullable=True))   # f152_cant_minima
    op.add_column('ubicaciones', sa.Column('stock_maximo', sa.Integer, nullable=True))   # f152_cant_maxima
    op.add_column('ubicaciones', sa.Column('secuencia_ruteo', sa.Integer, nullable=True))  # f152_secuencia
    op.create_index('ix_ubicaciones_tipo_zona', 'ubicaciones', ['tipo_zona'])

    # ── tareas_reposicion ──────────────────────────────────────────────────────
    op.create_table(
        'tareas_reposicion',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('codigo', sa.String(20), unique=True, nullable=False),

        # Qué producto reponer y cuánto
        sa.Column('producto_id', sa.Integer, sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('almacen_id', sa.Integer, sa.ForeignKey('almacenes.id'), nullable=False),
        sa.Column('cantidad_unidades', sa.Integer, nullable=False),  # UNDs base a mover

        # De dónde (RESERVA) y hacia dónde (PICKING)
        sa.Column('ubicacion_reserva_id', sa.Integer, sa.ForeignKey('ubicaciones.id'), nullable=False),
        sa.Column('ubicacion_picking_id', sa.Integer, sa.ForeignKey('ubicaciones.id'), nullable=False),

        # LPN involucrado (puede ser NULL si no hay LPN identificado aún)
        sa.Column('lpn_id', sa.Integer, sa.ForeignKey('lpn.id'), nullable=True),

        # Asignación y estado
        sa.Column('abastecedor_id', sa.Integer, sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('estado', sa.String(20), nullable=False, server_default='PENDIENTE'),
        # Estados: PENDIENTE | EN_PROCESO | COMPLETADA | CANCELADA

        # Resultado al completar
        sa.Column('unidades_movidas', sa.Integer, nullable=True),
        sa.Column('siesa_job_id', sa.String(50), nullable=True),   # job_id del conector 173076
        sa.Column('siesa_enviado', sa.Boolean, nullable=False, server_default='false'),

        sa.Column('notas', sa.Text, nullable=True),
        sa.Column('fecha_creacion', sa.DateTime, server_default=sa.func.now()),
        sa.Column('fecha_inicio', sa.DateTime, nullable=True),
        sa.Column('fecha_completada', sa.DateTime, nullable=True),
    )
    op.create_index('ix_tareas_reposicion_estado', 'tareas_reposicion', ['estado'])
    op.create_index('ix_tareas_reposicion_producto_id', 'tareas_reposicion', ['producto_id'])
    op.create_index('ix_tareas_reposicion_abastecedor_id', 'tareas_reposicion', ['abastecedor_id'])


def downgrade():
    op.drop_table('tareas_reposicion')
    op.drop_index('ix_ubicaciones_tipo_zona', table_name='ubicaciones')
    op.drop_column('ubicaciones', 'secuencia_ruteo')
    op.drop_column('ubicaciones', 'stock_maximo')
    op.drop_column('ubicaciones', 'stock_minimo')
    op.drop_column('ubicaciones', 'tipo_zona')
