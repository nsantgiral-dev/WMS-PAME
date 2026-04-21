"""add puede_abastecer to usuarios + tabla ubicaciones_huerfanas (cuarentena Fail-Fast)

Revision ID: a2b3c4d5e6f7
Revises: z7a8b9c0d1e2
Create Date: 2026-04-20

Cambios:
  1. usuarios.puede_abastecer (Boolean, default False)
     Operarios con rol='operario' que también pueden hacer reposición RESERVA→PICKING.
     No es un rol nuevo — es una capacidad adicional sobre el rol base.

  2. tabla ubicaciones_huerfanas
     Cuarentena Fail-Fast del sync de Siesa.
     Ubicaciones con prefijo no reconocido (no PIK-|RES-|AVE-) van aquí
     en vez de clasificarse como GENERAL automáticamente.
     El jefe de bodega corrige el nombre en Siesa y espera al próximo sync.
"""
from alembic import op
import sqlalchemy as sa

revision = 'a2b3c4d5e6f7'
down_revision = 'z7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Campo puede_abastecer en usuarios
    op.add_column(
        'usuarios',
        sa.Column('puede_abastecer', sa.Boolean(), nullable=True, server_default='false'),
    )

    # 2. Tabla de cuarentena Fail-Fast
    op.create_table(
        'ubicaciones_huerfanas',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('codigo_siesa', sa.String(50), nullable=False),
        sa.Column('bodega_id', sa.String(20), nullable=False),
        sa.Column('descripcion', sa.String(200), nullable=True),
        sa.Column('activo_siesa', sa.Boolean(), nullable=True),
        sa.Column('fecha_detectada', sa.DateTime(), nullable=False),
        sa.Column('fecha_ultima_vez', sa.DateTime(), nullable=False),
        sa.Column('veces_detectada', sa.Integer(), nullable=False, server_default='1'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('codigo_siesa', 'bodega_id', name='uq_huerfana_codigo_bodega'),
    )
    op.create_index(
        'ix_ubicaciones_huerfanas_bodega_id',
        'ubicaciones_huerfanas', ['bodega_id'],
    )


def downgrade():
    op.drop_index('ix_ubicaciones_huerfanas_bodega_id', table_name='ubicaciones_huerfanas')
    op.drop_table('ubicaciones_huerfanas')
    op.drop_column('usuarios', 'puede_abastecer')
