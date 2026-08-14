"""ENTREGADO_SIN_PAGO deja de ser un motivo y pasa a ser un estado

Revision ID: m006entregadosinpago
Revises: m005condpagoparada
Create Date: 2026-08-13

`NO_PAGO_SE_QUEDO` vivía como motivo dentro del estado `RECHAZADO`. **El estado
afirmaba una cosa y el motivo la negaba**: `RECHAZADO` significa que los bultos
vuelven al camión, y ese motivo dice justamente que no volvieron.

Mientras fue excepcional se podía vivir con la contradicción. El control «si no
paga completo, no se entrega» la vuelve cotidiana, y entonces **cada consumidor
del estado tiene que acordarse de mirar el motivo**. Alguno se olvida — ya pasó
una vez: la lista de reingreso mandaba a bodega a buscar cajas que nunca
volvieron.

Decidido el 2026-08-13 por Dirección de Operaciones, en línea con BK-OPS-01
v2.1 §4.2: *«Corresponde a un estado propio, no a un motivo: físicamente la
mercancía se entregó y lo que falta es el pago.»*

## Qué hace esta migración

1. Reclasifica las filas históricas `RECHAZADO` + `NO_PAGO_SE_QUEDO` al estado
   nuevo. El motivo **se conserva** — sigue diciendo por qué, y es lo que
   distingue este caso de otras formas de quedar sin pagar.
2. Dos CHECK: el catálogo de estados, y el invariante que impide que la
   combinación vieja vuelva a entrar por cualquier camino que no pase por
   `confirmar_parada`.

## Lo que NO hace, a propósito

**No toca el estado de los bultos históricos.** Hacia adelante, una parada
`ENTREGADO_SIN_PAGO` deja sus bultos en `ENTREGADO` —no volvieron, marcarlos
`RECHAZADO` diría que están en el camión—, pero reescribir el estado físico de
bultos ya registrados es un movimiento de inventario que nadie pidió y que
ningún conteo respalda. Regla 0.

`bultos_rechazados()` cubre los dos casos: trae los bultos de la parada, esté
el bulto marcado como esté. El bloque lo define la parada, no el bulto.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm006entregadosinpago'
down_revision = 'm005condpagoparada'
branch_labels = None
depends_on = None

_ESTADOS = "'ENTREGADO','PARCIAL','RECHAZADO','ENTREGADO_SIN_PAGO'"


def upgrade():
    # Primero los datos: un CHECK sobre filas que lo violan aborta la migración
    # y deja el deploy caído.
    op.execute(
        "UPDATE recaudos_entrega SET estado_entrega = 'ENTREGADO_SIN_PAGO' "
        "WHERE motivo_rechazo = 'NO_PAGO_SE_QUEDO'"
    )
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.create_check_constraint(
            'ck_recaudo_estado_entrega', f'estado_entrega IN ({_ESTADOS})')
        batch.create_check_constraint(
            'ck_recaudo_sin_pago_no_es_rechazo',
            "motivo_rechazo IS NULL OR motivo_rechazo <> 'NO_PAGO_SE_QUEDO' "
            "OR estado_entrega = 'ENTREGADO_SIN_PAGO'")


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.drop_constraint('ck_recaudo_sin_pago_no_es_rechazo', type_='check')
        batch.drop_constraint('ck_recaudo_estado_entrega', type_='check')
    op.execute(
        "UPDATE recaudos_entrega SET estado_entrega = 'RECHAZADO' "
        "WHERE estado_entrega = 'ENTREGADO_SIN_PAGO'"
    )
