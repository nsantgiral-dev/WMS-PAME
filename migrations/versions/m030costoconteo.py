"""El conteo guarda el costo promedio unitario de su foto de Siesa

Revision ID: m030costoconteo
Revises: m029fotoinicioconteo
Create Date: 2026-09-23

## Para qué

Las estadísticas de conteo cíclico reportan cuánta plata mueven los ajustes.
Sin esta columna solo se pueden contar unidades, y una unidad de un lápiz y una
de una calculadora no pesan lo mismo en el inventario.

`f400_costo_prom_uni` viene en la MISMA fila de `API_v2_Inventarios_InvFecha`
que ya se lee para la foto del conteo (contrato:
`docs/siesa-specs/API_v2_Inventarios_InvFecha.docx`), así que no cuesta una
llamada más. Se guarda en el instante del conteo por la razón de siempre: el
costo promedio cambia con cada entrada.

## Por qué nullable y sin backfill

El costo NO es parte de la foto obligatoria: si falta, el conteo sigue igual.
Una sesión anterior a esta columna no tiene costo y no se le inventa uno —
traer el de hoy para un ajuste de abril sería valorizar con un precio que ese
ajuste nunca tuvo—. El reporte la cuenta como «sin valorizar» y lo dice.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm030costoconteo'
down_revision = 'm029fotoinicioconteo'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.add_column(sa.Column('costo_prom_uni_siesa', sa.Numeric(14, 4), nullable=True))


def downgrade():
    with op.batch_alter_table('sesiones_conteo') as batch:
        batch.drop_column('costo_prom_uni_siesa')
