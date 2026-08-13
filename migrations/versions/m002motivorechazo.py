"""Motivo tipificado del rechazo, con la pregunta de si la mercancía volvió

Revision ID: m002motivorechazo
Revises: m001modopantalla
Create Date: 2026-08-13

`RECHAZADO` era el estado más barato de los tres: solo pedía observaciones en
texto libre, mientras ENTREGADO pedía forma de pago y PARCIAL pedía además
monto y observaciones. Nadie lo decidió — salió de cómo se fueron agregando las
validaciones. Y el control «si no paga completo, no se entrega» lo empeora: el
conductor que no quiere subir bultos marca el estado que menos pide.

El texto libre además no distingue lo único que importa contablemente: **si la
mercancía volvió**. Un rechazo donde el cliente se quedó con ella y no pagó no
es un rechazo — es un crédito sin registro, con el sistema creyendo que el
inventario volvió al camión.

`motivo_rechazo` queda nullable: las paradas confirmadas antes de hoy no lo
tienen, y eso es «no se preguntó», no «no hubo motivo». Un default a cualquier
código inventaría un dato que nadie dio.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm002motivorechazo'
down_revision = 'm001modopantalla'
branch_labels = None
depends_on = None

# La lista se escribe literal en el CHECK y no se importa del código: una
# migración tiene que poder aplicarse dentro de dos años sobre un repo donde el
# catálogo ya cambió. Si se agrega un motivo, va una migración nueva.
_CODIGOS = ("'CLIENTE_CERRADO','DIRECCION_ERRADA','NO_PIDIO',"
            "'MERCANCIA_AVERIADA','FUERA_DE_HORARIO','NO_PAGO','NO_PAGO_SE_QUEDO'")


def upgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.add_column(sa.Column('motivo_rechazo', sa.String(length=30), nullable=True))
        batch.create_check_constraint(
            'ck_recaudo_motivo_rechazo',
            f'motivo_rechazo IS NULL OR motivo_rechazo IN ({_CODIGOS})')


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.drop_constraint('ck_recaudo_motivo_rechazo', type_='check')
        batch.drop_column('motivo_rechazo')
