// ══════════════════════════════════════════════════════════════════
// INVENTARIO CÍCLICO — Admin tab
// Dependencias globales (de app.js): alerta(), API, TOKEN
// ══════════════════════════════════════════════════════════════════

let _INV_SUBTAB = 'conteos';
let _INV_ALMACENES = [];

/** Load almacenes for the ABC selector (once) and refresh the active inventory panel. */
async function cargarInventario() {
  // Cargar almacenes para el selector ABC (solo una vez)
  if (_INV_ALMACENES.length === 0) {
    try {
      const r = await fetch(API + '/api/almacenes/', { headers: { Authorization: 'Bearer ' + TOKEN } });
      if (r.ok) {
        _INV_ALMACENES = await r.json();
        const opts = _INV_ALMACENES.map(a =>
          `<option value="${a.id}">${a.nombre}${a.bodega_siesa_id ? ` (${a.bodega_siesa_id})` : ''}</option>`
        ).join('');
        const sel = document.getElementById('inv-abc-almacen');
        if (sel) sel.innerHTML = opts;
        const selM = document.getElementById('conteo-manual-almacen');
        if (selM) selM.innerHTML = opts;
        mostrarConfigBodega();
      }
    } catch (e) { /* silencioso */ }
  }
  if (_INV_SUBTAB === 'conteos') await cargarConteos();
  else await cargarResumenAbc();
}

// ── Config bodega por almacén ─────────────────────────────────────────────────

/** Display the Siesa bodega/CO info for the currently selected almacen. */
function mostrarConfigBodega() {
  const sel = document.getElementById('inv-abc-almacen');
  const info = document.getElementById('inv-abc-bodega-info');
  const label = document.getElementById('inv-abc-bodega-label');
  if (!sel || !info || !label) return;
  const alm = _INV_ALMACENES.find(a => a.id == sel.value);
  if (!alm) { info.style.display = 'none'; return; }
  const bod = alm.bodega_siesa_id || '—';
  const co = alm.centro_op_siesa || '—';
  label.innerHTML = `Bodega Siesa: <span style="color:#60a5fa;font-weight:700;">${bod}</span> · CO: <span style="color:#60a5fa;font-weight:700;">${co}</span>`;
  info.style.display = 'block';
  document.getElementById('inv-abc-bodega-edit').style.display = 'none';
}

/** Toggle the inline bodega/CO edit form visibility. */
function toggleEditBodega() {
  const edit = document.getElementById('inv-abc-bodega-edit');
  if (!edit) return;
  const visible = edit.style.display !== 'none';
  if (visible) { edit.style.display = 'none'; return; }
  const sel = document.getElementById('inv-abc-almacen');
  const alm = _INV_ALMACENES.find(a => a.id == sel.value);
  if (!alm) return;
  document.getElementById('inv-bodega-input').value = alm.bodega_siesa_id || '';
  document.getElementById('inv-centro-op-input').value = alm.centro_op_siesa || '';
  edit.style.display = 'block';
}

/** Save the bodega_siesa_id and centro_op_siesa for the selected almacen. */
async function guardarConfigBodega() {
  const sel = document.getElementById('inv-abc-almacen');
  const almId = sel?.value;
  if (!almId) return;
  const bodega = document.getElementById('inv-bodega-input').value.trim().toUpperCase();
  const centroOp = document.getElementById('inv-centro-op-input').value.trim();
  try {
    const r = await fetch(API + `/api/almacenes/${almId}`, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ bodega_siesa_id: bodega || null, centro_op_siesa: centroOp || null })
    });
    if (r.ok) {
      const updated = await r.json();
      const idx = _INV_ALMACENES.findIndex(a => a.id == almId);
      if (idx >= 0) _INV_ALMACENES[idx] = updated;
      sel.options[sel.selectedIndex].text = `${updated.nombre}${updated.bodega_siesa_id ? ` (${updated.bodega_siesa_id})` : ''}`;
      mostrarConfigBodega();
      alerta(`Bodega actualizada → ${bodega || '(sin bodega)'}`, 'exito');
    } else {
      const d = await r.json();
      alerta(d.error || 'Error al guardar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Switch the inventory sub-tab between conteos and ABC views.
 * @param {string} nombre - Sub-tab key: 'conteos' or 'abc'.
 */
function invSubtab(nombre) {
  _INV_SUBTAB = nombre;
  const tabs = { conteos: 'inv-tab-conteos', abc: 'inv-tab-abc', datos: 'inv-tab-datos' };
  const panels = { conteos: 'inv-panel-conteos', abc: 'inv-panel-abc', datos: 'inv-panel-datos' };
  Object.entries(tabs).forEach(([k, id]) => {
    const el = document.getElementById(id);
    if (!el) return;
    const activo = k === nombre;
    el.style.background = activo ? '#1E8395' : 'transparent';
    el.style.color = activo ? '#fff' : '#415A70';
    el.style.fontWeight = activo ? '700' : '400';
  });
  Object.entries(panels).forEach(([k, id]) => {
    const el = document.getElementById(id);
    if (el) el.style.display = k === nombre ? 'block' : 'none';
  });
  if (nombre === 'conteos') cargarConteos();
  else if (nombre === 'abc') cargarResumenAbc();
  else if (nombre === 'datos') kardexCargarPanel();
}

let _CONTEO_PAGE = 1;
let _CONTEO_VISTA = 'progreso';  // 'accion' | 'progreso' | 'resueltos'

/**
 * Switch the conteo view mode and reload data.
 * @param {string} v - View key: 'accion', 'progreso', or 'resueltos'.
 */
function conteoVista(v) {
  _CONTEO_VISTA = v;
  _CONTEO_PAGE = 1;
  ['accion', 'progreso', 'resueltos'].forEach(k => {
    const btn = document.getElementById(`cv-tab-${k}`);
    if (!btn) return;
    const activo = k === v;
    btn.style.background = activo ? 'var(--pm)' : 'var(--bg-s)';
    btn.style.color      = activo ? '#fff'       : 'var(--tx2)';
    btn.style.border     = activo ? 'none'       : '1px solid var(--brd)';
  });
  cargarConteos();
}

/** Reset to page 1 and reload conteos with the current filter values. */
function conteosFiltrar() {
  _CONTEO_PAGE = 1;
  cargarConteos();
}

// ── Render helpers por vista ──────────────────────────────────────────────────

/**
 * Render a small colored badge indicating the conteo type (PICKING/MANUAL).
 * @param {Object} s - Conteo session object.
 * @returns {string} HTML badge string or empty string.
 */
function _tipoTag(s) {
  if (s.tipo === 'EXCEPCION_PICKING')
    return `<span style="background:#1A0606;color:#F87171;font-size:9px;font-weight:700;padding:1px 6px;border-radius:6px;margin-left:3px;">PICKING</span>`;
  if (s.tipo === 'MANUAL')
    return `<span style="background:#0B3038;color:#1E8395;font-size:9px;font-weight:700;padding:1px 6px;border-radius:6px;margin-left:3px;">MANUAL</span>`;
  return '';
}

/**
 * Build the detailed action card for a conteo requiring admin review.
 * @param {Object} s - Conteo session object with segundo_conteo/tercer_conteo.
 * @returns {string} HTML string for the action card.
 */
function _renderCardAccion(s) {
  const hijo = s.segundo_conteo;           // CC2
  const cc3  = hijo?.tercer_conteo;        // CC3 (cuando CC1≠CC2)
  const TERMINADOS = ['MATCH','DESCUADRE','SEGUNDO_CONTEO','TERCER_CONTEO','AJUSTADO','CANCELADO'];
  const esTercerConteo = s.estado === 'TERCER_CONTEO';
  const cc2Pendiente = hijo ? !TERMINADOS.includes(hijo.estado) : true;
  const cc3Pendiente = cc3 ? !TERMINADOS.includes(cc3.estado) : true;
  const hijoPendiente = esTercerConteo ? cc3Pendiente : cc2Pendiente;
  const puedeAjustar = s.estado === 'DESCUADRE';
  const coinciden = hijo && !cc2Pendiente && hijo.cantidad_fisica != null && hijo.cantidad_fisica === s.cantidad_fisica;
  const bordColor = s.estado === 'DESCUADRE' ? '#7F1D1D' : s.estado === 'TERCER_CONTEO' ? '#7F4010' : '#164F5A';
  const badgeColor = s.estado === 'DESCUADRE' ? '#7F1D1D' : s.estado === 'TERCER_CONTEO' ? '#92400E' : '#1E8395';
  const dif = s.diferencia != null ? (s.diferencia > 0 ? `+${s.diferencia}` : `${s.diferencia}`) : '?';
  const difCol = (s.diferencia || 0) > 0 ? '#22C55E' : '#F87171';
  const mostrarOmitir = ['SEGUNDO_CONTEO','TERCER_CONTEO'].includes(s.estado) && hijoPendiente;
  const btnTexto = hijoPendiente
    ? (esTercerConteo ? '⏳ Esperando 3er conteo' : '⏳ Esperando 2do conteo')
    : '✓ Confirmar ajuste →';

  // Columna 3 del grid varía según estado
  let col3Html;
  if (esTercerConteo) {
    const cc2Val = hijo?.cantidad_fisica != null ? hijo.cantidad_fisica : '—';
    const cc3Hecho = cc3 && !cc3Pendiente;
    col3Html = `
      <div style="font-size:9px;color:#415A70;font-weight:700;text-transform:uppercase;margin-bottom:2px;">CC2 / CC3</div>
      <div style="font-size:14px;font-weight:700;color:#F87171;line-height:1.4;">${cc2Val}<span style="font-size:9px;color:#415A70;margin-left:3px;">CC2</span></div>
      ${cc3Hecho
        ? `<div style="font-size:14px;font-weight:700;color:#FBBF24;line-height:1.4;">${cc3.cantidad_fisica ?? '—'}<span style="font-size:9px;color:#415A70;margin-left:3px;">CC3</span></div>`
        : `<div style="font-size:13px;color:#415A70;">⏳ CC3</div>`}`;
  } else {
    col3Html = `
      <div style="font-size:9px;color:#415A70;font-weight:700;text-transform:uppercase;margin-bottom:3px;">2do Conteo</div>
      ${hijo && !cc2Pendiente
        ? `<div style="font-size:20px;font-weight:800;color:${coinciden?'#22C55E':'#F87171'};line-height:1;">${hijo.cantidad_fisica != null ? hijo.cantidad_fisica : '—'}</div>
           <div style="font-size:9px;color:#415A70;margin-top:2px;">${hijo.operario_nombre || (hijo.operario_id ? `Op #${hijo.operario_id}` : '—')}</div>`
        : `<div style="font-size:16px;color:#415A70;padding:2px 0;">⏳</div>
           <div style="font-size:9px;color:#415A70;margin-top:2px;">${hijo ? (hijo.operario_nombre || (hijo.operario_id ? `Op #${hijo.operario_id}` : 'asignado')) : 'sin asignar'}</div>`}`;
  }

  return `<div style="background:#121C26;border:1px solid ${bordColor};border-radius:12px;padding:14px;margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:700;">${s.producto_codigo || '—'}${_tipoTag(s)}${s.clasificacion_abc ? `<span style="background:#1C2B3A;color:#FBBF24;font-size:9px;font-weight:700;padding:1px 5px;border-radius:6px;margin-left:4px;">ABC-${s.clasificacion_abc}</span>` : ''}</div>
        <div style="font-size:11px;color:#415A70;margin-top:1px;">${s.producto_nombre || ''}</div>
        <div style="font-size:11px;color:#415A70;margin-top:1px;">📍 ${s.ubicacion_codigo || '—'}</div>
      </div>
      <span style="background:${badgeColor};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;white-space:nowrap;flex-shrink:0;margin-left:8px;">${s.estado}</span>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;background:#0B1117;border-radius:8px;padding:10px;margin-bottom:10px;text-align:center;">
      <div>
        <div style="font-size:9px;color:#415A70;font-weight:700;text-transform:uppercase;margin-bottom:3px;">Siesa</div>
        <div style="font-size:20px;font-weight:800;color:#60A5FA;line-height:1;">${s.existencia_siesa != null ? s.existencia_siesa : '—'}</div>
        <div style="font-size:9px;color:#415A70;margin-top:2px;">${s.bodega_siesa_id || 'stock'}</div>
      </div>
      <div style="border-left:1px solid #1C2B3A;border-right:1px solid #1C2B3A;">
        <div style="font-size:9px;color:#415A70;font-weight:700;text-transform:uppercase;margin-bottom:3px;">1er Conteo</div>
        <div style="font-size:20px;font-weight:800;color:#FBBF24;line-height:1;">${s.cantidad_fisica != null ? s.cantidad_fisica : '—'}</div>
        <div style="font-size:9px;color:#415A70;margin-top:2px;">${s.operario_id ? `Op #${s.operario_id}` : '—'}</div>
      </div>
      <div>${col3Html}</div>
    </div>

    <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#415A70;margin-bottom:10px;">
      <span>Δ <span style="color:${difCol};font-weight:700;">${dif} uds</span>${s.motivo_codigo ? ` · <span style="color:${s.motivo_codigo==='AJ-ENT'?'#22C55E':'#F87171'};">${s.motivo_codigo}</span>` : ''}</span>
      ${s.estado === 'DESCUADRE' && coinciden
        ? `<span style="color:#22C55E;font-size:10px;">✓ CC1==CC2 confirmado</span>`
        : s.estado === 'DESCUADRE' && !coinciden && hijo && !cc2Pendiente
          ? `<span style="color:#FBBF24;font-size:10px;">✓ CC3 definitivo</span>`
          : ''}
    </div>

    <div style="display:flex;gap:6px;flex-wrap:wrap;">
      ${s.estado !== 'AJUSTADO' && s.estado !== 'AJUSTANDO'
        ? `<button onclick="conteoAbrirEdicion(${JSON.stringify(s).replace(/"/g,'&quot;')})"
             style="padding:8px 10px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:8px;font-size:12px;cursor:pointer;">✏</button>`
        : ''}
      ${mostrarOmitir
        ? `<button onclick="conteoOmitirSegundo(${s.id})"
             title="Saltar CC2/CC3 y mover a DESCUADRE para revisión admin"
             style="padding:8px 10px;background:none;border:1px solid #415A70;color:#415A70;border-radius:8px;font-size:11px;cursor:pointer;white-space:nowrap;">Omitir CC2</button>`
        : ''}
      <button onclick="${puedeAjustar ? `conteoAbrirAjuste(${JSON.stringify(s).replace(/"/g,'&quot;')})` : 'void(0)'}"
        ${!puedeAjustar ? 'disabled' : ''}
        style="flex:2;padding:8px;background:${puedeAjustar?'#1E8395':'var(--bg-input)'};color:${puedeAjustar?'#fff':'var(--tx3)'};border:${puedeAjustar?'none':'1px solid var(--brd)'};border-radius:8px;font-size:12px;font-weight:700;cursor:${puedeAjustar?'pointer':'not-allowed'};min-width:120px;">
        ${btnTexto}
      </button>
      <button onclick="conteoCancelar(${s.id})" style="padding:8px;background:none;border:1px solid #7F1D1D;color:#F87171;border-radius:8px;font-size:11px;cursor:pointer;">✕</button>
    </div>
    ${s.editado_en ? `<div style="font-size:10px;color:#415A70;margin-top:6px;">✏ Editado: ${s.motivo_edicion}</div>` : ''}
  </div>`;
}

/**
 * Build a compact card for a conteo in progress (PENDIENTE/EN_PROCESO).
 * @param {Object} s - Conteo session object.
 * @returns {string} HTML string for the progress card.
 */
function _renderCardProgreso(s) {
  const col = s.estado === 'EN_PROCESO' ? '#164F5A' : '#253A4A';
  return `<div style="background:#121C26;border:1px solid #1C2B3A;border-radius:10px;padding:12px;margin-bottom:6px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:12px;font-weight:700;">${s.producto_codigo || '—'}${_tipoTag(s)}</div>
        <div style="font-size:11px;color:#415A70;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.producto_nombre || ''}</div>
        <div style="font-size:11px;color:#415A70;margin-top:2px;">📍 ${s.ubicacion_codigo || '—'}${s.operario_id ? ` · 👤 Op #${s.operario_id}` : ''}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0;margin-left:8px;">
        ${s.clasificacion_abc ? `<span style="background:#1C2B3A;color:#FBBF24;font-size:9px;font-weight:700;padding:1px 5px;border-radius:6px;">ABC-${s.clasificacion_abc}</span>` : ''}
        <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;">${s.estado}</span>
        <button onclick="conteoCancelar(${s.id})" style="background:none;border:1px solid #7F1D1D;color:#F87171;font-size:9px;padding:1px 6px;border-radius:6px;cursor:pointer;">Cancelar</button>
      </div>
    </div>
  </div>`;
}

/**
 * Build a muted card for a resolved conteo (MATCH/AJUSTADO/CANCELADO).
 * @param {Object} s - Conteo session object.
 * @returns {string} HTML string for the resolved card.
 */
function _renderCardResuelto(s) {
  const colMap = { MATCH:'#14532D', AJUSTADO:'#14532D', AJUSTANDO:'#7F1D1D', CANCELADO:'#253A4A' };
  const col = colMap[s.estado] || '#253A4A';
  const dif = s.diferencia != null ? (s.diferencia > 0 ? `+${s.diferencia}` : `${s.diferencia}`) : null;
  const difCol = (s.diferencia || 0) > 0 ? '#22C55E' : (s.diferencia || 0) < 0 ? '#F87171' : '#415A70';
  return `<div style="background:#0B1117;border:1px solid #1C2B3A;border-radius:10px;padding:12px;margin-bottom:6px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:12px;font-weight:700;color:#415A70;">${s.producto_codigo || '—'}</div>
        <div style="font-size:11px;color:#415A70;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.producto_nombre || ''}</div>
        <div style="font-size:11px;color:#415A70;margin-top:1px;">📍 ${s.ubicacion_codigo || '—'}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0;margin-left:8px;">
        <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;">${s.estado}</span>
        ${dif ? `<span style="color:${difCol};font-size:11px;font-weight:700;">Δ ${dif}</span>` : ''}
      </div>
    </div>
    ${s.aprobador_nombre ? `<div style="font-size:10px;color:#415A70;margin-top:4px;">✓ ${s.aprobador_nombre}</div>` : ''}
  </div>`;
}

// ── Stats dashboard + asignación en lote ─────────────────────────────────────

/** Fetch and display conteo KPI stats (pendientes, en curso, hoy, atrasados). */
async function cargarConteoStats() {
  const bar = document.getElementById('conteo-stats-bar');
  if (!bar) return;
  try {
    const qs = new URLSearchParams();
    const almId = document.getElementById('inv-abc-almacen')?.value;
    if (almId) qs.set('almacen_id', almId);
    const r = await fetch(API + '/api/conteo/stats?' + qs, { headers: { Authorization: 'Bearer ' + TOKEN } });
    if (!r.ok) return;
    const d = await r.json();
    document.getElementById('cs-pendientes').textContent = d.pendientes || 0;
    document.getElementById('cs-en-curso').textContent = d.en_proceso || 0;
    document.getElementById('cs-hoy').textContent = d.hoy_completados || 0;
    const atrasados = d.atrasados_2d || 0;
    const wrapAtraso = document.getElementById('cs-atrasados-wrap');
    if (wrapAtraso) {
      wrapAtraso.style.display = atrasados > 0 ? 'inline' : 'none';
      document.getElementById('cs-atrasados').textContent = atrasados;
    }
    const btnAsignar = document.getElementById('cs-btn-asignar');
    const sinAsignar = d.sin_asignar || 0;
    if (btnAsignar) {
      btnAsignar.style.display = sinAsignar > 0 ? 'inline-block' : 'none';
      document.getElementById('cs-sin-asignar').textContent = sinAsignar;
    }
    const wrapFallos = document.getElementById('cs-fallos-wrap');
    const fallosDlq = d.fallos_dlq || 0;
    if (wrapFallos) {
      wrapFallos.style.display = fallosDlq > 0 ? 'inline-flex' : 'none';
      document.getElementById('cs-fallos').textContent = fallosDlq;
    }
    bar.style.display = 'block';
  } catch (e) { /* silencioso */ }
}

/** Re-enqueue all failed Siesa adjustment jobs for retry. */
async function conteoReintentarFallos() {
  if (!confirm('Re-encolar todos los ajustes fallidos para reintentar con Siesa?')) return;
  try {
    const r = await fetch(API + '/api/conteo/reintentar-fallos', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`${d.reencolados} ajustes re-encolados`, 'exito');
      await cargarConteoStats();
    } else {
      alerta(d.error || 'Error', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Discard failed DLQ jobs and reset stuck AJUSTANDO sessions to DESCUADRE. */
async function conteoDescartarFallos() {
  if (!confirm('Descartar los ajustes fallidos?\n\nLas sesiones atascadas en AJUSTANDO vuelven a DESCUADRE para que puedas re-aprobar o cancelar.')) return;
  try {
    const r = await fetch(API + '/api/conteo/descartar-fallos', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`${d.descartados} jobs descartados · ${d.sesiones_reset} sesiones a DESCUADRE`, 'exito');
      await cargarConteoStats();
      await cargarConteos(_CONTEO_PAGE);
    } else {
      alerta(d.error || 'Error', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Skip the pending CC2/CC3 and move the session to DESCUADRE for admin review.
 * @param {number} id - Conteo session ID.
 */
async function conteoOmitirSegundo(id) {
  if (!confirm('¿Omitir el CC2/CC3 pendiente y mover esta sesión a DESCUADRE para revisión?\n\nEl conteo pendiente se cancelará. Podrás aprobar o rechazar el ajuste manualmente.')) return;
  try {
    const r = await fetch(API + '/api/conteo/' + id + '/omitir-segundo', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Sesión movida a DESCUADRE — revisa y confirma el ajuste', 'exito');
      await cargarConteoStats();
      await cargarConteos(_CONTEO_PAGE);
    } else {
      alerta(d.error || 'Error', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Export conteo sessions to a CSV file with optional date range filter. */
async function conteoExportar() {
  const almId = document.getElementById('inv-abc-almacen')?.value;
  const desde = prompt('Desde (YYYY-MM-DD, vacío = todo):', '')?.trim() || '';
  if (desde === null) return;
  const hasta = prompt('Hasta (YYYY-MM-DD, vacío = hoy):', '')?.trim() || '';
  const qs = new URLSearchParams();
  if (desde) qs.set('desde', desde);
  if (hasta) qs.set('hasta', hasta);
  if (almId) qs.set('almacen_id', almId);
  try {
    const r = await fetch(API + '/api/conteo/exportar?' + qs, { headers: { Authorization: 'Bearer ' + TOKEN } });
    if (!r.ok) { alerta('Error al exportar', 'error'); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `conteos_${desde || 'all'}_${hasta || 'all'}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) { alerta('Error de conexión', 'error'); }
}

let _CONTEO_OPERARIOS = [];

/** Show the batch-assign panel and load available operarios. */
async function conteoMostrarAsignar() {
  const panel = document.getElementById('conteo-asignar-panel');
  if (!panel) return;
  // Cargar operarios del almacén
  if (_CONTEO_OPERARIOS.length === 0) {
    try {
      const r = await fetch(API + '/api/auth/usuarios', { headers: { Authorization: 'Bearer ' + TOKEN } });
      if (r.ok) {
        const todos = await r.json();
        _CONTEO_OPERARIOS = (todos.usuarios || todos || []).filter(u =>
          u.activo && ['operario', 'jefe_almacen'].includes(u.rol)
        );
      }
    } catch (e) { /* silencioso */ }
  }
  const sel = document.getElementById('conteo-asignar-operario');
  if (sel) {
    sel.innerHTML = _CONTEO_OPERARIOS.map(u =>
      `<option value="${u.id}">${u.nombre || u.usuario} (${u.rol})</option>`
    ).join('');
  }
  panel.style.display = 'block';
}

/** Hide the batch-assign panel. */
function conteoCerrarAsignar() {
  const panel = document.getElementById('conteo-asignar-panel');
  if (panel) panel.style.display = 'none';
}

/** Assign a batch of unassigned conteo tasks to the selected operario. */
async function conteoAsignarLote() {
  const operarioId = document.getElementById('conteo-asignar-operario')?.value;
  const limite = parseInt(document.getElementById('conteo-asignar-limite')?.value) || 10;
  const almId = document.getElementById('inv-abc-almacen')?.value;
  if (!operarioId) { alerta('Selecciona un operario', 'error'); return; }
  try {
    const r = await fetch(API + '/api/conteo/asignar-lote', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ operario_id: parseInt(operarioId), almacen_id: almId ? parseInt(almId) : null, limite })
    });
    const d = await r.json();
    if (r.ok) {
      conteoCerrarAsignar();
      alerta(`${d.asignadas} tareas asignadas a ${d.operario_nombre}`, 'exito');
      await cargarConteoStats();
      await cargarConteos(_CONTEO_PAGE);
    } else {
      alerta(d.error || 'Error al asignar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Cancel a conteo session with a required reason prompt.
 * @param {number} id - Conteo session ID.
 */
async function conteoCancelar(id) {
  const motivo = prompt('Motivo de cancelación:');
  if (!motivo || !motivo.trim()) return;
  try {
    const r = await fetch(API + `/api/conteo/${id}/cancelar`, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo: motivo.trim() })
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Conteo cancelado', 'advertencia');
      await cargarConteoStats();
      await cargarConteos(_CONTEO_PAGE);
    } else {
      alerta(d.error || 'Error al cancelar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── Carga principal ───────────────────────────────────────────────────────────

/**
 * Fetch and render the paginated conteo list for the active view mode.
 * @param {number} [page] - Page number; uses current page if omitted.
 */
async function cargarConteos(page) {
  if (page !== undefined) _CONTEO_PAGE = page;
  const lista = document.getElementById('inv-conteos-lista');
  const pag   = document.getElementById('inv-conteos-paginacion');
  if (!lista) return;

  cargarConteoStats();

  const marca = document.getElementById('inv-filtro-marca')?.value?.trim() || '';
  const clase = document.getElementById('inv-filtro-clase')?.value || '';

  const VISTA_ESTADOS = {
    accion:    'SEGUNDO_CONTEO,TERCER_CONTEO,DESCUADRE',
    progreso:  'PENDIENTE,EN_PROCESO',
    resueltos: 'MATCH,AJUSTADO,AJUSTANDO,CANCELADO',
  };

  const esVacia = !lista.innerHTML.trim() || lista.innerHTML.includes('Cargando');
  if (esVacia) lista.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';

  try {
    const qs = new URLSearchParams({ page: _CONTEO_PAGE });
    qs.set('estados', VISTA_ESTADOS[_CONTEO_VISTA] || '');
    if (marca) qs.set('marca', marca);
    if (clase) qs.set('clasificacion', clase);

    const r = await fetch(API + '/api/conteo/?' + qs, { headers: { Authorization: 'Bearer ' + TOKEN } });
    const d = await r.json();
    const sesiones  = d.sesiones  || [];
    const total     = d.total     || 0;
    const totalPag  = d.total_paginas || 1;

    // Badge contador en tab "Acción"
    if (_CONTEO_VISTA === 'accion') {
      const badge = document.getElementById('cv-badge-accion');
      if (badge) badge.textContent = total > 0 ? total : '';
    }

    if (!sesiones.length) {
      lista.innerHTML = `<div style="text-align:center;padding:30px;color:#555;">${_CONTEO_VISTA === 'accion' ? '✓ Sin conteos pendientes de revisión' : 'No hay conteos con este filtro'}</div>`;
      if (pag) pag.innerHTML = '';
      return;
    }

    // En vista acción mostrar solo padres — los hijos van embebidos en segundo_conteo
    const filas = _CONTEO_VISTA === 'accion'
      ? sesiones.filter(s => !s.es_segundo_conteo)
      : sesiones;

    lista.innerHTML = filas.map(s => {
      if (_CONTEO_VISTA === 'accion')    return _renderCardAccion(s);
      if (_CONTEO_VISTA === 'progreso')  return _renderCardProgreso(s);
      return _renderCardResuelto(s);
    }).join('');

    if (pag) {
      if (totalPag <= 1) { pag.innerHTML = ''; return; }
      pag.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center;width:100%;padding:4px 0;">
          <button onclick="cargarConteos(${_CONTEO_PAGE - 1})" ${_CONTEO_PAGE <= 1 ? 'disabled' : ''}
            style="padding:8px 14px;background:#1a1a1a;border:1px solid #333;color:${_CONTEO_PAGE <= 1 ? '#333' : '#aaa'};border-radius:8px;font-size:13px;cursor:${_CONTEO_PAGE <= 1 ? 'default' : 'pointer'};">
            ← Anterior
          </button>
          <span style="font-size:12px;color:#555;">${total.toLocaleString()} conteos · Pág ${_CONTEO_PAGE}/${totalPag}</span>
          <button onclick="cargarConteos(${_CONTEO_PAGE + 1})" ${_CONTEO_PAGE >= totalPag ? 'disabled' : ''}
            style="padding:8px 14px;background:#1a1a1a;border:1px solid #333;color:${_CONTEO_PAGE >= totalPag ? '#333' : '#aaa'};border-radius:8px;font-size:13px;cursor:${_CONTEO_PAGE >= totalPag ? 'default' : 'pointer'};">
            Siguiente →
          </button>
        </div>`;
    }
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error cargando conteos</div>';
  }
}

/** Show the manual conteo creation form and focus the code input. */
function conteosMostrarFormManual() {
  document.getElementById('conteo-form-manual').style.display = 'block';
  document.getElementById('conteo-manual-codigo').focus();
}
/** Hide the manual conteo form and clear its inputs. */
function conteosOcultarFormManual() {
  document.getElementById('conteo-form-manual').style.display = 'none';
  document.getElementById('conteo-manual-codigo').value = '';
  document.getElementById('conteo-manual-error').textContent = '';
}
/** Create a manual conteo task for a specific product code and almacen. */
async function crearConteoManual() {
  const almacenId = document.getElementById('conteo-manual-almacen')?.value;
  const codigo = document.getElementById('conteo-manual-codigo')?.value.trim().toUpperCase();
  const errorEl = document.getElementById('conteo-manual-error');
  errorEl.textContent = '';
  if (!codigo) { errorEl.textContent = 'Ingresa el código del producto'; return; }
  if (!almacenId) { errorEl.textContent = 'Selecciona un almacén'; return; }
  try {
    const r = await fetch(API + '/api/conteo/manual', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ almacen_id: parseInt(almacenId), producto_codigo: codigo }),
    });
    const d = await r.json();
    if (r.ok) {
      if (d.tareas_creadas === 0) {
        errorEl.textContent = d.omitidas_ya_activas > 0 ? 'Ya existe un conteo activo para este producto' : 'Producto sin stock en este almacén';
      } else {
        alerta(`Conteo creado para ${d.producto_nombre || codigo}`, 'exito');
        conteosOcultarFormManual();
        await cargarConteos(1);
      }
    } else {
      errorEl.textContent = d.error || 'Error al crear conteo';
    }
  } catch (e) { errorEl.textContent = 'Error de conexión'; }
}

/** Fetch and render the ABC classification summary for the selected almacen. */
async function cargarResumenAbc() {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) return;
  const resumenEl = document.getElementById('inv-abc-resumen');
  if (!resumenEl) return;
  resumenEl.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';
  try {
    const r = await fetch(API + `/api/conteo/abc/resumen?almacen_id=${almacenId}`, {
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    const dist = d.distribucion_abc || {};
    const items = [
      { clase: 'A', col: '#4ade80', bg: '#1e3a1e', border: '#166534', desc: 'Alta rotación · cada 15 días' },
      { clase: 'B', col: '#60a5fa', bg: '#1e2a3a', border: '#1e40af', desc: 'Rotación media · cada 90 días' },
      { clase: 'C', col: '#f87171', bg: '#2a1e1e', border: '#7f1d1d', desc: 'Baja rotación · cada 180 días' },
    ];
    resumenEl.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px;">
        ${items.map(it => {
          const total = dist[it.clase]?.total_productos ?? '—';
          return `
          <div style="background:${it.bg};border:1px solid ${it.border};border-radius:10px;padding:14px;text-align:center;">
            <div style="font-size:22px;font-weight:900;color:${it.col};">${total}</div>
            <div style="font-size:11px;font-weight:700;color:${it.col};">Clase ${it.clase}</div>
            <div style="font-size:10px;color:#666;margin-top:2px;">${it.desc}</div>
          </div>`;
        }).join('')}
      </div>
      <div style="font-size:11px;color:#555;text-align:right;">Fuente: ${d.fuente || 'WMS'}</div>`;
  } catch (e) {
    resumenEl.innerHTML = '<div style="color:#ef4444;font-size:12px;">Error cargando resumen</div>';
  }
}

/**
 * Generate conteo tasks for a single ABC class (daily batch or forced all).
 * @param {string} clase - ABC class: 'A', 'B', or 'C'.
 * @param {boolean} [forzarTodo=false] - If true, skip daily batch limits.
 */
async function generarAbc(clase, forzarTodo = false) {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) { alerta('Selecciona un almacén primero', 'error'); return; }
  const res = document.getElementById('inv-abc-resultado');
  if (res) res.textContent = 'Generando...';
  try {
    const r = await fetch(API + '/api/conteo/abc/generar-tareas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ almacen_id: parseInt(almacenId), clasificacion: clase, forzar_todo: forzarTodo })
    });
    const d = await r.json();
    if (r.ok) {
      const loteInfo = d.batch_diario ? ` (lote ${d.batch_diario}/día de ${d.total_clase})` : ' (todo)';
      const msg = `Clase ${clase}: ${d.tareas_creadas} tareas${loteInfo}`;
      if (res) res.textContent = msg;
      alerta(msg, d.tareas_creadas > 0 ? 'exito' : 'advertencia');
      await cargarConteos(1);
    } else {
      if (res) res.textContent = '';
      alerta(d.error || 'Error generando tareas', 'error');
    }
  } catch (e) {
    if (res) res.textContent = '';
    alerta('Error de conexión', 'error');
  }
}

/**
 * Generate conteo tasks for all ABC classes at once.
 * @param {boolean} [forzarTodo=false] - If true, skip daily batch limits for all classes.
 */
async function generarTodasClases(forzarTodo = false) {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) { alerta('Selecciona un almacén primero', 'error'); return; }
  if (forzarTodo && !confirm('¿Generar tareas para TODOS los productos elegibles sin límite de lote? Puede crear miles de conteos.')) return;
  const res = document.getElementById('inv-abc-resultado');
  if (res) res.textContent = forzarTodo ? 'Forzando todo...' : 'Generando lote del día...';
  try {
    const r = await fetch(API + '/api/conteo/abc/generar-todas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ almacen_id: parseInt(almacenId), forzar_todo: forzarTodo })
    });
    const d = await r.json();
    if (r.ok) {
      const watchdog = d.por_clase?.watchdog;
      const wdMsg = watchdog?.overrides > 0 ? ` · 🤖 ${watchdog.overrides} watchdog` : '';
      const msg = `${d.total_tareas_creadas} tareas nuevas${wdMsg}`;
      if (res) res.textContent = msg;
      alerta(msg, d.total_tareas_creadas > 0 ? 'exito' : 'advertencia');
      await cargarConteos(1);
    } else {
      if (res) res.textContent = '';
      alerta(d.error || 'Error generando tareas', 'error');
    }
  } catch (e) {
    if (res) res.textContent = '';
    alerta('Error de conexión', 'error');
  }
}

/** Delete all PENDIENTE conteo tasks for a specific ABC class or all classes. */
async function limpiarPendientesAbc() {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) { alerta('Selecciona un almacén primero', 'error'); return; }
  const clase = prompt('¿Qué clase limpiar? Escribe A, B, C o deja vacío para TODAS (esto eliminará TODAS las tareas PENDIENTE de la clase):');
  if (clase === null) return; // canceló
  const claseUpper = clase.trim().toUpperCase();
  if (claseUpper && !['A','B','C'].includes(claseUpper)) {
    alerta('Clase inválida. Usa A, B, C o deja vacío.', 'error'); return;
  }
  const etiqueta = claseUpper || 'todas las clases';
  if (!confirm(`¿Eliminar TODAS las tareas PENDIENTE de ${etiqueta}? Esta acción no se puede deshacer.`)) return;
  try {
    const r = await fetch(API + '/api/conteo/abc/limpiar-pendientes', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ almacen_id: parseInt(almacenId), clasificacion: claseUpper || null }),
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`${d.eliminadas} tareas eliminadas (${etiqueta})`, 'exito');
      await cargarConteos(1);
    } else {
      alerta(d.error || 'Error limpiando cola', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ─── Edición de conteos (admin) ────────────────────────────────────────────

let _CONTEO_EDICION_ID = null;

/**
 * Open the admin edit modal for a conteo session.
 * @param {Object} s - Conteo session object with product, counts, and state.
 */
function conteoAbrirEdicion(s) {
  _CONTEO_EDICION_ID = s.id;
  const m = document.getElementById('modal-conteo-edicion');
  if (!m) return;

  const bloqueado = s.estado === 'AJUSTADO' || s.estado === 'CANCELADO';
  const cantInput = document.getElementById('conteo-edit-cantidad');
  const estadoBadge = document.getElementById('conteo-edit-estado');

  if (cantInput) {
    cantInput.value = s.cantidad_fisica ?? '';
    cantInput.disabled = bloqueado;
  }
  if (estadoBadge) estadoBadge.textContent = s.estado;

  const infoDiv = document.getElementById('conteo-edit-info');
  if (infoDiv) {
    const hijo = s.segundo_conteo;
    infoDiv.innerHTML = `
      <div style="font-size:12px;color:#888;margin-bottom:10px;">
        <b>${s.producto_codigo || '—'}</b> · ${s.producto_nombre || ''}<br>
        📍 ${s.ubicacion_codigo || s.ubicacion_id || '—'}${s.clasificacion_abc ? ` · ABC-${s.clasificacion_abc}` : ''}<br>
        <span style="display:inline-flex;gap:12px;margin-top:4px;">
          ${s.existencia_siesa != null ? `<span>Siesa <b style="color:#60a5fa;">${s.existencia_siesa}</b></span>` : '<span style="color:#374151;">Sin ref. Siesa</span>'}
          ${s.cantidad_fisica != null ? `<span>1er conteo <b style="color:#f59e0b;">${s.cantidad_fisica}</b></span>` : ''}
          ${hijo?.cantidad_fisica != null ? `<span>2do conteo <b style="color:${hijo.cantidad_fisica===s.cantidad_fisica?'#4ade80':'#f87171'};">${hijo.cantidad_fisica}</b></span>` : ''}
        </span>
        ${s.editado_en ? `<br><span style="color:#f59e0b;">Última edición: ${s.motivo_edicion}</span>` : ''}
      </div>`;
  }
  const motivoInput = document.getElementById('conteo-edit-motivo');
  if (motivoInput) motivoInput.value = '';

  m.style.display = 'flex';
}

/** Close the conteo edit modal and reset the editing state. */
function conteosCerrarEdicion() {
  const m = document.getElementById('modal-conteo-edicion');
  if (m) m.style.display = 'none';
  _CONTEO_EDICION_ID = null;
}

/** Save the edited conteo quantity and reason, then refresh the list. */
async function conteoGuardarEdicion() {
  if (!_CONTEO_EDICION_ID) return;
  const cantRaw = document.getElementById('conteo-edit-cantidad')?.value;
  const motivo  = document.getElementById('conteo-edit-motivo')?.value?.trim();

  if (!motivo) { alerta('El motivo de edición es obligatorio', 'error'); return; }

  const body = { motivo_edicion: motivo };
  const cantEl = document.getElementById('conteo-edit-cantidad');
  if (cantEl && !cantEl.disabled && cantRaw !== '') {
    const cant = parseInt(cantRaw, 10);
    if (isNaN(cant) || cant < 0) { alerta('Cantidad inválida', 'error'); return; }
    body.cantidad_fisica = cant;
  }

  try {
    const r = await fetch(API + `/api/conteo/${_CONTEO_EDICION_ID}/editar`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Conteo actualizado · ' + (d.cambios || []).join(', '), 'exito');
      conteosCerrarEdicion();
      await cargarConteos(_CONTEO_PAGE);
    } else {
      alerta(d.error || 'Error actualizando conteo', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Upload a CSV/Excel file with ABC classifications and update products.
 * @param {HTMLInputElement} input - File input element with the selected file.
 */
async function subirCsvAbc(input) {
  const archivo = input.files[0];
  if (!archivo) return;

  const label = document.getElementById('abc-upload-label');
  const res = document.getElementById('abc-upload-resultado');
  const nombreOriginal = label.innerHTML;

  label.style.borderColor = '#555';
  label.style.color = '#aaa';
  label.innerHTML = `⏳ Procesando ${archivo.name}... <input type="file" id="abc-csv-input" accept=".csv,.xlsx,.xls,.txt" style="display:none;" onchange="subirCsvAbc(this)">`;
  res.style.color = '#888';
  res.textContent = 'Subiendo archivo...';

  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  const form = new FormData();
  form.append('archivo', archivo);
  if (almacenId) form.append('almacen_id', almacenId);

  try {
    const r = await fetch(API + '/api/conteo/abc/cargar-csv', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
      body: form
    });
    const d = await r.json();

    if (r.ok) {
      const dist = d.distribucion || {};
      const msg = `✓ ${d.actualizados} productos actualizados · A:${dist.A||0} B:${dist.B||0} C:${dist.C||0}`;
      res.style.color = '#4ade80';
      res.textContent = msg;
      label.style.borderColor = '#166534';
      label.style.color = '#4ade80';
      label.innerHTML = `✓ ${archivo.name} cargado <input type="file" id="abc-csv-input" accept=".csv,.xlsx,.xls,.txt" style="display:none;" onchange="subirCsvAbc(this)">`;

      if (d.no_encontrados > 0) {
        res.textContent += ` · ${d.no_encontrados} refs no encontradas en WMS`;
      }
      // Recargar resumen ABC
      await cargarResumenAbc();
    } else {
      res.style.color = '#ef4444';
      res.textContent = `✗ ${d.error || 'Error procesando archivo'}`;
      label.style.borderColor = '#7f1d1d';
      label.style.color = '#ef4444';
      label.innerHTML = `✗ Error — volver a intentar <input type="file" id="abc-csv-input" accept=".csv,.xlsx,.xls,.txt" style="display:none;" onchange="subirCsvAbc(this)">`;
    }
  } catch (e) {
    res.style.color = '#ef4444';
    res.textContent = '✗ Error de conexión';
  }
  // Limpiar input para permitir subir el mismo archivo de nuevo
  input.value = '';
}

/** Run the ABC watchdog to detect anomalous rotation and force conteos. */
async function ejecutarWatchdog() {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) { alerta('Selecciona un almacén primero', 'error'); return; }
  const res = document.getElementById('inv-abc-resultado');
  if (res) res.textContent = '🤖 Escaneando anomalías...';
  try {
    const r = await fetch(API + '/api/conteo/abc/watchdog', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ almacen_id: parseInt(almacenId) })
    });
    const d = await r.json();
    if (r.ok) {
      const msg = d.overrides > 0
        ? `🤖 Watchdog: ${d.overrides} producto(s) con rotación anómala → conteo forzado`
        : '🤖 Watchdog: sin anomalías detectadas';
      if (res) res.textContent = msg;
      alerta(msg, d.overrides > 0 ? 'advertencia' : 'exito');
      if (d.overrides > 0) await cargarConteos();
    } else {
      if (res) res.textContent = '';
      alerta(d.error || 'Error en watchdog', 'error');
    }
  } catch (e) {
    if (res) res.textContent = '';
    alerta('Error de conexión', 'error');
  }
}

let _CONTEO_AJUSTE_SESION = null;

/**
 * Open the adjustment confirmation modal for a DESCUADRE conteo.
 * @param {Object} s - Conteo session object with difference and Siesa details.
 */
function conteoAbrirAjuste(s) {
  _CONTEO_AJUSTE_SESION = s;
  const m    = document.getElementById('modal-conteo-ajuste');
  const info = document.getElementById('conteo-ajuste-info');
  const obs  = document.getElementById('conteo-ajuste-obs');
  if (!m || !info) return;
  if (obs) obs.value = '';

  const hijo     = s.segundo_conteo;
  const difVal   = s.diferencia != null ? s.diferencia : 0;
  const dif      = difVal > 0 ? `+${difVal}` : `${difVal}`;
  const difCol   = difVal > 0 ? '#4ade80' : '#f87171';
  const motivo   = s.motivo_codigo || (difVal > 0 ? 'AJ-ENT' : 'AJ-SAL');
  const accion   = motivo === 'AJ-ENT' ? '📦 ENTRADA' : '📤 SALIDA';
  const cant     = Math.abs(difVal);
  const coinciden = hijo && hijo.cantidad_fisica != null && hijo.cantidad_fisica === s.cantidad_fisica;

  const bodega = s.bodega_siesa_id || '—';

  info.innerHTML = `
    <div style="margin-bottom:10px;">
      <div style="font-size:13px;font-weight:700;color:#e2e8f0;">${s.producto_codigo || '—'} · ${s.producto_nombre || ''}</div>
      <div style="font-size:11px;color:#4b5563;margin-top:1px;">📍 ${s.ubicacion_codigo || '—'} · Bodega: <span style="color:#60a5fa;font-weight:700;">${bodega}</span></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:12px;text-align:center;">
      <div>
        <div style="font-size:9px;color:#4b5563;text-transform:uppercase;margin-bottom:2px;">WMS</div>
        <div style="font-size:18px;font-weight:800;color:#60a5fa;">${s.existencia_siesa ?? '—'}</div>
      </div>
      <div style="border-left:1px solid #1f2937;border-right:1px solid #1f2937;">
        <div style="font-size:9px;color:#4b5563;text-transform:uppercase;margin-bottom:2px;">1er Conteo</div>
        <div style="font-size:18px;font-weight:800;color:#f59e0b;">${s.cantidad_fisica ?? '—'}</div>
      </div>
      <div>
        <div style="font-size:9px;color:#4b5563;text-transform:uppercase;margin-bottom:2px;">2do Conteo</div>
        <div style="font-size:18px;font-weight:800;color:${coinciden?'#4ade80':'#f87171'};">${hijo?.cantidad_fisica ?? '—'}</div>
      </div>
    </div>
    <div style="background:#0d0d0d;border-radius:8px;padding:10px;text-align:center;margin-bottom:8px;">
      <div style="font-size:10px;color:#4b5563;margin-bottom:4px;">Se enviará a SIESA → Bodega <span style="color:#60a5fa;font-weight:700;">${bodega}</span>:</div>
      <div style="font-size:16px;font-weight:800;color:${difCol};">${accion} de ${cant} unidades</div>
      <div style="font-size:10px;color:#4b5563;margin-top:2px;">${motivo} · Concepto 603 · Clase 63</div>
    </div>
    ${hijo && !coinciden ? `<div style="color:#f87171;font-size:11px;text-align:center;">⚠ Los operarios no coinciden — se usará el 2do conteo como referencia</div>` : ''}
    ${coinciden ? `<div style="color:#4ade80;font-size:11px;text-align:center;">✓ Ambos operarios confirmaron el mismo valor</div>` : ''}
  `;

  m.style.display = 'flex';
}

/** Close the adjustment modal and clear the active session reference. */
function conteosCerrarAjuste() {
  const m = document.getElementById('modal-conteo-ajuste');
  if (m) m.style.display = 'none';
  _CONTEO_AJUSTE_SESION = null;
}

/** Confirm and enqueue the inventory adjustment to Siesa for the active session. */
async function conteoConfirmarAjuste() {
  if (!_CONTEO_AJUSTE_SESION) return;
  const s    = _CONTEO_AJUSTE_SESION;
  const hijo = s.segundo_conteo;

  // Usar ID del hijo cuando está completo (datos verificados del 2do conteo);
  // caer al padre si no hay hijo (excepción picking, conteo directo a DESCUADRE).
  const TERMINADOS = ['DESCUADRE', 'SEGUNDO_CONTEO'];
  const sesionId   = (hijo && hijo.id && TERMINADOS.includes(hijo.estado)) ? hijo.id : s.id;

  const btn = document.getElementById('btn-confirmar-ajuste');
  if (btn) { btn.disabled = true; btn.textContent = 'Procesando...'; }

  try {
    const r = await fetch(API + `/api/conteo/${sesionId}/ajustar`, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      conteosCerrarAjuste();
      alerta(`Ajuste ${d.motivo_codigo} encolado a Siesa · Δ ${d.diferencia} uds`, 'exito');
      await cargarConteos(_CONTEO_PAGE);
    } else {
      alerta(d.error || 'Error al ajustar', 'error');
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Confirmar → SIESA'; }
  }
}

