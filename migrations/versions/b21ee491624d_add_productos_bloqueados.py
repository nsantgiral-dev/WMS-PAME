"""add productos_bloqueados — lista de bloqueo de recompra

Revision ID: b21ee491624d
Revises: d41591071f7b
Create Date: 2026-07-22

Impide recomprar SKUs muertos (velocity=0 en 12 meses, genéricos sin conteo).
Desbloqueo requiere persona autorizada + cantidad máxima + vigencia.
Recepción de SKU bloqueado → cuarentena + registro de fuga (no rechazo).
"""
from alembic import op
import sqlalchemy as sa

revision = 'b21ee491624d'
down_revision = 'd41591071f7b'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'productos_bloqueados',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('producto_id', sa.Integer, sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('motivo', sa.String(50), nullable=False),  # VELOCITY_CERO_12M, EXCESO_CRITICO, GENERICO_SIN_CONTEO
        sa.Column('bloqueado_por_id', sa.Integer, sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('bloqueado_por_sistema', sa.Boolean, default=False),
        sa.Column('fecha_bloqueo', sa.DateTime, server_default=sa.func.now()),
        sa.Column('notas_bloqueo', sa.Text, nullable=True),
        # Desbloqueo (NULL = sigue bloqueado)
        sa.Column('desbloqueado_por_id', sa.Integer, sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('motivo_desbloqueo', sa.Text, nullable=True),
        sa.Column('cantidad_autorizada', sa.Integer, nullable=True),  # máximo a comprar
        sa.Column('vigencia_desbloqueo', sa.Date, nullable=True),     # hasta cuándo vale
        sa.Column('fecha_desbloqueo', sa.DateTime, nullable=True),
        sa.Column('activo', sa.Boolean, default=True, nullable=False),
        # Índices
        sa.Index('ix_prod_bloq_producto_activo', 'producto_id', 'activo'),
    )

    # Tabla de fugas: recepciones de SKU bloqueado (detector, no compuerta)
    op.create_table(
        'fugas_recompra',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('producto_id', sa.Integer, sa.ForeignKey('productos.id'), nullable=False),
        sa.Column('bloqueo_id', sa.Integer, sa.ForeignKey('productos_bloqueados.id'), nullable=False),
        sa.Column('recepcion_id', sa.Integer, nullable=True),
        sa.Column('oc_siesa', sa.String(50), nullable=True),
        sa.Column('proveedor', sa.String(100), nullable=True),
        sa.Column('cantidad_recibida', sa.Integer, nullable=True),
        sa.Column('fecha', sa.DateTime, server_default=sa.func.now()),
        sa.Column('notas', sa.Text, nullable=True),
    )


def downgrade():
    op.drop_table('fugas_recompra')
    op.drop_table('productos_bloqueados')
