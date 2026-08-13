"""Guardar la factura resuelta en la tarea de packing

Revision ID: m003fepersistida
Revises: m002motivorechazo
Create Date: 2026-08-13

`TareaPacking` guardaba el pedido (`*_pedido_siesa`) y la remisión
(`rm_tipo`/`rm_consec`), pero **no la factura**. La FE la asigna Siesa al
facturar desde la remisión y nadie la anotaba.

Esa ausencia es la causa raíz del job 440 (2026-08-11): como no estaba, seis
sitios del flujo de liquidación le pasaban el PEDIDO a `get_rowids_factura`
—que filtra por `f350_*`, la factura— con la variable llamada `tipo_docto_fe`.
`fe_resolver` resolvió el síntoma consultando Siesa cada vez; esto resuelve la
causa.

Y desbloquea el cruce que cartera necesita: su cartera está por factura, la
lista del WMS estaba por pedido, así que el cruce sería aproximado por cliente
y ventana de fecha. Con la FE anotada es documento contra documento.

Nullable a propósito: una tarea sin despachar no tiene factura, y una
despachada antes de hoy no la tiene anotada. `NULL` significa «no se ha
resuelto», no «no existe» — se llena sola la primera vez que alguien la
resuelva.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm003fepersistida'
down_revision = 'm002motivorechazo'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('fe_tipo', sa.String(length=10), nullable=True))
        batch.add_column(sa.Column('fe_consec', sa.String(length=30), nullable=True))
        # Los dos o ninguno. Media factura —tipo sin consecutivo— no identifica
        # ningún documento y se leería como si sí.
        batch.create_check_constraint(
            'ck_packing_fe_completa',
            '(fe_tipo IS NULL AND fe_consec IS NULL) OR '
            '(fe_tipo IS NOT NULL AND fe_consec IS NOT NULL)')


def downgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_constraint('ck_packing_fe_completa', type_='check')
        batch.drop_column('fe_consec')
        batch.drop_column('fe_tipo')
