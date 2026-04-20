"""add producto_empaques y lpn

Revision ID: x5y6z7a8b9c0
Revises: v5w6x7y8z9a0
Create Date: 2026-04-20

producto_empaques: catálogo de empaques por producto (DUN-14, pacas, cajas).
lpn: instancias físicas de pacas/empaques en el almacén (License Plate Numbers).
"""
from alembic import op
import sqlalchemy as sa

revision = 'x5y6z7a8b9c0'
down_revision = 'v5w6x7y8z9a0'
branch_labels = None
depends_on = None


def upgrade():
    # ── producto_empaques ──────────────────────────────────────────────────────
    op.create_table(
        'producto_empaques',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('producto_id', sa.Integer, sa.ForeignKey('productos.id', ondelete='CASCADE'), nullable=False),
        sa.Column('referencia_item', sa.String(50), nullable=False),
        sa.Column('codigo_barras', sa.String(50), nullable=False),
        sa.Column('unidad_medida', sa.String(20), nullable=False),
        sa.Column('factor_conversion', sa.Integer, nullable=False),
        sa.Column('origen', sa.String(20), nullable=False, server_default='SIESA_GS1'),
        sa.Column('activo', sa.Boolean, nullable=False, server_default='true'),
        sa.Column('fecha_creacion', sa.DateTime, server_default=sa.func.now()),
        sa.Column('fecha_actualizacion', sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index('ix_producto_empaques_codigo_barras', 'producto_empaques', ['codigo_barras'])
    op.create_index('ix_producto_empaques_producto_id', 'producto_empaques', ['producto_id'])
    op.create_index('ix_producto_empaques_referencia_item', 'producto_empaques', ['referencia_item'])

    # ── lpn ───────────────────────────────────────────────────────────────────
    op.create_table(
        'lpn',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('codigo', sa.String(20), unique=True, nullable=False),
        sa.Column('producto_id', sa.Integer, sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('empaque_id', sa.Integer, sa.ForeignKey('producto_empaques.id'), nullable=True),
        sa.Column('factor_conversion', sa.Integer, nullable=False),
        sa.Column('cantidad_actual', sa.Integer, nullable=False),
        sa.Column('estado', sa.String(20), nullable=False, server_default='ACTIVO'),
        sa.Column('almacen_id', sa.Integer, sa.ForeignKey('almacenes.id'), nullable=True),
        sa.Column('ubicacion_id', sa.Integer, sa.ForeignKey('ubicaciones.id'), nullable=True),
        sa.Column('recepcion_id', sa.Integer, sa.ForeignKey('recepciones.id'), nullable=True),
        sa.Column('traslado_id', sa.Integer, sa.ForeignKey('solicitudes_traslado.id'), nullable=True),
        sa.Column('creado_por_id', sa.Integer, sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('fecha_creacion', sa.DateTime, server_default=sa.func.now()),
        sa.Column('fecha_consumo', sa.DateTime, nullable=True),
        sa.Column('notas', sa.Text, nullable=True),
    )
    op.create_index('ix_lpn_codigo', 'lpn', ['codigo'])
    op.create_index('ix_lpn_producto_id', 'lpn', ['producto_id'])
    op.create_index('ix_lpn_estado', 'lpn', ['estado'])
    op.create_index('ix_lpn_almacen_id', 'lpn', ['almacen_id'])


def downgrade():
    op.drop_table('lpn')
    op.drop_table('producto_empaques')
