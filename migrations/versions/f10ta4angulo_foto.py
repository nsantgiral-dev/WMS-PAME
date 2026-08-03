"""flota_foto.angulo — qué parte del vehículo muestra cada foto

Hasta el 2026-08-03 las ocho fotos de una custodia se guardaban anónimas. El
orden tampoco las identificaba: el frontend filtra las faltantes antes de
enviar, así que con `frontal` sin tomar la primera del arreglo es `trasera` y
todo queda corrido un lugar. Era imposible decir "el flanco herido está en la
llanta trasera derecha" — que es exactamente para lo que se toma la foto.

**Nullable a propósito.** Las filas anteriores a esta migración no se pueden
etiquetar: nadie sabe cuál era cuál, y adivinar por el orden sería inventar
evidencia. Quedan en NULL, declaradas, y el health las cuenta. Un default como
'desconocido' sería lo mismo con un nombre que miente.

Revision ID: f10ta4angulo
Revises: f10ta3forzado
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta4angulo'
down_revision = 'f10ta3forzado'
branch_labels = None
depends_on = None

# El vocabulario se congela acá a propósito: una migración describe el estado
# del esquema en un momento, y no debe cambiar de forma si mañana el dominio
# agrega un ángulo. Ese cambio será otra migración.
_ANGULOS = (
    'frontal', 'trasera', 'lateral_izq', 'lateral_der',
    'cajon_abierto', 'interior_cabina', 'tablero',
) + tuple(f'llanta_{i}' for i in range(1, 13))


def upgrade():
    op.add_column('flota_foto', sa.Column('angulo', sa.String(24), nullable=True))
    lista = ', '.join(f"'{a}'" for a in _ANGULOS)
    op.create_check_constraint(
        'ck_flota_angulo', 'flota_foto',
        f'angulo IS NULL OR angulo IN ({lista})',
    )


def downgrade():
    op.drop_constraint('ck_flota_angulo', 'flota_foto', type_='check')
    op.drop_column('flota_foto', 'angulo')
