// ══════════════════════════════════════════════════════════════════
// INVENTARIO CÍCLICO — Admin tab
// Dependencias globales (de app.js): alerta(), API, TOKEN
// ══════════════════════════════════════════════════════════════════

let _INV_SUBTAB = 'conteos';
let _INV_ALMACENES = [];

// ── Aviso de cajas POS al iniciar un conteo ─────────────────────────────
// Una caja que vende sin conexión no sube `f400_cant_pos_1` a Siesa central
// hasta que sincroniza. Mientras tanto el teórico (existencia − POS) queda
// ALTO: la mercancía ya salió del estante y Siesa no lo sabe. El conteo ve un
// faltante falso, se ajusta, y cuando la caja sincroniza la acumulación del
// POS descuenta otra vez: doble descuento. El servidor no lo puede detectar
// (ninguna foto de Siesa ve una venta que todavía no le llegó), así que se le
// pide a quien cuenta que lo confirme. Pendiente: confirmar con el consultor
// si Siesa POS puede vender offline.
//
// Sale en TODAS las bodegas —en Siesa QA hasta NB1 tiene POS pendiente— y en
// todo sitio donde un conteo empieza. Es un aviso, no un bloqueo: nada de
// confirm() nativo. El texto vive SOLO acá; cada pantalla llama
// `avisoCajasPosHtml()`. Trinquete:
// tests/test_conteo_ventas_durante_conteo.py::TestAvisoCajasPosEnCadaInicioDeConteo
const AVISO_CAJAS_POS = 'Antes de contar: confirma que todas las cajas POS de la tienda '
  + 'están en línea y al día — que la última venta de cada caja ya aparezca en Siesa '
  + 'central. Si una caja estuvo caída, cuenta después de que sincronice.';

/** Banner destacado con AVISO_CAJAS_POS. Texto estático: no lleva dato de nadie. */
function avisoCajasPosHtml() {
  return `<div class="aviso-cajas-pos" role="note" style="background:#1c1a0a;border:1px solid #b45309;border-radius:12px;padding:12px 14px;margin-bottom:12px;text-align:left;">
      <div style="font-size:12px;color:#f59e0b;font-weight:800;margin-bottom:4px;">⚠️ CAJAS POS AL DÍA</div>
      <div style="font-size:13px;color:#fde68a;line-height:1.45;">${AVISO_CAJAS_POS}</div>
    </div>`;
}

/** Load almacenes for the ABC selector (once) and refresh the active inventory panel. */
async function cargarInventario() {
  // Cargar almacenes para el selector ABC (solo una vez)
  if (_INV_ALMACENES.length === 0) {
    try {
      _INV_ALMACENES = await get('/api/almacenes/');
      const opts = _INV_ALMACENES.map(a =>
        `<option value="${esc(a.id)}">${esc(a.nombre)}${a.bodega_siesa_id ? ` (${esc(a.bodega_siesa_id)})` : ''}</option>`
      ).join('');
      const sel = document.getElementById('inv-abc-almacen');
      if (sel) sel.innerHTML = opts;
      const selM = document.getElementById('conteo-manual-almacen');
      if (selM) selM.innerHTML = opts;
      mostrarConfigBodega();
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
    const updated = await put(`/api/almacenes/${almId}`, { bodega_siesa_id: bodega || null, centro_op_siesa: centroOp || null });
    const idx = _INV_ALMACENES.findIndex(a => a.id == almId);
    if (idx >= 0) _INV_ALMACENES[idx] = updated;
    sel.options[sel.selectedIndex].text = `${updated.nombre}${updated.bodega_siesa_id ? ` (${updated.bodega_siesa_id})` : ''}`;
    mostrarConfigBodega();
    alerta(`Bodega actualizada → ${bodega || '(sin bodega)'}`, 'exito');
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

/**
 * Switch the inventory sub-tab.
 * @param {string} nombre - 'conteos', 'abc', 'datos', 'definitivo' o 'estadisticas'.
 */
function invSubtab(nombre) {
  _INV_SUBTAB = nombre;
  const tabs = { conteos: 'inv-tab-conteos', abc: 'inv-tab-abc', datos: 'inv-tab-datos', definitivo: 'inv-tab-definitivo', estadisticas: 'inv-tab-estadisticas' };
  const panels = { conteos: 'inv-panel-conteos', abc: 'inv-panel-abc', datos: 'inv-panel-datos', definitivo: 'inv-panel-definitivo', estadisticas: 'inv-panel-estadisticas' };
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
  else if (nombre === 'definitivo') cargarConteoDefinitivos();
  else if (nombre === 'estadisticas') conteoEstIniciar();
}

/** Refresca el contador de la pestaña "Definitivo" sin cambiar de subtab —
 * llamado desde el refresco general del dashboard admin. */
async function actualizarBadgeDefinitivos() {
  const badge = document.getElementById('inv-tab-definitivo-badge');
  if (!badge) return;
  try {
    const d = await get('/api/conteo/definitivos');
    const n = d.total || 0;
    badge.textContent = n;
    badge.style.display = n > 0 ? 'inline' : 'none';
  } catch (_) { /* silencioso — no es crítico */ }
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
 * ¿CC2 confirma a CC1? La MISMA regla que `ConteoService.conteos_coinciden`:
 * por diferencia contra la foto de Siesa de cada uno cuando los dos la tienen
 * (entre CC1 y CC2 la tienda sigue vendiendo: el físico baja y el POS sube
 * igual), por cantidad física cuando no.
 */
function _conteosCoinciden(s, hijo) {
  if (!hijo) return false;
  if (s.teorico_siesa != null && hijo.teorico_siesa != null) {
    return hijo.diferencia != null && hijo.diferencia === s.diferencia;
  }
  return hijo.cantidad_fisica != null && hijo.cantidad_fisica === s.cantidad_fisica;
}

/**
 * Línea bajo la cifra de Siesa: el teórico contra el que se contó cuando hay
 * venta POS pendiente — la diferencia (Δ) es contra ese número, no contra la
 * existencia de arriba.
 */
function _fotoSiesaHtml(s) {
  if (!s.cant_pos_siesa) return '';
  return `<div style="font-size:9px;color:#FBBF24;margin-top:2px;">POS ${esc(s.cant_pos_siesa)} → teórico ${esc(s.teorico_siesa)}</div>`;
}

/** Por qué el ajuste no se puede aprobar — el texto lo escribe el servicio
 *  (`ConteoService.motivo_bloqueo_ajuste`), la pantalla no decide nada. */
function _bloqueoAjusteHtml(s) {
  if (s.estado !== 'DESCUADRE' || !s.bloqueo_ajuste) return '';
  return `<div style="font-size:11px;color:#F87171;border-left:3px solid #F87171;padding:4px 8px;margin-bottom:10px;">⛔ ${esc(s.bloqueo_ajuste)}</div>`;
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
  const coinciden = !cc2Pendiente && _conteosCoinciden(s, hijo);
  const bordColor = s.estado === 'DESCUADRE' ? '#7F1D1D' : s.estado === 'TERCER_CONTEO' ? '#7F4010' : '#164F5A';
  const badgeColor = s.estado === 'DESCUADRE' ? '#7F1D1D' : s.estado === 'TERCER_CONTEO' ? '#92400E' : '#1E8395';
  const dif = s.diferencia != null ? (s.diferencia > 0 ? `+${esc(s.diferencia)}` : `${s.diferencia}`) : '?';
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
           <div style="font-size:9px;color:#415A70;margin-top:2px;">${hijo.operario_nombre || (hijo.operario_id ? `Op #${esc(hijo.operario_id)}` : '—')}</div>`
        : `<div style="font-size:16px;color:#415A70;padding:2px 0;">⏳</div>
           <div style="font-size:9px;color:#415A70;margin-top:2px;">${hijo ? (hijo.operario_nombre || (hijo.operario_id ? `Op #${esc(hijo.operario_id)}` : 'asignado')) : 'sin asignar'}</div>`}`;
  }

  return `<div style="background:#121C26;border:1px solid ${bordColor};border-radius:12px;padding:14px;margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:700;">${esc(s.producto_codigo || '—')}${_tipoTag(s)}${s.clasificacion_abc ? `<span style="background:#1C2B3A;color:#FBBF24;font-size:9px;font-weight:700;padding:1px 5px;border-radius:6px;margin-left:4px;">ABC-${esc(s.clasificacion_abc)}</span>` : ''}</div>
        <div style="font-size:11px;color:#415A70;margin-top:1px;">${esc(s.producto_nombre || '')}</div>
        <div style="font-size:11px;color:#415A70;margin-top:1px;">📍 ${esc(s.ubicacion_codigo || '—')}</div>
      </div>
      <span style="background:${badgeColor};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;white-space:nowrap;flex-shrink:0;margin-left:8px;">${esc(s.estado)}</span>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;background:#0B1117;border-radius:8px;padding:10px;margin-bottom:10px;text-align:center;">
      <div>
        <div style="font-size:9px;color:#415A70;font-weight:700;text-transform:uppercase;margin-bottom:3px;">Siesa</div>
        <div style="font-size:20px;font-weight:800;color:#60A5FA;line-height:1;">${s.existencia_siesa != null ? s.existencia_siesa : '—'}</div>
        <div style="font-size:9px;color:#415A70;margin-top:2px;">${esc(s.bodega_siesa_id || 'stock')}</div>
        ${_fotoSiesaHtml(s)}
      </div>
      <div style="border-left:1px solid #1C2B3A;border-right:1px solid #1C2B3A;">
        <div style="font-size:9px;color:#415A70;font-weight:700;text-transform:uppercase;margin-bottom:3px;">1er Conteo</div>
        <div style="font-size:20px;font-weight:800;color:#FBBF24;line-height:1;">${s.cantidad_fisica != null ? s.cantidad_fisica : '—'}</div>
        <div style="font-size:9px;color:#415A70;margin-top:2px;">${s.operario_id ? `Op #${esc(s.operario_id)}` : '—'}</div>
      </div>
      <div>${col3Html}</div>
    </div>

    <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#415A70;margin-bottom:10px;">
      <span>Δ <span style="color:${difCol};font-weight:700;">${dif} uds</span>${s.motivo_codigo ? ` · <span style="color:${s.motivo_codigo==='AJ-ENT'?'#22C55E':'#F87171'};">${esc(s.motivo_codigo)}</span>` : ''}</span>
      ${s.estado === 'DESCUADRE' && coinciden
        ? `<span style="color:#22C55E;font-size:10px;">✓ CC1==CC2 confirmado</span>`
        : s.estado === 'DESCUADRE' && !coinciden && hijo && !cc2Pendiente
          ? `<span style="color:#FBBF24;font-size:10px;">✓ CC3 definitivo</span>`
          : ''}
    </div>
    ${_bloqueoAjusteHtml(s)}

    <div style="display:flex;gap:6px;flex-wrap:wrap;">
      ${s.estado !== 'AJUSTADO' && s.estado !== 'AJUSTANDO'
        ? `<button onclick="conteoAbrirEdicion(${JSON.stringify(s).replace(/"/g,'&quot;')})"
             style="padding:8px 10px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:8px;font-size:12px;cursor:pointer;">✏</button>`
        : ''}
      ${mostrarOmitir
        ? `<button onclick="conteoOmitirSegundo(${esc(s.id)})"
             title="Saltar CC2/CC3 y mover a DESCUADRE para revisión admin"
             style="padding:8px 10px;background:none;border:1px solid #415A70;color:#415A70;border-radius:8px;font-size:11px;cursor:pointer;white-space:nowrap;">Omitir CC2</button>`
        : ''}
      <button onclick="${puedeAjustar ? `conteoAbrirAjuste(${JSON.stringify(s).replace(/"/g,'&quot;')})` : 'void(0)'}"
        ${!puedeAjustar ? 'disabled' : ''}
        style="flex:2;padding:8px;background:${puedeAjustar?'#1E8395':'var(--bg-input)'};color:${puedeAjustar?'#fff':'var(--tx3)'};border:${puedeAjustar?'none':'1px solid var(--brd)'};border-radius:8px;font-size:12px;font-weight:700;cursor:${puedeAjustar?'pointer':'not-allowed'};min-width:120px;">
        ${btnTexto}
      </button>
      <button onclick="conteoCancelar(${esc(s.id)})" style="padding:8px;background:none;border:1px solid #7F1D1D;color:#F87171;border-radius:8px;font-size:11px;cursor:pointer;">✕</button>
    </div>
    ${s.editado_en ? `<div style="font-size:10px;color:#415A70;margin-top:6px;">✏ Editado: ${esc(s.motivo_edicion)}</div>` : ''}
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
        <div style="font-size:12px;font-weight:700;">${esc(s.producto_codigo || '—')}${_tipoTag(s)}</div>
        <div style="font-size:11px;color:#415A70;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.producto_nombre || '')}</div>
        <div style="font-size:11px;color:#415A70;margin-top:2px;">📍 ${esc(s.ubicacion_codigo || '—')}${s.operario_id ? ` · 👤 Op #${esc(s.operario_id)}` : ''}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0;margin-left:8px;">
        ${s.clasificacion_abc ? `<span style="background:#1C2B3A;color:#FBBF24;font-size:9px;font-weight:700;padding:1px 5px;border-radius:6px;">ABC-${esc(s.clasificacion_abc)}</span>` : ''}
        <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;">${esc(s.estado)}</span>
        <div style="display:flex;gap:4px;">
          <button onclick="conteoAbrirEdicion(${JSON.stringify(s).replace(/"/g,'&quot;')})"
            title="Reasignar operario / corregir conteo"
            style="background:var(--bg-input);border:1px solid var(--brd);color:var(--tx2);font-size:9px;padding:1px 6px;border-radius:6px;cursor:pointer;">✏</button>
          <button onclick="conteoCancelar(${esc(s.id)})" style="background:none;border:1px solid #7F1D1D;color:#F87171;font-size:9px;padding:1px 6px;border-radius:6px;cursor:pointer;">Cancelar</button>
        </div>
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
  const dif = s.diferencia != null ? (s.diferencia > 0 ? `+${esc(s.diferencia)}` : `${s.diferencia}`) : null;
  const difCol = (s.diferencia || 0) > 0 ? '#22C55E' : (s.diferencia || 0) < 0 ? '#F87171' : '#415A70';
  return `<div style="background:#0B1117;border:1px solid #1C2B3A;border-radius:10px;padding:12px;margin-bottom:6px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:12px;font-weight:700;color:#415A70;">${esc(s.producto_codigo || '—')}</div>
        <div style="font-size:11px;color:#415A70;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(s.producto_nombre || '')}</div>
        <div style="font-size:11px;color:#415A70;margin-top:1px;">📍 ${esc(s.ubicacion_codigo || '—')}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0;margin-left:8px;">
        <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;">${esc(s.estado)}</span>
        ${dif ? `<span style="color:${difCol};font-size:11px;font-weight:700;">Δ ${dif}</span>` : ''}
      </div>
    </div>
    ${s.aprobador_nombre ? `<div style="font-size:10px;color:#415A70;margin-top:4px;">✓ ${esc(s.aprobador_nombre)}</div>` : ''}
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
    const d = await get('/api/conteo/stats?' + qs);
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
  if (!await _modalConfirmar('¿Re-encolar todos los ajustes fallidos para reintentar con Siesa?', { titulo: 'Reintentar fallos' })) return;
  try {
    const d = await post('/api/conteo/reintentar-fallos', {});
    alerta(`${d.reencolados} ajustes re-encolados`, 'exito');
    await cargarConteoStats();
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

/**
 * Discard failed DLQ jobs, showing the real plan first.
 *
 * El confirm anterior prometía "las sesiones atascadas vuelven a DESCUADRE" y
 * eso solo es cierto para una parte. Una sesión AJUSTANDO cuyo ajuste ya pudo
 * haber llegado a Siesa no se resetea —resetearla arriesga un doble ajuste de
 * inventario— y tampoco la recoge el barrido: se queda trabada y sin job.
 * Prometer lo que no pasa es peor que no prometer nada, porque nadie va a ir a
 * buscar esas sesiones.
 */
async function conteoDescartarFallos() {
  let plan;
  try {
    plan = await get('/api/conteo/descartar-fallos/preview');
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); return; }

  if (!plan.jobs_fallidos) { alerta('No hay ajustes fallidos que descartar', 'info'); return; }

  const etiquetas = {
    reset_a_descuadre: 'vuelven a DESCUADRE (podés re-aprobar o cancelar)',
    sin_tocar_ya_ajustada: 'ya estaban AJUSTADAS — no se tocan',
    sin_tocar_otro_estado: 'están en otro estado — no se tocan',
    QUEDA_HUERFANA: '⚠ QUEDAN TRABADAS en AJUSTANDO, sin nadie que las recoja',
    job_sin_sesion_en_payload: 'sin sesión en el payload',
    sesion_no_existe: 'su sesión ya no está en la base'
  };
  const detalle = Object.entries(plan.resumen)
    .map(([k, n]) => `  · ${n} ${etiquetas[k] || k}`)
    .join('\n');

  let texto = `Descartar ${plan.jobs_fallidos} ajuste(s) fallido(s)?\n\n${detalle}\n\n`
    + 'Descartar NO los envía a Siesa: los marca como abandonados.';
  if (plan.huerfanas && plan.huerfanas.length) {
    texto += `\n\n⚠ Sesiones ${plan.huerfanas.join(', ')} quedan trabadas. `
      + 'Verificá en Siesa si el ajuste llegó ANTES de descartar.';
  }
  if (!await _modalConfirmar(texto, { titulo: 'Descartar ajustes fallidos', peligro: true })) return;

  try {
    const d = await post('/api/conteo/descartar-fallos', {});
    const aviso = (d.huerfanas && d.huerfanas.length)
      ? ` · ${d.huerfanas.length} trabada(s): ${d.huerfanas.join(', ')}`
      : '';
    alerta(`${d.descartados} descartados · ${d.sesiones_reset} a DESCUADRE${aviso}`,
           aviso ? 'advertencia' : 'exito');
    await cargarConteoStats();
    await cargarConteos(_CONTEO_PAGE);
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

/**
 * Skip the pending CC2/CC3 and move the session to DESCUADRE for admin review.
 * @param {number} id - Conteo session ID.
 */
async function conteoOmitirSegundo(id) {
  if (!await _modalConfirmar('¿Omitir el CC2/CC3 pendiente y mover esta sesión a DESCUADRE para revisión?\n\nEl conteo pendiente se cancelará. Podrás aprobar o rechazar el ajuste manualmente.', { titulo: 'Omitir segundo conteo' })) return;
  try {
    await post('/api/conteo/' + id + '/omitir-segundo', {});
    alerta('Sesión movida a DESCUADRE — revisa y confirma el ajuste', 'exito');
    await cargarConteoStats();
    await cargarConteos(_CONTEO_PAGE);
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

/** Export conteo sessions to a CSV file with optional date range filter. */
async function conteoExportar() {
  const almId = document.getElementById('inv-abc-almacen')?.value;
  const desde = (await _modalTexto('Exportar conteos', 'Desde (YYYY-MM-DD, vacío = todo):', { obligatorio: false })) || '';
  const hasta = (await _modalTexto('Exportar conteos', 'Hasta (YYYY-MM-DD, vacío = hoy):', { obligatorio: false })) || '';
  const qs = new URLSearchParams();
  if (desde) qs.set('desde', desde);
  if (hasta) qs.set('hasta', hasta);
  if (almId) qs.set('almacen_id', almId);
  try {
    const r = await _fetchConTimeout('/api/conteo/exportar?' + qs);
    if (!r.ok) { alerta('Error al exportar', 'error'); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `conteos_${desde || 'all'}_${hasta || 'all'}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alerta(e.message || 'Error de conexión', 'error');
  }
}

let _CONTEO_OPERARIOS = [];

/** Carga (una vez) la lista de operarios activos — usada por el panel de
 * asignación en lote y por el selector de "forzar operario" del conteo manual. */
async function _cargarOperariosConteo() {
  if (_CONTEO_OPERARIOS.length > 0) return _CONTEO_OPERARIOS;
  try {
    const todos = await get('/api/auth/usuarios');
    _CONTEO_OPERARIOS = (todos.usuarios || todos || []).filter(u =>
      u.activo && ['operario', 'jefe_almacen'].includes(u.rol)
    );
  } catch (e) { /* silencioso */ }
  return _CONTEO_OPERARIOS;
}

/** Show the batch-assign panel and load available operarios. */
async function conteoMostrarAsignar() {
  const panel = document.getElementById('conteo-asignar-panel');
  if (!panel) return;
  const operarios = await _cargarOperariosConteo();
  const sel = document.getElementById('conteo-asignar-operario');
  if (sel) {
    sel.innerHTML = operarios.map(u =>
      `<option value="${esc(u.id)}">${u.nombre || u.usuario} (${esc(u.rol)})</option>`
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
    const d = await post('/api/conteo/asignar-lote', { operario_id: parseInt(operarioId), almacen_id: almId ? parseInt(almId) : null, limite });
    conteoCerrarAsignar();
    alerta(`${d.asignadas} tareas asignadas a ${d.operario_nombre}`, 'exito');
    await cargarConteoStats();
    await cargarConteos(_CONTEO_PAGE);
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

/**
 * Cancel a conteo session with a required reason prompt.
 * @param {number} id - Conteo session ID.
 */
async function conteoCancelar(id) {
  const motivo = await _modalTexto('Cancelar conteo', 'Motivo de cancelación:');
  if (!motivo) return;
  try {
    await put(`/api/conteo/${id}/cancelar`, { motivo: motivo.trim() });
    alerta('Conteo cancelado', 'advertencia');
    await cargarConteoStats();
    await cargarConteos(_CONTEO_PAGE);
  } catch (e) { alerta(e.message || 'Error al cancelar', 'error'); }
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

    const d = await get('/api/conteo/?' + qs);
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

/** Show the manual conteo creation form, load operarios, and focus the code input. */
async function conteosMostrarFormManual() {
  document.getElementById('conteo-form-manual').style.display = 'block';
  const aviso = document.getElementById('conteo-manual-aviso-pos');
  if (aviso) aviso.innerHTML = avisoCajasPosHtml();
  const operarios = await _cargarOperariosConteo();
  const selOp = document.getElementById('conteo-manual-operario');
  if (selOp) {
    selOp.innerHTML = '<option value="">Auto-asignar (el que lo tome primero)</option>' +
      operarios.map(u => `<option value="${esc(u.id)}">${esc(u.nombre || u.usuario)} (${esc(u.rol)})</option>`).join('');
  }
  document.getElementById('conteo-manual-codigo').focus();
}
/** Hide the manual conteo form and clear its inputs. */
function conteosOcultarFormManual() {
  document.getElementById('conteo-form-manual').style.display = 'none';
  document.getElementById('conteo-manual-codigo').value = '';
  document.getElementById('conteo-manual-error').textContent = '';
  conteoManualOcultarSugerencias();
}

let _CONTEO_MANUAL_BUSCAR_TIMER = null;

/** Escapa texto para insertarlo como HTML — evita que un nombre de
 * producto con `<`, `>`, `&`, comillas, etc. rompa el marcado o inyecte. */
function _conteoManualEscapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

/** Autocompletado del campo "Código producto" en Crear conteo manual —
 * busca por código, nombre O código de barras (GET /api/productos/ ya
 * soporta los tres, mismo endpoint que usa el catálogo general). Elegir
 * una sugerencia rellena el campo con la referencia real; seguir
 * escribiendo un código exacto sin elegir nada sigue funcionando igual
 * que antes (crearConteoManual lo resuelve server-side). */
function conteoManualBuscarProducto(valor) {
  clearTimeout(_CONTEO_MANUAL_BUSCAR_TIMER);
  const q = (valor || '').trim();
  const box = document.getElementById('conteo-manual-sugerencias');
  if (!box) return;
  if (q.length < 2) { box.style.display = 'none'; box.innerHTML = ''; return; }

  _CONTEO_MANUAL_BUSCAR_TIMER = setTimeout(async () => {
    let productos = [];
    try {
      const d = await get('/api/productos/?q=' + encodeURIComponent(q) + '&per_page=8');
      productos = d.productos || [];
    } catch (e) {
      box.style.display = 'none';
      return;
    }
    if (!productos.length) {
      box.innerHTML = '<div style="padding:10px 12px;font-size:13px;color:var(--tx3);">Sin resultados</div>';
      box.style.display = 'block';
      return;
    }
    box.innerHTML = productos.map(p => {
      const codigo = _conteoManualEscapeHtml(p.codigo);
      const nombre = _conteoManualEscapeHtml(p.nombre || '');
      const barras = p.codigo_barras ? ' · ' + _conteoManualEscapeHtml(p.codigo_barras) : '';
      return `
        <div onclick="conteoManualElegirProducto(this.dataset.codigo)" data-codigo="${codigo}"
          style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--brd);font-size:13px;"
          onmouseover="this.style.background='var(--bg-input)'" onmouseout="this.style.background=''">
          <div style="font-weight:700;color:var(--tx);">${codigo}${barras}</div>
          <div style="color:var(--tx3);font-size:12px;">${nombre}</div>
        </div>`;
    }).join('');
    box.style.display = 'block';
  }, 250);
}

/** Selecciona una sugerencia — rellena el campo con la referencia real del producto. */
function conteoManualElegirProducto(codigo) {
  const input = document.getElementById('conteo-manual-codigo');
  if (input) input.value = codigo;
  conteoManualOcultarSugerencias();
}

function conteoManualOcultarSugerencias() {
  const box = document.getElementById('conteo-manual-sugerencias');
  if (box) { box.style.display = 'none'; box.innerHTML = ''; }
}
/** Create a manual conteo task for a specific product code and almacen —
 * opcionalmente forzando a qué operario le cae el primer conteo (CC1). */
async function crearConteoManual() {
  const almacenId = document.getElementById('conteo-manual-almacen')?.value;
  const codigo = document.getElementById('conteo-manual-codigo')?.value.trim().toUpperCase();
  const operarioId = document.getElementById('conteo-manual-operario')?.value;
  const errorEl = document.getElementById('conteo-manual-error');
  errorEl.textContent = '';
  if (!codigo) { errorEl.textContent = 'Ingresa el código del producto'; return; }
  if (!almacenId) { errorEl.textContent = 'Selecciona un almacén'; return; }
  try {
    const body = { almacen_id: parseInt(almacenId), producto_codigo: codigo };
    if (operarioId) body.operario_id = parseInt(operarioId);
    const d = await post('/api/conteo/manual', body);
    if (d.tareas_creadas === 0) {
      errorEl.textContent = d.omitidas_ya_activas > 0 ? 'Ya existe un conteo activo para este producto' : 'Producto sin stock en este almacén';
    } else {
      const destino = d.operario_nombre ? ` — asignado a ${d.operario_nombre}` : '';
      alerta(`Conteo creado para ${d.producto_nombre || codigo}${destino}`, 'exito');
      conteosOcultarFormManual();
      await cargarConteos(1);
    }
  } catch (e) { errorEl.textContent = e.message || 'Error de conexión'; }
}

/**
 * Fetch and render the ABC classification summary for the selected almacen.
 *
 * Los intervalos, el cupo y los umbrales del watchdog salen del servidor
 * (`conteo_politica`): antes esta pantalla decía «cada 15 / 90 / 180 días»
 * escrito a mano, el resumen decía «semanal / mensual / trimestral» y el
 * generador usaba otra cosa. Una sola fuente, pintada donde se muestra.
 */
async function cargarResumenAbc() {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) return;
  const resumenEl = document.getElementById('inv-abc-resumen');
  if (!resumenEl) return;
  resumenEl.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';
  try {
    const d = await get(`/api/conteo/abc/resumen?almacen_id=${encodeURIComponent(almacenId)}`);
    resumenEl.innerHTML = _abcResumenHtml(d);
    _abcPintarEtiquetas(d);
  } catch (e) {
    resumenEl.innerHTML = '<div style="color:#ef4444;font-size:12px;">Error cargando resumen</div>';
  }
}

/** HTML del resumen ABC + el plan vigente (cupo, pendientes, lo que haría hoy). */
function _abcResumenHtml(d) {
  const dist = d.distribucion_abc || {};
  const plan = d.plan || {};
  const items = [
    { clase: 'A', col: '#4ade80', bg: '#1e3a1e', border: '#166534' },
    { clase: 'B', col: '#60a5fa', bg: '#1e2a3a', border: '#1e40af' },
    { clase: 'C', col: '#f87171', bg: '#2a1e1e', border: '#7f1d1d' },
  ];
  const tarjetas = items.map(it => {
    const c = dist[it.clase] || {};
    return `
      <div style="background:${esc(it.bg)};border:1px solid ${esc(it.border)};border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:22px;font-weight:900;color:${esc(it.col)};">${esc(c.total_productos ?? '—')}</div>
        <div style="font-size:11px;font-weight:700;color:${esc(it.col)};">Clase ${esc(it.clase)}</div>
        <div style="font-size:10px;color:#666;margin-top:2px;">${esc(c.descripcion || '')}</div>
      </div>`;
  }).join('');
  const dias = plan.dias_de_cupo_pendientes;
  const lineaPlan = `
    <div style="background:#0a0a0a;border:1px solid #222;border-radius:10px;padding:10px;font-size:12px;color:#aaa;line-height:1.6;margin-bottom:8px;">
      Cupo diario: <b style="color:#fff;">${esc(plan.cupo_diario ?? '—')}</b> conteos ·
      pendientes sin contar: <b style="color:#fff;">${esc(plan.pendientes_vivas ?? '—')}</b>${dias === null || dias === undefined ? '' : ` (${esc(dias)} días de cupo)`}<br>
      El generador corre a las ${esc(plan.hora_del_generador || '')} y hoy crearía <b style="color:#fff;">${esc(plan.generaria_hoy ?? 0)}</b>.
      ${plan.mensaje ? `<br><span style="color:#fbbf24;">${esc(plan.mensaje)}</span>` : ''}
      ${(plan.advertencias || []).map(a => `<br><span style="color:#ef4444;">⚠ ${esc(a)}</span>`).join('')}
    </div>`;
  return `
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:8px;">${tarjetas}</div>
    ${lineaPlan}
    <div style="font-size:11px;color:#555;text-align:right;">Fuente: ${esc(d.fuente || 'WMS')}</div>`;
}

/** Pone en los botones y en la nota del watchdog los números vigentes del plan. */
function _abcPintarEtiquetas(d) {
  const dist = d.distribucion_abc || {};
  const plan = d.plan || {};
  ['A', 'B', 'C'].forEach(cl => {
    const el = document.getElementById(`inv-abc-int-${cl}`);
    if (el && dist[cl]) el.textContent = `cada ${dist[cl].intervalo_dias} días`;
  });
  const wd = document.getElementById('inv-abc-watchdog-nota');
  if (wd && plan.watchdog_umbral_picks) {
    const u = plan.watchdog_umbral_picks;
    wd.textContent = `Productos B/C con alta rotación real (≥${u.C} picks en ${plan.watchdog_ventana_dias} días para C, `
      + `≥${u.B} para B) reciben un conteo inmediato — dentro del cupo del día, y nunca sobre un producto `
      + `contado hace menos de ${plan.watchdog_dias_sin_reabrir} días.`;
  }
}

/**
 * Generate conteo tasks for a single ABC class, within the almacen's daily cupo.
 * @param {string} clase - ABC class: 'A', 'B', or 'C'.
 */
async function generarAbc(clase) {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) { alerta('Selecciona un almacén primero', 'error'); return; }
  const res = document.getElementById('inv-abc-resultado');
  if (res) res.textContent = 'Generando...';
  try {
    const d = await post('/api/conteo/abc/generar-tareas', { almacen_id: parseInt(almacenId), clasificacion: clase });
    const msg = `Clase ${clase}: ${d.mensaje}`;
    if (res) res.textContent = msg;
    alerta(msg, d.tareas_creadas > 0 ? 'exito' : 'advertencia');
    await cargarResumenAbc();
    await cargarConteos(1);
  } catch (e) {
    if (res) res.textContent = '';
    alerta(e.message || 'Error de conexión', 'error');
  }
}

/**
 * Generate the day's batch (watchdog + A+B+C) within the almacen's daily cupo.
 * @param {boolean} [adelantar=false] - Also consider products still up to date
 *   (most overdue first). Never more than the cupo.
 */
async function generarTodasClases(adelantar = false) {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) { alerta('Selecciona un almacén primero', 'error'); return; }
  if (adelantar && !await _modalConfirmar('¿Adelantar conteos? Además de los vencidos, toma productos que todavía están al día (los más atrasados primero).\n\nNunca pasa del cupo diario del almacén.', { titulo: 'Adelantar conteos' })) return;
  const res = document.getElementById('inv-abc-resultado');
  if (res) res.textContent = adelantar ? 'Adelantando...' : 'Generando lote del día...';
  try {
    const d = await post('/api/conteo/abc/generar-todas', { almacen_id: parseInt(almacenId), adelantar });
    const watchdog = d.por_clase?.watchdog;
    const wdMsg = watchdog?.overrides > 0 ? ` · 🤖 ${watchdog.overrides} por watchdog` : '';
    const msg = `${d.mensaje}${wdMsg}`;
    if (res) res.textContent = msg;
    alerta(msg, d.total_tareas_creadas > 0 ? 'exito' : 'advertencia');
    await cargarResumenAbc();
    await cargarConteos(1);
  } catch (e) {
    if (res) res.textContent = '';
    alerta(e.message || 'Error de conexión', 'error');
  }
}

const _ETIQUETAS_NO_SE_TOCAN = {
  verificacion_cc2_cc3: 'segundos/terceros conteos (su cadena sigue viva)',
  auditoria_por_faltante: 'auditorías por faltante de picking',
  conteo_manual: 'conteos manuales',
  en_proceso: 'conteos que alguien está contando ahora',
  asignada_a_un_operario: 'conteos ya asignados a un operario',
  con_cadena_iniciada: 'conteos con la cadena ya iniciada',
};

/** Texto (ya escapado) de la vista previa de «Limpiar cola». */
function _limpiarColaPreviewHtml(plan, etiqueta) {
  const lista = (obj, fmt) => Object.entries(obj || {})
    .map(([k, n]) => `  · ${esc(n)} ${esc(fmt(k))}`).join('\n');
  let t = `Se cancelan ${esc(plan.a_cancelar)} conteo(s) del plan (${esc(etiqueta)}) que nadie tomó.\n\n`
    + `Por clase:\n${lista(plan.por_clase, k => 'clase ' + k)}\n\n`
    + `Por antigüedad:\n${lista(plan.por_antiguedad_dias, k => 'de ' + k + ' días')}\n`;
  const quedan = lista(plan.no_se_tocan, k => _ETIQUETAS_NO_SE_TOCAN[k] || k);
  if (quedan) t += `\nNO se tocan:\n${quedan}\n`;
  return t + '\nNo se borra nada: quedan CANCELADAS, con tu nombre y el motivo.';
}

/**
 * Cancel (not delete) the plan's backlog: shows the preview first, asks for a
 * reason, and confirms exactly what was previewed.
 */
async function limpiarPendientesAbc() {
  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  if (!almacenId) { alerta('Selecciona un almacén primero', 'error'); return; }
  const clase = await _modalTexto('Limpiar cola', 'Qué clase limpiar? Escribe A, B, C o deja vacío para TODAS:', { obligatorio: false });
  if (clase === null) return; // canceló
  const claseUpper = clase.trim().toUpperCase();
  if (claseUpper && !['A','B','C'].includes(claseUpper)) {
    alerta('Clase inválida. Usa A, B, C o deja vacío.', 'error'); return;
  }
  const etiqueta = claseUpper ? `clase ${claseUpper}` : 'todas las clases';
  let plan;
  try {
    plan = await get(`/api/conteo/abc/limpiar-pendientes/preview?almacen_id=${encodeURIComponent(almacenId)}&clasificacion=${encodeURIComponent(claseUpper)}`);
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); return; }
  if (!plan.a_cancelar) { alerta(`No hay conteos del plan sin tomar en ${etiqueta}`, 'info'); return; }

  const motivo = await _modalTexto('Motivo de la cancelación',
    _limpiarColaPreviewHtml(plan, etiqueta).replace(/\n/g, '<br>')
    + '<br><br>¿Por qué se cancela? (queda registrado en cada conteo)', { obligatorio: true });
  if (motivo === null || !motivo.trim()) return;
  if (!await _modalConfirmar(`¿Cancelar ${esc(plan.a_cancelar)} conteo(s) de ${esc(etiqueta)}?\n\nMotivo: ${esc(motivo.trim())}`, { titulo: 'Cancelar rezago', peligro: true })) return;
  try {
    const d = await post('/api/conteo/abc/limpiar-pendientes', {
      almacen_id: parseInt(almacenId), clasificacion: claseUpper || null,
      motivo: motivo.trim(), esperadas: plan.a_cancelar,
    });
    alerta(`${d.canceladas} conteo(s) cancelados (${etiqueta})`, 'exito');
    await cargarResumenAbc();
    await cargarConteos(1);
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
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
        <b>${esc(s.producto_codigo || '—')}</b> · ${esc(s.producto_nombre || '')}<br>
        📍 ${s.ubicacion_codigo || s.ubicacion_id || '—'}${s.clasificacion_abc ? ` · ABC-${esc(s.clasificacion_abc)}` : ''}<br>
        <span style="display:inline-flex;gap:12px;margin-top:4px;">
          ${s.existencia_siesa != null ? `<span>Siesa <b style="color:#60a5fa;">${esc(s.existencia_siesa)}</b></span>` : '<span style="color:#374151;">Sin ref. Siesa</span>'}
          ${s.cantidad_fisica != null ? `<span>1er conteo <b style="color:#f59e0b;">${esc(s.cantidad_fisica)}</b></span>` : ''}
          ${hijo?.cantidad_fisica != null ? `<span>2do conteo <b style="color:${hijo.cantidad_fisica===s.cantidad_fisica?'#4ade80':'#f87171'};">${esc(hijo.cantidad_fisica)}</b></span>` : ''}
        </span>
        ${s.editado_en ? `<br><span style="color:#f59e0b;">Última edición: ${esc(s.motivo_edicion)}</span>` : ''}
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
    const d = await put(`/api/conteo/${_CONTEO_EDICION_ID}/editar`, body);
    alerta('Conteo actualizado · ' + (d.cambios || []).join(', '), 'exito');
    conteosCerrarEdicion();
    await cargarConteos(_CONTEO_PAGE);
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
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
  label.innerHTML = `⏳ Procesando ${esc(archivo.name)}... <input type="file" id="abc-csv-input" accept=".csv,.xlsx,.xls,.txt" style="display:none;" onchange="subirCsvAbc(this)">`;
  res.style.color = '#888';
  res.textContent = 'Subiendo archivo...';

  const almacenId = document.getElementById('inv-abc-almacen')?.value;
  const form = new FormData();
  form.append('archivo', archivo);
  if (almacenId) form.append('almacen_id', almacenId);

  try {
    const r = await _fetchConTimeout('/api/conteo/abc/cargar-csv', { method: 'POST', body: form }, 60000);
    const d = await r.json();

    if (r.ok) {
      const dist = d.distribucion || {};
      const msg = `✓ ${d.actualizados} productos actualizados · A:${dist.A||0} B:${dist.B||0} C:${dist.C||0}`;
      res.style.color = '#4ade80';
      res.textContent = msg;
      label.style.borderColor = '#166534';
      label.style.color = '#4ade80';
      label.innerHTML = `✓ ${esc(archivo.name)} cargado <input type="file" id="abc-csv-input" accept=".csv,.xlsx,.xls,.txt" style="display:none;" onchange="subirCsvAbc(this)">`;

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
    res.textContent = '✗ ' + (e.message || 'Error de conexión');
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
    const d = await post('/api/conteo/abc/watchdog', { almacen_id: parseInt(almacenId) });
    let msg = d.overrides > 0
      ? `🤖 Watchdog: ${d.overrides} producto(s) con rotación anómala → conteo inmediato`
      : '🤖 Watchdog: sin anomalías nuevas';
    if (d.omitidos_por_cupo > 0) msg += ` · ${d.omitidos_por_cupo} sin crear por cupo (${d.cupo?.mensaje || 'cupo agotado'})`;
    if (res) res.textContent = msg;
    alerta(msg, d.overrides > 0 ? 'advertencia' : 'exito');
    if (d.overrides > 0) await cargarConteos();
  } catch (e) {
    if (res) res.textContent = '';
    alerta(e.message || 'Error de conexión', 'error');
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
  const coinciden = _conteosCoinciden(s, hijo);

  const bodega = s.bodega_siesa_id || '—';

  info.innerHTML = `
    <div style="margin-bottom:10px;">
      <div style="font-size:13px;font-weight:700;color:#e2e8f0;">${esc(s.producto_codigo || '—')} · ${esc(s.producto_nombre || '')}</div>
      <div style="font-size:11px;color:#4b5563;margin-top:1px;">📍 ${esc(s.ubicacion_codigo || '—')} · Bodega: <span style="color:#60a5fa;font-weight:700;">${bodega}</span></div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:12px;text-align:center;">
      <div>
        <div style="font-size:9px;color:#4b5563;text-transform:uppercase;margin-bottom:2px;">WMS</div>
        <div style="font-size:18px;font-weight:800;color:#60a5fa;">${s.existencia_siesa ?? '—'}</div>
        ${_fotoSiesaHtml(s)}
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
    ${_bloqueoAjusteHtml(s)}
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
    const d = await put(`/api/conteo/${sesionId}/ajustar`, {});
    conteosCerrarAjuste();
    alerta(`Ajuste ${d.motivo_codigo} encolado a Siesa · Δ ${d.diferencia} uds`, 'exito');
    await cargarConteos(_CONTEO_PAGE);
  } catch (e) {
    alerta(e.message || 'Error de conexión', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Confirmar → SIESA'; }
  }
}


// ══════════════════════════════════════════════════════════════════════════
// CONTEO DEFINITIVO (CC3) — pantalla de supervisor, escaneo + confirmación.
//
// CC1 y CC2 no coincidieron: el CC3 nace SIN operario asignado
// (ConteoService._crear_conteo_verificacion, 2026-09-04) y espera acá.
// Reutiliza el mismo contrato que ya usa picking.js para conteo ciego
// (/api/mobile/escanear, /api/mobile/confirmar con tipo='CONTEO') pero con
// su propia UI aislada — no toca TAREA_ACTUAL ni pedirTarea() de
// picking.js, que pertenecen a la pantalla del operario, no a la del
// supervisor en el panel admin.
// ══════════════════════════════════════════════════════════════════════════

let DEF_TAREA_ACTUAL = null;

/** Carga la cola de conteos definitivos pendientes (CC1≠CC2, sin resolver). */
async function cargarConteoDefinitivos() {
  const el = document.getElementById('inv-definitivo-lista');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Cargando…</div>';
  try {
    const d = await get('/api/conteo/definitivos');
    const pend = d.pendientes || [];
    const badge = document.getElementById('inv-tab-definitivo-badge');
    if (badge) {
      badge.textContent = pend.length;
      badge.style.display = pend.length > 0 ? 'inline' : 'none';
    }
    if (!pend.length) {
      el.innerHTML = '<div style="text-align:center;padding:40px 20px;color:var(--tx3);">Sin conteos definitivos pendientes ✓</div>';
      return;
    }
    el.innerHTML = pend.map(s => `
      <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:14px;margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <span style="font-size:11px;font-weight:700;color:#f59e0b;background:rgba(120,53,15,.35);padding:2px 8px;border-radius:8px;">CC1 ≠ CC2</span>
          <span style="font-size:11px;color:var(--tx3);">${esc(s.almacen_nombre || '')}</span>
        </div>
        <div style="font-size:15px;font-weight:700;color:var(--tx);">${s.producto_nombre || s.producto_codigo || '—'}</div>
        <div style="font-size:13px;color:var(--tx3);">${esc(s.producto_codigo || '')} · Ubicación ${esc(s.ubicacion_codigo || '—')}</div>
        <div style="font-size:11px;color:var(--tx3);margin-top:4px;">${s.operario_nombre ? `En proceso por ${esc(s.operario_nombre)}` : 'Sin asignar — tómalo vos'}</div>
        <button onclick="defAbrirConteo(${esc(s.id)})" style="width:100%;margin-top:10px;padding:12px;background:var(--pm);color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">🎯 Contar ahora</button>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando la cola</div>';
  }
}

/** Abre el modal de conteo definitivo para una sesión CC3 — se auto-asigna al supervisor. */
async function defAbrirConteo(id) {
  try {
    const t = await get(`/api/conteo/${id}/tarea`);
    DEF_TAREA_ACTUAL = {
      id: t.id,
      producto_codigo: t.producto_codigo,
      producto_nombre: t.producto_nombre,
      ubicacion: t.ubicacion_codigo,
      maneja_lote: t.maneja_lote,
      contado: 0,
    };
    document.getElementById('def-modal').style.display = 'flex';
    _defRender();
  } catch (e) {
    alerta('No se pudo abrir el conteo — ' + (e.message || 'error de conexión'), 'error');
  }
}

/** Renderiza el HUD de conteo ciego definitivo (mismo lenguaje visual que picking.js). */
function _defRender() {
  const t = DEF_TAREA_ACTUAL;
  if (!t) return;
  const puedeCamara = OPERARIO && OPERARIO.puede_usar_camara;
  document.getElementById('def-modal-contenido').innerHTML = `
    <div style="background:#78350f;color:#fcd34d;border-radius:12px;padding:10px 16px;font-size:18px;font-weight:700;text-align:center;margin-bottom:16px;">🎯 CONTEO DEFINITIVO</div>

    ${avisoCajasPosHtml()}

    <div style="background:#000;border:1px solid #222;border-radius:16px;padding:20px;margin-bottom:12px;">
      <div style="font-size:13px;color:#666;">UBICACIÓN</div>
      <div style="font-size:44px;font-weight:900;letter-spacing:2px;color:#fff;">${esc(t.ubicacion || '—')}</div>
    </div>

    <div style="background:#111;border-radius:16px;padding:16px;margin-bottom:12px;">
      <div style="font-size:13px;color:#666;">PRODUCTO</div>
      <div style="font-size:20px;font-weight:700;color:#fff;">${esc(t.producto_nombre || '—')}</div>
      <div style="font-size:15px;color:#aaa;font-weight:400;">${esc(t.producto_codigo || '')}</div>
    </div>

    <div style="background:#1a1a1a;border-radius:16px;padding:20px;margin-bottom:12px;text-align:center;">
      <div style="font-size:13px;color:#666;">CONTEO CIEGO — DEFINITIVO</div>
      <div id="def-contador" style="font-size:64px;font-weight:900;color:#fff;">${esc(t.contado)}</div>
      <div style="font-size:13px;color:#555;margin-top:6px;">Rompe el empate entre el 1er y 2do conteo — tampoco los ves</div>
    </div>

    ${puedeCamara ? `
    <button onclick="defAbrirCamara(this)" style="width:100%;padding:14px;font-size:17px;background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;margin-bottom:10px;">
      📷 Escanear con cámara
    </button>
    <div id="def-camara-box" style="display:none;margin-bottom:10px;">
      <div id="def-lector-qr" style="border-radius:12px;overflow:hidden;"></div>
      <button onclick="cerrarCamara('def-camara-box')" style="width:100%;padding:10px;margin-top:6px;font-size:15px;background:#333;color:#fff;border:none;border-radius:10px;cursor:pointer;">Cerrar cámara</button>
    </div>` : ''}

    <button id="def-btn-ok" onclick="defConfirmar()" style="width:100%;padding:20px;font-size:22px;font-weight:700;background:#16a34a;color:#fff;border:none;border-radius:16px;cursor:pointer;margin-bottom:10px;">
      ✓ Confirmar conteo definitivo
    </button>

    <button onclick="defConfirmarManual()" style="width:100%;padding:14px;font-size:15px;font-weight:600;background:#1a2a1a;color:#4ade80;border:1px solid #166534;border-radius:12px;cursor:pointer;margin-bottom:10px;">
      ✓ Confirmar conteo manual
    </button>

    <button onclick="defCerrarModal()" style="width:100%;padding:14px;font-size:15px;font-weight:600;background:#1a1a1a;color:#aaa;border:1px solid #333;border-radius:12px;cursor:pointer;">
      Cerrar sin confirmar
    </button>`;
}

/** Abre la cámara del navegador con su propia caja (aislada de picking.js). */
async function defAbrirCamara(btnEl = null) {
  await abrirCamara('def-lector-qr', 'def-camara-box', defProcesarScan, btnEl);
}

/** Procesa un código escaneado: mismo contrato que picking.js para CONTEO
 * (valida contra el producto en el servidor, incrementa cantidad_fisica). */
async function defProcesarScan(codigo) {
  if (!DEF_TAREA_ACTUAL) return;
  try {
    const r = await postConReintento('/api/mobile/escanear', {
      tarea_id: DEF_TAREA_ACTUAL.id, tipo: 'CONTEO', codigo, cantidad: 1,
    });
    if (r.error) {
      beepError();
      alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error');
      return;
    }
    beepOk();
    DEF_TAREA_ACTUAL.contado = r.cantidad_contada;
    const el = document.getElementById('def-contador');
    if (el) el.textContent = DEF_TAREA_ACTUAL.contado;
  } catch (e) {
    beepError();
    alerta(e.status ? e.message : 'Error de conexión', 'error');
  }
}

/** Confirma sin escáner — el supervisor contó físicamente y escribe el número. */
async function defConfirmarManual() {
  if (!DEF_TAREA_ACTUAL) return;
  const cant = await _modalCantidad('Conteo definitivo', '¿Cuántas unidades contaste físicamente?', { min: 0 });
  if (cant === null) return;
  DEF_TAREA_ACTUAL.contado = cant;
  const el = document.getElementById('def-contador');
  if (el) el.textContent = cant;
  await defConfirmar();
}

/** Envía el conteo definitivo al backend — dispara la propagación a la raíz (CC1). */
async function defConfirmar() {
  if (!DEF_TAREA_ACTUAL) return;
  const btn = document.getElementById('def-btn-ok');
  if (btn) { btn.textContent = 'Confirmando...'; btn.disabled = true; }
  const payload = { tarea_id: DEF_TAREA_ACTUAL.id, tipo: 'CONTEO', items_escaneados: [] };
  try {
    const r = await post('/api/mobile/confirmar', payload);
    if (r.error) {
      alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error');
      if (btn) { btn.textContent = '✓ Confirmar conteo definitivo'; btn.disabled = false; }
      return;
    }
    beepDone();
    _defMostrarResultado(r);
  } catch (e) {
    if (e.status) {
      // Error del servidor (400/500) — mostrar mensaje real, no guardar offline
      alerta(e.message || 'Error al confirmar', 'error');
      if (btn) { btn.textContent = '✓ Confirmar conteo definitivo'; btn.disabled = false; }
    } else {
      // Error de red real — guardar para sincronizar cuando haya WiFi (mismo patrón que picking.js)
      guardarOffline(payload);
      defCerrarModal();
    }
  }
}

/** Muestra el resultado (MATCH con Siesa, o DESCUADRE definitivo pendiente de aprobar). */
function _defMostrarResultado(r) {
  const esMatch = r.resultado === 'MATCH';
  const cont = document.getElementById('def-modal-contenido');
  if (!cont) return;
  if (r.resultado === 'RECONTAR') {
    // Siesa se movió mientras se contaba: el servidor descartó el conteo y
    // dejó el MISMO CC3 abierto, a tu nombre, para contar de nuevo.
    cont.innerHTML = `
      <div style="text-align:center;padding:30px 10px;">
        <div style="font-size:64px;">🔁</div>
        <div style="font-size:22px;font-weight:900;color:#FBBF24;margin-top:10px;">Recontar</div>
        <div style="font-size:14px;color:#aaa;margin-top:8px;line-height:1.5;">${esc(r.mensaje || '')}</div>
        <button onclick="defAbrirConteo(${esc(r.sesion_id)})" style="width:100%;margin-top:20px;padding:16px;font-size:16px;font-weight:700;background:var(--pm);color:#fff;border:none;border-radius:12px;cursor:pointer;">
          Contar de nuevo
        </button>
        <button onclick="defCerrarModal()" style="width:100%;margin-top:12px;padding:14px;font-size:14px;background:#1a1a1a;color:#aaa;border:1px solid #333;border-radius:12px;cursor:pointer;">
          Cerrar
        </button>
      </div>`;
    return;
  }
  cont.innerHTML = `
    <div style="text-align:center;padding:30px 10px;">
      <div style="font-size:64px;">${esMatch ? '✅' : '⚠️'}</div>
      <div style="font-size:22px;font-weight:900;color:${esMatch ? '#22C55E' : '#FBBF24'};margin-top:10px;">
        ${esMatch ? 'Coincide con Siesa' : 'Conteo definitivo registrado'}
      </div>
      <div style="font-size:14px;color:#aaa;margin-top:8px;line-height:1.5;">${esc(r.mensaje || '')}</div>
      ${(!esMatch && r.raiz_id) ? `
      <button onclick="defAprobarAjuste(${esc(r.raiz_id)})" style="width:100%;margin-top:20px;padding:16px;font-size:16px;font-weight:700;background:var(--pm);color:#fff;border:none;border-radius:12px;cursor:pointer;">
        Aprobar ajuste ahora → Siesa
      </button>
      <div style="font-size:11px;color:#666;margin-top:8px;">O revísalo después desde Conteos → Acción</div>` : ''}
      <button onclick="defCerrarModal()" style="width:100%;margin-top:12px;padding:14px;font-size:14px;background:#1a1a1a;color:#aaa;border:1px solid #333;border-radius:12px;cursor:pointer;">
        Cerrar
      </button>
    </div>`;
}

/** Aprueba el ajuste ya mismo (PUT /api/conteo/<raiz_id>/ajustar) — dispara el POST real a Siesa vía DLQ. */
async function defAprobarAjuste(raizId) {
  try {
    const d = await put(`/api/conteo/${raizId}/ajustar`, {});
    alerta(`Ajuste ${d.motivo_codigo || ''} encolado a Siesa`, 'exito');
    defCerrarModal();
  } catch (e) {
    alerta(e.message || 'Error de conexión', 'error');
  }
}

/** Cierra el modal de conteo definitivo y refresca la cola. */
function defCerrarModal() {
  const modal = document.getElementById('def-modal');
  if (modal) modal.style.display = 'none';
  DEF_TAREA_ACTUAL = null;
  cargarConteoDefinitivos();
}


// ── Estadísticas del conteo cíclico ─────────────────────────────────────────
// Pinta `GET /api/conteo/estadisticas`. La pantalla no calcula nada: cada
// número —y cada porcentaje que NO se publica por muestra chica— lo decide
// `app/services/metricas/conteo.py`. Carga y cobertura van primero porque hoy
// es lo único con n suficiente para decir algo. Todo dato pasa por esc().

/** Un número con separador de miles colombiano, ya escapado. */
function _ceNum(v, dec = 0) {
  if (v === null || v === undefined) return '—';
  return esc(Number(v).toLocaleString('es-CO', { maximumFractionDigits: dec }));
}

/** Una métrica {numerador, denominador, porcentaje}: el porcentaje solo si el
 * servidor lo publicó; si no, el n y el porqué. */
function _ceMetrica(m) {
  if (!m) return '—';
  const base = `${_ceNum(m.numerador)} / ${_ceNum(m.denominador)}`;
  if (m.porcentaje === null || m.porcentaje === undefined) {
    return `${base} <span style="color:var(--tx3);font-size:10px;">(${esc(m.sin_porcentaje_por || 'sin porcentaje')})</span>`;
  }
  return `${base} · <b>${_ceNum(m.porcentaje, 1)}%</b>`;
}

/** Los `excluidos` de una métrica, dichos — un cero sin denominador no se esconde. */
function _ceExcluidos(ex) {
  const pares = Object.entries(ex || {});
  if (!pares.length) return '';
  const txt = pares.map(([k, n]) => `${esc(k)}: ${_ceNum(n)}`).join(' · ');
  return `<div style="font-size:10px;color:var(--tx3);margin-top:4px;">Excluidos — ${txt}</div>`;
}

/** Contenedor de un bloque. `titulo` y `cuerpo` llegan ya armados y escapados. */
function _ceTarjeta(titulo, cuerpo) {
  return `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:12px 14px;margin-bottom:12px;">
    <div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">${titulo}</div>${cuerpo}</div>`;
}

/** Los filtros, armados una vez (las fechas por defecto las fija el servidor). */
function _ceFiltrosHtml() {
  const alms = (_INV_ALMACENES || []).map(a =>
    `<option value="${esc(a.id)}">${esc(a.nombre)}</option>`).join('');
  const campo = 'padding:8px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:12px;';
  return `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px;">
    <input id="ce-desde" type="date" style="${campo}flex:1;min-width:130px;">
    <input id="ce-hasta" type="date" style="${campo}flex:1;min-width:130px;">
    <select id="ce-almacen" style="${campo}flex:1;min-width:120px;"><option value="">Todos los almacenes</option>${alms}</select>
    <select id="ce-clase" style="${campo}"><option value="">A+B+C</option><option value="A">A</option><option value="B">B</option><option value="C">C</option></select>
    <select id="ce-tipo" style="${campo}"><option value="">Todos los tipos</option><option value="DIARIO_ABC">Plan ABC</option><option value="MANUAL">Manual</option><option value="WATCHDOG_ABC">Watchdog</option><option value="EXCEPCION_PICKING">Auditoría picking</option></select>
    <button onclick="conteoEstCargar()" style="padding:8px 14px;background:var(--pm);color:#fff;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">Actualizar</button>
  </div>`;
}

/** Entrada de la pestaña 📊 Estadísticas (la llama invSubtab). */
async function conteoEstIniciar() {
  const cont = document.getElementById('inv-estadisticas-filtros');
  if (cont && !document.getElementById('ce-desde')) cont.innerHTML = _ceFiltrosHtml();
  await conteoEstCargar();
}

/** Lee los filtros, pide el reporte y lo pinta. */
async function conteoEstCargar() {
  const el = document.getElementById('inv-estadisticas-contenido');
  if (!el) return;
  const qs = new URLSearchParams();
  [['desde', 'ce-desde'], ['hasta', 'ce-hasta'], ['almacen_id', 'ce-almacen'],
   ['clase', 'ce-clase'], ['tipo', 'ce-tipo']].forEach(([k, id]) => {
    const v = document.getElementById(id)?.value;
    if (v) qs.set(k, v);
  });
  el.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Cargando…</div>';
  try {
    const d = await get(`/api/conteo/estadisticas?${qs.toString()}`);
    const desde = document.getElementById('ce-desde');
    const hasta = document.getElementById('ce-hasta');
    if (desde && !desde.value) desde.value = d.parametros.desde;
    if (hasta && !hasta.value) hasta.value = d.parametros.hasta;
    el.innerHTML = _ceRender(d);
  } catch (e) {
    el.innerHTML = `<div style="text-align:center;padding:30px;color:var(--red);">${esc(e.message || 'Error cargando estadísticas')}</div>`;
  }
}

function _ceRender(d) {
  return [_ceCobertura(d.carga_cobertura, d.rezago), _ceVolumen(d.volumen),
          _ceAjustes(d.ajustes), _ceExactitud(d.exactitud),
          _ceProductos(d.productos_problema), _ceOperarios(d.por_operario)].join('')
    + `<div style="font-size:10px;color:var(--tx3);text-align:center;margin:6px 0 16px;">${esc(d.fuente)} · rango ${esc(d.parametros.desde)} a ${esc(d.parametros.hasta)}</div>`;
}

function _ceCobertura(c, r) {
  const filas = (c.filas || []).filter(f => f.universo_huecos > 0);
  const cuerpoFilas = filas.length ? filas.map(f => `
    <div style="border-top:1px solid var(--brd);padding:8px 0;">
      <div style="font-size:12px;font-weight:700;color:var(--tx);">${esc(f.almacen || '')} · clase ${esc(f.clase)} <span style="color:var(--tx3);font-weight:400;">(objetivo: cada ${_ceNum(f.frecuencia_dias)} días)</span></div>
      <div style="font-size:12px;color:var(--tx2);line-height:1.6;">
        Universo: ${_ceNum(f.universo_productos)} productos · ${_ceNum(f.universo_huecos)} huecos<br>
        Contados dentro de su intervalo: ${_ceMetrica(f.contados_en_frecuencia)}<br>
        Sin contar: <b>${_ceNum(f.sin_contar_en_ventana)}</b> (nunca contados ${_ceNum(f.nunca_contados)})<br>
        Para cumplir el intervalo harían falta <b>${_ceNum(f.exigencia_diaria)}</b>/día · ritmo real ${_ceNum(f.ritmo.por_dia, 2)}/día (${_ceNum(f.ritmo.cadenas_cerradas)} en ${_ceNum(f.ritmo.dias_ventana)} días)<br>
        Días para cerrar el ciclo: ${f.dias_para_cerrar_ciclo === null ? `<span style="color:var(--yellow);">${esc(f.sin_estimacion_por || 'sin estimación')}</span>` : `<b>${_ceNum(f.dias_para_cerrar_ciclo, 1)}</b>`}
      </div>
    </div>`).join('') : '<div style="font-size:12px;color:var(--tx3);">No hay universo ABC cargado para este filtro.</div>';
  // Cupo configurado contra ritmo real, por almacén: la capacidad fija el
  // ritmo, así que lo primero es ver si el equipo alcanza el cupo.
  const cupos = (c.por_almacen || []).map(a => `
    <div style="border-top:1px solid var(--brd);padding:8px 0;font-size:12px;color:var(--tx2);line-height:1.6;">
      <div style="font-weight:700;color:var(--tx);">${esc(a.almacen || a.bodega_siesa || '')}</div>
      Cupo configurado <b>${_ceNum(a.cupo_diario)}</b>/día · ritmo real <b>${_ceNum(a.ritmo_real_por_dia, 2)}</b>/día (${_ceNum(a.dias_ventana)} días) · el plan pediría ${_ceNum(a.exigencia_diaria_plan)}/día<br>
      Pendientes sin contar: ${_ceNum(a.pendientes_vivas)}${a.dias_de_cupo_pendientes === null || a.dias_de_cupo_pendientes === undefined ? '' : ` (${_ceNum(a.dias_de_cupo_pendientes, 1)} días de cupo)`}
      ${a.mensaje_generador ? `<br><span style="color:var(--yellow);">${esc(a.mensaje_generador)}</span>` : ''}
    </div>`).join('');
  const tramos = (t) => Object.entries(t || {}).map(([k, n]) => `${esc(k)} d: <b>${_ceNum(n)}</b>`).join(' · ');
  const rezago = `<div style="border-top:1px solid var(--brd);padding-top:8px;font-size:12px;color:var(--tx2);line-height:1.6;">
    Pendientes (${_ceNum(r.total_pendiente)}): ${tramos(r.pendiente)}<br>
    En curso (${_ceNum(r.total_en_curso)}): ${tramos(r.en_curso)}
    ${_ceExcluidos(r.excluidos)}</div>`;
  return _ceTarjeta(`📦 Carga y cobertura del plan <span style="font-size:10px;color:var(--tx3);font-weight:400;">al ${esc(c.al_dia_operativo)}</span>`,
    `<div style="font-size:10px;color:var(--tx3);margin-bottom:6px;">${esc(c.nota)}</div>${cupos}${cuerpoFilas}${_ceExcluidos(c.excluidos)}${rezago}`);
}

function _ceVolumen(v) {
  const sv = Object.entries(v.sin_veredicto || {}).map(([k, n]) => `${esc(k)} ${_ceNum(n)}`).join(' · ') || '—';
  const semanas = (v.por_semana || []).map(s => `<tr>
      <td style="padding:4px 6px;">${esc(s.semana)}</td>
      <td style="padding:4px 6px;text-align:right;">${_ceNum(s.cerradas)}</td>
      <td style="padding:4px 6px;text-align:right;">${_ceNum(s.ok)}</td>
      <td style="padding:4px 6px;text-align:right;">${_ceNum(s.error)}</td>
      <td style="padding:4px 6px;text-align:right;">${_ceNum(s.ajustes)}</td>
      <td style="padding:4px 6px;text-align:right;">${_ceNum(s.unidades_ajustadas)}</td></tr>`).join('');
  const tabla = semanas ? `<div style="overflow-x:auto;margin-top:8px;"><table style="width:100%;border-collapse:collapse;font-size:11px;color:var(--tx2);">
      <tr style="color:var(--tx3);"><th style="text-align:left;padding:4px 6px;">Semana (lunes)</th><th style="text-align:right;padding:4px 6px;">Cerradas</th><th style="text-align:right;padding:4px 6px;">OK</th><th style="text-align:right;padding:4px 6px;">Error</th><th style="text-align:right;padding:4px 6px;">Ajustes</th><th style="text-align:right;padding:4px 6px;">Uds</th></tr>
      ${semanas}</table></div>` : '<div style="font-size:12px;color:var(--tx3);margin-top:6px;">Sin cadenas cerradas en el rango.</div>';
  return _ceTarjeta('🔁 Volumen y flujo', `<div style="font-size:12px;color:var(--tx2);line-height:1.6;">
      Cadenas iniciadas: <b>${_ceNum(v.iniciadas)}</b> · cerradas: <b>${_ceNum(v.cerradas)}</b> · SKUs: ${_ceNum(v.skus_cerrados)}<br>
      Necesitaron 2º conteo: ${_ceMetrica(v.a_cc2)}<br>
      Necesitaron 3º conteo: ${_ceMetrica(v.a_cc3)}<br>
      Sin veredicto: ${sv}<br>
      Recuentos por venta durante el conteo: ${_ceMetrica(v.recuentos)}
      ${_ceExcluidos(v.recuentos && v.recuentos.excluidos)}${_ceExcluidos(v.excluidos)}
    </div>${tabla}
    <div style="font-size:10px;color:var(--tx3);margin-top:6px;">${esc(v.unidad)}</div>`);
}

function _ceAjustes(a) {
  const val = a.valor || {};
  const bl = a.bloqueados_hoy || {};
  const bloq = Object.entries(bl.por_motivo || {}).map(([k, n]) => `${esc(k)} ${_ceNum(n)}`).join(' · ') || '—';
  const aud = Object.entries((a.motivos_auditoria_picking || {}).por_motivo || {}).map(([k, n]) => `${esc(k)} ${_ceNum(n)}`).join(' · ') || '—';
  return _ceTarjeta('⚖️ Ajustes a Siesa', `<div style="font-size:12px;color:var(--tx2);line-height:1.6;">
      Ajustes: <b>${_ceNum(a.cantidad)}</b> (automáticos ${_ceNum(a.automaticos)} · por supervisor ${_ceNum(a.aprobados_por_supervisor)} · en vuelo ${_ceNum(a.en_vuelo)})<br>
      Unidades: +${_ceNum(a.unidades_ent)} / −${_ceNum(a.unidades_sal)} · neto ${_ceNum(a.unidades_neto)}<br>
      Valor <span style="color:var(--tx3);">(${esc(val.etiqueta || '')})</span>: +$${_ceNum(val.ent)} / −$${_ceNum(val.sal)} · neto <b>$${_ceNum(val.neto)}</b><br>
      <span style="color:var(--tx3);font-size:11px;">${_ceNum(val.ajustes_valorizados)} valorizados · ${_ceNum(val.ajustes_sin_costo)} sin costo en su foto</span><br>
      Hoy bloqueados: <b>${_ceNum(bl.total)}</b> (${bloq}) · aprobables: ${_ceNum(bl.descuadres_aprobables)} · jobs fallidos: ${_ceNum(a.jobs_fallidos_hoy)}<br>
      <span style="color:var(--tx3);font-size:11px;">Auditorías de picking (diagnóstico, no ajuste): ${aud}</span>
      ${_ceExcluidos(a.excluidos)}</div>`);
}

function _ceExactitud(e) {
  const filas = Object.entries(e.por_clase || {}).map(([cl, grupos]) =>
    Object.entries(grupos).map(([g, m]) =>
      `<div>Clase ${esc(cl)} · ${esc(g)}: ${_ceMetrica(m)}</div>`).join('')).join('');
  return _ceTarjeta('🎯 Exactitud de inventario', `<div style="font-size:12px;color:var(--tx2);line-height:1.6;">
      <div style="font-size:10px;color:var(--tx3);margin-bottom:4px;">${esc(e.definicion)} · sin porcentaje con n &lt; ${_ceNum(e.min_n)}</div>
      ${filas || '<div style="color:var(--tx3);">Sin cadenas medibles en el rango.</div>'}
      ${_ceExcluidos(e.excluidos)}</div>`);
}

function _ceProductos(p) {
  const dif = (p.por_diferencia || []).map(f => `<tr>
      <td style="padding:4px 6px;">${esc(f.producto_codigo || '')}</td>
      <td style="padding:4px 6px;">${esc(f.producto_nombre || '')}</td>
      <td style="padding:4px 6px;text-align:right;">${_ceNum(f.diferencia)}</td>
      <td style="padding:4px 6px;">${esc(f.dia)}</td></tr>`).join('');
  const lista = (arr) => (arr || []).map(f => `${esc(f.producto_codigo || '')} (${_ceNum(f.n)})`).join(' · ') || '—';
  const tabla = dif
    ? `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;color:var(--tx2);">
      <tr style="color:var(--tx3);"><th style="text-align:left;padding:4px 6px;">Código</th><th style="text-align:left;padding:4px 6px;">Producto</th><th style="text-align:right;padding:4px 6px;">Dif.</th><th style="text-align:left;padding:4px 6px;">Día</th></tr>${dif}</table></div>`
    : '<div style="font-size:12px;color:var(--tx3);">Sin diferencias confirmadas en el rango.</div>';
  return _ceTarjeta('🔎 Productos problema', `${tabla}
    <div style="font-size:12px;color:var(--tx2);margin-top:8px;line-height:1.6;">Más ajustes: ${lista(p.por_ajustes)}<br>Más recuentos: ${lista(p.por_recuentos)}</div>`);
}

function _ceOperarios(o) {
  const filas = (o.filas || []).map(f =>
    `<div>${esc(f.nombre || ('#' + f.operario_id))}: ${_ceNum(f.cadenas)} cadenas · ${_ceNum(f.conteos)} conteos</div>`).join('');
  return _ceTarjeta('👥 Participación por persona', `<div style="font-size:12px;color:var(--tx2);line-height:1.6;">
      <div style="font-size:10px;color:var(--tx3);margin-bottom:4px;">${esc(o.nota)}</div>
      ${filas || '<div style="color:var(--tx3);">Sin conteos en el rango.</div>'}
      ${_ceExcluidos(o.excluidos)}</div>`);
}
