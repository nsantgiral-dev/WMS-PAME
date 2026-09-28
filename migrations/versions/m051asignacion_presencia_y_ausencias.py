"""Presencia del personal y ausencias declaradas

Revision ID: m051asignacion
Revises: m050demanda
Create Date: 2026-09-27

Aditiva, nullable, sin backfill.

usuarios
- ultima_senal_at (datetime): última petición autenticada de la persona. Es la
  señal automática de «está trabajando» (app/services/presencia.py).

ausencias_usuario (nueva)
- Incapacidad, vacaciones, permiso… con fecha de regreso. Se anula, no se
  borra. Mientras está vigente la persona no recibe trabajo y lo que tenía
  asignado vuelve a la cola.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051asignacion'
down_revision = 'm050demanda'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('usuarios') as b:
        b.add_column(sa.Column('ultima_senal_at', sa.DateTime(), nullable=True))
    op.create_table(
        'ausencias_usuario',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('usuario_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=False),
        sa.Column('motivo', sa.String(20), nullable=False),
        sa.Column('desde', sa.Date(), nullable=False),
        sa.Column('regreso', sa.Date(), nullable=True),
        sa.Column('nota', sa.String(300), nullable=True),
        sa.Column('registrada_por_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('registrada_en', sa.DateTime(), nullable=False),
        sa.Column('anulada_por_id', sa.Integer(), sa.ForeignKey('usuarios.id'), nullable=True),
        sa.Column('anulada_en', sa.DateTime(), nullable=True),
        sa.Column('motivo_anulacion', sa.String(300), nullable=True),
    )
    op.create_index('ix_ausencias_usuario_usuario_id', 'ausencias_usuario', ['usuario_id'])


def downgrade():
    op.drop_index('ix_ausencias_usuario_usuario_id', table_name='ausencias_usuario')
    op.drop_table('ausencias_usuario')
    with op.batch_alter_table('usuarios') as b:
        b.drop_column('ultima_senal_at')
