"""Conteo: qué dijo la tolerancia del primer conteo, y si la cadena se cerró por ella

Revision ID: m032tol
Revises: m031hud
Create Date: 2026-09-23

## Para qué

Desde 2026-09-23 un primer conteo con una diferencia chica —dentro de la
tolerancia de su clase y del tope en pesos— se acepta sin segundo conteo y la
diferencia se ajusta (`conteo_politica.evaluar_tolerancia`). Dos hechos de ese
momento no se pueden reconstruir después, y por eso se guardan:

- **`tolerancia_primer_conteo`** — `EXACTO` | `DENTRO` | `FUERA`: qué dijo la
  tolerancia VIGENTE del primer conteo válido de la cadena. Es el acierto de la
  exactitud con tolerancia (ASCM: acierto = primer conteo dentro de tolerancia).
  Recalcularlo después con la configuración de hoy cambiaría el pasado cada vez
  que alguien mueve una variable, y el físico del CC1 se pisa cuando resuelve el
  CC2 (`_copiar_observacion`).
- **`ajuste_por_tolerancia`** — la cadena se cerró SIN segundo conteo porque el
  primer conteo (o su único recuento propio) quedó dentro. Lo lee el invariante
  CNT-04, que antes marcaba todo ajuste sin segundo conteo: ahora ese salto es
  una decisión de producto, y tiene que poder distinguirse del salto indebido
  (`omitir-segundo`). Lo escribe SOLO la rama de tolerancia de
  `registrar_conteo`.

## Sin backfill

Las cadenas anteriores quedan en `NULL`: se evaluaron con la regla vieja (toda
diferencia → segundo conteo) y no hay con qué saber qué habría dicho la
tolerancia. Las estadísticas las declaran en `excluidos`, no las adivinan.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm032tol'
down_revision = 'm031hud'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.add_column(sa.Column('tolerancia_primer_conteo', sa.String(10), nullable=True))
        batch.add_column(sa.Column('ajuste_por_tolerancia', sa.Boolean(), nullable=True))


def downgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.drop_column('ajuste_por_tolerancia')
        batch.drop_column('tolerancia_primer_conteo')
