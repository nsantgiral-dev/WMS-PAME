"""Guardar el valor de la factura en la tarea de packing

Revision ID: m004valorparada
Revises: m003fepersistida
Create Date: 2026-08-13

El valor de una parada se calcula al vuelo en `_valor_y_cond_pago` sumando
`f470_vlr_neto` de las líneas de la FE, se muestra en la pantalla del conductor
y se descarta. No queda en ningún lado.

Hace falta para la pantalla del conductor: el valor a cobrar, el tope del monto
parcial y la deducción por línea no entregada salen de ahí (`rutas.js`).

ADVERTENCIA (2026-08-13, posterior a esta migración). Esta columna se creó
además para calcular **el tope de exposición de contado** —el monto por debajo
del cual una parada pasaba sin evaluación de crédito—. **Ese diseño quedó
retirado** por BK-OPS-01 v2.1: dependía de que la factura se emitiera en la
liquidación, y una factura de contado no se aprueba sin recaudo. La columna se
queda porque la pantalla del conductor la usa; el tope no se implementa. Se
deja escrito para que nadie lo retome leyendo esta migración.

Y hay un riesgo de medición que esto evita: `recaudos_entrega.monto_cobrado`
existe y es tentador usarlo como proxy. **No es lo mismo** — es lo que se
cobró, no lo que estaba en riesgo. En una parada rechazada vale cero, y en una
parcial vale menos que la exposición. Poner un tope sobre esa variable lo
pondría sistemáticamente bajo.

Nullable: una tarea sin FE resuelta no tiene valor conocido, y `NULL` significa
«no se ha calculado», no «vale cero». Un default a 0 haría que toda parada
histórica entrara al percentil más bajo y hundiera cualquier umbral.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm004valorparada'
down_revision = 'm003fepersistida'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('valor_factura', sa.Numeric(14, 2), nullable=True))
        # Un valor negativo no es una parada: sería un error de suma o una FE
        # mal leída. Cero sí es posible (factura anulada, líneas en cero).
        batch.create_check_constraint(
            'ck_packing_valor_no_negativo',
            'valor_factura IS NULL OR valor_factura >= 0')


def downgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_constraint('ck_packing_valor_no_negativo', type_='check')
        batch.drop_column('valor_factura')
