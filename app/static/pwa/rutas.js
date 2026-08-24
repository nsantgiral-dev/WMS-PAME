// ══════════════════════════════════════════════════════════════════
// RUTAS — Muelle, conductor, planilla, maestras, vehículos
// Dependencias globales (de app.js): get(), post(), put(), alerta(),
//   flash(), set(), pantalla(), abrirCamara(), cerrarCamara(),
//   API, TOKEN, ALMACEN_ID, OPERARIO
// ══════════════════════════════════════════════════════════════════

// ─── MONITOR DE MUELLE ────────────────────────────────────────────────────────
const MUELLE_ORDEN_KEY = 'wms_muelle_orden'; // localStorage key

/** Recupera el orden de grupos de muelle guardado en localStorage. */
function muelleGetOrden() {
  try { return JSON.parse(localStorage.getItem(MUELLE_ORDEN_KEY) || '[]'); }
  catch { return []; }
}

/**
 * Persiste el orden de grupos de muelle en localStorage.
 * @param {string[]} orden - Lista de nombres de destino en el orden deseado
 */
function muelleSetOrden(orden) {
  localStorage.setItem(MUELLE_ORDEN_KEY, JSON.stringify(orden));
}

/**
 * Ordena los grupos de muelle segun el orden guardado, nuevos al final.
 * @param {Object[]} grupos - Grupos con propiedad destino y bultos
 * @returns {Object[]} Grupos reordenados
 */
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

/**
 * Renderiza los grupos de muelle con sus bultos en el DOM.
 * @param {Object[]} grupos - Grupos agrupados por destino con array de bultos
 */
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

/**
 * Mueve un grupo de muelle una posicion arriba o abajo y re-renderiza.
 * @param {number} idx - Indice actual del grupo en la lista
 * @param {number} dir - Direccion: -1 para arriba, 1 para abajo
 */
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

/** Carga las rutas EN_CARGUE en el selector del muelle. */
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

/** Muestra el campo de escaneo y oculta el boton de activacion en movil. */
function muelleActivarScan() {
  const btnActivar = document.getElementById('muelle-scan-activar');
  const campo      = document.getElementById('muelle-scan-campo');
  const input      = document.getElementById('muelle-scan-input');
  if (btnActivar) btnActivar.style.display = 'none';
  if (campo)      campo.style.display      = 'flex';
  if (input)      input.focus();
}

/** Abre la camara QR del muelle y procesa el codigo escaneado. */
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

/** En movil, oculta el campo de escaneo y muestra el boton si esta vacio. */
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

/**
 * Selecciona o deselecciona la ruta activa del muelle y actualiza la UI.
 * @param {string} idStr - ID de la ruta como string, o vacio para deseleccionar
 */
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
/** Orquestador principal del muelle: carga selector, datos y auto-refresco. */
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
/** Renderiza el muelle en modo informativo cuando no hay ruta seleccionada. */
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
    <div style="background:#1a1a1a;border-radius:10px;padding:12px;margin-bottom:16px;text-align:center;font-size:13px;color:#666;">
      Selecciona una ruta arriba para empezar a planificar el cargue
    </div>
    ${grupos.map(g => `
      <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="font-size:14px;font-weight:700;color:#f59e0b;">📍 ${g.destino}
          <span style="font-size:12px;color:#555;font-weight:400;"> · ${g.total} bulto${g.total !== 1 ? 's' : ''}</span>
        </div>
        ${g.bultos.map(b => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid #1a1a1a;margin-top:6px;">
            <div>
              <span style="font-family:monospace;font-size:13px;color:#ccc;">${b.codigo_barras}</span>
              <span style="font-size:11px;color:#555;margin-left:8px;">${b.tipo} ${b.numero}/${b.total}</span>
            </div>
            <span style="font-size:11px;color:#555;">${b.numero_pedido}</span>
          </div>`).join('')}
      </div>`).join('')}`;
}

// ── Con ruta seleccionada: planificación + confirmación ─
/**
 * Carga el muelle con manifiesto de la ruta y pendientes sin asignar.
 * @param {number} rutaId - ID de la ruta seleccionada
 */
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

  // — Cargue físico completo: ofrece cerrar la ruta desde el mismo lugar
  // donde se termina de escanear, sin cambiar a la pestaña Rutas. Es la
  // misma transición EN_CARGUE → EN_TRANSITO del botón "🚛 Salió" de allá
  // (mismo endpoint, misma validación de 0 pendientes en el servidor) —
  // este botón solo decide cuándo OFRECERLA, no duplica la regla.
  if (totalEnRuta > 0 && totalPlan === 0) {
    html += `
      <div style="background:#0a1a0a;border:1px solid #166534;border-radius:12px;padding:14px;margin-bottom:16px;text-align:center;">
        <div style="font-size:14px;color:#4ade80;font-weight:700;margin-bottom:8px;">✓ Los ${totalConf} bulto${totalConf !== 1 ? 's' : ''} de esta ruta ya están cargados</div>
        <button onclick="muelleConfirmarCargueCompleto(${rutaId})"
          style="width:100%;padding:14px;background:#14532d;color:#4ade80;border:none;border-radius:10px;font-size:16px;font-weight:800;cursor:pointer;">
          🚛 Confirmar cargue completo — Ruta lista para salir
        </button>
      </div>`;
  }

  // — Sección 1: bultos ya en la ruta —
  if (_RUTA_MANIFIESTO_ACTUAL.length) {
    html += `<div style="font-size:13px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px;">
      En esta ruta · ${totalConf} confirmado${totalConf !== 1 ? 's' : ''} · ${totalPlan} por confirmar
    </div>`;
    html += _RUTA_MANIFIESTO_ACTUAL.map((grupo, gi) =>
      _htmlGrupoRuta(grupo, gi, _RUTA_MANIFIESTO_ACTUAL.length, rutaId)
    ).join('');
  } else {
    html += `
      <div style="text-align:center;padding:20px;background:#111;border-radius:12px;border:1px dashed #333;margin-bottom:16px;">
        <div style="font-size:28px;margin-bottom:6px;">🚛</div>
        <div style="font-size:14px;font-weight:700;color:#eee;">Ruta vacía</div>
        <div style="font-size:12px;color:#555;margin-top:4px;">Asigna pedidos desde la lista de abajo</div>
      </div>`;
  }

  // — Sección 2: pendientes sin asignar —
  if (gruposPendientes.length) {
    html += `
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #222;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <span style="font-size:13px;font-weight:600;color:#555;text-transform:uppercase;letter-spacing:.08em;">📦 Pendientes por asignar</span>
          <span style="background:#333;color:#ccc;font-size:13px;padding:2px 10px;border-radius:10px;">${dPendientes.total_bultos}</span>
        </div>
        ${gruposPendientes.map(g => _htmlGrupoPendiente(g, rutaId)).join('')}
      </div>`;
  } else if (_RUTA_MANIFIESTO_ACTUAL.length > 0) {
    html += `
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid #222;text-align:center;font-size:13px;color:#555;">
        ✓ Todos los bultos del muelle están en esta ruta
      </div>`;
  }

  el.innerHTML = html;
}

/**
 * Confirma que la ruta activa terminó su cargue físico completo —misma
 * transición EN_CARGUE → EN_TRANSITO que ya existía como botón "🚛 Salió"
 * en la pestaña Rutas (`rutaCerrar`), ofrecida acá donde el operario
 * realmente termina de escanear. El backend (`RutaService.cerrar_ruta`) es
 * quien valida que no queden bultos PENDIENTE — este botón solo decide
 * cuándo mostrarse, la regla real vive del lado del servidor.
 * @param {number} rutaId - ID de la ruta a cerrar
 */
async function muelleConfirmarCargueCompleto(rutaId) {
  if (!confirm(`¿Confirmar que la Ruta #${rutaId} se cargó físicamente completa?\n\nPasará a EN TRÁNSITO — ya no se podrán agregar ni escanear más bultos.`)) return;
  try {
    const r = await fetch(API + '/api/rutas/' + rutaId + '/cerrar', {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) {
      alerta(`Ruta #${rutaId} confirmada — salió a reparto`, 'exito');
      // No hace falta limpiar RUTA_ACTIVA_ID a mano: cargarRutaSelector()
      // ya nota que la ruta dejó de estar EN_CARGUE y se reinicia sola
      // (mismo mecanismo que usa rutaCerrar() en la pestaña Rutas) — el
      // muelle queda listo para seleccionar o crear la próxima ruta.
      cargarMuelle();
    } else {
      alerta(d.error || 'Error al confirmar el cargue', 'error');
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ── Helpers de renderizado ────────────────────────────

/**
 * Genera el HTML de un grupo de parada dentro del manifiesto de la ruta.
 * @param {Object} grupo - Grupo con destino y bultos
 * @param {number} gi - Indice del grupo en la lista
 * @param {number} totalGrupos - Cantidad total de grupos
 * @param {number} rutaId - ID de la ruta activa
 * @returns {string} HTML del grupo
 */
function _htmlGrupoRuta(grupo, gi, totalGrupos, rutaId) {
  const confirmados = grupo.bultos.filter(b => b.estado === 'CARGADO').length;
  const totalGrupo = grupo.bultos.length;
  const todoConfirmado = confirmados === totalGrupo;

  return `
    <div id="ruta-grupo-${gi}" style="margin-bottom:16px;">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
        <div style="flex:1;">
          <span style="font-size:16px;font-weight:800;color:${todoConfirmado ? '#15803d' : '#b45309'};text-transform:uppercase;">
            📍 ${grupo.destino}
          </span>
          <span style="font-size:13px;color:#555;"> · ${confirmados}/${totalGrupo} conf.</span>
        </div>
        <div style="display:flex;gap:4px;">
          ${gi > 0
            ? `<button onclick="rutaMoverGrupo(${gi},-1,${rutaId})" style="background:#222;border:1px solid #333;color:#fff;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;">↑</button>`
            : `<div style="width:28px;"></div>`}
          ${gi < totalGrupos - 1
            ? `<button onclick="rutaMoverGrupo(${gi},1,${rutaId})" style="background:#222;border:1px solid #333;color:#fff;width:28px;height:28px;border-radius:6px;cursor:pointer;font-size:14px;">↓</button>`
            : `<div style="width:28px;"></div>`}
        </div>
        <div style="font-size:12px;color:#444;text-align:right;min-width:40px;">Parada<br>#${gi + 1}</div>
      </div>
      ${grupo.bultos.map(b => {
        const conf = b.estado === 'CARGADO';
        return `
          <div style="background:#111;border:1px solid ${conf ? '#14532d' : '#333'};border-left:4px solid ${conf ? '#4ade80' : '#f59e0b'};border-radius:10px;padding:10px 12px;margin-bottom:6px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <div style="flex:1;">
                <div style="font-size:15px;font-weight:700;font-family:monospace;color:${conf ? '#fff' : '#f59e0b'};">${b.codigo_barras}</div>
                <div style="font-size:13px;color:#999;margin-top:2px;">${b.numero_pedido} · ${b.cliente || ''}</div>
                <div style="font-size:12px;color:#777;">${b.tipo} · pieza ${b.numero}/${b.total}</div>
              </div>
              <div style="display:flex;align-items:center;gap:8px;">
                ${!conf ? `<button onclick="muelleDesasignar(${b.id})" title="Quitar de la ruta" style="background:none;border:none;color:#444;font-size:18px;cursor:pointer;line-height:1;padding:4px;">×</button>` : ''}
                <span style="background:${conf ? '#14532d' : '#451a03'};color:${conf ? '#4ade80' : '#f59e0b'};font-size:11px;padding:3px 10px;border-radius:20px;font-weight:700;white-space:nowrap;">
                  ${conf ? '✓ Cargado' : '⏳ Pendiente'}
                </span>
              </div>
            </div>
          </div>`;
      }).join('')}
    </div>`;
}

/**
 * Genera el HTML de un grupo de bultos pendientes por asignar a la ruta.
 * @param {Object} grupo - Grupo con destino y bultos sin asignar
 * @param {number} rutaId - ID de la ruta activa para asignacion
 * @returns {string} HTML del grupo pendiente
 */
function _htmlGrupoPendiente(grupo, rutaId) {
  const numeroPedido = grupo.bultos[0]?.numero_pedido || '';
  return `
    <div style="background:#111;border:1px solid #222;border-radius:12px;padding:14px;margin-bottom:8px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div>
          <div style="font-size:16px;font-weight:800;color:#b45309;">📍 ${grupo.destino}</div>
          <div style="font-size:13px;color:#555;margin-top:2px;">${grupo.total} bulto${grupo.total !== 1 ? 's' : ''}</div>
        </div>
        <button onclick="muelleAsignar(null,'${numeroPedido}')"
          style="background:#fff;color:#000;border:none;padding:8px 14px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;white-space:nowrap;">
          + Todo el pedido
        </button>
      </div>
      ${grupo.bultos.map(b => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid #1a1a1a;">
          <div>
            <span style="font-family:monospace;font-size:14px;color:#ddd;">${b.codigo_barras}</span>
            <span style="font-size:12px;color:#999;margin-left:8px;">${b.tipo} ${b.numero}/${b.total}</span>
            ${b.cliente ? `<span style="font-size:12px;color:#999;margin-left:8px;">· ${b.cliente}</span>` : ''}
          </div>
          <button onclick="muelleAsignar(${b.id},null)"
            style="background:#1a1a1a;color:#ccc;border:1px solid #333;padding:4px 10px;border-radius:6px;font-size:13px;cursor:pointer;">
            + Solo esta
          </button>
        </div>`).join('')}
    </div>`;
}

// ── Reordenar paradas ─────────────────────────────────
/**
 * Reordena una parada del manifiesto y persiste el nuevo orden.
 * @param {number} idx - Indice actual de la parada
 * @param {number} dir - Direccion: -1 arriba, 1 abajo
 * @param {number} rutaId - ID de la ruta activa
 */
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
/**
 * Asigna un bulto o todos los bultos de un pedido a la ruta activa.
 * @param {number|null} bultoId - ID del bulto individual, o null para asignar por pedido
 * @param {string|null} pedidoSiesa - Numero de pedido Siesa para asignar todos sus bultos
 */
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

/**
 * Quita un bulto de la ruta activa previa confirmacion del usuario.
 * @param {number} bultoId - ID del bulto a desasignar
 */
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
/** Confirma la carga fisica de una caja escaneada en la ruta activa. */
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

/**
 * Cambia el sub-tab activo en el modulo de rutas (rutas, maestras, vehiculos, conductores).
 * @param {string} nombre - Nombre del sub-tab a activar
 */
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

/** Despacha la carga del sub-tab activo de rutas. */
async function cargarRutas() {
  const hoy = new Date().toISOString().split('T')[0];
  const desde = document.getElementById('rutas-fecha-desde');
  const hasta = document.getElementById('rutas-fecha-hasta');
  if (desde && !desde.value) desde.value = hoy;
  if (hasta && !hasta.value) hasta.value = hoy;
  if      (RUTAS_SUBTAB === 'rutas')       await cargarListaRutas();
  else if (RUTAS_SUBTAB === 'maestras')    await cargarListaMaestras();
  else if (RUTAS_SUBTAB === 'vehiculos')   await cargarListaVehiculos();
  else                                     await cargarListaConductores();
}

// ── Rutas ────────────────────────────────────────────

/** Carga y renderiza la lista de rutas del rango de fechas seleccionado. */
async function cargarListaRutas() {
  const el = document.getElementById('lista-rutas');
  if (!el) return;
  try {
    const hoy = new Date().toISOString().split('T')[0];
    const desde = document.getElementById('rutas-fecha-desde')?.value || hoy;
    const hasta = document.getElementById('rutas-fecha-hasta')?.value || hoy;
    const d = await get(`/api/rutas/?fecha_desde=${desde}&fecha_hasta=${hasta}`);
    const rutas = d.rutas || [];
    if (!rutas.length) {
      el.innerHTML = '<div style="color:#555;text-align:center;padding:40px;">Sin rutas para este rango de fechas</div>';
      return;
    }
    el.innerHTML = rutas.map(r => rutaCard(r)).join('');
  } catch (e) {
    el.innerHTML = `<div style="color:#ef4444;text-align:center;padding:40px;">${e.message || 'Error cargando rutas'}<br><button onclick="cargarListaRutas()" style="margin-top:10px;padding:8px 16px;background:var(--pm);color:#fff;border:none;border-radius:8px;cursor:pointer;">Reintentar</button></div>`;
  }
}

/**
 * Genera el HTML de una tarjeta de ruta con estado, botones de accion y detalles.
 * @param {Object} r - Objeto ruta con id, estado, conductor_nombre, total_bultos, etc.
 * @returns {string} HTML de la tarjeta
 */
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

/**
 * Inicia el cargue de una ruta programada y redirige al muelle.
 * @param {number} id - ID de la ruta a iniciar
 */
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

/**
 * Cierra una ruta en cargue marcandola como EN_TRANSITO.
 * @param {number} id - ID de la ruta a cerrar
 */
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

/**
 * Abre el modal de entrega por bulto para una ruta en transito.
 * @param {number} id - ID de la ruta a entregar
 */
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

/** Renderiza la lista de bultos en el modal de entrega con toggle entregado/rechazado. */
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

/**
 * Alterna el estado entregado/rechazado de un bulto en el modal de entrega.
 * @param {number} i - Indice del bulto en _ENTREGA_BULTOS
 */
function _toggleEntrega(i) {
  _ENTREGA_BULTOS[i].entregado = !_ENTREGA_BULTOS[i].entregado;
  _renderEntregaLista();
}

/**
 * Establece el motivo de rechazo de un bulto en el modal de entrega.
 * @param {number} i - Indice del bulto en _ENTREGA_BULTOS
 * @param {string} motivo - Motivo de rechazo seleccionado
 */
function _setMotivo(i, motivo) {
  _ENTREGA_BULTOS[i].motivo_rechazo = motivo;
}

/** Cierra el modal de entrega y limpia el estado temporal. */
function cerrarModalEntrega() {
  document.getElementById('modal-entrega').style.display = 'none';
  _ENTREGA_RUTA_ID = null;
  _ENTREGA_BULTOS  = [];
}

/** Recopila los datos del modal de entrega y envia la confirmacion al servidor. */
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

/**
 * Envia la confirmacion de entrega de la ruta al backend.
 * @param {number} id - ID de la ruta
 * @param {Object[]} payload - Array de bultos con id, entregado y motivo_rechazo
 */
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

/**
 * Abre un modal con el manifiesto detallado de la ruta (paradas, bultos, estados).
 * @param {number} id - ID de la ruta
 */
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

/** Muestra el formulario de programacion de una nueva ruta. */
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

/** Oculta el formulario de programacion de ruta. */
function rutasCancelarForm() {
  document.getElementById('rutas-form').style.display = 'none';
}

/**
 * Al elegir conductor en "Programar viaje", preselecciona el vehículo que
 * ya tiene asignado en custodia (misma lógica de asignación/cambio de turno
 * de Flota, GET /flota/custodia/vehiculo-de-conductor) — sin esto, el admin
 * tenía que buscarlo a mano en la lista completa aunque el sistema ya supiera
 * cuál es. Sigue siendo editable: si no hay custodia vigente, o el admin
 * necesita otro vehículo, el select queda libre para elegir cualquiera.
 */
async function rutasConductorCambio() {
  const conductorId = document.getElementById('rutas-form-conductor')?.value;
  const selVehiculo = document.getElementById('rutas-form-vehiculo');
  if (!conductorId || !selVehiculo) return;
  try {
    const d = await get('/flota/custodia/vehiculo-de-conductor/' + conductorId);
    if (d.vehiculo_id && selVehiculo.querySelector(`option[value="${d.vehiculo_id}"]`)) {
      selVehiculo.value = d.vehiculo_id;
    }
  } catch (e) {}
}

/**
 * Selecciona el tipo de ruta (Urbana/Municipal) y actualiza los estilos de los botones.
 * @param {string} tipo - 'Urbana' o 'Municipal'
 */
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

/** Valida y envia la programacion de una nueva ruta al servidor. */
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

/**
 * Carga las rutas maestras activas en un elemento select.
 * @param {string} selectId - ID del elemento select en el DOM
 */
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

/** Carga y renderiza la lista completa de rutas maestras (activas e inactivas). */
async function cargarListaMaestras() {
  const el = document.getElementById('lista-maestras');
  if (!el) return;
  try {
    const d = await get('/api/rutas/maestras?activas=false');
    // Más reciente primero: la que se acaba de crear queda de primera.
    const maestras = (d.maestras || []).slice().sort((a, b) => b.id - a.id);
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

/**
 * Carga la lista de municipios desde el servidor (con cache en memoria).
 * @returns {Promise<void>}
 */
function _cargarMunicipios() {
  if (_MUNICIPIOS_CACHE.length) return Promise.resolve();
  if (_municipiosPromise) return _municipiosPromise;
  _municipiosPromise = get('/api/rutas/municipios')
    .then(d => { _MUNICIPIOS_CACHE = d.municipios || []; })
    .catch(e => { console.error('[WMS] municipios:', e); _municipiosPromise = null; });
  return _municipiosPromise;
}

/**
 * Filtra y muestra sugerencias de municipios mientras el usuario escribe.
 * @param {HTMLInputElement} input - Campo de texto de la parada
 */
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

/**
 * Selecciona un municipio del dropdown de sugerencias y lo pone en el input.
 * @param {HTMLElement} el - Elemento del dropdown con data-municipio
 */
function maestraSeleccionarMunicipio(el) {
  const inp = document.getElementById('maestras-parada-input');
  if (inp) inp.value = el.dataset.municipio;
  const drop = document.getElementById('maestras-sugerencias');
  if (drop) drop.style.display = 'none';
}

/** Oculta el dropdown de sugerencias de municipios con un pequeno delay. */
function maestraOcultarSugerencias() {
  setTimeout(() => {
    const el = document.getElementById('maestras-sugerencias');
    if (el) el.style.display = 'none';
  }, 150);
}

/**
 * Maneja teclas especiales en el input de parada (Enter agrega, Escape cierra).
 * @param {KeyboardEvent} event - Evento de teclado
 */
function maestraInputKeydown(event) {
  if (event.key === 'Enter') { event.preventDefault(); maestraAgregarParada(); }
  if (event.key === 'Escape') {
    const el = document.getElementById('maestras-sugerencias');
    if (el) el.style.display = 'none';
  }
}

/** Muestra el formulario para crear una nueva ruta maestra. */
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

/** Oculta el formulario de ruta maestra y limpia el estado. */
function maestraCancelarForm() {
  document.getElementById('maestras-form').style.display = 'none';
  document.getElementById('maestras-form-id').value = '';
  const el = document.getElementById('maestras-sugerencias');
  if (el) el.style.display = 'none';
}

/**
 * Carga una ruta maestra existente en el formulario para edicion.
 * @param {number} id - ID de la ruta maestra a editar
 */
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

/** Agrega el municipio del input a la lista de paradas de la ruta maestra. */
function maestraAgregarParada() {
  const val = document.getElementById('maestras-parada-input')?.value.trim();
  if (!val) return;
  _MAESTRAS_PARADAS.push(val);
  document.getElementById('maestras-parada-input').value = '';
  const el = document.getElementById('maestras-sugerencias');
  if (el) el.style.display = 'none';
  _maestraRenderParadas();
}

/**
 * Elimina una parada de la lista de la ruta maestra.
 * @param {number} idx - Indice de la parada a eliminar
 */
function maestraQuitarParada(idx) {
  _MAESTRAS_PARADAS.splice(idx, 1);
  _maestraRenderParadas();
}

/**
 * Mueve una parada de la ruta maestra una posicion arriba o abajo.
 * @param {number} idx - Indice actual de la parada
 * @param {number} dir - Direccion: -1 arriba, 1 abajo
 */
function maestraMoverParada(idx, dir) {
  const nuevoIdx = idx + dir;
  if (nuevoIdx < 0 || nuevoIdx >= _MAESTRAS_PARADAS.length) return;
  [_MAESTRAS_PARADAS[idx], _MAESTRAS_PARADAS[nuevoIdx]] =
    [_MAESTRAS_PARADAS[nuevoIdx], _MAESTRAS_PARADAS[idx]];
  _maestraRenderParadas();
}

/** Renderiza la lista de paradas en el formulario de ruta maestra. */
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

/** Valida y guarda (crea o actualiza) una ruta maestra en el servidor. */
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

/**
 * Activa o desactiva una ruta maestra.
 * @param {number} id - ID de la ruta maestra
 * @param {boolean} activar - true para activar, false para desactivar
 */
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

/**
 * Elimina una ruta maestra previa confirmacion del usuario.
 * @param {number} id - ID de la ruta maestra
 * @param {string} nombre - Nombre de la ruta (para el mensaje de confirmacion)
 */
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

/**
 * Carga los vehiculos activos en un elemento select.
 * @param {string} selectId - ID del elemento select en el DOM
 */
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

/** Carga y renderiza la lista completa de vehiculos (activos e inactivos). */
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
      <div class="flota-veh">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div>
            <div class="flota-placa">${v.placa}</div>
            <div class="flota-tipo">${v.tipo}${v.capacidad_kg ? ' · ' + v.capacidad_kg + ' kg' : ''}</div>
          </div>
          <span class="badge ${v.activo ? 'badge-green' : 'badge-red'}">
            ${v.activo ? 'ACTIVO' : 'INACTIVO'}</span>
        </div>
        <button class="btn-flota" onclick="vehiculoToggle(${v.id}, ${!v.activo})">
          ${v.activo ? 'Desactivar' : 'Activar'}</button>
        ${v.activo ? `<button class="btn-flota" onclick="verExpedienteVehiculo('${v.placa}')">
          Ver expediente →</button>` : ''}
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;">Error cargando vehículos</div>';
  }
}

/** Muestra el formulario para registrar un nuevo vehiculo. */
function vehiculosMostrarForm() {
  document.getElementById('vehiculos-form').style.display = 'block';
  document.getElementById('vehiculos-form-error').textContent = '';
}

/** Oculta el formulario de registro de vehiculo. */
function vehiculosCancelarForm() {
  document.getElementById('vehiculos-form').style.display = 'none';
}

/** Valida y crea un nuevo vehiculo en el servidor. */
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

/**
 * Activa o desactiva un vehiculo.
 * @param {number} id - ID del vehiculo
 * @param {boolean} activar - true para activar, false para desactivar
 */
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

/**
 * Carga los conductores activos en un elemento select.
 * @param {string} selectId - ID del elemento select en el DOM
 */
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

/** Carga y renderiza la lista completa de conductores (activos e inactivos). */
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
            <div style="font-size:12px;color:#555;margin-top:2px;">${identidadConductor(c, conductores)}${c.telefono ? ' · ' + c.telefono : ''}</div>
            ${c.usuario_email
              ? `<div style="font-size:11px;color:#facc15;margin-top:3px;">👤 ${c.usuario_email}</div>`
              : `<div style="font-size:11px;color:#fbbf24;margin-top:3px;">Sin cuenta PWA — no puede entrar a la app</div>`}
          </div>
          ${c.activo
            ? '<span style="background:#14532d;color:#4ade80;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;height:fit-content;">ACTIVO</span>'
            : '<span style="background:#3f1515;color:#f87171;padding:3px 8px;border-radius:8px;font-size:10px;font-weight:700;height:fit-content;">INACTIVO</span>'}
        </div>
        <div style="display:flex;gap:6px;">
          <button onclick="conductorToggle(${c.id}, ${!c.activo})"
            style="flex:1;padding:8px;background:#1a1a1a;border:1px solid #333;color:#aaa;border-radius:8px;font-size:12px;cursor:pointer;">
            ${c.activo ? 'Desactivar' : 'Activar'}
          </button>
          ${c.usuario_email ? '' : `<button onclick="conductorCrearCuenta(${c.id}, '${c.nombre.replace(/'/g, "\\'")}')"
            style="flex:1;padding:8px;background:#1e3a5f;border:1px solid #2563eb;color:#93c5fd;border-radius:8px;font-size:12px;cursor:pointer;">
            Crear cuenta PWA
          </button>`}
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#ef4444;text-align:center;">Error cargando conductores</div>';
  }
}

/** Muestra el formulario de registro de conductor y carga usuarios disponibles. */
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

/** Oculta el formulario de registro de conductor. */
function conductoresCancelarForm() {
  document.getElementById('conductores-form').style.display = 'none';
}

/** Valida y crea un nuevo conductor en el servidor. */
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

/**
 * Activa o desactiva un conductor.
 * @param {number} id - ID del conductor
 * @param {boolean} activar - true para activar, false para desactivar
 */
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
//: Catálogo de motivos de rechazo. Viene del backend
//: (`services/motivos_rechazo.py`) y no se escribe acá: dos listas del mismo
//: dominio divergen, y ya pasó con la condición de pago y los tipos de vehículo.
let _COND_MOTIVOS = [];
let _COND_PARADA_FORM = null;   // parada en formulario de confirmación
let _COND_SYNCING = false;
let _COND_OFFLINE_INIT = false;
let _COND_RETENCIONES = [];     // catálogo de motivos (mismo que Liquidación de escritorio)

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

/** Carga las rutas en transito asignadas al conductor con soporte offline. */
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

/**
 * Carga y muestra las paradas de una ruta para el conductor con fallback offline.
 * @param {number} rutaId - ID de la ruta
 */
async function condAbrirParadas(rutaId) {
  const el = document.getElementById('cond-contenido');
  el.innerHTML = '<div style="text-align:center;padding:60px;color:#555;">Cargando paradas...</div>';
  let data = null;
  try {
    // `navigator.onLine` NO confirma internet real — solo dice si el
    // adaptador de red está conectado a algo (cambio de torre en datos
    // móviles, VPN, redes raras: puede dar `false` con internet
    // funcionando). Cortar acá antes de intentar la petición real hacía
    // que el conductor viera "sin conexión" aunque sí tuviera señal — se
    // saltaba directo a la caché vacía sin comprobar nada.
    // `cargarRutasConductor()`, la función hermana justo arriba en este
    // archivo, ya hacía esto bien: intenta la red primero y solo cae a
    // caché si el fetch de verdad falla. Misma política, ahora en los dos.
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
  // Se pide una vez por ruta, no por parada. Si falla, el select queda vacío y
  // la validación del servidor rechaza igual — el conductor ve el error en vez
  // de guardar un rechazo sin motivo.
  if (!_COND_MOTIVOS.length) {
    try { _COND_MOTIVOS = (await get('/api/rutas/motivos-rechazo')).motivos || []; }
    catch (e) { console.warn('[RUTAS] no se pudo traer el catálogo de motivos', e); }
  }
  _COND_RETENCIONES = data.retenciones_disponibles || _COND_RETENCIONES;
  _condRenderParadas(data);
}

/**
 * Renderiza la lista de entregas del conductor con estado y boton de cierre.
 * Cada entrada es una FACTURA, no una parada fisica: un cliente con dos
 * facturas en una visita aparece dos veces.
 * @param {Object} d - Datos con facturas_gestionadas
 */
function _condRenderParadas(d) {
  const el = document.getElementById('cond-contenido');
  if (!el) return;
  const paradas = _COND_PARADAS;
  // paradas_gestionadas es el nombre viejo: puede venir de IndexedDB cacheado
  // antes del rename. Retirar el fallback post go-live.
  const gestionadas = d.facturas_gestionadas ?? d.paradas_gestionadas
                      ?? paradas.filter(p => p.recaudo).length;
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

/**
 * Abre el formulario de confirmacion para una parada especifica.
 * @param {number} idx - Indice de la parada en _COND_PARADAS
 */
function condAbrirFormParada(idx) {
  _COND_PARADA_FORM = { ..._COND_PARADAS[idx], _idx: idx };
  const el = document.getElementById('cond-contenido');
  if (el) el._tipoPagoSel = null;  // no arrastrar la selección de la parada anterior
  _condRenderFormParada();
}

/**
 * Cantidades entregadas/devueltas actuales por referencia, leyendo los
 * inputs en vivo (o el valor precargado si el DOM aún no se tocó).
 * Fuente única para "hay ajuste", el recálculo de valor y el payload final.
 */
function _condItemsAjustados() {
  const p = _COND_PARADA_FORM;
  if (!p || !p.items) return [];
  return p.items.map((it, idx) => {
    const pedido = it.cantidad_pedida || 0;
    const inp = document.getElementById('item-entregado-' + idx);
    const entregado = inp ? Math.max(0, Math.min(parseInt(inp.value) || 0, pedido)) : pedido;
    return { ...it, idx, pedido, entregado, devuelto: pedido - entregado };
  });
}

/**
 * Filas de Base + IVA, para pintar ARRIBA del total dentro del mismo
 * recuadro — base, luego IVA, luego el total (que arma el caller). Se
 * oculta si Siesa no respondió con alguno de los dos (backend manda
 * `null`, no inventa un desglose sobre un total que ya viene sin ese
 * detalle).
 */
function _condDesgloseHTML(p) {
  if (p.base_gravable == null || p.iva_factura == null) return '';
  return `
    <div style="display:flex;justify-content:space-between;font-size:15px;color:#aaa;margin-bottom:4px;">
      <span>Base</span><span>$${Number(p.base_gravable).toLocaleString('es-CO')}</span>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:15px;color:#aaa;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #333;">
      <span>IVA</span><span>$${Number(p.iva_factura).toLocaleString('es-CO')}</span>
    </div>`;
}

/** Renderiza el formulario de confirmacion de parada con estado, pago, foto y items. */
function _condRenderFormParada() {
  const el = document.getElementById('cond-contenido');
  const p = _COND_PARADA_FORM;
  if (!el || !p) return;

  const r = p.recaudo;
  // RESULTADO ya solo tiene 2 opciones — "parcial" no es un botón, surge
  // solo si el conductor ajusta alguna cantidad en Referencias. Un recaudo
  // viejo con estado_entrega=PARCIAL se edita igual que un Entregado.
  const estadoUi = (r && r.estado_entrega === 'RECHAZADO') ? 'RECHAZADO' : 'ENTREGADO';
  const motivoRechazoActual = (r && r.motivo_rechazo) || '';
  const formaActual  = r ? (r.forma_pago || '') : '';
  const montoActual  = r ? (r.monto_cobrado || 0) : 0;
  const obsActual    = r ? (r.observaciones || '') : '';
  const rechazadosActuales = r ? (r.bultos_rechazados_ids || []) : [];

  // Modo de pago — 3 posibles, según lo que Siesa confirmó de este pedido:
  //  · CREDITO:  se_cobra_en_puerta === false confirmado → crédito real (C04),
  //              no tiene sentido preguntar monto ni forma de pago.
  //  · DINAMICO: se cobra en la puerta (C01 o C02) Y Siesa dio el valor de la
  //              factura Y hay precio real por referencia → Total/Parcial.
  //  · LIBRE:    cualquier otro caso (Siesa no respondió, dato incompleto) —
  //              Regla 0, ante dato ausente, conservador: se pregunta a mano
  //              en vez de asumir.
  const hayValorConocido = p.valor_factura != null
    && !!(p.items && p.items.length) && p.items.every(it => it.valor_unitario != null);
  // `se_cobra_en_puerta`, NO `es_contado`: toda venta de ruta sale en C02, que
  // no es contado documental pero sí se cobra en la puerta. Con `es_contado`
  // esto era `false` en TODA parada y el widget del monto no aparecía nunca.
  //
  // El `??` cubre la caché vieja del service worker: un cliente que todavía no
  // recibió el SHELL nuevo recibe el payload nuevo pero sin el campo, y cae al
  // comportamiento anterior —conservador, no pide cobrar— en vez de romperse.
  const seCobraEnPuerta = p.se_cobra_en_puerta ?? p.es_contado;
  const mostrarValorDinamico = seCobraEnPuerta === true && hayValorConocido;
  // El modo lo decide el backend (`services/cond_pago.modo_pantalla`) — acá
  // estaba la segunda implementación de la misma política, y por eso el modo
  // no se podía contar: se calculaba en el navegador y se descartaba.
  //
  // Ante campo ausente (backend viejo o caché) cae a LIBRE, que es el modo que
  // NO afirma nada. Asumir DINAMICO le pediría cobrar un valor que nadie
  // confirmó; asumir CREDITO le impediría cobrar uno que sí correspondía.
  const modoPago = p.modo_pago || 'LIBRE';
  el._modoPago = modoPago;
  el._mostrarValorDinamico = mostrarValorDinamico;
  // El valor de la factura se muestra siempre que se conozca — hasta en
  // crédito, donde es solo informativo (no cobra, no dispara el toggle).
  el._hayValorConocido = hayValorConocido;
  el._valorAjustado = p.valor_factura;

  const tipoPagoInicial = (r && r.motivo_descuento) ? 'PARCIAL' : 'TOTAL';
  el._tipoPagoSel = el._tipoPagoSel || tipoPagoInicial;
  const motivoActual = r ? (r.motivo_descuento || '') : '';

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
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">RESULTADO</label>
      <div style="display:flex;gap:6px;" id="cond-estado-btns">
        ${['ENTREGADO','RECHAZADO'].map(e => `
          <button onclick="condSelEstado('${e}')"
            id="cond-estado-${e}"
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;border:2px solid ${e===estadoUi ? (e==='ENTREGADO'?'#16a34a':'#dc2626') : '#ddd'};background:${e===estadoUi ? (e==='ENTREGADO'?'#f0fdf4':'#fef2f2') : '#fff'};color:${e===estadoUi ? (e==='ENTREGADO'?'#15803d':'#b91c1c') : '#aaa'};">
            ${e === 'ENTREGADO' ? '✓ Entregado' : '✗ Rechazado'}
          </button>`).join('')}
      </div>
    </div>

    <div id="cond-bultos-rechazo" style="margin-bottom:14px;display:${estadoUi === 'RECHAZADO' ? 'block' : 'none'};">
      <label style="font-size:12px;color:#555;font-weight:700;display:block;margin-bottom:8px;">BULTOS RECHAZADOS</label>
      ${p.bultos.map(b => `
        <label style="display:flex;align-items:center;gap:10px;padding:10px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px;cursor:pointer;">
          <input type="checkbox" value="${b.id}" ${rechazadosActuales.includes(b.id) ? 'checked' : ''}
            style="width:18px;height:18px;cursor:pointer;" id="chk-bulto-${b.id}">
          <span style="font-size:13px;color:#374151;">${b.codigo_barras} · ${b.tipo} ${b.numero}/${b.total}</span>
        </label>`).join('')}
    </div>

    <div id="cond-items-parcial" style="margin-bottom:14px;display:${estadoUi === 'ENTREGADO' ? 'block' : 'none'};">
      <label style="font-size:12px;color:#555;font-weight:700;display:block;margin-bottom:8px;">REFERENCIAS</label>
      <div style="font-size:11px;color:#9ca3af;margin-bottom:10px;">Precargadas con la cantidad del pedido. Ajusta solo si algo no se entregó — lo que baje queda como devolución.</div>
      ${(p.items && p.items.length ? p.items : []).map((it, idx) => {
        const pedido = it.cantidad_pedida || 0;
        const prevEntregado = (() => {
          if (r && r.items_entregados && r.items_entregados.length) {
            const prev = r.items_entregados.find(x => x.codigo === it.codigo);
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

    <div id="cond-bultos-devolucion" style="margin-bottom:14px;display:none;">
      <label style="font-size:12px;color:#555;font-weight:700;display:block;margin-bottom:8px;">BULTOS CON DEVOLUCIÓN</label>
      <div style="font-size:11px;color:#9ca3af;margin-bottom:10px;">Opcional — en cuál caja/bulto físico va lo que vuelve a bodega.</div>
      ${p.bultos.map(b => `
        <label style="display:flex;align-items:center;gap:10px;padding:10px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px;cursor:pointer;">
          <input type="checkbox" value="${b.id}" ${rechazadosActuales.includes(b.id) ? 'checked' : ''}
            style="width:18px;height:18px;cursor:pointer;" id="chk-bulto-dev-${b.id}">
          <span style="font-size:13px;color:#374151;">${b.codigo_barras} · ${b.tipo} ${b.numero}/${b.total}</span>
        </label>`).join('')}
    </div>

    <div id="cond-entrega-payload" style="display:${estadoUi === 'ENTREGADO' ? 'block' : 'none'};">
      ${modoPago === 'CREDITO' ? `
      ${hayValorConocido ? `
      <div id="cond-valorfactura-wrap" style="margin-bottom:14px;">
        <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">VALOR FACTURA</label>
        <div id="cond-valorfactura-monto" style="padding:14px;background:#1a1a1a;border:1px solid #333;border-radius:10px;">
          ${_condDesgloseHTML(p)}
          <div id="cond-valorfactura-total" style="font-size:20px;font-weight:800;color:#4ade80;">$${Number(p.valor_factura).toLocaleString('es-CO')}</div>
        </div>
      </div>
      ` : ''}
      <div style="margin-bottom:14px;padding:14px;background:#1a1a1a;border:1px solid #333;border-radius:10px;font-size:13px;color:#aaa;display:flex;align-items:center;gap:10px;">
        💳 Pedido a crédito — no se cobra en la entrega. La cartera se gestiona aparte.
      </div>
      ` : `
      ${modoPago === 'DINAMICO' ? `
      <div id="cond-valorfactura-wrap" style="margin-bottom:14px;">
        <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">VALOR A COBRAR</label>
        <div id="cond-valorfactura-monto" style="padding:14px;background:#1a1a1a;border:1px solid #333;border-radius:10px;">
          ${_condDesgloseHTML(p)}
          <div id="cond-valorfactura-total" style="font-size:20px;font-weight:800;color:#4ade80;">$${Number(p.valor_factura).toLocaleString('es-CO')}</div>
        </div>
      </div>

      <div id="cond-pago-toggle-wrap" style="margin-bottom:14px;">
        <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">PAGO</label>
        <div style="display:flex;gap:6px;">
          <button type="button" onclick="condSelTipoPago('TOTAL')" id="cond-tipopago-TOTAL"
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;border:2px solid #ddd;background:#fff;color:#aaa;">
            ✓ Pago Total
          </button>
          <button type="button" onclick="condSelTipoPago('PARCIAL')" id="cond-tipopago-PARCIAL"
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:13px;font-weight:700;cursor:pointer;border:2px solid #ddd;background:#fff;color:#aaa;">
            ⚠ Pago Parcial
          </button>
        </div>
      </div>

      <div id="cond-parcial-detalle" style="margin-bottom:14px;display:none;">
        <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">MONTO PAGADO ($)</label>
        <input type="number" id="cond-monto-parcial" value="${motivoActual ? montoActual : ''}" min="0" max="${Math.floor(p.valor_factura)}" step="1"
          oninput="condActualizarMotivoVisible()"
          style="width:100%;padding:14px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:18px;font-weight:700;box-sizing:border-box;">

        <div id="cond-motivo-descuento-wrap" style="margin-top:12px;display:none;">
          <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">MOTIVO DEL DESCUENTO</label>
          <select id="cond-motivo-descuento"
            style="width:100%;padding:14px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:15px;">
            <option value="">— Seleccionar —</option>
            ${_COND_RETENCIONES.map(ret =>
              `<option value="${ret.tipo}" ${ret.tipo===motivoActual?'selected':''}>${ret.nombre}</option>`
            ).join('')}
          </select>
        </div>
      </div>
      ` : `
      <div id="cond-monto-wrap" style="margin-bottom:14px;">
        <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">MONTO COBRADO ($)</label>
        <input type="number" id="cond-monto" value="${montoActual}" min="0" step="100"
          style="width:100%;padding:14px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:18px;font-weight:700;box-sizing:border-box;">
      </div>
      `}

      <div id="cond-forma-pago-wrap" style="margin-bottom:14px;">
        <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">FORMA DE PAGO</label>
        <select id="cond-forma-pago"
          style="width:100%;padding:14px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:15px;">
          <option value="">— Seleccionar —</option>
          ${['EFECTIVO','TRANSFERENCIA','CHEQUE','CREDITO','EXENTO'].map(f =>
            `<option value="${f}" ${f===formaActual?'selected':''}>${f.charAt(0)+f.slice(1).toLowerCase()}</option>`
          ).join('')}
        </select>
      </div>
      `}
    </div>

    <!-- Motivo tipificado del rechazo. RECHAZADO era el estado mas barato de
         los tres (solo pedia prosa) y el control "si no paga, no se entrega"
         empuja hacia el. Un texto libre no dice lo unico que importa:
         SI LA MERCANCIA VOLVIO. El catalogo viene del backend, de
         services/motivos_rechazo.py, para no tener dos listas.
         Sin backticks: esto vive DENTRO de un template literal. -->
    <div id="cond-motivo-wrap" style="margin-bottom:14px;display:${estadoUi === 'RECHAZADO' ? 'block' : 'none'};">
      <label style="font-size:12px;color:#aaa;font-weight:700;display:block;margin-bottom:8px;">MOTIVO DEL RECHAZO *</label>
      <select id="cond-motivo"
        style="width:100%;padding:12px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:10px;font-size:14px;box-sizing:border-box;">
        <option value="">— Elegí el motivo —</option>
        ${(_COND_MOTIVOS || []).map(m => `
        <option value="${m.codigo}" ${motivoRechazoActual === m.codigo ? 'selected' : ''}>${m.etiqueta}</option>`).join('')}
      </select>
      <p id="cond-motivo-aviso" style="font-size:11px;color:#fbbf24;margin:6px 0 0;display:none;">
        La mercancía se queda con el cliente. El inventario NO vuelve al camión.
      </p>
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

    <div style="position:sticky;bottom:16px;">
      <button onclick="condGuardarParada()"
        style="width:100%;padding:20px;background:#1d4ed8;color:#fff;border:none;border-radius:14px;font-size:18px;font-weight:800;cursor:pointer;">
        ${r ? '💾 Actualizar Confirmación' : '✓ Confirmar Parada'}
      </button>
    </div>`;

  // Guardar estado seleccionado
  el._estadoSel = estadoUi;

  if (mostrarValorDinamico) {
    condSelTipoPago(el._tipoPagoSel);
  }
  condRecalcularPago();
}

/** Recalcula valor a cobrar + visibilidad de "Bultos con devolución" según lo tecleado en Referencias. */
function condRecalcularPago() {
  const el = document.getElementById('cond-contenido');
  const p = _COND_PARADA_FORM;
  if (!el || !p) return;

  const items = _condItemsAjustados();
  const hayAjuste = items.some(it => it.devuelto > 0);

  const divBultosDev = document.getElementById('cond-bultos-devolucion');
  if (divBultosDev) divBultosDev.style.display = hayAjuste ? 'block' : 'none';

  // El valor se recalcula si se conoce, aunque sea crédito (ahí es solo
  // informativo — no hay toggle Total/Parcial que sincronizar).
  if (!el._hayValorConocido) return;

  const deduccion = items.reduce((s, it) => s + it.devuelto * (it.valor_unitario || 0), 0);
  const valorAjustado = Math.max(0, Math.round(p.valor_factura - deduccion));
  el._valorAjustado = valorAjustado;

  const campoValor = document.getElementById('cond-valorfactura-total');
  if (campoValor) {
    campoValor.innerHTML = hayAjuste
      ? `<span style="text-decoration:line-through;color:#888;font-size:12px;font-weight:500;display:block;margin-bottom:2px;">$${Number(p.valor_factura).toLocaleString('es-CO')}</span>$${valorAjustado.toLocaleString('es-CO')}`
      : `$${valorAjustado.toLocaleString('es-CO')}`;
  }

  if (el._modoPago !== 'DINAMICO') return;  // resto solo aplica al toggle Total/Parcial

  const inpParcial = document.getElementById('cond-monto-parcial');
  if (inpParcial) inpParcial.max = valorAjustado;
  condActualizarMotivoVisible();
}

/**
 * Selecciona Pago Total o Pago Parcial en el formulario del conductor
 * (solo visible para pedidos de contado entregados completos) y ajusta la UI.
 * @param {string} tipo - 'TOTAL' o 'PARCIAL'
 */
function condSelTipoPago(tipo) {
  const el = document.getElementById('cond-contenido');
  if (!el) return;
  el._tipoPagoSel = tipo;

  ['TOTAL','PARCIAL'].forEach(t => {
    const btn = document.getElementById('cond-tipopago-' + t);
    if (!btn) return;
    const activo = t === tipo;
    btn.style.borderColor = activo ? (t === 'TOTAL' ? '#16a34a' : '#d97706') : '#ddd';
    btn.style.background  = activo ? (t === 'TOTAL' ? '#f0fdf4' : '#fffbeb') : '#fff';
    btn.style.color       = activo ? (t === 'TOTAL' ? '#15803d' : '#b45309') : '#aaa';
  });

  const detalle = document.getElementById('cond-parcial-detalle');
  if (detalle) detalle.style.display = tipo === 'PARCIAL' ? 'block' : 'none';
  if (tipo === 'PARCIAL') condActualizarMotivoVisible();
}

/** Muestra el desplegable de motivo (del DESCUENTO, no del rechazo) solo si el conductor ya escribió un monto parcial menor al valor a cobrar. */
function condActualizarMotivoVisible() {
  const el = document.getElementById('cond-contenido');
  const wrap = document.getElementById('cond-motivo-descuento-wrap');
  const inp = document.getElementById('cond-monto-parcial');
  if (!wrap || !inp || !el) return;
  const monto = parseInt(inp.value, 10) || 0;
  const techo = el._valorAjustado != null ? el._valorAjustado : 0;
  wrap.style.display = (monto > 0 && monto < techo) ? 'block' : 'none';
}

/**
 * Selecciona el resultado de la parada en el formulario del conductor y ajusta la UI.
 * "Parcial" ya no es una opción propia — surge solo si el conductor ajusta
 * alguna cantidad en Referencias (ver condRecalcularPago/condGuardarParada).
 * @param {string} estado - 'ENTREGADO' o 'RECHAZADO'
 */
function condSelEstado(estado) {
  const el = document.getElementById('cond-contenido');
  if (!el) return;
  el._estadoSel = estado;

  // Este wrap y el de "motivo del descuento" (pago parcial) compartían el
  // mismo id hasta 2026-08-19 — getElementById siempre agarraba el otro, así
  // que el select de motivo de rechazo nunca se mostraba al marcar RECHAZADO
  // (el backend sí exigía el campo — el conductor quedaba trabado sin ver
  // por qué). El del descuento se renombró: ver cond-motivo-descuento-wrap.
  const wrapMotivo = document.getElementById('cond-motivo-wrap');
  if (wrapMotivo) wrapMotivo.style.display = estado === 'RECHAZADO' ? 'block' : 'none';

  ['ENTREGADO','RECHAZADO'].forEach(e => {
    const btn = document.getElementById('cond-estado-' + e);
    if (!btn) return;
    const activo = e === estado;
    const colores = { ENTREGADO: ['#16a34a','#f0fdf4','#15803d'], RECHAZADO: ['#dc2626','#fef2f2','#b91c1c'] };
    const [borde, fondo, texto] = activo ? colores[e] : ['#ddd','#fff','#aaa'];
    btn.style.borderColor = borde;
    btn.style.background  = fondo;
    btn.style.color       = texto;
  });

  const divRechazo = document.getElementById('cond-bultos-rechazo');
  if (divRechazo) divRechazo.style.display = estado === 'RECHAZADO' ? 'block' : 'none';

  const divItems = document.getElementById('cond-items-parcial');
  if (divItems) divItems.style.display = estado === 'ENTREGADO' ? 'block' : 'none';

  const divPayload = document.getElementById('cond-entrega-payload');
  if (divPayload) divPayload.style.display = estado === 'ENTREGADO' ? 'block' : 'none';

  const divBultosDev = document.getElementById('cond-bultos-devolucion');
  if (divBultosDev && estado === 'RECHAZADO') divBultosDev.style.display = 'none';

  if (estado === 'ENTREGADO') condRecalcularPago();
}

/** Previsualiza la foto de evidencia seleccionada por el conductor. */
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

/** Elimina la foto de evidencia seleccionada y oculta la previsualizacion. */
function condEliminarFoto() {
  const input = document.getElementById('cond-foto');
  const preview = document.getElementById('cond-foto-preview');
  if (input) input.value = '';
  if (preview) preview.style.display = 'none';
}

/**
 * Recalcula la cantidad devuelta cuando el conductor cambia la cantidad entregada.
 * @param {number} idx - Indice del item en la lista de referencias
 * @param {number} pedido - Cantidad original pedida
 */
function condActualizarDevuelto(idx, pedido) {
  const inp = document.getElementById('item-entregado-' + idx);
  const div = document.getElementById('item-devuelto-' + idx);
  if (!inp || !div) return;
  let val = parseInt(inp.value) || 0;
  if (val < 0) { val = 0; inp.value = 0; }
  if (val > pedido) { val = pedido; inp.value = pedido; }
  div.textContent = pedido - val;
  condRecalcularPago();
}

/** Valida, recopila datos del formulario y guarda la confirmacion de parada (online u offline). */
async function condGuardarParada() {
  const el = document.getElementById('cond-contenido');
  const p = _COND_PARADA_FORM;
  if (!el || !p) return;

  const estadoUi = el._estadoSel || 'ENTREGADO';
  const obs      = document.getElementById('cond-obs')?.value?.trim() || '';

  // Observaciones obligatorias para RECHAZADO
  if (estadoUi === 'RECHAZADO' && !obs) {
    alerta('Escribe el motivo del rechazo (ej: cliente cerrado, dirección incorrecta)', 'error');
    document.getElementById('cond-obs')?.focus();
    return;
  }

  // Bultos rechazados — solo aplica a Rechazado.
  const bultosRechazados = [];
  if (estadoUi === 'RECHAZADO') {
    p.bultos.forEach(b => {
      const chk = document.getElementById('chk-bulto-' + b.id);
      if (chk && chk.checked) bultosRechazados.push(b.id);
    });
  }

  // "Parcial" no es un botón — surge solo si el conductor bajó alguna
  // cantidad en Referencias. estadoEntrega es lo que de verdad se envía.
  let estadoEntrega = estadoUi;
  let itemsEntregados = [];
  let bultosDevolucion = [];

  if (estadoUi === 'ENTREGADO') {
    const itemsAjustados = _condItemsAjustados();
    const hayAjuste = itemsAjustados.some(it => it.devuelto > 0);
    estadoEntrega = hayAjuste ? 'PARCIAL' : 'ENTREGADO';

    if (hayAjuste) {
      itemsEntregados = itemsAjustados.map(it => ({
        codigo: it.codigo, nombre: it.nombre, unidad: it.unidad || 'und',
        cantidad_pedida: it.pedido, cantidad_entregada: it.entregado,
      }));
      p.bultos.forEach(b => {
        const chk = document.getElementById('chk-bulto-dev-' + b.id);
        if (chk && chk.checked) bultosDevolucion.push(b.id);
      });
      if (!obs) {
        alerta('Escribe una observación: qué se entregó y qué se devolvió', 'error');
        document.getElementById('cond-obs')?.focus();
        return;
      }
    }
  }

  // Crédito confirmado por Siesa: no hay campo de forma de pago en el DOM
  // (no tiene sentido preguntarlo, ya lo sabemos) — se fija directo.
  const cobraEnLaPuerta = estadoEntrega === 'ENTREGADO' || estadoEntrega === 'PARCIAL';
  const esCredito = cobraEnLaPuerta && el._modoPago === 'CREDITO';
  const formaPago = esCredito ? 'CREDITO' : (document.getElementById('cond-forma-pago')?.value || '');
  if (cobraEnLaPuerta && !esCredito && !formaPago) {
    alerta('Selecciona la forma de pago antes de confirmar', 'error');
    document.getElementById('cond-forma-pago')?.focus();
    return;
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

  // ── Monto: crédito confirmado → 0 fijo, nada que preguntar. Si no, Valor a
  // Cobrar dinámico (Total/Parcial) cuando hay datos reales de Siesa, o
  // campo libre si no — igual en entrega completa o con ajuste.
  let montoFinal = 0;
  let motivoDescuentoFinal = null;
  let montoDescuentoFinal = 0;

  if (esCredito) {
    // montoFinal ya es 0 — no hay nada que leer del DOM en este modo.
  } else if (cobraEnLaPuerta && el._mostrarValorDinamico) {
    const valorBase = el._valorAjustado != null ? el._valorAjustado : Math.round(p.valor_factura);
    const tipoPago = el._tipoPagoSel || 'TOTAL';
    if (tipoPago === 'TOTAL') {
      montoFinal = valorBase;
    } else {
      const montoParcial = parseInt(document.getElementById('cond-monto-parcial')?.value, 10) || 0;
      if (montoParcial <= 0) {
        alerta('Ingresa el monto que pagó el cliente', 'error');
        document.getElementById('cond-monto-parcial')?.focus();
        return;
      }
      if (montoParcial >= valorBase) {
        alerta('El monto es igual o mayor al valor a cobrar — usa "Pago Total"', 'error');
        document.getElementById('cond-monto-parcial')?.focus();
        return;
      }
      const motivo = document.getElementById('cond-motivo-descuento')?.value || '';
      if (!motivo) {
        alerta('Selecciona el motivo del descuento', 'error');
        document.getElementById('cond-motivo-descuento')?.focus();
        return;
      }
      montoFinal = montoParcial;
      motivoDescuentoFinal = motivo;
      montoDescuentoFinal = valorBase - montoParcial;
    }
  } else if (cobraEnLaPuerta) {
    montoFinal = parseFloat(document.getElementById('cond-monto')?.value || 0) || 0;
    if (estadoEntrega === 'PARCIAL' && montoFinal <= 0) {
      alerta('Ingresa el monto cobrado por la parte entregada', 'error');
      document.getElementById('cond-monto')?.focus();
      return;
    }
  }

  const payload = {
    estado_entrega:    estadoEntrega,
    forma_pago:        formaPago || null,
    // Qué modo tenía la pantalla al confirmar, no solo qué eligió el
    // conductor. En LIBRE puede marcar CREDITO en una parada de contado, y
    // sin registrarlo esa frecuencia no se puede contar. Viaja en el payload
    // —y no se recalcula en el servidor— porque la confirmación tiene que
    // funcionar offline, sin volver a preguntarle a Siesa.
    modo_pantalla:     el._modoPago || null,
    motivo_rechazo:    document.getElementById('cond-motivo')?.value || null,
    monto_cobrado:     montoFinal,
    motivo_descuento:  motivoDescuentoFinal,
    monto_descuento:   montoDescuentoFinal,
    observaciones:     obs || null,
    foto_entrega:      fotoBase64 || null,
    bultos_rechazados: estadoEntrega === 'RECHAZADO' ? bultosRechazados : bultosDevolucion,
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
        monto_cobrado:         montoFinal,
        motivo_descuento:      motivoDescuentoFinal,
        monto_descuento:       montoDescuentoFinal,
        observaciones:         obs || null,
        foto_entrega:          fotoBase64 || null,
        bultos_rechazados_ids: payload.bultos_rechazados,
        items_entregados:      itemsEntregados.length ? itemsEntregados : null,
      };
      const gestionadas = _COND_PARADAS.filter(x => x.recaudo).length;
      await _condDB.set('paradas_' + _COND_RUTA_ACTIVA.id, { paradas: _COND_PARADAS, facturas_gestionadas: gestionadas, paradas_gestionadas: gestionadas });
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

/**
 * Vuelve a la vista de paradas desde el formulario de confirmacion.
 * @param {boolean} [recargar=false] - Si true, recarga los datos del servidor
 */
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


/** Cierra la ruta del conductor marcandola como entregada (online u offline). */
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

/** Registra los listeners de online/offline para el modulo conductor. */
function _condIniciarOffline() {
  if (!_COND_OFFLINE_INIT) {
    _COND_OFFLINE_INIT = true;
    window.addEventListener('online',  () => { _condActualizarBarras(); condSyncQueue(); });
    window.addEventListener('offline', () => { _condActualizarBarras(); });
  }
  _condActualizarBarras();
}

/** Actualiza las barras de estado offline y sincronizacion pendiente. */
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

/** Sincroniza la cola offline de confirmaciones pendientes con el servidor. */
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

/**
 * Fuerza el cierre de una ruta en transito, auto-rechazando paradas sin gestionar.
 * @param {number} id - ID de la ruta
 */
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

/**
 * Abre el modal de planilla de cuadre financiero para una ruta.
 * @param {number} id - ID de la ruta
 */
async function rutaVerPlanilla(id) {
  _PLAN_RUTA_ID = id;
  const modal = document.getElementById('modal-planilla');
  if (!modal) return;
  document.getElementById('modal-planilla-body').innerHTML =
    '<div style="text-align:center;padding:60px;color:#555;">Cargando planilla...</div>';
  modal.style.display = 'flex';
  await _cargarPlanilla(id);
}

/**
 * Carga y renderiza el contenido de la planilla de cuadre dentro del modal.
 * @param {number} id - ID de la ruta
 */
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
            ${r && d.estado_financiero === 'LIQUIDADA' ? `<span style="margin-left:8px;">
              ${r.siesa_nc_triggered ? '<span title="Nota crédito enviada" style="color:#60a5fa;">NC</span>' : ''}
              ${r.siesa_rc_triggered ? '<span title="Recibo de caja enviado" style="color:#4ade80;margin-left:4px;">RC</span>' : ''}
              ${r.siesa_dc_triggered ? '<span title="Documento contable enviado" style="color:#c084fc;margin-left:4px;">DC</span>' : ''}
            </span>` : ''}
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
            Liquidar Ruta — ${fmt(total)}
          </button>
        </div>`;
    } else if (d.estado_financiero === 'LIQUIDADA') {
      // Detectar si hay documentos Siesa pendientes de enviar
      const hayPendientesSiesa = (d.paradas || []).some(p => {
        const r = p.recaudo;
        if (!r) return false;
        if (r.estado_entrega === 'RECHAZADO' && !r.siesa_nc_triggered) return true;
        if (r.estado_entrega === 'PARCIAL' && !r.siesa_nc_triggered) return true;
        if (r.forma_pago && r.forma_pago !== 'CREDITO' && r.forma_pago !== 'EXENTO' && r.estado_entrega !== 'RECHAZADO' && !r.siesa_rc_triggered) return true;
        return false;
      });
      if (hayPendientesSiesa) {
        html += `
          <div style="position:sticky;bottom:0;padding-top:12px;background:var(--bg,#0a0a0a);">
            <button onclick="rutaLiquidarSiesa(${ruta.id})"
              style="width:100%;padding:18px;background:#1e3a5f;color:#60a5fa;border:none;border-radius:12px;font-size:16px;font-weight:800;cursor:pointer;">
              Enviar a Siesa (NC/RC/DC)
            </button>
          </div>`;
      }
    } else if (d.sin_gestionar > 0) {
      html += `
        <div style="background:#1a1a0d;border:1px solid #78350f;border-radius:10px;padding:12px;margin-top:8px;text-align:center;color:#fbbf24;font-size:13px;">
          Faltan ${d.sin_gestionar} parada${d.sin_gestionar !== 1 ? 's' : ''} por gestionar
        </div>`;
    }

    body.innerHTML = html;
  } catch (e) {
    const body = document.getElementById('modal-planilla-body');
    if (body) body.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando planilla</div>';
  }
}

/**
 * Liquida financieramente una ruta previa confirmacion del usuario.
 * @param {number} id - ID de la ruta a liquidar
 */
async function rutaLiquidar(id) {
  if (!confirm(`¿Liquidar Ruta #${id}?\nEsto confirma el cuadre financiero en WMS.\nLuego usa el módulo Liquidación para documentar NC/RC/DC en Siesa por parada.`)) return;
  try {
    const r = await fetch(API + '/api/rutas/' + id + '/liquidar', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (!r.ok) {
      alerta(d.error || 'Error al liquidar', 'error');
      return;
    }
    alerta('Ruta liquidada en WMS — documenta NC/RC/DC desde el módulo Liquidación', 'exito');
    // NO auto-fire Siesa — el operador decide per-parada en Liquidación
    await _cargarPlanilla(id);
    await cargarListaRutas();
  } catch (e) { alerta('Error de conexión', 'error'); }
}

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

/** Cierra el modal de planilla de cuadre y limpia el estado. */
function cerrarModalPlanilla() {
  const modal = document.getElementById('modal-planilla');
  if (modal) modal.style.display = 'none';
  _PLAN_RUTA_ID = null;
}

/** Le da cuenta PWA a un conductor que ya existe, sin duplicar su ficha.
 *
 * El alta normal de usuario crea OTRO conductor y la cédula única lo rechaza.
 * Este camino solo escribe `usuario_id` en la fila que ya está — el histórico
 * de rutas del conductor se queda donde estaba.
 */
async function conductorCrearCuenta(id, nombre) {
  const email = prompt(`Correo para la cuenta de ${nombre}:`);
  if (!email) return;
  const password = prompt('Contraseña inicial (el conductor la usa para entrar):');
  if (!password) return;
  try {
    const r = await fetch(API + '/api/rutas/conductores/' + id + '/cuenta', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ email: email.trim(), password: password }),
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'No se pudo crear la cuenta', 'error'); return; }
    alerta(`${nombre} ya puede entrar con ${email.trim()}`, 'exito');
    cargarListaConductores();
  } catch (e) {
    alerta('Sin conexión: ' + e.message, 'error');
  }
}

/** Salta al expediente del vehículo en el módulo Flota.
 *
 * Resuelve la duplicación que confunde desde el primer día: este maestro tiene
 * el alta y la baja; el expediente —ficha, documentos, custodia, odómetro— vive
 * en el módulo Flota. Sin este enlace, quien entra por Rutas no encuentra la
 * ficha, y quien entra por Flota no encuentra dónde dar de alta.
 *
 * Un maestro, un expediente, y un camino claro entre los dos.
 */
function verExpedienteVehiculo(placa) {
  tab('tab-flota');
  // `cargarFlota` es asíncrona: se espera a que pinte la lista antes de abrir
  // la ficha, o el modal escribiría sobre un contenedor que todavía no existe.
  cargarFlota().then(() => flotaAbrirFicha(placa));
}

// ─────────────────────────────────────────────────────────────
// MANIFIESTO DE CARGA — el papel que va con el conductor
// ─────────────────────────────────────────────────────────────

/** Imprime el manifiesto del día: qué bultos salen, agrupados por destino.
 *
 * `/api/muelle/manifiesto` existía desde antes y **no lo llamaba ninguna
 * pantalla**. El servicio ya agrupa por parada y cuenta bultos por tipo; lo que
 * faltaba era el gesto.
 *
 * Por qué importa que sea papel y no una pantalla: es lo que el conductor lleva
 * y contra lo que el cliente firma. Hoy esa cuenta se hace de memoria, y una
 * caja de menos aparece tres días después sin forma de saber si salió.
 *
 * El documento se arma acá porque el endpoint devuelve datos, no HTML. El
 * criterio para eso es del servidor —qué bultos, qué agrupación, qué día
 * operativo— y esta función solo lo dibuja.
 */
async function muelleImprimirManifiesto() {
  let d;
  try {
    d = await get('/api/muelle/manifiesto');
  } catch (e) {
    alerta('No se pudo traer el manifiesto: ' + e.message, 'error');
    return;
  }
  const paradas = d.manifiesto || [];
  if (!paradas.length) {
    alerta('No hay bultos cargados hoy — no hay manifiesto que imprimir', 'advertencia');
    return;
  }

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  const cuerpo = paradas.map(par => `
    <h2>${esc(par.destino)} <span class="tot">${par.total_bultos} bulto(s)</span></h2>
    <table>
      <thead><tr><th>Pedido</th><th>Cliente</th><th>Bultos</th><th class="firma">Recibido</th></tr></thead>
      <tbody>${par.pedidos.map(p => `
        <tr>
          <td>${esc(p.numero_pedido)}</td>
          <td>${esc(p.cliente)}</td>
          <td>${p.total_bultos} · ${Object.entries(p.resumen || {})
                .map(([t, n]) => `${esc(t)}×${n}`).join(' ')}</td>
          <td class="firma"></td>
        </tr>`).join('')}
      </tbody>
    </table>`).join('');

  const html = `<!doctype html><html lang="es"><head><meta charset="utf-8">
    <title>Manifiesto de carga ${esc(d.fecha)}</title>
    <style>
      body{font-family:system-ui,-apple-system,sans-serif;margin:24px;color:#000}
      h1{font-size:20px;margin:0 0 2px}
      .sub{color:#555;font-size:13px;margin-bottom:18px}
      h2{font-size:15px;margin:20px 0 6px;border-bottom:2px solid #000;padding-bottom:3px}
      .tot{font-weight:400;color:#555;font-size:13px}
      table{width:100%;border-collapse:collapse;font-size:13px}
      th,td{border:1px solid #999;padding:6px 8px;text-align:left;vertical-align:top}
      th{background:#eee}
      /* La columna de firma se imprime vacía a propósito: es donde el cliente
         firma que recibió esos bultos. Un manifiesto sin dónde firmar es una
         lista, no un comprobante. */
      .firma{width:26%}
      tbody .firma{height:34px}
      .pie{margin-top:26px;font-size:12px;color:#555;display:flex;
           justify-content:space-between;gap:30px}
      .pie div{border-top:1px solid #000;padding-top:4px;flex:1}
      @media print{body{margin:10mm} .noprint{display:none}}
    </style></head><body>
    <h1>Manifiesto de carga</h1>
    <div class="sub">${esc(d.fecha)} · ${d.total_bultos} bulto(s) · ${paradas.length} parada(s)</div>
    ${cuerpo}
    <div class="pie">
      <div>Entregó (bodega)</div><div>Recibió (conductor)</div><div>Placa</div>
    </div>
    <button class="noprint" onclick="window.print()"
      style="margin-top:20px;padding:10px 18px;font-size:14px;cursor:pointer">Imprimir</button>
    </body></html>`;

  const ventana = window.open('', '_blank');
  if (!ventana) {
    alerta('El navegador bloqueó la ventana emergente — permití popups', 'advertencia');
    return;
  }
  ventana.document.write(html);
  ventana.document.close();
}
