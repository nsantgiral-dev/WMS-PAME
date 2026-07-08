"""add fila, cuerpo, hueco a ubicaciones — direccion fisica de 5 ejes

Pasillo -> Fila (1/2, lado del pasillo) -> Cuerpo (bahia) -> Nivel (entrepano,
ya existia como 'nivel' pero sin uso) -> Hueco (espacio dentro del entrepano).

Ubicaciones creadas antes de este cambio (origen='MANUAL' via el mecanismo de
'fila' plano, u origen='SIESA') quedan con estos campos en NULL — siguen
funcionando igual que antes, este cambio es aditivo y no las toca.

Revision ID: c8d9e0f1a2b3
Revises: db33637f3b41
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

revision = 'c8d9e0f1a2b3'
down_revision = 'db33637f3b41'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('ubicaciones', sa.Column('fila', sa.Integer(), nullable=True))
    op.add_column('ubicaciones', sa.Column('cuerpo', sa.Integer(), nullable=True))
    op.add_column('ubicaciones', sa.Column('hueco', sa.Integer(), nullable=True))
    op.alter_column(
        'ubicaciones', 'nivel',
        existing_type=sa.String(length=10),
        type_=sa.Integer(),
        postgresql_using="NULLIF(nivel, '')::integer",
    )


def downgrade():
    op.alter_column(
        'ubicaciones', 'nivel',
        existing_type=sa.Integer(),
        type_=sa.String(length=10),
        postgresql_using='nivel::varchar',
    )
    op.drop_column('ubicaciones', 'hueco')
    op.drop_column('ubicaciones', 'cuerpo')
    op.drop_column('ubicaciones', 'fila')
