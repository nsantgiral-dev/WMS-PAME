'use strict';

const API = window.location.origin;
let TOKEN = localStorage.getItem('wms_token');
let OPERARIO = JSON.parse(localStorage.getItem('wms_operario') || 'null');
let TAREA_ACTUAL = null;
let COLA_OFFLINE = JSON.parse(localStorage.getItem('wms_cola_offline') || '[]');
let SCANNER_BUFFER = '';
let SCANNER_TIMER = null;
let CAMARA_ACTIVA = false;
let HTML5QR = null;
let CHART = null;
let TAB = 'tab-dashboard';
let ALMACEN_ID = 1;
let TIMER_ADMIN = null;
let TIMER_OPERARIO = null;
let RECEPCION_ACTUAL = null;   // recepción en escaneo activo (pantalla recepcionista)
let DEVOLUCION_ACTUAL = null;  // tarea de devolución en flujo activo
let REC_TAB_ACTIVO = 'ocs';   // tab activo en pantalla recepcionista
let TIMER_REC = null;          // polling recepcionista (30 seg)
let SIESA_PEDIDOS = [];        // pedidos cargados desde Siesa (admin tab-pedidos)
let SIESA_OCS = [];            // OCs cargadas desde Siesa (pantalla recepcionista)

document.addEventListener('DOMContentLoaded', () => {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/pwa/sw.js').catch(() => {});
  }
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
  const puedeEmpacar = OPERARIO?.puede_empacar || false;
  const puedePicar   = OPERARIO?.puede_picar !== false; // default true
  if (esAdmin) {
    pantalla('pantalla-admin');
    cargarAdmin();
    TIMER_ADMIN = setInterval(cargarAdmin, 30000);
  } else if (esRecepcion) {
    pantalla('pantalla-recepcion');
    cargarRecepciones();
    cargarDevoluciones();
    TIMER_REC = setInterval(() => {
      if (!RECEPCION_ACTUAL && !DEVOLUCION_ACTUAL) {
        cargarRecepciones();
        cargarDevoluciones();
      }
    }, 30000);
  } else if (puedeEmpacar && !puedePicar) {
    // Empacador puro → directo al HUD de packing
    pantalla('pantalla-empacador');
    document.getElementById('emp-nombre').textContent = OPERARIO.nombre;
    empCargarTareas();
    TIMER_OPERARIO = setInterval(empCargarTareas, 20000);
  } else if (puedeEmpacar && puedePicar) {
    // Rol dual: picker + empacador → picker por defecto con acceso a packing
    pantalla('pantalla-operario');
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  } else {
    // Picker puro (o operario sin flags)
    pantalla('pantalla-operario');
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  }
}

function pararTimers() {
  clearInterval(TIMER_ADMIN);
  clearInterval(TIMER_OPERARIO);
  clearInterval(TIMER_REC);
  RECEPCION_ACTUAL = null;
  DEVOLUCION_ACTUAL = null;
}

function monitorRed() {
  const update = () => {
    const on = navigator.onLine;
    ['conexion-status','conexion-status-admin'].forEach(id => {
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
  const focus = () => {
    const a = document.activeElement;
    const esForm = a && ['INPUT','TEXTAREA','SELECT'].includes(a.tagName);
    const hayModal = document.getElementById('modal-problema');
    if (!CAMARA_ACTIVA && !esForm && !hayModal) inp.focus();
  };
  document.addEventListener('click', focus);
  document.addEventListener('touchend', focus);
  setInterval(focus, 1000);
  inp.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const cod = SCANNER_BUFFER.trim();
      SCANNER_BUFFER = '';
      clearTimeout(SCANNER_TIMER);
      if (cod) procesarScan(cod);
    } else if (e.key.length === 1) {
      SCANNER_BUFFER += e.key;
      clearTimeout(SCANNER_TIMER);
      SCANNER_TIMER = setTimeout(() => { SCANNER_BUFFER = ''; }, 150);
    }
  });
}

async function get(url) {
  const r = await fetch(API + url, { headers: { Authorization: 'Bearer ' + TOKEN } });
  if (r.status === 401) { salir(); throw new Error('401'); }
  return r.json();
}

async function post(url, body) {
  const r = await fetch(API + url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(body)
  });
  if (r.status === 401) { salir(); throw new Error('401'); }
  return r.json();
}

async function put(url, body = {}) {
  const r = await fetch(API + url, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(body)
  });
  if (r.status === 401) { salir(); throw new Error('401'); }
  return r.json();
}

async function login() {
  const email = document.getElementById('login-email').value.trim();
  const pass = document.getElementById('login-password').value.trim();
  if (!email || !pass) { alerta('Ingresa usuario y contraseña', 'error'); return; }
  const btn = document.getElementById('btn-login');
  btn.textContent = 'Entrando...';
  btn.disabled = true;
  try {
    const r = await fetch(API + '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password: pass })
    });
    const d = await r.json();
    if (r.ok) {
      TOKEN = d.token;
      OPERARIO = d.usuario;
      localStorage.setItem('wms_token', TOKEN);
      localStorage.setItem('wms_operario', JSON.stringify(OPERARIO));
      actualizarUI(OPERARIO);
      mostrarSegunRol(OPERARIO.rol);
    } else {
      alerta(d.error || 'Credenciales incorrectas', 'error');
    }
  } catch (e) {
    alerta('Sin conexión', 'error');
  } finally {
    btn.textContent = 'Entrar';
    btn.disabled = false;
  }
}

function actualizarUI(op) {
  ['op-nombre','admin-nombre','rec-nombre'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = op.nombre; });
  ['op-rol','admin-rol'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = op.rol; });
}

function salir() {
  pararTimers();
  TOKEN = null; OPERARIO = null; TAREA_ACTUAL = null;
  localStorage.removeItem('wms_token');
  localStorage.removeItem('wms_operario');
  pantalla('pantalla-login');
}

async function cargarAdmin() {
  if (TAB === 'tab-dashboard') await cargarDashboard();
  else if (TAB === 'tab-pedidos') await cargarPedidos();
  else if (TAB === 'tab-operarios') await cargarOperarios();
  else if (TAB === 'tab-usuarios') await cargarUsuarios();
  else if (TAB === 'tab-stock') await cargarStock();
  else if (TAB === 'tab-connekta') await cargarConnekta();
  else if (TAB === 'tab-muelle') await cargarMuelle();
}

function tab(id) {
  ['tab-dashboard','tab-pedidos','tab-operarios','tab-usuarios','tab-stock','tab-connekta','tab-muelle'].forEach(t => {
    const el = document.getElementById(t);
    if (el) el.style.display = t === id ? 'block' : 'none';
  });
  document.querySelectorAll('.nav-tab').forEach((t, i) => {
    t.classList.toggle('active', ['tab-dashboard','tab-pedidos','tab-operarios','tab-usuarios','tab-stock','tab-connekta','tab-muelle'][i] === id);
  });
  TAB = id;
  cargarAdmin();
}

async function cargarDashboard() {
  try {
    const d = await get('/api/dashboard/resumen-completo?almacen_id=' + ALMACEN_ID);
    const k = d.kpis;
    set('kpi-pick-pend', k.picking.total_activo);
    set('kpi-pack-hoy', k.packing.facturas_generadas_hoy);
    set('kpi-rec-hoy', k.recepcion.confirmadas_hoy);
    set('kpi-alertas', k.alertas.productos_bajo_minimo);
    graficaActividad(k);
    movimientos(d.movimientos_recientes.movimientos);
  } catch (e) {}
}

function graficaActividad(k) {
  const ctx = document.getElementById('chart-actividad');
  if (!ctx || !window.Chart) return;
  if (CHART) CHART.destroy();
  CHART = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: ['Picking', 'Pick hoy', 'Pack hoy', 'Facturas', 'Conteos'],
      datasets: [{ data: [k.picking.total_activo, k.picking.completado_hoy, k.packing.completado_hoy, k.packing.facturas_generadas_hoy, k.conteo.match_hoy], backgroundColor: ['#1e3a5f','#14532d','#14532d','#065f46','#713f12'], borderRadius: 6 }]
    },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: '#666' }, grid: { color: '#1a1a1a' } }, y: { ticks: { color: '#666' }, grid: { color: '#1a1a1a' }, beginAtZero: true } } }
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

async function cargarPedidos() {
  const el = document.getElementById('lista-pedidos');
  if (!el) return;
  // Disparar sync en background — no esperar, UI carga de DB local igual
  fetch('/api/siesa/sync-pedidos', {
    method: 'POST',
    headers: { 'Authorization': 'Bearer ' + TOKEN }
  }).catch(() => {});
  try {
    const [siesa, db] = await Promise.all([
      get('/api/siesa/pedidos').catch(() => ({ pedidos: [] })),
      get('/api/picking/?activas=true&per_page=20').catch(() => ({ tareas: [] }))
    ]);
    SIESA_PEDIDOS = siesa.pedidos || [];
    let html = '';

    if (siesa.simulado) {
      html += `<div style="background:#1a1a00;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#facc15;border:1px solid #333300;">⚡ Connekta en simulación — conecta credenciales para ver pedidos reales</div>`;
    } else if (SIESA_PEDIDOS.length) {
      html += `<div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 6px;border-bottom:1px solid #222;margin-bottom:8px;">PENDIENTES EN SIESA</div>`;
      html += SIESA_PEDIDOS.map((p, i) => {
        const sinProd = p.items.filter(it => !it.producto_id).length;
        const totalUds = p.items.reduce((s, it) => s + (it.cantidad_pendiente || 0), 0);

        let accionBtn = '';
        if (p.siesa_triggered) {
          // Estado final: Siesa tiene la remisión
          accionBtn = `<div style="flex-shrink:0;background:#0d1a0d;color:#4ade80;border:1px solid #166534;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;text-align:center;">✓ Despachado<br>en Siesa</div>`;
        } else if (p.packing_estado === 'EN_PROCESO') {
          // Empacador verificando en mesa
          accionBtn = `<div style="flex-shrink:0;background:#1a0a2e;color:#c084fc;border:1px solid #4c1d95;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;text-align:center;">
            En empaque<br>🔄
          </div>`;
        } else if (p.packing_estado === 'VERIFICADO' && !p.siesa_triggered) {
          // Empaque listo pero Siesa falló — empacador debe reintentar Cerrar Caja
          accionBtn = `<div style="flex-shrink:0;background:#2d0a0a;color:#fca5a5;border:1px solid #7f1d1d;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;text-align:center;">
            ⚠ Error<br>Siesa
          </div>`;
        } else if (p.picking_completado) {
          // Picking listo, esperando empacador
          accionBtn = `<div style="flex-shrink:0;background:#1c1400;color:#fbbf24;border:1px solid #78350f;padding:8px 12px;border-radius:8px;font-size:12px;font-weight:700;text-align:center;">
            Packing<br>pendiente
          </div>`;
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

        return `
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

    if (db.tareas && db.tareas.length) {
      html += `<div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 6px;border-bottom:1px solid #222;margin:10px 0 8px;">TAREAS EN BODEGA</div>`;
      html += db.tareas.map(t => `
        <div class="tabla-card">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <div style="font-size:14px;font-weight:600;">${t.producto_nombre || t.producto_codigo}</div>
              <div style="font-size:12px;color:#666;margin-top:2px;">${t.referencia_documento || t.codigo} · ${t.ubicacion_codigo || '—'}</div>
              <div style="font-size:11px;color:#444;margin-top:2px;">${t.operario_id ? '👤 En proceso' : t.estado === 'BLOQUEADO' ? '🔴 Bloqueado' : '⏳ En cola'}</div>
            </div>
            <div style="text-align:right;">
              <span class="badge ${t.estado==='EN_PROCESO'?'badge-blue':t.estado==='COMPLETADO'?'badge-green':t.estado==='BLOQUEADO'?'badge-red':'badge-yellow'}">${t.estado}</span>
              <div style="font-size:22px;font-weight:800;margin-top:4px;">${t.cantidad_recogida||0}/${t.cantidad_solicitada}</div>
            </div>
          </div>
        </div>`).join('');
    }

    if (!html) html = '<div style="color:#555;text-align:center;padding:40px;">Sin actividad ✓</div>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando pedidos</div>';
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
    (prod.operarios || []).forEach(op => { metricas[op.id] = op; });

    // Todos los usuarios activos (operarios/jefe), con métricas si las tienen
    const todos = (usuariosData.usuarios || []).filter(u => u.activo);
    if (!todos.length) { el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin usuarios</div>'; return; }

    // Ordenar: más tareas primero
    todos.sort((a, b) => (metricas[b.id]?.total_tareas || 0) - (metricas[a.id]?.total_tareas || 0));

    el.innerHTML = todos.map((u, i) => {
      const op = metricas[u.id] || { total_tareas: 0, pickings_completados: 0, packings_completados: 0, conteos_completados: 0 };
      const badges = [u.puede_picar && '<span style="background:#1e40af;color:#fff;border-radius:4px;padding:1px 5px;font-size:10px;">Picker</span>',
                      u.puede_empacar && '<span style="background:#6b21a8;color:#fff;border-radius:4px;padding:1px 5px;font-size:10px;">Empacador</span>'].filter(Boolean).join(' ');
      const color = op.total_tareas > 0 ? (i === 0 ? '#4ade80' : '#fff') : '#555';
      return `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:14px;font-weight:600;">${u.nombre}</div>
            <div style="font-size:11px;color:#555;margin-bottom:2px;">${u.rol} ${badges}</div>
            <div style="font-size:11px;color:#444;">Pick:${op.pickings_completados} Pack:${op.packings_completados} Conteos:${op.conteos_completados}</div>
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
      <button id="btn-sync-productos" onclick="sincronizarProductos()"
        style="width:100%;margin-top:12px;padding:14px;background:#1e3a5f;color:#93c5fd;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">
        ↻ Sincronizar catálogo de productos desde Siesa
      </button>
      <div id="sync-resultado" style="margin-top:8px;font-size:12px;color:#666;text-align:center;"></div>

      <div style="border-top:1px solid #222;margin-top:16px;padding-top:16px;">
        <div style="font-size:13px;font-weight:700;margin-bottom:8px;">Inventario bilateral</div>
        <button onclick="cargarInventarioInicial()"
          style="width:100%;padding:12px;background:#0d2d1a;color:#4ade80;border:1px solid #1a4a2a;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;margin-bottom:6px;">
          ↓ Cargar stock inicial desde Siesa
        </button>
        <button onclick="verReconciliacion()"
          style="width:100%;padding:12px;background:#1a1a2e;color:#93c5fd;border:1px solid #2a2a5a;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;">
          ⚖ Ver reconciliación WMS vs Siesa
        </button>
        <div id="inv-resultado" style="margin-top:8px;font-size:12px;color:#666;text-align:center;"></div>
      </div>
      <div id="panel-reconciliacion" style="margin-top:8px;"></div>

      ${d.modo_ensayo ? `
      <div style="background:#1a0f00;border:1px solid #7c2d12;border-radius:10px;padding:12px;margin-top:8px;font-size:12px;color:#fb923c;line-height:1.6;">
        <strong>MODO ENSAYO activo</strong><br>
        Los pedidos y OCs vienen de Siesa real. Al confirmar despacho o recepción, el payload se certifica en los logs del servidor pero <strong>no mueve inventario en Siesa</strong>.<br>
        Para activar producción: borrar la variable <code>MODO_ENSAYO</code> en Railway.
      </div>` : ''}`;
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
}

async function sincronizarProductos() {
  const btn = document.getElementById('btn-sync-productos');
  const res = document.getElementById('sync-resultado');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = '↻ Sincronizando...';
  res.style.color = '#93c5fd';
  res.textContent = 'Iniciando sync en background...';
  try {
    const d = await post('/api/siesa/sync-productos', {});
    if (d.simulado) {
      res.style.color = '#fb923c';
      res.textContent = d.mensaje || 'Modo simulación';
      btn.disabled = false;
      btn.textContent = '↻ Sincronizar catálogo de productos desde Siesa';
      return;
    }
    if (d.omitido) {
      res.style.color = '#fb923c';
      res.textContent = d.mensaje || 'Sync reciente';
      if (d.ultimo_resultado) {
        const r = d.ultimo_resultado;
        res.textContent += ` — último: ✓ ${r.creados} creados · ${r.actualizados} actualizados`;
      }
      btn.disabled = false;
      btn.textContent = '↻ Sincronizar catálogo de productos desde Siesa';
      return;
    }
    if (d.en_curso && !d.iniciado) {
      res.style.color = '#fb923c';
      res.textContent = '⏳ ' + (d.mensaje || 'Ya en proceso — monitoreando...');
    }
    // Sync iniciado o ya en curso — polling cada 5 seg hasta completar
    res.textContent = '⏳ Sincronizando productos... (puede tardar ~30 seg)';
    const intervalo = setInterval(async () => {
      try {
        const estado = await get('/api/siesa/sync-estado');
        if (!estado.en_curso) {
          clearInterval(intervalo);
          btn.disabled = false;
          btn.textContent = '↻ Sincronizar catálogo de productos desde Siesa';
          if (estado.ultimo_error) {
            res.style.color = '#ef4444';
            res.textContent = 'Error: ' + estado.ultimo_error;
          } else if (estado.ultimo_resultado) {
            const r = estado.ultimo_resultado;
            res.style.color = '#4ade80';
            res.textContent = `✓ ${r.creados} creados · ${r.actualizados} actualizados · ${r.total_procesados} total`;
          }
        } else {
          res.textContent = '⏳ Sincronizando... en proceso';
        }
      } catch (e) { clearInterval(intervalo); }
    }, 5000);
  } catch (e) {
    res.style.color = '#ef4444';
    res.textContent = 'Error: ' + (e.message || e);
    btn.disabled = false;
    btn.textContent = '↻ Sincronizar catálogo de productos desde Siesa';
  }
}

async function cargarInventarioInicial() {
  const res = document.getElementById('inv-resultado');
  if (!res) return;
  res.style.color = '#93c5fd';
  res.textContent = '⏳ Iniciando carga de stock desde Siesa...';
  try {
    const d = await post('/api/siesa/cargar-inventario', {});
    if (d.simulado) { res.style.color = '#fb923c'; res.textContent = d.mensaje; return; }
    if (d.en_curso && !d.iniciado) { res.style.color = '#fb923c'; res.textContent = '⏳ Ya en proceso — monitoreando...'; }
    else { res.style.color = '#93c5fd'; res.textContent = '⏳ Cargando stock... (~60 seg)'; }
    const iv = setInterval(async () => {
      try {
        const e = await get('/api/siesa/carga-inventario-estado');
        if (!e.en_curso) {
          clearInterval(iv);
          if (e.ultimo_error) {
            res.style.color = '#ef4444'; res.textContent = 'Error: ' + e.ultimo_error;
          } else if (e.ultimo_resultado) {
            const r = e.ultimo_resultado;
            res.style.color = '#4ade80';
            res.textContent = `✓ ${r.cargados} nuevos · ${r.actualizados} actualizados · ${r.sin_producto_wms} sin match WMS`;
          } else {
            res.style.color = '#fb923c'; res.textContent = 'No hay resultado aún — intenta de nuevo';
          }
        } else { res.textContent = '⏳ Cargando stock desde Siesa...'; }
      } catch(e) { clearInterval(iv); }
    }, 6000);
  } catch(e) { res.style.color = '#ef4444'; res.textContent = 'Error: ' + (e.message || e); }
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
      document.getElementById('contenido-tarea').innerHTML = `
        <div style="text-align:center;padding:60px 20px;">
          <div style="font-size:80px;">✓</div>
          <div style="font-size:28px;font-weight:700;margin-top:16px;">Sin tareas</div>
          <div style="font-size:16px;color:#666;margin-top:8px;">El sistema te asignará la próxima automáticamente</div>
        </div>`;
      return;
    }
    TAREA_ACTUAL = d;
    renderTarea(d);
  } catch (e) {
    console.error('Error cargando tarea:', e);
  }
}

function renderTarea(t) {
  const colores = { PICKING: '#1d4ed8', PACKING: '#7c3aed', CONTEO: '#b45309' };
  const color = colores[t.tipo] || '#333';
  const esConteo = t.tipo === 'CONTEO';
  const pct = t.cantidad_requerida ? Math.min((t.cantidad_escaneada / t.cantidad_requerida) * 100, 100) : 0;
  const puedeCamara = OPERARIO && OPERARIO.puede_usar_camara;

  document.getElementById('contenido-tarea').innerHTML = `
    <div style="padding:16px;">
      <div style="background:${color};color:#fff;border-radius:12px;padding:10px 16px;font-size:20px;font-weight:700;text-align:center;margin-bottom:16px;">${t.tipo}</div>

      <div style="background:#000;border:1px solid #222;border-radius:16px;padding:20px;margin-bottom:12px;">
        <div style="font-size:13px;color:#666;">UBICACIÓN</div>
        <div style="font-size:44px;font-weight:900;letter-spacing:2px;">${t.ubicacion}</div>
      </div>

      <div style="background:#111;border-radius:16px;padding:16px;margin-bottom:12px;">
        <div style="font-size:13px;color:#666;">PRODUCTO</div>
        <div style="font-size:26px;font-weight:700;">${t.producto_codigo}</div>
        <div style="font-size:15px;color:#aaa;">${t.producto_nombre}</div>
      </div>

      ${!esConteo ? `
      <div style="background:#1a1a1a;border-radius:16px;padding:20px;margin-bottom:12px;text-align:center;">
        <div style="font-size:13px;color:#666;">CANTIDAD</div>
        <div id="contador" style="font-size:64px;font-weight:900;">${t.cantidad_escaneada}/${t.cantidad_requerida}</div>
        <div style="height:8px;background:#333;border-radius:4px;margin-top:10px;">
          <div id="barra" style="height:100%;background:#22c55e;border-radius:4px;width:${pct}%;transition:width 0.3s;"></div>
        </div>
      </div>` : `
      <div style="background:#1a1a1a;border-radius:16px;padding:20px;margin-bottom:12px;text-align:center;">
        <div style="font-size:13px;color:#666;">CONTEO CIEGO</div>
        <div id="contador" style="font-size:64px;font-weight:900;">0</div>
        <div style="font-size:13px;color:#555;margin-top:6px;">Cuenta sin ver cantidad esperada</div>
      </div>`}

      ${puedeCamara ? `
      <button onclick="abrirCamara()" style="width:100%;padding:14px;font-size:17px;background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;margin-bottom:10px;">
        📷 Escanear con cámara
      </button>
      <div id="camara-box" style="display:none;margin-bottom:10px;">
        <div id="lector-qr" style="border-radius:12px;overflow:hidden;"></div>
        <button onclick="cerrarCamara()" style="width:100%;padding:10px;margin-top:6px;font-size:15px;background:#333;color:#fff;border:none;border-radius:10px;cursor:pointer;">Cerrar cámara</button>
      </div>` : ''}

      <button id="btn-ok" onclick="confirmar()" ${esConteo ? '' : 'disabled'}
        style="width:100%;padding:20px;font-size:22px;font-weight:700;background:#000;color:#fff;border:none;border-radius:16px;cursor:pointer;opacity:${esConteo?1:0.3};margin-bottom:10px;">
        ✓ Confirmar
      </button>

      ${!esConteo ? `
      <button onclick="confirmarManual(${t.id}, ${t.cantidad_requerida})"
        style="width:100%;padding:14px;font-size:15px;font-weight:600;background:#1a2a1a;color:#4ade80;border:1px solid #166534;border-radius:12px;cursor:pointer;margin-bottom:10px;">
        ✓ Confirmar cantidad completa (manual)
      </button>` : ''}

      <button onclick="reportarProblema(${t.id})"
        style="width:100%;padding:14px;font-size:15px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:12px;cursor:pointer;">
        ⚠ Reportar problema
      </button>

      ${t.referencia ? `<div style="text-align:center;margin-top:10px;font-size:12px;color:#555;">Ref: ${t.referencia}</div>` : ''}
    </div>`;
}

async function abrirCamara() {
  const box = document.getElementById('camara-box');
  if (box) box.style.display = 'block';
  CAMARA_ACTIVA = true;
  if (!window.Html5Qrcode) await loadScript('https://cdnjs.cloudflare.com/ajax/libs/html5-qrcode/2.3.8/html5-qrcode.min.js');
  HTML5QR = new Html5Qrcode('lector-qr');
  try {
    await HTML5QR.start({ facingMode: 'environment' }, { fps: 10, qrbox: { width: 250, height: 150 } },
      cod => procesarScan(cod), () => {});
  } catch (e) {
    alerta('No se pudo activar la cámara', 'error');
    cerrarCamara();
  }
}

async function cerrarCamara() {
  if (HTML5QR) { try { await HTML5QR.stop(); } catch(e) {} HTML5QR = null; }
  CAMARA_ACTIVA = false;
  const box = document.getElementById('camara-box');
  if (box) box.style.display = 'none';
}

function loadScript(src) {
  return new Promise((res, rej) => {
    const s = document.createElement('script');
    s.src = src; s.onload = res; s.onerror = rej;
    document.head.appendChild(s);
  });
}

async function procesarScan(codigo) {
  if (DEVOLUCION_ACTUAL) { await procesarScanDevolucion(codigo); return; }
  if (RECEPCION_ACTUAL) { await procesarScanRecepcion(codigo); return; }
  // HUD del empacador activo
  if (EMP_TAREA && document.getElementById('emp-hud')?.classList.contains('activo')) {
    await empProcesarEscaneo(codigo); return;
  }
  if (!TAREA_ACTUAL) return;
  vibrar(); flash();
  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: TAREA_ACTUAL.id,
      tipo: TAREA_ACTUAL.tipo,
      codigo,
      cantidad: 1
    });
    if (r.error) { alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    const contador = document.getElementById('contador');
    if (contador) {
      contador.textContent = TAREA_ACTUAL.tipo === 'CONTEO'
        ? r.cantidad_contada
        : r.cantidad_actual + '/' + r.cantidad_requerida;
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
  } catch (e) { alerta('Error de conexión', 'error'); }
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
    alerta('¡Tarea completada!', 'exito');
    TAREA_ACTUAL = null;
    setTimeout(pedirTarea, 1500);
  } catch (e) {
    guardarOffline(payload);
    TAREA_ACTUAL = null;
    setTimeout(pedirTarea, 2000);
  }
}

async function confirmarManual(tareaId, cantidad) {
  if (!confirm(`¿Confirmar ${cantidad} unidades recogidas manualmente?`)) return;
  const payload = {
    tarea_id: tareaId,
    tipo: TAREA_ACTUAL?.tipo,
    items_escaneados: [],
    cantidad_manual: cantidad
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
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.9);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div style="background:#111;border-radius:16px;padding:24px;width:100%;max-width:380px;border:1px solid #333;">
        <div style="font-size:18px;font-weight:700;margin-bottom:6px;color:#f87171;">⚠ Reportar problema</div>
        <div style="font-size:13px;color:#555;margin-bottom:16px;">La tarea se bloqueará y el jefe la resolverá. Pasarás a la siguiente tarea automáticamente.</div>
        <button onclick="confirmarProblema(${tareaId},'UBICACION_VACIA')"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:15px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          📦 Ubicación vacía
        </button>
        <button onclick="confirmarProblema(${tareaId},'MERCANCIA_AVERIADA')"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:15px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          🚫 Mercancía averiada
        </button>
        <button onclick="confirmarProblema(${tareaId},'PRODUCTO_INCORRECTO')"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:15px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          ❌ Producto incorrecto
        </button>
        <button onclick="document.getElementById('modal-problema').remove()"
          style="width:100%;padding:12px;font-size:14px;background:#222;color:#666;border:none;border-radius:10px;cursor:pointer;margin-top:4px;">
          Cancelar
        </button>
      </div>
    </div>`;
  document.body.appendChild(modal);
}

async function confirmarProblema(tareaId, motivo) {
  const modal = document.getElementById('modal-problema');
  if (modal) modal.remove();
  try {
    await post('/api/picking/' + tareaId + '/reportar-problema', { motivo });
    alerta('Problema reportado — pasando a siguiente tarea', 'advertencia');
    TAREA_ACTUAL = null;
    setTimeout(pedirTarea, 1500);
  } catch (e) {
    alerta('Error reportando problema', 'error');
  }
}

async function cargarRecepciones() {
  if (RECEPCION_ACTUAL) return;
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:40px;color:#666;">Cargando...</div>';
  try {
    const [siesa, db] = await Promise.all([
      get('/api/siesa/ordenes-compra').catch(() => ({ ordenes: [] })),
      get('/api/recepcion/?estado=EN_PROCESO').catch(() => ({ recepciones: [] }))
    ]);
    SIESA_OCS = siesa.ordenes || [];
    renderListaRecepciones(siesa, db.recepciones || []);
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error cargando</div>'; }
}

function pantalla(id) {
  ['pantalla-login','pantalla-operario','pantalla-admin','pantalla-recepcion','pantalla-empacador'].forEach(p => {
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

// ─────────────────────────────────────────────────────────────
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

async function confirmarDespachoSiesa(packingId, numeroPedido) {
  if (!confirm(`¿Confirmar despacho ${numeroPedido} en Siesa?\nEsto genera la remisión en Siesa. No se puede deshacer.`)) return;
  try {
    const r = await post('/api/siesa/confirmar-despacho', { packing_id: packingId });
    if (r.error) { alerta(r.error, 'error'); return; }
    if (r.modo_ensayo) {
      alerta(`Modo ensayo — payload enviado pero Siesa no lo procesó (${numeroPedido})`, 'advertencia');
    } else if (r.simulado) {
      alerta(`Simulado — ${numeroPedido} marcado como despachado`, 'exito');
    } else {
      alerta(`¡${numeroPedido} remisionado en Siesa!`, 'exito');
    }
    setTimeout(cargarPedidos, 1200);
  } catch (e) { alerta('Error confirmando despacho', 'error'); }
}

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
      <button onclick="cargarRecepciones()" style="margin-top:20px;padding:12px 24px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">Actualizar</button>
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
  if (el) el.innerHTML = '<div style="text-align:center;padding:60px;color:#666;">Creando recepción...</div>';

  try {
    const r = await post('/api/siesa/iniciar-recepcion', {
      numero_oc: oc.numero_oc,
      tipo_docto: oc.tipo_docto,
      consec_docto: oc.consec_docto,
      co: oc.co,
      proveedor: oc.proveedor,
      almacen_id: ALMACEN_ID,
      items: itemsValidos
    });
    if (r.error) { alerta(r.error, 'error'); cargarRecepciones(); return; }
    RECEPCION_ACTUAL = r.recepcion;
    renderEscaneoRecepcion(r.recepcion);
  } catch (e) { alerta('Error creando recepción', 'error'); cargarRecepciones(); }
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

      <div style="background:#111;border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:12px;color:#666;text-align:center;">
        Apunta el escáner — 1 beep = 1 unidad
      </div>

      <div id="items-rec-list" style="margin-bottom:14px;">
        ${renderItemsRecepcion(rec.items)}
      </div>

      <button id="btn-confirmar-rec" onclick="confirmarRecepcionActiva()" ${btnActivo ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:20px;font-weight:700;background:${btnActivo ? btnColor : '#222'};color:#fff;border:none;border-radius:14px;cursor:${btnActivo ? 'pointer' : 'default'};margin-bottom:10px;">
        ${btnTexto}
      </button>

      <button onclick="volverListaRecepciones()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
        Guardar y salir (continuar más tarde)
      </button>
    </div>`;
}

function renderItemsRecepcion(items) {
  return items.map(it => {
    const pct = it.cantidad_ordenada > 0 ? Math.min((it.cantidad_recibida / it.cantidad_ordenada) * 100, 100) : 0;
    const completo = it.cantidad_recibida >= it.cantidad_ordenada;
    return `
      <div id="item-rec-${it.producto_id}"
        style="background:${completo ? '#0d1a0d' : '#111'};border:1px solid ${completo ? '#166534' : '#222'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;">
            <div style="font-size:14px;font-weight:600;color:${completo ? '#4ade80' : '#fff'};">${it.producto_nombre || it.producto_codigo}</div>
            <div style="font-size:11px;color:#555;">${it.producto_codigo}</div>
            ${it.destino === 'CROSS_DOCK' ? '<div style="font-size:11px;color:#60a5fa;margin-top:4px;">↔ CROSS-DOCK</div>' : ''}
          </div>
          <div style="text-align:right;flex-shrink:0;padding-left:8px;">
            <div style="font-size:28px;font-weight:900;color:${completo ? '#4ade80' : '#fff'};">${it.cantidad_recibida}/${it.cantidad_ordenada}</div>
          </div>
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
    // 1. Traducir código de barras → producto_id
    const prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
    if (prod.error) { alerta('Producto no encontrado: ' + codigo, 'error'); return; }

    // 2. Registrar en la recepción (cantidad 1 por escaneo)
    const r = await post('/api/recepcion/' + RECEPCION_ACTUAL.id + '/escanear', {
      producto_id: prod.producto_id,
      cantidad: 1
    });
    if (r.error) {
      const msg = typeof r.error === 'object' ? r.error.mensaje : r.error;
      alerta(msg, 'error');
      return;
    }

    // 3. Actualizar estado local
    const idx = RECEPCION_ACTUAL.items.findIndex(it => it.producto_id === prod.producto_id);
    if (idx >= 0) RECEPCION_ACTUAL.items[idx] = r.item;

    // 4. Re-renderizar items
    const lista = document.getElementById('items-rec-list');
    if (lista) lista.innerHTML = renderItemsRecepcion(RECEPCION_ACTUAL.items);

    if (r.alerta) {
      const tipo = r.alerta.includes('EXCESO') ? 'error' : r.alerta.includes('CROSS') ? 'advertencia' : 'info';
      alerta(r.alerta, tipo);
    }

    // 5. Habilitar confirmar si todo está listo
    const todoCompleto = RECEPCION_ACTUAL.items.every(it => it.cantidad_recibida >= it.cantidad_ordenada);
    const btn = document.getElementById('btn-confirmar-rec');
    if (btn && todoCompleto) {
      btn.disabled = false;
      btn.style.background = '#16a34a';
      btn.style.cursor = 'pointer';
      alerta('Todo escaneado — confirma la recepción', 'exito');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function confirmarRecepcionActiva() {
  if (!RECEPCION_ACTUAL) return;
  const todoCompleto = RECEPCION_ACTUAL.items.every(it => it.cantidad_recibida >= it.cantidad_ordenada);
  if (!todoCompleto) {
    const ok = confirm('Hay ítems sin completar. ¿Confirmar como recepción parcial?');
    if (!ok) return;
  }
  const btn = document.getElementById('btn-confirmar-rec');
  if (btn) { btn.textContent = 'Confirmando...'; btn.disabled = true; }

  try {
    const r = await put('/api/recepcion/' + RECEPCION_ACTUAL.id + '/confirmar');
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
    alerta('Error confirmando', 'error');
    if (btn) { btn.textContent = '✓ Confirmar recepción'; btn.disabled = false; }
  }
}

function volverListaRecepciones() {
  RECEPCION_ACTUAL = null;
  cargarRecepciones();
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Tabs (Recepciones / Devoluciones)
// ─────────────────────────────────────────────────────────────

function recTab(tab) {
  REC_TAB_ACTIVO = tab;
  const tabOcs = document.getElementById('rec-tab-ocs');
  const tabDev = document.getElementById('rec-tab-dev');
  const contOcs = document.getElementById('contenido-recepcion');
  const contDev = document.getElementById('contenido-devoluciones');
  if (!tabOcs || !tabDev) return;

  const activo = 'border-bottom:2px solid #fff;color:#fff;';
  const inactivo = 'border-bottom:2px solid transparent;color:#666;';
  tabOcs.style.cssText = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;${tab==='ocs' ? activo : inactivo}`;
  tabDev.style.cssText = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;position:relative;${tab==='dev' ? activo : inactivo}`;
  // re-append badge (se pierde al resetear cssText)
  const badge = document.getElementById('badge-dev');
  if (badge && tabDev) tabDev.appendChild(badge);

  if (contOcs) contOcs.style.display = tab === 'ocs' ? 'block' : 'none';
  if (contDev) contDev.style.display = tab === 'dev' ? 'block' : 'none';
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Lista de Devoluciones
// ─────────────────────────────────────────────────────────────

async function cargarDevoluciones() {
  if (DEVOLUCION_ACTUAL) return;
  const el = document.getElementById('contenido-devoluciones');
  const badge = document.getElementById('badge-dev');
  if (!el) return;

  try {
    const d = await get('/api/devoluciones/?almacen_id=' + ALMACEN_ID);
    const tareas = d.tareas || [];

    // Badge en el tab
    if (badge) {
      badge.style.display = tareas.length ? 'inline' : 'none';
      badge.textContent = tareas.length;
    }

    if (!tareas.length) {
      el.innerHTML = `<div style="text-align:center;padding:50px 20px;">
        <div style="font-size:48px;color:#4ade80;">✓</div>
        <div style="font-size:20px;font-weight:700;margin-top:12px;">Sin devoluciones pendientes</div>
        <div style="font-size:13px;color:#555;margin-top:8px;">La reconciliación detectará nuevas automáticamente</div>
      </div>`;
      return;
    }

    el.innerHTML = `
      <div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 8px;border-bottom:1px solid #222;margin-bottom:10px;">
        ${tareas.length} DEVOLUCIÓN(ES) PARA UBICAR
      </div>
      ${tareas.map(t => `
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
        </div>`).join('')}`;
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
// ─────────────────────────────────────────────────────────────
// EMPACADOR — Estado global
// ─────────────────────────────────────────────────────────────

let EMP_TAREA = null;       // TareaPacking activa en el HUD
let EMP_ITEMS = [];         // ItemPacking[] con progreso actual
let EMP_ITEM_IDX = 0;       // índice del ítem que se está escaneando

// ─────────────────────────────────────────────────────────────
// EMPACADOR — Lista de tareas
// ─────────────────────────────────────────────────────────────

async function empCargarTareas() {
  const el = document.getElementById('emp-lista');
  if (!el) return;
  try {
    const d = await get('/api/packing/?per_page=50');
    const tareas = (d.tareas || []).filter(t =>
      ['PENDIENTE', 'EN_PROCESO'].includes(t.estado) ||
      (t.estado === 'VERIFICADO' && !t.siesa_triggered)  // Siesa falló — permitir reintento
    );

    if (!tareas.length) {
      el.innerHTML = `<div style="text-align:center;padding:60px 20px;color:#555;">
        Sin tareas de empaque pendientes ✓<br>
        <button onclick="empCargarTareas()" style="margin-top:20px;background:#222;border:1px solid #333;color:#fff;padding:10px 20px;border-radius:10px;cursor:pointer;">↻ Actualizar</button>
      </div>`;
      return;
    }

    el.innerHTML = `
      <div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 12px;">TAREAS DE EMPAQUE</div>
      ${tareas.map(t => {
        const verificados = t.items_verificados || 0;
        const total = t.total_items || 0;
        const pct = total ? Math.round(verificados / total * 100) : 0;
        const pickingListo = t.picking_listo !== false;  // true si no hay picking o ya terminó
        const siesaFallo = t.estado === 'VERIFICADO' && !t.siesa_triggered;
        const enProceso = t.estado === 'EN_PROCESO';
        const bloqueado = !pickingListo && t.estado === 'PENDIENTE';
        const color = bloqueado ? '#6b7280' : siesaFallo ? '#fca5a5' : enProceso ? '#93c5fd' : '#facc15';
        const bg    = bloqueado ? '#1a1a1a'  : siesaFallo ? '#7f1d1d'  : enProceso ? '#1e3a5f' : '#713f12';
        const label = bloqueado ? 'Esperando picking' : siesaFallo ? '⚠ Reintentar Siesa' : enProceso ? 'En proceso' : 'Pendiente';
        const limpiarBtn = siesaFallo ? `
          <button onclick="event.stopPropagation();empLimpiarSiesa(${t.id})"
            style="margin-top:8px;width:100%;padding:8px;background:#1a1a1a;border:1px solid #444;color:#aaa;border-radius:8px;cursor:pointer;font-size:12px;">
            🗑 Limpiar bultos y redeclarar piezas
          </button>` : '';
        return `
        <div class="emp-task-card" onclick="${bloqueado ? '' : `empIniciarHUD(${t.id})`}"
          style="${bloqueado ? 'opacity:0.5;cursor:default;' : 'cursor:pointer;'}">
          <div class="emp-task-pedido">${t.numero_pedido_siesa}</div>
          <div class="emp-task-sub">${total} producto(s) · ${t.items_verificados || 0}/${total} verificados</div>
          ${total > 0 ? `<div style="margin-top:10px;background:#1a1a1a;border-radius:8px;height:6px;overflow:hidden;">
            <div style="height:100%;background:#4ade80;width:${pct}%;border-radius:8px;transition:width 0.3s;"></div>
          </div>` : ''}
          <span class="emp-task-badge" style="background:${bg};color:${color};">${label}</span>
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
      await fetch(`/api/packing/${packingId}/iniciar`, {
        method: 'PUT',
        headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }
      });
    }

    // Ítems pendientes de verificar van primero
    EMP_ITEMS = [...(t.items || [])].sort((a, b) => a.verificado - b.verificado);
    EMP_ITEM_IDX = EMP_ITEMS.findIndex(i => !i.verificado);
    if (EMP_ITEM_IDX < 0) EMP_ITEM_IDX = 0;

    empRenderHUDItem();
    document.getElementById('emp-hud').classList.add('activo');
    document.getElementById('scanner-input').focus();
  } catch (e) { alerta('Error iniciando tarea', 'error'); }
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
    alerta(`${t.numero_pedido_siesa} despachado — Siesa generó la remisión`, 'exito');
    empCargarTareas();
  } catch (e) {
    alerta('Error de conexión al reintentar Siesa', 'error');
  }
}

function empSimularScan() {
  const inp = document.getElementById('emp-input-manual');
  if (!inp) return;
  const codigo = inp.value.trim();
  if (!codigo) return;
  inp.value = '';
  empProcesarEscaneo(codigo);
}

function empCerrarHUD() {
  document.getElementById('emp-hud').classList.remove('activo');
  EMP_TAREA = null;
  EMP_ITEMS = [];
  EMP_ITEM_IDX = 0;
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

  document.getElementById('emp-hud-pedido').textContent = EMP_TAREA.numero_pedido_siesa;
  document.getElementById('emp-hud-producto').textContent = item.producto_nombre || item.producto_codigo || '—';
  document.getElementById('emp-hud-contador').textContent = item.cantidad_real || 0;
  document.getElementById('emp-hud-de').textContent = `de ${item.cantidad_esperada}`;
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
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: procesar escaneo láser
// ─────────────────────────────────────────────────────────────

async function empProcesarEscaneo(codigo) {
  if (!EMP_TAREA) return;

  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: EMP_TAREA.id,
      tipo: 'PACKING',
      codigo: codigo,
      cantidad: 1
    });

    if (r.error) {
      empFlash('rojo', r.error);
      return;
    }

    // Actualizar estado local del ítem
    const item = EMP_ITEMS.find(i =>
      i.producto_codigo === codigo ||
      (r.codigo_escaneado && i.producto_codigo === r.codigo_escaneado)
    );
    if (item) {
      item.cantidad_real = r.cantidad_actual;
      item.verificado = r.item_completado;
    }

    if (r.item_completado) {
      empFlash('verde', null);
    } else {
      // Ítem parcialmente escaneado — actualizar contador
      if (item) {
        document.getElementById('emp-hud-contador').textContent = r.cantidad_actual;
      }
      empFlash('verde', null);
    }

    // Si todos los ítems están listos
    if (r.todos_completados) {
      // Recargar estado real del servidor
      const detalle = await get(`/api/packing/${EMP_TAREA.id}`);
      EMP_ITEMS = detalle.items || EMP_ITEMS;
    }

    empRenderHUDItem();

  } catch (e) {
    empFlash('rojo', 'Error de conexión');
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

  if (!esVerde && mensaje) {
    // Sonido de error: oscilación roja con mensaje
    hud.style.background = '#1a0000';
    const msgEl = document.getElementById('emp-hud-producto');
    const prevText = msgEl.textContent;
    msgEl.style.color = '#f87171';
    msgEl.textContent = '⚠ ' + mensaje;
    setTimeout(() => {
      msgEl.style.color = '#fff';
      msgEl.textContent = prevText;
      hud.style.background = '#000';
    }, 1800);
  }

  setTimeout(() => {
    flash.style.opacity = '0';
    if (esVerde) hud.style.background = '#000';
  }, esVerde ? 150 : 300);
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

  try {
    const r = await fetch(`/api/packing/${EMP_TAREA.id}/cerrar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ bultos: _BULTOS_LINEAS.map(l => ({ tipo: l.tipo, cantidad: l.cantidad })) })
    });
    const data = await r.json();

    if (!r.ok) {
      errEl.textContent = data.error || 'Error al cerrar';
      if (btnConf) { btnConf.disabled = false; btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
      return;
    }

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
    alerta(`${data.bultos.length} pieza(s) registradas — Siesa generó la remisión`, 'exito');
    empCargarTareas();

  } catch (e) {
    errEl.textContent = 'Error de conexión';
    if (btnConf) { btnConf.disabled = false; btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
  }
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
  return `
    <div style="font-size:15px;font-weight:700;margin-bottom:16px;">${u.id ? 'Editar usuario' : 'Nuevo usuario'}</div>
    <div style="display:flex;flex-direction:column;gap:12px;">
      <input id="u-nombre" placeholder="Nombre completo" value="${u.nombre || ''}"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <input id="u-email" placeholder="email@empresa.com" value="${u.email || ''}" type="email" ${u.id ? 'readonly style="opacity:0.5;"' : ''}
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <input id="u-password" placeholder="${u.id ? 'Nueva contraseña (dejar vacío para no cambiar)' : 'Contraseña'}" type="password"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
      <select id="u-rol" style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
        <option value="operario" ${(u.rol||'operario')==='operario'?'selected':''}>Operario</option>
        <option value="recepcionista" ${u.rol==='recepcionista'?'selected':''}>Recepcionista</option>
        <option value="supervisor" ${u.rol==='supervisor'?'selected':''}>Supervisor</option>
        <option value="jefe_almacen" ${u.rol==='jefe_almacen'?'selected':''}>Jefe de almacén</option>
        <option value="admin" ${u.rol==='admin'?'selected':''}>Admin</option>
      </select>
      <div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:14px;">
        <div style="font-size:12px;font-weight:600;color:#aaa;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.05em;">Capacidades operativas</div>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-picar" ${u.puede_picar!==false?'checked':''} style="width:20px;height:20px;accent-color:#60a5fa;">
          <div>
            <div style="font-size:14px;font-weight:600;color:#60a5fa;">Picker</div>
            <div style="font-size:11px;color:#555;">Puede recoger productos del almacén</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;">
          <input type="checkbox" id="u-puede-empacar" ${u.puede_empacar?'checked':''} style="width:20px;height:20px;accent-color:#c084fc;">
          <div>
            <div style="font-size:14px;font-weight:600;color:#c084fc;">Empacador / Auditor</div>
            <div style="font-size:11px;color:#555;">Verifica y cierra cajas en mesa de empaque</div>
          </div>
        </label>
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
  const puedePicar   = document.getElementById('u-puede-picar')?.checked;
  const puedeEmpacar = document.getElementById('u-puede-empacar')?.checked;

  if (!nombre) { alerta('El nombre es requerido', 'error'); return; }

  const payload = { nombre, rol, puede_picar: puedePicar, puede_empacar: puedeEmpacar };
  if (pass) payload.password = pass;

  try {
    let r;
    if (uid) {
      r = await fetch(API + `/api/auth/usuarios/${uid}`, {
        method: 'PUT',
        headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    } else {
      if (!email) { alerta('El email es requerido', 'error'); return; }
      if (!pass)  { alerta('La contraseña es requerida', 'error'); return; }
      payload.email = email;
      r = await fetch(API + '/api/auth/register', {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
    }
    const data = await r.json();
    if (!r.ok) { alerta(data.error || 'Error guardando usuario', 'error'); return; }
    alerta(uid ? 'Usuario actualizado' : 'Usuario creado', 'exito');
    ocultarFormUsuario();
    cargarUsuarios();
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ─── MONITOR DE MUELLE ────────────────────────────────────────────────────────
let MUELLE_TIMER = null;
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

async function cargarMuelle() {
  const el = document.getElementById('lista-muelle');
  if (!el) return;
  try {
    const d = await get('/api/muelle/listos');
    const total = d.total_bultos || 0;

    const contador = document.getElementById('muelle-contador');
    if (contador) contador.textContent = total > 0 ? `${total} pieza${total !== 1 ? 's' : ''} esperando` : 'Sin piezas pendientes';

    const act = document.getElementById('muelle-ultima-act');
    if (act) act.textContent = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    if (!d.grupos?.length) {
      _MUELLE_GRUPOS_ACTUALES = [];
      el.innerHTML = '<div style="color:#4ade80;text-align:center;padding:40px;font-size:32px;">✓<br><span style="font-size:14px;">Muelle despejado</span></div>';
      return;
    }

    _MUELLE_GRUPOS_ACTUALES = muelleOrdenarGrupos(d.grupos);
    muelleRenderGrupos(_MUELLE_GRUPOS_ACTUALES);

  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error cargando muelle</div>'; }

  clearTimeout(MUELLE_TIMER);
  if (TAB === 'tab-muelle') MUELLE_TIMER = setTimeout(cargarMuelle, 5000);
}

async function muelleCargarCaja() {
  const input = document.getElementById('muelle-scan-input');
  const feedback = document.getElementById('muelle-scan-feedback');
  const codigo = (input?.value || '').trim().toUpperCase();
  if (!codigo) return;

  feedback.style.color = '#888';
  feedback.textContent = 'Procesando...';

  try {
    const r = await fetch(API + '/api/muelle/cargar/' + encodeURIComponent(codigo), {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();

    if (r.ok) {
      const pedidoCompleto = d.pedido_completo ? ' ✓ Pedido completo' : '';
      feedback.style.color = '#4ade80';
      feedback.textContent = `✓ ${d.codigo_barras} · ${d.tipo} ${d.numero}/${d.total} · ${d.municipio || d.cliente || 'cargado'}${pedidoCompleto}`;

      // Animar y eliminar la card
      const card = document.getElementById('muelle-bulto-' + (d.id || ''));
      if (card) { card.style.opacity = '0'; setTimeout(() => card.remove(), 300); }
      else await cargarMuelle(); // fallback: recargar toda la lista

      input.value = '';
    } else {
      feedback.style.color = '#ef4444';
      feedback.textContent = d.error || 'Error al registrar carga';
    }
  } catch (e) {
    feedback.style.color = '#ef4444';
    feedback.textContent = 'Error de conexión';
  }
  input?.focus();
}
