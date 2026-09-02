"""idx_picking_packing_conteo

Revision ID: a8b9c0d1e2f4
Revises: z7a8b9c0d1e2
Create Date: 2026-04-25

Índices de soporte para las tablas de operación:
  - tareas_picking: estado, operario_id, referencia_documento, (almacen_id, estado)
  - tareas_packing: numero_pedido_siesa, estado, siesa_triggered, empacador_id
  - sesiones_conteo: operario_id, estado
  - recaudos_entrega: ruta_id, tarea_id
  - tareas_reposicion: estado, (almacen_id, estado)
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'a8b9c0d1e2f4'
down_revision = 'z7a8b9c0d1e2'
branch_labels = None
depends_on = None


def upgrade():
    # ── tareas_picking ────────────────────────────────────────────────────────
    op.create_index('ix_tareas_picking_estado',
                    'tareas_picking', ['estado'], unique=False)
    op.create_index('ix_tareas_picking_operario_id',
                    'tareas_picking', ['operario_id'], unique=False)
    op.create_index('ix_tareas_picking_referencia_documento',
                    'tareas_picking', ['referencia_documento'], unique=False)
    op.create_index('ix_tareas_picking_almacen_estado',
                    'tareas_picking', ['almacen_id', 'estado'], unique=False)

    # ── tareas_packing ────────────────────────────────────────────────────────
    op.create_index('ix_tareas_packing_numero_pedido_siesa',
                    'tareas_packing', ['numero_pedido_siesa'], unique=False)
    op.create_index('ix_tareas_packing_estado',
                    'tareas_packing', ['estado'], unique=False)
    op.create_index('ix_tareas_packing_siesa_triggered',
                    'tareas_packing', ['siesa_triggered'], unique=False)
    op.create_index('ix_tareas_packing_empacador_id',
                    'tareas_packing', ['empacador_id'], unique=False)

    # ── sesiones_conteo ───────────────────────────────────────────────────────
    op.create_index('ix_sesiones_conteo_operario_id',
                    'sesiones_conteo', ['operario_id'], unique=False)
    op.create_index('ix_sesiones_conteo_estado',
                    'sesiones_conteo', ['estado'], unique=False)

    # ── recaudos_entrega ──────────────────────────────────────────────────────
    op.create_index('ix_recaudos_entrega_ruta_id',
                    'recaudos_entrega', ['ruta_id'], unique=False)
    op.create_index('ix_recaudos_entrega_tarea_id',
                    'recaudos_entrega', ['tarea_id'], unique=False)

    # ── tareas_reposicion ─────────────────────────────────────────────────────
    # `IF NOT EXISTS` porque `y6z7a8b9c0d1_add_zonas_reposicion` (dos
    # migraciones antes en la cadena) YA crea este mismo índice. Con
    # `create_index` normal, **la cadena desde cero no puede completarse**:
    # revienta acá con `DuplicateTable`. Verificado el 2026-08-20 contra una
    # base PostgreSQL limpia.
    #
    # En producción no cambia nada —ambas migraciones ya están aplicadas y
    # alembic no las re-ejecuta—; lo que desbloquea es levantar un ambiente
    # nuevo: un QA propio, la máquina de alguien que entra, o la verificación
    # de un respaldo restaurado.
    #
    # Es el único choque real del historial: las otras 43 repeticiones de
    # `create_index` son `batch_op`, que recrean el índice después de
    # reconstruir la tabla y por eso no colisionan.
    op.execute('CREATE INDEX IF NOT EXISTS ix_tareas_reposicion_estado '
               'ON tareas_reposicion (estado)')
    op.create_index('ix_tareas_reposicion_almacen_estado',
                    'tareas_reposicion', ['almacen_id', 'estado'], unique=False)


def downgrade():
    op.drop_index('ix_tareas_reposicion_almacen_estado',   table_name='tareas_reposicion')
    op.execute('DROP INDEX IF EXISTS ix_tareas_reposicion_estado')
    op.drop_index('ix_recaudos_entrega_tarea_id',          table_name='recaudos_entrega')
    op.drop_index('ix_recaudos_entrega_ruta_id',           table_name='recaudos_entrega')
    op.drop_index('ix_sesiones_conteo_estado',             table_name='sesiones_conteo')
    op.drop_index('ix_sesiones_conteo_operario_id',        table_name='sesiones_conteo')
    op.drop_index('ix_tareas_packing_empacador_id',        table_name='tareas_packing')
    op.drop_index('ix_tareas_packing_siesa_triggered',     table_name='tareas_packing')
    op.drop_index('ix_tareas_packing_estado',              table_name='tareas_packing')
    op.drop_index('ix_tareas_packing_numero_pedido_siesa', table_name='tareas_packing')
    op.drop_index('ix_tareas_picking_almacen_estado',      table_name='tareas_picking')
    op.drop_index('ix_tareas_picking_referencia_documento',table_name='tareas_picking')
    op.drop_index('ix_tareas_picking_operario_id',         table_name='tareas_picking')
    op.drop_index('ix_tareas_picking_estado',              table_name='tareas_picking')
