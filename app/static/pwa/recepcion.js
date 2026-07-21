// ══════════════════════════════════════════════════════════════════
// RECEPCIONISTA — OCs, escaneo ciego, traslados entrantes, devoluciones
// Dependencias globales (de app.js): get(), post(), put(), alerta(), flash(),
//   vibrar(), beepOk(), beepError(), beepDone(), pantalla(),
//   abrirCamara(), cerrarCamara(), TOKEN, OPERARIO, ALMACEN_ID, API,
//   RECEPCION_ACTUAL, DEVOLUCION_ACTUAL
// Dependencias cross-module (de packing.js): imprimirEtiquetaLPN()
// ══════════════════════════════════════════════════════════════════

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
    el.innerHTML = '<div style="text-align:center;padding:40px;color:#666;">Cargando...</div>';
  }
  try {
    const [siesa, db] = await Promise.all([
      get('/api/siesa/ordenes-compra').catch(() => ({ ordenes: [] })),
      get('/api/recepcion/?estado=EN_PROCESO').catch(() => ({ recepciones: [] }))
    ]);
    SIESA_OCS = siesa.ordenes || [];
    // Guard post-await: el operario pudo haber entrado a escaneo mientras las APIs respondían
    if (RECEPCION_ACTUAL) return;
    renderListaRecepciones(siesa, db.recepciones || []);
  } catch (e) {
    if (!silencioso) el.innerHTML = '<div style="color:#ef4444;">Error cargando</div>';
  }
}


// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Lista de OCs y recepciones en proceso
// ─────────────────────────────────────────────────────────────

/**
 * Renderiza el HTML de la lista de OCs pendientes y recepciones en proceso.
 * @param {Object} siesa - Respuesta de la API de OCs (contiene .ordenes y .simulado)
 * @param {Array<Object>} dbRecs - Recepciones en proceso desde la DB local
 */
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
      const wmsEstado = oc.recepcion_wms_estado;

      if (wmsEstado === 'CONFIRMADA') {
        return `
          <div class="rec-card" style="opacity:0.6;">
            <div class="rec-titulo">OC: ${oc.numero_oc}</div>
            <div class="rec-sub">${oc.proveedor || 'Sin proveedor'} · ${oc.items.length} productos · ${totalUds} uds</div>
            <div style="margin-top:10px;padding:10px;background:#0d1a0d;border-radius:8px;font-size:14px;font-weight:700;color:#4ade80;text-align:center;">
              ✓ Recepcionada en WMS — pendiente actualización en Siesa
            </div>
          </div>`;
      }

      if (wmsEstado === 'EN_PROCESO') {
        return `
          <div class="rec-card">
            <div class="rec-titulo">OC: ${oc.numero_oc}</div>
            <div class="rec-sub">${oc.proveedor || 'Sin proveedor'} · ${oc.items.length} productos · ${totalUds} uds</div>
            <button onclick="crearRecepcionDesdeSiesa(${i})"
              style="width:100%;margin-top:12px;padding:14px;font-size:17px;font-weight:700;background:#1d4ed8;color:#fff;border:none;border-radius:10px;cursor:pointer;">
              Continuar recepción
            </button>
          </div>`;
      }

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
      <button onclick="_refreshBtn(event, cargarRecepciones)" style="margin-top:20px;padding:12px 24px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">Actualizar</button>
    </div>`;
  }

  el.innerHTML = html;
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
  if (el) el.innerHTML = '<div style="text-align:center;padding:60px;color:#666;">Iniciando recepción...</div>';

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
        <div style="font-size:12px;color:#666;text-align:center;margin-bottom:10px;">Escanea unidad, caja o paca — el sistema calcula las unidades</div>
        <button onclick="abrirCamara('lector-qr-rec','camara-box-rec', cod => { cerrarCamara('camara-box-rec'); procesarScanRecepcion(cod); })"
          style="width:100%;padding:13px;font-size:16px;background:#fff;color:#000;border:2px solid #000;border-radius:10px;cursor:pointer;margin-bottom:8px;">
          📷 Escanear con cámara
        </button>
        <div id="camara-box-rec" style="display:none;margin-bottom:8px;">
          <div id="lector-qr-rec" style="border-radius:10px;overflow:hidden;"></div>
          <button onclick="cerrarCamara('camara-box-rec')" style="width:100%;padding:9px;margin-top:6px;font-size:14px;background:#333;color:#fff;border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
        </div>
        <div style="display:flex;gap:8px;margin-bottom:8px;">
          <input id="rec-codigo-manual" type="text" placeholder="O escribe / pega el código aquí"
            style="flex:1;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ procesarScanRecepcion(v); this.value=''; } }">
          <button onclick="const v=document.getElementById('rec-codigo-manual').value.trim();if(v){procesarScanRecepcion(v);document.getElementById('rec-codigo-manual').value='';}"
            style="padding:10px 14px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
        </div>
        <button onclick="abrirBusquedaManualRecepcion()"
          style="width:100%;padding:10px;font-size:14px;background:#1a1a1a;color:#9ca3af;border:1px solid #333;border-radius:8px;cursor:pointer;">
          📦 Sin código — buscar producto manualmente
        </button>
      </div>

      <div id="items-rec-list" style="margin-bottom:14px;">
        ${renderItemsRecepcion(rec.items)}
      </div>

      <button id="btn-confirmar-rec" onclick="confirmarRecepcionActiva()" ${btnActivo ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:20px;font-weight:700;background:${btnActivo ? btnColor : '#222'};color:#fff;border:none;border-radius:14px;cursor:${btnActivo ? 'pointer' : 'default'};margin-bottom:10px;">
        ${btnTexto}
      </button>

      <button onclick="modalObsequio()"
        style="width:100%;padding:13px;font-size:15px;font-weight:600;background:#1a1a2e;color:#a78bfa;border:1px solid #4c1d95;border-radius:10px;cursor:pointer;margin-bottom:8px;">
        🎁 Registrar Obsequio / Bonificación
      </button>

      <button onclick="volverListaRecepciones()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
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
        <div id="item-rec-${it.producto_id}"
          style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:12px;padding:14px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div style="min-width:0;flex:1;">
              <div style="display:flex;align-items:center;gap:8px;">
                <span style="background:#4c1d95;color:#a78bfa;font-size:10px;font-weight:700;padding:2px 7px;border-radius:20px;">🎁 BONO</span>
                <div style="font-size:14px;font-weight:600;color:#a78bfa;">${it.producto_nombre || it.producto_codigo}</div>
              </div>
              <div style="font-size:11px;color:#555;margin-top:2px;">${it.producto_codigo}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;padding-left:8px;">
              <div style="font-size:28px;font-weight:900;color:#a78bfa;">${it.cantidad_recibida}</div>
              <div style="font-size:10px;color:#6b7280;">und recibidas</div>
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

    const contadorDerecha = modoEmpaque ? `
      <div style="text-align:right;flex-shrink:0;padding-left:8px;">
        <div style="font-size:42px;font-weight:900;line-height:1;color:${completo ? '#4ade80' : '#fff'};">${empaques}</div>
        <div style="font-size:13px;font-weight:700;color:${completo ? '#4ade80' : '#facc15'};">${it.cantidad_recibida}/${it.cantidad_ordenada} und</div>
        <div style="font-size:10px;color:#6b7280;">${unidadEmpaque} · ×${factor}</div>
      </div>` : `
      <div style="text-align:right;flex-shrink:0;padding-left:8px;">
        <div style="font-size:28px;font-weight:900;color:${completo ? '#4ade80' : '#fff'};">${it.cantidad_recibida}/${it.cantidad_ordenada}</div>
      </div>`;

    return `
      <div id="item-rec-${it.producto_id}"
        style="background:${completo ? '#0d1a0d' : '#111'};border:1px solid ${completo ? '#166534' : '#222'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:14px;font-weight:600;color:${completo ? '#4ade80' : '#fff'};">${it.producto_nombre || it.producto_codigo}</div>
            <div style="font-size:11px;color:#555;">${it.producto_codigo}</div>
            ${it.destino === 'CROSS_DOCK' ? '<div style="font-size:11px;color:#60a5fa;margin-top:4px;">↔ CROSS-DOCK</div>' : ''}
          </div>
          ${contadorDerecha}
        </div>
        <div style="height:5px;background:#222;border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#16a34a' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.3s;"></div>
        </div>
      </div>`;
  }).join('');
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
      const esEmp = prod.es_empaque || false;
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
  let r;
  try {
    r = await post('/api/recepcion/' + RECEPCION_ACTUAL.id + '/escanear', {
      producto_id: productoId,
      cantidad: cantidad,
      es_empaque: esEmpaque,
      es_bonificacion: esBonificacion
    });
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
    beepError();
    alerta(e.message, 'error');
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
  if (btn && todoCompleto && itemsOC.length > 0) {
    btn.disabled = false;
    btn.style.background = '#16a34a';
    btn.style.cursor = 'pointer';
    alerta('Todo escaneado — confirma la recepción', 'exito');
  }
}

/** Muestra modal preguntando si hay obsequios/bonificaciones del proveedor. */
function modalObsequio() {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.85);z-index:9000;display:flex;align-items:center;justify-content:center;padding:24px;';
  overlay.innerHTML = `
    <div style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:16px;padding:28px;max-width:360px;width:100%;text-align:center;">
      <div style="font-size:40px;margin-bottom:12px;">🎁</div>
      <div style="font-size:18px;font-weight:800;color:#a78bfa;margin-bottom:10px;">¿Hay obsequios o bonificaciones?</div>
      <div style="font-size:14px;color:#9ca3af;margin-bottom:24px;">¿El proveedor envió productos adicionales que <strong style="color:#fff;">no están en la OC</strong>?</div>
      <button id="btn-bono-si" style="width:100%;padding:15px;font-size:16px;font-weight:700;background:#4c1d95;color:#fff;border:none;border-radius:10px;cursor:pointer;margin-bottom:10px;">
        Sí — escanear obsequio
      </button>
      <button id="btn-bono-no" style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
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
    <div style="background:#0d0d1a;border:1px solid #4c1d95;border-radius:16px;padding:24px;max-width:380px;width:100%;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
        <div style="font-size:16px;font-weight:800;color:#a78bfa;">🎁 Escanear Obsequio / Bonificación</div>
        <button onclick="this.closest('div[style*=fixed]').remove()" style="background:none;border:none;color:#555;font-size:22px;cursor:pointer;">✕</button>
      </div>
      <div style="font-size:12px;color:#6b7280;margin-bottom:14px;">Escanea el producto que el proveedor envió de más — entrará a inventario a $0.</div>
      <div id="camara-box-bono" style="display:none;margin-bottom:8px;">
        <div id="lector-qr-bono" style="border-radius:10px;overflow:hidden;"></div>
        <button onclick="cerrarCamara('camara-box-bono')" style="width:100%;padding:9px;margin-top:6px;font-size:14px;background:#333;color:#fff;border:none;border-radius:8px;cursor:pointer;">Cerrar cámara</button>
      </div>
      <button onclick="abrirCamara('lector-qr-bono','camara-box-bono', cod => { cerrarCamara('camara-box-bono'); _escanearBono(cod, this.closest('div[style*=fixed]')); })"
        style="width:100%;padding:13px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">
        📷 Escanear con cámara
      </button>
      <div style="display:flex;gap:8px;margin-bottom:8px;">
        <input id="bono-codigo-manual" type="text" placeholder="O escribe / pega el código"
          style="flex:1;padding:10px;background:#0a0a0a;border:1px solid #4c1d95;border-radius:8px;color:#fff;font-size:14px;"
          onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ _escanearBono(v, this.closest('div[style*=fixed]')); this.value=''; } }">
        <button onclick="const v=document.getElementById('bono-codigo-manual').value.trim();if(v){_escanearBono(v,this.closest('div[style*=fixed]'));document.getElementById('bono-codigo-manual').value='';}"
          style="padding:10px 14px;background:#4c1d95;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
      </div>
      <button onclick="abrirBusquedaManualBono(this.closest('div[style*=fixed]'))"
        style="width:100%;padding:10px;font-size:14px;background:#1a1a1a;color:#9ca3af;border:1px solid #333;border-radius:8px;cursor:pointer;">
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
      <div style="background:#111;border:1px solid #333;border-radius:16px;padding:28px;max-width:340px;width:100%;text-align:center;">
        <div style="font-size:17px;font-weight:800;color:#fff;margin-bottom:12px;">${titulo}</div>
        <div style="font-size:14px;color:#9ca3af;margin-bottom:24px;">${cuerpoHtml}</div>
        <button id="_cm-si" style="width:100%;padding:14px;font-size:15px;font-weight:700;background:#4c1d95;color:#fff;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">${txtSi}</button>
        <button id="_cm-no" style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">${txtNo}</button>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#_cm-si').onclick = () => { overlay.remove(); resolve(true); };
    overlay.querySelector('#_cm-no').onclick = () => { overlay.remove(); resolve(false); };
  });
}

/**
 * Muestra modal para resolver ambiguedad cuando un codigo corresponde a multiples empaques.
 * @param {string} codigo - Codigo de barras ambiguo
 * @param {Array<Object>} ambiguos - Lista de empaques posibles con producto_id, factor_conversion, unidad_medida
 */
function _modalAmbiguedadRecepcion(codigo, ambiguos) {
  // El mismo código de barras corresponde a múltiples niveles de empaque
  // El operario debe decir qué está escaneando
  const opciones = ambiguos.map((e, i) => `
    <button onclick="_elegirEmpaque('${codigo}',${e.producto_id},${e.factor_conversion},'${e.unidad_medida}',this.closest('.modal-rec'))"
      style="width:100%;padding:14px;margin-bottom:8px;background:#1a1a1a;border:1px solid #333;
             color:#fff;border-radius:10px;cursor:pointer;font-size:15px;text-align:left;">
      <span style="font-size:22px;font-weight:900;">${e.factor_conversion}</span>
      <span style="color:#9ca3af;margin-left:6px;">${e.unidad_medida}</span>
      <span style="color:#6b7280;font-size:12px;margin-left:8px;">(×${e.factor_conversion} und)</span>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-rec';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;display:flex;align-items:flex-end;padding:16px;';
  modal.innerHTML = `
    <div style="background:#111;border-radius:16px;padding:20px;width:100%;max-width:480px;margin:auto;">
      <div style="font-size:16px;font-weight:700;margin-bottom:6px;">⚠️ Código ambiguo</div>
      <div style="font-size:13px;color:#9ca3af;margin-bottom:16px;">${codigo} — ¿Qué estás escaneando?</div>
      ${opciones}
      <button onclick="this.closest('.modal-rec').remove()"
        style="width:100%;padding:12px;background:#0d0d0d;color:#6b7280;border:1px solid #222;border-radius:8px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
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
    <div style="background:#111;border-radius:16px;padding:20px;width:100%;max-width:480px;margin:auto;">
      <div style="font-size:16px;font-weight:700;margin-bottom:14px;">📦 Buscar producto manualmente</div>
      <input id="modal-buscar-input" type="text" placeholder="Nombre o código del producto..."
        style="width:100%;box-sizing:border-box;padding:12px;background:#0d0d0d;border:1px solid #444;border-radius:8px;color:#fff;font-size:15px;margin-bottom:10px;"
        oninput="_buscarProductoModal(this.value)">
      <div id="modal-buscar-resultados" style="max-height:280px;overflow-y:auto;"></div>
      <button onclick="this.closest('.modal-rec').remove()"
        style="width:100%;padding:12px;background:#0d0d0d;color:#6b7280;border:1px solid #222;border-radius:8px;cursor:pointer;margin-top:10px;">
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
    if (!productos.length) { el.innerHTML = '<div style="color:#6b7280;padding:10px;font-size:13px;">Sin resultados</div>'; return; }
    el.innerHTML = productos.map(p => `
      <button onclick="_seleccionarProductoManual(${p.id},'${(p.nombre||'').replace(/'/g,"\\'")}',this.closest('.modal-rec'))"
        style="width:100%;padding:12px;margin-bottom:6px;background:#1a1a1a;border:1px solid #333;color:#fff;border-radius:8px;cursor:pointer;text-align:left;">
        <div style="font-size:14px;font-weight:600;">${p.nombre}</div>
        <div style="font-size:11px;color:#6b7280;">${p.codigo}</div>
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
  const cant = prompt(`¿Cuántas unidades tiene esta paca de "${nombre}"?\n(Deja vacío si es 1 unidad suelta)`);
  if (cant === null) return; // canceló
  const cantidad = parseInt(cant) || 1;

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
      <div style="background:#111;border:1px solid #333;border-radius:16px;padding:28px;max-width:360px;width:100%;">
        <div style="font-size:17px;font-weight:800;color:#fff;margin-bottom:6px;">📄 Remisión del proveedor</div>
        <div style="font-size:13px;color:#9ca3af;margin-bottom:18px;">Ingresa el número de remisión o factura física que llegó con el camión. Siesa lo requiere para cerrar la entrada.</div>
        <input id="_rem-input" type="text" placeholder="Ej: 00123456"
          style="width:100%;box-sizing:border-box;padding:13px;background:#0d0d0d;border:1px solid #555;border-radius:10px;color:#fff;font-size:18px;font-weight:700;margin-bottom:16px;letter-spacing:1px;"
          onkeydown="if(event.key==='Enter') document.getElementById('_rem-ok').click()">
        <button id="_rem-ok"
          style="width:100%;padding:15px;font-size:16px;font-weight:700;background:#16a34a;color:#fff;border:none;border-radius:10px;cursor:pointer;margin-bottom:8px;">
          Confirmar recepción
        </button>
        <button id="_rem-cancel"
          style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
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

/**
 * Carga y renderiza la lista de traslados pendientes de recepcion (NB1).
 * @param {boolean} [silencioso=false] - true omite el spinner de carga inicial
 */
async function recepCargarTraslados(silencioso = false) {
  if (_REC_TRASLADO_ACTIVO) return;
  const el = document.getElementById('contenido-traslados-rec');
  if (!el) return;
  if (!silencioso) el.innerHTML = '<div style="text-align:center;padding:40px;color:#666;">Cargando...</div>';
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
        <div style="font-size:18px;font-weight:700;margin-top:10px;color:#4ade80;">Sin traslados pendientes</div>
        <button onclick="_refreshBtn(event, recepCargarTraslados)" style="margin-top:20px;padding:12px 24px;font-size:15px;background:#fff;color:#000;border:none;border-radius:10px;cursor:pointer;">Actualizar</button>
      </div>`;
      return;
    }
    el.innerHTML = _REC_TRASLADOS_PENDIENTES.map(s => {
      const totalEsp = (s.items || []).reduce((a, i) => a + (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0), 0);
      return `
      <div style="background:#0a1a0a;border:1px solid #166534;border-radius:12px;padding:14px;margin-bottom:10px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px;">
          <div style="font-size:15px;font-weight:800;">${s.codigo}</div>
          <div style="font-size:11px;color:#4ade80;font-weight:600;">Desde ${s.bodega_origen_siesa || '—'}</div>
        </div>
        <div style="font-size:12px;color:#4ade80;margin-bottom:8px;">📦 ${s.total_items} ítem${s.total_items !== 1 ? 's' : ''} · ${totalEsp} und esperadas</div>
        ${(s.items || []).slice(0, 3).map(i => `
          <div style="font-size:11px;color:#aaa;padding:2px 0;">
            ${i.producto_nombre || i.producto_codigo} · ${i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0} und
          </div>`).join('')}
        ${(s.items || []).length > 3 ? `<div style="font-size:11px;color:#555;padding:2px 0;">+ ${s.items.length - 3} más...</div>` : ''}
        <button onclick="recepAbrirConteoTraslado(${s.id})"
          style="width:100%;padding:13px;margin-top:12px;background:#1E8395;color:#fff;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;">
          📋 Contar productos
        </button>
      </div>`;
    }).join('');
  } catch (e) {
    if (!silencioso) el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando traslados</div>';
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
  _recepRenderPickingTraslado();
  setTimeout(() => { const inp = document.getElementById('rec-tras-scan-input'); if (inp) inp.focus(); }, 150);
}

/** Limpia el traslado activo y vuelve a la lista de traslados pendientes. */
function recepVolverListaTraslados() {
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
  const btnColor  = todoContado ? '#16a34a' : '#b45309';
  const btnTexto  = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial';

  el.innerHTML = `
    <div style="padding:0;">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
        <button onclick="recepVolverListaTraslados()"
          style="background:#222;border:1px solid #333;color:#fff;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:14px;flex-shrink:0;">
          ← Volver
        </button>
        <div style="min-width:0;">
          <div style="font-size:16px;font-weight:800;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${s.codigo}</div>
          <div style="font-size:12px;color:#666;">Desde ${s.bodega_origen_siesa || '—'} → ${s.bodega_destino_siesa || '—'}</div>
        </div>
      </div>

      <div style="background:#111;border-radius:10px;padding:12px;margin-bottom:14px;">
        <div style="font-size:12px;color:#666;text-align:center;margin-bottom:8px;">Escanea el código o usá los botones +/−</div>
        <div style="display:flex;gap:8px;">
          <input id="rec-tras-scan-input" type="text" placeholder="Escanea o escribe el código..."
            style="flex:1;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;"
            onkeydown="if(event.key==='Enter'){ const v=this.value.trim(); if(v){ recepScanTraslado(v); this.value=''; } }"
            autocomplete="off" autocorrect="off" spellcheck="false">
          <button onclick="const v=document.getElementById('rec-tras-scan-input').value.trim();if(v){recepScanTraslado(v);document.getElementById('rec-tras-scan-input').value='';}"
            style="padding:10px 14px;background:#1E8395;color:#fff;border:none;border-radius:8px;font-size:18px;cursor:pointer;">↵</button>
        </div>
      </div>

      <div id="rec-tras-items" style="margin-bottom:14px;">
        ${_recepRenderItemsTraslado(items)}
      </div>

      <button id="btn-confirmar-rec-traslado" onclick="recepConfirmarTraslado()"
        ${algoContado || todoContado ? '' : 'disabled'}
        style="width:100%;padding:18px;font-size:18px;font-weight:700;background:${algoContado || todoContado ? btnColor : '#222'};color:#fff;border:none;border-radius:14px;cursor:${algoContado || todoContado ? 'pointer' : 'default'};margin-bottom:10px;">
        ${algoContado || todoContado ? btnTexto : 'Contá al menos un ítem para continuar'}
      </button>

      <button onclick="recepVolverListaTraslados()"
        style="width:100%;padding:12px;font-size:14px;background:#1a1a1a;color:#555;border:1px solid #222;border-radius:10px;cursor:pointer;">
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
      <div id="rec-tras-item-${i.producto_id}"
        style="background:${completo ? '#0d1a0d' : '#111'};border:1px solid ${completo ? '#166534' : '#222'};border-radius:12px;padding:14px;margin-bottom:8px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div style="min-width:0;flex:1;">
            <div style="font-size:14px;font-weight:600;color:${completo ? '#4ade80' : '#fff'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${i.producto_nombre || i.producto_codigo}</div>
            <div style="font-size:11px;color:#555;margin-top:2px;">${i.producto_codigo_siesa || i.producto_codigo}</div>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;padding-left:8px;">
            <button onclick="recepContarItem(${i.producto_id}, -1)"
              style="width:34px;height:34px;background:#222;border:1px solid #333;color:#fff;border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">−</button>
            <div style="text-align:center;min-width:54px;">
              <div style="font-size:26px;font-weight:900;line-height:1;color:${completo ? '#4ade80' : '#fff'};">${contado}</div>
              <div style="font-size:10px;color:#6b7280;">/ ${esperado}</div>
            </div>
            <button onclick="recepContarItem(${i.producto_id}, 1)"
              style="width:34px;height:34px;background:#1E8395;border:none;color:#fff;border-radius:8px;font-size:20px;font-weight:700;cursor:pointer;line-height:1;">+</button>
          </div>
        </div>
        <div style="height:5px;background:#222;border-radius:3px;margin-top:8px;">
          <div style="height:100%;background:${completo ? '#16a34a' : '#2563eb'};border-radius:3px;width:${pct}%;transition:width 0.2s;"></div>
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
  const itemsEl = document.getElementById('rec-tras-items');
  if (itemsEl) itemsEl.innerHTML = _recepRenderItemsTraslado(_REC_TRASLADO_ACTIVO.items || []);
  const items = _REC_TRASLADO_ACTIVO.items || [];
  const todoContado = items.every(i => (_REC_CONTEOS[i.producto_id] || 0) >= (i.cantidad_enviada || i.cantidad_aprobada || i.cantidad_solicitada || 0));
  const algoContado = items.some(i => (_REC_CONTEOS[i.producto_id] || 0) > 0);
  const btn = document.getElementById('btn-confirmar-rec-traslado');
  if (btn) {
    btn.disabled = !(algoContado || todoContado);
    btn.style.background = !algoContado && !todoContado ? '#222' : (todoContado ? '#16a34a' : '#b45309');
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
    const r = await fetch(API + `/api/traslados/${s.id}/recibir`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items_recibidos: items.map(i => ({
          id: i.id,
          cantidad_recibida: _REC_CONTEOS[i.producto_id] || 0
        }))
      })
    });
    const d = await r.json();
    if (r.ok) {
      beepDone();
      alerta('✓ Recepción confirmada — ETS generado en Siesa', 'exito');
      _REC_TRASLADO_ACTIVO = null;
      _REC_CONTEOS = {};
      setTimeout(recepCargarTraslados, 1200);
    } else {
      alerta(d.error || 'Error al confirmar', 'error');
      if (btn) { btn.textContent = todoContado ? '✓ Confirmar recepción' : '⚠ Confirmar recepción parcial'; btn.disabled = false; }
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
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
  if (!tabOcs) return;

  const activo  = 'border-bottom:2px solid #1E8395;color:#1E8395;font-weight:600;';
  const inactivo = 'border-bottom:2px solid transparent;color:#415A70;';
  if (tabOcs)  tabOcs.style.cssText  = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;${tab==='ocs' ? activo : inactivo}`;
  if (tabTras) tabTras.style.cssText = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;position:relative;${tab==='traslados' ? activo : inactivo}`;
  if (tabDev)  tabDev.style.cssText  = `flex:1;padding:11px;font-size:13px;text-align:center;cursor:pointer;position:relative;${tab==='dev' ? activo : inactivo}`;
  // re-append badges (se pierden al resetear cssText)
  const badgeTras = document.getElementById('badge-traslados-rec');
  const badgeDev  = document.getElementById('badge-dev');
  if (badgeTras && tabTras) tabTras.appendChild(badgeTras);
  if (badgeDev  && tabDev)  tabDev.appendChild(badgeDev);

  if (contOcs)  contOcs.style.display  = tab === 'ocs'       ? 'block' : 'none';
  if (contTras) contTras.style.display  = tab === 'traslados' ? 'block' : 'none';
  if (contDev)  contDev.style.display   = tab === 'dev'       ? 'block' : 'none';

  if (tab === 'traslados' && !_REC_TRASLADO_ACTIVO) recepCargarTraslados();
  if (tab === 'dev') cargarDevoluciones();
}

// ─────────────────────────────────────────────────────────────
// RECEPCIONISTA — Lista de Devoluciones
// ─────────────────────────────────────────────────────────────

/**
 * Carga y renderiza la lista de devoluciones pendientes y bultos rechazados.
 * @param {boolean} [silencioso=false] - true omite el spinner de carga inicial
 */
async function cargarDevoluciones(silencioso = false) {
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

/**
 * Carga una tarea de devolucion y abre la pantalla de ubicacion.
 * @param {number} id - ID de la tarea de devolucion
 */
async function abrirDevolucion(id) {
  try {
    const d = await get('/api/devoluciones/?almacen_id=' + ALMACEN_ID);
    const tarea = (d.tareas || []).find(t => t.id === id);
    if (!tarea) { alerta('Tarea no encontrada', 'error'); return; }
    DEVOLUCION_ACTUAL = tarea;
    renderEscaneoDevolucion(tarea);
  } catch (e) { alerta('Error cargando tarea', 'error'); }
}

/**
 * Renderiza la pantalla de ubicacion de devolucion (buen estado o averia).
 * @param {Object} tarea - Tarea de devolucion con producto_nombre, cantidad_diferencia, etc.
 */
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

/** Confirma la ubicacion de la devolucion activa en bodega (buen estado). */
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

/**
 * Procesa un escaneo de ubicacion en el flujo de devolucion.
 * @param {string} codigo - Codigo de ubicacion escaneado
 */
async function procesarScanDevolucion(codigo) {
  // En flujo devolución el escáner llena el campo de ubicación
  const inp = document.getElementById('input-ubicacion-dev');
  if (inp) {
    inp.value = codigo;
    vibrar(); flash();
    await confirmarUbicacionDev();
  }
}

/**
 * Reporta averia de mercancia y traslada inventario a bodega AV1 en Siesa.
 * @param {number} tareaId - ID de la tarea de devolucion
 */
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

/** Limpia la devolucion activa y vuelve a la lista de devoluciones. */
function volverListaDevoluciones() {
  DEVOLUCION_ACTUAL = null;
  cargarDevoluciones();
}
