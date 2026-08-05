"""Consecutivo real de la NC y trazabilidad del motivo DIAN

Revision ID: nc001motivodian
Revises: f10ta5ubicacion
Create Date: 2026-08-05

El WMS creaba la nota crédito y nunca sabía qué consecutivo le había asignado
Siesa: contabilidad tenía que buscar el documento en Auditoría para aprobarlo.
`siesa_nc_consec` cierra ese hueco, y es además el insumo del segundo POST
(251546) que le pone el motivo DIAN.

`siesa_motivo_dian` es tri-estado a propósito — NULL (sin intentar),
'AUTOMATICO' (el WMS lo puso), 'MANUAL' (no se pudo, va a contabilidad). Las
tres cosas mandan a hacer algo distinto; un booleano las confundiría.

Todo nullable: las devoluciones que ya existen no tienen esta información y no
hay forma de inventarla hacia atrás. NULL aquí significa "no se sabe", que es
la verdad.
"""
from alembic import op
import sqlalchemy as sa

revision = 'nc001motivodian'
down_revision = 'f10ta5ubicacion'
branch_labels = None
depends_on = None


_COLUMNAS = (
    ('siesa_nc_consec', sa.String(30)),
    ('siesa_motivo_dian', sa.String(20)),
    ('siesa_motivo_dian_at', sa.DateTime()),
    ('siesa_motivo_dian_detalle', sa.Text()),
)


def upgrade():
    with op.batch_alter_table('devoluciones_cliente') as batch:
        for nombre, tipo in _COLUMNAS:
            batch.add_column(sa.Column(nombre, tipo, nullable=True))


def downgrade():
    with op.batch_alter_table('devoluciones_cliente') as batch:
        for nombre, _ in reversed(_COLUMNAS):
            batch.drop_column(nombre)
