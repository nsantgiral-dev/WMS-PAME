// ══════════════════════════════════════════════════════════════════════════════
// MÓDULO TIENDA — Pantalla punto de venta (solicitudes, stock, OCs)
// Dependencias globales (de app.js): get(), post(), alerta(), API, TOKEN,
//   OPERARIO, ALMACEN_ID, pantalla, actualizarUI
// Movido desde app.js 2026-07-21
// ══════════════════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════════════
// TIENDA — Pantalla punto de venta
// ══════════════════════════════════════════════════════════════════

const _BODEGAS_ORIGEN = [
  { id: 'NB1', nombre: 'Bodega Principal' },
  { id: 'NC1', nombre: 'Neiva Centro' },
  { id: 'NS1', nombre: 'Neiva Sur Principal' },
  { id: 'FC1', nombre: 'Florencia Centro' },
  { id: 'PC1', nombre: 'Pitalito Centro' },
  { id: 'PT1', nombre: 'Pitalito Terminal' },
  { id: 'FF1', nombre: 'Feria Florencia' },
  { id: 'FN1', nombre: 'Santa Lucía Plaza' },
  { id: 'FP1', nombre: 'Feria Pitalito' },
];

let _TIENDA_SUBTAB = 'solicitudes';
let _TIENDA_STOCK = [];          // cache del stock de la bodega origen seleccionada
let _TIENDA_STOCK_ESTADO = 'cargando'; // 'cargando' | 'listo' | 'error'
let _TIENDA_STOCK_META = { fuente: null, actualizado_en: null }; // de dónde y de cuándo es el stock mostrado
let _TIENDA_CARRITO = []; // [{producto_id, codigo_siesa, nombre, cantidad, disponible}]
let _TIENDA_ORIGEN = { id: 'NB1', nombre: 'Bodega Principal' }; // bodega fuente del pedido

// Estado de recepción de traslados (picking ítem por ítem)
let _TIENDA_PENDIENTES = [];       // cache traslados EN_TRANSITO+DESPACHADA
let _TIENDA_TRASLADO_ACTIVO = null; // solicitud abierta en picking
let _TIENDA_CONTEOS = {};          // {producto_id: cantidad_contada}

/** Initialize the tienda (store) screen with stock and request panels. */
function tiendaIniciar() {
  _TIENDA_STOCK = [];
  _TIENDA_STOCK_ESTADO = 'cargando';
  _TIENDA_CARRITO = [];

  const miTienda = OPERARIO?.bodega_siesa_id || '';
  const subtitulo = document.getElementById('tienda-subtitulo');
  if (subtitulo) subtitulo.textContent = miTienda ? `Punto de Venta · ${miTienda}` : 'Punto de Venta';

  // Poblar selector "Pedir desde" — excluir la propia tienda del usuario
  const sel = document.getElementById('tienda-destino-select');
  if (sel) {
    const opciones = _BODEGAS_ORIGEN.filter(b => b.id !== miTienda);
    sel.innerHTML = opciones.map(b =>
      `<option value="${b.id}" data-nombre="${b.nombre}"
        style="background:#0d2137;color:#fff;">${b.nombre} (${b.id})</option>`
    ).join('');
    // Pre-seleccionar NB1 como origen por defecto
    const porDefecto = opciones.find(b => b.id === 'NB1') || opciones[0];
    if (porDefecto) {
      sel.value = porDefecto.id;
      _TIENDA_ORIGEN = { id: porDefecto.id, nombre: porDefecto.nombre };
    }
  }

  tiendaSubtab('solicitudes');
  tiendaCargarStock();  // pre-carga stock en background
}

/** @param {HTMLSelectElement} sel - Origin warehouse selector that changed. */
function tiendaCambiarOrigen(sel) {
  const opt = sel.options[sel.selectedIndex];
  _TIENDA_ORIGEN = { id: sel.value, nombre: opt.dataset.nombre || sel.value };
  // Limpiar carrito y recargar stock de la nueva bodega origen
  _TIENDA_CARRITO = [];
  tiendaActualizarCarrito();
  _TIENDA_STOCK = [];
  _TIENDA_STOCK_ESTADO = 'cargando';
  tiendaCargarStock();
}

/** @param {string} nombre - Tienda sub-tab to activate ('pedir', 'solicitudes', 'recibir', 'compras'). */
function tiendaSubtab(nombre) {
  _TIENDA_SUBTAB = nombre;
  ['solicitudes','nueva','recibir','recibir-oc'].forEach(k => {
    const tab = document.getElementById(`tienda-tab-${k}`);
    const panel = document.getElementById(`tienda-panel-${k}`);
    const activo = k === nombre;
    if (tab) {
      tab.style.color = activo ? '#1E8395' : '#415A70';
      tab.style.fontWeight = activo ? '600' : '400';
      tab.style.borderBottomColor = activo ? '#1E8395' : 'transparent';
    }
    if (panel) panel.style.display = activo ? 'block' : 'none';
  });
  if (nombre === 'solicitudes') tiendaCargarSolicitudes();
  if (nombre === 'nueva') tiendaRenderStock();
  if (nombre === 'recibir') tiendaCargarRecibir();
  if (nombre === 'recibir-oc') tiendaOCCargar();
}

/** Fetch and render the store's transfer request history. */
async function tiendaCargarSolicitudes() {
  const el = document.getElementById('tienda-lista-solicitudes');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';
  try {
    const d = await get('/api/traslados/');
    const solicitudes = d.solicitudes || [];
    if (!solicitudes.length) {
      el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">No hay pedidos aún</div>';
      return;
    }
    el.innerHTML = solicitudes.map(s => {
      const col = TRAS_COL[s.estado] || '#333';
      return `
      <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <div style="font-size:13px;font-weight:700;">${s.codigo}</div>
          <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:3px 8px;border-radius:8px;">${s.estado}</span>
        </div>
        <div style="font-size:11px;color:#666;">${s.total_items} ítem${s.total_items !== 1 ? 's' : ''} · ${s.fecha_creacion ? new Date(s.fecha_creacion).toLocaleDateString('es-CO') : ''}</div>
        ${s.motivo_rechazo ? `<div style="font-size:11px;color:#f87171;margin-top:4px;">Motivo: ${s.motivo_rechazo}</div>` : ''}
        ${s.estado === 'BORRADOR' ? `
        <div style="display:flex;gap:8px;margin-top:10px;">
          <button onclick="tiendaEnviarSolicitudId(${s.id})"
            style="flex:1;padding:10px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">
            Enviar al almacén
          </button>
        </div>` : ''}
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error cargando pedidos</div>';
  }
}

/** Fetch stock data for the store's selected origin warehouse. */
async function tiendaCargarStock() {
  _TIENDA_STOCK_ESTADO = 'cargando';
  if (_TIENDA_SUBTAB === 'nueva') tiendaRenderStock();
  try {
    const bodega = _TIENDA_ORIGEN.id || 'NB1';
    const d = await get(`/api/traslados/stock-disponible?bodega=${bodega}`);
    _TIENDA_STOCK = (d.items || []).filter(i => i.producto_id && i.disponible > 0);
    _TIENDA_STOCK_META = { fuente: d.fuente || null, actualizado_en: d.actualizado_en || null };
    _TIENDA_STOCK_ESTADO = 'listo';
  } catch (e) {
    _TIENDA_STOCK_META = { fuente: null, actualizado_en: null };
    _TIENDA_STOCK_ESTADO = 'error';
  }
  if (_TIENDA_SUBTAB === 'nueva') tiendaRenderStock();
}

/**
 * Badge de frescura sobre la lista de "Pedir": de dónde salió el stock
 * mostrado (Siesa en vivo, respaldo local, o físico WMS) y hace cuánto.
 * Existe porque el backend puede caer en cascada (Siesa en vivo → snapshot
 * en BD → físico WMS) sin que la pantalla lo distinga — y sobre un número
 * viejo sin avisar, la tienda arma un pedido creyendo que es de ahora mismo.
 */
function tiendaRenderFrescura() {
  const el = document.getElementById('tienda-stock-frescura');
  if (!el) return;
  if (_TIENDA_STOCK_ESTADO !== 'listo' || !_TIENDA_STOCK_META.fuente) {
    el.innerHTML = '';
    return;
  }

  const { fuente, actualizado_en } = _TIENDA_STOCK_META;
  let minutos = null;
  if (actualizado_en) {
    const ms = Date.now() - new Date(actualizado_en).getTime();
    if (!isNaN(ms)) minutos = Math.max(0, Math.round(ms / 60000));
  }

  const ROTULOS = {
    siesa: 'Stock Siesa',
    siesa_bd_snapshot: 'Respaldo local — Siesa no respondió al último refresco',
    wms_fallback: 'Físico WMS — Siesa no disponible',
    sin_dato: 'Sin datos de stock',
  };
  const esAntiguo = fuente !== 'siesa' || (minutos !== null && minutos > 15);
  const color = esAntiguo ? '#f59e0b' : '#4ade80';
  const rotulo = ROTULOS[fuente] || fuente;
  const tiempo = minutos === null ? '' : (minutos < 1 ? ' · hace instantes' : ` · hace ${minutos} min`);

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:${color};">
      <span style="width:7px;height:7px;border-radius:50%;background:${color};flex-shrink:0;"></span>
      <span>${rotulo}${tiempo}</span>
    </div>`;
}

/** Refresh stock from the server for the tienda request form. */
async function tiendaActualizarStock() {
  const btn = document.getElementById('tienda-btn-refresh');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.4'; btn.textContent = '…'; }
  try {
    const bodega = _TIENDA_ORIGEN.id || 'NB1';
    await fetch(API + `/api/traslados/invalidar-cache-stock?bodega=${bodega}`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    _TIENDA_STOCK = [];
    _TIENDA_STOCK_ESTADO = 'cargando';
    await tiendaCargarStock();
  } catch (e) {
    alerta('Error actualizando stock', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.textContent = '↻'; }
  }
}

const _TIENDA_POR_PAGINA = 30;
let _TIENDA_FILTRO = '';
let _TIENDA_PAGINA = 1;

/** Apply text filter to the tienda stock list. */
function tiendaFiltrarStock() {
  _TIENDA_FILTRO = (document.getElementById('tienda-buscar')?.value || '').toLowerCase();
  _TIENDA_PAGINA = 1; // reset al filtrar
  tiendaRenderStock();
}

/** @param {number} p - Page number to navigate to in the tienda stock list. */
function tiendaIrPagina(p) {
  _TIENDA_PAGINA = p;
  tiendaRenderStock();
  document.getElementById('tienda-stock-lista')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Render the current page of tienda stock list with cart controls. */
function tiendaRenderStock() {
  tiendaRenderFrescura();
  const el = document.getElementById('tienda-stock-lista');
  if (!el) return;
  if (_TIENDA_STOCK_ESTADO === 'cargando') {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#555;">Consultando stock en bodega...</div>';
    return;
  }
  if (_TIENDA_STOCK_ESTADO === 'error') {
    el.innerHTML = `
      <div style="text-align:center;padding:40px 20px;">
        <div style="font-size:32px;margin-bottom:12px;">⚠️</div>
        <div style="font-size:15px;font-weight:600;color:#f87171;margin-bottom:8px;">No se pudo cargar el stock</div>
        <div style="font-size:13px;color:#555;margin-bottom:20px;">Verifica la conexión con Siesa o intenta de nuevo</div>
        <button onclick="tiendaCargarStock()"
          style="padding:12px 24px;background:#1d4ed8;color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">
          Reintentar
        </button>
      </div>`;
    return;
  }
  if (!_TIENDA_STOCK.length) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Sin productos disponibles en bodega</div>';
    return;
  }

  const filtrado = _TIENDA_FILTRO
    ? _TIENDA_STOCK.filter(i =>
        (i.nombre || '').toLowerCase().includes(_TIENDA_FILTRO) ||
        (i.codigo_siesa || '').toLowerCase().includes(_TIENDA_FILTRO))
    : _TIENDA_STOCK;

  if (!filtrado.length) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Sin resultados para "' + _TIENDA_FILTRO + '"</div>';
    return;
  }

  const totalPags = Math.ceil(filtrado.length / _TIENDA_POR_PAGINA);
  _TIENDA_PAGINA = Math.max(1, Math.min(_TIENDA_PAGINA, totalPags));
  const inicio = (_TIENDA_PAGINA - 1) * _TIENDA_POR_PAGINA;
  const pagina = filtrado.slice(inicio, inicio + _TIENDA_POR_PAGINA);

  // Paginación: muestra máximo 5 números de página centrados en la actual
  const rango = 2;
  const desde = Math.max(1, _TIENDA_PAGINA - rango);
  const hasta = Math.min(totalPags, _TIENDA_PAGINA + rango);
  const nums = [];
  if (desde > 1) nums.push('<span style="color:#555;padding:0 4px;">…</span>');
  for (let p = desde; p <= hasta; p++) {
    const activo = p === _TIENDA_PAGINA;
    nums.push(`<button onclick="tiendaIrPagina(${p})"
      style="min-width:32px;padding:6px 8px;background:${activo?'#1E8395':'#222'};color:#fff;border:none;border-radius:6px;font-size:12px;font-weight:${activo?'700':'400'};cursor:pointer;">${p}</button>`);
  }
  if (hasta < totalPags) nums.push('<span style="color:#555;padding:0 4px;">…</span>');

  const navHtml = totalPags > 1 ? `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 0 14px;flex-wrap:wrap;">
      <button onclick="tiendaIrPagina(${_TIENDA_PAGINA - 1})" ${_TIENDA_PAGINA===1?'disabled':''}
        style="padding:7px 14px;background:#222;color:${_TIENDA_PAGINA===1?'#444':'#fff'};border:none;border-radius:8px;font-size:13px;cursor:pointer;">← Ant</button>
      <div style="display:flex;align-items:center;gap:4px;flex-wrap:wrap;justify-content:center;">${nums.join('')}</div>
      <button onclick="tiendaIrPagina(${_TIENDA_PAGINA + 1})" ${_TIENDA_PAGINA===totalPags?'disabled':''}
        style="padding:7px 14px;background:#222;color:${_TIENDA_PAGINA===totalPags?'#444':'#fff'};border:none;border-radius:8px;font-size:13px;cursor:pointer;">Sig →</button>
    </div>
    <div style="text-align:center;font-size:11px;color:#555;margin-bottom:10px;">
      ${filtrado.length} productos · página ${_TIENDA_PAGINA} de ${totalPags}
    </div>` : '';

  el.innerHTML = navHtml + pagina.map(item => {
    const enCarrito = _TIENDA_CARRITO.find(c => c.codigo_siesa === item.codigo_siesa);
    const qid = 'qty-' + (item.codigo_siesa || '').replace(/[^a-zA-Z0-9]/g, '-');
    const nombreEsc = (item.nombre || '').replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return `
    <div style="background:#111;border:1px solid ${enCarrito?'#4ade80':'#222'};border-radius:10px;padding:12px;margin-bottom:8px;display:flex;align-items:center;gap:12px;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${item.nombre || '—'}</div>
        <div style="font-size:11px;color:#666;">${item.codigo_siesa || ''} · Disponible: <span style="color:#4ade80;font-weight:700;">${item.disponible}</span></div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
        <input type="number" min="1" max="${item.disponible}" value="${enCarrito?.cantidad || 1}"
          id="${qid}"
          style="width:56px;padding:7px;background:#000;border:1px solid #333;border-radius:6px;color:#fff;font-size:13px;text-align:center;">
        <button onclick="tiendaAgregarCarrito('${item.codigo_siesa}','${nombreEsc}',${item.disponible},${item.producto_id||'null'})"
          style="padding:8px 12px;background:${enCarrito?'#4ade80':'#fff'};color:#000;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
          ${enCarrito ? '✓' : '+'}
        </button>
      </div>
    </div>`;
  }).join('') + navHtml;
}

/**
 * @param {string} codigoSiesa - Siesa product code.
 * @param {string} nombre - Product name.
 * @param {number} disponible - Available stock.
 * @param {number} productoId - Product ID.
 */
function tiendaAgregarCarrito(codigoSiesa, nombre, disponible, productoId) {
  const inputId = `qty-${codigoSiesa.replace(/[^a-zA-Z0-9]/g,'-')}`;
  const cantidadInput = document.getElementById(inputId);
  const cantidad = Math.min(parseInt(cantidadInput?.value || 1), disponible);
  if (cantidad < 1) return;

  const idx = _TIENDA_CARRITO.findIndex(c => c.codigo_siesa === codigoSiesa);
  if (idx >= 0) {
    _TIENDA_CARRITO[idx].cantidad = cantidad;
  } else {
    _TIENDA_CARRITO.push({ codigo_siesa: codigoSiesa, nombre, disponible, cantidad, producto_id: productoId });
  }
  tiendaActualizarCarrito();
  tiendaRenderStock();
}

/** Re-render the tienda transfer cart summary. */
function tiendaActualizarCarrito() {
  const header = document.getElementById('tienda-carrito-header');
  const itemsEl = document.getElementById('tienda-carrito-items');
  if (!header || !itemsEl) return;
  if (!_TIENDA_CARRITO.length) { header.style.display = 'none'; return; }
  header.style.display = 'block';
  itemsEl.innerHTML = _TIENDA_CARRITO.map(c =>
    `<div style="display:flex;justify-content:space-between;padding:3px 0;">
      <span>${c.nombre}</span>
      <span style="color:#4ade80;font-weight:700;">${c.cantidad} und
        <button onclick="tiendaQuitarCarrito('${c.codigo_siesa}')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:11px;margin-left:4px;">✕</button>
      </span>
    </div>`
  ).join('');
}

/** @param {string} codigoSiesa - Product code to remove from tienda cart. */
function tiendaQuitarCarrito(codigoSiesa) {
  _TIENDA_CARRITO = _TIENDA_CARRITO.filter(c => c.codigo_siesa !== codigoSiesa);
  tiendaActualizarCarrito();
  tiendaRenderStock();
}

/** Submit the tienda transfer request with cart items. */
async function tiendaEnviarSolicitud() {
  if (!_TIENDA_CARRITO.length) { alerta('El carrito está vacío', 'error'); return; }
  const origen = _TIENDA_ORIGEN.nombre || _TIENDA_ORIGEN.id || 'la bodega';
  const miTienda = OPERARIO?.nombre_punto_venta || OPERARIO?.bodega_siesa_id || 'mi tienda';
  if (!confirm(`¿Solicitar ${_TIENDA_CARRITO.length} producto${_TIENDA_CARRITO.length !== 1 ? 's' : ''} desde ${origen} para ${miTienda}?`)) return;

  const items = _TIENDA_CARRITO
    .filter(c => c.producto_id)
    .map(c => ({
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
        bodega_origen_siesa: _TIENDA_ORIGEN.id,
        bodega_destino_siesa: OPERARIO?.bodega_siesa_id || undefined,
        nombre_punto_venta: OPERARIO?.nombre_punto_venta || OPERARIO?.bodega_siesa_id || undefined,
      })
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error creando solicitud', 'error'); return; }

    // Enviar inmediatamente
    const r2 = await fetch(API + `/api/traslados/${d.id}/enviar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    if (r2.ok) {
      alerta('Pedido enviado al almacén', 'exito');
      _TIENDA_CARRITO = [];
      tiendaActualizarCarrito();
      tiendaSubtab('solicitudes');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** @param {number} id - Transfer solicitud ID to resend to the warehouse. */
async function tiendaEnviarSolicitudId(id) {
  try {
    const r = await fetch(API + `/api/traslados/${id}/enviar`, {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) { alerta('Pedido enviado', 'exito'); tiendaCargarSolicitudes(); }
    else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ─────────────────────────────────────────────────────────────
// TIENDA — Recepción de traslados (picking ítem por ítem)
// ─────────────────────────────────────────────────────────────

/** Fetch and render transfers pending reception at the store. */
async function tiendaCargarRecibir() {
  const listaView = document.getElementById('tienda-recibir-lista-view');
  const pickingView = document.getElementById('tienda-recibir-picking-view');
  if (listaView) listaView.style.display = 'block';
  if (pickingView) pickingView.style.display = 'none';
  _TIENDA_TRASLADO_ACTIVO = null;

  const el = document.getElementById('tienda-lista-recibir');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';
  try {
    const [r1, r2] = await Promise.all([
      get('/api/traslados/?estado=EN_TRANSITO'),
      get('/api/traslados/?estado=DESPACHADA'),
    ]);
    _TIENDA_PENDIENTES = [...(r1.solicitudes || []), ...(r2.solicitudes || [])]
      .sort((a, b) => new Date(b.fecha_creacion) - new Date(a.fecha_creacion));

    const badgeEl = document.getElementById('badge-recibir');
    if (badgeEl) {
      badgeEl.style.display = _TIENDA_PENDIENTES.length ? 'inline' : 'none';
      badgeEl.textContent = _TIENDA_PENDIENTES.length;
    }

    if (!_TIENDA_PENDIENTES.length) {
      el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Nada pendiente de recibir</div>';
      return;
    }
    el.innerHTML = _TIENDA_PENDIENTES.map(s => {
      const totalEsperado = (s.items || []).reduce((a, i) => a + (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0), 0);
      return `
      <div style="background:#0a1a0a;border:1px solid #166534;border-radius:12px;padding:14px;margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
          <div style="font-size:14px;font-weight:700;">${s.codigo}</div>
          <div style="font-size:11px;color:#4ade80;font-weight:600;">${s.bodega_origen_siesa || ''}</div>
        </div>
        <div style="font-size:12px;color:#4ade80;margin-bottom:8px;">📦 ${s.total_items} ítem${s.total_items !== 1 ? 's' : ''} · ${totalEsperado} und esperadas</div>
        ${(s.items || []).slice(0, 3).map(i => `
          <div style="font-size:11px;color:#aaa;padding:2px 0;">
            ${i.producto_nombre || i.producto_codigo} · ${i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada} und
          </div>
        `).join('')}
        ${(s.items || []).length > 3 ? `<div style="font-size:11px;color:#555;padding:2px 0;">+ ${s.items.length - 3} más...</div>` : ''}
        <button onclick="tiendaAbrirPickingTraslado(${s.id})"
          style="width:100%;padding:13px;margin-top:12px;background:#1E8395;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;">
          📋 Contar productos
        </button>
      </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error cargando traslados</div>';
  }
}

/** @param {number} id - Transfer ID to open the store counting screen for. */
function tiendaAbrirPickingTraslado(id) {
  const s = _TIENDA_PENDIENTES.find(x => x.id === id);
  if (!s) return;
  _TIENDA_TRASLADO_ACTIVO = s;
  // Inicializar conteos en 0
  _TIENDA_CONTEOS = {};
  (s.items || []).forEach(i => { _TIENDA_CONTEOS[i.producto_id] = 0; });

  const listaView = document.getElementById('tienda-recibir-lista-view');
  const pickingView = document.getElementById('tienda-recibir-picking-view');
  if (listaView) listaView.style.display = 'none';
  if (pickingView) {
    pickingView.style.display = 'block';
    _tiendaRenderPickingTraslado();
    // Autofocus para que el scanner físico funcione sin tocar la pantalla
    setTimeout(() => {
      const inp = document.getElementById('tienda-scan-input');
      if (inp) inp.focus();
    }, 150);
  }
}

/** Exit store transfer counting and return to the receive list. */
function tiendaVolverListaRecibir() {
  _TIENDA_TRASLADO_ACTIVO = null;
  _TIENDA_CONTEOS = {};
  tiendaCargarRecibir();
}

/** Render the store transfer counting screen with scan input and item list. */
function _tiendaRenderPickingTraslado() {
  const s = _TIENDA_TRASLADO_ACTIVO;
  const el = document.getElementById('tienda-recibir-picking-view');
  if (!s || !el) return;

  const items = s.items || [];
  const todoContado = items.every(i => (_TIENDA_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  const algoContado = items.some(i => (_TIENDA_CONTEOS[i.producto_id] || 0) > 0);
  const btnColor = todoContado ? '#16a34a' : '#b45309';
  const btnTexto = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial';

  el.innerHTML = `
    <div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <button onclick="tiendaVolverListaRecibir()"
          style="background:#222;border:1px solid #333;color:#fff;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:14px;flex-shrink:0;">
          ← Volver
        </button>
        <div style="min-width:0;">
          <div style="font-size:16px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${s.codigo}</div>
          <div style="font-size:12px;color:#666;">Desde ${s.bodega_origen_siesa || '—'} → ${s.bodega_destino_siesa || '—'}</div>
        </div>
      </div>

      <!-- Escaneo / entrada manual -->
      <div style="background:#111;border-radius:10px;padding:12px;margin-bottom:14px;">
        <div style="font-size:12px;color:#666;text-align:center;margin-bottom:8px;">Escanea el código de barras o usá los botones +/−</div>
        <div style="display:flex;gap:8px;">
          <input id="tienda-scan-input" type="text" placeholder="Escanea o escribe el código..."
            style="flex:1;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ tiendaScanTraslado(v); this.value=''; } }"
            autocomplete="off" autocorrect="off" spellcheck="false">
          <button onclick="const v=document.getElementById('tienda-scan-input').value.trim();if(v){tiendaScanTraslado(v);document.getElementById('tienda-scan-input').value='';}"
            style="padding:10px 14px;background:#1E8395;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
        </div>
      </div>

      <div id="tienda-picking-items" style="margin-bottom:14px;">
        ${_tiendaRenderItemsPickingTraslado(items)}
      </div>

      <button id="btn-confirmar-traslado" onclick="tiendaConfirmarRecepcionTraslado()"
        ${algoContado || todoContado ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:18px;font-weight:700;background:${algoContado || todoContado ? btnColor : '#222'};color:#fff;border:none;border-radius:14px;cursor:${algoContado || todoContado ? 'pointer' : 'default'};margin-bottom:10px;">
        ${algoContado || todoContado ? btnTexto : 'Contá al menos un ítem para continuar'}
      </button>

      <button onclick="tiendaVolverListaRecibir()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
        Cancelar — volver a la lista
      </button>
    </div>`;
}

/**
 * @param {Array<Object>} items - Transfer items to render in store counting view.
 * @returns {string} HTML string.
 */
function _tiendaRenderItemsPickingTraslado(items) {
  return items.map(i => {
    const esperado = i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0;
    const contado = _TIENDA_CONTEOS[i.producto_id] || 0;
    const completo = contado >= esperado;
    const pct = esperado > 0 ? Math.min((contado / esperado) * 100, 100) : 0;
    return `
      <div id="tienda-item-pick-${i.producto_id}"
        style="background:${completo ? '#0d1a0d' : '#111'};border:1px solid ${completo ? '#166534' : '#222'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:14px;font-weight:600;color:${completo ? '#4ade80' : '#fff'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${i.producto_nombre || i.producto_codigo}</div>
            <div style="font-size:11px;color:#555;margin-top:2px;">${i.producto_codigo_siesa || i.producto_codigo}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;padding-left:8px;">
            <button onclick="tiendaContarItem(${i.producto_id}, -1)"
              style="width:34px;height:34px;background:#222;border:1px solid #333;color:#fff;border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">−</button>
            <div style="text-align:center;min-width:54px;">
              <div style="font-size:26px;font-weight:900;line-height:1;color:${completo ? '#4ade80' : '#fff'};">${contado}</div>
              <div style="font-size:10px;color:#6b7280;">/ ${esperado}</div>
            </div>
            <button onclick="tiendaContarItem(${i.producto_id}, 1)"
              style="width:34px;height:34px;background:#1E8395;border:none;color:#fff;border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">+</button>
          </div>
        </div>
        <div style="height:5px;background:#222;border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#16a34a' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.2s;"></div>
        </div>
      </div>`;
  }).join('');
}

/** @param {string} codigo - Barcode scanned during store transfer counting. */
async function tiendaScanTraslado(codigo) {
  if (!_TIENDA_TRASLADO_ACTIVO) return;
  const items = _TIENDA_TRASLADO_ACTIVO.items || [];
  let item = null;
  let delta = 1;

  // Resolver siempre via API para detectar empaques (factor_conversion)
  try {
    const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
    if (prod && prod.producto_id) {
      item = items.find(i => i.producto_id === prod.producto_id);
      if (prod.es_empaque && prod.factor_conversion > 1) {
        delta = prod.factor_conversion;
      }
    }
  } catch (_) { /* API falló — intentar match directo */ }

  // Fallback: match directo por codigo (sin detección de empaque)
  if (!item) {
    item = items.find(i =>
      i.producto_codigo_siesa === codigo ||
      i.producto_codigo === codigo
    );
  }

  if (!item) {
    alerta('Código no encontrado en este traslado: ' + codigo, 'error');
    return;
  }

  tiendaContarItem(item.producto_id, delta);
  if (delta > 1) alerta(`Empaque escaneado → +${delta} UND`, 'info');
  // Re-enfocar el input para el próximo escaneo
  const inp = document.getElementById('tienda-scan-input');
  if (inp) inp.focus();
}

/**
 * @param {number} productoId - Product ID.
 * @param {number} delta - Amount to add (or subtract).
 */
function tiendaContarItem(productoId, delta) {
  if (!_TIENDA_TRASLADO_ACTIVO) return;
  const item = (_TIENDA_TRASLADO_ACTIVO.items || []).find(i => i.producto_id === productoId);
  if (!item) return;
  const actual = _TIENDA_CONTEOS[productoId] || 0;
  const esperado = item.cantidad_enviada || item.cantidad_aprobada || item.cantidad_solicitada || 0;
  _TIENDA_CONTEOS[productoId] = Math.max(0, Math.min(actual + delta, esperado));
  // Re-render solo los ítems y el botón para no perder el scroll
  const itemsEl = document.getElementById('tienda-picking-items');
  if (itemsEl) itemsEl.innerHTML = _tiendaRenderItemsPickingTraslado(_TIENDA_TRASLADO_ACTIVO.items || []);
  // Actualizar botón confirmar
  const items = _TIENDA_TRASLADO_ACTIVO.items || [];
  const todoContado = items.every(i => (_TIENDA_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  const algoContado = items.some(i => (_TIENDA_CONTEOS[i.producto_id] || 0) > 0);
  const btn = document.getElementById('btn-confirmar-traslado');
  if (btn) {
    btn.disabled = !(algoContado || todoContado);
    btn.style.background = !algoContado && !todoContado ? '#222' : (todoContado ? '#16a34a' : '#b45309');
    btn.textContent = !algoContado && !todoContado ? 'Contá al menos un ítem para continuar' : (todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial');
    btn.style.cursor = algoContado || todoContado ? 'pointer' : 'default';
  }
}

/** Confirm the store's transfer reception with counted items. */
async function tiendaConfirmarRecepcionTraslado() {
  const s = _TIENDA_TRASLADO_ACTIVO;
  if (!s) return;

  const items = s.items || [];
  const todoContado = items.every(i => (_TIENDA_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));

  if (!todoContado) {
    const ok = await _confirmarModal(
      '⚠ Recepción incompleta',
      'Hay ítems sin contar o con cantidad menor a la esperada. ¿Confirmar como <strong>recepción parcial</strong>?',
      'Sí, confirmar parcial', 'Cancelar'
    );
    if (!ok) return;
  }

  const btn = document.getElementById('btn-confirmar-traslado');
  if (btn) { btn.textContent = 'Confirmando...'; btn.disabled = true; }

  try {
    const r = await fetch(API + `/api/traslados/${s.id}/recibir`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      // **Los conteos que la tienda acaba de hacer.** Iban vacíos: se contaba
      // ítem por ítem, se veían las barras de progreso, el modal decía
      // «¿confirmar como recepción parcial?» — y el cuerpo salía `{}`. El
      // servidor rellenaba `recibida = enviada` para TODO, así que una tienda
      // que contaba 3 de 10 hacía que el WMS y Siesa registraran 10.
      //
      // Siete unidades sin traza, y `TRA-01` —que exige enviada ≥ recibida— no
      // podía dispararse porque los dos valores se escribían iguales.
      //
      // Es la misma vía y el mismo endpoint que usa Recepción, que sí manda los
      // conteos (`recepcion.js`). Un endpoint con dos llamadores, uno honesto y
      // otro no, indistinguibles desde el servidor.
      body: JSON.stringify({
        items_recibidos: items.map(i => ({
          id: i.id,
          cantidad_recibida: _TIENDA_CONTEOS[i.producto_id] || 0,
        })),
      })
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Recepción confirmada — Siesa registró la entrada en tránsito', 'exito');
      _TIENDA_TRASLADO_ACTIVO = null;
      _TIENDA_CONTEOS = {};
      setTimeout(tiendaCargarRecibir, 1200);
    } else {
      const yaEntregado = (d.error || '').toLowerCase().includes('entregada');
      if (yaEntregado) {
        alerta('Este traslado ya fue recibido — actualizando lista', 'info');
        _TIENDA_TRASLADO_ACTIVO = null;
        _TIENDA_CONTEOS = {};
        setTimeout(tiendaCargarRecibir, 800);
      } else {
        alerta(d.error || 'Error al confirmar', 'error');
        if (btn) { btn.textContent = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial'; btn.disabled = false; }
      }
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
    if (btn) { btn.textContent = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial'; btn.disabled = false; }
  }
}

// _condConfirmarEntrega eliminado — reemplazado por el flujo por parada


// ══════════════════════════════════════════════════════════════════════════════
// TIENDA — Recepción de OCs (Órdenes de Compra)
// Flujo SOLID independiente: usa /api/tienda-oc/ (no toca recepcion)
// ══════════════════════════════════════════════════════════════════════════════

let _TIENDA_OCS = [];
let _TIENDA_OC_RECEPCION = null;

/** Fetch and render pending purchase orders for the store. */
async function tiendaOCCargar() {
  const listaView = document.getElementById('tienda-oc-lista-view');
  const scanView = document.getElementById('tienda-oc-scan-view');
  if (listaView) listaView.style.display = 'block';
  if (scanView) scanView.style.display = 'none';
  _TIENDA_OC_RECEPCION = null;

  const el = document.getElementById('tienda-oc-lista');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando OCs...</div>';

  try {
    const siesa = await get('/api/tienda-oc/').catch(() => ({ ordenes: [] }));
    _TIENDA_OCS = siesa.ordenes || [];

    const badgeEl = document.getElementById('badge-recibir-oc');
    if (badgeEl) {
      const pendientes = _TIENDA_OCS.filter(oc => !oc.recepcion_wms_estado || oc.recepcion_wms_estado === 'ABIERTA' || oc.recepcion_wms_estado === 'EN_PROCESO');
      badgeEl.style.display = pendientes.length ? 'inline' : 'none';
      badgeEl.textContent = pendientes.length;
    }

    if (siesa.simulado) {
      el.innerHTML = `<div style="background:#1a1a00;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#facc15;border:1px solid #333300;">
        Connekta en simulación — conecta credenciales para ver OCs reales de Siesa
      </div>`;
      return;
    }

    if (!_TIENDA_OCS.length) {
      el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">No hay OCs pendientes para este punto de venta</div>';
      return;
    }

    let html = '<div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 6px;border-bottom:1px solid #222;margin-bottom:8px;">OCs PENDIENTES EN SIESA</div>';
    html += _TIENDA_OCS.map((oc, i) => {
      const sinProd = oc.items.filter(it => !it.producto_id).length;
      const totalUds = oc.items.reduce((s, it) => s + (it.cantidad_pendiente || 0), 0);
      const wmsEstado = oc.recepcion_wms_estado;

      if (wmsEstado === 'CONFIRMADA') {
        return `
          <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:10px;opacity:0.6;">
            <div style="font-size:16px;font-weight:800;">OC: ${oc.numero_oc}</div>
            <div style="font-size:12px;color:#888;margin-bottom:8px;">${oc.proveedor || 'Sin proveedor'} · ${oc.items.length} productos · ${totalUds} uds</div>
            <div style="padding:10px;background:#0d1a0d;border-radius:8px;font-size:14px;font-weight:700;color:#4ade80;text-align:center;">
              Recepcionada en WMS
            </div>
          </div>`;
      }

      const enProceso = wmsEstado === 'EN_PROCESO' && oc.recepcion_wms_id;
      const btnLabel = enProceso ? 'Continuar recepción' : 'Iniciar recepción';
      const btnBg = enProceso ? '#1d4ed8' : '#1E8395';
      const btnClick = enProceso ? `tiendaOCContinuar(${oc.recepcion_wms_id})` : `tiendaOCIniciar(${i})`;
      return `
        <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:10px;">
          <div style="font-size:16px;font-weight:800;">OC: ${oc.numero_oc}</div>
          <div style="font-size:12px;color:#888;margin-bottom:4px;">${oc.proveedor || 'Sin proveedor'} · ${oc.items.length} productos · ${totalUds} uds</div>
          ${sinProd ? `<div style="font-size:11px;color:#f59e0b;margin-bottom:4px;">${sinProd} producto(s) sin registrar en WMS</div>` : ''}
          <button onclick="${btnClick}"
            style="width:100%;padding:14px;margin-top:8px;background:${btnBg};color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;">
            ${btnLabel}
          </button>
        </div>`;
    }).join('');
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error cargando OCs</div>';
  }
}

/** @param {number} idx - Index into SIESA_OCS for the store OC to start receiving. */
async function tiendaOCIniciar(idx) {
  const oc = _TIENDA_OCS[idx];
  if (!oc) return;

  const itemsValidos = oc.items.filter(it => it.producto_id);
  if (!itemsValidos.length) {
    alerta('Ningún producto está registrado en el WMS', 'error');
    return;
  }

  try {
    const r = await post('/api/tienda-oc/iniciar', {
      numero_oc: oc.numero_oc,
      tipo_docto: oc.tipo_docto,
      consec_docto: oc.consec_docto,
      co: oc.co,
      proveedor: oc.proveedor,
      proveedor_codigo: oc.proveedor_codigo,
      sucursal_prov: oc.sucursal_prov,
      cond_pago: oc.cond_pago,
      items: itemsValidos
    });
    if (r.error) { alerta(r.error, 'error'); return; }
    _TIENDA_OC_RECEPCION = r.recepcion;
    _tiendaOCRenderScan();
  } catch (e) {
    alerta(e.message || 'Error iniciando recepción', 'error');
  }
}

/** @param {number} id - Reception ID to resume store OC scanning. */
async function tiendaOCContinuar(id) {
  try {
    const r = await get('/api/tienda-oc/' + id);
    if (r.error) { alerta(r.error, 'error'); return; }
    _TIENDA_OC_RECEPCION = r;
    _tiendaOCRenderScan();
  } catch (e) { alerta('Error cargando recepción', 'error'); }
}

/** Render the store OC blind scanning screen. */
function _tiendaOCRenderScan() {
  const rec = _TIENDA_OC_RECEPCION;
  if (!rec) return;
  const listaView = document.getElementById('tienda-oc-lista-view');
  const scanView = document.getElementById('tienda-oc-scan-view');
  if (listaView) listaView.style.display = 'none';
  if (scanView) scanView.style.display = 'block';

  const itemsOC = (rec.items || []).filter(it => it.tipo !== 'BONIFICACION');
  const todoCompleto = itemsOC.length > 0 && itemsOC.every(it => it.cantidad_recibida >= it.cantidad_ordenada);
  const algoEscaneado = (rec.items || []).some(it => it.cantidad_recibida > 0);
  const btnColor = todoCompleto ? '#16a34a' : '#b45309';
  const btnTexto = todoCompleto ? 'Confirmar recepción' : 'Confirmar recepción parcial';
  const btnActivo = algoEscaneado || todoCompleto;

  scanView.innerHTML = `
    <div>
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <button onclick="tiendaOCVolverLista()"
          style="background:#222;border:1px solid #333;color:#fff;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:14px;flex-shrink:0;">
          ← Volver
        </button>
        <div style="min-width:0;">
          <div style="font-size:16px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">OC: ${rec.numero_oc_siesa}</div>
          <div style="font-size:12px;color:#666;">${rec.proveedor_nombre || ''}</div>
        </div>
      </div>

      <div style="background:#111;border-radius:10px;padding:12px;margin-bottom:12px;">
        <div style="font-size:12px;color:#666;text-align:center;margin-bottom:10px;">Escanea unidad, caja o paca — el sistema calcula las unidades</div>
        <button onclick="abrirCamara('lector-qr-toc','camara-box-toc', cod => { cerrarCamara('camara-box-toc'); tiendaOCProcesarScan(cod); })"
          style="width:100%;padding:13px;font-size:16px;background:#fff;color:#000;border:2px solid #000;border-radius:10px;cursor:pointer;margin-bottom:8px;">
          📷 Escanear con cámara
        </button>
        <div id="camara-box-toc" style="display:none;margin-bottom:8px;">
          <div id="lector-qr-toc" style="border-radius:10px;overflow:hidden;"></div>
          <button onclick="cerrarCamara('camara-box-toc')" style="width:100%;padding:9px;margin-top:6px;font-size:14px;background:#333;color:#fff;border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
        </div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <input id="toc-codigo-manual" type="text" placeholder="O escribe / pega el código aquí"
            style="flex:1;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ tiendaOCProcesarScan(v); this.value=''; } }"
            autocomplete="off" autocorrect="off" spellcheck="false">
          <button onclick="const v=document.getElementById('toc-codigo-manual').value.trim();if(v){tiendaOCProcesarScan(v);document.getElementById('toc-codigo-manual').value='';}"
            style="padding:10px 14px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
        </div>
        <button onclick="tiendaOCBuscarManual()"
          style="width:100%;padding:10px;font-size:14px;background:#1a1a1a;color:#9ca3af;border:1px solid #333;border-radius:8px;cursor:pointer;">
          📦 Sin código — buscar producto manualmente
        </button>
      </div>

      <div id="toc-items-list" style="margin-bottom:14px;">
        ${_tiendaOCRenderItems(rec.items || [])}
      </div>

      <button id="btn-confirmar-toc" onclick="tiendaOCConfirmar()" ${btnActivo ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:20px;font-weight:700;background:${btnActivo ? btnColor : '#222'};color:#fff;border:none;border-radius:14px;cursor:${btnActivo ? 'pointer' : 'default'};margin-bottom:10px;">
        ${btnActivo ? (todoCompleto ? '✓ ' : '⚠ ') + btnTexto : 'Escanea al menos un ítem para continuar'}
      </button>

      <button onclick="tiendaOCModalObsequio()"
        style="width:100%;padding:13px;font-size:15px;font-weight:600;background:#1a1a2e;color:#a78bfa;border:1px solid #4c1d95;border-radius:10px;cursor:pointer;margin-bottom:8px;">
        🎁 Registrar Obsequio / Bonificación
      </button>

      <button onclick="tiendaOCVolverLista()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
        Guardar y salir (continuar más tarde)
      </button>
    </div>`;

  setTimeout(() => {
    const inp = document.getElementById('toc-codigo-manual');
    if (inp) inp.focus();
  }, 150);
}

/**
 * @param {Array<Object>} items - Store OC reception items to render.
 * @returns {string} HTML string.
 */
function _tiendaOCRenderItems(items) {
  return items.map(it => {
    const esBono = it.tipo === 'BONIFICACION';
    if (esBono) {
      return `
        <div style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:12px;padding:14px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div style="min-width:0;flex:1;">
              <div style="display:flex;align-items:center;gap:8px;">
                <span style="background:#4c1d95;color:#a78bfa;font-size:10px;font-weight:700;padding:2px 7px;border-radius:20px;">🎁 BONO</span>
                <div style="font-size:14px;font-weight:600;color:#a78bfa;">${it.producto_nombre || it.producto_codigo}</div>
              </div>
              <div style="font-size:11px;color:#555;margin-top:2px;">${it.producto_codigo}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;padding-left:8px;">
              <div style="font-size:28px;font-weight:900;color:#a78bfa;">${it.cantidad_recibida}</div>
              <div style="font-size:10px;color:#6b7280;">und recibidas</div>
            </div>
          </div>
        </div>`;
    }

    const pct = it.cantidad_ordenada > 0 ? Math.min((it.cantidad_recibida / it.cantidad_ordenada) * 100, 100) : 0;
    const completo = it.cantidad_recibida >= it.cantidad_ordenada;
    const factor = it.factor_conversion || 1;
    const empaques = it.empaques_escaneados || 0;
    const unidadEmpaque = (it.unidad_empaque || '').trim() || 'emp';
    const modoEmpaque = factor > 1;

    const contadorDerecha = modoEmpaque ? `
      <div style="text-align:right;flex-shrink:0;padding-left:8px;">
        <div style="font-size:42px;font-weight:900;line-height:1;color:${completo ? '#4ade80' : '#fff'};">${empaques}</div>
        <div style="font-size:13px;font-weight:700;color:${completo ? '#4ade80' : '#facc15'};">${it.cantidad_recibida}/${it.cantidad_ordenada} und</div>
        <div style="font-size:10px;color:#6b7280;">${unidadEmpaque} · ×${factor}</div>
      </div>` : `
      <div style="text-align:right;flex-shrink:0;padding-left:8px;">
        <div style="font-size:28px;font-weight:900;color:${completo ? '#4ade80' : '#fff'};">${it.cantidad_recibida}/${it.cantidad_ordenada}</div>
      </div>`;

    return `
      <div style="background:${completo ? '#0d1a0d' : '#111'};border:1px solid ${completo ? '#166534' : '#222'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:14px;font-weight:600;color:${completo ? '#4ade80' : '#fff'};">${it.producto_nombre || it.producto_codigo}</div>
            <div style="font-size:11px;color:#555;">${it.producto_codigo}</div>
          </div>
          ${contadorDerecha}
        </div>
        <div style="height:5px;background:#222;border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#16a34a' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.3s;"></div>
        </div>
      </div>`;
  }).join('');
}

/** @param {string} codigo - Barcode scanned during store OC reception. */
async function tiendaOCProcesarScan(codigo) {
  if (!_TIENDA_OC_RECEPCION) return;
  vibrar(); flash();

  const items = _TIENDA_OC_RECEPCION.items || [];

  // Resolver siempre via API para detectar empaques (factor_conversion)
  try {
    const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
    if (prod && prod.producto_id) {
      const esEmpaque = prod.es_empaque || false;
      const factor = prod.factor_conversion || 1;

      await _tiendaOCRegistrarScan(prod.producto_id, 1, esEmpaque, false);
      if (esEmpaque && factor > 1) alerta(`Empaque escaneado → +${factor} UND`, 'info');
      return;
    }
  } catch (_) { /* API falló — intentar match directo */ }

  // Fallback: match directo contra items (sin detección de empaque)
  const item = items.find(i =>
    i.producto_codigo === codigo ||
    (i.producto_codigo_siesa && i.producto_codigo_siesa === codigo)
  );

  if (!item) {
    beepError();
    alerta('Código no reconocido: ' + codigo, 'error');
    return;
  }

  await _tiendaOCRegistrarScan(item.producto_id, 1, false, false);
}

/**
 * @param {number} productoId - Product ID.
 * @param {number} cantidad - Quantity scanned.
 * @param {boolean} esEmpaque - Whether scanning a package unit.
 * @param {boolean} esBonificacion - Whether this is a bonus item.
 */
async function _tiendaOCRegistrarScan(productoId, cantidad, esEmpaque, esBonificacion) {
  let r;
  try {
    r = await post('/api/tienda-oc/' + _TIENDA_OC_RECEPCION.id + '/escanear', {
      producto_id: productoId,
      cantidad: cantidad,
      es_empaque: esEmpaque,
      es_bonificacion: esBonificacion
    });
  } catch (e) {
    const body = e.body || {};
    if ((e.status === 400 || e.status === 409) && body.tipo === 'PRODUCTO_NO_EN_OC' && !esBonificacion) {
      const ok = await _confirmarModal(
        '⚠ Producto fuera de OC',
        'Este producto no está en la orden de compra.<br><br>¿Es un <strong>obsequio o bonificación</strong> del proveedor?',
        'Sí, registrar como bonificación', 'No, cancelar'
      );
      if (ok) await _tiendaOCRegistrarScan(productoId, cantidad, esEmpaque, true);
      return;
    }
    beepError();
    alerta(e.message || 'Error al escanear', 'error');
    return;
  }

  if (r.tipo === 'PRODUCTO_NO_EN_OC' && !esBonificacion) {
    const ok = await _confirmarModal(
      '⚠ Producto fuera de OC',
      'Este producto no está en la orden de compra.<br><br>¿Es un <strong>obsequio o bonificación</strong> del proveedor?',
      'Sí, registrar como bonificación', 'No, cancelar'
    );
    if (ok) await _tiendaOCRegistrarScan(productoId, cantidad, esEmpaque, true);
    return;
  }

  if (r.error) {
    const msg = typeof r.error === 'object' ? r.error.mensaje : r.error;
    alerta(msg, 'error');
    beepError();
    return;
  }

  beepOk();
  const idx = _TIENDA_OC_RECEPCION.items.findIndex(it => it.producto_id === productoId);
  if (idx >= 0) {
    _TIENDA_OC_RECEPCION.items[idx] = r.item;
  } else {
    _TIENDA_OC_RECEPCION.items.push(r.item);
  }

  const lista = document.getElementById('toc-items-list');
  if (lista) lista.innerHTML = _tiendaOCRenderItems(_TIENDA_OC_RECEPCION.items);

  if (r.alerta) {
    const tipo = r.alerta.includes('EXCESO') ? 'error' : r.alerta.includes('CROSS') ? 'advertencia' : 'info';
    alerta(r.alerta, tipo);
  }

  const itemsOC = _TIENDA_OC_RECEPCION.items.filter(it => it.tipo !== 'BONIFICACION');
  const todoCompleto = itemsOC.length > 0 && itemsOC.every(it => it.cantidad_recibida >= it.cantidad_ordenada);
  const btn = document.getElementById('btn-confirmar-toc');
  if (btn) {
    btn.disabled = false;
    btn.style.background = todoCompleto ? '#16a34a' : '#b45309';
    btn.style.cursor = 'pointer';
    btn.textContent = todoCompleto ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial';
    if (todoCompleto) alerta('Todo escaneado — confirma la recepción', 'exito');
  }
}

/** Open manual product search for store OC reception. */
async function tiendaOCBuscarManual() {
  const codigo = prompt('Ingresa el código WMS del producto:');
  if (!codigo) return;
  try {
    const prod = await get('/api/productos/?search=' + encodeURIComponent(codigo));
    if (!prod || !prod.productos || prod.productos.length === 0) { alerta('Producto no encontrado', 'error'); return; }
    const p = prod.productos[0];
    await _tiendaOCRegistrarScan(p.id, 1, false, false);
  } catch (e) { alerta('Error buscando producto', 'error'); }
}

/** Show bonus/gift registration modal for store OC reception. */
function tiendaOCModalObsequio() {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px;';
  overlay.innerHTML = `
    <div style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:16px;padding:28px;max-width:360px;width:100%;text-align:center;">
      <div style="font-size:40px;margin-bottom:12px;">🎁</div>
      <div style="font-size:18px;font-weight:800;color:#a78bfa;margin-bottom:10px;">¿Hay obsequios o bonificaciones?</div>
      <div style="font-size:14px;color:#9ca3af;margin-bottom:24px;">¿El proveedor envió productos adicionales que <strong style="color:#fff;">no están en la OC</strong>?</div>
      <div style="display:flex;gap:8px;margin-bottom:10px;">
        <input id="toc-bono-codigo" type="text" placeholder="Código del producto"
          style="flex:1;padding:10px;background:#0a0a0a;border:1px solid #4c1d95;border-radius:8px;color:#fff;font-size:14px;"
          onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ _tiendaOCEscanearBono(v); this.closest('div[style*=fixed]').remove(); } }">
        <button onclick="const v=document.getElementById('toc-bono-codigo').value.trim();if(v){_tiendaOCEscanearBono(v);this.closest('div[style*=fixed]').remove();}"
          style="padding:10px 14px;background:#4c1d95;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
      </div>
      <button onclick="this.closest('div[style*=fixed]').remove()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(overlay);
  setTimeout(() => overlay.querySelector('#toc-bono-codigo').focus(), 100);
}

/** @param {string} codigo - Barcode scanned for a bonus item in store OC reception. */
async function _tiendaOCEscanearBono(codigo) {
  vibrar(); flash();
  try {
    const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
    if (prod.error || !prod.producto_id) { alerta('Código no reconocido', 'error'); beepError(); return; }
    await _tiendaOCRegistrarScan(prod.producto_id, 1, false, true);
  } catch (e) { beepError(); alerta('Error al escanear bonificación', 'error'); }
}

/** Confirm the store OC reception. */
async function tiendaOCConfirmar() {
  if (!_TIENDA_OC_RECEPCION) return;

  const itemsOC = _TIENDA_OC_RECEPCION.items.filter(it => it.tipo !== 'BONIFICACION');
  const todoCompleto = itemsOC.every(it => it.cantidad_recibida >= it.cantidad_ordenada);

  if (!todoCompleto) {
    const ok = await _confirmarModal(
      '⚠ Recepción incompleta',
      'Hay ítems sin completar. ¿Confirmar como <strong>recepción parcial</strong>?',
      'Sí, confirmar parcial', 'Cancelar'
    );
    if (!ok) return;
  }

  const remision = await _pedirRemision();
  if (remision === null) return;

  const btn = document.getElementById('btn-confirmar-toc');
  if (btn) { btn.textContent = 'Confirmando...'; btn.disabled = true; }

  try {
    const r = await put('/api/tienda-oc/' + _TIENDA_OC_RECEPCION.id + '/confirmar', {
      num_remision_prov: remision
    });
    if (r.error) {
      alerta(r.error, 'error');
      if (btn) { btn.textContent = '✓ Confirmar recepción'; btn.disabled = false; }
      return;
    }
    alerta('Recepción confirmada — entrada enviada a Siesa', 'exito');
    _TIENDA_OC_RECEPCION = null;
    setTimeout(tiendaOCCargar, 1500);
  } catch (e) {
    alerta(e.message || 'Error confirmando', 'error');
    if (btn) { btn.textContent = '✓ Confirmar recepción'; btn.disabled = false; }
  }
}

/** Exit store OC scanning and return to the OC list. */
function tiendaOCVolverLista() {
  _TIENDA_OC_RECEPCION = null;
  tiendaOCCargar();
}


// ══════════════════════════════════════════════════════════════════════════════
// PANEL ADMIN — REPOSICIÓN
// ══════════════════════════════════════════════════════════════════════════════


// ── Navegación interna ────────────────────────────────────────────────────────


// Modal configurar límites




// ── SECCIÓN 3: Ubicaciones Huérfanas ─────────────────────────────────────────


// ── SECCIÓN 4: Jobs Siesa (DLQ) ──────────────────────────────────────────────

