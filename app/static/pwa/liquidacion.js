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
// Fallback únicamente — la fuente real es CATALOGO_RETENCIONES en
// liquidacion_service.py (retenciones_disponibles en la respuesta del
// backend). Mantener sincronizado a mano solo como default antes de que
// llegue esa respuesta; ver ese archivo para la fecha de verificación.
let _liqRetencionesDisponibles = [
  {tipo:'RETEFUENTE_2.5', nombre:'Retención por Compras 2.5%', puc:'13551501', tasa:0.025},
  {tipo:'RETEFUENTE_1.5', nombre:'Retención Bancos 1.5%', puc:'13551502', tasa:0.015},
  {tipo:'RETEIVA', nombre:'ReteIVA Ventas 15%', puc:'13551701', tasa:0.15},
  {tipo:'ICA_4X1000', nombre:'ICA Retenido a Favor 4x1000', puc:'13551801', tasa:0.004},
  {tipo:'ICA_3X1000', nombre:'ICA Retenido a Favor 3x1000', puc:'13551802', tasa:0.003},
  {tipo:'ICA_6X1000', nombre:'ICA Retenido a Favor 6x1000', puc:'13551803', tasa:0.006},
  {tipo:'ICA_10X1000', nombre:'ICA Retenido a Favor 10x1000', puc:'13551804', tasa:0.010},
  {tipo:'ICA_11X1000', nombre:'ICA Retenido a Favor 11x1000', puc:'13551805', tasa:0.011},
  {tipo:'AUTORRETENCION_ICA_NEIVA_3X1000', nombre:'Autorretención ICA Neiva 3x1000', puc:'13559501', tasa:0.003},
  {tipo:'AUTORRETENCION_ICA_NEIVA_3.5X1000', nombre:'Autorretención ICA Neiva 3.5x1000', puc:'13559502', tasa:0.0035},
  {tipo:'AUTORRETENCION_ICA_NEIVA_4.5X1000', nombre:'Autorretención ICA Neiva 4.5x1000', puc:'13559503', tasa:0.0045},
  {tipo:'AUTORRETENCION_ICA_NEIVA_8X1000', nombre:'Autorretención ICA Neiva 8x1000', puc:'13559504', tasa:0.008},
  {tipo:'AUTORRETENCION_ICA_PITALITO_4X1000', nombre:'Autorretención ICA Pitalito 4x1000', puc:'13559505', tasa:0.004},
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

/** Set today's date range as default and load the liquidacion dashboard. */
async function cargarLiquidacion() {
  const hoy = new Date().toISOString().split('T')[0];
  const desde = document.getElementById('liq-fecha-desde');
  const hasta = document.getElementById('liq-fecha-hasta');
  if (desde && !desde.value) desde.value = hoy;
  if (hasta && !hasta.value) hasta.value = hoy;
  await liqCargarDashboard();
}

// ── Dashboard + KPIs ────────────────────────────────────────────────────────

/** Fetch liquidacion dashboard data for the selected date range and render KPIs. */
/**
 * Los tres números que decidían el rediseño de facturación y que se estuvieron
 * estimando toda la semana: cuánto del flujo es contado, con qué frecuencia hay
 * PARCIAL o RECHAZADO, y cuánto se tarda alguien en liquidar.
 *
 * El rezago se muestra con su advertencia pegada. Hoy liquidar no tiene
 * consecuencia fiscal; si la factura pasa a emitirse ahí, la tendría. Un número
 * medido sin consecuencias no predice el mismo proceso con consecuencias — es
 * un piso, y mostrarlo sin decirlo invita a planear con él.
 */
async function liqCargarDesglose() {
  const el = document.getElementById('liq-desglose');
  if (!el) return;
  el.innerHTML = '<span style="color:var(--tx3);font-size:12px;">Contando…</span>';
  let d;
  try {
    d = await get('/api/rutas/liquidacion/desglose');
  } catch (e) {
    el.innerHTML = `<span style="color:var(--red);font-size:12px;">No se pudo consultar: ${e.message}</span>`;
    return;
  }
  const r = d.recaudos || {};
  const z = d.rezago_liquidacion || {};
  const c = d.condicion_pago_ausente || {};
  if (!r.total) {
    el.innerHTML = '<span style="color:var(--tx3);font-size:12px;">Todavía no hay entregas registradas.</span>';
    return;
  }
  const filas = Object.entries(r.matriz || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<div class="tabla-fila"><span class="tabla-nombre">${k}</span>
        <span>${n} · ${Math.round(100 * n / r.total)}%</span></div>`).join('');
  el.innerHTML = `
    <div class="tabla-card">
      <div class="tabla-titulo">Entregas por forma de pago y estado (${r.total})</div>
      ${filas}
      <p style="font-size:11px;color:var(--tx3);margin:8px 0 0;">
        <b>${r.parcial_o_rechazado}</b> parciales o rechazadas
        (${r.pct_parcial_o_rechazado}%) — es la frecuencia con que haría falta
        devolver mercancía al camión.
      </p>
    </div>
    <div class="tabla-card" style="margin-top:12px;">
      <div class="tabla-titulo">Rutas entregadas sin liquidar</div>
      <div class="tabla-fila"><span class="tabla-nombre">Cuántas</span>
        <span class="badge ${z.rutas_entregadas_sin_liquidar ? 'badge-yellow' : 'badge-green'}">${z.rutas_entregadas_sin_liquidar}</span></div>
      <div class="tabla-fila"><span class="tabla-nombre">Días máximo</span><span>${z.dias_max ?? '—'}</span></div>
      <div class="tabla-fila"><span class="tabla-nombre">Días promedio</span><span>${z.dias_promedio ?? '—'}</span></div>
      <p style="font-size:11px;color:var(--tx3);margin:8px 0 0;">${z.nota || ''}</p>
    </div>
    <div class="tabla-card" style="margin-top:12px;">
      <div class="tabla-titulo">Facturas emitidas como contado por dato faltante</div>
      <div class="tabla-fila"><span class="tabla-nombre">Veces</span>
        <span class="badge ${c.alertas ? 'badge-red' : 'badge-green'}">${c.alertas}</span></div>
      <p style="font-size:11px;color:var(--tx3);margin:8px 0 0;">${c.nota || ''}</p>
    </div>`;
}

async function liqCargarDashboard() {
  const hoy = new Date().toISOString().split('T')[0];
  const desde = document.getElementById('liq-fecha-desde')?.value || hoy;
  const hasta = document.getElementById('liq-fecha-hasta')?.value || hoy;
  try {
    _liqDashboard = await get(`/api/rutas/liquidacion/dashboard?fecha_desde=${desde}&fecha_hasta=${hasta}`);
    _liqRenderKpis(_liqDashboard.resumen || {});
    liqCargarDesglose();
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
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#555;">Sin rutas liquidadas para este rango de fechas</div>';
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

    // Siesa flags + badges fallidos si ya liquidada
    if (esLiquidada) {
      const retDet = rec.retenciones_detalle || [];
      html += `<div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;">`;
      if (rec.siesa_nc_triggered) html += '<span style="font-size:11px;padding:2px 8px;border-radius:20px;background:#1e3a5f33;color:#60a5fa;">NC ✓</span>';
      if (rec.siesa_rc_triggered) html += '<span style="font-size:11px;padding:2px 8px;border-radius:20px;background:#14532d33;color:#4ade80;">RC ✓</span>';
      retDet.forEach(rd => {
        if (rd.siesa_triggered) html += `<span style="font-size:11px;padding:2px 8px;border-radius:20px;background:#3b076633;color:#c084fc;">DC ${rd.tipo.replace('RETEFUENTE_','RF').replace('RETEIVA','RIVA').replace('ICA_','ICA')} ✓</span>`;
      });
      // Fallback: si siesa_dc_triggered pero no hay retenciones_detalle (flujo viejo)
      if (rec.siesa_dc_triggered && !retDet.length) html += '<span style="font-size:11px;padding:2px 8px;border-radius:20px;background:#3b076633;color:#c084fc;">DC ✓</span>';
      html += `</div>`;
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

    // La mercancia se entrego y el cliente no pago. NO es un rechazo: no hay
    // nota credito (nada volvio) ni recibo de caja (no entro plata). La
    // factura queda abierta en cartera y quien liquida decide si escala.
    if (estado === 'ENTREGADO_SIN_PAGO') {
      html += `
        <div style="margin-bottom:8px;padding:8px;background:#3a1616;border-radius:8px;border-left:3px solid #f87171;">
          <div style="font-size:10px;color:#f87171;font-weight:700;margin-bottom:4px;">ENTREGADO SIN PAGO</div>
          <div style="font-size:12px;color:#fca5a5;">${rec.observaciones || 'Sin detalle registrado'}</div>
          <div style="font-size:11px;color:var(--tx3);margin-top:6px;">
            La mercancía quedó con el cliente y no se cobró. No genera nota
            crédito ni recibo de caja — la factura queda abierta en cartera.
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

    // ── FASE 2: Acciones per-parada (solo si LIQUIDADA) ──────────
    if (esLiquidada) {
      // Botones de acción por tipo de parada
      if ((estado === 'RECHAZADO' || estado === 'PARCIAL') && !rec.siesa_nc_triggered) {
        // Ya no se dispara la NC directo desde acá — Liquidar en WMS ya armó
        // la devolución pendiente; recepción la confirma en el módulo de
        // Devoluciones (verifica físicamente y ESO dispara la NC real, con
        // cruce automático de cartera).
        if (rec.devolucion_pendiente) {
          html += `
            <div style="text-align:center;padding:8px;color:#93c5fd;font-size:12px;font-weight:700;margin-top:8px;background:#0f2a3f;border-radius:8px;">
              🔵 Enviado a Devoluciones (${rec.devolucion_pendiente.codigo}) — pendiente de que recepción confirme
            </div>`;
        } else {
          html += `
            <div style="text-align:center;padding:8px;color:#fca5a5;font-size:12px;font-weight:700;margin-top:8px;background:#3a1616;border-radius:8px;">
              ⚠ No se pudo enviar a Devoluciones — revisar logs, contactar soporte
            </div>`;
        }
      }
      // `ENTREGADO_SIN_PAGO` va en la exclusion junto a RECHAZADO. Sin eso el
      // boton aparecia: su `forma_pago` es null, asi que `!esCred` daba true y
      // la pantalla ofrecia registrar un cobro que nunca ocurrio.
      if (!esCred && fp !== 'EXENTO' && estado !== 'RECHAZADO'
          && estado !== 'ENTREGADO_SIN_PAGO' && !rec.siesa_rc_triggered) {
        const rcDisabled = (estado === 'PARCIAL' && !rec.siesa_nc_triggered);
        html += `
          <button onclick="liqToggleCobro(${ruta.id}, ${rec.id})" ${rcDisabled ? 'disabled' : ''}
            style="width:100%;margin-top:8px;padding:12px;background:${rcDisabled ? '#333' : '#14532d'};color:${rcDisabled ? '#666' : '#4ade80'};border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">
            ${rcDisabled ? 'Registrar Cobro (espera NC)' : 'Registrar Cobro (RC)'}
          </button>
          <div id="liq-cobro-panel-${rec.id}" style="display:none;"></div>`;
      }
      if (esCred) {
        html += '<div style="text-align:center;padding:8px;color:#60a5fa;font-size:12px;font-weight:700;margin-top:8px;background:#1e3a5f22;border-radius:8px;">A Cartera — sin acción requerida</div>';
      }
      if (fp === 'EXENTO') {
        html += '<div style="text-align:center;padding:8px;color:var(--tx3);font-size:12px;margin-top:8px;">Exento</div>';
      }
    }

    html += '</div>';
  });

  // ── FASE 1: Botón de liquidación WMS (solo si NO liquidada) ────
  if (!esLiquidada) {
    html += `
      <div style="background:var(--bg-s);border:2px solid var(--pm);border-radius:12px;padding:16px;margin-top:16px;">
        <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px;">
          <span style="color:var(--tx3);">Total facturas:</span>
          <span style="color:var(--tx);font-weight:700;">${_liqFmt(totalFacturas)}</span>
        </div>
        <div style="font-size:11px;color:var(--tx3);margin-bottom:12px;">
          Paso 1: Confirma el cuadre financiero en WMS.<br>
          Paso 2: Después de liquidar, documenta NC/RC/DC por parada.
        </div>
        <button onclick="liqLiquidarWMS(${ruta.id})"
          style="width:100%;padding:16px;background:#14532d;color:#4ade80;border:none;border-radius:10px;font-size:15px;font-weight:800;cursor:pointer;">
          💰 LIQUIDAR EN WMS
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

// ── Fase 1: Liquidar WMS (solo estado financiero) ─────────────────────────

async function liqLiquidarWMS(rutaId) {
  if (!confirm(`¿Liquidar Ruta #${rutaId} en WMS?\nDespués podrás documentar NC/RC/DC por parada.`)) return;
  try {
    const r = await fetch(API + `/api/rutas/${rutaId}/liquidar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Ruta liquidada en WMS — ahora documenta NC/RC/DC por parada', 'exito');
      // Recargar detalle para mostrar Fase 2
      _liqDetalleRuta = await get(`/api/rutas/${rutaId}/liquidacion-detalle`);
      _liqRenderDetalle();
    } else {
      alerta(d.error || 'Error al liquidar', 'error');
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ── Fase 2: Acciones per-recaudo ──────────────────────────────────────────

async function liqToggleCobro(rutaId, recaudoId) {
  const panel = document.getElementById(`liq-cobro-panel-${recaudoId}`);
  if (!panel) return;
  if (panel.style.display === 'block') {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = 'block';
  panel.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Consultando datos de Siesa...</div>';

  try {
    const preview = await get(`/api/rutas/${rutaId}/recaudos/${recaudoId}/preview-siesa`);
    const df = preview.datos_factura || {};
    const mCobrado = preview.monto_cobrado || 0;
    const mSiesa = df.total_neto || 0;
    const difieren = mSiesa > 0 && Math.abs(mSiesa - mCobrado) > 1;

    if (!df.datos_disponibles) {
      panel.innerHTML = `<div style="padding:12px;color:#f59e0b;background:#78350f22;border-radius:8px;margin-top:8px;">
        No se pudo obtener datos de la factura en Siesa. Reintenta cuando Siesa esté disponible.
      </div>`;
      return;
    }

    let html = `<div style="background:var(--bg);border:1px solid var(--brd);border-radius:10px;padding:14px;margin-top:10px;">`;

    // Montos: conductor vs Siesa
    if (difieren) {
      html += `
        <div style="margin-bottom:10px;padding:8px;background:#78350f22;border-radius:6px;">
          <div style="font-size:11px;color:#fbbf24;font-weight:700;margin-bottom:4px;">⚠ Montos difieren</div>
          <div style="font-size:12px;color:var(--tx3);">Conductor: ${_liqFmt(mCobrado)} · Siesa: ${_liqFmt(mSiesa)}</div>
          <div style="margin-top:6px;display:flex;gap:6px;">
            <button onclick="document.getElementById('liq-monto-${recaudoId}').value=${mSiesa}" style="padding:4px 10px;background:var(--pm);color:#fff;border:none;border-radius:4px;font-size:11px;cursor:pointer;">Usar Siesa</button>
            <button onclick="document.getElementById('liq-monto-${recaudoId}').value=${mCobrado}" style="padding:4px 10px;background:var(--bg-s);color:var(--tx2);border:1px solid var(--brd);border-radius:4px;font-size:11px;cursor:pointer;">Usar conductor</button>
          </div>
          <input type="number" id="liq-monto-${recaudoId}" value="${mSiesa}" step="0.01"
            onchange="liqPreviewCobro(${recaudoId})"
            style="width:100%;margin-top:6px;padding:6px;background:var(--bg-s);border:1px solid var(--brd);border-radius:4px;color:var(--tx);font-size:12px;">
        </div>`;
    } else {
      html += `<input type="hidden" id="liq-monto-${recaudoId}" value="${mSiesa || mCobrado}">`;
      html += `<div style="font-size:12px;color:var(--tx2);margin-bottom:8px;">Monto: ${_liqFmt(mSiesa || mCobrado)} · CO: ${df.co_factura || '—'} · CxC: ${df.cuenta_cxc || 'fallback'}</div>`;
    }

    // Retenciones checkboxes — premarcada la que el conductor eligió en
    // campo (pantalla de última milla, Pago Parcial); el admin decide si la
    // mantiene, la cambia o la quita antes de confirmar el cobro.
    const motivoSugerido = preview.motivo_descuento_sugerido || '';
    html += `<div style="font-size:11px;font-weight:700;color:var(--tx2);margin-bottom:6px;">Retenciones:</div>`;
    if (motivoSugerido) {
      html += `<div style="font-size:11px;color:#c084fc;margin-bottom:6px;">Sugerido por el conductor en campo — verifica antes de confirmar.</div>`;
    }
    (preview.retenciones_disponibles || []).forEach(ret => {
      if (ret.monto_estimado <= 0) return;
      html += `
        <label style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;color:var(--tx2);cursor:pointer;">
          <input type="checkbox" class="liq-ret-check-${recaudoId}" value="${ret.tipo}" ${ret.tipo===motivoSugerido?'checked':''} onchange="liqPreviewCobro(${recaudoId})">
          ${ret.nombre} — ${_liqFmt(ret.monto_estimado)} <span style="color:var(--tx3);">(base ${_liqFmt(ret.base)})</span>
        </label>`;
    });

    // Preview dinámico
    html += `<div id="liq-preview-${recaudoId}" style="margin-top:10px;padding:8px;background:var(--bg-s);border-radius:6px;font-size:12px;"></div>`;

    if (!preview.siesa_horario_ok) {
      html += `<div style="color:#f59e0b;font-size:11px;margin-top:6px;">⚠ Siesa no opera después de 8PM — jobs quedarán en cola</div>`;
    }

    html += `
      <button onclick="liqRegistrarCobro(${rutaId}, ${recaudoId})" id="liq-btn-cobro-${recaudoId}"
        style="width:100%;margin-top:12px;padding:14px;background:#14532d;color:#4ade80;border:none;border-radius:8px;font-size:14px;font-weight:800;cursor:pointer;">
        Confirmar y Enviar RC
      </button>
    </div>`;

    panel.innerHTML = html;

    // Store preview data for later use
    panel.dataset.preview = JSON.stringify(preview);

    // Initial preview render
    liqPreviewCobro(recaudoId);
  } catch (e) {
    panel.innerHTML = `<div style="padding:12px;color:#ef4444;">${e.message || 'Error obteniendo preview'}</div>`;
  }
}

function liqPreviewCobro(recaudoId) {
  const montoInput = document.getElementById(`liq-monto-${recaudoId}`);
  const previewDiv = document.getElementById(`liq-preview-${recaudoId}`);
  if (!montoInput || !previewDiv) return;

  const monto = parseFloat(montoInput.value) || 0;
  const checks = document.querySelectorAll(`.liq-ret-check-${recaudoId}:checked`);
  const panel = document.getElementById(`liq-cobro-panel-${recaudoId}`);
  const preview = panel?.dataset.preview ? JSON.parse(panel.dataset.preview) : {};
  const retsDisp = preview.retenciones_disponibles || [];

  let totalRet = 0;
  let detalleHtml = '';
  checks.forEach(chk => {
    const ret = retsDisp.find(r => r.tipo === chk.value);
    if (ret) {
      totalRet += ret.monto_estimado;
      detalleHtml += `<div style="color:#c084fc;">• DC ${ret.nombre}: ${_liqFmt(ret.monto_estimado)} → PUC ${ret.puc}</div>`;
    }
  });

  const neto = monto - totalRet;
  previewDiv.innerHTML = `
    <div style="font-size:11px;font-weight:700;color:var(--tx3);margin-bottom:4px;">Se enviará a Siesa:</div>
    <div style="color:#4ade80;">• RC por ${_liqFmt(neto)} (neto: ${_liqFmt(monto)} - ${_liqFmt(totalRet)} retenciones)</div>
    ${detalleHtml}
    <div style="margin-top:6px;font-weight:700;color:var(--tx);">Total CxC cerrado: ${_liqFmt(monto)}</div>`;
}

async function liqRegistrarCobro(rutaId, recaudoId) {
  const montoInput = document.getElementById(`liq-monto-${recaudoId}`);
  const monto = montoInput ? parseFloat(montoInput.value) : null;
  const checks = document.querySelectorAll(`.liq-ret-check-${recaudoId}:checked`);
  const retenciones = [];
  checks.forEach(chk => retenciones.push({ tipo: chk.value }));

  if (!confirm(`¿Registrar cobro${retenciones.length ? ' con ' + retenciones.length + ' retención(es)' : ''}?`)) return;

  try {
    const r = await fetch(API + `/api/rutas/${rutaId}/recaudos/${recaudoId}/registrar-cobro`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ retenciones, monto_override: monto }),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      const partes = [`RC por ${_liqFmt(d.monto_neto_rc)}`];
      if (d.dc_jobs && d.dc_jobs.length) partes.push(`${d.dc_jobs.length} DC`);
      alerta(partes.join(' + ') + ' encolados', 'exito');
      _liqDetalleRuta = await get(`/api/rutas/${rutaId}/liquidacion-detalle`);
      _liqRenderDetalle();
    } else {
      alerta(d.error || 'Error al registrar cobro', 'error');
    }
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

async function liqReintentarJob(jobId) {
  try {
    const d = await get(`/api/siesa/jobs/${jobId}`);
    const error = d.error_ultimo || 'Sin detalle de error';
    const tipo = d.tipo || '—';
    const intentos = d.intentos || 0;
    if (!confirm(`Job ${tipo} #${jobId}\nIntentos: ${intentos}\nÚltimo error: ${error}\n\n¿Reintentar?`)) return;
    const r = await fetch(API + `/api/siesa/jobs/${jobId}/reintentar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const res = await r.json();
    if (r.ok) {
      alerta('Job reintentado', 'exito');
    } else {
      alerta(res.error || 'Error al reintentar', 'error');
    }
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}
