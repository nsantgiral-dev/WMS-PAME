"""Venta diaria de Siesa con valor y costo; dónde quedó cada período del histórico

Revision ID: m052comprasc
Revises: m051comprasa
Create Date: 2026-09-27

Aditiva, nullable, sin backfill. `demanda_dia_siesa` gana el valor SIN
impuesto y el costo de lo vendido y lo devuelto (NULL = la consulta registrada
no los trae: no se sabe). `demanda_ventana_lectura` guarda dónde quedó la
lectura de cada período del histórico para retomarla. Ver CLAUDE.md «La venta
de Siesa: el histórico que completa el año, con valor y costo».
"""
import sqlalchemy as sa
from alembic import op

revision = 'm052comprasc'
down_revision = 'm051comprasa'
branch_labels = None
depends_on = None


def upgrade():
    for col in ('valor_vendido', 'valor_devuelto', 'costo_vendido', 'costo_devuelto'):
        op.add_column('demanda_dia_siesa', sa.Column(col, sa.Numeric(18, 4), nullable=True))
    for col in ('lineas_sin_valor', 'lineas_sin_costo'):
        op.add_column('demanda_dia_siesa', sa.Column(col, sa.Integer(), nullable=True))
    op.create_table(
        'demanda_ventana_lectura',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('consulta', sa.String(120), nullable=False),
        sa.Column('desde', sa.Date(), nullable=True),
        sa.Column('fin', sa.Date(), nullable=True),
        sa.Column('total_filas', sa.Integer(), nullable=True),
        sa.Column('orden_hasta', sa.Integer(), nullable=True),
        sa.Column('cubierto_desde', sa.Date(), nullable=True),
        sa.Column('ancla_fecha', sa.Date(), nullable=True),
        sa.Column('ancla_bodega', sa.String(10), nullable=True),
        sa.Column('ancla_referencia', sa.String(50), nullable=True),
        sa.Column('completa', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sin_registrar', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('motivo', sa.String(500), nullable=True),
        sa.Column('paginas', sa.Integer(), nullable=True),
        sa.Column('registro_id', sa.Integer(), nullable=True),
        sa.Column('leida_en', sa.DateTime(), nullable=False),
    )
    op.create_index('uq_demanda_ventana_lectura_consulta', 'demanda_ventana_lectura',
                    ['consulta'], unique=True)


def downgrade():
    op.drop_index('uq_demanda_ventana_lectura_consulta', table_name='demanda_ventana_lectura')
    op.drop_table('demanda_ventana_lectura')
    for col in ('lineas_sin_costo', 'lineas_sin_valor', 'costo_devuelto', 'costo_vendido',
                'valor_devuelto', 'valor_vendido'):
        op.drop_column('demanda_dia_siesa', col)
