// ============================================
// WMS PWA — Lógica principal
// Soporta cámara (ahora) y láser Bluetooth (después)
// ============================================

const API_BASE = window.location.origin;
let TOKEN = localStorage.getItem('wms_token');
let OPERARIO = JSON.parse(localStorage.getItem('wms_operario') || 'null');
let TAREA_ACTUAL = null;
let ITEMS_ESCANEADOS = [];
let COLA_OFFLINE = JSON.parse(localStorage.getItem('wms_cola_offline') || '[]');
let SCANNER_BUFFER = '';
let SCANNER_TIMEOUT = null;
let CAMARA_ACTIVA = false;
let HTML5QR = null;

// ============================================
// INICIALIZACIÓN
// ============================================
document.addEventListener('DOMContentLoaded', () => {
  registrarServiceWorker();
  monitorearConexion();

  if (TOKEN && OPERARIO) {
    mostrarPantalla('pantalla-tareas');
    cargarTareaActual();
  } else {
    mostrarPantalla('pantalla-login');
  }

  // Input invisible para escáner láser — siempre con focus
  inicializarScannerLaser();
});

// ============================================
// SERVICE WORKER
// ============================================
async function registrarServiceWorker() {
  if ('serviceWorker' in navigator) {
    try {
      await navigator.serviceWorker.register('/static/pwa/sw.js');
      navigator.serviceWorker.addEventListener('message', event => {
        if (event.data.tipo === 'SINCRONIZAR') {
          sincronizarColaOffline();
        }
      });
    } catch (e) {
      console.error('SW error:', e);
    }
  }
}

// ============================================
// ESCÁNER LÁSER — INPUT INVISIBLE CON FOCUS PERMANENTE
// ============================================
function inicializarScannerLaser() {
  const input = document.getElementById('scanner-input');
  if (!input) return;

  // Mantener focus siempre activo
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
      SCANNER_TIMEOUT = setTimeout(() => {
        SCANNER_BUFFER = '';
      }, 100);
    }
  });
}

// ============================================
// CONEXIÓN Y OFFLINE
// ============================================
function monitorearConexion() {
  const indicador = document.getElementById('conexion-status');

  const actualizar = () => {
    const online = navigator.onLine;
    if (indicador) {
      indicador.textContent = online ? '● Online' : '● Offline';
      indicador.style.color = online ? '#22c55e' : '#ef4444';
    }
    if (online && COLA_OFFLINE.length > 0) {
      sincronizarColaOffline();
    }
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
  } catch (e) {
    console.error('Error sincronizando:', e);
  }
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
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${TOKEN}`
    },
    body: JSON.stringify(body)
  });
  if (resp.status === 401) {
    cerrarSesion();
    throw new Error('Sesión expirada');
  }
  return resp.json();
}

async function apiGet(endpoint) {
  const resp = await fetch(API_BASE + endpoint, {
    headers: { 'Authorization': `Bearer ${TOKEN}` }
  });
  if (resp.status === 401) {
    cerrarSesion();
    throw new Error('Sesión expirada');
  }
  return resp.json();
}

// ============================================
// LOGIN
// ============================================
async function login() {
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value.trim();

  if (!email || !password) {
    mostrarAlerta('Ingresa tu usuario y contraseña', 'error');
    return;
  }

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
      mostrarPantalla('pantalla-tareas');
      cargarTareaActual();
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

// ============================================
// TAREAS
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
          <button onclick="cargarTareaActual()" style="margin-top:32px;padding:16px 32px;font-size:18px;background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;">
            Actualizar
          </button>
        </div>`;
      return;
    }

    TAREA_ACTUAL = data;
    ITEMS_ESCANEADOS = [];
    renderizarTarea(data);

  } catch (e) {
    mostrarAlerta('Error cargando tareas', 'error');
  }
}

function renderizarTarea(tarea) {
  const colores = {
    'PICKING': '#1d4ed8',
    'PACKING': '#7c3aed',
    'CONTEO': '#b45309'
  };

  const color = colores[tarea.tipo] || '#000';
  const esConteo = tarea.tipo === 'CONTEO';
  const cantidadTexto = esConteo ? '?' : `${tarea.cantidad_escaneada} / ${tarea.cantidad_requerida}`;

  document.getElementById('contenido-tarea').innerHTML = `
    <div style="padding:16px;">

      <!-- Tipo de tarea -->
      <div style="background:${color};color:#fff;border-radius:12px;padding:10px 16px;
                  font-size:20px;font-weight:700;text-align:center;margin-bottom:16px;">
        ${tarea.tipo}
      </div>

      <!-- Datos principales en letras grandes -->
      <div style="background:#000;color:#fff;border-radius:16px;padding:20px;margin-bottom:16px;">
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
        <div style="font-size:64px;font-weight:900;" id="contador-cantidad">${cantidadTexto}</div>
        <div style="height:8px;background:#333;border-radius:4px;margin-top:12px;">
          <div id="barra-progreso" style="height:100%;background:#22c55e;border-radius:4px;
               width:${tarea.cantidad_requerida ? (tarea.cantidad_escaneada/tarea.cantidad_requerida*100) : 0}%;
               transition:width 0.3s;"></div>
        </div>
      </div>` : `
      <div style="background:#1a1a1a;color:#fff;border-radius:16px;padding:20px;margin-bottom:16px;text-align:center;">
        <div style="font-size:14px;color:#999;margin-bottom:4px;">CONTEO CIEGO</div>
        <div style="font-size:64px;font-weight:900;" id="contador-cantidad">0</div>
        <div style="font-size:14px;color:#666;margin-top:8px;">Cuenta sin ver la cantidad esperada</div>
      </div>`}

      <!-- Escanear con cámara -->
      <div style="margin-bottom:12px;">
        <button onclick="toggleCamara()" style="width:100%;padding:16px;font-size:18px;
                background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;">
          📷 Escanear con cámara
        </button>
      </div>

      <div id="camara-container" style="display:none;margin-bottom:12px;">
        <div id="lector-qr" style="border-radius:12px;overflow:hidden;"></div>
        <button onclick="toggleCamara()" style="width:100%;padding:12px;margin-top:8px;
                font-size:16px;background:#333;color:#fff;border:none;border-radius:12px;cursor:pointer;">
          Cerrar cámara
        </button>
      </div>

      <!-- Botón confirmar -->
      <button id="btn-confirmar" onclick="confirmarTarea()"
              style="width:100%;padding:20px;font-size:22px;font-weight:700;
                     background:#000;color:#fff;border:none;border-radius:16px;
                     cursor:pointer;opacity:${esConteo ? 1 : 0.3};"
              ${esConteo ? '' : 'disabled'}>
        ✓ Confirmar
      </button>

      ${tarea.referencia ? `
      <div style="text-align:center;margin-top:12px;font-size:13px;color:#666;">
        Ref: ${tarea.referencia}
      </div>` : ''}

    </div>`;
}

// ============================================
// ESCANEO CON CÁMARA
// ============================================
async function toggleCamara() {
  const container = document.getElementById('camara-container');

  if (CAMARA_ACTIVA) {
    if (HTML5QR) {
      await HTML5QR.stop();
      HTML5QR = null;
    }
    CAMARA_ACTIVA = false;
    container.style.display = 'none';
    return;
  }

  container.style.display = 'block';
  CAMARA_ACTIVA = true;

  // Cargar librería de escaneo si no está cargada
  if (!window.Html5Qrcode) {
    await cargarScript('https://cdnjs.cloudflare.com/ajax/libs/html5-qrcode/2.3.8/html5-qrcode.min.js');
  }

  HTML5QR = new Html5Qrcode('lector-qr');

  try {
    await HTML5QR.start(
      { facingMode: 'environment' },
      { fps: 10, qrbox: { width: 250, height: 150 } },
      (codigo) => {
        procesarCodigoEscaneado(codigo);
      },
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
    s.src = src;
    s.onload = resolve;
    s.onerror = reject;
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
      mostrarAlerta(
        typeof resp.error === 'object' ? resp.error.mensaje : resp.error,
        'error'
      );
      return;
    }

    // Actualizar contador en pantalla
    const contador = document.getElementById('contador-cantidad');
    if (contador) {
      contador.textContent = TAREA_ACTUAL.tipo === 'CONTEO'
        ? resp.cantidad_contada
        : `${resp.cantidad_actual} / ${resp.cantidad_requerida}`;
    }

    // Actualizar barra de progreso
    const barra = document.getElementById('barra-progreso');
    if (barra && resp.cantidad_requerida) {
      const pct = (resp.cantidad_actual / resp.cantidad_requerida) * 100;
      barra.style.width = `${Math.min(pct, 100)}%`;
    }

    // Habilitar botón confirmar si está completo
    if (resp.puede_confirmar) {
      const btn = document.getElementById('btn-confirmar');
      if (btn) {
        btn.disabled = false;
        btn.style.opacity = '1';
        btn.style.background = '#16a34a';
      }
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

  const payload = {
    tarea_id: TAREA_ACTUAL.id,
    tipo: TAREA_ACTUAL.tipo,
    items_escaneados: ITEMS_ESCANEADOS
  };

  try {
    const resp = await apiPost('/api/mobile/confirmar', payload);

    if (resp.error) {
      mostrarAlerta(resp.error, 'error');
      btn.textContent = '✓ Confirmar';
      btn.disabled = false;
      return;
    }

    mostrarAlerta('¡Tarea completada!', 'exito');

    setTimeout(() => {
      TAREA_ACTUAL = null;
      ITEMS_ESCANEADOS = [];
      cargarTareaActual();
    }, 1500);

  } catch (e) {
    // Sin conexión — guardar en cola offline
    guardarEnColaOffline(payload);
    setTimeout(() => {
      TAREA_ACTUAL = null;
      cargarTareaActual();
    }, 2000);
  }
}

// ============================================
// UI HELPERS
// ============================================
function mostrarPantalla(id) {
  ['pantalla-login', 'pantalla-tareas'].forEach(p => {
    const el = document.getElementById(p);
    if (el) el.style.display = p === id ? 'block' : 'none';
  });
}

function mostrarAlerta(mensaje, tipo = 'info') {
  const colores = {
    exito: { bg: '#16a34a', text: '#fff' },
    error: { bg: '#dc2626', text: '#fff' },
    advertencia: { bg: '#d97706', text: '#fff' },
    info: { bg: '#2563eb', text: '#fff' }
  };

  const c = colores[tipo] || colores.info;
  const alerta = document.createElement('div');
  alerta.style.cssText = `
    position:fixed;top:20px;left:50%;transform:translateX(-50%);
    background:${c.bg};color:${c.text};
    padding:16px 24px;border-radius:12px;font-size:18px;font-weight:600;
    z-index:9999;max-width:90%;text-align:center;
    animation:fadeIn 0.2s ease;
  `;
  alerta.textContent = mensaje;
  document.body.appendChild(alerta);
  setTimeout(() => alerta.remove(), 2500);
}

function mostrarFlash() {
  const flash = document.createElement('div');
  flash.style.cssText = `
    position:fixed;inset:0;background:rgba(255,255,255,0.3);
    z-index:9998;pointer-events:none;animation:flash 0.15s ease;
  `;
  document.body.appendChild(flash);
  setTimeout(() => flash.remove(), 150);
}

function vibrar() {
  if (navigator.vibrate) navigator.vibrate(50);
}

function cerrarSesion() {
  TOKEN = null;
  OPERARIO = null;
  localStorage.removeItem('wms_token');
  localStorage.removeItem('wms_operario');
  mostrarPantalla('pantalla-login');
}