"""Recepción no podía decir que algo llegó roto: solo contar de menos

Revision ID: m025averiaenrecepcion
Revises: m024triggersfaltantes
Create Date: 2026-09-16

## El hueco

El proceso del negocio arranca en recepción: «llegó mal un producto, la auxiliar
de compras se contacta con el proveedor para pedir nota crédito o devolución».

El sistema no tenía dónde anotarlo. El payload del escaneo acepta producto,
cantidad, empaque, bonificación, lote y vencimiento — ningún campo de avería, ni
de motivo. Si llegaban 100 y 20 venían rotas, el recepcionista escaneaba 80, y
eso queda **idéntico a «el proveedor mandó menos»**: mismo `es_faltante()`,
mismo `es_parcial` hacia Siesa, misma fila en el panel de discrepancias.

`ItemRecepcion.destino` declaraba un valor `'BLOQUEADO'` en un comentario desde
el inicio y **ningún camino de código lo escribía nunca**. Un casillero hecho y
vacío.

## Por qué la avería SE CUENTA como recibida

`cantidad_averiada` es un subconjunto de `cantidad_recibida`, no una resta. Lo
que viaja a Siesa en la entrada por OC (142948) sigue siendo `cantidad_recibida`
— o sea que la unidad rota entra a la OC.

Lo decide el proceso, no una preferencia: «si dan nota crédito se da de baja, o
si no se devuelve». Una NC **reversa una entrada**, y devolver exige tener la
mercancía. Los dos desenlaces presuponen que entró.

Y no se le quita nada a nadie: quien prefiera no ingresarla sigue pudiendo
contar de menos, que es el comportamiento de hoy.

## Los dos CHECK

`cantidad_averiada >= 0` y `cantidad_averiada <= cantidad_recibida`. El segundo
no es cosmético: sin él, el reparto de `confirmar_recepcion` mandaría una
cantidad negativa al destino bueno y el `ck_cantidad_no_negativa` de
`UbicacionProducto` reventaría DESPUÉS, con la recepción a medio confirmar y el
POST a Siesa ya disparado.

SQLite no soporta agregar un CHECK con ALTER; en ese motor las columnas se
agregan y los CHECK los aplica `create_all` al armar el esquema de tests.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm025averiaenrecepcion'
down_revision = 'm024triggersfaltantes'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table('items_recepcion') as batch:
        batch.add_column(sa.Column('cantidad_averiada', sa.Integer(),
                                   nullable=False, server_default='0'))
        batch.add_column(sa.Column('motivo_averia', sa.String(length=200),
                                   nullable=True))
    if bind.dialect.name == 'postgresql':
        op.create_check_constraint(
            'ck_item_recepcion_averiada_no_negativa', 'items_recepcion',
            'cantidad_averiada >= 0')
        op.create_check_constraint(
            'ck_item_recepcion_averiada_subconjunto', 'items_recepcion',
            'cantidad_averiada <= cantidad_recibida')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.drop_constraint('ck_item_recepcion_averiada_subconjunto',
                           'items_recepcion', type_='check')
        op.drop_constraint('ck_item_recepcion_averiada_no_negativa',
                           'items_recepcion', type_='check')
    with op.batch_alter_table('items_recepcion') as batch:
        batch.drop_column('motivo_averia')
        batch.drop_column('cantidad_averiada')
