"""Parada registrada por la oficina, versión del conductor y fecha del RC

Revision ID: m049tardia
Revises: m048inv
Create Date: 2026-09-25

Aditiva, nullable, sin backfill (tanda 2).

recaudos_entrega
- registrada_por_oficina (bool): la parada la registró la oficina después
  del cierre de la ruta (formulario de parada tardía). Señal, no sanción.
- version_conductor (json) / version_conductor_en: lo que mandó el teléfono
  DESPUÉS de que la oficina la registró. No pisa lo de la oficina.
- diferencia_conductor (bool): True = la versión del conductor difiere de lo
  registrado (para revisar); False = coincide; NULL = no llegó.
- rc_cobro_otro_mes (date): el día real del cobro cuando el recibo de caja se
  fechó en el mes del envío (el mes del cobro ya había cambiado).
"""
import sqlalchemy as sa
from alembic import op

revision = 'm049tardia'
down_revision = 'm048inv'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recaudos_entrega') as b:
        b.add_column(sa.Column('registrada_por_oficina', sa.Boolean(), nullable=True))
        b.add_column(sa.Column('version_conductor', sa.JSON(), nullable=True))
        b.add_column(sa.Column('version_conductor_en', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('diferencia_conductor', sa.Boolean(), nullable=True))
        b.add_column(sa.Column('rc_cobro_otro_mes', sa.Date(), nullable=True))


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as b:
        b.drop_column('rc_cobro_otro_mes')
        b.drop_column('diferencia_conductor')
        b.drop_column('version_conductor_en')
        b.drop_column('version_conductor')
        b.drop_column('registrada_por_oficina')
