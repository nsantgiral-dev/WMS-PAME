// ══════════════════════════════════════════════════════════════════
// RECEPCIONISTA — OCs, escaneo ciego, traslados entrantes, devoluciones
// Dependencias globales (de app.js): get(), post(), put(), alerta(), flash(),
//   vibrar(), beepOk(), beepError(), beepDone(), pantalla(),
//   abrirCamara(), cerrarCamara(), TOKEN, OPERARIO, ALMACEN_ID, API,
//   RECEPCION_ACTUAL, DEVOLUCION_ACTUAL
// Dependencias cross-module (de packing.js): imprimirEtiquetaLPN()
// ══════════════════════════════════════════════════════════════════

// Página actual de la sección "RECEPCIONADAS" (recepciones ya CONFIRMADA, para seguimiento)
let _RECEPCION_CONFIRM_PAGE = 1;

// Sub-pestañas por estado en la pantalla de OCs (mismo patrón que PEDIDOS_TAB_*
// en app.js: se cachea el HTML ya renderizado de cada grupo, cambiar de pestaña
// no requiere refetch ni re-render, solo pintar el string cacheado).
const REC_OC_TAB_LABELS = ['PENDIENTES EN SIESA', 'EN PROCESO', 'RECEPCIONADAS'];
let REC_OC_TAB_ACTIVO = 0;
let REC_OC_GRUPOS_HTML = ['', '', ''];
let REC_OC_GRUPOS_COUNT = [0, 0, 0];

/**
 * Carga y renderiza la lista de recepciones activas (OCs de Siesa + DB).
 * @param {boolean} [silencioso=false] - true omite el spinner de carga inicial
 */
async function cargarRecepciones(silencioso = false) {
  if (RECEPCION_ACTUAL) return;
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  // Solo muestra spinner en carga inicial, no en polling automático
  if (!silencioso) {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:var(--tx3);">Cargando...</div>';
  }
  try {
    const [siesa, db, confirmadas] = await Promise.all([
      get('/api/siesa/ordenes-compra').catch(() => ({ ordenes: [] })),
      get('/api/recepcion/?estado=EN_PROCESO').catch(() => ({ recepciones: [] })),
      get(`/api/recepcion/?estado=CONFIRMADA&page=${_RECEPCION_CONFIRM_PAGE}`)
        .catch(() => ({ recepciones: [], total: 0, pagina_actual: _RECEPCION_CONFIRM_PAGE }))
    ]);
    SIESA_OCS = siesa.ordenes || [];
    // Guard post-await: el operario pudo haber entrado a escaneo mientras las APIs respondían
    if (RECEPCION_ACTUAL) return;
    renderListaRecepciones(siesa, db.recepciones || [], confirmadas);
  } catch (e) {
    if (!silencioso) el.innerHTML = '<div style="color:var(--err-tx);">Error cargando</div>';
  }
}

/**
 * Cambia de página en la sección "RECEPCIONADAS" y recarga solo esa vista.
 * @param {number} page - Número de página destino (1-indexed)
 */
function _recepcionConfirmadasPagina(page) {
  if (page < 1) return;
  _RECEPCION_CONFIRM_PAGE = page;
  cargarRecepciones(true);
}

/** Pinta la barra de sub-pestañas y el grupo activo — sin refetch, usa el HTML ya cacheado. */
function renderRecOcTabsYLista() {
  const tabsEl = document.getElementById('rec-oc-subtabs');
  const el = document.getElementById('contenido-recepcion');
  if (!tabsEl || !el) return;
  tabsEl.innerHTML = REC_OC_TAB_LABELS.map((label, i) => {
    const count = REC_OC_GRUPOS_COUNT[i] || 0;
    return `<div class="subtab${i === REC_OC_TAB_ACTIVO ? ' active' : ''}" onclick="recOcCambiarTab(${i})">${label}${count ? `<span class="subtab-badge">${count}</span>` : ''}</div>`;
  }).join('');
  el.innerHTML = REC_OC_GRUPOS_HTML[REC_OC_TAB_ACTIVO] || '<div style="text-align:center;padding:40px;color:var(--tx3);">Sin datos en esta pestaña</div>';
}

/** @param {number} idx - Índice de la sub-pestaña a activar (0=Pendientes en Siesa, 1=En proceso, 2=Recepcionadas) */
function recOcCambiarTab(idx) {
  REC_OC_TAB_ACTIVO = idx;
  renderRecOcTabsYLista();
}


// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Lista de OCs y recepciones en proceso
// ─────────────────────────────────────────────────────────────

/**
 * Arma el HTML de cada sub-pestaña (Pendientes en Siesa / En proceso /
 * Recepcionadas), lo cachea en REC_OC_GRUPOS_HTML/COUNT y pinta la activa.
 * @param {Object} siesa - Respuesta de la API de OCs (contiene .ordenes, .simulado, .error_siesa)
 * @param {Array<Object>} dbRecs - Recepciones en proceso desde la DB local
 * @param {Object} [confirmadas] - Respuesta paginada de /api/recepcion/?estado=CONFIRMADA
 */
function renderListaRecepciones(siesa, dbRecs, confirmadas) {
  // Grupo 0: OCs de Siesa
  let htmlSiesa = '';
  if (siesa.simulado) {
    htmlSiesa = `<div style="background:var(--warn-bg);border-radius:10px;padding:10px 12px;font-size:var(--fs-xs);color:var(--warn-tx);border:1px solid var(--warn-brd);">
      ⚡ Connekta en simulación — conecta credenciales para ver OCs reales de Siesa
    </div>`;
  } else if (siesa.error_siesa) {
    htmlSiesa = `<div style="background:var(--bg-s);border-radius:10px;padding:10px 12px;font-size:var(--fs-xs);color:var(--warn-tx);border:1px solid var(--err-brd);">
      ⚠ Siesa no respondió — no se pudo consultar OCs pendientes. Reintenta en un momento.
    </div>`;
  } else if (SIESA_OCS.length) {
    htmlSiesa = SIESA_OCS.map((oc, i) => {
      const sinProd = oc.items.filter(it => !it.producto_id).length;
      const totalUds = oc.items.reduce((s, it) => s + (it.cantidad_pendiente || 0), 0);
      const wmsEstado = oc.recepcion_wms_estado;

      if (wmsEstado === 'CONFIRMADA') {
        return `
          <div class="rec-card" style="opacity:0.6;">
            <div class="rec-titulo">OC: ${esc(oc.numero_oc)}</div>
            <div class="rec-sub">${esc(oc.proveedor || 'Sin proveedor')} · ${esc(oc.items.length)} productos · ${totalUds} uds</div>
            <div style="margin-top:10px;padding:10px;background:var(--ok-bg);border-radius:8px;font-size:var(--fs-sm);font-weight:700;color:var(--ok-tx);text-align:center;">
              ✓ Recepcionada en WMS — pendiente actualización en Siesa
            </div>
          </div>`;
      }

      if (wmsEstado === 'EN_PROCESO') {
        return `
          <div class="rec-card">
            <div class="rec-titulo">OC: ${esc(oc.numero_oc)}</div>
            <div class="rec-sub">${esc(oc.proveedor || 'Sin proveedor')} · ${esc(oc.items.length)} productos · ${totalUds} uds</div>
            <button onclick="crearRecepcionDesdeSiesa(${i})"
              style="width:100%;margin-top:12px;padding:14px;font-size:17px;font-weight:700;background:#1d4ed8;color:#fff;border:none;border-radius:10px;cursor:pointer;">
              Continuar recepción
            </button>
          </div>`;
      }

      return `
        <div class="rec-card">
          <div class="rec-titulo">OC: ${esc(oc.numero_oc)}</div>
          <div class="rec-sub">${esc(oc.proveedor || 'Sin proveedor')} · ${esc(oc.items.length)} productos · ${totalUds} uds</div>
          ${sinProd ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:4px;">⚠ ${sinProd} producto(s) no registrado(s) en WMS</div>` : ''}
          <button onclick="crearRecepcionDesdeSiesa(${i})"
            style="width:100%;margin-top:12px;padding:14px;font-size:17px;font-weight:700;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">
            Iniciar recepción
          </button>
        </div>`;
    }).join('');
  } else {
    htmlSiesa = `<div style="background:var(--ok-bg);border-radius:10px;padding:10px 12px;font-size:var(--fs-xs);color:var(--ok-tx);border:1px solid var(--ok-brd);">✓ Sin OCs pendientes en Siesa</div>`;
  }
  REC_OC_GRUPOS_HTML[0] = htmlSiesa;
  REC_OC_GRUPOS_COUNT[0] = SIESA_OCS.length;

  // Grupo 1: Recepciones en proceso (desde DB)
  let htmlProceso = '';
  if (dbRecs.length) {
    htmlProceso = dbRecs.map(r => `
      <div class="rec-card">
        <div class="rec-titulo">OC: ${esc(r.numero_oc_siesa)}</div>
        <div class="rec-sub">${esc(r.proveedor_nombre || 'Sin proveedor')}</div>
        <div style="margin-top:6px;font-size:var(--fs-sm);color:var(--tx3);">${esc(r.items_escaneados)} / ${esc(r.total_items)} ítems escaneados</div>
        <button onclick="continuarRecepcion(${esc(r.id)})"
          style="width:100%;margin-top:10px;padding:13px;font-size:var(--fs-md);font-weight:700;background:#1d4ed8;color:#fff;border:none;border-radius:10px;cursor:pointer;">
          Continuar escaneo
        </button>
      </div>`).join('');
  } else {
    htmlProceso = `<div style="text-align:center;padding:40px;color:var(--tx3);">Sin recepciones en proceso</div>`;
  }
  REC_OC_GRUPOS_HTML[1] = htmlProceso;
  REC_OC_GRUPOS_COUNT[1] = dbRecs.length;

  // Grupo 2: Recepciones ya confirmadas — para hacerles seguimiento
  // (ej. verificar si Siesa ya procesó la entrada contable o sigue pendiente)
  const confRecs = (confirmadas && confirmadas.recepciones) || [];
  const confTotal = (confirmadas && confirmadas.total) || 0;
  const confTotalPag = Math.max(1, Math.ceil(confTotal / 50));
  let htmlConfirmadas = '';
  if (confRecs.length) {
    htmlConfirmadas = confRecs.map(r => {
      const fecha = r.fecha_confirmacion ? new Date(r.fecha_confirmacion).toLocaleDateString('es-CO') : '—';
      const siesaBadge = r.siesa_triggered
        ? `<span style="color:var(--ok-tx);font-size:var(--fs-xs);font-weight:700;">✓ Sincronizada con Siesa</span>`
        : `<span style="color:var(--warn-tx);font-size:var(--fs-xs);font-weight:700;">⏳ Pendiente en Siesa</span>`;
      const itemsHtml = (r.items || []).map(it => `
        <div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-xs);">
          <span style="color:var(--tx);">${esc(it.producto_codigo || '')}</span>
          <span style="color:var(--tx2);">OC: ${esc(it.cantidad_ordenada)} → Rec: <strong style="color:${it.es_exceso ? 'var(--warn-tx)' : it.es_faltante ? 'var(--info-tx)' : 'var(--ok-tx)'};">${esc(it.cantidad_recibida)}</strong></span>
        </div>`).join('');
      return `
        <div class="rec-card">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div class="rec-titulo">OC: ${esc(r.numero_oc_siesa)}</div>
            <span style="font-size:var(--fs-xs);color:var(--tx3);">${fecha}</span>
          </div>
          <div class="rec-sub">${esc(r.proveedor_nombre || 'Sin proveedor')} · ${esc(r.total_items)} ítem(s)${r.es_parcial ? ' · <span style="color:var(--warn-tx);">PARCIAL</span>' : ''}</div>
          <div style="margin-top:8px;">${siesaBadge}</div>
          <div style="margin-top:8px;background:var(--bg-s);border-radius:8px;padding:6px 8px;max-height:150px;overflow-y:auto;">${itemsHtml}</div>
        </div>`;
    }).join('');

    if (confTotalPag > 1) {
      htmlConfirmadas += `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;">
          <button onclick="_recepcionConfirmadasPagina(${_RECEPCION_CONFIRM_PAGE - 1})" ${_RECEPCION_CONFIRM_PAGE <= 1 ? 'disabled' : ''}
            style="padding:8px 14px;background:var(--bg-input);border:1px solid var(--brd);color:${_RECEPCION_CONFIRM_PAGE <= 1 ? 'var(--tx3)' : 'var(--tx2)'};border-radius:8px;font-size:var(--fs-sm);cursor:${_RECEPCION_CONFIRM_PAGE <= 1 ? 'default' : 'pointer'};">
            ← Anterior
          </button>
          <span style="font-size:var(--fs-xs);color:var(--tx3);">Pág ${_RECEPCION_CONFIRM_PAGE}/${confTotalPag}</span>
          <button onclick="_recepcionConfirmadasPagina(${_RECEPCION_CONFIRM_PAGE + 1})" ${_RECEPCION_CONFIRM_PAGE >= confTotalPag ? 'disabled' : ''}
            style="padding:8px 14px;background:var(--bg-input);border:1px solid var(--brd);color:${_RECEPCION_CONFIRM_PAGE >= confTotalPag ? 'var(--tx3)' : 'var(--tx2)'};border-radius:8px;font-size:var(--fs-sm);cursor:${_RECEPCION_CONFIRM_PAGE >= confTotalPag ? 'default' : 'pointer'};">
            Siguiente →
          </button>
        </div>`;
    }
  } else {
    htmlConfirmadas = `<div style="text-align:center;padding:40px;color:var(--tx3);">Sin recepciones confirmadas aún</div>`;
  }
  REC_OC_GRUPOS_HTML[2] = htmlConfirmadas;
  REC_OC_GRUPOS_COUNT[2] = confTotal;

  renderRecOcTabsYLista();
}

/**
 * Inicia una recepcion en el WMS a partir de una OC de Siesa.
 * @param {number} idx - Indice de la OC en el array global SIESA_OCS
 */
async function crearRecepcionDesdeSiesa(idx) {
  const oc = SIESA_OCS[idx];
  if (!oc) return;
  const itemsValidos = oc.items.filter(it => it.producto_id);
  if (!itemsValidos.length) { alerta('Ningún producto de la OC está en el WMS', 'error'); return; }

  const el = document.getElementById('contenido-recepcion');
  if (el) el.innerHTML = '<div style="text-align:center;padding:60px;color:var(--tx3);">Iniciando recepción...</div>';

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

/**
 * Carga una recepcion existente desde la DB y abre la pantalla de escaneo.
 * @param {number} id - ID de la recepcion en la base de datos
 */
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

/**
 * Renderiza la pantalla de escaneo ciego para una recepcion activa.
 * @param {Object} rec - Objeto de recepcion con items, numero_oc_siesa, proveedor_nombre, etc.
 */
function renderEscaneoRecepcion(rec) {
  const el = document.getElementById('contenido-recepcion');
  if (!el) return;
  const todoCompleto = rec.items.every(it => it.cantidad_recibida >= it.cantidad_ordenada);
  const hayAlgoEscaneado = rec.items.some(it => it.cantidad_recibida > 0);
  const btnActivo = true;
  const btnTexto = todoCompleto ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial';
  const btnColor = todoCompleto ? '#15803d' : '#b45309';

  el.innerHTML = `
    <div style="padding:16px;">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <button onclick="volverListaRecepciones()"
          style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:var(--fs-sm);flex-shrink:0;">
          ← Volver
        </button>
        <div style="min-width:0;">
          <div style="font-size:var(--fs-md);font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">OC: ${esc(rec.numero_oc_siesa)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(rec.proveedor_nombre || '')}</div>
        </div>
      </div>

      <div style="background:var(--bg-s);border-radius:10px;padding:12px;margin-bottom:12px;">
        <div style="font-size:var(--fs-xs);color:var(--tx3);text-align:center;margin-bottom:10px;">Escanea unidad, caja o paca — el sistema calcula las unidades</div>
        ${OPERARIO && OPERARIO.puede_usar_camara ? `
        <button onclick="abrirCamara('lector-qr-rec','camara-box-rec', procesarScanRecepcion, this)"
          style="width:100%;padding:13px;font-size:var(--fs-md);background:#fff;color:#000;border:2px solid var(--brd);border-radius:10px;cursor:pointer;margin-bottom:8px;">
          📷 Escanear con cámara
        </button>
        <div id="camara-box-rec" style="display:none;margin-bottom:8px;">
          <div id="lector-qr-rec" style="border-radius:10px;overflow:hidden;"></div>
          <button onclick="cerrarCamara('camara-box-rec')" style="width:100%;padding:9px;margin-top:6px;font-size:var(--fs-sm);background:var(--bg-s2);color:var(--tx);border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
        </div>` : ''}
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <input id="rec-codigo-manual" type="text" placeholder="O escribe / pega el código aquí"
            style="flex:1;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ procesarScanRecepcion(v); this.value=''; } }">
          <button onclick="const v=document.getElementById('rec-codigo-manual').value.trim();if(v){procesarScanRecepcion(v);document.getElementById('rec-codigo-manual').value='';}"
            style="padding:10px 14px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:var(--fs-lg);cursor:pointer;">↵</button>
        </div>
        <button onclick="abrirBusquedaManualRecepcion()"
          style="width:100%;padding:10px;font-size:var(--fs-sm);background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:8px;cursor:pointer;">
          📦 Sin código — buscar producto manualmente
        </button>
      </div>

      <div id="items-rec-list" style="margin-bottom:14px;">
        ${renderItemsRecepcion(rec.items)}
      </div>

      <button id="btn-confirmar-rec" onclick="confirmarRecepcionActiva()" ${btnActivo ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:20px;font-weight:700;background:${btnActivo ? btnColor : 'var(--bg-s2)'};color:var(--tx);border:none;border-radius:14px;cursor:${btnActivo ? 'pointer' : 'default'};margin-bottom:10px;">
        ${btnTexto}
      </button>

      <button onclick="modalObsequio()"
        style="width:100%;padding:13px;font-size:var(--fs-md);font-weight:600;background:var(--bg-s);color:var(--lila-tx);border:1px solid var(--lila-brd);border-radius:10px;cursor:pointer;margin-bottom:8px;">
        🎁 Registrar Obsequio / Bonificación
      </button>

      <button onclick="volverListaRecepciones()"
        style="width:100%;padding:12px;font-size:var(--fs-sm);background:var(--bg-input);color:var(--tx3);border:1px solid var(--brd);border-radius:10px;cursor:pointer;">
        Guardar y salir (continuar más tarde)
      </button>
    </div>`;
}

/**
 * Genera el HTML de la lista de items de una recepcion con barras de progreso.
 * @param {Array<Object>} items - Items de la recepcion con cantidad_recibida, cantidad_ordenada, etc.
 * @returns {string} HTML concatenado de todos los items
 */
function renderItemsRecepcion(items) {
  return items.map(it => {
    const esBono = it.tipo === 'BONIFICACION';

    if (esBono) {
      return `
        <div id="item-rec-${esc(it.producto_id)}"
          style="background:var(--lila-bg);border:1px solid var(--lila-brd);border-radius:12px;padding:14px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div style="min-width:0;flex:1;">
              <div style="display:flex;align-items:center;gap:8px;">
                <span style="background:#4c1d95;color:var(--lila-tx);font-size:var(--fs-xs);font-weight:700;padding:2px 7px;border-radius:20px;">🎁 BONO</span>
                <div style="font-size:var(--fs-sm);font-weight:600;color:var(--lila-tx);">${esc(it.producto_nombre || it.producto_codigo)}</div>
              </div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(it.producto_codigo)}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;padding-left:8px;">
              <div style="font-size:var(--fs-2xl);font-weight:900;color:var(--lila-tx);">${esc(it.cantidad_recibida)}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);">und recibidas</div>
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
    const averiada = it.cantidad_averiada || 0;

    const contadorDerecha = modoEmpaque ? `
      <div style="text-align:right;flex-shrink:0;padding-left:8px;">
        <div style="font-size:42px;font-weight:900;line-height:1;color:${completo ? 'var(--ok-tx)' : 'var(--tx)'};">${empaques}</div>
        <div style="font-size:var(--fs-sm);font-weight:700;color:${completo ? 'var(--ok-tx)' : 'var(--warn-tx)'};">${esc(it.cantidad_recibida)}/${esc(it.cantidad_ordenada)} und</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);">${unidadEmpaque} · ×${factor}</div>
      </div>` : `
      <div style="text-align:right;flex-shrink:0;padding-left:8px;">
        <div style="font-size:var(--fs-2xl);font-weight:900;color:${completo ? 'var(--ok-tx)' : 'var(--tx)'};">${esc(it.cantidad_recibida)}/${esc(it.cantidad_ordenada)}</div>
      </div>`;

    return `
      <div id="item-rec-${esc(it.producto_id)}"
        style="background:${completo ? 'var(--ok-bg)' : 'var(--bg-s)'};border:1px solid ${completo ? '#166534' : 'var(--brd)'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:var(--fs-sm);font-weight:600;color:${completo ? 'var(--ok-tx)' : 'var(--tx)'};">${esc(it.producto_nombre || it.producto_codigo)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(it.producto_codigo)}</div>
            ${it.destino === 'CROSS_DOCK' ? '<div style="font-size:var(--fs-xs);color:var(--info-tx);margin-top:4px;">↔ CROSS-DOCK</div>' : ''}
          </div>
          ${contadorDerecha}
        </div>
        <div style="height:5px;background:var(--bg-s2);border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#15803d' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.3s;"></div>
        </div>
        ${averiada > 0 ? `
        <div style="margin-top:8px;padding:8px;background:var(--warn-bg);border:1px solid var(--warn-brd);border-radius:8px;">
          <div style="font-size:var(--fs-xs);font-weight:700;color:var(--warn-tx);">⚠ ${esc(averiada)} averiada(s) de ${esc(it.cantidad_recibida)}</div>
          ${it.motivo_averia ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:2px;">${esc(it.motivo_averia)}</div>` : ''}
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:3px;">van a la zona de averías, no al inventario vendible</div>
        </div>` : ''}
        ${it.cantidad_recibida > 0 ? `
        <button onclick="recepAbrirAveria(${esc(it.producto_id)})"
          style="margin-top:8px;width:100%;padding:8px;background:var(--bg-s);border:1px solid #92400e;color:var(--warn-tx);border-radius:8px;font-size:var(--fs-xs);font-weight:600;cursor:pointer;">
          ⚠ ${averiada > 0 ? 'Declarar más averiadas' : 'Declarar avería'}
        </button>` : ''}
      </div>`;
  }).join('');
}

/**
 * Declara cuantas de las unidades YA contadas llegaron averiadas.
 *
 * Puerta separada del escaneo a proposito: el recepcionista cuenta rapido con
 * la pistola y revisa despues. La regla de validacion es una sola y vive en el
 * backend (`RecepcionService._aplicar_averia`) — aca solo se pide el dato.
 *
 * @param {number} productoId - Producto sobre el que se declara la averia
 */
async function recepAbrirAveria(productoId) {
  const it = (RECEPCION_ACTUAL?.items || []).find(i => i.producto_id === productoId);
  if (!it) return;
  const restante = it.cantidad_recibida - (it.cantidad_averiada || 0);

  const n = await _modalCantidad('Mercancía averiada',
    `¿Cuántas de las ${esc(it.cantidad_recibida)} recibidas llegaron averiadas? ` +
    `(quedan ${esc(restante)} sin declarar)`,
    { min: 1, max: restante > 0 ? restante : undefined, textoConfirmar: 'Siguiente' });
  if (n === null) return;

  const motivo = await _modalTexto('¿Por qué llegaron averiadas?',
    'La auxiliar de compras lo necesita para reclamarle al proveedor.',
    { obligatorio: false, placeholder: 'Ej: cajas mojadas, estiba volcada, empaque roto de fábrica',
      textoConfirmar: 'Declarar avería' });
  if (motivo === null) return;

  let r;
  try {
    r = await post('/api/recepcion/' + RECEPCION_ACTUAL.id + '/averia', {
      producto_id: productoId,
      cantidad_averiada: n,
      motivo: motivo || null,
    });
  } catch (e) {
    alerta((e.body && e.body.error) || 'No se pudo declarar la avería', 'error');
    return;
  }

  // Mismo refresco que el escaneo: se pisa el ítem en memoria y se repinta la
  // lista. No se recarga la recepción entera — el operario tiene la pistola en
  // la mano y una recarga le mueve la pantalla debajo.
  const idx = RECEPCION_ACTUAL.items.findIndex(i => i.producto_id === productoId);
  if (idx >= 0) RECEPCION_ACTUAL.items[idx] = r.item;
  const lista = document.getElementById('items-rec-list');
  if (lista) lista.innerHTML = renderItemsRecepcion(RECEPCION_ACTUAL.items);
  alerta(`${n} unidad(es) declarada(s) como averiada(s) — van a la zona de averías`,
         'advertencia');
}

/**
 * Procesa un codigo escaneado o ingresado manualmente en la recepcion activa.
 * @param {string} codigo - Codigo de barras, GS1 o EAN escaneado
 */
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
      // `es_empaque === null` es «no sé», y NO es lo mismo que `false`.
      // El `|| false` colapsaba los dos: ante la duda registraba una unidad, o
      // —peor, con el `or` del servidor— afirmaba una caja que nadie confirmó.
      //
      // Pasa cuando el SKU tiene `factor > 1` y no tiene EAN de empaque
      // poblado: el proveedor pegó la EAN de unidad en la caja. Ahí el sistema
      // no puede saberlo, y quien sí puede es el que tiene la caja en la mano.
      let esEmp = prod.es_empaque;
      if (esEmp === null || esEmp === undefined) {
        const factor = prod.factor_conversion || 1;
        esEmp = await _confirmarModal(
          '¿Unidad o caja?',
          `Este código no distingue: <strong>${esc(prod.nombre)}</strong> se compra ` +
          `por caja de <strong>${factor}</strong>, pero el código escaneado ` +
          `sirve para las dos.<br><br>¿Qué tenés en la mano?`,
          `Caja de ${factor}`, '1 unidad'
        );
      }
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

/**
 * Registra un escaneo de producto en la recepcion activa.
 * @param {number} productoId - ID del producto en la DB
 * @param {number} cantidad - Cantidad a registrar (1 para empaque, N para LPN/paca)
 * @param {boolean} esEmpaque - true si el codigo escaneado es un empaque (DUN-14, caja)
 * @param {string|null} unidad - Unidad de medida (UND, PACA, etc.) o null para default
 * @param {boolean} [esBonificacion=false] - true si es un obsequio fuera de la OC
 */
async function _registrarEscaneoRecepcion(productoId, cantidad, esEmpaque, unidad, esBonificacion = false) {
  // scan_id: dedupe server-side si esta misma petición se reintenta (postConReintento)
  // o queda encolada offline — escanear_producto() es idempotente respecto a este id.
  const scanId = generarScanId();
  const payload = {
    producto_id: productoId,
    cantidad: cantidad,
    es_empaque: esEmpaque,
    es_bonificacion: esBonificacion,
    scan_id: scanId,
  };
  let r;
  try {
    r = await postConReintento('/api/recepcion/' + RECEPCION_ACTUAL.id + '/escanear', payload);
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
    if (e.status) {
      beepError();
      alerta(e.message, 'error');
      return;
    }
    // Corte de red real, sostenido más allá de los reintentos de postConReintento
    // — encolar para sincronizar cuando vuelva la señal (mismo scan_id: si el
    // primer intento sí llegó al servidor, el reintento diferido no duplica).
    guardarOffline({
      accion: 'recepcion_escanear',
      recepcion_id: RECEPCION_ACTUAL.id,
      ...payload,
    });
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
  if (btn && itemsOC.length > 0) {
    // El texto/color se calculan una vez en renderEscaneoRecepcion() y no se
    // tocaban más — el botón se ponía verde al completar pero seguía
    // diciendo "⚠ Confirmar recepción parcial", el texto quedaba viejo.
    btn.disabled = false;
    btn.style.cursor = 'pointer';
    if (todoCompleto) {
      btn.style.background = '#15803d';
      btn.textContent = '✓ Confirmar recepción';
      alerta('Todo escaneado — confirma la recepción', 'exito');
    } else {
      btn.style.background = '#b45309';
      btn.textContent = '⚠ Confirmar recepción parcial';
    }
  }
}

/** Muestra modal preguntando si hay obsequios/bonificaciones del proveedor. */
function modalObsequio() {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px;';
  overlay.innerHTML = `
    <div style="background:var(--lila-bg);border:1px solid var(--lila-brd);border-radius:16px;padding:28px;max-width:360px;width:100%;text-align:center;">
      <div style="font-size:40px;margin-bottom:12px;">🎁</div>
      <div style="font-size:var(--fs-lg);font-weight:800;color:var(--lila-tx);margin-bottom:10px;">¿Hay obsequios o bonificaciones?</div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:24px;">¿El proveedor envió productos adicionales que <strong style="color:var(--tx);">no están en la OC</strong>?</div>
      <button id="btn-bono-si" style="width:100%;padding:15px;font-size:var(--fs-md);font-weight:700;background:#4c1d95;color:var(--tx);border:none;border-radius:10px;cursor:pointer;margin-bottom:10px;">
        Sí — escanear obsequio
      </button>
      <button id="btn-bono-no" style="width:100%;padding:12px;font-size:var(--fs-sm);background:var(--bg-input);color:var(--tx3);border:1px solid var(--brd);border-radius:10px;cursor:pointer;">
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

/** Muestra panel overlay de escaneo exclusivo para bonificaciones. */
function _panelScanBonificacion() {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px;';
  overlay.innerHTML = `
    <div style="background:var(--lila-bg);border:1px solid var(--lila-brd);border-radius:16px;padding:24px;max-width:380px;width:100%;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div style="font-size:var(--fs-md);font-weight:800;color:var(--lila-tx);">🎁 Escanear Obsequio / Bonificación</div>
        <button onclick="this.closest('div[style*=fixed]').remove()" style="background:none;border:none;color:var(--tx3);font-size:var(--fs-xl);cursor:pointer;">✕</button>
      </div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:14px;">Escanea el producto que el proveedor envió de más — entrará a inventario a $0.</div>
      ${OPERARIO && OPERARIO.puede_usar_camara ? `
      <div id="camara-box-bono" style="display:none;margin-bottom:8px;">
        <div id="lector-qr-bono" style="border-radius:10px;overflow:hidden;"></div>
        <button onclick="cerrarCamara('camara-box-bono')" style="width:100%;padding:9px;margin-top:6px;font-size:var(--fs-sm);background:var(--bg-s2);color:var(--tx);border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
      </div>
      <button onclick="abrirCamara('lector-qr-bono','camara-box-bono', cod => { cerrarCamara('camara-box-bono'); _escanearBono(cod, this.closest('div[style*=fixed]')); }, this)"
        style="width:100%;padding:13px;font-size:var(--fs-md);background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">
        📷 Escanear con cámara
      </button>` : ''}
      <div style="display:flex;gap:8px;margin-bottom:8px;">
        <input id="bono-codigo-manual" type="text" placeholder="O escribe / pega el código"
          style="flex:1;padding:10px;background:var(--bg-s);border:1px solid var(--lila-brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);"
          onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ _escanearBono(v, this.closest('div[style*=fixed]')); this.value=''; } }">
        <button onclick="const v=document.getElementById('bono-codigo-manual').value.trim();if(v){_escanearBono(v,this.closest('div[style*=fixed]'));document.getElementById('bono-codigo-manual').value='';}"
          style="padding:10px 14px;background:#4c1d95;color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-lg);cursor:pointer;">↵</button>
      </div>
      <button onclick="abrirBusquedaManualBono(this.closest('div[style*=fixed]'))"
        style="width:100%;padding:10px;font-size:var(--fs-sm);background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:8px;cursor:pointer;">
        📦 Sin código — buscar producto manualmente
      </button>
    </div>`;
  document.body.appendChild(overlay);
}

/**
 * Procesa un escaneo dentro del panel de bonificaciones.
 * @param {string} codigo - Codigo de barras escaneado
 * @param {HTMLElement} panelEl - Elemento overlay del panel para cerrarlo tras registrar
 */
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

/**
 * Busqueda manual de producto para registrar como bonificacion.
 * @param {HTMLElement} panelEl - Elemento overlay del panel para cerrarlo tras registrar
 */
async function abrirBusquedaManualBono(panelEl) {
  const codigo = prompt('Ingresa el código WMS del producto:');
  if (!codigo) return;
  const prod = await get('/api/productos/?search=' + encodeURIComponent(codigo));
  if (!prod || !prod.productos || prod.productos.length === 0) { alerta('Producto no encontrado', 'error'); return; }
  const p = prod.productos[0];
  await _registrarEscaneoRecepcion(p.id, 1, false, null, true);
  if (panelEl) panelEl.remove();
}

/**
 * Modal de confirmacion reutilizable con dos botones.
 * @param {string} titulo - Titulo del modal
 * @param {string} cuerpoHtml - Contenido HTML del cuerpo
 * @param {string} txtSi - Texto del boton de confirmacion
 * @param {string} txtNo - Texto del boton de cancelacion
 * @returns {Promise<boolean>} true si el usuario confirma, false si cancela
 */
function _confirmarModal(titulo, cuerpoHtml, txtSi, txtNo) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9500;display:flex;align-items:center;justify-content:center;padding:24px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:16px;padding:28px;max-width:340px;width:100%;text-align:center;">
        <div style="font-size:17px;font-weight:800;color:var(--tx);margin-bottom:12px;">${titulo}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:24px;">${cuerpoHtml}</div>
        <button id="_cm-si" style="width:100%;padding:14px;font-size:var(--fs-md);font-weight:700;background:#4c1d95;color:var(--tx);border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">${txtSi}</button>
        <button id="_cm-no" style="width:100%;padding:12px;font-size:var(--fs-sm);background:var(--bg-input);color:var(--tx3);border:1px solid var(--brd);border-radius:10px;cursor:pointer;">${txtNo}</button>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#_cm-si').onclick = () => { overlay.remove(); resolve(true); };
    overlay.querySelector('#_cm-no').onclick = () => { overlay.remove(); resolve(false); };
  });
}

/**
 * Modal de ambigüedad abierto: el código escaneado y sus empaques. El botón lleva
 * solo su POSICIÓN en `empaques`, no el dato: un código o una unidad dentro de
 * `onclick="fn('…')"` no se protege con `esc()` —el navegador decodifica `&#39;`
 * a `'` antes de correr el JS— y una comilla en el dato rompe la cadena. Ver
 * CLAUDE.md, «Todo dato que se pinta va con esc()».
 * @type {{codigo: string, empaques: Array<Object>}}
 */
let _AMBIGUEDAD_RECEPCION = { codigo: '', empaques: [] };

/**
 * Muestra modal para resolver ambiguedad cuando un codigo corresponde a multiples empaques.
 * @param {string} codigo - Codigo de barras ambiguo
 * @param {Array<Object>} ambiguos - Lista de empaques posibles con producto_id, factor_conversion, unidad_medida
 */
function _modalAmbiguedadRecepcion(codigo, ambiguos) {
  // El mismo código de barras corresponde a múltiples niveles de empaque
  // El operario debe decir qué está escaneando
  _AMBIGUEDAD_RECEPCION = { codigo, empaques: ambiguos.slice() };
  const opciones = ambiguos.map((e, i) => `
    <button onclick="_elegirEmpaqueAmbiguoRecepcion(${i},this.closest('.modal-rec'))"
      style="width:100%;padding:14px;margin-bottom:8px;background:var(--bg-input);border:1px solid var(--brd);
             color:var(--tx);border-radius:10px;cursor:pointer;font-size:var(--fs-md);text-align:left;">
      <span style="font-size:var(--fs-xl);font-weight:900;">${esc(e.factor_conversion)}</span>
      <span style="color:var(--tx2);margin-left:6px;">${esc(e.unidad_medida)}</span>
      <span style="color:var(--tx3);font-size:var(--fs-xs);margin-left:8px;">(×${esc(e.factor_conversion)} und)</span>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-rec';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;display:flex;align-items:flex-end;padding:16px;';
  modal.innerHTML = `
    <div style="background:var(--bg-s);border-radius:16px;padding:20px;width:100%;max-width:480px;margin:auto;">
      <div style="font-size:var(--fs-md);font-weight:700;margin-bottom:6px;">⚠️ Código ambiguo</div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:16px;">${esc(codigo)} — ¿Qué estás escaneando?</div>
      ${opciones}
      <button onclick="this.closest('.modal-rec').remove()"
        style="width:100%;padding:12px;background:var(--bg-s);color:var(--tx3);border:1px solid var(--brd);border-radius:8px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

/**
 * El botón del modal: busca el empaque por posición y sigue igual que antes.
 * `Number(...)`: el id y el factor viajaban como literales numéricos dentro del
 * onclick, así que llegaban como números; se conserva.
 * @param {number} i @param {HTMLElement} modal
 */
function _elegirEmpaqueAmbiguoRecepcion(i, modal) {
  const e = _AMBIGUEDAD_RECEPCION.empaques[i];
  if (!e) { if (modal) modal.remove(); return; }
  return _elegirEmpaque(_AMBIGUEDAD_RECEPCION.codigo, Number(e.producto_id),
                        Number(e.factor_conversion), e.unidad_medida, modal);
}

/**
 * Registra el empaque elegido por el operario tras resolver ambiguedad.
 * @param {string} codigo - Codigo de barras original
 * @param {number} productoId - ID del producto seleccionado
 * @param {number} factor - Factor de conversion del empaque elegido
 * @param {string} unidad - Unidad de medida del empaque (PQ, CJ, etc.)
 * @param {HTMLElement} modal - Elemento del modal de ambiguedad para cerrarlo
 */
async function _elegirEmpaque(codigo, productoId, factor, unidad, modal) {
  if (modal) modal.remove();
  await _registrarEscaneoRecepcion(productoId, factor, false, unidad);
  alerta(`${unidad} × ${factor} UND registrada`, 'exito');
}

/** Abre modal de busqueda manual de producto para pacas sin codigo de barras. */
async function abrirBusquedaManualRecepcion() {
  // Busca producto por texto para pacas sin ningún código
  const modal = document.createElement('div');
  modal.className = 'modal-rec';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;display:flex;align-items:flex-start;padding:16px;padding-top:60px;';
  modal.innerHTML = `
    <div style="background:var(--bg-s);border-radius:16px;padding:20px;width:100%;max-width:480px;margin:auto;">
      <div style="font-size:var(--fs-md);font-weight:700;margin-bottom:14px;">📦 Buscar producto manualmente</div>
      <input id="modal-buscar-input" type="text" placeholder="Nombre o código del producto..."
        style="width:100%;box-sizing:border-box;padding:12px;background:var(--bg-s);border:1px solid #444;border-radius:8px;color:var(--tx);font-size:var(--fs-md);margin-bottom:10px;"
        oninput="_buscarProductoModal(this.value)">
      <div id="modal-buscar-resultados" style="max-height:280px;overflow-y:auto;"></div>
      <button onclick="this.closest('.modal-rec').remove()"
        style="width:100%;padding:12px;background:var(--bg-s);color:var(--tx3);border:1px solid var(--brd);border-radius:8px;cursor:pointer;margin-top:10px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
  setTimeout(() => { const i = document.getElementById('modal-buscar-input'); if(i) i.focus(); }, 100);
}

let _buscarModalTimer;
/**
 * Busca productos por texto con debounce y renderiza resultados en el modal.
 * @param {string} q - Texto de busqueda ingresado por el operario
 */
async function _buscarProductoModal(q) {
  clearTimeout(_buscarModalTimer);
  if (q.length < 2) { document.getElementById('modal-buscar-resultados').innerHTML = ''; return; }
  _buscarModalTimer = setTimeout(async () => {
    const res = await get('/api/productos/?q=' + encodeURIComponent(q) + '&limit=8').catch(() => ({ productos: [] }));
    const productos = res.productos || [];
    const el = document.getElementById('modal-buscar-resultados');
    if (!el) return;
    if (!productos.length) { el.innerHTML = '<div style="color:var(--tx3);padding:10px;font-size:var(--fs-sm);">Sin resultados</div>'; return; }
    el.innerHTML = productos.map(p => `
      <button onclick="_seleccionarProductoManual(${esc(p.id)},'${(p.nombre||'').replace(/'/g,"\\'")}',this.closest('.modal-rec'))"
        style="width:100%;padding:12px;margin-bottom:6px;background:var(--bg-input);border:1px solid var(--brd);color:var(--tx);border-radius:8px;cursor:pointer;text-align:left;">
        <div style="font-size:var(--fs-sm);font-weight:600;">${esc(p.nombre)}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(p.codigo)}</div>
      </button>`).join('');
  }, 350);
}

/**
 * Procesa la seleccion manual de un producto, pide cantidad y genera LPN si es paca.
 * @param {number} productoId - ID del producto seleccionado
 * @param {string} nombre - Nombre del producto para mostrar en el prompt
 * @param {HTMLElement} modal - Elemento del modal de busqueda para cerrarlo
 */
async function _seleccionarProductoManual(productoId, nombre, modal) {
  // Producto seleccionado sin código → preguntar cantidad y generar LPN
  const cantidad = await _modalCantidad('Unidades de la paca',
    `¿Cuántas unidades tiene esta paca de «${esc(nombre)}»? Si es una unidad suelta, escriba 1.`,
    { min: 1, valorInicial: 1, textoConfirmar: 'Continuar' });
  if (cantidad === null) return; // canceló

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

/** Confirma la recepcion activa, pidiendo remision y validando completitud. */
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

/**
 * Muestra modal para capturar el numero de remision/factura fisica del proveedor.
 * @returns {Promise<string|null>} Numero de remision ingresado, o null si el operario cancela
 */
function _pedirRemision() {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9500;display:flex;align-items:center;justify-content:center;padding:24px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:16px;padding:28px;max-width:360px;width:100%;">
        <div style="font-size:17px;font-weight:800;color:var(--tx);margin-bottom:6px;">📄 Remisión del proveedor</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:18px;">Ingresa el número de remisión o factura física que llegó con el camión. Siesa lo requiere para cerrar la entrada.</div>
        <input id="_rem-input" type="text" placeholder="Ej: 00123456"
          style="width:100%;box-sizing:border-box;padding:13px;background:var(--bg-s);border:1px solid #555;border-radius:10px;color:var(--tx);font-size:var(--fs-lg);font-weight:700;margin-bottom:16px;letter-spacing:1px;"
          onkeydown="if(event.key==='Enter') document.getElementById('_rem-ok').click()">
        <button id="_rem-ok"
          style="width:100%;padding:15px;font-size:var(--fs-md);font-weight:700;background:#15803d;color:#fff;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">
          Confirmar recepción
        </button>
        <button id="_rem-cancel"
          style="width:100%;padding:12px;font-size:var(--fs-sm);background:var(--bg-input);color:var(--tx3);border:1px solid var(--brd);border-radius:10px;cursor:pointer;">
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

/** Limpia la recepcion activa y vuelve a la lista de recepciones. */
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

// El conteo de un traslado entrante puede tener decenas de ítems y nunca toca
// el backend hasta el POST final (a propósito, para no depender de red
// mientras se cuenta) — pero eso significa que hasta ahora vivía solo en esta
// variable JS: un refresh o que Android recicle la pestaña de fondo borraba
// toda la sesión de conteo sin aviso. Se persiste en localStorage en cada
// ajuste y se restaura al reabrir el mismo traslado.
const _recTrasladoConteoKey = id => 'wms_rec_traslado_conteo_' + id;
function _recepGuardarConteoTraslado() {
  if (!_REC_TRASLADO_ACTIVO) return;
  try { localStorage.setItem(_recTrasladoConteoKey(_REC_TRASLADO_ACTIVO.id), JSON.stringify(_REC_CONTEOS)); } catch (_) {}
}
function _recepLimpiarConteoTraslado(id) {
  try { localStorage.removeItem(_recTrasladoConteoKey(id)); } catch (_) {}
}

/**
 * Carga y renderiza la lista de traslados pendientes de recepcion (NB1).
 * @param {boolean} [silencioso=false] - true omite el spinner de carga inicial
 */
async function recepCargarTraslados(silencioso = false) {
  if (_REC_TRASLADO_ACTIVO) return;
  const el = document.getElementById('contenido-traslados-rec');
  if (!el) return;
  if (!silencioso) el.innerHTML = '<div style="text-align:center;padding:40px;color:var(--tx3);">Cargando...</div>';
  try {
    const _bodRec = OPERARIO?.bodega_siesa_id || 'NB1';
    const r = await get(`/api/traslados/pendientes-recepcion?bodega=${_bodRec}`);
    _REC_TRASLADOS_PENDIENTES = r.solicitudes || [];
    const badge = document.getElementById('badge-traslados-rec');
    if (badge) {
      badge.style.display = _REC_TRASLADOS_PENDIENTES.length ? 'inline' : 'none';
      badge.textContent   = _REC_TRASLADOS_PENDIENTES.length;
    }
    if (!_REC_TRASLADOS_PENDIENTES.length) {
      el.innerHTML = `<div style="text-align:center;padding:50px 20px;">
        <div style="font-size:40px;">✓</div>
        <div style="font-size:var(--fs-lg);font-weight:700;margin-top:10px;color:var(--ok-tx);">Sin traslados pendientes</div>
        <button onclick="_refreshBtn(event, recepCargarTraslados)" style="margin-top:20px;padding:12px 24px;font-size:var(--fs-md);background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">Actualizar</button>
      </div>`;
      return;
    }
    el.innerHTML = _REC_TRASLADOS_PENDIENTES.map(s => {
      const totalEsp = (s.items || []).reduce((a, i) => a + (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0), 0);
      // Un traslado de averías NO puede verse igual que uno normal.
      //
      // Esta tarjeta se pintaba en verde de éxito para todo. Quien recibe
      // mercancía rota necesita saberlo ANTES de contarla, y necesita leer por
      // qué la declararon: ese motivo lo escribió alguien que estuvo ahí, y
      // quien cuenta acá no estuvo. Es el tercero de los cuatro momentos de
      // validación del proceso, y sin esta información no es una validación.
      const _ave = s.es_averia;
      const _fondo = _ave ? 'var(--warn-bg)' : 'var(--ok-bg)';
      const _borde = _ave ? 'var(--warn-brd)' : 'var(--ok-brd)';
      const _acento = _ave ? 'var(--warn-tx)' : 'var(--ok-tx)';
      return `
      <div style="background:${_fondo};border:1px solid ${_borde};border-radius:12px;padding:14px;margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
          <div style="font-size:var(--fs-md);font-weight:800;">${_ave ? '⚠ ' : ''}${esc(s.codigo)}</div>
          <div style="font-size:var(--fs-xs);color:${_acento};font-weight:600;">Desde ${esc(s.bodega_origen_siesa || '—')}</div>
        </div>
        ${_ave ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);font-weight:700;margin-bottom:6px;">MERCANCÍA AVERIADA — contá lo que llegó; si estaba rota lo decide el administrador después</div>` : ''}
        ${_ave && s.averia_evidencia ? `<div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:8px;border-left:2px solid var(--warn-brd);padding-left:8px;">Lo que revisó el punto: ${esc(s.averia_evidencia)}</div>` : ''}
        <div style="font-size:var(--fs-xs);color:${_acento};margin-bottom:8px;">📦 ${esc(s.total_items)} ítem${s.total_items !== 1 ? 's' : ''} · ${totalEsp} und esperadas</div>
        ${(s.items || []).slice(0, 3).map(i => `
          <div style="font-size:var(--fs-xs);color:var(--tx2);padding:2px 0;">
            ${i.producto_nombre || i.producto_codigo} · ${i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0} und
            ${i.motivo_averia ? `<div style="color:var(--warn-tx);padding-left:8px;">${esc(i.motivo_averia)}</div>` : ''}
          </div>`).join('')}
        ${(s.items || []).length > 3 ? `<div style="font-size:var(--fs-xs);color:var(--tx3);padding:2px 0;">+ ${s.items.length - 3} más...</div>` : ''}
        <button onclick="recepAbrirConteoTraslado(${esc(s.id)})"
          style="width:100%;padding:13px;margin-top:12px;background:var(--pm-fill);color:#fff;border:none;border-radius:10px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">
          📋 Contar productos
        </button>
      </div>`;
    }).join('');
  } catch (e) {
    if (!silencioso) el.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:20px;">Error cargando traslados</div>';
  }
}

/**
 * Abre la pantalla de conteo para un traslado pendiente.
 * @param {number} id - ID de la solicitud de traslado
 */
function recepAbrirConteoTraslado(id) {
  const s = _REC_TRASLADOS_PENDIENTES.find(x => x.id === id);
  if (!s) return;
  _REC_TRASLADO_ACTIVO = s;
  _REC_CONTEOS = {};
  (s.items || []).forEach(i => { _REC_CONTEOS[i.producto_id] = 0; });
  // Restaurar un conteo pendiente si la pestaña se recicló a mitad de esta recepción.
  try {
    const guardado = localStorage.getItem(_recTrasladoConteoKey(id));
    if (guardado) {
      const previo = JSON.parse(guardado);
      Object.keys(previo).forEach(pid => { if (pid in _REC_CONTEOS) _REC_CONTEOS[pid] = previo[pid]; });
      alerta('Se restauró un conteo pendiente de este traslado', 'info');
    }
  } catch (_) {}
  _recepRenderPickingTraslado();
  setTimeout(() => { const inp = document.getElementById('rec-tras-scan-input'); if (inp) inp.focus(); }, 150);
}

/** Limpia el traslado activo y vuelve a la lista de traslados pendientes. */
function recepVolverListaTraslados() {
  if (_REC_TRASLADO_ACTIVO) _recepLimpiarConteoTraslado(_REC_TRASLADO_ACTIVO.id);
  _REC_TRASLADO_ACTIVO = null;
  _REC_CONTEOS = {};
  recepCargarTraslados();
}

/** Renderiza la pantalla de conteo/escaneo del traslado activo. */
function _recepRenderPickingTraslado() {
  const el = document.getElementById('contenido-traslados-rec');
  if (!el || !_REC_TRASLADO_ACTIVO) return;
  const s = _REC_TRASLADO_ACTIVO;
  const items = s.items || [];
  const todoContado = items.every(i => (_REC_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  const algoContado = items.some(i => (_REC_CONTEOS[i.producto_id] || 0) > 0);
  const btnColor  = todoContado ? '#15803d' : '#b45309';
  const btnTexto  = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial';

  el.innerHTML = `
    <div style="padding:0;">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <button onclick="recepVolverListaTraslados()"
          style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:var(--fs-sm);flex-shrink:0;">
          ← Volver
        </button>
        <div style="min-width:0;">
          <div style="font-size:var(--fs-md);font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${s.es_averia ? '⚠ ' : ''}${esc(s.codigo)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">Desde ${esc(s.bodega_origen_siesa || '—')} → ${esc(s.bodega_destino_siesa || '—')}</div>
        </div>
      </div>

      ${s.es_averia ? `
      <div style="background:var(--warn-bg);border:1px solid var(--warn-brd);border-radius:10px;padding:12px;margin-bottom:14px;">
        <div style="font-size:var(--fs-sm);color:var(--warn-tx);font-weight:700;margin-bottom:4px;">Traslado de averías</div>
        <div style="font-size:var(--fs-xs);color:var(--tx2);">
          Contá cuántas unidades llegaron. <b>No decidas si estaban rotas</b> — eso lo
          dictamina el administrador antes de ubicarlas.
        </div>
        ${s.averia_evidencia ? `<div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:8px;border-left:2px solid #92400e;padding-left:8px;">Lo que revisó el punto: ${esc(s.averia_evidencia)}</div>` : ''}
      </div>` : ''}

      <div style="background:var(--bg-s);border-radius:10px;padding:12px;margin-bottom:14px;">
        <div style="font-size:var(--fs-xs);color:var(--tx3);text-align:center;margin-bottom:8px;">Escanea el código o usá los botones +/−</div>
        <div style="display:flex;gap:8px;">
          <input id="rec-tras-scan-input" type="text" placeholder="Escanea o escribe el código..."
            style="flex:1;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ recepScanTraslado(v); this.value=''; } }"
            autocomplete="off" autocorrect="off" spellcheck="false">
          <button onclick="const v=document.getElementById('rec-tras-scan-input').value.trim();if(v){recepScanTraslado(v);document.getElementById('rec-tras-scan-input').value='';}"
            style="padding:10px 14px;background:var(--pm-fill);color:#fff;border:none;border-radius:8px;font-size:var(--fs-lg);cursor:pointer;">↵</button>
        </div>
      </div>

      <div id="rec-tras-items" style="margin-bottom:14px;">
        ${_recepRenderItemsTraslado(items)}
      </div>

      <button id="btn-confirmar-rec-traslado" onclick="recepConfirmarTraslado()"
        ${algoContado || todoContado ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:var(--fs-lg);font-weight:700;background:${algoContado || todoContado ? btnColor : 'var(--bg-s2)'};color:var(--tx);border:none;border-radius:14px;cursor:${algoContado || todoContado ? 'pointer' : 'default'};margin-bottom:10px;">
        ${algoContado || todoContado ? btnTexto : 'Contá al menos un ítem para continuar'}
      </button>

      <button onclick="recepVolverListaTraslados()"
        style="width:100%;padding:12px;font-size:var(--fs-sm);background:var(--bg-input);color:var(--tx3);border:1px solid var(--brd);border-radius:10px;cursor:pointer;">
        Cancelar — volver a la lista
      </button>
    </div>`;
}

/**
 * Genera el HTML de los items de un traslado con contadores +/- y barras de progreso.
 * @param {Array<Object>} items - Items del traslado con producto_id, cantidad_enviada, etc.
 * @returns {string} HTML concatenado de todos los items
 */
function _recepRenderItemsTraslado(items) {
  return items.map(i => {
    const esperado = i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0;
    const contado  = _REC_CONTEOS[i.producto_id] || 0;
    const completo = contado >= esperado;
    const pct = esperado > 0 ? Math.min((contado / esperado) * 100, 100) : 0;
    return `
      <div id="rec-tras-item-${esc(i.producto_id)}"
        style="background:${completo ? 'var(--ok-bg)' : 'var(--bg-s)'};border:1px solid ${completo ? '#166534' : 'var(--brd)'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:var(--fs-sm);font-weight:600;color:${completo ? 'var(--ok-tx)' : 'var(--tx)'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${i.producto_nombre || i.producto_codigo}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${i.producto_codigo_siesa || i.producto_codigo}</div>
            ${i.motivo_averia ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:3px;">⚠ ${esc(i.motivo_averia)}</div>` : ''}
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;padding-left:8px;">
            <button onclick="recepContarItem(${esc(i.producto_id)}, -1)"
              style="width:34px;height:34px;background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">−</button>
            <div style="text-align:center;min-width:54px;">
              <div style="font-size:26px;font-weight:900;line-height:1;color:${completo ? 'var(--ok-tx)' : 'var(--tx)'};">${contado}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);">/ ${esperado}</div>
            </div>
            <button onclick="recepContarItem(${esc(i.producto_id)}, 1)"
              style="width:34px;height:34px;background:var(--pm-fill);border:none;color:#fff;border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">+</button>
          </div>
        </div>
        <div style="height:5px;background:var(--bg-s2);border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#15803d' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.2s;"></div>
        </div>
      </div>`;
  }).join('');
}

/**
 * Procesa un codigo escaneado en la recepcion de traslado, incrementando el conteo.
 * @param {string} codigo - Codigo de barras o codigo Siesa del producto
 */
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

/**
 * Ajusta el conteo de un item del traslado y actualiza la UI.
 * @param {number} productoId - ID del producto a ajustar
 * @param {number} delta - Incremento (+1) o decremento (-1) a aplicar
 */
function recepContarItem(productoId, delta) {
  if (!_REC_TRASLADO_ACTIVO) return;
  const item = (_REC_TRASLADO_ACTIVO.items || []).find(i => i.producto_id === productoId);
  if (!item) return;
  const esperado = item.cantidad_enviada || item.cantidad_aprobada || item.cantidad_solicitada || 0;
  const nuevo = (_REC_CONTEOS[productoId] || 0) + delta;
  _REC_CONTEOS[productoId] = Math.max(0, Math.min(nuevo, esperado));
  _recepGuardarConteoTraslado();
  const itemsEl = document.getElementById('rec-tras-items');
  if (itemsEl) itemsEl.innerHTML = _recepRenderItemsTraslado(_REC_TRASLADO_ACTIVO.items || []);
  const items = _REC_TRASLADO_ACTIVO.items || [];
  const todoContado = items.every(i => (_REC_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  const algoContado = items.some(i => (_REC_CONTEOS[i.producto_id] || 0) > 0);
  const btn = document.getElementById('btn-confirmar-rec-traslado');
  if (btn) {
    btn.disabled = !(algoContado || todoContado);
    btn.style.background = !algoContado && !todoContado ? '#222' : (todoContado ? '#15803d' : '#b45309');
    btn.textContent = !algoContado && !todoContado ? 'Contá al menos un ítem para continuar' : (todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial');
    btn.style.cursor = algoContado || todoContado ? 'pointer' : 'default';
  }
}

/** Confirma la recepcion del traslado activo enviando los conteos al backend. */
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
    // Idempotente en el backend (TrasladoService.confirmar_recepcion rechaza
    // un segundo intento sobre un traslado ya ENTREGADA con 400, no duplica
    // el 173079) — seguro reintentar automáticamente ante fallo de red.
    await postConReintento(`/api/traslados/${s.id}/recibir`, {
      items_recibidos: items.map(i => ({
        id: i.id,
        cantidad_recibida: _REC_CONTEOS[i.producto_id] || 0
      }))
    });
    beepDone();
    alerta('✓ Recepción confirmada — ETS generado en Siesa', 'exito');
    _recepLimpiarConteoTraslado(s.id);
    _REC_TRASLADO_ACTIVO = null;
    _REC_CONTEOS = {};
    setTimeout(recepCargarTraslados, 1200);
  } catch (e) {
    alerta(e.message || 'Error al confirmar', 'error');
    if (btn) { btn.textContent = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial'; btn.disabled = false; }
  }
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Tabs (OCs / Traslados / Devoluciones)
// ─────────────────────────────────────────────────────────────

/**
 * Cambia el tab activo del modulo recepcionista (OCs, Traslados, Devoluciones).
 * @param {string} tab - Identificador del tab: 'ocs', 'traslados' o 'dev'
 */
function recTab(tab) {
  REC_TAB_ACTIVO = tab;
  const tabOcs  = document.getElementById('rec-tab-ocs');
  const tabTras = document.getElementById('rec-tab-traslados');
  const tabDev  = document.getElementById('rec-tab-dev');
  const contOcs  = document.getElementById('contenido-recepcion');
  const contTras = document.getElementById('contenido-traslados-rec');
  const contDev  = document.getElementById('contenido-devoluciones');
  const subtabsOcs = document.getElementById('rec-oc-subtabs');
  if (!tabOcs) return;

  const activo  = 'border-bottom:2px solid var(--pm);color:var(--acento-tx);font-weight:600;';
  const inactivo = 'border-bottom:2px solid transparent;color:var(--tx3);';
  if (tabOcs)  tabOcs.style.cssText  = `flex:1;padding:11px;font-size:var(--fs-sm);text-align:center;cursor:pointer;${tab==='ocs' ? activo : inactivo}`;
  if (tabTras) tabTras.style.cssText = `flex:1;padding:11px;font-size:var(--fs-sm);text-align:center;cursor:pointer;position:relative;${tab==='traslados' ? activo : inactivo}`;
  if (tabDev)  tabDev.style.cssText  = `flex:1;padding:11px;font-size:var(--fs-sm);text-align:center;cursor:pointer;position:relative;${tab==='dev' ? activo : inactivo}`;
  // re-append badges (se pierden al resetear cssText)
  const badgeTras = document.getElementById('badge-traslados-rec');
  const badgeDev  = document.getElementById('badge-dev');
  if (badgeTras && tabTras) tabTras.appendChild(badgeTras);
  if (badgeDev  && tabDev)  tabDev.appendChild(badgeDev);

  if (contOcs)  contOcs.style.display  = tab === 'ocs'       ? 'block' : 'none';
  if (contTras) contTras.style.display  = tab === 'traslados' ? 'block' : 'none';
  if (contDev)  contDev.style.display   = tab === 'dev'       ? 'block' : 'none';
  if (subtabsOcs) subtabsOcs.style.display = tab === 'ocs' ? 'flex' : 'none';

  if (tab === 'traslados' && !_REC_TRASLADO_ACTIVO) recepCargarTraslados();
  if (tab === 'dev') cargarDevoluciones();
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Devolución de Cliente (busca pedido, cuenta, confirma)
// Reemplaza el flujo reactivo de reconciliación (TareaDevolucion, DEPRECATED).
// ─────────────────────────────────────────────────────────────

/**
 * Pantalla de devoluciones de recepción:
 *  · 🚚 «Llegó el camión» — por ruta: los bultos que el conductor declaró de
 *    vuelta (escanear → RETORNADO; «Cerrar llegada» → lo que no apareció queda
 *    FALTANTE) y las devoluciones sin contar de esa ruta (m045devol: nacen
 *    EN_CAMION al confirmar la parada, no al liquidar).
 *  · Avisos (sin contar 24/48 h, NC sin aprobar 3 días, RC esperando su NC).
 *  · El buscador de pedido para una devolución de mostrador.
 * Antes el panel de bultos era solo informativo, y el recepcionista ni lo
 * podía cargar (su endpoint era de admin).
 * @param {boolean} [silencioso=false] - true omite el spinner de carga inicial
 */
let _REC_LLEGADAS = [];

async function cargarDevoluciones(silencioso = false) {
  if (DEVOLUCION_ACTUAL) return;
  const el = document.getElementById('contenido-devoluciones');
  const badge = document.getElementById('badge-dev');
  if (!el) return;

  try {
    const r = await get('/api/devoluciones/llegadas').catch(() => ({ rutas: [] }));
    _REC_LLEGADAS = r.rutas || [];
    const pendientes = _REC_LLEGADAS.reduce(
      (n, ru) => n + (ru.cuadre ? ru.cuadre.en_camion_sin_recibir : 0) + (ru.devoluciones || []).length, 0);

    if (badge) {
      badge.style.display = pendientes ? 'inline' : 'none';
      badge.textContent = pendientes;
    }

    let html = '<div id="panel-avisos-devoluciones"></div>';
    html += recLlegadasHtml(_REC_LLEGADAS);
    html += `<div id="panel-pendientes-ruta"></div>`;

    html += `
      <div style="font-size:var(--fs-xs);font-weight:600;color:var(--tx2);padding:4px 0 8px;border-bottom:1px solid var(--brd);margin-bottom:10px;margin-top:16px;">
        DEVOLUCIÓN DE MOSTRADOR
      </div>
      <div class="rec-card">
        <div style="font-size:var(--fs-sm);color:var(--tx3);margin-bottom:10px;">Busca el pedido del cliente que devuelve mercancía</div>
        <div style="display:flex;gap:8px;">
          <input id="input-pedido-dev" type="text" placeholder="Número de pedido (ej: PD1347)"
            style="flex:1;padding:12px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-md);"
            onkeydown="if(event.key==='Enter') buscarPedidoDevolucion()" />
          <button onclick="buscarPedidoDevolucion()"
            style="padding:12px 16px;background:var(--pm-fill);color:#fff;border:none;border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">
            Buscar
          </button>
        </div>
        <div id="estado-busqueda-dev" style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx3);"></div>
      </div>
      <div id="panel-nc-pendientes"></div>`;

    el.innerHTML = html;

    cargarPendientesDeRuta();
    recCargarAvisosDevoluciones();

    // Solo admin/jefe_almacen — coincide con el gate del backend (Roles.SUPERVISION)
    if (OPERARIO && ['admin', 'jefe_almacen'].includes(OPERARIO.rol)) {
      cargarPendientesAprobacionNC();
    }
  } catch (e) {
    if (badge) badge.style.display = 'none';
    el.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:20px;">Error cargando devoluciones</div>';
  }
}

/**
 * HTML de la cola «Llegó el camión». Solo POSICIONES en los `onclick`: los
 * datos se buscan en `_REC_LLEGADAS` (un dato dentro de JS dentro de un
 * atributo no se protege con `esc`).
 * @param {Array} rutas - `GET /api/devoluciones/llegadas` → rutas
 * @returns {string}
 */
/**
 * Un código que el servidor mandó sin su texto (un servidor viejo en caché):
 * se dice en palabras, nunca crudo. «NO_PAGO» → «no pago». El texto bueno es
 * el del servidor (`motivo_texto`, del catálogo de motivos).
 * @param {string} codigo
 */
function recPalabraDeCodigo(codigo) {
  return String(codigo || '').toLowerCase().replace(/_/g, ' ');
}

function recLlegadasHtml(rutas) {
  if (!rutas || !rutas.length) return '';
  let html = `<div style="font-size:var(--fs-xs);font-weight:600;color:var(--acento-tx);padding:4px 0 8px;border-bottom:1px solid var(--brd);margin-bottom:10px;">
      🚚 LLEGÓ EL CAMIÓN — ${esc(rutas.length)} RUTA${rutas.length !== 1 ? 'S' : ''} CON MERCANCÍA DE VUELTA
    </div>`;
  rutas.forEach((ru, i) => {
    const c = ru.cuadre || {};
    const horas = ru.horas_sin_contar;
    const tono = horas == null ? 'var(--tx3)' : (horas >= 48 ? 'var(--err-tx)' : (horas >= 24 ? 'var(--warn-tx)' : 'var(--tx3)'));
    const devs = ru.devoluciones || [];
    html += `
      <div class="rec-card" style="margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
          <div>
            <div class="rec-titulo" style="font-size:var(--fs-sm);color:var(--tx);">Ruta #${esc(ru.ruta_id)} · ${esc(ru.conductor || 'Sin conductor')}</div>
            <div class="rec-sub">${esc(ru.placa || 'Sin placa')}${ru.llegada_cerrada_at ? ' · llegada cerrada' : ''}</div>
          </div>
          ${horas != null ? `<span style="font-size:var(--fs-xs);color:${tono};font-weight:700;">${esc(Math.round(horas))} h sin contar</span>` : ''}
        </div>
        <div style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx2);">
          Salieron ${esc(c.salieron)} · entregados ${esc(c.entregados)} · volvieron ${esc(c.retornados)} ·
          faltantes ${esc(c.faltantes)} · <b style="color:${c.en_camion_sin_recibir ? 'var(--warn-tx)' : 'var(--ok-tx)'};">por recibir ${esc(c.en_camion_sin_recibir)}</b>
          ${c.exacto ? ' · <span style="color:var(--ok-tx);">cuadre exacto</span>' : ''}
        </div>
        ${c.en_camion_sin_recibir ? `
        <div style="display:flex;gap:8px;margin-top:10px;">
          <input id="rec-lleg-bulto-${i}" type="text" placeholder="Escanea el bulto que volvió"
            style="flex:1;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);"
            onkeydown="if(event.key==='Enter') recLlegadaEscanear(${i})" autocomplete="off" autocorrect="off" spellcheck="false">
          <button onclick="recLlegadaEscanear(${i})"
            style="padding:10px 14px;background:var(--pm-fill);color:#fff;border:none;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">↵</button>
        </div>
        <button onclick="recLlegadaCerrar(${i})"
          style="margin-top:8px;width:100%;padding:10px;background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);border-radius:8px;font-size:var(--fs-sm);font-weight:600;cursor:pointer;">
          Cerrar llegada — lo no escaneado queda FALTANTE
        </button>` : ''}
        ${devs.map((d, k) => `
          <div style="margin-top:10px;padding:10px;background:var(--bg-s);border:1px solid ${d.problema_factura ? 'var(--err-brd)' : 'var(--brd)'};border-radius:8px;cursor:pointer;" onclick="recLlegadaContar(${i}, ${k})">
            <div style="display:flex;justify-content:space-between;gap:8px;">
              <span style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(d.pedido || '—')} · ${esc(d.cliente || 'Cliente sin nombre')}</span>
              <span style="font-size:var(--fs-xs);color:${d.estado === 'EN_CAMION' ? 'var(--info-tx)' : 'var(--acento-tx)'};">${d.estado === 'EN_CAMION' ? 'En el camión' : 'En bodega'}</span>
            </div>
            <div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:4px;">
              ${d.es_total ? 'Devolución total' : 'Devolución parcial'}${d.motivo ? ' · ' + esc(d.motivo_texto || recPalabraDeCodigo(d.motivo)) : ''}${d.sin_declaracion ? ' · el conductor no dijo qué volvió: cuente contra la factura' : ''} · toque para contar
            </div>
            ${d.problema_factura ? `<div style="font-size:var(--fs-xs);color:var(--err-tx);margin-top:4px;">${esc(d.problema_factura)}</div>` : ''}
          </div>`).join('')}
      </div>`;
  });
  return html;
}

/** Escanea un bulto que volvió (posición en `_REC_LLEGADAS`). */
async function recLlegadaEscanear(i) {
  const ru = _REC_LLEGADAS[i];
  const inp = document.getElementById('rec-lleg-bulto-' + i);
  const codigo = (inp ? inp.value : '').trim();
  if (!ru || !codigo) return;
  try {
    const r = await post(`/api/devoluciones/llegadas/${ru.ruta_id}/bulto`, { codigo_barras: codigo });
    vibrar(); flash();
    alerta(r.ya_estaba ? 'Ese bulto ya estaba recibido' :
      (r.no_declarado ? 'Recibido — el conductor no lo había marcado como devolución' : 'Bulto recibido'),
      r.no_declarado ? 'advertencia' : 'exito');
    cargarDevoluciones();
  } catch (e) {
    alerta('No se pudo recibir: ' + (e.status ? e.message : 'Error de conexión'), 'error');
  }
  if (inp) inp.value = '';
}

/** Cierra la llegada del camión: lo que no se escaneó queda FALTANTE (medido). */
async function recLlegadaCerrar(i) {
  const ru = _REC_LLEGADAS[i];
  if (!ru) return;
  const n = ru.cuadre ? ru.cuadre.en_camion_sin_recibir : 0;
  if (!(await _modalConfirmar(`${esc(n)} bulto(s) sin escanear quedan como FALTANTE (no volvieron).`,
      { titulo: `¿Cerrar la llegada de la ruta #${esc(ru.ruta_id)}?`, textoConfirmar: 'Cerrar llegada', peligro: n > 0 }))) return;
  try {
    const r = await post(`/api/devoluciones/llegadas/${ru.ruta_id}/cerrar`, {});
    vibrar(); flash();
    alerta(`Llegada cerrada · ${r.bultos_faltantes} faltante(s) · ${r.devoluciones_en_bodega} devolución(es) por contar`, r.bultos_faltantes ? 'advertencia' : 'exito');
    cargarDevoluciones();
  } catch (e) {
    alerta('No se pudo cerrar: ' + (e.status ? e.message : 'Error de conexión'), 'error');
  }
}

/** Abre el conteo de una devolución de la cola (posiciones, no datos). */
function recLlegadaContar(i, k) {
  const ru = _REC_LLEGADAS[i];
  const d = ru && (ru.devoluciones || [])[k];
  if (d) abrirPendienteDeRuta(d.id);
}

/**
 * Avisos de devoluciones (misma función que el resumen diario por correo) y,
 * para supervisión, «Verificar en Siesa» (lee `f350_ind_estado` de las NC).
 */
async function recCargarAvisosDevoluciones() {
  const cont = document.getElementById('panel-avisos-devoluciones');
  if (!cont) return;
  try {
    const t = await get('/api/devoluciones/tablero');
    cont.innerHTML = recAvisosHtml(t);
  } catch (e) {
    cont.innerHTML = '';
  }
}

/**
 * @param {Object} t - `GET /api/devoluciones/tablero`
 * @returns {string}
 */
function recAvisosHtml(t) {
  const a = (t && t.avisos) || {};
  const filas = [];
  const n = (x) => (x || []).length;
  if (n(a.sin_contar_48h)) filas.push(['var(--err-tx)', `${n(a.sin_contar_48h)} devolución(es) sin contar hace más de 48 h`]);
  if (n(a.sin_contar_24h)) filas.push(['var(--warn-tx)', `${n(a.sin_contar_24h)} devolución(es) sin contar hace más de 24 h`]);
  if (n(a.nc_anuladas)) filas.push(['var(--err-tx)', `${n(a.nc_anuladas)} nota(s) crédito ANULADA(S) en Siesa`]);
  if (n(a.nc_sin_aprobar_3d)) filas.push(['var(--warn-tx)', `${n(a.nc_sin_aprobar_3d)} nota(s) crédito sin aprobar hace más de 3 días`]);
  if (n(a.rc_esperando_nc_48h)) filas.push(['var(--warn-tx)', `${n(a.rc_esperando_nc_48h)} recibo(s) de caja esperando su nota crédito hace más de 48 h`]);
  const m = (t && t.medicion) || {};
  const h = m.horas_rechazo_a_conteo || {};
  const f = m.faltante_de_retorno || {};
  const medida = h.n ? `Del rechazo al conteo: mediana ${esc(h.mediana)} h · p90 ${esc(h.p90)} h (n=${esc(h.n)})` +
    (f.unidades ? ` · faltante de retorno ${esc(f.unidades)} und ($${esc(Math.round(f.valor || 0).toLocaleString('es-CO'))}${f.lineas_sin_valor ? ', ' + esc(f.lineas_sin_valor) + ' sin valor' : ''})` : '') : '';
  if (!filas.length && !medida) return '';
  return `<div class="rec-card" style="margin-bottom:10px;">
      ${filas.map(([tono, txt]) => `<div style="font-size:var(--fs-xs);color:${tono};font-weight:600;margin-bottom:4px;">${esc(txt)}</div>`).join('')}
      ${medida ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">${medida}</div>` : ''}
    </div>`;
}

/**
 * Devoluciones de ruta SIN CONTAR, en una sola lista (la de «Llegó el camión»
 * las agrupa por ruta). Nacen al confirmar la parada Parcial/Rechazada con lo
 * que declaró el conductor — la recepcionista cuenta y ajusta, no arma nada
 * desde cero. Las que siguen en el camión dicen «En el camión».
 */
async function cargarPendientesDeRuta() {
  const cont = document.getElementById('panel-pendientes-ruta');
  if (!cont) return;
  try {
    const r = await get('/api/devoluciones/pendientes-de-ruta');
    const pendientes = r.pendientes || [];
    if (!pendientes.length) { cont.innerHTML = ''; return; }

    cont.innerHTML = `
      <div style="font-size:var(--fs-xs);font-weight:600;color:var(--acento-tx);padding:4px 0 8px;border-bottom:1px solid var(--brd);margin-bottom:10px;">
        🔵 ${esc(pendientes.length)} ${pendientes.length !== 1 ? 'DEVOLUCIONES' : 'DEVOLUCIÓN'} DE RUTA SIN CONTAR
      </div>` +
      pendientes.map(d => `
        <div class="rec-card" style="margin-bottom:8px;cursor:pointer;" onclick="abrirPendienteDeRuta(${esc(d.id)})">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
              <div class="rec-titulo" style="font-size:var(--fs-sm);color:var(--tx);">${esc(d.numero_pedido_siesa || '—')}</div>
              <div class="rec-sub">${esc(d.cliente || 'Cliente sin nombre')}</div>
            </div>
            <span class="badge ${d.es_total ? 'badge-red' : 'badge-green'}">
              ${d.es_total ? 'DEVOLUCIÓN TOTAL' : 'DEVOLUCIÓN PARCIAL'}${d.estado === 'EN_CAMION' ? ' · EN EL CAMIÓN' : ''}
            </span>
          </div>
          <div style="margin-top:8px;font-size:var(--fs-xs);color:var(--tx2);">
            ${esc(d.lineas.length)} referencia${d.lineas.length !== 1 ? 's' : ''} · toca para verificar y confirmar
          </div>
        </div>`).join('');
  } catch (e) {
    cont.innerHTML = '';
  }
}

/**
 * Abre el conteo de una devolución de ruta. Antes la amarra a su factura
 * (`preparar-conteo`, con red): las líneas quedan una por línea de factura —
 * en un producto de doble unidad (PQ + UND) se cuenta cada una.
 */
function abrirPendienteDeRuta(devolucionId) {
  post(`/api/devoluciones/${devolucionId}/preparar-conteo`, {}).then(devolucion => {
    DEVOLUCION_ACTUAL = devolucion;
    renderLineasDevolucion(devolucion);
  }).catch(() => alerta('No se pudo abrir la devolución', 'error'));
}

/**
 * NC de devolución de cliente ya creadas en Siesa (Elaboración) que todavía
 * nadie ha marcado como aprobadas+cruzadas manualmente en Siesa. Ver
 * CLAUDE.md Regla #21 — Siesa no cruza la cartera sola, ni al crear ni al
 * aprobar el documento.
 */
async function cargarPendientesAprobacionNC() {
  const cont = document.getElementById('panel-nc-pendientes');
  if (!cont) return;
  try {
    const r = await get('/api/devoluciones/pendientes-aprobacion-nc');
    const pendientes = r.pendientes || [];
    if (!pendientes.length) { cont.innerHTML = ''; return; }

    cont.innerHTML = `
      <div style="font-size:var(--fs-xs);font-weight:600;color:var(--warn-tx);padding:4px 0 8px;border-bottom:1px solid var(--warn-brd);margin:16px 0 10px;">
        🟡 ${esc(pendientes.length)} NC PENDIENTE${pendientes.length !== 1 ? 'S' : ''} DE APROBAR EN SIESA
      </div>
      <button onclick="recVerificarNCSiesa()"
        style="width:100%;margin-bottom:10px;padding:10px;background:var(--info-bg);color:var(--info-tx);border:1px solid var(--info-brd);border-radius:8px;font-size:var(--fs-sm);font-weight:600;cursor:pointer;">
        Verificar en Siesa
      </button>` +
      pendientes.map(d => `
        <div class="rec-card" style="border-color:#78350f;background:var(--warn-bg);margin-bottom:8px;">
          <div class="rec-titulo" style="font-size:var(--fs-sm);color:var(--warn-tx);">${esc(d.codigo)} — ${esc(d.numero_pedido_siesa || '—')}</div>
          <div class="rec-sub" style="color:var(--warn-tx);">${esc(d.cliente || 'Cliente sin nombre')} · FE ${esc(d.tipo_docto_fe)}-${esc(d.consec_fe)}</div>
          <div style="margin-top:4px;font-size:var(--fs-xs);color:var(--warn-tx);">
            NC creada en Siesa: ${d.siesa_nc_triggered_at ? esc(new Date(d.siesa_nc_triggered_at).toLocaleString('es-CO')) : '—'}${d.siesa_nc_consec ? ' · NCE-' + esc(d.siesa_nc_consec) : ''}${d.nc_estado_siesa === 2 ? ' · ANULADA en Siesa' : ''}
          </div>
          <button onclick="marcarNCAprobada(${esc(d.id)})"
            style="margin-top:10px;width:100%;padding:10px;background:#78350f;color:var(--warn-tx);border:none;border-radius:8px;font-size:var(--fs-sm);font-weight:600;cursor:pointer;">
            Marcar aprobada a mano (con motivo)
          </button>
        </div>`).join('');
  } catch (e) {
    cont.innerHTML = '';
  }
}

/**
 * RESPALDO manual: alguien declara que aprobó la NC en Siesa. La verificación
 * normal la hace el cron (lee `f350_ind_estado`); esto pide motivo y queda en
 * la bitácora (FORZAR), y libera lo devuelto a picking.
 */
async function marcarNCAprobada(devolucionId) {
  const motivo = await _modalTexto('Marcar la nota crédito como aprobada',
    '¿Por qué la marca a mano? La verificación automática no la vio aprobada en Siesa.',
    { obligatorio: true, textoConfirmar: 'Marcar aprobada' });
  if (motivo == null) return;
  try {
    await post(`/api/devoluciones/${devolucionId}/marcar-nc-aprobada`, { motivo });
    vibrar(); flash();
    cargarPendientesAprobacionNC();
  } catch (e) {
    alerta('Error: ' + (e.status ? e.message : 'Error de conexión'), 'error');
  }
}

/** Lee en Siesa el estado de las NC pendientes y marca las aprobadas. */
async function recVerificarNCSiesa() {
  try {
    const r = await post('/api/devoluciones/verificar-nc', {});
    const txt = r.omitido ? r.omitido :
      `${r.aprobadas || 0} aprobada(s) · ${r.en_elaboracion || 0} en elaboración · ` +
      `${r.sin_consecutivo || 0} sin consecutivo · ${r.fuera_de_la_consulta || 0} fuera de la consulta` +
      (r.anuladas ? ` · ${r.anuladas} ANULADA(S)` : '');
    alerta(txt, r.anuladas ? 'advertencia' : 'exito');
    cargarPendientesAprobacionNC();
  } catch (e) {
    alerta('No se pudo verificar: ' + (e.status ? e.message : 'Error de conexión'), 'error');
  }
}

/** Busca el pedido/factura y abre la pantalla de conteo. */
async function buscarPedidoDevolucion() {
  const inp = document.getElementById('input-pedido-dev');
  const estado = document.getElementById('estado-busqueda-dev');
  const numero = (inp ? inp.value : '').trim();
  if (!numero) { alerta('Ingresa el número de pedido', 'advertencia'); return; }

  if (estado) { estado.textContent = '⏳ Buscando...'; estado.style.color = 'var(--info-tx)'; }
  try {
    const r = await get('/api/devoluciones/pedido/' + encodeURIComponent(numero));
    DEVOLUCION_ACTUAL = r;
    renderLineasDevolucion(r);
  } catch (e) {
    if (estado) {
      estado.textContent = 'Error: ' + (e.status ? e.message : 'Error de conexión');
      estado.style.color = 'var(--err-tx)';
    }
  }
}

/**
 * Renderiza la tabla de líneas facturadas para contar físicamente la devolución.
 * @param {Object} datos - Resultado de GET /api/devoluciones/pedido/<numero>:
 *   tarea_packing_id, numero_pedido_siesa, cliente, almacen_id, tipo_docto_fe,
 *   consec_fe, lineas[{producto_id, producto_codigo, producto_nombre, codigo_barras,
 *   codigo_siesa, cantidad_facturada, f470_id_unidad_medida, f150_id_bodega, f470_rowid}]
 */
function renderLineasDevolucion(datos) {
  recTab('dev');
  const el = document.getElementById('contenido-devoluciones');
  if (!el) return;

  // Si datos.id existe, es una devolución de ruta ya armada (nació al
  // confirmar la parada): las cantidades vienen con lo que declaró el
  // conductor; la recepcionista cuenta y ajusta. Si no, es la búsqueda de
  // mostrador (todo en 0, con el tope = facturado − lo ya devuelto).
  const esPendienteDeRuta = !!datos.id;
  const problema = datos.problema_factura || null;
  const avisos = ((datos.declaracion_conductor || {}).avisos_vinculacion) || [];

  const filas = (datos.lineas || []).map((l, i) => {
    const tope = Math.max(0, Number(l.cantidad_facturada || 0) - Number(l.ya_devuelto || 0));
    const declarado = l.cantidad_declarada;
    const averiadas = l.cantidad_averiada != null ? l.cantidad_averiada : (l.es_averiado ? l.cantidad_devuelta : 0);
    return `
    <div class="rec-card" style="margin-bottom:10px;">
      <div style="font-size:var(--fs-md);font-weight:700;">${esc(l.producto_nombre)}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">${esc(l.producto_codigo)} · Facturado: ${esc(l.cantidad_facturada)}${l.f470_id_unidad_medida ? ' ' + esc(l.f470_id_unidad_medida) : ''}${l.ya_devuelto ? ' · ya devuelto ' + esc(l.ya_devuelto) : ''}${esPendienteDeRuta ? (declarado != null ? ' · el conductor dijo ' + esc(declarado) : ' · el conductor no lo declaró por línea') : ''}</div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <span style="font-size:var(--fs-xs);color:var(--tx2);">Volvió:</span>
        <input id="cant-dev-${i}" type="number" min="0" max="${esc(tope)}" step="1" value="${esc(l.cantidad_devuelta || 0)}"
          style="width:90px;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-md);text-align:center;" />
        <span style="font-size:var(--fs-xs);color:var(--err-tx);">De esas, averiadas:</span>
        <input id="averiadas-dev-${i}" type="number" min="0" step="1" value="${esc(averiadas || 0)}"
          style="width:80px;padding:10px;background:var(--bg-s);border:1px solid var(--err-brd);border-radius:8px;color:var(--tx);font-size:var(--fs-md);text-align:center;" />
      </div>
    </div>`;
  }).join('');

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
      <button onclick="volverBusquedaDevolucion()"
        style="background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:var(--fs-sm);">
        ← Volver
      </button>
      <span style="font-size:var(--fs-sm);font-weight:700;">Pedido ${esc(datos.numero_pedido_siesa)} · ${esc(datos.cliente || '—')}</span>
    </div>

    ${problema ? `
    <div style="margin-bottom:10px;padding:10px 12px;background:var(--err-bg);border:1px solid var(--err-brd);border-radius:8px;font-size:var(--fs-sm);color:var(--err-tx);">
      ⛔ ${esc(problema)}
    </div>` : ''}
    ${avisos.map(a => `<div style="margin-bottom:6px;font-size:var(--fs-xs);color:var(--warn-tx);">⚠ ${esc(a)}</div>`).join('')}
    ${esPendienteDeRuta ? `
    <div style="margin-bottom:10px;padding:10px 12px;background:var(--bg-s2);border:1px solid var(--brd);border-radius:8px;font-size:var(--fs-xs);color:var(--acento-tx);">
      🔵 Declarado por el conductor — contá lo que volvió de verdad. Si no volvió nada, dejá todo en 0: queda como FALTANTE (medido), no se cancela. Lo sano queda fuera de la venta hasta que la nota crédito se apruebe en Siesa.
    </div>
    ` : `
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:10px;">Cuenta cuánto trajo el conductor de cada línea — deja en 0 lo que no se devolvió</div>
    `}

    <div style="background:var(--bg-s);border-radius:10px;padding:12px;margin-bottom:12px;">
      <div style="font-size:var(--fs-xs);color:var(--tx3);text-align:center;margin-bottom:10px;">Escanea cada unidad devuelta — suma 1 a la línea, hasta el tope facturado</div>
      ${OPERARIO && OPERARIO.puede_usar_camara ? `
      <button onclick="abrirCamara('lector-qr-dev','camara-box-dev', procesarScanDevolucion, this)"
        style="width:100%;padding:13px;font-size:var(--fs-md);background:#fff;color:#000;border:2px solid var(--brd);border-radius:10px;cursor:pointer;margin-bottom:8px;">
        📷 Escanear con cámara
      </button>
      <div id="camara-box-dev" style="display:none;margin-bottom:8px;">
        <div id="lector-qr-dev" style="border-radius:10px;overflow:hidden;"></div>
        <button onclick="cerrarCamara('camara-box-dev')" style="width:100%;padding:9px;margin-top:6px;font-size:var(--fs-sm);background:var(--bg-s2);color:var(--tx);border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
      </div>` : ''}
      <div style="display:flex;gap:8px;">
        <input id="dev-codigo-manual" type="text" placeholder="O escribe / pega el código aquí"
          style="flex:1;padding:10px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);"
          onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ procesarScanDevolucion(v); this.value=''; } }"
          autocomplete="off" autocorrect="off" spellcheck="false">
        <button onclick="const v=document.getElementById('dev-codigo-manual').value.trim();if(v){procesarScanDevolucion(v);document.getElementById('dev-codigo-manual').value='';}"
          style="padding:10px 14px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:var(--fs-lg);cursor:pointer;">↵</button>
      </div>
    </div>

    ${filas}

    <button onclick="confirmarDevolucionCliente()"
      style="width:100%;margin-top:8px;padding:16px;background:#15803d;color:#fff;border:none;border-radius:10px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">
      ✓ Confirmar devolución
    </button>
    <div id="estado-confirmar-dev" style="margin-top:10px;font-size:var(--fs-xs);color:var(--tx3);text-align:center;"></div>`;
}

/**
 * Encadena crear + confirmar la devolución con las cantidades contadas — un
 * solo clic para el recepcionista, aunque el backend lo modele en dos pasos.
 */
async function confirmarDevolucionCliente() {
  const datos = DEVOLUCION_ACTUAL;
  if (!datos) return;
  const estado = document.getElementById('estado-confirmar-dev');

  const lineas = recLineasContadas(datos);
  if (lineas.some(l => l.cantidad_averiada > l.cantidad_devuelta)) {
    alerta('Las averiadas no pueden ser más que las que volvieron', 'advertencia');
    return;
  }

  const esPendienteDeRuta = !!datos.id;
  let devolucionId = datos.id;

  if (esPendienteDeRuta && datos.problema_factura) {
    alerta(datos.problema_factura, 'error');
    return;
  }
  if (esPendienteDeRuta && !lineas.some(l => l.cantidad_devuelta > 0)) {
    if (!(await _modalConfirmar('No contó nada: se registra FALTANTE TOTAL (el conductor dijo que volvía y no llegó). No entra inventario ni sale nota crédito.',
        { titulo: '¿Registrar faltante total?', textoConfirmar: 'Sí, no llegó nada', peligro: true }))) return;
  }

  if (esPendienteDeRuta) {
    // Ya existe (Liquidación de ruta la armó) — no se crea de nuevo, solo se
    // confirma con lo que la recepcionista verificó/ajustó. Se manda TODO,
    // incluidas las líneas en 0 — así el backend sabe cuáles bajar, no solo
    // cuáles quedan igual.
    if (estado) { estado.textContent = '⏳ Ingresando stock y generando Nota Crédito...'; estado.style.color = 'var(--info-tx)'; }
    try {
      await post(`/api/devoluciones/${devolucionId}/confirmar`, { lineas });
    } catch (e) {
      if (estado) {
        estado.innerHTML = `Error al confirmar: ${e.status ? e.message : 'Error de conexión'}<br>
          <button onclick="reintentarConfirmarDevolucion(${devolucionId})"
            style="margin-top:8px;padding:8px 14px;background:#f59e0b;color:#000;border:none;border-radius:8px;cursor:pointer;">
            Reintentar confirmación
          </button>`;
        estado.style.color = 'var(--err-tx)';
      }
      return;
    }
    vibrar(); flash();
    alerta(lineas.some(l => l.cantidad_devuelta > 0)
      ? '✓ Devolución contada — queda en la zona de devoluciones hasta que la NC se apruebe'
      : 'Registrado como FALTANTE TOTAL', 'exito');
    DEVOLUCION_ACTUAL = null;
    setTimeout(cargarDevoluciones, 800);
    return;
  }

  const lineasNuevas = lineas.filter(l => l.cantidad_devuelta > 0);
  if (!lineasNuevas.length) { alerta('Cuenta al menos una unidad devuelta', 'advertencia'); return; }

  const esTotal = (datos.lineas || []).every(l => {
    const encontrada = lineasNuevas.find(x => x.codigo_siesa === l.codigo_siesa);
    return !!encontrada && encontrada.cantidad_devuelta === l.cantidad_facturada;
  });

  if (estado) { estado.textContent = '⏳ Creando devolución...'; estado.style.color = 'var(--info-tx)'; }
  try {
    const rCrear = await post('/api/devoluciones/', {
      tarea_packing_id: datos.tarea_packing_id,
      tipo_docto_fe: datos.tipo_docto_fe,
      consec_fe: datos.consec_fe,
      almacen_id: datos.almacen_id,
      lineas: lineasNuevas,
      es_total: esTotal
    });
    devolucionId = rCrear.devolucion.id;
  } catch (e) {
    if (estado) {
      estado.textContent = 'Error: ' + (e.status ? e.message : 'Error de conexión');
      estado.style.color = 'var(--err-tx)';
    }
    return;
  }

  if (estado) { estado.textContent = '⏳ Ingresando stock y generando Nota Crédito...'; }
  try {
    await post(`/api/devoluciones/${devolucionId}/confirmar`, {});
  } catch (e) {
    if (estado) {
      estado.innerHTML = `Error al confirmar: ${e.status ? e.message : 'Error de conexión'}<br>
        <button onclick="reintentarConfirmarDevolucion(${devolucionId})"
          style="margin-top:8px;padding:8px 14px;background:#f59e0b;color:#000;border:none;border-radius:8px;cursor:pointer;">
          Reintentar confirmación
        </button>`;
      estado.style.color = 'var(--err-tx)';
    }
    return;
  }

  vibrar(); flash();
  alerta('✓ Devolución confirmada — stock ingresado, Nota Crédito en proceso', 'exito');
  DEVOLUCION_ACTUAL = null;
  setTimeout(cargarDevoluciones, 800);
}

/**
 * Lo contado en pantalla, línea por línea. `linea_id` va cuando existe: en un
 * producto de doble unidad dos líneas tienen el mismo producto.
 * @param {Object} datos - la devolución en pantalla (`DEVOLUCION_ACTUAL`)
 * @returns {Array}
 */
function recLineasContadas(datos) {
  return (datos.lineas || []).map((l, i) => {
    const inpCant = document.getElementById(`cant-dev-${i}`);
    const inpAv = document.getElementById(`averiadas-dev-${i}`);
    const devuelta = inpCant ? (parseFloat(inpCant.value) || 0) : 0;
    const averiadas = inpAv ? (parseFloat(inpAv.value) || 0) : 0;
    return {
      linea_id: l.id || null,
      producto_id: l.producto_id,
      codigo_siesa: l.codigo_siesa,
      cantidad_facturada: l.cantidad_facturada,
      cantidad_devuelta: devuelta,
      cantidad_averiada: averiadas,
      es_averiado: devuelta > 0 && averiadas >= devuelta,
      f470_id_unidad_medida: l.f470_id_unidad_medida,
      f150_id_bodega: l.f150_id_bodega,
      f470_rowid: l.f470_rowid,
    };
  });
}

/**
 * Reintenta confirmar una devolución que quedó ABIERTA por un fallo previo
 * (ej. Siesa no respondió) — nada se pierde, el registro sigue esperando.
 * @param {number} devolucionId - ID de la devolución a reintentar
 */
async function reintentarConfirmarDevolucion(devolucionId) {
  const estado = document.getElementById('estado-confirmar-dev');
  if (estado) { estado.textContent = '⏳ Reintentando...'; estado.style.color = 'var(--info-tx)'; }
  try {
    await post(`/api/devoluciones/${devolucionId}/confirmar`, {});
    vibrar(); flash();
    alerta('✓ Devolución confirmada — stock ingresado, Nota Crédito en proceso', 'exito');
    DEVOLUCION_ACTUAL = null;
    setTimeout(cargarDevoluciones, 800);
  } catch (e) {
    if (estado) {
      estado.textContent = 'Error: ' + (e.status ? e.message : 'Error de conexión');
      estado.style.color = 'var(--err-tx)';
    }
  }
}

/** Limpia la búsqueda activa y vuelve al buscador de devoluciones. */
function volverBusquedaDevolucion() {
  DEVOLUCION_ACTUAL = null;
  cargarDevoluciones();
}

/**
 * Dispatcher global de escaneo (picking.js::procesarScan) enruta aquí mientras
 * DEVOLUCION_ACTUAL esté activo (pantalla de conteo). Escanear el código de
 * barras/referencia de un producto suma 1 unidad a esa línea — tope en la
 * cantidad facturada, igual que el input manual.
 * @param {string} codigo - Código escaneado (barras, QR, o manual)
 */
async function procesarScanDevolucion(codigo) {
  const datos = DEVOLUCION_ACTUAL;
  if (!datos || !datos.lineas) return;
  const cod = (codigo || '').trim();
  const idx = datos.lineas.findIndex(l =>
    l.codigo_barras === cod || l.codigo_siesa === cod || l.producto_codigo === cod);
  if (idx === -1) { alerta('Código no corresponde a ninguna línea de este pedido', 'advertencia'); return; }

  const inp = document.getElementById(`cant-dev-${idx}`);
  if (!inp) return;
  const linea = datos.lineas[idx];
  const actual = parseFloat(inp.value) || 0;
  if (actual >= linea.cantidad_facturada) {
    alerta(`Ya está al tope facturado (${linea.cantidad_facturada}) para ${linea.producto_nombre}`, 'advertencia');
    return;
  }
  inp.value = actual + 1;
  vibrar(); flash();
}


// Compras — movido desde app.js 2026-07-21
// ═══════════════════════════════════════════════════════════════════════════════
// COMPRAS — la pantalla del comprador vive en `compras_bandeja.js` (2026-09-25).
// Acá quedan la entrada por rol y el dispatcher de ⚙️ Avanzado: modelos, las
// tablas técnicas del punto de pedido y del contenedor, acuerdos, bloqueos y la
// operación de bodega (reposición interna PICKING, recepciones con problema,
// cuarentena, rastro de recepciones).
// ═══════════════════════════════════════════════════════════════════════════════

let COMP_SUBTAB = 'modelos';
let COMP_VELOCITY_DATA = [];    // cache para filtros client-side

/** Rol `compras`: la MISMA pantalla del admin, montada en `#pantalla-compras`. */
function compIniciarPantalla() {
  cmpMontar('compras');
  cmpIniciar();
}

/** Admin: pestaña Compras. */
async function cargarCompras() {
  cmpMontar('admin');
  cmpIniciar();
}

// ── Sub-pestañas de ⚙️ Avanzado ────────────────────────────────────────────
/** @param {string} id - Sección de Avanzado a mostrar (palabra fija). */
function compSubtab(id) {
  const secs = ['modelos','nacional','armador','acuerdos','bloqueos','velocity','dock','cuarentena','audit'];
  if (!secs.includes(id)) id = 'modelos';
  COMP_SUBTAB = id;
  secs.forEach(s => {
    const el = document.getElementById('comp-sec-' + s);
    const tab = document.getElementById('comp-sub-' + s);
    if (el) el.style.display = s === id ? 'block' : 'none';
    if (tab) {
      tab.style.background = s === id ? 'var(--acento-bg)' : 'transparent';
      tab.style.color = s === id ? 'var(--acento-tx)' : 'var(--tx2)';
      tab.style.borderColor = s === id ? 'var(--acento-brd)' : 'var(--brd)';
      tab.style.fontWeight = s === id ? '700' : '500';
    }
  });
  if (id === 'velocity') compCargarVelocity('comp');
  else if (id === 'dock') compCargarDock('comp');
  else if (id === 'cuarentena') compCargarCuarentena('comp');
  else if (id === 'bloqueos') compCargarBloqueos();
  else if (id === 'acuerdos') compCargarAcuerdos();
  else if (id === 'armador') compCargarArmador();
  else if (id === 'nacional') compCargarNacional();
  else if (id === 'modelos') modelosCargar();
  // audit: búsqueda manual — no carga sola
}

// ── RESUMEN (KPIs header) ─────────────────────────────────────────────────
/** @param {string} prefix - DOM prefix for the purchasing summary panel to load. */
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

/** @param {string} prefix - DOM prefix for the velocity analytics panel to load. */
async function compCargarVelocity(prefix) {
  const target = document.getElementById('comp-lista-velocity');
  if (!target) return;
  const diasSel = document.getElementById('comp-vel-dias');
  const dias = diasSel ? diasSel.value : 30;
  target.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Cargando la reposición interna…</div>';
  try {
    const r = await get(`/api/compras/velocity?dias=${dias}&almacen_id=${ALMACEN_ID}`);
    COMP_VELOCITY_DATA = r.items || [];
    _compRenderVelocityList(target, COMP_VELOCITY_DATA, r);
  } catch (e) {
    target.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:20px;">No se pudo cargar la reposición interna</div>';
  }
}

/** @param {string} prefix - DOM prefix; apply ABC/text filters to the velocity list. */
function compFiltrarVelocity(prefix) {
  const abcSel = document.getElementById('comp-vel-abc');
  const alertaSel = document.getElementById('comp-vel-alertas');
  const abcFiltro = abcSel ? abcSel.value : '';
  const soloAlertas = alertaSel ? alertaSel.checked : false;
  let filtered = COMP_VELOCITY_DATA;
  if (abcFiltro) filtered = filtered.filter(i => i.abc === abcFiltro);
  if (soloAlertas) filtered = filtered.filter(i => i.alerta);
  const target = document.getElementById('comp-lista-velocity');
  if (target) _compRenderVelocityList(target, filtered, { total_items: filtered.length, alertas_a: filtered.filter(i => i.alerta).length });
}

/**
 * Render the velocity analytics product list.
 * @param {HTMLElement} el - Container element.
 * @param {Array<Object>} items - Velocity data items.
 * @param {Object} meta - Metadata (totals, averages).
 */
function _compRenderVelocityList(el, items, meta) {
  if (!items.length) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Nada salió de los huecos de PICKING de NB1 en este período</div>';
    return;
  }

  const alertas = meta.alertas_a || 0;
  let html = alertas > 0
    ? `<div style="background:var(--err-bg);border:1px solid var(--err-brd);border-radius:10px;padding:10px 14px;margin-bottom:12px;">
         <span style="color:var(--err-tx);font-weight:700;">⚠ ${esc(alertas)} producto${alertas === 1 ? '' : 's'} A con menos de 15 días en su hueco de PICKING</span>
         <span style="color:var(--err-tx);font-size:var(--fs-xs);"> — sale más de lo que se repone: reponer desde RESERVA (no es una alerta de compra)</span>
       </div>`
    : '';

  html += `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">${esc(items.length)} productos salieron de PICKING en NB1</div>`;

  html += '<div style="display:flex;flex-direction:column;gap:6px;">';
  for (const it of items) {
    const abcTinta = it.abc === 'A' ? 'var(--err-tx)' : it.abc === 'B' ? 'var(--warn-tx)' : it.abc === 'C' ? 'var(--info-tx)' : 'var(--tx3)';
    const abcFondo = it.abc === 'A' ? 'var(--err-bg)' : it.abc === 'B' ? 'var(--warn-bg)' : it.abc === 'C' ? 'var(--info-bg)' : 'var(--bg)';
    const alertaBg = it.alerta ? 'background:var(--err-bg);border-color:var(--err-brd);' : '';
    html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:10px 12px;${alertaBg}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
        <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(it.codigo)}</div>
        <span style="font-size:var(--fs-xs);font-weight:800;color:${abcTinta};background:${abcFondo};padding:2px 8px;border-radius:6px;">${esc(it.abc || 'sin clase')}</span>
      </div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(it.nombre)}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(70px,1fr));gap:4px;font-size:var(--fs-xs);">
        <div><span style="color:var(--tx3);">Sale por día</span><br><strong style="color:var(--tx);">${esc(it.picks_dia)} u</strong></div>
        <div><span style="color:var(--tx3);">Salió en el período</span><br><strong style="color:var(--tx);">${esc(it.picks_periodo)} u</strong></div>
        <div><span style="color:var(--tx3);">En el hueco</span><br><strong style="color:var(--tx);">${esc(it.stock_picking)} u</strong></div>
        <div><span style="color:var(--tx3);">Alcanza en el hueco</span><br><strong style="color:${it.dias_stock_estimado < 15 ? 'var(--err-tx)' : it.dias_stock_estimado < 30 ? 'var(--warn-tx)' : 'var(--tx)'};">${it.dias_stock_estimado >= 999 ? 'no sale' : esc(Math.floor(it.dias_stock_estimado)) + ' días'}</strong></div>
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

  target.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Cargando dock lock...</div>';

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
      html += `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-bottom:14px;">
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:var(--fs-xl);font-weight:800;color:var(--err-tx);">${esc(r.total_recepciones_con_problema || 0)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">Con problema</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:var(--fs-xl);font-weight:800;color:var(--warn-tx);">${esc(r.total_excesos || 0)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">Excesos</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:var(--fs-xl);font-weight:800;color:var(--info-tx);">${esc(r.total_faltantes || 0)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">Faltantes</div>
        </div>
      </div>`;
    }

    const recs = r.recepciones || [];
    if (!recs.length) {
      html += '<div style="text-align:center;padding:30px;color:var(--tx3);">Sin rechazos en este período</div>';
    } else {
      html += '<div style="display:flex;flex-direction:column;gap:8px;">';
      for (const rec of recs) {
        const fecha = rec.fecha_confirmacion ? new Date(rec.fecha_confirmacion).toLocaleDateString('es-CO') : '—';
        let itemsHtml = '';
        for (const it of rec.items_problema) {
          const difColor = it.tipo_problema === 'EXCESO' ? '#f59e0b' : '#3b82f6';
          const difSign = it.diferencia > 0 ? '+' : '';
          itemsHtml += `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-xs);">
            <span style="color:var(--tx);">${esc(it.producto_codigo)} — ${(it.producto_nombre||'').substring(0,30)}</span>
            <span style="color:${difColor};font-weight:700;">${difSign}${esc(it.diferencia)} (${esc(it.tipo_problema)})</span>
          </div>`;
        }
        html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div>
              <span style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">OC ${esc(rec.oc_siesa)}</span>
              <span style="font-size:var(--fs-xs);color:var(--tx3);margin-left:8px;">${esc(rec.codigo)}</span>
            </div>
            <span style="font-size:var(--fs-xs);color:var(--tx3);">${fecha}</span>
          </div>
          <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:8px;">${rec.proveedor_nombre || rec.proveedor_codigo || '—'}${rec.es_parcial ? ' · <span style="color:var(--warn-tx);">PARCIAL</span>' : ''}</div>
          <div style="background:var(--bg-s2);border-radius:8px;padding:6px 8px;">
            ${itemsHtml}
          </div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">${esc(rec.total_problemas)} item(s) con discrepancia</div>
        </div>`;
      }
      html += '</div>';
    }

    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:20px;">Error cargando dock lock</div>';
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

  target.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Cargando cuarentena...</div>';

  try {
    const r = await get(`/api/compras/cuarentena?almacen_id=${ALMACEN_ID}`);

    if (!isP2) {
      set('cuar-pendientes', r.pendientes || 0);
      set('cuar-productos', r.total_productos_en_averias || 0);
    }

    let html = '';

    if (isP2) {
      html += `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:14px;">
        <div style="background:var(--err-bg);border:1px solid var(--err-brd);border-radius:10px;padding:14px;text-align:center;">
          <div style="font-size:26px;font-weight:800;color:var(--err-tx);">${esc(r.pendientes || 0)}</div>
          <div style="font-size:var(--fs-xs);color:var(--err-tx);">Pendientes de gestión</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:14px;text-align:center;">
          <div style="font-size:26px;font-weight:800;color:var(--tx);">${esc(r.total_productos_en_averias || 0)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">Productos en zona averías</div>
        </div>
      </div>`;
    }

    // Stock en zona AVERÍAS
    const stock = r.stock_en_averias || [];
    if (stock.length) {
      html += `<div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);margin-bottom:8px;">Stock en zona AVERÍAS</div>`;
      html += '<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:16px;">';
      for (const s of stock) {
        html += `<div style="display:flex;justify-content:space-between;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:8px 12px;">
          <div style="font-size:var(--fs-xs);"><strong>${esc(s.producto_codigo)}</strong> — ${(s.producto_nombre||'').substring(0,35)}</div>
          <div style="font-size:var(--fs-sm);font-weight:800;color:var(--err-tx);">${esc(s.cantidad_averiada)} UND</div>
        </div>`;
      }
      html += '</div>';
    }

    // Tareas averiadas
    const devs = r.devoluciones_averiadas || [];
    if (devs.length) {
      html += `<div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);margin-bottom:8px;">Devoluciones averiadas (${esc(devs.length)})</div>`;
      html += '<div style="display:flex;flex-direction:column;gap:6px;">';
      for (const d of devs) {
        const estadoColor = d.estado === 'PENDIENTE' ? '#f59e0b' : d.estado === 'EN_PROCESO' ? '#3b82f6' : d.estado === 'COMPLETADO' ? '#22c55e' : '#555';
        const diasColor = d.dias_sin_gestion > 7 ? '#ef4444' : d.dias_sin_gestion > 3 ? '#f59e0b' : 'var(--tx3)';
        const siesa = d.siesa_triggered ? '<span style="color:var(--ok-tx);font-size:var(--fs-xs);">✓ Siesa</span>' : '<span style="color:var(--err-tx);font-size:var(--fs-xs);">✗ Siesa</span>';
        html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:10px 12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <span style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(d.codigo)}</span>
              <span style="font-size:var(--fs-xs);color:${estadoColor};margin-left:8px;font-weight:700;">${esc(d.estado)}</span>
              ${siesa}
            </div>
            <span style="font-size:var(--fs-xs);font-weight:700;color:${diasColor};">${esc(d.dias_sin_gestion)}d</span>
          </div>
          <div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:4px;">${esc(d.producto_codigo || '—')} — ${(d.producto_nombre||'').substring(0,35)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(d.cantidad)} UND${d.observaciones ? ' · ' + d.observaciones.substring(0,60) : ''}</div>
        </div>`;
      }
      html += '</div>';
    }

    if (!stock.length && !devs.length) {
      html += '<div style="text-align:center;padding:30px;color:var(--ok-tx);font-weight:700;">Sin averías pendientes</div>';
    }

    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:20px;">Error cargando cuarentena</div>';
  }
}


// ═══════════════════════════════════════════════════════════════════════════
// AUDIT TRAIL — búsqueda por OC/proveedor
// ═══════════════════════════════════════════════════════════════════════════

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

  target.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Buscando...</div>';

  try {
    const r = await get(`/api/compras/audit-trail?q=${encodeURIComponent(q)}&dias=${dias}&almacen_id=${ALMACEN_ID}`);
    const resultados = r.resultados || [];

    if (!resultados.length) {
      target.innerHTML = `<div style="text-align:center;padding:30px;color:var(--tx3);">Sin resultados para "${q}"</div>`;
      return;
    }

    let html = `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">${esc(resultados.length)} recepción(es) encontradas</div>`;
    html += '<div style="display:flex;flex-direction:column;gap:8px;">';

    for (const rec of resultados) {
      const fecha = rec.fecha_confirmacion ? new Date(rec.fecha_confirmacion).toLocaleDateString('es-CO') : rec.fecha_creacion ? new Date(rec.fecha_creacion).toLocaleDateString('es-CO') : '—';
      const estadoColor = rec.estado === 'CONFIRMADA' ? '#22c55e' : rec.estado === 'EN_PROCESO' ? '#3b82f6' : rec.estado === 'CANCELADA' ? '#ef4444' : '#f59e0b';
      const siesa = rec.siesa_triggered ? '<span style="color:var(--ok-tx);font-size:var(--fs-xs);">✓ Siesa</span>' : '<span style="color:var(--err-tx);font-size:var(--fs-xs);">✗ Siesa</span>';

      let itemsHtml = '';
      for (const it of (rec.items || [])) {
        const difColor = it.es_exceso ? '#f59e0b' : it.es_faltante ? '#3b82f6' : '#22c55e';
        const difText = it.diferencia !== 0 ? ` (${it.diferencia > 0 ? '+' : ''}${esc(it.diferencia)})` : '';
        itemsHtml += `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-xs);">
          <span style="color:var(--tx);">${esc(it.producto_codigo)}${it.tipo === 'BONIFICACION' ? ' <span style="color:var(--lila-tx);">BONIF</span>' : ''}</span>
          <span>OC: ${esc(it.cantidad_ordenada)} → Rec: <strong style="color:${difColor};">${esc(it.cantidad_recibida)}${difText}</strong></span>
        </div>`;
      }

      html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <div>
            <span style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">OC ${esc(rec.oc_siesa)}</span>
            <span style="font-size:var(--fs-xs);color:${estadoColor};margin-left:8px;font-weight:700;">${esc(rec.estado)}</span>
            ${siesa}
          </div>
          <span style="font-size:var(--fs-xs);color:var(--tx3);">${fecha}</span>
        </div>
        <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:2px;">${rec.proveedor_nombre || rec.proveedor_codigo || '—'}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:6px;">
          Recepcionista: <strong>${esc(rec.recepcionista || '—')}</strong>
          · Remisión: ${esc(rec.remision || '—')}
          · ${esc(rec.codigo)}
          ${rec.es_parcial ? ' · <span style="color:var(--warn-tx);">PARCIAL</span>' : ''}
        </div>
        <div style="background:var(--bg-s2);border-radius:8px;padding:6px 8px;max-height:200px;overflow-y:auto;">
          ${itemsHtml}
        </div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">${esc(rec.total_items)} item(s)</div>
      </div>`;
    }
    html += '</div>';
    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:20px;">Error en búsqueda</div>';
  }
}


// ══════════════════════════════════════════════════════════════════════════════
// BLOQUEOS DE RECOMPRA — el sistema que dice NO
// ══════════════════════════════════════════════════════════════════════════════

// Sin costo conocido el servidor manda null: se dice, no se pinta $0.
const _liqFmtComp = v => (v == null ? 'sin costo' : '$' + Number(v).toLocaleString('es-CO'));

async function compCargarBloqueos() {
  const lista = document.getElementById('comp-bloqueos-lista');
  const resumen = document.getElementById('comp-bloqueos-resumen');
  if (!lista) return;
  lista.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Cargando...</div>';
  try {
    const d = await get('/api/compras/bloqueados');
    const items = d.items || [];

    if (resumen) {
      resumen.innerHTML = `
        <div style="background:var(--err-bg);border:1px solid var(--err-brd);border-radius:12px;padding:16px;margin-bottom:16px;text-align:center;">
          <div style="font-size:var(--fs-xs);color:var(--err-tx);text-transform:uppercase;font-weight:700;margin-bottom:4px;">Capital inmovilizado en cadáveres</div>
          <div style="font-size:var(--fs-2xl);font-weight:800;color:var(--err-tx);">${_liqFmtComp(d.total_inmovilizado)}</div>
          <div style="font-size:var(--fs-xs);color:var(--err-tx);margin-top:4px;">${esc(d.total_skus)} SKU${d.total_skus !== 1 ? 's' : ''} bloqueado${d.total_skus !== 1 ? 's' : ''}</div>
        </div>`;
    }

    if (!items.length) {
      lista.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Sin productos bloqueados — usa "Generar lista inicial" para poblar</div>';
      return;
    }

    let html = '';
    items.forEach((it, idx) => {
      html += `
        <div style="background:var(--bg-s);border:1px solid ${idx < 3 ? 'var(--err-brd)' : 'var(--brd)'};border-radius:10px;padding:12px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
              <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(it.codigo)}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(it.nombre)}</div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:var(--fs-sm);font-weight:800;color:var(--err-tx);">${_liqFmtComp(it.capital_inmovilizado)}</div>
              <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(it.stock)} UND × ${_liqFmtComp(it.costo_unitario)}</div>
            </div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;">
            <span style="font-size:var(--fs-xs);padding:2px 8px;border-radius:20px;background:#7f1d1d33;color:var(--err-tx);font-weight:700;">${esc(it.motivo)}</span>
            <button onclick="compDesbloquear(${esc(it.bloqueo_id)},'${esc(it.codigo)}')"
              style="padding:4px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx3);font-size:var(--fs-xs);cursor:pointer;">
              Solicitar desbloqueo
            </button>
          </div>
        </div>`;
    });
    lista.innerHTML = html;

    // Cargar fugas
    compCargarFugas();
  } catch (e) {
    lista.innerHTML = `<div style="color:var(--err-tx);text-align:center;padding:20px;">${esc(e.message || 'Error cargando bloqueos')}</div>`;
  }
}

async function compPoblarBloqueos() {
  if (!await _confirmarModal('Generar bloqueos', 'Se bloquearán todos los SKUs con velocity=0 en 12 meses y stock existente.', 'Generar', 'Cancelar')) return;
  try {
    const d = await postConReintento('/api/compras/bloqueados/poblar', {});
    if (d.no_se_bloqueo_por) { alerta(d.nota, 'advertencia'); return; }
    alerta(`${d.bloqueados_nuevos} SKU(s) bloqueado(s) — ${d.capital_es_cota_inferior ? 'al menos ' : ''}${_liqFmtComp(d.total_capital_inmovilizado)} inmovilizado`, 'exito');
    compCargarBloqueos();
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

async function compDesbloquear(bloqueoId, codigo) {
  const motivo = await _modalTexto('Desbloquear SKU', `¿Por qué desbloquear ${codigo}? (queda registrado)`);
  if (!motivo) return;
  const cantidad = await _modalCantidad('Cantidad autorizada', 'Cantidad máxima autorizada a comprar:', { min: 1 });
  if (cantidad === null) return;
  const dias = await _modalCantidad('Vigencia', 'Vigencia del desbloqueo en días:', { min: 1, valorInicial: 30 });
  if (dias === null) return;

  try {
    await postConReintento(`/api/compras/bloqueados/${bloqueoId}/desbloquear`,
      { motivo: motivo.trim(), cantidad_autorizada: Number(cantidad), vigencia_dias: dias });
    alerta(`${codigo} desbloqueado — máx ${cantidad} UND, vigencia ${dias} días`, 'exito');
    compCargarBloqueos();
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

async function compCargarFugas() {
  const el = document.getElementById('comp-fugas-lista');
  if (!el) return;
  try {
    const d = await get('/api/compras/bloqueados/fugas');
    const fugas = d.fugas || [];
    if (!fugas.length) {
      el.innerHTML = '<div style="text-align:center;padding:20px;color:var(--ok-tx);font-size:var(--fs-xs);">Sin fugas — todas las compras pasan por el sistema ✓</div>';
      return;
    }
    el.innerHTML = fugas.map(f => `
      <div style="display:flex;justify-content:space-between;padding:8px;background:#78350f22;border:1px solid var(--warn-brd);border-radius:8px;margin-bottom:6px;font-size:var(--fs-xs);">
        <div>
          <strong style="color:var(--warn-tx);">${esc(f.producto_codigo)}</strong> — ${esc(f.producto_nombre || '—')}
          <div style="font-size:var(--fs-xs);color:var(--tx3);">OC: ${esc(f.oc_siesa || '—')} · Prov: ${esc(f.proveedor || '—')} · ${esc(f.cantidad_recibida)} UND</div>
        </div>
        <span style="color:var(--tx3);font-size:var(--fs-xs);white-space:nowrap;">${new Date(f.fecha).toLocaleDateString('es-CO')}</span>
      </div>`).join('');
  } catch (e) {}
}
