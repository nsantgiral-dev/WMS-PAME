"""El número del conteo dice contra qué se comparó

Revision ID: m009fuenteexistencia
Revises: m008compromisosrit
Create Date: 2026-08-15

`existencia_siesa` es un nombre que promete procedencia y no la tenía: cuando
Siesa no respondía, el servicio caía al stock del WMS **con solo un WARNING** y
lo guardaba en la misma columna. El propio docstring de `comparar_conteo` lo
admitía — *«compara contra existencia_siesa (que ahora almacena stock WMS)»*.

Y el ajuste que sale a Siesa es un **delta**: `fisica − existencia_siesa`. Con
la base tomada del WMS, Siesa queda en `siesa_real + (fisica − wms)` en vez de
en `fisica`. Es decir: **precisamente cuando las dos bases discrepan —la única
razón para contar— el ajuste empeora el descuadre.**

Sin una columna que registrara la procedencia, ningún invariante podía
comprobarlo: el defecto no era solo indetectable, era **inauditable**.

## El histórico queda en NULL, a propósito

`NULL` es «no se sabe con qué se comparó», que es la verdad de todas las filas
anteriores. Rellenarlas con `'SIESA'` sería inventar la procedencia justo en el
campo que existe para no inventarla, y `CNT-07` las daría por buenas.

Por eso el invariante solo bloquea sobre `'WMS'` explícito: `NULL` es histórico
declarado, no una afirmación.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm009fuenteexistencia'
down_revision = 'm008compromisosrit'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.add_column(sa.Column('fuente_existencia', sa.String(10), nullable=True))


def downgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.drop_column('fuente_existencia')
