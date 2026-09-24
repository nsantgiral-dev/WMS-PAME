"""Bitácora de acciones y autores que faltaban (Fase 0 de analítica)

Revision ID: m034bitacora
Revises: m033ciclo
Create Date: 2026-09-24

## Para qué

La analítica tiene que poder reconstruir el recorrido pedido → caja y ver
todo lo que se elimina, cancela o edita, con quién, cuándo y por qué.

- `bitacora_acciones` — una fila por acción que interrumpe o reescribe el
  flujo (ver `app/services/bitacora.py`). **Sin claves foráneas**: está en
  `PROTEGIDAS_ANALITICAS` y sobrevive al acta de corte, las filas a las que
  apunta no. Ids sueltos + código legible.
- Autores del flujo normal, que no pasan por la bitácora:
  · `tareas_picking.ultimo_operario_id` — quien tenía la tarea cuando se
    reabrió, bloqueó, canceló o auditó (esas vías ponen `operario_id` en
    NULL para devolverla al pool, y con eso se perdía quién la trabajó).
  · `tareas_packing.cerrado_por_id` — quién cerró el empaque.
  · `bultos.cargado_por_id`, `bultos.asignado_ruta_por_id`,
    `bultos.asignado_ruta_at` — quién cargó el bulto y quién lo puso en la
    ruta.
  · `rutas_despacho.liquidada_en` / `liquidada_por_id` — la liquidación
    tenía estado y no tenía ni fecha ni autor.
  · `juicios_temporada.anulado_*` — retractar un juicio lo borraba, y la
    tabla es analítica protegida («no recalculable»). Ahora se anula.

## Aditiva y sin backfill

Todo nullable. El histórico queda en NULL: no se inventa un autor que no se
registró (Regla 0). `ultimo_operario_id` y `cerrado_por_id` no se infieren
de otras columnas por la misma razón.
"""
import sqlalchemy as sa
from alembic import op

revision = 'm034bitacora'
down_revision = 'm033ciclo'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'bitacora_acciones',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('ocurrido_en', sa.DateTime(), nullable=False),
        sa.Column('dia_operativo', sa.Date(), nullable=False),
        sa.Column('accion', sa.String(length=20), nullable=False),
        sa.Column('entidad', sa.String(length=40), nullable=False),
        sa.Column('entidad_id', sa.Integer(), nullable=True),
        sa.Column('entidad_codigo', sa.String(length=80), nullable=True),
        sa.Column('usuario_id', sa.Integer(), nullable=True),
        sa.Column('motivo', sa.Text(), nullable=True),
        sa.Column('antes', sa.JSON(), nullable=True),
        sa.Column('despues', sa.JSON(), nullable=True),
        sa.Column('almacen_id', sa.Integer(), nullable=True),
        sa.Column('origen', sa.String(length=120), nullable=True),
    )
    op.create_index('ix_bitacora_dia', 'bitacora_acciones', ['dia_operativo'])
    op.create_index('ix_bitacora_entidad', 'bitacora_acciones', ['entidad', 'entidad_id'])
    op.create_index('ix_bitacora_accion', 'bitacora_acciones', ['accion'])
    op.create_index('ix_bitacora_usuario', 'bitacora_acciones', ['usuario_id'])

    with op.batch_alter_table('tareas_picking') as batch:
        batch.add_column(sa.Column('ultimo_operario_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_tareas_picking_ultimo_operario',
                                 'usuarios', ['ultimo_operario_id'], ['id'])
    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('cerrado_por_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_tareas_packing_cerrado_por',
                                 'usuarios', ['cerrado_por_id'], ['id'])
    with op.batch_alter_table('bultos') as batch:
        batch.add_column(sa.Column('cargado_por_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('asignado_ruta_por_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('asignado_ruta_at', sa.DateTime(), nullable=True))
        batch.create_foreign_key('fk_bultos_cargado_por',
                                 'usuarios', ['cargado_por_id'], ['id'])
        batch.create_foreign_key('fk_bultos_asignado_ruta_por',
                                 'usuarios', ['asignado_ruta_por_id'], ['id'])
    with op.batch_alter_table('rutas_despacho') as batch:
        batch.add_column(sa.Column('liquidada_en', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('liquidada_por_id', sa.Integer(), nullable=True))
        batch.create_foreign_key('fk_rutas_despacho_liquidada_por',
                                 'usuarios', ['liquidada_por_id'], ['id'])
    with op.batch_alter_table('juicios_temporada') as batch:
        batch.add_column(sa.Column('anulado_en', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('anulado_por_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('anulado_motivo', sa.Text(), nullable=True))
        batch.create_foreign_key('fk_juicios_temporada_anulado_por',
                                 'usuarios', ['anulado_por_id'], ['id'])


def downgrade():
    with op.batch_alter_table('juicios_temporada') as batch:
        batch.drop_constraint('fk_juicios_temporada_anulado_por', type_='foreignkey')
        batch.drop_column('anulado_motivo')
        batch.drop_column('anulado_por_id')
        batch.drop_column('anulado_en')
    with op.batch_alter_table('rutas_despacho') as batch:
        batch.drop_constraint('fk_rutas_despacho_liquidada_por', type_='foreignkey')
        batch.drop_column('liquidada_por_id')
        batch.drop_column('liquidada_en')
    with op.batch_alter_table('bultos') as batch:
        batch.drop_constraint('fk_bultos_asignado_ruta_por', type_='foreignkey')
        batch.drop_constraint('fk_bultos_cargado_por', type_='foreignkey')
        batch.drop_column('asignado_ruta_at')
        batch.drop_column('asignado_ruta_por_id')
        batch.drop_column('cargado_por_id')
    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_constraint('fk_tareas_packing_cerrado_por', type_='foreignkey')
        batch.drop_column('cerrado_por_id')
    with op.batch_alter_table('tareas_picking') as batch:
        batch.drop_constraint('fk_tareas_picking_ultimo_operario', type_='foreignkey')
        batch.drop_column('ultimo_operario_id')
    op.drop_index('ix_bitacora_usuario', table_name='bitacora_acciones')
    op.drop_index('ix_bitacora_accion', table_name='bitacora_acciones')
    op.drop_index('ix_bitacora_entidad', table_name='bitacora_acciones')
    op.drop_index('ix_bitacora_dia', table_name='bitacora_acciones')
    op.drop_table('bitacora_acciones')
