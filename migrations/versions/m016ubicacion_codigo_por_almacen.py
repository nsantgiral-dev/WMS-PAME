"""`ubicaciones.codigo` era único GLOBAL — bloqueaba dos bodegas con SIESA-GENERAL

Revision ID: m016ubicacioncodigoporalmacen
Revises: m015nombresindiceskardex
Create Date: 2026-08-27

Descubierto en producción al calibrar NS1 (Fase 1 de calibración de tiendas,
ver `inventario_siesa_service._get_o_crear_ubicacion_general`): la tabla
`ubicaciones` tenía `UniqueConstraint('codigo')` sola, desde la migración
inicial (`bf21c9318db2`). Nunca se notó porque hasta hoy `SIESA-GENERAL`
solo se creaba para NB1 — el único almacén que alguna vez tuvo layout físico.

Al generalizar la carga inicial a NS1, crear su propio `SIESA-GENERAL`
(almacén distinto, mismo código) chocó con el de NB1:

    duplicate key value violates unique constraint "ubicaciones_codigo_key"
    DETAIL: Key (codigo)=(SIESA-GENERAL) already exists.

`SIESA-GENERAL` es un bucket genérico por almacén, no un código físico
global — el invariante real siempre fue "único DENTRO de un almacén", nunca
"único en todo el WMS". La restricción global era más estricta de lo que el
negocio necesitaba, y nadie lo pagó hasta que un segundo almacén la usó.

No reconstruye la tabla ni mueve datos — solo cambia qué combinación de
columnas exige la base como única.
"""
from alembic import op

revision = 'm016ubicacioncodigoporalmacen'
down_revision = 'm015nombresindiceskardex'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint('ubicaciones_codigo_key', 'ubicaciones', type_='unique')
    op.create_unique_constraint('uq_ubicacion_almacen_codigo', 'ubicaciones', ['almacen_id', 'codigo'])


def downgrade():
    op.drop_constraint('uq_ubicacion_almacen_codigo', 'ubicaciones', type_='unique')
    op.create_unique_constraint('ubicaciones_codigo_key', 'ubicaciones', ['codigo'])
