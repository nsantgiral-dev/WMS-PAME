'use strict';

// ── Tema (dark/light) — aplica antes de cualquier render ─────────────────────
(function () {
  if (localStorage.getItem('wms_theme') === 'light') {
    document.body.classList.add('light');
  }
})();

/** @param {boolean} isLight - Whether light theme is active. */
function _actualizarLogo(isLight) {
  const src = isLight ? '/static/pwa/logo-h.png' : '/static/pwa/logo-white.png';
  document.querySelectorAll('.header-logo img, .login-logo img').forEach(img => { img.src = src; });
}

/** Toggle between dark and light theme, persisting choice to localStorage. */
function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('wms_theme', isLight ? 'light' : 'dark');
  const btn = document.getElementById('btn-theme');
  if (btn) btn.textContent = isLight ? '☀️' : '🌙';
  _actualizarLogo(isLight);
}
// ─────────────────────────────────────────────────────────────────────────────

const API = window.location.origin;
let TOKEN = localStorage.getItem('wms_token');
let OPERARIO = JSON.parse(localStorage.getItem('wms_operario') || 'null');
let TAREA_ACTUAL = null;
let COLA_OFFLINE = JSON.parse(localStorage.getItem('wms_cola_offline') || '[]');
let SCANNER_BUFFER = '';
let SCANNER_TIMER = null;
let CAMARA_ACTIVA = false;
let HTML5QR = null;          // legacy — ya no se usa, conservado por si acaso
let _QUAGGA_BOX  = null;    // boxDivId activo
let _QUAGGA_CB   = null;    // callback del scan activo
let _SCAN_LAST_TS = 0;      // debounce: ms del último scan registrado
let CHART = null;
let TAB = 'tab-dashboard';
let ALMACEN_ID = 1;
let TIMER_ADMIN = null;
let TIMER_OPERARIO = null;
let RECEPCION_ACTUAL = null;   // recepción en escaneo activo (pantalla recepcionista)
let DEVOLUCION_ACTUAL = null;  // tarea de devolución en flujo activo
let _pickingTotal = 0;         // acumulador local de picking — sincronizado con servidor post-scan
let REC_TAB_ACTIVO = 'ocs';   // tab activo en pantalla recepcionista
let TIMER_REC = null;          // polling recepcionista (30 seg)
let SIESA_PEDIDOS = [];        // pedidos cargados desde Siesa (admin tab-pedidos)
let PEDIDOS_TAB_ACTIVO = 0;    // sub-tab activo en tab-pedidos (0=Por despachar..3=Error Siesa)
let PEDIDOS_GRUPOS_HTML = ['', '', '', ''];  // cache del HTML de cada grupo, para cambiar de tab sin refetch
let PEDIDOS_GRUPOS_COUNT = [0, 0, 0, 0];     // cache del conteo de cada grupo
let SIESA_OCS = [];            // OCs cargadas desde Siesa (pantalla recepcionista)
let RUTA_ACTIVA_ID = null;     // ruta EN_CARGUE seleccionada en tab-muelle
let RUTAS_TIPO_SEL = 'Urbana'; // tipo seleccionado en form nueva ruta
let RUTAS_SUBTAB = 'rutas';    // sub-tab activo en tab-rutas
let MUELLE_TIMER = null;

/** @param {ServiceWorkerRegistration} reg - SW registration with a waiting worker. */
function _mostrarBannerSW(reg) {
  if (document.getElementById('sw-update-banner')) return;
  const div = document.createElement('div');
  div.id = 'sw-update-banner';
  div.innerHTML = `<div style="position:fixed;bottom:0;left:0;right:0;z-index:99999;background:#1e3a5f;color:#fff;padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:15px;font-weight:600;box-shadow:0 -2px 16px rgba(0,0,0,0.5);">
    <span>Nueva version disponible</span>
    <button id="sw-update-btn" style="background:#3b82f6;color:#fff;border:none;border-radius:8px;padding:9px 20px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap;">Actualizar ahora</button>
  </div>`;
  document.body.appendChild(div);
  document.getElementById('sw-update-btn').addEventListener('click', () => {
    div.remove();
    if (reg.waiting) reg.waiting.postMessage('SKIP_WAITING');
  });
}

document.addEventListener('DOMContentLoaded', () => {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/pwa/sw.js').then(reg => {
      if (reg.waiting) _mostrarBannerSW(reg);
      reg.addEventListener('updatefound', () => {
        const sw = reg.installing;
        sw.addEventListener('statechange', () => {
          if (sw.state === 'installed' && navigator.serviceWorker.controller) _mostrarBannerSW(reg);
        });
      });
    }).catch(() => {});
    let _swRefreshing = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (!_swRefreshing) { _swRefreshing = true; window.location.reload(); }
    });
  }
  // Sync theme icon + logo with stored preference
  const isLight = document.body.classList.contains('light');
  const btnTheme = document.getElementById('btn-theme');
  if (btnTheme) btnTheme.textContent = isLight ? '☀️' : '🌙';
  _actualizarLogo(isLight);
  monitorRed();
  scannerLaser();
  if (TOKEN && OPERARIO) {
    mostrarSegunRol(OPERARIO.rol);
  } else {
    pantalla('pantalla-login');
  }
});

/**
 * Route user to the correct screen and start timers based on their role.
 * @param {string} rol - User role (admin, operario, recepcionista, conductor, tienda, compras, etc.).
 */
function mostrarSegunRol(rol) {
  pararTimers();
  const esAdmin = ['admin','gerente','jefe_almacen','supervisor'].includes(rol);
  const esRecepcion = rol === 'recepcionista';
  const esConductor = rol === 'conductor';
  const esTienda = rol === 'tienda';
  const esCompras = rol === 'compras';
  const puedeEmpacar    = OPERARIO?.puede_empacar    || false;
  const puedePicar      = OPERARIO?.puede_picar      !== false; // default true
  const puedeAbastecer  = OPERARIO?.puede_abastecer  || false;

  // pantalla() SIEMPRE primero — garantiza que el panel correcto es visible
  // antes de cualquier actualización del DOM. Evita que actualizarUI
  // popule el header de admin mientras ese panel aún pueda estar visible.
  if (esCompras) {
    pantalla('pantalla-compras');
    if (OPERARIO) actualizarUI(OPERARIO);
    document.getElementById('compras-nombre').textContent = OPERARIO.nombre || '—';
    compIniciarPantalla();
  } else if (esTienda) {
    pantalla('pantalla-tienda');
    if (OPERARIO) actualizarUI(OPERARIO);
    document.getElementById('tienda-nombre').textContent =
      OPERARIO.nombre_punto_venta || OPERARIO.nombre || 'Punto de Venta';
    tiendaIniciar();
  } else if (esConductor) {
    pantalla('pantalla-conductor');
    if (OPERARIO) actualizarUI(OPERARIO);
    document.getElementById('cond-nombre').textContent = OPERARIO.nombre || '—';
    _condIniciarOffline();
    cargarRutasConductor();
    TIMER_OPERARIO = setInterval(cargarRutasConductor, 30000);
  } else if (esAdmin) {
    pantalla('pantalla-admin');
    if (OPERARIO) actualizarUI(OPERARIO);
    cargarAdmin();
    TIMER_ADMIN = setInterval(() => cargarAdmin(true), 30000);
  } else if (esRecepcion) {
    pantalla('pantalla-recepcion');
    if (OPERARIO) actualizarUI(OPERARIO);
    cargarRecepciones();
    cargarDevoluciones();
    TIMER_REC = setInterval(() => {
      if (!RECEPCION_ACTUAL && !DEVOLUCION_ACTUAL) {
        cargarRecepciones(true);
        cargarDevoluciones(true);
      }
    }, 30000);
  } else if (puedeAbastecer && !puedePicar && !puedeEmpacar) {
    // Abastecedor puro → directo al HUD de reposición
    abastIniciar();
  } else if (puedeEmpacar && !puedePicar && !puedeAbastecer) {
    // Empacador puro → directo al HUD de packing
    pantalla('pantalla-empacador');
    if (OPERARIO) actualizarUI(OPERARIO);
    empCargarTareas();
    TIMER_OPERARIO = setInterval(empCargarTareas, 20000);
  } else if (rol === 'picker_traslado') {
    // Picker de tienda: pantalla unificada, scoping automático a TRASLADO en backend
    pantalla('pantalla-operario');
    if (OPERARIO) actualizarUI(OPERARIO);
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  } else if (rol === 'packer_traslado') {
    // Packer de tienda: pantalla unificada, scoping automático a TRASLADO en backend
    pantalla('pantalla-empacador');
    if (OPERARIO) actualizarUI(OPERARIO);
    empCargarTareas();
    TIMER_OPERARIO = setInterval(empCargarTareas, 20000);
  } else if (puedeAbastecer && (puedePicar || puedeEmpacar)) {
    // Rol dual: picker/empacador + abastecedor → picker por defecto, botón para cambiar
    pantalla('pantalla-operario');
    if (OPERARIO) actualizarUI(OPERARIO);
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  } else {
    // Picker puro, empacador+picker, o operario sin flags especiales
    pantalla('pantalla-operario');
    if (OPERARIO) actualizarUI(OPERARIO);
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  }
}

/** Clear all polling intervals and reset active reception/return state. */
function pararTimers() {
  clearInterval(TIMER_ADMIN);
  clearInterval(TIMER_OPERARIO);
  clearInterval(TIMER_REC);
  if (typeof COMP_TIMER !== 'undefined') clearInterval(COMP_TIMER);
  RECEPCION_ACTUAL = null;
  DEVOLUCION_ACTUAL = null;
}

/** Set up online/offline listeners and update connection status indicators. */
function monitorRed() {
  const update = () => {
    const on = navigator.onLine;
    ['conexion-status','conexion-status-admin','conexion-status-tienda','conexion-status-compras'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.textContent = on ? '● Online' : '● Offline'; el.style.color = on ? '#22c55e' : '#ef4444'; }
    });
    if (on && COLA_OFFLINE.length) syncOffline();
  };
  window.addEventListener('online', update);
  window.addEventListener('offline', update);
  update();
}

/** Send queued offline actions to the server and clear the local queue on success. */
async function syncOffline() {
  try {
    const r = await post('/api/mobile/sync', { cola: COLA_OFFLINE });
    if (r.sincronizados > 0) {
      COLA_OFFLINE = [];
      localStorage.setItem('wms_cola_offline', '[]');
      alerta('✓ ' + r.sincronizados + ' tarea(s) sincronizadas', 'exito');
    }
  } catch (e) {}
}

/** @param {Object} datos - Action payload to enqueue for later sync. */
function guardarOffline(datos) {
  COLA_OFFLINE.push({ ...datos, ts: Date.now() });
  localStorage.setItem('wms_cola_offline', JSON.stringify(COLA_OFFLINE));
  alerta('Sin WiFi — guardado para sincronizar', 'advertencia');
}

/** Initialize laser/Bluetooth scanner input listener with keystroke buffering. */
function scannerLaser() {
  const inp = document.getElementById('scanner-input');
  if (!inp) return;

  // En móvil NO auto-forzamos foco — evita que el teclado se abra al tocar cualquier cosa.
  // El escáner Bluetooth en móvil escribe donde el usuario tocó deliberadamente.
  const esMobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);

  if (!esMobile) {
    const focus = () => {
      const a = document.activeElement;
      const esForm = a && ['INPUT','TEXTAREA','SELECT'].includes(a.tagName);
      const hayModal = document.getElementById('modal-problema');
      if (!CAMARA_ACTIVA && !esForm && !hayModal) inp.focus();
    };
    document.addEventListener('click', focus);
    setInterval(focus, 1000);
  }

  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const cod = SCANNER_BUFFER.trim();
      SCANNER_BUFFER = '';
      clearTimeout(SCANNER_TIMER);
      if (cod) procesarScan(cod);
    } else if (e.key && e.key.length === 1) {
      SCANNER_BUFFER += e.key;
      clearTimeout(SCANNER_TIMER);
      SCANNER_TIMER = setTimeout(() => { SCANNER_BUFFER = ''; }, 150);
    }
  });
}

/**
 * Validate fetch response: handle 401, parse JSON, throw on error.
 * @param {Response} r - Fetch response.
 * @returns {Promise<Object>} Parsed JSON body.
 */
async function _checkResp(r) {
  if (r.status === 401) { salir(true); throw new Error('401'); }
  if (!r.ok) {
    let msg = `Error del servidor (${r.status})`;
    let body = null;
    try {
      body = await r.json();
      if (typeof body.error === 'object' && body.error !== null) {
        msg = body.error.mensaje || body.error.message || JSON.stringify(body.error);
      } else {
        msg = body.error || body.mensaje || msg;
      }
    } catch (_) {}
    const err = new Error(msg);
    err.status = r.status;
    err.body = body;
    throw err;
  }
  return r.json();
}

/**
 * Authenticated GET request.
 * @param {string} url - API path (relative to origin).
 * @returns {Promise<Object>} Parsed JSON response.
 */
async function get(url) {
  const r = await fetch(API + url, { headers: { Authorization: 'Bearer ' + TOKEN } });
  return _checkResp(r);
}

/** Ejecuta fn() dando feedback visual al botón que disparó el evento. */
async function _refreshBtn(event, fn) {
  const btn = event.currentTarget || event.target;
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.style.opacity = '0.5';
  btn.innerHTML = '⟳';
  try {
    await fn();
  } catch (e) {
    if (e.status !== 401) alerta(e.message || 'Error al actualizar', 'error');
  } finally {
    btn.innerHTML = orig;
    btn.disabled = false;
    btn.style.opacity = '';
  }
}

/**
 * Authenticated POST request with JSON body.
 * @param {string} url - API path.
 * @param {Object} body - Request payload.
 * @returns {Promise<Object>} Parsed JSON response.
 */
async function post(url, body) {
  const r = await fetch(API + url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(body)
  });
  return _checkResp(r);
}

/**
 * Authenticated PUT request with JSON body.
 * @param {string} url - API path.
 * @param {Object} [body={}] - Request payload.
 * @returns {Promise<Object>} Parsed JSON response.
 */
async function put(url, body = {}) {
  const r = await fetch(API + url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(body)
  });
  return _checkResp(r);
}

/** Authenticate user with email/password, store token, and route to role screen. */
async function login() {
  const email = document.getElementById('login-email').value.trim();
  const pass = document.getElementById('login-password').value.trim();
  if (!email || !pass) { alerta('Ingresa usuario y contraseña', 'error'); return; }
  const btn = document.getElementById('btn-login');
  btn.textContent = 'Entrando...';
  btn.disabled = true;
  const opts = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ email, password: pass }) };
  try {
    // Intento 1 — si falla por red (ECONNREFUSED, timeout), reintentar una vez
    let r;
    for (let intento = 0; intento < 2; intento++) {
      try {
        r = await fetch(API + '/api/auth/login', opts);
        break; // fetch conectó — salir del loop aunque sea 5xx
      } catch (_) {
        if (intento === 0) {
          btn.textContent = 'Reintentando...';
          await new Promise(res => setTimeout(res, 2000));
        } else {
          throw new Error('sin_red'); // ambos intentos fallaron por red
        }
      }
    }

    // Verificar status ANTES de parsear JSON (un 502 devuelve HTML, no JSON)
    if (!r.ok) {
      if (r.status >= 500) {
        alerta('Servidor no disponible — intenta en unos segundos', 'advertencia');
      } else {
        let msg = 'Credenciales incorrectas';
        try { const e = await r.json(); msg = e.error || msg; } catch (_) {}
        alerta(msg, 'error');
      }
      return;
    }

    const d = await r.json();
    TOKEN = d.token;
    OPERARIO = d.usuario;
    localStorage.setItem('wms_token', TOKEN);
    localStorage.setItem('wms_operario', JSON.stringify(OPERARIO));
    actualizarUI(OPERARIO);
    mostrarSegunRol(OPERARIO.rol);
  } catch (e) {
    alerta('Sin conexión — verifica tu red', 'error');
  } finally {
    btn.textContent = 'Entrar';
    btn.disabled = false;
  }
}

/** @param {Object} op - Operario object with nombre and rol fields. */
function actualizarUI(op) {
  ['op-nombre','admin-nombre','rec-nombre','abast-nombre','emp-nombre'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = op.nombre; });
  ['op-rol','admin-rol'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = op.rol; });
}

/**
 * Log out: clear tokens, stop timers, return to login screen.
 * @param {boolean} [porExpiracion=false] - True if logout was caused by token expiration.
 */
function salir(porExpiracion = false) {
  pararTimers();
  TOKEN = null; OPERARIO = null; TAREA_ACTUAL = null;
  localStorage.removeItem('wms_token');
  localStorage.removeItem('wms_operario');
  pantalla('pantalla-login');
  if (porExpiracion) alerta('Sesión expirada — vuelve a ingresar', 'advertencia');
}

/**
 * Refresh the currently active admin tab content.
 * @param {boolean} [desdeTimer=false] - True when called from the 30s polling timer.
 */
async function cargarAdmin(desdeTimer = false) {
  if (TAB === 'tab-dashboard') await cargarDashboard();
  else if (TAB === 'tab-pedidos') await cargarPedidos();
  else if (TAB === 'tab-requisiciones') await cargarRequisiciones();
  else if (TAB === 'tab-bodega') await cargarTareasBodega();
  else if (TAB === 'tab-operarios') await cargarOperarios();
  else if (TAB === 'tab-usuarios') await cargarUsuarios();
  else if (TAB === 'tab-stock') await cargarStock();
  else if (TAB === 'tab-connekta') await cargarConnekta();
  else if (TAB === 'tab-muelle') await cargarMuelle();
  else if (TAB === 'tab-rutas') await cargarRutas();
  else if (TAB === 'tab-inventario') await cargarInventario();
  else if (TAB === 'tab-traslados') await cargarTrasladosAdmin();
  else if (TAB === 'tab-reposicion') await cargarReposicion();
  else if (TAB === 'tab-liquidacion') await cargarLiquidacion();
  // Layout es un módulo de configuración, no de datos en vivo — no se autorefresca
  // cada 30s (rompía el scroll y cualquier modal abierto mientras se revisaba).
  // Solo carga al entrar manualmente a la pestaña.
  else if (TAB === 'tab-layout') { if (!desdeTimer) await cargarLayout(); }
  else if (TAB === 'tab-compras') await cargarCompras();
}

/** @param {string} id - Tab element ID to activate (e.g. 'tab-dashboard'). */
function tab(id) {
  const TABS = ['tab-dashboard','tab-pedidos','tab-requisiciones','tab-traslados','tab-bodega','tab-operarios','tab-usuarios','tab-stock','tab-connekta','tab-muelle','tab-rutas','tab-inventario','tab-reposicion','tab-liquidacion','tab-layout','tab-compras','tab-etiquetas'];
  TABS.forEach(t => {
    const el = document.getElementById(t);
    if (el) el.style.display = t === id ? 'block' : 'none';
  });
  document.querySelectorAll('.nav-tab').forEach((t, i) => {
    t.classList.toggle('active', TABS[i] === id);
  });
  TAB = id;
  cargarAdmin();
}

/** Fetch and render the full admin dashboard (KPIs, chart, alerts, productivity). */
async function cargarDashboard() {
  try {
    const d = await get('/api/dashboard/resumen-completo?almacen_id=' + ALMACEN_ID);
    const k = d.kpis;
    const tras = d.traslados || {};
    const rutas = d.rutas || {};

    // ── KPIs fila 1 ────────────────────────────────────────────────
    set('kpi-pick-pend', k.picking.total_activo);
    set('kpi-pack-hoy', k.packing.facturas_generadas_hoy);

    set('kpi-tras-activos', tras.total_activos ?? '—');
    const trasSub = document.getElementById('kpi-tras-sub');
    if (trasSub) trasSub.textContent = `Pick:${tras.en_picking||0} Prep:${tras.preparado||0} Trans:${tras.en_transito||0}`;

    const rutasActivas = (rutas.en_cargue || 0) + (rutas.en_transito || 0);
    set('kpi-rutas-activas', rutasActivas);
    const rutasSub = document.getElementById('kpi-rutas-sub');
    if (rutasSub) rutasSub.textContent = `Cargue:${rutas.en_cargue||0} Tránsito:${rutas.en_transito||0}`;

    set('kpi-conteos-desc', k.conteo.en_descuadre || 0);

    const nAud = d.auditorias_urgentes || 0;
    set('kpi-auditorias', nAud);
    const cardAud = document.getElementById('kpi-card-auditorias');
    if (cardAud) cardAud.style.borderColor = nAud > 0 ? '#7f1d1d' : '';

    // ── Semáforo de módulos ────────────────────────────────────────
    _semaforo('sem-picking',
      k.picking.total_activo > 0 ? 'verde' : 'gris',
      k.picking.total_activo + ' tareas');
    _semaforo('sem-traslados',
      (tras.total_activos > 0) ? ((tras.en_transito || 0) > 0 ? 'amarillo' : 'verde') : 'gris',
      tras.total_activos + ' activos');
    _semaforo('sem-rutas',
      rutasActivas > 0 ? 'amarillo' : (rutas.entregadas_hoy > 0 ? 'verde' : 'gris'),
      rutasActivas + ' en marcha');
    _semaforo('sem-conteos',
      k.conteo.en_descuadre > 0 ? 'rojo' : (k.conteo.pendientes > 0 ? 'verde' : 'gris'),
      k.conteo.en_descuadre > 0 ? k.conteo.en_descuadre + ' descuadres' : k.conteo.pendientes + ' pendientes');
    _semaforo('sem-recepciones',
      k.recepcion.confirmadas_hoy > 0 ? 'verde' : 'gris',
      k.recepcion.confirmadas_hoy + ' hoy');
    const siesa = k.connekta || {};
    const cb = siesa.circuit_breaker || {};
    if (siesa.modo_simulacion) {
      _semaforo('sem-siesa', 'gris', 'Simulación');
    } else if (cb.state === 'OPEN' || cb.state === 'HALF_OPEN') {
      _semaforo('sem-siesa', 'rojo', 'Siesa caído');
    } else if (siesa.modo_ensayo) {
      _semaforo('sem-siesa', 'amarillo', 'Ensayo');
    } else {
      _semaforo('sem-siesa', 'verde', 'Conectado');
    }

    // ── Gráfica tendencia 7 días ───────────────────────────────────
    graficaTendencia(d.tendencia_7d || []);

    // ── Productividad ──────────────────────────────────────────────
    const prodEl = document.getElementById('dash-productividad');
    if (prodEl && d.productividad && d.productividad.operarios) {
      const ops = d.productividad.operarios.filter(o => o.total_tareas > 0);
      if (!ops.length) {
        prodEl.innerHTML = '<div style="color:#444;font-size:12px;">Sin actividad en los últimos 7 días</div>';
      } else {
        prodEl.innerHTML = ops.slice(0, 5).map(o => {
          const pct = Math.min(100, Math.round(o.total_tareas / Math.max(...ops.map(x => x.total_tareas)) * 100));
          return `<div style="margin-bottom:8px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
              <span style="color:#aaa;font-size:12px;">${o.nombre}</span>
              <span style="color:#666;font-size:11px;">Pick:${o.pickings_completados} · Pack:${o.packings_completados} · Cont:${o.conteos_completados}</span>
            </div>
            <div style="background:#1a1a1a;border-radius:4px;height:5px;">
              <div style="background:#3b82f6;width:${pct}%;height:5px;border-radius:4px;"></div>
            </div>
          </div>`;
        }).join('');
      }
    }

    // ── Movimientos recientes ──────────────────────────────────────
    movimientos(d.movimientos_recientes.movimientos);

    // ── Alertas (solo si hay datos) ────────────────────────────────
    const nBloq = d.tareas_bloqueadas || 0;
    const bloqEl = document.getElementById('dashboard-tareas-bloqueadas');
    if (bloqEl) {
      bloqEl.style.display = nBloq > 0 ? 'block' : 'none';
      const b = document.getElementById('bloq-count');
      if (b) b.textContent = nBloq;
    }
    const audEl = document.getElementById('dashboard-auditorias-urgentes');
    if (audEl) {
      audEl.style.display = nAud > 0 ? 'block' : 'none';
      const badge = document.getElementById('aud-urgentes-count');
      if (badge) badge.textContent = nAud;
      if (nAud > 0) cargarAuditoriasUrgentes();
    }
    const tr = d.traslados_en_riesgo || {};
    const nCriticos = tr.total_critico || 0;
    const nAlertas  = tr.total_alerta  || 0;
    const trEl = document.getElementById('dashboard-traslados-riesgo');
    if (trEl) {
      trEl.style.display = (nCriticos + nAlertas) > 0 ? 'block' : 'none';
      const elC = document.getElementById('traslados-criticos-count');
      const elA = document.getElementById('traslados-alerta-count');
      if (elC) elC.textContent = nCriticos;
      if (elA) elA.textContent = nAlertas;
      const lista = document.getElementById('traslados-riesgo-lista');
      if (lista) {
        const todos = [...(tr.criticos || []), ...(tr.alertas || [])];
        lista.innerHTML = todos.slice(0, 5).map(t =>
          `<div style="padding:6px 0;border-bottom:1px solid #1e3a5f;display:flex;justify-content:space-between;">
            <span style="color:#cbd5e1;">${t.codigo} → ${t.nombre_punto_venta || t.bodega_destino}</span>
            <span style="color:${t.horas_en_transito > 24 ? '#fb923c' : '#60a5fa'};font-weight:700;">${t.horas_en_transito}h</span>
          </div>`
        ).join('');
      }
    }
  } catch (e) { console.error('[Dashboard]', e); }
}

/**
 * Update a traffic-light status indicator.
 * @param {string} id - DOM element ID of the semaphore.
 * @param {string} color - Status color key (verde, amarillo, rojo, gris).
 * @param {string} texto - Label text to display.
 */
function _semaforo(id, color, texto) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `sem-item sem-${color}`;
  const lbl = el.querySelector('.sem-lbl');
  if (lbl) lbl.textContent = el.querySelector('.sem-lbl').textContent.split('\n')[0].split(':')[0] + ': ' + texto;
}

/** @param {Array<Object>} dias - 7-day trend data with fecha, picking, conteos, traslados, rutas. */
function graficaTendencia(dias) {
  const ctx = document.getElementById('chart-tendencia');
  if (!ctx || !window.Chart) return;
  if (CHART) CHART.destroy();
  const labels   = dias.map(d => d.fecha);
  const picking  = dias.map(d => d.picking);
  const conteos  = dias.map(d => d.conteos);
  const trasl    = dias.map(d => d.traslados);
  const rutasD   = dias.map(d => d.rutas);
  const lineOpts = (color) => ({ borderColor: color, backgroundColor: color + '22', tension: 0.35, pointRadius: 3, pointHoverRadius: 5, fill: true, borderWidth: 2 });
  CHART = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Picking',    data: picking, ...lineOpts('#3b82f6') },
        { label: 'Conteos',    data: conteos, ...lineOpts('#10b981') },
        { label: 'Traslados',  data: trasl,   ...lineOpts('#f59e0b') },
        { label: 'Rutas',      data: rutasD,  ...lineOpts('#8b5cf6') },
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#111' } },
        y: { ticks: { color: '#555', font: { size: 10 } }, grid: { color: '#111' }, beginAtZero: true }
      }
    }
  });
}

/** @param {Array<Object>} lista - Recent inventory movements to render. */
function movimientos(lista) {
  const el = document.getElementById('movimientos-recientes');
  if (!el) return;
  if (!lista || !lista.length) { el.innerHTML = '<div class="tabla-titulo">Últimos movimientos</div><div style="color:#555;font-size:13px;padding:8px 0;">Sin movimientos</div>'; return; }
  const TIPOS_ENTRADA = new Set(['ENTRADA', 'CARGA_INICIAL_SIESA', 'RECEPCION', 'AJUSTE_ENTRADA', 'DEVOLUCION']);
  el.innerHTML = '<div class="tabla-titulo">Últimos movimientos</div>' + lista.slice(0,8).map(m => {
    const esEntrada = TIPOS_ENTRADA.has(m.tipo);
    const c = esEntrada ? '#4ade80' : (m.cantidad > 0 ? '#f87171' : '#666');
    const s = esEntrada ? '+' : (m.cantidad > 0 ? '-' : '');
    const fechaStr = m.fecha && !m.fecha.endsWith('Z') ? m.fecha + 'Z' : m.fecha;
    const h = new Date(fechaStr).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Bogota' });
    const doc = m.numero_documento ? `<div style="font-size:10px;color:#444;">${m.numero_documento}</div>` : '';
    return `<div class="tabla-fila"><div><div class="tabla-nombre">${m.tipo}</div><div style="font-size:11px;color:#555;">${h}</div>${doc}</div><div style="color:${c};font-weight:700;">${s}${m.cantidad}</div></div>`;
  }).join('');
}

/** @param {number} id - Picking task ID to reopen back into the pool. */
async function reabrirTareaPicking(id) {
  if (!confirm('¿Reabrir esta tarea al pool de picking? El operario que llegue a esa ubicación la tomará de nuevo.')) return;
  try {
    const r = await fetch(API + `/api/picking/${id}/reabrir`, { method: 'PUT', headers: { Authorization: 'Bearer ' + TOKEN } });
    const d = await r.json();
    if (r.ok) { alerta('Tarea reabierta al pool ✓', 'exito'); await cargarTareasBodega(); }
    else alerta(d.error || 'Error al reabrir', 'error');
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** @param {number} id - Picking task ID to cancel (prompts for reason). */
async function cancelarTareaPicking(id) {
  const motivo = prompt('Motivo de cancelación (obligatorio):');
  if (!motivo || !motivo.trim()) return;
  if (!confirm(`¿Cancelar esta tarea de picking? El pedido del cliente quedará incompleto.`)) return;
  try {
    const r = await fetch(API + `/api/picking/${id}/cancelar`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ motivo })
    });
    const d = await r.json();
    if (r.ok) { alerta('Tarea cancelada', 'advertencia'); await cargarTareasBodega(); }
    else alerta(d.error || 'Error al cancelar', 'error');
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** @param {number} id - Task ID whose inline audit form to show. */
function auditoriaMostrarPanel(id) {
  document.getElementById(`auditoria-panel-${id}`).style.display = 'block';
}

/** @param {number} id - Task ID whose inline audit form to hide. */
function auditoriaCancelarPanel(id) {
  document.getElementById(`auditoria-panel-${id}`).style.display = 'none';
}

/** @param {number} id - Task ID to submit audit result for. */
async function auditoriaGuardar(id) {
  const resultado       = document.getElementById(`auditoria-resultado-${id}`)?.value;
  const cantidadHallada = parseInt(document.getElementById(`auditoria-cantidad-${id}`)?.value || '0', 10);
  const ubicacion       = document.getElementById(`auditoria-ubicacion-${id}`)?.value.trim();
  const observaciones   = document.getElementById(`auditoria-obs-${id}`)?.value.trim();

  if (!resultado) { alerta('Selecciona un resultado antes de guardar', 'error'); return; }

  try {
    const r = await fetch(API + `/api/picking/${id}/auditar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({
        resultado,
        cantidad_hallada: cantidadHallada,
        ubicacion_hallada: ubicacion || null,
        observaciones: observaciones || null,
      }),
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Auditoría registrada ✓', 'exito');
      await cargarTareasBodega();
    } else {
      alerta(d.error || 'Error al guardar auditoría', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Fetch Siesa orders and render grouped pedidos list with action buttons. */
async function cargarPedidos() {
  const el = document.getElementById('lista-pedidos');
  if (!el) return;
  // Disparar sync en background — no esperar, UI carga de DB local igual
  fetch('/api/siesa/sync-pedidos', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + TOKEN }
  }).catch(() => {});
  try {
    const [siesa] = await Promise.all([
      get('/api/siesa/pedidos').catch(() => ({ pedidos: [] }))
    ]);
    SIESA_PEDIDOS = siesa.pedidos || [];
    const _g = p => p.siesa_triggered ? 2 : (p.packing_estado === 'VERIFICADO' && !p.siesa_triggered) ? 3 : (p.picking_iniciado || p.packing_estado) ? 1 : 0;
    SIESA_PEDIDOS.sort((a, b) => _g(a) - _g(b));

    const tabsEl = document.getElementById('ped-tabs');

    if (siesa.simulado) {
      if (tabsEl) tabsEl.innerHTML = '';
      el.innerHTML = `<div style="background:#1a1a00;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#facc15;border:1px solid #333300;">⚡ Connekta en simulación — conecta credenciales para ver pedidos reales</div>`;
      return;
    }

    if (!SIESA_PEDIDOS.length) {
      if (tabsEl) tabsEl.innerHTML = '';
      el.innerHTML = `<div style="background:#0d1a0d;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#4ade80;border:1px solid #1a2a1a;">✓ Sin pedidos pendientes en Siesa</div>`;
      return;
    }

    {
      const grupos = [[], [], [], []];
      SIESA_PEDIDOS.forEach((p, i) => {
        const _gp = _g(p);
        const sinProd = p.items.filter(it => !it.producto_id).length;
        const totalUds = p.items.reduce((s, it) => s + (it.cantidad_pendiente || 0), 0);

        let accionBtn = '';
        if (p.siesa_triggered) {
          // Estado final: Siesa tiene la factura
          const btnRemision = p.packing_id
            ? `<button onclick="imprimirFacturaAdmin(${p.packing_id})"
                style="margin-top:6px;width:100%;background:#1a1a1a;color:#fff;border:none;padding:5px 8px;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer;">
                🖨 Factura
               </button>`
            : '';
          accionBtn = `<div style="flex-shrink:0;background:#0d1a0d;color:#4ade80;border:1px solid #166534;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;text-align:center;">✓ Despachado<br>en Siesa${btnRemision}</div>`;
        } else if (p.packing_estado === 'EN_PROCESO') {
          // Empacador verificando en mesa — admin puede entrar a ayudar/probar
          accionBtn = `<button onclick="empIniciarHUD(${p.packing_id})"
            style="flex-shrink:0;background:#1a0a2e;color:#c084fc;border:1px solid #4c1d95;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-align:center;">
            Packing<br>🔄 Abrir
          </button>`;
        } else if (p.packing_estado === 'VERIFICADO' && !p.siesa_triggered) {
          // RM creada en Siesa pero FE falló — carril de recuperación
          accionBtn = p.packing_id
            ? `<div style="flex-shrink:0;display:flex;flex-direction:column;gap:4px;align-items:stretch;">
                <div style="background:#2d0a0a;color:#fca5a5;border:1px solid #7f1d1d;padding:6px 10px;border-radius:6px;font-size:11px;font-weight:700;text-align:center;">⚠ Error Siesa</div>
                <button onclick="facturarRemisionExistente(${p.packing_id})"
                  style="background:#7c2d12;color:#fed7aa;border:1px solid #c2410c;padding:6px 10px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;text-align:center;">
                  🧾 Facturar Remisión
                </button>
              </div>`
            : `<div style="flex-shrink:0;background:#2d0a0a;color:#fca5a5;border:1px solid #7f1d1d;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;text-align:center;">⚠ Error<br>Siesa</div>`;
        } else if (p.picking_completado) {
          // Picking listo — admin puede abrir directamente el packing
          accionBtn = `<button onclick="empIniciarHUD(${p.packing_id})"
            style="flex-shrink:0;background:#1c1400;color:#fbbf24;border:1px solid #78350f;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;text-align:center;">
            Packing<br>pendiente ▶
          </button>`;
        } else if (p.picking_iniciado) {
          // Operario recogiendo
          accionBtn = `<div style="flex-shrink:0;background:#1a1a2a;color:#93c5fd;border:1px solid #1e3a5f;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;text-align:center;">
            En picking<br>${p.picking_progreso || ''}
          </div>`;
        } else {
          // Sin tareas — listo para despachar
          accionBtn = `<button onclick="iniciarDespachoDesdeSiesa(${i})"
            style="flex-shrink:0;background:#fff;color:#000;border:none;padding:10px 14px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">
            Despachar
          </button>`;
        }

        grupos[_gp].push(`
          <div class="tabla-card">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
              <div style="min-width:0;">
                <div style="font-size:15px;font-weight:700;">${p.numero_pedido}</div>
                <div style="font-size:12px;color:#666;margin-top:2px;">${p.cliente || 'Sin cliente'}</div>
                <div style="font-size:11px;color:#444;margin-top:2px;">${p.items.length} producto(s) · ${totalUds} uds</div>
                ${sinProd ? `<div style="font-size:11px;color:#d97706;margin-top:2px;">⚠ ${sinProd} sin registrar en WMS</div>` : ''}
              </div>
              ${accionBtn}
            </div>
          </div>`);
      });

      PEDIDOS_GRUPOS_HTML = grupos.map(arr => arr.join(''));
      PEDIDOS_GRUPOS_COUNT = grupos.map(arr => arr.length);
      renderPedidosTabsYLista();
    }
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando pedidos</div>';
  }
}

const PEDIDOS_TAB_LABELS = ['POR DESPACHAR', 'EN PROCESO', 'DESPACHADO EN SIESA', 'ERROR SIESA'];

/** Render pedidos sub-tabs and the HTML for the currently active group. */
function renderPedidosTabsYLista() {
  const tabsEl = document.getElementById('ped-tabs');
  const el = document.getElementById('lista-pedidos');
  if (!tabsEl || !el) return;

  tabsEl.innerHTML = PEDIDOS_TAB_LABELS.map((label, i) => {
    const count = PEDIDOS_GRUPOS_COUNT[i] || 0;
    const badge = i === 3
      ? (count ? `<span class="subtab-badge">${count}</span>` : '')
      : (count ? ` (${count})` : '');
    return `<div class="subtab${i === PEDIDOS_TAB_ACTIVO ? ' active' : ''}" onclick="pedidosCambiarTab(${i})">${label}${badge}</div>`;
  }).join('');

  el.innerHTML = PEDIDOS_GRUPOS_HTML[PEDIDOS_TAB_ACTIVO]
    || '<div style="color:#555;text-align:center;padding:40px;">Sin pedidos en esta pestaña ✓</div>';
}

/** @param {number} idx - Index of the pedidos sub-tab to activate (0-3). */
function pedidosCambiarTab(idx) {
  PEDIDOS_TAB_ACTIVO = idx;
  renderPedidosTabsYLista();
}

const BODEGA_TAB_LABELS = ['PEDIDOS', 'TRASLADOS'];
let BODEGA_TAB_ACTIVO = 0;
let BODEGA_GRUPOS_HTML = ['', ''];
let BODEGA_GRUPOS_COUNT = [0, 0];

/** Fetch active picking tasks and render them grouped by type (pedidos/traslados). */
async function cargarTareasBodega() {
  const el = document.getElementById('lista-tareas-bodega');
  if (!el) return;
  try {
    const d = await get('/api/picking/?activas=true&per_page=50');
    // Más recientes primero — solo afecta esta pantalla, el endpoint sigue
    // devolviendo oldest-first por defecto para el resto de consumidores.
    const tareas = (d.tareas || []).slice().sort((a, b) =>
      new Date(b.fecha_creacion) - new Date(a.fecha_creacion));
    const porTipo = [
      tareas.filter(t => t.tipo_documento !== 'TRASLADO'),
      tareas.filter(t => t.tipo_documento === 'TRASLADO'),
    ];
    BODEGA_GRUPOS_COUNT = porTipo.map(ts => ts.length);
    BODEGA_GRUPOS_HTML = porTipo.map(ts => _renderTareasBodegaHTML(ts));
    renderBodegaTabsYLista();
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;">Error cargando tareas de bodega</div>';
  }
}

/** Render bodega sub-tabs and the HTML for the currently active task group. */
function renderBodegaTabsYLista() {
  const tabsEl = document.getElementById('bodega-tabs');
  const el = document.getElementById('lista-tareas-bodega');
  if (!tabsEl || !el) return;

  tabsEl.innerHTML = BODEGA_TAB_LABELS.map((label, i) => {
    const count = BODEGA_GRUPOS_COUNT[i] || 0;
    return `<div class="subtab${i === BODEGA_TAB_ACTIVO ? ' active' : ''}" onclick="bodegaCambiarTab(${i})">${label}${count ? ` (${count})` : ''}</div>`;
  }).join('');

  el.innerHTML = BODEGA_GRUPOS_HTML[BODEGA_TAB_ACTIVO]
    || '<div style="color:#555;text-align:center;padding:40px;">Sin tareas activas en esta pestaña ✓</div>';
}

/** @param {number} idx - Index of the bodega sub-tab to activate (0=pedidos, 1=traslados). */
function bodegaCambiarTab(idx) {
  BODEGA_TAB_ACTIVO = idx;
  renderBodegaTabsYLista();
}

/**
 * Build HTML for a list of bodega tasks grouped by status.
 * @param {Array<Object>} tareas - Picking tasks to render.
 * @returns {string} HTML string.
 */
function _renderTareasBodegaHTML(tareas) {
  if (!tareas.length) return '';
  try {
    const MOTIVO_LABEL = {
      UBICACION_VACIA:    '📦 Ubicación vacía',
      FALTANTE:           '📉 Agotado',
      MERCANCIA_AVERIADA: '🚫 Mercancía averiada',
      PRODUCTO_INCORRECTO:'❌ Producto incorrecto'
    };
    const porEstado = { BLOQUEADO: [], EN_PROCESO: [], PENDIENTE: [] };
    tareas.forEach(t => {
      const g = porEstado[t.estado] ?? porEstado.PENDIENTE;
      g.push(t);
    });
    const grupos = [
      { label: '🔴 Bloqueadas', color: '#f87171', tareas: porEstado.BLOQUEADO },
      { label: '🔵 En proceso', color: '#93c5fd', tareas: porEstado.EN_PROCESO },
      { label: '⏳ En cola',    color: '#aaa',    tareas: porEstado.PENDIENTE  },
    ];
    let html = '';
    grupos.forEach(({ label, color, tareas: ts }) => {
      if (!ts.length) return;
      html += `<div style="font-size:11px;font-weight:700;color:${color};text-transform:uppercase;letter-spacing:.8px;padding:10px 0 5px;border-bottom:1px solid #222;margin-bottom:8px;">${label} · ${ts.length}</div>`;
      html += ts.map(t => `
        <div class="tabla-card" style="${t.estado==='BLOQUEADO'?'border-color:#7f1d1d;background:#110a0a;':''}">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
            <div style="flex:1;min-width:0;">
              <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
                <span style="font-size:14px;font-weight:600;">${t.producto_nombre || t.producto_codigo}</span>
                ${t.tipo_documento === 'TRASLADO' ? '<span style="font-size:10px;font-weight:700;padding:2px 7px;border-radius:10px;background:#1e3a5f;color:#60a5fa;letter-spacing:.5px;">🔄 TRANSFERENCIA</span>' : ''}
              </div>
              <div style="font-size:12px;color:#666;margin-top:2px;">${t.referencia_documento || t.codigo} · ${t.ubicacion_codigo || '—'}</div>
              <div style="font-size:11px;color:#444;margin-top:2px;">${
                t.operario_id
                  ? '👤 En proceso'
                  : t.estado === 'BLOQUEADO'
                    ? '🔴 Bloqueado — ' + (MOTIVO_LABEL[t.motivo_bloqueo] || t.motivo_bloqueo || 'novedad reportada')
                    : '⏳ En cola'
              }</div>
              ${t.estado === 'BLOQUEADO' && t.observaciones_bloqueo
                ? `<div style="font-size:11px;color:#ef4444;margin-top:3px;font-style:italic;">"${t.observaciones_bloqueo}"</div>`
                : ''}
            </div>
            <div style="text-align:right;flex-shrink:0;">
              <span class="badge ${t.estado==='EN_PROCESO'?'badge-blue':t.estado==='BLOQUEADO'?'badge-red':'badge-yellow'}">${t.estado}</span>
              <div style="font-size:20px;font-weight:800;margin-top:4px;">${t.cantidad_recogida||0}/${t.cantidad_solicitada}</div>
            </div>
          </div>
          ${t.estado === 'BLOQUEADO' ? `
          <div style="margin-top:10px;padding-top:10px;border-top:1px solid #2a1010;">
            <button onclick="auditoriaMostrarPanel(${t.id})"
              style="width:100%;padding:9px;background:#1a1a2a;color:#a78bfa;border:1px solid #2d1b69;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
              🔍 Auditoría
            </button>
            <div id="auditoria-panel-${t.id}" style="display:none;margin-top:10px;">
              <div style="font-size:11px;color:#888;margin-bottom:8px;">¿Qué encontraste físicamente?</div>
              <select id="auditoria-resultado-${t.id}"
                style="width:100%;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:13px;margin-bottom:8px;">
                <option value="">— Selecciona resultado —</option>
                <option value="ENCONTRADO_COMPLETO">✅ Encontrado completo (error del operario)</option>
                <option value="ENCONTRADO_PARCIAL">📉 Encontrado parcial</option>
                <option value="NO_ENCONTRADO">❌ No encontrado — faltante confirmado</option>
                <option value="AVERIA">🚫 Mercancía averiada</option>
                <option value="DISCREPANCIA_SIESA">⚠️ Discrepancia Siesa (existe en sistema, no en físico)</option>
              </select>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;">
                <div>
                  <div style="font-size:11px;color:#666;margin-bottom:4px;">Cant. hallada</div>
                  <input id="auditoria-cantidad-${t.id}" type="number" min="0" value="0"
                    style="width:100%;padding:9px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:13px;box-sizing:border-box;">
                </div>
                <div>
                  <div style="font-size:11px;color:#666;margin-bottom:4px;">Ubicación hallada</div>
                  <input id="auditoria-ubicacion-${t.id}" type="text" placeholder="Ej: A-01-02"
                    style="width:100%;padding:9px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:13px;box-sizing:border-box;">
                </div>
              </div>
              <textarea id="auditoria-obs-${t.id}" placeholder="Observaciones (opcional)..."
                style="width:100%;padding:9px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:12px;resize:vertical;min-height:56px;box-sizing:border-box;margin-bottom:8px;"></textarea>
              <div style="display:flex;gap:8px;">
                <button onclick="auditoriaCancelarPanel(${t.id})"
                  style="flex:1;padding:9px;background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:8px;font-size:12px;cursor:pointer;">
                  Cancelar
                </button>
                <button onclick="auditoriaGuardar(${t.id})"
                  style="flex:2;padding:9px;background:#a78bfa;color:#000;border:none;border-radius:8px;font-size:12px;font-weight:800;cursor:pointer;">
                  Guardar auditoría →
                </button>
              </div>
            </div>
          </div>` : ''}
        </div>`).join('');
    });
    return html;
  } catch (e) {
    return '<div style="color:#ef4444;text-align:center;">Error mostrando tareas de bodega</div>';
  }
}

/** Fetch and render operator list with 7-day productivity metrics. */
async function cargarOperarios() {
  const el = document.getElementById('lista-operarios');
  if (!el) return;
  try {
    const [prod, usuariosData] = await Promise.all([
      get('/api/dashboard/productividad?almacen_id=' + ALMACEN_ID + '&dias=7'),
      get('/api/auth/usuarios')
    ]);
    const metricas = {};
    (prod.operarios || []).forEach(op => { metricas[op.operario_id || op.id] = op; });

    // Todos los usuarios activos (operarios/jefe), con métricas si las tienen
    const todos = (usuariosData.usuarios || []).filter(u => u.activo);
    if (!todos.length) { el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin usuarios</div>'; return; }

    // Ordenar: más tareas primero
    todos.sort((a, b) => (metricas[b.id]?.total_tareas || 0) - (metricas[a.id]?.total_tareas || 0));

    el.innerHTML = todos.map((u, i) => {
      const op = metricas[u.id] || { total_tareas: 0, pickings_completados: 0, packings_completados: 0, conteos_completados: 0 };
      const badges = [u.puede_picar && '<span style="background:#1e40af;color:#fff;border-radius:4px;padding:1px 5px;font-size:10px;">Picker</span>',
                      u.puede_empacar && '<span style="background:#6b21a8;color:#fff;border-radius:4px;padding:1px 5px;font-size:10px;">Empacador</span>',
                      u.puede_abastecer && '<span style="background:#7c2d12;color:#fed7aa;border-radius:4px;padding:1px 5px;font-size:10px;">Abastecedor</span>'].filter(Boolean).join(' ');
      const color = op.total_tareas > 0 ? (i === 0 ? '#4ade80' : '#fff') : '#555';
      return `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:14px;font-weight:600;">${u.nombre}</div>
            <div style="font-size:11px;color:#555;margin-bottom:2px;">${u.rol} ${badges}</div>
            <div style="font-size:11px;color:#444;">Pick:${op.pickings_completados} Pack:${op.packings_completados} Conteos:${op.conteos_completados}</div>
            ${u.puede_picar && u.capacidad_diaria_conteo != null ? (() => {
              const cap = u.capacidad_diaria_conteo;
              const hoy = op.conteos_hoy || 0;
              const pct = cap > 0 ? Math.min(100, Math.round(hoy / cap * 100)) : 0;
              const col = pct >= 100 ? '#ef4444' : pct >= 70 ? '#f59e0b' : '#4ade80';
              return `<div style="margin-top:4px;">
                <div style="display:flex;justify-content:space-between;font-size:10px;color:#555;margin-bottom:2px;">
                  <span>Conteos hoy</span><span style="color:${col};font-weight:600;">${hoy}/${cap > 0 ? cap : '∞'}</span>
                </div>
                ${cap > 0 ? `<div style="background:#222;border-radius:3px;height:3px;overflow:hidden;"><div style="background:${col};width:${pct}%;height:100%;border-radius:3px;transition:width .3s;"></div></div>` : ''}
              </div>`;
            })() : ''}
          </div>
          <div style="text-align:right;">
            <div style="font-size:28px;font-weight:800;color:${color}">${op.total_tareas}</div>
            <div style="font-size:10px;color:#555;">tareas 7d</div>
          </div>
        </div>
      </div>`;
    }).join('');
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
}

/** Fetch stock alerts and load the product catalog. */
async function cargarStock() {
  const el = document.getElementById('lista-alertas');
  if (!el) return;
  try {
    const d = await get('/api/dashboard/alertas-stock?almacen_id=' + ALMACEN_ID);
    if (!d.alertas || !d.alertas.length) {
      el.innerHTML = '<div style="color:#4ade80;text-align:center;padding:40px;">✓ Sin alertas</div>';
    } else {
      el.innerHTML = d.alertas.map(a => `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div><div style="font-size:13px;font-weight:600;">${a.nombre}</div><div style="font-size:11px;color:#555;">${a.codigo} · Clase ${a.clasificacion_abc||'—'}</div></div>
          <div style="text-align:right;">
            <span class="badge ${a.urgencia==='CRITICO'?'badge-red':'badge-yellow'}">${a.urgencia}</span>
            <div style="font-size:20px;font-weight:800;color:${a.urgencia==='CRITICO'?'#f87171':'#facc15'}">${a.stock_actual}</div>
            <div style="font-size:10px;color:#555;">mín:${a.stock_minimo}</div>
          </div>
        </div>
      </div>`).join('');
    }
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
  await cargarCatalogo(1);
}

let _catalogoPag = 1;
/** @param {number} pag - Page number for the paginated product catalog. */
async function cargarCatalogo(pag) {
  _catalogoPag = pag || 1;
  const el = document.getElementById('lista-productos');
  const totalEl = document.getElementById('total-productos');
  const pagEl = document.getElementById('paginacion-productos');
  if (!el) return;
  const q = (document.getElementById('input-buscar-producto') || {}).value || '';
  try {
    const d = await get(`/api/productos/?page=${_catalogoPag}&per_page=20&q=${encodeURIComponent(q)}`);
    if (totalEl) totalEl.textContent = `${d.total} productos`;
    if (!d.productos || !d.productos.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:20px;font-size:13px;">Sin productos</div>';
      if (pagEl) pagEl.innerHTML = '';
      return;
    }
    el.innerHTML = d.productos.map(p => `
      <div class="tabla-fila">
        <div>
          <div style="font-size:13px;font-weight:600;">${p.nombre}</div>
          <div style="font-size:11px;color:#555;">${p.codigo}${p.codigo_siesa && p.codigo_siesa !== p.codigo ? ' · Siesa: ' + p.codigo_siesa : ''} · Clase ${p.clasificacion_abc || '—'}</div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:16px;font-weight:700;color:${p.stock_total > 0 ? '#4ade80' : '#555'}">${p.stock_total}</div>
          <div style="font-size:10px;color:#555;">${p.unidad_medida || 'UND'}</div>
        </div>
      </div>`).join('');
    // Paginación
    if (pagEl && d.paginas > 1) {
      let btns = '';
      if (_catalogoPag > 1) btns += `<button onclick="cargarCatalogo(${_catalogoPag - 1})" style="padding:6px 12px;background:#222;border:1px solid #333;color:#aaa;border-radius:6px;cursor:pointer;">◀</button>`;
      btns += `<span style="font-size:12px;color:#555;align-self:center;">${_catalogoPag} / ${d.paginas}</span>`;
      if (_catalogoPag < d.paginas) btns += `<button onclick="cargarCatalogo(${_catalogoPag + 1})" style="padding:6px 12px;background:#222;border:1px solid #333;color:#aaa;border-radius:6px;cursor:pointer;">▶</button>`;
      pagEl.innerHTML = btns;
    } else if (pagEl) pagEl.innerHTML = '';
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error cargando productos</div>'; }
}

let _buscarTimer;
/** Debounced search trigger for the product catalog filter input. */
function buscarProductos() {
  clearTimeout(_buscarTimer);
  _buscarTimer = setTimeout(() => cargarCatalogo(1), 400);
}

/** Fetch and render Connekta/Siesa connection status panel. */
async function cargarConnekta() {
  const el = document.getElementById('estado-connekta');
  if (!el) return;
  try {
    const d = await get('/api/packing/connekta/estado');
    let color, estado, detalle;
    if (d.modo_simulacion) {
      color = '#facc15'; estado = 'SIMULACIÓN';
      detalle = 'Sin credenciales — todo simulado localmente';
    } else if (d.modo_ensayo) {
      color = '#fb923c'; estado = 'MODO ENSAYO';
      detalle = 'Credenciales activas · GETs reales · POSTs bloqueados en servidor';
    } else {
      color = '#4ade80'; estado = 'PRODUCCIÓN';
      detalle = d.mensaje || 'Listo para operar';
    }
    el.innerHTML = `
      <div class="tabla-card">
        <div style="text-align:center;padding:20px 0;">
          <div style="font-size:13px;color:#666;margin-bottom:8px;">Estado Connekta / Siesa</div>
          <div style="font-size:28px;font-weight:800;color:${color};">${estado}</div>
          <div style="font-size:12px;color:#666;margin-top:8px;line-height:1.5;">${detalle}</div>
        </div>
        <div class="tabla-fila"><span class="tabla-nombre">Credenciales</span><span class="badge ${d.credenciales_configuradas?'badge-green':'badge-red'}">${d.credenciales_configuradas?'✓ Activas':'✗ Faltan'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">GETs (lectura)</span><span class="badge ${!d.modo_simulacion?'badge-green':'badge-yellow'}">${!d.modo_simulacion?'✓ Real':'Simulado'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">POSTs (escritura)</span><span class="badge ${(!d.modo_simulacion&&!d.modo_ensayo)?'badge-green':d.modo_ensayo?'badge-yellow':'badge-red'}">${(!d.modo_simulacion&&!d.modo_ensayo)?'✓ Activos':d.modo_ensayo?'Bloqueados (ensayo)':'Simulados'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">Bodega</span><span style="font-size:13px;color:#aaa;">${d.bodega||'—'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">CO</span><span style="font-size:13px;color:#aaa;">${d.centro_operacion||'—'}</span></div>
      </div>
      <button id="btn-setup-inicial" onclick="setupInicial()"
        style="width:100%;margin-top:12px;padding:14px;background:#1e3a5f;color:#93c5fd;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">
        ↻ Sincronizar catálogo + cargar stock inicial
      </button>
      <div id="setup-resultado" style="margin-top:8px;font-size:12px;color:#666;text-align:center;"></div>

      <div style="border-top:1px solid #222;margin-top:16px;padding-top:16px;">
        <div style="font-size:13px;font-weight:700;margin-bottom:8px;">Inventario bilateral</div>
        <button onclick="verReconciliacion()"
          style="width:100%;padding:12px;background:#1a1a2e;color:#93c5fd;border:1px solid #2a2a5a;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;">
          ⚖ Ver reconciliación WMS vs Siesa
        </button>
        <div id="inv-resultado" style="margin-top:8px;font-size:12px;color:#666;text-align:center;"></div>
      </div>
      <div id="panel-reconciliacion" style="margin-top:8px;"></div>

      <!-- Sync barcodes EAN -->
      <div style="border-top:1px solid #222;margin-top:16px;padding-top:16px;">
        <div style="font-size:13px;font-weight:700;margin-bottom:4px;">📦 Sync códigos de barras EAN</div>
        <div style="font-size:11px;color:#666;margin-bottom:10px;">Vuelca todos los barcodes de Siesa a la DB local. Corre automático a las 2am; este botón lo fuerza ahora.</div>
        <button onclick="syncBarcodes()"
          style="width:100%;padding:12px;background:#1e3a5f;color:#93c5fd;border:none;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;">
          ↻ Sincronizar barcodes ahora
        </button>
        <div id="sync-barras-resultado" style="margin-top:8px;font-size:12px;color:#666;min-height:16px;"></div>
      </div>

      <!-- Diagnóstico barcodes Siesa -->
      <div style="border-top:1px solid #222;margin-top:16px;padding-top:16px;">
        <div style="font-size:13px;font-weight:700;margin-bottom:8px;">🔍 Diagnóstico códigos de barras</div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <input id="debug-barras-input" type="text" placeholder="EAN a probar (ej: 49218787)"
            style="flex:1;padding:10px;background:#111;border:1px solid #333;border-radius:8px;color:#fff;font-size:13px;">
          <button onclick="testBarras()"
            style="padding:10px 14px;background:#1e3a5f;color:#93c5fd;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">
            Probar
          </button>
        </div>
        <div id="debug-barras-resultado" style="font-size:12px;color:#666;min-height:20px;white-space:pre-wrap;word-break:break-all;"></div>
      </div>

      ${d.modo_ensayo ? `
      <div style="background:#1a0f00;border:1px solid #7c2d12;border-radius:10px;padding:12px;margin-top:8px;font-size:12px;color:#fb923c;line-height:1.6;">
        <strong>MODO ENSAYO activo</strong><br>
        Los pedidos y OCs vienen de Siesa real. Al confirmar despacho o recepción, el payload se certifica en los logs del servidor pero <strong>no mueve inventario en Siesa</strong>.<br>
        Para activar producción: borrar la variable <code>MODO_ENSAYO</code> en Railway.
      </div>` : ''}`;
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
}

/** Trigger catalog sync + initial stock load from Siesa, polling for progress. */
async function setupInicial() {
  const btn = document.getElementById('btn-setup-inicial');
  const res = document.getElementById('setup-resultado');
  if (!btn) return;

  const FASES = { iniciando: '⏳ Iniciando...', catalogo: '⏳ Fase 1/2: sincronizando catálogo (~2 min)...', stock: '⏳ Fase 2/2: cargando stock (~60 seg)...' };

  btn.disabled = true;
  btn.textContent = '↻ Procesando...';
  res.style.color = '#93c5fd';
  res.textContent = 'Iniciando setup...';

  try {
    const d = await post('/api/siesa/setup-inicial', {});
    if (d.simulado) {
      res.style.color = '#fb923c';
      res.textContent = d.mensaje || 'Modo simulación';
      btn.disabled = false;
      btn.textContent = '↻ Sincronizar catálogo + cargar stock inicial';
      return;
    }
    if (d.en_curso && !d.iniciado) {
      res.style.color = '#fb923c';
      res.textContent = '⏳ Ya en proceso — monitoreando fase: ' + (d.fase || '...');
    }

    const iv = setInterval(async () => {
      try {
        const e = await get('/api/siesa/setup-inicial-estado');
        res.textContent = FASES[e.fase] || ('⏳ ' + (e.fase || 'en proceso'));
        if (!e.en_curso) {
          clearInterval(iv);
          btn.disabled = false;
          btn.textContent = '↻ Sincronizar catálogo + cargar stock inicial';
          if (e.ultimo_error) {
            res.style.color = '#ef4444';
            res.textContent = 'Error en fase ' + e.fase + ': ' + e.ultimo_error;
          } else {
            const cat = e.resultado_catalogo;
            const stk = e.resultado_stock;
            res.style.color = '#4ade80';
            const partesCat = cat ? `catálogo: ${cat.creados} creados · ${cat.actualizados} actualizados` : '';
            const partesStk = stk ? `stock: ${stk.cargados} nuevos · ${stk.actualizados} actualizados` : '';
            res.textContent = '✓ ' + [partesCat, partesStk].filter(Boolean).join(' — ');
          }
        }
      } catch (err) { clearInterval(iv); }
    }, 5000);
  } catch (e) {
    res.style.color = '#ef4444';
    res.textContent = 'Error: ' + (e.message || e);
    btn.disabled = false;
    btn.textContent = '↻ Sincronizar catálogo + cargar stock inicial';
  }
}

/** Trigger EAN barcode sync from Siesa and poll for completion. */
async function syncBarcodes() {
  const res = document.getElementById('sync-barras-resultado');
  if (!res) return;
  res.style.color = '#93c5fd';
  res.textContent = '⏳ Iniciando sync... (corre en background, puede tardar varios minutos)';
  try {
    await post('/api/siesa/sync-barcodes', {});
    res.textContent = '✓ Sync iniciado. Consulta el estado en unos minutos con "Probar" (sin código) para ver cuántos barcodes se cargaron.';
    res.style.color = '#4ade80';
    // Polling estado cada 10s hasta que termine
    const intervalo = setInterval(async () => {
      try {
        const e = await get('/api/siesa/sync-barcodes-estado');
        if (!e.en_curso && e.ultimo_resultado) {
          clearInterval(intervalo);
          const r = e.ultimo_resultado;
          res.textContent = `✓ Sync completado — campo: ${r.campo_detectado || '?'} · actualizados: ${r.actualizados} · sin producto local: ${r.sin_producto_local} · errores: ${r.errores}`;
        } else if (!e.en_curso && e.ultimo_error) {
          clearInterval(intervalo);
          res.style.color = '#ef4444';
          res.textContent = `✗ Error: ${e.ultimo_error}`;
        }
      } catch (_) {}
    }, 10000);
  } catch (e) {
    res.style.color = '#ef4444';
    res.textContent = 'Error al iniciar: ' + (e.message || e);
  }
}

/** Diagnose a barcode by querying Siesa API_v2_ItemsBarras directly. */
async function testBarras() {
  const inp = document.getElementById('debug-barras-input');
  const res = document.getElementById('debug-barras-resultado');
  if (!res) return;
  const codigo = (inp ? inp.value.trim() : '');
  res.style.color = '#93c5fd';
  res.textContent = '⏳ Consultando Siesa...';
  try {
    const url = '/api/siesa/debug-barras-raw' + (codigo ? '?codigo=' + encodeURIComponent(codigo) : '');
    const d = await get(url);
    const tabla = d?.detalle?.Table || d?.Table || [];
    if (!tabla.length) {
      res.style.color = '#ef4444';
      res.textContent = '✗ Sin resultados — Siesa no tiene barcode "' + (codigo || '(sin filtro)') + '" en API_v2_ItemsBarras.\nEl escaneo por EAN físico no funcionará hasta configurar barcodes en Siesa.';
    } else {
      res.style.color = '#4ade80';
      res.textContent = '✓ Siesa SÍ tiene barcodes.\nPrimeros resultados:\n' + JSON.stringify(tabla.slice(0, 3), null, 2);
    }
  } catch (e) {
    res.style.color = '#ef4444';
    res.textContent = 'Error: ' + (e.message || e) + '\n(puede que el conector API_v2_ItemsBarras no esté configurado en Connekta)';
  }
}

/** Start WMS vs Siesa stock reconciliation and poll for results. */
async function verReconciliacion() {
  const res = document.getElementById('inv-resultado');
  const panel = document.getElementById('panel-reconciliacion');
  if (!res || !panel) return;
  res.style.color = '#93c5fd';
  res.textContent = '⏳ Iniciando reconciliación... (~2 min)';
  panel.innerHTML = '';
  try {
    const d = await post('/api/siesa/reconciliacion', {});
    if (d.simulado) { res.style.color = '#fb923c'; res.textContent = 'Modo simulación'; return; }
    if (d.en_curso && !d.iniciado) { res.textContent = '⏳ Ya en proceso — monitoreando...'; }
    const iv = setInterval(async () => {
      try {
        const e = await get('/api/siesa/reconciliacion-estado');
        if (!e.en_curso) {
          clearInterval(iv);
          if (e.ultimo_error) {
            res.style.color = '#ef4444'; res.textContent = 'Error: ' + e.ultimo_error; return;
          }
          const r = e.ultimo_resultado;
          if (!r) { res.style.color = '#fb923c'; res.textContent = 'Sin resultado — intenta de nuevo'; return; }
          if (r.total_discrepancias === 0) {
            res.style.color = '#4ade80';
            res.textContent = `✓ Sin diferencias — WMS y Siesa coinciden (${r.total_productos_siesa} productos)`;
            return;
          }
          res.style.color = '#facc15';
          res.textContent = `⚠ ${r.total_discrepancias} diferencias de ${r.total_productos_siesa} productos`;
          panel.innerHTML = `
            <div style="font-size:12px;color:#555;margin-bottom:8px;">Top diferencias (WMS vs Siesa):</div>
            ${r.discrepancias.slice(0,20).map(x => `
              <div class="tabla-fila" style="font-size:12px;">
                <div>
                  <div style="font-weight:600;">${x.nombre}</div>
                  <div style="color:#555;">${x.codigo}</div>
                </div>
                <div style="text-align:right;">
                  <span style="color:${x.diferencia > 0 ? '#4ade80' : '#f87171'}">WMS: ${x.stock_wms}</span>
                  <span style="color:#555;margin:0 4px;">·</span>
                  <span style="color:#93c5fd;">Siesa: ${x.stock_siesa}</span>
                  <div style="color:${x.diferencia > 0 ? '#4ade80':'#f87171'};font-size:11px;">${x.diferencia > 0 ? '+' : ''}${x.diferencia}</div>
                </div>
              </div>`).join('')}`;
        } else { res.textContent = '⏳ Comparando WMS vs Siesa...'; }
      } catch(err) { clearInterval(iv); res.style.color = '#ef4444'; res.textContent = 'Error polling'; }
    }, 8000);
  } catch(e) { res.style.color = '#ef4444'; res.textContent = 'Error: ' + (e.message || e); }
}



/** Generate an LPN label for an unlabeled pack detected during picking. */

// ─── Quagga2 — debounce interno ───────────────────────────────────────────────
/** @param {Object} result - Quagga2 detection result with codeResult and confidence data. */
function _onQuaggaDetect(result) {
  const code = result && result.codeResult && result.codeResult.code;
  if (!code) return;

  // Filtro de confianza: descartar lecturas con demasiados errores
  const codes = result.codeResult.decodedCodes || [];
  const errores = codes.filter(c => c.error !== undefined).map(c => c.error);
  if (errores.length > 0) {
    const errorProm = errores.reduce((s, e) => s + e, 0) / errores.length;
    if (errorProm > 0.25) return; // lectura dudosa — ignorar
  }

  const now = Date.now();
  if (now - _SCAN_LAST_TS < 900) return;
  _SCAN_LAST_TS = now;
  vibrar();
  if (_QUAGGA_CB) _QUAGGA_CB(code);
}

/** Stop Quagga2 barcode scanner and remove detection listener. */
async function _quaggaStop() {
  if (!window.Quagga) return;
  try { Quagga.offDetected(_onQuaggaDetect); } catch (_) {}
  try { Quagga.stop(); } catch (_) {}
}

/**
 * Open camera barcode scanner using Quagga2.
 * @param {string} [lectorDivId='lector-qr'] - ID of the video container div.
 * @param {string} [boxDivId='camara-box'] - ID of the wrapper div to show/hide.
 * @param {Function|null} [onScan=null] - Callback on successful scan; defaults to procesarScan.
 */
async function abrirCamara(lectorDivId = 'lector-qr', boxDivId = 'camara-box', onScan = null) {
  // Cerrar cámara previa si hay alguna
  if (_QUAGGA_BOX) await cerrarCamara(_QUAGGA_BOX);

  const box    = document.getElementById(boxDivId);
  const target = document.getElementById(lectorDivId);
  if (!box || !target) return;

  box.style.display = 'block';
  CAMARA_ACTIVA = true;
  _QUAGGA_BOX = boxDivId;
  _QUAGGA_CB  = onScan || procesarScan;
  _SCAN_LAST_TS = 0;

  if (!window.Quagga) {
    await loadScript('https://cdn.jsdelivr.net/npm/@ericblade/quagga2@1.8.2/dist/quagga.min.js');
  }

  const esMobil = /Mobi|Android|iPhone/i.test(navigator.userAgent);

  await new Promise(resolve => {
    Quagga.init({
      inputStream: {
        type: 'LiveStream',
        target,
        constraints: {
          facingMode: 'environment',
          width:  { min: 320, ideal: esMobil ? 640 : 1280 },
          height: { min: 240, ideal: esMobil ? 480 : 720  }
        }
      },
      decoder: {
        // ean_8_reader y upc_e_reader (formatos de 8 dígitos) quitados: no se
        // usan en el catálogo (todos los EAN son de 13 dígitos) y generaban
        // falsos positivos al confundir una lectura parcial/borrosa de un
        // EAN-13 real con un código corto inexistente.
        readers: [
          'ean_reader',
          'code_128_reader', 'code_39_reader',
          'upc_reader'
        ],
        multiple: false
      },
      locate: !esMobil,   // en móvil apagar locate — muy pesado, usar visor fijo
      numOfWorkers: 0,
      frequency: esMobil ? 5 : 10,
      halfSample: esMobil  // en móvil: procesar a mitad de resolución → más rápido
    }, err => {
      if (err) {
        console.error('Quagga init:', err);
        alerta('No se pudo activar la cámara', 'error');
        cerrarCamara(boxDivId);
        resolve(); return;
      }
      Quagga.onDetected(_onQuaggaDetect);
      Quagga.start();

      // Estilar video insertado por Quagga + agregar visor rectangular
      const video = target.querySelector('video');
      if (video) {
        video.style.cssText = 'width:100%;height:260px;object-fit:cover;display:block;border-radius:10px;';
      }
      const cvs = target.querySelector('canvas');
      if (cvs) cvs.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;';
      target.style.position = 'relative';
      target.style.overflow = 'hidden';
      target.style.borderRadius = '10px';

      resolve();
    });
  });
}

/** @param {string} [boxDivId='camara-box'] - ID of the camera wrapper div to close. */
async function cerrarCamara(boxDivId = 'camara-box') {
  await _quaggaStop();
  CAMARA_ACTIVA = false;
  _QUAGGA_BOX = null;
  _QUAGGA_CB  = null;
  const box = document.getElementById(boxDivId);
  if (box) {
    box.style.display = 'none';
    // Limpiar todo lo que Quagga insertó (video, canvas, overlay)
    box.querySelectorAll('[id^="lector-qr"]').forEach(el => { el.innerHTML = ''; });
  }
}

/**
 * Dynamically load an external script.
 * @param {string} src - Script URL.
 * @returns {Promise<void>}
 */
function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
}





// ── Auditorías Urgentes (admin) ──────────────────────

/** Fetch and render urgent audit tasks on the admin dashboard. */
async function cargarAuditoriasUrgentes() {
  const el = document.getElementById('lista-auditorias-urgentes');
  if (!el) return;
  try {
    const d = await get('/api/conteo/auditorias-urgentes?almacen_id=' + ALMACEN_ID);
    const tareas = d.auditorias || [];
    if (!tareas.length) {
      el.innerHTML = '<div style="color:#4ade80;text-align:center;padding:20px;font-size:13px;">✓ Sin auditorías pendientes</div>';
      return;
    }
    const MOTIVOS = {'UBICACION_VACIA':'📦 Ubicación vacía','FALTANTE':'📉 Agotado','MERCANCIA_AVERIADA':'🚫 Mercancía averiada','PRODUCTO_INCORRECTO':'❌ Producto incorrecto'};
    el.innerHTML = tareas.map(t => `
        <div style="background:#111;border:1px solid #7f1d1d;border-radius:12px;padding:14px;margin-bottom:8px;">
          <div style="margin-bottom:8px;">
            <div style="font-size:13px;font-weight:700;color:#f87171;">${t.codigo}</div>
            <div style="font-size:11px;color:#555;margin-top:2px;">${t.producto_nombre || ''} · ${t.ubicacion_codigo || ''}</div>
            <div style="font-size:10px;color:#444;margin-top:1px;">Pedido ${t.referencia_documento || '—'} · pedía ${t.cantidad_solicitada} uds</div>
            ${t.motivo_bloqueo ? `<div style="font-size:10px;color:#b45309;margin-top:1px;">Motivo: ${MOTIVOS[t.motivo_bloqueo] || t.motivo_bloqueo}</div>` : ''}
          </div>
          <select id="da-resultado-${t.id}"
            style="width:100%;padding:9px;margin-bottom:6px;background:#0a0a0a;border:1px solid #333;color:#ccc;border-radius:8px;font-size:12px;">
            <option value="">¿Qué encontraste al verificar?</option>
            <option value="NO_ENCONTRADO">Confirmo: agotado — no hay nada</option>
            <option value="ENCONTRADO_COMPLETO">Encontré todo lo pedido (mal ubicado)</option>
            <option value="ENCONTRADO_PARCIAL">Encontré una parte</option>
            <option value="AVERIA">Está averiado</option>
            <option value="DISCREPANCIA_SIESA">Discrepancia con Siesa — ajusto manual</option>
          </select>
          <input id="da-cantidad-${t.id}" type="number" min="0" placeholder="Cantidad hallada"
            style="width:100%;padding:9px;margin-bottom:8px;background:#0a0a0a;border:1px solid #333;color:#ccc;border-radius:8px;font-size:12px;box-sizing:border-box;">
          <button onclick="dashAuditarTarea(${t.id})"
            style="width:100%;padding:11px;background:#7f1d1d;color:#fca5a5;border:1px solid #f87171;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">
            ✓ Confirmar auditoría
          </button>
        </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando auditorías</div>';
  }
}

/** @param {number} tareaId - Task ID to submit dashboard audit result for. */
async function dashAuditarTarea(tareaId) {
  const resultado = document.getElementById(`da-resultado-${tareaId}`)?.value;
  const cantidad_hallada = parseInt(document.getElementById(`da-cantidad-${tareaId}`)?.value || '0', 10);
  if (!resultado) { alerta('Selecciona qué encontraste', 'error'); return; }
  const avisoParcial = resultado === 'NO_ENCONTRADO' || resultado === 'AVERIA'
    ? '\n\nEsta línea se retira del pedido y el pedido sigue parcial con el resto.'
    : '';
  if (!confirm('¿Confirmar esta auditoría? Ajusta el inventario y el estado del pedido.' + avisoParcial)) return;
  try {
    const r = await fetch(API + `/api/picking/${tareaId}/auditar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ resultado, cantidad_hallada }),
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Auditoría registrada ✓', 'exito');
      cargarAuditoriasUrgentes();
      cargarDashboard();
    } else {
      alerta(d.error || 'Error al guardar auditoría', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Fetch OCs from Siesa and in-process receptions from DB, then render the list.
 * @param {boolean} [silencioso=false] - Skip loading spinner when true (polling mode).
 */

/** @param {string} id - Screen element ID to show (hides all others). */
function pantalla(id) {
  ['pantalla-login','pantalla-operario','pantalla-admin','pantalla-recepcion',
   'pantalla-empacador','pantalla-conductor','pantalla-tienda','pantalla-abastecedor',
   'pantalla-picker-traslado','pantalla-packer-traslado','pantalla-compras'].forEach(p => {
    const el = document.getElementById(p);
    if (el) el.style.display = p === id ? 'block' : 'none';
  });
}

/**
 * Set textContent of a DOM element by ID.
 * @param {string} id - Element ID.
 * @param {*} val - Value to display (falls back to '—').
 */
function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

/**
 * Show a toast notification at the top of the screen.
 * @param {string} msg - Message text.
 * @param {string} [tipo='info'] - Type: exito, error, advertencia, or info.
 */
function alerta(msg, tipo = 'info') {
  const c = { exito: '#16a34a', error: '#dc2626', advertencia: '#d97706', info: '#2563eb' }[tipo] || '#2563eb';
  const d = document.createElement('div');
  d.style.cssText = `position:fixed;top:20px;left:50%;transform:translateX(-50%);background:${c};color:#fff;padding:14px 22px;border-radius:12px;font-size:17px;font-weight:600;z-index:9999;max-width:90%;text-align:center;`;
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2500);
}

/** Brief white screen flash for scan feedback. */
function flash() {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;inset:0;background:rgba(255,255,255,0.25);z-index:9998;pointer-events:none;';
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 120);
}

/** Trigger a short haptic vibration (40ms). */
function vibrar() { if (navigator.vibrate) navigator.vibrate(40); }

// ── Feedback auditivo (Web Audio API — sin dependencias) ─────
let _audioCtx = null;
/** @returns {AudioContext} Lazily initialized Web Audio context. */
function _getAudioCtx() {
  if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return _audioCtx;
}
/**
 * Play a single tone via Web Audio API.
 * @param {number} frecuencia - Frequency in Hz.
 * @param {number} duracion - Duration in seconds.
 * @param {string} [tipo='sine'] - Oscillator type.
 * @param {number} [ganancia=0.35] - Volume gain.
 */
function _tono(frecuencia, duracion, tipo = 'sine', ganancia = 0.35) {
  try {
    const ctx = _getAudioCtx();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.type = tipo;
    osc.frequency.setValueAtTime(frecuencia, ctx.currentTime);
    gain.gain.setValueAtTime(ganancia, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + duracion);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + duracion);
  } catch (_) {}
}
/** High-pitched short beep for successful scan. */
function beepOk()    { _tono(880, 0.12); }
/** Low double beep for scan error. */
function beepError() { _tono(220, 0.18, 'square', 0.3); setTimeout(() => _tono(180, 0.18, 'square', 0.3), 200); }
/** Ascending fanfare for task completion. */
function beepDone()  { _tono(523, 0.1); setTimeout(() => _tono(659, 0.1), 120); setTimeout(() => _tono(784, 0.25), 240); }

// ─────────────────────────────────────────────────────────────
// ADMIN — Factura de despacho (pedidos ya confirmados en Siesa)
// ─────────────────────────────────────────────────────────────

/** @param {number} packingId - Packing task ID to fetch and print the invoice for. */
async function imprimirFacturaAdmin(packingId) {
  try {
    const res = await fetch(`/api/admin/factura/${packingId}`, {
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alerta(d.error || 'No se pudo obtener la factura', 'error');
      return;
    }
    const html = await res.text();
    const ventana = window.open('', '_blank');
    if (!ventana) {
      alerta('El navegador bloqueó la ventana emergente — permite popups para este sitio', 'advertencia');
      return;
    }
    ventana.document.write(html);
    ventana.document.close();
  } catch (e) {
    alerta('Error de conexión al obtener la remisión', 'error');
  }
}

// ADMIN — Facturar remisión existente (carril de recuperación 142943)
// ─────────────────────────────────────────────────────────────

/** @param {number} packingId - Packing ID to generate FE from an existing Siesa remision (recovery lane). */
async function facturarRemisionExistente(packingId) {
  if (!confirm('¿Facturar la remisión detectada en Siesa?\nEsto generará la Factura Electrónica (142943) desde la RM existente.')) return;
  try {
    const r = await post(`/api/despacho_parcial/${packingId}/facturar-remision`, {});
    if (r.idempotente) {
      alerta(`FE ya existía en Siesa — tarea marcada como despachada (${r.rm})`, 'exito');
    } else {
      alerta(`Factura generada desde ${r.rm} ✓`, 'exito');
    }
    setTimeout(cargarPedidos, 800);
  } catch (e) {
    alerta(e.message || 'Error al facturar la remisión', 'error');
  }
}

// ADMIN — Despacho desde Siesa
// ─────────────────────────────────────────────────────────────

/** @param {number} idx - Index into SIESA_PEDIDOS array for the order to dispatch. */
async function iniciarDespachoDesdeSiesa(idx) {
  const pedido = SIESA_PEDIDOS[idx];
  if (!pedido) return;
  const itemsValidos = pedido.items.filter(it => it.producto_id);
  if (!itemsValidos.length) {
    alerta('Ningún producto está registrado en el WMS', 'error');
    return;
  }
  const totalUds = itemsValidos.reduce((s, it) => s + (it.cantidad_pendiente || 0), 0);
  if (!confirm(`¿Iniciar despacho ${pedido.numero_pedido}?\n${itemsValidos.length} productos · ${totalUds} uds → ${pedido.cliente || 'cliente'}`)) return;

  try {
    const r = await post('/api/siesa/iniciar-despacho', {
      numero_pedido: pedido.numero_pedido,
      tipo_docto: pedido.tipo_docto,
      consec_docto: pedido.consec_docto,
      co: pedido.centro_op,
      almacen_id: ALMACEN_ID,
      items: itemsValidos
    });
    if (r.error) { alerta(r.error, 'error'); return; }
    if (r.errores && r.errores.length) {
      console.warn(`[DESPACHO] ${pedido.numero_pedido} — ${r.errores.length} línea(s) sin stock, excluidas de picking y packing:`, r.errores);
      alerta(`Despacho iniciado — ${r.errores.length} línea(s) sin stock quedaron fuera (pedido parcial). Detalle en consola.`, 'advertencia');
    } else {
      alerta(`Despacho iniciado — Packing ${r.packing_codigo}`, 'exito');
    }
    setTimeout(cargarPedidos, 800);
  } catch (e) { alerta('Error iniciando despacho', 'error'); }
}

// confirmarDespachoSiesa eliminado — el único gatillo hacia Siesa
// es el empacador físico al declarar bultos (POST /packing/<id>/cerrar)

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Lista de OCs y recepciones en proceso
// ─────────────────────────────────────────────────────────────




// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Pantalla de escaneo ciego
// ─────────────────────────────────────────────────────────────





// Muestra aviso y abre flujo de escaneo para obsequios/bonificaciones

// Panel de escaneo exclusivo para bonificaciones



// Helper: modal de confirmación reutilizable — devuelve Promise<boolean>




let _buscarModalTimer;

/**
 * Show modal to request the supplier's remision/invoice number (required by Siesa).
 * @returns {Promise<string|null>} Remision number, or null if cancelled.
 */

/** Exit active reception scan and return to the reception list. */

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Recepción de Traslados (NB1)
// ─────────────────────────────────────────────────────────────
let _REC_TRASLADO_ACTIVO    = null;  // ST abierto en conteo
let _REC_CONTEOS            = {};    // {producto_id: cantidad_contada}
let _REC_TRASLADOS_PENDIENTES = [];  // lista cargada desde API



// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Tabs (OCs / Traslados / Devoluciones)
// ─────────────────────────────────────────────────────────────


// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Flujo de ubicación de devolución
// ─────────────────────────────────────────────────────────────






// EMPACADOR — Estado global
// ─────────────────────────────────────────────────────────────

let EMP_TAREA = null;       // TareaPacking activa en el HUD
let EMP_ITEMS = [];         // ItemPacking[] con progreso actual
let EMP_ITEM_IDX = 0;       // índice del ítem que se está escaneando
let EMP_EMPAQUES = {};      // producto_id → { factor, unidad } — cargado al iniciar HUD

// ─────────────────────────────────────────────────────────────
// EMPACADOR — Lista de tareas
// ─────────────────────────────────────────────────────────────




// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: renderizar ítem actual
// ─────────────────────────────────────────────────────────────


// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: procesar escaneo láser
// ─────────────────────────────────────────────────────────────


// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: flash visual verde/rojo
// ─────────────────────────────────────────────────────────────


// ─────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────
// EMPACADOR — Modal ambigüedad de empaque en packing
// ─────────────────────────────────────────────────────────────



// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: confirmar packing → Siesa se dispara solo
// ─────────────────────────────────────────────────────────────


// ─────────────────────────────────────────────────────────────
// MODAL BULTOS — declaración de piezas físicas al cerrar packing
// ─────────────────────────────────────────────────────────────

let _BULTOS_LINEAS = [];



// ─────────────────────────────────────────────────────────────
// ETIQUETA LPN — imprime la etiqueta de una paca/caja física
// Se llama desde recepción (manual y DUN-14) y desde picking
// (lazy labeling de inventario heredado sin etiqueta).
// ─────────────────────────────────────────────────────────────



// ─────────────────────────────────────────────────────────────
// ADMIN — Gestión de usuarios (tab-usuarios)
// ─────────────────────────────────────────────────────────────

const _USR_NOMBRES_BOD = {
  'NC1':'Neiva Centro','NS1':'Neiva Sur Principal','NS2':'Neiva Sur Fundación',
  'FC1':'Florencia Centro','PC1':'Pitalito Centro','PT1':'Pitalito Terminal',
  'FF1':'Feria Florencia','FN1':'Feria Neiva','FP1':'Feria Pitalito',
};
let USUARIOS_GRUPOS = [];       // [{clave, titulo, count, html}] — solo bodegas con usuarios
let USUARIOS_TAB_ACTIVA = null; // clave del grupo/pestaña activa

/** Fetch all users and render the user management list grouped by role. */
async function cargarUsuarios() {
  const el = document.getElementById('lista-usuarios');
  if (!el) return;
  try {
    const d = await get('/api/auth/usuarios');
    const usuarios = d.usuarios || [];
    const tabsEl = document.getElementById('usuarios-tabs');
    if (!usuarios.length) {
      if (tabsEl) tabsEl.innerHTML = '';
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin usuarios</div>';
      return;
    }
    const grupos = {};
    usuarios.forEach(u => {
      const clave = u.bodega_siesa_id || '_CD';
      if (!grupos[clave]) grupos[clave] = [];
      grupos[clave].push(u);
    });
    const ordenGrupos = ['_CD', ...Object.keys(_USR_NOMBRES_BOD)];
    USUARIOS_GRUPOS = ordenGrupos.filter(clave => grupos[clave] && grupos[clave].length).map(clave => {
      const lista = grupos[clave];
      const titulo = clave === '_CD' ? '🏭 Centro de Distribución (NB1)' : `🏪 ${_USR_NOMBRES_BOD[clave] || clave} (${clave})`;
      const html = lista.map(u => {
        const rolColor = u.rol === 'admin' ? '#f87171' : '#aaa';
        return `
        <div class="tabla-card" style="margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
              <div style="font-size:15px;font-weight:700;">${u.nombre}</div>
              <div style="font-size:12px;color:#555;margin-top:2px;">${u.email}</div>
              <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;">
                <span style="font-size:11px;font-weight:600;color:${rolColor};background:#1a1a1a;padding:2px 8px;border-radius:8px;">${u.rol}</span>
                ${u.puede_picar ? `<span style="font-size:11px;font-weight:600;color:#60a5fa;background:#1e3a5f;padding:2px 8px;border-radius:8px;">Picker</span>` : ''}
                ${u.puede_empacar ? `<span style="font-size:11px;font-weight:600;color:#c084fc;background:#1a0a2e;padding:2px 8px;border-radius:8px;">Empacador</span>` : ''}
                ${u.puede_abastecer ? `<span style="font-size:11px;font-weight:600;color:#fed7aa;background:#7c2d12;padding:2px 8px;border-radius:8px;">Abastecedor</span>` : ''}
              </div>
            </div>
            <button onclick="editarUsuario(${u.id})"
              style="background:#222;border:1px solid #333;color:#fff;padding:6px 12px;border-radius:8px;font-size:12px;cursor:pointer;flex-shrink:0;">
              Editar
            </button>
          </div>
        </div>`;
      }).join('');
      return { clave, titulo, count: lista.length, html };
    });
    if (!USUARIOS_GRUPOS.some(g => g.clave === USUARIOS_TAB_ACTIVA)) {
      USUARIOS_TAB_ACTIVA = USUARIOS_GRUPOS.length ? USUARIOS_GRUPOS[0].clave : null;
    }
    renderUsuariosTabsYLista();
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando usuarios</div>';
  }
}

/** Render user management sub-tabs and the list for the active group. */
function renderUsuariosTabsYLista() {
  const tabsEl = document.getElementById('usuarios-tabs');
  const el = document.getElementById('lista-usuarios');
  if (!tabsEl || !el) return;

  tabsEl.innerHTML = USUARIOS_GRUPOS.map(g =>
    `<div class="subtab${g.clave === USUARIOS_TAB_ACTIVA ? ' active' : ''}" onclick="usuariosCambiarTab('${g.clave}')">${g.titulo} · ${g.count}</div>`
  ).join('');

  const activo = USUARIOS_GRUPOS.find(g => g.clave === USUARIOS_TAB_ACTIVA);
  el.innerHTML = activo ? activo.html : '<div style="color:#555;text-align:center;padding:40px;">Sin usuarios en esta bodega</div>';
}

/** @param {string} clave - User group key to switch to. */
function usuariosCambiarTab(clave) {
  USUARIOS_TAB_ACTIVA = clave;
  renderUsuariosTabsYLista();
}

/**
 * Build HTML for the user creation/edit form.
 * @param {Object} [u={}] - Existing user data for editing, or empty for new user.
 * @returns {string} HTML string.
 */
function _formUsuario(u = {}) {
  const _TIENDA_ROLES = ['tienda', 'picker_traslado', 'packer_traslado', 'recepcionista'];
  return `
    <div style="font-size:15px;font-weight:700;margin-bottom:16px;">${u.id ? 'Editar usuario' : 'Nuevo usuario'}</div>
    <div style="display:flex;flex-direction:column;gap:12px;">
      <input id="u-nombre" placeholder="Nombre completo" value="${u.nombre || ''}"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <input id="u-email" placeholder="email@empresa.com" value="${u.email || ''}" type="email" ${u.id ? 'readonly style="opacity:0.5;"' : ''}
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <input id="u-password" placeholder="${u.id ? 'Nueva contraseña (dejar vacío para no cambiar)' : 'Contraseña'}" type="password"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <select id="u-rol" onchange="(function(v){var tr=['tienda','picker_traslado','packer_traslado','recepcionista'];document.getElementById('u-tienda-fields').style.display=tr.includes(v)?'block':'none';document.getElementById('u-conductor-fields').style.display=v==='conductor'?'block':'none';var canPicar=document.getElementById('u-puede-picar').checked;document.getElementById('u-conteo-wrapper').style.display=(canPicar&&!tr.includes(v))?'block':'none';})(this.value)"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
        <option value="operario" ${(u.rol||'operario')==='operario'?'selected':''}>Operario (pedidos)</option>
        <option value="recepcionista" ${u.rol==='recepcionista'?'selected':''}>Recepcionista</option>
        <option value="conductor" ${u.rol==='conductor'?'selected':''}>Conductor</option>
        <option value="tienda" ${u.rol==='tienda'?'selected':''}>Tienda (punto de venta)</option>
        <option value="supervisor" ${u.rol==='supervisor'?'selected':''}>Supervisor</option>
        <option value="jefe_almacen" ${u.rol==='jefe_almacen'?'selected':''}>Jefe de almacén</option>
        <option value="admin" ${u.rol==='admin'?'selected':''}>Admin</option>
        <optgroup label="── Traslados ──">
          <option value="picker_traslado" ${u.rol==='picker_traslado'?'selected':''}>Picker traslado</option>
          <option value="packer_traslado" ${u.rol==='packer_traslado'?'selected':''}>Packer traslado</option>
        </optgroup>
        <optgroup label="── Compras ──">
          <option value="compras" ${u.rol==='compras'?'selected':''}>Compras</option>
        </optgroup>
      </select>
      <!-- Campos conductor (solo si rol=conductor) -->
      <div id="u-conductor-fields" style="display:${u.rol==='conductor'?'block':'none'};background:#1a1a1a;border:1px solid #2d1b69;border-radius:8px;padding:14px;">
        <div style="font-size:11px;font-weight:700;color:#a78bfa;text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px;">Datos del conductor</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <div>
            <div style="font-size:11px;color:#888;margin-bottom:5px;">Cédula *</div>
            <input id="u-conductor-cedula" type="text" placeholder="12345678" value="${u.conductor_cedula || ''}"
              style="width:100%;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;box-sizing:border-box;">
          </div>
          <div>
            <div style="font-size:11px;color:#888;margin-bottom:5px;">Teléfono</div>
            <input id="u-conductor-telefono" type="tel" placeholder="3001234567" value="${u.conductor_telefono || ''}"
              style="width:100%;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;box-sizing:border-box;">
          </div>
        </div>
      </div>
      <!-- Campos tienda / picker_traslado / packer_traslado -->
      <div id="u-tienda-fields" style="display:${_TIENDA_ROLES.includes(u.rol)?'block':'none'};">
        <select id="u-bodega-siesa"
          onchange="(function(sel){const nombres={'NB1':'Bodega Principal','NC1':'Neiva Centro','NS1':'Neiva Sur Principal','NS2':'Neiva Sur Fundación','FC1':'Florencia Centro','PC1':'Pitalito Centro','PT1':'Pitalito Terminal','FF1':'Feria Florencia','FN1':'Feria Neiva','FP1':'Feria Pitalito'};document.getElementById('u-nombre-pv').value=nombres[sel.value]||'';})(this)"
          style="width:100%;padding:12px;background:#1a1a1a;border:1px solid #f59e0b;border-radius:8px;color:#fff;font-size:14px;box-sizing:border-box;">
          <option value="">— Seleccionar bodega —</option>
          <option value="NB1" ${u.bodega_siesa_id==='NB1'?'selected':''}>NB1 — Bodega Principal</option>
          <option value="NC1" ${u.bodega_siesa_id==='NC1'?'selected':''}>NC1 — Neiva Centro</option>
          <option value="NS1" ${u.bodega_siesa_id==='NS1'?'selected':''}>NS1 — Neiva Sur Principal</option>
          <option value="NS2" ${u.bodega_siesa_id==='NS2'?'selected':''}>NS2 — Neiva Sur Fundación</option>
          <option value="FC1" ${u.bodega_siesa_id==='FC1'?'selected':''}>FC1 — Florencia Centro</option>
          <option value="PC1" ${u.bodega_siesa_id==='PC1'?'selected':''}>PC1 — Pitalito Centro</option>
          <option value="PT1" ${u.bodega_siesa_id==='PT1'?'selected':''}>PT1 — Pitalito Terminal</option>
          <option value="FF1" ${u.bodega_siesa_id==='FF1'?'selected':''}>FF1 — Feria Florencia</option>
          <option value="FN1" ${u.bodega_siesa_id==='FN1'?'selected':''}>FN1 — Feria Neiva</option>
          <option value="FP1" ${u.bodega_siesa_id==='FP1'?'selected':''}>FP1 — Feria Pitalito</option>
        </select>
        <input id="u-nombre-pv" type="hidden" value="${u.nombre_punto_venta || ''}">
      </div>
      <div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:14px;">
        <div style="font-size:12px;font-weight:600;color:#aaa;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.05em;">Capacidades operativas</div>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-picar" ${u.puede_picar!==false?'checked':''} style="width:20px;height:20px;accent-color:#60a5fa;" onchange="(function(cb){var tr=['tienda','picker_traslado','packer_traslado','recepcionista'];var v=document.getElementById('u-rol').value;document.getElementById('u-conteo-wrapper').style.display=(cb.checked&&!tr.includes(v))?'block':'none';})(this)">
          <div>
            <div style="font-size:14px;font-weight:600;color:#60a5fa;">Picker</div>
            <div style="font-size:11px;color:#555;">Puede recoger productos del almacén</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-empacar" ${u.puede_empacar?'checked':''} style="width:20px;height:20px;accent-color:#c084fc;">
          <div>
            <div style="font-size:14px;font-weight:600;color:#c084fc;">Empacador / Auditor</div>
            <div style="font-size:11px;color:#555;">Verifica y cierra cajas en mesa de empaque</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-abastecer" ${u.puede_abastecer?'checked':''} style="width:20px;height:20px;accent-color:#f97316;">
          <div>
            <div style="font-size:14px;font-weight:600;color:#f97316;">Abastecedor</div>
            <div style="font-size:11px;color:#555;">Puede mover pacas de zona RESERVA a zona PICKING</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;">
          <input type="checkbox" id="u-puede-camara" ${u.puede_usar_camara!==false?'checked':''} style="width:20px;height:20px;accent-color:#34d399;">
          <div>
            <div style="font-size:14px;font-weight:600;color:#34d399;">Usar cámara para escanear</div>
            <div style="font-size:11px;color:#555;">Muestra botón de cámara en picking y recepción</div>
          </div>
        </label>
        <div id="u-conteo-wrapper" style="margin-top:14px;padding-top:14px;border-top:1px solid #222;display:${(u.puede_picar!==false && !_TIENDA_ROLES.includes(u.rol))?'block':'none'};">
          <label style="font-size:12px;color:#888;display:block;margin-bottom:6px;">Conteos cíclicos por día (0 = sin límite)</label>
          <input id="u-capacidad-conteo" type="number" min="0" max="200" step="1"
            value="${u.capacidad_diaria_conteo ?? 15}"
            style="width:100%;padding:10px;background:#0d0d0d;border:1px solid #333;color:#fff;border-radius:8px;font-size:14px;box-sizing:border-box;">
          <div style="font-size:11px;color:#555;margin-top:4px;">Máximo de conteos intercalados que el sistema le asigna en un turno. Recomendado: 15–25.</div>
        </div>
      </div>
      <div style="display:flex;gap:8px;">
        <button onclick="_guardarUsuario(${u.id || 'null'})"
          style="flex:1;padding:14px;background:#fff;color:#000;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;">
          ${u.id ? 'Guardar cambios' : 'Crear usuario'}
        </button>
        <button onclick="ocultarFormUsuario()"
          style="padding:14px 18px;background:#222;color:#fff;border:1px solid #333;border-radius:10px;font-size:14px;cursor:pointer;">
          Cancelar
        </button>
      </div>
    </div>`;
}

/** Show the new user creation form. */
function mostrarFormNuevoUsuario() {
  const f = document.getElementById('form-nuevo-usuario');
  if (!f) return;
  f.innerHTML = _formUsuario();
  f.style.display = 'block';
  f.scrollIntoView({ behavior: 'smooth' });
}

/** @param {number} uid - User ID to load into the edit form. */
async function editarUsuario(uid) {
  try {
    const d = await get('/api/auth/usuarios');
    const u = (d.usuarios || []).find(x => x.id === uid);
    if (!u) return;
    const f = document.getElementById('form-nuevo-usuario');
    f.innerHTML = _formUsuario(u);
    f.style.display = 'block';
    f.scrollIntoView({ behavior: 'smooth' });
  } catch (e) { alerta('Error cargando usuario', 'error'); }
}

/** Hide the user form and reload the user list. */
function ocultarFormUsuario() {
  const f = document.getElementById('form-nuevo-usuario');
  if (f) { f.style.display = 'none'; f.innerHTML = ''; }
}

/** @param {number|null} uid - User ID to update, or null to create a new user. */
async function _guardarUsuario(uid) {
  const nombre = document.getElementById('u-nombre')?.value.trim();
  const email  = document.getElementById('u-email')?.value.trim();
  const pass   = document.getElementById('u-password')?.value;
  const rol    = document.getElementById('u-rol')?.value;
  const puedePicar      = document.getElementById('u-puede-picar')?.checked;
  const puedeEmpacar    = document.getElementById('u-puede-empacar')?.checked;
  const puedeAbastecer  = document.getElementById('u-puede-abastecer')?.checked || false;
  const puedeCamara     = document.getElementById('u-puede-camara')?.checked ?? true;
  const capacidadConteo = puedePicar ? parseInt(document.getElementById('u-capacidad-conteo')?.value || '15', 10) : null;
  const conductorCedula   = rol === 'conductor' ? (document.getElementById('u-conductor-cedula')?.value.trim() || '') : null;
  const conductorTelefono = rol === 'conductor' ? (document.getElementById('u-conductor-telefono')?.value.trim() || null) : null;

  if (!nombre) { alerta('El nombre es requerido', 'error'); return; }
  if (rol === 'conductor' && !conductorCedula) { alerta('La cédula es requerida para conductores', 'error'); return; }

  const bodegaSiesaId = document.getElementById('u-bodega-siesa')?.value.trim() || null;
  const nombrePv = document.getElementById('u-nombre-pv')?.value.trim() || null;

  const payload = {
    nombre, rol, puede_picar: puedePicar, puede_empacar: puedeEmpacar,
    puede_abastecer: puedeAbastecer, puede_usar_camara: puedeCamara,
    capacidad_diaria_conteo: capacidadConteo === null ? null : (isNaN(capacidadConteo) ? 15 : Math.max(0, capacidadConteo)),
    bodega_siesa_id: bodegaSiesaId, nombre_punto_venta: nombrePv,
    ...(rol === 'conductor' && { cedula: conductorCedula, telefono: conductorTelefono })
  };
  if (pass) payload.password = pass;

  try {
    let data;
    if (uid) {
      data = await put(`/api/auth/usuarios/${uid}`, payload);
      if (uid === OPERARIO?.id) {
        OPERARIO = { ...OPERARIO, ...data };
        localStorage.setItem('wms_operario', JSON.stringify(OPERARIO));
        actualizarUI(OPERARIO);
      }
    } else {
      if (!email) { alerta('El email es requerido', 'error'); return; }
      if (!pass)  { alerta('La contraseña es requerida', 'error'); return; }
      payload.email = email;
      data = await post('/api/auth/register', payload);
    }
    alerta(uid ? 'Usuario actualizado' : 'Usuario creado', 'exito');
    ocultarFormUsuario();
    cargarUsuarios();
  } catch (e) { if (e.status !== 401) alerta(e.message || 'Error de conexión', 'error'); }
}

// ─── MONITOR DE MUELLE ────────────────────────────────────────────────────────
const MUELLE_ORDEN_KEY = 'wms_muelle_orden'; // localStorage key


/** @param {Array<Object>} grupos - Muelle groups to render as sortable cards. */

// Referencia a los grupos actuales para poder reordenarlos sin ir al servidor
let _MUELLE_GRUPOS_ACTUALES = [];
// Manifiesto de la ruta activa (grupos ordenados) para reordenamiento en memoria
let _RUTA_MANIFIESTO_ACTUAL = [];

/**
 * Move a muelle group up or down in the loading order.
 * @param {number} idx - Current index of the group.
 * @param {number} dir - Direction (-1 = up, 1 = down).
 */

/** Fetch EN_CARGUE routes and populate the route selector dropdown in muelle. */

// ── UX móvil: campo de escaneo muelle ────────────────────────────
// En desktop el input está siempre visible y con foco (escáner USB/serial).
// En móvil mostramos un botón de "tocar para escanear" que activa el campo
// initMuelleUXMobile() lives in rutas.js (IIFE, auto-executes on load)

// ── Sin ruta seleccionada: vista informativa ──────────
/** Load muelle view when no route is selected (all pending groups). */

// ── Con ruta seleccionada: planificación + confirmación ─
/** @param {number} rutaId - Route ID to load muelle groups for. */

// ── Helpers de renderizado ────────────────────────────



// ── Reordenar paradas ─────────────────────────────────

// ── Asignar / desasignar ──────────────────────────────


// ── Confirmación de carga física (scan) ───────────────

// ══════════════════════════════════════════════════════
//  MILLA CERO — RUTAS DE DESPACHO
// ══════════════════════════════════════════════════════



// ── Rutas ────────────────────────────────────────────





// ── Entrega por bulto ────────────────────────────────────────

let _ENTREGA_RUTA_ID = null;
let _ENTREGA_BULTOS  = [];   // [{ id, codigo_barras, tipo, numero, total, cliente, numero_pedido, entregado, motivo_rechazo }]

const MOTIVOS_RECHAZO = ['Cliente rechazó', 'Dirección incorrecta', 'Mercancía averiada', 'No había nadie', 'Pedido duplicado'];





// Paradas dinámicas en el form
let _MAESTRAS_PARADAS = [];
let _MUNICIPIOS_CACHE = [];

let _municipiosPromise = null;





// ── Vehículos ────────────────────────────────────────





// ── Conductores ──────────────────────────────────────








// ─────────────────────────────────────────────────────────────
// CONDUCTOR — Pantalla de confirmación de entregas en campo
// ─────────────────────────────────────────────────────────────

let _COND_RUTAS = [];
let _COND_RUTA_ACTIVA = null;   // ruta seleccionada
let _COND_PARADAS = [];         // paradas de la ruta activa
let _COND_PARADA_FORM = null;   // parada en formulario de confirmación
let _COND_SYNCING = false;
let _COND_OFFLINE_INIT = false;

// ── IndexedDB helper (módulo conductor) ──────────────────────────
const _condDB = (() => {
  let _db = null;
  return {
    async get(key) {
      const db = await _open();
      return new Promise(res => {
        const r = db.transaction('cache').objectStore('cache').get(key);
        r.onsuccess = () => res(r.result ? r.result.v : null);
        r.onerror   = () => res(null);
      });
    },
    async set(key, val) {
      const db = await _open();
      return new Promise((res, rej) => {
        const tx = db.transaction('cache', 'readwrite');
        tx.objectStore('cache').put({ k: key, v: val, ts: Date.now() });
        tx.oncomplete = res; tx.onerror = () => rej(tx.error);
      });
    },
    async enqueue(item) {
      const db = await _open();
      return new Promise((res, rej) => {
        const tx = db.transaction('queue', 'readwrite');
        const r  = tx.objectStore('queue').add({ ...item, ts: Date.now() });
        r.onsuccess = () => res(r.result);
        r.onerror   = () => rej(r.error);
      });
    },
    async queue() {
      const db = await _open();
      return new Promise(res => {
        const r = db.transaction('queue').objectStore('queue').getAll();
        r.onsuccess = () => res(r.result || []);
        r.onerror   = () => res([]);
      });
    },
    async dequeue(id) {
      const db = await _open();
      return new Promise((res, rej) => {
        const tx = db.transaction('queue', 'readwrite');
        tx.objectStore('queue').delete(id);
        tx.oncomplete = res; tx.onerror = () => rej(tx.error);
      });
    }
  };
})();

// ── Lista de rutas del conductor ──────────────────────────────────


// ── Formulario de confirmación de parada ──────────────────────────








// ── Offline: init, barras de estado y motor de sync ──────────────




// ══════════════════════════════════════════════════════════════════
// PLANILLA DE CUADRE — Admin
// ══════════════════════════════════════════════════════════════════

let _PLAN_RUTA_ID = null;

/** @param {number} id - Route ID to force-close from admin (bypasses driver confirmation). */

/** @param {number} id - Route ID to show the settlement sheet (planilla) for. */

/** @param {number} id - Route ID to fetch and render the settlement sheet for. */

/** @param {number} id - Route ID to trigger financial liquidation for. */

/** @param {number} id - Route ID to trigger Siesa liquidation (NCE+RC+DC jobs). */
async function rutaLiquidarSiesa(id) {
  if (!confirm(`¿Re-enviar documentos de Ruta #${id} a Siesa?\nSolo se procesarán los que no se hayan enviado aún.`)) return;
  try {
    const r = await fetch(API + '/api/rutas/' + id + '/liquidar-siesa', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (r.ok) {
      const partes = [];
      if (d.nc_encolados) partes.push(d.nc_encolados + ' NC');
      if (d.rc_encolados) partes.push(d.rc_encolados + ' RC');
      if (d.dc_encolados) partes.push(d.dc_encolados + ' DC');
      if (d.ya_procesados) partes.push(d.ya_procesados + ' ya procesados');
      alerta(partes.length ? 'Siesa: ' + partes.join(', ') : 'Sin documentos nuevos por enviar', 'exito');
      if (d.errores && d.errores.length) alerta(d.errores.length + ' error(es) — revisar Jobs Siesa', 'error');
      await _cargarPlanilla(id);
    } else {
      alerta(d.error || 'Error al liquidar en Siesa', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}


// ── Config bodega por almacén ─────────────────────────────────────────────────


let _CONTEO_PAGE = 1;
let _CONTEO_VISTA = 'accion';  // 'accion' | 'progreso' | 'resueltos'





let _CONTEO_OPERARIOS = [];



/** Confirm and send the inventory adjustment to Siesa. */

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


// ── Admin Pedir — solicitar traslado hacia NB1 ──────────────────
let _AP_STOCK = [];
let _AP_STOCK_ESTADO = 'idle';
let _AP_CARRITO = [];
let _AP_ORIGEN = null;
let _AP_FILTRO = '';
let _AP_PAGINA = 1;
const _AP_POR_PAGINA = 30;
let _AP_INICIADO = false;







// ══════════════════════════════════════════════════════════════════
// TIENDA — Pantalla punto de venta
// ══════════════════════════════════════════════════════════════════

const _BODEGAS_ORIGEN = [
  { id: 'NB1', nombre: 'Bodega Principal' },
  { id: 'NC1', nombre: 'Neiva Centro' },
  { id: 'NS1', nombre: 'Neiva Sur Principal' },
  { id: 'NS2', nombre: 'Neiva Sur Fundación' },
  { id: 'FC1', nombre: 'Florencia Centro' },
  { id: 'PC1', nombre: 'Pitalito Centro' },
  { id: 'PT1', nombre: 'Pitalito Terminal' },
  { id: 'FF1', nombre: 'Feria Florencia' },
  { id: 'FN1', nombre: 'Feria Neiva' },
  { id: 'FP1', nombre: 'Feria Pitalito' },
];

let _TIENDA_SUBTAB = 'solicitudes';
let _TIENDA_STOCK = [];          // cache del stock de la bodega origen seleccionada
let _TIENDA_STOCK_ESTADO = 'cargando'; // 'cargando' | 'listo' | 'error'
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
    _TIENDA_STOCK_ESTADO = 'listo';
  } catch (e) {
    _TIENDA_STOCK_ESTADO = 'error';
  }
  if (_TIENDA_SUBTAB === 'nueva') tiendaRenderStock();
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
      body: JSON.stringify({})
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
// MÓDULO ABASTECEDOR — Reposición RESERVA → PICKING
// ══════════════════════════════════════════════════════════════════════════════

let ABAST_TAREA = null;       // tarea activa del abastecedor
let ABAST_TIMER = null;       // polling automático

/** Initialize the replenisher (abastecedor) screen and load current task. */
function abastIniciar() {
  pantalla('pantalla-abastecedor');
  if (OPERARIO) actualizarUI(OPERARIO);

  // Mostrar botón "Cambiar a Picker" si el usuario también puede picar
  const btnPicker = document.getElementById('abast-badge-picker');
  if (btnPicker) btnPicker.style.display = (OPERARIO?.puede_picar !== false) ? 'inline' : 'none';

  abastCargarTarea();
  ABAST_TIMER = setInterval(() => {
    if (!ABAST_TAREA) abastCargarTarea();
  }, 8000);
}

/** Fetch and display the next replenishment task for the operator. */
async function abastCargarTarea() {
  const cont = document.getElementById('abast-contenido');
  try {
    const d = await get('/api/reposicion/tarea-actual');

    if (d.sin_tareas) {
      ABAST_TAREA = null;
      if (cont) cont.innerHTML = `
        <div style="text-align:center;padding:60px 20px;">
          <div style="font-size:48px;margin-bottom:16px;opacity:0.3;">📦</div>
          <div style="font-size:18px;font-weight:700;color:#555;">Sin tareas de reposición</div>
          <div style="font-size:13px;color:#444;margin-top:8px;">El stock está en niveles correctos.</div>
          <div style="font-size:12px;color:#333;margin-top:24px;">Revisando automáticamente...</div>
        </div>`;
      // Si tiene capacidad de picker, ofrecer cambio de modo
      if (OPERARIO?.puede_picar !== false) {
        if (cont) cont.innerHTML += `
          <div style="padding:0 16px 24px;">
            <button onclick="abastCambiarAModo('picker')"
              style="width:100%;padding:14px;background:#1e3a5f;border:1px solid #1e40af;border-radius:12px;color:#60a5fa;font-size:14px;font-weight:700;cursor:pointer;">
              Cambiar a modo Picker
            </button>
          </div>`;
      }
      return;
    }

    ABAST_TAREA = d;
    abastMostrarHUD(d);

  } catch (e) {
    if (cont) cont.innerHTML = `<div style="text-align:center;padding:40px;color:#ef4444;">Error de conexión</div>`;
  }
}

/** @param {Object} tarea - Replenishment task to display in the operator HUD. */
function abastMostrarHUD(tarea) {
  const hud = document.getElementById('abast-hud');
  const cont = document.getElementById('abast-contenido');
  if (!hud) return;

  // Ocultar lista, mostrar HUD
  if (cont) cont.style.display = 'none';
  hud.style.display = 'flex';

  // Paso e instrucción
  document.getElementById('abast-hud-paso').textContent = 'Tarea de reposición';
  document.getElementById('abast-hud-instruccion').textContent =
    'Busca la paca en la zona de reserva';
  document.getElementById('abast-hud-sub').textContent =
    `${tarea.producto_nombre || tarea.producto_codigo || '—'} · ${tarea.cantidad_unidades || '—'} uds`;

  // Tarjetas de ubicación
  document.getElementById('abast-ubicacion-origen').textContent = tarea.ubicacion_reserva || '—';
  document.getElementById('abast-lpn-codigo').textContent = `LPN: ${tarea.lpn_codigo || '—'}`;
  document.getElementById('abast-ubicacion-destino').textContent = tarea.ubicacion_picking || '—';
  document.getElementById('abast-cantidad').textContent =
    tarea.cantidad_unidades ? `${tarea.cantidad_unidades} unidades` : '';

  // Limpiar input
  const inp = document.getElementById('abast-input-lpn');
  if (inp) { inp.value = ''; inp.focus(); }

  // Configurar cámara para abastecedor
  if (typeof Html5Qrcode !== 'undefined') {
    const camId = 'lector-qr-abast';
    const boxId = 'camara-box-abast';
    // Reusar la función genérica de cámara del sistema
    document.getElementById(camId + '-callback') && null;
  }
}

/** Close the replenisher HUD and load the next task. */
function abastCerrarHUD() {
  const hud = document.getElementById('abast-hud');
  const cont = document.getElementById('abast-contenido');
  if (hud) hud.style.display = 'none';
  if (cont) cont.style.display = 'block';
  ABAST_TAREA = null;
  abastCargarTarea();
}

/** Confirm the replenishment scan at the destination location. */
async function abastConfirmarScan() {
  if (!ABAST_TAREA) return;

  const inp = document.getElementById('abast-input-lpn');
  const lpn_escaneado = (inp?.value || '').trim().toUpperCase();

  if (!lpn_escaneado) {
    alerta('Escanea el código LPN primero', 'error');
    return;
  }

  const btn = document.getElementById('abast-btn-confirmar');
  if (btn) { btn.disabled = true; btn.textContent = 'Confirmando...'; }

  try {
    const r = await fetch(API + '/api/reposicion/confirmar', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        tarea_id: ABAST_TAREA.id,
        lpn_codigo_escaneado: lpn_escaneado,
      }),
    });
    const d = await r.json();

    if (r.ok && d.ok) {
      // Flash verde de éxito
      _abastFlash('#166534');
      alerta(`Reposición completada — ${d.unidades_movidas || ''} uds a ${ABAST_TAREA.ubicacion_picking}`, 'ok');
      ABAST_TAREA = null;
      setTimeout(abastCerrarHUD, 800);
    } else {
      _abastFlash('#7f1d1d');
      alerta(d.error || 'LPN incorrecto — verifica el código', 'error');
      if (inp) { inp.value = ''; inp.focus(); }
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Confirmar entrega'; }
  }
}

/** @param {string} color - CSS color for the replenisher flash feedback. */
function _abastFlash(color) {
  const f = document.getElementById('abast-flash');
  if (!f) return;
  f.style.background = color + '66';
  setTimeout(() => { f.style.background = 'transparent'; }, 300);
}

/** @param {string} modo - Switch replenisher to 'picking' or 'abastecedor' mode. */
function abastCambiarAModo(modo) {
  clearInterval(ABAST_TIMER);
  ABAST_TAREA = null;
  const hud = document.getElementById('abast-hud');
  if (hud) hud.style.display = 'none';

  if (modo === 'picker') {
    pantalla('pantalla-operario');
    if (OPERARIO) actualizarUI(OPERARIO);
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  }
}

// Hook en pantalla-operario: si el usuario puede abastecer y no tiene tareas
// de picking, mostrar el botón flotante de cambio de modo.
/** Show or hide the mode switch button based on operator capabilities. */
function abastVerificarBotonModo() {
  const contenido = document.getElementById('contenido-tarea');
  if (!contenido || !OPERARIO?.puede_abastecer) return;

  // Si ya hay tarea activa de picking, no mostrar el botón
  if (TAREA_ACTUAL) {
    const btn = document.getElementById('btn-modo-abastecedor');
    if (btn) btn.remove();
    return;
  }

  // Si no hay tarea, mostrar el botón de cambio de modo
  if (!document.getElementById('btn-modo-abastecedor')) {
    const btn = document.createElement('button');
    btn.id = 'btn-modo-abastecedor';
    btn.textContent = 'Cambiar a modo Abastecedor';
    btn.style.cssText = `
      position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
      padding:12px 24px;background:#1c2a1c;border:1px solid #166534;
      border-radius:24px;color:#4ade80;font-size:13px;font-weight:700;
      cursor:pointer;z-index:50;box-shadow:0 4px 20px #00000066;
      white-space:nowrap;
    `;
    btn.onclick = () => {
      clearInterval(TIMER_OPERARIO);
      TAREA_ACTUAL = null;
      abastIniciar();
    };
    document.body.appendChild(btn);
  }
}


// ══════════════════════════════════════════════════════════════════════════════
// PANEL ADMIN — REPOSICIÓN
// ══════════════════════════════════════════════════════════════════════════════

let _repSubActual = 'ubicaciones';
let _repModalUbId = null;

// ── Navegación interna ────────────────────────────────────────────────────────


// Modal configurar límites




// ── SECCIÓN 3: Ubicaciones Huérfanas ─────────────────────────────────────────


// ── SECCIÓN 4: Jobs Siesa (DLQ) ──────────────────────────────────────────────







// ─────────────────────────────────────────────────────────────────────────────
// MÓDULO LAYOUT — ubicaciones gestionadas 100% en el WMS, independiente de
// Reposición. Este módulo es el cimiento (crear/clasificar/poblar); Reposición,
// Picking, Recepción, Averías y Compras son consumidores de estos datos.
// ─────────────────────────────────────────────────────────────────────────────

let _layoutSubActual = 'ubicaciones';
let _layoutZonaActual = 'PICKING';
let _layoutUbicacionesCache = [];
let _layoutUltimoCuerpo = null;   // { pasillo, fila, cuerpo, entrepanos, huecosPorNivel } — para "repetir"
let _layoutUbAsignarId = null;
let _layoutProductoId = null;

const _ZONA_COLOR = { PICKING: '#60a5fa', RESERVA: '#22c55e', AVERIAS: '#ef4444', GENERAL: '#555' };
const _ZONAS_TABS = ['PICKING', 'RESERVA', 'AVERIAS', 'GENERAL'];


// Sección por Entrepaño (título, subtítulo, badge de zona, botones de ancho
// completo) — dentro de la tarjeta blanca única del Cuerpo, no una tarjeta
// aparte cada una. El detalle hueco por hueco (código, SKU, iconos de editar/
// eliminar/reclasificar) vive en el modal "Ver", no acá.
/**
 * @param {Array<Object>} huecos - Shelf slot objects.
 * @param {boolean} esPrimero - Whether this is the first section (no top separator).
 * @returns {string} HTML for a shelf section.
 */
function _layoutRenderEntrepanoSeccion(huecos, esPrimero) {
  const zona = huecos[0].tipo_zona;
  const color = _ZONA_COLOR[zona] || '#888';
  const nivel = huecos[0].nivel;
  const idsCsv = huecos.map(u => u.id).join(',');
  const sinAsignar = huecos.filter(u => !u.producto_asignado_codigo).length;
  const subtitulo = sinAsignar
    ? `${huecos.length} hueco(s) · ${sinAsignar} sin SKU asignado`
    : `${huecos.length} hueco(s) · todos asignados`;

  return `
    <div style="${esPrimero ? '' : 'border-top:1px solid var(--brd);margin-top:14px;padding-top:14px;'}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:15px;font-weight:800;color:var(--tx);">Entrepaño ${nivel}</div>
          <div style="font-size:11px;color:${sinAsignar ? '#555' : '#60a5fa'};margin-top:3px;font-weight:600;">${subtitulo}</div>
        </div>
        <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${zona}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button onclick="layoutAbrirModalAsignarEntrepano('${idsCsv}', '${zona}')"
          style="flex:1;min-width:120px;padding:10px;background:var(--pm);border:none;border-radius:6px;color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
          Asignar SKU
        </button>
        <button onclick="layoutAbrirModalVerEntrepano('${idsCsv}')"
          style="flex:1;min-width:90px;padding:10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:12px;cursor:pointer;">
          Ver
        </button>
      </div>
    </div>`;
}

// Imprime una etiqueta física (80mm, mismo formato que LPN/canasto) por cada
// Hueco del Cuerpo — con su código completo (incluye el H de hueco, a
// diferencia del título de la tarjeta que solo muestra Pasillo+Fila+Cuerpo).
/** @param {string} idsCsv - Comma-separated location IDs to print labels for. */
function layoutImprimirEtiquetasCuerpo(idsCsv) {
  const ids = idsCsv.split(',').map(Number);
  const huecos = ids
    .map(id => _layoutUbicacionesCache.find(u => u.id === id))
    .filter(Boolean)
    .sort((a, b) => a.codigo.localeCompare(b.codigo));
  if (!huecos.length) return;

  const area = document.getElementById('print-area');
  if (!area) return;

  area.innerHTML = huecos.map(u => `
    <div class="etiqueta-ubicacion">
      <div class="eu-titulo">UBICACIÓN — BODEGA</div>
      <svg id="ub-bc-${u.id}"></svg>
      <div class="eu-codigo">${u.codigo}</div>
      <div class="eu-zona">${u.tipo_zona}</div>
    </div>`).join('');

  huecos.forEach(u => {
    try {
      JsBarcode(`#ub-bc-${u.id}`, u.codigo, { format: 'CODE128', displayValue: false, height: 55, margin: 0 });
    } catch (e) { /* JsBarcode no disponible aún */ }
  });

  setTimeout(() => {
    window.print();
    setTimeout(() => { area.innerHTML = ''; }, 1000);
  }, 300);
}

/** Fetch all warehouse locations from the API. */

// Pinta solo la zona activa (pestaña). Dentro de ella, cada grupo — fila legada,
// Cuerpo, o ubicación suelta (AVERIAS/GENERAL) — se ordena por la fecha de
// creación de su miembro más nuevo: lo que se acaba de crear queda primero.
/** Render locations grouped by zone, aisle, and row in the layout view. */

// ── Modal: Crear Cuerpo completo (Mecanismo A), wizard de 2 pasos ────────────
// Dirección de 5 ejes: Pasillo -> Fila (1/2) -> Cuerpo -> Entrepaños -> Huecos.
// Un Cuerpo es 100% de una sola zona — PICKING y RESERVA se arman como Cuerpos
// separados (botones "+ Crear picking" / "+ Crear reserva"), no mezclados por
// Nivel dentro del mismo Cuerpo: así cada Cuerpo se revisa completo en una sola
// pestaña de zona. Paso 1 define el cuerpo (pasillo/fila/número/cantidad de
// entrepaños); paso 2 pide, entrepaño por entrepaño, cuántos huecos tiene —
// variable, no un único número para todo el cuerpo, porque la profundidad
// física no es uniforme. El SKU de cada hueco se asigna después, aparte
// (Asignar SKU, Mecanismo B).

let _layoutCuerpoZona = 'PICKING';
let _layoutCuerpoHuecosPrevios = null; // huecosPorNivel del último cuerpo creado, para "repetir"




// ── Modal: Asignar SKU (Mecanismo B) ─────────────────────────────────────────



/** Confirm the product-to-location assignment. */

// ── Modal: Asignar SKU — Entrepaño completo (todos sus huecos de una vez) ───
// Se abre desde la tarjeta de Entrepaño en vez de hueco por hueco. Cada fila
// se resuelve al presionar Enter (como un lector de código de barras) y salta
// sola a la siguiente — fila que quede vacía simplemente se ignora al confirmar,
// no hay que llenar los 10 huecos de un tirón.

let _layoutAsignarEntHuecos = [];   // [{id, codigo, tipo_zona, producto_asignado_codigo, ...}]
let _layoutAsignarEntProductos = {}; // huecoId -> producto_id resuelto

/**
 * @param {string} idsCsv - Comma-separated shelf slot IDs.
 * @param {string} zona - Zone type for assignment context.
 */
function layoutAbrirModalAsignarEntrepano(idsCsv, zona) {
  const ids = idsCsv.split(',').map(Number);
  const huecos = ids
    .map(id => _layoutUbicacionesCache.find(u => u.id === id))
    .filter(Boolean)
    .sort((a, b) => a.codigo.localeCompare(b.codigo));
  if (!huecos.length) return;

  _layoutAsignarEntHuecos = huecos;
  _layoutAsignarEntProductos = {};

  const m = document.getElementById('modal-layout-asignar-entrepano');
  if (!m) return;

  const codigoCuerpo = huecos[0].codigo.split('-').slice(0, 3).join('-');
  document.getElementById('layout-asignar-ent-titulo').textContent =
    `${codigoCuerpo} · ${huecos.length} hueco(s) — deja en blanco los que no vayas a asignar hoy`;

  const cont = document.getElementById('layout-asignar-ent-filas');
  cont.innerHTML = huecos.map(u => {
    const huecoLabel = u.codigo.split('-').pop();
    const yaAsignado = u.producto_asignado_codigo ? `Ya tiene: ${u.producto_asignado_codigo}` : '';
    return `
      <div class="tabla-card" style="margin-bottom:8px;padding:10px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
          <div style="font-size:12px;font-weight:700;font-family:monospace;color:var(--tx);">${huecoLabel}</div>
          <div id="layout-asignar-ent-estado-${u.id}" style="font-size:11px;color:#60a5fa;">${yaAsignado}</div>
        </div>
        <div style="display:flex;gap:6px;">
          <input id="layout-asignar-ent-codigo-${u.id}" type="text" placeholder="Código o código de barras" autocomplete="off"
            onkeydown="if(event.key==='Enter'){event.preventDefault();layoutBuscarProductoEntrepano(${u.id});}"
            style="flex:1;min-width:0;padding:9px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:13px;box-sizing:border-box;">
          <input id="layout-asignar-ent-cantidad-${u.id}" type="number" min="1" placeholder="Cant."
            onkeydown="if(event.key==='Enter'){event.preventDefault();layoutAsignarEntSiguiente(${u.id});}"
            style="width:64px;padding:9px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:13px;text-align:center;box-sizing:border-box;">
        </div>
        ${zona === 'PICKING' ? `
        <input id="layout-asignar-ent-capacidad-${u.id}" type="number" min="0" placeholder="Capacidad máxima (opcional)"
          style="width:100%;margin-top:6px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:12px;box-sizing:border-box;">` : ''}
      </div>`;
  }).join('');

  const resEl = document.getElementById('layout-asignar-ent-resultado');
  if (resEl) resEl.innerHTML = '';

  m.style.display = 'flex';
  setTimeout(() => document.getElementById(`layout-asignar-ent-codigo-${huecos[0].id}`)?.focus(), 50);
}

/** Close the shelf slot assignment modal. */
function layoutCerrarModalAsignarEntrepano() {
  const m = document.getElementById('modal-layout-asignar-entrepano');
  if (m) m.style.display = 'none';
  _layoutAsignarEntHuecos = [];
  _layoutAsignarEntProductos = {};
}

/** @param {number} huecoId - Shelf slot ID to search a product for. */
async function layoutBuscarProductoEntrepano(huecoId) {
  const inp = document.getElementById(`layout-asignar-ent-codigo-${huecoId}`);
  const estado = document.getElementById(`layout-asignar-ent-estado-${huecoId}`);
  const codigo = inp?.value.trim();
  if (!codigo) return;
  try {
    const d = await get(`/api/productos/?q=${encodeURIComponent(codigo)}&per_page=1`);
    const prod = (d.productos || [])[0];
    if (!prod) {
      delete _layoutAsignarEntProductos[huecoId];
      if (estado) { estado.textContent = 'No encontrado'; estado.style.color = '#f87171'; }
      return;
    }
    _layoutAsignarEntProductos[huecoId] = prod.id;
    if (estado) { estado.textContent = `✓ ${prod.codigo} — ${prod.nombre}`; estado.style.color = '#4ade80'; }
    document.getElementById(`layout-asignar-ent-cantidad-${huecoId}`)?.focus();
  } catch (e) {
    if (estado) { estado.textContent = 'Error buscando'; estado.style.color = '#f87171'; }
  }
}

/** @param {number} huecoId - Current shelf slot ID; scroll to the next slot's assignment. */
function layoutAsignarEntSiguiente(huecoId) {
  const idx = _layoutAsignarEntHuecos.findIndex(u => u.id === huecoId);
  const siguiente = _layoutAsignarEntHuecos[idx + 1];
  if (siguiente) document.getElementById(`layout-asignar-ent-codigo-${siguiente.id}`)?.focus();
}

/** Confirm all shelf slot product assignments. */
async function layoutConfirmarAsignarEntrepano() {
  let ok = 0;
  const errores = [];

  for (const u of _layoutAsignarEntHuecos) {
    const productoId = _layoutAsignarEntProductos[u.id];
    const cantidad = parseInt(document.getElementById(`layout-asignar-ent-cantidad-${u.id}`)?.value);
    if (!productoId || isNaN(cantidad) || cantidad <= 0) continue; // fila vacía — se salta, no bloquea el resto

    const capRaw = document.getElementById(`layout-asignar-ent-capacidad-${u.id}`)?.value;
    const capacidad_maxima = capRaw ? parseInt(capRaw) : null;

    try {
      const payload = { producto_id: productoId, cantidad };
      if (capacidad_maxima !== null && !isNaN(capacidad_maxima)) payload.capacidad_maxima = capacidad_maxima;
      await post(`/api/almacenes/ubicaciones/${u.id}/asignar`, payload);
      ok++;
    } catch (e) {
      errores.push(`${u.codigo.split('-').pop()}: ${e.message || 'error'}`);
    }
  }

  if (ok === 0 && errores.length === 0) {
    alerta('No hay ninguna fila completa (código + cantidad) para asignar', 'error');
    return;
  }

  alerta(`${ok} hueco(s) asignado(s)${errores.length ? ` — ${errores.length} error(es)` : ''}`, errores.length ? 'advertencia' : 'ok');
  layoutCargarUbicaciones();

  if (errores.length === 0) {
    layoutCerrarModalAsignarEntrepano();
  } else {
    const resEl = document.getElementById('layout-asignar-ent-resultado');
    if (resEl) resEl.innerHTML = errores.map(e => `<div style="color:#f87171;font-size:11px;margin-top:2px;">✗ ${e}</div>`).join('');
  }
}

// ── Modal: Ver huecos de un Entrepaño (detalle + editar/eliminar/reclasificar) ─

/** @param {string} idsCsv - Comma-separated shelf slot IDs to view assignments for. */
function layoutAbrirModalVerEntrepano(idsCsv) {
  const ids = idsCsv.split(',').map(Number);
  const huecos = ids
    .map(id => _layoutUbicacionesCache.find(u => u.id === id))
    .filter(Boolean)
    .sort((a, b) => a.codigo.localeCompare(b.codigo));
  if (!huecos.length) return;

  const m = document.getElementById('modal-layout-ver-entrepano');
  if (!m) return;

  const codigoCuerpo = huecos[0].codigo.split('-').slice(0, 3).join('-');
  document.getElementById('layout-ver-ent-titulo').textContent =
    `${codigoCuerpo} · Entrepaño ${huecos[0].nivel} · ${huecos.length} hueco(s)`;

  const cont = document.getElementById('layout-ver-ent-filas');
  cont.innerHTML = huecos.map(u => {
    const huecoLabel = u.codigo.split('-').pop();
    const skuLabel = u.producto_asignado_codigo
      ? `📦 ${u.producto_asignado_nombre || u.producto_asignado_codigo} · ${u.stock_actual ?? 0} UND`
      : 'Sin SKU asignado';
    return `
      <div style="display:flex;align-items:center;gap:8px;padding:9px 0;border-top:1px solid var(--brd);">
        <div style="font-size:12px;font-family:monospace;font-weight:700;color:var(--tx);min-width:34px;">${huecoLabel}</div>
        <div style="flex:1;font-size:12px;color:${u.producto_asignado_codigo ? '#60a5fa' : '#888'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${u.producto_asignado_codigo ? `${u.producto_asignado_codigo} — ${u.producto_asignado_nombre || ''}` : ''}">${skuLabel}${!u.activo ? ' · INACTIVA' : ''}</div>
        <div style="display:flex;gap:4px;flex-shrink:0;">
          <button title="Editar" onclick="layoutAbrirModalEditarUbicacion(${u.id})" style="padding:5px 8px;background:var(--bg);border:1px solid var(--brd);border-radius:5px;color:var(--tx2);font-size:11px;cursor:pointer;">✏</button>
          <button title="Eliminar" onclick="layoutEliminarUbicacion(${u.id}, '${u.codigo}')" style="padding:5px 8px;background:var(--bg);border:1px solid #7f1d1d;border-radius:5px;color:#f87171;font-size:11px;cursor:pointer;">🗑</button>
          <button title="Reclasificar" onclick="layoutAbrirModalReclasificar(${u.id})" style="padding:5px 8px;background:var(--bg);border:1px solid var(--brd);border-radius:5px;color:var(--tx2);font-size:11px;cursor:pointer;">⇄</button>
        </div>
      </div>`;
  }).join('');

  m.style.display = 'flex';
}

/** Close the shelf slot view modal. */
function layoutCerrarModalVerEntrepano() {
  const m = document.getElementById('modal-layout-ver-entrepano');
  if (m) m.style.display = 'none';
}

// ── Modal: Editar ubicación individual ───────────────────────────────────────

let _layoutUbEditarId = null;


// ── Importador Excel (Mecanismo A, opción masiva) ────────────────────────────

/** @param {HTMLButtonElement} btn - Button element; imports locations from Excel file. */


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


// ═══════════════════════════════════════════════════════════════════════════════
// COMPRAS — 4 paneles: Velocity+ABC, Dock Lock, Cuarentena, Audit Trail
// ═══════════════════════════════════════════════════════════════════════════════

let COMP_SUBTAB = 'velocity';
let COMP_VELOCITY_DATA = [];    // cache para filtros client-side
let COMP_TIMER = null;
let COMP_PANTALLA = false;      // true si es pantalla-compras (rol compras)

/** Initialize the purchasing (compras) screen and load data. */
function compIniciarPantalla() {
  COMP_PANTALLA = true;
  compCargarResumen('comp2');
  compCargarVelocity('comp2');
  COMP_TIMER = setInterval(() => {
    compCargarResumen(COMP_PANTALLA ? 'comp2' : 'comp');
  }, 60000);
}

/** Fetch and render the active purchasing sub-tab content. */
async function cargarCompras() {
  COMP_PANTALLA = false;
  await Promise.all([
    compCargarResumen('comp'),
    compCargarVelocity('comp'),
  ]);
}

// ── Sub-tabs (admin tab-compras) ──────────────────────────────────────────
/** @param {string} id - Purchasing primary sub-tab to activate. */
function compSubtab(id) {
  COMP_SUBTAB = id;
  const secs = ['velocity','dock','cuarentena','audit'];
  secs.forEach(s => {
    const el = document.getElementById('comp-sec-' + s);
    const tab = document.getElementById('comp-sub-' + s);
    if (el) el.style.display = s === id ? 'block' : 'none';
    if (tab) {
      tab.style.background = s === id ? 'var(--pm)' : 'transparent';
      tab.style.color = s === id ? '#fff' : 'var(--tx3)';
      tab.style.fontWeight = s === id ? '700' : '400';
    }
  });
  if (id === 'velocity') compCargarVelocity('comp');
  else if (id === 'dock') compCargarDock('comp');
  else if (id === 'cuarentena') compCargarCuarentena('comp');
  // audit: manual search — no auto-load
}

// ── Sub-tabs (pantalla-compras dedicada) ──────────────────────────────────
/** @param {string} id - Purchasing secondary sub-tab to activate. */
function compSubtab2(id) {
  COMP_SUBTAB = id;
  const secs = ['velocity','dock','cuarentena','audit'];
  secs.forEach(s => {
    const tab = document.getElementById('comp2-sub-' + s);
    if (tab) {
      tab.style.background = s === id ? 'var(--pm)' : 'transparent';
      tab.style.color = s === id ? '#fff' : 'var(--tx3)';
      tab.style.fontWeight = s === id ? '700' : '400';
    }
  });
  const cont = document.getElementById('comp2-contenido');
  if (!cont) return;
  if (id === 'velocity') compCargarVelocity('comp2');
  else if (id === 'dock') compCargarDock('comp2');
  else if (id === 'cuarentena') compCargarCuarentena('comp2');
  else if (id === 'audit') compRenderAuditForm('comp2');
}

// ── RESUMEN (KPIs header) ─────────────────────────────────────────────────
/** @param {string} prefix - DOM prefix for the purchasing summary panel to load. */
async function compCargarResumen(prefix) {
  try {
    const r = await get('/api/compras/resumen?almacen_id=' + ALMACEN_ID);
    set(prefix + '-kpi-picks', r.picks_7d || 0);
    set(prefix + '-kpi-rechazos', r.recepciones_problema_30d || 0);
    set(prefix + '-kpi-averias', r.averias_pendientes || 0);
    set(prefix + '-kpi-auditorias', r.auditorias_criticas_30d || 0);
  } catch (e) {}
}

// ═══════════════════════════════════════════════════════════════════════════
// VELOCITY + ABC
// ═══════════════════════════════════════════════════════════════════════════

/** @param {string} prefix - DOM prefix for the velocity analytics panel to load. */
async function compCargarVelocity(prefix) {
  prefix = prefix || 'comp';
  const target = prefix === 'comp2'
    ? document.getElementById('comp2-contenido')
    : document.getElementById('comp-lista-velocity');
  if (!target) return;

  const diasSel = document.getElementById('comp-vel-dias');
  const dias = diasSel ? diasSel.value : 30;

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando velocity...</div>';

  try {
    const r = await get(`/api/compras/velocity?dias=${dias}&almacen_id=${ALMACEN_ID}`);
    COMP_VELOCITY_DATA = r.items || [];

    if (prefix === 'comp2') {
      // Render filters + list into comp2-contenido
      target.innerHTML = _compVelocityFiltersHtml() + '<div id="comp2-vel-list"></div>';
      _compRenderVelocityList(document.getElementById('comp2-vel-list'), COMP_VELOCITY_DATA, r);
    } else {
      _compRenderVelocityList(target, COMP_VELOCITY_DATA, r);
    }
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando velocity</div>';
  }
}

/**
 * Build HTML for the velocity analytics filter controls.
 * @returns {string} HTML string.
 */
function _compVelocityFiltersHtml() {
  return `<div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center;">
    <select id="comp2-vel-dias" onchange="compCargarVelocity('comp2')"
      style="padding:8px 12px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:13px;">
      <option value="7">7 días</option><option value="15">15 días</option>
      <option value="30" selected>30 días</option><option value="60">60 días</option>
    </select>
    <select id="comp2-vel-abc" onchange="compFiltrarVelocity('comp2')"
      style="padding:8px 12px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:13px;">
      <option value="">Todos ABC</option><option value="A">Solo A</option>
      <option value="B">Solo B</option><option value="C">Solo C</option>
    </select>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--tx3);cursor:pointer;">
      <input type="checkbox" id="comp2-vel-alertas" onchange="compFiltrarVelocity('comp2')"> Solo alertas
    </label>
  </div>`;
}

/** @param {string} prefix - DOM prefix; apply ABC/text filters to the velocity list. */
function compFiltrarVelocity(prefix) {
  prefix = prefix || 'comp';
  const abcSel = document.getElementById((prefix === 'comp2' ? 'comp2' : 'comp') + '-vel-abc');
  const alertaSel = document.getElementById((prefix === 'comp2' ? 'comp2' : 'comp') + '-vel-alertas');
  const abcFiltro = abcSel ? abcSel.value : '';
  const soloAlertas = alertaSel ? alertaSel.checked : false;

  let filtered = COMP_VELOCITY_DATA;
  if (abcFiltro) filtered = filtered.filter(i => i.abc === abcFiltro);
  if (soloAlertas) filtered = filtered.filter(i => i.alerta);

  const target = prefix === 'comp2'
    ? document.getElementById('comp2-vel-list')
    : document.getElementById('comp-lista-velocity');
  if (target) _compRenderVelocityList(target, filtered, { total_items: filtered.length, alertas_a: filtered.filter(i => i.alerta).length });
}

/**
 * Render the velocity analytics product list.
 * @param {HTMLElement} el - Container element.
 * @param {Array<Object>} items - Velocity data items.
 * @param {Object} meta - Metadata (totals, averages).
 */
function _compRenderVelocityList(el, items, meta) {
  if (!items.length) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Sin datos de velocity en este período</div>';
    return;
  }

  const alertas = meta.alertas_a || 0;
  let html = alertas > 0
    ? `<div style="background:#110a0a;border:1px solid #7f1d1d;border-radius:10px;padding:10px 14px;margin-bottom:12px;">
         <span style="color:#f87171;font-weight:700;">⚠ ${alertas} item(s) A agotándose</span>
         <span style="color:#fca5a5;font-size:11px;"> — consumo > entrada, stock PICKING <15 días</span>
       </div>`
    : '';

  html += `<div style="font-size:11px;color:var(--tx3);margin-bottom:8px;">${items.length} productos con movimiento</div>`;

  html += '<div style="display:flex;flex-direction:column;gap:6px;">';
  for (const it of items) {
    const abcColor = it.abc === 'A' ? '#ef4444' : it.abc === 'B' ? '#f59e0b' : it.abc === 'C' ? '#3b82f6' : '#555';
    const alertaBg = it.alerta ? 'background:#110a0a;border-color:#7f1d1d;' : '';
    html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:10px 12px;${alertaBg}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
        <div style="font-size:13px;font-weight:700;color:var(--tx);">${it.codigo}</div>
        <span style="font-size:11px;font-weight:800;color:${abcColor};background:${abcColor}22;padding:2px 8px;border-radius:6px;">${it.abc}</span>
      </div>
      <div style="font-size:11px;color:var(--tx3);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${it.nombre}</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;font-size:11px;">
        <div><span style="color:var(--tx3);">Picks/día</span><br><strong style="color:var(--tx);">${it.picks_dia}</strong></div>
        <div><span style="color:var(--tx3);">Total período</span><br><strong style="color:var(--tx);">${it.picks_periodo}</strong></div>
        <div><span style="color:var(--tx3);">Stock PICK</span><br><strong style="color:var(--tx);">${it.stock_picking}</strong></div>
        <div><span style="color:var(--tx3);">Días stock</span><br><strong style="color:${it.dias_stock_estimado < 15 ? '#ef4444' : it.dias_stock_estimado < 30 ? '#f59e0b' : 'var(--tx)'};">${it.dias_stock_estimado >= 999 ? '∞' : it.dias_stock_estimado}</strong></div>
      </div>
    </div>`;
  }
  html += '</div>';
  el.innerHTML = html;
}


// ═══════════════════════════════════════════════════════════════════════════
// DOCK LOCK — Rechazos recepción
// ═══════════════════════════════════════════════════════════════════════════

async function compCargarDock(prefix) {
  prefix = prefix || 'comp';

  const isP2 = prefix === 'comp2';
  const target = isP2
    ? document.getElementById('comp2-contenido')
    : document.getElementById('comp-lista-dock');
  if (!target) return;

  const diasSel = isP2 ? null : document.getElementById('comp-dock-dias');
  const dias = diasSel ? diasSel.value : 30;

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando dock lock...</div>';

  try {
    const r = await get(`/api/compras/dock-lock?dias=${dias}&almacen_id=${ALMACEN_ID}`);

    // Stats (solo en admin, comp2 los incluye inline)
    if (!isP2) {
      set('dock-total-prob', r.total_recepciones_con_problema || 0);
      set('dock-total-excesos', r.total_excesos || 0);
      set('dock-total-faltantes', r.total_faltantes || 0);
    }

    let html = '';

    if (isP2) {
      html += `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px;">
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:#ef4444;">${r.total_recepciones_con_problema || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Con problema</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:#f59e0b;">${r.total_excesos || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Excesos</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:#3b82f6;">${r.total_faltantes || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Faltantes</div>
        </div>
      </div>`;
    }

    const recs = r.recepciones || [];
    if (!recs.length) {
      html += '<div style="text-align:center;padding:30px;color:#555;">Sin rechazos en este período</div>';
    } else {
      html += '<div style="display:flex;flex-direction:column;gap:8px;">';
      for (const rec of recs) {
        const fecha = rec.fecha_confirmacion ? new Date(rec.fecha_confirmacion).toLocaleDateString('es-CO') : '—';
        let itemsHtml = '';
        for (const it of rec.items_problema) {
          const difColor = it.tipo_problema === 'EXCESO' ? '#f59e0b' : '#3b82f6';
          const difSign = it.diferencia > 0 ? '+' : '';
          itemsHtml += `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--brd);font-size:12px;">
            <span style="color:var(--tx);">${it.producto_codigo} — ${(it.producto_nombre||'').substring(0,30)}</span>
            <span style="color:${difColor};font-weight:700;">${difSign}${it.diferencia} (${it.tipo_problema})</span>
          </div>`;
        }
        html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div>
              <span style="font-size:13px;font-weight:700;color:var(--tx);">OC ${rec.oc_siesa}</span>
              <span style="font-size:11px;color:var(--tx3);margin-left:8px;">${rec.codigo}</span>
            </div>
            <span style="font-size:11px;color:var(--tx3);">${fecha}</span>
          </div>
          <div style="font-size:12px;color:var(--tx2);margin-bottom:8px;">${rec.proveedor_nombre || rec.proveedor_codigo || '—'}${rec.es_parcial ? ' · <span style="color:#f59e0b;">PARCIAL</span>' : ''}</div>
          <div style="background:var(--bg-s2);border-radius:8px;padding:6px 8px;">
            ${itemsHtml}
          </div>
          <div style="font-size:11px;color:var(--tx3);margin-top:4px;">${rec.total_problemas} item(s) con discrepancia</div>
        </div>`;
      }
      html += '</div>';
    }

    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando dock lock</div>';
  }
}


// ═══════════════════════════════════════════════════════════════════════════
// CUARENTENA / AVERÍAS
// ═══════════════════════════════════════════════════════════════════════════

async function compCargarCuarentena(prefix) {
  prefix = prefix || 'comp';
  const isP2 = prefix === 'comp2';
  const target = isP2
    ? document.getElementById('comp2-contenido')
    : document.getElementById('comp-lista-cuarentena');
  if (!target) return;

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando cuarentena...</div>';

  try {
    const r = await get(`/api/compras/cuarentena?almacen_id=${ALMACEN_ID}`);

    if (!isP2) {
      set('cuar-pendientes', r.pendientes || 0);
      set('cuar-productos', r.total_productos_en_averias || 0);
    }

    let html = '';

    if (isP2) {
      html += `<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px;">
        <div style="background:#110a0a;border:1px solid #7f1d1d;border-radius:10px;padding:14px;text-align:center;">
          <div style="font-size:26px;font-weight:800;color:#f87171;">${r.pendientes || 0}</div>
          <div style="font-size:11px;color:#fca5a5;">Pendientes de gestión</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:14px;text-align:center;">
          <div style="font-size:26px;font-weight:800;color:var(--tx);">${r.total_productos_en_averias || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Productos en zona averías</div>
        </div>
      </div>`;
    }

    // Stock en zona AVERÍAS
    const stock = r.stock_en_averias || [];
    if (stock.length) {
      html += `<div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">Stock en zona AVERÍAS</div>`;
      html += '<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:16px;">';
      for (const s of stock) {
        html += `<div style="display:flex;justify-content:space-between;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:8px 12px;">
          <div style="font-size:12px;"><strong>${s.producto_codigo}</strong> — ${(s.producto_nombre||'').substring(0,35)}</div>
          <div style="font-size:14px;font-weight:800;color:#ef4444;">${s.cantidad_averiada} UND</div>
        </div>`;
      }
      html += '</div>';
    }

    // Tareas averiadas
    const devs = r.devoluciones_averiadas || [];
    if (devs.length) {
      html += `<div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">Devoluciones averiadas (${devs.length})</div>`;
      html += '<div style="display:flex;flex-direction:column;gap:6px;">';
      for (const d of devs) {
        const estadoColor = d.estado === 'PENDIENTE' ? '#f59e0b' : d.estado === 'EN_PROCESO' ? '#3b82f6' : d.estado === 'COMPLETADO' ? '#22c55e' : '#555';
        const diasColor = d.dias_sin_gestion > 7 ? '#ef4444' : d.dias_sin_gestion > 3 ? '#f59e0b' : 'var(--tx3)';
        const siesa = d.siesa_triggered ? '<span style="color:#22c55e;font-size:10px;">✓ Siesa</span>' : '<span style="color:#ef4444;font-size:10px;">✗ Siesa</span>';
        html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:10px 12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <span style="font-size:13px;font-weight:700;color:var(--tx);">${d.codigo}</span>
              <span style="font-size:11px;color:${estadoColor};margin-left:8px;font-weight:700;">${d.estado}</span>
              ${siesa}
            </div>
            <span style="font-size:12px;font-weight:700;color:${diasColor};">${d.dias_sin_gestion}d</span>
          </div>
          <div style="font-size:12px;color:var(--tx2);margin-top:4px;">${d.producto_codigo || '—'} — ${(d.producto_nombre||'').substring(0,35)}</div>
          <div style="font-size:11px;color:var(--tx3);margin-top:2px;">${d.cantidad} UND${d.observaciones ? ' · ' + d.observaciones.substring(0,60) : ''}</div>
        </div>`;
      }
      html += '</div>';
    }

    if (!stock.length && !devs.length) {
      html += '<div style="text-align:center;padding:30px;color:#22c55e;font-weight:700;">Sin averías pendientes</div>';
    }

    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando cuarentena</div>';
  }
}


// ═══════════════════════════════════════════════════════════════════════════
// AUDIT TRAIL — búsqueda por OC/proveedor
// ═══════════════════════════════════════════════════════════════════════════

function compRenderAuditForm(prefix) {
  const target = document.getElementById('comp2-contenido');
  if (!target) return;
  target.innerHTML = `<div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center;">
    <input type="text" id="comp2-audit-buscar" placeholder="Buscar por OC, proveedor, remisión..."
      style="flex:1;min-width:200px;padding:10px 14px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:13px;"
      onkeydown="if(event.key==='Enter')compCargarAudit('comp2')">
    <button onclick="compCargarAudit('comp2')"
      style="padding:10px 18px;background:var(--pm);border:none;border-radius:8px;color:#fff;font-size:13px;font-weight:700;cursor:pointer;">
      Buscar
    </button>
  </div>
  <div id="comp2-audit-results">
    <div style="text-align:center;padding:30px;color:#555;">Escribe OC, proveedor o remisión y presiona Buscar</div>
  </div>`;
}

async function compCargarAudit(prefix) {
  prefix = prefix || 'comp';
  const isP2 = prefix === 'comp2';
  const inputId = isP2 ? 'comp2-audit-buscar' : 'comp-audit-buscar';
  const diasId = isP2 ? null : 'comp-audit-dias';
  const targetId = isP2 ? 'comp2-audit-results' : 'comp-lista-audit';

  const input = document.getElementById(inputId);
  const target = document.getElementById(targetId);
  if (!input || !target) return;

  const q = input.value.trim();
  if (!q) {
    alerta('Escribe algo para buscar', 'advertencia');
    return;
  }

  const diasSel = diasId ? document.getElementById(diasId) : null;
  const dias = diasSel ? diasSel.value : 90;

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Buscando...</div>';

  try {
    const r = await get(`/api/compras/audit-trail?q=${encodeURIComponent(q)}&dias=${dias}&almacen_id=${ALMACEN_ID}`);
    const resultados = r.resultados || [];

    if (!resultados.length) {
      target.innerHTML = `<div style="text-align:center;padding:30px;color:#555;">Sin resultados para "${q}"</div>`;
      return;
    }

    let html = `<div style="font-size:11px;color:var(--tx3);margin-bottom:8px;">${resultados.length} recepción(es) encontradas</div>`;
    html += '<div style="display:flex;flex-direction:column;gap:8px;">';

    for (const rec of resultados) {
      const fecha = rec.fecha_confirmacion ? new Date(rec.fecha_confirmacion).toLocaleDateString('es-CO') : rec.fecha_creacion ? new Date(rec.fecha_creacion).toLocaleDateString('es-CO') : '—';
      const estadoColor = rec.estado === 'CONFIRMADA' ? '#22c55e' : rec.estado === 'EN_PROCESO' ? '#3b82f6' : rec.estado === 'CANCELADA' ? '#ef4444' : '#f59e0b';
      const siesa = rec.siesa_triggered ? '<span style="color:#22c55e;font-size:10px;">✓ Siesa</span>' : '<span style="color:#ef4444;font-size:10px;">✗ Siesa</span>';

      let itemsHtml = '';
      for (const it of (rec.items || [])) {
        const difColor = it.es_exceso ? '#f59e0b' : it.es_faltante ? '#3b82f6' : '#22c55e';
        const difText = it.diferencia !== 0 ? ` (${it.diferencia > 0 ? '+' : ''}${it.diferencia})` : '';
        itemsHtml += `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--brd);font-size:11px;">
          <span style="color:var(--tx);">${it.producto_codigo}${it.tipo === 'BONIFICACION' ? ' <span style="color:#8b5cf6;">BONIF</span>' : ''}</span>
          <span>OC: ${it.cantidad_ordenada} → Rec: <strong style="color:${difColor};">${it.cantidad_recibida}${difText}</strong></span>
        </div>`;
      }

      html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <div>
            <span style="font-size:13px;font-weight:700;color:var(--tx);">OC ${rec.oc_siesa}</span>
            <span style="font-size:11px;color:${estadoColor};margin-left:8px;font-weight:700;">${rec.estado}</span>
            ${siesa}
          </div>
          <span style="font-size:11px;color:var(--tx3);">${fecha}</span>
        </div>
        <div style="font-size:12px;color:var(--tx2);margin-bottom:2px;">${rec.proveedor_nombre || rec.proveedor_codigo || '—'}</div>
        <div style="font-size:11px;color:var(--tx3);margin-bottom:6px;">
          Recepcionista: <strong>${rec.recepcionista || '—'}</strong>
          · Remisión: ${rec.remision || '—'}
          · ${rec.codigo}
          ${rec.es_parcial ? ' · <span style="color:#f59e0b;">PARCIAL</span>' : ''}
        </div>
        <div style="background:var(--bg-s2);border-radius:8px;padding:6px 8px;max-height:200px;overflow-y:auto;">
          ${itemsHtml}
        </div>
        <div style="font-size:11px;color:var(--tx3);margin-top:4px;">${rec.total_items} item(s)</div>
      </div>`;
    }
    html += '</div>';
    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error en búsqueda</div>';
  }
}
