"""La tarjeta de propiedad no vence

Revision ID: f10ta8sinvence
Revises: f10ta7aviso
Create Date: 2026-08-05

El invariante «un documento vigente tiene fecha de vencimiento» era falso. La
tarjeta de propiedad acredita titularidad mientras el vehículo sea del titular:
no caduca, ni en el papel ni en el RUNT.

Lo encontró Yesid cargando el THP696. Para poder guardar tuvo que escribir una
fecha inventada — quedó `2045-08-20`, «vence en 6955 días»— dentro del módulo
cuyo lema es que inventarlo es peor que no tenerlo.

El invariante no se afloja para todos: se hace **condicional al tipo**, la misma
forma que ya usan `custodio_estado` y las dimensiones de foto. Y va en los dos
sentidos — un tipo que no vence tampoco puede TENER vencimiento, porque si
pudiera, la fila inventada seguiría siendo legal y el aviso de renovación la
perseguiría como si fuera real.

Las filas ya cargadas con fecha inventada se limpian: dejarlas violaría el
CHECK nuevo y la migración fallaría a mitad. Se borra la fecha, no la fila —
el número y la entidad de la tarjeta son datos buenos.
"""
from alembic import op
import sqlalchemy as sa

revision = 'f10ta8sinvence'
down_revision = 'f10ta7aviso'
branch_labels = None
depends_on = None

_SIN_VENCE = "('tarjeta_propiedad')"

_COHERENTE_NUEVO = (
    "(estado = 'vigente' AND fecha_expedicion IS NOT NULL "
    " AND length(trim(numero)) > 0 AND length(trim(entidad)) > 0 "
    f" AND (fecha_vencimiento IS NOT NULL OR tipo IN {_SIN_VENCE})) OR "
    "(estado = 'no_encontrado' AND fecha_expedicion IS NULL "
    " AND fecha_vencimiento IS NULL)"
)
_COHERENTE_VIEJO = (
    "(estado = 'vigente' AND fecha_expedicion IS NOT NULL "
    " AND fecha_vencimiento IS NOT NULL "
    " AND length(trim(numero)) > 0 AND length(trim(entidad)) > 0) OR "
    "(estado = 'no_encontrado' AND fecha_expedicion IS NULL "
    " AND fecha_vencimiento IS NULL)"
)


def upgrade():
    # ORDEN: soltar el CHECK VIEJO, después los datos, después los nuevos.
    #
    # La primera versión ponía el UPDATE adelante «para que el CHECK nuevo no
    # encuentre filas que no admite». El razonamiento era sobre el constraint
    # equivocado: el que estaba en vigor durante el UPDATE era el VIEJO, que
    # exige `fecha_vencimiento IS NOT NULL` para todo documento vigente. Poner
    # esa fecha en NULL lo violaba, y el release abortó en producción:
    #
    #   CheckViolation: ... viola "ck_flota_doc_estado_coherente"
    #   DETAIL: Failing row contains (4, 6, tarjeta_propiedad, ..., null, vigente)
    #
    # Una migración que cambia una regla y además arregla los datos tiene que
    # dejar de exigir la regla vieja ANTES de tocar las filas. Entre el drop y
    # el create no hay ventana de riesgo: Alembic corre todo en una transacción
    # y PostgreSQL hace el DDL transaccional.
    with op.batch_alter_table('flota_documento_vehiculo') as batch:
        batch.drop_constraint('ck_flota_doc_estado_coherente', type_='check')

    op.execute(
        "UPDATE flota_documento_vehiculo SET fecha_vencimiento = NULL "
        f"WHERE tipo IN {_SIN_VENCE} AND fecha_vencimiento IS NOT NULL"
    )

    with op.batch_alter_table('flota_documento_vehiculo') as batch:
        batch.create_check_constraint('ck_flota_doc_estado_coherente', _COHERENTE_NUEVO)
        batch.create_check_constraint(
            'ck_flota_doc_sin_vencimiento',
            f"tipo NOT IN {_SIN_VENCE} OR fecha_vencimiento IS NULL")


def downgrade():
    # Volver atrás exige una fecha para las tarjetas, y no existe ninguna real.
    # Se borran esas filas en vez de inventarles una: el CHECK viejo las
    # rechazaría y una migración a medias es peor que una que no corre.
    op.execute(f"DELETE FROM flota_documento_vehiculo WHERE tipo IN {_SIN_VENCE}")
    with op.batch_alter_table('flota_documento_vehiculo') as batch:
        batch.drop_constraint('ck_flota_doc_sin_vencimiento', type_='check')
        batch.drop_constraint('ck_flota_doc_estado_coherente', type_='check')
        batch.create_check_constraint('ck_flota_doc_estado_coherente', _COHERENTE_VIEJO)
