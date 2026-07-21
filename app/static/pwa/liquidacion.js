// ══════════════════════════════════════════════════════════════════════════════
// LIQUIDACIÓN FINANCIERA — Cierre logístico + financiero en un solo evento
// Dependencias globales (de app.js): get(), alerta(), API, TOKEN, repReintentar()
// ══════════════════════════════════════════════════════════════════════════════

let _liqSubActual = 'pendientes';
let _liqDashboard = null;
let _liqDetalleRuta = null;
let _liqRetenciones = {}; // { recaudoId: [{tipo, nombre, puc, tasa, base, valor}] }

const _liqFmt = v => '$' + Number(v || 0).toLocaleString('es-CO');

// ── Catálogo de retenciones (se sobreescribe con datos del backend) ──────────
let _liqRetencionesDisponibles = [
  {tipo:'RETEFUENTE_2.5', nombre:'Retefuente Compras 2.5%', puc:'13551501', tasa:0.025},
  {tipo:'RETEFUENTE_1.5', nombre:'Retefuente Bancos 1.5%', puc:'13551502', tasa:0.015},
  {tipo:'RETEIVA', nombre:'ReteIVA 15%', puc:'13551701', tasa:0.15},
  {tipo:'ICA_3', nombre:'ICA 3x1000', puc:'13551801', tasa:0.003},
  {tipo:'ICA_4.14', nombre:'ICA 4.14x1000', puc:'13551802', tasa:0.00414},
  {tipo:'ICA_6.9', nombre:'ICA 6.9x1000', puc:'13551803', tasa:0.0069},
  {tipo:'ICA_8', nombre:'ICA 8x1000', puc:'13551804', tasa:0.008},
  {tipo:'ICA_11.04', nombre:'ICA 11.04x1000', puc:'13551805', tasa:0.01104},
];

// ── Navegación interna ──────────────────────────────────────────────────────

/**
 * Switch the active liquidacion sub-tab and load its content.
 * @param {string} sec - Section key: 'pendientes', 'liquidadas', or 'jobs'.
 */
function liqSubtab(sec) {
  _liqSubActual = sec;
  ['pendientes','liquidadas','jobs'].forEach(s => {
    const cont = document.getElementById(`liq-sec-${s}`);
    const btn  = document.getElementById(`liq-sub-${s}`);
    if (!cont || !btn) return;
    const activo = s === sec;
    cont.style.display = activo ? 'block' : 'none';
    btn.style.background = activo ? 'var(--pm)' : 'transparent';
    btn.style.color = activo ? '#fff' : 'var(--tx3)';
    btn.style.fontWeight = activo ? '700' : '400';
  });
  if (sec === 'pendientes') liqCargarPendientes();
  else if (sec === 'liquidadas') liqCargarLiquidadas();
  else if (sec === 'jobs') liqCargarJobs();
}

/** Set today's date as default and load the liquidacion dashboard. */
async function cargarLiquidacion() {
  // Poner fecha de hoy por defecto si no hay fecha seleccionada
  const fechaInput = document.getElementById('liq-fecha');
  if (fechaInput && !fechaInput.value) {
    fechaInput.value = new Date().toISOString().split('T')[0];
  }
  await liqCargarDashboard();
}

// ── Dashboard + KPIs ────────────────────────────────────────────────────────

/** Fetch liquidacion dashboard data for the selected date and render KPIs. */
async function liqCargarDashboard() {
  const fecha = document.getElementById('liq-fecha')?.value || new Date().toISOString().split('T')[0];
  try {
    _liqDashboard = await get(`/api/rutas/liquidacion/dashboard?fecha=${fecha}`);
    _liqRenderKpis(_liqDashboard.resumen || {});
    if (_liqSubActual === 'pendientes') liqCargarPendientes();
    else if (_liqSubActual === 'liquidadas') liqCargarLiquidadas();
    else if (_liqSubActual === 'jobs') liqCargarJobs();
  } catch (e) {
    document.getElementById('liq-kpis').innerHTML = '';
    const el = document.getElementById(`liq-lista-${_liqSubActual}`);
    if (el) el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando datos de liquidación</div>';
  }
}

/**
 * Render the financial KPI cards (pendientes, liquidadas, totals by payment type).
 * @param {Object} r - Resumen object with counts and totals.
 */
function _liqRenderKpis(r) {
  const el = document.getElementById('liq-kpis');
  if (!el) return;
  const kpi = (label, val, color) => `
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
      <div style="font-size:11px;color:var(--tx3);margin-bottom:4px;">${label}</div>
      <div style="font-size:16px;font-weight:800;color:${color || 'var(--tx)'};">${val}</div>
    </div>`;
  el.innerHTML =
    kpi('Por liquidar', r.pendientes || 0, '#f59e0b') +
    kpi('Liquidadas', r.liquidadas || 0, '#22c55e') +
    kpi('Efectivo', _liqFmt(r.total_efectivo), '#4ade80') +
    kpi('Transferencia', _liqFmt(r.total_transferencia), '#60a5fa') +
    kpi('Crédito', _liqFmt(r.total_credito), '#a78bfa');
}

// ── Lista de rutas pendientes ───────────────────────────────────────────────

/** Filter and render ENTREGADA routes that are not yet financially settled. */
function liqCargarPendientes() {
  const el = document.getElementById('liq-lista-pendientes');
  if (!el || !_liqDashboard) return;
  const rutas = (_liqDashboard.rutas || []).filter(r =>
    r.estado_financiero !== 'LIQUIDADA' && r.estado === 'ENTREGADA'
  );
  if (!rutas.length) {
    el.innerHTML = `
      <div style="text-align:center;padding:40px;color:#555;">
        <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">✅</div>
        <div style="font-size:15px;font-weight:600;">Sin rutas por liquidar</div>
        <div style="font-size:12px;margin-top:6px;">Todas las rutas del día ya fueron liquidadas.</div>
      </div>`;
    return;
  }
  el.innerHTML = rutas.map(r => _liqRutaCard(r, false)).join('');
}

/** Filter and render routes that have already been financially settled. */
function liqCargarLiquidadas() {
  const el = document.getElementById('liq-lista-liquidadas');
  if (!el || !_liqDashboard) return;
  const rutas = (_liqDashboard.rutas || []).filter(r => r.estado_financiero === 'LIQUIDADA');
  if (!rutas.length) {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#555;">Sin rutas liquidadas para esta fecha</div>';
    return;
  }
  el.innerHTML = rutas.map(r => _liqRutaCard(r, true)).join('');
}

/**
 * Build the HTML card for a ruta in the liquidacion list.
 * @param {Object} r - Ruta object with financials and Siesa flags.
 * @param {boolean} esLiquidada - Whether the route is already settled.
 * @returns {string} HTML string for the ruta card.
 */
function _liqRutaCard(r, esLiquidada) {
  const color = esLiquidada ? '#14532d' : '#78350f';
  const ncInfo = `NCE ${r.siesa_nc_enviados || 0}`;
  const rcInfo = `RC ${r.siesa_rc_enviados || 0}`;
  const dcInfo = `DC ${r.siesa_dc_enviados || 0}`;
  const jobsFallidos = r.jobs_fallidos || 0;

  return `
    <div class="tabla-card" style="margin-bottom:10px;border-left:3px solid ${color};cursor:pointer;"
         onclick="liqAbrirRuta(${r.id})">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;">
        <div>
          <div style="font-size:14px;font-weight:700;color:var(--tx);">Ruta #${r.id}</div>
          <div style="font-size:12px;color:var(--tx3);margin-top:2px;">
            ${r.conductor_nombre} · ${r.vehiculo_placa || '—'}
          </div>
          <div style="font-size:11px;color:var(--tx3);">
            ${r.ruta_maestra_nombre || r.tipo_ruta} · ${r.total_paradas || 0} paradas
          </div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:14px;font-weight:700;color:${esLiquidada ? '#4ade80' : '#fbbf24'};">
            ${_liqFmt(r.total_recaudado)}
          </div>
          <div style="font-size:11px;color:var(--tx3);margin-top:2px;">
            ${r.paradas_entregadas || 0} ok · ${r.paradas_parciales || 0} parcial · ${r.paradas_rechazadas || 0} rech.
          </div>
        </div>
      </div>
      ${esLiquidada ? `
        <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;">
          <span style="font-size:11px;padding:3px 8px;border-radius:20px;background:#1e3a5f22;color:#60a5fa;">${ncInfo}</span>
          <span style="font-size:11px;padding:3px 8px;border-radius:20px;background:#14532d22;color:#4ade80;">${rcInfo}</span>
          <span style="font-size:11px;padding:3px 8px;border-radius:20px;background:#3b076622;color:#c084fc;">${dcInfo}</span>
          ${jobsFallidos > 0 ? `<span style="font-size:11px;padding:3px 8px;border-radius:20px;background:#7f1d1d22;color:#f87171;font-weight:700;">${jobsFallidos} fallido(s)</span>` : ''}
        </div>` : `
        <div style="margin-top:8px;">
          <span style="font-size:12px;font-weight:700;color:#f59e0b;">Abrir para liquidar →</span>
        </div>`}
    </div>`;
}

// ── Jobs Siesa de liquidación ───────────────────────────────────────────────

/** Fetch and render Siesa DLQ jobs related to liquidacion (NCE, RC, DC). */
async function liqCargarJobs() {
  const el = document.getElementById('liq-lista-jobs');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';

  const filtro = document.getElementById('liq-filtro-job')?.value || 'FALLIDO';
  try {
    const d = await get(`/api/reposicion/siesa-jobs?estado=${filtro}&tipos=NOTA_CREDITO_FACTURA,RECIBO_CAJA,DOCUMENTO_CONTABLE_RET`);
    const jobs = d.jobs || [];
    if (!jobs.length) {
      el.innerHTML = `
        <div style="text-align:center;padding:40px;color:#555;">
          <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">${filtro === 'FALLIDO' ? '✅' : '📋'}</div>
          <div style="font-size:15px;font-weight:600;">Sin jobs ${filtro.toLowerCase()}s</div>
        </div>`;
      return;
    }
    el.innerHTML = jobs.map(j => _liqJobCard(j, filtro === 'FALLIDO')).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando jobs</div>';
  }
}

/**
 * Build the HTML card for a liquidacion-related Siesa job.
 * @param {Object} j - Job object with estado, intentos, error_ultimo, etc.
 * @param {boolean} mostrarReintentar - Whether to show the retry button.
 * @returns {string} HTML string for the job card.
 */
function _liqJobCard(j, mostrarReintentar) {
  const estadoColor = { PENDIENTE: '#f59e0b', COMPLETADO: '#22c55e', FALLIDO: '#ef4444' };
  const color = estadoColor[j.estado] || '#888';
  const fecha = j.fecha_creacion ? new Date(j.fecha_creacion).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';

  return `
    <div class="tabla-card" style="margin-bottom:10px;${j.estado === 'FALLIDO' ? 'border-left:3px solid #ef4444;' : ''}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:12px;font-weight:700;color:var(--tx);">${j.tipo || '—'}</div>
          <div style="font-size:11px;color:var(--tx3);margin-top:2px;">${fecha}</div>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:11px;color:#666;">${j.intentos || 0}/${j.max_intentos || 3}</span>
          <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${j.estado}</span>
        </div>
      </div>
      ${j.error_ultimo ? `
        <div style="font-size:11px;color:#f87171;background:#7f1d1d22;padding:6px 8px;border-radius:6px;margin-bottom:8px;font-family:monospace;word-break:break-all;">
          ${j.error_ultimo.slice(0, 200)}
        </div>` : ''}
      ${j.referencia_tipo ? `
        <div style="font-size:11px;color:var(--tx3);">Ref: ${j.referencia_tipo} #${j.referencia_id || '—'}</div>` : ''}
      ${mostrarReintentar ? `
        <div style="display:flex;justify-content:flex-end;margin-top:8px;">
          <button onclick="repReintentar(${j.id})"
            style="padding:6px 14px;background:var(--pm);border:none;border-radius:6px;color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
            Reintentar
          </button>
        </div>` : ''}
    </div>`;
}

// ── Modal: detalle de ruta para liquidar ─────────────────────────────────────

/**
 * Open the route detail modal and fetch Siesa invoice data for liquidation.
 * @param {number} id - Ruta ID.
 */
async function liqAbrirRuta(id) {
  const modal = document.getElementById('liq-modal-ruta');
  const body  = document.getElementById('liq-modal-body');
  if (!modal || !body) return;
  modal.style.display = 'block';
  body.innerHTML = '<div style="text-align:center;padding:40px;color:#555;">Cargando datos de Siesa...</div>';
  _liqRetenciones = {};

  try {
    _liqDetalleRuta = await get(`/api/rutas/${id}/liquidacion-detalle`);
    if (_liqDetalleRuta.retenciones_disponibles) {
      _liqRetencionesDisponibles = _liqDetalleRuta.retenciones_disponibles;
    }
    _liqRenderDetalle();
  } catch (e) {
    body.innerHTML = '<div style="text-align:center;padding:40px;color:#ef4444;">Error cargando datos de la ruta</div>';
  }
}

/** Close the route detail modal and clear temporary state. */
function liqCerrarModal() {
  const modal = document.getElementById('liq-modal-ruta');
  if (modal) modal.style.display = 'none';
  _liqDetalleRuta = null;
  _liqRetenciones = {};
}

/** Render the full route detail view with recaudos, retenciones, and action button. */
function _liqRenderDetalle() {
  const body = document.getElementById('liq-modal-body');
  if (!body || !_liqDetalleRuta) return;

  const ruta = _liqDetalleRuta.ruta;
  const recaudos = _liqDetalleRuta.recaudos || [];
  const esLiquidada = ruta.estado_financiero === 'LIQUIDADA';

  let html = `
    <div style="margin-bottom:16px;">
      <div style="font-size:14px;font-weight:700;">Ruta #${ruta.id} · ${ruta.conductor_nombre}</div>
      <div style="font-size:12px;color:var(--tx3);">${ruta.vehiculo_placa || '—'} · ${ruta.ruta_maestra_nombre || ruta.tipo_ruta}</div>
      ${esLiquidada ? '<div style="font-size:12px;color:#4ade80;font-weight:700;margin-top:4px;">LIQUIDADA</div>' : ''}
    </div>`;

  let totalFacturas = 0;
  let totalRetenciones = 0;

  recaudos.forEach((rec, idx) => {
    const estado = rec.estado_entrega;
    const colorEstado = estado === 'ENTREGADO' ? '#4ade80' : estado === 'PARCIAL' ? '#fbbf24' : '#f87171';
    const fp = rec.forma_pago || '—';
    const monto = rec.monto_cobrado || 0;
    const esCred = (fp === 'CREDITO');
    const factura = rec.factura_siesa;
    const baseGravable = factura ? factura.base_gravable : 0;

    totalFacturas += monto;

    // Inicializar retenciones para este recaudo si no existen
    if (!_liqRetenciones[rec.id]) _liqRetenciones[rec.id] = [];
    const rets = _liqRetenciones[rec.id];
    const totalRet = rets.reduce((s, r) => s + (r.valor || 0), 0);
    totalRetenciones += totalRet;

    html += `
      <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
          <div>
            <div style="font-size:13px;font-weight:700;color:var(--tx);">${rec.numero_pedido || '—'}</div>
            <div style="font-size:11px;color:var(--tx3);">${rec.cliente || '—'}</div>
          </div>
          <div style="text-align:right;">
            <span style="font-size:12px;font-weight:700;color:${colorEstado};">${estado}</span>
            <div style="font-size:12px;color:var(--tx2);">${_liqFmt(monto)} · ${fp}</div>
          </div>
        </div>`;

    // Siesa flags si ya liquidada
    if (esLiquidada) {
      html += `
        <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;">
          ${rec.siesa_nc_triggered ? '<span style="font-size:11px;padding:2px 8px;border-radius:20px;background:#1e3a5f33;color:#60a5fa;">NCE ✓</span>' : ''}
          ${rec.siesa_rc_triggered ? '<span style="font-size:11px;padding:2px 8px;border-radius:20px;background:#14532d33;color:#4ade80;">RC ✓</span>' : ''}
          ${rec.siesa_dc_triggered ? '<span style="font-size:11px;padding:2px 8px;border-radius:20px;background:#3b076633;color:#c084fc;">DC ✓</span>' : ''}
        </div>`;
    }

    // Detalle de devoluciones para PARCIAL / RECHAZADO
    if (estado === 'PARCIAL' && rec.items_entregados && rec.items_entregados.length) {
      html += `
        <div style="margin-bottom:8px;">
          <div style="font-size:10px;color:#fbbf24;font-weight:700;margin-bottom:4px;">DETALLE PARCIAL — verificar cantidades devueltas</div>
          <div style="display:grid;grid-template-columns:1fr auto auto ${esLiquidada ? '' : 'auto'};gap:4px 8px;font-size:11px;">
            <div style="color:var(--tx3);font-weight:700;">Referencia</div>
            <div style="color:var(--tx3);font-weight:700;text-align:right;">Pedido</div>
            <div style="color:var(--tx3);font-weight:700;text-align:right;">Entregado</div>
            ${esLiquidada ? '' : '<div style="color:var(--tx3);font-weight:700;text-align:right;">Devuelto</div>'}
            ${rec.items_entregados.map((it, itIdx) => `
              <div style="color:var(--tx2);">${it.nombre || it.codigo}</div>
              <div style="color:var(--tx3);text-align:right;">${it.cantidad_pedida}</div>
              <div style="color:#4ade80;text-align:right;">${it.cantidad_entregada}</div>
              ${esLiquidada ? '' : `
                <div style="text-align:right;">
                  <input type="number" id="liq-dev-${rec.id}-${itIdx}" value="${it.cantidad_devuelta || 0}"
                    min="0" max="${it.cantidad_pedida}" step="1"
                    style="width:50px;padding:2px 4px;background:var(--bg);border:1px solid var(--brd);border-radius:4px;color:#f87171;font-size:12px;text-align:right;font-weight:700;">
                </div>`}
            `).join('')}
          </div>
        </div>`;
    }

    if (estado === 'RECHAZADO') {
      html += `
        <div style="margin-bottom:8px;">
          <div style="font-size:10px;color:#f87171;font-weight:700;margin-bottom:4px;">RECHAZO TOTAL</div>
          <div style="font-size:12px;color:#fca5a5;">${rec.observaciones || 'Sin motivo registrado'}</div>
          ${factura ? `<div style="font-size:11px;color:var(--tx3);margin-top:4px;">NCE por: ${_liqFmt(factura.total_neto)} (total factura)</div>` : ''}
        </div>`;
    }

    // Base gravable de Siesa
    if (factura && baseGravable > 0) {
      html += `
        <div style="font-size:11px;color:var(--tx3);margin-bottom:8px;padding:6px 8px;background:var(--bg);border-radius:6px;">
          Base gravable (Siesa): ${_liqFmt(baseGravable)} · IVA: ${_liqFmt(factura.total_iva)} · Neto: ${_liqFmt(factura.total_neto)}
        </div>`;
    } else if (!factura && !esLiquidada) {
      html += `
        <div style="font-size:11px;color:#f59e0b;margin-bottom:8px;padding:6px 8px;background:#78350f22;border-radius:6px;">
          No se pudo obtener datos de la factura en Siesa. Las retenciones se calcularán al procesar.
        </div>`;
    }

    // Selector de retenciones (solo si CONTADO y no liquidada)
    if (!esLiquidada && !esCred && estado !== 'RECHAZADO') {
      html += `
        <div style="margin-top:8px;border-top:1px solid var(--brd);padding-top:8px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div style="font-size:11px;font-weight:700;color:var(--tx2);">Retenciones tributarias</div>
            <select onchange="liqAgregarRetencion(${rec.id}, this.value, ${baseGravable}); this.value='';"
              style="padding:4px 8px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:11px;">
              <option value="">+ Agregar retención...</option>
              ${_liqRetencionesDisponibles.map(rt =>
                `<option value="${rt.tipo}">${rt.nombre} (${rt.puc})</option>`
              ).join('')}
            </select>
          </div>
          <div id="liq-rets-${rec.id}">
            ${_liqRenderRetenciones(rec.id, baseGravable)}
          </div>
        </div>`;
    }

    html += '</div>';
  });

  // Resumen y botón de acción
  const cashRecibir = totalFacturas - totalRetenciones;
  if (!esLiquidada) {
    html += `
      <div style="background:var(--bg-s);border:2px solid var(--pm);border-radius:12px;padding:16px;margin-top:16px;">
        <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;">
          <span style="color:var(--tx3);">Total facturas:</span>
          <span style="color:var(--tx);font-weight:700;">${_liqFmt(totalFacturas)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;">
          <span style="color:#c084fc;">Retenciones:</span>
          <span style="color:#c084fc;font-weight:700;">-${_liqFmt(totalRetenciones)}</span>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:16px;font-weight:800;margin-top:8px;padding-top:8px;border-top:1px solid var(--brd);">
          <span style="color:var(--tx);">Cash a recibir:</span>
          <span style="color:#4ade80;">${_liqFmt(cashRecibir)}</span>
        </div>
        <button onclick="liqLiquidarRuta(${ruta.id})"
          style="width:100%;margin-top:14px;padding:16px;background:#14532d;color:#4ade80;border:none;border-radius:10px;font-size:15px;font-weight:800;cursor:pointer;">
          💰 LIQUIDAR RUTA EN SIESA
        </button>
      </div>`;
  }

  body.innerHTML = html;
}

/**
 * Render the retenciones grid for a specific recaudo.
 * @param {number} recaudoId - Recaudo ID to look up in _liqRetenciones.
 * @param {number} baseGravable - Taxable base amount from Siesa invoice.
 * @returns {string} HTML string for the retenciones table.
 */
function _liqRenderRetenciones(recaudoId, baseGravable) {
  const rets = _liqRetenciones[recaudoId] || [];
  if (!rets.length) return '<div style="font-size:11px;color:var(--tx3);padding:4px 0;">Sin retenciones</div>';

  let html = `
    <div style="display:grid;grid-template-columns:1fr auto auto auto auto;gap:2px 8px;font-size:11px;align-items:center;">
      <div style="color:var(--tx3);font-weight:700;">Retención</div>
      <div style="color:var(--tx3);font-weight:700;text-align:right;">Base</div>
      <div style="color:var(--tx3);font-weight:700;text-align:right;">Tasa</div>
      <div style="color:var(--tx3);font-weight:700;text-align:right;">Valor</div>
      <div></div>`;

  rets.forEach((r, i) => {
    html += `
      <div style="color:var(--tx2);">${r.nombre} <span style="color:var(--tx3);">${r.puc}</span></div>
      <div style="color:var(--tx3);text-align:right;">${_liqFmt(r.base)}</div>
      <div style="color:var(--tx3);text-align:right;">${r.tasa < 0.01 ? (r.tasa * 1000).toFixed(2) + '‰' : (r.tasa * 100).toFixed(1) + '%'}</div>
      <div style="color:#c084fc;text-align:right;font-weight:700;">${_liqFmt(r.valor)}</div>
      <div style="text-align:center;"><span onclick="liqQuitarRetencion(${recaudoId},${i},${baseGravable})" style="color:#ef4444;cursor:pointer;font-size:14px;">✕</span></div>`;
  });
  html += '</div>';

  const totalRet = rets.reduce((s, r) => s + r.valor, 0);
  html += `<div style="font-size:11px;color:#c084fc;text-align:right;margin-top:4px;font-weight:700;">Total retenciones: -${_liqFmt(totalRet)}</div>`;
  return html;
}

/**
 * Add a tax retention to a recaudo and re-render the detail view.
 * @param {number} recaudoId - Recaudo ID.
 * @param {string} tipo - Retention type key from the catalog.
 * @param {number} baseGravable - Taxable base amount for calculation.
 */
function liqAgregarRetencion(recaudoId, tipo, baseGravable) {
  if (!tipo) return;
  const catalogo = _liqRetencionesDisponibles.find(r => r.tipo === tipo);
  if (!catalogo) return;

  if (!_liqRetenciones[recaudoId]) _liqRetenciones[recaudoId] = [];
  // Evitar duplicados
  if (_liqRetenciones[recaudoId].find(r => r.tipo === tipo)) {
    alerta('Esta retención ya fue agregada', 'error');
    return;
  }

  const base = baseGravable || 0;
  const valor = Math.round(base * catalogo.tasa);
  _liqRetenciones[recaudoId].push({
    tipo: catalogo.tipo,
    nombre: catalogo.nombre,
    puc: catalogo.puc,
    tasa: catalogo.tasa,
    base: base,
    valor: valor,
  });

  // Re-render retenciones de este recaudo
  const container = document.getElementById(`liq-rets-${recaudoId}`);
  if (container) container.innerHTML = _liqRenderRetenciones(recaudoId, baseGravable);
  // Re-render resumen
  _liqRenderDetalle();
}

/**
 * Remove a retention by index from a recaudo and re-render.
 * @param {number} recaudoId - Recaudo ID.
 * @param {number} idx - Index of the retention to remove.
 * @param {number} baseGravable - Taxable base amount (unused, kept for onclick signature).
 */
function liqQuitarRetencion(recaudoId, idx, baseGravable) {
  if (_liqRetenciones[recaudoId]) {
    _liqRetenciones[recaudoId].splice(idx, 1);
  }
  _liqRenderDetalle();
}

// ── Liquidar ruta completa ──────────────────────────────────────────────────

/**
 * Execute full financial liquidation for a route, enqueuing NCE, RC, and DC to Siesa.
 * @param {number} rutaId - Ruta ID to liquidate.
 */
async function liqLiquidarRuta(rutaId) {
  if (!_liqDetalleRuta) return;
  const recaudos = _liqDetalleRuta.recaudos || [];

  // Construir payload con cantidades verificadas y retenciones
  const payload = { recaudos: [] };

  recaudos.forEach((rec, idx) => {
    const entry = { recaudo_id: rec.id, cantidades_verificadas: [], retenciones: [] };

    // Recoger cantidades devueltas verificadas por el Líder (solo para PARCIAL)
    if (rec.estado_entrega === 'PARCIAL' && rec.items_entregados) {
      rec.items_entregados.forEach((it, itIdx) => {
        const input = document.getElementById(`liq-dev-${rec.id}-${itIdx}`);
        const cantDev = input ? parseInt(input.value || 0) : (it.cantidad_devuelta || 0);
        entry.cantidades_verificadas.push({
          codigo: it.codigo,
          cantidad_devuelta: cantDev,
        });
      });
    }

    // Recoger retenciones seleccionadas
    const rets = _liqRetenciones[rec.id] || [];
    rets.forEach(r => {
      entry.retenciones.push({ tipo: r.tipo });
    });

    payload.recaudos.push(entry);
  });

  if (!confirm(`¿Liquidar Ruta #${rutaId} en Siesa?\nEsto disparará NCE, RC y DC automáticamente.`)) return;

  try {
    const r = await fetch(API + `/api/rutas/${rutaId}/liquidar-completo`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      const s = d.siesa || {};
      const partes = [];
      if (s.nc_encolados) partes.push(s.nc_encolados + ' NCE');
      if (s.rc_encolados) partes.push(s.rc_encolados + ' RC');
      if (s.dc_encolados || d.retenciones_encoladas) partes.push((s.dc_encolados || 0) + (d.retenciones_encoladas || 0) + ' DC');
      if (s.credito_omitidos) partes.push(s.credito_omitidos + ' a cartera');
      alerta(partes.length ? 'Siesa: ' + partes.join(', ') + ' encolados' : 'Ruta liquidada', 'exito');
      if (d.errores && d.errores.length) alerta(d.errores.length + ' error(es) al encolar', 'error');
      liqCerrarModal();
      await liqCargarDashboard();
    } else {
      alerta(d.error || 'Error al liquidar', 'error');
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}
