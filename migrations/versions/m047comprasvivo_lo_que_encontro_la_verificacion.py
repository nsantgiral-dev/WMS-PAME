"""Compras: lo que encontró la verificación en vivo — proveedores y marca

Revision ID: m047comprasvivo
Revises: m046compras
Create Date: 2026-09-25

Aditiva y nullable, **sin backfill**: nada de lo que existe cambia de valor.

proveedores
- `moneda`           `f202_id_moneda` del maestro `API_v2_Proveedores` (la
                     de todas sus sucursales activas si coinciden; si no, NULL).
- `tipo_proveedor`   `f202_id_tipo_prov` (los de sus sucursales, separados por
                     coma). Qué tipos son mercancía: `COMPRAS_TIPOS_PROVEEDOR`.

productos
- `marca_codigo`     el CÓDIGO del criterio de marca de Siesa (M001…);
                     `marca_siesa` queda para el NOMBRE (NORMA…).

marca_siesa_lectura (nueva)
- la última lectura completa del plan de marca (`API_v2_ItemsCriterios`), por
  (plan, referencia). Se lee en segundo plano; la vista previa lee de acá.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm047comprasvivo'
down_revision = 'm046compras'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('proveedores') as batch:
        batch.add_column(sa.Column('moneda', sa.String(5), nullable=True))
        batch.add_column(sa.Column('tipo_proveedor', sa.String(20), nullable=True))

    with op.batch_alter_table('productos') as batch:
        batch.add_column(sa.Column('marca_codigo', sa.String(20), nullable=True))

    op.create_table(
        'marca_siesa_lectura',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('plan', sa.String(10), nullable=False),
        sa.Column('referencia', sa.String(50), nullable=False),
        sa.Column('criterio_codigo', sa.String(20), nullable=True),
        sa.Column('criterio_nombre', sa.String(100), nullable=True),
        sa.Column('registro_id', sa.Integer(), nullable=True),
        sa.Column('vista_en', sa.DateTime(), nullable=True),
    )
    op.create_index('uq_marca_siesa_lectura_plan_ref', 'marca_siesa_lectura',
                    ['plan', 'referencia'], unique=True)
    op.create_index('ix_marca_siesa_lectura_registro', 'marca_siesa_lectura',
                    ['registro_id'])


def downgrade():
    op.drop_index('ix_marca_siesa_lectura_registro', table_name='marca_siesa_lectura')
    op.drop_index('uq_marca_siesa_lectura_plan_ref', table_name='marca_siesa_lectura')
    op.drop_table('marca_siesa_lectura')

    with op.batch_alter_table('productos') as batch:
        batch.drop_column('marca_codigo')

    with op.batch_alter_table('proveedores') as batch:
        batch.drop_column('tipo_proveedor')
        batch.drop_column('moneda')
