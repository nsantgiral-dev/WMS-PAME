"""Fase 1 de analítica: el KPI diario (capa semántica)

Revision ID: m037kpi
Revises: m036fotos
Create Date: 2026-09-24

Una tabla, aditiva: `analitica_kpi_diario`, una fila por día operativo
(Bogotá) × almacén (NULL = total) × métrica. La escribe
`app/services/analitica_kpi.py` y nadie más.

- **Único por (día, almacén, métrica) con dos índices parciales.** Un UNIQUE
  normal sobre una columna nullable deja pasar dos filas «total» del mismo día
  y métrica: PostgreSQL considera distinto cada NULL.
- `estado` ∈ OK / INCOMPLETO / AUSENTE, con dos CHECK: AUSENTE va sin valor (un
  AUSENTE con 0 es el cero fabricado que la tabla existe para impedir) y lo
  que no es OK lleva motivo.
- **Sin claves foráneas**: PROTEGIDA_ANALITICA en el acta de corte e
  IRRECUPERABLE en la verificación de respaldo. Sobrevive al corte; las filas
  de las que salió cada número, no.

Sin backfill: el cron (`ANALITICA_KPI`, nace apagado) o
`POST /api/analitica/kpi/recalcular` llenan los días.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm037kpi'
down_revision = 'm036fotos'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'analitica_kpi_diario',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dia_operativo', sa.Date(), nullable=False),
        sa.Column('almacen_id', sa.Integer(), nullable=True),
        sa.Column('metrica', sa.String(40), nullable=False),
        sa.Column('valor', sa.Numeric(20, 6), nullable=True),
        sa.Column('n', sa.Integer(), nullable=True),
        sa.Column('numerador', sa.Numeric(20, 4), nullable=True),
        sa.Column('denominador', sa.Numeric(20, 4), nullable=True),
        sa.Column('estado', sa.String(12), nullable=False),
        sa.Column('motivo', sa.Text(), nullable=True),
        sa.Column('fuente', sa.String(160), nullable=True),
        sa.Column('detalle', sa.Text(), nullable=True),
        sa.Column('calculado_en', sa.DateTime(), nullable=False),
        sa.CheckConstraint("estado IN ('OK','INCOMPLETO','AUSENTE')",
                           name='ck_kpi_diario_estado'),
        sa.CheckConstraint("(estado = 'AUSENTE') = (valor IS NULL)",
                           name='ck_kpi_diario_ausente_sin_valor'),
        sa.CheckConstraint("estado = 'OK' OR motivo IS NOT NULL",
                           name='ck_kpi_diario_motivo'),
    )
    op.create_index('uq_kpi_diario_almacen', 'analitica_kpi_diario',
                    ['dia_operativo', 'almacen_id', 'metrica'], unique=True,
                    postgresql_where=sa.text('almacen_id IS NOT NULL'),
                    sqlite_where=sa.text('almacen_id IS NOT NULL'))
    op.create_index('uq_kpi_diario_total', 'analitica_kpi_diario',
                    ['dia_operativo', 'metrica'], unique=True,
                    postgresql_where=sa.text('almacen_id IS NULL'),
                    sqlite_where=sa.text('almacen_id IS NULL'))
    op.create_index('ix_kpi_diario_metrica_dia', 'analitica_kpi_diario',
                    ['metrica', 'dia_operativo'])


def downgrade():
    op.drop_index('ix_kpi_diario_metrica_dia', table_name='analitica_kpi_diario')
    op.drop_index('uq_kpi_diario_total', table_name='analitica_kpi_diario')
    op.drop_index('uq_kpi_diario_almacen', table_name='analitica_kpi_diario')
    op.drop_table('analitica_kpi_diario')
