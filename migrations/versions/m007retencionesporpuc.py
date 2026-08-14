"""Idempotencia de retenciones POR DOCUMENTO, no por recaudo

Revision ID: m007retencionesporpuc
Revises: m006entregadosinpago
Create Date: 2026-08-13

`siesa_dc_triggered` es un booleano y las retenciones son **N**: un cliente con
retefuente + reteIVA + ICA genera tres documentos contables distintos, cada uno
con su cuenta PUC.

Con una sola bandera, el primer job que envía la enciende y **los otros dos
leen la guarda y se declaran idempotentes sin enviar nada**. El tablero cuenta
tres jobs completados y en Siesa hay un documento.

Esta migración agrega `siesa_dc_pucs`: la lista de cuentas PUC que ya se
enviaron. La guarda pasa a ser «¿ya mandé ESTA cuenta?» en vez de «¿ya mandé
alguna?».

## Por qué una columna y no derivarlo de los jobs

Se podría preguntar «¿hay otro job COMPLETADO con esta misma cuenta?». No
alcanza: el caso que la bandera existe para cubrir es el **crash entre el POST
y el commit** (Regla 6). Ese job no queda COMPLETADO — vuelve a PENDIENTE a los
10 minutos por la recuperación de atascados, y su reintento no encontraría
rastro de que ya envió.

La columna se escribe ANTES del POST y se revierte solo ante fallo explícito,
que es el mismo patrón que ya usan RC y NC.

## `siesa_dc_triggered` se conserva

Sigue significando «se envió al menos una retención» y lo leen el tablero y el
desglose. Lo que deja de ser es **la guarda**.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm007retencionesporpuc'
down_revision = 'm006entregadosinpago'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.add_column(sa.Column('siesa_dc_pucs', sa.Text(), nullable=True))
    # Los recaudos que ya tenían la bandera encendida se dan por enviados en
    # bloque: no hay forma de saber cuáles cuentas entraron y cuáles no.
    # Marcarlos con lista vacía los haría reenviar TODO, que es peor que
    # dejarlos como están — un documento contable duplicado es un ajuste
    # manual en el ERP. Regla 0: el lado conservador es no reenviar.
    op.execute(
        "UPDATE recaudos_entrega SET siesa_dc_pucs = '[\"__HISTORICO__\"]' "
        # `WHERE <bool>` y no `= 1`: en Postgres `boolean = integer` es un
        # error de tipo, y esto corre en el `releaseCommand` — habría tumbado
        # el deploy entero. SQLite lo aceptaba, y los tests usan `create_all()`,
        # así que **ninguna prueba lo ejercitaba**.
        "WHERE siesa_dc_triggered"
    )


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.drop_column('siesa_dc_pucs')
