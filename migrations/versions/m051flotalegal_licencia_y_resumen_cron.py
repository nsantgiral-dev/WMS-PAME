"""Licencia de conducción por conductor y resumen de la última corrida de un cron

Revision ID: m051flotalegal
Revises: m050inv2
Create Date: 2026-09-27

Aditiva, nullable, sin backfill.

conductores.licencia_numero / licencia_categoria / licencia_vence: la licencia
de conducción entra a la política de salida (`flota/dominio/salida.py`).
Vencida, la salida la autoriza solo un administrador; sin cargar es «no se
sabe» y no bloquea. No se inventa ninguna: las filas viejas quedan sin cargar.

cron_latido.ultimo_resumen: lo que la corrida dijo que hizo (el barrido de
avisos de flota: si estaba apagado, qué variables le faltan al servicio que lo
corre, cuántos mandó). El panel de avisos lo lee de la base porque el cron
corre en el worker y el panel en la web: leer el entorno de la web era mentir.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051flotalegal'
down_revision = 'm050inv2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('conductores') as b:
        b.add_column(sa.Column('licencia_numero', sa.String(30), nullable=True))
        b.add_column(sa.Column('licencia_categoria', sa.String(3), nullable=True))
        b.add_column(sa.Column('licencia_vence', sa.Date(), nullable=True))
    with op.batch_alter_table('cron_latido') as b:
        b.add_column(sa.Column('ultimo_resumen', sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table('cron_latido') as b:
        b.drop_column('ultimo_resumen')
    with op.batch_alter_table('conductores') as b:
        b.drop_column('licencia_vence')
        b.drop_column('licencia_categoria')
        b.drop_column('licencia_numero')
