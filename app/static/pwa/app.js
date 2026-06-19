'use strict';

// ── Tema (dark/light) — aplica antes de cualquier render ─────────────────────
(function () {
  if (localStorage.getItem('wms_theme') === 'light') {
    document.body.classList.add('light');
  }
})();

function _actualizarLogo(isLight) {
  const src = isLight ? '/static/pwa/logo-h.png' : '/static/pwa/logo-white.png';
  document.querySelectorAll('.header-logo img, .login-logo img').forEach(img => { img.src = src; });
}

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
let SIESA_OCS = [];            // OCs cargadas desde Siesa (pantalla recepcionista)
let RUTA_ACTIVA_ID = null;     // ruta EN_CARGUE seleccionada en tab-muelle
let RUTAS_TIPO_SEL = 'Urbana'; // tipo seleccionado en form nueva ruta
let RUTAS_SUBTAB = 'rutas';    // sub-tab activo en tab-rutas
let MUELLE_TIMER = null;

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
    TIMER_ADMIN = setInterval(cargarAdmin, 30000);
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

function pararTimers() {
  clearInterval(TIMER_ADMIN);
  clearInterval(TIMER_OPERARIO);
  clearInterval(TIMER_REC);
  if (typeof COMP_TIMER !== 'undefined') clearInterval(COMP_TIMER);
  RECEPCION_ACTUAL = null;
  DEVOLUCION_ACTUAL = null;
}

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

function guardarOffline(datos) {
  COLA_OFFLINE.push({ ...datos, ts: Date.now() });
  localStorage.setItem('wms_cola_offline', JSON.stringify(COLA_OFFLINE));
  alerta('Sin WiFi — guardado para sincronizar', 'advertencia');
}

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

async function post(url, body) {
  const r = await fetch(API + url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(body)
  });
  return _checkResp(r);
}

async function put(url, body = {}) {
  const r = await fetch(API + url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(body)
  });
  return _checkResp(r);
}

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

function actualizarUI(op) {
  ['op-nombre','admin-nombre','rec-nombre','abast-nombre','emp-nombre'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = op.nombre; });
  ['op-rol','admin-rol'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = op.rol; });
}

function salir(porExpiracion = false) {
  pararTimers();
  TOKEN = null; OPERARIO = null; TAREA_ACTUAL = null;
  localStorage.removeItem('wms_token');
  localStorage.removeItem('wms_operario');
  pantalla('pantalla-login');
  if (porExpiracion) alerta('Sesión expirada — vuelve a ingresar', 'advertencia');
}

async function cargarAdmin() {
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
  else if (TAB === 'tab-compras') await cargarCompras();
}

function tab(id) {
  const TABS = ['tab-dashboard','tab-pedidos','tab-requisiciones','tab-bodega','tab-operarios','tab-usuarios','tab-stock','tab-connekta','tab-muelle','tab-rutas','tab-inventario','tab-traslados','tab-reposicion','tab-compras'];
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
    _semaforo('sem-siesa',
      siesa.modo_simulacion ? 'gris' : (siesa.modo_ensayo ? 'amarillo' : 'verde'),
      siesa.modo_simulacion ? 'Simulación' : (siesa.modo_ensayo ? 'Ensayo' : 'Conectado'));

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

function _semaforo(id, color, texto) {
  const el = document.getElementById(id);
  if (!el) return;
  el.className = `sem-item sem-${color}`;
  const lbl = el.querySelector('.sem-lbl');
  if (lbl) lbl.textContent = el.querySelector('.sem-lbl').textContent.split('\n')[0].split(':')[0] + ': ' + texto;
}

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

async function reabrirTareaPicking(id) {
  if (!confirm('¿Reabrir esta tarea al pool de picking? El operario que llegue a esa ubicación la tomará de nuevo.')) return;
  try {
    const r = await fetch(API + `/api/picking/${id}/reabrir`, { method: 'PUT', headers: { Authorization: 'Bearer ' + TOKEN } });
    const d = await r.json();
    if (r.ok) { alerta('Tarea reabierta al pool ✓', 'exito'); await cargarTareasBodega(); }
    else alerta(d.error || 'Error al reabrir', 'error');
  } catch (e) { alerta('Error de conexión', 'error'); }
}

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

function auditoriaMostrarPanel(id) {
  document.getElementById(`auditoria-panel-${id}`).style.display = 'block';
}

function auditoriaCancelarPanel(id) {
  document.getElementById(`auditoria-panel-${id}`).style.display = 'none';
}

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
    SIESA_PEDIDOS.sort((a, b) => {
      const g = p => p.siesa_triggered ? 2 : (p.packing_estado === 'VERIFICADO' && !p.siesa_triggered) ? 3 : (p.picking_iniciado || p.packing_estado) ? 1 : 0;
      return g(a) - g(b);
    });
    let html = '';

    if (siesa.simulado) {
      html += `<div style="background:#1a1a00;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#facc15;border:1px solid #333300;">⚡ Connekta en simulación — conecta credenciales para ver pedidos reales</div>`;
    } else if (SIESA_PEDIDOS.length) {
      const _g = p => p.siesa_triggered ? 2 : (p.packing_estado === 'VERIFICADO' && !p.siesa_triggered) ? 3 : (p.picking_iniciado || p.packing_estado) ? 1 : 0;
      const _GL = [['POR DESPACHAR','#aaa'],['EN PROCESO','#93c5fd'],['DESPACHADO EN SIESA','#4ade80'],['ERROR SIESA','#fca5a5']];
      let _ga = -1;
      html += SIESA_PEDIDOS.map((p, i) => {
        const _gp = _g(p);
        let _header = '';
        if (_gp !== _ga) { _ga = _gp; _header = `<div style="font-size:11px;font-weight:700;color:${_GL[_gp][1]};text-transform:uppercase;letter-spacing:0.8px;padding:12px 0 5px;border-bottom:1px solid #222;margin-bottom:6px;margin-top:${_gp===0?'0':'14px'};">${_GL[_gp][0]}</div>`; }
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

        return _header + `
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
          </div>`;
      }).join('');
    } else {
      html += `<div style="background:#0d1a0d;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#4ade80;border:1px solid #1a2a1a;">✓ Sin pedidos pendientes en Siesa</div>`;
    }

    if (!html) html = '<div style="color:#555;text-align:center;padding:40px;">Sin actividad ✓</div>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando pedidos</div>';
  }
}

async function cargarTareasBodega() {
  const el = document.getElementById('lista-tareas-bodega');
  if (!el) return;
  try {
    const d = await get('/api/picking/?activas=true&per_page=50');
    const tareas = d.tareas || [];
    if (!tareas.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin tareas activas en bodega ✓</div>';
      return;
    }
    const MOTIVO_LABEL = {
      UBICACION_VACIA:    '📦 Ubicación vacía',
      FALTANTE:           '📉 Faltante parcial',
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
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;">Error cargando tareas de bodega</div>';
  }
}

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
function buscarProductos() {
  clearTimeout(_buscarTimer);
  _buscarTimer = setTimeout(() => cargarCatalogo(1), 400);
}

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

async function pedirTarea() {
  try {
    const d = await get('/api/mobile/tarea-actual');
    if (!d || d.sin_tareas) {
      TAREA_ACTUAL = null;
      const _esTiendaOp = OPERARIO && ['picker_traslado', 'packer_traslado'].includes(OPERARIO.rol);
      document.getElementById('contenido-tarea').innerHTML = _esTiendaOp ? `
        <div style="text-align:center;padding:40px 20px 16px;">
          <div style="font-size:60px;">📦</div>
          <div style="font-size:22px;font-weight:700;margin-top:12px;">No hay traslados pendientes</div>
          <div style="font-size:14px;color:#666;margin-top:6px;">Serás asignado automáticamente cuando llegue una solicitud</div>
          <div style="display:flex;gap:10px;justify-content:center;margin-top:14px;">
            <span style="font-size:11px;padding:3px 10px;border-radius:10px;background:#431407;color:#fb923c;font-weight:700;">TRASLADO</span>
          </div>
        </div>` : `
        <div style="text-align:center;padding:40px 20px 16px;">
          <div style="font-size:60px;">✓</div>
          <div style="font-size:24px;font-weight:700;margin-top:12px;">Sin tareas pendientes</div>
          <div style="font-size:14px;color:#666;margin-top:6px;">El sistema te asignará la próxima automáticamente</div>
          <div style="display:flex;gap:10px;justify-content:center;margin-top:14px;">
            <span style="font-size:11px;padding:3px 10px;border-radius:10px;background:#1e3a5f;color:#93c5fd;font-weight:700;">PEDIDO</span>
            <span style="font-size:11px;padding:3px 10px;border-radius:10px;background:#431407;color:#fb923c;font-weight:700;">TRASLADO</span>
          </div>
        </div>`;
      return;
    }
    TAREA_ACTUAL = d;
    renderTarea(d);
  } catch (e) {
    console.error('Error cargando tarea:', e);
    const el = document.getElementById('contenido-tarea');
    if (el) el.innerHTML = `
      <div style="text-align:center;padding:40px 20px;">
        <div style="font-size:40px;">⚠️</div>
        <div style="font-size:16px;font-weight:700;color:#f87171;margin-top:12px;">Error al cargar tareas</div>
        <div style="font-size:13px;color:#666;margin-top:6px;">${e.message || 'Error de conexión'}</div>
        <button onclick="pedirTarea()" style="margin-top:16px;padding:12px 24px;background:#1e3a5f;color:#93c5fd;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">
          🔄 Reintentar
        </button>
      </div>`;
  }
}

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

async function trasConfirmarRecogida(id) {
  let solicitud;
  try {
    const r = await fetch(API + `/api/traslados/${id}`, { headers: { Authorization: 'Bearer ' + TOKEN } });
    solicitud = await r.json();
  } catch (e) { alerta('Error de conexión', 'error'); return; }

  const items = (solicitud.items || []).filter(i => (i.cantidad_aprobada || i.cantidad_solicitada) > 0);
  const filas = items.map(i => {
    const aprobada = i.cantidad_aprobada || i.cantidad_solicitada;
    return `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
      <div style="flex:1;font-size:13px;">
        <div style="font-weight:700;">${i.producto_codigo}</div>
        <div style="color:#666;font-size:11px;">A recoger: ${aprobada} und</div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;">
        <label style="font-size:11px;color:#999;">Recogí:</label>
        <input type="number" id="rec-${i.id}" value="${aprobada}" min="0"
          style="width:72px;padding:8px;background:#1a1a1a;border:1px solid #333;border-radius:6px;color:#fff;font-size:15px;text-align:center;">
      </div>
    </div>`;
  }).join('');

  const modal = document.createElement('div');
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.95);z-index:9999;display:flex;align-items:flex-end;justify-content:center;">
      <div style="background:#111;border-radius:20px 20px 0 0;padding:24px;width:100%;max-width:480px;border-top:2px solid #7c3aed;max-height:80vh;overflow-y:auto;">
        <div style="font-size:18px;font-weight:700;margin-bottom:4px;">Confirmar recogida</div>
        <div style="font-size:12px;color:#666;margin-bottom:18px;">
          ${solicitud.nombre_punto_venta || solicitud.bodega_destino_siesa} · Ajusta si encontraste menos de lo esperado
        </div>
        ${filas}
        <div style="display:flex;gap:8px;margin-top:8px;">
          <button id="btn-rec-ok"
            style="flex:1;padding:14px;background:#7c3aed;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer;">
            Listo — entregué al admin
          </button>
          <button onclick="this.closest('[style*=fixed]').remove()"
            style="padding:14px 18px;background:#222;color:#fff;border:none;border-radius:12px;font-size:14px;cursor:pointer;">
            Cancelar
          </button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  modal.querySelector('#btn-rec-ok').onclick = async () => {
    const items_confirmados = items.map(i => ({
      id: i.id,
      cantidad_confirmada: Number(document.getElementById(`rec-${i.id}`).value) || 0
    }));
    modal.remove();
    try {
      const r = await fetch(API + `/api/traslados/${id}/confirmar-picking`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify({ items_confirmados })
      });
      const d = await r.json();
      if (r.ok) {
        alerta('Recogida confirmada — el admin puede despachar', 'exito');
        cargarTrasladosOperario();
      } else { alerta(d.error || 'Error', 'error'); }
    } catch (e) { alerta('Error de conexión', 'error'); }
  };
}

function renderTarea(t) {
  _pickingTotal = t.cantidad_escaneada || 0;
  const colores = { PICKING: '#1d4ed8', PACKING: '#7c3aed', CONTEO: '#b45309' };
  const color = colores[t.tipo] || '#333';
  const esConteo = t.tipo === 'CONTEO';
  const esPicking = t.tipo === 'PICKING';
  const pct = t.cantidad_requerida ? Math.min((t.cantidad_escaneada / t.cantidad_requerida) * 100, 100) : 0;
  const puedeCamara = OPERARIO && OPERARIO.puede_usar_camara;

  // Empaque
  const factor       = t.factor_conversion || 1;
  const tieneEmpaque = esPicking && factor > 1;
  const unidadLabel  = (t.unidad_empaque || 'PKG').toUpperCase();
  const pkgs         = t.empaques_escaneados || 0;
  const unds         = t.cantidad_escaneada || 0;
  const req          = t.cantidad_requerida || 0;
  const pkgsReq      = factor > 1 ? Math.ceil(req / factor) : req;
  const sueltas      = factor > 1 ? unds % factor : 0;

  const htmlContador = tieneEmpaque
    ? `<div style="background:#1a1a1a;border-radius:16px;padding:16px 20px;margin-bottom:12px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div style="font-size:12px;color:#555;padding-top:6px;letter-spacing:1px;">CANTIDAD</div>
          <div style="text-align:right;">
            <div id="contador-pkg" style="font-size:80px;font-weight:900;color:#22c55e;line-height:1;">${pkgs}</div>
            <div id="contador-und" style="font-size:20px;font-weight:700;color:#22c55e;margin-top:2px;">de ${pkgsReq} ${unidadLabel}${sueltas > 0 ? ` +${sueltas} und` : ''}</div>
            <div id="contador-factor" style="font-size:12px;color:#555;margin-top:4px;">${unds}/${req} und totales</div>
          </div>
        </div>
        <div style="height:8px;background:#333;border-radius:4px;margin-top:12px;">
          <div id="barra" style="height:100%;background:#22c55e;border-radius:4px;width:${pct}%;transition:width 0.3s;"></div>
        </div>
      </div>`
    : esConteo
      ? `<div style="background:#1a1a1a;border-radius:16px;padding:20px;margin-bottom:12px;text-align:center;">
          <div style="font-size:13px;color:#666;">CONTEO CIEGO</div>
          <div id="contador" style="font-size:64px;font-weight:900;">0</div>
          <div style="font-size:13px;color:#555;margin-top:6px;">Cuenta sin ver cantidad esperada</div>
        </div>`
      : `<div style="background:#1a1a1a;border-radius:16px;padding:20px;margin-bottom:12px;text-align:center;">
          <div style="font-size:13px;color:#666;">CANTIDAD</div>
          <div id="contador" style="font-size:64px;font-weight:900;">${unds}/${req}</div>
          <div style="height:8px;background:#333;border-radius:4px;margin-top:10px;">
            <div id="barra" style="height:100%;background:#22c55e;border-radius:4px;width:${pct}%;transition:width 0.3s;"></div>
          </div>
        </div>`;

  // Etiqueta de tipo de documento (PEDIDO / TRASLADO) para picking
  const _tipoDoc = t.tipo_documento || (t.referencia_documento && t.referencia_documento.startsWith('ST-') ? 'TRASLADO' : 'PEDIDO');
  const _etiquetaTipoDoc = _tipoDoc === 'TRASLADO'
    ? `<span style="font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;background:#431407;color:#fb923c;margin-left:8px;letter-spacing:.5px;">TRASLADO</span>`
    : `<span style="font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;background:#1e3a5f;color:#93c5fd;margin-left:8px;letter-spacing:.5px;">PEDIDO</span>`;

  document.getElementById('contenido-tarea').innerHTML = `
    <div style="padding:16px;">
      <div style="background:${color};color:#fff;border-radius:12px;padding:10px 16px;font-size:20px;font-weight:700;text-align:center;margin-bottom:16px;display:flex;align-items:center;justify-content:center;">${t.tipo}${esPicking ? _etiquetaTipoDoc : ''}</div>

      <div style="background:#000;border:1px solid #222;border-radius:16px;padding:20px;margin-bottom:12px;">
        <div style="font-size:13px;color:#666;">UBICACIÓN</div>
        <div style="font-size:44px;font-weight:900;letter-spacing:2px;">${t.ubicacion}</div>
      </div>

      <div style="background:#111;border-radius:16px;padding:16px;margin-bottom:12px;">
        <div style="font-size:13px;color:#666;">PRODUCTO</div>
        <div style="font-size:26px;font-weight:700;">${t.producto_codigo}</div>
        <div style="font-size:15px;color:#aaa;">${t.producto_nombre}</div>
      </div>

      ${esPicking && t.producto_id ? `
      <div id="card-descomposicion" style="background:#0a1a0a;border:2px solid #166534;border-radius:16px;padding:16px;margin-bottom:12px;text-align:center;">
        <div style="font-size:12px;color:#4ade80;font-weight:700;margin-bottom:6px;letter-spacing:1px;">EMPAQUE SUGERIDO</div>
        <div id="descomp-texto" style="font-size:22px;font-weight:800;color:#fff;">Calculando...</div>
        <div id="descomp-hint" style="font-size:12px;color:#166534;margin-top:4px;"></div>
        <button id="btn-generar-lpn-picking" onclick="_generarLPNEnPicking()" style="display:none;margin-top:10px;width:100%;padding:10px;font-size:13px;font-weight:700;background:#1a2a1a;color:#4ade80;border:1px solid #166534;border-radius:10px;cursor:pointer;">
          📦 Paca sin etiqueta — Generar LPN e imprimir
        </button>
      </div>` : ''}

      ${htmlContador}

      ${puedeCamara ? `
      <button onclick="abrirCamara()" style="width:100%;padding:14px;font-size:17px;background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;margin-bottom:10px;">
        📷 Escanear con cámara
      </button>
      <div id="camara-box" style="display:none;margin-bottom:10px;">
        <div id="lector-qr" style="border-radius:12px;overflow:hidden;"></div>
        <button onclick="cerrarCamara()" style="width:100%;padding:10px;margin-top:6px;font-size:15px;background:#333;color:#fff;border:none;border-radius:10px;cursor:pointer;">Cerrar cámara</button>
      </div>` : ''}

      <button id="btn-ok" onclick="${esPicking ? 'confirmarConGuard()' : 'confirmar()'}" ${(esConteo || esPicking || (req > 0 && unds >= req)) ? '' : 'disabled'}
        style="width:100%;padding:20px;font-size:22px;font-weight:700;background:${esPicking ? (unds >= req ? '#16a34a' : (unds > 0 ? '#b45309' : '#4b5563')) : ((esConteo || (req > 0 && unds >= req)) ? '#16a34a' : '#000')};color:#fff;border:none;border-radius:16px;cursor:pointer;opacity:${(esConteo || esPicking || (req > 0 && unds >= req)) ? 1 : 0.3};margin-bottom:10px;">
        ✓ Confirmar
      </button>

      ${!esConteo ? `
      <button onclick="confirmarManual(${t.id}, ${t.cantidad_requerida})"
        style="width:100%;padding:14px;font-size:15px;font-weight:600;background:#1a2a1a;color:#4ade80;border:1px solid #166534;border-radius:12px;cursor:pointer;margin-bottom:10px;">
        ✓ Confirmar conteo manual
      </button>` : ''}

      <button onclick="reportarProblema(${t.id})"
        style="width:100%;padding:14px;font-size:15px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:12px;cursor:pointer;">
        ⚠ Reportar problema
      </button>

      ${(t.cliente || t.referencia) ? `
      <div style="background:#0a1628;border:1px solid #1e3a5f;border-radius:12px;padding:10px 14px;margin-top:8px;display:flex;align-items:center;gap:10px;">
        <span style="font-size:18px;">🏪</span>
        <div>
          ${t.cliente ? `<div style="font-size:14px;font-weight:700;color:#60a5fa;">${t.cliente}</div>` : ''}
          ${t.referencia ? `<div style="font-size:11px;color:#3b82f6;">Pedido ${t.referencia}</div>` : ''}
        </div>
      </div>` : ''}

      ${t.conteo_intercalado ? `
      <div style="background:#1c1a0a;border:1px solid #b45309;border-radius:12px;padding:14px;margin-top:12px;">
        <div style="font-size:12px;color:#f59e0b;font-weight:700;margin-bottom:4px;">📊 CONTEO PENDIENTE AQUÍ</div>
        <div style="font-size:14px;font-weight:700;color:#fde68a;">${t.conteo_intercalado.producto_codigo}</div>
        <div style="font-size:12px;color:#d97706;">${t.conteo_intercalado.producto_nombre}</div>
        <div style="font-size:11px;color:#78350f;margin-top:4px;">Clase ${t.conteo_intercalado.clasificacion} · Hazlo al terminar el picking</div>
      </div>` : ''}
    </div>`;

  // Cargar descomposición de empaques en segundo plano
  if (esPicking && t.producto_id) {
    _cargarDescomposicionPicking(t.producto_id, t.almacen_id, t.cantidad_requerida);
  }
}

async function _cargarDescomposicionPicking(productoId, almacenId, cantidad) {
  const cardTexto = document.getElementById('descomp-texto');
  const cardHint  = document.getElementById('descomp-hint');
  const btnLPN    = document.getElementById('btn-generar-lpn-picking');
  if (!cardTexto) return;
  try {
    const d = await post('/api/empaques/descomponer', {
      producto_id: productoId,
      almacen_id: almacenId || null,
      cantidad_solicitada: cantidad
    });
    if (!cardTexto) return; // operario ya avanzó a otra tarea
    if (!d || d.factor_empaque <= 1) {
      const card = document.getElementById('card-descomposicion');
      if (card) card.style.display = 'none';
      return;
    }
    const lpns    = d.lpns || 0;
    const sueltas = d.sueltas || 0;
    const unidad  = d.unidad_empaque || 'PACA';
    let texto = '';
    if (lpns > 0 && sueltas > 0) {
      texto = `${lpns} ${unidad} + ${sueltas} UND`;
    } else if (lpns > 0) {
      texto = `${lpns} ${unidad}`;
    } else {
      texto = `${sueltas} UND sueltas`;
    }
    cardTexto.textContent = texto;
    cardHint.textContent  = `Escanea ${unidad.toLowerCase()} por ${unidad.toLowerCase()} — cada scan = ${d.factor_empaque} und`;

    // Lazy labeling: si hay pacas pero el producto no tiene LPNs activos,
    // mostrar el botón para que el picker genere la etiqueta en el momento.
    if (btnLPN && lpns > 0) {
      // Verificar si ya existen LPNs activos para este producto en bodega
      try {
        const lpnsActivos = await get(`/api/empaques/lpn/producto/${productoId}?almacen_id=${almacenId || ''}`);
        const hayLPNs = (lpnsActivos.lpns || []).length > 0;
        if (!hayLPNs) {
          btnLPN.style.display = 'block';
          // Guardar contexto para el generador
          btnLPN._productoId = productoId;
          btnLPN._almacenId  = almacenId;
          btnLPN._factor     = d.factor_empaque;
          btnLPN._unidad     = unidad;
        }
      } catch (_) { /* no bloquear el flujo si falla */ }
    }
  } catch (_) {
    if (cardTexto) {
      cardTexto.textContent = `${cantidad} UND`;
      cardHint.textContent  = '';
    }
  }
}

async function _generarLPNEnPicking() {
  const btn = document.getElementById('btn-generar-lpn-picking');
  if (!btn || !TAREA_ACTUAL) return;

  const productoId = btn._productoId;
  const almacenId  = btn._almacenId;
  const factor     = btn._factor || 1;
  const unidad     = btn._unidad || 'PACA';

  const cantStr = prompt(
    `Paca sin etiqueta detectada.\n` +
    `¿Cuántas unidades tiene esta ${unidad}?\n` +
    `(Factor estándar: ${factor} und)`
  );
  if (cantStr === null) return;
  const cantidad = parseInt(cantStr) || factor;

  try {
    const r = await post('/api/empaques/lpn/generar', {
      producto_id: productoId,
      cantidad_actual: cantidad,
      almacen_id: almacenId || null,
      notas: `Etiquetado en picking — tarea ${TAREA_ACTUAL.id}`
    });
    if (r.error) { alerta(r.error, 'error'); return; }
    alerta(`LPN ${r.lpn.codigo} generado — imprimiendo etiqueta...`, 'exito');
    imprimirEtiquetaLPN(r.lpn, TAREA_ACTUAL.producto_nombre);
    // Ocultar el botón — ya tiene etiqueta
    btn.style.display = 'none';
  } catch (e) { alerta('Error generando LPN', 'error'); }
}

// ─── Quagga2 — debounce interno ───────────────────────────────────────────────
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

async function _quaggaStop() {
  if (!window.Quagga) return;
  try { Quagga.offDetected(_onQuaggaDetect); } catch (_) {}
  try { Quagga.stop(); } catch (_) {}
}

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
        readers: [
          'ean_reader', 'ean_8_reader',
          'code_128_reader', 'code_39_reader',
          'upc_reader', 'upc_e_reader'
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

      // Rectángulo de enfoque sobre el video
      const ov = document.createElement('div');
      ov.className = '_scan-overlay';
      ov.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;';
      ov.innerHTML = `
        <div style="width:260px;height:80px;border:2px solid #00e5ff;border-radius:4px;
          box-shadow:0 0 0 9999px rgba(0,0,0,.45);position:relative;">
          <span style="position:absolute;bottom:-20px;left:50%;transform:translateX(-50%);
            color:#00e5ff;font-size:11px;font-weight:600;white-space:nowrap;
            text-shadow:0 1px 4px rgba(0,0,0,.9);">Centra el barcode aquí</span>
        </div>`;
      target.appendChild(ov);

      resolve();
    });
  });
}

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

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
}

async function procesarScan(codigo) {
  if (_TIENDA_OC_RECEPCION) { await tiendaOCProcesarScan(codigo); return; }
  if (DEVOLUCION_ACTUAL) { await procesarScanDevolucion(codigo); return; }
  if (RECEPCION_ACTUAL) { await procesarScanRecepcion(codigo); return; }
  // HUD del empacador activo
  if (EMP_TAREA && document.getElementById('emp-hud')?.classList.contains('activo')) {
    await empProcesarEscaneo(codigo); return;
  }
  if (!TAREA_ACTUAL) return;
  vibrar(); flash();

  // ── Picking: resolver empaque antes de registrar escaneo ─────────────────
  if (TAREA_ACTUAL.tipo === 'PICKING') {
    await _procesarScanPicking(codigo);
    return;
  }

  // Otros tipos (CONTEO, PACKING) — flujo original
  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: TAREA_ACTUAL.id,
      tipo: TAREA_ACTUAL.tipo,
      codigo,
      cantidad: 1
    });
    if (r.error) { beepError(); alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    beepOk();
    _actualizarContadorPicking(r);
  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

async function _procesarScanPicking(codigo) {
  // 1. Preguntar al sistema qué es este código
  let scan;
  try {
    scan = await get(`/api/empaques/scan/${encodeURIComponent(codigo)}`);
  } catch (_) {
    scan = { tipo: 'NO_ENCONTRADO' };
  }

  const tipo = scan.tipo || 'NO_ENCONTRADO';

  if (tipo === 'GS1_AMBIGUO') {
    _modalAmbiguedadPicking(codigo, scan.ambiguos || []);
    return;
  }

  // Resolver código de producto y cantidad según tipo de código escaneado.
  // El backend PICKING valida por producto.codigo / codigo_siesa / codigo_barras —
  // nunca acepta un DUN-14 o código LPN directamente.
  let codigoParaBackend = codigo;  // default: código de producto base (EAN-13 en productos.codigo_barras)
  let cantidad = 1;
  let etiqueta = '';

  if ((tipo === 'GS1_UNICO' || tipo === 'EAN_BASE') && scan.producto?.codigo) {
    codigoParaBackend = scan.producto.codigo;
    cantidad = scan.factor || 1;
    if (cantidad > 1) {
      const unidad = scan.empaque?.unidad_medida || 'EMPAQUE';
      etiqueta = ` (+${cantidad} und — ${unidad})`;
    }
  } else if (tipo === 'LPN' && scan.producto?.codigo) {
    codigoParaBackend = scan.producto.codigo;
    cantidad = scan.factor || 1;  // scan.factor = lpn.cantidad_actual
    etiqueta = ` (LPN: ${cantidad} und)`;
  }
  // NO_ENCONTRADO → enviar codigo original, el backend dará error descriptivo

  // Incluir lpn_codigo cuando es un LPN — el backend lo vincula al traslado si aplica
  // GS1/EAN/LPN: el frontend ya conoce las unidades reales → usar modo idempotente.
  // NO_ENCONTRADO (barcode empaque directo): el frontend no conoce el factor → el backend
  // aplica factor_conversion con +=; no enviar total_acumulado en ese caso.
  const _scanResuelto = tipo !== 'NO_ENCONTRADO';
  if (_scanResuelto) _pickingTotal += cantidad;
  const payload = { tarea_id: TAREA_ACTUAL.id, tipo: 'PICKING', codigo: codigoParaBackend, cantidad };
  if (tipo === 'LPN') payload.lpn_codigo = codigo;  // 'LPN-XXXXXXX' original
  if (_scanResuelto) payload.total_acumulado = _pickingTotal;

  try {
    const r = await post('/api/mobile/escanear', payload);
    if (r.error) { beepError(); if (_scanResuelto) _pickingTotal -= cantidad; alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    beepOk();
    _pickingTotal = r.cantidad_actual;  // siempre sincronizar con verdad del servidor
    if (etiqueta) alerta(`Registrado${etiqueta}`, 'exito');
    _actualizarContadorPicking(r);
  } catch (e) { beepError(); if (_scanResuelto) _pickingTotal -= cantidad; alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

function _actualizarContadorPicking(r) {
  if (TAREA_ACTUAL) TAREA_ACTUAL.cantidad_escaneada = r.cantidad_actual || 0;
  const pkgEl = document.getElementById('contador-pkg');
  const undEl = document.getElementById('contador-und');

  if (pkgEl && undEl) {
    const factor   = TAREA_ACTUAL.factor_conversion || 1;
    const unidad   = (TAREA_ACTUAL.unidad_empaque || 'PKG').toUpperCase();
    const pkgs     = r.empaques_escaneados || 0;
    const unds     = r.cantidad_actual || 0;
    const req      = r.cantidad_requerida || 0;
    const pkgsReq  = factor > 1 ? Math.ceil(req / factor) : req;
    const sueltas  = factor > 1 ? unds % factor : 0;
    pkgEl.textContent = pkgs;
    undEl.textContent = `de ${pkgsReq} ${unidad}${sueltas > 0 ? ` +${sueltas} und` : ''}`;
    const factorEl = document.getElementById('contador-factor');
    if (factorEl) factorEl.textContent = `${unds}/${req} und totales`;
    if (r.puede_confirmar) { pkgEl.style.color = '#4ade80'; undEl.style.color = '#4ade80'; }
  } else {
    // Vista simple (unidades sueltas o conteo)
    const contador = document.getElementById('contador');
    if (contador) {
      contador.textContent = TAREA_ACTUAL.tipo === 'CONTEO'
        ? r.cantidad_contada
        : `${r.cantidad_actual}/${r.cantidad_requerida}`;
    }
  }

  const barra = document.getElementById('barra');
  if (barra && r.cantidad_requerida) {
    barra.style.width = Math.min((r.cantidad_actual / r.cantidad_requerida) * 100, 100) + '%';
  }

  if (r.puede_confirmar) {
    const btn = document.getElementById('btn-ok');
    if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.style.background = '#16a34a'; }
    alerta(r.mensaje || '¡Listo!', 'exito');
  }
}

function _modalAmbiguedadPicking(codigo, empaques) {
  // empaques: array de ProductoEmpaque.to_dict() — incluye producto_codigo
  const opciones = empaques.map(e => `
    <button onclick="_elegirEmpaquePicking('${e.producto_codigo || e.referencia_item}', ${e.factor_conversion}, '${e.unidad_medida}', this.closest('.modal-ambig'))"
      style="width:100%;padding:16px;font-size:18px;font-weight:700;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:12px;cursor:pointer;margin-bottom:8px;">
      ${e.unidad_medida} — ${e.factor_conversion} und
      <div style="font-size:12px;color:#666;font-weight:400;margin-top:2px;">${e.producto_nombre || ''}</div>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-ambig';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.85);display:flex;align-items:flex-end;';
  modal.innerHTML = `
    <div style="background:#0a0a0a;border-top:2px solid #1d4ed8;border-radius:20px 20px 0 0;padding:24px;width:100%;max-height:70vh;overflow-y:auto;">
      <div style="font-size:16px;font-weight:700;color:#60a5fa;margin-bottom:4px;">Código en múltiples empaques</div>
      <div style="font-size:13px;color:#666;margin-bottom:16px;">${codigo} — ¿Cuál estás recogiendo?</div>
      ${opciones}
      <button onclick="this.closest('.modal-ambig').remove()"
        style="width:100%;padding:12px;font-size:14px;background:#111;color:#666;border:1px solid #222;border-radius:10px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

async function _elegirEmpaquePicking(productoCodigo, factor, unidad, modal) {
  // productoCodigo ya es el código del producto (no el DUN-14) — el backend lo acepta
  if (modal) modal.remove();
  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: TAREA_ACTUAL.id,
      tipo: 'PICKING',
      codigo: productoCodigo,
      cantidad: factor
    });
    if (r.error) { beepError(); alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    beepOk();
    alerta(`+${factor} und — ${unidad}`, 'exito');
    _actualizarContadorPicking(r);
  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

async function confirmar() {
  if (!TAREA_ACTUAL) return;
  const btn = document.getElementById('btn-ok');
  if (btn) { btn.textContent = 'Confirmando...'; btn.disabled = true; }
  const payload = { tarea_id: TAREA_ACTUAL.id, tipo: TAREA_ACTUAL.tipo, items_escaneados: [] };
  try {
    const r = await post('/api/mobile/confirmar', payload);
    if (r.error) {
      alerta(r.error, 'error');
      if (btn) { btn.textContent = '✓ Confirmar'; btn.disabled = false; }
      return;
    }
    beepDone();
    TAREA_ACTUAL = null;
    // Picking con packing asociado → mostrar botón etiqueta canasto
    if (r.canasto_data) {
      _modalEtiquetaCanasto(r.canasto_data);
      return;
    }
    // Conteos: mostrar resultado MATCH vs SEGUNDO_CONTEO antes de pedir siguiente tarea
    if (r.resultado === 'MATCH' || r.resultado === 'SEGUNDO_CONTEO') {
      const esMatch = r.resultado === 'MATCH';
      const overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:16px;';
      overlay.style.background = esMatch ? '#052e16' : '#1c1400';
      overlay.innerHTML = `
        <div style="font-size:80px;">${esMatch ? '✅' : '⚠️'}</div>
        <div style="font-size:28px;font-weight:900;color:${esMatch ? '#4ade80' : '#fbbf24'};text-align:center;padding:0 20px;">
          ${esMatch ? 'Inventario correcto' : 'Diferencia detectada'}
        </div>
        <div style="font-size:15px;color:${esMatch ? '#166534' : '#92400e'};text-align:center;padding:0 30px;line-height:1.5;">
          ${esMatch ? 'El conteo cuadra con el sistema.' : 'Se asignó un segundo conteo\npara verificación.'}
        </div>`;
      document.body.appendChild(overlay);
      setTimeout(() => { overlay.remove(); pedirTarea(); }, esMatch ? 2000 : 3000);
    } else {
      alerta('¡Tarea completada!', 'exito');
      setTimeout(pedirTarea, 1500);
    }
  } catch (e) {
    if (e.status) {
      // Error del servidor (400/500) — mostrar mensaje real, no guardar offline
      alerta(e.message || 'Error al confirmar', 'error');
      if (btn) { btn.textContent = '✓ Confirmar'; btn.disabled = false; }
    } else {
      // Error de red real — guardar para sincronizar cuando haya WiFi
      guardarOffline(payload);
      TAREA_ACTUAL = null;
      setTimeout(pedirTarea, 2000);
    }
  }
}

function _modalEtiquetaCanasto(canasto) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.93);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
  overlay.innerHTML = `
    <div style="background:#111;border-radius:16px;padding:28px 24px;width:100%;max-width:360px;border:2px solid #16a34a;text-align:center;">
      <div style="font-size:56px;margin-bottom:8px;">✅</div>
      <div style="font-size:22px;font-weight:900;color:#4ade80;margin-bottom:8px;">Picking completado</div>
      <div style="font-size:14px;color:#aaa;margin-bottom:20px;line-height:1.6;">
        ¿Cuántos canastos usaste para este pedido?
      </div>
      <div style="display:flex;align-items:center;justify-content:center;gap:16px;margin-bottom:24px;">
        <button id="_ecan-menos" style="width:48px;height:48px;background:#1a1a1a;color:#fff;border:2px solid #333;border-radius:12px;font-size:24px;font-weight:900;cursor:pointer;">−</button>
        <span id="_ecan-num" style="font-size:40px;font-weight:900;color:#fff;min-width:48px;">1</span>
        <button id="_ecan-mas" style="width:48px;height:48px;background:#1a1a1a;color:#fff;border:2px solid #333;border-radius:12px;font-size:24px;font-weight:900;cursor:pointer;">+</button>
      </div>
      <button id="_ecan-print" style="width:100%;padding:16px;background:#16a34a;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:800;cursor:pointer;margin-bottom:12px;">
        🖨 Imprimir etiquetas
      </button>
      <button id="_ecan-skip" style="width:100%;padding:14px;background:#1a1a1a;color:#666;border:1px solid #333;border-radius:12px;font-size:14px;font-weight:600;cursor:pointer;">
        Continuar sin imprimir
      </button>
    </div>`;
  document.body.appendChild(overlay);
  let copias = 1;
  const numEl = overlay.querySelector('#_ecan-num');
  overlay.querySelector('#_ecan-menos').onclick = () => { if (copias > 1) numEl.textContent = --copias; };
  overlay.querySelector('#_ecan-mas').onclick  = () => { if (copias < 10) numEl.textContent = ++copias; };
  overlay.querySelector('#_ecan-print').onclick = () => {
    const hoy = new Date().toLocaleString('es-CO');
    const bloque = `
      <div class="etiqueta-canasto">
        <div class="ec-titulo">CANASTO — PICKING PAME</div>
        <div class="ec-pedido">${canasto.pedido}</div>
        <div class="ec-cliente">${canasto.cliente || '—'}</div>
        <div class="ec-items">
          <div class="ec-item"><span class="ec-ref">${canasto.ref}</span><span class="ec-cant">${canasto.cantidad} uds</span></div>
        </div>
        <div class="ec-footer">Operario: ${canasto.operario} · ${hoy}</div>
      </div>`;
    const area = document.getElementById('print-area');
    area.innerHTML = bloque.repeat(copias);
    window.print();
    setTimeout(() => { area.innerHTML = ''; }, 1200);
    overlay.remove();
    setTimeout(pedirTarea, 800);
  };
  overlay.querySelector('#_ecan-skip').onclick = () => {
    overlay.remove();
    pedirTarea();
  };
}

async function confirmarConGuard() {
  if (!TAREA_ACTUAL) return;
  const unds    = TAREA_ACTUAL.cantidad_escaneada || 0;
  const req     = TAREA_ACTUAL.cantidad_requerida  || 0;
  const tareaId = TAREA_ACTUAL.id;

  if (req === 0 || unds >= req) {
    await confirmar();
    return;
  }

  const ok = await _modalFaltanteParcial(unds, req);
  if (!ok) return;

  await confirmar();
  _reportarFaltanteInfo(tareaId, unds, req).catch(() => {});
}

function _modalFaltanteParcial(encontradas, requeridas) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:#111;border-radius:16px;padding:24px;width:100%;max-width:360px;border:2px solid #b45309;">
        <div style="font-size:20px;font-weight:800;color:#fb923c;margin-bottom:8px;">⚠ Faltante parcial</div>
        <div style="font-size:14px;color:#aaa;margin-bottom:20px;line-height:1.6;">
          Encontraste <strong style="color:#fb923c;font-size:18px;font-weight:800;">${encontradas}</strong> de
          <strong style="color:#fb923c;font-size:18px;font-weight:800;">${requeridas}</strong> unidades.<br>
          <span style="font-size:12px;color:#666;">Se notificará al administrador para revisar el faltante.</span>
        </div>
        <div style="display:flex;gap:10px;">
          <button id="_fp-no" style="flex:1;padding:14px;background:#1a1a1a;color:#aaa;border:1px solid #333;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">Cancelar</button>
          <button id="_fp-si" style="flex:1;padding:14px;background:#b45309;color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">Continuar</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#_fp-si').onclick = () => { overlay.remove(); resolve(true); };
    overlay.querySelector('#_fp-no').onclick = () => { overlay.remove(); resolve(false); };
  });
}

async function _reportarFaltanteInfo(tareaId, cantRecogida, cantSolicitada) {
  await post('/api/mobile/faltante-info', {
    tarea_id: tareaId,
    cantidad_recogida: cantRecogida,
    cantidad_solicitada: cantSolicitada,
  });
}

async function confirmarManual(tareaId, cantMax) {
  const cantStr = prompt(`¿Cuántas unidades encontraste físicamente? (máx. ${cantMax})`);
  if (cantStr === null) return;
  const cant = parseInt(cantStr, 10);
  if (isNaN(cant) || cant <= 0 || cant > cantMax) {
    alerta(`Cantidad inválida — debe ser entre 1 y ${cantMax}. Si no hay stock usa "Reportar problema".`, 'error');
    return;
  }
  if (!confirm(`¿Confirmar ${cant} unidades recogidas manualmente?`)) return;
  const payload = {
    tarea_id: tareaId,
    tipo: TAREA_ACTUAL?.tipo,
    items_escaneados: [],
    cantidad_manual: cant
  };
  try {
    const r = await post('/api/mobile/confirmar', payload);
    if (r.error) { alerta(r.error, 'error'); return; }
    alerta('¡Tarea completada!', 'exito');
    TAREA_ACTUAL = null;
    setTimeout(pedirTarea, 1500);
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function reportarProblema(tareaId) {
  const modal = document.createElement('div');
  modal.id = 'modal-problema';
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div style="background:#111;border-radius:16px;padding:24px;width:100%;max-width:380px;border:1px solid #7f1d1d;">
        <div style="font-size:18px;font-weight:700;margin-bottom:4px;color:#f87171;">⚠ Reportar problema</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">La tarea se bloquea. El jefe auditará y ajustará el inventario.</div>

        <button onclick="confirmarProblema(${tareaId},'UBICACION_VACIA',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          📦 Ubicación vacía — no había nada
        </button>

        <button onclick="confirmarProblema(${tareaId},'MERCANCIA_AVERIADA',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          🚫 Mercancía averiada
        </button>

        <button onclick="confirmarProblema(${tareaId},'PRODUCTO_INCORRECTO',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          ❌ Producto incorrecto
        </button>

        <div style="margin-top:4px;margin-bottom:8px;">
          <div style="font-size:11px;color:#555;margin-bottom:4px;">Observaciones (opcional)</div>
          <textarea id="obs-problema" rows="2" placeholder="Describe lo que encontraste..."
            style="width:100%;padding:10px;background:#000;border:1px solid #333;border-radius:8px;color:#ccc;font-size:14px;resize:none;box-sizing:border-box;"></textarea>
        </div>

        <button onclick="document.getElementById('modal-problema').remove()"
          style="width:100%;padding:12px;font-size:14px;background:#222;color:#666;border:none;border-radius:10px;cursor:pointer;margin-top:4px;">
          Cancelar
        </button>
      </div>
    </div>`;
  document.body.appendChild(modal);
}

async function confirmarProblema(tareaId, motivo, cantidadEncontrada) {
  const observaciones = document.getElementById('obs-problema')?.value?.trim() || '';
  const modal = document.getElementById('modal-problema');
  if (modal) modal.remove();
  const tipo = TAREA_ACTUAL?.tipo || 'PICKING';
  try {
    await post('/api/mobile/reportar-problema', {
      tarea_id: tareaId,
      tipo,
      motivo,
      cantidad_encontrada: cantidadEncontrada || 0,
      observaciones: observaciones || undefined,
    });
    const msg = cantidadEncontrada > 0
      ? `Short-pick: ${cantidadEncontrada} unidades registradas. Auditoría creada.`
      : 'Problema reportado — auditoría urgente creada para el jefe';
    alerta(msg, 'advertencia');
    TAREA_ACTUAL = null;
    setTimeout(pedirTarea, 1500);
  } catch (e) {
    alerta(e.message || 'Error reportando problema', 'error');
  }
}

// ── Auditorías Urgentes (admin) ──────────────────────

async function cargarAuditoriasUrgentes() {
  const el = document.getElementById('lista-auditorias-urgentes');
  if (!el) return;
  try {
    const d = await get('/api/conteo/auditorias-urgentes?almacen_id=' + ALMACEN_ID);
    const auds = d.auditorias || [];
    if (!auds.length) {
      el.innerHTML = '<div style="color:#4ade80;text-align:center;padding:20px;font-size:13px;">✓ Sin auditorías pendientes</div>';
      return;
    }
    el.innerHTML = auds.map(a => {
      const diff = a.diferencia;
      const diffColor = diff === null ? '#666' : diff < 0 ? '#f87171' : '#fb923c';
      const diffTxt  = diff === null ? 'Pendiente de conteo' : `Diferencia: ${diff > 0 ? '+' : ''}${diff} uds`;
      const puedAprobar = ['SEGUNDO_CONTEO','DESCUADRE'].includes(a.estado) && diff !== null;
      return `
        <div style="background:#111;border:1px solid #7f1d1d;border-radius:12px;padding:14px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
            <div>
              <div style="font-size:13px;font-weight:700;color:#f87171;">${a.codigo}</div>
              <div style="font-size:11px;color:#555;margin-top:2px;">${a.producto_nombre || ''} · ${a.ubicacion_codigo || ''}</div>
              ${a.tarea_picking_id ? `<div style="font-size:10px;color:#444;margin-top:1px;">Originó: tarea picking #${a.tarea_picking_id}</div>` : ''}
              ${a.motivo_codigo && !['AJ-ENT','AJ-SAL'].includes(a.motivo_codigo) ? `<div style="font-size:10px;color:#b45309;margin-top:1px;">Motivo: ${({'UBICACION_VACIA':'📦 Ubicación vacía','FALTANTE':'📉 Faltante parcial','MERCANCIA_AVERIADA':'🚫 Mercancía averiada','PRODUCTO_INCORRECTO':'❌ Producto incorrecto'})[a.motivo_codigo] || a.motivo_codigo}</div>` : ''}
            </div>
            <span style="background:#3f1515;color:#f87171;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;">${a.estado}</span>
          </div>
          <div style="font-size:12px;color:${diffColor};margin-bottom:8px;">${diffTxt}</div>
          ${puedAprobar ? `
            <div style="background:#0a0a0a;border-radius:8px;padding:10px;margin-bottom:8px;font-size:12px;color:#aaa;">
              ${diff < 0 ? `<b style="color:#f87171;">AJ-SAL</b> — faltan ${Math.abs(diff)} uds en Siesa` : `<b style="color:#4ade80;">AJ-ENT</b> — sobran ${diff} uds en Siesa`}
            </div>
            <button onclick="aprobarAjusteConteo(${a.id})"
              style="width:100%;padding:11px;background:#7f1d1d;color:#fca5a5;border:1px solid #f87171;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">
              ✓ Aprobar ajuste → Siesa
            </button>` : `
            <div style="font-size:11px;color:#444;">Esperando que un operario complete el conteo físico</div>`}
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando auditorías</div>';
  }
}

async function aprobarAjusteConteo(sesionId) {
  if (!confirm('¿Confirmar envío del ajuste a Siesa? Esta acción es contable e irreversible.')) return;
  try {
    const r = await fetch(API + '/api/conteo/' + sesionId + '/ajustar', {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`Ajuste ${d.motivo_codigo} enviado a Siesa ✓`, 'exito');
      cargarAuditoriasUrgentes();
      cargarDashboard();
    } else {
      alert(d.error || 'Error al enviar ajuste');
    }
  } catch (e) { alert('Error de conexión'); }
}

async function cargarRecepciones(silencioso = false) {
  if (RECEPCION_ACTUAL) return;
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  // Solo muestra spinner en carga inicial, no en polling automático
  if (!silencioso) {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#666;">Cargando...</div>';
  }
  try {
    const [siesa, db] = await Promise.all([
      get('/api/siesa/ordenes-compra').catch(() => ({ ordenes: [] })),
      get('/api/recepcion/?estado=EN_PROCESO').catch(() => ({ recepciones: [] }))
    ]);
    SIESA_OCS = siesa.ordenes || [];
    // Guard post-await: el operario pudo haber entrado a escaneo mientras las APIs respondían
    if (RECEPCION_ACTUAL) return;
    renderListaRecepciones(siesa, db.recepciones || []);
  } catch (e) {
    if (!silencioso) el.innerHTML = '<div style="color:#ef4444;">Error cargando</div>';
  }
}

function pantalla(id) {
  ['pantalla-login','pantalla-operario','pantalla-admin','pantalla-recepcion',
   'pantalla-empacador','pantalla-conductor','pantalla-tienda','pantalla-abastecedor',
   'pantalla-picker-traslado','pantalla-packer-traslado','pantalla-compras'].forEach(p => {
    const el = document.getElementById(p);
    if (el) el.style.display = p === id ? 'block' : 'none';
  });
}

function set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

function alerta(msg, tipo = 'info') {
  const c = { exito: '#16a34a', error: '#dc2626', advertencia: '#d97706', info: '#2563eb' }[tipo] || '#2563eb';
  const d = document.createElement('div');
  d.style.cssText = `position:fixed;top:20px;left:50%;transform:translateX(-50%);background:${c};color:#fff;padding:14px 22px;border-radius:12px;font-size:17px;font-weight:600;z-index:9999;max-width:90%;text-align:center;`;
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 2500);
}

function flash() {
  const d = document.createElement('div');
  d.style.cssText = 'position:fixed;inset:0;background:rgba(255,255,255,0.25);z-index:9998;pointer-events:none;';
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 120);
}

function vibrar() { if (navigator.vibrate) navigator.vibrate(40); }

// ── Feedback auditivo (Web Audio API — sin dependencias) ─────
let _audioCtx = null;
function _getAudioCtx() {
  if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return _audioCtx;
}
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
function beepOk()    { _tono(880, 0.12); }                                   // agudo corto — scan OK
function beepError() { _tono(220, 0.18, 'square', 0.3); setTimeout(() => _tono(180, 0.18, 'square', 0.3), 200); } // grave doble — error
function beepDone()  { _tono(523, 0.1); setTimeout(() => _tono(659, 0.1), 120); setTimeout(() => _tono(784, 0.25), 240); } // fanfarria — tarea completa

// ─────────────────────────────────────────────────────────────
// ADMIN — Factura de despacho (pedidos ya confirmados en Siesa)
// ─────────────────────────────────────────────────────────────

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
    alerta(`Despacho iniciado — Packing ${r.packing_codigo}`, 'exito');
    setTimeout(cargarPedidos, 800);
  } catch (e) { alerta('Error iniciando despacho', 'error'); }
}

// confirmarDespachoSiesa eliminado — el único gatillo hacia Siesa
// es el empacador físico al declarar bultos (POST /packing/<id>/cerrar)

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Lista de OCs y recepciones en proceso
// ─────────────────────────────────────────────────────────────

function renderListaRecepciones(siesa, dbRecs) {
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  let html = '';

  // Sección 1: OCs de Siesa
  if (siesa.simulado) {
    html += `<div style="background:#1a1a00;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#facc15;border:1px solid #333300;">
      ⚡ Connekta en simulación — conecta credenciales para ver OCs reales de Siesa
    </div>`;
  } else if (SIESA_OCS.length) {
    html += `<div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 6px;border-bottom:1px solid #222;margin-bottom:8px;">OCs PENDIENTES EN SIESA</div>`;
    html += SIESA_OCS.map((oc, i) => {
      const sinProd = oc.items.filter(it => !it.producto_id).length;
      const totalUds = oc.items.reduce((s, it) => s + (it.cantidad_pendiente || 0), 0);
      const wmsEstado = oc.recepcion_wms_estado;

      if (wmsEstado === 'CONFIRMADA') {
        return `
          <div class="rec-card" style="opacity:0.6;">
            <div class="rec-titulo">OC: ${oc.numero_oc}</div>
            <div class="rec-sub">${oc.proveedor || 'Sin proveedor'} · ${oc.items.length} productos · ${totalUds} uds</div>
            <div style="margin-top:10px;padding:10px;background:#0d1a0d;border-radius:8px;font-size:14px;font-weight:700;color:#4ade80;text-align:center;">
              ✓ Recepcionada en WMS — pendiente actualización en Siesa
            </div>
          </div>`;
      }

      if (wmsEstado === 'EN_PROCESO') {
        return `
          <div class="rec-card">
            <div class="rec-titulo">OC: ${oc.numero_oc}</div>
            <div class="rec-sub">${oc.proveedor || 'Sin proveedor'} · ${oc.items.length} productos · ${totalUds} uds</div>
            <button onclick="crearRecepcionDesdeSiesa(${i})"
              style="width:100%;margin-top:12px;padding:14px;font-size:17px;font-weight:700;background:#1d4ed8;color:#fff;border:none;border-radius:10px;cursor:pointer;">
              Continuar recepción
            </button>
          </div>`;
      }

      return `
        <div class="rec-card">
          <div class="rec-titulo">OC: ${oc.numero_oc}</div>
          <div class="rec-sub">${oc.proveedor || 'Sin proveedor'} · ${oc.items.length} productos · ${totalUds} uds</div>
          ${sinProd ? `<div style="font-size:11px;color:#d97706;margin-top:4px;">⚠ ${sinProd} producto(s) no registrado(s) en WMS</div>` : ''}
          <button onclick="crearRecepcionDesdeSiesa(${i})"
            style="width:100%;margin-top:12px;padding:14px;font-size:17px;font-weight:700;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">
            Iniciar recepción
          </button>
        </div>`;
    }).join('');
  } else {
    html += `<div style="background:#0d1a0d;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#4ade80;border:1px solid #1a2a1a;">✓ Sin OCs pendientes en Siesa</div>`;
  }

  // Sección 2: Recepciones en proceso (desde DB)
  if (dbRecs.length) {
    html += `<div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 6px;border-bottom:1px solid #222;margin:10px 0 8px;">EN PROCESO</div>`;
    html += dbRecs.map(r => `
      <div class="rec-card">
        <div class="rec-titulo">OC: ${r.numero_oc_siesa}</div>
        <div class="rec-sub">${r.proveedor_nombre || 'Sin proveedor'}</div>
        <div style="margin-top:6px;font-size:13px;color:#666;">${r.items_escaneados} / ${r.total_items} ítems escaneados</div>
        <button onclick="continuarRecepcion(${r.id})"
          style="width:100%;margin-top:10px;padding:13px;font-size:16px;font-weight:700;background:#1d4ed8;color:#fff;border:none;border-radius:10px;cursor:pointer;">
          Continuar escaneo
        </button>
      </div>`).join('');
  }

  if (!html) {
    html = `<div style="text-align:center;padding:50px 20px;">
      <div style="font-size:50px;">✓</div>
      <div style="font-size:22px;font-weight:700;margin-top:12px;">Sin recepciones</div>
      <button onclick="_refreshBtn(event, cargarRecepciones)" style="margin-top:20px;padding:12px 24px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">Actualizar</button>
    </div>`;
  }

  el.innerHTML = html;
}

async function crearRecepcionDesdeSiesa(idx) {
  const oc = SIESA_OCS[idx];
  if (!oc) return;
  const itemsValidos = oc.items.filter(it => it.producto_id);
  if (!itemsValidos.length) { alerta('Ningún producto de la OC está en el WMS', 'error'); return; }

  const el = document.getElementById('contenido-recepcion');
  if (el) el.innerHTML = '<div style="text-align:center;padding:60px;color:#666;">Iniciando recepción...</div>';

  try {
    const r = await post('/api/siesa/iniciar-recepcion', {
      numero_oc: oc.numero_oc,
      tipo_docto: oc.tipo_docto,
      consec_docto: oc.consec_docto,
      co: oc.co,
      proveedor: oc.proveedor,
      proveedor_codigo: oc.proveedor_codigo || '',
      sucursal_prov: oc.sucursal_prov || '',
      cond_pago: oc.cond_pago || '',
      almacen_id: ALMACEN_ID,
      items: itemsValidos
    });
    if (r.error) { alerta(r.error, 'error'); cargarRecepciones(); return; }
    if (r.advertencias?.length) r.advertencias.forEach(a => alerta(a, 'advertencia'));
    RECEPCION_ACTUAL = r.recepcion;
    renderEscaneoRecepcion(r.recepcion);
  } catch (e) { alerta(e.message || 'Error iniciando recepción', 'error'); cargarRecepciones(); }
}

async function continuarRecepcion(id) {
  try {
    const r = await get('/api/recepcion/' + id);
    RECEPCION_ACTUAL = r;
    renderEscaneoRecepcion(r);
  } catch (e) { alerta('Error cargando recepción', 'error'); }
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Pantalla de escaneo ciego
// ─────────────────────────────────────────────────────────────

function renderEscaneoRecepcion(rec) {
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  const todoCompleto = rec.items.every(it => it.cantidad_recibida >= it.cantidad_ordenada);
  const hayAlgoEscaneado = rec.items.some(it => it.cantidad_recibida > 0);
  const btnActivo = true;
  const btnTexto = todoCompleto ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial';
  const btnColor = todoCompleto ? '#16a34a' : '#b45309';

  el.innerHTML = `
    <div style="padding:16px;">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <button onclick="volverListaRecepciones()"
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
        <button onclick="abrirCamara('lector-qr-rec','camara-box-rec', cod => { cerrarCamara('camara-box-rec'); procesarScanRecepcion(cod); })"
          style="width:100%;padding:13px;font-size:16px;background:#fff;color:#000;border:2px solid #000;border-radius:10px;cursor:pointer;margin-bottom:8px;">
          📷 Escanear con cámara
        </button>
        <div id="camara-box-rec" style="display:none;margin-bottom:8px;">
          <div id="lector-qr-rec" style="border-radius:10px;overflow:hidden;"></div>
          <button onclick="cerrarCamara('camara-box-rec')" style="width:100%;padding:9px;margin-top:6px;font-size:14px;background:#333;color:#fff;border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
        </div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <input id="rec-codigo-manual" type="text" placeholder="O escribe / pega el código aquí"
            style="flex:1;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ procesarScanRecepcion(v); this.value=''; } }">
          <button onclick="const v=document.getElementById('rec-codigo-manual').value.trim();if(v){procesarScanRecepcion(v);document.getElementById('rec-codigo-manual').value='';}"
            style="padding:10px 14px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
        </div>
        <button onclick="abrirBusquedaManualRecepcion()"
          style="width:100%;padding:10px;font-size:14px;background:#1a1a1a;color:#9ca3af;border:1px solid #333;border-radius:8px;cursor:pointer;">
          📦 Sin código — buscar producto manualmente
        </button>
      </div>

      <div id="items-rec-list" style="margin-bottom:14px;">
        ${renderItemsRecepcion(rec.items)}
      </div>

      <button id="btn-confirmar-rec" onclick="confirmarRecepcionActiva()" ${btnActivo ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:20px;font-weight:700;background:${btnActivo ? btnColor : '#222'};color:#fff;border:none;border-radius:14px;cursor:${btnActivo ? 'pointer' : 'default'};margin-bottom:10px;">
        ${btnTexto}
      </button>

      <button onclick="modalObsequio()"
        style="width:100%;padding:13px;font-size:15px;font-weight:600;background:#1a1a2e;color:#a78bfa;border:1px solid #4c1d95;border-radius:10px;cursor:pointer;margin-bottom:8px;">
        🎁 Registrar Obsequio / Bonificación
      </button>

      <button onclick="volverListaRecepciones()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
        Guardar y salir (continuar más tarde)
      </button>
    </div>`;
}

function renderItemsRecepcion(items) {
  return items.map(it => {
    const esBono = it.tipo === 'BONIFICACION';

    if (esBono) {
      return `
        <div id="item-rec-${it.producto_id}"
          style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:12px;padding:14px;margin-bottom:8px;">
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
      <div id="item-rec-${it.producto_id}"
        style="background:${completo ? '#0d1a0d' : '#111'};border:1px solid ${completo ? '#166534' : '#222'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:14px;font-weight:600;color:${completo ? '#4ade80' : '#fff'};">${it.producto_nombre || it.producto_codigo}</div>
            <div style="font-size:11px;color:#555;">${it.producto_codigo}</div>
            ${it.destino === 'CROSS_DOCK' ? '<div style="font-size:11px;color:#60a5fa;margin-top:4px;">↔ CROSS-DOCK</div>' : ''}
          </div>
          ${contadorDerecha}
        </div>
        <div style="height:5px;background:#222;border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#16a34a' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.3s;"></div>
        </div>
      </div>`;
  }).join('');
}

async function procesarScanRecepcion(codigo) {
  if (!RECEPCION_ACTUAL) return;
  vibrar(); flash();

  try {
    // 1. Resolver barcode contra producto_empaques (nuevo sistema)
    const scan = await get('/api/empaques/scan/' + encodeURIComponent(codigo) +
      '?almacen_id=' + (RECEPCION_ACTUAL.almacen_id || ''));

    if (scan.tipo === 'GS1_AMBIGUO') {
      // Mismo código en múltiples empaques → operario decide cuál es
      _modalAmbiguedadRecepcion(codigo, scan.ambiguos);
      return;
    }

    if (scan.tipo === 'NO_ENCONTRADO') {
      // No está en producto_empaques → intentar lookup clásico (codigo_barras en productos)
      const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
      if (prod.error || !prod.producto_id) {
        alerta('Código no reconocido: ' + codigo + ' — usa búsqueda manual', 'error');
        return;
      }
      const esEmp = prod.es_empaque || false;
      await _registrarEscaneoRecepcion(prod.producto_id, 1, esEmp, null);
      if (esEmp && prod.factor_conversion > 1) alerta(`Empaque escaneado → +${prod.factor_conversion} UND`, 'info');
      return;
    }

    // GS1_UNICO, EAN_BASE o LPN — producto y factor conocidos
    const productoId = scan.producto ? scan.producto.id : null;
    if (!productoId) { alerta('Producto no identificado', 'error'); return; }

    const factor = scan.factor || 1;
    const unidad = scan.empaque ? scan.empaque.unidad_medida : 'UND';

    if (scan.tipo === 'LPN') {
      // LPN ya registrado — registrar el contenido completo
      await _registrarEscaneoRecepcion(productoId, scan.lpn.cantidad_actual, false, unidad);
      alerta(`LPN ${codigo} → +${scan.lpn.cantidad_actual} UND`, 'exito');
      return;
    }

    // GS1_UNICO o EAN_BASE — escaneo caja a caja, factor ya calculado
    const esEmpaqueScan = factor > 1;
    const flash_msg = esEmpaqueScan
      ? `${unidad} escaneada → +${factor} UND`
      : null;

    // Enviamos cantidad=1 y es_empaque=true; el backend multiplica por factor_conversion
    await _registrarEscaneoRecepcion(productoId, 1, esEmpaqueScan, unidad);
    if (flash_msg) alerta(flash_msg, 'info');

  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

async function _registrarEscaneoRecepcion(productoId, cantidad, esEmpaque, unidad, esBonificacion = false) {
  let r;
  try {
    r = await post('/api/recepcion/' + RECEPCION_ACTUAL.id + '/escanear', {
      producto_id: productoId,
      cantidad: cantidad,
      es_empaque: esEmpaque,
      es_bonificacion: esBonificacion
    });
  } catch (e) {
    const body = e.body || {};
    if (e.status === 409 && body.tipo === 'PRODUCTO_NO_EN_OC' && !esBonificacion) {
      const ok = await _confirmarModal(
        '⚠ Producto fuera de OC',
        'Este producto no está en la orden de compra.<br><br>¿Es un <strong>obsequio o bonificación</strong> del proveedor?',
        'Sí, registrar como bonificación', 'No, cancelar'
      );
      if (ok) await _registrarEscaneoRecepcion(productoId, cantidad, esEmpaque, unidad, true);
      return;
    }
    beepError();
    alerta(e.message, 'error');
    return;
  }

  // Producto no está en la OC y no se indicó bonificación → ofrecer registrarlo como bono
  if (r.tipo === 'PRODUCTO_NO_EN_OC' && !esBonificacion) {
    const confirmar = await _confirmarModal(
      '⚠ Producto fuera de OC',
      'Este producto no está en la orden de compra.<br><br>¿Es un <strong>obsequio o bonificación</strong> del proveedor?',
      'Sí, registrar como bonificación',
      'No, cancelar'
    );
    if (confirmar) await _registrarEscaneoRecepcion(productoId, cantidad, esEmpaque, unidad, true);
    return;
  }

  if (r.error) {
    const msg = typeof r.error === 'object' ? r.error.mensaje : r.error;
    alerta(msg, 'error');
    return;
  }

  // Ítem nuevo (bonificación recién creada) → agregarlo al array local
  const idx = RECEPCION_ACTUAL.items.findIndex(it => it.producto_id === productoId);
  if (idx >= 0) {
    RECEPCION_ACTUAL.items[idx] = r.item;
  } else {
    RECEPCION_ACTUAL.items.push(r.item);
  }

  const lista = document.getElementById('items-rec-list');
  if (lista) lista.innerHTML = renderItemsRecepcion(RECEPCION_ACTUAL.items);

  if (r.alerta) {
    const tipo = r.alerta.includes('EXCESO') ? 'error' : r.alerta.includes('CROSS') ? 'advertencia' : 'info';
    alerta(r.alerta, tipo);
  }

  const itemsOC = RECEPCION_ACTUAL.items.filter(it => it.tipo !== 'BONIFICACION');
  const todoCompleto = itemsOC.every(it => it.cantidad_recibida >= it.cantidad_ordenada);
  const btn = document.getElementById('btn-confirmar-rec');
  if (btn && todoCompleto && itemsOC.length > 0) {
    btn.disabled = false;
    btn.style.background = '#16a34a';
    btn.style.cursor = 'pointer';
    alerta('Todo escaneado — confirma la recepción', 'exito');
  }
}

// Muestra aviso y abre flujo de escaneo para obsequios/bonificaciones
function modalObsequio() {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px;';
  overlay.innerHTML = `
    <div style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:16px;padding:28px;max-width:360px;width:100%;text-align:center;">
      <div style="font-size:40px;margin-bottom:12px;">🎁</div>
      <div style="font-size:18px;font-weight:800;color:#a78bfa;margin-bottom:10px;">¿Hay obsequios o bonificaciones?</div>
      <div style="font-size:14px;color:#9ca3af;margin-bottom:24px;">¿El proveedor envió productos adicionales que <strong style="color:#fff;">no están en la OC</strong>?</div>
      <button id="btn-bono-si" style="width:100%;padding:15px;font-size:16px;font-weight:700;background:#4c1d95;color:#fff;border:none;border-radius:10px;cursor:pointer;margin-bottom:10px;">
        Sí — escanear obsequio
      </button>
      <button id="btn-bono-no" style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
        No, cancelar
      </button>
    </div>`;
  document.body.appendChild(overlay);

  overlay.querySelector('#btn-bono-no').onclick = () => overlay.remove();
  overlay.querySelector('#btn-bono-si').onclick = () => {
    overlay.remove();
    _panelScanBonificacion();
  };
}

// Panel de escaneo exclusivo para bonificaciones
function _panelScanBonificacion() {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px;';
  overlay.innerHTML = `
    <div style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:16px;padding:24px;max-width:380px;width:100%;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div style="font-size:16px;font-weight:800;color:#a78bfa;">🎁 Escanear Obsequio / Bonificación</div>
        <button onclick="this.closest('div[style*=fixed]').remove()" style="background:none;border:none;color:#555;font-size:22px;cursor:pointer;">✕</button>
      </div>
      <div style="font-size:12px;color:#6b7280;margin-bottom:14px;">Escanea el producto que el proveedor envió de más — entrará a inventario a $0.</div>
      <div id="camara-box-bono" style="display:none;margin-bottom:8px;">
        <div id="lector-qr-bono" style="border-radius:10px;overflow:hidden;"></div>
        <button onclick="cerrarCamara('camara-box-bono')" style="width:100%;padding:9px;margin-top:6px;font-size:14px;background:#333;color:#fff;border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
      </div>
      <button onclick="abrirCamara('lector-qr-bono','camara-box-bono', cod => { cerrarCamara('camara-box-bono'); _escanearBono(cod, this.closest('div[style*=fixed]')); })"
        style="width:100%;padding:13px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">
        📷 Escanear con cámara
      </button>
      <div style="display:flex;gap:8px;margin-bottom:8px;">
        <input id="bono-codigo-manual" type="text" placeholder="O escribe / pega el código"
          style="flex:1;padding:10px;background:#0a0a0a;border:1px solid #4c1d95;border-radius:8px;color:#fff;font-size:14px;"
          onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ _escanearBono(v, this.closest('div[style*=fixed]')); this.value=''; } }">
        <button onclick="const v=document.getElementById('bono-codigo-manual').value.trim();if(v){_escanearBono(v,this.closest('div[style*=fixed]'));document.getElementById('bono-codigo-manual').value='';}"
          style="padding:10px 14px;background:#4c1d95;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
      </div>
      <button onclick="abrirBusquedaManualBono(this.closest('div[style*=fixed]'))"
        style="width:100%;padding:10px;font-size:14px;background:#1a1a1a;color:#9ca3af;border:1px solid #333;border-radius:8px;cursor:pointer;">
        📦 Sin código — buscar producto manualmente
      </button>
    </div>`;
  document.body.appendChild(overlay);
}

async function _escanearBono(codigo, panelEl) {
  vibrar(); flash();
  try {
    const scan = await get('/api/empaques/scan/' + encodeURIComponent(codigo) +
      '?almacen_id=' + (RECEPCION_ACTUAL.almacen_id || ''));
    let productoId, cantidad, esEmpaque;
    if (scan.tipo === 'NO_ENCONTRADO') {
      const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
      if (prod.error || !prod.producto_id) { alerta('Código no reconocido — usa búsqueda manual', 'error'); return; }
      productoId = prod.producto_id; cantidad = 1; esEmpaque = false;
    } else if (scan.tipo === 'GS1_AMBIGUO') {
      alerta('Código ambiguo — usa búsqueda manual', 'advertencia'); return;
    } else {
      productoId = scan.producto ? scan.producto.id : null;
      if (!productoId) { alerta('Producto no identificado', 'error'); return; }
      cantidad = 1; esEmpaque = (scan.factor || 1) > 1;
    }
    await _registrarEscaneoRecepcion(productoId, cantidad, esEmpaque, null, true);
    if (panelEl) panelEl.remove();
  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

async function abrirBusquedaManualBono(panelEl) {
  const codigo = prompt('Ingresa el código WMS del producto:');
  if (!codigo) return;
  const prod = await get('/api/productos/?search=' + encodeURIComponent(codigo));
  if (!prod || !prod.productos || prod.productos.length === 0) { alerta('Producto no encontrado', 'error'); return; }
  const p = prod.productos[0];
  await _registrarEscaneoRecepcion(p.id, 1, false, null, true);
  if (panelEl) panelEl.remove();
}

// Helper: modal de confirmación reutilizable — devuelve Promise<boolean>
function _confirmarModal(titulo, cuerpoHtml, txtSi, txtNo) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9500;display:flex;align-items:center;justify-content:center;padding:24px;';
    overlay.innerHTML = `
      <div style="background:#111;border:1px solid #333;border-radius:16px;padding:28px;max-width:340px;width:100%;text-align:center;">
        <div style="font-size:17px;font-weight:800;color:#fff;margin-bottom:12px;">${titulo}</div>
        <div style="font-size:14px;color:#9ca3af;margin-bottom:24px;">${cuerpoHtml}</div>
        <button id="_cm-si" style="width:100%;padding:14px;font-size:15px;font-weight:700;background:#4c1d95;color:#fff;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">${txtSi}</button>
        <button id="_cm-no" style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">${txtNo}</button>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#_cm-si').onclick = () => { overlay.remove(); resolve(true); };
    overlay.querySelector('#_cm-no').onclick = () => { overlay.remove(); resolve(false); };
  });
}

function _modalAmbiguedadRecepcion(codigo, ambiguos) {
  // El mismo código de barras corresponde a múltiples niveles de empaque
  // El operario debe decir qué está escaneando
  const opciones = ambiguos.map((e, i) => `
    <button onclick="_elegirEmpaque('${codigo}',${e.producto_id},${e.factor_conversion},'${e.unidad_medida}',this.closest('.modal-rec'))"
      style="width:100%;padding:14px;margin-bottom:8px;background:#1a1a1a;border:1px solid #333;
             color:#fff;border-radius:10px;cursor:pointer;font-size:15px;text-align:left;">
      <span style="font-size:22px;font-weight:900;">${e.factor_conversion}</span>
      <span style="color:#9ca3af;margin-left:6px;">${e.unidad_medida}</span>
      <span style="color:#6b7280;font-size:12px;margin-left:8px;">(×${e.factor_conversion} und)</span>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-rec';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;display:flex;align-items:flex-end;padding:16px;';
  modal.innerHTML = `
    <div style="background:#111;border-radius:16px;padding:20px;width:100%;max-width:480px;margin:auto;">
      <div style="font-size:16px;font-weight:700;margin-bottom:6px;">⚠️ Código ambiguo</div>
      <div style="font-size:13px;color:#9ca3af;margin-bottom:16px;">${codigo} — ¿Qué estás escaneando?</div>
      ${opciones}
      <button onclick="this.closest('.modal-rec').remove()"
        style="width:100%;padding:12px;background:#0d0d0d;color:#6b7280;border:1px solid #222;border-radius:8px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

async function _elegirEmpaque(codigo, productoId, factor, unidad, modal) {
  if (modal) modal.remove();
  await _registrarEscaneoRecepcion(productoId, factor, false, unidad);
  alerta(`${unidad} × ${factor} UND registrada`, 'exito');
}

async function abrirBusquedaManualRecepcion() {
  // Busca producto por texto para pacas sin ningún código
  const modal = document.createElement('div');
  modal.className = 'modal-rec';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;display:flex;align-items:flex-start;padding:16px;padding-top:60px;';
  modal.innerHTML = `
    <div style="background:#111;border-radius:16px;padding:20px;width:100%;max-width:480px;margin:auto;">
      <div style="font-size:16px;font-weight:700;margin-bottom:14px;">📦 Buscar producto manualmente</div>
      <input id="modal-buscar-input" type="text" placeholder="Nombre o código del producto..."
        style="width:100%;box-sizing:border-box;padding:12px;background:#0d0d0d;border:1px solid #444;border-radius:8px;color:#fff;font-size:15px;margin-bottom:10px;"
        oninput="_buscarProductoModal(this.value)">
      <div id="modal-buscar-resultados" style="max-height:280px;overflow-y:auto;"></div>
      <button onclick="this.closest('.modal-rec').remove()"
        style="width:100%;padding:12px;background:#0d0d0d;color:#6b7280;border:1px solid #222;border-radius:8px;cursor:pointer;margin-top:10px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
  setTimeout(() => { const i = document.getElementById('modal-buscar-input'); if(i) i.focus(); }, 100);
}

let _buscarModalTimer;
async function _buscarProductoModal(q) {
  clearTimeout(_buscarModalTimer);
  if (q.length < 2) { document.getElementById('modal-buscar-resultados').innerHTML = ''; return; }
  _buscarModalTimer = setTimeout(async () => {
    const res = await get('/api/productos/?q=' + encodeURIComponent(q) + '&limit=8').catch(() => ({ productos: [] }));
    const productos = res.productos || [];
    const el = document.getElementById('modal-buscar-resultados');
    if (!el) return;
    if (!productos.length) { el.innerHTML = '<div style="color:#6b7280;padding:10px;font-size:13px;">Sin resultados</div>'; return; }
    el.innerHTML = productos.map(p => `
      <button onclick="_seleccionarProductoManual(${p.id},'${(p.nombre||'').replace(/'/g,"\\'")}',this.closest('.modal-rec'))"
        style="width:100%;padding:12px;margin-bottom:6px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:8px;cursor:pointer;text-align:left;">
        <div style="font-size:14px;font-weight:600;">${p.nombre}</div>
        <div style="font-size:11px;color:#6b7280;">${p.codigo}</div>
      </button>`).join('');
  }, 350);
}

async function _seleccionarProductoManual(productoId, nombre, modal) {
  // Producto seleccionado sin código → preguntar cantidad y generar LPN
  const cant = prompt(`¿Cuántas unidades tiene esta paca de "${nombre}"?\n(Deja vacío si es 1 unidad suelta)`);
  if (cant === null) return; // canceló
  const cantidad = parseInt(cant) || 1;

  if (modal) modal.remove();

  if (cantidad > 1) {
    // Es una paca → generar LPN
    try {
      const lpnRes = await post('/api/empaques/lpn/generar', {
        producto_id: productoId,
        cantidad_actual: cantidad,
        almacen_id: RECEPCION_ACTUAL.almacen_id,
        recepcion_id: RECEPCION_ACTUAL.id,
        notas: 'Generado en recepción manual'
      });
      if (lpnRes.error) { alerta(lpnRes.error, 'error'); return; }
      alerta(`LPN ${lpnRes.lpn.codigo} generado — imprimiendo etiqueta...`, 'exito');
      imprimirEtiquetaLPN(lpnRes.lpn, nombre);
      await _registrarEscaneoRecepcion(productoId, cantidad, false, 'PACA');
    } catch(e) { alerta('Error generando LPN', 'error'); }
  } else {
    // Unidad suelta
    await _registrarEscaneoRecepcion(productoId, 1, false, 'UND');
  }
}

async function confirmarRecepcionActiva() {
  if (!RECEPCION_ACTUAL) return;

  const todoCompleto = RECEPCION_ACTUAL.items
    .filter(it => it.tipo !== 'BONIFICACION')
    .every(it => it.cantidad_recibida >= it.cantidad_ordenada);
  if (!todoCompleto) {
    const ok = await _confirmarModal(
      '⚠ Recepción incompleta',
      'Hay ítems sin completar. ¿Confirmar como <strong>recepción parcial</strong>?',
      'Sí, confirmar parcial', 'Cancelar'
    );
    if (!ok) return;
  }

  // Pedir número de remisión del proveedor — Siesa lo exige obligatorio
  const remision = await _pedirRemision();
  if (remision === null) return; // operario canceló

  const btn = document.getElementById('btn-confirmar-rec');
  if (btn) { btn.textContent = 'Confirmando...'; btn.disabled = true; }

  try {
    const r = await put('/api/recepcion/' + RECEPCION_ACTUAL.id + '/confirmar', {
      num_remision_prov: remision
    });
    if (r.error) {
      alerta(r.error, 'error');
      if (btn) { btn.textContent = '✓ Confirmar recepción'; btn.disabled = false; }
      return;
    }
    let msg = 'Recepción confirmada';
    if (r.siesa_triggered) msg += ' — Siesa actualizó inventario';
    if (r.tiene_cross_dock) msg += ' · revisar Cross-Dock';
    alerta(msg, 'exito');
    RECEPCION_ACTUAL = null;
    setTimeout(cargarRecepciones, 1500);
  } catch (e) {
    alerta(e.message || 'Error confirmando', 'error');
    if (btn) { btn.textContent = '✓ Confirmar recepción'; btn.disabled = false; }
  }
}

function _pedirRemision() {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9500;display:flex;align-items:center;justify-content:center;padding:24px;';
    overlay.innerHTML = `
      <div style="background:#111;border:1px solid #333;border-radius:16px;padding:28px;max-width:360px;width:100%;">
        <div style="font-size:17px;font-weight:800;color:#fff;margin-bottom:6px;">📄 Remisión del proveedor</div>
        <div style="font-size:13px;color:#9ca3af;margin-bottom:18px;">Ingresa el número de remisión o factura física que llegó con el camión. Siesa lo requiere para cerrar la entrada.</div>
        <input id="_rem-input" type="text" placeholder="Ej: 00123456"
          style="width:100%;box-sizing:border-box;padding:13px;background:#0d0d0d;border:1px solid #555;border-radius:10px;color:#fff;font-size:18px;font-weight:700;margin-bottom:16px;letter-spacing:1px;"
          onkeydown="if(event.key==='Enter') document.getElementById('_rem-ok').click()">
        <button id="_rem-ok"
          style="width:100%;padding:15px;font-size:16px;font-weight:700;background:#16a34a;color:#fff;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">
          Confirmar recepción
        </button>
        <button id="_rem-cancel"
          style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
          Cancelar
        </button>
      </div>`;
    document.body.appendChild(overlay);
    setTimeout(() => overlay.querySelector('#_rem-input').focus(), 100);
    overlay.querySelector('#_rem-cancel').onclick = () => { overlay.remove(); resolve(null); };
    overlay.querySelector('#_rem-ok').onclick = () => {
      const val = overlay.querySelector('#_rem-input').value.trim();
      if (!val) { overlay.querySelector('#_rem-input').style.border = '1px solid #ef4444'; return; }
      overlay.remove();
      resolve(val);
    };
  });
}

function volverListaRecepciones() {
  RECEPCION_ACTUAL = null;
  cargarRecepciones();
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Recepción de Traslados (NB1)
// ─────────────────────────────────────────────────────────────
let _REC_TRASLADO_ACTIVO    = null;  // ST abierto en conteo
let _REC_CONTEOS            = {};    // {producto_id: cantidad_contada}
let _REC_TRASLADOS_PENDIENTES = [];  // lista cargada desde API

async function recepCargarTraslados(silencioso = false) {
  if (_REC_TRASLADO_ACTIVO) return;
  const el = document.getElementById('contenido-traslados-rec');
  if (!el) return;
  if (!silencioso) el.innerHTML = '<div style="text-align:center;padding:40px;color:#666;">Cargando...</div>';
  try {
    const r = await get('/api/traslados/pendientes-recepcion');
    _REC_TRASLADOS_PENDIENTES = r.solicitudes || [];
    const badge = document.getElementById('badge-traslados-rec');
    if (badge) {
      badge.style.display = _REC_TRASLADOS_PENDIENTES.length ? 'inline' : 'none';
      badge.textContent   = _REC_TRASLADOS_PENDIENTES.length;
    }
    if (!_REC_TRASLADOS_PENDIENTES.length) {
      el.innerHTML = `<div style="text-align:center;padding:50px 20px;">
        <div style="font-size:40px;">✓</div>
        <div style="font-size:18px;font-weight:700;margin-top:10px;color:#4ade80;">Sin traslados pendientes</div>
        <button onclick="_refreshBtn(event, recepCargarTraslados)" style="margin-top:20px;padding:12px 24px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">Actualizar</button>
      </div>`;
      return;
    }
    el.innerHTML = _REC_TRASLADOS_PENDIENTES.map(s => {
      const totalEsp = (s.items || []).reduce((a, i) => a + (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0), 0);
      return `
      <div style="background:#0a1a0a;border:1px solid #166534;border-radius:12px;padding:14px;margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
          <div style="font-size:15px;font-weight:800;">${s.codigo}</div>
          <div style="font-size:11px;color:#4ade80;font-weight:600;">Desde ${s.bodega_origen_siesa || '—'}</div>
        </div>
        <div style="font-size:12px;color:#4ade80;margin-bottom:8px;">📦 ${s.total_items} ítem${s.total_items !== 1 ? 's' : ''} · ${totalEsp} und esperadas</div>
        ${(s.items || []).slice(0, 3).map(i => `
          <div style="font-size:11px;color:#aaa;padding:2px 0;">
            ${i.producto_nombre || i.producto_codigo} · ${i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0} und
          </div>`).join('')}
        ${(s.items || []).length > 3 ? `<div style="font-size:11px;color:#555;padding:2px 0;">+ ${s.items.length - 3} más...</div>` : ''}
        <button onclick="recepAbrirConteoTraslado(${s.id})"
          style="width:100%;padding:13px;margin-top:12px;background:#1E8395;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;">
          📋 Contar productos
        </button>
      </div>`;
    }).join('');
  } catch (e) {
    if (!silencioso) el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando traslados</div>';
  }
}

function recepAbrirConteoTraslado(id) {
  const s = _REC_TRASLADOS_PENDIENTES.find(x => x.id === id);
  if (!s) return;
  _REC_TRASLADO_ACTIVO = s;
  _REC_CONTEOS = {};
  (s.items || []).forEach(i => { _REC_CONTEOS[i.producto_id] = 0; });
  _recepRenderPickingTraslado();
  setTimeout(() => { const inp = document.getElementById('rec-tras-scan-input'); if (inp) inp.focus(); }, 150);
}

function recepVolverListaTraslados() {
  _REC_TRASLADO_ACTIVO = null;
  _REC_CONTEOS = {};
  recepCargarTraslados();
}

function _recepRenderPickingTraslado() {
  const el = document.getElementById('contenido-traslados-rec');
  if (!el || !_REC_TRASLADO_ACTIVO) return;
  const s = _REC_TRASLADO_ACTIVO;
  const items = s.items || [];
  const todoContado = items.every(i => (_REC_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  const algoContado = items.some(i => (_REC_CONTEOS[i.producto_id] || 0) > 0);
  const btnColor  = todoContado ? '#16a34a' : '#b45309';
  const btnTexto  = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial';

  el.innerHTML = `
    <div style="padding:0;">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <button onclick="recepVolverListaTraslados()"
          style="background:#222;border:1px solid #333;color:#fff;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:14px;flex-shrink:0;">
          ← Volver
        </button>
        <div style="min-width:0;">
          <div style="font-size:16px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${s.codigo}</div>
          <div style="font-size:12px;color:#666;">Desde ${s.bodega_origen_siesa || '—'} → ${s.bodega_destino_siesa || '—'}</div>
        </div>
      </div>

      <div style="background:#111;border-radius:10px;padding:12px;margin-bottom:14px;">
        <div style="font-size:12px;color:#666;text-align:center;margin-bottom:8px;">Escanea el código o usá los botones +/−</div>
        <div style="display:flex;gap:8px;">
          <input id="rec-tras-scan-input" type="text" placeholder="Escanea o escribe el código..."
            style="flex:1;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ recepScanTraslado(v); this.value=''; } }"
            autocomplete="off" autocorrect="off" spellcheck="false">
          <button onclick="const v=document.getElementById('rec-tras-scan-input').value.trim();if(v){recepScanTraslado(v);document.getElementById('rec-tras-scan-input').value='';}"
            style="padding:10px 14px;background:#1E8395;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
        </div>
      </div>

      <div id="rec-tras-items" style="margin-bottom:14px;">
        ${_recepRenderItemsTraslado(items)}
      </div>

      <button id="btn-confirmar-rec-traslado" onclick="recepConfirmarTraslado()"
        ${algoContado || todoContado ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:18px;font-weight:700;background:${algoContado || todoContado ? btnColor : '#222'};color:#fff;border:none;border-radius:14px;cursor:${algoContado || todoContado ? 'pointer' : 'default'};margin-bottom:10px;">
        ${algoContado || todoContado ? btnTexto : 'Contá al menos un ítem para continuar'}
      </button>

      <button onclick="recepVolverListaTraslados()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
        Cancelar — volver a la lista
      </button>
    </div>`;
}

function _recepRenderItemsTraslado(items) {
  return items.map(i => {
    const esperado = i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0;
    const contado  = _REC_CONTEOS[i.producto_id] || 0;
    const completo = contado >= esperado;
    const pct = esperado > 0 ? Math.min((contado / esperado) * 100, 100) : 0;
    return `
      <div id="rec-tras-item-${i.producto_id}"
        style="background:${completo ? '#0d1a0d' : '#111'};border:1px solid ${completo ? '#166534' : '#222'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:14px;font-weight:600;color:${completo ? '#4ade80' : '#fff'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${i.producto_nombre || i.producto_codigo}</div>
            <div style="font-size:11px;color:#555;margin-top:2px;">${i.producto_codigo_siesa || i.producto_codigo}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;padding-left:8px;">
            <button onclick="recepContarItem(${i.producto_id}, -1)"
              style="width:34px;height:34px;background:#222;border:1px solid #333;color:#fff;border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">−</button>
            <div style="text-align:center;min-width:54px;">
              <div style="font-size:26px;font-weight:900;line-height:1;color:${completo ? '#4ade80' : '#fff'};">${contado}</div>
              <div style="font-size:10px;color:#6b7280;">/ ${esperado}</div>
            </div>
            <button onclick="recepContarItem(${i.producto_id}, 1)"
              style="width:34px;height:34px;background:#1E8395;border:none;color:#fff;border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">+</button>
          </div>
        </div>
        <div style="height:5px;background:#222;border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#16a34a' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.2s;"></div>
        </div>
      </div>`;
  }).join('');
}

async function recepScanTraslado(codigo) {
  if (!_REC_TRASLADO_ACTIVO) return;
  const items = _REC_TRASLADO_ACTIVO.items || [];
  let item = items.find(i => i.producto_codigo_siesa === codigo || i.producto_codigo === codigo);
  if (!item) {
    try {
      const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
      if (prod && prod.producto_id) item = items.find(i => i.producto_id === prod.producto_id);
    } catch (_) {}
  }
  if (!item) { beepError(); alerta('Código no encontrado en este traslado: ' + codigo, 'error'); return; }
  beepOk(); vibrar();
  recepContarItem(item.producto_id, 1);
  const inp = document.getElementById('rec-tras-scan-input');
  if (inp) inp.focus();
}

function recepContarItem(productoId, delta) {
  if (!_REC_TRASLADO_ACTIVO) return;
  const item = (_REC_TRASLADO_ACTIVO.items || []).find(i => i.producto_id === productoId);
  if (!item) return;
  _REC_CONTEOS[productoId] = Math.max(0, (_REC_CONTEOS[productoId] || 0) + delta);
  const itemsEl = document.getElementById('rec-tras-items');
  if (itemsEl) itemsEl.innerHTML = _recepRenderItemsTraslado(_REC_TRASLADO_ACTIVO.items || []);
  const items = _REC_TRASLADO_ACTIVO.items || [];
  const todoContado = items.every(i => (_REC_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  const algoContado = items.some(i => (_REC_CONTEOS[i.producto_id] || 0) > 0);
  const btn = document.getElementById('btn-confirmar-rec-traslado');
  if (btn) {
    btn.disabled = !(algoContado || todoContado);
    btn.style.background = !algoContado && !todoContado ? '#222' : (todoContado ? '#16a34a' : '#b45309');
    btn.textContent = !algoContado && !todoContado ? 'Contá al menos un ítem para continuar' : (todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial');
    btn.style.cursor = algoContado || todoContado ? 'pointer' : 'default';
  }
}

async function recepConfirmarTraslado() {
  const s = _REC_TRASLADO_ACTIVO;
  if (!s) return;
  const items = s.items || [];
  const todoContado = items.every(i => (_REC_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  if (!todoContado) {
    const ok = await _confirmarModal(
      '⚠ Recepción incompleta',
      'Hay ítems sin contar o con cantidad menor a la esperada. ¿Confirmar como <strong>recepción parcial</strong>?',
      'Sí, confirmar parcial', 'Cancelar'
    );
    if (!ok) return;
  }
  const btn = document.getElementById('btn-confirmar-rec-traslado');
  if (btn) { btn.textContent = 'Confirmando...'; btn.disabled = true; }
  try {
    const r = await fetch(API + `/api/traslados/${s.id}/recibir`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (r.ok) {
      beepDone();
      alerta('✓ Recepción confirmada — ETS generado en Siesa', 'exito');
      _REC_TRASLADO_ACTIVO = null;
      _REC_CONTEOS = {};
      setTimeout(recepCargarTraslados, 1200);
    } else {
      alerta(d.error || 'Error al confirmar', 'error');
      if (btn) { btn.textContent = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial'; btn.disabled = false; }
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
    if (btn) { btn.textContent = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial'; btn.disabled = false; }
  }
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Tabs (OCs / Traslados / Devoluciones)
// ─────────────────────────────────────────────────────────────

function recTab(tab) {
  REC_TAB_ACTIVO = tab;
  const tabOcs  = document.getElementById('rec-tab-ocs');
  const tabTras = document.getElementById('rec-tab-traslados');
  const tabDev  = document.getElementById('rec-tab-dev');
  const contOcs  = document.getElementById('contenido-recepcion');
  const contTras = document.getElementById('contenido-traslados-rec');
  const contDev  = document.getElementById('contenido-devoluciones');
  if (!tabOcs) return;

  const activo  = 'border-bottom:2px solid #1E8395;color:#1E8395;font-weight:600;';
  const inactivo = 'border-bottom:2px solid transparent;color:#415A70;';
  if (tabOcs)  tabOcs.style.cssText  = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;${tab==='ocs' ? activo : inactivo}`;
  if (tabTras) tabTras.style.cssText = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;position:relative;${tab==='traslados' ? activo : inactivo}`;
  if (tabDev)  tabDev.style.cssText  = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;position:relative;${tab==='dev' ? activo : inactivo}`;
  // re-append badges (se pierden al resetear cssText)
  const badgeTras = document.getElementById('badge-traslados-rec');
  const badgeDev  = document.getElementById('badge-dev');
  if (badgeTras && tabTras) tabTras.appendChild(badgeTras);
  if (badgeDev  && tabDev)  tabDev.appendChild(badgeDev);

  if (contOcs)  contOcs.style.display  = tab === 'ocs'       ? 'block' : 'none';
  if (contTras) contTras.style.display  = tab === 'traslados' ? 'block' : 'none';
  if (contDev)  contDev.style.display   = tab === 'dev'       ? 'block' : 'none';

  if (tab === 'traslados' && !_REC_TRASLADO_ACTIVO) recepCargarTraslados();
  if (tab === 'dev') cargarDevoluciones();
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Lista de Devoluciones
// ─────────────────────────────────────────────────────────────

async function cargarDevoluciones(silencioso = false) {
  if (DEVOLUCION_ACTUAL) return;
  const el = document.getElementById('contenido-devoluciones');
  const badge = document.getElementById('badge-dev');
  if (!el) return;

  try {
    const [devResp, rechResp] = await Promise.all([
      get('/api/devoluciones/?almacen_id=' + ALMACEN_ID).catch(() => ({ tareas: [] })),
      get('/api/rutas/bultos-rechazados').catch(() => ({ bultos: [] }))
    ]);
    const tareas   = devResp.tareas  || [];
    const rechazados = rechResp.bultos || [];
    const total    = tareas.length + rechazados.length;

    // Badge en el tab
    if (badge) {
      badge.style.display = total ? 'inline' : 'none';
      badge.textContent = total;
    }

    if (!total) {
      el.innerHTML = `<div style="text-align:center;padding:50px 20px;">
        <div style="font-size:48px;color:#4ade80;">✓</div>
        <div style="font-size:20px;font-weight:700;margin-top:12px;">Sin devoluciones pendientes</div>
        <div style="font-size:13px;color:#555;margin-top:8px;">La reconciliación detectará nuevas automáticamente</div>
      </div>`;
      return;
    }

    let html = '';

    // Sección 1: bultos rechazados en entrega
    if (rechazados.length) {
      html += `<div style="font-size:12px;font-weight:600;color:#f87171;padding:4px 0 8px;border-bottom:1px solid #2a1010;margin-bottom:10px;">
        🔴 ${rechazados.length} BULTO${rechazados.length !== 1 ? 'S' : ''} RECHAZADO${rechazados.length !== 1 ? 'S' : ''} — RE-INGRESAR A BODEGA
      </div>`;
      html += rechazados.map(b => `
        <div class="rec-card" style="border-color:#7f1d1d;background:#1a0d0d;">
          <div class="rec-titulo" style="font-size:16px;color:#f87171;">${b.codigo_barras}</div>
          <div class="rec-sub">${b.tipo} ${b.numero}/${b.total} · ${b.numero_pedido} · ${b.cliente || '—'}</div>
          <div style="margin-top:6px;font-size:12px;color:#f87171;">Motivo: ${b.motivo_rechazo || 'Sin especificar'}</div>
          <div style="margin-top:10px;padding:10px;background:#2a1010;border-radius:8px;font-size:12px;color:#f87171;">
            📦 Ubicar físicamente en bodega
          </div>
        </div>`).join('');
    }

    // Sección 2: devoluciones por reconciliación Siesa
    if (tareas.length) {
      html += `<div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 8px;border-bottom:1px solid #222;margin-bottom:10px;margin-top:${rechazados.length ? 16 : 0}px;">
        ${tareas.length} DEVOLUCIÓN(ES) POR RECONCILIACIÓN
      </div>`;
      html += tareas.map(t => `
        <div class="rec-card" onclick="abrirDevolucion(${t.id})" style="cursor:pointer;">
          <div class="rec-titulo" style="font-size:18px;">${t.producto_nombre}</div>
          <div class="rec-sub">${t.producto_codigo}</div>
          <div style="margin-top:8px;display:flex;justify-content:space-between;align-items:center;">
            <span style="font-size:13px;color:#666;">Diferencia Siesa vs WMS</span>
            <span style="font-size:28px;font-weight:800;color:#facc15;">+${t.cantidad_diferencia}</span>
          </div>
          <div style="margin-top:10px;padding:10px;background:#1a1500;border-radius:8px;font-size:12px;color:#facc15;">
            ⚠ Tocar para ubicar en bodega
          </div>
        </div>`).join('');
    }

    el.innerHTML = html;
  } catch (e) {
    if (badge) badge.style.display = 'none';
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando devoluciones</div>';
  }
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Flujo de ubicación de devolución
// ─────────────────────────────────────────────────────────────

async function abrirDevolucion(id) {
  try {
    const d = await get('/api/devoluciones/?almacen_id=' + ALMACEN_ID);
    const tarea = (d.tareas || []).find(t => t.id === id);
    if (!tarea) { alerta('Tarea no encontrada', 'error'); return; }
    DEVOLUCION_ACTUAL = tarea;
    renderEscaneoDevolucion(tarea);
  } catch (e) { alerta('Error cargando tarea', 'error'); }
}

function renderEscaneoDevolucion(tarea) {
  // Cambiar al tab devoluciones si no está ahí
  recTab('dev');
  const el = document.getElementById('contenido-devoluciones');
  if (!el) return;

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
      <button onclick="volverListaDevoluciones()"
        style="background:#222;border:1px solid #333;color:#fff;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:14px;">
        ← Volver
      </button>
      <span style="font-size:14px;font-weight:700;">Ubicar devolución</span>
    </div>

    <div class="rec-card" style="margin-bottom:16px;">
      <div style="font-size:22px;font-weight:800;">${tarea.producto_nombre}</div>
      <div style="font-size:13px;color:#666;margin-top:4px;">${tarea.producto_codigo}</div>
      <div style="margin-top:12px;display:flex;justify-content:space-between;align-items:center;">
        <span style="font-size:13px;color:#aaa;">Unidades a ubicar</span>
        <span style="font-size:36px;font-weight:800;color:#facc15;">${tarea.cantidad_diferencia}</span>
      </div>
    </div>

    <div style="background:#0d1f0d;border:1px solid #1a3a1a;border-radius:12px;padding:16px;margin-bottom:16px;">
      <div style="font-size:14px;font-weight:700;color:#4ade80;margin-bottom:8px;">FLUJO 1 — Mercancía en buen estado</div>
      <div style="font-size:12px;color:#666;margin-bottom:12px;">Escanea el código de barras de la ubicación/estante donde vas a poner la mercancía</div>
      <button onclick="abrirCamara('lector-qr-dev','camara-box-dev')"
        style="width:100%;padding:13px;background:#111;border:1px solid #2a5a2a;border-radius:8px;color:#4ade80;font-size:15px;cursor:pointer;margin-bottom:8px;">
        📷 Escanear ubicación con cámara
      </button>
      <div id="camara-box-dev" style="display:none;margin-bottom:8px;">
        <div id="lector-qr-dev" style="border-radius:10px;overflow:hidden;"></div>
        <button onclick="cerrarCamara('camara-box-dev')"
          style="width:100%;margin-top:6px;padding:10px;background:#111;border:1px solid #333;color:#aaa;border-radius:8px;font-size:13px;cursor:pointer;">
          Cerrar cámara
        </button>
      </div>
      <div style="display:flex;gap:8px;">
        <input id="input-ubicacion-dev" type="text" placeholder="Código ubicación (ej: A-01-02)"
          style="flex:1;padding:12px;background:#111;border:1px solid #333;border-radius:8px;color:#fff;font-size:16px;"
          onkeydown="if(event.key==='Enter') confirmarUbicacionDev()" />
        <button onclick="confirmarUbicacionDev()"
          style="padding:12px 16px;background:#16a34a;color:#fff;border:none;border-radius:8px;font-size:20px;cursor:pointer;">
          ✓
        </button>
      </div>
      <div id="estado-ubicacion-dev" style="margin-top:8px;font-size:12px;color:#555;text-align:center;"></div>
    </div>

    <div style="background:#1f0d0d;border:1px solid #3a1a1a;border-radius:12px;padding:16px;">
      <div style="font-size:14px;font-weight:700;color:#f87171;margin-bottom:8px;">FLUJO 2 — Mercancía averiada</div>
      <div style="font-size:12px;color:#666;margin-bottom:12px;">La mercancía está dañada. Se moverá a bodega AV1 en Siesa automáticamente.</div>
      <button onclick="confirmarAveria(${tarea.id})"
        style="width:100%;padding:16px;background:#7f1d1d;color:#fca5a5;border:1px solid #991b1b;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer;">
        ⚠ Reportar Avería
      </button>
    </div>`;
}

async function confirmarUbicacionDev() {
  if (!DEVOLUCION_ACTUAL) return;
  const inp = document.getElementById('input-ubicacion-dev');
  const estado = document.getElementById('estado-ubicacion-dev');
  const codigo = (inp ? inp.value : '').trim();
  if (!codigo) { alerta('Escanea o ingresa el código de ubicación', 'advertencia'); return; }

  if (estado) { estado.textContent = '⏳ Confirmando...'; estado.style.color = '#93c5fd'; }
  try {
    const r = await post(`/api/devoluciones/${DEVOLUCION_ACTUAL.id}/ubicar`, {
      ubicacion_codigo: codigo
    });
    if (r.error) { if (estado) { estado.textContent = 'Error: ' + r.error; estado.style.color = '#ef4444'; } return; }
    vibrar(); flash();
    alerta(`✓ ${DEVOLUCION_ACTUAL.cantidad_diferencia} uds ubicadas en ${codigo}`, 'exito');
    DEVOLUCION_ACTUAL = null;
    setTimeout(cargarDevoluciones, 800);
  } catch (e) {
    if (estado) { estado.textContent = 'Error de conexión'; estado.style.color = '#ef4444'; }
  }
}

async function procesarScanDevolucion(codigo) {
  // En flujo devolución el escáner llena el campo de ubicación
  const inp = document.getElementById('input-ubicacion-dev');
  if (inp) {
    inp.value = codigo;
    vibrar(); flash();
    await confirmarUbicacionDev();
  }
}

async function confirmarAveria(tareaId) {
  const tarea = DEVOLUCION_ACTUAL;
  if (!tarea) return;
  const ok = confirm(
    `¿Confirmas que esta mercancía está AVERIADA?\n\n` +
    `Producto: ${tarea.producto_nombre}\n` +
    `Cantidad: ${tarea.cantidad_diferencia} uds\n\n` +
    `Esta acción moverá el inventario en Siesa a bodega AV1.\nNo se puede deshacer.`
  );
  if (!ok) return;

  const btn = document.querySelector(`button[onclick="confirmarAveria(${tareaId})"]`);
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Procesando...'; }

  try {
    const r = await post(`/api/devoluciones/${tareaId}/ubicar`, { es_averiado: true });
    if (r.error) { alerta('Error: ' + r.error, 'error'); if (btn) { btn.disabled = false; btn.textContent = '⚠ Reportar Avería'; } return; }
    vibrar(); flash();
    alerta(`✓ Avería registrada — ${tarea.cantidad_diferencia} uds trasladadas a AV1 en Siesa`, 'exito');
    DEVOLUCION_ACTUAL = null;
    setTimeout(cargarDevoluciones, 800);
  } catch (e) {
    alerta('Error de conexión', 'error');
    if (btn) { btn.disabled = false; btn.textContent = '⚠ Reportar Avería'; }
  }
}

function volverListaDevoluciones() {
  DEVOLUCION_ACTUAL = null;
  cargarDevoluciones();
}
// EMPACADOR — Estado global
// ─────────────────────────────────────────────────────────────

let EMP_TAREA = null;       // TareaPacking activa en el HUD
let EMP_ITEMS = [];         // ItemPacking[] con progreso actual
let EMP_ITEM_IDX = 0;       // índice del ítem que se está escaneando
let EMP_EMPAQUES = {};      // producto_id → { factor, unidad } — cargado al iniciar HUD

// ─────────────────────────────────────────────────────────────
// EMPACADOR — Lista de tareas
// ─────────────────────────────────────────────────────────────

async function empCargarTareas() {
  const el = document.getElementById('emp-lista');
  if (!el) return;
  try {
    const d = await get('/api/packing/?activas=true');
    const tareas = (d.tareas || []).filter(t =>
      ['PENDIENTE', 'EN_PROCESO'].includes(t.estado) ||
      (t.estado === 'VERIFICADO' && !t.siesa_triggered)  // Siesa falló — permitir reintento
    );

    if (!tareas.length) {
      const titulo = document.getElementById('emp-modo-titulo');
      if (titulo) titulo.textContent = '';
      el.innerHTML = `<div style="text-align:center;padding:60px 20px;color:#555;">
        Sin tareas de empaque pendientes ✓<br>
        <button onclick="_refreshBtn(event, empCargarTareas)" style="margin-top:20px;background:#222;border:1px solid #333;color:#fff;padding:10px 20px;border-radius:10px;cursor:pointer;">↻ Actualizar</button>
      </div>`;
      return;
    }

    // Título de contexto en el header blanco
    const hayTraslados = tareas.some(t => t.tipo_documento === 'TRASLADO');
    const hayPedidos   = tareas.some(t => t.tipo_documento !== 'TRASLADO');
    const titulo = document.getElementById('emp-modo-titulo');
    if (titulo) {
      if (hayTraslados && !hayPedidos) {
        titulo.textContent = '📦 Packing Traslado';
        titulo.style.color = '#c2410c';
      } else if (hayPedidos && !hayTraslados) {
        titulo.textContent = '🛒 Packing Pedido';
        titulo.style.color = '#1d4ed8';
      } else {
        titulo.textContent = '📦 Packing Mixto';
        titulo.style.color = 'var(--tx)';
      }
    }

    el.innerHTML = `
      <div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 12px;">TAREAS DE EMPAQUE</div>
      ${tareas.map(t => {
        const verificados = t.items_verificados || 0;
        const total = t.total_items || 0;
        const pct = total ? Math.round(verificados / total * 100) : 0;
        const pickingListo = t.picking_listo !== false;
        const pedidoAnulado = t.pedido_anulado_siesa === true;
        const siesaFallo = t.estado === 'VERIFICADO' && !t.siesa_triggered && !pedidoAnulado;
        const enProceso = t.estado === 'EN_PROCESO';
        const bloqueado = (!pickingListo && t.estado === 'PENDIENTE') || pedidoAnulado;
        const color = pedidoAnulado ? '#fca5a5' : bloqueado ? '#6b7280' : siesaFallo ? '#fca5a5' : enProceso ? '#93c5fd' : '#facc15';
        const bg    = pedidoAnulado ? '#7f1d1d'  : bloqueado ? '#1a1a1a'  : siesaFallo ? '#7f1d1d'  : enProceso ? '#1e3a5f' : '#713f12';
        const label = pedidoAnulado ? '🚫 PEDIDO ANULADO EN SIESA' : bloqueado ? 'Esperando picking' : siesaFallo ? '⚠ Reintentar Siesa' : enProceso ? 'En proceso' : 'Pendiente';
        const anulado_banner = pedidoAnulado ? `
          <div style="margin-top:10px;background:#1a0505;border:1px solid #7f1d1d;border-radius:8px;padding:10px 12px;">
            <div style="font-size:12px;font-weight:700;color:#ef4444;margin-bottom:4px;">🚫 Pedido anulado en Siesa (estado ${t.pedido_estado_siesa_detectado || '9'})</div>
            <div style="font-size:11px;color:#fca5a5;line-height:1.4;">
              El área comercial anuló este pedido en el ERP.<br>
              <strong>Acción:</strong> Cancelar este packing y esperar el nuevo pedido clonado.
            </div>
            <button onclick="event.stopPropagation();empCancelarPacking(${t.id})"
              style="margin-top:8px;width:100%;padding:8px;background:#7f1d1d;border:none;color:#fff;border-radius:8px;cursor:pointer;font-size:12px;font-weight:700;">
              Cancelar packing
            </button>
          </div>` : '';
        const limpiarBtn = siesaFallo ? `
          <button onclick="event.stopPropagation();empLimpiarSiesa(${t.id})"
            style="margin-top:8px;width:100%;padding:8px;background:#1a1a1a;border:1px solid #444;color:#aaa;border-radius:8px;cursor:pointer;font-size:12px;">
            🗑 Limpiar bultos y redeclarar piezas
          </button>` : '';
        const esTraslado = t.tipo_documento === 'TRASLADO';
        const refDisplay = t.referencia_doc || t.numero_pedido_siesa || '—';
        const etiquetaHtml = esTraslado
          ? `<span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;background:#431407;color:#fb923c;letter-spacing:.5px;margin-left:8px;">TRASLADO</span>`
          : `<span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;background:#1e3a5f;color:#93c5fd;letter-spacing:.5px;margin-left:8px;">PEDIDO</span>`;
        const destinoHtml = esTraslado && t.tienda_destino
          ? `<div style="font-size:11px;color:#fb923c;margin-top:2px;">→ ${t.tienda_destino}</div>` : '';
        return `
        <div class="emp-task-card" onclick="${(bloqueado || pedidoAnulado) ? '' : `empIniciarHUD(${t.id})`}"
          style="${(bloqueado || pedidoAnulado) ? 'cursor:default;' : 'cursor:pointer;'}${pedidoAnulado ? 'border-color:#7f1d1d;background:#110505;' : ''}${esTraslado ? 'border-color:#431407;' : ''}">
          <div class="emp-task-pedido" style="display:flex;align-items:center;">${refDisplay}${etiquetaHtml}</div>
          ${destinoHtml}
          <div class="emp-task-sub">${total} producto(s) · ${t.items_verificados || 0}/${total} verificados</div>
          ${total > 0 ? `<div style="margin-top:10px;background:#1a1a1a;border-radius:8px;height:6px;overflow:hidden;">
            <div style="height:100%;background:#4ade80;width:${pct}%;border-radius:8px;transition:width 0.3s;"></div>
          </div>` : ''}
          <span class="emp-task-badge" style="background:${bg};color:${color};">${label}</span>
          ${anulado_banner}
          ${limpiarBtn}
        </div>`;
      }).join('')}`;
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando tareas</div>';
  }
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: iniciar tarea y mostrar primer ítem
// ─────────────────────────────────────────────────────────────

async function empIniciarHUD(packingId) {
  try {
    // Cargar detalle completo de la tarea
    const t = await get(`/api/packing/${packingId}`);
    if (!t || !t.id) { alerta('Tarea no encontrada', 'error'); return; }

    // Bloquear si el picking aún no está completo
    if (t.picking_listo === false && t.estado === 'PENDIENTE') {
      alerta('El operario aún está pickeando — espera a que termine', 'advertencia');
      return;
    }

    EMP_TAREA = { ...t, id: packingId };

    // Retry Siesa: bultos ya creados pero Siesa falló — reintentar directamente
    if (t.estado === 'VERIFICADO' && !t.siesa_triggered && t.bultos?.length) {
      await empReintentarSiesa(t);
      return;
    }

    // Iniciar si aún está PENDIENTE
    if (t.estado === 'PENDIENTE') {
      // Ajustar cantidades con lo que el picker realmente recogió (faltantes parciales)
      await fetch(`/api/packing/${packingId}/sincronizar-picking`, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }
      });
      await fetch(`/api/packing/${packingId}/iniciar`, {
        method: 'PUT',
        headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }
      });
      // Re-cargar para obtener cantidades actualizadas tras el sync de picking
      const tFresh = await get(`/api/packing/${packingId}`);
      if (tFresh && tFresh.id) Object.assign(t, tFresh);
    }

    // Ítems pendientes de verificar van primero
    EMP_ITEMS = [...(t.items || [])].sort((a, b) => a.verificado - b.verificado);
    EMP_ITEM_IDX = EMP_ITEMS.findIndex(i => !i.verificado);
    if (EMP_ITEM_IDX < 0) EMP_ITEM_IDX = 0;

    // Poblar empaques directamente desde factor_conversion del producto (fuente de verdad)
    EMP_EMPAQUES = {};
    for (const item of EMP_ITEMS) {
      const fc = item.factor_conversion || 1;
      if (fc > 1) {
        EMP_EMPAQUES[item.producto_id] = {
          factor: fc,
          unidad: item.unidad_empaque || 'PIEZA'
        };
      }
    }

    empRenderHUDItem();
    document.getElementById('emp-hud').classList.add('activo');
    if (!/Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
      document.getElementById('scanner-input').focus();
    }
  } catch (e) { alerta('Error iniciando tarea', 'error'); }
}

async function empCancelarPacking(packingId) {
  if (!confirm('¿Cancelar este packing? El pedido fue anulado en Siesa. La mercancía que ya fue pickeada debe devolverse a la ubicación o esperar el nuevo pedido.')) return;
  try {
    const r = await fetch(`/api/packing/${packingId}/cancelar`, {
      method: 'PUT',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo: 'Pedido anulado en Siesa ERP — cancelado desde WMS' })
    });
    const data = await r.json();
    if (!r.ok) { alerta(data.error || 'Error al cancelar', 'error'); return; }
    alerta('Packing cancelado — avisa al jefe de almacén para devolver la mercancía', 'info');
    empCargarTareas();
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function empLimpiarSiesa(packingId) {
  if (!confirm('¿Eliminar los bultos registrados y volver a declarar las piezas?')) return;
  try {
    const r = await fetch(`/api/packing/${packingId}/resetear-siesa`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    });
    const data = await r.json();
    if (!r.ok) { alerta(data.error || 'Error al limpiar', 'error'); return; }
    alerta('Listo — declara las piezas de nuevo al abrir la tarea', 'exito');
    empCargarTareas();
  } catch (e) { alerta('Error de conexión', 'error'); }
}


async function empDespacharConFaltantes() {
  if (!EMP_TAREA) return;
  const sinEscanear = EMP_ITEMS.filter(i => !i.verificado);
  const nombres = sinEscanear.map(i =>
    `• ${i.producto_nombre || i.producto_codigo || 'Producto'} (esperado: ${i.cantidad_esperada})`
  ).join('\n');
  if (!confirm(`Sin stock — quedarán en 0 y Siesa los cancelará automáticamente:\n\n${nombres}\n\n¿Confirmar despacho parcial?`)) return;

  const btn = document.getElementById('emp-btn-faltantes');
  if (btn) { btn.disabled = true; btn.textContent = 'Procesando...'; }

  try {
    const r = await fetch(`/api/packing/${EMP_TAREA.id}/confirmar`, {
      method: 'PUT',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ forzar: true })
    });
    const data = await r.json();
    if (!r.ok) {
      empFlash('rojo', data.error || 'Error confirmando');
      if (btn) { btn.disabled = false; btn.textContent = 'Despachar con faltantes ⚠'; }
      return;
    }
    if (data.tarea && data.tarea.items) {
      EMP_ITEMS = data.tarea.items;
      Object.assign(EMP_TAREA, data.tarea);
    }
    empRenderHUDItem();
    _BULTOS_LINEAS = [];
    document.getElementById('modal-bultos-lineas').innerHTML = '';
    document.getElementById('modal-bultos-error').textContent = '';
    document.getElementById('modal-bultos-pedido').textContent =
      `${EMP_TAREA.numero_pedido_siesa} · ${EMP_TAREA.cliente || ''} · ${EMP_TAREA.municipio || ''}`;
    const _btnConf = document.querySelector('#modal-bultos button[onclick="bultosConfirmar()"]');
    if (_btnConf) { _btnConf.disabled = false; _btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
    document.getElementById('modal-bultos').style.display = 'flex';
  } catch (e) {
    empFlash('rojo', 'Error de conexión');
    if (btn) { btn.disabled = false; btn.textContent = 'Despachar con faltantes ⚠'; }
  }
}

async function empReintentarSiesa(t) {
  // Los bultos ya existen — el backend los reutiliza, solo reintenta Siesa
  const bultoResumen = t.bultos.reduce((acc, b) => {
    acc[b.tipo] = (acc[b.tipo] || 0) + 1;
    return acc;
  }, {});
  const resumenTexto = Object.entries(bultoResumen).map(([tipo, n]) => `${n} ${tipo}`).join(', ');

  alerta(`Reintentando Siesa para ${t.numero_pedido_siesa} (${resumenTexto})…`, 'info');

  try {
    const r = await fetch(`/api/packing/${t.id}/cerrar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      // bultos_data vacío — el backend detecta bultos existentes y solo reintenta Siesa
      body: JSON.stringify({ bultos: t.bultos.map(b => ({ tipo: b.tipo, cantidad: 1 })) })
    });
    const data = await r.json();
    if (!r.ok) {
      alerta(`Error Siesa: ${data.error || 'ver logs'}`, 'error');
      return;
    }
    empImprimirEtiquetas(data.bultos, {
      numero_pedido: data.numero_pedido,
      cliente: data.cliente,
      municipio: data.municipio
    });
    alerta(`${t.numero_pedido_siesa} despachado — Siesa procesó la factura`, 'exito');
    empCargarTareas();
  } catch (e) {
    alerta('Error de conexión al reintentar Siesa', 'error');
  }
}


function empCerrarHUD() {
  document.getElementById('emp-hud').classList.remove('activo');
  EMP_TAREA = null;
  EMP_ITEMS = [];
  EMP_ITEM_IDX = 0;
  EMP_EMPAQUES = {};
  empCargarTareas();
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: renderizar ítem actual
// ─────────────────────────────────────────────────────────────

function empRenderHUDItem() {
  if (!EMP_TAREA || !EMP_ITEMS.length) return;

  const verificados = EMP_ITEMS.filter(i => i.verificado).length;
  const total = EMP_ITEMS.length;
  const pendientes = EMP_ITEMS.filter(i => !i.verificado);
  const item = pendientes[0] || EMP_ITEMS[EMP_ITEM_IDX] || EMP_ITEMS[0];

  // Calcular display en piezas si el producto tiene empaque.
  // cantidad_esperada puede estar en UND (normalizado) o en unidades de empaque / PQ (legado).
  // Heurístico: si cantEsp > 0 && cantEsp < factor → está en PQ (no dividir).
  const emp = EMP_EMPAQUES[item.producto_id];
  const factor = emp ? emp.factor : 1;
  const unidad = emp ? emp.unidad : 'und';
  const cantReal = item.cantidad_real || 0;
  const cantEsp  = item.cantidad_esperada || 0;

  // esPQ: cantidad_esperada en unidades de empaque (legado pre-normalización)
  const esPQ = factor > 1 && cantEsp > 0 && cantEsp < factor;

  const piezasReal = esPQ
    ? cantReal                            // cantReal ya está en PQ
    : (factor > 1 ? Math.floor(cantReal / factor) : cantReal);
  const piezasEsp = esPQ
    ? cantEsp                             // cantEsp ya está en PQ
    : (factor > 1 ? Math.ceil(cantEsp / factor) : cantEsp);
  const sueltas = esPQ
    ? 0
    : (factor > 1 ? cantReal % factor : 0);

  document.getElementById('emp-hud-pedido').textContent = EMP_TAREA.numero_pedido_siesa;
  document.getElementById('emp-hud-producto').textContent = item.producto_nombre || item.producto_codigo || '—';

  const undEl = document.getElementById('emp-hud-und');
  if (factor > 1) {
    document.getElementById('emp-hud-contador').textContent = piezasReal;
    document.getElementById('emp-hud-de').textContent =
      `de ${piezasEsp} ${unidad}${sueltas > 0 ? ` (+${sueltas} sueltas)` : ''}`;
    // Línea secundaria: en UND para items normalizados, en PQ para legado
    if (undEl) undEl.textContent = esPQ
      ? `${cantReal * factor} / ${cantEsp * factor} und estimadas`
      : `${cantReal} / ${cantEsp} und`;
  } else {
    document.getElementById('emp-hud-contador').textContent = cantReal;
    document.getElementById('emp-hud-de').textContent = `de ${cantEsp}`;
    if (undEl) undEl.textContent = '';
  }
  document.getElementById('emp-hud-items').textContent = `${verificados} de ${total} ítems verificados`;

  const pct = total ? Math.round(verificados / total * 100) : 0;
  document.getElementById('emp-hud-barra').style.width = pct + '%';

  // Botón cerrar caja: solo visible si TODOS verificados
  const btn = document.getElementById('emp-btn-cerrar-caja');
  if (verificados === total && total > 0) {
    btn.style.display = 'block';
    btn.disabled = false;
    document.getElementById('emp-hud-producto').textContent = '¡Todo verificado! Cierra la caja.';
  } else {
    btn.style.display = 'none';
  }

  // Botón faltantes: visible cuando hay ítems escaneados pero algunos siguen sin stock
  const btnFaltantes = document.getElementById('emp-btn-faltantes');
  if (btnFaltantes) {
    btnFaltantes.style.display = (verificados > 0 && verificados < total) ? 'block' : 'none';
  }
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: procesar escaneo láser
// ─────────────────────────────────────────────────────────────

async function empProcesarEscaneo(codigo) {
  if (!EMP_TAREA) return;

  // ── Resolver empaque antes de registrar ───────────────────────────────────
  // Si el operario escanea un DUN-14 (caja/paca), necesitamos:
  //   1. Identificar el producto real (no el barcode de la caja)
  //   2. Enviar cantidad = factor (no 1) al backend
  let codigoParaBackend = codigo;
  let cantidadParaBackend = 1;
  let etiquetaEmpaque = '';

  try {
    const scan = await get(`/api/empaques/scan/${encodeURIComponent(codigo)}`);
    const tipo = scan.tipo || 'NO_ENCONTRADO';

    if (tipo === 'GS1_UNICO' && scan.producto && scan.factor > 1) {
      // Barcode de empaque → enviar código de producto y factor como cantidad
      codigoParaBackend = scan.producto.codigo;
      cantidadParaBackend = scan.factor;
      etiquetaEmpaque = `${scan.empaque?.unidad_medida || 'PIEZA'} completa — ${scan.factor} und`;
    } else if (tipo === 'LPN' && scan.producto) {
      codigoParaBackend = scan.producto.codigo;
      cantidadParaBackend = scan.factor || 1;
      etiquetaEmpaque = `LPN — ${cantidadParaBackend} und`;
    } else if (tipo === 'GS1_AMBIGUO') {
      _modalAmbiguedadPackingEmp(codigo, scan.ambiguos || []);
      return;
    }
    // EAN_BASE o NO_ENCONTRADO → flujo original (codigoParaBackend = codigo, cantidad = 1)
  } catch (_) {
    // Si /api/empaques/scan falla, continuar con flujo original
  }

  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: EMP_TAREA.id,
      tipo: 'PACKING',
      codigo: codigoParaBackend,
      cantidad: cantidadParaBackend
    });

    if (r.error) {
      empFlash('rojo', r.error);
      return;
    }

    // Actualizar estado local del ítem
    const item = EMP_ITEMS.find(i =>
      (r.producto_id && i.producto_id === r.producto_id) ||
      (r.producto_codigo && i.producto_codigo === r.producto_codigo)
    );
    if (item) {
      item.cantidad_real = r.cantidad_actual;
      item.verificado = r.item_completado;
    }

    empFlash('verde', etiquetaEmpaque || null);

    // Si todos los ítems están listos
    if (r.todos_completados) {
      const detalle = await get(`/api/packing/${EMP_TAREA.id}`);
      EMP_ITEMS = detalle.items || EMP_ITEMS;
    }

    empRenderHUDItem();

  } catch (e) {
    empFlash('rojo', e.message && e.message !== '401' ? e.message : 'Error de conexión');
  }
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: flash visual verde/rojo
// ─────────────────────────────────────────────────────────────

function empFlash(color, mensaje) {
  const flash = document.getElementById('emp-flash');
  const hud = document.getElementById('emp-hud');
  const esVerde = color === 'verde';

  flash.style.background = esVerde ? '#15803d' : '#991b1b';
  flash.style.opacity = '0.7';

  const msgEl = document.getElementById('emp-hud-producto');
  const prevText = msgEl ? msgEl.textContent : '';

  if (esVerde && mensaje && msgEl) {
    // Mostrar brevemente qué se registró (ej. "CAJA completa — 24 und").
    // Al expirar, re-renderizar el ítem ACTUAL (no restaurar prevText:
    // empRenderHUDItem ya pudo haber avanzado al siguiente producto).
    msgEl.style.color = '#4ade80';
    msgEl.textContent = mensaje;
    setTimeout(() => {
      msgEl.style.color = '';
      empRenderHUDItem();   // ← ítem correcto, no el anterior
    }, 900);
  } else if (!esVerde && mensaje && msgEl) {
    hud.style.background = '#1a0000';
    msgEl.style.color = '#f87171';
    msgEl.textContent = '⚠ ' + mensaje;
    setTimeout(() => {
      msgEl.style.color = '';
      msgEl.textContent = prevText;
      hud.style.background = '';
    }, 1800);
  }

  setTimeout(() => {
    flash.style.opacity = '0';
    if (esVerde) hud.style.background = '';
  }, esVerde ? 150 : 300);
}

// ─────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────
// EMPACADOR — Modal ambigüedad de empaque en packing
// ─────────────────────────────────────────────────────────────

function _modalAmbiguedadPackingEmp(codigo, ambiguos) {
  // ambiguos: array de ProductoEmpaque.to_dict()
  const opciones = ambiguos.map(e => `
    <button onclick="_elegirEmpaquePacking('${codigo}', '${e.producto_codigo || ''}', ${e.factor_conversion}, '${e.unidad_medida}', this.closest('.modal-ambig-emp'))"
      style="width:100%;padding:16px;font-size:18px;font-weight:700;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:12px;cursor:pointer;margin-bottom:8px;">
      ${e.unidad_medida} — ${e.factor_conversion} und
      <div style="font-size:12px;color:#666;font-weight:400;margin-top:2px;">${e.producto_nombre || e.referencia_item || ''}</div>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-ambig-emp';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.85);display:flex;align-items:flex-end;';
  modal.innerHTML = `
    <div style="background:#0a0a0a;border-top:2px solid #7c3aed;border-radius:20px 20px 0 0;padding:24px;width:100%;max-height:70vh;overflow-y:auto;">
      <div style="font-size:16px;font-weight:700;color:#a78bfa;margin-bottom:4px;">Código en múltiples empaques</div>
      <div style="font-size:13px;color:#666;margin-bottom:16px;">${codigo} — ¿Cuál estás empacando?</div>
      ${opciones}
      <button onclick="this.closest('.modal-ambig-emp').remove()"
        style="width:100%;padding:12px;font-size:14px;background:#111;color:#666;border:1px solid #222;border-radius:10px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

async function _elegirEmpaquePacking(codigoBarras, productoCodigo, factor, unidad, modal) {
  if (modal) modal.remove();
  if (!EMP_TAREA) return;
  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: EMP_TAREA.id,
      tipo: 'PACKING',
      codigo: productoCodigo || codigoBarras,
      cantidad: factor
    });
    if (r.error) { empFlash('rojo', r.error); return; }
    const item = EMP_ITEMS.find(i => r.producto_id && i.producto_id === r.producto_id);
    if (item) { item.cantidad_real = r.cantidad_actual; item.verificado = r.item_completado; }
    empFlash('verde', `${unidad} — ${factor} und`);
    if (r.todos_completados) {
      const detalle = await get(`/api/packing/${EMP_TAREA.id}`);
      EMP_ITEMS = detalle.items || EMP_ITEMS;
    }
    empRenderHUDItem();
  } catch (e) { empFlash('rojo', e.message && e.message !== '401' ? e.message : 'Error de conexión'); }
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: confirmar packing → Siesa se dispara solo
// ─────────────────────────────────────────────────────────────

async function empConfirmarPacking() {
  if (!EMP_TAREA) return;
  const btn = document.getElementById('emp-btn-cerrar-caja');
  btn.disabled = true;
  btn.textContent = 'Verificando...';

  try {
    const r = await fetch(`/api/packing/${EMP_TAREA.id}/confirmar`, {
      method: 'PUT',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ forzar: false })
    });
    const data = await r.json();

    if (!r.ok) {
      if (r.status === 409 && data.diferencias) {
        const resumen = data.diferencias.map(d => `${d.producto}: esperado ${d.esperado}, real ${d.real}`).join('\n');
        if (confirm(`Hay diferencias en cantidades:\n${resumen}\n\n¿Confirmar de todas formas?`)) {
          const r2 = await fetch(`/api/packing/${EMP_TAREA.id}/confirmar`, {
            method: 'PUT',
            headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
            body: JSON.stringify({ forzar: true })
          });
          if (!r2.ok) {
            const d2 = await r2.json();
            empFlash('rojo', d2.error || 'Error confirmando');
            btn.disabled = false; btn.textContent = 'Cerrar Caja ✓';
            return;
          }
        } else {
          btn.disabled = false; btn.textContent = 'Cerrar Caja ✓';
          return;
        }
      } else {
        const msg = typeof data.error === 'string' ? data.error : 'Error confirmando';
        empFlash('rojo', msg);
        btn.disabled = false; btn.textContent = 'Cerrar Caja ✓';
        return;
      }
    }

    // Ítems verificados — abrir modal para declarar piezas físicas
    btn.disabled = false; btn.textContent = 'Cerrar Caja ✓';
    empFlash('verde', null);

    _BULTOS_LINEAS = [];
    document.getElementById('modal-bultos-lineas').innerHTML = '';
    document.getElementById('modal-bultos-error').textContent = '';
    document.getElementById('modal-bultos-pedido').textContent =
      `${EMP_TAREA.numero_pedido_siesa} · ${EMP_TAREA.cliente || ''} · ${EMP_TAREA.municipio || ''}`;
    const _btnConf = document.querySelector('#modal-bultos button[onclick="bultosConfirmar()"]');
    if (_btnConf) { _btnConf.disabled = false; _btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
    document.getElementById('modal-bultos').style.display = 'flex';

  } catch (e) {
    empFlash('rojo', 'Error de conexión');
    btn.disabled = false; btn.textContent = 'Cerrar Caja ✓';
  }
}

// ─────────────────────────────────────────────────────────────
// MODAL BULTOS — declaración de piezas físicas al cerrar packing
// ─────────────────────────────────────────────────────────────

let _BULTOS_LINEAS = [];

function bultosAgregarLinea(tipo) {
  const existing = _BULTOS_LINEAS.find(l => l.tipo === tipo);
  if (existing) { existing.cantidad++; }
  else { _BULTOS_LINEAS.push({ tipo, cantidad: 1 }); }
  bultosRenderLineas();
}

function bultosRenderLineas() {
  const el = document.getElementById('modal-bultos-lineas');
  if (!el) return;
  if (!_BULTOS_LINEAS.length) {
    el.innerHTML = '<div style="color:#555;font-size:13px;text-align:center;padding:12px;">Agrega al menos una pieza ↑</div>';
    return;
  }
  el.innerHTML = _BULTOS_LINEAS.map((l, i) => `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
      <div style="flex:1;font-size:14px;font-weight:600;">${l.tipo}</div>
      <button onclick="bultosAjustarCantidad(${i},-1)" style="width:32px;height:32px;background:#222;border:1px solid #333;color:#fff;border-radius:6px;cursor:pointer;font-size:18px;">−</button>
      <div style="min-width:28px;text-align:center;font-size:17px;font-weight:700;">${l.cantidad}</div>
      <button onclick="bultosAjustarCantidad(${i},1)" style="width:32px;height:32px;background:#222;border:1px solid #333;color:#fff;border-radius:6px;cursor:pointer;font-size:18px;">+</button>
      <button onclick="bultosEliminarLinea(${i})" style="width:32px;height:32px;background:#1a1a1a;border:1px solid #333;color:#ef4444;border-radius:6px;cursor:pointer;font-size:14px;">✕</button>
    </div>`).join('');
}

function bultosAjustarCantidad(idx, delta) {
  _BULTOS_LINEAS[idx].cantidad = Math.max(1, _BULTOS_LINEAS[idx].cantidad + delta);
  bultosRenderLineas();
}

function bultosEliminarLinea(idx) {
  _BULTOS_LINEAS.splice(idx, 1);
  bultosRenderLineas();
}

function bultosCancelar() {
  document.getElementById('modal-bultos').style.display = 'none';
  _BULTOS_LINEAS = [];
}

async function bultosConfirmar() {
  const errEl = document.getElementById('modal-bultos-error');
  errEl.textContent = '';
  const total = _BULTOS_LINEAS.reduce((s, l) => s + l.cantidad, 0);
  if (!_BULTOS_LINEAS.length || total < 1) {
    errEl.textContent = 'Debes agregar al menos una pieza';
    return;
  }

  const btnConf = document.querySelector('#modal-bultos button[onclick="bultosConfirmar()"]');
  if (btnConf) { btnConf.disabled = true; btnConf.textContent = 'Cerrando...'; }

  const _abort = new AbortController();
  const _timeout = setTimeout(() => _abort.abort(), 45000);

  try {
    const r = await fetch(`/api/packing/${EMP_TAREA.id}/cerrar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ bultos: _BULTOS_LINEAS.map(l => ({ tipo: l.tipo, cantidad: l.cantidad })) }),
      signal: _abort.signal
    });
    clearTimeout(_timeout);
    const data = await r.json();

    if (!r.ok) {
      errEl.textContent = data.error || 'Error al cerrar';
      if (btnConf) { btnConf.disabled = false; btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
      return;
    }

    const tareaId = EMP_TAREA.id;

    document.getElementById('modal-bultos').style.display = 'none';
    _BULTOS_LINEAS = [];

    empImprimirEtiquetas(data.bultos, {
      numero_pedido: data.numero_pedido,
      cliente: data.cliente,
      municipio: data.municipio
    });

    document.getElementById('emp-hud').classList.remove('activo');
    EMP_TAREA = null;
    EMP_ITEMS = [];
    alerta(`${data.bultos.length} pieza(s) registradas — Siesa procesó la factura`, 'exito');
    empCargarTareas();
    // Factura solo para empacadores NB1 con tareas PD — los packer_traslado
    // cierran traslados (numero_pedido=null) y no generan factura/remisión.
    const _esPacTras = OPERARIO && ['packer_traslado','picker_traslado'].includes(OPERARIO.rol);
    if (data.numero_pedido && !_esPacTras) empMostrarBotonFactura(tareaId, data.numero_pedido);

  } catch (e) {
    errEl.textContent = 'Error de conexión';
    if (btnConf) { btnConf.disabled = false; btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
  }
}

// ─────────────────────────────────────────────────────────────
// FACTURA — botón flotante post-cierre e impresión con JWT
// ─────────────────────────────────────────────────────────────

function empMostrarBotonFactura(packingId, numeroPedido) {
  const existing = document.getElementById('btn-remision-flotante');
  if (existing) existing.remove();

  const div = document.createElement('div');
  div.id = 'btn-remision-flotante';
  div.style.cssText = 'position:fixed;bottom:90px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;align-items:center;gap:8px;';
  div.innerHTML = `
    <div style="background:#14532d;border:1px solid #16a34a;color:#bbf7d0;font-size:11px;font-weight:600;padding:6px 14px;border-radius:20px;text-align:center;">
      Pedido ${numeroPedido || ''} cerrado
    </div>
    <button onclick="empImprimirFactura(${packingId})"
      style="background:#16a34a;color:#fff;border:none;border-radius:12px;padding:14px 28px;font-size:15px;font-weight:700;cursor:pointer;box-shadow:0 4px 20px rgba(0,0,0,0.5);">
      🖨 Imprimir Factura
    </button>
    <button onclick="document.getElementById('btn-remision-flotante').remove()"
      style="background:transparent;color:#6b7280;border:none;font-size:12px;cursor:pointer;padding:4px;">
      Cerrar
    </button>`;
  document.body.appendChild(div);
}

async function empImprimirFactura(packingId) {
  try {
    const resp = await fetch(`/api/packing/${packingId}/factura`, {
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alerta(err.error || 'No se pudo generar la factura', 'error');
      return;
    }
    const html = await resp.text();
    const win = window.open('', '_blank');
    if (!win) { alerta('Permite ventanas emergentes para imprimir la factura', 'error'); return; }
    win.document.write(html);
    win.document.close();
  } catch (e) {
    alerta('Error de conexión al generar factura', 'error');
  }
}

// ─────────────────────────────────────────────────────────────
// ETIQUETA LPN — imprime la etiqueta de una paca/caja física
// Se llama desde recepción (manual y DUN-14) y desde picking
// (lazy labeling de inventario heredado sin etiqueta).
// ─────────────────────────────────────────────────────────────

function imprimirEtiquetaLPN(lpn, productoNombre) {
  const area = document.getElementById('print-area');
  if (!area) return;

  const hoy = new Date().toLocaleDateString('es-CO');
  const uid = `lpn-bc-${lpn.id || Date.now()}`;

  area.innerHTML = `
    <div class="etiqueta-lpn">
      <div class="el-titulo">BODEGA — PACA / CAJA</div>
      <svg id="${uid}"></svg>
      <div class="el-codigo">${lpn.codigo}</div>
      <div class="el-producto">${productoNombre || lpn.producto_nombre || ''}</div>
      <div class="el-cantidad">${lpn.cantidad_actual} UND</div>
      <div class="el-fecha">${hoy}</div>
    </div>`;

  try {
    JsBarcode(`#${uid}`, lpn.codigo, {
      format: 'CODE128', displayValue: false, height: 55, margin: 0
    });
  } catch (_) {}

  setTimeout(() => {
    window.print();
    setTimeout(() => { area.innerHTML = ''; }, 1000);
  }, 300);
}

function empImprimirEtiquetas(bultos, meta) {
  if (!bultos?.length) return;

  const area = document.getElementById('print-area');
  if (!area) return;

  area.innerHTML = bultos.map(b => `
    <div class="etiqueta-print">
      <div class="ep-pedido">${meta.numero_pedido || ''}</div>
      <div class="ep-cliente">${meta.cliente || ''}</div>
      <div class="ep-municipio">${meta.municipio || ''}</div>
      <svg id="bc-${b.id}"></svg>
      <div class="ep-codigo">${b.codigo_barras}</div>
      <div class="ep-pieza">${b.tipo} ${b.numero} de ${b.total}</div>
    </div>`).join('');

  // Renderizar códigos de barras antes de imprimir
  bultos.forEach(b => {
    try {
      JsBarcode(`#bc-${b.id}`, b.codigo_barras, {
        format: 'CODE128', displayValue: false, height: 50, margin: 0
      });
    } catch (e) { /* JsBarcode no disponible aún */ }
  });

  setTimeout(() => {
    window.print();
    // Limpiar después de imprimir
    setTimeout(() => { area.innerHTML = ''; }, 1000);
  }, 300);
}

// ─────────────────────────────────────────────────────────────
// ADMIN — Gestión de usuarios (tab-usuarios)
// ─────────────────────────────────────────────────────────────

async function cargarUsuarios() {
  const el = document.getElementById('lista-usuarios');
  if (!el) return;
  try {
    const d = await get('/api/auth/usuarios');
    const usuarios = d.usuarios || [];
    if (!usuarios.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin usuarios</div>';
      return;
    }
    el.innerHTML = usuarios.map(u => {
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
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando usuarios</div>';
  }
}

function _formUsuario(u = {}) {
  const _TIENDA_ROLES = ['tienda', 'picker_traslado', 'packer_traslado'];
  return `
    <div style="font-size:15px;font-weight:700;margin-bottom:16px;">${u.id ? 'Editar usuario' : 'Nuevo usuario'}</div>
    <div style="display:flex;flex-direction:column;gap:12px;">
      <input id="u-nombre" placeholder="Nombre completo" value="${u.nombre || ''}"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <input id="u-email" placeholder="email@empresa.com" value="${u.email || ''}" type="email" ${u.id ? 'readonly style="opacity:0.5;"' : ''}
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <input id="u-password" placeholder="${u.id ? 'Nueva contraseña (dejar vacío para no cambiar)' : 'Contraseña'}" type="password"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <select id="u-rol" onchange="(function(v){var tr=['tienda','picker_traslado','packer_traslado'];document.getElementById('u-tienda-fields').style.display=tr.includes(v)?'block':'none';document.getElementById('u-conductor-fields').style.display=v==='conductor'?'block':'none';var canPicar=document.getElementById('u-puede-picar').checked;document.getElementById('u-conteo-wrapper').style.display=(canPicar&&!tr.includes(v))?'block':'none';})(this.value)"
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
          onchange="(function(sel){const nombres={'NC1':'Neiva Centro','NS1':'Neiva Sur Principal','NS2':'Neiva Sur Fundación','FC1':'Florencia Centro','PC1':'Pitalito Centro','PT1':'Pitalito Terminal','FF1':'Feria Florencia','FN1':'Feria Neiva','FP1':'Feria Pitalito'};document.getElementById('u-nombre-pv').value=nombres[sel.value]||'';})(this)"
          style="width:100%;padding:12px;background:#1a1a1a;border:1px solid #f59e0b;border-radius:8px;color:#fff;font-size:14px;box-sizing:border-box;">
          <option value="">— Seleccionar punto de venta —</option>
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
          <input type="checkbox" id="u-puede-picar" ${u.puede_picar!==false?'checked':''} style="width:20px;height:20px;accent-color:#60a5fa;" onchange="(function(cb){var tr=['tienda','picker_traslado','packer_traslado'];var v=document.getElementById('u-rol').value;document.getElementById('u-conteo-wrapper').style.display=(cb.checked&&!tr.includes(v))?'block':'none';})(this)">
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

function mostrarFormNuevoUsuario() {
  const f = document.getElementById('form-nuevo-usuario');
  if (!f) return;
  f.innerHTML = _formUsuario();
  f.style.display = 'block';
  f.scrollIntoView({ behavior: 'smooth' });
}

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

function ocultarFormUsuario() {
  const f = document.getElementById('form-nuevo-usuario');
  if (f) { f.style.display = 'none'; f.innerHTML = ''; }
}

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

function muelleGetOrden() {
  try { return JSON.parse(localStorage.getItem(MUELLE_ORDEN_KEY) || '[]'); }
  catch { return []; }
}

function muelleSetOrden(orden) {
  localStorage.setItem(MUELLE_ORDEN_KEY, JSON.stringify(orden));
}

function muelleOrdenarGrupos(grupos) {
  const orden = muelleGetOrden();
  // Municipios conocidos primero (en su orden guardado), los nuevos al final
  const conocidos = orden.filter(m => grupos.some(g => g.destino === m));
  const nuevos    = grupos.map(g => g.destino).filter(m => !orden.includes(m));
  const ordenFinal = [...conocidos, ...nuevos];
  // Guardar orden actualizado (incluye nuevos)
  muelleSetOrden(ordenFinal);
  return ordenFinal.map(m => grupos.find(g => g.destino === m)).filter(Boolean);
}

function muelleRenderGrupos(grupos) {
  const el = document.getElementById('lista-muelle');
  if (!el) return;

  el.innerHTML = grupos.map((g, gi) => `
    <div id="muelle-grupo-${gi}" style="margin-bottom:20px;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
        <div style="flex:1;font-size:13px;font-weight:700;color:#f59e0b;text-transform:uppercase;letter-spacing:.06em;">
          📍 ${g.destino}
          <span style="color:#555;font-weight:400;font-size:11px;">(${g.total} pieza${g.total !== 1 ? 's' : ''})</span>
        </div>
        <div style="display:flex;gap:4px;">
          ${gi > 0
            ? `<button onclick="muelleMoverGrupo(${gi},-1)" style="background:#222;border:1px solid #333;color:#fff;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;">↑</button>`
            : `<div style="width:28px;"></div>`}
          ${gi < grupos.length - 1
            ? `<button onclick="muelleMoverGrupo(${gi},1)" style="background:#222;border:1px solid #333;color:#fff;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;">↓</button>`
            : `<div style="width:28px;"></div>`}
        </div>
        <div style="font-size:11px;color:#444;min-width:40px;text-align:right;">
          Carga<br>#${gi + 1}
        </div>
      </div>
      ${g.bultos.map((b, bi) => `
        <div id="muelle-bulto-${b.id}" class="tabla-card" style="border-left:3px solid #f59e0b;margin-bottom:8px;transition:opacity .3s;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <div style="font-size:14px;font-weight:700;font-family:monospace;">${b.codigo_barras}</div>
              <div style="font-size:12px;color:#888;margin-top:2px;">${b.numero_pedido} · ${b.cliente || ''}</div>
              <div style="font-size:11px;color:#555;">${b.tipo} · pieza ${b.numero} de ${b.total}</div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:20px;color:#555;font-weight:800;">${bi + 1}</div>
              <div style="font-size:9px;color:#333;">LIFO</div>
            </div>
          </div>
        </div>`).join('')}
    </div>`).join('');
}

// Referencia a los grupos actuales para poder reordenarlos sin ir al servidor
let _MUELLE_GRUPOS_ACTUALES = [];
// Manifiesto de la ruta activa (grupos ordenados) para reordenamiento en memoria
let _RUTA_MANIFIESTO_ACTUAL = [];

function muelleMoverGrupo(idx, dir) {
  const orden = _MUELLE_GRUPOS_ACTUALES.map(g => g.destino);
  const nuevoIdx = idx + dir;
  if (nuevoIdx < 0 || nuevoIdx >= orden.length) return;
  // Intercambiar
  [orden[idx], orden[nuevoIdx]] = [orden[nuevoIdx], orden[idx]];
  muelleSetOrden(orden);
  // Re-renderizar con nuevo orden sin ir al servidor
  const reordenado = orden.map(m => _MUELLE_GRUPOS_ACTUALES.find(g => g.destino === m)).filter(Boolean);
  _MUELLE_GRUPOS_ACTUALES = reordenado;
  muelleRenderGrupos(reordenado);
}

async function cargarRutaSelector() {
  const sel = document.getElementById('muelle-ruta-select');
  if (!sel) return;
  try {
    const d = await get('/api/rutas/?estado=EN_CARGUE');
    const rutas = d.rutas || [];
    const valorActual = RUTA_ACTIVA_ID;
    sel.innerHTML = '<option value="">— Sin ruta (solo registrar) —</option>';
    rutas.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.id;
      const nombre = r.ruta_maestra_nombre || r.tipo_ruta;
      opt.textContent = `#${r.id} · ${nombre} · ${r.conductor_nombre} · ${r.total_bultos} bultos`;
      if (r.id === valorActual) opt.selected = true;
      sel.appendChild(opt);
    });
    // Si la ruta activa ya no está EN_CARGUE, resetear
    if (valorActual && !rutas.find(r => r.id === valorActual)) {
      RUTA_ACTIVA_ID = null;
      muelleSeleccionarRuta('');
    }
  } catch (e) {}
}

// ── UX móvil: campo de escaneo muelle ────────────────────────────
// En desktop el input está siempre visible y con foco (escáner USB/serial).
// En móvil mostramos un botón de "tocar para escanear" que activa el campo
// solo cuando el usuario lo pide intencionalmente, evitando el teclado fantasma.

(function initMuelleUXMobile() {
  const esMobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
  if (!esMobile) return;
  const btnActivar = document.getElementById('muelle-scan-activar');
  const campo      = document.getElementById('muelle-scan-campo');
  if (btnActivar) btnActivar.style.display = 'block';
  if (campo)      campo.style.display      = 'none';
})();

function muelleActivarScan() {
  const btnActivar = document.getElementById('muelle-scan-activar');
  const campo      = document.getElementById('muelle-scan-campo');
  const input      = document.getElementById('muelle-scan-input');
  if (btnActivar) btnActivar.style.display = 'none';
  if (campo)      campo.style.display      = 'flex';
  if (input)      input.focus();
}

async function abrirCamaraMuelle() {
  await abrirCamara('lector-qr-muelle', 'camara-box-muelle', async cod => {
    await cerrarCamara('camara-box-muelle');
    const input = document.getElementById('muelle-scan-input');
    if (input) input.value = cod.toUpperCase();
    const campo = document.getElementById('muelle-scan-campo');
    if (campo) campo.style.display = 'flex';
    await muelleCargarCaja();
  });
}

function muelleScanBlur() {
  // Al perder el foco en móvil, volvemos al botón si el campo está vacío
  const esMobile = /Mobi|Android|iPhone|iPad/i.test(navigator.userAgent);
  if (!esMobile) return;
  const input = document.getElementById('muelle-scan-input');
  if (input && input.value.trim() !== '') return; // tiene texto, no ocultar
  setTimeout(() => {
    const btnActivar = document.getElementById('muelle-scan-activar');
    const campo      = document.getElementById('muelle-scan-campo');
    if (btnActivar) btnActivar.style.display = 'block';
    if (campo)      campo.style.display      = 'none';
  }, 200); // pequeño delay para no interferir con click en ✓
}

function muelleSeleccionarRuta(idStr) {
  RUTA_ACTIVA_ID = idStr ? parseInt(idStr) : null;

  const info = document.getElementById('muelle-ruta-info');
  const scanLabel = document.getElementById('muelle-scan-label');

  if (!RUTA_ACTIVA_ID) {
    if (info) { info.style.color = '#555'; info.textContent = 'Sin ruta — los bultos no se asignarán a ningún viaje.'; }
    if (scanLabel) scanLabel.textContent = 'ESCANEAR CAJA AL CARGAR VEHÍCULO';
  } else {
    const sel = document.getElementById('muelle-ruta-select');
    const txt = sel?.options[sel.selectedIndex]?.textContent || '';
    if (info) { info.style.color = '#4ade80'; info.textContent = `Ruta activa: ${txt}`; }
    if (scanLabel) scanLabel.textContent = 'ESCANEAR PARA CONFIRMAR CARGA FÍSICA';
  }

  clearTimeout(MUELLE_TIMER);
  cargarMuelle();
}

// ── Orquestador principal ─────────────────────────────
async function cargarMuelle() {
  const el = document.getElementById('lista-muelle');
  if (!el) return;

  await cargarRutaSelector();

  const act = document.getElementById('muelle-ultima-act');
  if (act) act.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  try {
    if (RUTA_ACTIVA_ID) {
      await cargarMuelleConRuta(RUTA_ACTIVA_ID);
    } else {
      await cargarMuelleSinRuta();
    }
  } catch (e) {
    console.error('[MUELLE] Error:', e);
    el.innerHTML = `<div style="color:#ef4444;text-align:center;padding:40px;">
      Error cargando muelle<br>
      <span style="font-size:11px;color:#555;">${e.message || 'Error desconocido'}</span>
    </div>`;
  }

  clearTimeout(MUELLE_TIMER);
  if (TAB === 'tab-muelle') MUELLE_TIMER = setTimeout(cargarMuelle, 8000);
}

// ── Sin ruta seleccionada: vista informativa ──────────
async function cargarMuelleSinRuta() {
  const el = document.getElementById('lista-muelle');
  const contador = document.getElementById('muelle-contador');

  const d = await get('/api/muelle/listos');
  const grupos = d.grupos || [];
  const total = d.total_bultos || 0;

  if (contador) contador.textContent = total > 0 ? `${total} bulto${total !== 1 ? 's' : ''} sin asignar` : 'Sin pedidos pendientes';

  if (!grupos.length) {
    el.innerHTML = '<div style="color:#4ade80;text-align:center;padding:40px;font-size:32px;">✓<br><span style="font-size:14px;">Sin bultos pendientes</span></div>';
    return;
  }

  el.innerHTML = `
    <div style="background:#1a1a1a;border-radius:10px;padding:12px;margin-bottom:16px;text-align:center;font-size:12px;color:#666;">
      Selecciona una ruta arriba para empezar a planificar el cargue
    </div>
    ${grupos.map(g => `
      <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="font-size:13px;font-weight:700;color:#f59e0b;">📍 ${g.destino}
          <span style="font-size:11px;color:#555;font-weight:400;"> · ${g.total} bulto${g.total !== 1 ? 's' : ''}</span>
        </div>
        ${g.bultos.map(b => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid #1a1a1a;margin-top:6px;">
            <div>
              <span style="font-family:monospace;font-size:12px;color:#ccc;">${b.codigo_barras}</span>
              <span style="font-size:10px;color:#555;margin-left:8px;">${b.tipo} ${b.numero}/${b.total}</span>
            </div>
            <span style="font-size:10px;color:#555;">${b.numero_pedido}</span>
          </div>`).join('')}
      </div>`).join('')}`;
}

// ── Con ruta seleccionada: planificación + confirmación ─
async function cargarMuelleConRuta(rutaId) {
  const el = document.getElementById('lista-muelle');
  const contador = document.getElementById('muelle-contador');

  // Fetch paralelo: detalle de la ruta + pendientes sin asignar
  const [dRuta, dPendientes] = await Promise.all([
    get('/api/rutas/' + rutaId),
    get('/api/muelle/listos')
  ]);

  if (!dRuta.ruta) throw new Error('Ruta #' + rutaId + ' no encontrada');

  const ruta = dRuta.ruta;
  const manifiesto = ruta.manifiesto || [];           // bultos en la ruta (PENDIENTE + CARGADO)
  const gruposPendientes = dPendientes.grupos || [];  // bultos sin asignar a ninguna ruta

  // Contar confirmados vs planificados
  let totalPlan = 0, totalConf = 0;
  manifiesto.forEach(g => g.bultos.forEach(b => {
    if (b.estado === 'CARGADO') totalConf++; else totalPlan++;
  }));
  const totalEnRuta = totalPlan + totalConf;

  if (contador) {
    if (totalEnRuta === 0) {
      contador.textContent = `Ruta #${rutaId} · Sin bultos`;
    } else {
      contador.textContent = `Ruta #${rutaId} · ${totalConf}/${totalEnRuta} confirmados · ${totalPlan} pendientes`;
    }
  }

  // Aplicar orden guardado a las paradas de la ruta
  const ordenKey = 'wms_ruta_orden_' + rutaId;
  let orden;
  try { orden = JSON.parse(localStorage.getItem(ordenKey) || '[]'); } catch { orden = []; }
  const conocidos = orden.filter(dest => manifiesto.some(g => g.destino === dest));
  const nuevos = manifiesto.map(g => g.destino).filter(dest => !orden.includes(dest));
  const ordenFinal = [...conocidos, ...nuevos];
  localStorage.setItem(ordenKey, JSON.stringify(ordenFinal));
  _RUTA_MANIFIESTO_ACTUAL = ordenFinal
    .map(dest => manifiesto.find(g => g.destino === dest))
    .filter(Boolean);

  // Construir HTML completo en un solo paso
  let html = '';

  // — Sección 1: bultos ya en la ruta —
  if (_RUTA_MANIFIESTO_ACTUAL.length) {
    html += `<div style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px;">
      En esta ruta · ${totalConf} confirmado${totalConf !== 1 ? 's' : ''} · ${totalPlan} por confirmar
    </div>`;
    html += _RUTA_MANIFIESTO_ACTUAL.map((grupo, gi) =>
      _htmlGrupoRuta(grupo, gi, _RUTA_MANIFIESTO_ACTUAL.length, rutaId)
    ).join('');
  } else {
    html += `
      <div style="text-align:center;padding:20px;background:#111;border-radius:12px;border:1px dashed #333;margin-bottom:16px;">
        <div style="font-size:28px;margin-bottom:6px;">🚛</div>
        <div style="font-size:13px;font-weight:700;color:#eee;">Ruta vacía</div>
        <div style="font-size:11px;color:#555;margin-top:4px;">Asigna pedidos desde la lista de abajo</div>
      </div>`;
  }

  // — Sección 2: pendientes sin asignar —
  if (gruposPendientes.length) {
    html += `
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #222;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <span style="font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.08em;">📦 Pendientes por asignar</span>
          <span style="background:#333;color:#aaa;font-size:11px;padding:2px 10px;border-radius:10px;">${dPendientes.total_bultos}</span>
        </div>
        ${gruposPendientes.map(g => _htmlGrupoPendiente(g, rutaId)).join('')}
      </div>`;
  } else if (_RUTA_MANIFIESTO_ACTUAL.length > 0) {
    html += `
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #222;text-align:center;font-size:12px;color:#555;">
        ✓ Todos los bultos del muelle están en esta ruta
      </div>`;
  }

  el.innerHTML = html;
}

// ── Helpers de renderizado ────────────────────────────

function _htmlGrupoRuta(grupo, gi, totalGrupos, rutaId) {
  const confirmados = grupo.bultos.filter(b => b.estado === 'CARGADO').length;
  const totalGrupo = grupo.bultos.length;
  const todoConfirmado = confirmados === totalGrupo;

  return `
    <div id="ruta-grupo-${gi}" style="margin-bottom:16px;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
        <div style="flex:1;">
          <span style="font-size:13px;font-weight:700;color:${todoConfirmado ? '#4ade80' : '#eee'};text-transform:uppercase;">
            📍 ${grupo.destino}
          </span>
          <span style="font-size:11px;color:#555;"> · ${confirmados}/${totalGrupo} conf.</span>
        </div>
        <div style="display:flex;gap:4px;">
          ${gi > 0
            ? `<button onclick="rutaMoverGrupo(${gi},-1,${rutaId})" style="background:#222;border:1px solid #333;color:#fff;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;">↑</button>`
            : `<div style="width:28px;"></div>`}
          ${gi < totalGrupos - 1
            ? `<button onclick="rutaMoverGrupo(${gi},1,${rutaId})" style="background:#222;border:1px solid #333;color:#fff;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;">↓</button>`
            : `<div style="width:28px;"></div>`}
        </div>
        <div style="font-size:10px;color:#444;text-align:right;min-width:40px;">Parada<br>#${gi + 1}</div>
      </div>
      ${grupo.bultos.map(b => {
        const conf = b.estado === 'CARGADO';
        return `
          <div style="background:#111;border:1px solid ${conf ? '#14532d' : '#333'};border-left:4px solid ${conf ? '#4ade80' : '#f59e0b'};border-radius:10px;padding:10px 12px;margin-bottom:6px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <div style="flex:1;">
                <div style="font-size:13px;font-weight:700;font-family:monospace;color:${conf ? '#fff' : '#f59e0b'};">${b.codigo_barras}</div>
                <div style="font-size:11px;color:#888;margin-top:2px;">${b.numero_pedido} · ${b.cliente || ''}</div>
                <div style="font-size:10px;color:#555;">${b.tipo} · pieza ${b.numero}/${b.total}</div>
              </div>
              <div style="display:flex;align-items:center;gap:8px;">
                ${!conf ? `<button onclick="muelleDesasignar(${b.id})" title="Quitar de la ruta" style="background:none;border:none;color:#444;font-size:18px;cursor:pointer;line-height:1;padding:4px;">×</button>` : ''}
                <span style="background:${conf ? '#14532d' : '#451a03'};color:${conf ? '#4ade80' : '#f59e0b'};font-size:10px;padding:3px 10px;border-radius:20px;font-weight:700;white-space:nowrap;">
                  ${conf ? '✓ Cargado' : '⏳ Pendiente'}
                </span>
              </div>
            </div>
          </div>`;
      }).join('')}
    </div>`;
}

function _htmlGrupoPendiente(grupo, rutaId) {
  const numeroPedido = grupo.bultos[0]?.numero_pedido || '';
  return `
    <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:8px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div>
          <div style="font-size:13px;font-weight:700;color:#f59e0b;">📍 ${grupo.destino}</div>
          <div style="font-size:11px;color:#555;margin-top:2px;">${grupo.total} bulto${grupo.total !== 1 ? 's' : ''}</div>
        </div>
        <button onclick="muelleAsignar(null,'${numeroPedido}')"
          style="background:#fff;color:#000;border:none;padding:8px 14px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;">
          + Todo el pedido
        </button>
      </div>
      ${grupo.bultos.map(b => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid #1a1a1a;">
          <div>
            <span style="font-family:monospace;font-size:12px;color:#ccc;">${b.codigo_barras}</span>
            <span style="font-size:10px;color:#555;margin-left:8px;">${b.tipo} ${b.numero}/${b.total}</span>
            ${b.cliente ? `<span style="font-size:10px;color:#666;margin-left:8px;">· ${b.cliente}</span>` : ''}
          </div>
          <button onclick="muelleAsignar(${b.id},null)"
            style="background:#1a1a1a;color:#aaa;border:1px solid #333;padding:4px 10px;border-radius:6px;font-size:11px;cursor:pointer;">
            + Solo esta
          </button>
        </div>`).join('')}
    </div>`;
}

// ── Reordenar paradas ─────────────────────────────────
function rutaMoverGrupo(idx, dir, rutaId) {
  const nuevoIdx = idx + dir;
  if (nuevoIdx < 0 || nuevoIdx >= _RUTA_MANIFIESTO_ACTUAL.length) return;
  [_RUTA_MANIFIESTO_ACTUAL[idx], _RUTA_MANIFIESTO_ACTUAL[nuevoIdx]] =
    [_RUTA_MANIFIESTO_ACTUAL[nuevoIdx], _RUTA_MANIFIESTO_ACTUAL[idx]];
  localStorage.setItem('wms_ruta_orden_' + rutaId,
    JSON.stringify(_RUTA_MANIFIESTO_ACTUAL.map(g => g.destino)));
  // Re-render solo la sección de la ruta sin tocar los pendientes
  cargarMuelleConRuta(rutaId);
}

// ── Asignar / desasignar ──────────────────────────────
async function muelleAsignar(bultoId, pedidoSiesa) {
  if (!RUTA_ACTIVA_ID) {
    alerta('Selecciona una ruta primero', 'advertencia');
    return;
  }
  try {
    const payload = { ruta_id: RUTA_ACTIVA_ID };
    if (bultoId)     payload.bultos_ids  = [bultoId];
    if (pedidoSiesa) payload.pedido_siesa = pedidoSiesa;

    const r = await post('/api/muelle/asignar', payload);
    if (r.ok) {
      alerta(r.mensaje, 'exito');
      await cargarMuelleConRuta(RUTA_ACTIVA_ID);
      await cargarRutaSelector(); // actualizar contador de bultos en dropdown
    } else {
      alerta(r.error || 'Error al asignar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function muelleDesasignar(bultoId) {
  if (!confirm('¿Quitar este bulto de la ruta?')) return;
  try {
    const r = await fetch(API + '/api/muelle/desasignar/' + bultoId, {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      await cargarMuelleConRuta(RUTA_ACTIVA_ID);
      await cargarRutaSelector();
    } else {
      alerta(d.error || 'Error al desasignar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── Confirmación de carga física (scan) ───────────────
async function muelleCargarCaja() {
  const input    = document.getElementById('muelle-scan-input');
  const feedback = document.getElementById('muelle-scan-feedback');
  const codigo   = (input?.value || '').trim().toUpperCase();
  if (!codigo) return;

  if (!RUTA_ACTIVA_ID) {
    feedback.style.color = '#f59e0b';
    feedback.textContent = '⚠ Selecciona una ruta antes de escanear';
    return;
  }

  feedback.style.color = '#888';
  feedback.textContent = 'Verificando...';

  try {
    const r = await fetch(API + '/api/muelle/cargar/' + encodeURIComponent(codigo), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ ruta_id: RUTA_ACTIVA_ID }),
    });
    const d = await r.json();

    if (r.ok) {
      if (d.ya_cargado) {
        feedback.style.color = '#60a5fa';
        feedback.textContent = `ℹ Ya estaba cargado: ${d.codigo_barras}`;
      } else {
        feedback.style.color = '#4ade80';
        const rutaLista = d.pedido_completo_en_ruta ? ' · ✓ Pedido completo en ruta' : ` · ${d.bultos_pendientes_pedido_ruta} bulto${d.bultos_pendientes_pedido_ruta !== 1 ? 's' : ''} pendientes en pedido`;
        feedback.textContent = `✓ ${d.codigo_barras} · ${d.tipo} ${d.numero}/${d.total}${rutaLista}`;
        if (navigator.vibrate) navigator.vibrate(50);
      }
      input.value = '';
      // En móvil, después de escanear volvemos al botón para no dejar el teclado abierto
      if (/Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
        input.blur();
        const btnActivar = document.getElementById('muelle-scan-activar');
        const campo      = document.getElementById('muelle-scan-campo');
        if (btnActivar) btnActivar.style.display = 'block';
        if (campo)      campo.style.display      = 'none';
      }
      await cargarMuelleConRuta(RUTA_ACTIVA_ID);
      await cargarRutaSelector();
    } else {
      feedback.style.color = '#ef4444';
      feedback.textContent = d.error || 'Error de verificación';
      if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
    }
  } catch (e) {
    feedback.style.color = '#ef4444';
    feedback.textContent = 'Error de conexión';
  }
  // Solo re-enfocar en desktop (donde hay escáner físico con cable)
  if (!/Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) input?.focus();
}

// ══════════════════════════════════════════════════════
//  MILLA CERO — RUTAS DE DESPACHO
// ══════════════════════════════════════════════════════

function rutasSubTab(nombre) {
  RUTAS_SUBTAB = nombre;
  const paneles = {
    rutas:       'rutas-panel-rutas',
    maestras:    'rutas-panel-maestras',
    vehiculos:   'rutas-panel-vehiculos',
    conductores: 'rutas-panel-conductores',
  };
  Object.entries(paneles).forEach(([k, id]) => {
    const el = document.getElementById(id);
    if (el) el.style.display = k === nombre ? 'block' : 'none';
  });
  ['rutas','maestras','vehiculos','conductores'].forEach(k => {
    const btn = document.getElementById('rutas-subnav-' + k);
    if (btn) {
      btn.style.background = k === nombre ? '#1E8395' : 'none';
      btn.style.color      = k === nombre ? '#fff' : '#415A70';
    }
  });
  cargarRutas();
}

async function cargarRutas() {
  if      (RUTAS_SUBTAB === 'rutas')       await cargarListaRutas();
  else if (RUTAS_SUBTAB === 'maestras')    await cargarListaMaestras();
  else if (RUTAS_SUBTAB === 'vehiculos')   await cargarListaVehiculos();
  else                                     await cargarListaConductores();
}

// ── Rutas ────────────────────────────────────────────

async function cargarListaRutas() {
  const el = document.getElementById('lista-rutas');
  if (!el) return;
  try {
    const d = await get('/api/rutas/');
    const rutas = d.rutas || [];
    if (!rutas.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin rutas registradas hoy</div>';
      return;
    }
    el.innerHTML = rutas.map(r => rutaCard(r)).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando rutas</div>';
  }
}

function rutaCard(r) {
  const estadoBadge = {
    PROGRAMADO:  '<span style="background:#2d1b69;color:#a78bfa;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:700;">PROGRAMADO</span>',
    EN_CARGUE:   '<span style="background:#713f12;color:#facc15;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:700;">EN CARGUE</span>',
    EN_TRANSITO: '<span style="background:#1e3a5f;color:#60a5fa;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:700;">EN TRÁNSITO</span>',
    ENTREGADA:   '<span style="background:#14532d;color:#4ade80;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:700;">ENTREGADA</span>',
  }[r.estado] || r.estado;

  const tipoIcon = r.tipo_ruta === 'Urbana' ? '🏙️' : '🛣️';
  const fechaRef = r.fecha_programada
    ? new Date(r.fecha_programada + 'T00:00:00').toLocaleDateString('es-CO', { weekday:'short', month:'short', day:'numeric' })
    : new Date(r.fecha_creacion).toLocaleString('es-CO', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });

  const btnIniciar = r.estado === 'PROGRAMADO'
    ? `<button onclick="rutaIniciar(${r.id})" style="flex:1;padding:10px;background:#2d1b69;color:#a78bfa;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">▶ Iniciar Cargue</button>`
    : '';
  const btnCerrar = r.estado === 'EN_CARGUE'
    ? `<button onclick="rutaCerrar(${r.id})" style="flex:1;padding:10px;background:#1e3a5f;color:#60a5fa;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">🚛 Salió</button>`
    : '';
  // EN_TRANSITO: estado informativo — solo el conductor marca como entregada desde su app
  const btnEntregar = r.estado === 'EN_TRANSITO'
    ? `<div style="flex:1;padding:10px;background:#1e3a5f22;color:#60a5fa;border:1px solid #1e3a5f;border-radius:8px;font-size:13px;font-weight:700;text-align:center;pointer-events:none;">🚛 En camino</div>`
    : '';
  const btnManifiesto = r.total_bultos > 0
    ? `<button onclick="rutaVerManifiesto(${r.id})" style="flex:1;padding:10px;background:#1a1a1a;color:#aaa;border:1px solid #333;border-radius:8px;font-size:13px;cursor:pointer;">📋 Ver</button>`
    : '';
  const btnPlanilla = ['EN_TRANSITO','ENTREGADA'].includes(r.estado)
    ? `<button onclick="rutaVerPlanilla(${r.id})" style="flex:1;padding:10px;background:#1a1a2a;color:#a78bfa;border:1px solid #2d1b69;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">💰 Planilla${r.estado_financiero === 'LIQUIDADA' ? ' ✓' : ''}</button>`
    : '';
  const btnForzarCierre = r.estado === 'EN_TRANSITO'
    ? `<button onclick="rutaForzarCierre(${r.id})" style="flex:1;padding:10px;background:#110a00;color:#f59e0b;border:1px solid #78350f;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">⚡ Forzar cierre</button>`
    : '';

  return `
    <div id="ruta-card-${r.id}" style="background:#111;border:1px solid ${r.estado === 'PROGRAMADO' ? '#2d1b69' : '#222'};border-radius:14px;padding:16px;margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:16px;font-weight:800;">${tipoIcon} ${r.ruta_maestra_nombre || 'Ruta'} <span style="color:#555;font-weight:400;font-size:13px;">#${r.id}</span></div>
          <div style="font-size:13px;color:#ccc;margin-top:2px;">${r.conductor_nombre}</div>
          <div style="font-size:11px;color:#555;margin-top:1px;">${r.vehiculo_placa ? r.vehiculo_placa + ' · ' + r.vehiculo_tipo + ' · ' : ''}${fechaRef}</div>
        </div>
        <div style="text-align:right;">
          ${estadoBadge}
          <div style="font-size:20px;font-weight:800;margin-top:6px;">${r.total_bultos}</div>
          <div style="font-size:10px;color:#555;">
            ${r.total_confirmados > 0 || r.total_planificados > 0
              ? `${r.total_confirmados} conf · ${r.total_planificados} plan`
              : 'bultos'}
          </div>
        </div>
      </div>
      ${r.pedidos?.length ? `<div style="font-size:11px;color:#555;margin-bottom:10px;">Pedidos: ${r.pedidos.join(', ')}</div>` : ''}
      ${r.notas ? `<div style="font-size:12px;color:#666;font-style:italic;margin-bottom:10px;">"${r.notas}"</div>` : ''}
      ${(btnIniciar || btnCerrar || btnEntregar || btnManifiesto || btnPlanilla || btnForzarCierre)
        ? `<div style="display:flex;gap:6px;flex-wrap:wrap;">${btnIniciar}${btnCerrar}${btnEntregar}${btnManifiesto}${btnPlanilla}${btnForzarCierre}</div>`
        : ''}
    </div>`;
}

async function rutaIniciar(id) {
  try {
    const r = await fetch(API + '/api/rutas/' + id + '/iniciar', {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      const card = document.getElementById('ruta-card-' + id);
      if (card) card.outerHTML = rutaCard(d.ruta);
      await cargarRutaSelector();
      const infoSug = d.sugeridos_count > 0
        ? ` · ${d.sugeridos_count} bulto${d.sugeridos_count !== 1 ? 's' : ''} disponibles para asignar`
        : '';
      alerta(`Cargue iniciado${infoSug}. Asigna los bultos manualmente en el muelle.`, 'exito');
      // Siempre redirigir al muelle con la ruta activa pre-seleccionada
      setTimeout(() => {
        tab('tab-muelle');
        RUTA_ACTIVA_ID = id;
        const sel = document.getElementById('muelle-ruta-select');
        if (sel) sel.value = id;
        muelleSeleccionarRuta(String(id));
      }, 800);
    } else { alert(d.error || 'Error al iniciar ruta'); }
  } catch (e) { alert('Error de conexión'); }
}

async function rutaCerrar(id) {
  if (!confirm(`¿Confirmar que la Ruta #${id} salió? Ya no se podrán agregar bultos.`)) return;
  try {
    const r = await fetch(API + '/api/rutas/' + id + '/cerrar', { method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN } });
    const d = await r.json();
    if (r.ok) {
      // Actualizar card sin recargar todo
      const card = document.getElementById('ruta-card-' + id);
      if (card) card.outerHTML = rutaCard(d.ruta);
      // Limpiar ruta activa si era esta
      if (RUTA_ACTIVA_ID === id) { RUTA_ACTIVA_ID = null; }
      await cargarRutaSelector();
    } else { alert(d.error || 'Error al cerrar ruta'); }
  } catch (e) { alert('Error de conexión'); }
}

// ── Entrega por bulto ────────────────────────────────────────

let _ENTREGA_RUTA_ID = null;
let _ENTREGA_BULTOS  = [];   // [{ id, codigo_barras, tipo, numero, total, cliente, numero_pedido, entregado, motivo_rechazo }]

const MOTIVOS_RECHAZO = ['Cliente rechazó', 'Dirección incorrecta', 'Mercancía averiada', 'No había nadie', 'Pedido duplicado'];

async function rutaEntregar(id) {
  try {
    const d = await get('/api/rutas/' + id);
    const ruta = d.ruta;
    const bultos = (ruta.manifiesto || []).flatMap(g => g.bultos || []);

    if (!bultos.length) {
      // Sin bultos asignados — cierre directo (ruta sin bultos escaneados)
      if (!confirm(`¿Confirmar entrega de Ruta #${id}?\nNo tiene bultos registrados.`)) return;
      _enviarConfirmacionEntrega(id, []);
      return;
    }

    _ENTREGA_RUTA_ID = id;
    _ENTREGA_BULTOS  = bultos.map(b => ({ ...b, entregado: true, motivo_rechazo: MOTIVOS_RECHAZO[0] }));

    const modal = document.getElementById('modal-entrega');
    document.getElementById('modal-entrega-sub').textContent =
      `Ruta #${id} · ${ruta.conductor_nombre} · ${bultos.length} bulto${bultos.length !== 1 ? 's' : ''}`;
    _renderEntregaLista();
    modal.style.display = 'flex';
  } catch (e) { alerta('Error cargando bultos de la ruta', 'error'); }
}

function _renderEntregaLista() {
  const el = document.getElementById('modal-entrega-lista');
  const rechazados = _ENTREGA_BULTOS.filter(b => !b.entregado).length;
  document.getElementById('modal-entrega-resumen').innerHTML =
    rechazados > 0
      ? `<span style="color:#ef4444;font-weight:700;">${rechazados} bulto${rechazados !== 1 ? 's' : ''} marcado${rechazados !== 1 ? 's' : ''} como rechazado${rechazados !== 1 ? 's' : ''}</span> — aparecerán en Devoluciones`
      : `<span style="color:#4ade80;">Todos los bultos entregados</span>`;

  el.innerHTML = _ENTREGA_BULTOS.map((b, i) => `
    <div style="background:${b.entregado ? '#0d1a0d' : '#1a0d0d'};border:1px solid ${b.entregado ? '#166534' : '#7f1d1d'};border-radius:10px;padding:12px;margin-bottom:8px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:13px;font-weight:700;color:#fff;">${b.codigo_barras}</div>
          <div style="font-size:11px;color:#666;margin-top:2px;">${b.tipo} ${b.numero}/${b.total} · ${b.numero_pedido} · ${b.cliente || '—'}</div>
          ${!b.entregado ? `<select onchange="_setMotivo(${i}, this.value)"
            style="margin-top:8px;width:100%;padding:6px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:6px;font-size:12px;">
            ${MOTIVOS_RECHAZO.map(m => `<option value="${m}" ${b.motivo_rechazo===m?'selected':''}>${m}</option>`).join('')}
          </select>` : ''}
        </div>
        <button onclick="_toggleEntrega(${i})"
          style="flex-shrink:0;padding:8px 14px;background:${b.entregado ? '#166534' : '#7f1d1d'};color:${b.entregado ? '#4ade80' : '#f87171'};border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;">
          ${b.entregado ? '✓ Entregado' : '✗ Rechazado'}
        </button>
      </div>
    </div>`).join('');
}

function _toggleEntrega(i) {
  _ENTREGA_BULTOS[i].entregado = !_ENTREGA_BULTOS[i].entregado;
  _renderEntregaLista();
}

function _setMotivo(i, motivo) {
  _ENTREGA_BULTOS[i].motivo_rechazo = motivo;
}

function cerrarModalEntrega() {
  document.getElementById('modal-entrega').style.display = 'none';
  _ENTREGA_RUTA_ID = null;
  _ENTREGA_BULTOS  = [];
}

async function confirmarEntregaFinal() {
  if (!_ENTREGA_RUTA_ID) return;
  const btn = document.getElementById('btn-confirmar-entrega');
  btn.disabled = true;
  btn.textContent = 'Guardando...';

  const payload = _ENTREGA_BULTOS.map(b => ({
    id:             b.id,
    entregado:      b.entregado,
    motivo_rechazo: b.entregado ? null : b.motivo_rechazo
  }));

  await _enviarConfirmacionEntrega(_ENTREGA_RUTA_ID, payload);
  cerrarModalEntrega();
}

async function _enviarConfirmacionEntrega(id, payload) {
  try {
    const r = await fetch(API + '/api/rutas/' + id + '/entregar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ bultos: payload })
    });
    const d = await r.json();
    if (r.ok) {
      const rechazados = d.rechazados || 0;
      if (rechazados > 0) {
        alerta(`Ruta entregada · ${rechazados} bulto${rechazados !== 1 ? 's' : ''} rechazado${rechazados !== 1 ? 's' : ''} → Devoluciones`, 'advertencia');
      } else {
        alerta('Ruta marcada como entregada', 'exito');
      }
      const card = document.getElementById('ruta-card-' + id);
      if (card) card.outerHTML = rutaCard(d.ruta);
    } else {
      alerta(d.error || 'Error al confirmar entrega', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function rutaVerManifiesto(id) {
  try {
    const [dr, dp] = await Promise.all([
      get('/api/rutas/' + id),
      get('/api/rutas/' + id + '/planilla').catch(() => ({ paradas: [] })),
    ]);
    const ruta    = dr.ruta;
    const paradas = dp.paradas || [];

    // Paleta modo día
    const EST = {
      ENTREGADO: { border: '#16a34a', bg: '#f0fdf4', badge: '#15803d', badgeBg: '#dcfce7', label: '✓ Entregado' },
      PARCIAL:   { border: '#d97706', bg: '#fffbeb', badge: '#b45309', badgeBg: '#fef3c7', label: '⚠ Parcial'   },
      RECHAZADO: { border: '#dc2626', bg: '#fef2f2', badge: '#b91c1c', badgeBg: '#fee2e2', label: '✗ Rechazado' },
    };
    const EST_DEF = { border: '#d1d5db', bg: '#f9fafb', badge: '#6b7280', badgeBg: '#f3f4f6', label: 'Sin gestionar' };

    let filas = '';
    if (paradas.length) {
      paradas.forEach(p => {
        const r   = p.recaudo;
        const est = r ? r.estado_entrega : null;
        const e   = est ? EST[est] : EST_DEF;

        filas += `<div style="background:${e.bg};border:1px solid ${e.border};border-radius:10px;padding:12px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div>
              <div style="font-size:13px;font-weight:800;color:#111827;">${p.numero_pedido}</div>
              <div style="font-size:11px;color:#6b7280;">${p.cliente} · 📍 ${p.municipio}</div>
            </div>
            <span style="font-size:11px;font-weight:700;color:${e.badge};background:${e.badgeBg};padding:3px 10px;border-radius:8px;">${e.label}</span>
          </div>`;

        // Bultos
        const rechazadosIds = new Set(r ? (r.bultos_rechazados_ids || []) : []);
        filas += `<div style="font-size:11px;margin-bottom:${est && est !== 'ENTREGADO' ? 6 : 0}px;">`;
        (p.bultos_detalle || []).forEach(b => {
          const rechazado = rechazadosIds.has(b.id);
          filas += `<span style="color:${rechazado ? '#b91c1c' : '#15803d'};margin-right:8px;">
            ${rechazado ? '✗' : '✓'} ${b.codigo_barras} (${b.tipo} ${b.numero}/${b.total})</span>`;
        });
        filas += '</div>';

        // Detalle PARCIAL
        if (est === 'PARCIAL' && r.items_entregados && r.items_entregados.length) {
          filas += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid #fde68a;">
            <div style="font-size:10px;color:#92400e;font-weight:700;margin-bottom:4px;">DETALLE PARCIAL</div>
            <div style="display:grid;grid-template-columns:1fr auto auto;gap:3px 10px;font-size:11px;">
              ${r.items_entregados.map(it => `
                <div style="color:#374151;">${it.nombre || it.codigo}</div>
                <div style="color:#15803d;text-align:right;font-weight:700;">✓ ${it.cantidad_entregada}</div>
                <div style="color:${it.cantidad_devuelta > 0 ? '#b91c1c' : '#9ca3af'};text-align:right;font-weight:700;">↩ ${it.cantidad_devuelta}</div>
              `).join('')}
            </div>
          </div>`;
        }

        // Motivo + evidencia RECHAZADO
        if (est === 'RECHAZADO') {
          if (r.observaciones) {
            filas += `<div style="margin-top:6px;font-size:11px;color:#b91c1c;font-style:italic;">"${r.observaciones}"</div>`;
          }
          if (r.foto_entrega) {
            filas += `<button onclick="(function(){const w=window.open();w.document.write('<img src=\\'data:image/jpeg;base64,${r.foto_entrega}\\' style=\\'max-width:100%;\\'>');w.document.title='Evidencia ${p.numero_pedido}';})()"
              style="margin-top:8px;padding:6px 14px;background:#fee2e2;color:#b91c1c;border:1px solid #dc2626;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
              📷 Ver evidencia fotográfica
            </button>`;
          }
        }

        filas += '</div>';
      });
    } else {
      filas = '<div style="color:#9ca3af;text-align:center;padding:20px;">Sin paradas registradas</div>';
    }

    const modal = document.createElement('div');
    modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px;';
    modal.innerHTML = `
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:20px;max-width:560px;width:100%;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 20px 40px rgba(0,0,0,.15);">
        <div style="font-size:16px;font-weight:800;color:#111827;margin-bottom:4px;">${ruta.ruta_maestra_nombre || 'Ruta'} <span style="color:#9ca3af;font-weight:400;font-size:13px;">#${ruta.id}</span></div>
        <div style="font-size:12px;color:#6b7280;margin-bottom:14px;">${ruta.conductor_nombre} · ${ruta.tipo_ruta} · ${paradas.length} pedido${paradas.length !== 1 ? 's' : ''}</div>
        <div style="overflow-y:auto;flex:1;">${filas}</div>
        <button onclick="this.closest('div[style*=fixed]').remove()" style="margin-top:16px;padding:10px;background:#f3f4f6;color:#374151;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;cursor:pointer;width:100%;font-weight:600;">Cerrar</button>
      </div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  } catch (e) { alerta('Error cargando manifiesto', 'error'); }
}

function rutasMostrarForm() {
  document.getElementById('rutas-form').style.display = 'block';
  document.getElementById('rutas-form-error').textContent = '';
  // Fecha por defecto = hoy
  const fechaEl = document.getElementById('rutas-form-fecha');
  if (fechaEl && !fechaEl.value) fechaEl.value = new Date().toISOString().slice(0, 10);
  cargarListaMaestrasEnSelect('rutas-form-maestra');
  cargarListaConductoresEnSelect('rutas-form-conductor');
  cargarListaVehiculosEnSelect('rutas-form-vehiculo');
}

function rutasCancelarForm() {
  document.getElementById('rutas-form').style.display = 'none';
}

function rutasSeleccionarTipo(tipo) {
  RUTAS_TIPO_SEL = tipo;
  const isLight = document.body.classList.contains('light');
  const ON  = isLight ? { bg:'#0d9488', color:'#fff',    border:'#0d9488' }
                      : { bg:'#0C3535', color:'#25BBBB', border:'#174848' };
  const OFF = isLight ? { bg:'#f1f5f9', color:'#64748b', border:'#cbd5e1' }
                      : { bg:'#0D1622', color:'#415A70', border:'#1C2B3A' };
  const apply = (btn, active) => {
    const s = active ? ON : OFF;
    btn.style.background  = s.bg;
    btn.style.color       = s.color;
    btn.style.borderColor = s.border;
  };
  const btnU = document.getElementById('rutas-tipo-urbana');
  const btnM = document.getElementById('rutas-tipo-municipal');
  if (btnU) apply(btnU, tipo === 'Urbana');
  if (btnM) apply(btnM, tipo === 'Municipal');
}

async function rutasProgramar() {
  const errorEl    = document.getElementById('rutas-form-error');
  const maestraId  = document.getElementById('rutas-form-maestra')?.value;
  const conductorId = document.getElementById('rutas-form-conductor')?.value;
  const vehiculoId  = document.getElementById('rutas-form-vehiculo')?.value;
  const fecha      = document.getElementById('rutas-form-fecha')?.value;
  const notas      = document.getElementById('rutas-form-notas')?.value.trim();
  errorEl.textContent = '';

  if (!maestraId)   { errorEl.textContent = 'Selecciona una ruta maestra'; return; }
  if (!conductorId) { errorEl.textContent = 'Selecciona un conductor'; return; }
  if (!vehiculoId)  { errorEl.textContent = 'Selecciona un vehículo'; return; }
  if (!fecha)       { errorEl.textContent = 'Selecciona la fecha de despacho'; return; }

  try {
    const r = await fetch(API + '/api/rutas/programar', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ruta_maestra_id: parseInt(maestraId),
        conductor_id:    parseInt(conductorId),
        vehiculo_id:     parseInt(vehiculoId),
        fecha_programada: fecha,
        notas,
      }),
    });
    const d = await r.json();
    if (r.ok) {
      rutasCancelarForm();
      document.getElementById('rutas-form-notas').value = '';
      await cargarListaRutas();
      await cargarRutaSelector();
    } else {
      errorEl.textContent = d.error || 'Error al programar ruta';
    }
  } catch (e) { errorEl.textContent = 'Error de conexión'; }
}

// ── Rutas Maestras ───────────────────────────────────

async function cargarListaMaestrasEnSelect(selectId) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  try {
    const d = await get('/api/rutas/maestras?activas=true');
    sel.innerHTML = '<option value="">— Selecciona ruta maestra —</option>';
    (d.maestras || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `${m.nombre} · ${m.tipo_ruta}`;
      sel.appendChild(opt);
    });
  } catch (e) {}
}

async function cargarListaMaestras() {
  const el = document.getElementById('lista-maestras');
  if (!el) return;
  try {
    const d = await get('/api/rutas/maestras?activas=false');
    const maestras = d.maestras || [];
    if (!maestras.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin rutas maestras. Crea la primera con el botón +</div>';
      return;
    }
    el.innerHTML = maestras.map(m => `
      <div style="background:#111;border:1px solid ${m.activa ? '#222' : '#1a1a1a'};border-radius:12px;padding:14px;margin-bottom:8px;opacity:${m.activa ? '1' : '0.5'};">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
          <div>
            <div style="font-size:15px;font-weight:800;">${m.tipo_ruta === 'Urbana' ? '🏙️' : '🛣️'} ${m.nombre}</div>
            <div style="font-size:11px;color:#555;margin-top:2px;">${m.tipo_ruta} · ${(m.paradas || []).length} parada${(m.paradas || []).length !== 1 ? 's' : ''}</div>
          </div>
          <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;justify-content:flex-end;">
            ${m.activa
              ? '<span style="background:#14532d;color:#4ade80;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;">ACTIVA</span>'
              : '<span style="background:#3f1515;color:#f87171;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;">INACTIVA</span>'}
            <button onclick="maestraEditar(${m.id})"
              style="padding:5px 10px;background:#1a1a2a;border:1px solid #2d1b69;color:#a78bfa;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;">
              ✏ Editar
            </button>
            <button onclick="maestraToggle(${m.id},${!m.activa})"
              style="padding:5px 10px;background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:6px;font-size:11px;cursor:pointer;">
              ${m.activa ? 'Desactivar' : 'Activar'}
            </button>
            <button onclick="maestraEliminar(${m.id},${JSON.stringify(m.nombre)})"
              style="padding:5px 10px;background:#1a0000;border:1px solid #5c1a1a;color:#f87171;border-radius:6px;font-size:11px;cursor:pointer;">
              🗑
            </button>
          </div>
        </div>
        ${(m.paradas || []).length ? `
          <div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:6px;">
            ${m.paradas.map((p, i) => `
              <span style="background:#1a1a1a;border:1px solid #333;border-radius:20px;padding:3px 10px;font-size:11px;color:#aaa;">
                ${i + 1}. ${p.municipio}
              </span>`).join('')}
          </div>` : ''}
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;">Error cargando rutas maestras</div>';
  }
}

// Paradas dinámicas en el form
let _MAESTRAS_PARADAS = [];
let _MUNICIPIOS_CACHE = [];

let _municipiosPromise = null;

function _cargarMunicipios() {
  if (_MUNICIPIOS_CACHE.length) return Promise.resolve();
  if (_municipiosPromise) return _municipiosPromise;
  _municipiosPromise = get('/api/rutas/municipios')
    .then(d => { _MUNICIPIOS_CACHE = d.municipios || []; })
    .catch(e => { console.error('[WMS] municipios:', e); _municipiosPromise = null; });
  return _municipiosPromise;
}

function maestraInputParada(input) {
  const q = (input.value || '').trim().toLowerCase();
  const el = document.getElementById('maestras-sugerencias');
  if (!el) return;
  if (!q) { el.style.display = 'none'; return; }
  if (!_MUNICIPIOS_CACHE.length) {
    el.innerHTML = '<div style="padding:12px;font-size:13px;color:var(--tx2);text-align:center;">Cargando municipios…</div>';
    el.style.display = 'block';
    _cargarMunicipios().then(() => {
      const inp = document.getElementById('maestras-parada-input');
      if (!inp || !inp.value.trim()) { el.style.display = 'none'; return; }
      if (!_MUNICIPIOS_CACHE.length) {
        el.innerHTML = '<div style="padding:12px;font-size:13px;color:var(--red);text-align:center;">Error cargando municipios</div>';
        return;
      }
      maestraInputParada(inp);
    });
    return;
  }
  const matches = _MUNICIPIOS_CACHE.filter(m => m.toLowerCase().includes(q)).slice(0, 50);
  if (!matches.length) { el.style.display = 'none'; return; }
  el.innerHTML = matches.map(m =>
    `<div onmousedown="maestraSeleccionarMunicipio(this)" data-municipio="${m.replace(/"/g, '&quot;')}"
      style="padding:9px 12px;cursor:pointer;font-size:13px;color:var(--tx);border-bottom:1px solid var(--brd);"
      onmouseover="this.style.background='var(--bg-s2)'" onmouseout="this.style.background=''">${m}</div>`
  ).join('');
  el.style.display = 'block';
}

function maestraSeleccionarMunicipio(el) {
  const inp = document.getElementById('maestras-parada-input');
  if (inp) inp.value = el.dataset.municipio;
  const drop = document.getElementById('maestras-sugerencias');
  if (drop) drop.style.display = 'none';
}

function maestraOcultarSugerencias() {
  setTimeout(() => {
    const el = document.getElementById('maestras-sugerencias');
    if (el) el.style.display = 'none';
  }, 150);
}

function maestraInputKeydown(event) {
  if (event.key === 'Enter') { event.preventDefault(); maestraAgregarParada(); }
  if (event.key === 'Escape') {
    const el = document.getElementById('maestras-sugerencias');
    if (el) el.style.display = 'none';
  }
}

function maestraMostrarForm() {
  _MAESTRAS_PARADAS = [];
  document.getElementById('maestras-form-id').value = '';
  document.getElementById('maestras-form-titulo').textContent = 'Nueva ruta maestra';
  document.getElementById('maestra-form-nombre').value = '';
  document.getElementById('maestras-form-error').textContent = '';
  rutasSeleccionarTipo('Urbana');
  document.getElementById('maestras-form').style.display = 'block';
  _maestraRenderParadas();
  _cargarMunicipios();
}

function maestraCancelarForm() {
  document.getElementById('maestras-form').style.display = 'none';
  document.getElementById('maestras-form-id').value = '';
  const el = document.getElementById('maestras-sugerencias');
  if (el) el.style.display = 'none';
}

async function maestraEditar(id) {
  try {
    const d = await get('/api/rutas/maestras/' + id);
    const m = d.maestra;
    if (!m) { alerta('Maestra no encontrada', 'error'); return; }
    _MAESTRAS_PARADAS = (m.paradas || []).sort((a, b) => a.orden - b.orden).map(p => p.municipio);
    document.getElementById('maestras-form-id').value = id;
    document.getElementById('maestras-form-titulo').textContent = 'Editar ruta maestra';
    document.getElementById('maestra-form-nombre').value = m.nombre;
    document.getElementById('maestras-form-error').textContent = '';
    rutasSeleccionarTipo(m.tipo_ruta);
    document.getElementById('maestras-form').style.display = 'block';
    _maestraRenderParadas();
    _cargarMunicipios();
    document.getElementById('maestras-form').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) { alerta('Error cargando la ruta', 'error'); }
}

function maestraAgregarParada() {
  const val = document.getElementById('maestras-parada-input')?.value.trim();
  if (!val) return;
  _MAESTRAS_PARADAS.push(val);
  document.getElementById('maestras-parada-input').value = '';
  const el = document.getElementById('maestras-sugerencias');
  if (el) el.style.display = 'none';
  _maestraRenderParadas();
}

function maestraQuitarParada(idx) {
  _MAESTRAS_PARADAS.splice(idx, 1);
  _maestraRenderParadas();
}

function maestraMoverParada(idx, dir) {
  const nuevoIdx = idx + dir;
  if (nuevoIdx < 0 || nuevoIdx >= _MAESTRAS_PARADAS.length) return;
  [_MAESTRAS_PARADAS[idx], _MAESTRAS_PARADAS[nuevoIdx]] =
    [_MAESTRAS_PARADAS[nuevoIdx], _MAESTRAS_PARADAS[idx]];
  _maestraRenderParadas();
}

function _maestraRenderParadas() {
  const el = document.getElementById('maestras-paradas-lista');
  if (!el) return;
  if (!_MAESTRAS_PARADAS.length) {
    el.innerHTML = '<div style="color:#444;font-size:12px;text-align:center;padding:10px;">Agrega las paradas en orden de entrega (1ª = primera entrega)</div>';
    return;
  }
  el.innerHTML = _MAESTRAS_PARADAS.map((m, i) => `
    <div style="display:flex;align-items:center;gap:6px;padding:7px 0;border-bottom:1px solid #1a1a1a;">
      <span style="background:#1a1a1a;color:#666;border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:11px;flex-shrink:0;">${i+1}</span>
      <span style="flex:1;font-size:13px;">${m}</span>
      <button onclick="maestraMoverParada(${i},-1)" ${i===0?'disabled':''} style="background:none;border:none;color:#555;cursor:pointer;font-size:14px;padding:2px 4px;">↑</button>
      <button onclick="maestraMoverParada(${i},1)" ${i===_MAESTRAS_PARADAS.length-1?'disabled':''} style="background:none;border:none;color:#555;cursor:pointer;font-size:14px;padding:2px 4px;">↓</button>
      <button onclick="maestraQuitarParada(${i})" style="background:none;border:none;color:#444;cursor:pointer;font-size:18px;padding:2px 4px;">×</button>
    </div>`).join('');
}

async function maestrasGuardar() {
  const errorEl = document.getElementById('maestras-form-error');
  const nombre  = document.getElementById('maestra-form-nombre')?.value.trim();
  const editId  = document.getElementById('maestras-form-id')?.value;
  errorEl.textContent = '';

  if (!nombre) { errorEl.textContent = 'El nombre es requerido'; return; }
  if (!_MAESTRAS_PARADAS.length) { errorEl.textContent = 'Agrega al menos una parada'; return; }

  try {
    const url    = editId ? (API + '/api/rutas/maestras/' + editId) : (API + '/api/rutas/maestras');
    const method = editId ? 'PUT' : 'POST';
    const r = await fetch(url, {
      method,
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ nombre, tipo_ruta: RUTAS_TIPO_SEL, paradas: _MAESTRAS_PARADAS }),
    });
    const d = await r.json();
    if (r.ok) {
      maestraCancelarForm();
      _MAESTRAS_PARADAS = [];
      await cargarListaMaestras();
      alerta(editId ? 'Ruta actualizada' : 'Ruta creada', 'exito');
    } else {
      errorEl.textContent = d.error || 'Error al guardar';
    }
  } catch (e) { errorEl.textContent = 'Error de conexión'; }
}

async function maestraToggle(id, activar) {
  try {
    const r = await fetch(API + '/api/rutas/maestras/' + id, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ activa: activar }),
    });
    if (r.ok) {
      await cargarListaMaestras();
    } else {
      const d = await r.json();
      alerta(d.error || 'Error al cambiar estado', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function maestraEliminar(id, nombre) {
  if (!confirm(`¿Eliminar la ruta "${nombre}"?\n\nEsta acción no se puede deshacer. Si tiene viajes asociados no se podrá eliminar.`)) return;
  try {
    const r = await fetch(API + '/api/rutas/maestras/' + id, {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (r.ok) {
      await cargarListaMaestras();
      alerta('Ruta eliminada', 'exito');
    } else {
      alerta(d.error || 'No se pudo eliminar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── Vehículos ────────────────────────────────────────

async function cargarListaVehiculosEnSelect(selectId) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  try {
    const d = await get('/api/rutas/vehiculos?activos=true');
    sel.innerHTML = '<option value="">— Selecciona vehículo —</option>';
    (d.vehiculos || []).forEach(v => {
      const opt = document.createElement('option');
      opt.value = v.id;
      opt.textContent = `${v.placa} · ${v.tipo}${v.capacidad_kg ? ' · ' + v.capacidad_kg + ' kg' : ''}`;
      sel.appendChild(opt);
    });
  } catch (e) {}
}

async function cargarListaVehiculos() {
  const el = document.getElementById('lista-vehiculos');
  if (!el) return;
  try {
    const d = await get('/api/rutas/vehiculos?activos=false');
    const vehiculos = d.vehiculos || [];
    if (!vehiculos.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin vehículos registrados</div>';
      return;
    }
    el.innerHTML = vehiculos.map(v => `
      <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center;">
        <div>
          <div style="font-size:15px;font-weight:800;font-family:monospace;">${v.placa}</div>
          <div style="font-size:12px;color:#888;margin-top:2px;">${v.tipo}${v.capacidad_kg ? ' · ' + v.capacidad_kg + ' kg' : ''}</div>
        </div>
        <div style="display:flex;gap:6px;align-items:center;">
          ${v.activo
            ? '<span style="background:#14532d;color:#4ade80;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;">ACTIVO</span>'
            : '<span style="background:#3f1515;color:#f87171;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;">INACTIVO</span>'}
          <button onclick="vehiculoToggle(${v.id}, ${!v.activo})"
            style="padding:6px 10px;background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:8px;font-size:12px;cursor:pointer;">
            ${v.activo ? 'Desactivar' : 'Activar'}
          </button>
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;">Error cargando vehículos</div>';
  }
}

function vehiculosMostrarForm() {
  document.getElementById('vehiculos-form').style.display = 'block';
  document.getElementById('vehiculos-form-error').textContent = '';
}

function vehiculosCancelarForm() {
  document.getElementById('vehiculos-form').style.display = 'none';
}

async function vehiculosCrear() {
  const errorEl    = document.getElementById('vehiculos-form-error');
  const placa      = document.getElementById('veh-form-placa')?.value.trim().toUpperCase();
  const tipo       = document.getElementById('veh-form-tipo')?.value;
  const capacidad  = document.getElementById('veh-form-capacidad')?.value.trim();
  errorEl.textContent = '';

  if (!placa) { errorEl.textContent = 'La placa es requerida'; return; }
  if (!tipo)  { errorEl.textContent = 'Selecciona el tipo de vehículo'; return; }

  try {
    const r = await fetch(API + '/api/rutas/vehiculos', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ placa, tipo, capacidad_kg: capacidad ? parseFloat(capacidad) : null }),
    });
    const d = await r.json();
    if (r.ok) {
      vehiculosCancelarForm();
      ['veh-form-placa','veh-form-capacidad'].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = '';
      });
      await cargarListaVehiculos();
    } else {
      errorEl.textContent = d.error || 'Error al guardar vehículo';
    }
  } catch (e) { errorEl.textContent = 'Error de conexión'; }
}

async function vehiculoToggle(id, activar) {
  try {
    await fetch(API + '/api/rutas/vehiculos/' + id, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ activo: activar }),
    });
    await cargarListaVehiculos();
  } catch (e) { alert('Error de conexión'); }
}

// ── Conductores ──────────────────────────────────────

async function cargarListaConductoresEnSelect(selectId) {
  const sel = document.getElementById(selectId);
  if (!sel) return;
  try {
    const d = await get('/api/rutas/conductores?activos=true');
    sel.innerHTML = '<option value="">— Selecciona conductor —</option>';
    (d.conductores || []).forEach(c => {
      const opt = document.createElement('option');
      opt.value = c.id;
      opt.textContent = `${c.nombre}${c.telefono ? ' · ' + c.telefono : ''}`;
      sel.appendChild(opt);
    });
  } catch (e) {}
}

async function cargarListaConductores() {
  const el = document.getElementById('lista-conductores');
  if (!el) return;
  try {
    const d = await get('/api/rutas/conductores?activos=false');
    const conductores = d.conductores || [];
    if (!conductores.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin conductores registrados</div>';
      return;
    }
    el.innerHTML = conductores.map(c => `
      <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
          <div>
            <div style="font-size:14px;font-weight:700;">${c.nombre}</div>
            <div style="font-size:12px;color:#555;margin-top:2px;">CC ${c.cedula}${c.telefono ? ' · ' + c.telefono : ''}</div>
            ${c.usuario_email
              ? `<div style="font-size:11px;color:#facc15;margin-top:3px;">👤 ${c.usuario_email}</div>`
              : `<div style="font-size:11px;color:#555;margin-top:3px;">Sin cuenta PWA</div>`}
          </div>
          ${c.activo
            ? '<span style="background:#14532d;color:#4ade80;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;height:fit-content;">ACTIVO</span>'
            : '<span style="background:#3f1515;color:#f87171;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;height:fit-content;">INACTIVO</span>'}
        </div>
        <button onclick="conductorToggle(${c.id}, ${!c.activo})"
          style="width:100%;padding:8px;background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:8px;font-size:12px;cursor:pointer;">
          ${c.activo ? 'Desactivar' : 'Activar'}
        </button>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;">Error cargando conductores</div>';
  }
}

async function conductoresMostrarForm() {
  document.getElementById('conductores-form').style.display = 'block';
  document.getElementById('conductores-form-error').textContent = '';
  // Cargar usuarios con rol conductor para el selector
  try {
    const d = await get('/api/rutas/usuarios-conductores');
    const sel = document.getElementById('cond-form-usuario');
    if (sel) {
      sel.innerHTML = '<option value="">Sin cuenta (solo flota)</option>' +
        (d.usuarios || []).map(u => `<option value="${u.id}">${u.nombre} (${u.email})</option>`).join('');
    }
  } catch (_) {}
}

function conductoresCancelarForm() {
  document.getElementById('conductores-form').style.display = 'none';
}

async function conductoresCrear() {
  const errorEl  = document.getElementById('conductores-form-error');
  const nombre     = document.getElementById('cond-form-nombre')?.value.trim();
  const cedula     = document.getElementById('cond-form-cedula')?.value.trim();
  const telefono   = document.getElementById('cond-form-telefono')?.value.trim();
  const usuarioId  = document.getElementById('cond-form-usuario')?.value || null;
  errorEl.textContent = '';

  if (!nombre) { errorEl.textContent = 'El nombre es requerido'; return; }
  if (!cedula) { errorEl.textContent = 'La cédula es requerida'; return; }

  try {
    const r = await fetch(API + '/api/rutas/conductores', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ nombre, cedula, telefono: telefono || null, usuario_id: usuarioId ? parseInt(usuarioId) : null }),
    });
    const d = await r.json();
    if (r.ok) {
      conductoresCancelarForm();
      ['cond-form-nombre','cond-form-cedula','cond-form-telefono'].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = '';
      });
      await cargarListaConductores();
    } else {
      errorEl.textContent = d.error || 'Error al guardar conductor';
    }
  } catch (e) { errorEl.textContent = 'Error de conexión'; }
}

async function conductorToggle(id, activar) {
  try {
    const r = await fetch(API + '/api/rutas/conductores/' + id, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ activo: activar }),
    });
    if (r.ok) await cargarListaConductores();
    else { const d = await r.json(); alert(d.error || 'Error'); }
  } catch (e) { alert('Error de conexión'); }
}


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
  function _open() {
    if (_db) return Promise.resolve(_db);
    return new Promise((res, rej) => {
      const r = indexedDB.open('wms_cond', 1);
      r.onupgradeneeded = e => {
        const db = e.target.result;
        if (!db.objectStoreNames.contains('cache'))
          db.createObjectStore('cache', { keyPath: 'k' });
        if (!db.objectStoreNames.contains('queue'))
          db.createObjectStore('queue', { keyPath: 'id', autoIncrement: true });
      };
      r.onsuccess = e => { _db = e.target.result; res(_db); };
      r.onerror   = () => rej(r.error);
    });
  }
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

async function cargarRutasConductor() {
  const el = document.getElementById('cond-contenido');
  if (!el) return;
  try {
    const d = await get('/api/rutas/mis-rutas');
    _COND_RUTAS = d.rutas || [];
    await _condDB.set('rutas', _COND_RUTAS);
  } catch (e) {
    const cached = await _condDB.get('rutas');
    if (cached !== null) {
      _COND_RUTAS = cached;
    } else if (!_COND_RUTA_ACTIVA) {
      el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Sin conexión y sin datos en caché. Abre la app con señal primero.</div>';
      return;
    }
  }

  document.getElementById('cond-badge-rutas').textContent = _COND_RUTAS.length;

  // Conductor llenando formulario → no interrumpir bajo ninguna circunstancia
  if (_COND_PARADA_FORM) return;
  // Conductor viendo lista de paradas → refrescar esa vista sin redirigir
  if (_COND_RUTA_ACTIVA) {
    await condAbrirParadas(_COND_RUTA_ACTIVA.id);
    return;
  }

  if (!_COND_RUTAS.length) {
    el.innerHTML = `<div style="text-align:center;padding:80px 20px;">
      <div style="font-size:60px;">✅</div>
      <div style="font-size:20px;font-weight:700;color:#4ade80;margin-top:16px;">Sin rutas en tránsito</div>
      <div style="font-size:13px;color:#555;margin-top:8px;">El jefe de almacén te asignará una cuando salgas</div>
      <button onclick="cargarRutasConductor()" style="margin-top:24px;padding:14px 28px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:12px;font-size:15px;cursor:pointer;">🔄 Actualizar</button>
    </div>`;
    return;
  }

  el.innerHTML = _COND_RUTAS.map((r) => {
    const totalBultos = r.total_bultos || 0;
    return `
      <div style="background:#111;border:2px solid #1e3a5f;border-radius:16px;padding:20px;margin-bottom:12px;">
        <div style="font-size:18px;font-weight:800;color:#60a5fa;margin-bottom:4px;">🚛 Ruta #${r.id}</div>
        <div style="font-size:14px;color:#ccc;margin-bottom:12px;">${r.ruta_maestra_nombre || r.tipo_ruta} · ${r.vehiculo_placa || 'Sin vehículo'}</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">${totalBultos} bulto${totalBultos !== 1 ? 's' : ''}</div>
        <button onclick="condAbrirParadas(${r.id})"
          style="width:100%;padding:18px;background:#1d4ed8;color:#fff;border:none;border-radius:12px;font-size:18px;font-weight:800;cursor:pointer;letter-spacing:0.02em;">
          📦 Ver Paradas y Cobros
        </button>
      </div>`;
  }).join('');
}

// ── Lista de paradas de la ruta ───────────────────────────────────

async function condAbrirParadas(rutaId) {
  const el = document.getElementById('cond-contenido');
  el.innerHTML = '<div style="text-align:center;padding:60px;color:#555;">Cargando paradas...</div>';
  let data = null;
  try {
    if (!navigator.onLine) throw new Error('offline');
    const d = await get('/api/rutas/' + rutaId + '/paradas');
    data = d;
    await _condDB.set('paradas_' + rutaId, d);
  } catch (e) {
    data = await _condDB.get('paradas_' + rutaId);
    if (!data) {
      el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Sin conexión y sin datos en caché para esta ruta.</div>';
      return;
    }
  }
  _COND_RUTA_ACTIVA = { id: rutaId };
  _COND_PARADAS = data.paradas || [];
  _condRenderParadas(data);
}

function _condRenderParadas(d) {
  const el = document.getElementById('cond-contenido');
  if (!el) return;
  const paradas = _COND_PARADAS;
  const gestionadas = d.paradas_gestionadas || paradas.filter(p => p.recaudo).length;
  const total = paradas.length;
  const todasGestionadas = gestionadas === total && total > 0;

  let html = `
    <div style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;">
      <button onclick="cargarRutasConductor()" style="background:none;border:none;color:#666;font-size:14px;cursor:pointer;padding:0;">← Volver</button>
      <span style="font-size:13px;color:#555;">${gestionadas}/${total} gestionadas</span>
    </div>
    <div style="background:#111;border:1px solid #1e3a5f;border-radius:14px;padding:14px;margin-bottom:16px;">
      <div style="font-size:14px;color:#aaa;">Ruta #${_COND_RUTA_ACTIVA.id} · <span style="color:${todasGestionadas ? '#4ade80' : '#facc15'};font-weight:700;">${todasGestionadas ? 'Lista para cerrar' : 'En curso'}</span></div>
      <div style="display:flex;gap:16px;margin-top:10px;">
        <div style="text-align:center;"><div style="font-size:24px;font-weight:800;color:#4ade80;">${gestionadas}</div><div style="font-size:10px;color:#555;">GESTIONADAS</div></div>
        <div style="text-align:center;"><div style="font-size:24px;font-weight:800;color:#555;">${total - gestionadas}</div><div style="font-size:10px;color:#555;">PENDIENTES</div></div>
      </div>
    </div>`;

  paradas.forEach((p, idx) => {
    const r = p.recaudo;
    const EST_C = {
      ENTREGADO: { borde: '#16a34a', fondo: '#f0fdf4', badgeBg: '#dcfce7', badgeColor: '#15803d', label: 'ENTREGADO' },
      PARCIAL:   { borde: '#d97706', fondo: '#fffbeb', badgeBg: '#fef3c7', badgeColor: '#b45309', label: 'PARCIAL'   },
      RECHAZADO: { borde: '#dc2626', fondo: '#fef2f2', badgeBg: '#fee2e2', badgeColor: '#b91c1c', label: 'RECHAZADO' },
    };
    const est = r ? r.estado_entrega : null;
    const c = est ? EST_C[est] : { borde: '#d1d5db', fondo: '#f9fafb', badgeBg: '#f3f4f6', badgeColor: '#6b7280', label: 'PENDIENTE' };
    const badge = `<span style="background:${c.badgeBg};color:${c.badgeColor};padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;">${c.label}</span>`;
    const monto = r ? ` · $${Number(r.monto_cobrado || 0).toLocaleString('es-CO')}` : '';

    html += `
      <div style="background:${c.fondo};border:1px solid ${c.borde};border-radius:12px;padding:14px;margin-bottom:8px;cursor:pointer;"
           onclick="condAbrirFormParada(${idx})">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div style="flex:1;min-width:0;">
            <div style="font-size:14px;font-weight:800;color:#111827;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${p.cliente}</div>
            <div style="font-size:12px;color:#6b7280;margin-top:2px;">📍 ${p.municipio} · ${p.numero_pedido}</div>
            <div style="font-size:11px;color:#9ca3af;margin-top:2px;">${p.bultos.length} bulto${p.bultos.length !== 1 ? 's' : ''}${monto}</div>
          </div>
          <div style="margin-left:10px;flex-shrink:0;">${badge}</div>
        </div>
        ${r ? `<div style="font-size:11px;color:#6b7280;margin-top:6px;">${r.forma_pago || ''}${r.observaciones ? ' · ' + r.observaciones.substring(0,40) : ''}</div>` : ''}
      </div>`;
  });

  if (todasGestionadas) {
    html += `
      <div style="position:sticky;bottom:16px;margin-top:12px;">
        <button onclick="condCerrarRuta()"
          style="width:100%;padding:20px;background:#166534;color:#4ade80;border:none;border-radius:14px;font-size:18px;font-weight:800;cursor:pointer;">
          ✅ Cerrar Ruta — Todo Gestionado
        </button>
      </div>`;
  }

  el.innerHTML = html;
}

// ── Formulario de confirmación de parada ──────────────────────────

function condAbrirFormParada(idx) {
  _COND_PARADA_FORM = { ..._COND_PARADAS[idx], _idx: idx };
  _condRenderFormParada();
}

function _condRenderFormParada() {
  const el = document.getElementById('cond-contenido');
  const p = _COND_PARADA_FORM;
  if (!el || !p) return;

  const r = p.recaudo;
  const estadoActual = r ? r.estado_entrega : 'ENTREGADO';
  const formaActual  = r ? (r.forma_pago || '') : '';
  const montoActual  = r ? (r.monto_cobrado || 0) : 0;
  const obsActual    = r ? (r.observaciones || '') : '';
  const rechazadosActuales = r ? (r.bultos_rechazados_ids || []) : [];

  el.innerHTML = `
    <div style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;">
      <button onclick="condVolverAParadas()" style="background:none;border:none;color:#666;font-size:14px;cursor:pointer;padding:0;">← Paradas</button>
      ${r ? `<span style="font-size:11px;color:#555;">Editando confirmación</span>` : ''}
    </div>

    <div style="background:#111;border:1px solid #333;border-radius:14px;padding:14px;margin-bottom:16px;">
      <div style="font-size:16px;font-weight:800;color:#fff;">${p.cliente}</div>
      <div style="font-size:13px;color:#aaa;margin-top:4px;">📍 ${p.municipio}</div>
      <div style="font-size:12px;color:#555;margin-top:2px;">${p.numero_pedido} · ${p.bultos.length} bulto${p.bultos.length !== 1 ? 's' : ''}</div>
    </div>

    <div style="margin-bottom:14px;">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">RESULTADO ENTREGA</label>
      <div style="display:flex;gap:6px;" id="cond-estado-btns">
        ${['ENTREGADO','PARCIAL','RECHAZADO'].map(e => `
          <button onclick="condSelEstado('${e}')"
            id="cond-estado-${e}"
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;border:2px solid ${e===estadoActual ? (e==='ENTREGADO'?'#16a34a':e==='PARCIAL'?'#d97706':'#dc2626') : '#ddd'};background:${e===estadoActual ? (e==='ENTREGADO'?'#f0fdf4':e==='PARCIAL'?'#fffbeb':'#fef2f2') : '#fff'};color:${e===estadoActual ? (e==='ENTREGADO'?'#15803d':e==='PARCIAL'?'#b45309':'#b91c1c') : '#aaa'};">
            ${e === 'ENTREGADO' ? '✓ Entregado' : e === 'PARCIAL' ? '⚠ Parcial' : '✗ Rechazado'}
          </button>`).join('')}
      </div>
    </div>

    <div id="cond-bultos-rechazo" style="margin-bottom:14px;display:${estadoActual !== 'ENTREGADO' ? 'block' : 'none'};">
      <label style="font-size:12px;color:#555;font-weight:700;display:block;margin-bottom:8px;">BULTOS RECHAZADOS</label>
      ${p.bultos.map(b => `
        <label style="display:flex;align-items:center;gap:10px;padding:10px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px;cursor:pointer;">
          <input type="checkbox" value="${b.id}" ${rechazadosActuales.includes(b.id) ? 'checked' : ''}
            style="width:18px;height:18px;cursor:pointer;" id="chk-bulto-${b.id}">
          <span style="font-size:13px;color:#374151;">${b.codigo_barras} · ${b.tipo} ${b.numero}/${b.total}</span>
        </label>`).join('')}
    </div>

    <div id="cond-items-parcial" style="margin-bottom:14px;display:${estadoActual === 'PARCIAL' ? 'block' : 'none'};">
      <label style="font-size:12px;color:#555;font-weight:700;display:block;margin-bottom:8px;">REFERENCIAS ENTREGADAS</label>
      <div style="font-size:11px;color:#9ca3af;margin-bottom:10px;">Ajusta la cantidad entregada por referencia. Lo que no se entregó queda como devolución.</div>
      ${(p.items && p.items.length ? p.items : []).map((it, idx) => {
        const pedido = it.cantidad_pedida || 0;
        const prevEntregado = (() => {
          if (!rechazadosActuales.length && r && r.items_entregados) {
            const prev = (r.items_entregados || []).find(x => x.codigo === it.codigo);
            return prev ? prev.cantidad_entregada : pedido;
          }
          return pedido;
        })();
        return `
        <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:12px;margin-bottom:8px;">
          <div style="font-size:13px;font-weight:700;color:#111827;margin-bottom:2px;">${it.nombre || it.codigo}</div>
          <div style="font-size:11px;color:#6b7280;margin-bottom:10px;">${it.codigo} · ${it.unidad || 'und'}</div>
          <div style="display:flex;align-items:center;gap:10px;">
            <div style="flex:1;text-align:center;">
              <div style="font-size:10px;color:#9ca3af;margin-bottom:2px;">PEDIDO</div>
              <div style="font-size:18px;font-weight:800;color:#374151;">${pedido}</div>
            </div>
            <div style="flex:2;">
              <div style="font-size:10px;color:#9ca3af;margin-bottom:4px;">ENTREGADO</div>
              <input type="number" id="item-entregado-${idx}"
                value="${prevEntregado}" min="0" max="${pedido}" step="1"
                oninput="condActualizarDevuelto(${idx}, ${pedido})"
                style="width:100%;padding:10px;border:2px solid #d1d5db;border-radius:8px;font-size:18px;font-weight:800;color:#15803d;text-align:center;box-sizing:border-box;">
            </div>
            <div style="flex:1;text-align:center;">
              <div style="font-size:10px;color:#9ca3af;margin-bottom:2px;">DEVUELTO</div>
              <div id="item-devuelto-${idx}" style="font-size:18px;font-weight:800;color:#b91c1c;">${pedido - prevEntregado}</div>
            </div>
          </div>
        </div>`;
      }).join('')}
    </div>

    <div id="cond-forma-pago-wrap" style="margin-bottom:14px;display:${estadoActual === 'RECHAZADO' ? 'none' : 'block'};">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">FORMA DE PAGO</label>
      <select id="cond-forma-pago"
        style="width:100%;padding:14px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:15px;">
        <option value="">— Seleccionar —</option>
        ${['EFECTIVO','TRANSFERENCIA','CHEQUE','CREDITO','EXENTO'].map(f =>
          `<option value="${f}" ${f===formaActual?'selected':''}>${f.charAt(0)+f.slice(1).toLowerCase()}</option>`
        ).join('')}
      </select>
    </div>

    <div id="cond-monto-wrap" style="margin-bottom:14px;display:${estadoActual === 'RECHAZADO' ? 'none' : 'block'};">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">MONTO COBRADO ($)</label>
      <input type="number" id="cond-monto" value="${montoActual}" min="0" step="100"
        style="width:100%;padding:14px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:18px;font-weight:700;box-sizing:border-box;">
    </div>

    <div style="margin-bottom:14px;">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">OBSERVACIONES</label>
      <textarea id="cond-obs" rows="2"
        style="width:100%;padding:12px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:14px;resize:none;box-sizing:border-box;"
        placeholder="Ej: Cliente solicitó factura electrónica...">${obsActual}</textarea>
    </div>

    <div style="margin-bottom:20px;">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">FOTO EVIDENCIA <span style="color:#555;font-weight:400;">(opcional)</span></label>
      <input type="file" id="cond-foto" accept="image/*" capture="environment"
        style="display:none;" onchange="condPrevisualizarFoto()">
      <button type="button" onclick="document.getElementById('cond-foto').click()"
        style="width:100%;padding:16px;background:#f0f9ff;color:#0369a1;border:2px dashed #7dd3fc;border-radius:12px;font-size:16px;font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:10px;">
        📷 Tomar foto con la cámara
      </button>
      <div id="cond-foto-preview" style="margin-top:8px;display:none;">
        <img id="cond-foto-img" src="" style="width:100%;border-radius:10px;border:2px solid #7dd3fc;max-height:200px;object-fit:cover;">
        <button type="button" onclick="condEliminarFoto()"
          style="margin-top:6px;width:100%;padding:8px;background:#fef2f2;color:#b91c1c;border:1px solid #dc2626;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
          ✕ Quitar foto
        </button>
      </div>
      ${r && r.foto_entrega ? `<div style="margin-top:8px;font-size:11px;color:#4ade80;">✓ Foto guardada — toma una nueva para reemplazarla</div>` : ''}
    </div>

    <div style="position:sticky;bottom:16px;display:flex;flex-direction:column;gap:8px;">
      <button onclick="condGuardarParada()"
        style="width:100%;padding:20px;background:#1d4ed8;color:#fff;border:none;border-radius:14px;font-size:18px;font-weight:800;cursor:pointer;">
        ${r ? '💾 Actualizar Confirmación' : '✓ Confirmar Parada'}
      </button>
      ${!r ? `<button onclick="condNoSePudoEntregar()"
        style="width:100%;padding:14px;background:#1a0d0d;color:#f87171;border:1px solid #7f1d1d;border-radius:12px;font-size:15px;font-weight:700;cursor:pointer;">
        🚫 No se pudo entregar
      </button>` : ''}
    </div>`;

  // Guardar estado seleccionado
  el._estadoSel = estadoActual;
}

function condSelEstado(estado) {
  const el = document.getElementById('cond-contenido');
  if (!el) return;
  el._estadoSel = estado;

  ['ENTREGADO','PARCIAL','RECHAZADO'].forEach(e => {
    const btn = document.getElementById('cond-estado-' + e);
    if (!btn) return;
    const activo = e === estado;
    const colores = { ENTREGADO: ['#16a34a','#f0fdf4','#15803d'], PARCIAL: ['#d97706','#fffbeb','#b45309'], RECHAZADO: ['#dc2626','#fef2f2','#b91c1c'] };
    const [borde, fondo, texto] = activo ? colores[e] : ['#ddd','#fff','#aaa'];
    btn.style.borderColor = borde;
    btn.style.background  = fondo;
    btn.style.color       = texto;
  });

  const divRechazo = document.getElementById('cond-bultos-rechazo');
  if (divRechazo) divRechazo.style.display = estado !== 'ENTREGADO' ? 'block' : 'none';

  const divItems = document.getElementById('cond-items-parcial');
  if (divItems) divItems.style.display = estado === 'PARCIAL' ? 'block' : 'none';

  const mostrarPago = estado !== 'RECHAZADO';
  const divFormaPago = document.getElementById('cond-forma-pago-wrap');
  const divMonto     = document.getElementById('cond-monto-wrap');
  if (divFormaPago) divFormaPago.style.display = mostrarPago ? 'block' : 'none';
  if (divMonto)     divMonto.style.display     = mostrarPago ? 'block' : 'none';
}

function condPrevisualizarFoto() {
  const input = document.getElementById('cond-foto');
  const preview = document.getElementById('cond-foto-preview');
  const img = document.getElementById('cond-foto-img');
  if (!input || !input.files[0]) return;
  const reader = new FileReader();
  reader.onload = ev => {
    img.src = ev.target.result;
    preview.style.display = 'block';
  };
  reader.readAsDataURL(input.files[0]);
}

function condEliminarFoto() {
  const input = document.getElementById('cond-foto');
  const preview = document.getElementById('cond-foto-preview');
  if (input) input.value = '';
  if (preview) preview.style.display = 'none';
}

function condActualizarDevuelto(idx, pedido) {
  const inp = document.getElementById('item-entregado-' + idx);
  const div = document.getElementById('item-devuelto-' + idx);
  if (!inp || !div) return;
  let val = parseInt(inp.value) || 0;
  if (val < 0) { val = 0; inp.value = 0; }
  if (val > pedido) { val = pedido; inp.value = pedido; }
  div.textContent = pedido - val;
}

async function condGuardarParada() {
  const el = document.getElementById('cond-contenido');
  const p = _COND_PARADA_FORM;
  if (!el || !p) return;

  const estadoEntrega = el._estadoSel || 'ENTREGADO';
  const formaPago     = document.getElementById('cond-forma-pago')?.value || '';
  const monto         = parseFloat(document.getElementById('cond-monto')?.value || 0) || 0;
  const obs           = document.getElementById('cond-obs')?.value?.trim() || '';

  // Validación por campo según estado
  if (estadoEntrega !== 'RECHAZADO') {
    if (!formaPago) {
      alerta('Selecciona la forma de pago antes de confirmar', 'error');
      document.getElementById('cond-forma-pago')?.focus();
      return;
    }
    if (estadoEntrega === 'PARCIAL' && monto <= 0) {
      alerta('Ingresa el monto cobrado por la parte entregada', 'error');
      document.getElementById('cond-monto')?.focus();
      return;
    }
    if (estadoEntrega === 'PARCIAL' && !obs) {
      alerta('Escribe una observación: qué se entregó y qué se devolvió', 'error');
      document.getElementById('cond-obs')?.focus();
      return;
    }
  }

  // Observaciones obligatorias para RECHAZADO
  if (estadoEntrega === 'RECHAZADO' && !obs) {
    alerta('Escribe el motivo del rechazo (ej: cliente cerrado, dirección incorrecta)', 'error');
    document.getElementById('cond-obs')?.focus();
    return;
  }

  // Bultos rechazados
  const bultosRechazados = [];
  if (estadoEntrega !== 'ENTREGADO') {
    p.bultos.forEach(b => {
      const chk = document.getElementById('chk-bulto-' + b.id);
      if (chk && chk.checked) bultosRechazados.push(b.id);
    });
    if (estadoEntrega === 'PARCIAL' && !bultosRechazados.length) {
      alerta('Para entrega parcial selecciona qué bultos fueron rechazados', 'error');
      return;
    }
  }

  // Ítems con cantidades para PARCIAL
  const itemsEntregados = [];
  if (estadoEntrega === 'PARCIAL' && p.items && p.items.length) {
    let hayDevolucion = false;
    p.items.forEach((it, idx) => {
      const pedido    = it.cantidad_pedida || 0;
      const inp       = document.getElementById('item-entregado-' + idx);
      const entregado = inp ? Math.max(0, Math.min(parseInt(inp.value) || pedido, pedido)) : pedido;
      if (entregado < pedido) hayDevolucion = true;
      itemsEntregados.push({
        codigo:             it.codigo,
        nombre:             it.nombre,
        unidad:             it.unidad || 'und',
        cantidad_pedida:    pedido,
        cantidad_entregada: entregado,
      });
    });
    if (!hayDevolucion) {
      alerta('Si no hay devolución de referencias, usa "Entregado" en lugar de Parcial', 'advertencia');
      return;
    }
  }

  // Foto (base64 comprimida — máx 800×600 @ JPEG 0.65)
  let fotoBase64 = '';
  const fotoInput = document.getElementById('cond-foto');
  if (fotoInput && fotoInput.files[0]) {
    try {
      fotoBase64 = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = reject;
        reader.onload = ev => {
          const img = new Image();
          img.onerror = reject;
          img.onload = () => {
            const MAX_W = 800, MAX_H = 600;
            let w = img.width, h = img.height;
            if (w > MAX_W || h > MAX_H) {
              const ratio = Math.min(MAX_W / w, MAX_H / h);
              w = Math.round(w * ratio); h = Math.round(h * ratio);
            }
            const canvas = document.createElement('canvas');
            canvas.width = w; canvas.height = h;
            canvas.getContext('2d').drawImage(img, 0, 0, w, h);
            resolve(canvas.toDataURL('image/jpeg', 0.65));
          };
          img.src = ev.target.result;
        };
        reader.readAsDataURL(fotoInput.files[0]);
      });
    } catch (_) { alerta('Error procesando la foto', 'error'); return; }
  }

  const payload = {
    estado_entrega:    estadoEntrega,
    forma_pago:        formaPago || null,
    monto_cobrado:     monto,
    observaciones:     obs || null,
    foto_entrega:      fotoBase64 || null,
    bultos_rechazados: bultosRechazados,
    items_entregados:  itemsEntregados.length ? itemsEntregados : null,
  };

  // ── Sin conexión: encolar y actualizar local ────────────────────
  if (!navigator.onLine) {
    await _condDB.enqueue({ tipo: 'confirmar', rutaId: _COND_RUTA_ACTIVA.id, tareaId: p.tarea_id, payload });
    const idx = _COND_PARADAS.findIndex(x => x.tarea_id === p.tarea_id);
    if (idx >= 0) {
      _COND_PARADAS[idx].recaudo = {
        estado_entrega:        estadoEntrega,
        forma_pago:            formaPago || null,
        monto_cobrado:         monto,
        observaciones:         obs || null,
        foto_entrega:          fotoBase64 || null,
        bultos_rechazados_ids: bultosRechazados,
        items_entregados:      itemsEntregados.length ? itemsEntregados : null,
      };
      const gestionadas = _COND_PARADAS.filter(x => x.recaudo).length;
      await _condDB.set('paradas_' + _COND_RUTA_ACTIVA.id, { paradas: _COND_PARADAS, paradas_gestionadas: gestionadas });
    }
    await _condActualizarBarras();
    alerta('Guardado sin conexión — se enviará al reconectar', 'advertencia');
    condVolverAParadas(false);
    return;
  }

  const btn = el.querySelector('button[onclick="condGuardarParada()"]');
  if (btn) { btn.disabled = true; btn.textContent = 'Guardando...'; }

  try {
    const r = await fetch(API + '/api/rutas/' + _COND_RUTA_ACTIVA.id + '/paradas/' + p.tarea_id + '/confirmar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.ok) {
      alerta(d.es_edicion ? 'Confirmación actualizada' : 'Parada confirmada ✓', 'exito');
      condVolverAParadas(true);
    } else {
      alerta(d.error || 'Error al confirmar parada', 'error');
      if (btn) { btn.disabled = false; btn.textContent = 'Confirmar Parada'; }
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Confirmar Parada'; }
  }
}

async function condVolverAParadas(recargar = false) {
  _COND_PARADA_FORM = null;
  if (recargar && _COND_RUTA_ACTIVA) {
    await condAbrirParadas(_COND_RUTA_ACTIVA.id);
  } else if (_COND_RUTA_ACTIVA) {
    await condAbrirParadas(_COND_RUTA_ACTIVA.id);
  } else {
    cargarRutasConductor();
  }
}

async function condNoSePudoEntregar() {
  const p = _COND_PARADA_FORM;
  if (!p) return;
  const obs = prompt('Motivo (opcional):', 'Cliente no disponible');
  if (obs === null) return;
  const payload = {
    estado_entrega: 'RECHAZADO',
    forma_pago:     'EXENTO',
    monto_cobrado:  0,
    observaciones:  obs || 'Cliente no disponible',
    bultos_rechazados: [],
  };
  if (!navigator.onLine) {
    await _condDB.enqueue({ tipo: 'confirmar', rutaId: _COND_RUTA_ACTIVA.id, tareaId: p.tarea_id, payload });
    const idx = _COND_PARADAS.findIndex(x => x.tarea_id === p.tarea_id);
    if (idx >= 0) {
      _COND_PARADAS[idx].recaudo = { ...payload, bultos_rechazados_ids: [], items_entregados: null };
      const gestionadas = _COND_PARADAS.filter(x => x.recaudo).length;
      await _condDB.set('paradas_' + _COND_RUTA_ACTIVA.id, { paradas: _COND_PARADAS, paradas_gestionadas: gestionadas });
    }
    await _condActualizarBarras();
    alerta('Registrado sin conexión — se enviará al reconectar', 'advertencia');
    await condAbrirParadas(_COND_RUTA_ACTIVA.id);
    return;
  }
  try {
    const r = await fetch(API + `/api/rutas/${_COND_RUTA_ACTIVA.id}/paradas/${p.tarea_id}/confirmar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Registrado como no entregado', 'advertencia');
      await condAbrirParadas(_COND_RUTA_ACTIVA.id);
    } else {
      alerta(d.error || 'Error al registrar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function condCerrarRuta() {
  if (!_COND_RUTA_ACTIVA) return;
  if (!confirm('¿Confirmar cierre de ruta? Ya no podrás agregar más confirmaciones de parada.')) return;
  if (!navigator.onLine) {
    await _condDB.enqueue({ tipo: 'cerrar', rutaId: _COND_RUTA_ACTIVA.id, payload: { bultos: [] } });
    await _condActualizarBarras();
    alerta('Cierre en cola — se enviará al reconectar', 'advertencia');
    _COND_RUTA_ACTIVA = null;
    _COND_PARADAS = [];
    cargarRutasConductor();
    return;
  }
  try {
    const r = await fetch(API + '/api/rutas/' + _COND_RUTA_ACTIVA.id + '/entregar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ bultos: [] }),
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Ruta cerrada — ¡Buen trabajo!', 'exito');
      _COND_RUTA_ACTIVA = null;
      _COND_PARADAS = [];
      cargarRutasConductor();
    } else {
      alerta(d.error || 'Error al cerrar ruta', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── Offline: init, barras de estado y motor de sync ──────────────

function _condIniciarOffline() {
  if (!_COND_OFFLINE_INIT) {
    _COND_OFFLINE_INIT = true;
    window.addEventListener('online',  () => { _condActualizarBarras(); condSyncQueue(); });
    window.addEventListener('offline', () => { _condActualizarBarras(); });
  }
  _condActualizarBarras();
}

async function _condActualizarBarras() {
  const offlineBar = document.getElementById('cond-offline-bar');
  const syncBar    = document.getElementById('cond-sync-bar');
  const syncStatus = document.getElementById('cond-sync-status');
  const syncBtn    = document.getElementById('cond-sync-btn');
  if (!offlineBar || !syncBar) return;

  offlineBar.style.display = navigator.onLine ? 'none' : 'block';

  const items = await _condDB.queue();
  const n = items.length;
  if (n > 0) {
    syncBar.style.display = 'flex';
    if (syncStatus && !_COND_SYNCING)
      syncStatus.textContent = `⏳ ${n} confirmación${n !== 1 ? 'es' : ''} pendiente${n !== 1 ? 's' : ''} de sincronizar`;
    if (syncBtn) syncBtn.disabled = _COND_SYNCING || !navigator.onLine;
  } else {
    syncBar.style.display = 'none';
  }
}

async function condSyncQueue() {
  if (_COND_SYNCING || !navigator.onLine) return;
  const items = await _condDB.queue();
  if (!items.length) { await _condActualizarBarras(); return; }

  _COND_SYNCING = true;
  const syncStatus = document.getElementById('cond-sync-status');
  const syncBtn    = document.getElementById('cond-sync-btn');
  if (syncBtn) syncBtn.disabled = true;

  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (syncStatus)
      syncStatus.textContent = `🔄 Sincronizando ${i + 1}/${items.length}…`;
    try {
      const url = item.tipo === 'confirmar'
        ? `${API}/api/rutas/${item.rutaId}/paradas/${item.tareaId}/confirmar`
        : `${API}/api/rutas/${item.rutaId}/entregar`;
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
        body: JSON.stringify(item.payload),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.error || `Error ${r.status}`);
      }
      await _condDB.dequeue(item.id);
    } catch (e) {
      _COND_SYNCING = false;
      await _condActualizarBarras();
      alerta(`Error al sincronizar: ${e.message}`, 'error');
      return;
    }
  }

  _COND_SYNCING = false;
  alerta('✓ Sincronización completa', 'exito');
  await _condActualizarBarras();

  // Recargar datos frescos del servidor
  if (_COND_RUTA_ACTIVA) {
    await condAbrirParadas(_COND_RUTA_ACTIVA.id);
  } else {
    await cargarRutasConductor();
  }
}

// ══════════════════════════════════════════════════════════════════
// PLANILLA DE CUADRE — Admin
// ══════════════════════════════════════════════════════════════════

let _PLAN_RUTA_ID = null;

async function rutaForzarCierre(id) {
  if (!confirm('¿Forzar el cierre de esta ruta?\n\nLas paradas sin gestionar quedarán registradas como RECHAZADAS automáticamente.\nEsta acción es irreversible.')) return;
  try {
    const r = await fetch(API + `/api/rutas/${id}/forzar-cierre`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`Ruta cerrada — ${d.paradas_auto_cerradas} parada(s) auto-rechazadas`, 'advertencia');
      await cargarRutas();
    } else {
      alerta(d.error || 'Error al forzar cierre', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function rutaVerPlanilla(id) {
  _PLAN_RUTA_ID = id;
  const modal = document.getElementById('modal-planilla');
  if (!modal) return;
  document.getElementById('modal-planilla-body').innerHTML =
    '<div style="text-align:center;padding:60px;color:#555;">Cargando planilla...</div>';
  modal.style.display = 'flex';
  await _cargarPlanilla(id);
}

async function _cargarPlanilla(id) {
  try {
    const d = await get('/api/rutas/' + id + '/planilla');
    const body = document.getElementById('modal-planilla-body');
    if (!body) return;

    const ruta = d.ruta;
    const finBadge = {
      PENDIENTE:      '<span style="background:#1a1a1a;color:#555;padding:2px 10px;border-radius:8px;font-size:11px;font-weight:700;">PENDIENTE</span>',
      EN_LIQUIDACION: '<span style="background:#78350f;color:#fbbf24;padding:2px 10px;border-radius:8px;font-size:11px;font-weight:700;">EN LIQUIDACIÓN</span>',
      LIQUIDADA:      '<span style="background:#14532d;color:#4ade80;padding:2px 10px;border-radius:8px;font-size:11px;font-weight:700;">LIQUIDADA</span>',
    }[d.estado_financiero] || d.estado_financiero;

    const fmt = v => '$' + Number(v || 0).toLocaleString('es-CO');
    const totales = d.totales_por_forma || {};
    const total = d.total_recaudado || 0;

    let html = `
      <div style="margin-bottom:16px;">
        <div style="font-size:16px;font-weight:800;">Ruta #${ruta.id} — ${ruta.conductor_nombre}</div>
        <div style="font-size:13px;color:#aaa;margin-top:4px;">${ruta.ruta_maestra_nombre || ruta.tipo_ruta} · ${ruta.vehiculo_placa || ''}</div>
        <div style="margin-top:8px;">${finBadge}</div>
      </div>

      <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:16px;">
        <div style="font-size:12px;color:#aaa;font-weight:700;margin-bottom:10px;">RESUMEN FINANCIERO</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
          ${Object.entries(totales).filter(([,v]) => v > 0).map(([k,v]) => `
            <div style="background:#1a1a1a;border-radius:8px;padding:10px;">
              <div style="font-size:10px;color:#555;">${k}</div>
              <div style="font-size:16px;font-weight:800;color:#fff;">${fmt(v)}</div>
            </div>`).join('')}
        </div>
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid #222;display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:13px;color:#aaa;">Total recaudado</span>
          <span style="font-size:22px;font-weight:800;color:#4ade80;">${fmt(total)}</span>
        </div>
      </div>

      <div style="font-size:12px;color:#aaa;font-weight:700;margin-bottom:10px;">
        PARADAS (${d.total_paradas - d.sin_gestionar}/${d.total_paradas} gestionadas)
      </div>`;

    (d.paradas || []).forEach(p => {
      const r = p.recaudo;
      const colorBorde = r
        ? (r.estado_entrega === 'ENTREGADO' ? '#166534' : r.estado_entrega === 'PARCIAL' ? '#78350f' : '#7f1d1d')
        : '#333';
      html += `
        <div style="background:#111;border:1px solid ${colorBorde};border-radius:10px;padding:12px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
              <div style="font-size:14px;font-weight:700;color:#fff;">${p.cliente}</div>
              <div style="font-size:12px;color:#555;margin-top:2px;">📍 ${p.municipio} · ${p.numero_pedido}</div>
            </div>
            <div style="text-align:right;">
              ${r
                ? `<div style="font-size:13px;font-weight:700;color:${r.estado_entrega === 'ENTREGADO' ? '#4ade80' : r.estado_entrega === 'PARCIAL' ? '#fbbf24' : '#f87171'};">${r.estado_entrega}</div>
                   <div style="font-size:12px;color:#aaa;">${fmt(r.monto_cobrado)}</div>
                   <div style="font-size:11px;color:#555;">${r.forma_pago || '—'}</div>`
                : `<div style="font-size:12px;color:#555;">Sin gestionar</div>`}
            </div>
          </div>
          <div style="font-size:11px;color:#555;margin-top:6px;">
            ${p.bultos_entregados} entregado${p.bultos_entregados !== 1 ? 's' : ''} · ${p.bultos_rechazados} rechazado${p.bultos_rechazados !== 1 ? 's' : ''}
          </div>
          ${r && r.estado_entrega === 'RECHAZADO' ? `
          <div style="margin-top:10px;border-top:1px solid #3f1515;padding-top:10px;">
            <div style="font-size:10px;color:#f87171;font-weight:700;margin-bottom:6px;">BULTOS RECHAZADOS</div>
            ${(p.bultos_detalle || []).filter(b => b.rechazado).map(b =>
              `<div style="font-size:12px;color:#fca5a5;padding:3px 0;">${b.codigo_barras} · ${b.tipo} ${b.numero}/${b.total}</div>`
            ).join('')}
            ${r.observaciones ? `<div style="margin-top:8px;font-size:12px;color:#fbbf24;font-style:italic;">"${r.observaciones}"</div>` : ''}
          </div>` : ''}
          ${r && r.estado_entrega === 'PARCIAL' && r.items_entregados && r.items_entregados.length ? `
          <div style="margin-top:10px;border-top:1px solid #222;padding-top:10px;">
            <div style="font-size:10px;color:#fbbf24;font-weight:700;margin-bottom:6px;">DETALLE PARCIAL</div>
            <div style="display:grid;grid-template-columns:1fr auto auto auto;gap:4px 10px;font-size:11px;">
              <div style="color:#555;font-weight:700;">REFERENCIA</div>
              <div style="color:#555;font-weight:700;text-align:right;">PEDIDO</div>
              <div style="color:#4ade80;font-weight:700;text-align:right;">ENTREGADO</div>
              <div style="color:#f87171;font-weight:700;text-align:right;">DEVUELTO</div>
              ${r.items_entregados.map(it => `
                <div style="color:#ccc;">${it.nombre || it.codigo}</div>
                <div style="color:#555;text-align:right;">${it.cantidad_pedida}</div>
                <div style="color:#4ade80;text-align:right;font-weight:700;">${it.cantidad_entregada}</div>
                <div style="color:${it.cantidad_devuelta > 0 ? '#f87171' : '#555'};text-align:right;font-weight:700;">${it.cantidad_devuelta}</div>
              `).join('')}
            </div>
          </div>` : ''}
        </div>`;
    });

    if (d.sin_gestionar === 0 && d.estado_financiero !== 'LIQUIDADA') {
      html += `
        <div style="position:sticky;bottom:0;padding-top:12px;background:var(--bg,#0a0a0a);">
          <button onclick="rutaLiquidar(${ruta.id})"
            style="width:100%;padding:18px;background:#14532d;color:#4ade80;border:none;border-radius:12px;font-size:16px;font-weight:800;cursor:pointer;">
            ✅ Liquidar Ruta — ${fmt(total)}
          </button>
        </div>`;
    } else if (d.sin_gestionar > 0) {
      html += `
        <div style="background:#1a1a0d;border:1px solid #78350f;border-radius:10px;padding:12px;margin-top:8px;text-align:center;color:#fbbf24;font-size:13px;">
          ⚠ Faltan ${d.sin_gestionar} parada${d.sin_gestionar !== 1 ? 's' : ''} por gestionar
        </div>`;
    }

    body.innerHTML = html;
  } catch (e) {
    const body = document.getElementById('modal-planilla-body');
    if (body) body.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando planilla</div>';
  }
}

async function rutaLiquidar(id) {
  if (!confirm(`¿Liquidar Ruta #${id}? Esta acción confirma el cuadre financiero.`)) return;
  try {
    const r = await fetch(API + '/api/rutas/' + id + '/liquidar', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`Ruta liquidada — Total: $${Number(d.total_recaudado || 0).toLocaleString('es-CO')}`, 'exito');
      await _cargarPlanilla(id);
      await cargarListaRutas();
    } else {
      alerta(d.error || 'Error al liquidar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

function cerrarModalPlanilla() {
  const modal = document.getElementById('modal-planilla');
  if (modal) modal.style.display = 'none';
  _PLAN_RUTA_ID = null;
}

// ══════════════════════════════════════════════════════════════════
// INVENTARIO CÍCLICO — Admin tab
// ══════════════════════════════════════════════════════════════════

let _INV_SUBTAB = 'conteos';
let _INV_ALMACENES = [];

async function cargarInventario() {
  // Cargar almacenes para el selector ABC (solo una vez)
  if (_INV_ALMACENES.length === 0) {
    try {
      const r = await fetch(API + '/api/almacenes/', { headers: { Authorization: 'Bearer ' + TOKEN } });
      if (r.ok) {
        _INV_ALMACENES = await r.json();
        const sel = document.getElementById('inv-abc-almacen');
        if (sel) {
          sel.innerHTML = _INV_ALMACENES.map(a =>
            `<option value="${a.id}">${a.nombre}${a.bodega_siesa_id ? ` (${a.bodega_siesa_id})` : ''}</option>`
          ).join('');
        }
        mostrarConfigBodega();
      }
    } catch (e) { /* silencioso */ }
  }
  if (_INV_SUBTAB === 'conteos') await cargarConteos();
  else await cargarResumenAbc();
}

// ── Config bodega por almacén ─────────────────────────────────────────────────

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

function invSubtab(nombre) {
  _INV_SUBTAB = nombre;
  const tabs = { conteos: 'inv-tab-conteos', abc: 'inv-tab-abc' };
  const panels = { conteos: 'inv-panel-conteos', abc: 'inv-panel-abc' };
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
  else cargarResumenAbc();
}

let _CONTEO_PAGE = 1;
let _CONTEO_VISTA = 'accion';  // 'accion' | 'progreso' | 'resueltos'

function conteoVista(v) {
  _CONTEO_VISTA = v;
  _CONTEO_PAGE = 1;
  ['accion', 'progreso', 'resueltos'].forEach(k => {
    const btn = document.getElementById(`cv-tab-${k}`);
    if (!btn) return;
    const activo = k === v;
    if (k === 'accion') {
      btn.style.background = activo ? '#b45309' : '#111';
      btn.style.color      = activo ? '#fff'    : '#94a3b8';
      btn.style.border     = activo ? 'none'    : '1px solid #333';
    } else {
      btn.style.background = activo ? '#1E8395' : '#111';
      btn.style.color      = activo ? '#fff'    : '#94a3b8';
      btn.style.border     = activo ? 'none'    : '1px solid #333';
    }
  });
  cargarConteos();
}

function conteosFiltrar() {
  _CONTEO_PAGE = 1;
  cargarConteos();
}

// ── Render helpers por vista ──────────────────────────────────────────────────

function _tipoTag(s) {
  if (s.tipo === 'EXCEPCION_PICKING')
    return `<span style="background:#1a0a0a;color:#f87171;font-size:9px;font-weight:700;padding:1px 6px;border-radius:6px;margin-left:3px;">PICKING</span>`;
  if (s.tipo === 'MANUAL')
    return `<span style="background:#1a1a2a;color:#a78bfa;font-size:9px;font-weight:700;padding:1px 6px;border-radius:6px;margin-left:3px;">MANUAL</span>`;
  return '';
}

function _renderCardAccion(s) {
  const hijo = s.segundo_conteo;
  const TERMINADOS = ['MATCH','DESCUADRE','SEGUNDO_CONTEO','AJUSTADO','CANCELADO'];
  const hijoPendiente = hijo ? !TERMINADOS.includes(hijo.estado) : (s.estado !== 'DESCUADRE');
  const dif = s.diferencia != null ? (s.diferencia > 0 ? `+${s.diferencia}` : `${s.diferencia}`) : '?';
  const difCol = (s.diferencia || 0) > 0 ? '#4ade80' : '#f87171';
  const puedeAjustar = s.estado === 'DESCUADRE' || (s.estado === 'SEGUNDO_CONTEO' && hijo && !hijoPendiente);
  const coinciden = hijo && hijo.cantidad_fisica != null && hijo.cantidad_fisica === s.cantidad_fisica;
  const bordColor = s.estado === 'DESCUADRE' ? '#7c2d12' : '#2d1b69';

  return `<div style="background:#111;border:1px solid ${bordColor};border-radius:12px;padding:14px;margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:13px;font-weight:700;">${s.producto_codigo || '—'}${_tipoTag(s)}${s.clasificacion_abc ? `<span style="background:#1c1a0a;color:#f59e0b;font-size:9px;font-weight:700;padding:1px 5px;border-radius:6px;margin-left:4px;">ABC-${s.clasificacion_abc}</span>` : ''}</div>
        <div style="font-size:11px;color:#555;margin-top:1px;">${s.producto_nombre || ''}</div>
        <div style="font-size:11px;color:#444;margin-top:1px;">📍 ${s.ubicacion_codigo || '—'}</div>
      </div>
      <span style="background:${s.estado==='DESCUADRE'?'#7c2d12':'#4c1d95'};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;white-space:nowrap;flex-shrink:0;margin-left:8px;">${s.estado}</span>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;background:#0a0a0a;border-radius:8px;padding:10px;margin-bottom:10px;text-align:center;">
      <div>
        <div style="font-size:9px;color:#4b5563;font-weight:700;text-transform:uppercase;margin-bottom:3px;">WMS</div>
        <div style="font-size:20px;font-weight:800;color:#60a5fa;line-height:1;">${s.existencia_siesa != null ? s.existencia_siesa : '—'}</div>
        <div style="font-size:9px;color:#374151;margin-top:2px;">${s.bodega_siesa_id || 'stock'}</div>
      </div>
      <div style="border-left:1px solid #1f2937;border-right:1px solid #1f2937;">
        <div style="font-size:9px;color:#4b5563;font-weight:700;text-transform:uppercase;margin-bottom:3px;">1er Conteo</div>
        <div style="font-size:20px;font-weight:800;color:#f59e0b;line-height:1;">${s.cantidad_fisica != null ? s.cantidad_fisica : '—'}</div>
        <div style="font-size:9px;color:#374151;margin-top:2px;">${s.operario_id ? `Op #${s.operario_id}` : '—'}</div>
      </div>
      <div>
        <div style="font-size:9px;color:#4b5563;font-weight:700;text-transform:uppercase;margin-bottom:3px;">2do Conteo</div>
        ${hijo && !hijoPendiente
          ? `<div style="font-size:20px;font-weight:800;color:${coinciden?'#4ade80':'#f87171'};line-height:1;">${hijo.cantidad_fisica != null ? hijo.cantidad_fisica : '—'}</div>
             <div style="font-size:9px;color:#374151;margin-top:2px;">${hijo.operario_nombre || (hijo.operario_id ? `Op #${hijo.operario_id}` : '—')}</div>`
          : `<div style="font-size:16px;color:#374151;padding:2px 0;">⏳</div>
             <div style="font-size:9px;color:#374151;margin-top:2px;">${hijo ? (hijo.operario_nombre || (hijo.operario_id ? `Op #${hijo.operario_id}` : 'asignado')) : 'sin asignar'}</div>`
        }
      </div>
    </div>

    <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#555;margin-bottom:10px;">
      <span>Δ <span style="color:${difCol};font-weight:700;">${dif} uds</span>${s.motivo_codigo ? ` · <span style="color:${s.motivo_codigo==='AJ-ENT'?'#4ade80':'#f87171'};">${s.motivo_codigo}</span>` : ''}</span>
      ${!hijoPendiente && hijo
        ? coinciden
          ? `<span style="color:#4ade80;font-size:10px;">✓ Ambos coinciden</span>`
          : `<span style="color:#f87171;font-size:10px;">⚠ Operarios no coinciden</span>`
        : ''
      }
    </div>

    <div style="display:flex;gap:6px;">
      ${s.estado !== 'AJUSTADO' && s.estado !== 'AJUSTANDO'
        ? `<button onclick="conteoAbrirEdicion(${JSON.stringify(s).replace(/"/g,'&quot;')})"
             style="flex:1;padding:8px;background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:8px;font-size:12px;cursor:pointer;">✏ Corregir</button>`
        : ''
      }
      <button onclick="${puedeAjustar ? `conteoAbrirAjuste(${JSON.stringify(s).replace(/"/g,'&quot;')})` : 'void(0)'}"
        ${!puedeAjustar ? 'disabled' : ''}
        style="flex:2;padding:8px;background:${puedeAjustar?'#b45309':'#1a1a1a'};color:${puedeAjustar?'#fff':'#374151'};border:${puedeAjustar?'none':'1px solid #1f2937'};border-radius:8px;font-size:12px;font-weight:700;cursor:${puedeAjustar?'pointer':'not-allowed'};">
        ${hijoPendiente ? '⏳ Esperando 2do conteo' : '✓ Confirmar ajuste →'}
      </button>
      <button onclick="conteoCancelar(${s.id})" style="padding:8px;background:none;border:1px solid #7f1d1d;color:#ef4444;border-radius:8px;font-size:11px;cursor:pointer;">✕</button>
    </div>
    ${s.editado_en ? `<div style="font-size:10px;color:#78350f;margin-top:6px;">✏ Editado: ${s.motivo_edicion}</div>` : ''}
  </div>`;
}

function _renderCardProgreso(s) {
  const col = s.estado === 'EN_PROCESO' ? '#1d4ed8' : '#374151';
  return `<div style="background:#111;border:1px solid #1f2937;border-radius:10px;padding:12px;margin-bottom:6px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:12px;font-weight:700;">${s.producto_codigo || '—'}${_tipoTag(s)}</div>
        <div style="font-size:11px;color:#555;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.producto_nombre || ''}</div>
        <div style="font-size:11px;color:#444;margin-top:2px;">📍 ${s.ubicacion_codigo || '—'}${s.operario_id ? ` · 👤 Op #${s.operario_id}` : ''}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0;margin-left:8px;">
        ${s.clasificacion_abc ? `<span style="background:#1c1a0a;color:#f59e0b;font-size:9px;font-weight:700;padding:1px 5px;border-radius:6px;">ABC-${s.clasificacion_abc}</span>` : ''}
        <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;">${s.estado}</span>
        <button onclick="conteoCancelar(${s.id})" style="background:none;border:1px solid #7f1d1d;color:#ef4444;font-size:9px;padding:1px 6px;border-radius:6px;cursor:pointer;">Cancelar</button>
      </div>
    </div>
  </div>`;
}

function _renderCardResuelto(s) {
  const colMap = { MATCH:'#166534', AJUSTADO:'#065f46', AJUSTANDO:'#7c2d12', CANCELADO:'#374151' };
  const col = colMap[s.estado] || '#333';
  const dif = s.diferencia != null ? (s.diferencia > 0 ? `+${s.diferencia}` : `${s.diferencia}`) : null;
  const difCol = (s.diferencia || 0) > 0 ? '#4ade80' : (s.diferencia || 0) < 0 ? '#f87171' : '#aaa';
  return `<div style="background:#0a0a0a;border:1px solid #111;border-radius:10px;padding:12px;margin-bottom:6px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:12px;font-weight:700;color:#4b5563;">${s.producto_codigo || '—'}</div>
        <div style="font-size:11px;color:#374151;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.producto_nombre || ''}</div>
        <div style="font-size:11px;color:#2d3748;margin-top:1px;">📍 ${s.ubicacion_codigo || '—'}</div>
      </div>
      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px;flex-shrink:0;margin-left:8px;">
        <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;">${s.estado}</span>
        ${dif ? `<span style="color:${difCol};font-size:11px;font-weight:700;">Δ ${dif}</span>` : ''}
      </div>
    </div>
    ${s.aprobador_nombre ? `<div style="font-size:10px;color:#374151;margin-top:4px;">✓ ${s.aprobador_nombre}</div>` : ''}
  </div>`;
}

// ── Stats dashboard + asignación en lote ─────────────────────────────────────

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

function conteoCerrarAsignar() {
  const panel = document.getElementById('conteo-asignar-panel');
  if (panel) panel.style.display = 'none';
}

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

async function cargarConteos(page) {
  if (page !== undefined) _CONTEO_PAGE = page;
  const lista = document.getElementById('inv-conteos-lista');
  const pag   = document.getElementById('inv-conteos-paginacion');
  if (!lista) return;

  cargarConteoStats();

  const marca = document.getElementById('inv-filtro-marca')?.value?.trim() || '';
  const clase = document.getElementById('inv-filtro-clase')?.value || '';

  const VISTA_ESTADOS = {
    accion:    'SEGUNDO_CONTEO,DESCUADRE',
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

function conteosMostrarFormManual() {
  document.getElementById('conteo-form-manual').style.display = 'block';
  document.getElementById('conteo-manual-codigo').focus();
}
function conteosOcultarFormManual() {
  document.getElementById('conteo-form-manual').style.display = 'none';
  document.getElementById('conteo-manual-codigo').value = '';
  document.getElementById('conteo-manual-error').textContent = '';
}
async function crearConteoManual() {
  const almacenId = document.getElementById('inv-abc-almacen')?.value || _INV_ALMACENES[0]?.id;
  const codigo = document.getElementById('conteo-manual-codigo')?.value.trim().toUpperCase();
  const errorEl = document.getElementById('conteo-manual-error');
  errorEl.textContent = '';
  if (!codigo) { errorEl.textContent = 'Ingresa el código del producto'; return; }
  if (!almacenId) { errorEl.textContent = 'Selecciona un almacén en la pestaña ABC'; return; }
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

function conteosCerrarEdicion() {
  const m = document.getElementById('modal-conteo-edicion');
  if (m) m.style.display = 'none';
  _CONTEO_EDICION_ID = null;
}

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

function conteosCerrarAjuste() {
  const m = document.getElementById('modal-conteo-ajuste');
  if (m) m.style.display = 'none';
  _CONTEO_AJUSTE_SESION = null;
}

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

// ══════════════════════════════════════════════════════════════════
// TRASLADOS — Admin tab
// ══════════════════════════════════════════════════════════════════

let _TRAS_SUBTAB = 'pendientes';
const TRAS_ESTADO = {
  pendientes: ['BORRADOR','ENVIADA','EN_PICKING','PREPARADO'],
  transito:   ['EN_TRANSITO'],
  historial:  ['ENTREGADA','RECHAZADA','CANCELADA','REVERTIDA']
};
const TRAS_COL = {
  BORRADOR:'#374151', ENVIADA:'#1d4ed8', EN_PICKING:'#7c3aed', PREPARADO:'#166534',
  EN_TRANSITO:'#9a3412', ENTREGADA:'#065f46',
  RECHAZADA:'#7f1d1d', CANCELADA:'#374151', REVERTIDA:'#4b5563'
};

function trasSubtab(nombre) {
  _TRAS_SUBTAB = nombre;
  ['pendientes','transito','historial'].forEach(k => {
    const el = document.getElementById(`tras-tab-${k}`);
    if (!el) return;
    const activo = k === nombre;
    el.style.background = activo ? '#1E8395' : 'transparent';
    el.style.color = activo ? '#fff' : '#415A70';
    el.style.fontWeight = activo ? '700' : '400';
  });
  cargarTrasladosAdmin();
}

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
    acciones.push(`<button onclick="trasConfirmarRecepcion(${s.id})" style="flex:1;padding:10px;background:#065f46;color:#4ade80;border:1px solid #166534;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">✓ Confirmar Recepción</button>`);
    acciones.push(`<button onclick="trasVerLPNs(${s.id})" style="padding:10px 10px;background:#1a1a1a;color:#a78bfa;border:1px solid #4c1d95;border-radius:8px;font-size:11px;cursor:pointer;">📦 LPNs en ruta</button>`);
    acciones.push(`<button onclick="trasRevertir(${s.id})" style="padding:10px 10px;background:#1a1a1a;color:#f87171;border:1px solid #7f1d1d;border-radius:8px;font-size:11px;cursor:pointer;">↩ Revertir</button>`);
  }
  if (s.siesa_necesita_atencion && s.estado === 'EN_TRANSITO') {
    acciones.push(`<button onclick="trasReintentarDespachoSiesa(${s.id})" style="flex:1;padding:10px;background:#7c3aed;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">⚠ Reintentar Siesa</button>`);
  }
  if (s.estado === 'ENTREGADA' && s.modo_transferencia === 'EN_TRANSITO' && !s.siesa_entrada_consec) {
    acciones.push(`<button onclick="trasReintentarRecepcionSiesa(${s.id})" style="flex:1;padding:10px;background:#7c2d12;color:#fed7aa;border:1px solid #9a3412;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">⚠ Reintentar entrada Siesa</button>`);
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

async function trasConfirmarRecogida(id) {
  if (!confirm('¿Confirmar recogida completa? El traslado pasará a PREPARADO y podrás despacharlo.')) return;
  try {
    await post(`/api/traslados/${id}/confirmar-picking`, {});
    alerta('Recogida confirmada — listo para despachar', 'exito');
    cargarTrasladosAdmin();
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

async function trasDespacharDirecto(id) {
  if (!confirm('¿Despachar directamente sin confirmar picking? Se usarán las cantidades aprobadas como enviadas.')) return;
  try {
    await post(`/api/traslados/${id}/despachar`, {});
    alerta('Despachado — mercancía en tránsito', 'exito');
    cargarTrasladosAdmin();
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

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

function tiendaFiltrarStock() {
  _TIENDA_FILTRO = (document.getElementById('tienda-buscar')?.value || '').toLowerCase();
  _TIENDA_PAGINA = 1; // reset al filtrar
  tiendaRenderStock();
}

function tiendaIrPagina(p) {
  _TIENDA_PAGINA = p;
  tiendaRenderStock();
  document.getElementById('tienda-stock-lista')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

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

function tiendaQuitarCarrito(codigoSiesa) {
  _TIENDA_CARRITO = _TIENDA_CARRITO.filter(c => c.codigo_siesa !== codigoSiesa);
  tiendaActualizarCarrito();
  tiendaRenderStock();
}

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

function tiendaVolverListaRecibir() {
  _TIENDA_TRASLADO_ACTIVO = null;
  _TIENDA_CONTEOS = {};
  tiendaCargarRecibir();
}

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

async function tiendaScanTraslado(codigo) {
  if (!_TIENDA_TRASLADO_ACTIVO) return;
  const items = _TIENDA_TRASLADO_ACTIVO.items || [];

  // 1. Match directo por codigo_siesa o producto_codigo
  let item = items.find(i =>
    i.producto_codigo_siesa === codigo ||
    i.producto_codigo === codigo
  );

  // 2. Si no hay match directo, resolver via API (codigo de barras del producto)
  if (!item) {
    try {
      const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
      if (prod && prod.producto_id) {
        item = items.find(i => i.producto_id === prod.producto_id);
      }
    } catch (_) { /* continúa con item=undefined */ }
  }

  if (!item) {
    alerta('Código no encontrado en este traslado: ' + codigo, 'error');
    return;
  }

  tiendaContarItem(item.producto_id, 1);
  // Re-enfocar el input para el próximo escaneo
  const inp = document.getElementById('tienda-scan-input');
  if (inp) inp.focus();
}

function tiendaContarItem(productoId, delta) {
  if (!_TIENDA_TRASLADO_ACTIVO) return;
  const item = (_TIENDA_TRASLADO_ACTIVO.items || []).find(i => i.producto_id === productoId);
  if (!item) return;
  const actual = _TIENDA_CONTEOS[productoId] || 0;
  _TIENDA_CONTEOS[productoId] = Math.max(0, actual + delta);
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

async function tiendaOCContinuar(id) {
  try {
    const r = await get('/api/tienda-oc/' + id);
    if (r.error) { alerta(r.error, 'error'); return; }
    _TIENDA_OC_RECEPCION = r;
    _tiendaOCRenderScan();
  } catch (e) { alerta('Error cargando recepción', 'error'); }
}

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

async function tiendaOCProcesarScan(codigo) {
  if (!_TIENDA_OC_RECEPCION) return;
  vibrar(); flash();

  try {
    const scan = await get('/api/empaques/scan/' + encodeURIComponent(codigo) +
      '?almacen_id=' + (_TIENDA_OC_RECEPCION.almacen_id || ''));

    if (scan.tipo === 'GS1_AMBIGUO') {
      alerta('Código ambiguo — usa búsqueda manual', 'advertencia');
      return;
    }

    if (scan.tipo === 'NO_ENCONTRADO') {
      const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
      if (prod.error || !prod.producto_id) {
        alerta('Código no reconocido: ' + codigo, 'error');
        beepError();
        return;
      }
      const esEmp = prod.es_empaque || false;
      await _tiendaOCRegistrarScan(prod.producto_id, 1, esEmp, false);
      if (esEmp && prod.factor_conversion > 1) alerta(`Empaque → +${prod.factor_conversion} UND`, 'info');
      return;
    }

    const productoId = scan.producto ? scan.producto.id : null;
    if (!productoId) { alerta('Producto no identificado', 'error'); return; }

    const factor = scan.factor || 1;
    const esEmpaqueScan = factor > 1;
    const unidad = scan.empaque ? scan.empaque.unidad_medida : 'UND';

    if (scan.tipo === 'LPN') {
      await _tiendaOCRegistrarScan(productoId, scan.lpn.cantidad_actual, false, false);
      alerta(`LPN ${codigo} → +${scan.lpn.cantidad_actual} UND`, 'exito');
      return;
    }

    await _tiendaOCRegistrarScan(productoId, 1, esEmpaqueScan, false);
    if (esEmpaqueScan) alerta(`${unidad} escaneada → +${factor} UND`, 'info');

  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

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

async function _tiendaOCEscanearBono(codigo) {
  vibrar(); flash();
  try {
    const scan = await get('/api/empaques/scan/' + encodeURIComponent(codigo) +
      '?almacen_id=' + (_TIENDA_OC_RECEPCION.almacen_id || ''));
    let productoId;
    if (scan.tipo === 'NO_ENCONTRADO') {
      const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
      if (prod.error || !prod.producto_id) { alerta('Código no reconocido', 'error'); return; }
      productoId = prod.producto_id;
    } else {
      productoId = scan.producto ? scan.producto.id : null;
      if (!productoId) { alerta('Producto no identificado', 'error'); return; }
    }
    await _tiendaOCRegistrarScan(productoId, 1, false, true);
  } catch (e) { beepError(); alerta('Error al escanear bonificación', 'error'); }
}

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

function tiendaOCVolverLista() {
  _TIENDA_OC_RECEPCION = null;
  tiendaOCCargar();
}


// ══════════════════════════════════════════════════════════════════════════════
// MÓDULO ABASTECEDOR — Reposición RESERVA → PICKING
// ══════════════════════════════════════════════════════════════════════════════

let ABAST_TAREA = null;       // tarea activa del abastecedor
let ABAST_TIMER = null;       // polling automático

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

function abastCerrarHUD() {
  const hud = document.getElementById('abast-hud');
  const cont = document.getElementById('abast-contenido');
  if (hud) hud.style.display = 'none';
  if (cont) cont.style.display = 'block';
  ABAST_TAREA = null;
  abastCargarTarea();
}

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

function _abastFlash(color) {
  const f = document.getElementById('abast-flash');
  if (!f) return;
  f.style.background = color + '66';
  setTimeout(() => { f.style.background = 'transparent'; }, 300);
}

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

function repSubtab(sec) {
  _repSubActual = sec;
  ['ubicaciones','tareas','huerfanas','jobs'].forEach(s => {
    const cont = document.getElementById(`rep-sec-${s}`);
    const btn  = document.getElementById(`rep-sub-${s}`);
    if (!cont || !btn) return;
    const activo = s === sec;
    cont.style.display = activo ? 'block' : 'none';
    btn.style.background = activo ? 'var(--pm)' : 'transparent';
    btn.style.color = activo ? '#fff' : 'var(--tx3)';
    btn.style.fontWeight = activo ? '700' : '400';
  });
  if (sec === 'ubicaciones') repCargarUbicaciones();
  else if (sec === 'tareas')   repCargarTareas();
  else if (sec === 'huerfanas') repCargarHuerfanas();
  else if (sec === 'jobs')     repCargarJobs();
}

async function cargarReposicion() {
  // Revisar alerta de jobs fallidos en paralelo
  try {
    const d = await get('/api/reposicion/siesa-jobs/fallidos');
    const alerta = document.getElementById('rep-alerta-jobs');
    const txt    = document.getElementById('rep-alerta-jobs-txt');
    if (alerta && txt) {
      if (d.total > 0 && d.alerta) {
        alerta.style.display = 'block';
        txt.textContent = d.alerta;
      } else {
        alerta.style.display = 'none';
      }
    }
  } catch (_) {}

  // Cargar la sección activa
  repSubtab(_repSubActual);
}

// ── SECCIÓN 1: Ubicaciones PICKING ───────────────────────────────────────────

async function repCargarUbicaciones() {
  const el = document.getElementById('rep-lista-ubicaciones');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';
  try {
    const d = await get('/api/reposicion/ubicaciones');
    const ubs = d.ubicaciones || [];

    if (!ubs.length) {
      el.innerHTML = `
        <div style="text-align:center;padding:40px;color:#555;">
          <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">📍</div>
          <div style="font-size:15px;font-weight:600;">Sin ubicaciones PICKING registradas</div>
          <div style="font-size:12px;margin-top:6px;">Haz sync desde Siesa para importar ubicaciones PIK-*</div>
        </div>`;
      return;
    }

    el.innerHTML = ubs.map(u => {
      const actual    = u.stock_actual ?? 0;
      const minimo    = u.stock_minimo;
      const maximo    = u.stock_maximo;
      const sinLimite = minimo == null;
      const sku       = u.sku_asignado;
      const skuLabel  = sku ? `${sku.codigo} — ${sku.nombre}` : null;

      // Semáforo
      let color, label, pct = 0;
      if (sinLimite) {
        color = '#555'; label = 'Sin límite';
      } else if (actual < minimo) {
        color = '#ef4444'; label = 'Crítico';
        pct = Math.min(100, Math.round((actual / minimo) * 100));
      } else if (actual < minimo * 1.3) {
        color = '#f59e0b'; label = 'Alerta';
        pct = maximo ? Math.min(100, Math.round((actual / maximo) * 100)) : 60;
      } else {
        color = '#22c55e'; label = 'OK';
        pct = maximo ? Math.min(100, Math.round((actual / maximo) * 100)) : 80;
      }

      const skuEsc = skuLabel ? skuLabel.replace(/'/g, "\\'") : '';

      return `
        <div class="tabla-card" style="margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
            <div>
              <div style="font-size:16px;font-weight:800;font-family:monospace;color:var(--tx);">${u.codigo}</div>
              ${skuLabel
                ? `<div style="font-size:11px;color:#60a5fa;margin-top:3px;font-weight:600;">📦 ${skuLabel}</div>`
                : `<div style="font-size:11px;color:#555;margin-top:3px;">Sin producto asignado</div>`
              }
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${label}</span>
              <button onclick="repAbrirModal(${u.id}, '${u.codigo}', ${minimo ?? ''}, ${maximo ?? ''}, ${u.secuencia_ruteo ?? ''}, '${skuEsc}')"
                style="padding:5px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:11px;cursor:pointer;">
                Configurar
              </button>
            </div>
          </div>

          <!-- Barra de stock -->
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
            <div style="flex:1;background:var(--brd);border-radius:6px;height:8px;overflow:hidden;">
              <div style="width:${sinLimite ? 0 : pct}%;height:100%;background:${color};border-radius:6px;transition:width .3s;"></div>
            </div>
            <div style="font-size:13px;font-weight:700;color:var(--tx);min-width:36px;text-align:right;">${actual}</div>
          </div>

          <!-- Límites -->
          <div style="display:flex;gap:16px;font-size:11px;color:var(--tx3);">
            ${sinLimite
              ? `<span style="color:#f59e0b;">⚠ Sin mínimo/máximo — toca "Configurar" para activar reposición</span>`
              : `<span>Mín <strong style="color:var(--tx);">${minimo}</strong></span>
                 <span>Máx <strong style="color:var(--tx);">${maximo ?? '—'}</strong></span>
                 ${u.secuencia_ruteo != null ? `<span>Seq <strong style="color:var(--tx);">${u.secuencia_ruteo}</strong></span>` : ''}
                 <span style="color:#22c55e;">✓ Motor activo</span>`
            }
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando ubicaciones</div>';
  }
}

// Modal configurar límites
function repAbrirModal(ubId, codigo, min, max, seq, sku) {
  _repModalUbId = ubId;
  const m = document.getElementById('modal-rep-limites');
  if (!m) return;
  document.getElementById('rep-modal-ub-nombre').textContent = codigo;
  document.getElementById('rep-modal-min').value = min !== '' ? min : '';
  document.getElementById('rep-modal-max').value = max !== '' ? max : '';
  document.getElementById('rep-modal-seq').value = seq !== '' ? seq : '';
  const skuEl = document.getElementById('rep-modal-sku');
  if (skuEl) skuEl.textContent = sku || 'Sin producto (vacía)';
  m.style.display = 'flex';
}

function repCerrarModal() {
  const m = document.getElementById('modal-rep-limites');
  if (m) m.style.display = 'none';
  _repModalUbId = null;
}

async function repGuardarLimites() {
  if (!_repModalUbId) return;
  const min = parseInt(document.getElementById('rep-modal-min')?.value);
  const max = parseInt(document.getElementById('rep-modal-max')?.value);
  const seq = parseInt(document.getElementById('rep-modal-seq')?.value);

  const payload = {};
  if (!isNaN(min)) payload.stock_minimo = min;
  if (!isNaN(max)) payload.stock_maximo = max;
  if (!isNaN(seq)) payload.secuencia_ruteo = seq;

  if (!Object.keys(payload).length) { alerta('Ingresa al menos un límite', 'error'); return; }

  try {
    const r = await fetch(API + `/api/reposicion/ubicacion/${_repModalUbId}/limites`, {
      method: 'PATCH',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      alerta('Límites guardados', 'ok');
      repCerrarModal();
      repCargarUbicaciones();
    } else {
      alerta(d.error || 'Error guardando', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function repVerificarStock() {
  try {
    const r = await fetch(API + '/api/reposicion/verificar-stock', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ almacen_id: ALMACEN_ID }),
    });
    const d = await r.json();
    if (r.ok) {
      const n = d.tareas_generadas || 0;
      alerta(n > 0 ? `${n} tarea${n > 1 ? 's' : ''} de reposición generada${n > 1 ? 's' : ''}` : 'Stock en niveles correctos — sin nuevas tareas', n > 0 ? 'ok' : 'info');
      repCargarUbicaciones();
    } else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function repSyncUbicaciones() {
  try {
    const r = await fetch(API + '/api/reposicion/sync-ubicaciones', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await r.json();
    if (r.ok) {
      alerta(d.mensaje || 'Sync iniciado en background', 'ok');
    } else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── SECCIÓN 2: Tareas de Reposición ─────────────────────────────────────────

async function repCargarTareas() {
  const el = document.getElementById('rep-lista-tareas');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';

  const estado = document.getElementById('rep-filtro-estado')?.value || 'PENDIENTE';

  try {
    let url = '/api/reposicion/pendientes';
    if (estado === 'EN_PROCESO') url += '?estado=EN_PROCESO';
    else if (estado === 'COMPLETADA') url += '?estado=COMPLETADA';
    else if (estado === 'CANCELADA') url += '?estado=CANCELADA';

    const d = await get(url);
    const tareas = d.tareas || [];

    if (!tareas.length) {
      el.innerHTML = `<div style="text-align:center;padding:40px;color:#555;">Sin tareas ${estado.toLowerCase()}s</div>`;
      return;
    }

    const estadoColor = { PENDIENTE: '#f59e0b', EN_PROCESO: '#3b82f6', COMPLETADA: '#22c55e', CANCELADA: '#555' };

    el.innerHTML = tareas.map(t => {
      const color = estadoColor[t.estado] || '#888';
      const fecha = t.fecha_creacion ? new Date(t.fecha_creacion).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
      return `
        <div class="tabla-card" style="margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
            <div>
              <div style="font-size:13px;font-weight:700;font-family:monospace;color:var(--tx);">${t.codigo}</div>
              <div style="font-size:12px;color:var(--tx3);margin-top:2px;">${fecha}</div>
            </div>
            <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${t.estado}</span>
          </div>

          <div style="font-size:13px;font-weight:600;color:var(--tx);margin-bottom:6px;">${t.producto_nombre || t.producto_codigo || '—'}</div>

          <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
            <span style="font-size:12px;font-family:monospace;background:#166534;color:#4ade80;padding:3px 8px;border-radius:6px;">${t.ubicacion_reserva || '—'}</span>
            <span style="font-size:14px;color:#555;">→</span>
            <span style="font-size:12px;font-family:monospace;background:#1e3a5f;color:#60a5fa;padding:3px 8px;border-radius:6px;">${t.ubicacion_picking || '—'}</span>
            <span style="font-size:12px;color:var(--tx3);">${t.cantidad_unidades} uds</span>
          </div>

          <div style="display:flex;gap:6px;justify-content:flex-end;">
            ${t.lpn_codigo ? `<span style="font-size:11px;color:#555;font-family:monospace;">LPN: ${t.lpn_codigo}</span>` : ''}
            ${(t.estado === 'PENDIENTE' || t.estado === 'EN_PROCESO') ? `
              <button onclick="repCancelarTarea(${t.id}, '${t.codigo}')"
                style="padding:5px 10px;background:var(--bg);border:1px solid #7f1d1d;border-radius:6px;color:#f87171;font-size:11px;cursor:pointer;">
                Cancelar
              </button>` : ''
            }
            ${t.siesa_enviado ? `<span style="font-size:11px;color:#22c55e;">✓ Siesa</span>` : (t.estado === 'COMPLETADA' ? `<span style="font-size:11px;color:#f59e0b;">⏳ Pendiente Siesa</span>` : '')}
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando tareas</div>';
  }
}

async function repCancelarTarea(id, codigo) {
  if (!confirm(`¿Cancelar tarea ${codigo}?`)) return;
  try {
    const r = await fetch(API + `/api/reposicion/cancelar/${id}`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo: 'Cancelada desde admin' }),
    });
    const d = await r.json();
    if (r.ok && d.ok) { alerta('Tarea cancelada', 'ok'); repCargarTareas(); }
    else alerta(d.error || 'Error', 'error');
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── SECCIÓN 3: Ubicaciones Huérfanas ─────────────────────────────────────────

async function repCargarHuerfanas() {
  const el = document.getElementById('rep-lista-huerfanas');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';
  try {
    const d = await get('/api/reposicion/ubicaciones-huerfanas');
    const items = d.huerfanas || [];

    if (!items.length) {
      el.innerHTML = `
        <div style="text-align:center;padding:40px;color:#555;">
          <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">✅</div>
          <div style="font-size:15px;font-weight:600;">Sin ubicaciones huérfanas</div>
          <div style="font-size:12px;margin-top:6px;">Todos los códigos de Siesa tienen prefijo válido.</div>
        </div>`;
      return;
    }

    el.innerHTML = `
      <div style="font-size:12px;color:#f59e0b;margin-bottom:10px;font-weight:600;">${items.length} ubicación${items.length > 1 ? 'es' : ''} requiere${items.length === 1 ? '' : 'n'} corrección en Siesa</div>
      ${items.map(h => {
        const ultima = h.fecha_ultima_vez ? new Date(h.fecha_ultima_vez).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
        return `
          <div class="tabla-card" style="margin-bottom:10px;border-left:3px solid #f59e0b;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;">
              <div>
                <div style="font-size:15px;font-weight:800;font-family:monospace;color:#fbbf24;">${h.codigo_siesa}</div>
                <div style="font-size:11px;color:var(--tx3);margin-top:2px;">${h.descripcion || 'Sin descripción'} · Bodega: ${h.bodega_id}</div>
              </div>
              <span style="font-size:11px;color:#f59e0b;background:#78350f44;padding:3px 8px;border-radius:20px;">${h.veces_detectada}x detectada</span>
            </div>
            <div style="font-size:11px;color:var(--tx3);margin-top:8px;">
              Última vez: ${ultima} · Estado Siesa: ${h.activo_siesa ? 'Activo' : 'Inactivo'}
            </div>
            <div style="font-size:11px;color:#92400e;margin-top:6px;background:#78350f22;padding:6px 8px;border-radius:6px;">
              Acción: renombrar en Siesa con prefijo <strong>PIK-</strong>, <strong>RES-</strong> o <strong>AVE-</strong> y esperar sync nocturno (03:00)
            </div>
          </div>`;
      }).join('')}`;
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando datos</div>';
  }
}

// ── SECCIÓN 4: Jobs Siesa (DLQ) ──────────────────────────────────────────────

async function repCargarJobs() {
  const el = document.getElementById('rep-lista-jobs');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';

  const filtro = document.getElementById('rep-filtro-job')?.value || 'FALLIDO';

  try {
    let d;
    if (filtro === 'FALLIDO') {
      d = await get('/api/reposicion/siesa-jobs/fallidos');
      const jobs = d.jobs || [];

      if (!jobs.length) {
        el.innerHTML = `
          <div style="text-align:center;padding:40px;color:#555;">
            <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">✅</div>
            <div style="font-size:15px;font-weight:600;">Sin jobs fallidos</div>
            <div style="font-size:12px;margin-top:6px;">Todas las transferencias a Siesa se procesaron correctamente.</div>
          </div>`;
        return;
      }

      el.innerHTML = jobs.map(j => _repJobCard(j, true)).join('');
    } else {
      d = await get(`/api/reposicion/siesa-jobs?estado=${filtro}`);
      const jobs = d.jobs || [];
      if (!jobs.length) {
        el.innerHTML = `<div style="text-align:center;padding:40px;color:#555;">Sin jobs ${filtro.toLowerCase()}s</div>`;
        return;
      }
      el.innerHTML = jobs.map(j => _repJobCard(j, false)).join('');
    }
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando jobs</div>';
  }
}

function _repJobCard(j, mostrarReintentar) {
  const estadoColor = { PENDIENTE: '#f59e0b', COMPLETADO: '#22c55e', FALLIDO: '#ef4444' };
  const color = estadoColor[j.estado] || '#888';
  const fecha = j.fecha_creacion ? new Date(j.fecha_creacion).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
  const proximo = j.proximo_intento ? new Date(j.proximo_intento).toLocaleString('es-CO', { hour:'2-digit', minute:'2-digit' }) : null;

  return `
    <div class="tabla-card" style="margin-bottom:10px;${j.estado === 'FALLIDO' ? 'border-left:3px solid #ef4444;' : ''}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:12px;font-weight:700;color:var(--tx);">${j.tipo || '—'}</div>
          <div style="font-size:11px;color:var(--tx3);margin-top:2px;">${fecha}</div>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:11px;color:#666;">${j.intentos || 0}/${j.max_intentos || 3} intentos</span>
          <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${j.estado}</span>
        </div>
      </div>
      ${j.error_ultimo ? `
        <div style="font-size:11px;color:#f87171;background:#7f1d1d22;padding:6px 8px;border-radius:6px;margin-bottom:8px;font-family:monospace;word-break:break-all;">
          ${j.error_ultimo.slice(0, 200)}
        </div>` : ''
      }
      ${proximo && j.estado === 'PENDIENTE' ? `
        <div style="font-size:11px;color:#f59e0b;margin-bottom:8px;">Próximo intento: ${proximo}</div>` : ''
      }
      ${j.referencia_tipo ? `
        <div style="font-size:11px;color:var(--tx3);">Ref: ${j.referencia_tipo} #${j.referencia_id || '—'}</div>` : ''
      }
      ${mostrarReintentar ? `
        <div style="display:flex;justify-content:flex-end;margin-top:8px;">
          <button onclick="repReintentar(${j.id})"
            style="padding:6px 14px;background:var(--pm);border:none;border-radius:6px;color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
            Reintentar ahora
          </button>
        </div>` : ''
      }
    </div>`;
}

async function repReintentar(jobId) {
  try {
    const r = await fetch(API + `/api/reposicion/siesa-jobs/${jobId}/reintentar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await r.json();
    if (r.ok && d.ok) { alerta('Job enviado a reintentar', 'ok'); repCargarJobs(); }
    else alerta(d.error || 'Error', 'error');
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function repReintentarTodosFallidos(btn) {
  if (!confirm('¿Reintentar TODOS los jobs DESPACHO_F470 fallidos? Esto enviará las facturas pendientes a Siesa.')) return;
  if (btn) { btn.disabled = true; btn.textContent = 'Procesando...'; }
  try {
    const r = await fetch(API + '/api/siesa/resetear-jobs-fallidos', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
    });
    const d = await r.json();
    if (r.ok) {
      alerta(d.mensaje || `${d.reseteados} job(s) enviados a reintentar`, 'ok');
      repCargarJobs();
    } else {
      alerta(d.error || 'Error al reintentar', 'error');
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Reintentar todos los fallidos'; }
  }
}

async function repTestEmail(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Enviando...'; }
  try {
    const r = await fetch(API + '/api/reposicion/alertas/test-email', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      alerta('Email enviado — revisa la bandeja de wms@papeleriamedellin.com.co', 'ok');
    } else {
      alerta(d.error || 'Error SMTP — revisa las variables en Railway', 'error');
    }
  } catch (e) {
    alerta(`Error: ${e.message || 'no se pudo conectar al servidor'}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Enviar email de prueba ahora'; }
  }
}


// ─────────────────────────────────────────────────────────────────────────────
// MÓDULO REQUISICIONES — Solicitudes de traslado enviadas desde tienda
// Responsabilidad única: mostrar requisiciones y disparar despacho.
// No comparte estado ni funciones con tab-pedidos ni tab-traslados.
// ─────────────────────────────────────────────────────────────────────────────

const _REQ_ESTADOS = ['ENVIADA', 'EN_PICKING', 'EN_PACKING', 'PREPARADO'];

async function cargarRequisiciones() {
  const lista = document.getElementById('req-lista');
  if (!lista) return;
  lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--tx3);">Cargando...</div>';
  try {
    const promesas = _REQ_ESTADOS.map(e => get(`/api/traslados/?estado=${e}`).catch(() => ({ solicitudes: [] })));
    const resultados = await Promise.all(promesas);
    const todas = resultados.flatMap(r => r.solicitudes || []);
    const _prioEstado = { 'ENVIADA': 0, 'EN_PICKING': 1, 'EN_PACKING': 2, 'PREPARADO': 3 };
    todas.sort((a, b) => {
      const ep = (_prioEstado[a.estado] ?? 99) - (_prioEstado[b.estado] ?? 99);
      if (ep !== 0) return ep;
      return new Date(b.fecha_creacion) - new Date(a.fecha_creacion);
    });
    if (!todas.length) {
      lista.innerHTML = '<div style="text-align:center;padding:40px;color:var(--tx3);">Sin requisiciones pendientes</div>';
      return;
    }
    lista.innerHTML = todas.map(r => _renderRequisicionCard(r)).join('');
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error cargando requisiciones</div>';
  }
}

const _REQ_BODEGA_NOMBRES = {
  'NB1':'Bodega Principal','NC1':'Neiva Centro','NS1':'Neiva Sur Principal',
  'NS2':'Neiva Sur Fundación','FC1':'Florencia Centro','PC1':'Pitalito Centro',
  'PT1':'Pitalito Terminal','FF1':'Feria Florencia','FN1':'Feria Neiva','FP1':'Feria Pitalito',
};
function _reqNombreBodega(id) {
  return id ? (_REQ_BODEGA_NOMBRES[id] ? `${_REQ_BODEGA_NOMBRES[id]} (${id})` : id) : '—';
}

function _renderRequisicionCard(r) {
  const BADGE = {
    ENVIADA:    { color: '#d97706', bg: '#fef3c7', label: '⏳ Pendiente aprobar' },
    EN_PICKING: { color: '#2563eb', bg: '#dbeafe', label: '🔍 En picking' },
    EN_PACKING: { color: '#ea580c', bg: '#fff7ed', label: '📦 En empaque' },
    PREPARADO:  { color: '#7c3aed', bg: '#ede9fe', label: '✅ Listo despachar' },
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
      ? `<button onclick="aprobarRequisicion(${r.id})"
           style="padding:8px 16px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;
                  background:#16a34a;color:#fff;border:1px solid #16a34a;">
           ✓ Aprobar
         </button>`
    : r.estado === 'EN_PICKING'
      ? `<span style="font-size:12px;color:#2563eb;font-weight:600;">🔍 Operario pickeando...</span>`
    : r.estado === 'EN_PACKING'
      ? `<div style="text-align:right;">
           <span style="font-size:12px;color:#ea580c;font-weight:600;">📦 Empacador verificando...</span>
           ${r.packing_info ? `<div style="font-size:10px;color:#6b7280;margin-top:2px;">${r.packing_info.codigo} · ${r.packing_info.empacador || 'sin asignar'}</div>` : ''}
         </div>`
    : r.estado === 'PREPARADO'
      ? `<button onclick="despacharRequisicion(${r.id})"
           style="padding:8px 16px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;
                  background:#111;color:#fff;border:1px solid #111;">
           🚚 Despachar
         </button>`
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

function trasPickerDelta(delta) {
  if (!TRAS_PICK) return;
  const item = TRAS_PICK.items[TRAS_PICK.idx];
  TRAS_PICK.counts[item.item_id] = Math.max(0, Math.min((TRAS_PICK.counts[item.item_id] || 0) + delta, item.cantidad_aprobada));
  _trasPickerRenderHUD();
}

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

async function trasPickerSiguiente() {
  if (!TRAS_PICK) return;
  if (TRAS_PICK.idx < TRAS_PICK.items.length - 1) { TRAS_PICK.idx++; _trasPickerRenderHUD(); return; }
  await _trasPickerConfirmar();
}

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

function trasPickerPausarHUD() {
  cerrarCamara('tpick-cambox');
  document.getElementById('tpick-hud').style.display = 'none';
  TRAS_PICK = null;
}

function trasPickerProblema() {
  if (!TRAS_PICK) return;
  alerta('Reporta el problema al supervisor — código: ' + TRAS_PICK.codigo, 'advertencia');
}


// PACKER TRASLADO

let TRAS_PACK = null;

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

function trasPackerDelta(delta) {
  if (!TRAS_PACK) return;
  const pendientes = TRAS_PACK.items.filter(i => !(TRAS_PACK.counts[i.item_id] >= (i.cantidad_enviada || i.cantidad_aprobada || 0)));
  const item = pendientes[0] || TRAS_PACK.items[0];
  const max  = item.cantidad_enviada || item.cantidad_aprobada || 0;
  TRAS_PACK.counts[item.item_id] = Math.max(0, Math.min((TRAS_PACK.counts[item.item_id] || 0) + delta, max));
  _trasPackerRenderHUD();
}

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

async function trasPackerSiguiente() {
  if (!TRAS_PACK) return;
  const todoListo = TRAS_PACK.items.every(i => TRAS_PACK.counts[i.item_id] >= (i.cantidad_enviada || i.cantidad_aprobada || 0));
  if (!todoListo) { _trasPackerRenderHUD(); return; }
  await _trasPackerConfirmar();
}

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

function compIniciarPantalla() {
  COMP_PANTALLA = true;
  compCargarResumen('comp2');
  compCargarVelocity('comp2');
  COMP_TIMER = setInterval(() => {
    compCargarResumen(COMP_PANTALLA ? 'comp2' : 'comp');
  }, 60000);
}

async function cargarCompras() {
  COMP_PANTALLA = false;
  await Promise.all([
    compCargarResumen('comp'),
    compCargarVelocity('comp'),
  ]);
}

// ── Sub-tabs (admin tab-compras) ──────────────────────────────────────────
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
