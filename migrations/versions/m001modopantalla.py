"""Registrar en qué modo estaba la pantalla cuando el conductor confirmó

Revision ID: m001modopantalla
Revises: r0t1a9devolrutas
Create Date: 2026-08-13

`rutas.js:1908` calcula tres modos de pago —CREDITO, DINAMICO, LIBRE— y en
**LIBRE el conductor elige forma de pago sin restricción**, incluido CREDITO en
una parada de contado. `confirmar_parada` solo valida que el valor esté en la
lista; nada lo ata a la condición del pedido.

El modo se calculaba en el navegador y se descartaba. El desglose sabía qué
eligió el conductor, **no qué opciones tenía enfrente** — y esa es justamente la
pregunta de riesgo.

No se puede reconstruir hacia atrás: dependía de datos de Siesa en ese momento
(`es_contado` y si el valor de la factura se conocía). Por eso se registra de
acá en adelante y las filas viejas quedan en NULL, que es honesto: significa
«se confirmó antes de que esto se midiera», no «fue LIBRE».
"""
from alembic import op
import sqlalchemy as sa

revision = 'm001modopantalla'
down_revision = 'r0t1a9devolrutas'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.add_column(sa.Column('modo_pantalla', sa.String(length=12), nullable=True))
        # NULL admitido a propósito y sin default: un default a 'LIBRE' haría
        # que todo lo histórico se contara como el caso de riesgo, y un default
        # a 'DINAMICO' lo escondería. El hueco declarado es el único valor
        # honesto para lo que no se midió.
        batch.create_check_constraint(
            'ck_recaudo_modo_pantalla',
            "modo_pantalla IS NULL OR modo_pantalla IN ('CREDITO','DINAMICO','LIBRE')")


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.drop_constraint('ck_recaudo_modo_pantalla', type_='check')
        batch.drop_column('modo_pantalla')
