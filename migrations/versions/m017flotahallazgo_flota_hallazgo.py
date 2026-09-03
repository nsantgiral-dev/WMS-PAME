"""flota: la tabla donde nace un dano

Revision ID: m017flotahallazgo
Revises: m016binaveriadossintipozona
Create Date: 2026-09-01

ADITIVA. Una tabla nueva y **un vocabulario ensanchado** — ningun DROP de
datos, ninguna migracion de filas.

El ensanche es `ck_flota_origen` sobre `flota_lectura_odometro`, que pasa de
seis valores a siete con `'hallazgo'`. Ensanchar un CHECK no puede rechazar
ninguna fila existente: todo lo que satisfacia la lista corta satisface la
larga. La reversa SI puede fallar, y el `downgrade` lo dice.

`flota/dominio/hallazgo.py` tiene 153 lineas de politica —canon cerrado, 23
tests escritos antes del calculo— y hasta hoy **cero callers**: no habia donde
escribir un hallazgo. Los 53 items de inspeccion sembrados en produccion
esperaban una tabla que no existia.

El cuerpo NO se escribio a mano: se emitio desde `db.metadata` con
`alembic.autogenerate.render_python_code`, igual que `f10ta1cimientos`. Una
transcripcion manual de 16 columnas y 5 CHECK es donde se pierde un constraint
sin que nadie lo note, y un invariante que la base no impone es una sugerencia.

## El CHECK de desenlace, y por que va con CASE

`ck_flota_hallazgo_desenlace` es una sola expresion y no tres CHECK sueltos:
los cuatro estados son un vocabulario, y partirlo deja huecos entre las piezas.

Escrito con `CASE WHEN` y no con aritmetica sobre predicados. Un CHECK de la
forma `(a IS NOT NULL) + (b IS NOT NULL) = 1` pasa contra SQLite —donde los
booleanos son enteros— y revienta el `CREATE TABLE` en PostgreSQL, que no suma
booleanos. Verificado antes de escribir esto: la DDL se compilo contra el
dialecto `postgresql` y se corrio contra una PostgreSQL real
(`scripts/verificar_flota_postgres.sh`).

## Lo que esta tabla NO trae

`inspeccion_id`. Un hallazgo puede venir de una inspeccion diaria, del recibo de
un turno, o de alguien que vio un golpe en el patio — y hoy la pantalla de
inspeccion no existe, asi que las dos ultimas son las unicas vias reales.
Atarlo a una inspeccion lo dejaria sin poder nacer. La columna llega cuando la
tabla exista; una FK que apunta a una tabla ausente no es previsora, es rota.

Backup de referencia: PITR activo en Railway. Head previo:
m016binaveriadossintipozona.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm017flotahallazgo'
down_revision = 'm016binaveriadossintipozona'
branch_labels = None
depends_on = None

# El vocabulario se congela aca a proposito: una migracion describe el esquema
# en un momento, y no debe cambiar de forma si manana el dominio agrega un
# origen. Ese cambio sera otra migracion. Mismo criterio que `f10ta4angulo`.
_ORIGEN_ANTES = ('entrega', 'preoperacional', 'cierre_dia', 'ot', 'tanqueo',
                 'correccion')
_ORIGEN_DESPUES = _ORIGEN_ANTES + ('hallazgo',)


def _rehacer_check_origen(valores):
    lista = ', '.join(f"'{v}'" for v in valores)
    op.drop_constraint('ck_flota_origen', 'flota_lectura_odometro', type_='check')
    op.create_check_constraint(
        'ck_flota_origen', 'flota_lectura_odometro', f'origen IN ({lista})')


def upgrade():
    op.create_table(
        'flota_hallazgo',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vehiculo_id', sa.Integer(), nullable=False),
        sa.Column('criticidad', sa.String(length=20), nullable=False),
        sa.Column('descripcion', sa.Text(), nullable=False),
        sa.Column('reportado_ts', sa.DateTime(), nullable=False),
        sa.Column('reportado_por_usuario_id', sa.Integer(), nullable=False),
        sa.Column('lectura_id', sa.Integer(), nullable=False),
        sa.Column('custodia_id', sa.Integer(), nullable=True),
        sa.Column('fecha_limite', sa.DateTime(), nullable=False),
        sa.Column('aplazado_veces', sa.Integer(), server_default='0', nullable=False),
        sa.Column('estado', sa.String(length=20), server_default='abierto', nullable=False),
        sa.Column('cerrado_ts', sa.DateTime(), nullable=True),
        sa.Column('cerrado_por_usuario_id', sa.Integer(), nullable=True),
        sa.Column('motivo_cierre', sa.Text(), nullable=True),
        sa.Column('bitacora', sa.Text(), nullable=True),
        sa.Column('linea_base', sa.Boolean(), server_default='0', nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "CASE  WHEN estado = 'abierto' THEN cerrado_ts IS NULL"
            "  WHEN estado = 'cerrado' THEN cerrado_ts IS NOT NULL"
            "  WHEN estado = 'descartado' THEN"
            "       motivo_cierre IS NOT NULL AND length(trim(motivo_cierre)) > 0"
            "  ELSE 1 = 1 END",
            name='ck_flota_hallazgo_desenlace'),
        sa.CheckConstraint("criticidad IN ('bloqueante', 'mayor', 'menor')",
                           name='ck_flota_criticidad'),
        sa.CheckConstraint("estado IN ('abierto', 'cerrado', 'descartado', 'no_aplica')",
                           name='ck_flota_estado'),
        sa.CheckConstraint('aplazado_veces >= 0', name='ck_flota_hallazgo_aplazos'),
        sa.CheckConstraint('length(trim(descripcion)) > 0',
                           name='ck_flota_hallazgo_descripcion'),
        sa.ForeignKeyConstraint(['cerrado_por_usuario_id'], ['usuarios.id'], ),
        sa.ForeignKeyConstraint(['custodia_id'], ['flota_custodia.id'], ),
        sa.ForeignKeyConstraint(['item_id'], ['flota_item_inspeccion.id'], ),
        sa.ForeignKeyConstraint(['lectura_id'], ['flota_lectura_odometro.id'], ),
        sa.ForeignKeyConstraint(['reportado_por_usuario_id'], ['usuarios.id'], ),
        sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_flota_hallazgo_abiertos', 'flota_hallazgo',
                    ['vehiculo_id', 'estado'], unique=False)
    op.create_index(op.f('ix_flota_hallazgo_custodia_id'), 'flota_hallazgo',
                    ['custodia_id'], unique=False)
    op.create_index(op.f('ix_flota_hallazgo_estado'), 'flota_hallazgo',
                    ['estado'], unique=False)

    # Un hallazgo lleva kilometraje (regla 3) y ninguno de los seis origenes
    # existentes describe el gesto de mirar el tablero para reportar un dano.
    # Reusar 'preoperacional' u 'ot' seria escribir una lectura que dice de
    # donde vino y miente — la columna existe justamente para no adivinarlo.
    #
    # SQLite no sabe hacer DROP CONSTRAINT: alla el CHECK se rehace solo
    # cuando `create_all` construye la tabla desde el modelo, que es como
    # corren los tests. Mismo criterio que los triggers de `f10ta1cimientos`.
    if op.get_bind().dialect.name == 'postgresql':
        _rehacer_check_origen(_ORIGEN_DESPUES)


def downgrade():
    # ⚠ Falla si ya existe alguna lectura con origen 'hallazgo' — y eso es lo
    # correcto: estrechar el vocabulario con filas que lo violan dejaria la
    # base afirmando una regla que sus propios datos incumplen. Antes de bajar
    # esta revision hay que decidir que se hace con esas lecturas.
    if op.get_bind().dialect.name == 'postgresql':
        _rehacer_check_origen(_ORIGEN_ANTES)

    op.drop_index(op.f('ix_flota_hallazgo_estado'), table_name='flota_hallazgo')
    op.drop_index(op.f('ix_flota_hallazgo_custodia_id'), table_name='flota_hallazgo')
    op.drop_index('ix_flota_hallazgo_abiertos', table_name='flota_hallazgo')
    op.drop_table('flota_hallazgo')
