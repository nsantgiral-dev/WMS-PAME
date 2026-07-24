"""add proveedores + acuerdos_marco + precios_proveedor

Revision ID: e1f3a5b7c9d2
Revises: c5d8e2f4a917
Create Date: 2026-07-24

Módulo de compras inteligente: acuerdos marco con vigencia,
memoria de cotizaciones, detector de deriva de precios.
"""
from alembic import op
import sqlalchemy as sa

revision = 'e1f3a5b7c9d2'
down_revision = 'c5d8e2f4a917'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'proveedores',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('codigo', sa.String(20), unique=True, nullable=False),
        sa.Column('nombre', sa.String(200), nullable=False),
        sa.Column('nit', sa.String(20)),
        sa.Column('contacto_nombre', sa.String(100)),
        sa.Column('contacto_telefono', sa.String(20)),
        sa.Column('contacto_whatsapp', sa.String(20)),
        sa.Column('pais', sa.String(20), server_default='COLOMBIA'),
        sa.Column('condicion_pago_default', sa.String(20)),
        sa.Column('activo', sa.Boolean(), server_default='true'),
        sa.Column('fecha_creacion', sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_table(
        'acuerdos_marco',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('producto_id', sa.Integer(),
                  sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('proveedor_id', sa.Integer(),
                  sa.ForeignKey('proveedores.id'), nullable=False),
        sa.Column('precio_unitario', sa.Numeric(12, 2), nullable=False),
        sa.Column('moneda', sa.String(5), server_default='COP'),
        sa.Column('condicion_pago', sa.String(20)),
        sa.Column('volumen_comprometido', sa.Integer()),
        sa.Column('vigencia_desde', sa.Date(), nullable=False),
        sa.Column('vigencia_hasta', sa.Date(), nullable=False),
        sa.Column('negociado_por', sa.String(100)),
        sa.Column('notas', sa.Text()),
        sa.Column('activo', sa.Boolean(), server_default='true'),
        sa.Column('fecha_creacion', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_acuerdo_prod_prov', 'acuerdos_marco',
                    ['producto_id', 'proveedor_id'])

    op.create_table(
        'precios_proveedor',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('producto_id', sa.Integer(),
                  sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('proveedor_id', sa.Integer(),
                  sa.ForeignKey('proveedores.id'), nullable=False),
        sa.Column('precio_unitario', sa.Numeric(12, 2), nullable=False),
        sa.Column('moneda', sa.String(5), server_default='COP'),
        sa.Column('cantidad_cotizada', sa.Integer()),
        sa.Column('condicion_pago', sa.String(20)),
        sa.Column('fuente', sa.String(20), server_default='COTIZACION'),
        sa.Column('cotizado_por', sa.String(100)),
        sa.Column('fecha', sa.Date(), nullable=False),
        sa.Column('vigente', sa.Boolean(), server_default='true'),
    )
    op.create_index('ix_precio_prod_prov_fecha', 'precios_proveedor',
                    ['producto_id', 'proveedor_id', 'fecha'])


def downgrade():
    op.drop_index('ix_precio_prod_prov_fecha', table_name='precios_proveedor')
    op.drop_table('precios_proveedor')
    op.drop_index('ix_acuerdo_prod_prov', table_name='acuerdos_marco')
    op.drop_table('acuerdos_marco')
    op.drop_table('proveedores')
