"""flota_custodia.ubicacion — dónde queda el vehículo, aparte de quién responde

Hasta el 2026-08-03 el traspaso era siempre cerrar+abrir y no existía el gesto
"entrego y el camión queda en el patio". El modelo ya tenía `custodio_tipo =
'sede'` para eso, sin ninguna pantalla que lo encendiera; un conductor que
terminaba a las 6 p.m. no tenía a quién entregarle y su única salida era volver
a recibirse el vehículo a sí mismo. Eso produjo nueve custodias de cero
kilómetros en el THP696 el mismo minuto.

**Dos columnas y no una, a propósito.** `ubicacion` dice dónde está;
`custodio_tipo` dice quién responde. Mezclarlas rompe el único caso con riesgo
real: si el camión duerme en la casa del conductor y la ubicación arrastra la
custodia a `sede`, el registro descarga de responsabilidad a la única persona
que efectivamente lo tiene.

Nullable: las custodias anteriores no registraron dónde quedaron y no se puede
saber. Se dice, no se rellena.

Revision ID: f10ta5ubicacion
Revises: f10ta4angulo
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta5ubicacion'
down_revision = 'f10ta4angulo'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('flota_custodia', sa.Column('ubicacion', sa.String(20), nullable=True))
    op.add_column('flota_custodia', sa.Column('ubicacion_motivo', sa.Text(), nullable=True))

    op.create_check_constraint(
        'ck_flota_ubicacion', 'flota_custodia',
        "ubicacion IN ('sede', 'taller', 'fuera_de_sede')",
    )
    # La combinación imposible: fuera de sede sin que nadie de carne responda.
    op.create_check_constraint(
        'ck_flota_fuera_de_sede_responde_el_conductor', 'flota_custodia',
        "ubicacion IS NULL OR ubicacion <> 'fuera_de_sede' "
        "OR custodio_tipo = 'conductor'",
    )
    op.create_check_constraint(
        'ck_flota_fuera_de_sede_con_motivo', 'flota_custodia',
        "ubicacion IS NULL OR ubicacion <> 'fuera_de_sede' "
        "OR (ubicacion_motivo IS NOT NULL AND length(trim(ubicacion_motivo)) > 0)",
    )


def downgrade():
    for c in ('ck_flota_fuera_de_sede_con_motivo',
              'ck_flota_fuera_de_sede_responde_el_conductor',
              'ck_flota_ubicacion'):
        op.drop_constraint(c, 'flota_custodia', type_='check')
    op.drop_column('flota_custodia', 'ubicacion_motivo')
    op.drop_column('flota_custodia', 'ubicacion')
