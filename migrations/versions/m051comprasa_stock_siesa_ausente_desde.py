"""stock_siesa: lo que Siesa dejó de reportar queda en cero, con fecha

Revision ID: m051comprasa
Revises: m050demanda
Create Date: 2026-09-27

Aditiva, nullable, sin backfill. `ausente_desde` es el primer momento en que
una lectura COMPLETA de existencias no trajo la fila: desde ahí vale cero
(«se sabe que no hay»). NULL = Siesa la reporta. Ver CLAUDE.md «Existencias
verdaderas (2026-09-27)». La primera lectura completa después del deploy
limpia las filas fantasma.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm051comprasa'
down_revision = 'm050demanda'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('stock_siesa', sa.Column('ausente_desde', sa.DateTime(), nullable=True))


def downgrade():
    op.drop_column('stock_siesa', 'ausente_desde')
