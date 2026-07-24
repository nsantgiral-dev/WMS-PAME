"""add fuente to serie_vigia — separa línea base histórica de operación viva

Revision ID: d3f7a1c5e8b2
Revises: e1f3a5b7c9d2
Create Date: 2026-07-24

La limpieza transaccional del acta de corte borra picks, pedidos y movimientos
de prueba. NO debe tocar serie_vigia: el CUSUM calcula mu_ref y sigma_ref sobre
26 semanas de historia, y sin esa línea base queda ciego hasta acumularla de
nuevo (~6 meses). Los modelos TSB, ROP dual y newsvendor consumen la misma
historia.

Este campo permite filtrar por procedencia en el panel sin destruir nada:
  HISTORICO  — cargado desde export TXT de Siesa, previo al go-live
  PRODUCCION — generado por la operación viva (schedulers, conectores)

Backfill: las filas existentes al momento de migrar provienen de la carga TXT
inicial, así que se marcan HISTORICO. El default para filas nuevas es
PRODUCCION.
"""
from alembic import op
import sqlalchemy as sa

revision = 'd3f7a1c5e8b2'
down_revision = 'e1f3a5b7c9d2'
branch_labels = None
depends_on = None


def upgrade():
    # server_default para que las filas ya existentes no violen el NOT NULL
    op.add_column(
        'serie_vigia',
        sa.Column('fuente', sa.String(12), nullable=False,
                  server_default='PRODUCCION'),
    )
    # Lo cargado antes de esta migración vino del TXT: es la línea base histórica
    op.execute("UPDATE serie_vigia SET fuente = 'HISTORICO'")


def downgrade():
    op.drop_column('serie_vigia', 'fuente')
