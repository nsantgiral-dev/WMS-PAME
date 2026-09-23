"""Índices por producto para la guarda de «mercancía en proceso» del conteo

Revision ID: m031guardaenproceso
Revises: m030costoconteo
Create Date: 2026-09-23

## Para qué

`ConteoService.procesos_en_curso` (caso 7 de `motivo_bloqueo_ajuste`) pregunta,
por SKU × almacén, si hay picking recogido sin remisión, recepción sin
EntradaOC o devolución con la NC sin aprobar. `SesionConteo.to_dict` lo evalúa
por cada fila DESCUADRE de un listado, y el generador de conteos lo va a
evaluar por SKU.

Sin estos índices la consulta queda acotada por almacén y no por producto:

- `tareas_picking` solo tenía `(almacen_id, estado)`, y el grueso de la tabla
  es COMPLETADO: cada evaluación recorría todo el picking histórico de NB1;
- `items_recepcion` y `lineas_devolucion_cliente` no tenían ningún índice por
  producto.

Los tres son índices comunes (no únicos) sobre columnas NOT NULL: no pueden
fallar contra datos existentes.
"""
from alembic import op

revision = 'm031guardaenproceso'
down_revision = 'm030costoconteo'
branch_labels = None
depends_on = None

_INDICES = (
    ('ix_tareas_picking_producto_almacen', 'tareas_picking', ['producto_id', 'almacen_id']),
    ('ix_items_recepcion_producto', 'items_recepcion', ['producto_id']),
    ('ix_lineas_devolucion_cliente_producto', 'lineas_devolucion_cliente', ['producto_id']),
)


def upgrade():
    for nombre, tabla, columnas in _INDICES:
        op.create_index(nombre, tabla, columnas)


def downgrade():
    for nombre, tabla, _columnas in reversed(_INDICES):
        op.drop_index(nombre, table_name=tabla)
