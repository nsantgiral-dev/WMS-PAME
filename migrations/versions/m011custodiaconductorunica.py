"""Un conductor, un vehículo — el invariante que solo vivía en Python

Revision ID: m011custodiaconductorunica
Revises: m010declaracionambiente
Create Date: 2026-08-19

`uq_flota_custodia_activa` impone 0-o-1 custodia activa **por vehículo**. Nada
imponía 0-o-1 **por conductor**: eso se comprobaba solo en
`dom.validar_un_vehiculo_por_conductor`, con un `.all()` sin bloqueo en
`traspaso.py`.

## Por qué importa la diferencia, y no es simetría

Las dos reglas fallan distinto:

· dos aperturas simultáneas sobre el **mismo vehículo** chocan contra el índice
  único parcial. El usuario ve un `IntegrityError` feo, pero **el dato queda
  íntegro**;
· dos aperturas simultáneas del **mismo conductor** sobre vehículos distintos
  no chocaban con nada. Pasaban las dos, sin error, y dejaban el invariante
  roto en silencio.

Un 500 se ve. Esto no.

Y no hacía falta una condición de carrera para romperlo: el 2026-08-13 un
conductor acumuló **tres** custodias abiertas —TGZ653, TGZ655, UPQ606, sin
entregar ninguna— y tumbó `/flota/conductor/mi-turno`, cuyo `one_or_none()`
reventó con `MultipleResultsFound`. Su pantalla quedó en blanco: no podía ver
ni recibir ningún vehículo. La validación de dominio se escribió por ese
incidente. El respaldo en disco no.

## Los NULL no estorban

`custodio_conductor_id` es NULL cuando el custodio es una sede, y Postgres
trata los NULL como distintos en un índice único: cualquier cantidad de
custodias de sede convive. No hace falta condicionar por `custodio_tipo`.

## Si hay datos que ya violan la regla, esto se detiene y los nombra

`CREATE UNIQUE INDEX` sobre datos sucios falla con un mensaje que dice el
nombre del índice y nada más. Acá se comprueba antes y se levanta un error con
**el conductor y los vehículos concretos**, porque quien corre la migración a
las 6 p.m. de un viernes necesita saber qué arreglar, no que algo falló.

No se limpia automáticamente: cuál de las custodias abiertas es la buena lo
decide una persona que sepa dónde está el camión. Adivinarlo sería inventar
responsabilidad sobre un vehículo, que es justo lo que este módulo registra.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm011custodiaconductorunica'
down_revision = 'm010declaracionambiente'
branch_labels = None
depends_on = None

_INDICE = 'uq_flota_custodia_conductor_activa'


def upgrade():
    conn = op.get_bind()

    sucias = conn.execute(sa.text("""
        SELECT c.custodio_conductor_id,
               count(*) AS abiertas,
               string_agg(coalesce(v.placa, '(sin placa)'), ', ') AS placas
          FROM flota_custodia c
          LEFT JOIN vehiculos v ON v.id = c.vehiculo_id
         WHERE c.fin_ts IS NULL
           AND c.custodio_conductor_id IS NOT NULL
         GROUP BY c.custodio_conductor_id
        HAVING count(*) > 1
    """)).fetchall()

    if sucias:
        detalle = '\n'.join(
            f'  · conductor id={f[0]}: {f[1]} custodias abiertas → {f[2]}'
            for f in sucias)
        raise RuntimeError(
            'No se puede crear el índice: hay conductores con más de una '
            'custodia abierta.\n\n'
            f'{detalle}\n\n'
            'Cerrá las que no correspondan desde el panel de flota —«Entregar '
            'turno» o cierre forzado con motivo— y volvé a correr la '
            'migración. No se limpian solas a propósito: cuál custodia es la '
            'buena depende de dónde está el camión, y eso lo sabe una persona.')

    op.create_index(_INDICE, 'flota_custodia', ['custodio_conductor_id'],
                    unique=True,
                    postgresql_where=sa.text('fin_ts IS NULL'))


def downgrade():
    op.drop_index(_INDICE, table_name='flota_custodia')
