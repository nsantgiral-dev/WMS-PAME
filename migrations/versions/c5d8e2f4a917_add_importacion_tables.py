"""add importacion tables + origen en productos

Revision ID: c5d8e2f4a917
Revises: a7c3f82e9b14
Create Date: 2026-07-24

Infraestructura para Armador de Contenedor:
- ficha_importacion: cubicaje por SKU China
- contenedores: registro con 4 fechas (OC/ETD/ETA/recepción) para σ_LT
- items_en_transito: SKUs navegando (evita duplicar pedidos)
- productos.origen: NACIONAL/CHINA para ROP dual
- productos.marca_siesa: M003/M009/M175 para identificar brands China
"""
from alembic import op
import sqlalchemy as sa

revision = 'c5d8e2f4a917'
down_revision = 'a7c3f82e9b14'
branch_labels = None
depends_on = None


def upgrade():
    # Campo origen en productos
    op.add_column('productos', sa.Column('origen', sa.String(10), nullable=True))
    op.add_column('productos', sa.Column('marca_siesa', sa.String(50), nullable=True))

    # Ficha de importación (cubicaje por SKU)
    op.create_table(
        'ficha_importacion',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('producto_id', sa.Integer(),
                  sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('unidades_por_caja', sa.Integer(), nullable=False),
        sa.Column('cbm_por_caja', sa.Numeric(8, 4), nullable=False),
        sa.Column('peso_kg_por_caja', sa.Numeric(8, 2), nullable=False),
        sa.Column('moq_cajas', sa.Integer(), default=1),
        sa.Column('proveedor_china', sa.String(100)),
        sa.Column('costo_fob_usd', sa.Numeric(10, 2)),
        sa.Column('fuente', sa.String(20), nullable=False,
                  server_default='ESTIMADO'),
        sa.Column('fecha_ficha', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_ficha_importacion_producto', 'ficha_importacion',
                    ['producto_id'], unique=True)

    # Contenedores (registro histórico y en curso)
    op.create_table(
        'contenedores',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('numero', sa.String(20)),
        sa.Column('tipo', sa.String(10), server_default='40STD'),
        sa.Column('proveedor', sa.String(100)),
        sa.Column('fecha_oc', sa.Date()),
        sa.Column('fecha_etd', sa.Date()),
        sa.Column('fecha_eta', sa.Date()),
        sa.Column('fecha_llegada_puerto', sa.Date()),
        sa.Column('fecha_recepcion_cedi', sa.Date()),
        sa.Column('cbm_facturado', sa.Numeric(8, 2)),
        sa.Column('peso_kg', sa.Numeric(10, 2)),
        sa.Column('valor_fob_usd', sa.Numeric(12, 2)),
        sa.Column('valor_nacionalizado_cop', sa.Numeric(14, 0)),
        sa.Column('estado', sa.String(20), server_default='BORRADOR'),
        sa.Column('notas', sa.Text()),
        sa.Column('fecha_creacion', sa.DateTime(), server_default=sa.func.now()),
    )

    # Items en tránsito
    op.create_table(
        'items_en_transito',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('producto_id', sa.Integer(),
                  sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('contenedor_id', sa.Integer(),
                  sa.ForeignKey('contenedores.id'), nullable=True),
        sa.Column('cantidad', sa.Integer(), nullable=False),
        sa.Column('estado', sa.String(20), server_default='NAVEGANDO'),
        sa.Column('fecha_registro', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_items_transito_prod_cont', 'items_en_transito',
                    ['producto_id', 'contenedor_id'])


def downgrade():
    op.drop_index('ix_items_transito_prod_cont', table_name='items_en_transito')
    op.drop_table('items_en_transito')
    op.drop_table('contenedores')
    op.drop_index('ix_ficha_importacion_producto', table_name='ficha_importacion')
    op.drop_table('ficha_importacion')
    op.drop_column('productos', 'marca_siesa')
    op.drop_column('productos', 'origen')
