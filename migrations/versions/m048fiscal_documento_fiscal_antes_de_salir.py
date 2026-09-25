"""Fiscal y despacho: ninguna mercancía sale sin documento

Revision ID: m048fiscal
Revises: m047comprasvivo
Create Date: 2026-09-25

Aditiva y nullable. Tres columnas en `tareas_packing`:

- `rm_enviada_at`             pre-flag de la Regla 6 para el 142945: se escribe
                              ANTES del POST de la remisión y solo se revierte
                              ante un rechazo explícito de Siesa. Con él puesto
                              y sin `rm_consec`, la remisión PUEDE existir: nadie
                              reenvía el 142945 hasta identificarla.
- `fe_confirmada_at`          la factura existe: el 142943 respondió `codigo:0`
                              o se encontró en Siesa con su consecutivo. El
                              muelle y la ruta exigen RM + FE confirmadas.
- `reconciliacion_intento_at` el último intento del barrido de reconciliación,
                              para rotar (antes `.limit(10)` sin orden: diez
                              tareas imposibles tapaban a la once para siempre).

**Un backfill, imprescindible:** `fe_confirmada_at` de las tareas que ya
tienen `siesa_triggered` y `rm_consec`. Hasta hoy `siesa_triggered` con
remisión solo se escribía en `_persistir_resultado`, **después** de que el
142943 respondía bien o de encontrar la factura en Siesa — o sea que esas
filas tienen FE. Sin el backfill, sus bultos pendientes desaparecerían del
muelle el día del deploy. Las tareas con `siesa_triggered` y SIN remisión
(la rama `244328-AUTO`, la reconciliación por estado 9) no se tocan: esas son
exactamente las que ahora no pueden salir.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm048fiscal'
down_revision = 'm047comprasvivo'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('rm_enviada_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('fe_confirmada_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('reconciliacion_intento_at', sa.DateTime(),
                                   nullable=True))

    op.execute(sa.text(
        "UPDATE tareas_packing "
        "SET fe_confirmada_at = COALESCE(siesa_triggered_at, fecha_despachado, "
        "                                fecha_creacion) "
        "WHERE siesa_triggered = :si AND rm_consec IS NOT NULL "
        "AND fe_confirmada_at IS NULL "
        "AND (tipo_documento IS NULL OR tipo_documento <> 'TRASLADO')"
    ).bindparams(si=True))


def downgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_column('reconciliacion_intento_at')
        batch.drop_column('fe_confirmada_at')
        batch.drop_column('rm_enviada_at')
