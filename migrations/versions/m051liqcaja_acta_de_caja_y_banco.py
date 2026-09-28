"""Acta de entrega de caja del conductor y verificación bancaria de los pagos

Revision ID: m051liqcaja
Revises: m050inv2
Create Date: 2026-09-27

Aditiva, sin backfill.

entregas_caja / entregas_caja_gastos: lo que el conductor entregó, contado por
quien lo recibió, con la diferencia y su motivo (`services/caja_conductor.py`).
rutas_despacho.entrega_caja_id: el acta que recibió la plata de la ruta. `NULL`
en las filas existentes = sin acta (las rutas ya LIQUIDADAS antes de esto no
la piden: la exigencia está en `liquidar_ruta`).

recaudos_entrega.verificado_banco_*: quién vio en el banco una transferencia
(`services/verificacion_banco.py`). `NULL` = nadie la miró.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051liqcaja'
down_revision = 'm050inv2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'entregas_caja',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conductor_id', sa.Integer(), sa.ForeignKey('conductores.id'), nullable=False),
        sa.Column('dia', sa.Date(), nullable=False),
        sa.Column('estado', sa.String(20), nullable=False),
        sa.Column('esperado_efectivo', sa.Numeric(14, 2), nullable=False),
        sa.Column('contado_efectivo', sa.Numeric(14, 2), nullable=False),
        sa.Column('gastos_declarados', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('gastos_aceptados', sa.Numeric(14, 2), nullable=False, server_default='0'),
        sa.Column('diferencia', sa.Numeric(14, 2), nullable=False),
        sa.Column('motivo_diferencia', sa.Text(), nullable=True),
        sa.Column('detalle', sa.JSON(), nullable=True),
        sa.Column('registrada_por_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=False),
        sa.Column('registrada_en', sa.DateTime(), nullable=False),
        sa.Column('conductor_respuesta_en', sa.DateTime(), nullable=True),
        sa.Column('conductor_comentario', sa.Text(), nullable=True),
        sa.Column('sin_confirmar_por_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('sin_confirmar_en', sa.DateTime(), nullable=True),
        sa.Column('sin_confirmar_motivo', sa.Text(), nullable=True),
        sa.Column('anulada_por_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('anulada_en', sa.DateTime(), nullable=True),
        sa.Column('anulada_motivo', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "estado IN ('PENDIENTE_CONDUCTOR','CONFIRMADA','OBJETADA','SIN_CONFIRMAR','ANULADA')",
            name='ck_entrega_caja_estado'),
        sa.CheckConstraint('contado_efectivo >= 0', name='ck_entrega_caja_contado'),
        sa.CheckConstraint(
            "diferencia = 0 OR (motivo_diferencia IS NOT NULL AND "
            "length(trim(motivo_diferencia)) > 0)",
            name='ck_entrega_caja_diferencia_con_motivo'),
    )
    op.create_index('ix_entregas_caja_conductor_dia', 'entregas_caja', ['conductor_id', 'dia'])
    op.create_table(
        'entregas_caja_gastos',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('entrega_id', sa.Integer(), sa.ForeignKey('entregas_caja.id'), nullable=False),
        sa.Column('gasto_id', sa.Integer(), sa.ForeignKey('flota_gasto.id'), nullable=False,
                  unique=True),
        sa.Column('valor', sa.Numeric(14, 2), nullable=False),
        sa.Column('aceptado', sa.Boolean(), nullable=False),
        sa.Column('motivo', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "aceptado OR (motivo IS NOT NULL AND length(trim(motivo)) > 0)",
            name='ck_entrega_caja_gasto_rechazo_con_motivo'),
    )
    with op.batch_alter_table('rutas_despacho') as b:
        b.add_column(sa.Column('entrega_caja_id', sa.Integer(), nullable=True))
        b.create_foreign_key('fk_rutas_despacho_entrega_caja', 'entregas_caja',
                             ['entrega_caja_id'], ['id'])
    with op.batch_alter_table('recaudos_entrega') as b:
        b.add_column(sa.Column('verificado_banco_resultado', sa.String(20), nullable=True))
        b.add_column(sa.Column('verificado_banco_por', sa.Integer(), nullable=True))
        b.add_column(sa.Column('verificado_banco_en', sa.DateTime(), nullable=True))
        b.add_column(sa.Column('verificado_banco_nota', sa.Text(), nullable=True))
        b.create_foreign_key('fk_recaudo_verificado_banco_por', 'usuarios',
                             ['verificado_banco_por'], ['id'])
        b.create_check_constraint(
            'ck_recaudo_verificado_banco',
            "verificado_banco_resultado IS NULL OR "
            "verificado_banco_resultado IN ('VERIFICADA','NO_ENCONTRADA')")


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as b:
        b.drop_constraint('ck_recaudo_verificado_banco', type_='check')
        b.drop_constraint('fk_recaudo_verificado_banco_por', type_='foreignkey')
        b.drop_column('verificado_banco_nota')
        b.drop_column('verificado_banco_en')
        b.drop_column('verificado_banco_por')
        b.drop_column('verificado_banco_resultado')
    with op.batch_alter_table('rutas_despacho') as b:
        b.drop_constraint('fk_rutas_despacho_entrega_caja', type_='foreignkey')
        b.drop_column('entrega_caja_id')
    op.drop_table('entregas_caja_gastos')
    op.drop_index('ix_entregas_caja_conductor_dia', table_name='entregas_caja')
    op.drop_table('entregas_caja')
