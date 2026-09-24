"""Retención de cartera: mora y cupo antes de despachar a crédito real

Revision ID: m044cartera
Revises: m043contado
Create Date: 2026-09-24

Regla del dueño: un pedido de crédito REAL (> 15 días, `cond_pago.
cobro_contraentrega(...)['cobrar'] is False`) no se despacha si el cliente
tiene facturas vencidas, si con lo que ya debe supera su cupo, o si no tiene
cupo asignado. La excepción la da un usuario de cartera, con motivo y nombre,
desde el Gestor de Cartera (o desde el WMS con `puede_autorizar_cartera`).
Contado (≤ 15 días, y todo lo supuesto) siempre sale.

Aditiva, nullable, **sin backfill**: las tareas anteriores quedan con
`cartera_decision` NULL («nunca se evaluó»), que es la verdad.

Tablas nuevas (ver `app/models/cartera.py`): `retenciones_cartera`,
`cartera_cliente`, `cartera_habilitaciones`, `cartera_idempotencia`.

tareas_packing
- `despacho_iniciado_por_id`  quién tocó «Aprobar» (no puede autorizarse a sí mismo).
- `cartera_decision`          PASA | AUTORIZADO | CONTADO | NO_APLICA.
- `cartera_decidido_en`       cuándo (UTC).

usuarios
- `puede_autorizar_cartera`   respaldo en el WMS de la autorización del Gestor.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm044cartera'
down_revision = 'm043contado'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'retenciones_cartera',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('creada_en', sa.DateTime(), nullable=False),
        sa.Column('actualizada_en', sa.DateTime(), nullable=False),
        sa.Column('pedido_clave', sa.String(40), nullable=False),
        sa.Column('numero_pedido', sa.String(50)),
        sa.Column('nit', sa.String(40), nullable=False),
        sa.Column('sucursal', sa.String(10)),
        sa.Column('cliente', sa.String(200)),
        sa.Column('vendedor_id', sa.String(40)),
        sa.Column('tarea_packing_id', sa.Integer()),
        sa.Column('almacen_id', sa.Integer()),
        sa.Column('compuerta', sa.String(10), nullable=False),
        sa.Column('condicion_pago', sa.String(10)),
        sa.Column('dias_credito', sa.Integer()),
        sa.Column('valor', sa.Numeric(16, 2)),
        sa.Column('motivos', sa.JSON()),
        sa.Column('evaluacion', sa.JSON()),
        sa.Column('as_of', sa.DateTime()),
        sa.Column('origen_dato', sa.String(12)),
        sa.Column('estado', sa.String(20), nullable=False),
        sa.Column('iniciado_por_id', sa.Integer()),
        sa.Column('iniciado_por', sa.String(160)),
        sa.Column('siesa_usuario_creacion', sa.String(80)),
        sa.Column('contexto_siesa', sa.JSON()),
        sa.Column('resuelta_en', sa.DateTime()),
        sa.Column('resuelta_por', sa.String(160)),
        sa.Column('resuelta_por_id', sa.Integer()),
        sa.Column('resuelta_origen', sa.String(20)),
        sa.Column('motivo_resolucion', sa.Text()),
        sa.Column('codigo_excepcion', sa.String(10)),
        sa.Column('tope_valor', sa.Numeric(16, 2)),
        sa.Column('vence_en', sa.Date()),
        sa.Column('reevaluada_en', sa.DateTime()),
        sa.Column('reevaluaciones', sa.Integer(), nullable=False, server_default='0'),
        sa.CheckConstraint(
            "estado IN ('RETENIDO','LIBERADO_PAGO','AUTORIZADO',"
            "'CONVERTIDO_CONTADO','CANCELADO')", name='ck_retencion_cartera_estado'),
        sa.CheckConstraint("compuerta IN ('G1','G2','EMISION')",
                           name='ck_retencion_cartera_compuerta'),
    )
    op.create_index('ix_retenciones_cartera_pedido_clave', 'retenciones_cartera',
                    ['pedido_clave'])
    op.create_index('ix_retenciones_cartera_nit', 'retenciones_cartera', ['nit'])
    op.create_index('ix_retenciones_cartera_tarea_packing_id', 'retenciones_cartera',
                    ['tarea_packing_id'])
    op.create_index('ix_retenciones_cartera_actualizada_en', 'retenciones_cartera',
                    ['actualizada_en'])
    op.create_index('uq_retencion_cartera_viva', 'retenciones_cartera', ['pedido_clave'],
                    unique=True,
                    postgresql_where=sa.text("estado = 'RETENIDO'"),
                    sqlite_where=sa.text("estado = 'RETENIDO'"))

    op.create_table(
        'cartera_cliente',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('nit', sa.String(40), nullable=False, unique=True),
        sa.Column('as_of', sa.DateTime()),
        sa.Column('clientes', sa.JSON()),
        sa.Column('filas', sa.JSON()),
        sa.Column('ultimo_intento_en', sa.DateTime()),
        sa.Column('ultimo_error', sa.Text()),
    )

    op.create_table(
        'cartera_habilitaciones',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('nit', sa.String(40), nullable=False),
        sa.Column('sucursal', sa.String(10), nullable=False, server_default=''),
        sa.Column('canal', sa.String(20)),
        sa.Column('acuerdo_vigente', sa.Boolean()),
        sa.Column('acuerdo_vence', sa.Date()),
        sa.Column('excepciones', sa.JSON()),
        sa.Column('as_of', sa.DateTime(), nullable=False),
        sa.Column('recibido_en', sa.DateTime(), nullable=False),
        sa.Column('enviado_por', sa.String(160)),
        sa.UniqueConstraint('nit', 'sucursal', name='uq_cartera_habilitacion_nit_suc'),
    )

    op.create_table(
        'cartera_idempotencia',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('clave', sa.String(120), nullable=False, unique=True),
        sa.Column('endpoint', sa.String(120), nullable=False),
        sa.Column('retencion_id', sa.Integer()),
        sa.Column('status', sa.Integer(), nullable=False),
        sa.Column('respuesta', sa.JSON()),
        sa.Column('creada_en', sa.DateTime(), nullable=False),
    )

    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('despacho_iniciado_por_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('cartera_decision', sa.String(12), nullable=True))
        batch.add_column(sa.Column('cartera_decidido_en', sa.DateTime(), nullable=True))
        batch.create_check_constraint(
            'ck_packing_cartera_decision',
            "cartera_decision IS NULL OR cartera_decision IN "
            "('PASA','AUTORIZADO','CONTADO','NO_APLICA')")

    with op.batch_alter_table('usuarios') as batch:
        batch.add_column(sa.Column('puede_autorizar_cartera', sa.Boolean(), nullable=True,
                                   server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('usuarios') as batch:
        batch.drop_column('puede_autorizar_cartera')
    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_constraint('ck_packing_cartera_decision', type_='check')
        batch.drop_column('cartera_decidido_en')
        batch.drop_column('cartera_decision')
        batch.drop_column('despacho_iniciado_por_id')
    op.drop_table('cartera_idempotencia')
    op.drop_table('cartera_habilitaciones')
    op.drop_table('cartera_cliente')
    op.drop_index('uq_retencion_cartera_viva', table_name='retenciones_cartera')
    op.drop_index('ix_retenciones_cartera_actualizada_en', table_name='retenciones_cartera')
    op.drop_index('ix_retenciones_cartera_tarea_packing_id', table_name='retenciones_cartera')
    op.drop_index('ix_retenciones_cartera_nit', table_name='retenciones_cartera')
    op.drop_index('ix_retenciones_cartera_pedido_clave', table_name='retenciones_cartera')
    op.drop_table('retenciones_cartera')
