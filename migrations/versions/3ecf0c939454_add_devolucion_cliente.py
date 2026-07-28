"""add devolucion_cliente — devolución proactiva ligada al pedido, con NC automática

Revision ID: 3ecf0c939454
Revises: e7b2d5a83c14
Create Date: 2026-07-28

Reemplaza TareaDevolucion (reactiva, ciega, sin NC). El recepcionista busca
el pedido/factura original (ancla: TareaPacking.numero_pedido_siesa) y confirma
cuánto de cada línea se devuelve — total o parcial — disparando 142946
(NotaFactura) automáticamente al confirmar la entrada física. tipo_docto_fe/
consec_fe son el tipo/consecutivo REAL de la factura electrónica (resuelto vía
connekta.get_detalle_factura), nunca los del pedido — ver hallazgo del DOCX
oficial de 142946 documentado en devolucion_cliente_service.py.
"""
from alembic import op
import sqlalchemy as sa

revision = '3ecf0c939454'
down_revision = 'e7b2d5a83c14'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'devoluciones_cliente',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('codigo', sa.String(50), nullable=False),
        sa.Column('tarea_packing_id', sa.Integer(), sa.ForeignKey('tareas_packing.id'), nullable=False),
        sa.Column('numero_pedido_siesa', sa.String(50), nullable=True),
        sa.Column('tipo_docto_fe', sa.String(20), nullable=False),
        sa.Column('consec_fe', sa.String(30), nullable=False),
        sa.Column('cliente', sa.String(200), nullable=True),
        sa.Column('almacen_id', sa.Integer(), sa.ForeignKey('almacenes.id'), nullable=False),
        sa.Column('recepcionista_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('estado', sa.String(20), nullable=False, server_default='ABIERTA'),
        sa.Column('es_total', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('observaciones', sa.Text(), nullable=True),
        sa.Column('siesa_nc_triggered', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('siesa_nc_triggered_at', sa.DateTime(), nullable=True),
        sa.Column('siesa_nc_response', sa.Text(), nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(), nullable=True),
        sa.Column('fecha_confirmacion', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_devolucion_cliente_codigo', 'devoluciones_cliente', ['codigo'], unique=True)
    op.create_index('ix_devolucion_cliente_pedido', 'devoluciones_cliente', ['numero_pedido_siesa'])

    op.create_table(
        'lineas_devolucion_cliente',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('devolucion_id', sa.Integer(), sa.ForeignKey('devoluciones_cliente.id'), nullable=False),
        sa.Column('producto_id', sa.Integer(), sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('codigo_siesa', sa.String(50), nullable=False),
        sa.Column('cantidad_facturada', sa.Numeric(14, 4), nullable=False, server_default='0'),
        sa.Column('cantidad_devuelta', sa.Numeric(14, 4), nullable=False, server_default='0'),
        sa.Column('es_averiado', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('ubicacion_id', sa.Integer(), sa.ForeignKey('ubicaciones.id'), nullable=True),
        sa.Column('f470_id_unidad_medida', sa.String(20), nullable=True),
        sa.Column('f150_id_bodega', sa.String(20), nullable=True),
        sa.Column('f470_rowid', sa.String(20), nullable=True),
        sa.Column('fecha_creacion', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('lineas_devolucion_cliente')
    op.drop_index('ix_devolucion_cliente_pedido', table_name='devoluciones_cliente')
    op.drop_index('ix_devolucion_cliente_codigo', table_name='devoluciones_cliente')
    op.drop_table('devoluciones_cliente')
