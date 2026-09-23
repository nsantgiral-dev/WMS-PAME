"""El conteo guarda la foto de Siesa que usó: existencia, POS pendiente y teórico

Revision ID: m028fotosiesaconteo
Revises: m027mergedoscadenas
Create Date: 2026-09-23

## El doble descuento

`API_v2_Inventarios_InvFecha` trae, por ítem × bodega, además de
`f400_cant_existencia_1`, la venta POS que Siesa todavía no acumuló
(`f400_cant_pos_1`). Esa mercancía ya salió del estante. El conteo cíclico
comparaba el físico contra la existencia sola, así que en una tienda con POS
pendiente **fabricaba un faltante igual al POS**, lo ajustaba (automático si
CC1 == CC2), y al día siguiente la acumulación del POS descontaba otra vez.

La base correcta es el **teórico**: `existencia − cant_pos`.

## Por qué se guarda y no se vuelve a leer

El ajuste sale como DELTA. Antes se recalculaba al aprobar contra la
existencia de ese momento: si entre el conteo y la aprobación salía una
remisión, el delta cambiaba — hasta de signo (conteo 95 contra 100; sale una
remisión de 10 → 90; al aprobar 95 − 90 = +5 AJ-ENT, cuando era −5). El delta
se fija con la foto del conteo, y la foto queda en la fila para que se pueda
auditar contra qué se midió.

## Por qué nullable, y qué pasa con las filas viejas

Las sesiones anteriores no tienen foto y no se les inventa una: un NULL acá es
«no se sabe», y el servicio **no aprueba** un ajuste sin foto — igual que ya no
lo aprobaba contra el stock del WMS. Una sesión vieja en DESCUADRE se recuenta.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm028fotosiesaconteo'
down_revision = 'm027mergedoscadenas'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.add_column(sa.Column('cant_pos_siesa', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('salida_sin_conf_siesa', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('teorico_siesa', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('foto_siesa_at', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.drop_column('foto_siesa_at')
        batch.drop_column('teorico_siesa')
        batch.drop_column('salida_sin_conf_siesa')
        batch.drop_column('cant_pos_siesa')
