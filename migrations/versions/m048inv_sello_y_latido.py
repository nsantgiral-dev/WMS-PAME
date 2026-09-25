"""Sello de ambiente de la base y latido de los crons

Revision ID: m048inv
Revises: m047comprasvivo
Create Date: 2026-09-25

Aditiva, sin backfill.

sello_ambiente (nueva, P0-9)
- una fila: el `RAILWAY_ENVIRONMENT_NAME` del proceso que habló con Siesa por
  primera vez sobre esta base. Un proceso de otro ambiente no postea: una
  copia de producción restaurada en QA queda sellada `production`.

cron_latido (nueva, P1-11)
- una fila por (cron, servicio): última corrida, si terminó bien, el error,
  fallos seguidos. La escribe el decorador de cada job de APScheduler.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm048inv'
down_revision = 'm047comprasvivo'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sello_ambiente',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ambiente', sa.String(40), nullable=False),
        sa.Column('sellado_en', sa.DateTime(), nullable=False),
        sa.Column('sellado_por', sa.String(80), nullable=False),
        sa.Column('motivo', sa.Text(), nullable=True),
        sa.Column('ambiente_anterior', sa.String(40), nullable=True),
    )
    op.create_table(
        'cron_latido',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('nombre', sa.String(80), nullable=False),
        sa.Column('servicio', sa.String(60), nullable=False),
        sa.Column('ultimo_inicio', sa.DateTime(), nullable=True),
        sa.Column('ultimo_fin', sa.DateTime(), nullable=True),
        sa.Column('ultimo_ok', sa.Boolean(), nullable=True),
        sa.Column('ultimo_ok_en', sa.DateTime(), nullable=True),
        sa.Column('ultimo_error', sa.Text(), nullable=True),
        sa.Column('corridas', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('fallos_seguidos', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('nombre', 'servicio', name='uq_cron_latido_nombre_servicio'),
    )


def downgrade():
    op.drop_table('cron_latido')
    op.drop_table('sello_ambiente')
