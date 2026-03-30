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
    const [siesa, db] = await Promise.all([
      get('/api/siesa/pedidos').catch(() => ({ pedidos: [] })),
      get('/api/picking/?per_page=20').catch(() => ({ tareas: [] }))
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
        return `
          <div class="tabla-card">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
              <div style="min-width:0;">
                <div style="font-size:15px;font-weight:700;">${p.numero_pedido}</div>
                <div style="font-size:12px;color:#666;margin-top:2px;">${p.cliente || 'Sin cliente'}</div>
                <div style="font-size:11px;color:#444;margin-top:2px;">${p.items.length} producto(s) · ${totalUds} uds</div>
                ${sinProd ? `<div style="font-size:11px;color:#d97706;margin-top:2px;">⚠ ${sinProd} sin registrar en WMS</div>` : ''}
              </div>
              <button onclick="iniciarDespachoDesdeSiesa(${i})"
                style="flex-shrink:0;background:#fff;color:#000;border:none;padding:10px 14px;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">
                Despachar
              </button>
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
              <div style="font-size:12px;color:#666;margin-top:2px;">${t.codigo} · ${t.ubicacion_codigo || '—'}</div>
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
  if (RECEPCION_ACTUAL) { await procesarScanRecepcion(codigo); return; }
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