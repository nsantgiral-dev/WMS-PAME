"""kardex: hash_origen + clave natural — hace la descarga idempotente

Revision ID: c4a9e1f70b28
Revises: b8c2d4e6f013
Create Date: 2026-07-27

La ingesta del kardex era un INSERT plano sobre un índice NO único. Dos
consecuencias, y la segunda es la peligrosa:

  · Pulsar "Descargar" dos veces duplicaba el kardex entero.
  · Reanudar desde una página con orden inestable duplicaba el solape.

La duplicación es PEOR que la omisión: el perfil mensual no la delata —un mes
con 8% de filas de más se ve plausible— y los movimientos duplicados inflan la
demanda, que infla el ROP, que infla el contenedor.

hash_origen es SHA-256 sobre la tupla que identifica el movimiento, incluida
la clave natural del documento (consecutivo + línea) cuando Siesa la envía.
El índice único lo vuelve idempotente por construcción: repetir o reanudar ya
no puede duplicar, sin necesidad de conocer el orden de la consulta.

El backfill deja hash_origen NULL en lo ya cargado. El índice único es PARCIAL
en Postgres (WHERE hash_origen IS NOT NULL) para que los NULL no colisionen
entre sí; en SQLite los NULL ya se consideran distintos.
"""
from alembic import op
import sqlalchemy as sa

revision = 'c4a9e1f70b28'
down_revision = 'b8c2d4e6f013'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('kardex_movimientos', sa.Column('consec_docto', sa.String(20), nullable=True))
    op.add_column('kardex_movimientos', sa.Column('nro_registro', sa.Integer(), nullable=True))
    op.add_column('kardex_movimientos', sa.Column('hash_origen', sa.String(64), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        # Parcial: varias filas con hash NULL no deben colisionar
        op.execute(
            'CREATE UNIQUE INDEX ix_kardex_hash_origen ON kardex_movimientos '
            '(hash_origen) WHERE hash_origen IS NOT NULL'
        )
    else:
        op.create_index('ix_kardex_hash_origen', 'kardex_movimientos',
                        ['hash_origen'], unique=True)


def downgrade():
    op.drop_index('ix_kardex_hash_origen', table_name='kardex_movimientos')
    op.drop_column('kardex_movimientos', 'hash_origen')
    op.drop_column('kardex_movimientos', 'nro_registro')
    op.drop_column('kardex_movimientos', 'consec_docto')
