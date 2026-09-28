"""Índice de pedidos_historia por cliente y faltante de recepción de traslados

Revision ID: m050inv2
Revises: m049tardia
Create Date: 2026-09-26

Aditiva. La compuerta de cartera busca los pedidos de un NIT en cada
evaluación (`cartera_service._claves_del_nit`); sin índice, cada despacho a
crédito recorría la tabla entera.

solicitudes_traslado.faltante_* (nullable, sin backfill): quién decidió qué
se hizo con lo que la tienda no recibió (DEVUELTO_AL_ORIGEN · AJUSTADO ·
EN_INVESTIGACION). Ver `traslado_service.faltante_de_recepcion`.

tareas_devolucion.siesa_response: el desenlace del pre-flag (las otras dos
tablas con pre-flag ya lo tenían). Ver `siesa_job_service.estado_del_preflag`.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm050inv2'
down_revision = 'm049tardia'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index('ix_pedidos_historia_cliente', 'pedidos_historia', ['cliente_id'])
    with op.batch_alter_table('solicitudes_traslado') as b:
        b.add_column(sa.Column('faltante_resolucion', sa.String(20), nullable=True))
        b.add_column(sa.Column('faltante_resuelto_at', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('faltante_resuelto_por_id', sa.Integer(), nullable=True))
        b.add_column(sa.Column('faltante_nota', sa.Text(), nullable=True))
    # El desenlace del pre-flag del traslado a averías (estado_del_preflag).
    # Backfill imprescindible y acotado: las tareas cuyo TRASLADO_AVERIAS ya
    # terminó COMPLETADO entraron a Siesa; sin la marca se leerían «sin
    # verificar» para siempre. Las demás quedan NULL (no se sabe).
    with op.batch_alter_table('tareas_devolucion') as b:
        b.add_column(sa.Column('siesa_response', sa.Text(), nullable=True))
    op.execute(
        "UPDATE tareas_devolucion SET siesa_response = '{\"historico\": true}' "
        "WHERE siesa_triggered = true AND id IN (SELECT referencia_id FROM siesa_jobs "
        "WHERE tipo = 'TRASLADO_AVERIAS' AND referencia_tipo = 'TareaDevolucion' "
        "AND estado = 'COMPLETADO')")


def downgrade():
    with op.batch_alter_table('tareas_devolucion') as b:
        b.drop_column('siesa_response')
    with op.batch_alter_table('solicitudes_traslado') as b:
        b.drop_column('faltante_nota')
        b.drop_column('faltante_resuelto_por_id')
        b.drop_column('faltante_resuelto_at')
        b.drop_column('faltante_resolucion')
    op.drop_index('ix_pedidos_historia_cliente', table_name='pedidos_historia')
