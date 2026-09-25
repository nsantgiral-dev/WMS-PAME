'use strict';

// ── Tema (dark/light) — aplica antes de cualquier render ─────────────────────
(function () {
  if (localStorage.getItem('wms_theme') === 'light') {
    document.body.classList.add('light');
  }
})();

/**
 * Cambia el logo según el tema.
 *
 * `logo-white.png` es blanco: sobre fondo claro **desaparece**. El HTML lo
 * trae fijo y esta función lo sustituye al cargar y al alternar tema.
 *
 * `.emp-header-logo` faltaba en el selector, así que las pantallas de
 * empacador y picker se quedaban con el blanco y en tema claro mostraban un
 * hueco. Un selector que enumera sitios a mano es exactamente la forma que
 * deja fuera al que se agregue después — por eso ahora se buscan todas las
 * imágenes que apunten a cualquiera de las dos variantes.
 *
 * @param {boolean} isLight - Whether light theme is active.
 */
function _actualizarLogo(isLight) {
  const src = isLight ? '/static/pwa/logo-h.png' : '/static/pwa/logo-white.png';
  document.querySelectorAll('img[src*="logo-white.png"], img[src*="logo-h.png"]')
    .forEach(img => { img.src = src; });
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
let _QUAGGA_BTN  = null;    // botón "Escanear con cámara" que abrió la cámara activa
let _SCAN_LAST_TS = 0;      // debounce: ms del último scan registrado
// Ventana mínima entre dos escaneos aceptados durante lectura continua con
// cámara (picking, recepción, devoluciones, traslados de tienda — cualquier
// pantalla que deje la cámara abierta entre unidades, ver abrirCamara()).
// 900ms alcanzaba para leer el mismo código detectado en frames sucesivos,
// pero no le daba al operario tiempo real de RETIRAR la unidad ya contada y
// poner la siguiente en foco — el código anterior, todavía en cuadro,
// se volvía a aceptar como si fuera la unidad nueva. 1600ms es el punto
// donde un swap manual de unidad ya alcanza a completarse.
//
// _SCAN_DEBOUNCE_MS ahora es el TECHO de seguridad, no el mecanismo
// principal — ver _onQuaggaProcessed(). El lector físico (Bluetooth/USB,
// ver SCANNER_BUFFER más abajo) nunca necesitó este ajuste: dispara un
// código por gatillazo, evento discreto. La cámara analiza frames de video
// sin parar mientras el código siga en cuadro, así que "cuánto esperar" es
// la pregunta equivocada — lo que hace falta saber es "¿ya se fue el
// código de encuadre?". Eso es lo que _onQuaggaProcessed() rastrea vía
// Quagga.onProcessed (se dispara en cada frame, haya o no detección),
// independiente de _onQuaggaDetect (que solo se dispara cuando SÍ hay
// una decodificación). Con el código todavía en cuadro, _SCAN_ARMADO
// se queda en false sin importar cuánto tiempo pase — el techo de
// _SCAN_DEBOUNCE_MS solo existe por si el navegador no soporta
// onProcessed o dejara de disparar, para no bloquear el escaneo para
// siempre.
let _SCAN_ARMADO = true;
let _SCAN_SIN_DETECCION_DESDE = null;
const _SCAN_REARME_GAP_MS = 350;
const _SCAN_DEBOUNCE_MS = 1600;
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
let PEDIDOS_TAB_ACTIVO = 0;    // sub-tab activo en tab-pedidos (0=Por despachar..3=Error Siesa, 4=Cartera)
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
  div.innerHTML = `<div style="position:fixed;bottom:0;left:0;right:0;z-index:99999;background:#1e3a5f;color:var(--tx);padding:14px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:var(--fs-md);font-weight:600;box-shadow:0 -2px 16px rgba(0,0,0,0.5);">
    <span>Nueva version disponible</span>
    <button id="sw-update-btn" style="background:#2563eb;color:#fff;border:none;border-radius:8px;padding:9px 20px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;white-space:nowrap;">Actualizar ahora</button>
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

// Pestañas del panel admin que un `supervisor` no puede usar en el backend
// (siempre 403), así que tampoco se muestran. Ver el bloque `esSupervisor`
// dentro de `mostrarSegunRol()`.
// 'tab-compras' agregado 2026-09-07: compras.py exige Roles.COMPRAS_ROLES
// (admin/jefe_almacen/gerente/compras) en cada endpoint — supervisor nunca
// estuvo en ese grupo y la pestaña quedaba viva mostrando error.
// 'tab-dashboard' agregado 2026-09-14: el supervisor ahora aterriza en
// Pedidos (ver bloque esSupervisor) — el Dashboard es visión gerencial
// general, no la pantalla de trabajo diaria de quien también apoya picking.
const _TABS_OCULTAS_SUPERVISOR = ['tab-usuarios', 'tab-muelle', 'tab-liquidacion', 'tab-compras', 'tab-dashboard'];

// 📈 Analítica: la ven los mismos roles que `_es_gestion` deja pasar en sus
// endpoints (`Roles.GESTION`). Una pestaña que el servidor le niega con 403 a
// quien la ve enseña a ignorar errores. `test_analitica_recorrido.py` cruza esta
// lista contra `Roles.GESTION`.
const _ROLES_ANALITICA = ['admin', 'supervisor', 'jefe_almacen', 'gerente'];

/**
 * Roles que entran al shell de admin pero SOLO ven sus pestañas; la primera es
 * donde aterrizan. No es cosmético: dejarles a la vista pestañas que el
 * backend les va a negar con 403 enseña a ignorar errores.
 *
 * - `control_flota`: el procedimiento dice que ve el tablero y no aprueba.
 * - `liquidador` (2026-09-25): liquida rutas, registra cobros, reintenta los
 *   envíos de la liquidación (`permisos_liquidacion`). No es admin: no toca
 *   usuarios, maestros, inventario ni Siesa en general.
 * - `lider_cartera` (2026-09-25): los retenidos por cartera y, en Liquidación,
 *   confirmar retenciones, corregir cobros y autorizar crédito.
 *
 * `test_permisos_por_pantalla` exige que cada GET de esas pantallas le
 * conteste a ese rol, y `test_roles_plata` corre esta función de verdad.
 */
const _TABS_DE_ROL = {
  control_flota: ['tab-flota'],
  liquidador:    ['tab-liquidacion'],
  lider_cartera: ['tab-cartera', 'tab-liquidacion'],
};

// ⛔ Cartera: pestaña propia del líder de cartera. Los demás ven el mismo
// bloque en «Operación hoy» (cartera.js pinta los dos contenedores).
const _TABS_SOLO_DE_ROL = ['tab-cartera'];

/**
 * Route user to the correct screen and start timers based on their role.
 * @param {string} rol - User role (admin, operario, recepcionista, conductor, tienda, compras, etc.).
 */
function mostrarSegunRol(rol) {
  pararTimers();
  const tabsDeRol = _TABS_DE_ROL[rol] || null;
  const esAdmin = ['admin','gerente','jefe_almacen','supervisor'].includes(rol) || !!tabsDeRol;
  const esSupervisor = rol === 'supervisor';
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
    // Reset SIEMPRE antes de aplicar el ocultamiento de este rol: login()
    // llama mostrarSegunRol() sin recargar la página, así que el DOM puede
    // traer pestañas escondidas por la sesión anterior (supervisor,
    // control_flota) — sin este reset, un admin que entra justo después de
    // un supervisor hereda sus pestañas ocultas hasta que alguien recarga.
    document.querySelectorAll('.nav-tab').forEach(el => {
      const oc = el.getAttribute('onclick') || '';
      el.style.display = _TABS_SOLO_DE_ROL.some(t => oc.includes(`'${t}'`)) ? 'none' : '';
    });
    if (!_ROLES_ANALITICA.includes(rol)) {
      document.querySelectorAll('.nav-tab[onclick*="tab-analitica"]').forEach(el => { el.style.display = 'none'; });
    }
    const btnModoOp = document.getElementById('nav-modo-operario-supervisor');
    if (btnModoOp) btnModoOp.style.display = 'none';
    if (tabsDeRol) {
      pantalla('pantalla-admin');
      if (OPERARIO) actualizarUI(OPERARIO);
      document.querySelectorAll('.nav-tab').forEach(el => {
        const oc = el.getAttribute('onclick') || '';
        el.style.display = tabsDeRol.some(t => oc.includes(`'${t}'`)) ? '' : 'none';
      });
      tab(tabsDeRol[0]);
      // Flota carga al entrar y no se refresca sola; las de plata sí, como
      // para el admin (cargarAdmin decide qué refresca cada pestaña).
      if (rol !== 'control_flota') TIMER_ADMIN = setInterval(() => cargarAdmin(true), 30000);
      return;
    }
    pantalla('pantalla-admin');
    if (OPERARIO) actualizarUI(OPERARIO);
    if (esSupervisor) {
      // Cosmético: el backend ya bloquea estas acciones con 403 para
      // supervisor (Usuarios exige admin puro en auth.py; Muelle exige
      // admin/jefe_almacen en requisiciones.py; Liquidación exige admin
      // puro en rutas.py). Esto solo evita que la pestaña quede ahí sin
      // servir para nada — si algún día cambia el guard del backend y
      // nadie actualiza esta lista, la pestaña queda mal escondida o mal
      // mostrada sin que nada avise.
      _TABS_OCULTAS_SUPERVISOR.forEach(id => {
        document.querySelectorAll(`.nav-tab[onclick*="${id}"]`).forEach(el => {
          el.style.display = 'none';
        });
      });
      // Modo Operario (2026-09-14) — exclusivo de NB1: el backend
      // (get_tarea_actual) corta en seco a cualquier supervisor de otra
      // bodega, así que el ítem del menú ni se muestra ahí — evita un
      // enlace que lleva a una pantalla vacía por diseño.
      if (btnModoOp && OPERARIO?.almacen_bodega_siesa_id === 'NB1') {
        btnModoOp.style.display = 'block';
      }
      // Dashboard queda oculto para este rol (arriba, _TABS_OCULTAS_SUPERVISOR)
      // — aterriza en Pedidos, su pantalla de trabajo real. tab() ya llama
      // cargarAdmin() una vez; el timer de abajo sigue haciendo falta para
      // el refresco periódico que el otro branch arma después del if.
      tab('tab-pedidos');
      TIMER_ADMIN = setInterval(() => cargarAdmin(true), 30000);
      return;
    }
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
    // Rol dual: picker/empacador + abastecedor → picker por defecto; Reposición
    // entra sola como nivel 2 de la cola unificada (pedirTarea), sin botón de modo.
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
  clearInterval(_SYNC_TIMER);
  clearInterval(TIMER_ADMIN);
  clearInterval(TIMER_OPERARIO);
  clearInterval(TIMER_REC);
  if (typeof ABAST_TIMER !== 'undefined') clearInterval(ABAST_TIMER);
  if (typeof invSalir === 'function') invSalir();
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

/**
 * Send queued offline actions to the server. Solo se quitan de la cola los ítems
 * que el servidor confirmó — uno que falle (ej. rechazado por regla de negocio)
 * se queda encolado para el próximo intento en vez de perderse junto con los que
 * sí sincronizaron.
 */
async function syncOffline() {
  if (!COLA_OFFLINE.length) return;
  try {
    const r = await post('/api/mobile/sync', { cola: COLA_OFFLINE });
    const resultados = r.resultados || [];
    // Sale de la cola lo que llegó Y lo que el servidor rechazó para siempre
    // (`definitivo`): reenviarlo no lo arregla, y dejarlo trababa la pantalla
    // (el empacador frente a «Reintentando…» sin fin). Lo que falló por algo
    // pasajero (Siesa caído, red) se queda y se reintenta.
    const qidsFuera = new Set(resultados.filter(x => x.exito || x.definitivo).map(x => x._qid));
    COLA_OFFLINE = COLA_OFFLINE.filter(item => !qidsFuera.has(item._qid));
    localStorage.setItem('wms_cola_offline', JSON.stringify(COLA_OFFLINE));
    if (r.sincronizados > 0) alerta('✓ ' + r.sincronizados + ' tarea(s) sincronizadas', 'exito');
    // Avisar al módulo dueño de cada acción puntual (ej. packing.js espera a
    // cerrar_packing para imprimir la etiqueta e quitar el bloqueo de pantalla).
    resultados.filter(x => x.exito && x.accion).forEach(x => {
      const cb = window['onSync_' + x.accion];
      if (typeof cb === 'function') cb(x.resultado);
    });
    resultados.filter(x => !x.exito).forEach(x => {
      const cb = window[(x.definitivo ? 'onSyncRechazo_' : 'onSyncPendiente_') + (x.accion || '')];
      if (typeof cb === 'function') cb(x);
      else if (x.definitivo) alerta('No se pudo registrar lo guardado sin señal: ' + (x.error || 'rechazado'), 'error');
    });
  } catch (e) {}
}

/** @param {Object} datos - Action payload to enqueue for later sync. @returns {Object} el ítem encolado (incluye `_qid`) */
function guardarOffline(datos) {
  const item = { ...datos, ts: Date.now(), _qid: `${Date.now()}_${Math.random().toString(36).slice(2, 8)}` };
  COLA_OFFLINE.push(item);
  localStorage.setItem('wms_cola_offline', JSON.stringify(COLA_OFFLINE));
  alerta('Sin WiFi — guardado para sincronizar', 'advertencia');
  return item;
}

/**
 * POST con reintento automático ante corte de red real (no ante error del servidor).
 * Pensado para escaneos individuales: el backend no tiene forma de "encolarlos" como
 * hace `/api/mobile/sync` con una confirmación completa, así que la única resiliencia
 * posible aquí es reintentar antes de que el operario pierda el escaneo.
 * @param {string} url
 * @param {Object} payload
 * @param {number} [intentos=2] - reintentos adicionales tras el primer intento fallido
 * @param {number} [esperaMs=600] - pausa entre reintentos
 */
/** Id de escaneo para deduplicar reintentos server-side — no necesita ser criptográficamente fuerte. */
function generarScanId() {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  return 'scan-' + Date.now() + '-' + Math.random().toString(36).slice(2, 10);
}

async function postConReintento(url, payload, intentos = 2, esperaMs = 600) {
  for (let i = 0; ; i++) {
    try {
      return await post(url, payload);
    } catch (e) {
      if (e.status || i >= intentos) throw e; // error del servidor, o sin reintentos restantes
      await new Promise(res => setTimeout(res, esperaMs));
    }
  }
}

/**
 * Modal propio para capturar una cantidad numérica — reemplaza `prompt()`.
 * Teclado numérico garantizado (`inputmode="numeric"`), valida el rango
 * antes de dejar confirmar (no substituye silenciosamente un valor inválido
 * como hacía `parseInt(x) || default`), y con un solo paso en vez de
 * encadenar `prompt()` + `confirm()`.
 * @param {string} titulo
 * @param {string} mensajeHtml - se inserta tal cual, permite HTML simple
 * @param {Object} [opts]
 * @param {number} [opts.min=1]
 * @param {number} [opts.max]
 * @param {number|string} [opts.valorInicial='']
 * @param {string} [opts.textoConfirmar='Confirmar']
 * @param {string} [opts.textoCancelar='Cancelar']
 * @returns {Promise<number|null>} la cantidad, o null si se canceló
 */
function _modalCantidad(titulo, mensajeHtml, opts = {}) {
  const {
    min = 1, max, valorInicial = '',
    textoConfirmar = 'Confirmar', textoCancelar = 'Cancelar',
  } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:360px;border:1px solid var(--brd);">
        <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);margin-bottom:10px;">${titulo}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:16px;line-height:1.5;">${mensajeHtml}</div>
        <input id="_mc-input" type="number" inputmode="numeric"
          ${min != null ? `min="${min}"` : ''} ${max != null ? `max="${max}"` : ''} value="${valorInicial}"
          style="width:100%;padding:14px;font-size:var(--fs-xl);font-weight:700;background:var(--bg-s);border:2px solid var(--brd);border-radius:10px;color:var(--tx);text-align:center;margin-bottom:6px;box-sizing:border-box;">
        <div id="_mc-error" style="font-size:var(--fs-xs);color:var(--err-tx);min-height:16px;margin-bottom:10px;"></div>
        <div style="display:flex;gap:10px;">
          <button id="_mc-no" style="flex:1;padding:14px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoCancelar}</button>
          <button id="_mc-si" style="flex:1;padding:14px;background:var(--pm-fill);color:#fff;border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoConfirmar}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#_mc-input');
    const errEl = overlay.querySelector('#_mc-error');
    const cerrar = valor => { overlay.remove(); resolve(valor); };
    const intentarConfirmar = () => {
      const val = parseInt(input.value, 10);
      if (isNaN(val) || val < min || (max != null && val > max)) {
        errEl.textContent = max != null ? `Debe ser un número entre ${min} y ${max}` : `Debe ser un número desde ${min}`;
        input.focus();
        return;
      }
      cerrar(val);
    };
    overlay.querySelector('#_mc-si').onclick = intentarConfirmar;
    overlay.querySelector('#_mc-no').onclick = () => cerrar(null);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') intentarConfirmar(); });
    input.focus();
    input.select();
  });
}

/**
 * Modal de confirmación compartido — reemplazo de `confirm()` nativo, que en
 * iOS/Android bloquea el hilo con un diálogo del sistema operativo (no
 * estilizable, texto largo se corta) en vez de la UI de la app.
 * @param {string} mensajeHtml
 * @param {{titulo?:string, textoConfirmar?:string, textoCancelar?:string, peligro?:boolean}} [opts]
 * @returns {Promise<boolean>}
 */
function _modalConfirmar(mensajeHtml, opts = {}) {
  const {
    titulo = '¿Confirmar?', textoConfirmar = 'Confirmar', textoCancelar = 'Cancelar',
    peligro = false,
  } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:400px;border:1px solid var(--brd);max-height:80vh;overflow-y:auto;">
        <div style="font-size:17px;font-weight:800;color:var(--tx);margin-bottom:10px;">${titulo}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx);margin-bottom:20px;line-height:1.5;white-space:pre-line;">${mensajeHtml}</div>
        <div style="display:flex;gap:10px;">
          <button id="_mconf-no" style="flex:1;padding:14px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoCancelar}</button>
          <button id="_mconf-si" style="flex:1;padding:14px;background:${peligro ? '#7f1d1d' : 'var(--pm-fill)'};color:#fff;border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoConfirmar}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const cerrar = valor => { overlay.remove(); resolve(valor); };
    overlay.querySelector('#_mconf-si').onclick = () => cerrar(true);
    overlay.querySelector('#_mconf-no').onclick = () => cerrar(false);
  });
}

/**
 * Modal propio para capturar texto libre — reemplaza `prompt()` para motivos,
 * observaciones, etc. Mismo patrón que `_modalCantidad`/`_modalConfirmar`.
 * @param {string} titulo
 * @param {string} mensajeHtml
 * @param {{obligatorio?:boolean, valorInicial?:string, placeholder?:string, textoConfirmar?:string, textoCancelar?:string}} [opts]
 * @returns {Promise<string|null>} el texto, o null si se canceló
 */
function _modalTexto(titulo, mensajeHtml, opts = {}) {
  const {
    obligatorio = true, valorInicial = '', placeholder = '',
    textoConfirmar = 'Confirmar', textoCancelar = 'Cancelar',
  } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:400px;border:1px solid var(--brd);">
        <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);margin-bottom:10px;">${titulo}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:16px;line-height:1.5;">${mensajeHtml}</div>
        <textarea id="_mt-input" placeholder="${placeholder}" rows="3"
          style="width:100%;padding:12px;font-size:var(--fs-md);background:var(--bg-s);border:2px solid var(--brd);border-radius:10px;color:var(--tx);margin-bottom:6px;box-sizing:border-box;font-family:inherit;resize:vertical;">${valorInicial}</textarea>
        <div id="_mt-error" style="font-size:var(--fs-xs);color:var(--err-tx);min-height:16px;margin-bottom:10px;"></div>
        <div style="display:flex;gap:10px;">
          <button id="_mt-no" style="flex:1;padding:14px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoCancelar}</button>
          <button id="_mt-si" style="flex:1;padding:14px;background:var(--pm-fill);color:#fff;border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoConfirmar}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#_mt-input');
    const errEl = overlay.querySelector('#_mt-error');
    const cerrar = valor => { overlay.remove(); resolve(valor); };
    const intentarConfirmar = () => {
      const val = input.value.trim();
      if (obligatorio && !val) {
        errEl.textContent = 'Este campo es obligatorio';
        input.focus();
        return;
      }
      cerrar(val);
    };
    overlay.querySelector('#_mt-si').onclick = intentarConfirmar;
    overlay.querySelector('#_mt-no').onclick = () => cerrar(null);
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); intentarConfirmar(); } });
    input.focus();
  });
}

/**
 * Resuelve qué representa un código escaneado contra `/api/empaques/scan/` —
 * compartido entre picking (`_procesarScanPicking`) y packing
 * (`empProcesarEscaneo`), que hasta ahora reimplementaban la misma
 * interpretación GS1/EAN/LPN por separado: un fix en un lado no llegaba
 * al otro (ver CLAUDE.md sobre `identidadConductor`, mismo patrón de riesgo).
 *
 * `EAN_BASE` siempre trae `factor:1` desde el backend (scan_barcode() en
 * empaques_service.py solo lo devuelve cuando `factor_conversion == 1`), así
 * que tratarlo igual que GS1_UNICO no cambia ningún resultado — solo unifica
 * el camino.
 *
 * @param {string} codigo
 * @returns {Promise<{tipo:string, codigoParaBackend:string, cantidad:number,
 *   lpnCodigo:string|null, unidad:string|null, ambiguos:Array|null}>}
 */
async function resolverEscaneoEmpaque(codigo) {
  let scan;
  try {
    scan = await get(`/api/empaques/scan/${encodeURIComponent(codigo)}`);
  } catch (_) {
    scan = { tipo: 'NO_ENCONTRADO' };
  }
  const tipo = scan.tipo || 'NO_ENCONTRADO';

  if (tipo === 'GS1_AMBIGUO') {
    return { tipo, ambiguos: scan.ambiguos || [] };
  }

  let codigoParaBackend = codigo;  // default: código de producto base (EAN-13 en productos.codigo_barras)
  let cantidad = 1;
  let unidad = null;
  let lpnCodigo = null;

  if ((tipo === 'GS1_UNICO' || tipo === 'EAN_BASE') && scan.producto?.codigo) {
    codigoParaBackend = scan.producto.codigo;
    cantidad = scan.factor || 1;
    unidad = scan.empaque?.unidad_medida || null;
  } else if (tipo === 'LPN' && scan.producto?.codigo) {
    codigoParaBackend = scan.producto.codigo;
    cantidad = scan.factor || 1;  // scan.factor = lpn.cantidad_actual
    lpnCodigo = codigo;           // 'LPN-XXXXXXX' original
    unidad = scan.empaque?.unidad_medida || null;
  }
  // NO_ENCONTRADO → enviar código original, el backend da error descriptivo

  return { tipo, codigoParaBackend, cantidad, lpnCodigo, unidad, ambiguos: null };
}

// e.key depende del layout de teclado ACTIVO (SO + firmware del lector).
// Un lector configurado para US emulando sobre un Windows en Español
// Latinoamérica transmite el guion como apóstrofe — confirmado en vivo
// (2026-09-14): "BN-10" llegaba como "BN'10", incluso después de
// reprogramar el lector con el código de barras "Spanish Keyboard" del
// manual (esa vía de hardware quedó agotada, seguía fallando igual).
// e.code identifica la TECLA FÍSICA, no el carácter que el layout le
// asigna — es inmune a cualquier desacople lector/SO. Solo hace falta
// mapear los símbolos que de verdad difieren entre layouts; letras y
// dígitos ya llegan bien vía e.key en todos los layouts latinos probados.
const SCANNER_CODE_A_CHAR = {
  Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']',
  Backslash: '\\', Semicolon: ';', Quote: "'", Backquote: '`',
  Comma: ',', Period: '.', Slash: '/',
};

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
      const haySeleccion = (window.getSelection() || '').toString().length > 0;
      if (!CAMARA_ACTIVA && !esForm && !hayModal && !haySeleccion) inp.focus();
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
    } else {
      const ch = SCANNER_CODE_A_CHAR[e.code] || (e.key && e.key.length === 1 ? e.key : null);
      if (ch) {
        SCANNER_BUFFER += ch;
        clearTimeout(SCANNER_TIMER);
        SCANNER_TIMER = setTimeout(() => { SCANNER_BUFFER = ''; }, 150);
      }
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

/**
 * Deshabilita el botón que disparó el evento mientras `fn()` corre — evita
 * doble-tap en wifi/datos inestables (latencias de 2-5s son comunes en
 * bodega): sin esto, un toque sin reacción visible invita a tocar de nuevo,
 * disparando la misma acción dos veces. El backend suele validar estado y
 * evitar corrupción de datos, pero el operario igual ve una petición extra,
 * un toast confuso, o corre contra ese mismo guard del servidor.
 * @param {Event} event - el evento del onclick (pasar literalmente `event` en el call-site)
 * @param {Function} fn - función (async o no) a ejecutar
 * @param {string} [textoOcupado] - texto a mostrar en el botón mientras corre
 */
async function conBotonOcupado(event, fn, textoOcupado) {
  const btn = event?.currentTarget || event?.target;
  if (!btn) { await fn(); return; }
  const origTexto = btn.textContent;
  const origDisabled = btn.disabled;
  btn.disabled = true;
  if (textoOcupado) btn.textContent = textoOcupado;
  try {
    await fn();
  } finally {
    btn.disabled = origDisabled;
    if (textoOcupado) btn.textContent = origTexto;
  }
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
  const ctrl = new AbortController();
  // 25s, no 15s como get(): varias rutas detrás de post()/put() disparan Siesa
  // (packing, liquidación) y /api/mobile/confirmar ya devuelve 503+retry_after
  // cuando Siesa está genuinamente lenta — este timeout es el respaldo para el
  // caso en que ni siquiera esa respuesta rápida llega.
  const timer = setTimeout(() => ctrl.abort(), 25000);
  try {
    const r = await fetch(API + url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(body),
      signal: ctrl.signal
    });
    return _checkResp(r);
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('Tiempo de espera agotado — intenta de nuevo');
    throw e;
  } finally { clearTimeout(timer); }
}

/**
 * Authenticated PUT request with JSON body.
 * @param {string} url - API path.
 * @param {Object} [body={}] - Request payload.
 * @returns {Promise<Object>} Parsed JSON response.
 */
async function put(url, body = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 25000);
  try {
    const r = await fetch(API + url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(body),
      signal: ctrl.signal
    });
    return _checkResp(r);
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('Tiempo de espera agotado — intenta de nuevo');
    throw e;
  } finally { clearTimeout(timer); }
}

/**
 * fetch() autenticado con timeout — para los pocos casos que get()/post()/put()
 * no cubren: cuerpo de respuesta que no es JSON (blob de un CSV) o un método
 * que esos tres no soportan (PATCH). Devuelve la `Response` cruda sin
 * parsear — a propósito, porque `_checkResp()` asume JSON y estos casos no
 * lo son. Mismo timeout/abort que `get()`/`post()`/`put()`, para no
 * reinventar esa parte cada vez (ver hallazgo: `fetch()` crudo bypassa los
 * helpers centralizados — no repetir el mismo patrón con otro nombre).
 * @param {string} url - Ruta de la API (relativa al origen).
 * @param {RequestInit} [options] - Igual que `fetch()` (method, headers, body...).
 * @param {number} [timeoutMs=25000]
 * @returns {Promise<Response>}
 */
async function _fetchConTimeout(url, options = {}, timeoutMs = 25000) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(API + url, {
      ...options,
      headers: { Authorization: 'Bearer ' + TOKEN, ...(options.headers || {}) },
      signal: ctrl.signal,
    });
  } catch (e) {
    if (e.name === 'AbortError') throw new Error('Tiempo de espera agotado — intenta de nuevo');
    throw e;
  } finally { clearTimeout(timer); }
}

/**
 * Sube un archivo (multipart/form-data) reportando progreso real de subida.
 * Único helper de transporte que usa `XMLHttpRequest` en vez de `fetch()`:
 * es el único de los dos que expone `upload.onprogress` — `fetch()` no lo
 * tiene. El resto de la lógica (headers, timeout, parseo de error) es la
 * misma política que `post()`/`put()`, para que un cambio ahí no diverja del
 * resto de los helpers (Regla 0 corolario: una política, una función).
 * @param {string} url - Ruta de la API (relativa al origen).
 * @param {FormData} formData - Archivo(s) a subir.
 * @param {(pct:number)=>void} [onProgress] - Callback con el % de subida (0-100).
 * @param {number} [timeoutMs=90000] - Timeout total, más largo que post()/put()
 *   porque el archivo puede tardar en subir Y el servidor puede procesarlo
 *   síncrono después de recibirlo.
 * @returns {Promise<Object>} Cuerpo JSON de la respuesta.
 */
function subirArchivoConProgreso(url, formData, onProgress, timeoutMs = 90000) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', API + url);
    xhr.setRequestHeader('Authorization', 'Bearer ' + TOKEN);
    xhr.timeout = timeoutMs;
    if (onProgress) {
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable) onProgress(Math.round((ev.loaded / ev.total) * 100));
      };
    }
    xhr.onload = () => {
      let body;
      try { body = JSON.parse(xhr.responseText); } catch (_) { body = {}; }
      if (xhr.status === 401) { salir(true); reject(new Error('401')); return; }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else reject(new Error(body.error || `Error ${xhr.status}`));
    };
    xhr.ontimeout = () => reject(new Error('Tiempo de espera agotado — intenta de nuevo'));
    xhr.onerror = () => reject(new Error('Error de conexión'));
    xhr.send(formData);
  });
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
  // Botón "🧭 Layout" en las pantallas de picking/packing — solo para quien
  // tiene el flag puede_organizar_layout (admin/jefe ya entra por su propio
  // panel, no necesita este atajo).
  const _mostrarBtnLayout = !!op.puede_organizar_layout;
  ['btn-layout-operario', 'btn-layout-empacador'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = _mostrarBtnLayout ? 'inline-flex' : 'none';
  });
}

// Rol (operario/empacador) desde el que se entró a Layout vía el botón de
// arriba — layoutVolverDesdeOperario() lo usa para saber a qué pantalla
// devolver. null cuando no se entró por ese atajo (ej. admin/jefe normal).
let _LAYOUT_ROL_ORIGEN = null;

/** Operario/empacador con puede_organizar_layout entra a Layout sin dejar de ser quien es. */
function layoutAbrirDesdeOperario() {
  _LAYOUT_ROL_ORIGEN = OPERARIO?.rol || 'operario';
  pararTimers();
  pantalla('pantalla-admin');
  actualizarUI(OPERARIO);
  document.querySelectorAll('.nav-tab').forEach(el => {
    if (!(el.getAttribute('onclick') || '').includes('tab-layout')) el.style.display = 'none';
  });
  const btnVolver = document.getElementById('btn-volver-layout-operario');
  if (btnVolver) btnVolver.style.display = 'inline-flex';
  tab('tab-layout');
}

/** Vuelve a la pantalla de picking o packing de la que se entró a Layout. */
function layoutVolverDesdeOperario() {
  document.querySelectorAll('.nav-tab').forEach(el => { el.style.display = ''; });
  const btnVolver = document.getElementById('btn-volver-layout-operario');
  if (btnVolver) btnVolver.style.display = 'none';
  const rolOrigen = _LAYOUT_ROL_ORIGEN;
  _LAYOUT_ROL_ORIGEN = null;
  if (rolOrigen === 'empacador' || rolOrigen === 'packer_traslado') {
    pantalla('pantalla-empacador');
    actualizarUI(OPERARIO);
    empCargarTareas();
    TIMER_OPERARIO = setInterval(empCargarTareas, 20000);
  } else {
    pantalla('pantalla-operario');
    actualizarUI(OPERARIO);
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  }
}

/**
 * Modo Operario del supervisor (2026-09-14) — apoya picking de
 * pedidos/traslados y reposición en NB1, sin salir de su misma sesión.
 * Nunca conteo cíclico: get_tarea_actual ya lo excluye server-side (sería
 * juez y parte, siendo quien resuelve el Conteo Definitivo). El botón que
 * llama a esta función solo se muestra si almacen_bodega_siesa_id es NB1
 * (mostrarSegunRol) — igual, el backend lo corta en seco si no lo es.
 */
function supervisorEntrarModoOperario() {
  pararTimers();
  pantalla('pantalla-operario');
  actualizarUI(OPERARIO);
  const btnVolver = document.getElementById('btn-volver-admin-supervisor');
  if (btnVolver) btnVolver.style.display = 'inline-flex';
  pedirTarea();
  TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
}

/** Vuelve del Modo Operario al panel admin del supervisor. */
function supervisorVolverAdmin() {
  const btnVolver = document.getElementById('btn-volver-admin-supervisor');
  if (btnVolver) btnVolver.style.display = 'none';
  mostrarSegunRol(OPERARIO.rol);
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
  // Badge de "🎯 Definitivo" (Inventario) — se refresca en cada tick sin
  // importar la pestaña activa, igual que kpi-definitivos en el Dashboard,
  // para que un supervisor lo vea llegar aunque esté parado en otra pantalla.
  actualizarBadgeDefinitivos();
  if (TAB === 'tab-dashboard') await cargarDashboard();
  // Analítica es de consulta: carga al entrar, nunca por el timer de 30 s
  // (repintar mientras alguien lee una cifra se la cambia debajo del dedo).
  else if (TAB === 'tab-analitica') { if (!desdeTimer) await cargarAnalitica(); }
  else if (TAB === 'tab-pedidos') await cargarPedidos();
  else if (TAB === 'tab-requisiciones') await cargarRequisiciones();
  else if (TAB === 'tab-bodega') await cargarTareasBodega();
  else if (TAB === 'tab-operarios') await cargarOperarios();
  else if (TAB === 'tab-usuarios') await cargarUsuarios();
  else if (TAB === 'tab-stock') await cargarStock();
  else if (TAB === 'tab-connekta') { await cargarConnekta(); await siesaRecuperacionCargar(); await syncEstadosCargar(); await mapeoUnidadesCargar(); await cargarAuditoriaFlujo(); }
  else if (TAB === 'tab-muelle') await cargarMuelle();
  else if (TAB === 'tab-rutas') await cargarRutas();
  // Inventario decide solo qué refresca el tick: solo la sub-pestaña visible y
  // de datos vivos, sin vaciarla (conteo.js, «Refresco de las pestañas»).
  else if (TAB === 'tab-inventario') await cargarInventario(desdeTimer);
  else if (TAB === 'tab-traslados') await cargarTrasladosAdmin();
  else if (TAB === 'tab-reposicion') await cargarReposicion();
  else if (TAB === 'tab-liquidacion') await cargarLiquidacion();
  else if (TAB === 'tab-cartera') { if (typeof carteraCargarBloque === 'function') await carteraCargarBloque(); }
  // Layout es un módulo de configuración, no de datos en vivo — no se autorefresca
  // cada 30s (rompía el scroll y cualquier modal abierto mientras se revisaba).
  // Solo carga al entrar manualmente a la pestaña.
  else if (TAB === 'tab-layout') { if (!desdeTimer) await cargarLayout(); }
  // Compras es de consulta y decisión: el timer de 30 s repintaba la pantalla
  // entera (sub-pestaña, «¿por qué?» abiertos, filtros, lo tecleado en la
  // lista de temporada) y la devolvía a «Cargando…». Carga al entrar.
  else if (TAB === 'tab-compras') { if (!desdeTimer) await cargarCompras(); }
  else if (TAB === 'tab-vigia') { if (!desdeTimer) await cargarVigia(); }
  // `flotaEntrar` y no `cargarFlota`: despacha al sub-tab que el usuario dejó
  // abierto. Sin esto, cada F5 rebota a Expedientes — y `control_flota`, que va
  // a vivir en Analítica, vuelve a la pantalla equivocada todas las veces.
  else if (TAB === 'tab-flota') { if (!desdeTimer) await flotaEntrar(); }
  // Manual de Usuario: lista estática, no hay nada que refrescar cada 30s.
  else if (TAB === 'tab-manuales') { if (!desdeTimer) cargarManuales(); }
}

/** @param {string} id - Tab element ID to activate (e.g. 'tab-dashboard'). */
function tab(id) {
  const TABS = ['tab-dashboard','tab-analitica','tab-pedidos','tab-requisiciones','tab-traslados','tab-bodega','tab-operarios','tab-usuarios','tab-stock','tab-connekta','tab-muelle','tab-rutas','tab-inventario','tab-liquidacion','tab-cartera','tab-layout','tab-reposicion','tab-compras','tab-etiquetas','tab-vigia','tab-flota','tab-manuales'];
  TABS.forEach(t => {
    const el = document.getElementById(t);
    if (el) el.style.display = t === id ? 'block' : 'none';
  });
  document.querySelectorAll('.nav-tab').forEach((t, i) => {
    t.classList.toggle('active', TABS[i] === id);
  });
  // Lo que Inventario dejó corriendo (el sondeo del kardex) no sigue en otra pestaña.
  if (TAB === 'tab-inventario' && id !== 'tab-inventario' && typeof invSalir === 'function') invSalir();
  TAB = id;
  cargarAdmin();
}

/** Fetch and render the full admin dashboard (KPIs, chart, alerts, productivity). */
/**
 * La franja de ambiente. **Va primero y no depende del resto del dashboard.**
 *
 * El 19-ago-2026 el Gestor de Cartera pasó ocho horas escribiendo contra la
 * base equivocada sin que sonara nada, porque nadie había declarado nada y el
 * silencio se leyó como conformidad. Un endpoint que hay que abrir no avisa:
 * esto tiene que estar en la cara de quien entra.
 *
 * No intenta adivinar el ambiente —no se puede— sino mostrar si alguien
 * cuadró una cifra contra algo de afuera, con nombre y fecha.
 */
async function cargarFranjaAmbiente() {
  const cont = document.getElementById('franja-ambiente');
  if (!cont) return;
  let d;
  try {
    d = await get('/api/health/ambiente');
  } catch (e) {
    // El endpoint responde 409 cuando está en ALARMA, así que un error acá
    // **no se puede leer como «todo bien»**: se pinta la alarma igual.
    d = e && e.datos ? e.datos : null;
    if (!d) {
      cont.innerHTML = `<div style="padding:8px 12px;background:#7f1d1d;color:var(--tx);
        font-size:var(--fs-xs);font-weight:700;">AMBIENTE SIN VERIFICAR — no se pudo
        consultar el estado. El silencio no es «todo bien».</div>`;
      return;
    }
  }
  if (d.estado === 'DECLARADO') {
    const u = d.ultima_declaracion || {};
    cont.innerHTML = `<div style="padding:6px 12px;background:#064e3b;color:var(--ok-tx);
      font-size:var(--fs-xs);">Ambiente contrastado por <b>${esc(u.declarado_por_nombre || '—')}</b>
      el ${esc((u.declarado_en || '').slice(0, 10))} · ${esc(u.concepto || '')}
      (WMS ${esc(u.cifra_wms)} vs ${esc(u.fuente_externa)}: ${esc(u.cifra_externa)})</div>`;
    return;
  }
  cont.innerHTML = `<div style="padding:10px 12px;background:#7f1d1d;color:var(--tx);font-size:var(--fs-xs);">
    <b style="font-size:var(--fs-sm);">AMBIENTE SIN VERIFICAR</b><br>
    ${(d.motivos || []).map(m => `• ${m}`).join('<br>')}
    <div style="margin-top:6px;opacity:.85;font-size:var(--fs-xs);">
      El host y la compañía no distinguen producción de una copia: los dos son
      iguales en las dos. Hace falta que alguien cuadre una cifra contra una
      fuente externa al ERP.</div></div>`;
}

async function cargarDashboard() {
  // Fuera del try de abajo a propósito: si el dashboard falla, la franja
  // tiene que salir igual. Es la que avisa.
  cargarFranjaAmbiente();
  // Retenidos por cartera (cartera.js): independiente del resumen, se
  // esconde solo si el rol no lo ve.
  if (typeof carteraCargarBloque === 'function') carteraCargarBloque();
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

    // La edad de lo más viejo de cada cola: un número sin edad no distingue
    // la cola de hoy de la del ensayo (el servidor ya cuenta desde el corte).
    _dashEdad('kpi-pick-pend', k.picking);
    _dashEdad('kpi-tras-activos', tras);
    _dashEdad('kpi-rutas-activas', rutas);
    _dashEdad('kpi-conteos-desc', k.conteo.en_descuadre_edad);
    _dashEdad('kpi-definitivos', k.conteo.definitivos_edad);

    const nAud = d.auditorias_urgentes || 0;
    set('kpi-auditorias', nAud);
    const cardAud = document.getElementById('kpi-card-auditorias');
    if (cardAud) cardAud.style.borderColor = nAud > 0 ? '#7f1d1d' : '';

    const nDef = k.conteo.definitivos_pendientes || 0;
    set('kpi-definitivos', nDef);
    const cardDef = document.getElementById('kpi-card-definitivos');
    if (cardDef) cardDef.style.borderColor = nDef > 0 ? '#78350f' : '';

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
    // El semáforo de conteo lo decide el servidor (`semaforo_de_conteo`:
    // pendientes vivas contra el cupo diario). El respaldo es para un
    // servidor viejo que todavía no lo manda.
    const semC = k.conteo.semaforo;
    if (semC && _DASH_COLORES.includes(semC.color)) {
      _semaforo('sem-conteos', semC.color, semC.texto || '');
    } else {
      _semaforo('sem-conteos',
        (k.conteo.en_descuadre > 0 || nDef > 0) ? 'rojo' : (k.conteo.pendientes > 0 ? 'verde' : 'gris'),
        nDef > 0 ? nDef + ' definitivo(s) pendiente(s)'
          : (k.conteo.en_descuadre > 0 ? k.conteo.en_descuadre + ' con diferencia' : k.conteo.pendientes + ' pendientes'));
    }
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
        prodEl.innerHTML = '<div style="color:var(--tx3);font-size:var(--fs-xs);">Sin actividad en los últimos 7 días</div>';
      } else {
        prodEl.innerHTML = ops.slice(0, 5).map(o => {
          const pct = Math.min(100, Math.round(o.total_tareas / Math.max(...ops.map(x => x.total_tareas)) * 100));
          return `<div style="margin-bottom:8px;">
            <div style="display:flex;justify-content:space-between;margin-bottom:3px;">
              <span style="color:var(--tx2);font-size:var(--fs-xs);">${esc(o.nombre)}</span>
              <span style="color:var(--tx3);font-size:var(--fs-xs);">Pick:${esc(o.pickings_completados)} · Pack:${esc(o.packings_completados)} · Cont:${esc(o.conteos_completados)}</span>
            </div>
            <div style="background:var(--bg-input);border-radius:4px;height:5px;">
              <div style="background:#3b82f6;width:${pct}%;height:5px;border-radius:4px;"></div>
            </div>
          </div>`;
        }).join('');
      }
    }

    // ── Movimientos recientes ──────────────────────────────────────
    movimientos(d.movimientos_recientes.movimientos);

    // ── Alertas (solo si hay datos) ────────────────────────────────
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
          `<div style="padding:6px 0;border-bottom:1px solid var(--info-brd);display:flex;justify-content:space-between;">
            <span style="color:var(--tx);">${esc(t.codigo)} → ${esc(t.nombre_punto_venta || t.bodega_destino)}</span>
            <span style="color:${t.horas_en_transito > 24 ? 'var(--orange)' : 'var(--info-tx)'};font-weight:700;">${esc(t.horas_en_transito)}h</span>
          </div>`
        ).join('');
      }
    }

    // ── Tablero BI (métricas 1, 3, 5) — sección propia dentro de Dashboard,
    // con su propio try/catch interno por widget (cargarBI en tablero_bi.js);
    // si falla, no debe tumbar lo que ya se pintó arriba.
    if (typeof cargarBI === 'function') await cargarBI();
  } catch (e) { console.error('[Dashboard]', e); }
}

/**
 * Update a traffic-light status indicator.
 * @param {string} id - DOM element ID of the semaphore.
 * @param {string} color - Status color key (verde, amarillo, rojo, gris).
 * @param {string} texto - Label text to display.
 */
const _DASH_COLORES = ['rojo', 'amarillo', 'verde', 'gris'];

/** «lo más viejo: 3 h» / «lo más viejo: 4 días» — vacío si no hay nada en cola. */
function _dashEdadTexto(b) {
  if (!b || b.edad_horas == null) return '';
  const h = Number(b.edad_horas);
  return h < 48 ? `lo más viejo: ${Math.round(h)} h` : `lo más viejo: ${Math.round(h / 24)} días`;
}

/** Pone la edad de la cola bajo el valor de su tarjeta (crea la línea si no existe). */
function _dashEdad(idValor, bloque) {
  const v = document.getElementById(idValor);
  if (!v || !v.parentElement) return;
  let sub = v.parentElement.querySelector('.kpi-edad');
  if (!sub) {
    sub = document.createElement('div');
    sub.className = 'kpi-sub kpi-edad';
    v.parentElement.appendChild(sub);
  }
  sub.textContent = _dashEdadTexto(bloque);
}

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
  if (!lista || !lista.length) { el.innerHTML = '<div class="tabla-titulo">Últimos movimientos</div><div style="color:var(--tx3);font-size:var(--fs-sm);padding:8px 0;">Sin movimientos</div>'; return; }
  const TIPOS_ENTRADA = new Set(['ENTRADA', 'CARGA_INICIAL_SIESA', 'RECEPCION', 'AJUSTE_ENTRADA', 'DEVOLUCION']);
  el.innerHTML = '<div class="tabla-titulo">Últimos movimientos</div>' + lista.slice(0,8).map(m => {
    const esEntrada = TIPOS_ENTRADA.has(m.tipo);
    const c = esEntrada ? '#4ade80' : (m.cantidad > 0 ? '#f87171' : '#666');
    const s = esEntrada ? '+' : (m.cantidad > 0 ? '-' : '');
    const fechaStr = m.fecha && !m.fecha.endsWith('Z') ? m.fecha + 'Z' : m.fecha;
    const h = new Date(fechaStr).toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Bogota' });
    const doc = m.numero_documento ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(m.numero_documento)}</div>` : '';
    return `<div class="tabla-fila"><div><div class="tabla-nombre">${esc(m.tipo)}</div><div style="font-size:var(--fs-xs);color:var(--tx3);">${h}</div>${doc}</div><div style="color:${c};font-weight:700;">${s}${esc(m.cantidad)}</div></div>`;
  }).join('');
}

/** @param {number} id - Picking task ID to reopen back into the pool. */
async function reabrirTareaPicking(id) {
  if (!await _modalConfirmar('¿Reabrir esta tarea al pool de picking? El operario que llegue a esa ubicación la tomará de nuevo.', { titulo: 'Reabrir tarea' })) return;
  try {
    await put(`/api/picking/${id}/reabrir`);
    alerta('Tarea reabierta al pool ✓', 'exito');
    await cargarTareasBodega();
  } catch (e) { alerta(e.message || 'Error al reabrir', 'error'); }
}

/** @param {number} id - Picking task ID to cancel (prompts for reason). */
async function cancelarTareaPicking(id) {
  const motivo = await _modalTexto('Cancelar tarea', 'Motivo de cancelación (obligatorio):');
  if (!motivo) return;
  if (!await _modalConfirmar('¿Cancelar esta tarea de picking? El pedido del cliente quedará incompleto.', { titulo: 'Confirmar cancelación', peligro: true })) return;
  try {
    await put(`/api/picking/${id}/cancelar`, { motivo });
    alerta('Tarea cancelada', 'advertencia');
    await cargarTareasBodega();
  } catch (e) { alerta(e.message || 'Error al cancelar', 'error'); }
}

/** @param {number} id - Task ID whose inline audit form to show. */
function auditoriaMostrarPanel(id) {
  document.getElementById(`auditoria-panel-${id}`).style.display = 'block';
}

/** @param {number} id - Task ID whose inline audit form to hide. */
function auditoriaCancelarPanel(id) {
  document.getElementById(`auditoria-panel-${id}`).style.display = 'none';
}

/** Muestra el bloque de "conteo forzado" solo para el resultado que lo dispara
 * (ENCONTRADO) y carga los operarios una vez (helper de conteo.js). */
async function auditoriaResultadoCambio(id) {
  const resultado = document.getElementById(`auditoria-resultado-${id}`)?.value;
  const bloque = document.getElementById(`auditoria-conteo-${id}`);
  if (!bloque) return;
  bloque.style.display = resultado === 'ENCONTRADO' ? 'block' : 'none';
  const sel = document.getElementById(`auditoria-operario-${id}`);
  if (resultado === 'ENCONTRADO' && sel && sel.options.length <= 1
      && typeof _cargarOperariosConteo === 'function') {
    const operarios = await _cargarOperariosConteo();
    sel.innerHTML = '<option value="">Auto-asignar (el que lo tome primero)</option>' +
      operarios.map(u => `<option value="${esc(u.id)}">${esc(u.nombre || u.usuario)} (${esc(u.rol)})</option>`).join('');
  }
}

/** @param {number} id - Task ID to submit audit result for. */
async function auditoriaGuardar(id) {
  const resultado       = document.getElementById(`auditoria-resultado-${id}`)?.value;
  const cantidadHallada = parseInt(document.getElementById(`auditoria-cantidad-${id}`)?.value || '0', 10);
  const ubicacion       = document.getElementById(`auditoria-ubicacion-${id}`)?.value.trim();
  const observaciones   = document.getElementById(`auditoria-obs-${id}`)?.value.trim();

  if (!resultado) { alerta('Selecciona un resultado antes de guardar', 'error'); return; }

  try {
    const r = await post(`/api/picking/${id}/auditar`, {
      resultado,
      cantidad_hallada: cantidadHallada,
      ubicacion_hallada: ubicacion || null,
      observaciones: observaciones || null,
      forzar_conteo: document.getElementById(`auditoria-forzar-${id}`)?.checked !== false,
      conteo_operario_id: document.getElementById(`auditoria-operario-${id}`)?.value || null,
    });
    const c = r && r.conteo_forzado;
    if (c && c.ok) {
      alerta(`Auditoría registrada ✓ — conteo cíclico generado (${c.codigos[0]})`, 'exito');
    } else if (c && !c.ok) {
      alerta(`Auditoría registrada, pero NO se generó el conteo: ${c.error}`, 'advertencia');
    } else {
      alerta('Auditoría registrada ✓', 'exito');
    }
    await cargarTareasBodega();
  } catch (e) { alerta(e.message || 'Error al guardar auditoría', 'error'); }
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
    const _g = pedidoGrupo;
    const _num = p => parseInt(String(p.numero_pedido).replace(/\D/g, ''), 10) || 0;
    SIESA_PEDIDOS.sort((a, b) => _g(a) - _g(b) || _num(b) - _num(a));

    const tabsEl = document.getElementById('ped-tabs');

    if (siesa.simulado) {
      if (tabsEl) tabsEl.innerHTML = '';
      el.innerHTML = `<div style="background:var(--warn-bg);border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:var(--fs-xs);color:var(--warn-tx);border:1px solid var(--warn-brd);">⚡ Connekta en simulación — conecta credenciales para ver pedidos reales</div>`;
      return;
    }

    if (!SIESA_PEDIDOS.length) {
      if (tabsEl) tabsEl.innerHTML = '';
      el.innerHTML = `<div style="background:var(--ok-bg);border-radius:10px;padding:10px 12px;margin-bottom:12px;font-size:var(--fs-xs);color:var(--ok-tx);border:1px solid var(--ok-brd);">✓ Sin pedidos pendientes en Siesa</div>`;
      return;
    }

    {
      const grupos = PEDIDOS_TAB_LABELS.map(() => []);
      SIESA_PEDIDOS.forEach((p, i) => {
        const _gp = _g(p);
        const sinProd = p.items.filter(it => !it.producto_id).length;
        const totalUds = p.items.reduce((s, it) => s + (it.cantidad_pendiente || 0), 0);

        let accionBtn = '';
        const _rc = p.retencion_cartera;
        if (_rc && !p.siesa_triggered) {
          // Retenido por cartera: ni «Aprobar» ni «Error Siesa». Lo libera un
          // usuario de cartera (Gestor) o el pago; reintentar re-evalúa.
          accionBtn = `<div style="flex-shrink:0;display:flex;flex-direction:column;gap:4px;align-items:stretch;max-width:180px;">
              <div style="background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);padding:6px 10px;border-radius:6px;font-size:var(--fs-xs);font-weight:700;text-align:center;">⛔ Retenido por cartera</div>
              <div style="font-size:var(--fs-xs);color:var(--tx2);">${esc(_rc.resumen || (_rc.motivos || []).join(', '))}</div>
            </div>`;
        } else if (p.cartera_liberada && p.packing_estado === 'VERIFICADO' && !p.siesa_triggered && p.packing_id) {
          accionBtn = `<button onclick="carteraCerrarLiberado(${esc(p.packing_id)})"
            style="flex-shrink:0;background:var(--ok-bg);color:var(--ok-tx);border:1px solid var(--ok-brd);padding:8px 12px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;text-align:center;">
            Cartera lo liberó<br>▶ Cerrar caja
          </button>`;
        } else if (p.siesa_triggered) {
          // Estado final: Siesa tiene la factura
          // Los DOS papeles: la remisión descarga inventario y viaja con el
          // camión; la factura cobra. El endpoint de remisión existía sin
          // botón desde que se escribió.
          const _bt = 'width:100%;background:var(--bg-input);color:var(--tx);border:none;'
                    + 'padding:5px 8px;border-radius:6px;font-size:var(--fs-xs);'
                    + 'font-weight:600;cursor:pointer;margin-top:6px;';
          const btnRemision = p.packing_id
            ? `<button onclick="imprimirRemisionAdmin(${esc(p.packing_id)})" style="${_bt}">
                🖨 Remisión
               </button>
               <button onclick="imprimirFacturaAdmin(${esc(p.packing_id)})" style="${_bt}">
                🖨 Factura
               </button>`
            : '';
          accionBtn = `<div style="flex-shrink:0;background:var(--ok-bg);color:var(--ok-tx);border:1px solid var(--ok-brd);padding:8px 12px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;text-align:center;">✓ Despachado<br>en Siesa${btnRemision}</div>`;
        } else if (p.packing_estado === 'EN_PROCESO') {
          // Empacador verificando en mesa — admin puede entrar a ayudar/probar
          accionBtn = `<button onclick="empIniciarHUD(${esc(p.packing_id)})"
            style="flex-shrink:0;background:var(--lila-bg);color:var(--lila-tx);border:1px solid var(--lila-brd);padding:8px 12px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;text-align:center;">
            Packing<br>🔄 Abrir
          </button>`;
        } else if (p.packing_estado === 'VERIFICADO' && !p.siesa_triggered) {
          // RM creada en Siesa pero FE falló — carril de recuperación
          accionBtn = p.packing_id
            ? `<div style="flex-shrink:0;display:flex;flex-direction:column;gap:4px;align-items:stretch;">
                <div style="background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd);padding:6px 10px;border-radius:6px;font-size:var(--fs-xs);font-weight:700;text-align:center;">⚠ Error Siesa</div>
                <button onclick="facturarRemisionExistente(${esc(p.packing_id)})"
                  style="background:#7c2d12;color:#fdba74;border:1px solid #c2410c;padding:6px 10px;border-radius:6px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;text-align:center;">
                  🧾 Facturar Remisión
                </button>
              </div>`
            : `<div style="flex-shrink:0;background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd);padding:8px 12px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;text-align:center;">⚠ Error<br>Siesa</div>`;
        } else if (p.picking_completado) {
          // Picking listo — admin puede abrir directamente el packing
          accionBtn = `<button onclick="empIniciarHUD(${esc(p.packing_id)})"
            style="flex-shrink:0;background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);padding:8px 12px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;text-align:center;">
            Packing<br>pendiente ▶
          </button>`;
        } else if (p.picking_iniciado) {
          // Operario recogiendo
          accionBtn = `<div style="flex-shrink:0;background:var(--lila-bg);color:var(--info-tx);border:1px solid var(--info-brd);padding:8px 12px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;text-align:center;">
            En picking<br>${esc(p.picking_progreso || '')}
          </div>`;
        } else {
          // Sin tareas — listo para despachar
          accionBtn = `<button onclick="iniciarDespachoDesdeSiesa(${i})"
            style="flex-shrink:0;background:var(--pm-fill);color:#fff;border:none;padding:10px 14px;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">
            Aprobar
          </button>`;
        }

        grupos[_gp].push(`
          <div class="tabla-card">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
              <div style="min-width:0;">
                <div style="font-size:var(--fs-md);font-weight:700;">${esc(p.numero_pedido)}</div>
                <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(p.cliente || 'Sin cliente')}</div>
                <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(p.items.length)} producto(s) · ${totalUds} uds</div>
                ${sinProd ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:2px;">⚠ ${sinProd} sin registrar en WMS</div>` : ''}
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
    el.innerHTML = '<div style="color:var(--err-tx);">Error cargando pedidos</div>';
  }
}

const PEDIDOS_TAB_LABELS = ['POR DESPACHAR', 'EN PROCESO', 'DESPACHADO EN SIESA', 'ERROR SIESA', 'RETENIDO POR CARTERA'];
/** Pestañas con insignia de atención (las que esperan que alguien actúe). */
const PEDIDOS_TABS_ALERTA = [3, 4];

/**
 * A qué pestaña va un pedido. **Retenido por cartera no es «Error Siesa»**
 * (2026-09-25): la caja quedó VERIFICADA sin factura porque cartera la frenó,
 * y la pestaña de error la contaba con su insignia roja junto a los fallos
 * reales. Tampoco lo es una caja que cartera ya liberó y espera cerrarse.
 * @param {Object} p - pedido de `/api/siesa/pedidos`
 * @returns {number} índice en `PEDIDOS_TAB_LABELS`
 */
function pedidoGrupo(p) {
  if (p.siesa_triggered) return 2;
  if (p.retencion_cartera || p.cartera_liberada) return 4;
  if (p.packing_estado === 'VERIFICADO') return 3;
  if (p.picking_iniciado || p.packing_estado) return 1;
  return 0;
}

/** Render pedidos sub-tabs and the HTML for the currently active group. */
function renderPedidosTabsYLista() {
  const tabsEl = document.getElementById('ped-tabs');
  const el = document.getElementById('lista-pedidos');
  if (!tabsEl || !el) return;

  tabsEl.innerHTML = PEDIDOS_TAB_LABELS.map((label, i) => {
    const count = PEDIDOS_GRUPOS_COUNT[i] || 0;
    const badge = PEDIDOS_TABS_ALERTA.includes(i)
      ? (count ? `<span class="subtab-badge">${count}</span>` : '')
      : (count ? ` (${count})` : '');
    return `<div class="subtab${i === PEDIDOS_TAB_ACTIVO ? ' active' : ''}" onclick="pedidosCambiarTab(${i})">${label}${badge}</div>`;
  }).join('');

  el.innerHTML = PEDIDOS_GRUPOS_HTML[PEDIDOS_TAB_ACTIVO]
    || '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin pedidos en esta pestaña ✓</div>';
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
    el.innerHTML = '<div style="color:var(--err-tx);text-align:center;">Error cargando tareas de bodega</div>';
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
    || '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin tareas activas en esta pestaña ✓</div>';
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
      PRODUCTO_INCORRECTO:'❌ Producto incorrecto',
      BACKORDER_SIESA:    '🔒 Sin backorder en Siesa'
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
    // Una línea/producto por tarea, pero varias tareas pueden ser del mismo
    // pedido (ej. dos referencias bloqueadas del mismo PD). Agrupar por
    // pedido evita que el mismo PD aparezca repetido como si fueran envíos
    // distintos — una tarjeta por pedido, una fila por línea adentro, cada
    // línea con su propio botón/panel de auditoría (id sigue siendo t.id,
    // porque la decisión de auditoría es por línea, no por pedido entero).
    const _lineaHTML = (t, esPrimera) => `
      <div style="padding:10px 0;${esPrimera ? '' : 'border-top:1px solid var(--brd);'}">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
          <div style="flex:1;min-width:0;">
            <span style="font-size:var(--fs-sm);font-weight:600;">${esc(t.producto_nombre || t.producto_codigo)}</span>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(t.ubicacion_codigo || '—')}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${
              t.operario_id
                ? '👤 En proceso'
                : t.estado === 'BLOQUEADO'
                  ? '🔴 Bloqueado — ' + (MOTIVO_LABEL[t.motivo_bloqueo] || t.motivo_bloqueo || 'novedad reportada')
                  : '⏳ En cola'
            }</div>
            ${t.estado === 'BLOQUEADO' && t.observaciones_bloqueo
              ? `<div style="font-size:var(--fs-xs);color:var(--err-tx);margin-top:3px;font-style:italic;">"${esc(t.observaciones_bloqueo)}"</div>`
              : ''}
          </div>
          <div style="text-align:right;flex-shrink:0;">
            <span class="badge ${t.estado==='EN_PROCESO'?'badge-blue':t.estado==='BLOQUEADO'?'badge-red':'badge-yellow'}">${esc(t.estado)}</span>
            <div style="font-size:20px;font-weight:800;margin-top:4px;">${esc(t.cantidad_recogida||0)}/${esc(t.cantidad_solicitada)}</div>
          </div>
        </div>
        ${t.estado === 'BLOQUEADO' ? `
        <div style="margin-top:10px;padding-top:10px;border-top:1px solid var(--err-brd);">
          <button onclick="auditoriaMostrarPanel(${esc(t.id)})"
            style="width:100%;padding:9px;background:var(--lila-bg);color:var(--lila-tx);border:1px solid var(--info-brd);border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">
            🔍 Auditoría
          </button>
          <div id="auditoria-panel-${esc(t.id)}" style="display:none;margin-top:10px;">
            <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:8px;">¿Qué encontraste físicamente?</div>
            <select id="auditoria-resultado-${esc(t.id)}" onchange="auditoriaResultadoCambio(${esc(t.id)})"
              style="width:100%;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);margin-bottom:8px;">
              <option value="">— Selecciona resultado —</option>
              <option value="ENCONTRADO">✅ Encontrado (se genera conteo cíclico)</option>
              <option value="NO_ENCONTRADO">❌ No encontrado — faltante confirmado</option>
              <option value="AVERIA">🚫 Mercancía averiada</option>
            </select>
            <div id="auditoria-conteo-${esc(t.id)}" style="display:none;margin-bottom:8px;padding:9px;background:var(--bg-s);border:1px solid var(--info-brd);border-radius:8px;">
              <label style="display:flex;align-items:center;gap:8px;font-size:var(--fs-xs);color:var(--lila-tx);cursor:pointer;">
                <input type="checkbox" id="auditoria-forzar-${esc(t.id)}" checked>
                Generar conteo cíclico forzado de este SKU
              </label>
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin:4px 0 6px;">El conteo es lo que ajusta Siesa — esta auditoría no mueve inventario.</div>
              <select id="auditoria-operario-${esc(t.id)}"
                style="width:100%;padding:8px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-xs);">
                <option value="">Auto-asignar (el que lo tome primero)</option>
              </select>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px;">
              <div>
                <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:4px;">Cant. hallada</div>
                <input id="auditoria-cantidad-${esc(t.id)}" type="number" min="0" value="0"
                  style="width:100%;padding:9px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);box-sizing:border-box;">
              </div>
              <div>
                <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:4px;">Ubicación hallada</div>
                <input id="auditoria-ubicacion-${esc(t.id)}" type="text" placeholder="Ej: A-01-02"
                  style="width:100%;padding:9px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);box-sizing:border-box;">
              </div>
            </div>
            <textarea id="auditoria-obs-${esc(t.id)}" placeholder="Observaciones (opcional)..."
              style="width:100%;padding:9px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-xs);resize:vertical;min-height:56px;box-sizing:border-box;margin-bottom:8px;"></textarea>
            <div style="display:flex;gap:8px;">
              <button onclick="auditoriaCancelarPanel(${esc(t.id)})"
                style="flex:1;padding:9px;background:var(--bg-input);border:1px solid var(--brd);color:var(--tx2);border-radius:8px;font-size:var(--fs-xs);cursor:pointer;">
                Cancelar
              </button>
              <button onclick="auditoriaGuardar(${esc(t.id)})"
                style="flex:2;padding:9px;background:#a78bfa;color:#000;border:none;border-radius:8px;font-size:var(--fs-xs);font-weight:800;cursor:pointer;">
                Guardar auditoría →
              </button>
            </div>
          </div>
        </div>` : ''}
      </div>`;

    let html = '';
    grupos.forEach(({ label, color, tareas: ts }) => {
      if (!ts.length) return;

      const porPedido = new Map();
      ts.forEach(t => {
        const key = `${t.referencia_documento || t.codigo}`;
        if (!porPedido.has(key)) porPedido.set(key, []);
        porPedido.get(key).push(t);
      });

      html += `<div style="font-size:var(--fs-xs);font-weight:700;color:${color};text-transform:uppercase;letter-spacing:.8px;padding:10px 0 5px;border-bottom:1px solid var(--brd);margin-bottom:8px;">${label} · ${esc(ts.length)} línea${ts.length!==1?'s':''} · ${esc(porPedido.size)} pedido${porPedido.size!==1?'s':''}</div>`;

      html += Array.from(porPedido.entries()).map(([pedido, items]) => {
        const hayBloqueada = items.some(t => t.estado === 'BLOQUEADO');
        const esTraslado = items[0].tipo_documento === 'TRASLADO';
        return `
        <div class="tabla-card" style="${hayBloqueada?'border-color:#7f1d1d;background:var(--err-bg);':''}">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:2px;">
            <span style="font-size:var(--fs-sm);font-weight:700;">${pedido}</span>
            ${esTraslado ? '<span style="font-size:var(--fs-xs);font-weight:700;padding:2px 7px;border-radius:10px;background:#1e3a5f;color:var(--info-tx);letter-spacing:.5px;">🔄 TRANSFERENCIA</span>' : ''}
            <span style="font-size:var(--fs-xs);color:var(--tx3);">· ${esc(items.length)} línea${items.length!==1?'s':''}</span>
          </div>
          ${items.map((t, i) => _lineaHTML(t, i === 0)).join('')}
        </div>`;
      }).join('');
    });
    return html;
  } catch (e) {
    return '<div style="color:var(--err-tx);text-align:center;">Error mostrando tareas de bodega</div>';
  }
}

/**
 * Renderiza la línea "Ahora: ..." de la tarjeta de operario — snapshot en
 * vivo de `tarea_actual` (viene de /api/dashboard/productividad, refrescado
 * cada 30s por TIMER_ADMIN mientras la pestaña Operarios esté abierta).
 * @param {{tipo:string, tipo_documento?:string, referencia:string, ubicacion:string, producto:?string, minutos_en_tarea:?number}|null} t
 */
function _tareaActualHTML(t) {
  if (!t) {
    return `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:4px;">⚪ Sin tarea asignada</div>`;
  }
  const COLORES = { PICKING: '#1d4ed8', REPOSICION: '#c2410c', CONTEO: '#b45309', PACKING: '#7c3aed' };
  const color = COLORES[t.tipo] || '#555';
  const min = t.minutos_en_tarea;
  // Mismo umbral que ConteoService/reposicion_service.liberar_tareas_zombi (2h) —
  // si lleva más que eso en la misma tarea, probablemente está atascado, no trabajando.
  const punto = min != null && min >= 120 ? '🔴' : '🟢';
  const tiempo = min == null ? '' : min < 1 ? ' · recién' : ` · hace ${min} min`;
  const etiqueta = t.tipo === 'PICKING'
    ? `PICKING · ${t.tipo_documento === 'TRASLADO' ? 'Traslado' : 'Pedido'} ${t.referencia || ''}`
    : `${t.tipo}${t.referencia ? ' · ' + t.referencia : ''}`;
  const detalle = [t.ubicacion, t.producto].filter(Boolean).join(' · ');
  return `
    <div style="font-size:var(--fs-xs);font-weight:700;color:${color};margin-bottom:1px;">${punto} Ahora: ${etiqueta}</div>
    ${detalle ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:4px;">${detalle}${tiempo}</div>` : ''}
  `;
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
    if (!todos.length) { el.innerHTML = '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin usuarios</div>'; return; }

    // Ordenar: más tareas primero
    todos.sort((a, b) => (metricas[b.id]?.total_tareas || 0) - (metricas[a.id]?.total_tareas || 0));

    el.innerHTML = todos.map((u, i) => {
      const op = metricas[u.id] || { total_tareas: 0, pickings_completados: 0, packings_completados: 0, conteos_completados: 0, reposiciones_completadas: 0, tarea_actual: null };
      const badges = [u.puede_picar && '<span style="background:#1e40af;color:var(--tx);border-radius:4px;padding:1px 5px;font-size:var(--fs-xs);">Picker</span>',
                      u.puede_empacar && '<span style="background:#6b21a8;color:var(--tx);border-radius:4px;padding:1px 5px;font-size:var(--fs-xs);">Empacador</span>',
                      u.puede_abastecer && '<span style="background:#7c2d12;color:#fdba74;border-radius:4px;padding:1px 5px;font-size:var(--fs-xs);">Abastecedor</span>',
                      u.puede_organizar_layout && '<span style="background:#1e3a5f;color:var(--info-tx);border-radius:4px;padding:1px 5px;font-size:var(--fs-xs);">Layout</span>'].filter(Boolean).join(' ');
      const color = op.total_tareas > 0 ? (i === 0 ? '#4ade80' : '#fff') : '#555';
      return `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:600;">${esc(u.nombre)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:2px;">${esc(u.rol)} ${badges}</div>
            ${_tareaActualHTML(op.tarea_actual)}
            <div style="font-size:var(--fs-xs);color:var(--tx3);">Pick:${esc(op.pickings_completados)} Pack:${esc(op.packings_completados)} Repo:${esc(op.reposiciones_completadas || 0)} Conteos:${esc(op.conteos_completados)}</div>
            ${u.puede_picar && u.capacidad_diaria_conteo != null ? (() => {
              const cap = u.capacidad_diaria_conteo;
              const hoy = op.conteos_hoy || 0;
              const pct = cap > 0 ? Math.min(100, Math.round(hoy / cap * 100)) : 0;
              const col = pct >= 100 ? '#ef4444' : pct >= 70 ? '#f59e0b' : '#4ade80';
              return `<div style="margin-top:4px;">
                <div style="display:flex;justify-content:space-between;font-size:var(--fs-xs);color:var(--tx3);margin-bottom:2px;">
                  <span>Conteos hoy</span><span style="color:${col};font-weight:600;">${hoy}/${cap > 0 ? cap : '∞'}</span>
                </div>
                ${cap > 0 ? `<div style="background:var(--bg-s2);border-radius:3px;height:3px;overflow:hidden;"><div style="background:${col};width:${pct}%;height:100%;border-radius:3px;transition:width .3s;"></div></div>` : ''}
              </div>`;
            })() : ''}
          </div>
          <div style="text-align:right;">
            <div style="font-size:var(--fs-2xl);font-weight:800;color:${color}">${esc(op.total_tareas)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">tareas 7d</div>
          </div>
        </div>
      </div>`;
    }).join('');
  } catch (e) { el.innerHTML = '<div style="color:var(--err-tx);">Error</div>'; }
}

let _filtroAlmacenStockListo = false;
/** Fill the warehouse selector of the Stock tab once; defaults to ALMACEN_ID. */
async function _poblarFiltroAlmacenStock() {
  const sel = document.getElementById('filtro-almacen-stock');
  if (!sel || _filtroAlmacenStockListo) return;
  try {
    const almacenes = await get('/api/almacenes/');
    (almacenes || []).forEach(a => {
      const op = document.createElement('option');
      op.value = String(a.id);
      op.textContent = `${a.codigo} · ${a.nombre}`;
      sel.appendChild(op);
    });
    if ((almacenes || []).some(a => a.id === ALMACEN_ID)) sel.value = String(ALMACEN_ID);
    _filtroAlmacenStockListo = true;
  } catch (e) { /* sin la lista queda «Todas las bodegas», que sigue siendo verdad */ }
}

/** Load the product catalog (top of the tab) and the stock alerts. */
async function cargarStock() {
  await _poblarFiltroAlmacenStock();
  await cargarCatalogo(1);
  const el = document.getElementById('lista-alertas');
  if (!el) return;
  try {
    const d = await get('/api/dashboard/alertas-stock?almacen_id=' + ALMACEN_ID);
    if (!d.alertas || !d.alertas.length) {
      el.innerHTML = '<div style="color:var(--ok-tx);text-align:center;padding:40px;">✓ Sin alertas</div>';
    } else {
      el.innerHTML = d.alertas.map(a => `
      <div class="tabla-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div><div style="font-size:var(--fs-sm);font-weight:600;">${esc(a.nombre)}</div><div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(a.codigo)} · Clase ${esc(a.clasificacion_abc||'—')}</div></div>
          <div style="text-align:right;">
            <span class="badge ${a.urgencia==='CRITICO'?'badge-red':'badge-yellow'}">${esc(a.urgencia)}</span>
            <div style="font-size:20px;font-weight:800;color:${a.urgencia==='CRITICO'?'var(--err-tx)':'var(--warn-tx)'}">${esc(a.stock_actual)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">mín:${esc(a.stock_minimo)}</div>
          </div>
        </div>
      </div>`).join('');
    }
  } catch (e) { el.innerHTML = '<div style="color:var(--err-tx);">Error</div>'; }
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
  // Sin selector (u opción vacía) = todas las bodegas, que es lo que la API
  // devuelve sin `almacen_id`.
  const almacenId = (document.getElementById('filtro-almacen-stock') || {}).value || '';
  const filtroAlm = almacenId ? `&almacen_id=${encodeURIComponent(almacenId)}` : '';
  try {
    const d = await get(`/api/productos/?page=${_catalogoPag}&per_page=20&q=${encodeURIComponent(q)}${filtroAlm}`);
    if (totalEl) totalEl.textContent = `${d.total} productos`;
    if (!d.productos || !d.productos.length) {
      el.innerHTML = '<div style="color:var(--tx3);text-align:center;padding:20px;font-size:var(--fs-sm);">Sin productos</div>';
      if (pagEl) pagEl.innerHTML = '';
      return;
    }
    el.innerHTML = d.productos.map(p => `
      <div class="tabla-fila">
        <div>
          <div style="font-size:var(--fs-sm);font-weight:600;">${esc(p.nombre)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(p.codigo)}${p.codigo_siesa && p.codigo_siesa !== p.codigo ? ' · Siesa: ' + p.codigo_siesa : ''} · Clase ${esc(p.clasificacion_abc || '—')}</div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:var(--fs-md);font-weight:700;color:${(p.stock_vendible ?? p.stock_total) > 0 ? 'var(--ok-tx)' : (p.stock_total > 0 ? 'var(--warn-tx)' : 'var(--tx3)')}">${esc(p.stock_total)}</div>
          ${p.stock_averiado > 0 ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);">${esc(p.stock_averiado)} averiadas</div>` : ''}
          ${!almacenId && Array.isArray(p.stock_por_almacen) && p.stock_por_almacen.length
            ? `<div style="font-size:var(--fs-xs);color:var(--info-tx);">${p.stock_por_almacen.map(a => `${esc(a.almacen)} ${esc(a.stock_total)}`).join(' · ')}</div>`
            : ''}
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(p.unidad_medida || 'UND')}</div>
        </div>
      </div>`).join('');
    // Paginación
    if (pagEl && d.paginas > 1) {
      let btns = '';
      if (_catalogoPag > 1) btns += `<button onclick="cargarCatalogo(${_catalogoPag - 1})" style="padding:6px 12px;background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx2);border-radius:6px;cursor:pointer;">◀</button>`;
      btns += `<span style="font-size:var(--fs-xs);color:var(--tx3);align-self:center;">${_catalogoPag} / ${esc(d.paginas)}</span>`;
      if (_catalogoPag < d.paginas) btns += `<button onclick="cargarCatalogo(${_catalogoPag + 1})" style="padding:6px 12px;background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx2);border-radius:6px;cursor:pointer;">▶</button>`;
      pagEl.innerHTML = btns;
    } else if (pagEl) pagEl.innerHTML = '';
  } catch (e) { el.innerHTML = '<div style="color:var(--err-tx);">Error cargando productos</div>'; }
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
    // El color y la palabra salen de `modo_datos`, que MIRA EL HOST. Antes
    // salían solo de las credenciales: con `CONNEKTA_URL` en serviciosqa esta
    // pantalla decía «PRODUCCIÓN · Listo para operar» en verde, con el banner
    // rojo de «DATOS DE PRUEBA» arriba, en la misma vista. Los dos venían del
    // mismo backend.
    //
    // El destino manda sobre el modo: tener credenciales reales y POSTs
    // habilitados no es producción si los documentos aterrizan en el Siesa de
    // pruebas.
    let color, estado, detalle;
    if (d.modo_datos === 'datos_de_prueba') {
      color = '#f87171'; estado = 'DATOS DE PRUEBA';
      detalle = `Siesa apunta a <b>${esc(d.siesa_host || '?')}</b> — nada de lo que se `
        + 'envíe llega al Siesa real. NO usar estos números para decidir.';
    } else if (d.modo_datos === 'simulacion') {
      color = '#facc15'; estado = 'SIMULACIÓN';
      detalle = 'Sin credenciales — todo simulado localmente';
    } else if (d.modo_datos === 'ensayo') {
      color = '#fb923c'; estado = 'MODO ENSAYO';
      detalle = 'Credenciales activas · GETs reales · POSTs bloqueados en servidor';
    } else if (d.modo_datos === 'produccion') {
      color = '#4ade80'; estado = 'PRODUCCIÓN';
      detalle = d.mensaje || 'Listo para operar';
    } else {
      // Backend viejo o campo ausente: NO se asume producción. Un verde de más
      // es peor que un "no sé" — es la regla 0 aplicada al banner.
      color = '#a3a3a3'; estado = 'AMBIENTE DESCONOCIDO';
      detalle = 'El servidor no informó a qué Siesa apunta. Verificar antes de operar.';
    }
    el.innerHTML = `
      <div class="tabla-card">
        <div style="text-align:center;padding:20px 0;">
          <div style="font-size:var(--fs-sm);color:var(--tx3);margin-bottom:8px;">Estado Connekta / Siesa</div>
          <div style="font-size:var(--fs-2xl);font-weight:800;color:${color};">${estado}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:8px;line-height:1.5;">${detalle}</div>
        </div>
        <div class="tabla-fila"><span class="tabla-nombre">Credenciales</span><span class="badge ${d.credenciales_configuradas?'badge-green':'badge-red'}">${d.credenciales_configuradas?'✓ Activas':'✗ Faltan'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">GETs (lectura)</span><span class="badge ${!d.modo_simulacion?'badge-green':'badge-yellow'}">${!d.modo_simulacion?'✓ Real':'Simulado'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">POSTs (escritura)</span><span class="badge ${(!d.modo_simulacion&&!d.modo_ensayo)?'badge-green':d.modo_ensayo?'badge-yellow':'badge-red'}">${(!d.modo_simulacion&&!d.modo_ensayo)?'✓ Activos':d.modo_ensayo?'Bloqueados (ensayo)':'Simulados'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">Bodega</span><span style="font-size:var(--fs-sm);color:var(--tx2);">${esc(d.bodega||'—')}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">CO</span><span style="font-size:var(--fs-sm);color:var(--tx2);">${esc(d.centro_operacion||'—')}</span></div>
      </div>
      <button id="btn-setup-inicial" onclick="setupInicial()"
        style="width:100%;margin-top:12px;padding:14px;background:#1e3a5f;color:var(--info-tx);border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">
        ↻ Sincronizar catálogo + cargar stock inicial
      </button>
      <div id="setup-resultado" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx3);text-align:center;"></div>

      <div style="border-top:1px solid var(--brd);margin-top:16px;padding-top:16px;">
        <div style="font-size:var(--fs-sm);font-weight:700;margin-bottom:8px;">Inventario bilateral</div>
        <button onclick="verReconciliacion()"
          style="width:100%;padding:12px;background:var(--bg-s);color:var(--info-tx);border:1px solid var(--info-brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">
          ⚖ Ver reconciliación WMS vs Siesa
        </button>
        <div id="inv-resultado" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx3);text-align:center;"></div>
      </div>
      <div id="panel-reconciliacion" style="margin-top:8px;"></div>

      <!-- Sync barcodes EAN -->
      <div style="border-top:1px solid var(--brd);margin-top:16px;padding-top:16px;">
        <div style="font-size:var(--fs-sm);font-weight:700;margin-bottom:4px;">📦 Sync códigos de barras EAN</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:10px;">Vuelca todos los barcodes de Siesa a la DB local. Corre automático a las 2am; este botón lo fuerza ahora.</div>
        <button onclick="syncBarcodes()"
          style="width:100%;padding:12px;background:#1e3a5f;color:var(--info-tx);border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">
          ↻ Sincronizar barcodes ahora
        </button>
        <div id="sync-barras-resultado" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx3);min-height:16px;"></div>
        <button onclick="barcodesCobertura()"
          style="width:100%;margin-top:8px;padding:10px;background:var(--bg-input);color:var(--info-tx);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">
          ¿Cuántos NO se pueden escanear?
        </button>
        <div id="barras-cobertura" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx3);"></div>
        <!-- Cada SKU de esta lista es una pregunta que el recepcionista tiene
             que contestar en cada escaneo: «¿unidad o caja?». El sistema no lo
             puede saber —el proveedor pegó la EAN de unidad en la caja— y
             adivinarlo es lo que hacía que el CD sobre-recibiera. Poblar el EAN
             de empaque cierra la pregunta para siempre. -->
        <button onclick="skusSinEanEmpaque()"
          style="width:100%;margin-top:8px;padding:10px;background:var(--bg-input);color:var(--warn-tx);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">
          ¿En cuántos hay que preguntar «unidad o caja»?
        </button>
        <div id="skus-sin-ean-empaque" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx3);"></div>
      </div>

      <!-- Diagnóstico barcodes Siesa -->
      <div style="border-top:1px solid var(--brd);margin-top:16px;padding-top:16px;">
        <div style="font-size:var(--fs-sm);font-weight:700;margin-bottom:8px;">🔍 Diagnóstico códigos de barras</div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <input id="debug-barras-input" type="text" placeholder="EAN a probar (ej: 49218787)"
            style="flex:1;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);">
          <button onclick="testBarras()"
            style="padding:10px 14px;background:#1e3a5f;color:var(--info-tx);border:none;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">
            Probar
          </button>
        </div>
        <div id="debug-barras-resultado" style="font-size:var(--fs-xs);color:var(--tx3);min-height:20px;white-space:pre-wrap;word-break:break-all;"></div>
      </div>

      ${d.modo_ensayo ? `
      <div style="background:var(--warn-bg);border:1px solid var(--err-brd);border-radius:10px;padding:12px;margin-top:8px;font-size:var(--fs-xs);color:var(--orange);line-height:1.6;">
        <strong>MODO ENSAYO activo</strong><br>
        Los pedidos y OCs vienen de Siesa real. Al confirmar despacho o recepción, el payload se certifica en los logs del servidor pero <strong>no mueve inventario en Siesa</strong>.<br>
        Para activar producción: borrar la variable <code>MODO_ENSAYO</code> en Railway.
      </div>` : ''}`;
  } catch (e) { el.innerHTML = '<div style="color:var(--err-tx);">Error</div>'; }
}

/** Trigger catalog sync + initial stock load from Siesa, polling for progress. */
async function setupInicial() {
  const btn = document.getElementById('btn-setup-inicial');
  const res = document.getElementById('setup-resultado');
  if (!btn) return;

  const FASES = { iniciando: '⏳ Iniciando...', catalogo: '⏳ Fase 1/2: sincronizando catálogo (~2 min)...', stock: '⏳ Fase 2/2: cargando stock (~60 seg)...' };

  btn.disabled = true;
  btn.textContent = '↻ Procesando...';
  res.style.color = 'var(--info-tx)';
  res.textContent = 'Iniciando setup...';

  try {
    const d = await post('/api/siesa/setup-inicial', {});
    if (d.simulado) {
      res.style.color = 'var(--orange)';
      res.textContent = d.mensaje || 'Modo simulación';
      btn.disabled = false;
      btn.textContent = '↻ Sincronizar catálogo + cargar stock inicial';
      return;
    }
    if (d.en_curso && !d.iniciado) {
      res.style.color = 'var(--orange)';
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
            res.style.color = 'var(--err-tx)';
            res.textContent = 'Error en fase ' + e.fase + ': ' + e.ultimo_error;
          } else {
            const cat = e.resultado_catalogo;
            const stk = e.resultado_stock;
            res.style.color = 'var(--ok-tx)';
            const partesCat = cat ? `catálogo: ${cat.creados} creados · ${cat.actualizados} actualizados` : '';
            const partesStk = stk ? `stock: ${stk.cargados} nuevos · ${stk.actualizados} actualizados` : '';
            res.textContent = '✓ ' + [partesCat, partesStk].filter(Boolean).join(' — ');
          }
        }
      } catch (err) { clearInterval(iv); }
    }, 5000);
  } catch (e) {
    res.style.color = 'var(--err-tx)';
    res.textContent = 'Error: ' + (e.message || e);
    btn.disabled = false;
    btn.textContent = '↻ Sincronizar catálogo + cargar stock inicial';
  }
}

/** Trigger EAN barcode sync from Siesa and poll for completion. */
/**
 * Cuántos SKU activos NO se pueden escanear, y cuáles.
 *
 * El resultado del sync decía «completado» igual si actualizó 3 productos que
 * si los cubrió todos. Esto mide la cobertura contra la base, que es lo que le
 * pasa al operario con la pistola en la mano.
 *
 * La lista se muestra recortada y con enlace al CSV completo: 2.118 nombres en
 * un panel no los lee nadie, y una lista que nadie lee es lo mismo que no
 * tenerla.
 */
async function barcodesCobertura() {
  const el = document.getElementById('barras-cobertura');
  if (!el) return;
  el.innerHTML = '<span style="color:var(--info-tx);">Contando…</span>';
  let d;
  try {
    d = await get('/api/productos/sin-codigo-barras?per_page=20');
  } catch (e) {
    el.innerHTML = `<span style="color:var(--err-tx);">No se pudo consultar: ${esc(e.message)}</span>`;
    return;
  }
  const c = d.cobertura || {};
  if (!c.hay_catalogo) {
    el.innerHTML = '<span style="color:var(--warn-tx);">No hay catálogo cargado — '
      + 'sincronizalo antes de mirar cobertura.</span>';
    return;
  }
  const muestra = (d.productos || [])
    .map(p => `<div style="padding:2px 0;border-bottom:1px solid var(--brd);">
      <code style="color:var(--info-tx);">${esc(p.codigo)}</code> ${esc(p.nombre || '')}</div>`)
    .join('');
  el.innerHTML = `
    <div style="color:var(--tx);margin-bottom:6px;">
      <strong>${c.con_codigo_barras.toLocaleString('es-CO')}</strong> de
      <strong>${c.productos_activos.toLocaleString('es-CO')}</strong> se pueden escanear
      (${esc(c.porcentaje)}%).
    </div>
    <div style="color:${c.sin_codigo_barras ? 'var(--warn-tx)' : 'var(--ok-tx)'};margin-bottom:8px;">
      ${c.sin_codigo_barras.toLocaleString('es-CO')} hay que teclearlos a mano.
    </div>
    ${d.total ? `<div style="max-height:180px;overflow:auto;font-size:var(--fs-xs);">${muestra}</div>
    <div style="margin-top:6px;font-size:var(--fs-xs);color:var(--tx3);">
      Mostrando ${esc(d.productos.length)} de ${d.total.toLocaleString('es-CO')} ·
      <a href="#" onclick="barcodesDescargarFaltantes();return false;"
         style="color:var(--info-tx);">bajar la lista completa (CSV)</a>
    </div>` : ''}`;
}

/** Baja el CSV de los que no se pueden escanear, para repartirlo en bodega. */
async function barcodesDescargarFaltantes() {
  try {
    const r = await _fetchConTimeout('/api/productos/sin-codigo-barras?formato=csv');
    if (!r.ok) { alerta('No se pudo bajar la lista', 'error'); return; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'productos_sin_codigo_barras.csv';
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alerta('No se pudo bajar la lista: ' + e.message, 'error');
  }
}

async function syncBarcodes() {
  const res = document.getElementById('sync-barras-resultado');
  if (!res) return;
  res.style.color = 'var(--info-tx)';
  res.textContent = '⏳ Iniciando sync... (corre en background, puede tardar varios minutos)';
  try {
    await post('/api/siesa/sync-barcodes', {});
    res.textContent = '✓ Sync iniciado. Consulta el estado en unos minutos con "Probar" (sin código) para ver cuántos barcodes se cargaron.';
    res.style.color = 'var(--ok-tx)';
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
          res.style.color = 'var(--err-tx)';
          res.textContent = `✗ Error: ${e.ultimo_error}`;
        }
      } catch (_) {}
    }, 10000);
  } catch (e) {
    res.style.color = 'var(--err-tx)';
    res.textContent = 'Error al iniciar: ' + (e.message || e);
  }
}

/** Diagnose a barcode by querying Siesa API_v2_ItemsBarras directly. */
async function testBarras() {
  const inp = document.getElementById('debug-barras-input');
  const res = document.getElementById('debug-barras-resultado');
  if (!res) return;
  const codigo = (inp ? inp.value.trim() : '');
  res.style.color = 'var(--info-tx)';
  res.textContent = '⏳ Consultando Siesa...';
  try {
    const url = '/api/siesa/debug-barras-raw' + (codigo ? '?codigo=' + encodeURIComponent(codigo) : '');
    const d = await get(url);
    const tabla = d?.detalle?.Table || d?.Table || [];
    if (!tabla.length) {
      res.style.color = 'var(--err-tx)';
      res.textContent = '✗ Sin resultados — Siesa no tiene barcode "' + (codigo || '(sin filtro)') + '" en API_v2_ItemsBarras.\nEl escaneo por EAN físico no funcionará hasta configurar barcodes en Siesa.';
    } else {
      res.style.color = 'var(--ok-tx)';
      res.textContent = '✓ Siesa SÍ tiene barcodes.\nPrimeros resultados:\n' + JSON.stringify(tabla.slice(0, 3), null, 2);
    }
  } catch (e) {
    res.style.color = 'var(--err-tx)';
    res.textContent = 'Error: ' + (e.message || e) + '\n(puede que el conector API_v2_ItemsBarras no esté configurado en Connekta)';
  }
}

/**
 * Start WMS vs Siesa stock reconciliation and poll for results.
 *
 * Pinta TRES respuestas, no dos. «No se puede comparar» —un almacén sin bodega
 * Siesa, una bodega Siesa sin almacén WMS— no es «cuadra»: antes se mezclaba en
 * la suma y desaparecía, y el veredicto verde de esta pantalla es la casilla de
 * la Fase 4 de `docs/arranque_produccion.md`, la luz verde para que los
 * operarios arranquen.
 *
 * Y pinta el DENOMINADOR: el backend ya calculaba `cobertura_pct` y lo publicaba
 * en el JSON, y el render no lo mostraba. Un conteo pelado de diferencias sin
 * cuántos SKU se compararon no se puede interpretar.
 *
 * De las tres respuestas, la tercera se abre en dos: incomparable **previsto y
 * declarado** (AV1, TRA1 — no llevan almacén en el WMS y nunca lo van a llevar)
 * e incomparable que **nadie previó**. Se pintan distinto porque exigen cosas
 * distintas: el previsto se lee y se sigue, el imprevisto manda a levantarse de
 * la silla. Solo el segundo deja el veredicto en ámbar.
 */
async function verReconciliacion() {
  const res = document.getElementById('inv-resultado');
  const panel = document.getElementById('panel-reconciliacion');
  if (!res || !panel) return;
  res.style.color = 'var(--info-tx)';
  res.textContent = '⏳ Iniciando reconciliación... (~2 min)';
  panel.innerHTML = '';
  try {
    const d = await post('/api/siesa/reconciliacion', {});
    if (d.simulado) { res.style.color = 'var(--orange)'; res.textContent = 'Modo simulación'; return; }
    if (d.en_curso && !d.iniciado) { res.textContent = '⏳ Ya en proceso — monitoreando...'; }
    const iv = setInterval(async () => {
      try {
        const e = await get('/api/siesa/reconciliacion-estado');
        if (!e.en_curso) {
          clearInterval(iv);
          if (e.ultimo_error) {
            res.style.color = 'var(--err-tx)'; res.textContent = 'Error: ' + e.ultimo_error; return;
          }
          const r = e.ultimo_resultado;
          if (!r) { res.style.color = 'var(--orange)'; res.textContent = 'Sin resultado — intenta de nuevo'; return; }
          if (r.abortado) {
            res.style.color = 'var(--orange)'; res.textContent = '⚠ ' + (r.motivo || 'Reconciliación abortada'); return;
          }
          const nc = r.no_comparable || {};
          const noComp = r.total_no_comparable || 0;
          // «Previsto y declarado» vs. «nadie lo previó». El backend ya hizo la
          // distinción (`_incomparable_esperado`); acá NO se vuelve a decidir,
          // solo se pinta — si la pantalla tuviera su propio criterio serían
          // dos políticas para una pregunta, y divergirían.
          const noCompImprev = r.total_no_comparable_imprevisto != null
            ? r.total_no_comparable_imprevisto : noComp;
          const noCompPrev = r.total_no_comparable_esperado || 0;
          const bodegasSinAlmacen = nc.bodegas_siesa_sin_almacen_wms || [];
          const previstas = bodegasSinAlmacen.filter(b => b.esperado);
          const imprevistas = bodegasSinAlmacen.filter(b => !b.esperado);
          const porBodega = r.por_bodega || [];
          const cuadre = (r.cuadre_pct === null || r.cuadre_pct === undefined) ? '—' : r.cuadre_pct + '%';
          const comparadas = (r.bodegas_comparadas || []).length;

          // Veredicto. Verde SOLO si no hay diferencias Y no hay nada que se
          // haya quedado sin comparar.
          if (r.sin_diferencias) {
            res.style.color = 'var(--ok-tx)';
            res.textContent = `✓ Sin diferencias — cada bodega cuadra con la suya `
              + `(${r.skus_cuadran}/${r.skus_comparados} SKU en ${comparadas} bodega(s))`
              + (noCompPrev ? ` · ${noCompPrev} SKU incomparables previstos` : '');
          } else if (r.total_discrepancias === 0) {
            res.style.color = 'var(--orange)';
            res.textContent = `⚠ Cuadra lo comparable (${cuadre} de ${r.skus_comparados} SKU), `
              + `pero ${noCompImprev} SKU no se pueden comparar`;
          } else {
            res.style.color = 'var(--warn-tx)';
            res.textContent = `⚠ ${r.total_discrepancias} diferencias · cuadre ${cuadre} `
              + `de ${r.skus_comparados} SKU comparados`
              + (noCompImprev ? ` · ${noCompImprev} SKU sin comparar` : '');
          }

          const filaNoComparable = (etiqueta, detalle, color) => `
            <div class="tabla-fila" style="font-size:var(--fs-xs);">
              <div><div style="font-weight:600;color:${color || 'var(--orange)'};">${etiqueta}</div>
                   <div style="color:var(--tx3);">${detalle}</div></div>
            </div>`;
          const filaBodegaSinAlmacen = (b, color) => filaNoComparable(
            `${b.bodega}: bodega de Siesa sin almacén en el WMS`,
            `${b.skus_siesa} SKU · ${b.unidades_siesa} und`
            + (b.contraparte_wms ? ` — ${b.contraparte_wms.descripcion}`
               + (b.contraparte_wms.unidades != null ? ` (${b.contraparte_wms.unidades} und en el WMS, sin cuadrar)` : '')
               : '')
            + (b.justificacion ? `<br>${esc(b.justificacion)}` : ''), color);

          // Lo IMPREVISTO: naranja, y es lo que dejó el veredicto en ámbar.
          const bloqueNoComparable = noCompImprev ? `
            <div style="font-size:var(--fs-xs);color:var(--orange);margin:12px 0 6px;">
              No se puede comparar y nadie lo previó — ${noCompImprev} SKU (no es «cuadra»):</div>
            ${(nc.almacenes_sin_bodega_siesa || []).map(a => filaNoComparable(
                `Almacén ${esc(a.almacen)} sin bodega Siesa asignada`,
                `${esc(a.skus)} SKU · ${esc(a.unidades)} und — no se sabe contra qué bodega comparar`)).join('')}
            ${(nc.bodegas_wms_sin_datos_siesa || []).map(b => filaNoComparable(
                `${esc(b.bodega)}: el WMS tiene stock y Siesa no reportó nada`,
                `${esc(b.skus_wms)} SKU · ${esc(b.unidades_wms)} und`)).join('')}
            ${imprevistas.map(b => filaBodegaSinAlmacen(b)).join('')}
            ${nc.skus_siesa_sin_producto_wms ? filaNoComparable(
                `${esc(nc.skus_siesa_sin_producto_wms)} SKU de Siesa sin producto en el catálogo WMS`,
                'no hay contra qué compararlos — falta sincronizar catálogo') : ''}` : '';

          // Lo PREVISTO: gris, y con el motivo escrito. No descalificar no es
          // esconder — una exención que no se ve en la pantalla es una
          // exención silenciosa, que es de donde salen las listas que crecen.
          const bloquePrevisto = previstas.length ? `
            <div style="font-size:var(--fs-xs);color:var(--tx2);margin:12px 0 6px;">
              Incomparable previsto y declarado — ${noCompPrev} SKU
              (no descalifica el veredicto):</div>
            ${previstas.map(b => filaBodegaSinAlmacen(b, '#94a3b8')).join('')}` : '';

          panel.innerHTML = `
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:6px;">
              Cuadre por bodega (SKU que coinciden / SKU comparados):</div>
            ${porBodega.map(b => `
              <div class="tabla-fila" style="font-size:var(--fs-xs);">
                <div><div style="font-weight:600;">${esc(b.bodega)}</div>
                     <div style="color:var(--tx3);">${b.comparable ? (b.discrepancias + ' diferencia(s)') : b.motivo}</div></div>
                <div style="text-align:right;">
                  <span style="color:${b.comparable ? (b.cuadre_pct === 100 ? 'var(--ok-tx)' : 'var(--warn-tx)') : (b.esperado ? 'var(--tx2)' : 'var(--orange)')};font-weight:700;">
                    ${b.comparable ? (b.cuadran + '/' + b.denominador + ' · ' + (b.cuadre_pct === null ? '—' : b.cuadre_pct + '%'))
                      : (b.esperado ? 'no comparable (previsto)' : 'no comparable')}</span>
                  <div style="color:var(--tx3);font-size:var(--fs-xs);">WMS ${esc(b.unidades_wms)} und · Siesa ${esc(b.unidades_siesa)} und</div>
                </div>
              </div>`).join('')}
            ${bloqueNoComparable}
            ${bloquePrevisto}
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin:12px 0 6px;">
              Cobertura de catálogo: ${esc(r.cobertura_pct)}% (${esc(r.total_productos_wms)} productos con stock en el WMS
              de ${esc(r.total_productos_siesa)} que Siesa reporta)</div>
            ${r.total_discrepancias ? `
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin:12px 0 6px;">Top diferencias (WMS vs Siesa):</div>
            ${r.discrepancias.slice(0,20).map(x => `
              <div class="tabla-fila" style="font-size:var(--fs-xs);">
                <div>
                  <div style="font-weight:600;">${esc(x.nombre)}</div>
                  <div style="color:var(--tx3);">${esc(x.codigo)} · ${esc(x.bodega || '—')}</div>
                </div>
                <div style="text-align:right;">
                  <span style="color:${x.diferencia > 0 ? 'var(--ok-tx)' : 'var(--err-tx)'}">WMS: ${esc(x.stock_wms)}</span>
                  <span style="color:var(--tx3);margin:0 4px;">·</span>
                  <span style="color:var(--info-tx);">Siesa: ${esc(x.stock_siesa)}</span>
                  <div style="color:${x.diferencia > 0 ? 'var(--ok-tx)':'var(--err-tx)'};font-size:var(--fs-xs);">${x.diferencia > 0 ? '+' : ''}${esc(x.diferencia)}</div>
                </div>
              </div>`).join('')}` : ''}`;
        } else { res.textContent = '⏳ Comparando WMS vs Siesa...'; }
      } catch(err) { clearInterval(iv); res.style.color = 'var(--err-tx)'; res.textContent = 'Error polling'; }
    }, 8000);
  } catch(e) { res.style.color = 'var(--err-tx)'; res.textContent = 'Error: ' + (e.message || e); }
}



/** Generate an LPN label for an unlabeled pack detected during picking. */

// ─── Quagga2 — rearme por hueco de encuadre + techo de debounce ───────────────
/**
 * Se dispara en CADA frame que Quagga procesa, haya o no una decodificación
 * — a diferencia de onDetected, que solo dispara cuando SÍ decodificó algo.
 * Es lo que permite saber "¿el código sigue en cuadro?" en vez de adivinarlo
 * con un timer. Mientras haya algo detectado (aunque no pase el filtro de
 * confianza de _onQuaggaDetect — acá basta con que algo esté en cuadro) el
 * hueco no arranca; apenas un frame no ve nada, empieza a contar, y solo
 * tras _SCAN_REARME_GAP_MS consecutivos sin nada se rearma el escaneo.
 * @param {Object} result - Resultado de Quagga2 para este frame (puede no traer codeResult).
 */
function _onQuaggaProcessed(result) {
  const hayCodigo = !!(result && result.codeResult && result.codeResult.code);
  if (hayCodigo) {
    _SCAN_SIN_DETECCION_DESDE = null;
    return;
  }
  const ahora = Date.now();
  if (_SCAN_SIN_DETECCION_DESDE === null) _SCAN_SIN_DETECCION_DESDE = ahora;
  if (!_SCAN_ARMADO && ahora - _SCAN_SIN_DETECCION_DESDE >= _SCAN_REARME_GAP_MS) {
    _SCAN_ARMADO = true;
  }
}

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
  // Armado (hubo un hueco real de encuadre) → acepta ya, sin esperar el
  // techo. Si no, el techo sigue protegiendo por si onProcessed no rearmó.
  if (!_SCAN_ARMADO && now - _SCAN_LAST_TS < _SCAN_DEBOUNCE_MS) return;

  _SCAN_LAST_TS = now;
  _SCAN_ARMADO = false;
  _SCAN_SIN_DETECCION_DESDE = null;
  vibrar();
  if (_QUAGGA_CB) _QUAGGA_CB(code);
}

/** Stop Quagga2 barcode scanner and remove detection listener. */
async function _quaggaStop() {
  if (!window.Quagga) return;
  try { Quagga.offDetected(_onQuaggaDetect); } catch (_) {}
  try { Quagga.offProcessed(_onQuaggaProcessed); } catch (_) {}
  try { Quagga.stop(); } catch (_) {}
}

/**
 * Open camera barcode scanner using Quagga2.
 * @param {string} [lectorDivId='lector-qr'] - ID of the video container div.
 * @param {string} [boxDivId='camara-box'] - ID of the wrapper div to show/hide.
 * @param {Function|null} [onScan=null] - Callback on successful scan; defaults to procesarScan.
 */
async function abrirCamara(lectorDivId = 'lector-qr', boxDivId = 'camara-box', onScan = null, btnEl = null) {
  // Cerrar cámara previa si hay alguna
  if (_QUAGGA_BOX) await cerrarCamara(_QUAGGA_BOX);

  const box    = document.getElementById(boxDivId);
  const target = document.getElementById(lectorDivId);
  if (!box || !target) return;

  // El botón "Escanear con cámara" se oculta mientras la cámara está abierta
  // — en móvil, con la vista previa ocupando el ancho de pantalla, el botón
  // quedaba flotando arriba del video sin ninguna función. cerrarCamara() lo
  // restaura, sin importar por qué camino se cerró (botón "Cerrar cámara" o
  // un scan que la cierra solo).
  if (btnEl) btnEl.style.display = 'none';
  _QUAGGA_BTN = btnEl;

  box.style.display = 'block';
  CAMARA_ACTIVA = true;
  _QUAGGA_BOX = boxDivId;
  _QUAGGA_CB  = onScan || procesarScan;
  _SCAN_LAST_TS = 0;
  _SCAN_ARMADO = true;
  _SCAN_SIN_DETECCION_DESDE = null;

  if (!window.Quagga) {
    // Vendorizada localmente (igual que JsBarcode) y cacheada por sw.js —
    // antes se descargaba de cdn.jsdelivr.net en caliente, un origen externo
    // que el propio service worker rechaza cachear, así que la primera
    // apertura de cámara con wifi caída fallaba sin ningún aviso.
    try {
      await loadScript('/static/vendor/quagga2.min.js?v=1.8.2');
    } catch (e) {
      CAMARA_ACTIVA = false;
      _QUAGGA_BOX = null;
      _QUAGGA_CB = null;
      if (_QUAGGA_BTN) { _QUAGGA_BTN.style.display = ''; _QUAGGA_BTN = null; }
      box.style.display = 'none';
      alerta('No se pudo activar la cámara — usa el ingreso manual del código', 'error');
      return;
    }
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
        const nombre = err.name || (err.message && /NotAllowed|Permission denied/i.test(err.message) ? 'NotAllowedError' : '');
        let msg;
        if (nombre === 'NotAllowedError') {
          msg = 'Cámara bloqueada — habilita el permiso de cámara para este sitio en el navegador y vuelve a intentar';
        } else if (nombre === 'NotFoundError' || nombre === 'OverconstrainedError') {
          msg = 'No se encontró una cámara disponible en este dispositivo';
        } else if (nombre === 'NotReadableError') {
          msg = 'La cámara está siendo usada por otra app — ciérrala e intenta de nuevo';
        } else {
          msg = 'No se pudo activar la cámara — usa el ingreso manual del código';
        }
        alerta(msg, 'error');
        cerrarCamara(boxDivId);
        resolve(); return;
      }
      Quagga.onDetected(_onQuaggaDetect);
      try { Quagga.onProcessed(_onQuaggaProcessed); } catch (_) {}
      Quagga.start();

      // Estilar video insertado por Quagga + agregar visor rectangular
      const video = target.querySelector('video');
      if (video) {
        video.style.cssText = 'width:100%;height:260px;object-fit:cover;display:block;border-radius:10px;';
        // Forzado explícito — iOS Safari puede ignorar el atributo si Quagga
        // solo lo fija vía propiedad JS después de insertar el <video>.
        video.setAttribute('playsinline', '');
        video.setAttribute('muted', '');
        video.playsInline = true;
        video.muted = true;
      }
      const cvs = target.querySelector('canvas');
      if (cvs) cvs.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;';
      target.style.position = 'relative';
      target.style.overflow = 'hidden';
      target.style.borderRadius = '10px';
      target.style.touchAction = 'none'; // evita que un pellizco sobre el visor dispare el zoom global de la página

      // Linterna para poca luz (bodega/muelle de noche) — solo donde el
      // hardware la expone (Android/Chrome vía getUserMedia torch constraint).
      // iOS Safari no soporta esta capability desde web: sin el botón ahí,
      // no es un bug, es el navegador.
      try {
        const track = Quagga.CameraAccess.getActiveTrack();
        const caps = track && track.getCapabilities ? track.getCapabilities() : null;
        if (caps && caps.torch) {
          let torchOn = false;
          const btnTorch = document.createElement('button');
          btnTorch.type = 'button';
          btnTorch.textContent = '🔦';
          btnTorch.title = 'Linterna';
          btnTorch.style.cssText = 'position:absolute;top:8px;right:8px;z-index:5;width:44px;height:44px;border-radius:50%;border:none;background:#000000aa;color:var(--tx);font-size:var(--fs-lg);cursor:pointer;';
          btnTorch.onclick = async () => {
            torchOn = !torchOn;
            try {
              await track.applyConstraints({ advanced: [{ torch: torchOn }] });
              btnTorch.style.background = torchOn ? '#f59e0bcc' : '#000000aa';
            } catch (e) {
              console.error('Torch:', e);
            }
          };
          target.appendChild(btnTorch);
        }
      } catch (e) {
        // getActiveTrack/getCapabilities no soportados en este navegador — sin control de torch
      }

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
  if (_QUAGGA_BTN) { _QUAGGA_BTN.style.display = ''; _QUAGGA_BTN = null; }
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





// ── Auditorías urgentes ──────────────────────────────
// La lista vive en el tablero del líder (Inventario Cíclico → 🧭 Líder,
// `liderCargar` en conteo.js), y el KPI «Auditorías urgentes» del dashboard
// cuenta esa MISMA lista (`tablero_lider_conteo.contar_auditorias_urgentes`).
// Acá hubo un `cargarAuditoriasUrgentes()` que llamaba a
// `/api/conteo/auditorias-urgentes`, ruta que se borró el 2026-08-24 (0266118)
// y que un merge posterior resucitó del lado del JS: sin ruta, sin contenedor
// en el HTML y sin nadie que la llamara. Las tareas de PICKING bloqueadas se
// auditan en la pestaña Bodega (`auditoriaGuardar`).

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
 * Una hora de la API, mostrada en hora de Colombia.
 *
 * Lo que reemplaza: `ts.slice(0, 16).replace('T', ' ')`. Cortar el string no
 * convierte nada — mostraba UTC crudo, y un recibo de turno hecho a las 15:00
 * decía 20:00 (reportado 2026-08-03 sobre el TGZ653). Cinco horas de corrimiento
 * en el dato cuyo valor entero es ser confiable frente a un tercero.
 *
 * Se apoya en que la API declara la zona (`flota/api/_tiempo.py`). Sin esa `Z`,
 * `new Date()` interpreta la hora como local del teléfono y este helper
 * devolvería el mismo número equivocado — por eso el arreglo son las dos
 * mitades, no ésta sola.
 *
 * @param {string} iso - Timestamp ISO 8601 con zona.
 * @param {boolean} [conFecha=true] - Incluir la fecha además de la hora.
 * @returns {string} '03/08/2026 15:04', o '—' si no hay dato.
 */
function horaColombia(iso, conFecha = true) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  const opts = { timeZone: 'America/Bogota', hour: '2-digit', minute: '2-digit' };
  if (conFecha) Object.assign(opts, { day: '2-digit', month: '2-digit', year: 'numeric' });
  return d.toLocaleString('es-CO', opts).replace(',', '');
}

/**
 * Cómo se identifica a un conductor en pantalla cuando la cédula puede faltar.
 *
 * `RutaService.listar_conductores` **borra** cédula, teléfono y email para todo
 * rol que no sea admin o jefe de almacén. Eso no es un campo que se perdió: es
 * el guard de datos personales haciendo su trabajo. Por eso la corrección no es
 * pedir la cédula desde el frontend — es rendir lo que hay.
 *
 * Lo que sí no puede quedar ambiguo es un desplegable donde se elige **quién
 * queda responsable de un vehículo**. Si dos conductores activos comparten
 * nombre y no hay cédula para distinguirlos, se agrega el id, que no es dato
 * personal. Sin eso, Yesid podría asignarle la custodia al homónimo y el
 * registro diría el nombre correcto de la persona equivocada.
 *
 * Vive acá y no en `flota.js` porque el maestro de conductores de `rutas.js`
 * tiene el mismo agujero: escribía `CC undefined` para supervisor y gerente.
 * El mismo fallback en dos sitios diverge — ya pasó una vez y costó 25×.
 *
 * @param {object} c - Conductor tal como llega del API.
 * @param {Array}  [todos] - La lista completa, para detectar homónimos.
 * @returns {string} Sufijo de identidad, o '' si no hay nada que agregar.
 */
function identidadConductor(c, todos) {
  if (c.cedula) return `CC ${c.cedula}`;
  const homonimos = (todos || []).filter(o => o.nombre === c.nombre).length > 1;
  return homonimos ? `#${c.id}` : '';
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

/** Qué avisos no se van solos: los de error. */
const ALERTA_SE_QUEDA = ['error'];
/** Cuántos avisos fijos caben a la vez. */
const ALERTA_MAX_FIJAS = 3;

/**
 * Show a toast notification at the top of the screen.
 * @param {string} msg - Message text.
 * @param {string} [tipo='info'] - Type: exito, error, advertencia, or info.
 */
function alerta(msg, tipo = 'info') {
  const c = { exito: '#15803d', error: '#dc2626', advertencia: '#d97706', info: '#2563eb' }[tipo] || '#2563eb';
  // Una pila arriba: dos avisos seguidos ya no se tapan uno al otro.
  let pila = document.getElementById('alertas-pila');
  if (!pila) {
    pila = document.createElement('div');
    pila.id = 'alertas-pila';
    pila.style.cssText = 'position:fixed;top:20px;left:50%;transform:translateX(-50%);z-index:10000;display:flex;flex-direction:column;gap:8px;align-items:center;width:90%;max-width:520px;pointer-events:none;';
    document.body.appendChild(pila);
  }
  const texto = String(msg == null ? '' : msg);
  const d = document.createElement('div');
  d.setAttribute('role', tipo === 'error' ? 'alert' : 'status');
  d.style.cssText = `background:${c};color:#fff;padding:14px 22px;border-radius:12px;font-size:17px;font-weight:600;text-align:center;pointer-events:auto;box-shadow:0 4px 16px rgba(0,0,0,.35);`;
  const cuerpo = document.createElement('div');
  cuerpo.textContent = texto;
  d.appendChild(cuerpo);
  if (ALERTA_SE_QUEDA.includes(tipo)) {
    // Un error se queda hasta que alguien lo cierra (2026-09-25): a los 2,5 s
    // desaparecía el motivo de un rechazo antes de poder leerlo. El mismo
    // error repetido no se apila, y como mucho quedan tres.
    [...pila.children].filter(x => x.dataset && x.dataset.texto === texto).forEach(x => x.remove());
    d.dataset.texto = texto;
    const cerrar = document.createElement('button');
    cerrar.textContent = '✕ Cerrar';
    cerrar.style.cssText = 'margin-top:8px;background:rgba(255,255,255,.18);color:inherit;border:1px solid rgba(255,255,255,.5);border-radius:8px;padding:6px 14px;font-size:14px;font-weight:700;cursor:pointer;';
    cerrar.onclick = () => d.remove();
    d.appendChild(cerrar);
    const quedan = [...pila.children].filter(x => x.dataset && x.dataset.texto);
    if (quedan.length >= ALERTA_MAX_FIJAS) quedan[0].remove();
  } else {
    setTimeout(() => d.remove(), tipo === 'advertencia' ? 6000 : 2500);
  }
  pila.appendChild(d);
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
/** Abre un documento del servidor en una ventana para imprimir.
 *
 * Los documentos que se imprimen —factura, remisión— los sirve un endpoint que
 * exige JWT en un header, y **un `<a href target=_blank>` no manda headers**.
 * Ese enlace devuelve 401 siempre, y como nadie lo abre hasta el día que hay que
 * imprimir, pasa por bueno durante meses. Ya ocurrió con «ver foto» en flota.
 *
 * Una sola función para los tres documentos: el mismo bloque estaba copiado en
 * `packing.js` y dos veces en este archivo, y la tercera copia ya decía «error
 * al obtener la remisión» dentro de la función de la factura.
 */

/**
 * ¿Se puede pintar un código de barras ahora mismo?
 *
 * JsBarcode es el ÚNICO motor de código de barras del WMS: etiquetas de
 * producto, de ubicación, de LPN y de bulto salen todas de él. Venía de un CDN
 * externo que `sw.js` no cachea —sólo cachea el propio origen—, así que con la
 * red floja simplemente no estaba.
 *
 * Y los cuatro sitios que lo usaban lo llamaban dentro de `try {} catch (_) {}`.
 * El resultado no era un error: era una **etiqueta impresa sin código**, con su
 * texto legible y su membrete, indistinguible de una buena hasta que alguien la
 * pasa por el láser. Un bulto sin código no entra al manifiesto.
 *
 * @returns {boolean}
 */
function hayMotorDeCodigoBarras() {
  return typeof JsBarcode === 'function';
}

/**
 * Guard para TODA función que imprime etiquetas. Llamar ANTES de armar el
 * `#print-area`: si no hay motor, no se imprime nada.
 *
 * Es preferible no imprimir a imprimir una etiqueta muda — la etiqueta muda se
 * pega en la caja y el problema aparece tres días después, en la ruta.
 *
 * @param {string} que - qué se iba a imprimir, para el mensaje.
 * @returns {boolean} true si se puede seguir.
 */
function puedeImprimirEtiquetas(que) {
  if (hayMotorDeCodigoBarras()) return true;
  alerta(
    `No se pueden imprimir ${que}: no cargó el generador de códigos de barras. ` +
    `Recargá la página (Ctrl+F5). Si sigue, avisá a sistemas — imprimir sin ` +
    `código deja una etiqueta que el láser no lee.`, 'error');
  return false;
}

/**
 * Pinta un código de barras. Devuelve `false` si no pudo, en vez de callarse.
 *
 * @param {string} selector - selector CSS del `<svg>`.
 * @param {string} valor - lo que codifica.
 * @param {Object} [opciones] - se mezclan sobre CODE128.
 * @returns {boolean}
 */
function pintarCodigoBarras(selector, valor, opciones) {
  if (!hayMotorDeCodigoBarras() || !valor) return false;
  try {
    JsBarcode(selector, valor, Object.assign(
      { format: 'CODE128', displayValue: false, height: 55, margin: 0 },
      opciones || {}));
    return true;
  } catch (e) {
    console.error('[BARCODE] no se pudo pintar', selector, valor, e);
    return false;
  }
}

async function imprimirDocumento(url, que) {
  try {
    const res = await fetch(API + url, { headers: { Authorization: 'Bearer ' + TOKEN } });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      alerta(d.error || `No se pudo generar ${que}`, 'error');
      return false;
    }
    const html = await res.text();
    const ventana = window.open('', '_blank');
    if (!ventana) {
      alerta('El navegador bloqueó la ventana emergente — permití popups para este sitio',
             'advertencia');
      return false;
    }
    ventana.document.write(html);
    ventana.document.close();
    return true;
  } catch (e) {
    alerta(`Sin conexión al generar ${que}: ${e.message}`, 'error');
    return false;
  }
}

/** @param {number} packingId */
async function imprimirFacturaAdmin(packingId) {
  await imprimirDocumento(`/api/admin/factura/${packingId}`, 'la factura');
}

/** La remisión de un despacho ya confirmado por Siesa.
 *
 * El endpoint existía desde antes y **no lo llamaba ninguna pantalla**: la
 * remisión es el papel que viaja con el camión, se generaba bien y no había
 * botón. Es distinta de la factura — la remisión descarga inventario, la
 * factura cobra— y quien despacha necesita las dos.
 */
async function imprimirRemisionAdmin(packingId) {
  await imprimirDocumento(`/api/admin/remision/${packingId}`, 'la remisión');
}

// ADMIN — Facturar remisión existente (carril de recuperación 142943)
// ─────────────────────────────────────────────────────────────

/** @param {number} packingId - Packing ID to generate FE from an existing Siesa remision (recovery lane). */
async function facturarRemisionExistente(packingId) {
  if (!(await _modalConfirmar('Se genera la factura electrónica en Siesa desde la remisión que ya existe.',
      { titulo: '¿Facturar la remisión detectada?', textoConfirmar: 'Facturar' }))) return;
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
  if (!(await _modalConfirmar(`${esc(itemsValidos.length)} productos · ${esc(totalUds)} uds → ${esc(pedido.cliente || 'cliente')}`,
      { titulo: `¿Aprobar el pedido ${esc(pedido.numero_pedido)}?`, textoConfirmar: 'Aprobar' }))) return;

  try {
    const r = await post('/api/siesa/iniciar-despacho', {
      numero_pedido: pedido.numero_pedido,
      tipo_docto: pedido.tipo_docto,
      consec_docto: pedido.consec_docto,
      co: pedido.centro_op,
      almacen_id: ALMACEN_ID,
      items: itemsValidos
    });
    if (r.error) { alerta(r.error, 'error'); setTimeout(cargarPedidos, 800); return; }
    if (r.errores && r.errores.length) {
      console.warn(`[DESPACHO] ${pedido.numero_pedido} — ${r.errores.length} línea(s) sin stock, excluidas de picking y packing:`, r.errores);
      alerta(`Pedido aprobado — ${r.errores.length} línea(s) sin stock quedaron fuera (pedido parcial). Detalle en consola.`, 'advertencia');
    } else {
      alerta(`Pedido aprobado — Packing ${r.packing_codigo}`, 'exito');
    }
    setTimeout(cargarPedidos, 800);
  } catch (e) {
    // Un 409 de cartera trae el motivo: mostrarlo, no «Error aprobando».
    alerta(e.message || 'Error aprobando pedido', 'error');
    setTimeout(cargarPedidos, 800);
  }
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

// 'NB1' se mantiene en este mapa a propósito (tests/test_bodegas_coherentes.py
// exige que todo mapa de nombres liste las 9 bodegas operadas, sin excepción,
// para no repetir el bug de mapas divergentes de 2026-08-10/14) aunque
// `cargarUsuarios()` ya no la consulte — ver el comentario junto a `clave`
// más abajo. Etiquetada igual que la pestaña con la que se fusiona.
const _USR_NOMBRES_BOD = {
  'NB1':'Centro de Distribución','NC1':'Neiva Centro','NS1':'Neiva Sur Principal',
  'NS2':'Neiva Sur Fundación (parqueo licitaciones)',
  'FC1':'Florencia Centro','PC1':'Pitalito Centro','PT1':'Pitalito Terminal',
  'FF1':'Feria Florencia','FN1':'Santa Lucía Plaza','FP1':'Feria Pitalito',
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
      el.innerHTML = '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin usuarios</div>';
      return;
    }
    const grupos = {};
    usuarios.forEach(u => {
      // 'NB1' explícito y sin bodega_siesa_id son la MISMA bodega (Centro de
      // Distribución/Bodega Neiva, la única que tiene almacén NB1 en la BD) —
      // antes de 2026-09-09 caían en pestañas separadas ("Centro de
      // Distribución (NB1)" vs "Bodega Principal (NB1)") solo porque unos
      // usuarios (ej. Recepcionista, que sí lo necesita para filtrar
      // traslados) tienen el campo puesto a mano y el resto no. Unificar acá
      // no toca ningún dato — bodega_siesa_id sigue intacto para quien lo usa.
      const clave = (u.bodega_siesa_id && u.bodega_siesa_id !== 'NB1') ? u.bodega_siesa_id : '_CD';
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
              <div style="font-size:var(--fs-md);font-weight:700;">${esc(u.nombre)}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(u.email)}</div>
              <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;">
                <span style="font-size:var(--fs-xs);font-weight:600;color:${rolColor};background:var(--bg-input);padding:2px 8px;border-radius:8px;">${esc(u.rol)}</span>
                ${u.puede_picar ? `<span style="font-size:var(--fs-xs);font-weight:600;color:var(--info-tx);background:#1e3a5f;padding:2px 8px;border-radius:8px;">Picker</span>` : ''}
                ${u.puede_empacar ? `<span style="font-size:var(--fs-xs);font-weight:600;color:var(--lila-tx);background:var(--lila-bg);padding:2px 8px;border-radius:8px;">Empacador</span>` : ''}
                ${u.puede_abastecer ? `<span style="font-size:var(--fs-xs);font-weight:600;color:#fdba74;background:#7c2d12;padding:2px 8px;border-radius:8px;">Abastecedor</span>` : ''}
                ${u.puede_organizar_layout ? `<span style="font-size:var(--fs-xs);font-weight:600;color:var(--info-tx);background:#1e3a5f;padding:2px 8px;border-radius:8px;">Layout</span>` : ''}
              </div>
            </div>
            <button onclick="editarUsuario(${esc(u.id)})"
              style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);padding:6px 12px;border-radius:8px;font-size:var(--fs-xs);cursor:pointer;flex-shrink:0;">
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
    el.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:40px;">Error cargando usuarios</div>';
  }
}

/** Render user management sub-tabs and the list for the active group. */
function renderUsuariosTabsYLista() {
  const tabsEl = document.getElementById('usuarios-tabs');
  const el = document.getElementById('lista-usuarios');
  if (!tabsEl || !el) return;

  tabsEl.innerHTML = USUARIOS_GRUPOS.map(g =>
    `<div class="subtab${g.clave === USUARIOS_TAB_ACTIVA ? ' active' : ''}" onclick="usuariosCambiarTab('${esc(g.clave)}')">${esc(g.titulo)} · ${esc(g.count)}</div>`
  ).join('');

  const activo = USUARIOS_GRUPOS.find(g => g.clave === USUARIOS_TAB_ACTIVA);
  el.innerHTML = activo ? activo.html : '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin usuarios en esta bodega</div>';
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
    <div style="font-size:var(--fs-md);font-weight:700;margin-bottom:16px;">${u.id ? 'Editar usuario' : 'Nuevo usuario'}</div>
    <div style="display:flex;flex-direction:column;gap:12px;">
      <input id="u-nombre" placeholder="Nombre completo" value="${esc(u.nombre || '')}"
        style="padding:12px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);">
      <input id="u-email" placeholder="email@empresa.com" value="${esc(u.email || '')}" type="email" ${u.id ? 'readonly style="opacity:0.5;"' : ''}
        style="padding:12px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);">
      <input id="u-password" placeholder="${u.id ? 'Nueva contraseña (dejar vacío para no cambiar)' : 'Contraseña'}" type="password"
        style="padding:12px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);">
      <select id="u-rol" onchange="(function(v){var tr=['tienda','picker_traslado','packer_traslado','recepcionista'];document.getElementById('u-tienda-fields').style.display=tr.includes(v)?'block':'none';document.getElementById('u-conductor-fields').style.display=v==='conductor'?'block':'none';var canPicar=document.getElementById('u-puede-picar').checked;document.getElementById('u-conteo-wrapper').style.display=(canPicar&&!tr.includes(v))?'block':'none';})(this.value)"
        style="padding:12px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);">
        <option value="operario" ${(u.rol||'operario')==='operario'?'selected':''}>Operario (pedidos)</option>
        <option value="recepcionista" ${u.rol==='recepcionista'?'selected':''}>Recepcionista</option>
        <option value="conductor" ${u.rol==='conductor'?'selected':''}>Conductor</option>
        <option value="control_flota" ${u.rol==='control_flota'?'selected':''}>Control de flota</option>
        <option value="tienda" ${u.rol==='tienda'?'selected':''}>Tienda (punto de venta)</option>
        <option value="supervisor" ${u.rol==='supervisor'?'selected':''}>Supervisor</option>
        <option value="jefe_almacen" ${u.rol==='jefe_almacen'?'selected':''}>Jefe de almacén</option>
        <!-- gerente y empacador faltaban aca y el backend SI los acepta
             (_ROLES_VALIDOS en app/routes/auth.py). No eran roles teoricos:
             gerente tiene autoridad en cinco tuplas (GESTION, DESPACHO,
             COMPRAS_ROLES, LECTURA_FLOTA, VISTA_FLOTA), o sea que es quien
             DECIDE en flota, y no se podia crear desde ninguna pantalla.
             empacador esta en PACKING_ROLES.
             El trinquete tests/test_roles_creables.py cruza este desplegable
             contra _ROLES_VALIDOS para que no vuelvan a divergir. -->
        <option value="gerente" ${u.rol==='gerente'?'selected':''}>Gerente</option>
        <option value="empacador" ${u.rol==='empacador'?'selected':''}>Empacador</option>
        <option value="admin" ${u.rol==='admin'?'selected':''}>Admin</option>
        <optgroup label="── Traslados ──">
          <option value="picker_traslado" ${u.rol==='picker_traslado'?'selected':''}>Picker traslado</option>
          <option value="packer_traslado" ${u.rol==='packer_traslado'?'selected':''}>Packer traslado</option>
        </optgroup>
        <optgroup label="── Compras ──">
          <option value="compras" ${u.rol==='compras'?'selected':''}>Compras</option>
        </optgroup>
        <optgroup label="── Plata de la ruta y cartera ──">
          <option value="liquidador" ${u.rol==='liquidador'?'selected':''}>Liquidador (liquida rutas y registra cobros)</option>
          <option value="lider_cartera" ${u.rol==='lider_cartera'?'selected':''}>Líder de cartera (autoriza crédito y retenciones)</option>
        </optgroup>
      </select>
      <!-- Campos conductor (solo si rol=conductor) -->
      <div id="u-conductor-fields" style="display:${u.rol==='conductor'?'block':'none'};background:var(--bg-input);border:1px solid var(--info-brd);border-radius:8px;padding:14px;">
        <div style="font-size:var(--fs-xs);font-weight:700;color:var(--lila-tx);text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px;">Datos del conductor</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;">
          <div>
            <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:5px;">Cédula *</div>
            <input id="u-conductor-cedula" type="text" placeholder="12345678" value="${esc(u.conductor_cedula || '')}"
              style="width:100%;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);box-sizing:border-box;">
          </div>
          <div>
            <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:5px;">Teléfono</div>
            <input id="u-conductor-telefono" type="tel" placeholder="3001234567" value="${esc(u.conductor_telefono || '')}"
              style="width:100%;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);box-sizing:border-box;">
          </div>
        </div>
      </div>
      <!-- Campos tienda / picker_traslado / packer_traslado -->
      <div id="u-tienda-fields" style="display:${_TIENDA_ROLES.includes(u.rol)?'block':'none'};">
        <select id="u-bodega-siesa"
          onchange="(function(sel){const nombres={'NB1':'Bodega Principal','NC1':'Neiva Centro','NS1':'Neiva Sur Principal','FC1':'Florencia Centro','PC1':'Pitalito Centro','PT1':'Pitalito Terminal','FF1':'Feria Florencia','FN1':'Santa Lucía Plaza','FP1':'Feria Pitalito'};document.getElementById('u-nombre-pv').value=nombres[sel.value]||'';})(this)"
          style="width:100%;padding:12px;background:var(--bg-input);border:1px solid #f59e0b;border-radius:8px;color:var(--tx);font-size:var(--fs-sm);box-sizing:border-box;">
          <option value="">— Seleccionar bodega —</option>
          <option value="NB1" ${u.bodega_siesa_id==='NB1'?'selected':''}>NB1 — Bodega Principal</option>
          <option value="NC1" ${u.bodega_siesa_id==='NC1'?'selected':''}>NC1 — Neiva Centro</option>
          <option value="NS1" ${u.bodega_siesa_id==='NS1'?'selected':''}>NS1 — Neiva Sur Principal</option>
          <option value="FC1" ${u.bodega_siesa_id==='FC1'?'selected':''}>FC1 — Florencia Centro</option>
          <option value="PC1" ${u.bodega_siesa_id==='PC1'?'selected':''}>PC1 — Pitalito Centro</option>
          <option value="PT1" ${u.bodega_siesa_id==='PT1'?'selected':''}>PT1 — Pitalito Terminal</option>
          <option value="FF1" ${u.bodega_siesa_id==='FF1'?'selected':''}>FF1 — Feria Florencia</option>
          <option value="FN1" ${u.bodega_siesa_id==='FN1'?'selected':''}>FN1 — Santa Lucía Plaza</option>
          <option value="FP1" ${u.bodega_siesa_id==='FP1'?'selected':''}>FP1 — Feria Pitalito</option>
        </select>
        <input id="u-nombre-pv" type="hidden" value="${esc(u.nombre_punto_venta || '')}">
      </div>
      <div style="background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;padding:14px;">
        <div style="font-size:var(--fs-xs);font-weight:600;color:var(--tx2);margin-bottom:12px;text-transform:uppercase;letter-spacing:0.05em;">Capacidades operativas</div>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-picar" ${u.puede_picar!==false?'checked':''} style="width:20px;height:20px;accent-color:#60a5fa;" onchange="(function(cb){var tr=['tienda','picker_traslado','packer_traslado','recepcionista'];var v=document.getElementById('u-rol').value;document.getElementById('u-conteo-wrapper').style.display=(cb.checked&&!tr.includes(v))?'block':'none';})(this)">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:600;color:var(--info-tx);">Picker</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">Puede recoger productos del almacén</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-empacar" ${u.puede_empacar?'checked':''} style="width:20px;height:20px;accent-color:#c084fc;">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:600;color:var(--lila-tx);">Empacador / Auditor</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">Verifica y cierra cajas en mesa de empaque</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-abastecer" ${u.puede_abastecer?'checked':''} style="width:20px;height:20px;accent-color:#f97316;">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:600;color:var(--orange);">Abastecedor</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">Puede mover pacas de zona RESERVA a zona PICKING</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-organizar-layout" ${u.puede_organizar_layout?'checked':''} style="width:20px;height:20px;accent-color:#60a5fa;">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:600;color:var(--info-tx);">Organiza Layout</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">Puede crear ubicaciones y registrar SKU en Layout (no editar/eliminar/reclasificar)</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;margin-bottom:10px;">
          <input type="checkbox" id="u-puede-autorizar-cartera" ${u.puede_autorizar_cartera?'checked':''} style="width:20px;height:20px;">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:600;color:var(--warn-tx);">Autoriza cartera</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">Puede dejar salir a crédito un pedido retenido por mora o cupo, con motivo (respaldo del Gestor de Cartera). Vale para admin, supervisor, jefe de almacén y gerente; el líder de cartera ya lo tiene por su rol</div>
          </div>
        </label>
        <label style="display:flex;align-items:center;gap:12px;cursor:pointer;">
          <input type="checkbox" id="u-puede-camara" ${u.puede_usar_camara!==false?'checked':''} style="width:20px;height:20px;accent-color:#34d399;">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:600;color:var(--ok-tx);">Usar cámara para escanear</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">Muestra botón de cámara en picking y recepción</div>
          </div>
        </label>
        <div id="u-conteo-wrapper" style="margin-top:14px;padding-top:14px;border-top:1px solid var(--brd);display:${(u.puede_picar!==false && !_TIENDA_ROLES.includes(u.rol))?'block':'none'};">
          <label style="font-size:var(--fs-xs);color:var(--tx2);display:block;margin-bottom:6px;">Conteos cíclicos por día (0 = sin límite)</label>
          <input id="u-capacidad-conteo" type="number" min="0" max="200" step="1"
            value="${u.capacidad_diaria_conteo ?? 15}"
            style="width:100%;padding:10px;background:var(--bg-s);border:1px solid var(--brd);color:var(--tx);border-radius:8px;font-size:var(--fs-sm);box-sizing:border-box;">
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">Máximo de conteos intercalados que el sistema le asigna en un turno. Recomendado: 15–25.</div>
        </div>
      </div>
      <div style="display:flex;gap:8px;">
        <button onclick="_guardarUsuario(${esc(u.id || 'null')})"
          style="flex:1;padding:14px;background:#fff;color:#000;border:none;border-radius:10px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">
          ${u.id ? 'Guardar cambios' : 'Crear usuario'}
        </button>
        <button onclick="ocultarFormUsuario()"
          style="padding:14px 18px;background:var(--bg-s2);color:var(--tx);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-sm);cursor:pointer;">
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
  const puedeOrganizarLayout = document.getElementById('u-puede-organizar-layout')?.checked || false;
  const puedeAutorizarCartera = document.getElementById('u-puede-autorizar-cartera')?.checked || false;
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
    puede_abastecer: puedeAbastecer, puede_organizar_layout: puedeOrganizarLayout,
    puede_autorizar_cartera: puedeAutorizarCartera,
    puede_usar_camara: puedeCamara,
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

// ═══════════════════════════════════════════════════════════════════════════
// RECUPERACIÓN SIESA — las herramientas que existían y nadie podía alcanzar
//
// Nueve endpoints de recuperación estaban construidos, probados y desplegados
// SIN UN SOLO GESTO que los disparara. El día que Siesa falle, la persona que
// necesita reintentar un despacho o resolver un job colgado tenía que abrir
// una terminal y armar un curl con un JWT.
//
// Una capacidad de recuperación que solo se alcanza por curl no existe cuando
// hace falta: hace falta justo el día en que nadie tiene tiempo de armar un
// curl. Van acá, en la pestaña donde alguien mira cuando algo se rompe.
// ═══════════════════════════════════════════════════════════════════════════

/** Estado de la cola y de los sincronizadores. Lo primero que se mira. */
async function siesaRecuperacionCargar() {
  const el = document.getElementById('siesa-recuperacion');
  if (!el) return;
  el.innerHTML = '<div style="padding:14px;color:var(--tx3);">Consultando…</div>';

  // Se piden en paralelo y CADA UNO declara si falló. Un panel de recuperación
  // que se cae entero porque una consulta falla es inútil justo cuando importa.
  const [monitor, fallidos] = await Promise.all([
    get('/api/siesa/monitor').catch(e => ({ _error: e.message })),
    get('/api/siesa/jobs-fallidos').catch(e => ({ _error: e.message })),
  ]);

  const bloque = (titulo, datos, render) => datos._error
    ? `<div class="tabla-fila"><span class="tabla-nombre">${titulo}</span>
         <span style="color:var(--red);font-size:var(--fs-xs);">no se pudo consultar: ${esc(datos._error)}</span></div>`
    : render(datos);

  // Los cuatro semáforos de sincronización. `/api/siesa/monitor` los calculaba
  // desde siempre y `modulos` **no se pintaba en ninguna pantalla del PWA** —
  // una luz encendida en un tablero que nadie ve. Van acá, que es la pestaña
  // donde alguien mira cuando algo se rompió.
  const COLOR_SEMAFORO = {
    VERDE: 'var(--green)', AMARILLO: '#f59e0b',
    ROJO: 'var(--red)', GRIS: 'var(--tx3)',
  };
  const filaModulo = (nombre, m) => {
    const d = m.detalle || {};
    // El motivo, al lado del color: un rojo sin motivo manda a leer logs, que
    // es justo de donde este panel existe para sacar a alguien.
    const nota = d.ultimo_error
      ? `<span style="color:var(--red);">${String(d.ultimo_error).slice(0, 80)}</span>`
      : (m.estado === 'GRIS' ? 'nunca corrió en este proceso' : (d.ultimo_inicio || ''));
    return `
      <div class="tabla-fila">
        <span class="tabla-nombre" style="font-size:var(--fs-xs);">${nombre}<br>
          <span style="color:var(--tx3);font-size:var(--fs-xs);">${nota}</span></span>
        <span style="color:${esc(COLOR_SEMAFORO[m.estado] || 'var(--tx3)')};font-weight:700;font-size:var(--fs-xs);">
          ${esc(m.estado)}</span>
      </div>`;
  };

  el.innerHTML = `
    <div class="tabla-card">
      <div class="tabla-titulo">Recuperación Siesa</div>
      <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px;">
        Herramientas para cuando algo no llegó a Siesa. Todas dejan registro con
        tu nombre.
      </p>
      ${bloque('Sincronizadores', monitor, d => Object.entries(d.modulos || {})
          .map(([nombre, m]) => filaModulo(nombre, m)).join(''))}
      ${bloque('Cola DLQ', monitor, d => `
        <div class="tabla-fila"><span class="tabla-nombre">Jobs pendientes</span>
          <span class="badge ${(d.pendientes||0) ? 'badge-yellow' : 'badge-green'}">${d.pendientes ?? '—'}</span></div>
        <div class="tabla-fila"><span class="tabla-nombre">Jobs fallidos</span>
          <span class="badge ${(d.fallidos||0) ? 'badge-red' : 'badge-green'}">${d.fallidos ?? '—'}</span></div>`)}
      ${bloque('Fallidos', fallidos, d => {
        const js = d.jobs || [];
        if (!js.length) return '<div class="tabla-fila"><span class="tabla-nombre">Sin jobs fallidos</span></div>';
        // `error_ultimo` (así lo manda `SiesaJob.to_dict`): esto leía
        // `ultimo_error`, que no existe, y el motivo del fallo salía vacío.
        return js.slice(0, 10).map(j => `
          <div class="tabla-fila" style="align-items:flex-start;">
            <span class="tabla-nombre" style="font-size:var(--fs-xs);">
              <b>${esc(j.tipo || '?')}</b> #${esc(j.id)} · ${esc((j.fecha_creacion || '').slice(0, 10))}<br>
              <span style="color:var(--tx3);font-size:var(--fs-xs);">${esc((j.error_ultimo || '').slice(0, 90))}</span>
            </span>
            <button class="btn-flota" style="flex:0 0 auto;"
                    onclick="siesaDescartarJob(${Number(j.id)})">Descartar</button>
          </div>`).join('');
      })}
      <button class="btn-flota" style="width:100%;margin-top:10px;"
              onclick="siesaDispararDLQ()">Procesar la cola ahora</button>
      <p style="font-size:var(--fs-xs);color:var(--tx3);margin:6px 0 0;">
        El cron la procesa cada 5 minutos. Esto la adelanta — no reintenta lo que
        ya agotó sus 3 intentos.
      </p>
    </div>

    <div class="tabla-card" style="margin-top:12px;">
      <div class="tabla-titulo">Resolver una tarea de packing</div>
      <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px;">
        Cuando el WMS cree que no se despachó y Siesa ya lo procesó, o al revés.
      </p>
      <input id="rec-packing-id" type="number" inputmode="numeric"
             placeholder="ID de la tarea de packing"
             style="width:100%;padding:8px;border-radius:8px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);">
      <!-- ORDEN POR RIESGO, no por frecuencia. Lo que solo LEE va arriba; lo
           que puede crear un documento fiscal duplicado va abajo y separado.
           Un panel de emergencia donde el botón peligroso queda al lado del
           inocuo se usa a las 6 p.m. con prisa. -->
      <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;">
        <button class="btn-flota" style="flex:1" onclick="siesaVerCompromisos()">
          ¿Por qué falló?</button>
        <button class="btn-flota" style="flex:1" onclick="siesaReconciliarPacking()">
          Reconciliar</button>
        <button class="btn-flota" style="flex:1" onclick="siesaVerRemision()">
          Ver remisión</button>
      </div>
      <p style="font-size:var(--fs-xs);color:var(--tx3);margin:6px 0 0;">
        Los tres solo <b>leen</b>. <b>Reconciliar</b> pregunta a Siesa si la
        factura ya existe y, si existe, marca la tarea como despachada — no crea
        nada. <b>¿Por qué falló?</b> trae los compromisos del pedido (paso
        244328): si las cantidades comprometidas no cuadran, ahí está la causa.
      </p>
      <div id="rec-resultado" style="margin-top:10px;"></div>

      <hr style="border:0;border-top:1px solid var(--brd);margin:14px 0 10px;">
      <div style="font-size:var(--fs-xs);font-weight:700;color:var(--red);margin-bottom:4px;">
        Crean documentos en Siesa</div>
      <p style="font-size:var(--fs-xs);color:var(--tx3);margin:0 0 8px;">
        Usar solo después de mirar arriba. Los dos pueden dejar una factura
        <b>duplicada</b> si el documento ya existía — y una factura duplicada se
        anula con una nota crédito, a mano, en contabilidad.
      </p>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        <input id="rec-rm-tipo" placeholder="Tipo RM (ej. RS)" maxlength="4"
               style="width:110px;padding:8px;border-radius:8px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);text-transform:uppercase;">
        <input id="rec-rm-consec" type="number" inputmode="numeric" placeholder="Consecutivo"
               style="flex:1;min-width:110px;padding:8px;border-radius:8px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);">
        <button class="btn-flota" style="flex:1;min-width:130px;border-color:var(--red);color:var(--red);"
                onclick="siesaFacturarRMManual()">Facturar esa RM</button>
      </div>
      <p style="font-size:var(--fs-xs);color:var(--tx3);margin:6px 0 0;">
        Para cuando la remisión SÍ existe en Siesa y el WMS no guardó su número:
        se busca en Siesa y se escribe acá. Crea la factura (142943) sobre esa RM.
      </p>
    </div>

    <div class="tabla-card" style="margin-top:12px;">
      <div class="tabla-titulo">Traslados trabados</div>
      <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px;">
        Un traslado que quedó a medias entre bodegas. Sin esto había que
        arreglarlo por consola.
      </p>
      <button class="btn-flota" style="width:100%;" onclick="siesaRecuperarPackingTraslados()">
        Recrear los packings que faltan</button>
      <p style="font-size:var(--fs-xs);color:var(--tx3);margin:6px 0 0;">
        Crea la tarea de packing de las solicitudes que quedaron en EN_PICKING o
        EN_PACKING sin ella. No toca Siesa: solo repara el WMS.
      </p>
      <div style="display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;">
        <input id="rec-traslado-id" type="number" inputmode="numeric"
               placeholder="ID de la solicitud"
               style="flex:1;min-width:130px;padding:8px;border-radius:8px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);">
        <button class="btn-flota" style="flex:1;min-width:150px;border-color:var(--red);color:var(--red);"
                onclick="siesaReintentarTraslado()">Requisición formal (174646)</button>
      </div>
      <p style="font-size:var(--fs-xs);color:var(--tx3);margin:6px 0 0;">
        <b>174646 NO es parte del flujo normal</b> — el traslado real usa 173076
        al despachar y 173079 al recibir. Esto es para cuando el consultor de
        Siesa pide una requisición previa. Solo en EN_PICKING o APROBADA.
      </p>
      <div id="rec-traslado-resultado" style="margin-top:10px;"></div>
    </div>`;
}

/** Diagnóstico: qué cantidades tiene comprometidas Siesa para ese pedido.
 *
 * Es el paso 244328 de la cadena, y cuando un despacho parcial falla, la causa
 * suele estar acá: lo comprometido no coincide con lo que se quiere despachar.
 * Va primero en el panel porque es lo único que responde POR QUÉ antes de que
 * alguien apriete algo que escribe.
 */
async function siesaVerCompromisos() {
  const id = parseInt(document.getElementById('rec-packing-id')?.value, 10);
  const out = document.getElementById('rec-resultado');
  if (!Number.isFinite(id)) { alerta('Poné el ID de la tarea', 'error'); return; }
  out.innerHTML = '<p style="color:var(--tx3);font-size:var(--fs-xs);">Preguntando a Siesa…</p>';
  try {
    const r = await get(`/api/despacho_parcial/${id}/compromisos`);
    const filas = r.compromisos || [];
    if (!filas.length) {
      out.innerHTML = `<p style="color:var(--yellow);font-size:var(--fs-xs);">
        Siesa no reporta compromisos para el pedido ${r.pedido || id}. Si el
        pedido existe, es que el paso 244328 nunca corrió.</p>`;
      return;
    }
    out.innerHTML = `<div style="font-size:var(--fs-xs);">
      <p style="margin:0 0 6px;color:var(--tx2);">Pedido <b>${esc(r.pedido || '')}</b> —
        ${esc(filas.length)} línea(s) comprometida(s) en Siesa:</p>
      ${filas.map(f => `<div class="tabla-fila">
          <span class="tabla-nombre">${esc(f.f120_referencia || '?')}</span>
          <span>${f.f400_cant_comprometida_1 ?? '—'}</span>
        </div>`).join('')}</div>`;
  } catch (e) {
    out.innerHTML = `<p style="color:var(--red);font-size:var(--fs-xs);">${esc(e.message)}</p>`;
  }
}

/** Factura una RM cuyo número se leyó en Siesa a mano. CREA un documento. */
async function siesaFacturarRMManual() {
  const id = parseInt(document.getElementById('rec-packing-id')?.value, 10);
  const tipo = (document.getElementById('rec-rm-tipo')?.value || '').trim().toUpperCase();
  const consec = parseInt(document.getElementById('rec-rm-consec')?.value, 10);
  const out = document.getElementById('rec-resultado');
  if (!Number.isFinite(id)) { alerta('Poné el ID de la tarea', 'error'); return; }
  if (!tipo || !Number.isFinite(consec)) {
    alerta('Faltan el tipo y el consecutivo de la remisión', 'error');
    return;
  }
  // La confirmación nombra el documento y la consecuencia. Un «¿estás seguro?»
  // pelado se contesta que sí sin leerlo.
  if (!(await _modalConfirmar(`Se va a crear la FACTURA en Siesa sobre la remisión ${esc(tipo)}-${esc(consec)} ` +
               `(tarea ${esc(id)}).

Si esa remisión ya estaba facturada, queda una ` +
               `factura DUPLICADA que hay que anular con nota crédito a mano.

` +
               `¿Verificó en Siesa que no tiene factura?`,
      { titulo: 'Crear la factura en Siesa', textoConfirmar: 'Sí, crear la factura', peligro: true }))) return;
  // El servidor exige el documento repetido y un motivo (queda en la bitácora):
  // la remisión la digitó una persona y el WMS no la puede verificar en Siesa.
  const motivo = await _modalTexto('Facturar sobre la remisión ' + tipo + '-' + consec,
    '¿Por qué se factura sobre esta remisión digitada a mano? (obligatorio — queda en la bitácora con su nombre)',
    { obligatorio: true });
  if (!motivo || !motivo.trim()) return;
  out.innerHTML = '<p style="color:var(--tx3);font-size:var(--fs-xs);">Facturando…</p>';
  try {
    const r = await post(`/api/despacho_parcial/${id}/facturar-rm-manual`,
                         { tipo_rm: tipo, consec_rm: consec,
                           confirmacion: `${tipo}-${consec}`, motivo: motivo.trim() });
    out.innerHTML = `<p style="color:var(--green);font-size:var(--fs-xs);">
      ${esc(r.mensaje || 'Factura creada')} ${r.consec_fe ? '· FE ' + r.consec_fe : ''}</p>`;
  } catch (e) {
    out.innerHTML = `<p style="color:var(--red);font-size:var(--fs-xs);">${esc(e.message)}</p>`;
  }
}


/** Repara traslados sin tarea de packing. Solo toca el WMS, no Siesa. */
async function siesaRecuperarPackingTraslados() {
  const out = document.getElementById('rec-traslado-resultado');
  out.innerHTML = '<p style="color:var(--tx3);font-size:var(--fs-xs);">Buscando traslados sin packing…</p>';
  try {
    const r = await post('/api/traslados/recuperar-packing', {});
    const n = r.creados ?? r.recuperados ?? 0;
    out.innerHTML = `<p style="color:${n ? 'var(--green)' : 'var(--tx2)'};font-size:var(--fs-xs);">
      ${n ? `${n} packing(s) recreado(s)` : 'No había traslados sin packing'}</p>`;
  } catch (e) {
    out.innerHTML = `<p style="color:var(--red);font-size:var(--fs-xs);">${esc(e.message)}</p>`;
  }
}

/** Dispara el 174646 sobre una solicitud. Fuera del flujo normal. */
async function siesaReintentarTraslado() {
  const id = parseInt(document.getElementById('rec-traslado-id')?.value, 10);
  const out = document.getElementById('rec-traslado-resultado');
  if (!Number.isFinite(id)) { alerta('Poné el ID de la solicitud', 'error'); return; }
  if (!await _modalConfirmar(
    `Se va a crear una REQUISICIÓN formal (174646) en Siesa para la solicitud ${id}.\n\n` +
    `Esto NO es parte del flujo normal de traslados. Solo hacelo si el consultor de Siesa lo pidió.`,
    { titulo: '¿Continuar?', peligro: true }
  )) return;
  out.innerHTML = '<p style="color:var(--tx3);font-size:var(--fs-xs);">Enviando…</p>';
  try {
    const r = await post(`/api/traslados/${id}/reintentar-siesa`, {});
    out.innerHTML = `<p style="color:var(--green);font-size:var(--fs-xs);">
      ${esc(r.mensaje || 'Requisición creada')} ${r.consecutivo ? '· ' + r.consecutivo : ''}</p>`;
  } catch (e) {
    out.innerHTML = `<p style="color:var(--red);font-size:var(--fs-xs);">${esc(e.message)}</p>`;
  }
}

/** Descarta un job FALLIDO con motivo. NO reenvía nada a Siesa (Regla 3):
 *  solo deja de contarlo como trabado. Queda en la bitácora con tu nombre. */
async function siesaDescartarJob(id) {
  const motivo = await _modalTexto('Descartar el envío',
    'Descartar NO reenvía nada: si el documento pudo haber llegado a Siesa, sigue ahí. ' +
    '¿Por qué se descarta? (queda en la bitácora)',
    { obligatorio: true, textoConfirmar: 'Descartar' });
  if (motivo === null) return;
  try {
    const r = await post(`/api/siesa/jobs/${Number(id)}/descartar`, { motivo: motivo.trim() });
    alerta(r.mensaje || 'Descartado', 'exito');
    siesaRecuperacionCargar();
  } catch (e) {
    alerta('No se pudo descartar: ' + e.message, 'error');
  }
}

/** Adelanta el ciclo de la cola. No fuerza nada: solo no espera los 5 minutos. */
async function siesaDispararDLQ() {
  try {
    const r = await post('/api/siesa/trigger-dlq', {});
    alerta(r.mensaje || 'Cola disparada — se procesa en segundo plano', 'exito');
    setTimeout(siesaRecuperacionCargar, 3000);
  } catch (e) {
    alerta('No se pudo disparar la cola: ' + e.message, 'error');
  }
}

/** Pregunta a Siesa si esa tarea ya tiene factura. NO crea documentos. */
async function siesaReconciliarPacking() {
  const id = parseInt(document.getElementById('rec-packing-id')?.value, 10);
  const out = document.getElementById('rec-resultado');
  if (!Number.isFinite(id)) { alerta('Poné el ID de la tarea', 'error'); return; }
  out.innerHTML = '<p style="color:var(--tx3);font-size:var(--fs-xs);">Preguntando a Siesa…</p>';
  try {
    const r = await post(`/api/packing/${id}/reconciliar`, {});
    out.innerHTML = `<p style="color:var(--green);font-size:var(--fs-xs);">
      ${esc(r.mensaje || 'Reconciliada')}</p>`;
  } catch (e) {
    out.innerHTML = `<p style="color:var(--red);font-size:var(--fs-xs);">${esc(e.message)}</p>`;
  }
}

/** La remisión de una tarea — para cotejar contra el documento físico. */
async function siesaVerRemision() {
  const id = parseInt(document.getElementById('rec-packing-id')?.value, 10);
  const out = document.getElementById('rec-resultado');
  if (!Number.isFinite(id)) { alerta('Poné el ID de la tarea', 'error'); return; }
  try {
    const r = await get(`/api/packing/${id}/remision`);
    out.innerHTML = `<pre style="font-size:var(--fs-xs);white-space:pre-wrap;color:var(--tx2);
      max-height:220px;overflow:auto;">${JSON.stringify(r, null, 2)}</pre>`;
  } catch (e) {
    out.innerHTML = `<p style="color:var(--red);font-size:var(--fs-xs);">${esc(e.message)}</p>`;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// MAPEO DE UNIDADES DE NEGOCIO — la pantalla que el sistema daba por hecha
//
// `siesa_sync_service` auto-inserta cada tipo de inventario nuevo que Siesa
// devuelve, con `unidad_negocio_id` vacío, y su comentario dice:
//
//     "El admin los verá en /api/config/mapeo-unidades y los completa con un
//      click"
//
// Ese click nunca existió. Y la consecuencia no es cosmética: aprobar un
// traslado LEVANTA si algún producto no tiene unidad de negocio —
//
//     "Productos sin Unidad de Negocio configurada: X.
//      Configura el mapeo en /api/config/mapeo-unidades y vuelve a aprobar."
//
// — o sea que el sistema sabe exactamente qué hay que hacer y manda a la
// persona a un endpoint que no puede abrir desde el navegador.
// ═══════════════════════════════════════════════════════════════════════════

async function mapeoUnidadesCargar() {
  const el = document.getElementById('mapeo-unidades');
  if (!el) return;
  el.innerHTML = '<div style="padding:14px;color:var(--tx3);">Cargando…</div>';

  const [mapeos, diag] = await Promise.all([
    get('/api/config/mapeo-unidades').catch(e => ({ _error: e.message })),
    get('/api/config/mapeo-unidades/tipos-sin-mapeo').catch(e => ({ _error: e.message })),
  ]);

  if (mapeos._error) {
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo cargar el mapeo: ${esc(mapeos._error)}</div>`;
    return;
  }

  const lista = Array.isArray(mapeos) ? mapeos : [];
  // Los vacíos primero: son los que bloquean traslados AHORA.
  const pendientes = lista.filter(m => !(m.unidad_negocio_id || '').trim());
  const completos = lista.filter(m => (m.unidad_negocio_id || '').trim());

  const fila = (m) => {
    const vacio = !(m.unidad_negocio_id || '').trim();
    return `<div class="tabla-fila" style="align-items:center;gap:8px;">
      <span class="tabla-nombre" style="flex:1;font-size:var(--fs-sm);">
        <b>${esc(m.tipo_inv_siesa)}</b>
        <span style="display:block;font-size:var(--fs-xs);color:var(--tx3);">${esc(m.descripcion || '')}</span>
      </span>
      <input id="mu-${esc(m.id)}" value="${esc(m.unidad_negocio_id || '')}"
             placeholder="unidad" maxlength="10"
             style="width:90px;padding:5px;border-radius:6px;font-size:var(--fs-sm);
                    border:1px solid ${vacio ? 'var(--red)' : 'var(--brd)'};
                    background:var(--bg);color:var(--tx);">
      <button class="btn-flota" style="padding:4px 10px;font-size:var(--fs-xs);"
              onclick="mapeoUnidadesGuardar(${esc(m.id)})">Guardar</button>
    </div>`;
  };

  const bloqueados = diag._error ? null : (diag.total_sin_unidad_negocio || 0);

  el.innerHTML = `
    <div class="tabla-card">
      <div class="tabla-titulo">Unidades de negocio (Siesa)</div>
      <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px;">
        Cada tipo de inventario de Siesa necesita su unidad de negocio.
        <b>Siesa no la hereda de la bodega</b>, así que sin este mapeo un
        traslado no se puede aprobar.
      </p>

      ${pendientes.length ? `
        <div style="border-left:3px solid var(--red);padding:8px 10px;margin-bottom:10px;
                    background:var(--bg-s);border-radius:6px;">
          <b style="color:var(--red);font-size:var(--fs-sm);">
            ${esc(pendientes.length)} tipo(s) sin asignar</b>
          <p style="font-size:var(--fs-xs);color:var(--tx2);margin:4px 0 0;">
            El sync los descubrió solo. Mientras estén vacíos, cualquier traslado
            que incluya uno de sus productos falla al aprobar.
          </p>
        </div>
        ${pendientes.map(fila).join('')}
        <hr style="border-color:var(--brd);margin:12px 0;">
      ` : `<p style="font-size:var(--fs-xs);color:var(--green);margin-bottom:10px;">
             ✓ Todos los tipos tienen unidad asignada</p>`}

      ${completos.map(fila).join('')}

      ${bloqueados === null
        ? `<p style="font-size:var(--fs-xs);color:var(--tx3);margin-top:10px;">
             No se pudo consultar cuántos productos están sin unidad: ${esc(diag._error)}</p>`
        : bloqueados > 0
          ? `<p style="font-size:var(--fs-xs);color:var(--yellow);margin-top:10px;">
               <b>${bloqueados} producto(s) activos sin unidad de negocio.</b>
               No se pueden trasladar hasta que su tipo tenga mapeo.</p>`
          : `<p style="font-size:var(--fs-xs);color:var(--green);margin-top:10px;">
               Ningún producto activo quedó sin unidad.</p>`}
    </div>`;
}

/** Guarda una unidad. Vacío NO se acepta: sería volver al estado que bloquea. */
async function mapeoUnidadesGuardar(id) {
  const input = document.getElementById(`mu-${id}`);
  const valor = (input?.value || '').trim();
  if (!valor) {
    alerta('La unidad de negocio no puede quedar vacía — es lo que bloquea el traslado', 'error');
    return;
  }
  try {
    await put(`/api/config/mapeo-unidades/${id}`, { unidad_negocio_id: valor });
    alerta('Unidad guardada ✓', 'exito');
    mapeoUnidadesCargar();
  } catch (e) {
    alerta('No se pudo guardar: ' + e.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// ESTADO DE LOS SINCRONIZADORES
//
// Cinco endpoints reportaban el avance de sincronizaciones largas y NADIE los
// consultaba. Dos de esas syncs se disparan desde el PWA: la persona tocaba el
// botón y la pantalla no volvía a decir nada durante minutos.
//
// Un botón que no responde se toca dos veces — y en `sync-pedidos` eso es una
// segunda paginación completa contra Siesa mientras la primera sigue corriendo.
//
// Los otros tres corren por cron. Su estado importa igual, y por otra razón:
// es la única forma de saber si un cron dejó de correr. Un sincronizador
// muerto no avisa; simplemente los datos envejecen.
// ═══════════════════════════════════════════════════════════════════════════

let _SYNC_TIMER = null;

const _SYNCS = [
  { id: 'productos',   nombre: 'Catálogo de productos', url: '/api/siesa/sync-estado',                     cron: '30 min, 7-20h' },
  { id: 'pedidos',     nombre: 'Pedidos comprometidos', url: '/api/siesa/sync-pedidos-estado',             cron: 'cada minuto, 7-20h' },
  { id: 'inventario',  nombre: 'Carga de inventario',   url: '/api/siesa/carga-inventario-estado',         cron: 'manual' },
  { id: 'empaques',    nombre: 'Empaques',              url: '/api/empaques/sync/estado',                  cron: '2:30 a.m.' },
  { id: 'ubicaciones', nombre: 'Ubicaciones',           url: '/api/reposicion/sync-ubicaciones/estado',    cron: '3:00 a.m.' },
];

async function syncEstadosCargar() {
  const el = document.getElementById('sync-estados');
  if (!el) return;

  const datos = await Promise.all(_SYNCS.map(s =>
    get(s.url).then(d => ({ ...s, d })).catch(e => ({ ...s, _error: e.message }))));

  const enCurso = datos.some(x => x.d && x.d.en_curso);

  el.innerHTML = `
    <div class="tabla-card">
      <div class="tabla-titulo">Sincronizadores</div>
      <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px;">
        Cuándo corrió cada uno y cómo le fue. <b>Un sincronizador que deja de
        correr no avisa</b> — los datos simplemente envejecen.
      </p>
      ${datos.map(x => {
        if (x._error) return `<div class="tabla-fila">
          <span class="tabla-nombre" style="font-size:var(--fs-sm);">${esc(x.nombre)}</span>
          <span style="font-size:var(--fs-xs);color:var(--red);">no se pudo consultar</span></div>`;
        const d = x.d || {};
        const err = d.ultimo_error;
        const color = d.en_curso ? 'var(--yellow)' : err ? 'var(--red)' : 'var(--green)';
        const texto = d.en_curso ? 'corriendo…' : err ? 'falló' : 'ok';
        return `<div class="tabla-fila" style="align-items:flex-start;">
          <span class="tabla-nombre" style="font-size:var(--fs-sm);">
            ${x.nombre}
            <span style="display:block;font-size:var(--fs-xs);color:var(--tx3);">${x.cron}</span>
            ${err ? `<span style="display:block;font-size:var(--fs-xs);color:var(--red);">${String(err).slice(0,110)}</span>` : ''}
          </span>
          <span style="text-align:right;">
            <span style="font-size:var(--fs-xs);color:${color};font-weight:700;">${texto}</span>
            <span style="display:block;font-size:var(--fs-xs);color:var(--tx3);">
              ${d.ultimo_inicio ? horaColombia(d.ultimo_inicio) : 'sin registro'}</span>
          </span>
        </div>`;
      }).join('')}
    </div>`;

  // Mientras algo corre, se refresca solo. Cuando nada corre, se deja de
  // preguntar: un poll permanente es lo que infla la factura de red.
  clearInterval(_SYNC_TIMER);
  if (enCurso) _SYNC_TIMER = setInterval(syncEstadosCargar, 5000);
}


/** Auditoría de invariantes de frontera — `/api/auditoria/flujo`.
 *
 * Los mismos invariantes que corren en CI sobre un pedido sintético, acá
 * corridos sobre los datos reales. Si algo aparece acá y no allá, es un
 * problema de datos y no de código.
 *
 * Se pinta el CATÁLOGO completo, no solo lo roto: «0 hallazgos» y «no corrió
 * nada» se leen igual, y esa confusión es la que hace inútil un tablero.
 *
 * Y se pinta el UNIVERSO MIRADO (`consultas_truncadas`): «0 hallazgos sobre
 * todo» y «0 hallazgos sobre los primeros 20.000 movimientos» también se leen
 * igual, y ése es el tercer modo del mismo error.
 */
async function cargarAuditoriaFlujo() {
  const el = document.getElementById('auditoria-flujo');
  if (!el) return;

  // Mientras corre, decirlo. La auditoría tarda segundos y repinta un
  // resultado idéntico: sin esto, un botón que funciona **no se distingue de
  // uno muerto**. Es el mismo problema de «0 hallazgos vs no corrió», en la
  // pantalla en vez de en el reporte.
  const btn = el.querySelector('button');
  if (btn) { btn.disabled = true; btn.textContent = 'Auditando…'; }

  try {
    const d = await get('/api/auditoria/flujo');
    const sev = { BLOQUEA: '#dc2626', AVISA: '#f59e0b', OBSERVA: '#6b7280' };

    const filas = (d.resultados || []).map(r => {
      const roto = r.total > 0 || r.error;
      const color = roto ? (sev[r.severidad] || '#6b7280') : '#15803d';
      const detalle = r.error
        ? `<div style="color:var(--err-tx);font-size:var(--fs-xs);">no se pudo evaluar: ${esc(r.error)}</div>`
        : (r.hallazgos || []).slice(0, 5).map(h =>
            `<div style="font-size:var(--fs-xs);color:var(--tx3);padding-left:8px;">
               · <b>${esc(h.referencia)}</b> — ${esc(h.detalle)}</div>`).join('');
      return `
        <div style="padding:8px 0;border-bottom:1px solid var(--brd);">
          <div style="display:flex;align-items:center;gap:8px;">
            <span style="width:8px;height:8px;border-radius:50%;background:${color};flex:none;"></span>
            <b style="font-size:var(--fs-xs);">${esc(r.codigo)}</b>
            <span style="font-size:var(--fs-xs);color:var(--tx3);">${esc(r.frontera)}</span>
            <span style="margin-left:auto;font-size:var(--fs-xs);color:${color};font-weight:700;">
              ${r.error ? 'ERROR' : (r.total || 0)}</span>
          </div>
          ${roto ? `<div style="font-size:var(--fs-xs);color:var(--tx2);margin:4px 0 2px 16px;">${esc(r.consecuencia)}</div>` : ''}
          ${detalle}
          ${r.truncado ? '<div style="font-size:var(--fs-xs);color:var(--tx3);padding-left:8px;">(mostrando los primeros 100)</div>' : ''}
        </div>`;
    }).join('');

    const bloq = d.bloqueantes || 0;
    // La hora de ESTA corrida. Es lo único que hace visible que el botón hizo
    // algo cuando el resultado no cambió.
    const hora = new Date().toLocaleTimeString('es-CO');

    // El UNIVERSO MIRADO. `auditar()` declara qué consultas chocaron con su
    // tope y el panel no lo pintaba: con `movimientos_inventario` —la tabla
    // más grande, y la del tope más fácil de alcanzar (20.000)— truncada, el
    // «0 hallazgos» de arriba se lee como limpio cuando significa «no se
    // buscó en todo». Va arriba de las filas a propósito: es la advertencia de
    // cómo leer lo que sigue, no una nota al pie.
    const truncadas = d.consultas_truncadas || [];
    const bloqueTruncadas = truncadas.length ? `
      <div style="border:1px solid #f59e0b;border-radius:8px;padding:8px 10px;
                  margin-bottom:10px;font-size:var(--fs-xs);color:var(--tx2);">
        <b style="color:var(--warn-tx);">Universo parcial — no le creas al 0</b>
        <div style="margin-top:4px;">
          ${esc(truncadas.length)} consulta(s) chocaron con su tope de filas: los
          hallazgos de abajo salen de una muestra, no de todo. Subí el tope
          antes de dar esto por limpio.</div>
        ${truncadas.map(t => `<div style="padding-left:8px;">· <code>${t}</code></div>`).join('')}
      </div>` : '';

    el.innerHTML = `
      <div class="tabla-card">
        <div class="tabla-titulo">Auditoría de flujo
          <span style="font-size:var(--fs-xs);font-weight:400;color:var(--tx3);">
            · ${esc(d.invariantes_corridos)} invariantes · ${bloq} hallazgo(s) bloqueante(s)
            · <span title="hora de esta corrida">${hora}</span></span>
        </div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">${esc(d.nota || '')}</div>
        ${bloqueTruncadas}
        ${filas}
        <button onclick="cargarAuditoriaFlujo()"
          style="margin-top:10px;padding:8px 14px;border:none;border-radius:8px;cursor:pointer;background:var(--brd);color:var(--tx);font-size:var(--fs-xs);font-weight:700;">
          Volver a auditar
        </button>
      </div>`;
  } catch (e) {
    el.innerHTML = `<div class="tabla-card" style="color:var(--err-tx);">
      No se pudo correr la auditoría: ${e.message || e}
      <button onclick="cargarAuditoriaFlujo()"
        style="margin-left:10px;padding:6px 12px;border:none;border-radius:8px;cursor:pointer;">
        Reintentar</button></div>`;
  }
}


/** SKU donde el escaneo no distingue caja de unidad — el backlog del sync de EAN.
 *
 *  `factor > 1` y `codigo_barras_empaque` vacío: ahí `/producto/<codigo>`
 *  devuelve `es_empaque: null` y la pantalla del recepcionista PREGUNTA. Cada
 *  fila es una pregunta que alguien va a contestar varias veces al día, y
 *  cerrarla la elimina.
 */
async function skusSinEanEmpaque() {
  const el = document.getElementById('skus-sin-ean-empaque');
  if (el) el.textContent = 'Consultando...';
  try {
    const d = await get('/api/siesa/skus-sin-ean-empaque');
    if (!el) return;
    if (!d.total) { el.textContent = '✓ Ninguno: todo escaneo distingue caja de unidad.'; return; }
    const top = (d.skus || []).slice(0, 10)
      .map(s => `${s.codigo_siesa || s.codigo} · x${s.factor_conversion}${s.clasificacion_abc ? ' · ' + s.clasificacion_abc : ''}`)
      .join('<br>');
    el.innerHTML = `<b style="color:var(--warn-tx);">${esc(d.total)}</b> SKU sin EAN de empaque` +
      (d.truncado ? ' (lista truncada en 500)' : '') +
      `<div style="margin-top:6px;line-height:1.5;">${top}</div>` +
      (d.total > 10 ? `<div style="margin-top:4px;color:var(--tx3);">…y ${d.total - 10} más</div>` : '');
  } catch (e) {
    if (el) el.textContent = 'Error consultando el backlog de EAN';
  }
}
