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
  const esAdmin = ['admin','gerente','jefe_almacen','supervisor','control_flota'].includes(rol);
  // control_flota entra al shell de admin pero SOLO ve Flota. No es cosmética:
  // el procedimiento dice que ve el tablero y no aprueba, y dejarle a la vista
  // pestañas que el backend le va a negar con 403 enseña a ignorar errores.
  const soloFlota = rol === 'control_flota';
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
    flotaCondCargar();
    cargarRutasConductor();
    TIMER_OPERARIO = setInterval(cargarRutasConductor, 30000);
  } else if (esAdmin) {
    if (soloFlota) {
      pantalla('pantalla-admin');
      if (OPERARIO) actualizarUI(OPERARIO);
      document.querySelectorAll('.nav-tab').forEach(el => {
        if (!(el.getAttribute('onclick') || '').includes('tab-flota')) el.style.display = 'none';
      });
      tab('tab-flota');
      return;
    }
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
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  try {
    const r = await fetch(API + url, { headers: { Authorization: 'Bearer ' + TOKEN }, signal: ctrl.signal });
    return _checkResp(r);
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('Tiempo de espera agotado — intenta de nuevo');
    throw e;
  } finally { clearTimeout(timer); }
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
  else if (TAB === 'tab-vigia') { if (!desdeTimer) await cargarVigia(); }
  else if (TAB === 'tab-flota') { if (!desdeTimer) await cargarFlota(); }
}

/** @param {string} id - Tab element ID to activate (e.g. 'tab-dashboard'). */
function tab(id) {
  const TABS = ['tab-dashboard','tab-pedidos','tab-requisiciones','tab-traslados','tab-bodega','tab-operarios','tab-usuarios','tab-stock','tab-connekta','tab-muelle','tab-rutas','tab-inventario','tab-reposicion','tab-liquidacion','tab-layout','tab-compras','tab-etiquetas','tab-vigia','tab-flota'];
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




/**
 * Show modal to request the supplier's remision/invoice number (required by Siesa).
 * @returns {Promise<string|null>} Remision number, or null if cancelled.
 */

/** Exit active reception scan and return to the reception list. */

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Recepción de Traslados (NB1)
// ─────────────────────────────────────────────────────────────



// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Tabs (OCs / Traslados / Devoluciones)
// ─────────────────────────────────────────────────────────────


// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Flujo de ubicación de devolución
// ─────────────────────────────────────────────────────────────






// EMPACADOR — Estado global
// ─────────────────────────────────────────────────────────────


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
        <option value="control_flota" ${u.rol==='control_flota'?'selected':''}>Control de flota</option>
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


/** @param {Array<Object>} grupos - Muelle groups to render as sortable cards. */

// Referencia a los grupos actuales para poder reordenarlos sin ir al servidor
// Manifiesto de la ruta activa (grupos ordenados) para reordenamiento en memoria

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







// Paradas dinámicas en el form






// ── Vehículos ────────────────────────────────────────





// ── Conductores ──────────────────────────────────────








// ─────────────────────────────────────────────────────────────
// CONDUCTOR — Pantalla de confirmación de entregas en campo
// ─────────────────────────────────────────────────────────────



// ── Lista de rutas del conductor ──────────────────────────────────


// ── Formulario de confirmación de parada ──────────────────────────








// ── Offline: init, barras de estado y motor de sync ──────────────




// ══════════════════════════════════════════════════════════════════
// PLANILLA DE CUADRE — Admin
// ══════════════════════════════════════════════════════════════════


/** @param {number} id - Route ID to force-close from admin (bypasses driver confirmation). */

/** @param {number} id - Route ID to show the settlement sheet (planilla) for. */

/** @param {number} id - Route ID to fetch and render the settlement sheet for. */

/** @param {number} id - Route ID to trigger financial liquidation for. */

// rutaLiquidarSiesa → movida a rutas.js


// ── Config bodega por almacén ─────────────────────────────────────────────────










/** Confirm and send the inventory adjustment to Siesa. */

// ══════════════════════════════════════════════════════════════════
// TRASLADOS — Admin tab
// ══════════════════════════════════════════════════════════════════



// ── Admin Pedir — solicitar traslado hacia NB1 ──────────────────


// ══════════════════════════════════════════════════════════════════════════
// Banner de modo — protege lo único que no se recarga: los hábitos.
//
// En un ensayo con datos parciales, ver "faltan 400 tableros" produce una de
// dos cosas y ambas son malas: o se le cree (y se aprende a obedecer números
// falsos) o se descubre que estaba mal (y se aprende que el sistema miente).
// La etiqueta cuesta nada y evita las dos.
// ══════════════════════════════════════════════════════════════════════════
function _pintarBannerModo(modo) {
  const el = document.getElementById('banner-modo');
  if (!el) return;

  // REGLA 0 aplicada al propio banner: solo un 'produccion' EXPLÍCITO lo apaga.
  // Si la respuesta no llegó, vino rara, o el campo falta por configuración,
  // se asume que NO es producción y se avisa. Un banner de más es una molestia;
  // un banner de menos es alguien tomando por real un número de ensayo — y esa
  // es la misma omisión de configuración que produjo el 403 del Vigía.
  if (modo === 'produccion') { el.style.display = 'none'; return; }

  const cfg = modo === 'datos_de_prueba'
    ? { txt: 'DATOS DE PRUEBA — Siesa apunta al ambiente QA, no a producción', bg: '#7F1D1D', fg: '#FCA5A5' }
    : modo === 'simulacion'
    ? { txt: 'MODO SIMULACIÓN — datos ficticios, nada llega a Siesa', bg: '#7C2D12', fg: '#FDBA74' }
    : modo === 'ensayo'
    ? { txt: 'MODO ENSAYO — los números de pantalla NO son la realidad', bg: '#78350F', fg: '#FCD34D' }
    : { txt: 'MODO NO VERIFICADO — no asumas que estos números son reales', bg: '#7F1D1D', fg: '#FCA5A5' };

  el.textContent = cfg.txt;
  el.style.background = cfg.bg;
  el.style.color = cfg.fg;
  el.style.display = 'block';
}

async function verificarModoSistema() {
  try {
    const r = await fetch(API + '/api/health/ping');
    if (!r.ok) { _pintarBannerModo(null); return; }
    const d = await r.json();
    _pintarBannerModo(d.modo);
  } catch (_) {
    // Sin respuesta no se puede afirmar que sea producción
    _pintarBannerModo(null);
  }
}

document.addEventListener('DOMContentLoaded', verificarModoSistema);
