"""flota: el mantenimiento preventivo

Revision ID: m022flotapreventivo
Revises: m021flotataller
Create Date: 2026-09-02

PURAMENTE ADITIVA. Dos tablas, ningun ALTER, ningun DROP.

## La pieza de mayor valor del plan, y costaba una consulta

`flota_ficha_tecnica.distribucion_km_cambio` **ya estaba cargado en la base y
nadie lo leia**. Era la tabla diciendo a que kilometraje toca cambiar la correa,
mientras la correa envejecia. Una correa que revienta en motor de interferencia
es motor nuevo.

El plan se **siembra desde la ficha**, no se escribe a mano, y **hereda la
procedencia**: una tarea sembrada desde un `distribucion_km_cambio` cuyo
`distribucion_fuente` es `estimado` no vale lo mismo que una de
`manual_fabricante`, y quien la vea tiene que saberlo (regla 13).

## Una tarea que nunca se ejecuto no esta al dia ni vencida

Es un tercer estado, con palabras (regla 4). El health la cuenta como
`tareas_sin_linea_base` y **el aviso no sale**, porque no hay contra que
comparar. Un preventivo recien sembrado que mandara 40 avisos el primer dia
entrena a silenciar el canal, que es la leccion de los 639 avisos conocidos.

El cuerpo se EMITIO desde `db.metadata` con `scratchpad/emitir_migracion.py`, no
se transcribio. Una transcripcion manual de columnas, CHECK e indices es donde se
pierde un constraint sin que nadie lo note, y un invariante que la base no impone
es una sugerencia.

Backup de referencia: PITR activo en Railway. Head previo: m021flotataller.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm022flotapreventivo'
down_revision = 'm021flotataller'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('flota_plan_tarea',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('vehiculo_id', sa.Integer(), nullable=False),
    sa.Column('tipo', sa.String(length=30), nullable=False),
    sa.Column('intervalo_km', sa.Integer(), nullable=True),
    sa.Column('fuente', sa.String(length=30), server_default='sin_dato', nullable=False),
    sa.Column('origen', sa.String(length=10), server_default='ficha', nullable=False),
    sa.Column('activo', sa.Boolean(), server_default='1', nullable=False),
    sa.Column('sembrado_ts', sa.DateTime(), nullable=False),
    sa.Column('nota', sa.Text(), nullable=True),
    sa.CheckConstraint("(intervalo_km IS NULL AND fuente = 'sin_dato') OR (intervalo_km IS NOT NULL AND fuente <> 'sin_dato')", name='ck_flota_plan_intervalo_con_procedencia'),
    sa.CheckConstraint("fuente IN ('manual_fabricante', 'concesionario', 'placa_motor', 'taller', 'estimado', 'sin_dato')", name='ck_flota_fuente'),
    sa.CheckConstraint("origen IN ('ficha', 'manual')", name='ck_flota_origen'),
    sa.CheckConstraint("tipo IN ('distribucion', 'aceite_motor', 'aceite_caja', 'aceite_diferencial', 'refrigerante', 'lubricacion_cadena')", name='ck_flota_tipo'),
    sa.CheckConstraint('intervalo_km IS NULL OR intervalo_km > 0', name='ck_flota_plan_intervalo_positivo'),
    sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('vehiculo_id', 'tipo', name='uq_flota_plan_tarea')
    )
    op.create_index('ix_flota_plan_vigentes', 'flota_plan_tarea', ['vehiculo_id', 'activo'], unique=False)
    op.create_table('flota_ejecucion_tarea',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('plan_id', sa.Integer(), nullable=False),
    sa.Column('lectura_id', sa.Integer(), nullable=False),
    sa.Column('ejecutado_ts', sa.DateTime(), nullable=False),
    sa.Column('registrado_por_usuario_id', sa.Integer(), nullable=False),
    sa.Column('taller', sa.Text(), nullable=True),
    sa.Column('nota', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['lectura_id'], ['flota_lectura_odometro.id'], ),
    sa.ForeignKeyConstraint(['plan_id'], ['flota_plan_tarea.id'], ),
    sa.ForeignKeyConstraint(['registrado_por_usuario_id'], ['usuarios.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_flota_ejecucion_plan', 'flota_ejecucion_tarea', ['plan_id', 'ejecutado_ts'], unique=False)


def downgrade():
    op.drop_table('flota_ejecucion_tarea')
    op.drop_table('flota_plan_tarea')
