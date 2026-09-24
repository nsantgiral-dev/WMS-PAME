"""Contado contraentrega vs crédito real: snapshot de la clasificación de cobro

Revision ID: m043contado
Revises: m041flfugas
Create Date: 2026-09-24

Regla del dueño: toda factura con ≤ 15 días de crédito se cobra
contraentrega; > 15 es crédito real. La política vive en
`app/services/cond_pago.cobro_contraentrega`; esto es su snapshot, para que
la pantalla del conductor y la guarda del servidor funcionen sin red.

Aditiva y nullable, **sin backfill**: un NULL significa «no se clasificó
todavía» (la política lo trata como contado supuesto y lo declara), nunca una
condición inventada.

tareas_packing
- `cond_pago_fe`        `f461_id_cond_pago`: la condición con la que la FE salió.
- `dias_credito`        días de esa condición según la tabla vigente.
- `cobro_contraentrega` True = se cobra al entregar; False = crédito real.
- `clasif_origen`       MAESTRO | SUPUESTO_AUSENTE | SUPUESTO_DESCONOCIDO | SIN_MAESTRO.
- `clasif_en`           cuándo se escribió (UTC).

recaudos_entrega
- `cobro_contraentrega`       la clasificación congelada al confirmar.
- `credito_autorizado_por/_razon/_en`  crédito autorizado por la oficina sobre
                              una parada de contado que no trajo plata.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm043contado'
down_revision = 'm041flfugas'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('cond_pago_fe', sa.String(10), nullable=True))
        batch.add_column(sa.Column('dias_credito', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('cobro_contraentrega', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('clasif_origen', sa.String(24), nullable=True))
        batch.add_column(sa.Column('clasif_en', sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            'ck_packing_dias_credito', 'dias_credito IS NULL OR dias_credito >= 0')
        batch.create_check_constraint(
            'ck_packing_clasif_origen',
            "clasif_origen IS NULL OR clasif_origen IN "
            "('MAESTRO','SUPUESTO_AUSENTE','SUPUESTO_DESCONOCIDO','SIN_MAESTRO')")

    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.add_column(sa.Column('cobro_contraentrega', sa.Boolean(), nullable=True))
        batch.add_column(sa.Column('credito_autorizado_por', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('credito_autorizado_razon', sa.Text(), nullable=True))
        batch.add_column(sa.Column('credito_autorizado_en', sa.DateTime(), nullable=True))
        batch.create_foreign_key(
            'fk_recaudo_credito_autorizado_por', 'usuarios',
            ['credito_autorizado_por'], ['id'])


def downgrade():
    with op.batch_alter_table('recaudos_entrega') as batch:
        batch.drop_constraint('fk_recaudo_credito_autorizado_por', type_='foreignkey')
        batch.drop_column('credito_autorizado_en')
        batch.drop_column('credito_autorizado_razon')
        batch.drop_column('credito_autorizado_por')
        batch.drop_column('cobro_contraentrega')

    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_constraint('ck_packing_clasif_origen', type_='check')
        batch.drop_constraint('ck_packing_dias_credito', type_='check')
        batch.drop_column('clasif_en')
        batch.drop_column('clasif_origen')
        batch.drop_column('cobro_contraentrega')
        batch.drop_column('dias_credito')
        batch.drop_column('cond_pago_fe')
