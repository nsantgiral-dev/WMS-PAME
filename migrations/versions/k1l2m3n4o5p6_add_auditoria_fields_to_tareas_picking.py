"""add auditoria fields to tareas_picking

Revision ID: k1l2m3n4o5p6
Revises: 7d08d3a13326
Create Date: 2026-05-28 18:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'k1l2m3n4o5p6'
down_revision = '7d08d3a13326'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('tareas_picking', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auditoria_resultado',       sa.String(length=30),  nullable=True))
        batch_op.add_column(sa.Column('auditoria_cantidad_hallada', sa.Integer(),          nullable=True))
        batch_op.add_column(sa.Column('auditoria_ubicacion_hallada', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('auditoria_observaciones',   sa.Text(),             nullable=True))
        batch_op.add_column(sa.Column('auditoria_resuelta_por_id', sa.Integer(),          nullable=True))
        batch_op.add_column(sa.Column('fecha_auditoria',           sa.DateTime(),         nullable=True))
        batch_op.create_foreign_key(
            'fk_tareas_picking_auditoria_usuario',
            'usuarios',
            ['auditoria_resuelta_por_id'], ['id']
        )


def downgrade():
    with op.batch_alter_table('tareas_picking', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tareas_picking_auditoria_usuario', type_='foreignkey')
        batch_op.drop_column('fecha_auditoria')
        batch_op.drop_column('auditoria_resuelta_por_id')
        batch_op.drop_column('auditoria_observaciones')
        batch_op.drop_column('auditoria_ubicacion_hallada')
        batch_op.drop_column('auditoria_cantidad_hallada')
        batch_op.drop_column('auditoria_resultado')
