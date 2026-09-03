"""flota: la llanta como entidad, no como renglon

Revision ID: m023flotallantas
Revises: m022flotapreventivo
Create Date: 2026-09-02

PURAMENTE ADITIVA. Dos tablas, ningun ALTER, ningun DROP.

## Entidad propia y no un tipo de repuesto

Las llantas tienen identidad que sobrevive al vehiculo: rotan de posicion, pasan
a otro camion, van a reencauche y vuelven. Como renglon de factura, *«¿cuanto
duran?»* no se puede ni formular.

## `flota_montaje_llanta` es literalmente la forma de `flota_custodia`

Intervalo abierto, lectura de apertura y de cierre, `fin_ts IS NULL` = vigente, y
**dos indices unicos parciales** que impiden las dos combinaciones imposibles:

  · `uq_flota_montaje_llanta_vigente`   — una llanta en dos vehiculos a la vez
  · `uq_flota_montaje_posicion_vigente` — dos llantas en la misma posicion

Parciales sobre `fin_ts IS NULL` a proposito: N montajes CERRADOS de la misma
posicion conviven, que es la historia entera de esa rueda. Un `check-then-insert`
sin indice detras no es un invariante, es una carrera.

`km_acumulado` **no se guarda, se calcula**: con 24 llantas, denormalizar es
garantizar divergencia.

## Y el modo de fallo caro no es que se gasten

Es que se gasten **mal** —desalineacion, presion, un eje que come el flanco
interno— y eso solo se ve **por posicion**. De ahi que la posicion sea parte de
la clave del montaje y no un atributo suelto.

El cuerpo se EMITIO desde `db.metadata` con `scratchpad/emitir_migracion.py`, no
se transcribio. Una transcripcion manual de columnas, CHECK e indices es donde se
pierde un constraint sin que nadie lo note, y un invariante que la base no impone
es una sugerencia.

Backup de referencia: PITR activo en Railway. Head previo: m022flotapreventivo.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm023flotallantas'
down_revision = 'm022flotapreventivo'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('flota_llanta',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('codigo', sa.String(length=40), nullable=False),
    sa.Column('marca', sa.String(length=40), server_default='sin_dato', nullable=False),
    sa.Column('medida', sa.Text(), nullable=False),
    sa.Column('registrada_por_usuario_id', sa.Integer(), nullable=False),
    sa.Column('creada_ts', sa.DateTime(), nullable=False),
    sa.CheckConstraint('length(trim(codigo)) > 0', name='ck_flota_llanta_codigo_no_vacio'),
    sa.CheckConstraint('length(trim(medida)) > 0', name='ck_flota_llanta_medida_no_vacia'),
    sa.ForeignKeyConstraint(['registrada_por_usuario_id'], ['usuarios.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('codigo')
    )
    op.create_table('flota_montaje_llanta',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('llanta_id', sa.Integer(), nullable=False),
    sa.Column('vehiculo_id', sa.Integer(), nullable=False),
    sa.Column('posicion', sa.Integer(), nullable=False),
    sa.Column('inicio_ts', sa.DateTime(), nullable=False),
    sa.Column('fin_ts', sa.DateTime(), nullable=True),
    sa.Column('lectura_inicio_id', sa.Integer(), nullable=False),
    sa.Column('lectura_fin_id', sa.Integer(), nullable=True),
    sa.Column('motivo_desmontaje', sa.String(length=30), nullable=True),
    sa.Column('montada_por_usuario_id', sa.Integer(), nullable=False),
    sa.Column('desmontada_por_usuario_id', sa.Integer(), nullable=True),
    sa.Column('gasto_id', sa.Integer(), nullable=True),
    sa.Column('observacion', sa.Text(), nullable=True),
    sa.CheckConstraint("motivo_desmontaje IS NULL OR motivo_desmontaje IN ('desgaste_normal', 'desgaste_irregular', 'pinchazo', 'corte_flanco', 'reencauche', 'rotacion', 'sin_dato')", name='ck_flota_montaje_motivo'),
    sa.CheckConstraint('CASE  WHEN fin_ts IS NULL THEN lectura_fin_id IS NULL                       AND desmontada_por_usuario_id IS NULL                       AND motivo_desmontaje IS NULL  ELSE lectura_fin_id IS NOT NULL       AND desmontada_por_usuario_id IS NOT NULL       AND motivo_desmontaje IS NOT NULL END', name='ck_flota_montaje_desenlace'),
    sa.CheckConstraint('fin_ts IS NULL OR fin_ts >= inicio_ts', name='ck_flota_montaje_cierre_posterior'),
    sa.CheckConstraint('posicion >= 1 AND posicion <= 12', name='ck_flota_montaje_posicion_en_rango'),
    sa.ForeignKeyConstraint(['desmontada_por_usuario_id'], ['usuarios.id'], ),
    sa.ForeignKeyConstraint(['gasto_id'], ['flota_gasto.id'], ),
    sa.ForeignKeyConstraint(['lectura_fin_id'], ['flota_lectura_odometro.id'], ),
    sa.ForeignKeyConstraint(['lectura_inicio_id'], ['flota_lectura_odometro.id'], ),
    sa.ForeignKeyConstraint(['llanta_id'], ['flota_llanta.id'], ),
    sa.ForeignKeyConstraint(['montada_por_usuario_id'], ['usuarios.id'], ),
    sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_flota_montaje_historia', 'flota_montaje_llanta', ['llanta_id', 'inicio_ts'], unique=False)
    op.create_index('ix_flota_montaje_vehiculo', 'flota_montaje_llanta', ['vehiculo_id', 'posicion'], unique=False)
    op.create_index('uq_flota_montaje_llanta_vigente', 'flota_montaje_llanta', ['llanta_id'], unique=True, postgresql_where=sa.text('fin_ts IS NULL'), sqlite_where=sa.text('fin_ts IS NULL'))
    op.create_index('uq_flota_montaje_posicion_vigente', 'flota_montaje_llanta', ['vehiculo_id', 'posicion'], unique=True, postgresql_where=sa.text('fin_ts IS NULL'), sqlite_where=sa.text('fin_ts IS NULL'))


def downgrade():
    op.drop_table('flota_montaje_llanta')
    op.drop_table('flota_llanta')
