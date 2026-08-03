"""flota: rastro del cierre forzado de custodia

Revision ID: f10ta3forzado
Revises: f10ta2plantillas
Create Date: 2026-08-03

Tres columnas en flota_custodia. La tabla existe desde ayer y esta VACIA, asi
que el ALTER no toca ni una fila.

Un cierre forzado es un turno cerrado sin la firma del custodio anterior y sin
fotos_fin: el turno siguiente arranca sin nada con que comparar, y el proximo
danio que aparezca no se le puede atribuir a nadie. Si eso no queda
distinguible de un cierre normal, nadie va a saber por que.

El CHECK impide un forzado anonimo: o va sin marca y sin autor ni motivo, o va
marcado con los dos. Un cierre forzado sin quien lo autorizo dejaria el rastro
de que paso algo raro y ninguna forma de saber quien.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta3forzado'
down_revision = 'f10ta2plantillas'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('flota_custodia', sa.Column(
        'cierre_forzado', sa.Boolean(), nullable=False, server_default='0'))
    op.add_column('flota_custodia', sa.Column(
        'cierre_forzado_por_usuario_id', sa.Integer(), nullable=True))
    op.add_column('flota_custodia', sa.Column(
        'cierre_forzado_motivo', sa.Text(), nullable=True))
    op.create_foreign_key('fk_flota_cierre_forzado_por', 'flota_custodia',
                          'usuarios', ['cierre_forzado_por_usuario_id'], ['id'])
    op.create_check_constraint(
        'ck_flota_cierre_forzado_declarado', 'flota_custodia',
        "(cierre_forzado = '0' AND cierre_forzado_por_usuario_id IS NULL "
        " AND cierre_forzado_motivo IS NULL) OR "
        "(cierre_forzado = '1' AND cierre_forzado_por_usuario_id IS NOT NULL "
        " AND length(trim(cierre_forzado_motivo)) > 0)")


def downgrade():
    op.drop_constraint('ck_flota_cierre_forzado_declarado', 'flota_custodia',
                       type_='check')
    op.drop_constraint('fk_flota_cierre_forzado_por', 'flota_custodia',
                       type_='foreignkey')
    op.drop_column('flota_custodia', 'cierre_forzado_motivo')
    op.drop_column('flota_custodia', 'cierre_forzado_por_usuario_id')
    op.drop_column('flota_custodia', 'cierre_forzado')
