"""Pedir por paquete: cómo se pidió cada línea de traslado, y paquetes sin código

Revision ID: m053pedirpaquete
Revises: m052itemsiesa
Create Date: 2026-10-02

Aditiva. Dos cosas:

· `items_solicitud_traslado` gana cuatro columnas nullable que dicen CÓMO se
  pidió la línea («2 PQ × 12 + 5 sueltas»). La cantidad sigue en
  `cantidad_solicitada`, en unidades; las líneas viejas quedan en NULL
  (= pedidas en unidades).
· `producto_empaques.codigo_barras` pasa a admitir NULL: un paquete que Siesa
  declara (API_v2_ItemsUnidadesMedida) sin código de barras propio se guarda
  para mostrarlo y pedirlo, nunca para escanearlo. Ninguna fila existente
  cambia.

El downgrade borra las filas sin código antes de volver a exigirlo.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm053pedirpaquete'
down_revision = 'm052itemsiesa'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('items_solicitud_traslado') as b:
        b.add_column(sa.Column('paquetes_pedidos', sa.Integer(), nullable=True))
        b.add_column(sa.Column('sueltas_pedidas', sa.Integer(), nullable=True))
        b.add_column(sa.Column('factor_al_pedir', sa.Integer(), nullable=True))
        b.add_column(sa.Column('unidad_empaque_al_pedir', sa.String(20), nullable=True))
    with op.batch_alter_table('producto_empaques') as b:
        b.alter_column('codigo_barras', existing_type=sa.String(50), nullable=True)


def downgrade():
    op.execute('DELETE FROM producto_empaques WHERE codigo_barras IS NULL')
    with op.batch_alter_table('producto_empaques') as b:
        b.alter_column('codigo_barras', existing_type=sa.String(50), nullable=False)
    with op.batch_alter_table('items_solicitud_traslado') as b:
        b.drop_column('unidad_empaque_al_pedir')
        b.drop_column('factor_al_pedir')
        b.drop_column('sueltas_pedidas')
        b.drop_column('paquetes_pedidos')
