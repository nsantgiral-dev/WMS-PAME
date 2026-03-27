const API_BASE = window.location.origin;
let TOKEN = localStorage.getItem('wms_token');
let OPERARIO = JSON.parse(localStorage.getItem('wms_operario') || 'null');
let TAREA_ACTUAL = null;
let COLA_OFFLINE = JSON.parse(localStorage.getItem('wms_cola_offline') || '[]');
let SCANNER_BUFFER = '';
let SCANNER_TIMEOUT = null;
let CAMARA_ACTIVA = false;
let HTML5QR = null;
let CHART_ACTIVIDAD = null;
let TAB_ACTUAL = 'tab-dashboard';
let ADMIN_ALMACEN_ID = 1;

// ============================================
// INICIALIZACIÓN
// ============================================
document.addEventListener('DOMContentLoaded', () => {
  registrarServiceWorker();
  monitorearConexion();
  inicializarScannerLaser();

  if (TOKEN && OPERARIO) {
    mostrarPantallaSegunRol(OPERARIO.rol);
  } else {
    mostrarPantalla('pantalla-login');
  }
});

// ============================================
// ROLES — pantalla según quien entra
// ============================================
function mostrarPantallaSegunRol(rol) {
  const rolesAdmin = ['admin', 'gerente', 'jefe_almacen', 'supervisor'];
  const rolesRecepcion = ['recepcionista'];

  detenerRefresh();

  if (rolesAdmin.includes(rol)) {
    mostrarPantalla('pantalla-admin');
    cargarDashboardAdmin();
    iniciarAutoRefreshAdmin();
  } else if (rolesRecepcion.includes(rol)) {
    mostrarPantalla('pantalla-recepcion');
    cargarRecepcionesActivas();
  } else {
    mostrarPantalla('pantalla-operario');
    cargarTareaActual();
    iniciarAutoRefreshOperario();
  }
}

// ============================================
// SERVICE WORKER
// ============================================
async function registrarServiceWorker() {
  if ('serviceWorker' in navigator) {
    try {
      await navigator.serviceWorker.register('/static/pwa/sw.js');
      navigator.serviceWorker.addEventListener('message', event => {
        if (event.data.tipo === 'SINCRONIZAR') sincronizarColaOffline();
      });
    } catch (e) { console.error('SW error:', e); }
  }
}

// ============================================
// SCANNER LÁSER
// ============================================
function inicializarScannerLaser() {
  const input = document.getElementById('scanner-input');
  if (!input) return;

  const mantenerFocus = () => {
    const activo = document.activeElement;
    const esInputUsuario = activo && (
      activo.id === 'login-email' ||
      activo.id === 'login-password' ||
      activo.tagName === 'INPUT' ||
      activo.tagName === 'TEXTAREA'
    );
    if (!CAMARA_ACTIVA && !esInputUsuario && activo !== input) {
      input.focus();
    }
  };

  document.addEventListener('click', mantenerFocus);
  document.addEventListener('touchend', mantenerFocus);
  setInterval(mantenerFocus, 500);

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && SCANNER_BUFFER.trim()) {
      const codigo = SCANNER_BUFFER.trim();
      SCANNER_BUFFER = '';
      clearTimeout(SCANNER_TIMEOUT);
      procesarCodigoEscaneado(codigo);
    } else {
      SCANNER_BUFFER += e.key;
      clearTimeout(SCANNER_TIMEOUT);
      SCANNER_TIMEOUT = setTimeout(() => { SCANNER_BUFFER = ''; }, 100);
    }
  });
}

// ============================================
// CONEXIÓN Y OFFLINE
// ============================================
function monitorearConexion() {
  const actualizar = () => {
    const online = navigator.onLine;
    ['conexion-status', 'conexion-status-admin'].forEach(id => {
      const el = document.getElementById(id);
      if (el) {
        el.textContent = online ? '● Online' : '● Offline';
        el.style.color = online ? '#22c55e' : '#ef4444';
      }
    });
    if (online && COLA_OFFLINE.length > 0) sincronizarColaOffline();
  };
  window.addEventListener('online', actualizar);
  window.addEventListener('offline', actualizar);
  actualizar();
}

async function sincronizarColaOffline() {
  if (COLA_OFFLINE.length === 0) return;
  try {
    const resp = await apiPost('/api/mobile/sync', { cola: COLA_OFFLINE });
    if (resp.sincronizados > 0) {
      COLA_OFFLINE = [];
      localStorage.setItem('wms_cola_offline', '[]');
      mostrarAlerta(`✓ ${resp.sincronizados} tarea(s) sincronizadas`, 'exito');
    }
  } catch (e) { console.error('Error sincronizando:', e); }
}

function guardarEnColaOffline(datos) {
  COLA_OFFLINE.push({ ...datos, timestamp: Date.now() });
  localStorage.setItem('wms_cola_offline', JSON.stringify(COLA_OFFLINE));
  mostrarAlerta('Sin WiFi — guardado para sincronizar después', 'advertencia');
}

// ============================================
// API
// ============================================
async function apiPost(endpoint, body) {
  const resp = await fetch(API_BASE + endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${TOKEN}` },
    body: JSON.stringify(body)
  });
  if (resp.status === 401) { cerrarSesion(); throw new Error('Sesión expirada'); }
  return resp.json();
}

async function apiGet(endpoint) {
  const resp = await fetch(API_BASE + endpoint, {
    headers: { 'Authorization': `Bearer ${TOKEN}` }
  });
  if (resp.status === 401) { cerrarSesion(); throw new Error('Sesión expirada'); }
  return resp.json();
}

async function apiPut(endpoint, body = {}) {
  const resp = await fetch(API_BASE + endpoint, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${TOKEN}` },
    body: JSON.stringify(body)
  });
  if (resp.status === 401) { cerrarSesion(); throw new Error('Sesión expirada'); }
  return resp.json();
}

// ============================================
// LOGIN
// ============================================
async function login() {
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value.trim();
  if (!email || !password) { mostrarAlerta('Ingresa usuario y contraseña', 'error'); return; }

  const btn = document.getElementById('btn-login');
  btn.textContent = 'Entrando...';
  btn.disabled = true;

  try {
    const resp = await fetch(API_BASE + '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password })
    });
    const data = await resp.json();

    if (resp.ok) {
      TOKEN = data.token;
      OPERARIO = data.usuario;
      localStorage.setItem('wms_token', TOKEN);
      localStorage.setItem('wms_operario', JSON.stringify(OPERARIO));
      actualizarNombresUI(OPERARIO);
      mostrarPantallaSegunRol(OPERARIO.rol);
    } else {
      mostrarAlerta(data.error || 'Credenciales incorrectas', 'error');
    }
  } catch (e) {
    mostrarAlerta('Sin conexión — verifica el WiFi', 'error');
  } finally {
    btn.textContent = 'Entrar';
    btn.disabled = false;
  }
}

function actualizarNombresUI(op) {
  ['op-nombre','admin-nombre','rec-nombre'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = op.nombre;
  });
  ['op-rol','admin-rol'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = op.rol;
  });
}

// ============================================
// DASHBOARD ADMIN
// ============================================
async function cargarDashboardAdmin() {
  try {
    const data = await apiGet(`/api/dashboard/resumen-completo?almacen_id=${ADMIN_ALMACEN_ID}`);

    // KPIs
    const k = data.kpis;
    setText('kpi-pick-pend', k.picking.total_activo);
    setText('kpi-pack-hoy', k.packing.facturas_generadas_hoy);
    setText('kpi-rec-hoy', k.recepcion.confirmadas_hoy);
    setText('kpi-alertas', k.alertas.productos_bajo_minimo);

    // Gráfica de actividad
    renderizarGraficaActividad(k);

    // Movimientos recientes
    renderizarMovimientos(data.movimientos_recientes.movimientos);
    // Auto-refresh cada 30 segundos
    setTimeout(() => {
      if (document.getElementById('pantalla-admin').style.display !== 'none') {
        cargarDashboardAdmin();
      }
    }, 30000);

  } catch (e) {
    mostrarAlerta('Error cargando dashboard', 'error');
  }
}
// ============================================
// AUTO-REFRESH — TIEMPO REAL
// ============================================
let REFRESH_INTERVAL_ADMIN = null;
let REFRESH_INTERVAL_OPERARIO = null;

function iniciarAutoRefreshAdmin() {
  if (REFRESH_INTERVAL_ADMIN) clearInterval(REFRESH_INTERVAL_ADMIN);
  REFRESH_INTERVAL_ADMIN = setInterval(() => {
    if (TAB_ACTUAL === 'tab-dashboard') cargarDashboardAdmin();
    else if (TAB_ACTUAL === 'tab-pedidos') cargarPedidos();
    else if (TAB_ACTUAL === 'tab-operarios') cargarProductividad();
    else if (TAB_ACTUAL === 'tab-stock') cargarAlertasStock();
  }, 30000); // cada 30 segundos
}

function iniciarAutoRefreshOperario() {
  if (REFRESH_INTERVAL_OPERARIO) clearInterval(REFRESH_INTERVAL_OPERARIO);
  REFRESH_INTERVAL_OPERARIO = setInterval(() => {
    if (!TAREA_ACTUAL) cargarTareaActual();
  }, 5000); // cada 5 segundos
}

function detenerRefresh() {
  if (REFRESH_INTERVAL_ADMIN) clearInterval(REFRESH_INTERVAL_ADMIN);
  if (REFRESH_INTERVAL_OPERARIO) clearInterval(REFRESH_INTERVAL_OPERARIO);
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val ?? '—';
}

function renderizarGraficaActividad(kpis) {
  const ctx = document.getElementById('chart-actividad');
  if (!ctx) return;

  if (CHART_ACTIVIDAD) CHART_ACTIVIDAD.destroy();

  CHART_ACTIVIDAD = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Picking\npendiente', 'Picking\nhoy', 'Packing\nhoy', 'Facturas\nSiesa', 'Conteos\nMatch'],
      datasets: [{
        data: [
          kpis.picking.total_activo,
          kpis.picking.completado_hoy,
          kpis.packing.completado_hoy,
          kpis.packing.facturas_generadas_hoy,
          kpis.conteo.match_hoy
        ],
        backgroundColor: ['#1e3a5f','#14532d','#14532d','#065f46','#713f12'],
        borderRadius: 6
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#666', font: { size: 10 } }, grid: { color: '#1a1a1a' } },
        y: { ticks: { color: '#666' }, grid: { color: '#1a1a1a' }, beginAtZero: true }
      }
    }
  });
}

function renderizarMovimientos(movimientos) {
  const el = document.getElementById('movimientos-recientes');
  if (!el) return;

  if (!movimientos || movimientos.length === 0) {
    el.innerHTML = '<div class="tabla-titulo">Últimos movimientos</div><div style="color:#555;font-size:13px;padding:8px 0;">Sin movimientos hoy</div>';
    return;
  }

  const filas = movimientos.slice(0, 8).map(m => {
    const color = m.tipo === 'ENTRADA' ? '#4ade80' : '#f87171';
    const signo = m.tipo === 'ENTRADA' ? '+' : '-';
    const fecha = new Date(m.fecha).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
    return `
      <div class="tabla-fila">
        <div>
          <div class="tabla-nombre">${m.tipo}</div>
          <div style="font-size:11px;color:#555;">${fecha} · Doc: ${m.numero_documento || '—'}</div>
        </div>
        <div style="color:${color};font-weight:700;">${signo}${m.cantidad}</div>
      </div>`;
  }).join('');

  el.innerHTML = `<div class="tabla-titulo">Últimos movimientos</div>${filas}`;
}

// ============================================
// TABS ADMIN
// ============================================
function mostrarTab(tabId) {
  ['tab-dashboard','tab-pedidos','tab-operarios','tab-stock','tab-connekta'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = id === tabId ? 'block' : 'none';
  });

  document.querySelectorAll('.nav-tab').forEach((tab, i) => {
    const tabs = ['tab-dashboard','tab-pedidos','tab-operarios','tab-stock','tab-connekta'];
    tab.classList.toggle('active', tabs[i] === tabId);
  });

  TAB_ACTUAL = tabId;

  if (tabId === 'tab-pedidos') cargarPedidos();
  else if (tabId === 'tab-operarios') cargarProductividad();
  else if (tabId === 'tab-stock') cargarAlertasStock();
  else if (tabId === 'tab-connekta') cargarEstadoConnekta();
}

async function cargarPedidos() {
  const el = document.getElementById('lista-pedidos');
  if (!el) return;
  try {
    const data = await apiGet('/api/picking/?estado=PENDIENTE&per_page=20');

    // Botón crear picking
    const btnCrear = `
      <div style="margin-bottom:12px;">
        <button onclick="mostrarFormCrearPicking()"
                style="width:100%;padding:14px;font-size:16px;font-weight:700;
                       background:#fff;color:#000;border:none;border-radius:12px;cursor:pointer;">
          + Crear tarea de picking
        </button>
      </div>
      <div id="form-crear-picking" style="display:none;"></div>`;

    if (!data.tareas || data.tareas.length === 0) {
      el.innerHTML = btnCrear + '<div style="color:#555;text-align:center;padding:40px;">Sin pedidos pendientes ✓</div>';
      return;
    }

    const filas = data.tareas.map(t => `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:14px;font-weight:600;">${t.producto_nombre || t.producto_codigo}</div>
            <div style="font-size:12px;color:#666;margin-top:2px;">${t.codigo} · ${t.ubicacion_codigo || '—'}</div>
            <div style="font-size:11px;color:#444;margin-top:2px;">Operario: ${t.operario_id ? '#'+t.operario_id : 'Sin asignar'}</div>
          </div>
          <div style="text-align:right;">
            <span class="badge ${t.estado === 'EN_PROCESO' ? 'badge-blue' : 'badge-yellow'}">${t.estado}</span>
            <div style="font-size:24px;font-weight:800;margin-top:4px;">${t.cantidad_recogida}/${t.cantidad_solicitada}</div>
            <div style="font-size:10px;color:#555;">unidades</div>
          </div>
        </div>
      </div>`).join('');

    el.innerHTML = btnCrear + filas;
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando pedidos</div>';
  }
}

async function mostrarFormCrearPicking() {
  const form = document.getElementById('form-crear-picking');
  if (!form) return;

  // Cargar productos disponibles
  let opcionesProductos = '<option value="">Selecciona producto</option>';
  try {
    const data = await apiGet('/api/productos/');
    if (data.productos) {
      opcionesProductos += data.productos.map(p =>
        `<option value="${p.id}" data-codigo="${p.codigo}">${p.codigo} — ${p.nombre}</option>`
      ).join('');
    }
  } catch (e) {}

  // Cargar ubicaciones
  let opcionesUbicaciones = '<option value="">Selecciona ubicación</option>';
  try {
    const data = await apiGet('/api/almacenes/1/ubicaciones');
    if (data.ubicaciones) {
      opcionesUbicaciones += data.ubicaciones
        .filter(u => u.tipo !== 'cross_dock')
        .map(u => `<option value="${u.id}">${u.codigo}</option>`)
        .join('');
    }
  } catch (e) {}

  form.style.display = 'block';
  form.innerHTML = `
    <div style="background:#111;border-radius:12px;padding:16px;margin-bottom:12px;border:1px solid #333;">
      <div style="font-size:15px;font-weight:700;margin-bottom:12px;">Nueva tarea de picking</div>

      <div style="margin-bottom:10px;">
        <div style="font-size:11px;color:#666;margin-bottom:4px;text-transform:uppercase;">Producto</div>
        <select id="pick-producto" style="width:100%;padding:12px;background:#000;color:#fff;border:1px solid #333;border-radius:8px;font-size:14px;">
          ${opcionesProductos}
        </select>
      </div>

      <div style="margin-bottom:10px;">
        <div style="font-size:11px;color:#666;margin-bottom:4px;text-transform:uppercase;">Ubicación</div>
        <select id="pick-ubicacion" style="width:100%;padding:12px;background:#000;color:#fff;border:1px solid #333;border-radius:8px;font-size:14px;">
          ${opcionesUbicaciones}
        </select>
      </div>

      <div style="margin-bottom:10px;">
        <div style="font-size:11px;color:#666;margin-bottom:4px;text-transform:uppercase;">Cantidad</div>
        <input id="pick-cantidad" type="number" min="1" value="10"
               style="width:100%;padding:12px;background:#000;color:#fff;border:1px solid #333;border-radius:8px;font-size:20px;font-weight:700;">
      </div>

      <div style="margin-bottom:12px;">
        <div style="font-size:11px;color:#666;margin-bottom:4px;text-transform:uppercase;">Referencia pedido Siesa</div>
        <input id="pick-referencia" type="text" placeholder="PV-2024-001"
               style="width:100%;padding:12px;background:#000;color:#fff;border:1px solid #333;border-radius:8px;font-size:14px;">
      </div>

      <div style="display:flex;gap:8px;">
        <button onclick="crearPickingDesdeAdmin()"
                style="flex:1;padding:14px;font-size:16px;font-weight:700;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">
          Crear tarea
        </button>
        <button onclick="document.getElementById('form-crear-picking').style.display='none'"
                style="padding:14px 20px;font-size:16px;background:#222;color:#fff;border:none;border-radius:10px;cursor:pointer;">
          Cancelar
        </button>
      </div>
    </div>`;
}

async function crearPickingDesdeAdmin() {
  const productoId = document.getElementById('pick-producto').value;
  const ubicacionId = document.getElementById('pick-ubicacion').value;
  const cantidad = parseInt(document.getElementById('pick-cantidad').value);
  const referencia = document.getElementById('pick-referencia').value;

  if (!productoId || !ubicacionId || !cantidad) {
    mostrarAlerta('Completa todos los campos', 'error');
    return;
  }

  try {
    const resp = await apiPost('/api/picking/crear', {
      producto_id: parseInt(productoId),
      ubicacion_id: parseInt(ubicacionId),
      almacen_id: 1,
      cantidad_solicitada: cantidad,
      referencia_documento: referencia || 'MANUAL',
      tipo_documento: 'PEDIDO_VENTA',
      prioridad: 1
    });

    if (resp.error) {
      mostrarAlerta(resp.error, 'error');
      return;
    }

    mostrarAlerta('✓ Tarea de picking creada — el operario la verá en segundos', 'exito');
    document.getElementById('form-crear-picking').style.display = 'none';
    setTimeout(() => cargarPedidos(), 1000);

  } catch (e) {
    mostrarAlerta('Error creando picking', 'error');
  }
}

async function cargarProductividad() {
  const el = document.getElementById('lista-operarios');
  if (!el) return;
  try {
    const data = await apiGet(`/api/dashboard/productividad?almacen_id=${ADMIN_ALMACEN_ID}&dias=7`);
    if (!data.operarios || data.operarios.length === 0) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin datos de operarios</div>';
      return;
    }
    el.innerHTML = data.operarios.map((op, i) => `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:14px;font-weight:600;">${op.nombre}</div>
            <div style="font-size:11px;color:#555;margin-top:2px;">${op.rol}</div>
            <div style="font-size:11px;color:#444;margin-top:4px;">
              Pick: ${op.pickings_completados} · Pack: ${op.packings_completados} · Conteos: ${op.conteos_completados}
            </div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:28px;font-weight:800;color:${i === 0 ? '#4ade80' : '#fff'};">${op.total_tareas}</div>
            <div style="font-size:10px;color:#555;">tareas 7d</div>
          </div>
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando operarios</div>';
  }
}

async function cargarAlertasStock() {
  const el = document.getElementById('lista-alertas');
  if (!el) return;
  try {
    const data = await apiGet(`/api/dashboard/alertas-stock?almacen_id=${ADMIN_ALMACEN_ID}`);
    if (!data.alertas || data.alertas.length === 0) {
      el.innerHTML = '<div style="color:#4ade80;text-align:center;padding:40px;">✓ Sin alertas de stock</div>';
      return;
    }
    el.innerHTML = data.alertas.map(a => `
      <div class="tabla-card">
        <div class="alerta-item">
          <div>
            <div class="alerta-producto">${a.nombre}</div>
            <div class="alerta-codigo">${a.codigo} · Clase ${a.clasificacion_abc || '—'}</div>
          </div>
          <div class="alerta-stock">
            <span class="badge ${a.urgencia === 'CRITICO' ? 'badge-red' : 'badge-yellow'}">${a.urgencia}</span>
            <div class="alerta-num" style="color:${a.urgencia === 'CRITICO' ? '#f87171' : '#facc15'}">${a.stock_actual}</div>
            <div style="font-size:10px;color:#555;">mín: ${a.stock_minimo}</div>
          </div>
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando alertas</div>';
  }
}

async function cargarEstadoConnekta() {
  const el = document.getElementById('estado-connekta');
  if (!el) return;
  try {
    const token = TOKEN;
    const resp = await fetch(API_BASE + '/api/packing/connekta/estado', {
      headers: { 'Authorization': `Bearer ${token}` }
    });
    const data = await resp.json();
    const color = data.modo_simulacion ? '#facc15' : '#4ade80';
    const estado = data.modo_simulacion ? 'SIMULACIÓN' : 'PRODUCCIÓN';
    el.innerHTML = `
      <div class="tabla-card">
        <div style="text-align:center;padding:20px 0;">
          <div style="font-size:14px;color:#666;margin-bottom:8px;">Estado Connekta</div>
          <div style="font-size:28px;font-weight:800;color:${color};">${estado}</div>
          <div style="font-size:13px;color:#555;margin-top:12px;">${data.mensaje}</div>
        </div>
        <div style="margin-top:16px;">
          <div class="tabla-fila"><span class="tabla-nombre">URL configurada</span><span class="badge ${data.url_configurada ? 'badge-green' : 'badge-red'}">${data.url_configurada ? 'Sí' : 'No'}</span></div>
          <div class="tabla-fila"><span class="tabla-nombre">Credenciales</span><span class="badge ${data.credenciales_configuradas ? 'badge-green' : 'badge-red'}">${data.credenciales_configuradas ? 'Sí' : 'No'}</span></div>
          <div class="tabla-fila"><span class="tabla-nombre">Modo</span><span class="badge ${data.modo_simulacion ? 'badge-yellow' : 'badge-green'}">${data.modo_simulacion ? 'Simulación' : 'Real'}</span></div>
        </div>
      </div>`;
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error consultando Connekta</div>';
  }
}

// ============================================
// RECEPCIÓN
// ============================================
async function cargarRecepcionesActivas() {
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  try {
    const data = await apiGet('/api/recepcion/?estado=ABIERTA');
    if (!data.recepciones || data.recepciones.length === 0) {
      el.innerHTML = `
        <div style="text-align:center;padding:40px 20px;">
          <div style="font-size:60px;">✓</div>
          <div style="font-size:24px;font-weight:700;margin-top:12px;">Sin recepciones</div>
          <div style="font-size:14px;color:#666;margin-top:8px;">No hay OCs pendientes de recibir</div>
          <button onclick="cargarRecepcionesActivas()" style="margin-top:24px;padding:14px 28px;font-size:16px;background:#fff;color:#000;border:none;border-radius:12px;cursor:pointer;">Actualizar</button>
        </div>`;
      return;
    }
    el.innerHTML = data.recepciones.map(r => `
      <div class="rec-card">
        <div class="rec-titulo">OC: ${r.numero_oc_siesa}</div>
        <div class="rec-sub">${r.proveedor_nombre || 'Sin proveedor'}</div>
        <div style="margin-top:12px;display:flex;justify-content:space-between;align-items:center;">
          <span class="badge badge-blue">${r.estado}</span>
          <span style="font-size:12px;color:#555;">${r.total_items} ítems</span>
        </div>
        <button onclick="iniciarRecepcion(${r.id})" style="width:100%;margin-top:12px;padding:14px;font-size:16px;font-weight:700;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">
          Iniciar recepción
        </button>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando recepciones</div>';
  }
}

async function iniciarRecepcion(id) {
  try {
    await apiPut(`/api/recepcion/${id}/iniciar`);
    mostrarAlerta('Recepción iniciada — escanea los productos', 'exito');
    cargarRecepcionesActivas();
  } catch (e) {
    mostrarAlerta('Error iniciando recepción', 'error');
  }
}

// ============================================
// OPERARIO — TAREAS
// ============================================
async function cargarTareaActual() {
  try {
    const data = await apiGet('/api/mobile/tarea-actual');
    if (data.sin_tareas) {
      document.getElementById('contenido-tarea').innerHTML = `
        <div style="text-align:center;padding:60px 20px;">
          <div style="font-size:80px;">✓</div>
          <div style="font-size:28px;font-weight:700;margin-top:16px;">Sin tareas</div>
          <div style="font-size:18px;color:#666;margin-top:8px;">Todas las tareas completadas</div>
          <button onclick="cargarTareaActual()" style="margin-top:32px;padding:16px 32px;font-size:18px;background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;">Actualizar</button>
        </div>`;
      return;
    }
    TAREA_ACTUAL = data;
    renderizarTarea(data);
    // Auto-refresh cada 5 segundos si no hay tarea activa
    if (data.sin_tareas) {
      setTimeout(() => {
        if (document.getElementById('pantalla-operario').style.display !== 'none') {
          cargarTareaActual();
        }
      }, 5000);
    }
  } catch (e) {
    mostrarAlerta('Error cargando tareas', 'error');
  }
}

function renderizarTarea(tarea) {
  const colores = { 'PICKING': '#1d4ed8', 'PACKING': '#7c3aed', 'CONTEO': '#b45309' };
  const color = colores[tarea.tipo] || '#000';
  const esConteo = tarea.tipo === 'CONTEO';

  document.getElementById('contenido-tarea').innerHTML = `
    <div style="padding:16px;">
      <div style="background:${color};color:#fff;border-radius:12px;padding:10px 16px;font-size:20px;font-weight:700;text-align:center;margin-bottom:16px;">${tarea.tipo}</div>
      <div style="background:#000;color:#fff;border-radius:16px;padding:20px;margin-bottom:16px;border:1px solid #222;">
        <div style="font-size:14px;color:#999;margin-bottom:4px;">UBICACIÓN</div>
        <div style="font-size:42px;font-weight:900;letter-spacing:2px;">${tarea.ubicacion}</div>
      </div>
      <div style="background:#111;color:#fff;border-radius:16px;padding:20px;margin-bottom:16px;">
        <div style="font-size:14px;color:#999;margin-bottom:4px;">PRODUCTO</div>
        <div style="font-size:28px;font-weight:700;">${tarea.producto_codigo}</div>
        <div style="font-size:16px;color:#aaa;margin-top:4px;">${tarea.producto_nombre}</div>
      </div>
      ${!esConteo ? `
      <div style="background:#1a1a1a;color:#fff;border-radius:16px;padding:20px;margin-bottom:16px;text-align:center;">
        <div style="font-size:14px;color:#999;margin-bottom:4px;">CANTIDAD</div>
        <div style="font-size:64px;font-weight:900;" id="contador-cantidad">${tarea.cantidad_escaneada} / ${tarea.cantidad_requerida}</div>
        <div style="height:8px;background:#333;border-radius:4px;margin-top:12px;">
          <div id="barra-progreso" style="height:100%;background:#22c55e;border-radius:4px;width:${tarea.cantidad_requerida ? (tarea.cantidad_escaneada/tarea.cantidad_requerida*100) : 0}%;transition:width 0.3s;"></div>
        </div>
      </div>` : `
      <div style="background:#1a1a1a;color:#fff;border-radius:16px;padding:20px;margin-bottom:16px;text-align:center;">
        <div style="font-size:14px;color:#999;margin-bottom:4px;">CONTEO CIEGO</div>
        <div style="font-size:64px;font-weight:900;" id="contador-cantidad">0</div>
        <div style="font-size:14px;color:#666;margin-top:8px;">Cuenta sin ver la cantidad esperada</div>
      </div>`}
      <div style="margin-bottom:12px;">
        <button onclick="toggleCamara()" style="width:100%;padding:16px;font-size:18px;background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;">📷 Escanear con cámara</button>
      </div>
      <div id="camara-container" style="display:none;margin-bottom:12px;">
        <div id="lector-qr" style="border-radius:12px;overflow:hidden;"></div>
        <button onclick="toggleCamara()" style="width:100%;padding:12px;margin-top:8px;font-size:16px;background:#333;color:#fff;border:none;border-radius:12px;cursor:pointer;">Cerrar cámara</button>
      </div>
      <button id="btn-confirmar" onclick="confirmarTarea()" style="width:100%;padding:20px;font-size:22px;font-weight:700;background:#000;color:#fff;border:none;border-radius:16px;cursor:pointer;opacity:${esConteo ? 1 : 0.3};" ${esConteo ? '' : 'disabled'}>
        ✓ Confirmar
      </button>
      ${tarea.referencia ? `<div style="text-align:center;margin-top:12px;font-size:13px;color:#666;">Ref: ${tarea.referencia}</div>` : ''}
    </div>`;
}

// ============================================
// ESCANEO CON CÁMARA
// ============================================
async function toggleCamara() {
  const container = document.getElementById('camara-container');
  if (CAMARA_ACTIVA) {
    if (HTML5QR) { await HTML5QR.stop(); HTML5QR = null; }
    CAMARA_ACTIVA = false;
    container.style.display = 'none';
    return;
  }
  container.style.display = 'block';
  CAMARA_ACTIVA = true;
  if (!window.Html5Qrcode) {
    await cargarScript('https://cdnjs.cloudflare.com/ajax/libs/html5-qrcode/2.3.8/html5-qrcode.min.js');
  }
  HTML5QR = new Html5Qrcode('lector-qr');
  try {
    await HTML5QR.start(
      { facingMode: 'environment' },
      { fps: 10, qrbox: { width: 250, height: 150 } },
      (codigo) => { procesarCodigoEscaneado(codigo); },
      () => {}
    );
  } catch (e) {
    mostrarAlerta('No se pudo activar la cámara', 'error');
    CAMARA_ACTIVA = false;
    container.style.display = 'none';
  }
}

function cargarScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src; s.onload = resolve; s.onerror = reject;
    document.head.appendChild(s);
  });
}

// ============================================
// PROCESAR CÓDIGO ESCANEADO
// ============================================
async function procesarCodigoEscaneado(codigo) {
  if (!TAREA_ACTUAL) return;
  vibrar();
  mostrarFlash();
  try {
    const resp = await apiPost('/api/mobile/escanear', {
      tarea_id: TAREA_ACTUAL.id,
      tipo: TAREA_ACTUAL.tipo,
      codigo: codigo,
      cantidad: 1
    });
    if (resp.error) {
      mostrarAlerta(typeof resp.error === 'object' ? resp.error.mensaje : resp.error, 'error');
      return;
    }
    const contador = document.getElementById('contador-cantidad');
    if (contador) {
      contador.textContent = TAREA_ACTUAL.tipo === 'CONTEO'
        ? resp.cantidad_contada
        : `${resp.cantidad_actual} / ${resp.cantidad_requerida}`;
    }
    const barra = document.getElementById('barra-progreso');
    if (barra && resp.cantidad_requerida) {
      barra.style.width = `${Math.min((resp.cantidad_actual / resp.cantidad_requerida) * 100, 100)}%`;
    }
    if (resp.puede_confirmar) {
      const btn = document.getElementById('btn-confirmar');
      if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.style.background = '#16a34a'; }
      mostrarAlerta(resp.mensaje || '¡Listo para confirmar!', 'exito');
    }
  } catch (e) {
    mostrarAlerta('Error de conexión', 'error');
  }
}

// ============================================
// CONFIRMAR TAREA
// ============================================
async function confirmarTarea() {
  if (!TAREA_ACTUAL) return;
  const btn = document.getElementById('btn-confirmar');
  btn.textContent = 'Confirmando...';
  btn.disabled = true;
  const payload = { tarea_id: TAREA_ACTUAL.id, tipo: TAREA_ACTUAL.tipo, items_escaneados: [] };
  try {
    const resp = await apiPost('/api/mobile/confirmar', payload);
    if (resp.error) {
      mostrarAlerta(resp.error, 'error');
      btn.textContent = '✓ Confirmar';
      btn.disabled = false;
      return;
    }
    mostrarAlerta('¡Tarea completada!', 'exito');
    setTimeout(() => { TAREA_ACTUAL = null; cargarTareaActual(); }, 1500);
  } catch (e) {
    guardarEnColaOffline(payload);
    setTimeout(() => { TAREA_ACTUAL = null; cargarTareaActual(); }, 2000);
  }
}

// ============================================
// UI HELPERS
// ============================================
function mostrarPantalla(id) {
  ['pantalla-login','pantalla-operario','pantalla-admin','pantalla-recepcion'].forEach(p => {
    const el = document.getElementById(p);
    if (el) el.style.display = p === id ? 'block' : 'none';
  });
}

function mostrarAlerta(mensaje, tipo = 'info') {
  const colores = { exito: '#16a34a', error: '#dc2626', advertencia: '#d97706', info: '#2563eb' };
  const alerta = document.createElement('div');
  alerta.style.cssText = `position:fixed;top:20px;left:50%;transform:translateX(-50%);background:${colores[tipo]};color:#fff;padding:16px 24px;border-radius:12px;font-size:18px;font-weight:600;z-index:9999;max-width:90%;text-align:center;animation:fadeIn 0.2s ease;`;
  alerta.textContent = mensaje;
  document.body.appendChild(alerta);
  setTimeout(() => alerta.remove(), 2500);
}

function mostrarFlash() {
  const flash = document.createElement('div');
  flash.style.cssText = 'position:fixed;inset:0;background:rgba(255,255,255,0.3);z-index:9998;pointer-events:none;animation:flash 0.15s ease;';
  document.body.appendChild(flash);
  setTimeout(() => flash.remove(), 150);
}

function vibrar() { if (navigator.vibrate) navigator.vibrate(50); }

function cerrarSesion() {
  detenerRefresh();
  TOKEN = null; OPERARIO = null;
  localStorage.removeItem('wms_token');
  localStorage.removeItem('wms_operario');
  mostrarPantalla('pantalla-login');
}