"""Fase 0 de analítica: claves de la cadena del pedido, historia y fotos de Siesa

Revision ID: m034fotos
Revises: m033ciclo
Create Date: 2026-09-24

## Para qué

«La analítica tiene que reconstruir el recorrido pedido → caja de punta a
punta y tener historia que Siesa no guarda.» Tres grupos, todos aditivos:

1. **Clave del pedido** en `tareas_picking` y `tareas_packing`
   (`pedido_clave` = `'003-PD-1502'`, CO + tipo + consecutivo). Hasta hoy se
   unían por un string sin CO. Se rellena el histórico (abajo).
2. **Desenlace de los documentos Siesa** en `recaudos_entrega`
   (`siesa_{nc,rc,dc}_{resultado,consec,at}`, `siesa_dc_detalle`): hasta hoy
   solo en `siesa_jobs`, que es una cola.
3. **Historia y fotos** — cinco tablas nuevas, solo agregar/upsert,
   PROTEGIDAS_ANALITICAS en el acta de corte y **sin FK** hacia operativas:
   `pedidos_historia`, `fotos_siesa_corridas`, `foto_ventas_lineas`,
   `foto_stock_diaria`, `foto_cartera_diaria`.

## Backfill de `pedido_clave`

Con **la misma regla** de `app/services/cadena_pedido.py` (una migración no
importa código de la app; `tests/test_cadena_pedido.py` compara las dos):

- tipo + consecutivo: los del packing si están; si no, se parte el número
  (`'PD1502'` → `PD`, `1502`).
- CO: el de `pedidos_siesa` para ese tipo + consecutivo si hay exactamente
  uno; si no, `almacenes.centro_op_siesa` del almacén de la tarea; si no,
  **NULL** — no se inventa (Regla 0).
- Picking: solo `tipo_documento` `PEDIDO`/`PEDIDO_SIESA`.

Lo que quede NULL se declara en el log del release (`print`).
"""
import re

import sqlalchemy as sa
from alembic import op

revision = 'm034fotos'
down_revision = 'm033ciclo'
branch_labels = None
depends_on = None

_TIPOS_PEDIDO = ('PEDIDO', 'PEDIDO_SIESA')
_NUMERO = re.compile(r'^\s*([A-Za-z]+)\s*-?\s*0*(\d+)\s*$')


# ── La regla, copiada de cadena_pedido.py (comparada por test) ──────────────

def _co(co):
    s = '' if co is None else str(co).strip()
    if not s:
        return None
    return s.zfill(3) if s.isdigit() else s.upper()


def _clave(co, tipo, consec):
    c = _co(co)
    t = ('' if tipo is None else str(tipo).strip().upper()) or None
    s = '' if consec is None else str(consec).strip()
    n = int(s) if s.isdigit() else None
    if not (c and t and n is not None):
        return None
    return f'{c}-{t}-{n}'


def _partir(numero):
    m = _NUMERO.match(str(numero or ''))
    return (m.group(1).upper(), int(m.group(2))) if m else None


def backfill_pedido_clave(conn) -> dict:
    """Rellena `pedido_clave` en picking y packing. Idempotente: solo toca NULL."""
    cos_pedido = {}
    for tipo, consec, co in conn.execute(sa.text(
            'SELECT DISTINCT tipo_docto, consec_docto, centro_op FROM pedidos_siesa')):
        k = ((tipo or '').strip().upper(), consec)
        if (co or '').strip():
            cos_pedido.setdefault(k, set()).add((co or '').strip())
    cos_almacen = {aid: (co or '').strip() or None for aid, co in conn.execute(
        sa.text('SELECT id, centro_op_siesa FROM almacenes'))}

    def _co_de(tipo, consec, almacen_id):
        cos = cos_pedido.get((tipo, consec)) or set()
        if len(cos) == 1:
            return next(iter(cos))
        if len(cos) > 1:
            return None
        return cos_almacen.get(almacen_id)

    res = {'packing': 0, 'packing_sin_clave': 0, 'picking': 0, 'picking_sin_clave': 0}

    filas = conn.execute(sa.text(
        "SELECT id, numero_pedido_siesa, tipo_docto_pedido_siesa, "
        "consec_docto_pedido_siesa, almacen_id FROM tareas_packing "
        "WHERE pedido_clave IS NULL AND numero_pedido_siesa IS NOT NULL "
        "AND (tipo_documento IS NULL OR tipo_documento <> 'TRASLADO')")).fetchall()
    for tid, numero, tipo, consec, alm in filas:
        t = (tipo or '').strip().upper() or None
        s = '' if consec is None else str(consec).strip()
        n = int(s) if s.isdigit() else None
        if not (t and n is not None):
            partes = _partir(numero)
            if not partes:
                res['packing_sin_clave'] += 1
                continue
            t, n = partes
        clave = _clave(_co_de(t, n, alm), t, n)
        if clave is None:
            res['packing_sin_clave'] += 1
            continue
        conn.execute(sa.text('UPDATE tareas_packing SET pedido_clave = :c WHERE id = :i'),
                     {'c': clave, 'i': tid})
        res['packing'] += 1

    filas = conn.execute(sa.text(
        "SELECT id, referencia_documento, almacen_id FROM tareas_picking "
        "WHERE pedido_clave IS NULL AND referencia_documento IS NOT NULL "
        "AND tipo_documento IN ('PEDIDO', 'PEDIDO_SIESA')")).fetchall()
    for tid, ref, alm in filas:
        partes = _partir(ref)
        if not partes:
            res['picking_sin_clave'] += 1
            continue
        t, n = partes
        clave = _clave(_co_de(t, n, alm), t, n)
        if clave is None:
            res['picking_sin_clave'] += 1
            continue
        conn.execute(sa.text('UPDATE tareas_picking SET pedido_clave = :c WHERE id = :i'),
                     {'c': clave, 'i': tid})
        res['picking'] += 1
    return res


def upgrade():
    # ── 1 · Clave del pedido ───────────────────────────────────────────────
    with op.batch_alter_table('tareas_picking') as batch:
        batch.add_column(sa.Column('pedido_clave', sa.String(40), nullable=True))
        batch.create_index('ix_tareas_picking_pedido_clave', ['pedido_clave'])
    with op.batch_alter_table('tareas_packing') as batch:
        batch.add_column(sa.Column('pedido_clave', sa.String(40), nullable=True))
        batch.create_index('ix_tareas_packing_pedido_clave', ['pedido_clave'])

    # ── 2 · Desenlace de los documentos Siesa en el recaudo ────────────────
    with op.batch_alter_table('recaudos_entrega') as batch:
        for doc in ('nc', 'rc'):
            batch.add_column(sa.Column(f'siesa_{doc}_resultado', sa.String(20), nullable=True))
            batch.add_column(sa.Column(f'siesa_{doc}_consec', sa.String(30), nullable=True))
            batch.add_column(sa.Column(f'siesa_{doc}_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('siesa_dc_resultado', sa.String(20), nullable=True))
        batch.add_column(sa.Column('siesa_dc_at', sa.DateTime(), nullable=True))
        batch.add_column(sa.Column('siesa_dc_detalle', sa.Text(), nullable=True))

    # ── 3 · Historia del pedido ────────────────────────────────────────────
    op.create_table(
        'pedidos_historia',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('linea_rowid', sa.BigInteger(), nullable=False),
        sa.Column('pedido_clave', sa.String(40)),
        sa.Column('co', sa.String(10)),
        sa.Column('tipo_docto', sa.String(10)),
        sa.Column('consec_docto', sa.Integer()),
        sa.Column('bodega', sa.String(10)),
        sa.Column('item_codigo', sa.String(60)),
        sa.Column('item_id_siesa', sa.String(50)),
        sa.Column('cliente_id', sa.String(40)),
        sa.Column('cliente', sa.String(200)),
        sa.Column('municipio', sa.String(100)),
        sa.Column('vendedor_id', sa.String(40)),
        sa.Column('cond_pago', sa.String(10)),
        sa.Column('fecha_pedido', sa.Date()),
        sa.Column('fecha_entrega', sa.Date()),
        sa.Column('cantidad_pedida_inicial', sa.Numeric(14, 4)),
        sa.Column('cantidad_pedida', sa.Numeric(14, 4)),
        sa.Column('cantidad_remisionada', sa.Numeric(14, 4)),
        sa.Column('cantidad_comprometida', sa.Numeric(14, 4)),
        sa.Column('vlr_neto', sa.Numeric(16, 2)),
        sa.Column('estado_siesa', sa.Integer()),
        sa.Column('primera_vez_vista_at', sa.DateTime(), nullable=False),
        sa.Column('primer_dia_visto', sa.Date(), nullable=False),
        sa.Column('ultima_vez_vista_at', sa.DateTime(), nullable=False),
        sa.Column('ultimo_dia_visto', sa.Date(), nullable=False),
        sa.Column('salida_at', sa.DateTime()),
        sa.Column('salida_dia', sa.Date()),
        sa.Column('motivo_salida', sa.String(20)),
        sa.Column('estado_siesa_salida', sa.Integer()),
        sa.Column('reapariciones', sa.Integer(), nullable=False, server_default='0'),
        sa.UniqueConstraint('linea_rowid', name='uq_pedidos_historia_linea_rowid'),
        sa.CheckConstraint(
            "motivo_salida IS NULL OR motivo_salida IN "
            "('CUMPLIDO','DESAPARECIDO','ANULADO','OTRO_ESTADO')",
            name='ck_pedidos_historia_motivo'),
    )
    op.create_index('ix_pedidos_historia_pedido_clave', 'pedidos_historia', ['pedido_clave'])
    op.create_index('ix_pedidos_historia_abiertas', 'pedidos_historia', ['salida_at'])

    # ── 4 · Fotos diarias de Siesa ─────────────────────────────────────────
    op.create_table(
        'fotos_siesa_corridas',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('run_id', sa.String(40), nullable=False),
        sa.Column('tipo', sa.String(10), nullable=False),
        sa.Column('alcance', sa.String(10), nullable=False),
        sa.Column('dia_operativo', sa.Date(), nullable=False),
        sa.Column('iniciada_at', sa.DateTime(), nullable=False),
        sa.Column('terminada_at', sa.DateTime()),
        sa.Column('completa', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('filas', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('paginas', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('motivo', sa.Text()),
        sa.Column('detalle', sa.Text()),
        sa.CheckConstraint("tipo IN ('VENTAS','STOCK','CARTERA')",
                           name='ck_fotos_corridas_tipo'),
        sa.CheckConstraint('completa = false OR motivo IS NULL',
                           name='ck_fotos_corridas_completa_sin_motivo'),
    )
    op.create_index('ix_fotos_siesa_corridas_run_id', 'fotos_siesa_corridas', ['run_id'])
    op.create_index('ix_fotos_corridas_alcance', 'fotos_siesa_corridas',
                    ['tipo', 'alcance', 'dia_operativo'])

    op.create_table(
        'foto_ventas_lineas',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('f470_rowid', sa.BigInteger(), nullable=False),
        sa.Column('run_id', sa.String(40), nullable=False),
        sa.Column('completa', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('dia_operativo', sa.Date(), nullable=False),
        sa.Column('co', sa.String(10), nullable=False),
        sa.Column('f350_rowid', sa.BigInteger()),
        sa.Column('tipo_docto', sa.String(10)),
        sa.Column('consec_docto', sa.Integer()),
        sa.Column('clase_docto', sa.Integer()),
        sa.Column('estado_docto', sa.Integer()),
        sa.Column('pedido_tipo', sa.String(10)),
        sa.Column('pedido_consec', sa.Integer()),
        sa.Column('pedido_co', sa.String(10)),
        sa.Column('pedido_clave', sa.String(40)),
        sa.Column('cliente_nit', sa.String(40)),
        sa.Column('cliente_razon_social', sa.String(200)),
        sa.Column('cliente_sucursal', sa.String(10)),
        sa.Column('vendedor_codigo', sa.String(20)),
        sa.Column('vendedor_id', sa.String(40)),
        sa.Column('cond_pago', sa.String(10)),
        sa.Column('bodega', sa.String(10)),
        sa.Column('item_id', sa.Integer()),
        sa.Column('referencia', sa.String(60)),
        sa.Column('concepto', sa.Integer()),
        sa.Column('motivo', sa.String(10)),
        sa.Column('causal_devolucion', sa.String(10)),
        sa.Column('naturaleza', sa.Integer()),
        sa.Column('unidad_negocio', sa.String(10)),
        sa.Column('cantidad', sa.Numeric(16, 4)),
        sa.Column('precio_uni', sa.Numeric(16, 4)),
        sa.Column('vlr_bruto', sa.Numeric(16, 2)),
        sa.Column('vlr_dscto', sa.Numeric(16, 2)),
        sa.Column('vlr_imp', sa.Numeric(16, 2)),
        sa.Column('vlr_neto', sa.Numeric(16, 2)),
        sa.Column('costo_prom_tot', sa.Numeric(16, 2)),
        sa.Column('capturada_at', sa.DateTime(), nullable=False),
        sa.Column('actualizada_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('f470_rowid', name='uq_foto_ventas_f470_rowid'),
    )
    op.create_index('ix_foto_ventas_lineas_run_id', 'foto_ventas_lineas', ['run_id'])
    op.create_index('ix_foto_ventas_lineas_pedido_clave', 'foto_ventas_lineas', ['pedido_clave'])
    op.create_index('ix_foto_ventas_co_dia', 'foto_ventas_lineas', ['co', 'dia_operativo'])
    op.create_index('ix_foto_ventas_documento', 'foto_ventas_lineas',
                    ['tipo_docto', 'consec_docto'])

    op.create_table(
        'foto_stock_diaria',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dia_operativo', sa.Date(), nullable=False),
        sa.Column('bodega', sa.String(20), nullable=False),
        sa.Column('codigo_siesa', sa.String(60), nullable=False),
        sa.Column('run_id', sa.String(40), nullable=False),
        sa.Column('completa', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('vendible', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('existencia', sa.Float()),
        sa.Column('comprometido', sa.Float()),
        sa.Column('salida_sin_conf', sa.Float()),
        sa.Column('stock_actualizado_at', sa.DateTime()),
        sa.Column('rezagada', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('costo_prom_uni', sa.Numeric(16, 4)),
        sa.Column('costo_prom_tot', sa.Numeric(18, 2)),
        sa.Column('capturada_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('dia_operativo', 'bodega', 'codigo_siesa',
                            name='uq_foto_stock_dia_bodega_sku'),
    )
    op.create_index('ix_foto_stock_diaria_run_id', 'foto_stock_diaria', ['run_id'])

    op.create_table(
        'foto_cartera_diaria',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dia_operativo', sa.Date(), nullable=False),
        sa.Column('f353_rowid', sa.BigInteger(), nullable=False),
        sa.Column('run_id', sa.String(40), nullable=False),
        sa.Column('completa', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('co', sa.String(10)),
        sa.Column('tipo_docto', sa.String(10)),
        sa.Column('consec_docto', sa.Integer()),
        sa.Column('nro_cuota', sa.Integer()),
        sa.Column('tercero_id', sa.String(40)),
        sa.Column('sucursal', sa.String(10)),
        sa.Column('cuenta', sa.String(20)),
        sa.Column('unidad_negocio', sa.String(10)),
        sa.Column('fecha_docto', sa.Date()),
        sa.Column('fecha_vcto', sa.Date()),
        sa.Column('total_db', sa.Numeric(18, 2)),
        sa.Column('total_cr', sa.Numeric(18, 2)),
        sa.Column('saldo', sa.Numeric(18, 2)),
        sa.Column('dias_vencido', sa.Integer()),
        sa.Column('capturada_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('dia_operativo', 'f353_rowid', name='uq_foto_cartera_dia_rowid'),
    )
    op.create_index('ix_foto_cartera_diaria_run_id', 'foto_cartera_diaria', ['run_id'])
    op.create_index('ix_foto_cartera_documento', 'foto_cartera_diaria',
                    ['tipo_docto', 'consec_docto'])

    # ── Backfill de la clave del pedido ────────────────────────────────────
    res = backfill_pedido_clave(op.get_bind())
    print(f'[m034fotos] pedido_clave rellenada: {res}')


def downgrade():
    for t in ('foto_cartera_diaria', 'foto_stock_diaria', 'foto_ventas_lineas',
              'fotos_siesa_corridas', 'pedidos_historia'):
        op.drop_table(t)
    with op.batch_alter_table('recaudos_entrega') as batch:
        for c in ('siesa_dc_detalle', 'siesa_dc_at', 'siesa_dc_resultado',
                  'siesa_rc_at', 'siesa_rc_consec', 'siesa_rc_resultado',
                  'siesa_nc_at', 'siesa_nc_consec', 'siesa_nc_resultado'):
            batch.drop_column(c)
    with op.batch_alter_table('tareas_packing') as batch:
        batch.drop_index('ix_tareas_packing_pedido_clave')
        batch.drop_column('pedido_clave')
    with op.batch_alter_table('tareas_picking') as batch:
        batch.drop_index('ix_tareas_picking_pedido_clave')
        batch.drop_column('pedido_clave')
