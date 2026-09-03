"""flota: la inspeccion respondida y la plata que sale

Revision ID: m018flotainspecciongasto
Revises: m017flotahallazgo
Create Date: 2026-09-02

ADITIVA. Cuatro tablas nuevas y dos columnas sobre `flota_ficha_tecnica`.
Ningun DROP de datos, ninguna migracion de filas.

## Que trae

| Tabla | Que faltaba |
|---|---|
| `flota_inspeccion` + `flota_respuesta_item` | `flota/dominio/inspeccion.py` tiene la politica desde la tanda 1 y los 53 items estan sembrados en produccion desde el 2026-08-02. **No existia donde responderlos**: la politica ordenaba una lista que nadie veia |
| `flota_gasto` + `flota_tanqueo` | La espina de la fase 1. Sin ella no hay costo por kilometro, que es la pregunta que el ERP no contesta hoy **no porque le falte la plata: le falta el kilometro** |

`flota_ficha_tecnica` gana `capacidad_tanque_galones` y su `capacidad_tanque_fuente`.
Es el unico detector de sobre-tanqueo que **no necesita umbral ni canon ni
medicion previa**: un tanque de 15 galones que recibe 22 no es error de
medicion. La procedencia va al lado del dato, igual que `distribucion_fuente` y
`frenos_fuente` — un numero sin procedencia se lee como si alguien lo hubiera
verificado.

## El cuerpo se emitio, no se transcribio

`scratchpad/emitir_migracion.py` sobre `db.metadata`, igual que
`f10ta1cimientos` y `m017flotahallazgo`. Una transcripcion manual de 4 tablas,
20 CHECK, 5 indices y 3 constraints unicos es donde se pierde un constraint sin
que nadie lo note, y un invariante que la base no impone es una sugerencia.

## Por que esta migracion existe, y quien la pidio

**La escribio el trinquete, no una persona.** Las cuatro tablas entraron a los
modelos el 2026-09-01 sin migracion, y
`tests/test_deriva_esquema.py::TestNingunaColumnaSinMigracion` se puso rojo
nombrando las 20 columnas huerfanas. Sin ese guard, produccion habria seguido
funcionando —las tablas entran por `create_all()`— y **una base construida desde
cero no las habria tenido**: la aplicacion revienta al leerlas, y el dia que se
descubre es el dia de la restauracion de un respaldo.

Las dos columnas de la ficha van con `ADD COLUMN IF NOT EXISTS` (patron de
`m014`): correcta tanto si la columna ya entro por `create_all` como si no.

## Los CHECK con `CASE WHEN`, otra vez a proposito

`ck_flota_insp_veredicto_coherente` es el que impone la regla 1 del modulo en la
base: **`incompleta` no es `no_apto`**. Un item bloqueante sin responder no es
"esta mal", es "no se sabe" — y no saber tampoco autoriza despacho. Escrito con
`CASE WHEN` y no sumando predicados porque PostgreSQL no suma booleanos; esa
forma paso 25 tests contra SQLite y revento el `CREATE TABLE` en el release del
2026-08-01.

El `ELSE 1 = 0` del final no es decorativo: un veredicto fuera del vocabulario
queda rechazado por esta expresion **ademas** de por `ck_flota_veredicto`. Dos
puertas, porque es la regla que decide si un camion sale.

Backup de referencia: PITR activo en Railway. Head previo: m017flotahallazgo.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm018flotainspecciongasto'
down_revision = 'm017flotahallazgo'
branch_labels = None
depends_on = None

# El vocabulario se congela aca a proposito: una migracion describe el esquema
# en un momento y no debe cambiar de forma si manana el dominio agrega una
# categoria de gasto. Ese cambio sera otra migracion. Mismo criterio que
# `f10ta4angulo` y `m017`.
_FUENTE = ('manual_fabricante', 'concesionario', 'placa_motor', 'taller',
           'estimado', 'sin_dato')


def upgrade():
    op.create_table(
        'flota_inspeccion',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vehiculo_id', sa.Integer(), nullable=False),
        sa.Column('plantilla_id', sa.Integer(), nullable=False),
        sa.Column('dia', sa.Date(), nullable=False),
        sa.Column('respondida_ts', sa.DateTime(), nullable=False),
        sa.Column('inspeccionada_por_usuario_id', sa.Integer(), nullable=False),
        sa.Column('lectura_id', sa.Integer(), nullable=False),
        sa.Column('custodia_id', sa.Integer(), nullable=True),
        sa.Column('veredicto', sa.String(length=20), nullable=False),
        sa.Column('items_esperados', sa.Integer(), nullable=False),
        sa.Column('items_sin_dato', sa.Integer(), nullable=False),
        sa.Column('bloqueantes_no_aptos', sa.Integer(), nullable=False),
        sa.Column('segundos_llenado', sa.Integer(), nullable=False),
        sa.Column('observacion', sa.Text(), nullable=True),
        sa.CheckConstraint(
            "CASE  WHEN veredicto = 'no_apto'    THEN bloqueantes_no_aptos > 0"
            "  WHEN veredicto = 'incompleta' THEN bloqueantes_no_aptos = 0"
            "                                 AND items_sin_dato > 0"
            "  WHEN veredicto = 'apto'       THEN bloqueantes_no_aptos = 0"
            "                                 AND items_sin_dato = 0"
            "  ELSE 1 = 0 END",
            name='ck_flota_insp_veredicto_coherente'),
        sa.CheckConstraint("veredicto IN ('apto', 'no_apto', 'incompleta')",
                           name='ck_flota_veredicto'),
        sa.CheckConstraint(
            'bloqueantes_no_aptos >= 0 AND bloqueantes_no_aptos <= items_esperados',
            name='ck_flota_insp_bloqueantes_en_rango'),
        sa.CheckConstraint('items_esperados > 0',
                           name='ck_flota_insp_items_esperados'),
        sa.CheckConstraint('items_sin_dato >= 0 AND items_sin_dato <= items_esperados',
                           name='ck_flota_insp_sin_dato_en_rango'),
        sa.CheckConstraint('segundos_llenado >= 0',
                           name='ck_flota_insp_segundos_no_negativos'),
        sa.ForeignKeyConstraint(['custodia_id'], ['flota_custodia.id'], ),
        sa.ForeignKeyConstraint(['inspeccionada_por_usuario_id'], ['usuarios.id'], ),
        sa.ForeignKeyConstraint(['lectura_id'], ['flota_lectura_odometro.id'], ),
        sa.ForeignKeyConstraint(['plantilla_id'], ['flota_plantilla_inspeccion.id'], ),
        sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_flota_inspeccion_custodia_id'), 'flota_inspeccion',
                    ['custodia_id'], unique=False)
    op.create_index('ix_flota_inspeccion_dia', 'flota_inspeccion',
                    ['vehiculo_id', 'dia'], unique=False)

    op.create_table(
        'flota_respuesta_item',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('inspeccion_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('respuesta', sa.String(length=20), nullable=False),
        sa.Column('orden_mostrado', sa.Integer(), nullable=False),
        sa.Column('nota', sa.Text(), nullable=True),
        sa.Column('hallazgo_id', sa.Integer(), nullable=True),
        sa.CheckConstraint("hallazgo_id IS NULL OR respuesta = 'no_apto'",
                           name='ck_flota_resp_hallazgo_solo_si_no_apto'),
        sa.CheckConstraint("respuesta IN ('optimo', 'no_apto', 'sin_dato')",
                           name='ck_flota_respuesta'),
        sa.CheckConstraint('orden_mostrado > 0', name='ck_flota_resp_orden_positivo'),
        sa.ForeignKeyConstraint(['hallazgo_id'], ['flota_hallazgo.id'], ),
        sa.ForeignKeyConstraint(['inspeccion_id'], ['flota_inspeccion.id'], ),
        sa.ForeignKeyConstraint(['item_id'], ['flota_item_inspeccion.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('inspeccion_id', 'item_id', name='uq_flota_resp_item'),
        sa.UniqueConstraint('inspeccion_id', 'orden_mostrado',
                            name='uq_flota_resp_orden'),
    )
    op.create_index(op.f('ix_flota_respuesta_item_inspeccion_id'),
                    'flota_respuesta_item', ['inspeccion_id'], unique=False)

    op.create_table(
        'flota_gasto',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('vehiculo_id', sa.Integer(), nullable=False),
        sa.Column('categoria', sa.String(length=20), nullable=False),
        sa.Column('fecha', sa.Date(), nullable=False),
        sa.Column('valor', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('lectura_id', sa.Integer(), nullable=False),
        sa.Column('proveedor', sa.Text(), nullable=False),
        sa.Column('documento_numero', sa.String(length=60), nullable=True),
        sa.Column('periodo_desde', sa.Date(), nullable=False),
        sa.Column('periodo_hasta', sa.Date(), nullable=False),
        sa.Column('centro_op', sa.String(length=10), nullable=True),
        sa.Column('origen_costo', sa.String(length=20), nullable=False),
        sa.Column('descripcion', sa.Text(), nullable=True),
        sa.Column('registrado_por_usuario_id', sa.Integer(), nullable=False),
        sa.Column('creado_ts', sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "categoria <> 'otro' OR (descripcion IS NOT NULL "
            "AND length(trim(descripcion)) > 0)",
            name='ck_flota_gasto_otro_con_descripcion'),
        sa.CheckConstraint(
            "categoria IN ('combustible', 'mantenimiento', 'repuesto', 'llanta', "
            "'soat', 'rtm', 'impuesto_vehicular', 'seguro', 'peaje', 'lavado', "
            "'parqueadero', 'multa', 'otro')",
            name='ck_flota_categoria'),
        sa.CheckConstraint(
            "origen_costo IN ('tarjeta_convenio', 'credito_proveedor', "
            "'efectivo_conductor', 'sin_dato')",
            name='ck_flota_origen_costo'),
        sa.CheckConstraint('centro_op IS NULL OR length(trim(centro_op)) > 0',
                           name='ck_flota_gasto_centro_op_no_vacio'),
        sa.CheckConstraint(
            'documento_numero IS NULL OR length(trim(documento_numero)) > 0',
            name='ck_flota_gasto_documento_no_vacio'),
        sa.CheckConstraint('length(trim(proveedor)) > 0',
                           name='ck_flota_gasto_proveedor'),
        sa.CheckConstraint('periodo_hasta >= periodo_desde',
                           name='ck_flota_gasto_periodo_coherente'),
        sa.CheckConstraint('valor > 0', name='ck_flota_gasto_valor_positivo'),
        sa.ForeignKeyConstraint(['lectura_id'], ['flota_lectura_odometro.id'], ),
        sa.ForeignKeyConstraint(['registrado_por_usuario_id'], ['usuarios.id'], ),
        sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('vehiculo_id', 'documento_numero',
                            name='uq_flota_gasto_documento'),
    )
    op.create_index('ix_flota_gasto_ventana', 'flota_gasto',
                    ['vehiculo_id', 'periodo_hasta', 'periodo_desde'], unique=False)

    op.create_table(
        'flota_tanqueo',
        sa.Column('gasto_id', sa.Integer(), nullable=False),
        sa.Column('galones', sa.Numeric(precision=8, scale=3), nullable=False),
        sa.Column('tanque', sa.String(length=12), nullable=False),
        sa.Column('estacion', sa.Text(), nullable=False),
        sa.CheckConstraint("tanque IN ('lleno', 'parcial', 'sin_dato')",
                           name='ck_flota_tanque'),
        sa.CheckConstraint('galones > 0', name='ck_flota_tanqueo_galones'),
        sa.CheckConstraint('length(trim(estacion)) > 0',
                           name='ck_flota_tanqueo_estacion'),
        sa.ForeignKeyConstraint(['gasto_id'], ['flota_gasto.id'], ),
        sa.PrimaryKeyConstraint('gasto_id'),
    )

    # ── La capacidad del tanque, con su procedencia ───────────────────────
    # `IF NOT EXISTS` (patron de m014): las columnas ya pueden existir en
    # produccion, porque entraron por `create_all()` antes de que existiera
    # esta migracion. La forma es correcta en los dos mundos.
    op.execute('ALTER TABLE flota_ficha_tecnica '
               'ADD COLUMN IF NOT EXISTS capacidad_tanque_galones NUMERIC(6,2)')
    op.execute("ALTER TABLE flota_ficha_tecnica ADD COLUMN IF NOT EXISTS "
               "capacidad_tanque_fuente VARCHAR(30) NOT NULL DEFAULT 'sin_dato'")

    # Los CHECK van aparte y solo en PostgreSQL: SQLite no sabe hacer
    # `ALTER TABLE ... ADD CONSTRAINT`, y alla el constraint se rehace solo
    # cuando `create_all` construye la tabla desde el modelo — que es como
    # corren los tests. Mismo criterio que los triggers de `f10ta1cimientos`.
    if op.get_bind().dialect.name == 'postgresql':
        op.create_check_constraint(
            'ck_flota_capacidad_tanque_positiva', 'flota_ficha_tecnica',
            'capacidad_tanque_galones IS NULL OR capacidad_tanque_galones > 0')
        op.create_check_constraint(
            'ck_flota_capacidad_tanque_fuente', 'flota_ficha_tecnica',
            'capacidad_tanque_fuente IN (%s)' % ', '.join(
                f"'{f}'" for f in _FUENTE))
        # Procedencia obligatoria: un numero sin fuente se lee como si alguien
        # lo hubiera verificado. Es el mismo par que ya imponen
        # `ck_flota_distribucion_con_procedencia` y su hermano de frenos.
        op.create_check_constraint(
            'ck_flota_capacidad_tanque_con_procedencia', 'flota_ficha_tecnica',
            "(capacidad_tanque_galones IS NULL "
            "  AND capacidad_tanque_fuente = 'sin_dato') OR "
            "(capacidad_tanque_galones IS NOT NULL "
            "  AND capacidad_tanque_fuente <> 'sin_dato')")


def downgrade():
    if op.get_bind().dialect.name == 'postgresql':
        for nombre in ('ck_flota_capacidad_tanque_con_procedencia',
                       'ck_flota_capacidad_tanque_fuente',
                       'ck_flota_capacidad_tanque_positiva'):
            op.drop_constraint(nombre, 'flota_ficha_tecnica', type_='check')
    op.execute('ALTER TABLE flota_ficha_tecnica '
               'DROP COLUMN IF EXISTS capacidad_tanque_fuente')
    op.execute('ALTER TABLE flota_ficha_tecnica '
               'DROP COLUMN IF EXISTS capacidad_tanque_galones')

    # Orden inverso, hijos antes que padres: `flota_tanqueo` cuelga de
    # `flota_gasto` y `flota_respuesta_item` de `flota_inspeccion`.
    op.drop_table('flota_tanqueo')
    op.drop_index('ix_flota_gasto_ventana', table_name='flota_gasto')
    op.drop_table('flota_gasto')
    op.drop_index(op.f('ix_flota_respuesta_item_inspeccion_id'),
                  table_name='flota_respuesta_item')
    op.drop_table('flota_respuesta_item')
    op.drop_index('ix_flota_inspeccion_dia', table_name='flota_inspeccion')
    op.drop_index(op.f('ix_flota_inspeccion_custodia_id'),
                  table_name='flota_inspeccion')
    op.drop_table('flota_inspeccion')
