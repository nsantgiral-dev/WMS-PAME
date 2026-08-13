"""Guardar la condición de pago declarada por el pedido

Revision ID: m005condpagoparada
Revises: m004valorparada
Create Date: 2026-08-13

`f430_id_cond_pago` se consulta a Siesa cada vez que se arma el detalle de una
ruta, se usa para decidir qué ve el conductor, y se descarta. No queda anotada
en ninguna parte.

Hacen falta tres cosas que hoy no se pueden hacer:

  1. **Aplicar la restricción de la entrega.** El diseño acordado prohíbe
     `forma_pago = CREDITO` sobre una parada que el pedido declaró de contado.
     `confirmar_parada` no puede validarlo: no tiene el dato, y **tiene que
     funcionar sin señal**, así que no puede ir a preguntarle a Siesa.
  2. **Responder qué condición llevan los pedidos de ruta.** Es una de las
     verificaciones pendientes —«abrir un pedido en Siesa, dos minutos»— y con
     esto se responde sobre todos los pedidos a la vez, no sobre uno.
  3. Dejar de consultar Siesa por cada parada en cada carga de ruta.

Nullable: una parada cuyo detalle nunca se cargó en línea no lo tiene. `NULL`
significa **«no se ha consultado»**, distinto de `''`, que significa «Siesa
respondió y el pedido no trae condición». Esa distinción es la que el booleano
`es_contado` colapsaba, y colapsarla fue el defecto que puso «Valor a Cobrar»
en la pantalla del conductor para clientes de condición desconocida.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm005condpagoparada'
down_revision = 'm004valorparada'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('cond_pago', sa.String(length=10), nullable=True))


def downgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_column('cond_pago')
