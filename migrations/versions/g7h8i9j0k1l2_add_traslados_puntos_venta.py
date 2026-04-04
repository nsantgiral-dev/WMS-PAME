"""traslados entre bodega principal y puntos de venta

Revision ID: g7h8i9j0k1l2
Revises: f6g7h8i9j0k1
Branch labels: None
Depends on: None
Create Date: 2026-04-03
"""
from alembic import op
import sqlalchemy as sa

revision = 'g7h8i9j0k1l2'
down_revision = 'f6g7h8i9j0k1'
branch_labels = None
depends_on = None


def upgrade():
    # Campos tienda en usuarios
    op.add_column('usuarios', sa.Column('bodega_siesa_id', sa.String(20), nullable=True))
    op.add_column('usuarios', sa.Column('nombre_punto_venta', sa.String(100), nullable=True))

    # Solicitudes de traslado
    op.create_table(
        'solicitudes_traslado',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('codigo', sa.String(30), unique=True, nullable=False),
        sa.Column('bodega_origen_siesa', sa.String(20), nullable=False),
        sa.Column('bodega_destino_siesa', sa.String(20), nullable=False),
        sa.Column('nombre_punto_venta', sa.String(100)),
        sa.Column('estado', sa.String(30), default='BORRADOR', nullable=False),
        sa.Column('modo_transferencia', sa.String(20), default='EN_TRANSITO'),
        sa.Column('bodega_transito_siesa', sa.String(20)),
        sa.Column('solicitante_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=False),
        sa.Column('aprobador_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('siesa_requisicion_consec', sa.Integer()),
        sa.Column('siesa_salida_consec', sa.Integer()),
        sa.Column('siesa_entrada_consec', sa.Integer()),
        sa.Column('siesa_error', sa.Text()),
        sa.Column('fecha_creacion', sa.DateTime()),
        sa.Column('fecha_envio', sa.DateTime()),
        sa.Column('fecha_aprobacion', sa.DateTime()),
        sa.Column('fecha_despacho', sa.DateTime()),
        sa.Column('fecha_entrega', sa.DateTime()),
        sa.Column('observaciones', sa.Text()),
        sa.Column('motivo_rechazo', sa.String(200)),
    )

    # Ítems de solicitud de traslado
    op.create_table(
        'items_solicitud_traslado',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('solicitud_id', sa.Integer(),
                  sa.ForeignKey('solicitudes_traslado.id', ondelete='CASCADE'), nullable=False),
        sa.Column('producto_id', sa.Integer(), sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('producto_codigo_siesa', sa.String(50)),
        sa.Column('cantidad_solicitada', sa.Integer(), nullable=False),
        sa.Column('cantidad_aprobada', sa.Integer()),
        sa.Column('cantidad_enviada', sa.Integer(), default=0),
        sa.Column('cantidad_recibida', sa.Integer(), default=0),
        sa.Column('disponible_siesa', sa.Integer()),
    )

    op.create_index('ix_solicitudes_traslado_estado', 'solicitudes_traslado', ['estado'])
    op.create_index('ix_solicitudes_traslado_solicitante', 'solicitudes_traslado', ['solicitante_id'])


def downgrade():
    op.drop_index('ix_solicitudes_traslado_solicitante', table_name='solicitudes_traslado')
    op.drop_index('ix_solicitudes_traslado_estado', table_name='solicitudes_traslado')
    op.drop_table('items_solicitud_traslado')
    op.drop_table('solicitudes_traslado')
    op.drop_column('usuarios', 'nombre_punto_venta')
    op.drop_column('usuarios', 'bodega_siesa_id')
