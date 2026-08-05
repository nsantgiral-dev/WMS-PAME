"""Documentos de flota: aceptar el archivo, no solo la foto

Revision ID: f10ta6adjunto
Revises: nc001motivodian
Create Date: 2026-08-05

El SOAT llega por correo en PDF. La pantalla solo aceptaba foto con cámara, así
que quien cargaba el documento tenía que abrir el PDF y fotografiar la pantalla
— un rodeo que además degrada el original justo en la parte que importa (el
número y la fecha).

Dos cambios y nada más:

· `clase` admite `documento_adjunto`. No se reusó `foto_dato` porque su mínimo
  de 1600 px existe para el odómetro; aflojarlo para que quepa un PDF sería
  quitar la protección donde sí hace falta.
· `ancho`/`alto` pasan a nullable con CHECK condicional. Un PDF no tiene
  píxeles: guardarle 0×0 es un número que miente sobre un archivo que sí
  existe. Para toda clase que no sea adjunto, las dimensiones siguen siendo
  obligatorias y positivas — el CHECK lo impone, no la convención.

Las filas existentes no se tocan: todas son imágenes con dimensiones, y siguen
cumpliendo el CHECK nuevo por la primera rama.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta6adjunto'
down_revision = 'nc001motivodian'
branch_labels = None
depends_on = None

_CLASES_NUEVAS = ('evidencia_estado', 'foto_dato', 'documento_adjunto')
_CLASES_VIEJAS = ('evidencia_estado', 'foto_dato')

_MEDIDAS_NUEVO = (
    "bytes > 0 AND ("
    "(ancho IS NOT NULL AND alto IS NOT NULL AND ancho > 0 AND alto > 0) OR "
    "(clase = 'documento_adjunto' AND ancho IS NULL AND alto IS NULL))"
)
_MEDIDAS_VIEJO = 'bytes > 0 AND ancho > 0 AND alto > 0'


def _en(clases):
    return 'clase IN (%s)' % ', '.join(f"'{c}'" for c in clases)


def upgrade():
    with op.batch_alter_table('flota_foto') as batch:
        batch.drop_constraint('ck_flota_clase', type_='check')
        batch.create_check_constraint('ck_flota_clase', _en(_CLASES_NUEVAS))
        batch.alter_column('ancho', existing_type=sa.Integer(), nullable=True)
        batch.alter_column('alto', existing_type=sa.Integer(), nullable=True)
        batch.drop_constraint('ck_flota_foto_medidas', type_='check')
        batch.create_check_constraint('ck_flota_foto_medidas', _MEDIDAS_NUEVO)


def downgrade():
    # Volver atrás con adjuntos ya cargados dejaría filas que el CHECK viejo
    # rechaza. Se borran sus FILAS, no sus archivos: el binario sigue en el
    # almacén y se puede recuperar. Falla ruidosa si alguien lo intenta a
    # ciegas es preferible a un downgrade que se cuelga a mitad.
    op.execute("DELETE FROM flota_foto WHERE clase = 'documento_adjunto'")
    with op.batch_alter_table('flota_foto') as batch:
        batch.drop_constraint('ck_flota_foto_medidas', type_='check')
        batch.create_check_constraint('ck_flota_foto_medidas', _MEDIDAS_VIEJO)
        batch.alter_column('ancho', existing_type=sa.Integer(), nullable=False)
        batch.alter_column('alto', existing_type=sa.Integer(), nullable=False)
        batch.drop_constraint('ck_flota_clase', type_='check')
        batch.create_check_constraint('ck_flota_clase', _en(_CLASES_VIEJAS))
