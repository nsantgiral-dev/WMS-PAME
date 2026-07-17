"""add liquidation fields for financial automation

Adds fields needed by LiquidacionService to automate Siesa financial connectors:
- vehiculos.codigo_siesa: Siesa vehicle master code (e.g. "DOMICILIOS")
- rutas_despacho.numero_guia: transport guide number
- recaudos_entrega: causal_devolucion, motivo_descuento, monto_descuento,
  siesa_rc/nc/dc_triggered (idempotency flags for 142888/142946/142882)

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-07-16
"""
from alembic import op
import sqlalchemy as sa

revision = 'd9e0f1a2b3c4'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    # Vehiculo — código maestro Siesa (no placa)
    op.add_column(
        'vehiculos',
        sa.Column('codigo_siesa', sa.String(30), nullable=True),
    )

    # RutaDespacho — guía transportadora
    op.add_column(
        'rutas_despacho',
        sa.Column('numero_guia', sa.String(50), nullable=True),
    )

    # RecaudoEntrega — campos de liquidación
    op.add_column(
        'recaudos_entrega',
        sa.Column('causal_devolucion', sa.String(10), nullable=True),
    )
    op.add_column(
        'recaudos_entrega',
        sa.Column('motivo_descuento', sa.String(30), nullable=True),
    )
    op.add_column(
        'recaudos_entrega',
        sa.Column('monto_descuento', sa.Numeric(12, 2), nullable=True, server_default='0'),
    )
    op.add_column(
        'recaudos_entrega',
        sa.Column('siesa_rc_triggered', sa.Boolean, nullable=True, server_default='false'),
    )
    op.add_column(
        'recaudos_entrega',
        sa.Column('siesa_nc_triggered', sa.Boolean, nullable=True, server_default='false'),
    )
    op.add_column(
        'recaudos_entrega',
        sa.Column('siesa_dc_triggered', sa.Boolean, nullable=True, server_default='false'),
    )


def downgrade():
    op.drop_column('recaudos_entrega', 'siesa_dc_triggered')
    op.drop_column('recaudos_entrega', 'siesa_nc_triggered')
    op.drop_column('recaudos_entrega', 'siesa_rc_triggered')
    op.drop_column('recaudos_entrega', 'monto_descuento')
    op.drop_column('recaudos_entrega', 'motivo_descuento')
    op.drop_column('recaudos_entrega', 'causal_devolucion')
    op.drop_column('rutas_despacho', 'numero_guia')
    op.drop_column('vehiculos', 'codigo_siesa')
