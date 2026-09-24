"""Fugas de plata en ruta: comprobante, hora del teléfono, distancia al cliente, declarado vs contado

Revision ID: m041flfugas
Revises: m037kpi
Create Date: 2026-09-24

Aditiva y nullable, sin backfill. Un NULL en cualquiera de estas columnas
significa «la parada se confirmó antes de que esto se midiera» — nunca cero,
nunca «coherente».

recaudos_entrega
- `referencia_pago`   (30) últimos dígitos del comprobante de transferencia /
                       consignación. Viaja a `F358_REFERENCIA_OTROS` del 142888
                       (Alfanumérico 30, spec DOCX).
- `foto_comprobante`  foto-dato del comprobante (base64, sin recomprimir).
- `ts_dispositivo`    hora del teléfono en el instante de confirmar (UTC).
- `ts_desfase_s`      teléfono − servidor, en segundos, medido al sincronizar.
                       Positivo = teléfono adelantado.
- `via_cola`          llegó desde la cola offline.
- `distancia_cliente_m` distancia de la captura GPS al punto del maestro del
                       cliente, medida ANTES de que la captura vote.

entregas_geo
- `ts_dispositivo`, `pos_ts_dispositivo` (el `pos.timestamp` del navegador).

lineas_devolucion_cliente
- `cantidad_declarada` lo que declaró el conductor. `cantidad_devuelta` sigue
  siendo lo contado por recepción; la diferencia es el faltante de retorno.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm041flfugas'
down_revision = 'm037kpi'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.add_column(sa.Column('referencia_pago', sa.String(30), nullable=True))
        batch.add_column(sa.Column('foto_comprobante', sa.Text(), nullable=True))
        batch.add_column(sa.Column('ts_dispositivo', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('ts_desfase_s', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('via_cola', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('distancia_cliente_m', sa.Numeric(10, 1), nullable=True))

    with op.batch_alter_table('entregas_geo') as batch:
        batch.add_column(sa.Column('ts_dispositivo', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('pos_ts_dispositivo', sa.DateTime(), nullable=True))

    with op.batch_alter_table('lineas_devolucion_cliente') as batch:
        batch.add_column(sa.Column('cantidad_declarada', sa.Numeric(14, 4), nullable=True))


def downgrade():
    with op.batch_alter_table('lineas_devolucion_cliente') as batch:
        batch.drop_column('cantidad_declarada')

    with op.batch_alter_table('entregas_geo') as batch:
        batch.drop_column('pos_ts_dispositivo')
        batch.drop_column('ts_dispositivo')

    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.drop_column('distancia_cliente_m')
        batch.drop_column('via_cola')
        batch.drop_column('ts_desfase_s')
        batch.drop_column('ts_dispositivo')
        batch.drop_column('foto_comprobante')
        batch.drop_column('referencia_pago')
