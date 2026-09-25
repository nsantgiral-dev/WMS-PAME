"""Compras: las fuentes de datos — OCs de Siesa, proveedores, origen y marca

Revision ID: m046compras
Revises: m045devol
Create Date: 2026-09-24

Compras no tenía datos de entrada que se llenaran solos: `items_en_transito`
sin escritor (el «en tránsito» del armador valía siempre 0), `proveedores`
vacía, `origen`/`marca_siesa` sin fuente, lead time fijo.

Aditiva y nullable, **sin backfill**: nada de lo que existe cambia de valor.

oc_linea_siesa (nueva)
- una fila por línea de OC (`f421_rowid`, único), abiertas y cumplidas.
  Se llena por `compras_oc_sync` (cron que nace apagado, `COMPRAS_OC_SYNC`).

proveedores
- `fuente`            SIESA_OC | SIESA_TERCEROS | MANUAL (NULL = anterior).
- `sincronizado_en`   última confirmación desde Siesa (UTC).

items_en_transito
- `oc_referencia`     CO-TIPO-CONSEC de la OC que el contenedor mueve.
- `bodega_destino`    NULL = el CDI.

productos
- `origen_fuente`, `marca_fuente`   quién escribió origen / marca.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm046compras'
down_revision = 'm045devol'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'oc_linea_siesa',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('rowid_linea', sa.BigInteger(), nullable=False),
        sa.Column('rowid_oc', sa.BigInteger(), nullable=True),
        sa.Column('co', sa.String(10), nullable=True),
        sa.Column('tipo_docto', sa.String(10), nullable=True),
        sa.Column('consec_docto', sa.Integer(), nullable=True),
        sa.Column('fecha_oc', sa.Date(), nullable=True),
        sa.Column('estado_oc', sa.SmallInteger(), nullable=True),
        sa.Column('fecha_aprobacion', sa.DateTime(), nullable=True),
        sa.Column('fecha_parcial', sa.DateTime(), nullable=True),
        sa.Column('fecha_cumplido', sa.DateTime(), nullable=True),
        sa.Column('moneda', sa.String(5), nullable=True),
        sa.Column('tasa_conv', sa.Numeric(18, 6), nullable=True),
        sa.Column('proveedor_codigo', sa.String(20), nullable=True),
        sa.Column('proveedor_nit', sa.String(20), nullable=True),
        sa.Column('proveedor_nombre', sa.String(200), nullable=True),
        sa.Column('proveedor_sucursal', sa.String(10), nullable=True),
        sa.Column('proveedor_id', sa.Integer(), sa.ForeignKey('proveedores.id'),
                  nullable=True),
        sa.Column('referencia', sa.String(50), nullable=True),
        sa.Column('bodega', sa.String(10), nullable=True),
        sa.Column('co_movto', sa.String(10), nullable=True),
        sa.Column('estado_linea', sa.SmallInteger(), nullable=True),
        sa.Column('ind_obsequio', sa.SmallInteger(), nullable=True),
        sa.Column('unidad_medida', sa.String(10), nullable=True),
        sa.Column('factor', sa.Numeric(18, 6), nullable=True),
        sa.Column('cant_pedida', sa.Numeric(18, 4), nullable=True),
        sa.Column('cant_entrada', sa.Numeric(18, 4), nullable=True),
        sa.Column('cant_pedida_base', sa.Numeric(18, 4), nullable=True),
        sa.Column('cant_entrada_base', sa.Numeric(18, 4), nullable=True),
        sa.Column('cant_importacion_base', sa.Numeric(18, 4), nullable=True),
        sa.Column('pendiente_base', sa.Numeric(18, 4), nullable=True),
        sa.Column('precio_unitario', sa.Numeric(18, 4), nullable=True),
        sa.Column('fecha_entrega', sa.Date(), nullable=True),
        sa.Column('abierta', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('primera_vista_en', sa.DateTime(), nullable=True),
        sa.Column('vista_en', sa.DateTime(), nullable=True),
        sa.Column('cerrada_en', sa.DateTime(), nullable=True),
        sa.Column('motivo_cierre', sa.String(40), nullable=True),
    )
    op.create_index('uq_oc_linea_siesa_rowid', 'oc_linea_siesa', ['rowid_linea'],
                    unique=True)
    op.create_index('ix_oc_linea_siesa_ref_abierta', 'oc_linea_siesa',
                    ['referencia', 'abierta'])
    op.create_index('ix_oc_linea_siesa_oc', 'oc_linea_siesa',
                    ['co', 'tipo_docto', 'consec_docto'])
    op.create_index('ix_oc_linea_siesa_proveedor', 'oc_linea_siesa',
                    ['proveedor_codigo'])

    with op.batch_alter_table('proveedores') as batch:
        batch.add_column(sa.Column('fuente', sa.String(20), nullable=True))
        batch.add_column(sa.Column('sincronizado_en', sa.DateTime(), nullable=True))

    with op.batch_alter_table('items_en_transito') as batch:
        batch.add_column(sa.Column('oc_referencia', sa.String(40), nullable=True))
        batch.add_column(sa.Column('bodega_destino', sa.String(10), nullable=True))

    with op.batch_alter_table('productos') as batch:
        batch.add_column(sa.Column('origen_fuente', sa.String(20), nullable=True))
        batch.add_column(sa.Column('marca_fuente', sa.String(20), nullable=True))


def downgrade():
    with op.batch_alter_table('productos') as batch:
        batch.drop_column('marca_fuente')
        batch.drop_column('origen_fuente')

    with op.batch_alter_table('items_en_transito') as batch:
        batch.drop_column('bodega_destino')
        batch.drop_column('oc_referencia')

    with op.batch_alter_table('proveedores') as batch:
        batch.drop_column('sincronizado_en')
        batch.drop_column('fuente')

    op.drop_index('ix_oc_linea_siesa_proveedor', table_name='oc_linea_siesa')
    op.drop_index('ix_oc_linea_siesa_oc', table_name='oc_linea_siesa')
    op.drop_index('ix_oc_linea_siesa_ref_abierta', table_name='oc_linea_siesa')
    op.drop_index('uq_oc_linea_siesa_rowid', table_name='oc_linea_siesa')
    op.drop_table('oc_linea_siesa')
