"""eventos_stock_agotado sobrevive al acta de corte y declara «sin precio»

Revision ID: m034agotado
Revises: m033ciclo
Create Date: 2026-09-24

## El acta de corte no podía correr

`eventos_stock_agotado` es PROTEGIDA_ANALITICA en `reset_transaccional.py`
(evento hacia adelante del tablero BI, no reconstruible), pero su
`tarea_picking_id` era FK **NOT NULL sin `ondelete`** hacia `tareas_picking`,
que es OPERATIVA. En Postgres el `DELETE FROM tareas_picking` del corte falla
por clave foránea en cuanto exista un solo evento: el bucle lo imprime como
aviso y el corte termina RESET INCOMPLETO. La tabla que el corte promete
conservar es la que le impide cortar.

Ahora:

- `tarea_picking_id` nullable con `ON DELETE SET NULL`: la tarea del ensayo se
  va, el evento se queda.
- El evento guarda lo que necesitaba de la tarea para no depender de ella:
  `tarea_codigo` y `cantidad_solicitada` (el pedido, el producto y el faltante
  ya estaban copiados). Se rellenan desde `tareas_picking` mientras las tareas
  todavía existen.

## «Sin precio» no es cero

`precio_venta_capturado` se llenaba con `producto.precio_venta or 0`, y
`Producto.precio_venta` no lo puebla ninguna sincronización (ver
`costo_service`): todo evento valía $0, y la métrica de venta perdida sumaba
ceros que parecían dato. Pasa a nullable; `NULL` es «sin precio» y la métrica
lo cuenta aparte. Los ceros ya guardados salen de ese mismo `or 0` —ningún
producto se vende a $0— así que se pasan a `NULL`.

## Portable

Las columnas y la nulabilidad van por `batch_alter_table`. El cambio de la FK
solo se hace en PostgreSQL, buscando el nombre real del constraint por
inspección (se creó sin nombre en m019): SQLite no hace cumplir las claves
foráneas por defecto y los tests arman el esquema con `create_all()` desde el
modelo, que ya declara el `ondelete`.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm034agotado'
down_revision = 'm033ciclo'
branch_labels = None
depends_on = None

_TABLA = 'eventos_stock_agotado'
_FK_NUEVA = 'fk_eventos_stock_agotado_tarea_picking'


def _fk_tarea(bind):
    for fk in sa.inspect(bind).get_foreign_keys(_TABLA):
        if fk.get('constrained_columns') == ['tarea_picking_id']:
            return fk.get('name')
    return None


def upgrade():
    bind = op.get_bind()
    with op.batch_alter_table(_TABLA) as batch:
        batch.add_column(sa.Column('tarea_codigo', sa.String(length=50), nullable=True))
        batch.add_column(sa.Column('cantidad_solicitada', sa.Integer(), nullable=True))
        batch.alter_column('tarea_picking_id', existing_type=sa.Integer(), nullable=True)
        batch.alter_column('precio_venta_capturado',
                           existing_type=sa.Numeric(precision=12, scale=2),
                           nullable=True)

    op.execute(
        'UPDATE eventos_stock_agotado SET '
        'tarea_codigo = (SELECT t.codigo FROM tareas_picking t '
        '                WHERE t.id = eventos_stock_agotado.tarea_picking_id), '
        'cantidad_solicitada = (SELECT t.cantidad_solicitada FROM tareas_picking t '
        '                       WHERE t.id = eventos_stock_agotado.tarea_picking_id)')
    op.execute('UPDATE eventos_stock_agotado SET precio_venta_capturado = NULL '
               'WHERE precio_venta_capturado = 0')

    if bind.dialect.name == 'postgresql':
        vieja = _fk_tarea(bind)
        if vieja:
            op.drop_constraint(vieja, _TABLA, type_='foreignkey')
        op.create_foreign_key(_FK_NUEVA, _TABLA, 'tareas_picking',
                              ['tarea_picking_id'], ['id'], ondelete='SET NULL')


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        vieja = _fk_tarea(bind)
        if vieja:
            op.drop_constraint(vieja, _TABLA, type_='foreignkey')
        op.create_foreign_key('eventos_stock_agotado_tarea_picking_id_fkey', _TABLA, 'tareas_picking',
                              ['tarea_picking_id'], ['id'])
    # NO se restaura el NOT NULL de `tarea_picking_id` ni de
    # `precio_venta_capturado`: exigiría borrar los eventos cuya tarea ya se
    # vació en un corte, o reescribir «sin precio» como $0. Esta tabla es
    # memoria analítica no reconstruible; un downgrade no la destruye.
    with op.batch_alter_table(_TABLA) as batch:
        batch.drop_column('cantidad_solicitada')
        batch.drop_column('tarea_codigo')
