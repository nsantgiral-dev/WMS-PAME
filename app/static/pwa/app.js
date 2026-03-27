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
  if (esAdmin) {
    pantalla('pantalla-admin');
    cargarAdmin();
    TIMER_ADMIN = setInterval(cargarAdmin, 30000);
  } else if (esRecepcion) {
    pantalla('pantalla-recepcion');
    cargarRecepciones();
  } else {
    pantalla('pantalla-operario');
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  }
}

function pararTimers() {
  clearInterval(TIMER_ADMIN);
  clearInterval(TIMER_OPERARIO);
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
  else if (TAB === 'tab-stock') await cargarStock();
  else if (TAB === 'tab-connekta') await cargarConnekta();
}

function tab(id) {
  ['tab-dashboard','tab-pedidos','tab-operarios','tab-stock','tab-connekta'].forEach(t => {
    const el = document.getElementById(t);
    if (el) el.style.display = t === id ? 'block' : 'none';
  });
  document.querySelectorAll('.nav-tab').forEach((t, i) => {
    t.classList.toggle('active', ['tab-dashboard','tab-pedidos','tab-operarios','tab-stock','tab-connekta'][i] === id);
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
  el.innerHTML = '<div class="tabla-titulo">Últimos movimientos</div>' + lista.slice(0,8).map(m => {
    const c = m.tipo === 'ENTRADA' ? '#4ade80' : '#f87171';
    const s = m.tipo === 'ENTRADA' ? '+' : '-';
    const h = new Date(m.fecha).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
    return `<div class="tabla-fila"><div><div class="tabla-nombre">${m.tipo}</div><div style="font-size:11px;color:#555;">${h}</div></div><div style="color:${c};font-weight:700;">${s}${m.cantidad}</div></div>`;
  }).join('');
}

async function cargarPedidos() {
  const el = document.getElementById('lista-pedidos');
  if (!el) return;
  try {
    const d = await get('/api/picking/?per_page=30');
    if (!d.tareas || !d.tareas.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin pedidos activos ✓</div>';
      return;
    }
    el.innerHTML = d.tareas.map(t => `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:14px;font-weight:600;">${t.producto_nombre || t.producto_codigo}</div>
            <div style="font-size:12px;color:#666;margin-top:2px;">${t.codigo} · ${t.ubicacion_codigo || '—'}</div>
            <div style="font-size:11px;color:#444;margin-top:2px;">
              ${t.operario_id ? '👤 En proceso' : t.estado === 'BLOQUEADO' ? '🔴 Bloqueado — requiere atención' : '⏳ En cola'}
            </div>
          </div>
          <div style="text-align:right;">
            <span class="badge ${t.estado === 'EN_PROCESO' ? 'badge-blue' : t.estado === 'COMPLETADO' ? 'badge-green' : t.estado === 'BLOQUEADO' ? 'badge-red' : 'badge-yellow'}">${t.estado}</span>
            <div style="font-size:24px;font-weight:800;margin-top:4px;">${t.cantidad_recogida}/${t.cantidad_solicitada}</div>
          </div>
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;">Error cargando pedidos</div>';
  }
}

async function cargarOperarios() {
  const el = document.getElementById('lista-operarios');
  if (!el) return;
  try {
    const d = await get('/api/dashboard/productividad?almacen_id=' + ALMACEN_ID + '&dias=7');
    if (!d.operarios || !d.operarios.length) { el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin datos</div>'; return; }
    el.innerHTML = d.operarios.map((op, i) => `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:14px;font-weight:600;">${op.nombre}</div>
            <div style="font-size:11px;color:#555;">${op.rol}</div>
            <div style="font-size:11px;color:#444;margin-top:4px;">Pick:${op.pickings_completados} Pack:${op.packings_completados} Conteos:${op.conteos_completados}</div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:28px;font-weight:800;color:${i===0?'#4ade80':'#fff'}">${op.total_tareas}</div>
            <div style="font-size:10px;color:#555;">tareas 7d</div>
          </div>
        </div>
      </div>`).join('');
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
}

async function cargarStock() {
  const el = document.getElementById('lista-alertas');
  if (!el) return;
  try {
    const d = await get('/api/dashboard/alertas-stock?almacen_id=' + ALMACEN_ID);
    if (!d.alertas || !d.alertas.length) { el.innerHTML = '<div style="color:#4ade80;text-align:center;padding:40px;">✓ Sin alertas</div>'; return; }
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
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
}

async function cargarConnekta() {
  const el = document.getElementById('estado-connekta');
  if (!el) return;
  try {
    const d = await get('/api/packing/connekta/estado');
    const color = d.modo_simulacion ? '#facc15' : '#4ade80';
    const estado = d.modo_simulacion ? 'SIMULACIÓN' : 'PRODUCCIÓN';
    el.innerHTML = `
      <div class="tabla-card">
        <div style="text-align:center;padding:20px 0;">
          <div style="font-size:13px;color:#666;margin-bottom:8px;">Estado Connekta</div>
          <div style="font-size:32px;font-weight:800;color:${color};">${estado}</div>
          <div style="font-size:12px;color:#555;margin-top:10px;">${d.mensaje}</div>
        </div>
        <div class="tabla-fila"><span class="tabla-nombre">URL</span><span class="badge ${d.url_configurada?'badge-green':'badge-red'}">${d.url_configurada?'✓':'✗'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">Credenciales</span><span class="badge ${d.credenciales_configuradas?'badge-green':'badge-red'}">${d.credenciales_configuradas?'✓':'✗'}</span></div>
      </div>`;
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
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
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  try {
    const d = await get('/api/recepcion/?estado=ABIERTA');
    if (!d.recepciones || !d.recepciones.length) {
      el.innerHTML = `<div style="text-align:center;padding:40px;">
        <div style="font-size:50px;">✓</div>
        <div style="font-size:22px;font-weight:700;margin-top:12px;">Sin recepciones</div>
        <button onclick="cargarRecepciones()" style="margin-top:20px;padding:12px 24px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">Actualizar</button>
      </div>`;
      return;
    }
    el.innerHTML = d.recepciones.map(r => `
      <div class="rec-card">
        <div class="rec-titulo">OC: ${r.numero_oc_siesa}</div>
        <div class="rec-sub">${r.proveedor_nombre || 'Sin proveedor'}</div>
        <div style="margin-top:10px;display:flex;justify-content:space-between;">
          <span class="badge badge-blue">${r.estado}</span>
          <span style="font-size:12px;color:#555;">${r.total_items} ítems</span>
        </div>
        <button onclick="iniciarRec(${r.id})" style="width:100%;margin-top:10px;padding:12px;font-size:15px;font-weight:700;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">
          Iniciar recepción
        </button>
      </div>`).join('');
  } catch (e) { el.innerHTML = '<div style="color:#ef4444;">Error</div>'; }
}

async function iniciarRec(id) {
  try {
    await put('/api/recepcion/' + id + '/iniciar');
    alerta('Recepción iniciada', 'exito');
    cargarRecepciones();
  } catch (e) { alerta('Error', 'error'); }
}

function pantalla(id) {
  ['pantalla-login','pantalla-operario','pantalla-admin','pantalla-recepcion'].forEach(p => {
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