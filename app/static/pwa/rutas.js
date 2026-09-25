// ══════════════════════════════════════════════════════════════════
// RUTAS — Muelle, conductor, planilla, maestras, vehículos
// Dependencias globales (de app.js): get(), post(), put(), alerta(),
//   flash(), set(), pantalla(), abrirCamara(), cerrarCamara(),
//   API, TOKEN, ALMACEN_ID, OPERARIO
// ══════════════════════════════════════════════════════════════════

// ─── ESTADO DE ENTREGA → estilo (una política para todo rutas.js) ─────────────
//
// Un solo mapa por estado de entrega, con TODOS los valores del modelo
// (`EstadoEntrega.TODOS` en app/models/recaudo_entrega.py), y un estilo neutro
// para el que no conozca. Antes había dos mapas locales con tres de los cuatro
// estados: con una parada ENTREGADO_SIN_PAGO, `MAPA[est].badgeBg` reventaba
// (TypeError), la lista del conductor quedaba en «Cargando paradas...» y con
// ella se iba el botón «Cerrar Ruta». Un estado que el servidor agregue mañana
// se pinta neutro: nunca revienta la pantalla.
// Trinquete: tests/test_mapas_estado_entrega_pwa.py.
const ENTREGA_ESTILO = {
  ENTREGADO:          { borde: '#15803d', fondo: '#f0fdf4', badgeBg: '#dcfce7', badgeColor: '#15803d', label: 'ENTREGADO',  etiqueta: '✓ Entregado' },
  PARCIAL:            { borde: '#d97706', fondo: '#fffbeb', badgeBg: '#fef3c7', badgeColor: '#b45309', label: 'PARCIAL',    etiqueta: '⚠ Parcial' },
  RECHAZADO:          { borde: '#dc2626', fondo: '#fef2f2', badgeBg: '#fee2e2', badgeColor: '#b91c1c', label: 'RECHAZADO',  etiqueta: '✗ Rechazado' },
  ENTREGADO_SIN_PAGO: { borde: '#c2410c', fondo: '#fff7ed', badgeBg: '#ffedd5', badgeColor: '#9a3412', label: 'SIN PAGO',   etiqueta: '⚠ Se quedó sin pagar' },
};
const ENTREGA_ESTILO_NEUTRO = { borde: '#d1d5db', fondo: '#f9fafb', badgeBg: '#f3f4f6', badgeColor: '#4b5563', label: 'OTRO ESTADO', etiqueta: 'Estado no reconocido' };
const ENTREGA_ESTILO_PENDIENTE = { borde: '#d1d5db', fondo: '#f9fafb', badgeBg: '#f3f4f6', badgeColor: '#6b7280', label: 'PENDIENTE', etiqueta: 'Sin gestionar' };

/**
 * Estilo de una parada según su estado de entrega. Sin recaudo → pendiente;
 * estado desconocido → neutro (nunca `undefined`).
 * @param {string|null} est - `recaudo.estado_entrega`
 */
function estiloEntrega(est) {
  if (!est) return ENTREGA_ESTILO_PENDIENTE;
  return Object.prototype.hasOwnProperty.call(ENTREGA_ESTILO, est) ? ENTREGA_ESTILO[est] : ENTREGA_ESTILO_NEUTRO;
}

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
        <div style="flex:1;font-size:var(--fs-sm);font-weight:700;color:var(--warn-tx);text-transform:uppercase;letter-spacing:.06em;">
          📍 ${esc(g.destino)}
          <span style="color:var(--tx3);font-weight:400;font-size:var(--fs-xs);">(${esc(g.total)} pieza${g.total !== 1 ? 's' : ''})</span>
        </div>
        <div style="display:flex;gap:4px;">
          ${gi > 0
            ? `<button onclick="muelleMoverGrupo(${gi},-1)" style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);width:44px;height:44px;border-radius:6px;cursor:pointer;font-size:var(--fs-sm);">↑</button>`
            : `<div style="width:44px;"></div>`}
          ${gi < grupos.length - 1
            ? `<button onclick="muelleMoverGrupo(${gi},1)" style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);width:44px;height:44px;border-radius:6px;cursor:pointer;font-size:var(--fs-sm);">↓</button>`
            : `<div style="width:44px;"></div>`}
        </div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);min-width:40px;text-align:right;">
          Carga<br>#${gi + 1}
        </div>
      </div>
      ${g.bultos.map((b, bi) => `
        <div id="muelle-bulto-${esc(b.id)}" class="tabla-card" style="border-left:3px solid #f59e0b;margin-bottom:8px;transition:opacity .3s;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <div style="font-size:var(--fs-sm);font-weight:700;font-family:monospace;">${esc(b.codigo_barras)}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:2px;">${esc(b.numero_pedido)} · ${esc(b.cliente || '')}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(b.tipo)} · pieza ${esc(b.numero)} de ${esc(b.total)}</div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:20px;color:var(--tx3);font-weight:800;">${bi + 1}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);">LIFO</div>
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
  if (btnActivar) btnActivar.style.display = (OPERARIO && OPERARIO.puede_usar_camara) ? 'block' : 'none';
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
async function abrirCamaraMuelle(btnEl = null) {
  await abrirCamara('lector-qr-muelle', 'camara-box-muelle', async cod => {
    await cerrarCamara('camara-box-muelle');
    const input = document.getElementById('muelle-scan-input');
    if (input) input.value = cod.toUpperCase();
    const campo = document.getElementById('muelle-scan-campo');
    if (campo) campo.style.display = 'flex';
    await muelleCargarCaja();
  }, btnEl);
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
    if (info) { info.style.color = 'var(--tx3)'; info.textContent = 'Sin ruta — los bultos no se asignarán a ningún viaje.'; }
    if (scanLabel) scanLabel.textContent = 'ESCANEAR CAJA AL CARGAR VEHÍCULO';
  } else {
    const sel = document.getElementById('muelle-ruta-select');
    const txt = sel?.options[sel.selectedIndex]?.textContent || '';
    if (info) { info.style.color = 'var(--ok-tx)'; info.textContent = `Ruta activa: ${txt}`; }
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
    el.innerHTML = `<div style="color:var(--err-tx);text-align:center;padding:40px;">
      Error cargando muelle<br>
      <span style="font-size:var(--fs-xs);color:var(--tx3);">${esc(e.message || 'Error desconocido')}</span>
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
    el.innerHTML = '<div style="color:var(--ok-tx);text-align:center;padding:40px;font-size:32px;">✓<br><span style="font-size:var(--fs-sm);">Sin bultos pendientes</span></div>';
    return;
  }

  el.innerHTML = `
    <div style="background:var(--bg-input);border-radius:10px;padding:12px;margin-bottom:16px;text-align:center;font-size:var(--fs-sm);color:var(--tx3);">
      Selecciona una ruta arriba para empezar a planificar el cargue
    </div>
    ${grupos.map(g => `
      <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="font-size:var(--fs-sm);font-weight:700;color:var(--warn-tx);">📍 ${esc(g.destino)}
          <span style="font-size:var(--fs-xs);color:var(--tx3);font-weight:400;"> · ${esc(g.total)} bulto${g.total !== 1 ? 's' : ''}</span>
        </div>
        ${g.bultos.map(b => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid var(--brd);margin-top:6px;">
            <div>
              <span style="font-family:monospace;font-size:var(--fs-sm);color:var(--tx);">${esc(b.codigo_barras)}</span>
              <span style="font-size:var(--fs-xs);color:var(--tx3);margin-left:8px;">${esc(b.tipo)} ${esc(b.numero)}/${esc(b.total)}</span>
            </div>
            <span style="font-size:var(--fs-xs);color:var(--tx3);">${esc(b.numero_pedido)}</span>
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
      <div style="background:var(--ok-bg);border:1px solid var(--ok-brd);border-radius:12px;padding:14px;margin-bottom:16px;text-align:center;">
        <div style="font-size:var(--fs-sm);color:var(--ok-tx);font-weight:700;margin-bottom:8px;">✓ Los ${totalConf} bulto${totalConf !== 1 ? 's' : ''} de esta ruta ya están cargados</div>
        <button onclick="conBotonOcupado(event, () => muelleConfirmarCargueCompleto(${rutaId}))"
          style="width:100%;padding:14px;background:#14532d;color:#bbf7d0;border:none;border-radius:10px;font-size:var(--fs-md);font-weight:800;cursor:pointer;">
          🚛 Confirmar cargue completo — Ruta lista para salir
        </button>
      </div>`;
  }

  // — Sección 1: bultos ya en la ruta —
  if (_RUTA_MANIFIESTO_ACTUAL.length) {
    html += `<div style="font-size:var(--fs-sm);font-weight:600;color:var(--tx3);text-transform:uppercase;letter-spacing:.08em;margin-bottom:10px;">
      En esta ruta · ${totalConf} confirmado${totalConf !== 1 ? 's' : ''} · ${totalPlan} por confirmar
    </div>`;
    html += _RUTA_MANIFIESTO_ACTUAL.map((grupo, gi) =>
      _htmlGrupoRuta(grupo, gi, _RUTA_MANIFIESTO_ACTUAL.length, rutaId)
    ).join('');
  } else {
    html += `
      <div style="text-align:center;padding:20px;background:var(--bg-s);border-radius:12px;border:1px dashed var(--brd);margin-bottom:16px;">
        <div style="font-size:var(--fs-2xl);margin-bottom:6px;">🚛</div>
        <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">Ruta vacía</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">Asigna pedidos desde la lista de abajo</div>
      </div>`;
  }

  // — Sección 2: pendientes sin asignar —
  if (gruposPendientes.length) {
    html += `
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--brd);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
          <span style="font-size:var(--fs-sm);font-weight:600;color:var(--tx3);text-transform:uppercase;letter-spacing:.08em;">📦 Pendientes por asignar</span>
          <span style="background:var(--bg-s2);color:var(--tx);font-size:var(--fs-sm);padding:2px 10px;border-radius:10px;">${esc(dPendientes.total_bultos)}</span>
        </div>
        ${gruposPendientes.map(g => _htmlGrupoPendiente(g, rutaId)).join('')}
      </div>`;
  } else if (_RUTA_MANIFIESTO_ACTUAL.length > 0) {
    html += `
      <div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--brd);text-align:center;font-size:var(--fs-sm);color:var(--tx3);">
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
    const { r, d } = await _rutaPostConFlota('/api/rutas/' + rutaId + '/cerrar', 'despachar');
    if (!r) return;
    if (r.ok) {
      const _avisoCobro = _rutaAvisoCobro(d);
      alerta(`Ruta #${rutaId} confirmada — salió a reparto${_avisoCobro}`,
             _avisoCobro ? 'advertencia' : 'exito');
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
          <span style="font-size:var(--fs-md);font-weight:800;color:${todoConfirmado ? 'var(--ok-tx)' : 'var(--warn-tx)'};text-transform:uppercase;">
            📍 ${esc(grupo.destino)}
          </span>
          <span style="font-size:var(--fs-sm);color:var(--tx3);"> · ${confirmados}/${totalGrupo} conf.</span>
        </div>
        <div style="display:flex;gap:4px;">
          ${gi > 0
            ? `<button onclick="rutaMoverGrupo(${gi},-1,${rutaId})" style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);width:44px;height:44px;border-radius:6px;cursor:pointer;font-size:var(--fs-sm);">↑</button>`
            : `<div style="width:44px;"></div>`}
          ${gi < totalGrupos - 1
            ? `<button onclick="rutaMoverGrupo(${gi},1,${rutaId})" style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);width:44px;height:44px;border-radius:6px;cursor:pointer;font-size:var(--fs-sm);">↓</button>`
            : `<div style="width:44px;"></div>`}
        </div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);text-align:right;min-width:40px;">Parada<br>#${gi + 1}</div>
      </div>
      ${grupo.bultos.map(b => {
        const conf = b.estado === 'CARGADO';
        return `
          <div style="background:var(--bg-s);border:1px solid ${conf ? 'var(--ok-brd)' : 'var(--brd)'};border-left:4px solid ${conf ? '#4ade80' : '#f59e0b'};border-radius:10px;padding:10px 12px;margin-bottom:6px;">
            <div style="display:flex;justify-content:space-between;align-items:center;">
              <div style="flex:1;">
                <div style="font-size:var(--fs-md);font-weight:700;font-family:monospace;color:${conf ? 'var(--tx)' : 'var(--warn-tx)'};">${esc(b.codigo_barras)}</div>
                <div style="font-size:var(--fs-sm);color:var(--tx2);margin-top:2px;">${esc(b.numero_pedido)} · ${esc(b.cliente || '')}</div>
                <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(b.tipo)} · pieza ${esc(b.numero)}/${esc(b.total)}</div>
              </div>
              <div style="display:flex;align-items:center;gap:8px;">
                ${!conf ? `<button onclick="conBotonOcupado(event, () => muelleDesasignar(${esc(b.id)}))" title="Quitar de la ruta" style="background:none;border:none;color:var(--tx3);font-size:var(--fs-lg);cursor:pointer;line-height:1;padding:4px;">×</button>` : ''}
                <span style="background:${conf ? '#14532d' : 'var(--warn-bg)'};color:${conf ? '#4ade80' : '#f59e0b'};font-size:var(--fs-xs);padding:3px 10px;border-radius:20px;font-weight:700;white-space:nowrap;">
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
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:14px;margin-bottom:8px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div>
          <div style="font-size:var(--fs-md);font-weight:800;color:var(--warn-tx);">📍 ${esc(grupo.destino)}</div>
          <div style="font-size:var(--fs-sm);color:var(--tx3);margin-top:2px;">${esc(grupo.total)} bulto${grupo.total !== 1 ? 's' : ''}</div>
        </div>
        <button onclick="conBotonOcupado(event, () => muelleAsignar(null,'${numeroPedido}'))"
          style="background:#fff;color:#000;border:none;padding:8px 14px;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;white-space:nowrap;">
          + Todo el pedido
        </button>
      </div>
      ${grupo.bultos.map(b => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-top:1px solid var(--brd);">
          <div>
            <span style="font-family:monospace;font-size:var(--fs-sm);color:var(--tx);">${esc(b.codigo_barras)}</span>
            <span style="font-size:var(--fs-xs);color:var(--tx2);margin-left:8px;">${esc(b.tipo)} ${esc(b.numero)}/${esc(b.total)}</span>
            ${b.cliente ? `<span style="font-size:var(--fs-xs);color:var(--tx2);margin-left:8px;">· ${esc(b.cliente)}</span>` : ''}
          </div>
          <button onclick="conBotonOcupado(event, () => muelleAsignar(${esc(b.id)},null))"
            style="background:var(--bg-input);color:var(--tx);border:1px solid var(--brd);padding:4px 10px;border-radius:6px;font-size:var(--fs-sm);cursor:pointer;">
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
    feedback.style.color = 'var(--warn-tx)';
    feedback.textContent = '⚠ Selecciona una ruta antes de escanear';
    return;
  }

  feedback.style.color = 'var(--tx2)';
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
        feedback.style.color = 'var(--info-tx)';
        feedback.textContent = `ℹ Ya estaba cargado: ${d.codigo_barras}`;
      } else {
        feedback.style.color = 'var(--ok-tx)';
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
      feedback.style.color = 'var(--err-tx)';
      feedback.textContent = d.error || 'Error de verificación';
      if (navigator.vibrate) navigator.vibrate([100, 50, 100]);
    }
  } catch (e) {
    feedback.style.color = 'var(--err-tx)';
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
      btn.style.background = k === nombre ? 'var(--pm-fill)' : 'none';
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
      el.innerHTML = '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin rutas para este rango de fechas</div>';
      return;
    }
    el.innerHTML = rutas.map(r => rutaCard(r)).join('');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--err-tx);text-align:center;padding:40px;">${esc(e.message || 'Error cargando rutas')}<br><button onclick="cargarListaRutas()" style="margin-top:10px;padding:8px 16px;background:var(--pm-fill);color:#fff;border:none;border-radius:8px;cursor:pointer;">Reintentar</button></div>`;
  }
}

/**
 * Genera el HTML de una tarjeta de ruta con estado, botones de accion y detalles.
 * @param {Object} r - Objeto ruta con id, estado, conductor_nombre, total_bultos, etc.
 * @returns {string} HTML de la tarjeta
 */
function rutaCard(r) {
  const estadoBadge = {
    PROGRAMADO:  '<span style="background:var(--lila-bg);color:var(--lila-tx);padding:3px 10px;border-radius:10px;font-size:var(--fs-xs);font-weight:700;">PROGRAMADO</span>',
    EN_CARGUE:   '<span style="background:#713f12;color:var(--warn-tx);padding:3px 10px;border-radius:10px;font-size:var(--fs-xs);font-weight:700;">EN CARGUE</span>',
    EN_TRANSITO: '<span style="background:#1e3a5f;color:var(--info-tx);padding:3px 10px;border-radius:10px;font-size:var(--fs-xs);font-weight:700;">EN TRÁNSITO</span>',
    ENTREGADA:   '<span style="background:#14532d;color:#bbf7d0;padding:3px 10px;border-radius:10px;font-size:var(--fs-xs);font-weight:700;">ENTREGADA</span>',
  }[r.estado] || r.estado;

  const tipoIcon = r.tipo_ruta === 'Urbana' ? '🏙️' : '🛣️';
  const fechaRef = r.fecha_programada
    ? new Date(r.fecha_programada + 'T00:00:00').toLocaleDateString('es-CO', { weekday:'short', month:'short', day:'numeric' })
    : new Date(r.fecha_creacion).toLocaleString('es-CO', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });

  const btnIniciar = r.estado === 'PROGRAMADO'
    ? `<button onclick="conBotonOcupado(event, () => rutaIniciar(${esc(r.id)}))" style="flex:1;padding:10px;background:var(--lila-bg);color:var(--lila-tx);border:none;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">▶ Iniciar Cargue</button>`
    : '';
  const btnCerrar = r.estado === 'EN_CARGUE'
    ? `<button onclick="conBotonOcupado(event, () => rutaCerrar(${esc(r.id)}))" style="flex:1;padding:10px;background:#1e3a5f;color:var(--info-tx);border:none;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">🚛 Salió</button>`
    : '';
  // EN_TRANSITO: estado informativo — solo el conductor marca como entregada desde su app
  const btnEntregar = r.estado === 'EN_TRANSITO'
    ? `<div style="flex:1;padding:10px;background:#1e3a5f22;color:var(--info-tx);border:1px solid var(--info-brd);border-radius:8px;font-size:var(--fs-sm);font-weight:700;text-align:center;pointer-events:none;">🚛 En camino</div>`
    : '';
  const btnManifiesto = r.total_bultos > 0
    ? `<button onclick="rutaVerManifiesto(${esc(r.id)})" style="flex:1;padding:10px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">📋 Ver</button>`
    : '';
  const btnPlanilla = ['EN_TRANSITO','ENTREGADA'].includes(r.estado)
    ? `<button onclick="rutaVerPlanilla(${esc(r.id)})" style="flex:1;padding:10px;background:var(--lila-bg);color:var(--lila-tx);border:1px solid var(--info-brd);border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">💰 Planilla${r.estado_financiero === 'LIQUIDADA' ? ' ✓' : ''}</button>`
    : '';
  const btnForzarCierre = r.estado === 'EN_TRANSITO'
    ? `<button onclick="conBotonOcupado(event, () => rutaForzarCierre(${esc(r.id)}))" style="flex:1;padding:10px;background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">⚡ Forzar cierre</button>`
    : '';

  return `
    <div id="ruta-card-${esc(r.id)}" style="background:var(--bg-s);border:1px solid ${r.estado === 'PROGRAMADO' ? 'var(--info-brd)' : 'var(--brd)'};border-radius:14px;padding:16px;margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:var(--fs-md);font-weight:800;">${tipoIcon} ${esc(r.ruta_maestra_nombre || 'Ruta')} <span style="color:var(--tx3);font-weight:400;font-size:var(--fs-sm);">#${esc(r.id)}</span></div>
          <div style="font-size:var(--fs-sm);color:var(--tx);margin-top:2px;">${esc(r.conductor_nombre)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:1px;">${r.vehiculo_placa ? r.vehiculo_placa + ' · ' + r.vehiculo_tipo + ' · ' : ''}${fechaRef}</div>
        </div>
        <div style="text-align:right;">
          ${estadoBadge}
          <div style="font-size:20px;font-weight:800;margin-top:6px;">${esc(r.total_bultos)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">
            ${r.total_confirmados > 0 || r.total_planificados > 0
              ? `${esc(r.total_confirmados)} conf · ${esc(r.total_planificados)} plan`
              : 'bultos'}
          </div>
        </div>
      </div>
      ${r.pedidos?.length ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:10px;">Pedidos: ${r.pedidos.join(', ')}</div>` : ''}
      ${r.notas ? `<div style="font-size:var(--fs-xs);color:var(--tx3);font-style:italic;margin-bottom:10px;">"${esc(r.notas)}"</div>` : ''}
      ${(btnIniciar || btnCerrar || btnEntregar || btnManifiesto || btnPlanilla || btnForzarCierre)
        ? `<div style="display:flex;gap:6px;flex-wrap:wrap;">${btnIniciar}${btnCerrar}${btnEntregar}${btnManifiesto}${btnPlanilla}${btnForzarCierre}</div>`
        : ''}
    </div>`;
}

/**
 * POST de una transición de ruta que puede traer advertencias de flota
 * (SOAT/RTM vencido, sin inspección apta hoy, en taller, custodia de otro).
 *
 * **Informa, no bloquea**: si el servidor responde 409 con
 * `advertencias_flota`, se muestran y se pide el motivo; con motivo se reenvía
 * y la ruta sigue — el motivo queda en la bitácora como FORZAR. Cancelar el
 * cuadro no hace nada (devuelve `{r: null}`).
 * @param {string} ruta - '/api/rutas/<id>/iniciar' o '/cerrar'
 * @param {string} accion - para el texto del cuadro
 */
/**
 * El informe de cobro del despacho (`RutaService._informe_de_cobro`) en una
 * frase. Informa, no bloquea. `''` si no hay nada que decir.
 */
function _rutaAvisoCobro(d) {
  const inf = (d && Array.isArray(d.informe_cobro)) ? d.informe_cobro : [];
  if (!inf.length) return '';
  const n = (clave) => inf.filter(x => x.clave === clave).length;
  const partes = [];
  const sup = n('cobro_supuesto'), cont = n('fe_contado'), sald = n('fe_saldada');
  if (sup) partes.push(`${sup} sin condición de pago conocida (se cobran)`);
  if (cont) partes.push(`${cont} declarada${cont !== 1 ? 's' : ''} de contado`);
  if (sald) partes.push(`${sald} ya pagada${sald !== 1 ? 's' : ''} según la cartera (no cobrar otra vez)`);
  return partes.length ? ' · Facturas: ' + partes.join(', ') : '';
}

async function _rutaPostConFlota(ruta, accion) {
  const enviar = (cuerpo) => fetch(API + ruta, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(cuerpo || {}),
  });
  let r = await enviar({});
  let d = await r.json().catch(() => ({}));
  if (r.status === 409 && Array.isArray(d.advertencias_flota)) {
    const lista = d.advertencias_flota.map(a => `<li>${esc(a.texto)}</li>`).join('');
    const motivo = await _modalTexto(
      '⚠️ Advertencias del vehículo',
      `<ul style="margin:0 0 10px;padding-left:18px;">${lista}</ul>` +
      `Podés ${esc(accion)} igual. El motivo queda registrado.`,
      { placeholder: 'Motivo', textoConfirmar: 'Continuar' });
    if (motivo === null || motivo === undefined || !String(motivo).trim()) return { r: null, d: null };
    r = await enviar({ motivo_advertencias: motivo.trim() });
    d = await r.json().catch(() => ({}));
  }
  return { r, d };
}

/**
 * Inicia el cargue de una ruta programada y redirige al muelle.
 * @param {number} id - ID de la ruta a iniciar
 */
async function rutaIniciar(id) {
  try {
    const { r, d } = await _rutaPostConFlota('/api/rutas/' + id + '/iniciar', 'iniciar el cargue');
    if (!r) return;
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
    const { r, d } = await _rutaPostConFlota('/api/rutas/' + id + '/cerrar', 'despachar');
    if (!r) return;
    if (r.ok) {
      // Actualizar card sin recargar todo
      const card = document.getElementById('ruta-card-' + id);
      if (card) card.outerHTML = rutaCard(d.ruta);
      // Limpiar ruta activa si era esta
      if (RUTA_ACTIVA_ID === id) { RUTA_ACTIVA_ID = null; }
      await cargarRutaSelector();
      const _avisoCobro = _rutaAvisoCobro(d);
      if (_avisoCobro) alerta('Ruta despachada' + _avisoCobro, 'advertencia');
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
      ? `<span style="color:var(--err-tx);font-weight:700;">${rechazados} bulto${rechazados !== 1 ? 's' : ''} marcado${rechazados !== 1 ? 's' : ''} como rechazado${rechazados !== 1 ? 's' : ''}</span> — aparecerán en Devoluciones`
      : `<span style="color:var(--ok-tx);">Todos los bultos entregados</span>`;

  el.innerHTML = _ENTREGA_BULTOS.map((b, i) => `
    <div style="background:${b.entregado ? 'var(--ok-bg)' : 'var(--err-bg)'};border:1px solid ${b.entregado ? 'var(--ok-brd)' : 'var(--err-brd)'};border-radius:10px;padding:12px;margin-bottom:8px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(b.codigo_barras)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(b.tipo)} ${esc(b.numero)}/${esc(b.total)} · ${esc(b.numero_pedido)} · ${esc(b.cliente || '—')}</div>
          ${!b.entregado ? `<select onchange="_setMotivo(${i}, this.value)"
            style="margin-top:8px;width:100%;padding:6px;background:var(--bg-input);border:1px solid var(--brd);color:var(--tx);border-radius:6px;font-size:var(--fs-xs);">
            ${MOTIVOS_RECHAZO.map(m => `<option value="${m}" ${b.motivo_rechazo===m?'selected':''}>${m}</option>`).join('')}
          </select>` : ''}
        </div>
        <button onclick="_toggleEntrega(${i})"
          style="flex-shrink:0;padding:8px 14px;background:${b.entregado ? '#166534' : '#7f1d1d'};color:${b.entregado ? '#4ade80' : '#f87171'};border:none;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;white-space:nowrap;">
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
      const sinDeclarar = (d.sin_declarar || []).length;
      if (sinDeclarar > 0) {
        // No se dan por entregados: conservan su estado y hay que ir a mirarlos.
        alerta(`Ruta entregada · ${sinDeclarar} bulto${sinDeclarar !== 1 ? 's' : ''} sin declarar — quedaron como estaban`, 'advertencia');
      } else if (rechazados > 0) {
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
    // El fallo de la planilla NO se convierte en «no hay paradas».
    //
    // `GET /<id>/planilla` es `_es_admin_o_jefe`, pero `GET /<id>` no: un
    // supervisor abre el manifiesto (200) y recibe 403 en el detalle. Con
    // `.catch(() => ({paradas: []}))` eso se pintaba como «Sin paradas
    // registradas» — medido el 2026-09-21 contra la ruta 31, que tiene tres.
    // Un no-sé pintado como un hecho es peor que un error: no se reporta.
    const [dr, dp] = await Promise.all([
      get('/api/rutas/' + id),
      get('/api/rutas/' + id + '/planilla').then(
        d => ({ ok: true, paradas: d.paradas || [] }),
        e => ({ ok: false, status: e && e.status, paradas: [] })),
    ]);
    const ruta    = dr.ruta;
    const paradas = dp.paradas;

    // Paleta modo día: `estiloEntrega` (todos los estados + neutro).

    let filas = '';
    if (paradas.length) {
      paradas.forEach(p => {
        const r   = p.recaudo;
        const est = r ? r.estado_entrega : null;
        const e   = estiloEntrega(est);

        filas += `<div style="background:${esc(e.fondo)};border:1px solid ${esc(e.borde)};border-radius:10px;padding:12px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div>
              <div style="font-size:var(--fs-sm);font-weight:800;color:var(--tx);">${esc(p.numero_pedido)}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(p.cliente)} · 📍 ${esc(p.municipio)}</div>
            </div>
            <span style="font-size:var(--fs-xs);font-weight:700;color:${esc(e.badgeColor)};background:${esc(e.badgeBg)};padding:3px 10px;border-radius:8px;">${esc(e.etiqueta)}</span>
          </div>`;

        // Bultos
        const rechazadosIds = new Set(r ? (r.bultos_rechazados_ids || []) : []);
        filas += `<div style="font-size:var(--fs-xs);margin-bottom:${est && est !== 'ENTREGADO' ? 6 : 0}px;">`;
        (p.bultos_detalle || []).forEach(b => {
          const rechazado = rechazadosIds.has(b.id);
          filas += `<span style="color:${rechazado ? 'var(--err-tx)' : 'var(--ok-tx)'};margin-right:8px;">
            ${rechazado ? '✗' : '✓'} ${esc(b.codigo_barras)} (${esc(b.tipo)} ${esc(b.numero)}/${esc(b.total)})</span>`;
        });
        filas += '</div>';

        // Detalle PARCIAL
        if (est === 'PARCIAL' && r.items_entregados && r.items_entregados.length) {
          filas += `<div style="margin-top:8px;padding-top:8px;border-top:1px solid #fde68a;">
            <div style="font-size:var(--fs-xs);color:var(--warn-tx);font-weight:700;margin-bottom:4px;">DETALLE PARCIAL</div>
            <div style="display:grid;grid-template-columns:1fr auto auto;gap:3px 10px;font-size:var(--fs-xs);">
              ${r.items_entregados.map(it => `
                <div style="color:var(--tx2);">${esc(it.nombre || it.codigo)}</div>
                <div style="color:var(--ok-tx);text-align:right;font-weight:700;">✓ ${esc(it.cantidad_entregada)}</div>
                <div style="color:${it.cantidad_devuelta > 0 ? 'var(--err-tx)' : 'var(--tx3)'};text-align:right;font-weight:700;">↩ ${esc(it.cantidad_devuelta)}</div>
              `).join('')}
            </div>
          </div>`;
        }

        // Motivo + evidencia RECHAZADO
        if (est === 'RECHAZADO') {
          if (r.observaciones) {
            filas += `<div style="margin-top:6px;font-size:var(--fs-xs);color:var(--err-tx);font-style:italic;">"${esc(r.observaciones)}"</div>`;
          }
          if (r.foto_entrega) {
            filas += `<button onclick="(function(){const w=window.open();w.document.write('<img src=\\'data:image/jpeg;base64,${esc(r.foto_entrega)}\\' style=\\'max-width:100%;\\'>');w.document.title='Evidencia ${esc(p.numero_pedido)}';})()"
              style="margin-top:8px;padding:6px 14px;background:#fee2e2;color:var(--err-tx);border:1px solid #dc2626;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">
              📷 Ver evidencia fotográfica
            </button>`;
          }
        }

        filas += '</div>';
      });
    } else if (!dp.ok) {
      filas = `<div style="color:var(--warn-tx);background:#fffbeb;border:1px solid #fcd34d;border-radius:10px;text-align:center;padding:20px;font-size:var(--fs-sm);">
        ${dp.status === 403
          ? 'No tienes permiso para ver el detalle de paradas de esta ruta.'
          : 'No se pudo cargar el detalle de paradas.'}
        <div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:6px;">La ruta puede tener paradas — esta pantalla no las pudo leer.</div>
      </div>`;
    } else {
      filas = '<div style="color:var(--tx2);text-align:center;padding:20px;">Sin paradas registradas</div>';
    }

    const modal = document.createElement('div');
    modal.className = 'tema-claro-fijo';   // paleta modo día: tokens claros en los dos temas
    modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px;';
    modal.innerHTML = `
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:20px;max-width:560px;width:100%;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 20px 40px rgba(0,0,0,.15);">
        <div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);margin-bottom:4px;">${esc(ruta.ruta_maestra_nombre || 'Ruta')} <span style="color:var(--tx3);font-weight:400;font-size:var(--fs-sm);">#${esc(ruta.id)}</span></div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:14px;">${esc(ruta.conductor_nombre)} · ${esc(ruta.tipo_ruta)} · ${dp.ok ? `${esc(paradas.length)} pedido${paradas.length !== 1 ? 's' : ''}` : 'pedidos: sin dato'}</div>
        <div style="overflow-y:auto;flex:1;">${filas}</div>
        <button onclick="this.closest('div[style*=fixed]').remove()" style="margin-top:16px;padding:10px;background:#f3f4f6;color:var(--tx2);border:1px solid #e5e7eb;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;width:100%;font-weight:600;">Cerrar</button>
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

/**
 * Cuantos clientes ya tienen coordenada y cuantos no.
 *
 * Ese numero subiendo mes a mes es el activo entero de la captura del
 * conductor. Vive en Maestras —el catalogo de recorridos— y se carga solo al
 * abrir la pestana: un numero que hay que ir a buscar con un boton es un
 * numero que nadie mira, y a los tres meses nadie sabe si la funcionalidad
 * sirvio. El boton de recargar existe para despues de una ruta, no para
 * llegar por primera vez.
 */
async function rutasCargarCoberturaGeo() {
  const el = document.getElementById('rutas-cobertura-geo');
  if (!el) return;
  try {
    const d = await get('/api/rutas/geo/cobertura');
    const con = d.con_coordenada || 0;
    const sin = d.sin_coordenada || 0;
    const base = con + sin;
    const pct = base ? Math.round((con * 100) / base) : 0;
    const umbral = d.umbral_para_rutear || 0;
    const solas = d.con_una_sola_captura || 0;
    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div style="font-size:var(--fs-xs);color:var(--tx3);font-weight:700;">UBICACIÓN DE CLIENTES</div>
        <button onclick="rutasCargarCoberturaGeo()"
          style="padding:4px 10px;background:var(--bg-input);border:1px solid var(--brd);color:var(--tx2);border-radius:6px;font-size:var(--fs-xs);cursor:pointer;">↻</button>
      </div>
      <div style="display:flex;align-items:baseline;gap:8px;">
        <div style="font-size:26px;font-weight:800;color:var(--ok-tx);">${con}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx2);">de ${base} clientes visitados tienen coordenada (${pct}%)</div>
      </div>
      <div style="height:6px;background:var(--bg-input);border-radius:3px;margin:8px 0;overflow:hidden;">
        <div style="height:100%;width:${pct}%;background:#15803d;"></div>
      </div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);line-height:1.6;">
        ${solas} con una sola visita · ${esc(d.capturas_con_punto || 0)} capturas con punto de ${esc(d.capturas_totales || 0)}<br>
        Ruteo no se construye hasta llegar a <b style="color:var(--tx2);">${umbral}</b> clientes con coordenada.
        ${con >= umbral ? '<span style="color:var(--ok-tx);">Umbral alcanzado — ver docs/flota/ESTADO.md.</span>' : ''}
      </div>`;
  } catch (e) {
    el.innerHTML = `<div style="font-size:var(--fs-xs);color:var(--err-tx);">No se pudo leer la cobertura: ${esc(e.message || 'error')}
      <button onclick="rutasCargarCoberturaGeo()" style="margin-left:8px;padding:3px 8px;background:var(--bg-input);border:1px solid var(--brd);color:var(--tx2);border-radius:6px;font-size:var(--fs-xs);cursor:pointer;">Reintentar</button></div>`;
  }
}

/** Carga y renderiza la lista completa de rutas maestras (activas e inactivas). */
async function cargarListaMaestras() {
  rutasCargarCoberturaGeo();
  const el = document.getElementById('lista-maestras');
  if (!el) return;
  try {
    const d = await get('/api/rutas/maestras?activas=false');
    // Más reciente primero: la que se acaba de crear queda de primera.
    const maestras = (d.maestras || []).slice().sort((a, b) => b.id - a.id);
    if (!maestras.length) {
      el.innerHTML = '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin rutas maestras. Crea la primera con el botón +</div>';
      return;
    }
    el.innerHTML = maestras.map(m => `
      <div style="background:var(--bg-s);border:1px solid ${m.activa ? 'var(--brd)' : 'var(--brd)'};border-radius:12px;padding:14px;margin-bottom:8px;opacity:${m.activa ? '1' : '0.5'};">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
          <div>
            <div style="font-size:var(--fs-md);font-weight:800;">${m.tipo_ruta === 'Urbana' ? '🏙️' : '🛣️'} ${esc(m.nombre)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(m.tipo_ruta)} · ${(m.paradas || []).length} parada${(m.paradas || []).length !== 1 ? 's' : ''}</div>
          </div>
          <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;justify-content:flex-end;">
            ${m.activa
              ? '<span style="background:#14532d;color:#bbf7d0;padding:2px 8px;border-radius:6px;font-size:var(--fs-xs);font-weight:700;">ACTIVA</span>'
              : '<span style="background:var(--err-bg);color:var(--err-tx);padding:2px 8px;border-radius:6px;font-size:var(--fs-xs);font-weight:700;">INACTIVA</span>'}
            <button onclick="maestraEditar(${esc(m.id)})"
              style="padding:5px 10px;background:var(--lila-bg);border:1px solid var(--info-brd);color:var(--lila-tx);border-radius:6px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">
              ✏ Editar
            </button>
            <button onclick="conBotonOcupado(event, () => maestraToggle(${esc(m.id)},${!m.activa}))"
              style="padding:5px 10px;background:var(--bg-input);border:1px solid var(--brd);color:var(--tx2);border-radius:6px;font-size:var(--fs-xs);cursor:pointer;">
              ${m.activa ? 'Desactivar' : 'Activar'}
            </button>
            <button onclick="conBotonOcupado(event, () => maestraEliminar(${esc(m.id)},${JSON.stringify(m.nombre)}))"
              style="padding:5px 10px;background:var(--err-bg);border:1px solid var(--err-brd);color:var(--err-tx);border-radius:6px;font-size:var(--fs-xs);cursor:pointer;">
              🗑
            </button>
          </div>
        </div>
        ${(m.paradas || []).length ? `
          <div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:6px;">
            ${m.paradas.map((p, i) => `
              <span style="background:var(--bg-input);border:1px solid var(--brd);border-radius:20px;padding:3px 10px;font-size:var(--fs-xs);color:var(--tx2);">
                ${i + 1}. ${esc(p.municipio)}
              </span>`).join('')}
          </div>` : ''}
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:var(--err-tx);text-align:center;">Error cargando rutas maestras</div>';
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
    el.innerHTML = '<div style="padding:12px;font-size:var(--fs-sm);color:var(--tx2);text-align:center;">Cargando municipios…</div>';
    el.style.display = 'block';
    _cargarMunicipios().then(() => {
      const inp = document.getElementById('maestras-parada-input');
      if (!inp || !inp.value.trim()) { el.style.display = 'none'; return; }
      if (!_MUNICIPIOS_CACHE.length) {
        el.innerHTML = '<div style="padding:12px;font-size:var(--fs-sm);color:var(--red);text-align:center;">Error cargando municipios</div>';
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
      style="padding:9px 12px;cursor:pointer;font-size:var(--fs-sm);color:var(--tx);border-bottom:1px solid var(--brd);"
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
    el.innerHTML = '<div style="color:var(--tx3);font-size:var(--fs-xs);text-align:center;padding:10px;">Agrega las paradas en orden de entrega (1ª = primera entrega)</div>';
    return;
  }
  el.innerHTML = _MAESTRAS_PARADAS.map((m, i) => `
    <div style="display:flex;align-items:center;gap:6px;padding:7px 0;border-bottom:1px solid var(--brd);">
      <span style="background:var(--bg-input);color:var(--tx3);border-radius:50%;width:22px;height:22px;display:flex;align-items:center;justify-content:center;font-size:var(--fs-xs);flex-shrink:0;">${i+1}</span>
      <span style="flex:1;font-size:var(--fs-sm);">${m}</span>
      <button onclick="maestraMoverParada(${i},-1)" ${i===0?'disabled':''} style="background:none;border:none;color:var(--tx3);cursor:pointer;font-size:var(--fs-sm);padding:2px 4px;">↑</button>
      <button onclick="maestraMoverParada(${i},1)" ${i===_MAESTRAS_PARADAS.length-1?'disabled':''} style="background:none;border:none;color:var(--tx3);cursor:pointer;font-size:var(--fs-sm);padding:2px 4px;">↓</button>
      <button onclick="maestraQuitarParada(${i})" style="background:none;border:none;color:var(--tx3);cursor:pointer;font-size:var(--fs-lg);padding:2px 4px;">×</button>
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
      el.innerHTML = '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin vehículos registrados</div>';
      return;
    }
    el.innerHTML = vehiculos.map(v => `
      <div class="flota-veh">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div>
            <div class="flota-placa">${esc(v.placa)}</div>
            <div class="flota-tipo">${esc(v.tipo)}${v.capacidad_kg ? ' · ' + v.capacidad_kg + ' kg' : ''}</div>
          </div>
          <span class="badge ${v.activo ? 'badge-green' : 'badge-red'}">
            ${v.activo ? 'ACTIVO' : 'INACTIVO'}</span>
        </div>
        <button class="btn-flota" onclick="conBotonOcupado(event, () => vehiculoToggle(${esc(v.id)}, ${!v.activo}))">
          ${v.activo ? 'Desactivar' : 'Activar'}</button>
        ${v.activo ? `<button class="btn-flota" onclick="verExpedienteVehiculo('${esc(v.placa)}')">
          Ver expediente →</button>` : ''}
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:var(--err-tx);text-align:center;">Error cargando vehículos</div>';
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

/** ¿Este conductor puede entrar a la PWA?

    Se pregunta por el HECHO (`tiene_cuenta_pwa`), no por el correo: la
    dirección se borra por privacidad para quien no es de almacén
    (`RutaService.listar_conductores`), y leer esa ausencia como «no tiene
    cuenta» le decía a supervisor y a control_flota —los dos roles que
    administran flota— que los tres conductores activos «no pueden entrar a
    la app». `usuario_id` sobrevive hoy a la redacción, pero por casualidad:
    el contrato es el booleano. */
function conCuentaPwa(c) {
  return c.tiene_cuenta_pwa === true
      || (c.tiene_cuenta_pwa === undefined && (c.usuario_id != null || !!c.usuario_email));
}

/** Solo admin crea cuentas (`POST /conductores/<id>/cuenta` es `_solo_admin`).
    Misma doctrina que `mostrarSegunRol`: ofrecer un gesto que el backend va a
    negar con 403 enseña a ignorar errores. */
function puedeCrearCuentaPwa() {
  return typeof OPERARIO !== 'undefined' && OPERARIO?.rol === 'admin';
}

/** Carga y renderiza la lista completa de conductores (activos e inactivos). */
async function cargarListaConductores() {
  const el = document.getElementById('lista-conductores');
  if (!el) return;
  try {
    const d = await get('/api/rutas/conductores?activos=false');
    const conductores = d.conductores || [];
    if (!conductores.length) {
      el.innerHTML = '<div style="color:var(--tx3);text-align:center;padding:40px;">Sin conductores registrados</div>';
      return;
    }
    el.innerHTML = conductores.map(c => `
      <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:700;">${esc(c.nombre)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${identidadConductor(c, conductores)}${c.telefono ? ' · ' + c.telefono : ''}</div>
            ${conCuentaPwa(c)
              ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:3px;">👤 ${esc(c.usuario_email || 'tiene cuenta')}</div>`
              : `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:3px;">Sin cuenta PWA — no puede entrar a la app</div>`}
          </div>
          ${c.activo
            ? '<span style="background:#14532d;color:#bbf7d0;padding:3px 8px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;height:fit-content;">ACTIVO</span>'
            : '<span style="background:var(--err-bg);color:var(--err-tx);padding:3px 8px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;height:fit-content;">INACTIVO</span>'}
        </div>
        <div style="display:flex;gap:6px;">
          <button onclick="conBotonOcupado(event, () => conductorToggle(${esc(c.id)}, ${!c.activo}))"
            style="flex:1;padding:8px;background:var(--bg-input);border:1px solid var(--brd);color:var(--tx2);border-radius:8px;font-size:var(--fs-xs);cursor:pointer;">
            ${c.activo ? 'Desactivar' : 'Activar'}
          </button>
          ${(conCuentaPwa(c) || !puedeCrearCuentaPwa()) ? '' : `<button onclick="conBotonOcupado(event, () => conductorCrearCuenta(${esc(c.id)}, '${c.nombre.replace(/'/g, "\\'")}'))"
            style="flex:1;padding:8px;background:#1e3a5f;border:1px solid #2563eb;color:var(--info-tx);border-radius:8px;font-size:var(--fs-xs);cursor:pointer;">
            Crear cuenta PWA
          </button>`}
        </div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:var(--err-tx);text-align:center;">Error cargando conductores</div>';
  }
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
//: Formas de pago que piden comprobante. Vienen del servidor dentro del payload
//: de paradas (`senales_ruta.requiere_comprobante`), que se cachea offline.
let _COND_FORMAS_COMPROBANTE = null;
//: Lo que este formulario sabe pedir. El servidor exige comprobante y
//: evidencia solo a los formularios que lo declaran: un ítem viejo de la cola
//: offline no queda trabado para siempre.
//: 3 = sabe ofrecer el select sin CRÉDITO/EXENTO en una parada de contado
//: contraentrega (`cond_pago.VERSION_FORMULARIO_CONTADO`, 2026-09-24).
//: 4 = una PARCIAL siempre dice qué volvió, referencia por referencia
//: (`devolucion_ruta.VERSION_FORMULARIO_DEVOLUCION`, m045devol): el servidor la
//: exige; un ítem de la cola con versión < 4 no se traba.
const COND_VERSION_FORMULARIO = 4;
//: Formas que declaran «no entró plata». Del servidor (`cond_pago.FORMAS_QUE_NO_COBRAN`),
//: dentro del payload que se cachea; el literal es solo el respaldo de una caché vieja.
let _COND_FORMAS_NO_COBRAN = ['CREDITO', 'EXENTO'];
//: Cuánto puede faltar por redondeo (`liquidacion_service.tope_diferencia_recaudo`).
let _COND_TOLERANCIA_COBRO = 100;
//: Mínimo de caracteres de la referencia (mismo que `senales_ruta.MIN_REFERENCIA`).
const COND_MIN_REFERENCIA = 4;

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
    },
    /** Reescribe un ítem de la cola (intentos, último error). */
    async actualizar(item) {
      const db = await _open();
      return new Promise((res, rej) => {
        const tx = db.transaction('queue', 'readwrite');
        tx.objectStore('queue').put(item);
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
      el.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:40px;">Sin conexión y sin datos en caché. Abre la app con señal primero.</div>';
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
      <div style="font-size:20px;font-weight:700;color:var(--ok-tx);margin-top:16px;">Sin rutas en tránsito</div>
      <div style="font-size:var(--fs-sm);color:var(--tx3);margin-top:8px;">El jefe de almacén te asignará una cuando salgas</div>
      <button onclick="cargarRutasConductor()" style="margin-top:24px;padding:14px 28px;background:#1d4ed8;border:none;color:#fff;border-radius:12px;font-size:var(--fs-md);cursor:pointer;">🔄 Actualizar</button>
    </div>`;
    return;
  }

  el.innerHTML = _COND_RUTAS.map((r) => {
    const totalBultos = r.total_bultos || 0;
    return `
      <div style="background:#fff;border:2px solid #bfdbfe;border-radius:16px;padding:20px;margin-bottom:12px;box-shadow:0 1px 3px rgba(0,0,0,.06);">
        <div style="font-size:var(--fs-lg);font-weight:800;color:var(--info-tx);margin-bottom:4px;">🚛 Ruta #${esc(r.id)}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:12px;">${esc(r.ruta_maestra_nombre || r.tipo_ruta)} · ${esc(r.vehiculo_placa || 'Sin vehículo')}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:16px;">${totalBultos} bulto${totalBultos !== 1 ? 's' : ''}</div>
        <button onclick="condAbrirParadas(${esc(r.id)})"
          style="width:100%;padding:18px;background:#1d4ed8;color:#fff;border:none;border-radius:12px;font-size:var(--fs-lg);font-weight:800;cursor:pointer;letter-spacing:0.02em;">
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
  el.innerHTML = '<div style="text-align:center;padding:60px;color:var(--tx3);">Cargando paradas...</div>';
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
      el.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:40px;">Sin conexión y sin datos en caché para esta ruta.</div>';
      return;
    }
  }
  _COND_RUTA_ACTIVA = { id: rutaId };
  _COND_PARADAS = data.paradas || [];
  // Lo guardado en el teléfono y no enviado manda sobre lo que dice el
  // servidor: si no, la parada aparece pendiente y el conductor la rehace.
  try {
    const enCola = await _condPendientesDeRuta(rutaId);
    if (enCola.length) {
      enCola.forEach(it => {
        const x = _COND_PARADAS.find(q => q.tarea_id === it.tareaId);
        if (x) x.recaudo = _condRecaudoLocal(it.payload, x.recaudo);
      });
      const g = _COND_PARADAS.filter(q => q.recaudo).length;
      data = { ...data, facturas_gestionadas: g, paradas_gestionadas: g };
    }
  } catch (_) { /* sin cola legible, se muestra lo del servidor */ }
  // Se pide una vez por ruta, no por parada. Si falla, el select queda vacío y
  // la validación del servidor rechaza igual — el conductor ve el error en vez
  // de guardar un rechazo sin motivo.
  if (!_COND_MOTIVOS.length) {
    try { _COND_MOTIVOS = (await get('/api/rutas/motivos-rechazo')).motivos || []; }
    catch (e) { console.warn('[RUTAS] no se pudo traer el catálogo de motivos', e); }
  }
  _COND_RETENCIONES = data.retenciones_disponibles || _COND_RETENCIONES;
  if (Array.isArray(data.formas_con_comprobante)) _COND_FORMAS_COMPROBANTE = data.formas_con_comprobante;
  if (Array.isArray(data.formas_que_no_cobran)) _COND_FORMAS_NO_COBRAN = data.formas_que_no_cobran;
  if (typeof data.tolerancia_cobro === 'number') _COND_TOLERANCIA_COBRO = data.tolerancia_cobro;
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
      <button onclick="cargarRutasConductor()" style="background:none;border:none;color:var(--tx3);font-size:var(--fs-sm);cursor:pointer;padding:0;">← Volver</button>
      <span style="font-size:var(--fs-sm);color:var(--tx3);">${gestionadas}/${total} gestionadas</span>
    </div>
    <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:14px;padding:14px;margin-bottom:16px;">
      <div style="font-size:var(--fs-sm);color:var(--tx2);">Ruta #${esc(_COND_RUTA_ACTIVA.id)} · <span style="color:${todasGestionadas ? 'var(--ok-tx)' : 'var(--warn-tx)'};font-weight:700;">${todasGestionadas ? 'Lista para cerrar' : 'En curso'}</span></div>
      <div style="display:flex;gap:16px;margin-top:10px;">
        <div style="text-align:center;"><div style="font-size:24px;font-weight:800;color:var(--ok-tx);">${gestionadas}</div><div style="font-size:var(--fs-xs);color:var(--tx3);">GESTIONADAS</div></div>
        <div style="text-align:center;"><div style="font-size:24px;font-weight:800;color:var(--tx3);">${total - gestionadas}</div><div style="font-size:var(--fs-xs);color:var(--tx3);">PENDIENTES</div></div>
      </div>
    </div>`;

  paradas.forEach((p, idx) => {
    const r = p.recaudo;
    const est = r ? r.estado_entrega : null;
    const c = estiloEntrega(est);
    const badge = `<span style="background:${esc(c.badgeBg)};color:${esc(c.badgeColor)};padding:2px 8px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;">${esc(c.label)}</span>`;
    const monto = r ? ` · $${Number(r.monto_cobrado || 0).toLocaleString('es-CO')}` : '';

    html += `
      <div style="background:${esc(c.fondo)};border:1px solid ${esc(c.borde)};border-radius:12px;padding:14px;margin-bottom:8px;cursor:pointer;"
           onclick="condAbrirFormParada(${idx})">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div style="flex:1;min-width:0;">
            <div style="font-size:var(--fs-sm);font-weight:800;color:var(--tx);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(p.cliente)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">📍 ${esc(p.municipio)} · ${esc(p.numero_pedido)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(p.bultos.length)} bulto${p.bultos.length !== 1 ? 's' : ''}${monto}</div>
          </div>
          <div style="margin-left:10px;flex-shrink:0;">${badge}</div>
        </div>
        ${r ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">${esc(r.forma_pago || '')}${r.observaciones ? ' · ' + esc(r.observaciones.substring(0,40)) : ''}</div>` : ''}
        ${r && r._en_cola ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);font-weight:700;margin-top:6px;">⏳ Guardada en el teléfono, todavía sin enviar</div>` : ''}
      </div>`;
  });

  if (todasGestionadas) {
    html += `
      <div style="position:sticky;bottom:16px;margin-top:12px;">
        <button onclick="conBotonOcupado(event, () => condCerrarRuta())"
          style="width:100%;padding:20px;background:#15803d;color:#fff;border:none;border-radius:14px;font-size:var(--fs-lg);font-weight:800;cursor:pointer;">
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
    <div style="display:flex;justify-content:space-between;font-size:var(--fs-md);color:var(--tx3);margin-bottom:4px;">
      <span>Base</span><span>$${Number(p.base_gravable).toLocaleString('es-CO')}</span>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:var(--fs-md);color:var(--tx3);margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #e5e7eb;">
      <span>IVA</span><span>$${Number(p.iva_factura).toLocaleString('es-CO')}</span>
    </div>`;
}

// Medios de pago del cobro en puerta — alineados 1:1 con `MedioPago` de
// gestor-cartera-pame (`dominio/recaudo/modelo.py`) y con `_forma_pago_map`
// en `connekta_gateway.py`: mismo Siesa, mismo maestro de medios de pago.
// CHEQUE/CREDITO/EXENTO no existen en el Gestor (ahí no hay "no se cobró" —
// solo recaudos que sí ocurrieron); acá siguen porque describen un hecho
// distinto del conductor en la puerta, no un medio de pago bancario.
const _FORMAS_PAGO_COBRO = [
  { v: 'EFECTIVO', l: 'Efectivo' },
  { v: 'TRANSFERENCIA_BANCOLOMBIA_AH', l: 'Transferencia Bancolombia Ahorros' },
  { v: 'TRANSFERENCIA_BANCOLOMBIA_CTE', l: 'Transferencia Bancolombia Corriente' },
  { v: 'TRANSFERENCIA_BBVA', l: 'Transferencia BBVA' },
  { v: 'TRANSFERENCIA_BOGOTA', l: 'Transferencia Bogotá' },
  { v: 'TRANSFERENCIA_AGRARIO_AH', l: 'Transferencia Agrario Ahorros' },
  { v: 'TRANSFERENCIA_AGRARIO_CTE', l: 'Transferencia Agrario Corriente' },
  { v: 'TRANSFERENCIA_DAVIVIENDA', l: 'Transferencia Davivienda' },
  { v: 'TRANSFERENCIA_IHO_CTE', l: 'Transferencia IHO Corriente' },
  { v: 'TARJETA', l: 'Tarjeta (datáfono)' },
  { v: 'CHEQUE', l: 'Cheque' },
  { v: 'CREDITO', l: 'Crédito' },
  { v: 'EXENTO', l: 'Exento' },
];

/**
 * ¿Esta parada es crédito REAL? Solo si el servidor lo confirmó
 * (`cobro_contraentrega === false`, días conocidos > 15). Contado
 * contraentrega y el supuesto (sin condición) se cobran.
 *
 * Una caché vieja del payload (sin el campo) cae a `modo_pago`, que es lo que
 * esa caché sabía: no se le cambia la pantalla a un conductor sin señal.
 */
function _condEsCreditoReal(p) {
  if (!p) return false;
  if (p.cobro_contraentrega === false) return true;
  if (p.cobro_contraentrega === true) return false;
  return p.modo_pago === 'CREDITO';
}

/**
 * Las formas de pago que el conductor puede elegir en esta parada. En contado
 * contraentrega (y en el supuesto) NO se ofrecen crédito ni exento: el
 * conductor no otorga crédito — si no pagó, es Rechazado → «No pagó».
 */
function _condFormasPago(p) {
  if (_condEsCreditoReal(p)) return _FORMAS_PAGO_COBRO;
  const fuera = _COND_FORMAS_NO_COBRAN || ['CREDITO', 'EXENTO'];
  return _FORMAS_PAGO_COBRO.filter(f => !fuera.includes(f.v));
}

/**
 * El aviso de cobro de la parada: texto y tono, del servidor
 * (`cond_pago.etiqueta_conductor`). Sin el campo (caché vieja) se arma algo
 * equivalente con lo que haya — nunca se inventa una condición.
 */
function _condAvisoCobro(p) {
  const e = p && p.cobro_etiqueta;
  if (e && e.texto) return { texto: e.texto, tono: e.tono || 'info' };
  if (_condEsCreditoReal(p)) return { texto: 'Crédito — no se cobra', tono: 'info' };
  return { texto: 'Sin condición de pago: cobrá al entregar', tono: 'warn' };
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
  const avisoCobro = _condAvisoCobro(p);
  const _TONO_AVISO = { ok: ['--ok-bg', '--ok-brd', '--ok-tx'], warn: ['--warn-bg', '--warn-brd', '--warn-tx'],
                        info: ['--info-bg', '--info-brd', '--info-tx'] };
  const [_avBg, _avBrd, _avTx] = _TONO_AVISO[avisoCobro.tono] || _TONO_AVISO.info;
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
      <button onclick="condVolverAParadas()" style="background:none;border:none;color:var(--tx3);font-size:var(--fs-sm);cursor:pointer;padding:0;">← Paradas</button>
      ${r ? `<span style="font-size:var(--fs-xs);color:var(--tx3);">Editando confirmación</span>` : ''}
    </div>

    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:14px;margin-bottom:16px;">
      <div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);">${esc(p.cliente)}</div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-top:4px;">📍 ${esc(p.municipio)}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(p.numero_pedido)} · ${esc(p.bultos.length)} bulto${p.bultos.length !== 1 ? 's' : ''}</div>
    </div>

    ${(p.vendedor_nombre || p.vendedor_telefono) ? `
    <div style="background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;padding:10px 14px;margin-bottom:16px;display:flex;align-items:center;gap:10px;">
      <span style="font-size:var(--fs-lg);">🧑‍💼</span>
      <div>
        <div style="font-size:var(--fs-xs);color:var(--info-tx);font-weight:700;letter-spacing:.5px;">ASESOR DEL PEDIDO</div>
        <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(p.vendedor_nombre || '—')}</div>
        ${p.vendedor_telefono ? `<a href="tel:${esc(p.vendedor_telefono)}" style="color:var(--ok-tx);font-size:var(--fs-sm);text-decoration:none;">📞 ${esc(p.vendedor_telefono)}</a>` : ''}
      </div>
    </div>
    ` : ''}

    <div style="margin-bottom:14px;">
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">RESULTADO</label>
      <div style="display:flex;gap:6px;" id="cond-estado-btns">
        ${['ENTREGADO','RECHAZADO'].map(e => `
          <button onclick="condSelEstado('${e}')"
            id="cond-estado-${e}"
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;border:2px solid ${e===estadoUi ? (e==='ENTREGADO'?'#15803d':'#dc2626') : '#d1d5db'};background:${e===estadoUi ? (e==='ENTREGADO'?'#f0fdf4':'#fef2f2') : '#fff'};color:${e===estadoUi ? (e==='ENTREGADO'?'var(--ok-tx)':'var(--err-tx)') : 'var(--tx3)'};">
            ${e === 'ENTREGADO' ? '✓ Entregado' : '✗ Rechazado'}
          </button>`).join('')}
      </div>
    </div>

    <div id="cond-bultos-rechazo" style="margin-bottom:14px;display:${estadoUi === 'RECHAZADO' ? 'block' : 'none'};">
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">BULTOS RECHAZADOS</label>
      ${p.bultos.map(b => `
        <label style="display:flex;align-items:center;gap:10px;padding:10px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px;cursor:pointer;">
          <input type="checkbox" value="${esc(b.id)}" ${rechazadosActuales.includes(b.id) ? 'checked' : ''}
            style="width:18px;height:18px;cursor:pointer;" id="chk-bulto-${esc(b.id)}">
          <span style="font-size:var(--fs-sm);color:var(--tx2);">${esc(b.codigo_barras)} · ${esc(b.tipo)} ${esc(b.numero)}/${esc(b.total)}</span>
        </label>`).join('')}
    </div>

    <div id="cond-items-parcial" style="margin-bottom:14px;display:${estadoUi === 'ENTREGADO' ? 'block' : 'none'};">
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">REFERENCIAS</label>
      <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:10px;">Precargadas con la cantidad del pedido. Ajusta solo si algo no se entregó — lo que baje queda como devolución.</div>
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
          <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);margin-bottom:2px;">${it.nombre || it.codigo}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:10px;">${it.codigo} · ${it.unidad || 'und'}</div>
          <div style="display:flex;align-items:center;gap:10px;">
            <div style="flex:1;text-align:center;">
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:2px;">PEDIDO</div>
              <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx2);">${pedido}</div>
            </div>
            <div style="flex:2;">
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:4px;">ENTREGADO</div>
              <input type="number" id="item-entregado-${idx}"
                value="${prevEntregado}" min="0" max="${pedido}" step="1"
                oninput="condActualizarDevuelto(${idx}, ${pedido})"
                style="width:100%;padding:10px;border:2px solid #d1d5db;border-radius:8px;font-size:var(--fs-lg);font-weight:800;color:var(--ok-tx);text-align:center;box-sizing:border-box;">
            </div>
            <div style="flex:1;text-align:center;">
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:2px;">DEVUELTO</div>
              <div id="item-devuelto-${idx}" style="font-size:var(--fs-lg);font-weight:800;color:var(--err-tx);">${pedido - prevEntregado}</div>
            </div>
          </div>
        </div>`;
      }).join('')}
    </div>

    <div id="cond-bultos-devolucion" style="margin-bottom:14px;display:none;">
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">BULTOS CON DEVOLUCIÓN</label>
      <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:10px;">Opcional — en cuál caja/bulto físico va lo que vuelve a bodega.</div>
      ${p.bultos.map(b => `
        <label style="display:flex;align-items:center;gap:10px;padding:10px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px;cursor:pointer;">
          <input type="checkbox" value="${esc(b.id)}" ${rechazadosActuales.includes(b.id) ? 'checked' : ''}
            style="width:18px;height:18px;cursor:pointer;" id="chk-bulto-dev-${esc(b.id)}">
          <span style="font-size:var(--fs-sm);color:var(--tx2);">${esc(b.codigo_barras)} · ${esc(b.tipo)} ${esc(b.numero)}/${esc(b.total)}</span>
        </label>`).join('')}
    </div>

    <div id="cond-entrega-payload" style="display:${estadoUi === 'ENTREGADO' ? 'block' : 'none'};">
      ${modoPago === 'CREDITO' ? `
      ${hayValorConocido ? `
      <div id="cond-valorfactura-wrap" style="margin-bottom:14px;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">VALOR FACTURA</label>
        <div id="cond-valorfactura-monto" style="padding:14px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;">
          ${_condDesgloseHTML(p)}
          <div id="cond-valorfactura-total" style="font-size:20px;font-weight:800;color:var(--ok-tx);">$${Number(p.valor_factura).toLocaleString('es-CO')}</div>
        </div>
      </div>
      ` : ''}
      <div id="cond-aviso-cobro" style="margin-bottom:14px;padding:14px;background:var(${_avBg});border:1px solid var(${_avBrd});border-radius:10px;font-size:var(--fs-sm);font-weight:700;color:var(${_avTx});display:flex;align-items:center;gap:10px;">
        💳 ${esc(avisoCobro.texto)}
      </div>
      ` : `
      <div id="cond-aviso-cobro" style="margin-bottom:14px;padding:12px 14px;background:var(${_avBg});border:1px solid var(${_avBrd});border-radius:10px;font-size:var(--fs-sm);font-weight:700;color:var(${_avTx});">
        💵 ${esc(avisoCobro.texto)}
      </div>
      ${modoPago === 'DINAMICO' ? `
      <div id="cond-valorfactura-wrap" style="margin-bottom:14px;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">VALOR A COBRAR</label>
        <div id="cond-valorfactura-monto" style="padding:14px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;">
          ${_condDesgloseHTML(p)}
          <div id="cond-valorfactura-total" style="font-size:20px;font-weight:800;color:var(--ok-tx);">$${Number(p.valor_factura).toLocaleString('es-CO')}</div>
        </div>
      </div>

      <div id="cond-pago-toggle-wrap" style="margin-bottom:14px;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">PAGO</label>
        <div style="display:flex;gap:6px;">
          <button type="button" onclick="condSelTipoPago('TOTAL')" id="cond-tipopago-TOTAL"
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;border:2px solid #d1d5db;background:#fff;color:var(--tx3);">
            ✓ Pago Total
          </button>
          <button type="button" onclick="condSelTipoPago('PARCIAL')" id="cond-tipopago-PARCIAL"
            style="flex:1;padding:14px 4px;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;border:2px solid #d1d5db;background:#fff;color:var(--tx3);">
            ⚠ Pago Parcial
          </button>
        </div>
      </div>

      <div id="cond-parcial-detalle" style="margin-bottom:14px;display:none;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">MONTO PAGADO ($)</label>
        <input type="number" id="cond-monto-parcial" value="${motivoActual ? montoActual : ''}" min="0" max="${Math.floor(p.valor_factura)}" step="1"
          oninput="condActualizarMotivoVisible()"
          style="width:100%;padding:14px;background:#fff;border:2px solid #d1d5db;color:var(--tx);border-radius:10px;font-size:var(--fs-lg);font-weight:700;box-sizing:border-box;">

        <div id="cond-motivo-descuento-wrap" style="margin-top:12px;display:none;">
          <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">MOTIVO DEL DESCUENTO</label>
          <select id="cond-motivo-descuento" onchange="condActualizarPreviewDescuento()"
            style="width:100%;padding:14px;background:#fff;border:1px solid #d1d5db;color:var(--tx);border-radius:10px;font-size:var(--fs-md);">
            <option value="">— Seleccionar —</option>
            ${_COND_RETENCIONES.map(ret =>
              `<option value="${esc(ret.tipo)}" ${ret.tipo===motivoActual?'selected':''}>${esc(ret.nombre)}</option>`
            ).join('')}
          </select>
          <div id="cond-motivo-descuento-preview" style="margin-top:8px;font-size:var(--fs-sm);color:var(--tx3);"></div>
          ${(p.vendedor_nombre || p.vendedor_telefono) ? `
          <div style="margin-top:10px;padding:12px;background:#fffbeb;border:1px solid #d97706;border-radius:10px;">
            <div style="font-size:var(--fs-xs);color:var(--warn-tx);font-weight:700;margin-bottom:6px;">⚠ Comunícate con el vendedor para validar este pago parcial</div>
            <div style="font-size:var(--fs-sm);color:var(--tx);font-weight:700;">${esc(p.vendedor_nombre || '—')}</div>
            ${p.vendedor_telefono ? `<a href="tel:${esc(p.vendedor_telefono)}" style="display:inline-block;margin-top:4px;color:var(--ok-tx);font-size:var(--fs-sm);text-decoration:none;">📞 ${esc(p.vendedor_telefono)}</a>` : ''}
          </div>
          ` : ''}
        </div>
      </div>
      ` : `
      <div id="cond-monto-wrap" style="margin-bottom:14px;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">MONTO COBRADO ($)</label>
        <input type="number" id="cond-monto" value="${montoActual}" min="0" step="100"
          style="width:100%;padding:14px;background:#fff;border:2px solid #d1d5db;color:var(--tx);border-radius:10px;font-size:var(--fs-lg);font-weight:700;box-sizing:border-box;">
      </div>
      `}

      <div id="cond-forma-pago-wrap" style="margin-bottom:14px;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">FORMA DE PAGO</label>
        <select id="cond-forma-pago" onchange="condFormaPagoCambio()"
          style="width:100%;padding:14px;background:#fff;border:1px solid #d1d5db;color:var(--tx);border-radius:10px;font-size:var(--fs-md);">
          <option value="">— Seleccionar —</option>
          ${_condFormasPago(p).map(f =>
            `<option value="${esc(f.v)}" ${f.v===formaActual?'selected':''}>${esc(f.l)}</option>`
          ).join('')}
        </select>
      </div>

      <!-- Solo cuando la plata entra por un banco o un datáfono. La parada en
           efectivo no pide nada nuevo. -->
      <div id="cond-comprobante-wrap" style="margin-bottom:14px;display:none;padding:14px;background:var(--info-bg);border:1px solid var(--info-brd);border-radius:12px;">
        <label style="font-size:var(--fs-xs);color:var(--info-tx);font-weight:700;display:block;margin-bottom:8px;">REFERENCIA DEL COMPROBANTE *</label>
        <input type="text" id="cond-referencia" inputmode="text" autocomplete="off" maxlength="30"
          value="${esc((r && r.referencia_pago) || '')}"
          placeholder="Últimos dígitos del comprobante"
          style="width:100%;padding:14px;background:#fff;border:2px solid #d1d5db;color:var(--tx);border-radius:10px;font-size:var(--fs-lg);font-weight:700;box-sizing:border-box;letter-spacing:1px;">
        <input type="file" id="cond-foto-comprobante" accept="image/*" capture="environment"
          style="display:none;" onchange="condPrevisualizarComprobante()">
        <button type="button" onclick="document.getElementById('cond-foto-comprobante').click()"
          style="margin-top:10px;width:100%;padding:14px;background:#fff;color:var(--info-tx);border:2px dashed var(--info-brd);border-radius:12px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">
          📷 Foto del comprobante *
        </button>
        <div id="cond-comprobante-estado" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx2);">
          ${r && r.tiene_foto_comprobante ? '✓ Foto del comprobante guardada — tomá otra para reemplazarla' : 'Sin foto todavía'}
        </div>
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
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">MOTIVO DEL RECHAZO *</label>
      <select id="cond-motivo" onchange="condMotivoCambio()"
        style="width:100%;padding:12px;background:#fff;border:1px solid #d1d5db;color:var(--tx);border-radius:10px;font-size:var(--fs-sm);box-sizing:border-box;">
        <option value="">— Elegí el motivo —</option>
        ${(_COND_MOTIVOS || []).map(m => `
        <option value="${esc(m.codigo)}" ${motivoRechazoActual === m.codigo ? 'selected' : ''}>${esc(m.etiqueta)}</option>`).join('')}
      </select>
      <div id="cond-motivo-aviso" style="margin-top:8px;padding:12px;background:var(--warn-bg);border:1px solid var(--warn-brd);border-radius:10px;display:none;">
        <div style="font-size:var(--fs-sm);color:var(--warn-tx);font-weight:700;">La mercancía se queda con el cliente. El inventario NO vuelve al camión.</div>
        <div style="font-size:var(--fs-xs);color:var(--tx);margin-top:6px;">Obligatorio: <b>una foto</b> (la mercancía en el local o la fachada) y tocar <b>📍 Estoy aquí</b>.</div>
        ${(p.vendedor_nombre || p.vendedor_telefono) ? `
        <div style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx2);">Antes de dejarla, llamá al asesor del pedido:</div>
        <div style="font-size:var(--fs-sm);color:var(--tx);font-weight:700;">${esc(p.vendedor_nombre || '—')}</div>
        ${p.vendedor_telefono ? `<a href="tel:${esc(p.vendedor_telefono)}" style="display:inline-block;margin-top:4px;color:var(--ok-tx);font-size:var(--fs-md);font-weight:700;text-decoration:none;">📞 ${esc(p.vendedor_telefono)}</a>` : ''}
        ` : `<div style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx2);">Este pedido no trae el teléfono del asesor: avisá a la oficina.</div>`}
      </div>
    </div>

    <div style="margin-bottom:14px;">
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">OBSERVACIONES</label>
      <textarea id="cond-obs" rows="2"
        style="width:100%;padding:12px;background:#fff;border:1px solid #d1d5db;color:var(--tx);border-radius:10px;font-size:var(--fs-sm);resize:none;box-sizing:border-box;"
        placeholder="Ej: Cliente solicitó factura electrónica...">${esc(obsActual)}</textarea>
    </div>

    <div style="margin-bottom:20px;">
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">FOTO EVIDENCIA <span id="cond-foto-oblig" style="color:var(--tx2);font-weight:400;">(opcional)</span></label>
      <input type="file" id="cond-foto" accept="image/*" capture="environment"
        style="display:none;" onchange="condPrevisualizarFoto()">
      <button type="button" onclick="document.getElementById('cond-foto').click()"
        style="width:100%;padding:16px;background:#f0f9ff;color:var(--info-tx);border:2px dashed #7dd3fc;border-radius:12px;font-size:var(--fs-md);font-weight:700;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:10px;">
        📷 Tomar foto con la cámara
      </button>
      <div id="cond-foto-preview" style="margin-top:8px;display:none;">
        <img id="cond-foto-img" src="" style="width:100%;border-radius:10px;border:2px solid #7dd3fc;max-height:200px;object-fit:cover;">
        <button type="button" onclick="condEliminarFoto()"
          style="margin-top:6px;width:100%;padding:8px;background:#fef2f2;color:var(--err-tx);border:1px solid #dc2626;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">
          ✕ Quitar foto
        </button>
      </div>
      ${r && r.foto_entrega ? `<div style="margin-top:8px;font-size:var(--fs-xs);color:var(--ok-tx);">✓ Foto guardada — toma una nueva para reemplazarla</div>` : ''}
    </div>

    <div style="margin-bottom:20px;">
      <label style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;display:block;margin-bottom:8px;">
        UBICACIÓN DEL CLIENTE <span style="color:var(--tx3);font-weight:400;">(opcional)</span>
      </label>
      <button type="button" onclick="condCapturarUbicacion()" id="cond-geo-btn"
        style="width:100%;padding:16px;background:#f0fdf4;color:var(--ok-tx);border:2px dashed #86efac;border-radius:12px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">
        📍 Estoy aquí
      </button>
      <div id="cond-geo-estado" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx2);">
        Tocalo parado en la puerta del cliente. Queda guardado para que la próxima ruta sepa dónde es.
      </div>
    </div>

    <div style="position:sticky;bottom:16px;">
      <button onclick="condGuardarParada()"
        style="width:100%;padding:20px;background:#1d4ed8;color:#fff;border:none;border-radius:14px;font-size:var(--fs-lg);font-weight:800;cursor:pointer;">
        ${r ? '💾 Actualizar Confirmación' : '✓ Confirmar Parada'}
      </button>
    </div>`;

  // Guardar estado seleccionado
  el._estadoSel = estadoUi;
  // Arranca en «no se pidió» y no en null: el conductor que confirma sin tocar
  // el botón está dando una respuesta —«no lo hice»— y ésa es contable. Un
  // null llegaría al servidor como «no vino nada», que es lo que significa un
  // cliente viejo con el service worker en caché, y son cosas distintas.
  el._geo = { fuente: 'sin_dato', motivo: 'no_se_pidio' };

  if (mostrarValorDinamico) {
    condSelTipoPago(el._tipoPagoSel);
  }
  condRecalcularPago();
  condFormaPagoCambio();
  condMotivoCambio();
}

/** ¿Esta forma de pago pide comprobante? La lista la manda el servidor. */
function _condRequiereComprobante(forma) {
  const f = String(forma || '').toUpperCase();
  if (!f) return false;
  if (Array.isArray(_COND_FORMAS_COMPROBANTE)) return _COND_FORMAS_COMPROBANTE.includes(f);
  // Caché de paradas anterior al campo: el lado conservador es pedirlo.
  return f.startsWith('TRANSFERENCIA') || ['CONSIGNACION', 'TARJETA', 'CHEQUE'].includes(f);
}

/** Muestra el bloque del comprobante solo si la forma de pago lo pide. */
function condFormaPagoCambio() {
  const wrap = document.getElementById('cond-comprobante-wrap');
  if (!wrap) return;
  const forma = document.getElementById('cond-forma-pago')?.value || '';
  wrap.style.display = _condRequiereComprobante(forma) ? 'block' : 'none';
}

/** ¿El motivo elegido deja la mercancía con el cliente? (catálogo del servidor) */
function _condMotivoExigeEvidencia(codigo) {
  const m = (_COND_MOTIVOS || []).find(x => x.codigo === codigo);
  if (!m) return false;
  return m.exige_evidencia ?? (m.retorna === false);
}

/** Muestra el aviso + asesor y marca la foto como obligatoria para «se quedó sin pagar». */
function condMotivoCambio() {
  const codigo = document.getElementById('cond-motivo')?.value || '';
  const exige = _condMotivoExigeEvidencia(codigo);
  const aviso = document.getElementById('cond-motivo-aviso');
  if (aviso) aviso.style.display = exige ? 'block' : 'none';
  const obl = document.getElementById('cond-foto-oblig');
  if (obl) {
    obl.textContent = exige ? '(obligatoria)' : '(opcional)';
    obl.style.color = exige ? 'var(--err-tx)' : 'var(--tx2)';
    obl.style.fontWeight = exige ? '700' : '400';
  }
}

/** Confirma en pantalla que la foto del comprobante quedó tomada. */
function condPrevisualizarComprobante() {
  const input = document.getElementById('cond-foto-comprobante');
  const estado = document.getElementById('cond-comprobante-estado');
  if (!input || !estado) return;
  estado.textContent = input.files && input.files[0] ? '✓ Foto del comprobante tomada' : 'Sin foto todavía';
  estado.style.color = input.files && input.files[0] ? 'var(--ok-tx)' : 'var(--tx2)';
}

/**
 * Lee un archivo de imagen y lo devuelve como JPEG base64, con el lado largo
 * acotado. La foto de evidencia va chica; la del comprobante es una foto-dato
 * (regla 7 de flota): el número tiene que poder leerse, así que va a 1600 px y
 * calidad 0.8, y el servidor no la recomprime.
 */
function _condLeerFoto(file, ladoMax, calidad) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = reject;
    reader.onload = ev => {
      const img = new Image();
      img.onerror = reject;
      img.onload = () => {
        let w = img.width, h = img.height;
        const ratio = Math.min(1, ladoMax / Math.max(w, h));
        w = Math.round(w * ratio); h = Math.round(h * ratio);
        const canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL('image/jpeg', calidad));
      };
      img.src = ev.target.result;
    };
    reader.readAsDataURL(file);
  });
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
      ? `<span style="text-decoration:line-through;color:var(--tx2);font-size:var(--fs-xs);font-weight:500;display:block;margin-bottom:2px;">$${Number(p.valor_factura).toLocaleString('es-CO')}</span>$${valorAjustado.toLocaleString('es-CO')}`
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
    btn.style.borderColor = activo ? (t === 'TOTAL' ? '#15803d' : '#d97706') : '#d1d5db';
    btn.style.background  = activo ? (t === 'TOTAL' ? '#f0fdf4' : '#fffbeb') : '#fff';
    btn.style.color       = activo ? (t === 'TOTAL' ? '#15803d' : '#b45309') : '#6b7280';
  });

  const detalle = document.getElementById('cond-parcial-detalle');
  if (detalle) detalle.style.display = tipo === 'PARCIAL' ? 'block' : 'none';
  if (tipo === 'PARCIAL') condActualizarMotivoVisible();
}

/**
 * Muestra el desplegable de motivo (del DESCUENTO, no del rechazo) apenas se
 * elige Pago Parcial — antes esperaba a que el conductor ya hubiera escrito
 * un monto menor al valor a cobrar, así que el select quedaba escondido justo
 * cuando más falta hacía (recién elegido "Pago Parcial", campo de monto
 * todavía vacío). El motivo no depende de cuánto se vaya a escribir, depende
 * de que el pago sea parcial — así que se muestra con eso solo.
 */
function condActualizarMotivoVisible() {
  const el = document.getElementById('cond-contenido');
  const wrap = document.getElementById('cond-motivo-descuento-wrap');
  if (!wrap || !el) return;
  wrap.style.display = (el._tipoPagoSel === 'PARCIAL') ? 'block' : 'none';
  condActualizarPreviewDescuento();
}

/**
 * Calcula y muestra cuánto sería el descuento según la tasa real de la
 * retención elegida — misma fórmula que usa Liquidación de escritorio
 * (`base_de_retencion`/`monto_de_retencion` en liquidacion_service.py):
 * ReteIVA va sobre el IVA de la factura, todo lo demás sobre la base
 * gravable. `base_gravable`/`iva_factura` ya vienen en la parada (los trae
 * `RutaService.listar_paradas` desde Siesa cuando se cargó la ruta), así que
 * esto corre sin conexión — no hace falta volver a preguntarle a Siesa.
 *
 * Es una vista previa para que el conductor y quien liquide vean el mismo
 * número desde el principio — la fuente de verdad sigue siendo Liquidación,
 * que sí valida contra Siesa antes de mandar el Documento Contable.
 */
function condActualizarPreviewDescuento() {
  const prev = document.getElementById('cond-motivo-descuento-preview');
  if (!prev) return;
  const tipo = document.getElementById('cond-motivo-descuento')?.value || '';
  const p = _COND_PARADA_FORM;
  if (!tipo || !p) { prev.textContent = ''; return; }

  const ret = _COND_RETENCIONES.find(r => r.tipo === tipo);
  const baseGravable = p.base_gravable;
  const iva = p.iva_factura;
  if (!ret || !ret.tasa || baseGravable == null || iva == null) {
    prev.innerHTML = '<span style="color:var(--warn-tx);">No se pudo calcular el valor estimado — falta el desglose de Siesa para esta factura.</span>';
    return;
  }
  const base = tipo === 'RETEIVA' ? iva : baseGravable;
  const valor = Math.round(base * ret.tasa * 100) / 100;
  let html = `Descuento estimado: <strong style="color:var(--ok-tx);">${_condFmt(valor)}</strong>` +
    ` (${(ret.tasa * 100).toLocaleString('es-CO', {maximumFractionDigits: 3})}% sobre ${_condFmt(base)})`;

  // Total con el descuento ya aplicado — lo que de verdad debería cobrar el
  // conductor en la puerta, no solo cuánto se descuenta. `p.valor_factura`
  // es el mismo total que ya se ve arriba en "VALOR A COBRAR" ($61.588 en
  // este caso), así que acá se resta el mismo descuento que se muestra
  // arriba, no un cálculo aparte.
  if (p.valor_factura != null) {
    const totalConDescuento = Math.max(0, Math.round((p.valor_factura - valor) * 100) / 100);
    html += `<div style="margin-top:4px;">Total a cobrar con descuento: ` +
      `<strong style="color:var(--ok-tx);">${_condFmt(totalConDescuento)}</strong></div>`;
  }
  prev.innerHTML = html;
}

/** Formatea un número como pesos colombianos, igual que el resto de la pantalla del conductor. */
function _condFmt(v) {
  return '$' + Number(v || 0).toLocaleString('es-CO');
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
    const colores = { ENTREGADO: ['#15803d','#f0fdf4','#15803d'], RECHAZADO: ['#dc2626','#fef2f2','#b91c1c'] };
    const [borde, fondo, texto] = activo ? colores[e] : ['#d1d5db','#fff','#6b7280'];
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

// ── Dónde queda el cliente ────────────────────────────────────────
//
// El WMS **no tiene direcciones de clientes** — el sync de pedidos lee códigos
// de departamento y ciudad, no calles. Y geocodificar tampoco resuelve: en
// Neiva hay 72 números de casa mapeados en todo el municipio, 10 en Pitalito.
// El conductor que ya estuvo parado en la puerta es estrictamente mejor que
// cualquier API, y por eso el punto lo pone él.

/**
 * El bloque de navegacion de la ficha del cliente.
 *
 * Degrada CON HONESTIDAD: sin coordenada NO pinta boton. Un boton de Waze
 * alimentado con "Neiva" abre el centro de Neiva, que no es la tienda — y un
 * conductor que abre eso una vez no vuelve a tocar el boton nunca, aunque
 * despues sirva. Mejor decir que no se sabe y explicar como se arregla.
 *
 * Tres estados, y son tres cosas distintas (Regla 4 — la ausencia se modela
 * con palabras):
 *   · hay punto            -> Waze + Google Maps, con cuantas visitas lo
 *                             sostienen y con que precision.
 *   · geo == null          -> nadie capturo nada todavia.
 *   · geo sin lat          -> se capturo y no se pudo elegir un punto, con el
 *                             motivo (disperso, precision insuficiente).
 * @param {Object} p - Parada del conductor
 * @returns {string} HTML del bloque
 */
function _condBloqueNavegacion(p) {
  const g = p.geo;
  const aviso = (txt) => `<div style="margin-top:10px;padding:10px;background:var(--bg-input);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-xs);color:var(--tx2);line-height:1.5;">${txt}</div>`;

  if (!g) {
    return aviso('📍 <b style="color:var(--tx);">No sabemos dónde queda.</b><br>Tocá «Estoy aquí» al confirmar y la próxima ruta ya lo va a tener.');
  }
  if (g.lat == null || g.lon == null) {
    const motivos = {
      sin_capturas:           'todavía no se capturó ninguna ubicación',
      precision_insuficiente: 'las capturas llegaron sin precisión suficiente',
      capturas_dispersas:     'las capturas no coinciden entre sí — puede haber dos clientes con el mismo nombre',
    };
    const razon = motivos[g.motivo_sin_maestro] || 'no se pudo determinar';
    return aviso(`📍 <b style="color:var(--tx);">No sabemos dónde queda</b> — ${razon}.<br>Tocá «Estoy aquí» al confirmar.`);
  }

  const ll = `${esc(g.lat)},${esc(g.lon)}`;
  const prec = g.precision_m != null ? ` · ±${Math.round(g.precision_m)} m` : '';
  const n = g.capturas_consideradas || 0;
  return `
    <div style="display:flex;gap:8px;margin-top:12px;">
      <a href="https://waze.com/ul?ll=${ll}&navigate=yes" target="_blank" rel="noopener"
        style="flex:1;text-align:center;padding:12px;background:#0a3d62;color:#7dd3fc;border:1px solid #1e5f8a;border-radius:10px;font-size:var(--fs-sm);font-weight:700;text-decoration:none;">
        🧭 Waze
      </a>
      <a href="https://www.google.com/maps/dir/?api=1&destination=${ll}" target="_blank" rel="noopener"
        style="flex:1;text-align:center;padding:12px;background:#14532d;color:#bbf7d0;border:1px solid #166534;border-radius:10px;font-size:var(--fs-sm);font-weight:700;text-decoration:none;">
        🗺️ Maps
      </a>
    </div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">
      Punto de ${n} visita${n !== 1 ? 's' : ''}${prec} · lo pusieron los conductores
    </div>`;
}

/**
 * Captura la coordenada del GPS del telefono para esta parada.
 *
 * NO bloquea nada: si el GPS no responde, el conductor confirma igual y la
 * parada queda con "sin_dato" y su motivo. Una entrega trabada en la calle no
 * la desbloquea nadie — mismo criterio que `confirmar_parada` ya aplica con la
 * condicion de pago que no se alcanzo a anotar.
 *
 * Lo que NUNCA hace es guardar 0,0: cada rama de error escribe una palabra.
 */
function condCapturarUbicacion() {
  const el = document.getElementById('cond-contenido');
  const estado = document.getElementById('cond-geo-estado');
  const btn = document.getElementById('cond-geo-btn');
  if (!el) return;

  const decir = (txt, color) => { if (estado) { estado.textContent = txt; estado.style.color = color || '#9ca3af'; } };

  if (!navigator.geolocation) {
    el._geo = { fuente: 'sin_dato', motivo: 'no_soportado' };
    decir('Este dispositivo no tiene GPS disponible. Podés confirmar igual.', '#f59e0b');
    return;
  }

  if (btn) { btn.disabled = true; btn.textContent = '📍 Buscando señal...'; }
  decir('Buscando señal...');

  navigator.geolocation.getCurrentPosition(
    (pos) => {
      if (btn) { btn.disabled = false; btn.textContent = '📍 Ubicación tomada ✓'; btn.style.background = '#dcfce7'; }
      el._geo = {
        lat: pos.coords.latitude,
        lon: pos.coords.longitude,
        // El radio de incertidumbre que declara el navegador. Viaja siempre:
        // sin el, el servidor no puede decidir si esta captura sirve para
        // ubicar la tienda o solo para saber que el camion anduvo cerca.
        precision_m: pos.coords.accuracy,
        fuente: 'gps_conductor',
        // Cuándo fijó el GPS esta posición, según el teléfono.
        pos_ts: pos.timestamp,
      };
      decir(`Ubicación tomada · ±${Math.round(pos.coords.accuracy || 0)} m de precisión`, '#4ade80');
    },
    (err) => {
      if (btn) { btn.disabled = false; btn.textContent = '📍 Estoy aquí'; }
      // Las tres ramas del error del navegador son tres cosas distintas, y se
      // guardan distinto: 1 = el usuario dijo que no, 2 = no hubo senal,
      // 3 = se acabo la espera. Colapsarlas en "sin ubicacion" borraria la
      // unica informacion que dice si esto se arregla hablando con el
      // conductor o cambiandole el telefono.
      const motivos = { 1: 'permiso_denegado', 2: 'sin_senal', 3: 'timeout' };
      const motivo = motivos[err && err.code] || 'no_declarado';
      el._geo = { fuente: 'sin_dato', motivo: motivo };
      const textos = {
        permiso_denegado: 'Permiso de ubicación denegado. Podés confirmar igual.',
        sin_senal:        'Sin señal de GPS acá. Podés confirmar igual.',
        timeout:          'El GPS tardó demasiado. Podés confirmar igual.',
        no_declarado:     'No se pudo tomar la ubicación. Podés confirmar igual.',
      };
      decir(textos[motivo], '#f59e0b');
    },
    { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
  );
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

  // Foto de evidencia (chica: 800 px @ 0.65 — el servidor la recomprime).
  let fotoBase64 = '';
  const fotoInput = document.getElementById('cond-foto');
  if (fotoInput && fotoInput.files[0]) {
    try { fotoBase64 = await _condLeerFoto(fotoInput.files[0], 800, 0.65); }
    catch (_) { alerta('Error procesando la foto', 'error'); return; }
  }
  const rPrevio = p.recaudo || null;

  // «No pagó y se quedó con la mercancía»: foto y «Estoy aquí» obligatorios.
  // Solo en esa excepción; el rechazo normal y la entrega no piden nada nuevo.
  const motivoRechazo = document.getElementById('cond-motivo')?.value || '';
  if (estadoUi === 'RECHAZADO' && _condMotivoExigeEvidencia(motivoRechazo)) {
    if (!fotoBase64 && !(rPrevio && rPrevio.tiene_foto_entrega)) {
      alerta('Tomá una foto: la mercancía en el local o la fachada', 'error');
      return;
    }
    const g = el._geo || {};
    const intentado = (g.lat != null && g.lon != null) || (g.motivo && g.motivo !== 'no_se_pidio');
    if (!intentado && !rPrevio) {
      alerta('Tocá «📍 Estoy aquí» antes de confirmar', 'error');
      document.getElementById('cond-geo-btn')?.focus();
      return;
    }
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
    // Contado contraentrega: una entrega completa se cobra completa. La misma
    // regla que aplica el servidor (`confirmar_parada`), dicha antes de encolar
    // para que la parada no quede rechazada en la cola sin señal.
    const _faltaCobro = !_condEsCreditoReal(p) && estadoEntrega === 'ENTREGADO' && (
      montoFinal <= 0 || (p.valor_factura != null && montoFinal < Number(p.valor_factura) - _COND_TOLERANCIA_COBRO));
    if (_faltaCobro) {
      alerta('Esta factura se cobra al entregar y el monto no alcanza. Si no pagó, marcá Rechazado → «No pagó»; si pagó una parte, ajustá lo entregado.', 'error');
      document.getElementById('cond-monto')?.focus();
      return;
    }
  }

  // Pago bancario: referencia + foto del comprobante. La foto es una foto-dato
  // (el número tiene que leerse): 1600 px, calidad 0.8, sin recompresión.
  let referenciaPago = null;
  let fotoComprobante = '';
  if (cobraEnLaPuerta && !esCredito && _condRequiereComprobante(formaPago) && montoFinal > 0) {
    referenciaPago = (document.getElementById('cond-referencia')?.value || '').trim();
    const alnum = referenciaPago.replace(/[^0-9A-Za-z]/g, '');
    if (alnum.length < COND_MIN_REFERENCIA) {
      alerta(`Escribí la referencia del comprobante (al menos los últimos ${COND_MIN_REFERENCIA} dígitos)`, 'error');
      document.getElementById('cond-referencia')?.focus();
      return;
    }
    const fc = document.getElementById('cond-foto-comprobante');
    if (fc && fc.files[0]) {
      try { fotoComprobante = await _condLeerFoto(fc.files[0], 1600, 0.8); }
      catch (_) { alerta('Error procesando la foto del comprobante', 'error'); return; }
    }
    if (!fotoComprobante && !(rPrevio && rPrevio.tiene_foto_comprobante)) {
      alerta('Tomale una foto al comprobante del pago', 'error');
      return;
    }
  }

  const payload = {
    // Qué sabe pedir este formulario: el servidor exige comprobante y
    // evidencia solo a los que lo declaran.
    version_formulario: COND_VERSION_FORMULARIO,
    // La hora del TELÉFONO al confirmar. Viaja aparte de la del servidor y no
    // la reemplaza; `ts_envio` se pone al mandar (ver `_condEnviarParada`).
    ts_dispositivo:    new Date().toISOString(),
    referencia_pago:   referenciaPago,
    foto_comprobante:  fotoComprobante || null,
    estado_entrega:    estadoEntrega,
    forma_pago:        formaPago || null,
    // Qué modo tenía la pantalla al confirmar, no solo qué eligió el
    // conductor. En LIBRE puede marcar CREDITO en una parada de contado, y
    // sin registrarlo esa frecuencia no se puede contar. Viaja en el payload
    // —y no se recalcula en el servidor— porque la confirmación tiene que
    // funcionar offline, sin volver a preguntarle a Siesa.
    modo_pantalla:     el._modoPago || null,
    motivo_rechazo:    motivoRechazo || null,
    monto_cobrado:     montoFinal,
    motivo_descuento:  motivoDescuentoFinal,
    monto_descuento:   montoDescuentoFinal,
    observaciones:     obs || null,
    foto_entrega:      fotoBase64 || null,
    bultos_rechazados: estadoEntrega === 'RECHAZADO' ? bultosRechazados : bultosDevolucion,
    items_entregados:  itemsEntregados.length ? itemsEntregados : null,
    // Dónde estaba el camión. Va DENTRO del payload y no en una llamada
    // aparte: la confirmación tiene que funcionar sin señal, y un segundo
    // request es un segundo request que se pierde. Con la cola offline, la
    // coordenada viaja pegada a la entrega que la produjo.
    //
    // El servidor no lo revalida contra Siesa ni contra nada: solo lo juzga
    // (¿cae en Colombia? ¿trae precisión?) y guarda lo que sea, con su
    // procedencia. Ver `services/geo_cliente.leer_captura_del_conductor`.
    geo:               el._geo || { fuente: 'sin_dato', motivo: 'no_se_pidio' },
  };

  const btn = el.querySelector('button[onclick="condGuardarParada()"]');
  await _condMandarConfirmacion(_COND_RUTA_ACTIVA.id, p, payload, rPrevio, btn);
}

/**
 * Manda una confirmación ya armada, o la guarda en el teléfono. Separada del
 * formulario para poder ejecutarla en un test con la cola real.
 * @param {number} rutaId
 * @param {Object} p - la parada
 * @param {Object} payload
 * @param {Object|null} rPrevio - el recaudo que la parada ya tenía
 * @param {Object|null} btn - el botón «Confirmar Parada», si está
 */
async function _condMandarConfirmacion(rutaId, p, payload, rPrevio, btn) {
  // ── Sin conexión: se guarda en el teléfono ─────────────────────
  if (!navigator.onLine) {
    await _condEncolarConfirmacion(rutaId, p, payload, rPrevio,
      'Guardado sin conexión — se enviará al reconectar');
    return;
  }
  if (btn) { btn.disabled = true; btn.textContent = 'Guardando...'; }

  // Con señal débil `navigator.onLine` dice «conectado» y el envío se muere
  // en el camino. Antes eso terminaba en «Error de conexión» y la entrega —
  // fotos, cobro, lo que volvió— se perdía: el conductor tenía que rehacerla.
  // Ahora «no hubo respuesta» y «el servidor falló» se guardan en la cola
  // igual que sin señal; solo un RECHAZO del servidor deja el formulario
  // abierto, con su motivo, para corregirlo.
  const res = await _condEnviarUno(
    `/api/rutas/${rutaId}/paradas/${p.tarea_id}/confirmar`,
    _condSelloDeEnvio(payload, false, null));
  if (res.estado === 'hecho') {
    alerta(res.datos && res.datos.es_edicion ? 'Confirmación actualizada' : 'Parada confirmada ✓', 'exito');
    condVolverAParadas(true);
    return;
  }
  if (res.estado === 'rechazado') {
    alerta(res.mensaje || 'El servidor no aceptó la confirmación', 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Confirmar Parada'; }
    return;
  }
  await _condEncolarConfirmacion(rutaId, p, payload, rPrevio,
    `${res.mensaje} La confirmación quedó guardada en el teléfono y se envía sola.`);
}

// ── Envío y cola del conductor ──────────────────────────────────────
//
// Mismo contrato que la cola de flota (`flotaColaEnviarUna`): una respuesta
// se clasifica en cuatro, y cada una tiene UN destino.
//
//   · `hecho`      — el servidor la tiene (2xx).
//   · `rechazado`  — el servidor dijo que no (4xx salvo 401/408/429): reenviarla
//                     no la arregla, hay que hacerla de nuevo. Sale de la cola
//                     y queda ANOTADA, con su motivo, hasta que el conductor la
//                     lea. Antes quedaba en la cola para siempre, reintentándose
//                     en silencio.
//   · `sin_red`    — no hubo respuesta. Puede haber llegado: reenviar una
//                     confirmación es seguro (el servidor la trata como edición).
//   · `reintentar` — el servidor falló (5xx, 408, 429) o la sesión venció (401).

/** Cuánto se espera una respuesta antes de darla por perdida. */
const COND_TIMEOUT_MS = 25000;
/** Llave (en la caché de `wms_cond`) de los rechazos que el conductor no leyó. */
const COND_LLAVE_RECHAZOS = 'cond_rechazos';

/**
 * Manda UNA operación del conductor y clasifica la respuesta.
 * @param {string} url - ruta de la API
 * @param {Object} cuerpo
 * @returns {Promise<{estado:string, mensaje?:string, datos?:Object}>}
 */
async function _condEnviarUno(url, cuerpo) {
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), COND_TIMEOUT_MS) : null;
  let r;
  try {
    r = await fetch(API + url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(cuerpo),
      signal: ctrl ? ctrl.signal : undefined,
    });
  } catch (e) {
    return { estado: 'sin_red', mensaje: 'No hubo respuesta del servidor (señal débil).' };
  } finally {
    if (timer) clearTimeout(timer);
  }
  let d = {};
  try { d = await r.json(); } catch (_) { d = {}; }
  if (r.ok) return { estado: 'hecho', datos: d };
  if (r.status === 401) {
    return { estado: 'reintentar', mensaje: 'La sesión venció. Entre de nuevo y se envía sola.' };
  }
  if (r.status >= 500 || r.status === 408 || r.status === 429) {
    return { estado: 'reintentar', mensaje: 'El servidor no respondió bien.' };
  }
  return { estado: 'rechazado', datos: d,
           mensaje: (d && d.error) ? String(d.error) : `El servidor la rechazó (error ${r.status}).` };
}

/** La URL de un ítem de la cola. Un tipo desconocido levanta: no se manda a ningún lado. */
function _condUrlDe(item) {
  if (item.tipo === 'confirmar') return `/api/rutas/${item.rutaId}/paradas/${item.tareaId}/confirmar`;
  if (item.tipo === 'cerrar') return `/api/rutas/${item.rutaId}/entregar`;
  throw new Error('operación del conductor desconocida: ' + item.tipo);
}

/**
 * Lo que la pantalla muestra de una parada confirmada que todavía no llegó al
 * servidor. Sale del MISMO payload que se va a mandar: la vista no puede
 * decir una cosa y la cola otra.
 * @param {Object} payload
 * @param {Object|null} previo - el recaudo que la parada ya tenía
 */
function _condRecaudoLocal(payload, previo) {
  return {
    estado_entrega:         payload.estado_entrega,
    forma_pago:             payload.forma_pago || null,
    monto_cobrado:          payload.monto_cobrado,
    motivo_descuento:       payload.motivo_descuento,
    monto_descuento:        payload.monto_descuento,
    observaciones:          payload.observaciones || null,
    foto_entrega:           payload.foto_entrega || null,
    tiene_foto_entrega:     !!payload.foto_entrega || !!(previo && previo.tiene_foto_entrega),
    referencia_pago:        payload.referencia_pago,
    tiene_foto_comprobante: !!payload.foto_comprobante || !!(previo && previo.tiene_foto_comprobante),
    bultos_rechazados_ids:  payload.bultos_rechazados,
    items_entregados:       payload.items_entregados,
    _en_cola:               true,
  };
}

/**
 * Guarda una confirmación en la cola del teléfono y la refleja en la lista.
 * La usan el camino sin señal y el de «no hubo respuesta»: una forma de
 * guardar, no dos.
 */
async function _condEncolarConfirmacion(rutaId, parada, payload, previo, aviso) {
  try {
    await _condDB.enqueue({ tipo: 'confirmar', rutaId, tareaId: parada.tarea_id,
                            cliente: parada.cliente || '', payload });
  } catch (e) {
    // No se pudo guardar: se dice, y el formulario queda como estaba.
    alerta('El teléfono no pudo guardar la confirmación. No cierre esta pantalla ' +
           'y vuelva a intentarlo con señal.', 'error');
    return false;
  }
  const idx = _COND_PARADAS.findIndex(x => x.tarea_id === parada.tarea_id);
  if (idx >= 0) {
    _COND_PARADAS[idx].recaudo = _condRecaudoLocal(payload, previo);
    const gestionadas = _COND_PARADAS.filter(x => x.recaudo).length;
    try {
      await _condDB.set('paradas_' + rutaId, { paradas: _COND_PARADAS,
        facturas_gestionadas: gestionadas, paradas_gestionadas: gestionadas });
    } catch (_) { /* la cola ya la tiene: la vista se rehace al recargar */ }
  }
  await _condActualizarBarras();
  alerta(aviso, 'advertencia');
  condVolverAParadas(false);
  return true;
}

/** Los rechazos que el conductor todavía no leyó. */
async function _condRechazos() {
  try {
    const v = await _condDB.get(COND_LLAVE_RECHAZOS);
    return Array.isArray(v) ? v : [];
  } catch (_) { return []; }
}

/** Anota un rechazo para que no se pierda aunque nadie esté mirando. */
async function _condAnotarRechazo(item, mensaje) {
  const lista = await _condRechazos();
  lista.push({ tipo: item.tipo, rutaId: item.rutaId, tareaId: item.tareaId || null,
               cliente: item.cliente || '', mensaje, ts: Date.now() });
  try { await _condDB.set(COND_LLAVE_RECHAZOS, lista); } catch (_) { /* queda la alerta */ }
}

/** El conductor leyó el rechazo. Se quita por posición, no por texto. */
async function condDescartarRechazo(i) {
  const lista = (await _condRechazos()).filter((_, j) => j !== i);
  try { await _condDB.set(COND_LLAVE_RECHAZOS, lista); } catch (_) { /* nada */ }
  await _condActualizarBarras();
}

/** Las confirmaciones de una ruta que todavía no llegaron al servidor. */
async function _condPendientesDeRuta(rutaId) {
  return (await _condDB.queue()).filter(x => x.tipo === 'confirmar' && x.rutaId === rutaId);
}

/** HTML de los rechazos sin leer. Separado del pintado para ejecutarlo en un test. */
function _condRechazosHtml(lista) {
  return (lista || []).map((r, i) => {
    const que = r.tipo === 'cerrar'
      ? `El cierre de la ruta #${esc(r.rutaId)} no se envió`
      : `No se registró la parada de ${esc(r.cliente || 'la factura ' + r.tareaId)}`;
    return `<div style="background:var(--err-bg);border:1px solid var(--err-brd);color:var(--err-tx);border-radius:10px;padding:10px 12px;margin-top:8px;font-size:var(--fs-sm);">
      <b>⚠ ${que}.</b> ${esc(r.mensaje || '')} Hágala de nuevo.
      <button onclick="condDescartarRechazo(${i})" style="display:block;margin-top:8px;background:var(--bg-s);color:var(--err-tx);border:1px solid var(--err-brd);padding:6px 12px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">Entendido</button>
    </div>`;
  }).join('');
}

/**
 * Le pone al payload de una parada la hora del teléfono AL ENVIAR y si viene
 * de la cola. Con `ts_envio` el servidor mide el desfase del reloj en el mismo
 * instante (teléfono − servidor), que separa «llegó tarde porque no había
 * señal» de «el reloj del teléfono miente».
 *
 * Un ítem encolado por una versión anterior no traía `ts_dispositivo`: se usa
 * la hora a la que se encoló (`item.ts`), que es la del teléfono al confirmar.
 * @param {Object} payload
 * @param {boolean} viaCola
 * @param {number|null} tsEncolado - `Date.now()` del momento de encolar
 */
function _condSelloDeEnvio(payload, viaCola, tsEncolado) {
  const out = { ...payload, ts_envio: new Date().toISOString(), via_cola: viaCola };
  if (!out.ts_dispositivo && tsEncolado) out.ts_dispositivo = new Date(tsEncolado).toISOString();
  return out;
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


/**
 * Cierra la ruta del conductor marcándola como entregada (con o sin señal).
 *
 * **El cierre no sale con confirmaciones pendientes en el teléfono**
 * (2026-09-25). Si cerraba antes de que llegaran, el servidor pasaba la ruta a
 * ENTREGADA y cada confirmación rezagada se rechazaba después («la ruta debe
 * estar EN_TRANSITO»): la entrega, el cobro y lo que volvió se perdían. Con
 * señal se intenta mandar lo pendiente primero; si algo no sale, se dice cuál
 * y el cierre espera. Sin señal el cierre se encola DETRÁS de ellas, y la
 * sincronización no lo manda mientras alguna de su ruta siga sin llegar.
 */
async function condCerrarRuta() {
  if (!_COND_RUTA_ACTIVA) return;
  const rutaId = _COND_RUTA_ACTIVA.id;
  if (navigator.onLine && (await _condPendientesDeRuta(rutaId)).length) {
    await condSyncQueue();
  }
  const pendientes = await _condPendientesDeRuta(rutaId);
  const rechazadas = (await _condRechazos()).filter(r => r.tipo === 'confirmar' && r.rutaId === rutaId);
  if (rechazadas.length) {
    alerta(`Hay ${rechazadas.length} parada(s) que el servidor no aceptó: ` +
           `${rechazadas.map(r => r.cliente || r.tareaId).join(', ')}. Hágalas de nuevo antes de cerrar la ruta.`,
           'error');
    return;
  }
  if (pendientes.length && navigator.onLine) {
    alerta(`Todavía hay ${pendientes.length} confirmación(es) sin enviar: ` +
           `${pendientes.map(x => x.cliente || x.tareaId).join(', ')}. ` +
           'Sincronice cuando haya señal y después cierre la ruta.', 'error');
    return;
  }
  const aviso = pendientes.length
    ? `Hay ${pendientes.length} confirmación(es) guardadas en el teléfono. El cierre ` +
      'se envía después de ellas, cuando vuelva la señal.<br>'
    : '';
  if (!(await _modalConfirmar(aviso + 'Después de cerrar ya no podrá agregar más confirmaciones de parada.',
      { titulo: '¿Cerrar la ruta?', textoConfirmar: 'Cerrar ruta' }))) return;
  if (!navigator.onLine) {
    await _condDB.enqueue({ tipo: 'cerrar', rutaId, payload: { bultos: [] } });
    await _condActualizarBarras();
    alerta('Cierre guardado en el teléfono — se enviará al reconectar', 'advertencia');
    _COND_RUTA_ACTIVA = null;
    _COND_PARADAS = [];
    cargarRutasConductor();
    return;
  }
  const res = await _condEnviarUno(`/api/rutas/${rutaId}/entregar`, { bultos: [] });
  if (res.estado === 'hecho') {
    // El servidor no niega el cierre por una parada sin gestionar (la cola sin
    // señal no se puede trabar), pero la declara: la oficina la revisa (P1-4).
    const faltan = ((res.datos || {}).paradas_sin_gestionar || []).length;
    if (faltan) {
      alerta(`Ruta cerrada con ${faltan} parada${faltan !== 1 ? 's' : ''} sin confirmar: la oficina las tiene que revisar.`, 'advertencia');
    } else {
      alerta('Ruta cerrada — ¡Buen trabajo!', 'exito');
    }
    _COND_RUTA_ACTIVA = null;
    _COND_PARADAS = [];
    cargarRutasConductor();
  } else if (res.estado === 'rechazado') {
    alerta(res.mensaje || 'El servidor no aceptó el cierre', 'error');
  } else {
    alerta(`${res.mensaje} La ruta no se cerró: inténtelo de nuevo con señal.`, 'error');
  }
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
    // Lo que no sale se dice: con cuántos intentos y por qué. Un ítem que
    // reintenta en silencio es un ítem trabado que nadie ve.
    const trabados = items.filter(x => (x.intentos || 0) > 0);
    const detalle = trabados.length
      ? ` · ${trabados.length} sin salir: ${trabados[0].ultimo_error || 'sin respuesta'}`
      : '';
    if (syncStatus && !_COND_SYNCING)
      syncStatus.textContent = `⏳ ${n} ${n !== 1 ? 'confirmaciones pendientes' : 'confirmación pendiente'} de sincronizar${detalle}`;
    if (syncBtn) syncBtn.disabled = _COND_SYNCING || !navigator.onLine;
  } else {
    syncBar.style.display = 'none';
  }
  const rechEl = document.getElementById('cond-rechazos');
  if (rechEl) rechEl.innerHTML = _condRechazosHtml(await _condRechazos());
}

/**
 * Sincroniza la cola del conductor con el servidor.
 *
 * · Un ítem que no sale (sin red, servidor caído) NO frena a los demás: son
 *   entregas independientes. Queda en la cola con sus intentos y su error.
 * · Un RECHAZO sale de la cola y queda anotado con su motivo (se muestra hasta
 *   que el conductor toque «Entendido»). Reenviarlo no lo arregla.
 * · Un cierre de ruta no se manda mientras alguna confirmación de ESA ruta
 *   siga en la cola; si alguna fue rechazada, el cierre tampoco sale y se
 *   anota: la parada hay que rehacerla y la ruta cerrarla de nuevo.
 */
async function condSyncQueue() {
  if (_COND_SYNCING || !navigator.onLine) return;
  const items = await _condDB.queue();
  if (!items.length) { await _condActualizarBarras(); return; }

  _COND_SYNCING = true;
  const syncStatus = document.getElementById('cond-sync-status');
  const syncBtn    = document.getElementById('cond-sync-btn');
  if (syncBtn) syncBtn.disabled = true;

  const rutasConPendiente = new Set();   // confirmaciones que siguen en la cola
  const rutasConRechazo = new Set();     // confirmaciones que el servidor no aceptó
  let hechos = 0;
  const rechazos = [];
  const trabados = [];
  try {
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (syncStatus) syncStatus.textContent = `🔄 Sincronizando ${i + 1}/${items.length}…`;
      if (item.tipo === 'cerrar') {
        if (rutasConRechazo.has(item.rutaId)) {
          await _condDB.dequeue(item.id);
          const msg = 'Una parada de esta ruta fue rechazada: rehágala y cierre la ruta de nuevo.';
          await _condAnotarRechazo(item, msg);
          rechazos.push(msg);
          continue;
        }
        if (rutasConPendiente.has(item.rutaId)) continue;   // espera a sus confirmaciones
      }
      let res;
      try {
        const cuerpo = item.tipo === 'confirmar'
          ? _condSelloDeEnvio(item.payload, true, item.ts)
          : item.payload;
        res = await _condEnviarUno(_condUrlDe(item), cuerpo);
      } catch (e) {
        res = { estado: 'rechazado', mensaje: e.message };
      }
      if (res.estado === 'hecho') {
        await _condDB.dequeue(item.id);
        hechos++;
      } else if (res.estado === 'rechazado') {
        await _condDB.dequeue(item.id);
        await _condAnotarRechazo(item, res.mensaje);
        rechazos.push(res.mensaje);
        if (item.tipo === 'confirmar') rutasConRechazo.add(item.rutaId);
      } else {
        await _condDB.actualizar({ ...item, intentos: (item.intentos || 0) + 1,
                                   ultimo_error: res.mensaje });
        trabados.push(res.mensaje);
        if (item.tipo === 'confirmar') rutasConPendiente.add(item.rutaId);
      }
    }
  } finally {
    _COND_SYNCING = false;
  }

  if (rechazos.length) {
    alerta(`${rechazos.length} registro(s) no se aceptaron y salieron de la cola: ${rechazos[0]} ` +
           'Están en la lista de arriba: hay que hacerlos de nuevo.', 'error');
  } else if (trabados.length) {
    alerta(`${hechos} de ${items.length} sincronizadas — ${trabados.length} siguen guardadas ` +
           `en el teléfono: ${trabados[0]}`, 'advertencia');
  } else {
    alerta('✓ Sincronización completa', 'exito');
  }
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
  // Un paso, no dos (confirm + prompt): el modal dice qué pasa y pide el
  // motivo, que el servidor exige y deja en la bitácora con quién forzó.
  const motivo = await _modalTexto('Forzar cierre de ruta',
    'Las paradas sin gestionar quedan registradas como RECHAZADAS. No se deshace. ¿Por qué se fuerza?',
    { obligatorio: true, textoConfirmar: 'Forzar cierre', textoCancelar: 'Volver' });
  if (!motivo || !motivo.trim()) return;
  try {
    const r = await fetch(API + `/api/rutas/${id}/forzar-cierre`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ motivo: motivo.trim() })
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
    '<div style="text-align:center;padding:60px;color:var(--tx3);">Cargando planilla...</div>';
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
      PENDIENTE:      '<span style="background:var(--bg-input);color:var(--tx3);padding:2px 10px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;">PENDIENTE</span>',
      EN_LIQUIDACION: '<span style="background:#78350f;color:var(--warn-tx);padding:2px 10px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;">EN LIQUIDACIÓN</span>',
      LIQUIDADA:      '<span style="background:#14532d;color:#bbf7d0;padding:2px 10px;border-radius:8px;font-size:var(--fs-xs);font-weight:700;">LIQUIDADA</span>',
    }[d.estado_financiero] || d.estado_financiero;

    const fmt = v => '$' + Number(v || 0).toLocaleString('es-CO');
    const totales = d.totales_por_forma || {};
    const total = d.total_recaudado || 0;

    let html = `
      <div style="margin-bottom:16px;">
        <div style="font-size:var(--fs-md);font-weight:800;">Ruta #${esc(ruta.id)} — ${esc(ruta.conductor_nombre)}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-top:4px;">${esc(ruta.ruta_maestra_nombre || ruta.tipo_ruta)} · ${esc(ruta.vehiculo_placa || '')}</div>
        <div style="margin-top:8px;">${finBadge}</div>
      </div>

      <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:14px;margin-bottom:16px;">
        <div style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;margin-bottom:10px;">RESUMEN FINANCIERO</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
          ${Object.entries(totales).filter(([,v]) => v > 0).map(([k,v]) => `
            <div style="background:var(--bg-input);border-radius:8px;padding:10px;">
              <div style="font-size:var(--fs-xs);color:var(--tx3);">${k}</div>
              <div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);">${fmt(v)}</div>
            </div>`).join('')}
        </div>
        <div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--brd);display:flex;justify-content:space-between;align-items:center;">
          <span style="font-size:var(--fs-sm);color:var(--tx2);">Total recaudado</span>
          <span style="font-size:var(--fs-xl);font-weight:800;color:var(--ok-tx);">${fmt(total)}</span>
        </div>
      </div>

      <div style="font-size:var(--fs-xs);color:var(--tx2);font-weight:700;margin-bottom:10px;">
        PARADAS (${d.total_paradas - d.sin_gestionar}/${esc(d.total_paradas)} gestionadas)
      </div>`;

    (d.paradas || []).forEach(p => {
      const r = p.recaudo;
      const colorBorde = r
        ? (r.estado_entrega === 'ENTREGADO' ? '#166534' : r.estado_entrega === 'PARCIAL' ? '#78350f' : '#7f1d1d')
        : '#333';
      html += `
        <div style="background:var(--bg-s);border:1px solid ${colorBorde};border-radius:10px;padding:12px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
              <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(p.cliente)}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">📍 ${esc(p.municipio)} · ${esc(p.numero_pedido)}</div>
            </div>
            <div style="text-align:right;">
              ${r
                ? `<div style="font-size:var(--fs-sm);font-weight:700;color:${r.estado_entrega === 'ENTREGADO' ? 'var(--ok-tx)' : r.estado_entrega === 'PARCIAL' ? 'var(--warn-tx)' : 'var(--err-tx)'};">${esc(r.estado_entrega)}</div>
                   <div style="font-size:var(--fs-xs);color:var(--tx2);">${fmt(r.monto_cobrado)}</div>
                   <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(r.forma_pago || '—')}</div>`
                : `<div style="font-size:var(--fs-xs);color:var(--tx3);">Sin gestionar</div>`}
            </div>
          </div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">
            ${esc(p.bultos_entregados)} entregado${p.bultos_entregados !== 1 ? 's' : ''} · ${esc(p.bultos_rechazados)} rechazado${p.bultos_rechazados !== 1 ? 's' : ''}
            ${r && d.estado_financiero === 'LIQUIDADA' ? `<span style="margin-left:8px;">
              ${r.siesa_nc_triggered ? '<span title="Nota crédito enviada" style="color:var(--info-tx);">NC</span>' : ''}
              ${p.rc_llego ? '<span title="Recibo de caja en Siesa" style="color:var(--ok-tx);margin-left:4px;">RC</span>' : (r.siesa_rc_triggered ? '<span title="Recibo de caja sin verificar en Siesa" style="color:var(--warn-tx);margin-left:4px;">RC ?</span>' : '')}
              ${r.siesa_dc_triggered ? '<span title="Documento contable enviado" style="color:var(--lila-tx);margin-left:4px;">DC</span>' : ''}
            </span>` : ''}
          </div>
          ${r && r.estado_entrega === 'RECHAZADO' ? `
          <div style="margin-top:10px;border-top:1px solid var(--err-brd);padding-top:10px;">
            <div style="font-size:var(--fs-xs);color:var(--err-tx);font-weight:700;margin-bottom:6px;">BULTOS RECHAZADOS</div>
            ${(p.bultos_detalle || []).filter(b => b.rechazado).map(b =>
              `<div style="font-size:var(--fs-xs);color:var(--err-tx);padding:3px 0;">${esc(b.codigo_barras)} · ${esc(b.tipo)} ${esc(b.numero)}/${esc(b.total)}</div>`
            ).join('')}
            ${r.observaciones ? `<div style="margin-top:8px;font-size:var(--fs-xs);color:var(--warn-tx);font-style:italic;">"${esc(r.observaciones)}"</div>` : ''}
          </div>` : ''}
          ${r && r.estado_entrega === 'PARCIAL' && r.items_entregados && r.items_entregados.length ? `
          <div style="margin-top:10px;border-top:1px solid var(--brd);padding-top:10px;">
            <div style="font-size:var(--fs-xs);color:var(--warn-tx);font-weight:700;margin-bottom:6px;">DETALLE PARCIAL</div>
            <div style="display:grid;grid-template-columns:1fr auto auto auto;gap:4px 10px;font-size:var(--fs-xs);">
              <div style="color:var(--tx3);font-weight:700;">REFERENCIA</div>
              <div style="color:var(--tx3);font-weight:700;text-align:right;">PEDIDO</div>
              <div style="color:var(--ok-tx);font-weight:700;text-align:right;">ENTREGADO</div>
              <div style="color:var(--err-tx);font-weight:700;text-align:right;">DEVUELTO</div>
              ${r.items_entregados.map(it => `
                <div style="color:var(--tx);">${esc(it.nombre || it.codigo)}</div>
                <div style="color:var(--tx3);text-align:right;">${esc(it.cantidad_pedida)}</div>
                <div style="color:var(--ok-tx);text-align:right;font-weight:700;">${esc(it.cantidad_entregada)}</div>
                <div style="color:${it.cantidad_devuelta > 0 ? 'var(--err-tx)' : 'var(--tx3)'};text-align:right;font-weight:700;">${esc(it.cantidad_devuelta)}</div>
              `).join('')}
            </div>
          </div>` : ''}
        </div>`;
    });

    if (d.sin_gestionar === 0 && d.estado_financiero !== 'LIQUIDADA') {
      html += `
        <div style="position:sticky;bottom:0;padding-top:12px;background:var(--bg,#0a0a0a);">
          <button onclick="conBotonOcupado(event, () => rutaLiquidar(${esc(ruta.id)}))"
            style="width:100%;padding:18px;background:#14532d;color:#bbf7d0;border:none;border-radius:12px;font-size:var(--fs-md);font-weight:800;cursor:pointer;">
            Liquidar Ruta — ${fmt(total)}
          </button>
        </div>`;
    } else if (d.estado_financiero === 'LIQUIDADA') {
      // Qué falta mandar lo dice el servidor (`pendiente_siesa`, de la
      // política de cobro): una devolución contada en cero no va a tener NC,
      // y con la regla copiada acá el botón quedaba visible para siempre.
      const hayPendientesSiesa = (d.paradas || []).some(p => (p.pendiente_siesa || []).length > 0);
      if (hayPendientesSiesa) {
        html += `
          <div style="position:sticky;bottom:0;padding-top:12px;background:var(--bg,#0a0a0a);">
            <button onclick="conBotonOcupado(event, () => rutaLiquidarSiesa(${esc(ruta.id)}))"
              style="width:100%;padding:18px;background:#1e3a5f;color:var(--info-tx);border:none;border-radius:12px;font-size:var(--fs-md);font-weight:800;cursor:pointer;">
              Enviar a Siesa (NC/RC/DC)
            </button>
          </div>`;
      }
    } else if (d.sin_gestionar > 0) {
      html += `
        <div style="background:var(--warn-bg);border:1px solid var(--warn-brd);border-radius:10px;padding:12px;margin-top:8px;text-align:center;color:var(--warn-tx);font-size:var(--fs-sm);">
          Faltan ${esc(d.sin_gestionar)} parada${d.sin_gestionar !== 1 ? 's' : ''} por gestionar
        </div>`;
      // Ruta ya cerrada con paradas sin gestionar: la salida que no tenía
      // (P1-4). Lo que falta se da por rechazado con motivo (FORZAR); una
      // confirmación que llegue después por la cola del conductor entra sola.
      if (ruta.estado === 'ENTREGADA') {
        html += `
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;text-align:center;">
          La ruta ya se cerró. Si el conductor no va a enviar esas paradas, cierre lo que falta:
          quedan como rechazadas y recepción cuenta lo que volvió.
        </div>
        <button onclick="conBotonOcupado(event, () => rutaForzarCierre(${esc(ruta.id)}))"
          style="width:100%;margin-top:8px;padding:12px;background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">
          ⚡ Cerrar las paradas que faltan
        </button>`;
      }
    }

    body.innerHTML = html;
  } catch (e) {
    const body = document.getElementById('modal-planilla-body');
    if (body) body.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:40px;">Error cargando planilla</div>';
  }
}

/**
 * Liquida financieramente una ruta previa confirmacion del usuario.
 * @param {number} id - ID de la ruta a liquidar
 */
async function rutaLiquidar(id) {
  if (!confirm(`¿Liquidar Ruta #${id}?\nEsto confirma el cuadre financiero en WMS.\nLuego usa el módulo Liquidación para documentar NC/RC/DC en Siesa por parada.`)) return;
  try {
    const liquidar = (cuerpo) => fetch(API + '/api/rutas/' + id + '/liquidar', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(cuerpo || {}),
    });
    let r = await liquidar({});
    let d = await r.json();
    // Mercancía de vuelta sin contar (m045devol): se fuerza solo con motivo.
    if (!r.ok && String(d.error || '').startsWith('devoluciones_sin_contar')) {
      const motivo = prompt(String(d.error).replace(/^devoluciones_sin_contar:\s*/, '') +
        '\n\nPara liquidar igual, escribí el motivo (queda en la bitácora):');
      if (!motivo || !motivo.trim()) return;
      r = await liquidar({ motivo_devoluciones: motivo.trim() });
      d = await r.json();
    }
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
  const email = await _modalTexto('Crear cuenta', `Correo para la cuenta de ${nombre}:`);
  if (!email) return;
  const password = await _modalTexto('Crear cuenta', 'Contraseña inicial (el conductor la usa para entrar):');
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
    <h2>${esc(par.destino)} <span class="tot">${esc(par.total_bultos)} bulto(s)</span></h2>
    <table>
      <thead><tr><th>Pedido</th><th>Cliente</th><th>Bultos</th><th class="firma">Recibido</th></tr></thead>
      <tbody>${par.pedidos.map(p => `
        <tr>
          <td>${esc(p.numero_pedido)}</td>
          <td>${esc(p.cliente)}</td>
          <td>${esc(p.total_bultos)} · ${Object.entries(p.resumen || {})
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
