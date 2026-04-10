"""add ultima milla: recaudos_entrega, conductor.disponible, ruta.estado_financiero

Revision ID: m6n7o8p9q0r1
Revises: l5m6n7o8p9q0
Create Date: 2026-04-10

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'm6n7o8p9q0r1'
down_revision = 'l5m6n7o8p9q0'
branch_labels = None
depends_on = None


def upgrade():
    # ── conductor.disponible ──────────────────────────────────────────
    op.add_column('conductores',
        sa.Column('disponible', sa.Boolean(), nullable=False, server_default='true'))

    # ── rutas_despacho.estado_financiero ─────────────────────────────
    op.add_column('rutas_despacho',
        sa.Column('estado_financiero', sa.String(20), nullable=False, server_default='PENDIENTE'))

    # ── recaudos_entrega ─────────────────────────────────────────────
    op.create_table(
        'recaudos_entrega',
        sa.Column('id',                    sa.Integer(),     primary_key=True),
        sa.Column('ruta_id',               sa.Integer(),     sa.ForeignKey('rutas_despacho.id'), nullable=False),
        sa.Column('tarea_id',              sa.Integer(),     sa.ForeignKey('tareas_packing.id'), nullable=False),
        sa.Column('estado_entrega',        sa.String(20),    nullable=False),
        sa.Column('forma_pago',            sa.String(30),    nullable=True),
        sa.Column('monto_cobrado',         sa.Numeric(12, 2), nullable=True, server_default='0'),
        sa.Column('observaciones',         sa.Text(),        nullable=True),
        sa.Column('foto_entrega',          sa.Text(),        nullable=True),
        sa.Column('bultos_rechazados_ids', postgresql.JSON(astext_type=sa.Text()), nullable=True),
        sa.Column('confirmado_por',        sa.Integer(),     sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('editado_por',           sa.Integer(),     sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('editado_en',            sa.DateTime(),    nullable=True),
        sa.Column('fecha_confirmacion',    sa.DateTime(),    nullable=True),
        sa.Column('fecha_creacion',        sa.DateTime(),    nullable=True),
    )
    op.create_index('ix_recaudos_ruta_id',  'recaudos_entrega', ['ruta_id'])
    op.create_index('ix_recaudos_tarea_id', 'recaudos_entrega', ['tarea_id'])


def downgrade():
    op.drop_index('ix_recaudos_tarea_id', table_name='recaudos_entrega')
    op.drop_index('ix_recaudos_ruta_id',  table_name='recaudos_entrega')
    op.drop_table('recaudos_entrega')
    op.drop_column('rutas_despacho', 'estado_financiero')
    op.drop_column('conductores', 'disponible')
