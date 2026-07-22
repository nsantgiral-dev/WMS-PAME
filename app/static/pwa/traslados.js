// ══════════════════════════════════════════════════════════════════
// TRASLADOS — Admin tab + operario picker traslado
// Dependencias globales (de app.js): get(), post(), alerta(),
//   API, TOKEN, OPERARIO, ALMACEN_ID
// ══════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════════════
// TRASLADOS — Admin tab
// ══════════════════════════════════════════════════════════════════

let _TRAS_SUBTAB = 'transito';
const TRAS_ESTADO = {
  transito:   ['EN_TRANSITO'],
  historial:  ['ENTREGADA','RECHAZADA','CANCELADA','REVERTIDA']
};
const TRAS_COL = {
  BORRADOR:'#374151', ENVIADA:'#1d4ed8', EN_PICKING:'#7c3aed', PREPARADO:'#166534',
  EN_TRANSITO:'#9a3412', ENTREGADA:'#065f46',
  RECHAZADA:'#7f1d1d', CANCELADA:'#374151', REVERTIDA:'#4b5563'
};

/**
 * Switch the active traslados sub-tab and load its content.
 * @param {string} nombre - Sub-tab key: 'pedir', 'transito', or 'historial'.
 */
function trasSubtab(nombre) {
  _TRAS_SUBTAB = nombre;
  ['pedir','transito','historial'].forEach(k => {
    const el = document.getElementById(`tras-tab-${k}`);
    if (!el) return;
    const activo = k === nombre;
    el.style.background = activo ? '#1E8395' : 'transparent';
    el.style.color = activo ? '#fff' : '#415A70';
    el.style.fontWeight = activo ? '700' : '400';
  });
  const lista = document.getElementById('tras-lista');
  const panelPedir = document.getElementById('tras-panel-pedir');
  if (nombre === 'pedir') {
    if (lista) lista.style.display = 'none';
    if (panelPedir) panelPedir.style.display = 'block';
    adminPedirIniciar();
  } else {
    if (lista) lista.style.display = 'block';
    if (panelPedir) panelPedir.style.display = 'none';
    cargarTrasladosAdmin();
  }
}

/** Fetch and render traslado cards for the current admin sub-tab. */
async function cargarTrasladosAdmin() {
  const lista = document.getElementById('tras-lista');
  if (!lista) return;
  const estados = TRAS_ESTADO[_TRAS_SUBTAB] || [];
  lista.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';
  try {
    // Cargar solicitudes para cada estado del subtab actual
    const promesas = estados.map(e => get(`/api/traslados/?estado=${e}`));
    const resultados = await Promise.all(promesas);
    const todas = resultados.flatMap(r => r.solicitudes || []);
    todas.sort((a,b) => new Date(b.fecha_creacion) - new Date(a.fecha_creacion));

    if (!todas.length) {
      lista.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Sin solicitudes</div>';
      return;
    }
    lista.innerHTML = todas.map(s => _renderTrasladoCard(s)).join('');
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error cargando traslados</div>';
  }
}

/**
 * Build the HTML card for a single traslado solicitud.
 * @param {Object} s - Solicitud object from the API.
 * @returns {string} HTML string for the card.
 */
function _renderTrasladoCard(s) {
  const col = TRAS_COL[s.estado] || '#333';
  const fechaCreacion = s.fecha_creacion ? new Date(s.fecha_creacion) : null;
  const fecha = fechaCreacion ? fechaCreacion.toLocaleDateString('es-CO') : '';

  // Antigüedad — alertar si lleva demasiado tiempo en estados activos
  let alertaAntiguedad = '';
  if (fechaCreacion && ['EN_PICKING','PREPARADO','ENVIADA'].includes(s.estado)) {
    const diasTranscurridos = Math.floor((Date.now() - fechaCreacion) / 86400000);
    if (diasTranscurridos >= 3) {
      alertaAntiguedad = `<span style="color:#ef4444;font-weight:700;font-size:10px;">⚠ ${diasTranscurridos}d sin avanzar</span>`;
    } else if (diasTranscurridos >= 1) {
      alertaAntiguedad = `<span style="color:#f59e0b;font-size:10px;">${diasTranscurridos}d</span>`;
    }
  }

  const itemsResumen = (s.items || []).map(i => {
    const aprobado = i.cantidad_aprobada && i.cantidad_aprobada !== i.cantidad_solicitada
      ? ` <span style="color:#f59e0b;">(aprobado: ${i.cantidad_aprobada})</span>` : '';
    const enviado = i.cantidad_enviada > 0
      ? ` <span style="color:#4ade80;">→ enviado: ${i.cantidad_enviada}</span>` : '';
    return `<div style="font-size:11px;color:#666;">${i.producto_codigo || i.producto_nombre} · ${i.cantidad_solicitada} und${aprobado}${enviado}</div>`;
  }).join('');

  // Barra de progreso picking
  let pickingInfo = '';
  const pp = s.picking_progreso;
  if (pp && !pp.sin_tareas && pp.total > 0) {
    const pct = pp.porcentaje || 0;
    const barColor = pct === 100 ? '#4ade80' : pct > 50 ? '#f59e0b' : '#7c3aed';
    pickingInfo = `
      <div style="margin:8px 0 4px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <span style="font-size:10px;color:#888;">Picking: ${pp.completadas}/${pp.total} tareas</span>
          <span style="font-size:10px;color:${barColor};font-weight:700;">${pct}%</span>
        </div>
        <div style="height:4px;background:#222;border-radius:4px;overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:${barColor};border-radius:4px;transition:width .3s;"></div>
        </div>
      </div>`;
  } else if (pp && pp.sin_tareas && s.estado === 'EN_PICKING') {
    pickingInfo = `<div style="font-size:10px;color:#f59e0b;margin:6px 0;">⚠ Sin tareas de picking — picking manual requerido</div>`;
  }

  const acciones = [];

  if (s.estado === 'ENVIADA') {
    acciones.push(`<button onclick="trasAprobar(${s.id})" style="flex:1;padding:10px;background:#166534;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">Aprobar y asignar</button>`);
    acciones.push(`<button onclick="trasRechazar(${s.id})" style="padding:10px 12px;background:#7f1d1d;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;">Rechazar</button>`);
  }

  if (s.estado === 'EN_PICKING') {
    const pickingCompleto = pp && !pp.sin_tareas && pp.completadas === pp.total && pp.total > 0;
    if (pickingCompleto) {
      // Picking formal completo → confirmar y avanzar a PREPARADO
      acciones.push(`<button onclick="trasConfirmarRecogida(${s.id})" style="flex:1;padding:10px;background:#166534;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">✅ Confirmar recogida</button>`);
    } else {
      // Picking en proceso o manual → forzar confirmación o despachar directo
      acciones.push(`<button onclick="trasConfirmarRecogida(${s.id})" style="flex:1;padding:10px;background:#1e3a5f;color:#60a5fa;border:1px solid #1e3a5f;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">Confirmar recogida manual</button>`);
    }
    acciones.push(`<button onclick="trasDespacharDirecto(${s.id})" style="padding:10px 10px;background:#78350f;color:#fbbf24;border:1px solid #92400e;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">Despachar ⚡</button>`);
    acciones.push(`<button onclick="trasReasignarOperario(${s.id})" style="padding:10px 10px;background:#1a1a1a;color:#aaa;border:1px solid #333;border-radius:8px;font-size:11px;cursor:pointer;">↺ Operario</button>`);
  }

  if (s.estado === 'PREPARADO') {
    const packDespachado = s.packing_info && s.packing_info.estado === 'DESPACHADO';
    if (packDespachado) {
      acciones.push(`<div style="flex:1;padding:10px;color:#9ca3af;font-size:12px;text-align:center;background:#1a1a1a;border:1px solid #374151;border-radius:8px;">⏳ Despacho en proceso...</div>`);
    } else {
      acciones.push(`<button onclick="trasDespachar(${s.id})" style="flex:1;padding:10px;background:#b45309;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">🚛 Despachar</button>`);
    }
    acciones.push(`<button onclick="trasVerLPNs(${s.id})" style="padding:10px 10px;background:#1a1a1a;color:#a78bfa;border:1px solid #4c1d95;border-radius:8px;font-size:11px;cursor:pointer;">📦 LPNs</button>`);
  }

  if (s.estado === 'EN_TRANSITO') {
    acciones.push(`<span style="font-size:12px;color:#f59e0b;font-weight:600;">🚚 Mercancía en camino — la tienda confirma recepción</span>`);
  }

  const operarioTag = s.operario_nombre
    ? `<div style="font-size:11px;color:#7c3aed;margin-bottom:6px;">👷 ${s.operario_nombre}${s.estado==='PREPARADO' ? ' · Listo para despachar' : s.estado==='EN_PICKING' ? ' · Recogiendo' : ''}</div>`
    : (s.estado === 'EN_PICKING' ? `<div style="font-size:11px;color:#f59e0b;margin-bottom:6px;">⚠ Sin operario asignado</div>` : '');

  return `
  <div style="background:#111;border:1px solid ${s.estado==='EN_PICKING' && pp?.sin_tareas ? '#78350f' : '#222'};border-radius:12px;padding:14px;margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
      <div>
        <div style="font-size:13px;font-weight:700;">${s.codigo}</div>
        <div style="font-size:11px;color:#555;margin-top:2px;">${s.nombre_punto_venta || s.bodega_destino_siesa} · ${fecha} ${alertaAntiguedad}</div>
      </div>
      <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:3px 8px;border-radius:8px;white-space:nowrap;">${s.estado}</span>
    </div>
    <div style="margin-bottom:6px;">${itemsResumen}</div>
    ${pickingInfo}
    ${operarioTag}
    ${s.siesa_error ? `<div style="font-size:10px;color:#f87171;margin-bottom:8px;">⚠ Siesa: ${s.siesa_error}</div>` : ''}
    ${acciones.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;">${acciones.join('')}</div>` : ''}
  </div>`;
}

// ── Admin Pedir — solicitar traslado hacia NB1 ──────────────────
let _AP_STOCK = [];
let _AP_STOCK_ESTADO = 'idle';
let _AP_CARRITO = [];
let _AP_ORIGEN = null;
let _AP_FILTRO = '';
let _AP_PAGINA = 1;
const _AP_POR_PAGINA = 30;
let _AP_INICIADO = false;

/** Initialize the "Pedir" panel: populate origin selector and load stock. */
function adminPedirIniciar() {
  if (_AP_INICIADO) return;
  _AP_INICIADO = true;
  const sel = document.getElementById('admin-pedir-origen');
  if (!sel) return;
  const opciones = _BODEGAS_ORIGEN.filter(b => b.id !== 'NB1');
  sel.innerHTML = opciones.map(b =>
    `<option value="${b.id}" data-nombre="${b.nombre}" style="background:#0d2137;color:#fff;">${b.nombre} (${b.id})</option>`
  ).join('');
  const def = opciones[0];
  if (def) {
    sel.value = def.id;
    _AP_ORIGEN = { id: def.id, nombre: def.nombre };
  }
  adminPedirCargarStock();
}

/**
 * Handle origin bodega change; reset cart and reload stock.
 * @param {HTMLSelectElement} sel - The origin bodega dropdown.
 */
function adminPedirCambiarOrigen(sel) {
  const opt = sel.options[sel.selectedIndex];
  _AP_ORIGEN = { id: sel.value, nombre: opt.dataset.nombre || sel.value };
  _AP_CARRITO = [];
  adminPedirActualizarCarrito();
  _AP_STOCK = [];
  _AP_STOCK_ESTADO = 'cargando';
  adminPedirCargarStock();
}

/** Fetch available stock from the selected origin bodega. */
async function adminPedirCargarStock() {
  _AP_STOCK_ESTADO = 'cargando';
  adminPedirRenderStock();
  try {
    const d = await get(`/api/traslados/stock-disponible?bodega=${_AP_ORIGEN?.id || 'NS1'}`);
    _AP_STOCK = (d.items || []).filter(i => i.producto_id && i.disponible > 0);
    _AP_STOCK_ESTADO = 'listo';
  } catch (e) {
    _AP_STOCK_ESTADO = 'error';
  }
  adminPedirRenderStock();
}

/** Invalidate stock cache on the server and reload fresh data. */
async function adminPedirActualizarStock() {
  const btn = document.getElementById('admin-pedir-btn-refresh');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.4'; btn.textContent = '…'; }
  try {
    await fetch(API + `/api/traslados/invalidar-cache-stock?bodega=${_AP_ORIGEN?.id || 'NS1'}`, {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN },
    });
    _AP_STOCK = [];
    _AP_STOCK_ESTADO = 'cargando';
    await adminPedirCargarStock();
  } catch (e) { alerta('Error actualizando stock', 'error'); }
  finally { if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.textContent = '↻'; } }
}

/** Apply text filter from the search input to the stock list. */
function adminPedirFiltrarStock() {
  _AP_FILTRO = (document.getElementById('admin-pedir-buscar')?.value || '').toLowerCase();
  _AP_PAGINA = 1;
  adminPedirRenderStock();
}

/**
 * Navigate to a specific page in the stock list.
 * @param {number} p - Page number to display.
 */
function adminPedirIrPagina(p) {
  _AP_PAGINA = p;
  adminPedirRenderStock();
  document.getElementById('admin-pedir-stock-lista')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Render the paginated stock grid with filter and cart state applied. */
function adminPedirRenderStock() {
  const el = document.getElementById('admin-pedir-stock-lista');
  if (!el) return;
  if (_AP_STOCK_ESTADO === 'cargando') {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#555;">Consultando stock...</div>';
    return;
  }
  if (_AP_STOCK_ESTADO === 'error') {
    el.innerHTML = `<div style="text-align:center;padding:40px;"><div style="color:#f87171;margin-bottom:12px;">No se pudo cargar el stock</div>
      <button onclick="adminPedirCargarStock()" style="padding:10px 20px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;cursor:pointer;">Reintentar</button></div>`;
    return;
  }
  if (!_AP_STOCK.length) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Sin productos disponibles</div>';
    return;
  }
  const filtrado = _AP_FILTRO
    ? _AP_STOCK.filter(i => (i.nombre||'').toLowerCase().includes(_AP_FILTRO) || (i.codigo_siesa||'').toLowerCase().includes(_AP_FILTRO))
    : _AP_STOCK;
  if (!filtrado.length) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Sin resultados</div>';
    return;
  }
  const totalPags = Math.ceil(filtrado.length / _AP_POR_PAGINA);
  _AP_PAGINA = Math.max(1, Math.min(_AP_PAGINA, totalPags));
  const inicio = (_AP_PAGINA - 1) * _AP_POR_PAGINA;
  const pagina = filtrado.slice(inicio, inicio + _AP_POR_PAGINA);

  const rango = 2, desde = Math.max(1, _AP_PAGINA - rango), hasta = Math.min(totalPags, _AP_PAGINA + rango);
  const nums = [];
  if (desde > 1) nums.push('<span style="color:#555;">…</span>');
  for (let p = desde; p <= hasta; p++) {
    nums.push(`<button onclick="adminPedirIrPagina(${p})" style="min-width:32px;padding:6px 8px;background:${p===_AP_PAGINA?'#1E8395':'#222'};color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:${p===_AP_PAGINA?'700':'400'};cursor:pointer;">${p}</button>`);
  }
  if (hasta < totalPags) nums.push('<span style="color:#555;">…</span>');
  const nav = totalPags > 1 ? `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 0 14px;flex-wrap:wrap;">
    <button onclick="adminPedirIrPagina(${_AP_PAGINA-1})" ${_AP_PAGINA===1?'disabled':''} style="padding:7px 14px;background:#222;color:${_AP_PAGINA===1?'#444':'#fff'};border:none;border-radius:8px;font-size:13px;cursor:pointer;">← Ant</button>
    <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:center;">${nums.join('')}</div>
    <button onclick="adminPedirIrPagina(${_AP_PAGINA+1})" ${_AP_PAGINA===totalPags?'disabled':''} style="padding:7px 14px;background:#222;color:${_AP_PAGINA===totalPags?'#444':'#fff'};border:none;border-radius:8px;font-size:13px;cursor:pointer;">Sig →</button>
  </div><div style="text-align:center;font-size:11px;color:#555;margin-bottom:10px;">${filtrado.length} productos · pág ${_AP_PAGINA}/${totalPags}</div>` : '';

  el.innerHTML = nav + pagina.map(item => {
    const enCarrito = _AP_CARRITO.find(c => c.codigo_siesa === item.codigo_siesa);
    const qid = 'ap-qty-' + (item.codigo_siesa || '').replace(/[^a-zA-Z0-9]/g, '-');
    const nombreEsc = (item.nombre || '').replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return `<div style="background:#111;border:1px solid ${enCarrito?'#4ade80':'#222'};border-radius:10px;padding:12px;margin-bottom:8px;display:flex;align-items:center;gap:12px;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${item.nombre||'—'}</div>
        <div style="font-size:11px;color:#666;">${item.codigo_siesa||''} · Disponible: <span style="color:#4ade80;font-weight:700;">${item.disponible}</span></div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
        <input type="number" min="1" max="${item.disponible}" value="${enCarrito?.cantidad||1}" id="${qid}"
          style="width:56px;padding:7px;background:#000;border:1px solid #333;border-radius:6px;color:#fff;font-size:13px;text-align:center;">
        <button onclick="adminPedirAgregarCarrito('${item.codigo_siesa}','${nombreEsc}',${item.disponible},${item.producto_id||'null'})"
          style="padding:8px 12px;background:${enCarrito?'#4ade80':'#fff'};color:#000;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">${enCarrito?'✓':'+'}</button>
      </div>
    </div>`;
  }).join('') + nav;
}

/**
 * Add or update a product in the request cart.
 * @param {string} codigoSiesa - Siesa product code.
 * @param {string} nombre - Product display name.
 * @param {number} disponible - Maximum available quantity.
 * @param {number|null} productoId - WMS product ID.
 */
function adminPedirAgregarCarrito(codigoSiesa, nombre, disponible, productoId) {
  const inp = document.getElementById(`ap-qty-${codigoSiesa.replace(/[^a-zA-Z0-9]/g,'-')}`);
  const cantidad = Math.min(parseInt(inp?.value || 1), disponible);
  if (cantidad < 1) return;
  const idx = _AP_CARRITO.findIndex(c => c.codigo_siesa === codigoSiesa);
  if (idx >= 0) _AP_CARRITO[idx].cantidad = cantidad;
  else _AP_CARRITO.push({ codigo_siesa: codigoSiesa, nombre, disponible, cantidad, producto_id: productoId });
  adminPedirActualizarCarrito();
  adminPedirRenderStock();
}

/** Re-render the cart summary panel with current items. */
function adminPedirActualizarCarrito() {
  const header = document.getElementById('admin-pedir-carrito-header');
  const items = document.getElementById('admin-pedir-carrito-items');
  if (!header || !items) return;
  if (!_AP_CARRITO.length) { header.style.display = 'none'; return; }
  header.style.display = 'block';
  items.innerHTML = _AP_CARRITO.map(c =>
    `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--brd);">
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;color:var(--tx);">${c.nombre}</span>
      <span style="flex-shrink:0;color:#4ade80;font-weight:700;font-size:13px;">${c.cantidad}</span>
      <button onclick="adminPedirQuitarCarrito('${c.codigo_siesa}')" style="flex-shrink:0;background:none;border:none;color:#ef4444;cursor:pointer;font-size:13px;padding:2px 4px;">✕</button>
    </div>`
  ).join('');
}

/**
 * Remove a product from the request cart by its Siesa code.
 * @param {string} codigoSiesa - Siesa product code to remove.
 */
function adminPedirQuitarCarrito(codigoSiesa) {
  _AP_CARRITO = _AP_CARRITO.filter(c => c.codigo_siesa !== codigoSiesa);
  adminPedirActualizarCarrito();
  adminPedirRenderStock();
}

/** Create and immediately send a traslado solicitud from the current cart. */
async function adminPedirEnviarSolicitud() {
  if (!_AP_CARRITO.length) { alerta('El carrito está vacío', 'error'); return; }
  const origen = _AP_ORIGEN?.nombre || _AP_ORIGEN?.id || 'la bodega';
  if (!confirm(`¿Solicitar ${_AP_CARRITO.length} producto${_AP_CARRITO.length!==1?'s':''} desde ${origen} para Bodega Principal (NB1)?`)) return;
  const items = _AP_CARRITO.filter(c => c.producto_id).map(c => ({
    producto_id: c.producto_id,
    cantidad_solicitada: c.cantidad,
    disponible_siesa: c.disponible,
  }));
  if (!items.length) { alerta('No se encontraron productos válidos', 'error'); return; }
  try {
    const r = await fetch(API + '/api/traslados/', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items,
        bodega_origen_siesa: _AP_ORIGEN.id,
        bodega_destino_siesa: 'NB1',
        nombre_punto_venta: 'Bodega Principal (NB1)',
      })
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error creando solicitud', 'error'); return; }
    const r2 = await fetch(API + `/api/traslados/${d.id}/enviar`, {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN }
    });
    if (r2.ok) {
      alerta('Pedido enviado', 'exito');
      _AP_CARRITO = [];
      adminPedirActualizarCarrito();
      adminPedirRenderStock();
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Fetch and render traslados assigned to the current operario. */
async function cargarTrasladosOperario() {
  const contenedor = document.getElementById('traslados-operario');
  if (!contenedor) return;
  try {
    const d = await get('/api/traslados/mis-traslados');
    const traslados = d.traslados || [];
    if (!traslados.length) {
      contenedor.innerHTML = '';
      return;
    }
    contenedor.innerHTML = `
      <div style="font-size:12px;font-weight:700;color:#7c3aed;margin-bottom:10px;text-transform:uppercase;letter-spacing:1px;">
        Traslados asignados a ti (${traslados.length})
      </div>
      ${traslados.map(t => _renderTrasladoOperario(t)).join('')}`;
  } catch (e) {
    contenedor.innerHTML = '';
  }
}

/**
 * Build the HTML card for a traslado assigned to an operario.
 * @param {Object} t - Traslado object with items and metadata.
 * @returns {string} HTML string for the operario card.
 */
function _renderTrasladoOperario(t) {
  const itemsHtml = (t.items || []).map(i => {
    const cant = i.cantidad_aprobada || i.cantidad_solicitada;
    return `<div style="font-size:12px;color:#aaa;">${i.producto_codigo} — <b style="color:#fff;">${cant} und</b></div>`;
  }).join('');
  return `
    <div style="background:#0d0d1a;border:1px solid #7c3aed;border-radius:12px;padding:14px;margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div style="font-size:13px;font-weight:700;">${t.codigo}</div>
        <span style="font-size:11px;color:#7c3aed;">${t.nombre_punto_venta || t.bodega_destino_siesa}</span>
      </div>
      <div style="margin-bottom:10px;">${itemsHtml}</div>
      <button onclick="trasConfirmarRecogida(${t.id})"
        style="width:100%;padding:12px;background:#7c3aed;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">
        Confirmar recogida
      </button>
    </div>`;
}

/**
 * Confirm picking completion for a traslado, advancing it to PREPARADO.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasConfirmarRecogida(id) {
  if (!confirm('¿Confirmar recogida completa? El traslado pasará a PREPARADO y podrás despacharlo.')) return;
  try {
    await post(`/api/traslados/${id}/confirmar-picking`, {});
    alerta('Recogida confirmada — listo para despachar', 'exito');
    cargarTrasladosAdmin();
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

/**
 * Dispatch a traslado directly, skipping picking confirmation.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasDespacharDirecto(id) {
  if (!confirm('¿Despachar directamente sin confirmar picking? Se usarán las cantidades aprobadas como enviadas.')) return;
  try {
    await post(`/api/traslados/${id}/despachar`, {});
    alerta('Despachado — mercancía en tránsito', 'exito');
    cargarTrasladosAdmin();
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

/**
 * Show a modal to reassign a different operario to a traslado.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasReasignarOperario(id) {
  let operariosData;
  try {
    operariosData = await get('/api/traslados/operarios-disponibles');
  } catch (e) { alerta('Error cargando operarios', 'error'); return; }

  const operarios = operariosData.operarios || [];
  if (!operarios.length) { alerta('No hay operarios disponibles', 'advertencia'); return; }

  const opciones = operarios.map(o => `<option value="${o.id}">${o.nombre}</option>`).join('');
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:900;display:flex;align-items:center;justify-content:center;';
  modal.innerHTML = `
    <div style="background:#111;border:1px solid #333;border-radius:16px;padding:24px;width:320px;">
      <div style="font-size:15px;font-weight:700;margin-bottom:16px;">↺ Reasignar operario</div>
      <select id="modal-nuevo-operario" style="width:100%;padding:10px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;margin-bottom:16px;">
        ${opciones}
      </select>
      <div style="display:flex;gap:8px;">
        <button onclick="this.closest('div[style*=fixed]').remove()" style="flex:1;padding:10px;background:#222;color:#aaa;border:1px solid #333;border-radius:8px;cursor:pointer;">Cancelar</button>
        <button id="btn-confirmar-reasignar" style="flex:1;padding:10px;background:#7c3aed;color:#fff;border:none;border-radius:8px;font-weight:700;cursor:pointer;">Confirmar</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector('#btn-confirmar-reasignar').onclick = async () => {
    const nuevo_id = parseInt(modal.querySelector('#modal-nuevo-operario').value);
    modal.remove();
    try {
      await post(`/api/traslados/${id}/reasignar-operario`, { operario_id: nuevo_id });
      alerta('Operario reasignado', 'exito');
      cargarTrasladosAdmin();
    } catch (e) { alerta(e.message || 'Error', 'error'); }
  };
}

/**
 * Open the approval modal to approve quantities and assign an operario.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasAprobar(id) {
  // Carga solicitud y operarios en paralelo
  let solicitud, operariosData;
  try {
    [solicitud, operariosData] = await Promise.all([
      fetch(API + `/api/traslados/${id}`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
      fetch(API + `/api/traslados/operarios-disponibles`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
    ]);
  } catch (e) { alerta('Error de conexión', 'error'); return; }

  const items = solicitud.items || [];
  const operarios = operariosData.operarios || [];

  const filasItems = items.map(i => `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <div style="flex:1;font-size:12px;">
        <div style="font-weight:600;">${i.producto_codigo}</div>
        <div style="color:#666;font-size:11px;">Solicitado: ${i.cantidad_solicitada} · Disp. Siesa: ${i.disponible_siesa ?? '—'}</div>
      </div>
      <div style="display:flex;align-items:center;gap:4px;">
        <label style="font-size:11px;color:#999;">Aprobar:</label>
        <input type="number" id="apr-${i.id}" value="${i.cantidad_solicitada}" min="0"
          style="width:70px;padding:6px;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#fff;font-size:13px;text-align:center;">
      </div>
    </div>
  `).join('');

  const opcioneOperarios = operarios.length
    ? `<option value="">Sin asignar (admin recoge)</option>` + operarios.map(o => `<option value="${o.id}">${o.nombre}</option>`).join('')
    : `<option value="">No hay operarios disponibles</option>`;

  const modal = document.createElement('div');
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div style="background:#111;border-radius:16px;padding:24px;width:100%;max-width:440px;border:1px solid #166534;max-height:85vh;overflow-y:auto;">
        <div style="font-size:17px;font-weight:700;margin-bottom:4px;">Aprobar y asignar traslado</div>
        <div style="font-size:12px;color:#666;margin-bottom:16px;">${solicitud.nombre_punto_venta || solicitud.bodega_destino_siesa}</div>

        <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:#aaa;">CANTIDADES A ENVIAR</div>
        ${filasItems}

        <div style="font-size:12px;font-weight:600;margin-top:14px;margin-bottom:8px;color:#aaa;">OPERARIO QUE RECOGE</div>
        <select id="apr-operario"
          style="width:100%;padding:10px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;margin-bottom:16px;">
          ${opcioneOperarios}
        </select>

        <div style="display:flex;gap:8px;">
          <button id="btn-apr-ok" style="flex:1;padding:12px;background:#166534;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">Aprobar</button>
          <button onclick="this.closest('[style*=fixed]').parentElement.remove()" style="padding:12px 16px;background:#222;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">Cancelar</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  modal.querySelector('#btn-apr-ok').onclick = async () => {
    const items_aprobados = items.map(i => ({
      id: i.id,
      cantidad_aprobada: Number(document.getElementById(`apr-${i.id}`).value) || 0
    }));
    const operario_id = document.getElementById('apr-operario').value
      ? Number(document.getElementById('apr-operario').value) : null;
    modal.remove();
    try {
      const r = await fetch(API + `/api/traslados/${id}/aprobar`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify({ items_aprobados, operario_id })
      });
      const d = await r.json();
      if (r.ok) {
        alerta(operario_id ? 'Aprobado — operario notificado' : 'Aprobado — sin operario asignado', 'exito');
        cargarTrasladosAdmin();
      } else { alerta(d.error || 'Error', 'error'); }
    } catch (e) { alerta('Error de conexión', 'error'); }
  };
}

/**
 * Reject a traslado solicitud with a reason prompt.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasRechazar(id) {
  const motivo = prompt('Motivo del rechazo:');
  if (!motivo) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/rechazar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo })
    });
    const d = await r.json();
    if (r.ok) { alerta('Solicitud rechazada', 'advertencia'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Display a bottom-sheet modal with LPNs linked to a traslado.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasVerLPNs(id) {
  let data;
  try {
    data = await get(`/api/traslados/${id}/lpns`);
  } catch (e) { alerta('Error cargando LPNs', 'error'); return; }

  const lpns = data.lpns || [];
  const estadoColor = { ACTIVO: '#4ade80', EN_TRANSITO: '#f59e0b', CONSUMIDO: '#6b7280' };

  const filas = lpns.length
    ? lpns.map(l => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #1a1a1a;">
          <div>
            <div style="font-size:13px;font-weight:700;color:#a78bfa;">${l.codigo}</div>
            <div style="font-size:11px;color:#666;">${l.producto_codigo} — ${l.cantidad_actual} und</div>
          </div>
          <span style="font-size:10px;font-weight:700;color:${estadoColor[l.estado]||'#fff'};">${l.estado}</span>
        </div>`).join('')
    : '<div style="color:#555;text-align:center;padding:20px;">Sin LPNs vinculados a este traslado</div>';

  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.9);display:flex;align-items:flex-end;';
  modal.innerHTML = `
    <div style="background:#0a0a0a;border-top:2px solid #7c3aed;border-radius:20px 20px 0 0;padding:24px;width:100%;max-height:75vh;overflow-y:auto;">
      <div style="font-size:16px;font-weight:700;color:#a78bfa;margin-bottom:4px;">📦 LPNs — Traslado #${id}</div>
      <div style="font-size:12px;color:#555;margin-bottom:16px;">${lpns.length} paca(s)/caja(s) vinculadas</div>
      ${filas}
      <button onclick="this.closest('[style*=fixed]').remove()"
        style="width:100%;padding:14px;margin-top:16px;background:#111;color:#666;border:1px solid #222;border-radius:10px;cursor:pointer;font-size:14px;">
        Cerrar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

/**
 * Confirm dispatch of a prepared traslado and notify Siesa.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasDespachar(id) {
  if (!confirm('¿Confirmar despacho? El operario ya preparó los ítems. Se notificará a Siesa (salida de bodega).')) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/despachar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) { alerta('Despachado — mercancía en tránsito', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Confirm reception of a traslado at the destination store.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasConfirmarRecepcion(id) {
  if (!confirm('¿Confirmar recepción? Se notificará a Siesa (entrada en tienda).')) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/recibir`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (r.ok) { alerta('Recepción confirmada — inventario en tienda', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Revert a traslado, returning units to the source warehouse inventory.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasRevertir(id) {
  const motivo = prompt('Motivo de la reversión (opcional):\nEj: "Camión regresó — mercancía no entregada"', '');
  if (motivo === null) return;
  if (!confirm(`¿Revertir este traslado?\n\nLas unidades volverán al inventario del almacén.\n⚠ Deberás anular manualmente el STS en Siesa.`)) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/revertir`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo })
    });
    const d = await r.json();
    if (r.ok) { alerta(d.mensaje || 'Traslado revertido — unidades devueltas al inventario', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error al revertir', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Retry the Siesa reception entry (connector 173079) for a traslado.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasReintentarRecepcionSiesa(id) {
  if (!confirm('¿Reintentar registro de entrada en Siesa (173079)? Solo usar si la recepción física ya fue confirmada.')) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/reintentar-recepcion`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (r.ok) { alerta('Entrada Siesa registrada', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error al reintentar', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Retry the Siesa dispatch notification for a traslado without changing state.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasReintentarDespachoSiesa(id) {
  if (!confirm('¿Reintentar notificación a Siesa del despacho? No mueve el estado.')) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/reintentar-despacho`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (r.ok) { alerta('Siesa notificado — despacho registrado', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error al reintentar', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}



// Requisiciones — movido desde app.js 2026-07-21
// ─────────────────────────────────────────────────────────────────────────────
// MÓDULO REQUISICIONES — Solicitudes de traslado enviadas desde tienda
// Responsabilidad única: mostrar requisiciones y disparar despacho.
// No comparte estado ni funciones con tab-pedidos ni tab-traslados.
// ─────────────────────────────────────────────────────────────────────────────

const _REQ_ESTADOS = ['ENVIADA', 'EN_PICKING', 'EN_PACKING', 'PREPARADO', 'EN_TRANSITO', 'ENTREGADA'];
const REQ_TAB_LABELS = ['PENDIENTE APROBAR', 'EN PICKING', 'EN EMPAQUE', 'LISTO DESPACHAR', 'EN TRÁNSITO', 'RECIBIDO'];
let REQ_TAB_ACTIVO = 0;
let REQ_GRUPOS_HTML = ['', '', '', '', '', ''];
let REQ_GRUPOS_COUNT = [0, 0, 0, 0, 0, 0];

/** Fetch and render transfer requisitions (RIT) grouped by status. */
async function cargarRequisiciones() {
  const lista = document.getElementById('req-lista');
  if (!lista) return;
  lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--tx3);">Cargando...</div>';
  try {
    const promesas = _REQ_ESTADOS.map(e => get(`/api/traslados/?estado=${e}`).catch(() => ({ solicitudes: [] })));
    const resultados = await Promise.all(promesas);
    REQ_GRUPOS_HTML = resultados.map((r, i) => {
      // ENTREGADA ya está resuelta — solo mostramos las últimas para confirmar recepción sin saturar la cola
      const visibles = _REQ_ESTADOS[i] === 'ENTREGADA' ? (r.solicitudes || []).slice(0, 5) : (r.solicitudes || []);
      REQ_GRUPOS_COUNT[i] = visibles.length;
      return visibles.map(s => _renderRequisicionCard(s)).join('');
    });
    renderReqTabsYLista();
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error cargando requisiciones</div>';
  }
}

/** Render requisition sub-tabs and the HTML for the active group. */
function renderReqTabsYLista() {
  const tabsEl = document.getElementById('req-tabs');
  const lista = document.getElementById('req-lista');
  if (!tabsEl || !lista) return;

  tabsEl.innerHTML = REQ_TAB_LABELS.map((label, i) => {
    const count = REQ_GRUPOS_COUNT[i] || 0;
    return `<div class="subtab${i === REQ_TAB_ACTIVO ? ' active' : ''}" onclick="reqCambiarTab(${i})">${label}${count ? ` (${count})` : ''}</div>`;
  }).join('');

  lista.innerHTML = REQ_GRUPOS_HTML[REQ_TAB_ACTIVO]
    || '<div style="text-align:center;padding:40px;color:var(--tx3);">Sin requisiciones en esta pestaña ✓</div>';
}

/** @param {number} idx - Requisition sub-tab index to activate. */
function reqCambiarTab(idx) {
  REQ_TAB_ACTIVO = idx;
  renderReqTabsYLista();
}

const _REQ_BODEGA_NOMBRES = {
  'NB1':'Bodega Principal','NC1':'Neiva Centro','NS1':'Neiva Sur Principal',
  'NS2':'Neiva Sur Fundación','FC1':'Florencia Centro','PC1':'Pitalito Centro',
  'PT1':'Pitalito Terminal','FF1':'Feria Florencia','FN1':'Feria Neiva','FP1':'Feria Pitalito',
};
/**
 * @param {number} id - Warehouse ID.
 * @returns {string} Display name for the warehouse.
 */
function _reqNombreBodega(id) {
  return id ? (_REQ_BODEGA_NOMBRES[id] ? `${_REQ_BODEGA_NOMBRES[id]} (${id})` : id) : '—';
}

/**
 * @param {Object} r - Requisition object.
 * @returns {string} HTML card with status and action buttons.
 */
function _renderRequisicionCard(r) {
  const BADGE = {
    ENVIADA:     { color: '#d97706', bg: '#fef3c7', label: '⏳ Pendiente aprobar' },
    EN_PICKING:  { color: '#2563eb', bg: '#dbeafe', label: '🔍 En picking' },
    EN_PACKING:  { color: '#ea580c', bg: '#fff7ed', label: '📦 En empaque' },
    PREPARADO:   { color: '#7c3aed', bg: '#ede9fe', label: '✅ Listo despachar' },
    EN_TRANSITO: { color: '#0891b2', bg: '#cffafe', label: '🚚 En tránsito' },
    ENTREGADA:   { color: '#16a34a', bg: '#dcfce7', label: '✓ Recibido' },
  };
  const badge  = BADGE[r.estado] || { color: '#6b7280', bg: '#f3f4f6', label: r.estado };
  const fecha  = r.fecha_creacion ? new Date(r.fecha_creacion).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
  const items  = (r.items || []);
  const totalUnd = items.reduce((s, i) => s + (i.cantidad_solicitada || 0), 0);

  const itemsHtml = items.slice(0, 4).map(i =>
    `<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--tx3);padding:2px 0;">
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%;">${i.producto_nombre || i.producto_codigo_siesa || '—'}</span>
      <span style="font-weight:600;color:var(--tx2);">${i.cantidad_solicitada}</span>
    </div>`
  ).join('');
  const masItems = items.length > 4
    ? `<div style="font-size:11px;color:var(--tx3);margin-top:2px;">+${items.length - 4} más</div>`
    : '';

  const accionBtn =
    r.estado === 'ENVIADA'
      ? `<div style="display:flex;gap:6px;flex-wrap:wrap;">
           <button onclick="reqRechazar(${r.id})"
             style="padding:8px 14px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;
                    background:#dc2626;color:#fff;border:none;">
             ✕ Rechazar
           </button>
           <button onclick="reqEditarAprobar(${r.id})"
             style="padding:8px 14px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;
                    background:#1d4ed8;color:#fff;border:none;">
             ✏ Editar
           </button>
           <button onclick="aprobarRequisicion(${r.id})"
             style="padding:8px 14px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;
                    background:#16a34a;color:#fff;border:none;">
             ✓ Aprobar
           </button>
         </div>`
    : r.estado === 'EN_PICKING'
      ? `<span style="font-size:12px;color:#2563eb;font-weight:600;">🔍 Operario pickeando...</span>`
    : r.estado === 'EN_PACKING'
      ? `<div style="text-align:right;">
           <span style="font-size:12px;color:#ea580c;font-weight:600;">📦 Empacando en ${_reqNombreBodega(r.bodega_origen_siesa)}...</span>
           ${r.packing_info ? `<div style="font-size:10px;color:#6b7280;margin-top:2px;">${r.packing_info.codigo} · ${r.packing_info.empacador || 'sin asignar'}</div>` : ''}
         </div>`
    : r.estado === 'PREPARADO'
      ? `<button onclick="despacharRequisicion(${r.id})"
           style="padding:8px 16px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;
                  background:#111;color:#fff;border:1px solid #111;">
           🚚 Despachar
         </button>`
    : r.estado === 'EN_TRANSITO'
      ? `<span style="font-size:12px;color:#0891b2;font-weight:600;">🚚 En camino a ${_reqNombreBodega(r.bodega_destino_siesa)}</span>`
    : r.estado === 'ENTREGADA'
      ? `<span style="font-size:12px;color:#16a34a;font-weight:600;">✓ Recibido${r.fecha_entrega ? ' · ' + new Date(r.fecha_entrega).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : ''}</span>`
    : '';

  return `
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:14px;margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:13px;font-weight:700;color:var(--tx1);">${r.codigo}</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:4px;flex-wrap:wrap;">
            <span style="font-size:11px;font-weight:600;padding:2px 7px;border-radius:4px;background:#1e3a5f;color:#93c5fd;">
              📦 ${_reqNombreBodega(r.bodega_origen_siesa)}
            </span>
            <span style="font-size:12px;color:var(--tx3);">→</span>
            <span style="font-size:11px;font-weight:600;padding:2px 7px;border-radius:4px;background:#431407;color:#fb923c;">
              🏪 ${r.nombre_punto_venta ? `${r.nombre_punto_venta} (${r.bodega_destino_siesa || ''})` : _reqNombreBodega(r.bodega_destino_siesa)}
            </span>
          </div>
          <div style="font-size:11px;color:var(--tx3);margin-top:3px;">
            Solicita: <strong style="color:var(--tx2);">${r.solicitante_nombre || '—'}</strong> · ${fecha}
          </div>
        </div>
        <span style="font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;
                     color:${badge.color};background:${badge.bg};">
          ${badge.label}
        </span>
      </div>
      <div style="background:var(--bg-s2);border-radius:8px;padding:8px;margin-bottom:10px;">
        ${itemsHtml}${masItems}
        <div style="font-size:11px;color:var(--tx3);margin-top:4px;border-top:1px solid var(--brd);padding-top:4px;">
          ${items.length} producto${items.length !== 1 ? 's' : ''} · ${totalUnd} unidades
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;">
        ${accionBtn}
      </div>
    </div>`;
}

/** @param {number} id - Requisition ID to dispatch (triggers Siesa STS from RIT). */
async function despacharRequisicion(id) {
  if (!confirm('¿Confirmar despacho de esta requisición?')) return;
  try {
    const r = await fetch(`/api/traslados/${id}/despachar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al despachar', 'error'); return; }
    alerta('Requisición despachada ✓', 'exito');
    await cargarRequisiciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

/** @param {number} id - Requisition ID to approve. */
async function aprobarRequisicion(id) {
  if (!confirm('¿Aprobar esta requisición? Se crearán las tareas de picking en Bodega.')) return;
  try {
    const r = await fetch(`/api/traslados/${id}/aprobar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al aprobar', 'error'); return; }
    alerta('Requisición aprobada — el operario de traslado puede iniciar el picking ✓', 'exito');
    await cargarRequisiciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

/** @param {number} id - Requisition ID to reject. */
async function reqRechazar(id) {
  const motivo = prompt('Motivo del rechazo:');
  if (!motivo) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/rechazar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo })
    });
    const d = await r.json();
    if (r.ok) { alerta('Solicitud rechazada', 'advertencia'); cargarRequisiciones(); }
    else { alerta(d.error || 'Error al rechazar', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** @param {number} id - Requisition ID to open the edit/approve modal for. */
async function reqEditarAprobar(id) {
  let solicitud, operariosData;
  try {
    [solicitud, operariosData] = await Promise.all([
      fetch(API + `/api/traslados/${id}`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
      fetch(API + `/api/traslados/operarios-disponibles`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
    ]);
  } catch (e) { alerta('Error de conexión', 'error'); return; }

  const items = solicitud.items || [];
  const operarios = operariosData.operarios || [];

  const filasItems = items.map(i => `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <div style="flex:1;font-size:12px;">
        <div style="font-weight:600;">${i.producto_nombre || i.producto_codigo}</div>
        <div style="color:#666;font-size:11px;">Solicitado: ${i.cantidad_solicitada} · Disp. Siesa: ${i.disponible_siesa ?? '—'}</div>
      </div>
      <div style="display:flex;align-items:center;gap:4px;">
        <label style="font-size:11px;color:#999;">Aprobar:</label>
        <input type="number" id="req-apr-${i.id}" value="${i.cantidad_solicitada}" min="0"
          style="width:70px;padding:6px;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#fff;font-size:13px;text-align:center;">
      </div>
    </div>
  `).join('');

  const opcionesOperarios = operarios.length
    ? `<option value="">Sin asignar (admin recoge)</option>` + operarios.map(o => `<option value="${o.id}">${o.nombre}</option>`).join('')
    : `<option value="">No hay operarios disponibles</option>`;

  const modal = document.createElement('div');
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div style="background:#111;border-radius:16px;padding:24px;width:100%;max-width:440px;border:1px solid #166534;max-height:85vh;overflow-y:auto;">
        <div style="font-size:17px;font-weight:700;margin-bottom:4px;">Editar y aprobar requisición</div>
        <div style="font-size:12px;color:#666;margin-bottom:16px;">${solicitud.nombre_punto_venta || solicitud.bodega_destino_siesa || '—'}</div>

        <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:#aaa;">CANTIDADES A ENVIAR</div>
        ${filasItems}

        <div style="font-size:12px;font-weight:600;margin-top:14px;margin-bottom:8px;color:#aaa;">OPERARIO QUE RECOGE</div>
        <select id="req-apr-operario"
          style="width:100%;padding:10px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;margin-bottom:16px;">
          ${opcionesOperarios}
        </select>

        <div style="display:flex;gap:8px;">
          <button id="btn-req-apr-ok" style="flex:1;padding:12px;background:#166534;color:#fff;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">Aprobar</button>
          <button onclick="this.closest('[style*=fixed]').parentElement.remove()" style="padding:12px 16px;background:#222;color:#fff;border:none;border-radius:8px;font-size:14px;cursor:pointer;">Cancelar</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  modal.querySelector('#btn-req-apr-ok').onclick = async () => {
    const items_aprobados = items.map(i => ({
      id: i.id,
      cantidad_aprobada: Number(document.getElementById(`req-apr-${i.id}`).value) || 0
    }));
    const operario_id = document.getElementById('req-apr-operario').value
      ? Number(document.getElementById('req-apr-operario').value) : null;
    modal.remove();
    try {
      const r = await fetch(API + `/api/traslados/${id}/aprobar`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify({ items_aprobados, operario_id })
      });
      const d = await r.json();
      if (r.ok) {
        alerta(operario_id ? 'Aprobado — operario notificado' : 'Aprobado — sin operario asignado', 'exito');
        cargarRequisiciones();
      } else { alerta(d.error || 'Error', 'error'); }
    } catch (e) { alerta('Error de conexión', 'error'); }
  };
}



// TrasPicker/Packer + confirmar picking/packing — movido desde app.js 2026-07-21

/** @param {number} id - Transfer ID to confirm picking completion for. */
async function confirmarPickingTraslado(id) {
  if (!confirm('¿Confirmar que el picking de esta transferencia está completo?')) return;
  try {
    const r = await fetch(`/api/traslados/${id}/confirmar-picking`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al confirmar picking', 'error'); return; }
    alerta('Picking confirmado — pendiente de verificar empaque 📦', 'exito');
    await cargarRequisiciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

/** @param {number} id - Transfer ID to confirm packing completion for. */
async function confirmarPackingTraslado(id) {
  if (!confirm('¿Confirmar verificación de empaque? Esto disparará los compromisos en Siesa.')) return;
  try {
    const r = await fetch(`/api/traslados/${id}/confirmar-packing`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al confirmar packing', 'error'); return; }
    alerta('Empaque verificado — listo para despachar ✓', 'exito');
    await cargarRequisiciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ─── TRASLADOS — PICKING / PACKING ────────────────────────────────────────────
// Los roles picker_traslado y packer_traslado usan las pantallas unificadas
// (pantalla-operario y pantalla-empacador). El scoping por bodega_siesa_id
// en el backend garantiza que solo vean tareas tipo TRASLADO de su tienda.
// ─────────────────────────────────────────────────────────────────────────────

// PICKER TRASLADO (legacy — mantenido solo para cargarTrasladosOperario en pantalla-operario)

let TRAS_PICK = null;

/** Fetch and render the transfer picking queue for the store picker. */
async function trasPickerCargarCola() {
  const el = document.getElementById('tpick-lista');
  if (!el) return;
  try {
    const d = await get('/api/traslados/cola-picker');
    const solicitudes = d.solicitudes || [];
    if (!solicitudes.length) {
      el.innerHTML = '<div style="text-align:center;padding:60px 20px;color:#666;">Sin traslados para pickear ✓<br><button onclick="trasPickerCargarCola()" style="margin-top:20px;background:#222;border:1px solid #333;color:#fff;padding:10px 20px;border-radius:10px;cursor:pointer;">↻ Actualizar</button></div>';
      return;
    }
    el.innerHTML = solicitudes.map(s => {
      const items = s.items || [];
      const totalUnd = items.reduce((a, i) => a + (i.cantidad_aprobada || i.cantidad_solicitada || 0), 0);
      return `<div onclick="trasPickerAbrirHUD(${s.id})" style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:10px;cursor:pointer;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <div style="font-size:15px;font-weight:700;color:#fff;">${s.codigo}</div>
          <span style="font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;background:#dbeafe;color:#2563eb;">🔍 En picking</span>
        </div>
        <div style="font-size:12px;color:#666;margin-bottom:4px;">${s.nombre_punto_venta || s.bodega_destino_siesa}</div>
        <div style="font-size:12px;color:#666;">${items.length} producto(s) · ${totalUnd} uds</div>
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando cola</div>';
  }
}

/** @param {number} solicitudId - Transfer solicitud ID to open the picker HUD for. */
async function trasPickerAbrirHUD(solicitudId) {
  try {
    const d = await get(`/api/traslados/${solicitudId}/items-picking`);
    if (!d.items || !d.items.length) { alerta('Sin ítems para pickear', 'error'); return; }
    TRAS_PICK = { solicitudId, codigo: d.codigo, items: d.items, idx: 0, counts: {} };
    for (const it of d.items) TRAS_PICK.counts[it.item_id] = it.cantidad_recogida || 0;
    _trasPickerRenderHUD();
    document.getElementById('tpick-hud').style.display = 'block';
  } catch (e) { alerta('Error cargando traslado', 'error'); }
}

/** Render the current item in the transfer picker HUD. */
function _trasPickerRenderHUD() {
  if (!TRAS_PICK) return;
  const { items, idx, counts, codigo } = TRAS_PICK;
  const item    = items[idx];
  const cant    = counts[item.item_id] || 0;
  const req     = item.cantidad_aprobada || 0;
  const pct     = req > 0 ? Math.min(cant / req * 100, 100) : 0;
  const esUltimo = idx === items.length - 1;
  document.getElementById('tpick-hud-badge').textContent    = '🔄 TRANSFERENCIA — PICKING';
  document.getElementById('tpick-hud-progreso').textContent = `Ítem ${idx + 1} de ${items.length} · ${codigo}`;
  document.getElementById('tpick-hud-ubicacion').textContent = item.ubicacion || 'BODEGA';
  document.getElementById('tpick-hud-prod-codigo').textContent = item.producto_codigo;
  document.getElementById('tpick-hud-prod-nombre').textContent = item.producto_nombre;
  const contEl = document.getElementById('tpick-hud-contador');
  contEl.textContent = `${cant}/${req}`;
  contEl.style.color = cant >= req ? '#22c55e' : '#fff';
  const barEl = document.getElementById('tpick-hud-barra');
  barEl.style.width = pct + '%';
  barEl.style.background = cant >= req ? '#22c55e' : (cant > 0 ? '#f59e0b' : '#4b5563');
  const btnSig = document.getElementById('tpick-btn-sig');
  if (btnSig) {
    btnSig.textContent = esUltimo ? '✓ Confirmar picking' : '→ Siguiente ítem';
    btnSig.style.background = (esUltimo && cant >= req) ? '#16a34a' : '#7c3aed';
  }
}

/** @param {number} delta - Amount to add/subtract from the current transfer picking item count. */
function trasPickerDelta(delta) {
  if (!TRAS_PICK) return;
  const item = TRAS_PICK.items[TRAS_PICK.idx];
  TRAS_PICK.counts[item.item_id] = Math.max(0, Math.min((TRAS_PICK.counts[item.item_id] || 0) + delta, item.cantidad_aprobada));
  _trasPickerRenderHUD();
}

/** @param {string} codigo - Barcode scanned during transfer picking. */
async function trasPickerScan(codigo) {
  if (!TRAS_PICK) return;
  const item = TRAS_PICK.items[TRAS_PICK.idx];
  vibrar();
  if (item.tarea_picking_id) {
    try {
      const r = await post('/api/mobile/escanear', { codigo, tarea_id: item.tarea_picking_id, tipo: 'PICKING' });
      if (r.error) { beepError(); alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
      TRAS_PICK.counts[item.item_id] = r.cantidad_actual;
      _trasPickerRenderHUD();
      if (r.completado) beepDone(); else beepOk();
    } catch (e) { alerta('Error de escaneo', 'error'); }
  } else {
    const limpio  = (codigo || '').trim().toUpperCase();
    const validos = [item.producto_codigo, item.producto_codigo_barras].filter(Boolean).map(c => c.toUpperCase());
    if (!validos.includes(limpio)) { beepError(); alerta(`Código incorrecto — escanea ${item.producto_codigo}`, 'error'); return; }
    beepOk();
    trasPickerDelta(1);
  }
}

/** Prompt for manual quantity entry in transfer picking. */
function trasPickerManual() {
  if (!TRAS_PICK) return;
  const item = TRAS_PICK.items[TRAS_PICK.idx];
  const cantStr = prompt(`¿Cuántas unidades de ${item.producto_codigo} recogiste? (máx. ${item.cantidad_aprobada})`);
  if (cantStr === null) return;
  const cant = parseInt(cantStr, 10);
  if (isNaN(cant) || cant < 0 || cant > item.cantidad_aprobada) { alerta(`Ingresa un número entre 0 y ${item.cantidad_aprobada}`, 'error'); return; }
  TRAS_PICK.counts[item.item_id] = cant;
  _trasPickerRenderHUD();
}

/** Advance to the next item in transfer picking. */
async function trasPickerSiguiente() {
  if (!TRAS_PICK) return;
  if (TRAS_PICK.idx < TRAS_PICK.items.length - 1) { TRAS_PICK.idx++; _trasPickerRenderHUD(); return; }
  await _trasPickerConfirmar();
}

/** Submit all confirmed items for the transfer picking session. */
async function _trasPickerConfirmar() {
  if (!TRAS_PICK) return;
  const { solicitudId, items, counts } = TRAS_PICK;
  const items_confirmados = items.map(i => ({ id: i.item_id, cantidad_confirmada: counts[i.item_id] || 0 }));
  try {
    const r = await fetch(API + `/api/traslados/${solicitudId}/confirmar-picking`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ items_confirmados })
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al confirmar picking', 'error'); return; }
    beepDone();
    alerta('Picking confirmado ✓ — empaque pendiente', 'exito');
    trasPickerPausarHUD();
    await trasPickerCargarCola();
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Pause the transfer picker HUD and return to the queue. */
function trasPickerPausarHUD() {
  cerrarCamara('tpick-cambox');
  document.getElementById('tpick-hud').style.display = 'none';
  TRAS_PICK = null;
}

/** Report a problem during transfer picking. */
function trasPickerProblema() {
  if (!TRAS_PICK) return;
  alerta('Reporta el problema al supervisor — código: ' + TRAS_PICK.codigo, 'advertencia');
}


// PACKER TRASLADO

let TRAS_PACK = null;

/** Fetch and render the transfer packing queue for the store packer. */
async function trasPackerCargarCola() {
  const el = document.getElementById('tpack-lista');
  if (!el) return;
  try {
    const d = await get('/api/traslados/cola-packer');
    const solicitudes = d.solicitudes || [];
    if (!solicitudes.length) {
      el.innerHTML = '<div style="text-align:center;padding:60px 20px;color:#666;">Sin traslados para verificar ✓<br><button onclick="_refreshBtn(event,trasPackerCargarCola)" style="margin-top:20px;background:#e2e8f0;border:none;color:#1a202c;padding:10px 20px;border-radius:10px;cursor:pointer;">↻ Actualizar</button></div>';
      return;
    }
    el.innerHTML = '<div style="font-size:11px;font-weight:600;color:#718096;padding:16px 16px 8px;text-transform:uppercase;">TAREAS DE EMPAQUE</div>' +
      solicitudes.map(s => {
        const items = s.items || [];
        return `<div onclick="trasPackerAbrirHUD(${s.id})" style="background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin:0 12px 10px;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.06);">
          <div style="font-size:16px;font-weight:700;color:#1a202c;margin-bottom:4px;">${s.codigo}</div>
          <div style="font-size:13px;color:#718096;margin-bottom:8px;">${items.length} producto(s) · 0/${items.length} verificados</div>
          <span style="font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;background:#fff7ed;color:#ea580c;">Pendiente</span>
        </div>`;
      }).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando tareas</div>';
  }
}

/** @param {number} solicitudId - Transfer solicitud ID to open the packer HUD for. */
async function trasPackerAbrirHUD(solicitudId) {
  try {
    const d = await get(`/api/traslados/${solicitudId}/items-picking`);
    if (!d.items || !d.items.length) { alerta('Sin ítems para verificar', 'error'); return; }
    TRAS_PACK = { solicitudId, codigo: d.codigo, items: d.items, idx: 0, counts: {} };
    for (const it of d.items) TRAS_PACK.counts[it.item_id] = 0;
    _trasPackerRenderHUD();
    document.getElementById('tpack-hud').style.display = 'flex';
  } catch (e) { alerta('Error cargando traslado', 'error'); }
}

/** Render the current item in the transfer packer HUD. */
function _trasPackerRenderHUD() {
  if (!TRAS_PACK) return;
  const { items, counts } = TRAS_PACK;
  const pendientes  = items.filter(i => !(counts[i.item_id] >= (i.cantidad_enviada || i.cantidad_aprobada || 0)));
  const item        = pendientes[0] || items[0];
  const cant        = counts[item.item_id] || 0;
  const req         = item.cantidad_enviada || item.cantidad_aprobada || 0;
  const verificados = items.filter(i => counts[i.item_id] >= (i.cantidad_enviada || i.cantidad_aprobada || 0)).length;
  const pct         = req > 0 ? Math.min(cant / req * 100, 100) : 0;
  const todoListo   = verificados === items.length;
  document.getElementById('tpack-hud-codigo').textContent      = TRAS_PACK.codigo;
  document.getElementById('tpack-hud-prod-nombre').textContent = todoListo ? '¡Todo verificado! Confirma el empaque.' : (item.producto_nombre || item.producto_codigo);
  const cEl = document.getElementById('tpack-hud-contador');
  cEl.textContent = cant;
  cEl.style.color = todoListo ? '#16a34a' : '#0d9488';
  document.getElementById('tpack-hud-de').textContent          = 'de ' + req;
  const bEl = document.getElementById('tpack-hud-barra');
  bEl.style.width      = pct + '%';
  bEl.style.background = todoListo ? '#16a34a' : '#0d9488';
  document.getElementById('tpack-hud-items').textContent = verificados + ' de ' + items.length + ' ítems verificados';
  const btnConf = document.getElementById('tpack-btn-confirmar');
  if (btnConf) {
    btnConf.textContent      = todoListo ? 'Confirmar empaque ✓' : '→ Siguiente';
    btnConf.style.background = todoListo ? '#16a34a' : '#0d9488';
  }
}

/** @param {number} delta - Amount to add/subtract from the current transfer packing item count. */
function trasPackerDelta(delta) {
  if (!TRAS_PACK) return;
  const pendientes = TRAS_PACK.items.filter(i => !(TRAS_PACK.counts[i.item_id] >= (i.cantidad_enviada || i.cantidad_aprobada || 0)));
  const item = pendientes[0] || TRAS_PACK.items[0];
  const max  = item.cantidad_enviada || item.cantidad_aprobada || 0;
  TRAS_PACK.counts[item.item_id] = Math.max(0, Math.min((TRAS_PACK.counts[item.item_id] || 0) + delta, max));
  _trasPackerRenderHUD();
}

/** @param {string} codigo - Barcode scanned during transfer packing verification. */
function trasPackerScan(codigo) {
  if (!TRAS_PACK) return;
  const pendientes = TRAS_PACK.items.filter(i => !(TRAS_PACK.counts[i.item_id] >= (i.cantidad_enviada || i.cantidad_aprobada || 0)));
  const item = pendientes[0];
  if (!item) { beepOk(); return; }
  const limpio  = (codigo || '').trim().toUpperCase();
  const validos = [item.producto_codigo, item.producto_codigo_barras].filter(Boolean).map(c => c.toUpperCase());
  if (!validos.includes(limpio)) { beepError(); alerta('Código incorrecto — escanea ' + item.producto_codigo, 'error'); return; }
  vibrar(); beepOk(); trasPackerDelta(1);
}

/** Advance to the next item in transfer packing. */
async function trasPackerSiguiente() {
  if (!TRAS_PACK) return;
  const todoListo = TRAS_PACK.items.every(i => TRAS_PACK.counts[i.item_id] >= (i.cantidad_enviada || i.cantidad_aprobada || 0));
  if (!todoListo) { _trasPackerRenderHUD(); return; }
  await _trasPackerConfirmar();
}

/** Submit all confirmed items for the transfer packing session. */
async function _trasPackerConfirmar() {
  if (!TRAS_PACK) return;
  try {
    const r = await fetch(API + '/api/traslados/' + TRAS_PACK.solicitudId + '/confirmar-packing', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al confirmar empaque', 'error'); return; }
    beepDone();
    alerta('Empaque verificado — listo para despachar ✓', 'exito');
    trasPackerPausarHUD();
    await trasPackerCargarCola();
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Pause the transfer packer HUD and return to the queue. */
function trasPackerPausarHUD() {
  cerrarCamara('tpack-cambox');
  document.getElementById('tpack-hud').style.display = 'none';
  TRAS_PACK = null;
}

