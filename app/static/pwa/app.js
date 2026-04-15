'use strict';

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
let REC_TAB_ACTIVO = 'ocs';   // tab activo en pantalla recepcionista
let TIMER_REC = null;          // polling recepcionista (30 seg)
let SIESA_PEDIDOS = [];        // pedidos cargados desde Siesa (admin tab-pedidos)
let SIESA_OCS = [];            // OCs cargadas desde Siesa (pantalla recepcionista)
let RUTA_ACTIVA_ID = null;     // ruta EN_CARGUE seleccionada en tab-muelle
let RUTAS_TIPO_SEL = 'Urbana'; // tipo seleccionado en form nueva ruta
let RUTAS_SUBTAB = 'rutas';    // sub-tab activo en tab-rutas
let MUELLE_TIMER = null;

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
  const esConductor = rol === 'conductor';
  const esTienda = rol === 'tienda';
  const puedeEmpacar = OPERARIO?.puede_empacar || false;
  const puedePicar   = OPERARIO?.puede_picar !== false; // default true
  if (esTienda) {
    pantalla('pantalla-tienda');
    document.getElementById('tienda-nombre').textContent =
      OPERARIO.nombre_punto_venta || OPERARIO.nombre || 'Punto de Venta';
    tiendaIniciar();
  } else if (esConductor) {
    pantalla('pantalla-conductor');
    document.getElementById('cond-nombre').textContent = OPERARIO.nombre || '—';
    cargarRutasConductor();
    TIMER_OPERARIO = setInterval(cargarRutasConductor, 30000);
  } else if (esAdmin) {
    pantalla('pantalla-admin');
    if (OPERARIO) actualizarUI(OPERARIO);
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
    ['conexion-status','conexion-status-admin','conexion-status-tienda'].forEach(id => {
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
    try { const d = await r.json(); msg = d.error || msg; } catch (_) {}
    const err = new Error(msg);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

async function get(url) {
  const r = await fetch(API + url, { headers: { Authorization: 'Bearer ' + TOKEN } });
  return _checkResp(r);
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
  ['op-nombre','admin-nombre','rec-nombre'].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = op.nombre; });
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
  else if (TAB === 'tab-operarios') await cargarOperarios();
  else if (TAB === 'tab-usuarios') await cargarUsuarios();
  else if (TAB === 'tab-stock') await cargarStock();
  else if (TAB === 'tab-connekta') await cargarConnekta();
  else if (TAB === 'tab-muelle') await cargarMuelle();
  else if (TAB === 'tab-rutas') await cargarRutas();
  else if (TAB === 'tab-inventario') await cargarInventario();
  else if (TAB === 'tab-traslados') await cargarTrasladosAdmin();
}

function tab(id) {
  const TABS = ['tab-dashboard','tab-pedidos','tab-operarios','tab-usuarios','tab-stock','tab-connekta','tab-muelle','tab-rutas','tab-inventario','tab-traslados'];
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
    set('kpi-pick-pend', k.picking.total_activo);
    set('kpi-pack-hoy', k.packing.facturas_generadas_hoy);
    set('kpi-rec-hoy', k.recepcion.confirmadas_hoy);
    set('kpi-alertas', k.alertas.productos_bajo_minimo);
    graficaActividad(k);
    movimientos(d.movimientos_recientes.movimientos);
    // Auditorías urgentes — Supervisor Guard
    const nAud = d.auditorias_urgentes || 0;
    const audEl = document.getElementById('dashboard-auditorias-urgentes');
    if (audEl) {
      audEl.style.display = nAud > 0 ? 'block' : 'none';
      const badge = document.getElementById('aud-urgentes-count');
      if (badge) badge.textContent = nAud;
      if (nAud > 0) cargarAuditoriasUrgentes();
    }
    // Tareas de picking bloqueadas — pedidos del cliente sin resolver
    const nBloq = d.tareas_bloqueadas || 0;
    const bloqEl = document.getElementById('dashboard-tareas-bloqueadas');
    if (bloqEl) {
      bloqEl.style.display = nBloq > 0 ? 'block' : 'none';
      const bloqBadge = document.getElementById('bloq-count');
      if (bloqBadge) bloqBadge.textContent = nBloq;
    }
    // Traslados estancados EN_TRANSITO
    const tr = d.traslados_en_riesgo || {};
    const nCriticos = tr.total_critico || 0;
    const nAlertas = tr.total_alerta || 0;
    const trEl = document.getElementById('dashboard-traslados-riesgo');
    if (trEl) {
      trEl.style.display = (nCriticos + nAlertas) > 0 ? 'block' : 'none';
      const elCrit = document.getElementById('traslados-criticos-count');
      const elAlerta = document.getElementById('traslados-alerta-count');
      if (elCrit) elCrit.textContent = nCriticos;
      if (elAlerta) elAlerta.textContent = nAlertas;
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

async function reabrirTareaPicking(id) {
  if (!confirm('¿Reabrir esta tarea al pool de picking? El operario que llegue a esa ubicación la tomará de nuevo.')) return;
  try {
    const r = await fetch(API + `/api/picking/${id}/reabrir`, { method: 'PUT', headers: { Authorization: 'Bearer ' + TOKEN } });
    const d = await r.json();
    if (r.ok) { alerta('Tarea reabierta al pool ✓', 'exito'); await cargarPedidos(); }
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
    if (r.ok) { alerta('Tarea cancelada', 'advertencia'); await cargarPedidos(); }
    else alerta(d.error || 'Error al cancelar', 'error');
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
      const MOTIVO_LABEL = { UBICACION_VACIA:'📦 Ubicación vacía', FALTANTE:'📉 Faltante parcial', MERCANCIA_AVERIADA:'🚫 Mercancía averiada', PRODUCTO_INCORRECTO:'❌ Producto incorrecto' };
      html += db.tareas.map(t => `
        <div class="tabla-card" style="${t.estado==='BLOQUEADO'?'border-color:#7f1d1d;background:#110a0a;':''}">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
            <div style="flex:1;min-width:0;">
              <div style="font-size:14px;font-weight:600;">${t.producto_nombre || t.producto_codigo}</div>
              <div style="font-size:12px;color:#666;margin-top:2px;">${t.referencia_documento || t.codigo} · ${t.ubicacion_codigo || '—'}</div>
              <div style="font-size:11px;color:#444;margin-top:2px;">${t.operario_id ? '👤 En proceso' : t.estado === 'BLOQUEADO' ? '🔴 Bloqueado — ' + (MOTIVO_LABEL[t.motivo_bloqueo] || t.motivo_bloqueo || 'novedad reportada') : '⏳ En cola'}</div>
              ${t.estado === 'BLOQUEADO' && t.observaciones_bloqueo ? `<div style="font-size:11px;color:#ef4444;margin-top:3px;font-style:italic;">"${t.observaciones_bloqueo}"</div>` : ''}
            </div>
            <div style="text-align:right;flex-shrink:0;">
              <span class="badge ${t.estado==='EN_PROCESO'?'badge-blue':t.estado==='COMPLETADO'?'badge-green':t.estado==='BLOQUEADO'?'badge-red':'badge-yellow'}">${t.estado}</span>
              <div style="font-size:20px;font-weight:800;margin-top:4px;">${t.cantidad_recogida||0}/${t.cantidad_solicitada}</div>
            </div>
          </div>
          ${t.estado === 'BLOQUEADO' ? `
          <div style="display:flex;gap:8px;margin-top:10px;padding-top:10px;border-top:1px solid #2a1010;">
            <button onclick="reabrirTareaPicking(${t.id})"
              style="flex:1;padding:9px;background:#1e3a1e;color:#4ade80;border:1px solid #166534;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
              ↩ Reabrir al pool
            </button>
            <button onclick="cancelarTareaPicking(${t.id})"
              style="flex:1;padding:9px;background:#1a0a0a;color:#f87171;border:1px solid #7f1d1d;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
              ✕ Cancelar tarea
            </button>
          </div>` : ''}
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
    (prod.operarios || []).forEach(op => { metricas[op.operario_id || op.id] = op; });

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
      document.getElementById('contenido-tarea').innerHTML = `
        <div style="text-align:center;padding:40px 20px 16px;">
          <div style="font-size:60px;">✓</div>
          <div style="font-size:24px;font-weight:700;margin-top:12px;">Sin tareas de picking</div>
          <div style="font-size:14px;color:#666;margin-top:6px;">El sistema te asignará la próxima automáticamente</div>
        </div>
        <div id="traslados-operario" style="padding:0 16px 16px;"></div>`;
      cargarTrasladosOperario();
      return;
    }
    TAREA_ACTUAL = d;
    renderTarea(d);
  } catch (e) {
    console.error('Error cargando tarea:', e);
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
}

// ─── Quagga2 — debounce interno ───────────────────────────────────────────────
function _onQuaggaDetect(result) {
  const code = result && result.codeResult && result.codeResult.code;
  if (!code) return;
  const now = Date.now();
  if (now - _SCAN_LAST_TS < 900) return;   // 900 ms cooldown entre scans
  _SCAN_LAST_TS = now;
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

  await new Promise(resolve => {
    Quagga.init({
      inputStream: {
        type: 'LiveStream',
        target,
        constraints: {
          facingMode: 'environment',
          width:  { min: 640, ideal: 1280 },
          height: { min: 480, ideal: 720  }
        }
      },
      decoder: {
        readers: [
          'ean_reader', 'ean_8_reader',
          'code_128_reader', 'code_39_reader',
          'upc_reader', 'upc_e_reader'
        ]
      },
      locate: true,
      numOfWorkers: 0,   // sin web-workers → funciona desde CDN en cualquier browser
      frequency: 10
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
    if (r.error) { beepError(); alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    beepOk();
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
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div style="background:#111;border-radius:16px;padding:24px;width:100%;max-width:380px;border:1px solid #7f1d1d;">
        <div style="font-size:18px;font-weight:700;margin-bottom:4px;color:#f87171;">⚠ Reportar problema</div>
        <div style="font-size:12px;color:#555;margin-bottom:16px;">La tarea se bloquea. El jefe auditará y ajustará el inventario.</div>

        <button onclick="confirmarProblema(${tareaId},'UBICACION_VACIA',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          📦 Ubicación vacía — no había nada
        </button>

        <button onclick="mostrarShortPick(${tareaId})"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#451a03;color:#fb923c;border:1px solid #7c2d12;border-radius:10px;cursor:pointer;text-align:left;">
          📉 Faltante parcial — encontré menos
        </button>

        <button onclick="confirmarProblema(${tareaId},'MERCANCIA_AVERIADA',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          🚫 Mercancía averiada
        </button>

        <button onclick="confirmarProblema(${tareaId},'PRODUCTO_INCORRECTO',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          ❌ Producto incorrecto
        </button>

        <div id="short-pick-panel" style="display:none;background:#1a0a00;border:1px solid #7c2d12;border-radius:10px;padding:14px;margin-bottom:8px;">
          <div style="font-size:12px;color:#fb923c;margin-bottom:8px;">¿Cuántas unidades SÍ encontraste?</div>
          <input id="short-pick-qty" type="number" min="0" placeholder="Cantidad encontrada"
            style="width:100%;padding:10px;background:#000;border:2px solid #7c2d12;border-radius:8px;color:#fff;font-size:16px;margin-bottom:10px;">
          <button onclick="confirmarShortPick(${tareaId})"
            style="width:100%;padding:12px;background:#fb923c;color:#000;border:none;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">
            Confirmar faltante parcial
          </button>
        </div>

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

function mostrarShortPick(tareaId) {
  const panel = document.getElementById('short-pick-panel');
  if (panel) panel.style.display = 'block';
}

async function confirmarShortPick(tareaId) {
  const qty = parseInt(document.getElementById('short-pick-qty')?.value || '0');
  await confirmarProblema(tareaId, 'FALTANTE', qty);
}

async function confirmarProblema(tareaId, motivo, cantidadEncontrada) {
  const observaciones = document.getElementById('obs-problema')?.value?.trim() || '';
  const modal = document.getElementById('modal-problema');
  if (modal) modal.remove();
  try {
    await post('/api/picking/' + tareaId + '/reportar-problema', {
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
    alerta('Error reportando problema', 'error');
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
  ['pantalla-login','pantalla-operario','pantalla-admin','pantalla-recepcion','pantalla-empacador','pantalla-conductor','pantalla-tienda'].forEach(p => {
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

      <div style="background:#111;border-radius:10px;padding:12px;margin-bottom:12px;">
        <div style="font-size:12px;color:#666;text-align:center;margin-bottom:10px;">Escanea cada producto — 1 escaneo = 1 unidad</div>
        <button onclick="abrirCamara('lector-qr-rec','camara-box-rec')"
          style="width:100%;padding:13px;font-size:16px;background:#fff;color:#000;border:2px solid #000;border-radius:10px;cursor:pointer;margin-bottom:8px;">
          📷 Escanear con cámara
        </button>
        <div id="camara-box-rec" style="display:none;margin-bottom:8px;">
          <div id="lector-qr-rec" style="border-radius:10px;overflow:hidden;"></div>
          <button onclick="cerrarCamara('camara-box-rec')" style="width:100%;padding:9px;margin-top:6px;font-size:14px;background:#333;color:#fff;border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
        </div>
        <div style="display:flex;gap:8px;">
          <input id="rec-codigo-manual" type="text" placeholder="O escribe / pega el código aquí"
            style="flex:1;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ procesarScan(v); this.value=''; } }">
          <button onclick="const v=document.getElementById('rec-codigo-manual').value.trim();if(v){procesarScan(v);document.getElementById('rec-codigo-manual').value='';}"
            style="padding:10px 14px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
        </div>
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
  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
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

  const activo = 'border-bottom:2px solid #1E8395;color:#1E8395;font-weight:600;';
  const inactivo = 'border-bottom:2px solid transparent;color:#415A70;';
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
    if (!/Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
      document.getElementById('scanner-input').focus();
    }
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
      <select id="u-rol" onchange="document.getElementById('u-tienda-fields').style.display=this.value==='tienda'?'block':'none'"
        style="padding:12px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;">
        <option value="operario" ${(u.rol||'operario')==='operario'?'selected':''}>Operario</option>
        <option value="recepcionista" ${u.rol==='recepcionista'?'selected':''}>Recepcionista</option>
        <option value="conductor" ${u.rol==='conductor'?'selected':''}>Conductor</option>
        <option value="tienda" ${u.rol==='tienda'?'selected':''}>Tienda (punto de venta)</option>
        <option value="supervisor" ${u.rol==='supervisor'?'selected':''}>Supervisor</option>
        <option value="jefe_almacen" ${u.rol==='jefe_almacen'?'selected':''}>Jefe de almacén</option>
        <option value="admin" ${u.rol==='admin'?'selected':''}>Admin</option>
      </select>
      <!-- Campos tienda (solo si rol=tienda) -->
      <div id="u-tienda-fields" style="display:${(u.rol==='tienda')?'block':'none'};">
        <input id="u-bodega-siesa" placeholder="ID Bodega Siesa (ej. TP1)" value="${u.bodega_siesa_id || ''}"
          style="width:100%;padding:12px;background:#1a1a1a;border:1px solid #f59e0b;border-radius:8px;color:#fff;font-size:14px;box-sizing:border-box;">
        <input id="u-nombre-pv" placeholder="Nombre punto de venta (ej. Tienda Centro)" value="${u.nombre_punto_venta || ''}"
          style="width:100%;padding:12px;background:#1a1a1a;border:1px solid #f59e0b;border-radius:8px;color:#fff;font-size:14px;margin-top:8px;box-sizing:border-box;">
      </div>
      <div style="background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:14px;">
        <div style="font-size:12px;font-weight:600;color:#aaa;margin-bottom:12px;text-transform:uppercase;letter-spacing:0.05em;">Capacidades operativas</div>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-picar" ${u.puede_picar!==false?'checked':''} style="width:20px;height:20px;accent-color:#60a5fa;">
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
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;">
          <input type="checkbox" id="u-puede-camara" ${u.puede_usar_camara!==false?'checked':''} style="width:20px;height:20px;accent-color:#34d399;">
          <div>
            <div style="font-size:14px;font-weight:600;color:#34d399;">📷 Usar cámara para escanear</div>
            <div style="font-size:11px;color:#555;">Muestra botón de cámara en picking y recepción</div>
          </div>
        </label>
        <div style="margin-top:14px;padding-top:14px;border-top:1px solid #222;">
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
  const puedePicar   = document.getElementById('u-puede-picar')?.checked;
  const puedeEmpacar = document.getElementById('u-puede-empacar')?.checked;
  const puedeCamara  = document.getElementById('u-puede-camara')?.checked ?? true;
  const capacidadConteo = parseInt(document.getElementById('u-capacidad-conteo')?.value || '15', 10);

  if (!nombre) { alerta('El nombre es requerido', 'error'); return; }

  const bodegaSiesaId = document.getElementById('u-bodega-siesa')?.value.trim() || null;
  const nombrePv = document.getElementById('u-nombre-pv')?.value.trim() || null;

  const payload = {
    nombre, rol, puede_picar: puedePicar, puede_empacar: puedeEmpacar, puede_usar_camara: puedeCamara,
    capacidad_diaria_conteo: isNaN(capacidadConteo) ? 15 : Math.max(0, capacidadConteo),
    bodega_siesa_id: bodegaSiesaId, nombre_punto_venta: nombrePv
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
      opt.textContent = `#${r.id} · ${r.conductor_nombre} · ${r.vehiculo_placa || ''} · ${r.tipo_ruta} · ${r.total_bultos} bultos`;
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
        const rutaLista = d.ruta_lista ? ' · ✓ Ruta lista para salir' : ` · ${d.bultos_pendientes_pedido} bulto${d.bultos_pendientes_pedido !== 1 ? 's' : ''} pendientes en pedido`;
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
  const btnEntregar = r.estado === 'EN_TRANSITO'
    ? `<button onclick="rutaEntregar(${r.id})" style="flex:1;padding:10px;background:#14532d;color:#4ade80;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">✓ Entregada</button>`
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
      if (d.sugeridos_count > 0) {
        // Asignar automáticamente los sugeridos y abrir muelle
        await fetch(API + '/api/muelle/asignar', {
          method: 'POST',
          headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
          body: JSON.stringify({ ruta_id: id, bultos_ids: d.sugeridos_ids }),
        });
        alerta(`✓ ${d.sugeridos_count} bulto${d.sugeridos_count !== 1 ? 's' : ''} asignados automáticamente. Abriendo muelle...`, 'exito');
        // Cambiar a tab muelle con esta ruta activa
        setTimeout(() => {
          tab('tab-muelle');
          RUTA_ACTIVA_ID = id;
          const sel = document.getElementById('muelle-ruta-select');
          if (sel) sel.value = id;
          muelleSeleccionarRuta(String(id));
        }, 800);
      } else {
        alerta('Ruta iniciada. Sin bultos sugeridos para auto-asignar.', 'advertencia');
      }
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
    const d = await get('/api/rutas/' + id);
    const ruta = d.ruta;
    const manifiesto = ruta.manifiesto || [];
    let html = `<b>Ruta #${ruta.id} — ${ruta.conductor_nombre} — ${ruta.tipo_ruta}</b>\n\n`;
    manifiesto.forEach(grupo => {
      html += `📍 ${grupo.destino}\n`;
      grupo.bultos.forEach(b => {
        html += `  ${b.codigo_barras} · ${b.tipo} ${b.numero}/${b.total} · ${b.numero_pedido} · ${b.cliente}\n`;
      });
      html += '\n';
    });
    alert(html);
  } catch (e) { alert('Error cargando manifiesto'); }
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
  const btnU = document.getElementById('rutas-tipo-urbana');
  const btnM = document.getElementById('rutas-tipo-municipal');
  if (btnU) { btnU.style.background = tipo === 'Urbana' ? '#0C3535' : '#0D1622'; btnU.style.color = tipo === 'Urbana' ? '#25BBBB' : '#415A70'; btnU.style.borderColor = tipo === 'Urbana' ? '#174848' : '#1C2B3A'; }
  if (btnM) { btnM.style.background = tipo === 'Municipal' ? '#0C3535' : '#0D1622'; btnM.style.color = tipo === 'Municipal' ? '#25BBBB' : '#415A70'; btnM.style.borderColor = tipo === 'Municipal' ? '#174848' : '#1C2B3A'; }
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
          <div style="display:flex;gap:6px;align-items:center;">
            ${m.activa
              ? '<span style="background:#14532d;color:#4ade80;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;">ACTIVA</span>'
              : '<span style="background:#3f1515;color:#f87171;padding:2px 8px;border-radius:6px;font-size:10px;font-weight:700;">INACTIVA</span>'}
            <button onclick="maestraToggle(${m.id},${!m.activa})"
              style="padding:5px 10px;background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:6px;font-size:11px;cursor:pointer;">
              ${m.activa ? 'Desactivar' : 'Activar'}
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

function maestraMostrarForm() {
  _MAESTRAS_PARADAS = [];
  document.getElementById('maestras-form').style.display = 'block';
  document.getElementById('maestras-form-error').textContent = '';
  _maestraRenderParadas();
}

function maestraCancelarForm() {
  document.getElementById('maestras-form').style.display = 'none';
}

function maestraAgregarParada() {
  const val = document.getElementById('maestras-parada-input')?.value.trim();
  if (!val) return;
  _MAESTRAS_PARADAS.push(val);
  document.getElementById('maestras-parada-input').value = '';
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

async function maestrasCrear() {
  const errorEl = document.getElementById('maestras-form-error');
  const nombre  = document.getElementById('maestra-form-nombre')?.value.trim();
  errorEl.textContent = '';

  if (!nombre) { errorEl.textContent = 'El nombre es requerido'; return; }
  if (!_MAESTRAS_PARADAS.length) { errorEl.textContent = 'Agrega al menos una parada'; return; }

  try {
    const r = await fetch(API + '/api/rutas/maestras', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ nombre, tipo_ruta: RUTAS_TIPO_SEL, paradas: _MAESTRAS_PARADAS }),
    });
    const d = await r.json();
    if (r.ok) {
      maestraCancelarForm();
      document.getElementById('maestra-form-nombre').value = '';
      _MAESTRAS_PARADAS = [];
      await cargarListaMaestras();
    } else {
      errorEl.textContent = d.error || 'Error al guardar';
    }
  } catch (e) { errorEl.textContent = 'Error de conexión'; }
}

async function maestraToggle(id, activar) {
  try {
    await fetch(API + '/api/rutas/maestras/' + id, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ activa: activar }),
    });
    await cargarListaMaestras();
  } catch (e) { alert('Error de conexión'); }
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
        <div style="display:flex;gap:6px;">
          <button onclick="conductorEditar(${c.id})"
            style="flex:1;padding:8px;background:#1a1a2a;border:1px solid #2d1b69;color:#a78bfa;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;">
            ✏ Editar
          </button>
          <button onclick="conductorToggle(${c.id}, ${!c.activo})"
            style="flex:1;padding:8px;background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:8px;font-size:12px;cursor:pointer;">
            ${c.activo ? 'Desactivar' : 'Activar'}
          </button>
        </div>
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

let _CONDUCTOR_EDIT_ID = null;

async function conductorEditar(id) {
  // Cargar datos del conductor y abrir modal de edición
  _CONDUCTOR_EDIT_ID = id;
  const modal = document.getElementById('modal-conductor-edit');
  if (!modal) return;

  // Precargar usuarios disponibles
  try {
    const [dc, du] = await Promise.all([
      get('/api/rutas/conductores?activos=false'),
      get('/api/rutas/usuarios-conductores'),
    ]);
    const conductor = (dc.conductores || []).find(c => c.id === id);
    if (!conductor) { alert('Conductor no encontrado'); return; }

    document.getElementById('cedit-nombre').value    = conductor.nombre;
    document.getElementById('cedit-telefono').value  = conductor.telefono || '';

    const sel = document.getElementById('cedit-usuario');
    sel.innerHTML = '<option value="">Sin cuenta (solo flota)</option>' +
      (du.usuarios || []).map(u =>
        `<option value="${u.id}" ${u.id === conductor.usuario_id ? 'selected' : ''}>${u.nombre} (${u.email})</option>`
      ).join('');

    document.getElementById('cedit-error').textContent = '';
    modal.style.display = 'flex';
  } catch (e) { alert('Error cargando datos del conductor'); }
}

async function conductorEditarGuardar() {
  const id       = _CONDUCTOR_EDIT_ID;
  const nombre   = document.getElementById('cedit-nombre')?.value.trim();
  const telefono = document.getElementById('cedit-telefono')?.value.trim();
  const usuarioId = document.getElementById('cedit-usuario')?.value || null;
  const errorEl  = document.getElementById('cedit-error');
  errorEl.textContent = '';

  if (!nombre) { errorEl.textContent = 'El nombre es requerido'; return; }

  try {
    const r = await fetch(API + '/api/rutas/conductores/' + id, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        nombre,
        telefono: telefono || null,
        usuario_id: usuarioId ? parseInt(usuarioId) : null,
      }),
    });
    const d = await r.json();
    if (r.ok) {
      cerrarModalConductorEdit();
      await cargarListaConductores();
      alerta('Conductor actualizado', 'exito');
    } else {
      errorEl.textContent = d.error || 'Error al guardar';
    }
  } catch (e) { errorEl.textContent = 'Error de conexión'; }
}

function cerrarModalConductorEdit() {
  const modal = document.getElementById('modal-conductor-edit');
  if (modal) modal.style.display = 'none';
  _CONDUCTOR_EDIT_ID = null;
}

// ─────────────────────────────────────────────────────────────
// CONDUCTOR — Pantalla de confirmación de entregas en campo
// ─────────────────────────────────────────────────────────────

let _COND_RUTAS = [];
let _COND_RUTA_ACTIVA = null;   // ruta seleccionada
let _COND_PARADAS = [];         // paradas de la ruta activa
let _COND_PARADA_FORM = null;   // parada en formulario de confirmación

// ── Lista de rutas del conductor ──────────────────────────────────

async function cargarRutasConductor() {
  const el = document.getElementById('cond-contenido');
  if (!el) return;
  try {
    const d = await get('/api/rutas/mis-rutas');
    _COND_RUTAS = d.rutas || [];
    document.getElementById('cond-badge-rutas').textContent = _COND_RUTAS.length;

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
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando rutas. Verifica conexión.</div>';
  }
}

// ── Lista de paradas de la ruta ───────────────────────────────────

async function condAbrirParadas(rutaId) {
  const el = document.getElementById('cond-contenido');
  el.innerHTML = '<div style="text-align:center;padding:60px;color:#555;">Cargando paradas...</div>';
  try {
    const d = await get('/api/rutas/' + rutaId + '/paradas');
    _COND_RUTA_ACTIVA = { id: rutaId };
    _COND_PARADAS = d.paradas || [];
    _condRenderParadas(d);
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando paradas.</div>';
  }
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
    const colorBorde = r
      ? (r.estado_entrega === 'ENTREGADO' ? '#166534' : r.estado_entrega === 'PARCIAL' ? '#78350f' : '#7f1d1d')
      : '#222';
    const colorFondo = r
      ? (r.estado_entrega === 'ENTREGADO' ? '#0d1a0d' : r.estado_entrega === 'PARCIAL' ? '#1a0d00' : '#1a0d0d')
      : '#111';
    const badge = r
      ? (r.estado_entrega === 'ENTREGADO'
          ? `<span style="background:#166534;color:#4ade80;padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;">ENTREGADO</span>`
          : r.estado_entrega === 'PARCIAL'
          ? `<span style="background:#78350f;color:#fbbf24;padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;">PARCIAL</span>`
          : `<span style="background:#7f1d1d;color:#f87171;padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;">RECHAZADO</span>`)
      : `<span style="background:#1a1a1a;color:#555;padding:2px 8px;border-radius:8px;font-size:10px;font-weight:700;">PENDIENTE</span>`;
    const monto = r ? ` · $${Number(r.monto_cobrado || 0).toLocaleString('es-CO')}` : '';

    html += `
      <div style="background:${colorFondo};border:1px solid ${colorBorde};border-radius:12px;padding:14px;margin-bottom:8px;cursor:pointer;"
           onclick="condAbrirFormParada(${idx})">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div style="flex:1;min-width:0;">
            <div style="font-size:14px;font-weight:800;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${p.cliente}</div>
            <div style="font-size:12px;color:#666;margin-top:2px;">📍 ${p.municipio} · ${p.numero_pedido}</div>
            <div style="font-size:11px;color:#555;margin-top:2px;">${p.bultos.length} bulto${p.bultos.length !== 1 ? 's' : ''}${monto}</div>
          </div>
          <div style="margin-left:10px;flex-shrink:0;">${badge}</div>
        </div>
        ${r ? `<div style="font-size:11px;color:#555;margin-top:6px;">${r.forma_pago || ''}${r.observaciones ? ' · ' + r.observaciones.substring(0,40) : ''}</div>` : ''}
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
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;border:2px solid ${e===estadoActual ? (e==='ENTREGADO'?'#166534':e==='PARCIAL'?'#78350f':'#7f1d1d') : '#222'};background:${e===estadoActual ? (e==='ENTREGADO'?'#0d1a0d':e==='PARCIAL'?'#1a0d00':'#1a0d0d') : '#111'};color:${e===estadoActual ? (e==='ENTREGADO'?'#4ade80':e==='PARCIAL'?'#fbbf24':'#f87171') : '#555'};">
            ${e === 'ENTREGADO' ? '✓ Entregado' : e === 'PARCIAL' ? '⚠ Parcial' : '✗ Rechazado'}
          </button>`).join('')}
      </div>
    </div>

    <div id="cond-bultos-rechazo" style="margin-bottom:14px;display:${estadoActual !== 'ENTREGADO' ? 'block' : 'none'};">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">BULTOS RECHAZADOS</label>
      ${p.bultos.map(b => `
        <label style="display:flex;align-items:center;gap:10px;padding:10px;background:#1a1a1a;border-radius:8px;margin-bottom:6px;cursor:pointer;">
          <input type="checkbox" value="${b.id}" ${rechazadosActuales.includes(b.id) ? 'checked' : ''}
            style="width:18px;height:18px;cursor:pointer;" id="chk-bulto-${b.id}">
          <span style="font-size:13px;color:#ccc;">${b.codigo_barras} · ${b.tipo} ${b.numero}/${b.total}</span>
        </label>`).join('')}
    </div>

    <div style="margin-bottom:14px;">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">FORMA DE PAGO</label>
      <select id="cond-forma-pago"
        style="width:100%;padding:14px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:15px;">
        <option value="">— Seleccionar —</option>
        ${['EFECTIVO','TRANSFERENCIA','CHEQUE','CREDITO','EXENTO'].map(f =>
          `<option value="${f}" ${f===formaActual?'selected':''}>${f.charAt(0)+f.slice(1).toLowerCase()}</option>`
        ).join('')}
      </select>
    </div>

    <div style="margin-bottom:14px;">
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
        style="width:100%;padding:10px;background:#1a1a1a;border:1px dashed #333;color:#aaa;border-radius:10px;font-size:13px;box-sizing:border-box;">
      ${r && r.foto_entrega ? `<div style="margin-top:8px;font-size:11px;color:#4ade80;">✓ Foto guardada (subir nueva para reemplazar)</div>` : ''}
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
    const colores = { ENTREGADO: ['#166534','#0d1a0d','#4ade80'], PARCIAL: ['#78350f','#1a0d00','#fbbf24'], RECHAZADO: ['#7f1d1d','#1a0d0d','#f87171'] };
    const [borde, fondo, texto] = activo ? colores[e] : ['#222','#111','#555'];
    btn.style.borderColor = borde;
    btn.style.background  = fondo;
    btn.style.color       = texto;
  });

  const divRechazo = document.getElementById('cond-bultos-rechazo');
  if (divRechazo) divRechazo.style.display = estado !== 'ENTREGADO' ? 'block' : 'none';
}

async function condGuardarParada() {
  const el = document.getElementById('cond-contenido');
  const p = _COND_PARADA_FORM;
  if (!el || !p) return;

  const estadoEntrega = el._estadoSel || 'ENTREGADO';
  const formaPago     = document.getElementById('cond-forma-pago')?.value || '';
  const monto         = parseFloat(document.getElementById('cond-monto')?.value || 0) || 0;
  const obs           = document.getElementById('cond-obs')?.value?.trim() || '';

  // Bultos rechazados
  const bultosRechazados = [];
  if (estadoEntrega !== 'ENTREGADO') {
    p.bultos.forEach(b => {
      const chk = document.getElementById('chk-bulto-' + b.id);
      if (chk && chk.checked) bultosRechazados.push(b.id);
    });
    // RECHAZADO total: el backend auto-selecciona todos los bultos — no bloquear aquí
    // PARCIAL sí requiere selección explícita
    if (estadoEntrega === 'PARCIAL' && !bultosRechazados.length) {
      alerta('Para entrega parcial selecciona qué bultos fueron rechazados', 'error');
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

  const btn = el.querySelector('button[onclick="condGuardarParada()"]');
  if (btn) { btn.disabled = true; btn.textContent = 'Guardando...'; }

  try {
    const r = await fetch(API + '/api/rutas/' + _COND_RUTA_ACTIVA.id + '/paradas/' + p.tarea_id + '/confirmar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({
        estado_entrega:    estadoEntrega,
        forma_pago:        formaPago || null,
        monto_cobrado:     monto,
        observaciones:     obs || null,
        foto_entrega:      fotoBase64 || null,
        bultos_rechazados: bultosRechazados,
      }),
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
  if (obs === null) return; // canceló
  try {
    const r = await fetch(API + `/api/rutas/${_COND_RUTA_ACTIVA.id}/paradas/${p.tarea_id}/confirmar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({
        estado_entrega: 'RECHAZADO',
        forma_pago: 'EXENTO',
        monto_cobrado: 0,
        observaciones: obs || 'Cliente no disponible',
        bultos_rechazados: [],  // backend auto-rechaza todos
      })
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
            ${r && r.observaciones ? ' · "' + r.observaciones.substring(0, 50) + '"' : ''}
          </div>
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
            `<option value="${a.id}">${a.nombre}</option>`
          ).join('');
        }
      }
    } catch (e) { /* silencioso */ }
  }
  if (_INV_SUBTAB === 'conteos') await cargarConteos();
  else await cargarResumenAbc();
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

function conteosFiltrar() {
  _CONTEO_PAGE = 1;  // resetear a página 1 cuando cambian los filtros
  cargarConteos();
}

async function cargarConteos(page) {
  if (page !== undefined) _CONTEO_PAGE = page;
  const lista = document.getElementById('inv-conteos-lista');
  const pag   = document.getElementById('inv-conteos-paginacion');
  if (!lista) return;

  const estado = document.getElementById('inv-filtro-estado')?.value || '';
  const marca  = document.getElementById('inv-filtro-marca')?.value?.trim() || '';
  const clase  = document.getElementById('inv-filtro-clase')?.value || '';

  // Solo mostrar "Cargando" en primera carga — no en refresh de fondo
  const esVacia = !lista.innerHTML.trim() || lista.innerHTML.includes('Cargando');
  if (esVacia) lista.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';

  try {
    const qs = new URLSearchParams({ page: _CONTEO_PAGE });
    if (estado) qs.set('estado', estado);
    if (marca)  qs.set('marca', marca);
    if (clase)  qs.set('clasificacion', clase);

    const r = await fetch(API + '/api/conteo/?' + qs, { headers: { Authorization: 'Bearer ' + TOKEN } });
    const d = await r.json();
    const sesiones = d.sesiones || [];
    const total = d.total || 0;
    const totalPag = d.total_paginas || 1;

    if (!sesiones.length) {
      lista.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">No hay conteos con este filtro</div>';
      if (pag) pag.innerHTML = '';
      return;
    }

    const colEstado = {
      PENDIENTE: '#555', EN_PROCESO: '#1d4ed8', SEGUNDO_CONTEO: '#7c3aed',
      DESCUADRE: '#b45309', MATCH: '#166534', AJUSTADO: '#065f46'
    };
    lista.innerHTML = sesiones.map(s => {
      const col = colEstado[s.estado] || '#333';
      const dif = s.diferencia != null ? (s.diferencia > 0 ? `+${s.diferencia}` : s.diferencia) : '—';
      const difCol = s.diferencia > 0 ? '#4ade80' : s.diferencia < 0 ? '#f87171' : '#aaa';
      const tipoTag = s.tipo === 'MANUAL'
        ? `<span style="background:#1a1a2a;color:#a78bfa;font-size:9px;font-weight:700;padding:1px 6px;border-radius:6px;">MANUAL</span>`
        : s.tipo === 'EXCEPCION_PICKING'
        ? `<span style="background:#1a0a0a;color:#f87171;font-size:9px;font-weight:700;padding:1px 6px;border-radius:6px;">PICKING</span>`
        : '';
      return `
      <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
          <div style="flex:1;min-width:0;">
            <div style="font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.producto_codigo || '—'}</div>
            <div style="font-size:11px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${s.producto_nombre || ''}</div>
          </div>
          <div style="display:flex;gap:4px;align-items:center;flex-shrink:0;margin-left:8px;">
            ${tipoTag}
            ${s.clasificacion_abc ? `<span style="background:#1c1a0a;color:#f59e0b;font-size:10px;font-weight:700;padding:2px 6px;border-radius:8px;">ABC-${s.clasificacion_abc}</span>` : ''}
            <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:8px;">${s.estado}</span>
          </div>
        </div>
        <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#666;">
          <span>📍 ${s.ubicacion_codigo || s.ubicacion_id || '—'}</span>
          <span>Δ <span style="color:${difCol};font-weight:700;">${dif}</span></span>
          <div style="display:flex;gap:4px;">
            ${s.estado === 'DESCUADRE' || s.estado === 'SEGUNDO_CONTEO' ? `<button onclick="ajustarConteo(${s.id})"
              style="background:#b45309;color:#fff;border:none;padding:4px 10px;border-radius:6px;font-size:11px;cursor:pointer;">
              Ajustar →</button>` : ''}
            ${s.estado !== 'AJUSTADO' && s.estado !== 'CANCELADO' ? `<button onclick="conteoAbrirEdicion(${JSON.stringify(s).replace(/"/g,'&quot;')})"
              style="background:#1e293b;color:#94a3b8;border:1px solid #334155;padding:4px 10px;border-radius:6px;font-size:11px;cursor:pointer;">
              ✏ Editar</button>` : ''}
          </div>
        </div>
        ${s.operario_id ? `<div style="font-size:11px;color:#555;margin-top:3px;">👤 Op #${s.operario_id}</div>` : ''}
        ${s.editado_en ? `<div style="font-size:10px;color:#78350f;margin-top:2px;">✏ Editado por ${s.editado_por_nombre || '#'+s.editado_por} — ${s.motivo_edicion}</div>` : ''}
      </div>`;
    }).join('');

    // Paginación
    if (pag) {
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
    infoDiv.innerHTML = `
      <div style="font-size:12px;color:#888;margin-bottom:10px;">
        <b>${s.producto_codigo || '—'}</b> · ${s.producto_nombre || ''}<br>
        📍 ${s.ubicacion_codigo || s.ubicacion_id || '—'} · ABC-${s.clasificacion_abc || '?'}<br>
        ${s.existencia_siesa != null ? `Siesa: <b>${s.existencia_siesa}</b> uds` : 'Sin ref. Siesa'}
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

async function ajustarConteo(sesionId) {
  if (!confirm('¿Confirmar ajuste de inventario? Esto enviará el movimiento a Siesa.')) return;
  try {
    const r = await fetch(API + `/api/conteo/${sesionId}/ajustar`, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`Ajuste ${d.motivo_codigo} enviado a Siesa · Diferencia: ${d.diferencia}`, 'exito');
      await cargarConteos();
    } else {
      alerta(d.error || 'Error al ajustar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ══════════════════════════════════════════════════════════════════
// TRASLADOS — Admin tab
// ══════════════════════════════════════════════════════════════════

let _TRAS_SUBTAB = 'pendientes';
const TRAS_ESTADO = {
  pendientes: ['BORRADOR','ENVIADA','EN_PICKING','PREPARADO'],
  transito:   ['DESPACHADA','EN_TRANSITO'],
  historial:  ['ENTREGADA','RECHAZADA','CANCELADA']
};
const TRAS_COL = {
  BORRADOR:'#555', ENVIADA:'#1d4ed8', EN_PICKING:'#7c3aed', PREPARADO:'#166534',
  DESPACHADA:'#b45309', EN_TRANSITO:'#9a3412', ENTREGADA:'#065f46',
  RECHAZADA:'#7f1d1d', CANCELADA:'#374151'
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
  const fecha = s.fecha_creacion ? new Date(s.fecha_creacion).toLocaleDateString('es-CO') : '';
  const itemsResumen = (s.items || []).map(i =>
    `<div style="font-size:11px;color:#666;">${i.producto_codigo} · ${i.cantidad_solicitada} und${i.cantidad_aprobada && i.cantidad_aprobada !== i.cantidad_solicitada ? ` <span style="color:#f59e0b">(aprobado: ${i.cantidad_aprobada})</span>` : ''}</div>`
  ).join('');

  const acciones = [];
  if (s.estado === 'ENVIADA') {
    acciones.push(`<button onclick="trasAprobar(${s.id})" style="flex:1;padding:10px;background:#166534;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">Aprobar y asignar</button>`);
    acciones.push(`<button onclick="trasRechazar(${s.id})" style="padding:10px 12px;background:#7f1d1d;color:#fff;border:none;border-radius:8px;font-size:13px;cursor:pointer;">Rechazar</button>`);
  }
  if (s.estado === 'PREPARADO') {
    acciones.push(`<button onclick="trasDespachar(${s.id})" style="flex:1;padding:10px;background:#b45309;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">Despachar</button>`);
  }
  // Confirmar recepción es exclusivo de la tienda (flujo tienda → /recibir).
  // El admin NUNCA confirma recepción — haría entrar inventario sin verificación física.
  // Si Siesa falló en el despacho (siesa_necesita_atencion), el admin puede reintentar.
  if (s.siesa_necesita_atencion && s.estado === 'EN_TRANSITO') {
    acciones.push(`<button onclick="trasReintentarDespachoSiesa(${s.id})" style="flex:1;padding:10px;background:#7c3aed;color:#fff;border:none;border-radius:8px;font-size:13px;font-weight:700;cursor:pointer;">⚠ Reintentar Siesa</button>`);
  }

  return `
  <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
      <div>
        <div style="font-size:13px;font-weight:700;">${s.codigo}</div>
        <div style="font-size:11px;color:#666;">${s.nombre_punto_venta || s.bodega_destino_siesa} · ${fecha}</div>
      </div>
      <span style="background:${col};color:#fff;font-size:10px;font-weight:700;padding:3px 8px;border-radius:8px;">${s.estado}</span>
    </div>
    <div style="margin-bottom:${acciones.length?'10px':'0'};">${itemsResumen}</div>
    ${s.operario_nombre ? `<div style="font-size:11px;color:#7c3aed;margin-bottom:6px;">👷 ${s.operario_nombre}${s.estado==='PREPARADO'?' · Listo para despachar':' · Recogiendo'}</div>` : ''}
    ${s.siesa_error ? `<div style="font-size:10px;color:#f87171;margin-bottom:8px;">⚠ Siesa: ${s.siesa_error}</div>` : ''}
    ${acciones.length ? `<div style="display:flex;gap:8px;">${acciones.join('')}</div>` : ''}
  </div>`;
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

let _TIENDA_SUBTAB = 'solicitudes';
let _TIENDA_STOCK = [];          // cache del stock disponible en NB1
let _TIENDA_STOCK_ESTADO = 'cargando'; // 'cargando' | 'listo' | 'error'
let _TIENDA_CARRITO = []; // [{producto_id, codigo_siesa, nombre, cantidad, disponible}]

function tiendaIniciar() {
  _TIENDA_STOCK = [];
  _TIENDA_STOCK_ESTADO = 'cargando';
  _TIENDA_CARRITO = [];
  tiendaSubtab('solicitudes');
  tiendaCargarStock();  // pre-carga stock en background
}

function tiendaSubtab(nombre) {
  _TIENDA_SUBTAB = nombre;
  ['solicitudes','nueva','recibir'].forEach(k => {
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
    const d = await get('/api/traslados/stock-disponible');
    // El backend ahora devuelve nombre y producto_id directo desde WMS local
    _TIENDA_STOCK = (d.items || []).filter(i => i.producto_id && i.disponible > 0);
    _TIENDA_STOCK_ESTADO = 'listo';
  } catch (e) {
    _TIENDA_STOCK_ESTADO = 'error';
  }
  if (_TIENDA_SUBTAB === 'nueva') tiendaRenderStock();
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
  if (!confirm(`¿Enviar pedido con ${_TIENDA_CARRITO.length} productos al almacén?`)) return;

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
      body: JSON.stringify({ items })
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

async function tiendaCargarRecibir() {
  const el = document.getElementById('tienda-lista-recibir');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Cargando...</div>';
  try {
    const [r1, r2] = await Promise.all([
      get('/api/traslados/?estado=EN_TRANSITO'),
      get('/api/traslados/?estado=DESPACHADA'),
    ]);
    const pendientes = [...(r1.solicitudes || []), ...(r2.solicitudes || [])];

    const badgeEl = document.getElementById('badge-recibir');
    if (badgeEl) {
      badgeEl.style.display = pendientes.length ? 'inline' : 'none';
      badgeEl.textContent = pendientes.length;
    }

    if (!pendientes.length) {
      el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Nada pendiente de recibir</div>';
      return;
    }
    el.innerHTML = pendientes.map(s => `
    <div style="background:#0a1a0a;border:1px solid #166534;border-radius:12px;padding:14px;margin-bottom:10px;">
      <div style="font-size:14px;font-weight:700;margin-bottom:4px;">${s.codigo}</div>
      <div style="font-size:12px;color:#4ade80;margin-bottom:8px;">📦 ${s.total_items} ítem${s.total_items !== 1?'s':''}</div>
      ${(s.items || []).map(i => `
        <div style="font-size:11px;color:#aaa;padding:2px 0;">${i.producto_codigo} · ${i.cantidad_aprobada || i.cantidad_solicitada} und</div>
      `).join('')}
      <button onclick="tiendaRecibirSolicitud(${s.id})"
        style="width:100%;padding:13px;margin-top:12px;background:#4ade80;color:#000;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;">
        ✓ Confirmar recepción
      </button>
    </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:20px;color:#ef4444;">Error</div>';
  }
}

async function tiendaRecibirSolicitud(id) {
  if (!confirm('¿Confirmar que recibiste todos los productos del almacén?')) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/recibir`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (r.ok) {
      alerta('Recepción confirmada — stock actualizado en Siesa', 'exito');
      tiendaCargarRecibir();
    } else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// _condConfirmarEntrega eliminado — reemplazado por el flujo por parada
