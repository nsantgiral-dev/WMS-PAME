"""add_nc_aprobada_devolucion_cliente

Revision ID: c0a1cecc16dc
Revises: 3ecf0c939454
Create Date: 2026-07-29

Seguimiento interno de aprobación contable de la Nota Crédito (142946).
Regla #21 del CLAUDE.md: la NC se crea en Siesa en Elaboración (nunca
Aprobado), y ni crearla ni aprobarla desde el escritorio cruza sola la
cartera — requiere un paso manual de contabilidad en Siesa. Estos campos
no reflejan ningún estado de Siesa, son solo el registro de que alguien
ya hizo ese paso, para poder listar pendientes sin que crezca para siempre.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c0a1cecc16dc'
down_revision = '3ecf0c939454'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('devoluciones_cliente',
                   sa.Column('nc_aprobada_siesa', sa.Boolean(), nullable=False, server_default='false'))
    op.add_column('devoluciones_cliente',
                   sa.Column('nc_aprobada_siesa_at', sa.DateTime(), nullable=True))
    op.add_column('devoluciones_cliente',
                   sa.Column('nc_aprobada_siesa_por', sa.Integer(),
                             sa.ForeignKey('usuarios.id'), nullable=True))


def downgrade():
    op.drop_column('devoluciones_cliente', 'nc_aprobada_siesa_por')
    op.drop_column('devoluciones_cliente', 'nc_aprobada_siesa_at')
    op.drop_column('devoluciones_cliente', 'nc_aprobada_siesa')
