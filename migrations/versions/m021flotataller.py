"""flota: la orden de trabajo y la garantia

Revision ID: m021flotataller
Revises: m020geoentrega
Create Date: 2026-09-02

PURAMENTE ADITIVA. Dos tablas, ningun ALTER, ningun DROP.

## Las tres decisiones que la forma impone

**La OT no lleva el costo.** El camion entra hoy y la factura llega el 30. Una OT
con `valor NOT NULL` obliga a inventar un numero para cerrarla. El gasto llega
despues apuntando a la OT — y mientras tanto «trabajos hechos sin factura
recibida» es un numero que el health cuenta.

**La garantia va en la INTERVENCION, no en la OT.** Una visita produce varios
trabajos con garantias distintas: el embrague trae 6 meses, el cambio de aceite
ninguna. Con un campo en la OT hay que dar una sola respuesta para una bolsa
mezclada.

**`flota_intervencion` es la segunda extremidad de la espina `flota_gasto`**
(la primera es `flota_tanqueo`). Si cada tabla llevara su propia columna de
valor, el CPK seria un `UNION` de N ramas y alguien olvidaria la N+1 — es
`_BODEGA_CO_MAP` otra vez, con plata.

`garantia_hasta_fecha` y `garantia_hasta_km` se calculan al nacer. Si alguien
pudiera escribir «esta vence en marzo», la garantia dejaria de significar algo.
Y `garantia_declarada` admite `sin_dato`: una factura que no dice nada **no es
una factura sin garantia**, es que no se pregunto (regla 4).

El cuerpo se EMITIO desde `db.metadata` con `scratchpad/emitir_migracion.py`, no
se transcribio. Una transcripcion manual de columnas, CHECK e indices es donde se
pierde un constraint sin que nadie lo note, y un invariante que la base no impone
es una sugerencia.

Backup de referencia: PITR activo en Railway. Head previo: m020geoentrega.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm021flotataller'
down_revision = 'm020geoentrega'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('flota_orden_trabajo',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('vehiculo_id', sa.Integer(), nullable=False),
    sa.Column('tipo', sa.String(length=20), nullable=False),
    sa.Column('estado', sa.String(length=20), server_default='abierta', nullable=False),
    sa.Column('taller', sa.Text(), nullable=False),
    sa.Column('descripcion', sa.Text(), nullable=False),
    sa.Column('lectura_id', sa.Integer(), nullable=False),
    sa.Column('hallazgo_id', sa.Integer(), nullable=True),
    sa.Column('abierta_ts', sa.DateTime(), nullable=False),
    sa.Column('abierta_por_usuario_id', sa.Integer(), nullable=False),
    sa.Column('cerrada_ts', sa.DateTime(), nullable=True),
    sa.Column('cerrada_por_usuario_id', sa.Integer(), nullable=True),
    sa.Column('motivo_cierre', sa.Text(), nullable=True),
    sa.CheckConstraint("CASE  WHEN estado = 'abierta' THEN cerrada_ts IS NULL  WHEN estado = 'cerrada' THEN cerrada_ts IS NOT NULL  WHEN estado = 'anulada' THEN       cerrada_ts IS NOT NULL AND motivo_cierre IS NOT NULL       AND length(trim(motivo_cierre)) > 0  ELSE 1 = 1 END", name='ck_flota_ot_desenlace'),
    sa.CheckConstraint("estado IN ('abierta', 'cerrada', 'anulada')", name='ck_flota_estado'),
    sa.CheckConstraint("tipo IN ('correctiva', 'preventiva')", name='ck_flota_tipo'),
    sa.CheckConstraint('length(trim(descripcion)) > 0', name='ck_flota_ot_descripcion'),
    sa.CheckConstraint('length(trim(taller)) > 0', name='ck_flota_ot_taller'),
    sa.ForeignKeyConstraint(['abierta_por_usuario_id'], ['usuarios.id'], ),
    sa.ForeignKeyConstraint(['cerrada_por_usuario_id'], ['usuarios.id'], ),
    sa.ForeignKeyConstraint(['hallazgo_id'], ['flota_hallazgo.id'], ),
    sa.ForeignKeyConstraint(['lectura_id'], ['flota_lectura_odometro.id'], ),
    sa.ForeignKeyConstraint(['vehiculo_id'], ['vehiculos.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_flota_orden_trabajo_estado'), 'flota_orden_trabajo', ['estado'], unique=False)
    op.create_index(op.f('ix_flota_orden_trabajo_hallazgo_id'), 'flota_orden_trabajo', ['hallazgo_id'], unique=False)
    op.create_index('ix_flota_ot_abiertas', 'flota_orden_trabajo', ['vehiculo_id', 'estado'], unique=False)
    op.create_table('flota_intervencion',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('orden_trabajo_id', sa.Integer(), nullable=False),
    sa.Column('gasto_id', sa.Integer(), nullable=True),
    sa.Column('sistema', sa.String(length=30), nullable=False),
    sa.Column('descripcion', sa.Text(), nullable=True),
    sa.Column('garantia_declarada', sa.String(length=12), nullable=False),
    sa.Column('garantia_meses', sa.Integer(), nullable=True),
    sa.Column('garantia_km', sa.Integer(), nullable=True),
    sa.Column('garantia_hasta_fecha', sa.Date(), nullable=True),
    sa.Column('garantia_hasta_km', sa.Integer(), nullable=True),
    sa.Column('registrada_ts', sa.DateTime(), nullable=False),
    sa.Column('registrada_por_usuario_id', sa.Integer(), nullable=False),
    sa.CheckConstraint("CASE  WHEN garantia_declarada = 'si'  THEN garantia_meses IS NOT NULL OR garantia_km IS NOT NULL  ELSE garantia_meses IS NULL AND garantia_km IS NULL END", name='ck_flota_interv_garantia_coherente'),
    sa.CheckConstraint("garantia_declarada IN ('si', 'no', 'sin_dato')", name='ck_flota_garantia_declarada'),
    sa.CheckConstraint("sistema <> 'otro' OR (descripcion IS NOT NULL AND length(trim(descripcion)) > 0)", name='ck_flota_interv_otro_con_descripcion'),
    sa.CheckConstraint("sistema IN ('motor', 'transmision', 'embrague', 'frenos', 'suspension', 'direccion', 'electrico', 'refrigeracion', 'escape', 'llantas', 'carroceria', 'aire_acondicionado', 'otro')", name='ck_flota_sistema'),
    sa.CheckConstraint('CASE WHEN garantia_km IS NULL     THEN garantia_hasta_km IS NULL     ELSE garantia_hasta_km IS NOT NULL END', name='ck_flota_interv_km_viene_de_km'),
    sa.CheckConstraint('CASE WHEN garantia_meses IS NULL     THEN garantia_hasta_fecha IS NULL     ELSE garantia_hasta_fecha IS NOT NULL END', name='ck_flota_interv_fecha_viene_de_meses'),
    sa.CheckConstraint('garantia_km IS NULL OR garantia_km > 0', name='ck_flota_interv_km_positivos'),
    sa.CheckConstraint('garantia_meses IS NULL OR garantia_meses > 0', name='ck_flota_interv_meses_positivos'),
    sa.ForeignKeyConstraint(['gasto_id'], ['flota_gasto.id'], ),
    sa.ForeignKeyConstraint(['orden_trabajo_id'], ['flota_orden_trabajo.id'], ),
    sa.ForeignKeyConstraint(['registrada_por_usuario_id'], ['usuarios.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_flota_interv_sistema', 'flota_intervencion', ['orden_trabajo_id', 'sistema'], unique=False)
    op.create_index(op.f('ix_flota_intervencion_gasto_id'), 'flota_intervencion', ['gasto_id'], unique=False)


def downgrade():
    op.drop_table('flota_intervencion')
    op.drop_table('flota_orden_trabajo')
