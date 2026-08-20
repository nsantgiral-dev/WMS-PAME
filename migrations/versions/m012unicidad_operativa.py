"""Los cuatro check-then-insert que no tenían índice detrás

Revision ID: m012unicidadoperativa
Revises: m011custodiaconductorunica
Create Date: 2026-08-19

Cuatro servicios comprueban en Python que una fila no exista y después
insertan, sin bloqueo y sin índice único que respalde la comprobación:

    recaudos_entrega       ruta_service.py:978   «un recaudo por parada»
    tareas_packing         packing_service.py:48 «Ya existe una tarea de packing
                                                  para el pedido {n}»
    recepciones            recepcion_service.py:44 «Ya existe una recepción para
                                                  la OC {n}»
    devoluciones_cliente   liquidacion_service.py:1033 «no se duplica»

Entre el `.first()` y el `db.session.add()` no hay nada. Dos transacciones
concurrentes leen `None` las dos y insertan las dos.

## No hace falta concurrencia real para romper tres de las cuatro

`recaudos_entrega` tiene **dos escritores distintos**: `confirmar_parada` (lee
en :978, inserta en :1018) y `forzar_cierre_ruta` (lee en :1167, inserta en
:1181). El conductor confirmando mientras el admin fuerza el cierre basta.

`devoluciones_cliente` también: `crear_devoluciones_pendientes_ruta` se llama
desde `liquidar_ruta` y desde `forzar_cierre_ruta`.

`recepciones` lo dice su propio endpoint (`routes/siesa.py:1141`,
«Idempotente: si ya existe una recepción activa para esta OC, redirigir a
ella»): dos recepcionistas abriendo la misma OC desde el listado.

## Qué cuesta cada duplicado

· recaudo: `total_recaudado()` suma las dos filas, la liquidación itera todas
  → dos RC/NC al ERP por la misma factura. Y el congelamiento del monto tras
  el RC (`ruta_service.py:1000`) se decide con un `.first()` **sin `order_by`**:
  con dos filas, que la guardia dispare o no depende del orden del heap.
· packing: dos remisiones y dos facturas para el mismo pedido.
· recepción: dos entradas de compras 142948 y doble suma de inventario.
· devolución: dos notas crédito 251126, con cruce de cartera automático.

## El precedente que ya existía en el repo

`f4b84ad06843_add_sesion_conteo_unique_idx.py` hizo exactamente esto para
`sesiones_conteo`, con este docstring: *«Prevents race condition where API +
scheduler create duplicate CC1 sessions»*. Mismo problema, misma solución, y
las cuatro tablas de arriba —que mueven plata, inventario y documentos
fiscales— se quedaron sin ella. Nada explicaba la diferencia.

## Los índices son PARCIALES, y cada uno copia el filtro de su servicio

Un índice total prohibiría el historial legítimo: un pedido cuya tarea se
canceló y se rehízo, una OC recibida el año pasado y otra vez este. Cada
`postgresql_where` reproduce el `notin_`/`!=` que ya usa el check en Python —
si divergieran, el índice bloquearía casos que el servicio permite, y eso se
descubre cuando alguien no puede trabajar.

`recaudos_entrega` no lleva filtro: no tiene estado de anulación, y su
unicidad es sobre `(ruta_id, tarea_id)` a secas.

## Si hay datos sucios, esto se detiene y los nombra

Igual que `m011`: no se limpian solos. Cuál de las filas duplicadas es la
buena —cuál recaudo tiene el monto real, cuál recepción tiene el conteo
físico— lo decide una persona. Adivinarlo sería inventar el dato que estas
tablas existen para registrar.
"""
from alembic import op
import sqlalchemy as sa

revision = 'm012unicidadoperativa'
down_revision = 'm011custodiaconductorunica'
branch_labels = None
depends_on = None

#: (índice, tabla, columnas, where, cómo describir el duplicado)
_INDICES = [
    ('uq_recaudo_por_parada', 'recaudos_entrega',
     ['ruta_id', 'tarea_id'], None,
     'recaudos para la misma parada'),
    ('uq_packing_pedido_activo', 'tareas_packing',
     ['numero_pedido_siesa'], "estado <> 'CANCELADO' AND numero_pedido_siesa IS NOT NULL",
     'tareas de packing activas para el mismo pedido'),
    ('uq_recepcion_oc_activa', 'recepciones',
     ['numero_oc_siesa', 'co_oc_siesa'], "estado <> 'CANCELADA'",
     'recepciones activas para la misma OC'),
    ('uq_devolucion_por_recaudo', 'devoluciones_cliente',
     ['recaudo_entrega_id'], "estado <> 'CANCELADA' AND recaudo_entrega_id IS NOT NULL",
     'devoluciones activas para el mismo recaudo'),
]


def upgrade():
    conn = op.get_bind()
    problemas = []

    for indice, tabla, cols, where, descripcion in _INDICES:
        cols_sql = ', '.join(cols)
        filtro = f'WHERE {where}' if where else ''
        # `IS NOT DISTINCT FROM` no hace falta: se agrupa igual que agrupa el
        # índice, así que los NULL que el índice considera distintos también
        # quedan fuera de este conteo. Si un día eso deja de ser cierto, el
        # `CREATE INDEX` fallará y se verá.
        filas = conn.execute(sa.text(f"""
            SELECT {cols_sql}, count(*) AS n
              FROM {tabla}
              {filtro}
             GROUP BY {cols_sql}
            HAVING count(*) > 1
             LIMIT 20
        """)).fetchall()
        if filas:
            detalle = '\n'.join(
                f'    · {dict(zip(cols, f[:-1]))} → {f[-1]} filas' for f in filas)
            problemas.append(f'  {tabla} — {descripcion}:\n{detalle}')

    if problemas:
        raise RuntimeError(
            'No se pueden crear los índices: hay duplicados que la operación '
            'ya prohíbe.\n\n' + '\n\n'.join(problemas) +
            '\n\nResolvelos antes de migrar. NO se limpian automáticamente: '
            'cuál de las filas es la buena —cuál recaudo tiene el monto real, '
            'cuál recepción tiene el conteo físico— lo decide una persona que '
            'sepa qué pasó. Cancelar la sobrante (estado CANCELADO/CANCELADA) '
            'la saca del índice sin borrar el rastro.')

    for indice, tabla, cols, where, _ in _INDICES:
        op.create_index(indice, tabla, cols, unique=True,
                        postgresql_where=sa.text(where) if where else None)


def downgrade():
    for indice, tabla, _c, _w, _d in reversed(_INDICES):
        op.drop_index(indice, table_name=tabla)
