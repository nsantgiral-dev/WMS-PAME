"""Si el cliente no paga, la mercancía vuelve — sin cabos sueltos

Revision ID: m045devol
Revises: m044cartera
Create Date: 2026-09-24

La devolución de ruta nacía recién al liquidar: entre el regreso del camión y
la liquidación la mercancía no existía en el WMS. Ahora nace EN_CAMION al
confirmar la parada RECHAZADA/PARCIAL, recepción la recibe (bultos RETORNADO /
FALTANTE) y la cuenta, el reingreso va a una zona NO vendible hasta que la NC
esté aprobada en Siesa, y la aprobación la verifica un cron.

Aditiva y nullable, **sin backfill**: las devoluciones viejas quedan como
estaban (ABIERTA/CONFIRMADA/CANCELADA); los estados nuevos los escribe el
código de acá en adelante.

devoluciones_cliente
- `declaracion_conductor`   JSON: lo que dijo el conductor, tal cual.
- `fecha_llegada`           recepción recibió el camión.
- `vinculada_factura_at`    las líneas se amarraron a las filas de la FE.
- `problema_factura`        lo que impide contarla, en palabras.
- `nc_aprobada_fuente`      SIESA (cron) | MANUAL (botón de respaldo).
- `nc_estado_siesa`, `nc_estado_leido_at`   último f350_ind_estado leído.
- `reingreso_liberado_at`   lo sano salió de la zona DEVOLUCION.
- `cancelada_at`
- CHECK del estado (EN_CAMION, ABIERTA, CONFIRMADA, FALTANTE_TOTAL, CANCELADA).

lineas_devolucion_cliente
- `cantidad_averiada`       parte la línea en sanas/averiadas.
- `ubicacion_liberada_id`   a dónde fue lo sano al aprobarse la NC.
- `valor_unitario`         f470_vlr_neto / f470_cant_base, para valorizar el faltante.
- `f470_rowid_activo` + índice único parcial: una sola devolución ACTIVA por
  línea de factura, en la base.

bultos: `fecha_retorno`, `retorno_por_id` (estados RETORNADO / FALTANTE, sin
CHECK: la columna nunca tuvo uno).

rutas_despacho: `llegada_cerrada_at`, `llegada_cerrada_por_id`.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm045devol'
down_revision = 'm044cartera'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('devoluciones_cliente') as batch:
        batch.add_column(sa.Column('declaracion_conductor', sa.JSON(), nullable=True))
        batch.add_column(sa.Column('fecha_llegada', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('vinculada_factura_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('problema_factura', sa.Text(), nullable=True))
        batch.add_column(sa.Column('nc_aprobada_fuente', sa.String(10), nullable=True))
        batch.add_column(sa.Column('nc_estado_siesa', sa.SmallInteger(), nullable=True))
        batch.add_column(sa.Column('nc_estado_leido_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('reingreso_liberado_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('cancelada_at', sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            'ck_devolucion_cliente_estado',
            "estado IN ('EN_CAMION','ABIERTA','CONFIRMADA','FALTANTE_TOTAL','CANCELADA')")
        batch.create_check_constraint(
            'ck_devolucion_nc_fuente',
            "nc_aprobada_fuente IS NULL OR nc_aprobada_fuente IN ('SIESA','MANUAL')")

    with op.batch_alter_table('lineas_devolucion_cliente') as batch:
        batch.add_column(sa.Column('cantidad_averiada', sa.Numeric(14, 4), nullable=True))
        batch.add_column(sa.Column('ubicacion_liberada_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('f470_rowid_activo', sa.String(20), nullable=True))
        batch.add_column(sa.Column('valor_unitario', sa.Numeric(14, 4), nullable=True))
        batch.create_foreign_key(
            'fk_linea_devolucion_ubicacion_liberada', 'ubicaciones',
            ['ubicacion_liberada_id'], ['id'])
        batch.create_check_constraint(
            'ck_linea_devolucion_averiada',
            'cantidad_averiada IS NULL OR (cantidad_averiada >= 0 '
            'AND cantidad_averiada <= cantidad_devuelta)')
    op.create_index('ix_lineas_devolucion_cliente_rowid', 'lineas_devolucion_cliente',
                    ['f470_rowid'])
    op.create_index('uq_linea_devolucion_rowid_activo', 'lineas_devolucion_cliente',
                    ['f470_rowid_activo'], unique=True,
                    postgresql_where=sa.text('f470_rowid_activo IS NOT NULL'),
                    sqlite_where=sa.text('f470_rowid_activo IS NOT NULL'))

    with op.batch_alter_table('bultos') as batch:
        batch.add_column(sa.Column('fecha_retorno', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('retorno_por_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_bulto_retorno_por', 'usuarios',
                                 ['retorno_por_id'], ['id'])

    with op.batch_alter_table('rutas_despacho') as batch:
        batch.add_column(sa.Column('llegada_cerrada_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('llegada_cerrada_por_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_ruta_llegada_cerrada_por', 'usuarios',
                                 ['llegada_cerrada_por_id'], ['id'])


def downgrade():
    with op.batch_alter_table('rutas_despacho') as batch:
        batch.drop_constraint('fk_ruta_llegada_cerrada_por', type_='foreignkey')
        batch.drop_column('llegada_cerrada_por_id')
        batch.drop_column('llegada_cerrada_at')

    with op.batch_alter_table('bultos') as batch:
        batch.drop_constraint('fk_bulto_retorno_por', type_='foreignkey')
        batch.drop_column('retorno_por_id')
        batch.drop_column('fecha_retorno')

    op.drop_index('uq_linea_devolucion_rowid_activo', table_name='lineas_devolucion_cliente')
    op.drop_index('ix_lineas_devolucion_cliente_rowid', table_name='lineas_devolucion_cliente')
    with op.batch_alter_table('lineas_devolucion_cliente') as batch:
        batch.drop_constraint('ck_linea_devolucion_averiada', type_='check')
        batch.drop_constraint('fk_linea_devolucion_ubicacion_liberada', type_='foreignkey')
        batch.drop_column('valor_unitario')
        batch.drop_column('f470_rowid_activo')
        batch.drop_column('ubicacion_liberada_id')
        batch.drop_column('cantidad_averiada')

    with op.batch_alter_table('devoluciones_cliente') as batch:
        batch.drop_constraint('ck_devolucion_nc_fuente', type_='check')
        batch.drop_constraint('ck_devolucion_cliente_estado', type_='check')
        batch.drop_column('cancelada_at')
        batch.drop_column('reingreso_liberado_at')
        batch.drop_column('nc_estado_leido_at')
        batch.drop_column('nc_estado_siesa')
        batch.drop_column('nc_aprobada_fuente')
        batch.drop_column('problema_factura')
        batch.drop_column('vinculada_factura_at')
        batch.drop_column('fecha_llegada')
        batch.drop_column('declaracion_conductor')
