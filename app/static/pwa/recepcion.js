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
// RECEPCIONISTA — Devolución de Cliente (busca pedido, cuenta, confirma)
// Reemplaza el flujo reactivo de reconciliación (TareaDevolucion, DEPRECATED).
// ─────────────────────────────────────────────────────────────

/**
 * Carga bultos rechazados (panel informativo, sin cambios) y muestra el
 * buscador de pedido para iniciar una devolución de cliente.
 * @param {boolean} [silencioso=false] - true omite el spinner de carga inicial
 */
async function cargarDevoluciones(silencioso = false) {
  if (DEVOLUCION_ACTUAL) return;
  const el = document.getElementById('contenido-devoluciones');
  const badge = document.getElementById('badge-dev');
  if (!el) return;

  try {
    const rechResp = await get('/api/rutas/bultos-rechazados').catch(() => ({ bultos: [] }));
    const rechazados = rechResp.bultos || [];

    if (badge) {
      badge.style.display = rechazados.length ? 'inline' : 'none';
      badge.textContent = rechazados.length;
    }

    let html = '';

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

    html += `
      <div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 8px;border-bottom:1px solid #222;margin-bottom:10px;margin-top:${rechazados.length ? 16 : 0}px;">
        DEVOLUCIÓN DE CLIENTE
      </div>
      <div class="rec-card">
        <div style="font-size:13px;color:#666;margin-bottom:10px;">Busca el pedido del cliente que devuelve mercancía</div>
        <div style="display:flex;gap:8px;">
          <input id="input-pedido-dev" type="text" placeholder="Número de pedido (ej: PD1347)"
            style="flex:1;padding:12px;background:#111;border:1px solid #333;border-radius:8px;color:#fff;font-size:16px;"
            onkeydown="if(event.key==='Enter') buscarPedidoDevolucion()" />
          <button onclick="buscarPedidoDevolucion()"
            style="padding:12px 16px;background:#1E8395;color:#fff;border:none;border-radius:8px;font-size:15px;font-weight:700;cursor:pointer;">
            Buscar
          </button>
        </div>
        <div id="estado-busqueda-dev" style="margin-top:8px;font-size:12px;color:#555;"></div>
      </div>`;

    el.innerHTML = html;
  } catch (e) {
    if (badge) badge.style.display = 'none';
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando devoluciones</div>';
  }
}

/** Busca el pedido/factura y abre la pantalla de conteo. */
async function buscarPedidoDevolucion() {
  const inp = document.getElementById('input-pedido-dev');
  const estado = document.getElementById('estado-busqueda-dev');
  const numero = (inp ? inp.value : '').trim();
  if (!numero) { alerta('Ingresa el número de pedido', 'advertencia'); return; }

  if (estado) { estado.textContent = '⏳ Buscando...'; estado.style.color = '#93c5fd'; }
  try {
    const r = await get('/api/devoluciones/pedido/' + encodeURIComponent(numero));
    if (r.error) { if (estado) { estado.textContent = 'Error: ' + r.error; estado.style.color = '#ef4444'; } return; }
    DEVOLUCION_ACTUAL = r;
    renderLineasDevolucion(r);
  } catch (e) {
    if (estado) { estado.textContent = 'Error de conexión'; estado.style.color = '#ef4444'; }
  }
}

/**
 * Renderiza la tabla de líneas facturadas para contar físicamente la devolución.
 * @param {Object} datos - Resultado de GET /api/devoluciones/pedido/<numero>:
 *   tarea_packing_id, numero_pedido_siesa, cliente, almacen_id, tipo_docto_fe,
 *   consec_fe, lineas[{producto_id, producto_codigo, producto_nombre,
 *   codigo_siesa, cantidad_facturada, f470_id_unidad_medida, f150_id_bodega, f470_rowid}]
 */
function renderLineasDevolucion(datos) {
  recTab('dev');
  const el = document.getElementById('contenido-devoluciones');
  if (!el) return;

  const filas = (datos.lineas || []).map((l, i) => `
    <div class="rec-card" style="margin-bottom:10px;">
      <div style="font-size:16px;font-weight:700;">${l.producto_nombre}</div>
      <div style="font-size:12px;color:#666;margin-bottom:8px;">${l.producto_codigo} · Facturado: ${l.cantidad_facturada}</div>
      <div style="display:flex;align-items:center;gap:10px;">
        <span style="font-size:12px;color:#aaa;">Devuelto:</span>
        <input id="cant-dev-${i}" type="number" min="0" max="${l.cantidad_facturada}" step="1" value="0"
          style="width:90px;padding:10px;background:#111;border:1px solid #333;border-radius:8px;color:#fff;font-size:16px;text-align:center;" />
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#f87171;cursor:pointer;margin-left:auto;">
          <input id="averiado-dev-${i}" type="checkbox" style="width:18px;height:18px;" /> Averiado
        </label>
      </div>
    </div>`).join('');

  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px;">
      <button onclick="volverBusquedaDevolucion()"
        style="background:#222;border:1px solid #333;color:#fff;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:14px;">
        ← Volver
      </button>
      <span style="font-size:14px;font-weight:700;">Pedido ${datos.numero_pedido_siesa} · ${datos.cliente || '—'}</span>
    </div>

    <div style="font-size:12px;color:#666;margin-bottom:10px;">Cuenta cuánto trajo el conductor de cada línea — deja en 0 lo que no se devolvió</div>

    ${filas}

    <button onclick="confirmarDevolucionCliente()"
      style="width:100%;margin-top:8px;padding:16px;background:#16a34a;color:#fff;border:none;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer;">
      ✓ Confirmar devolución
    </button>
    <div id="estado-confirmar-dev" style="margin-top:10px;font-size:12px;color:#555;text-align:center;"></div>`;
}

/**
 * Encadena crear + confirmar la devolución con las cantidades contadas — un
 * solo clic para el recepcionista, aunque el backend lo modele en dos pasos.
 */
async function confirmarDevolucionCliente() {
  const datos = DEVOLUCION_ACTUAL;
  if (!datos) return;
  const estado = document.getElementById('estado-confirmar-dev');

  const lineas = (datos.lineas || []).map((l, i) => {
    const inpCant = document.getElementById(`cant-dev-${i}`);
    const inpAv = document.getElementById(`averiado-dev-${i}`);
    return {
      producto_id: l.producto_id,
      codigo_siesa: l.codigo_siesa,
      cantidad_facturada: l.cantidad_facturada,
      cantidad_devuelta: inpCant ? (parseFloat(inpCant.value) || 0) : 0,
      es_averiado: inpAv ? inpAv.checked : false,
      f470_id_unidad_medida: l.f470_id_unidad_medida,
      f150_id_bodega: l.f150_id_bodega,
      f470_rowid: l.f470_rowid,
    };
  }).filter(l => l.cantidad_devuelta > 0);

  if (!lineas.length) { alerta('Cuenta al menos una unidad devuelta', 'advertencia'); return; }

  const esTotal = (datos.lineas || []).every(l => {
    const encontrada = lineas.find(x => x.codigo_siesa === l.codigo_siesa);
    return !!encontrada && encontrada.cantidad_devuelta === l.cantidad_facturada;
  });

  if (estado) { estado.textContent = '⏳ Creando devolución...'; estado.style.color = '#93c5fd'; }
  try {
    const rCrear = await post('/api/devoluciones/', {
      tarea_packing_id: datos.tarea_packing_id,
      tipo_docto_fe: datos.tipo_docto_fe,
      consec_fe: datos.consec_fe,
      almacen_id: datos.almacen_id,
      lineas,
      es_total: esTotal
    });
    if (rCrear.error) { if (estado) { estado.textContent = 'Error: ' + rCrear.error; estado.style.color = '#ef4444'; } return; }

    const devolucionId = rCrear.devolucion.id;
    if (estado) { estado.textContent = '⏳ Ingresando stock y generando Nota Crédito...'; }
    const rConfirmar = await post(`/api/devoluciones/${devolucionId}/confirmar`, {});
    if (rConfirmar.error) {
      if (estado) {
        estado.innerHTML = `Error al confirmar: ${rConfirmar.error}<br>
          <button onclick="reintentarConfirmarDevolucion(${devolucionId})"
            style="margin-top:8px;padding:8px 14px;background:#f59e0b;color:#000;border:none;border-radius:8px;cursor:pointer;">
            Reintentar confirmación
          </button>`;
        estado.style.color = '#ef4444';
      }
      return;
    }

    vibrar(); flash();
    alerta('✓ Devolución confirmada — stock ingresado, Nota Crédito en proceso', 'exito');
    DEVOLUCION_ACTUAL = null;
    setTimeout(cargarDevoluciones, 800);
  } catch (e) {
    if (estado) { estado.textContent = 'Error de conexión'; estado.style.color = '#ef4444'; }
  }
}

/**
 * Reintenta confirmar una devolución que quedó ABIERTA por un fallo previo
 * (ej. Siesa no respondió) — nada se pierde, el registro sigue esperando.
 * @param {number} devolucionId - ID de la devolución a reintentar
 */
async function reintentarConfirmarDevolucion(devolucionId) {
  const estado = document.getElementById('estado-confirmar-dev');
  if (estado) { estado.textContent = '⏳ Reintentando...'; estado.style.color = '#93c5fd'; }
  try {
    const r = await post(`/api/devoluciones/${devolucionId}/confirmar`, {});
    if (r.error) { if (estado) { estado.textContent = 'Error: ' + r.error; estado.style.color = '#ef4444'; } return; }
    vibrar(); flash();
    alerta('✓ Devolución confirmada — stock ingresado, Nota Crédito en proceso', 'exito');
    DEVOLUCION_ACTUAL = null;
    setTimeout(cargarDevoluciones, 800);
  } catch (e) {
    if (estado) { estado.textContent = 'Error de conexión'; estado.style.color = '#ef4444'; }
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
  const idx = datos.lineas.findIndex(l => l.codigo_siesa === cod || l.producto_codigo === cod);
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
// COMPRAS — 4 paneles: Velocity+ABC, Dock Lock, Cuarentena, Audit Trail
// ═══════════════════════════════════════════════════════════════════════════════

let COMP_SUBTAB = 'velocity';
let COMP_VELOCITY_DATA = [];    // cache para filtros client-side
let COMP_TIMER = null;
let COMP_PANTALLA = false;      // true si es pantalla-compras (rol compras)

/** Initialize the purchasing (compras) screen and load data. */
function compIniciarPantalla() {
  COMP_PANTALLA = true;
  compCargarResumen('comp2');
  compCargarVelocity('comp2');
  COMP_TIMER = setInterval(() => {
    compCargarResumen(COMP_PANTALLA ? 'comp2' : 'comp');
  }, 60000);
}

/** Fetch and render the active purchasing sub-tab content. */
async function cargarCompras() {
  COMP_PANTALLA = false;
  await Promise.all([
    compCargarResumen('comp'),
    compCargarVelocity('comp'),
  ]);
}

// ── Sub-tabs (admin tab-compras) ──────────────────────────────────────────
/** @param {string} id - Purchasing primary sub-tab to activate. */
function compSubtab(id) {
  COMP_SUBTAB = id;
  const secs = ['velocity','dock','cuarentena','audit','bloqueos','acuerdos','armador','deriva','temporada','modelos','nacional'];
  secs.forEach(s => {
    const el = document.getElementById('comp-sec-' + s);
    const tab = document.getElementById('comp-sub-' + s);
    if (el) el.style.display = s === id ? 'block' : 'none';
    if (tab) {
      tab.style.background = s === id ? 'var(--pm)' : 'transparent';
      tab.style.color = s === id ? '#fff' : 'var(--tx3)';
      tab.style.fontWeight = s === id ? '700' : '400';
    }
  });
  if (id === 'velocity') compCargarVelocity('comp');
  else if (id === 'dock') compCargarDock('comp');
  else if (id === 'cuarentena') compCargarCuarentena('comp');
  else if (id === 'bloqueos') compCargarBloqueos();
  else if (id === 'acuerdos') compCargarAcuerdos();
  else if (id === 'armador') compCargarArmador();
  else if (id === 'deriva') compCargarDeriva();
  else if (id === 'nacional') compCargarNacional();
  else if (id === 'temporada') temporadaCargar();
  else if (id === 'modelos') modelosCargar();
  // audit: manual search — no auto-load
}

// ── Sub-tabs (pantalla-compras dedicada) ──────────────────────────────────
/** @param {string} id - Purchasing secondary sub-tab to activate. */
function compSubtab2(id) {
  COMP_SUBTAB = id;
  const secs = ['velocity','dock','cuarentena','audit'];
  secs.forEach(s => {
    const tab = document.getElementById('comp2-sub-' + s);
    if (tab) {
      tab.style.background = s === id ? 'var(--pm)' : 'transparent';
      tab.style.color = s === id ? '#fff' : 'var(--tx3)';
      tab.style.fontWeight = s === id ? '700' : '400';
    }
  });
  const cont = document.getElementById('comp2-contenido');
  if (!cont) return;
  if (id === 'velocity') compCargarVelocity('comp2');
  else if (id === 'dock') compCargarDock('comp2');
  else if (id === 'cuarentena') compCargarCuarentena('comp2');
  else if (id === 'audit') compRenderAuditForm('comp2');
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
  prefix = prefix || 'comp';
  const target = prefix === 'comp2'
    ? document.getElementById('comp2-contenido')
    : document.getElementById('comp-lista-velocity');
  if (!target) return;

  const diasSel = document.getElementById('comp-vel-dias');
  const dias = diasSel ? diasSel.value : 30;

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando velocity...</div>';

  try {
    const r = await get(`/api/compras/velocity?dias=${dias}&almacen_id=${ALMACEN_ID}`);
    COMP_VELOCITY_DATA = r.items || [];

    if (prefix === 'comp2') {
      // Render filters + list into comp2-contenido
      target.innerHTML = _compVelocityFiltersHtml() + '<div id="comp2-vel-list"></div>';
      _compRenderVelocityList(document.getElementById('comp2-vel-list'), COMP_VELOCITY_DATA, r);
    } else {
      _compRenderVelocityList(target, COMP_VELOCITY_DATA, r);
    }
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando velocity</div>';
  }
}

/**
 * Build HTML for the velocity analytics filter controls.
 * @returns {string} HTML string.
 */
function _compVelocityFiltersHtml() {
  return `<div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center;">
    <select id="comp2-vel-dias" onchange="compCargarVelocity('comp2')"
      style="padding:8px 12px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:13px;">
      <option value="7">7 días</option><option value="15">15 días</option>
      <option value="30" selected>30 días</option><option value="60">60 días</option>
    </select>
    <select id="comp2-vel-abc" onchange="compFiltrarVelocity('comp2')"
      style="padding:8px 12px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:13px;">
      <option value="">Todos ABC</option><option value="A">Solo A</option>
      <option value="B">Solo B</option><option value="C">Solo C</option>
    </select>
    <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--tx3);cursor:pointer;">
      <input type="checkbox" id="comp2-vel-alertas" onchange="compFiltrarVelocity('comp2')"> Solo alertas
    </label>
  </div>`;
}

/** @param {string} prefix - DOM prefix; apply ABC/text filters to the velocity list. */
function compFiltrarVelocity(prefix) {
  prefix = prefix || 'comp';
  const abcSel = document.getElementById((prefix === 'comp2' ? 'comp2' : 'comp') + '-vel-abc');
  const alertaSel = document.getElementById((prefix === 'comp2' ? 'comp2' : 'comp') + '-vel-alertas');
  const abcFiltro = abcSel ? abcSel.value : '';
  const soloAlertas = alertaSel ? alertaSel.checked : false;

  let filtered = COMP_VELOCITY_DATA;
  if (abcFiltro) filtered = filtered.filter(i => i.abc === abcFiltro);
  if (soloAlertas) filtered = filtered.filter(i => i.alerta);

  const target = prefix === 'comp2'
    ? document.getElementById('comp2-vel-list')
    : document.getElementById('comp-lista-velocity');
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
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Sin datos de velocity en este período</div>';
    return;
  }

  const alertas = meta.alertas_a || 0;
  let html = alertas > 0
    ? `<div style="background:#110a0a;border:1px solid #7f1d1d;border-radius:10px;padding:10px 14px;margin-bottom:12px;">
         <span style="color:#f87171;font-weight:700;">⚠ ${alertas} item(s) A agotándose</span>
         <span style="color:#fca5a5;font-size:11px;"> — consumo > entrada, stock PICKING <15 días</span>
       </div>`
    : '';

  html += `<div style="font-size:11px;color:var(--tx3);margin-bottom:8px;">${items.length} productos con movimiento</div>`;

  html += '<div style="display:flex;flex-direction:column;gap:6px;">';
  for (const it of items) {
    const abcColor = it.abc === 'A' ? '#ef4444' : it.abc === 'B' ? '#f59e0b' : it.abc === 'C' ? '#3b82f6' : '#555';
    const alertaBg = it.alerta ? 'background:#110a0a;border-color:#7f1d1d;' : '';
    html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:10px 12px;${alertaBg}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
        <div style="font-size:13px;font-weight:700;color:var(--tx);">${it.codigo}</div>
        <span style="font-size:11px;font-weight:800;color:${abcColor};background:${abcColor}22;padding:2px 8px;border-radius:6px;">${it.abc}</span>
      </div>
      <div style="font-size:11px;color:var(--tx3);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${it.nombre}</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:4px;font-size:11px;">
        <div><span style="color:var(--tx3);">Picks/día</span><br><strong style="color:var(--tx);">${it.picks_dia}</strong></div>
        <div><span style="color:var(--tx3);">Total período</span><br><strong style="color:var(--tx);">${it.picks_periodo}</strong></div>
        <div><span style="color:var(--tx3);">Stock PICK</span><br><strong style="color:var(--tx);">${it.stock_picking}</strong></div>
        <div><span style="color:var(--tx3);">Días stock</span><br><strong style="color:${it.dias_stock_estimado < 15 ? '#ef4444' : it.dias_stock_estimado < 30 ? '#f59e0b' : 'var(--tx)'};">${it.dias_stock_estimado >= 999 ? '∞' : it.dias_stock_estimado}</strong></div>
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

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando dock lock...</div>';

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
      html += `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px;">
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:#ef4444;">${r.total_recepciones_con_problema || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Con problema</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:#f59e0b;">${r.total_excesos || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Excesos</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;text-align:center;">
          <div style="font-size:22px;font-weight:800;color:#3b82f6;">${r.total_faltantes || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Faltantes</div>
        </div>
      </div>`;
    }

    const recs = r.recepciones || [];
    if (!recs.length) {
      html += '<div style="text-align:center;padding:30px;color:#555;">Sin rechazos en este período</div>';
    } else {
      html += '<div style="display:flex;flex-direction:column;gap:8px;">';
      for (const rec of recs) {
        const fecha = rec.fecha_confirmacion ? new Date(rec.fecha_confirmacion).toLocaleDateString('es-CO') : '—';
        let itemsHtml = '';
        for (const it of rec.items_problema) {
          const difColor = it.tipo_problema === 'EXCESO' ? '#f59e0b' : '#3b82f6';
          const difSign = it.diferencia > 0 ? '+' : '';
          itemsHtml += `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--brd);font-size:12px;">
            <span style="color:var(--tx);">${it.producto_codigo} — ${(it.producto_nombre||'').substring(0,30)}</span>
            <span style="color:${difColor};font-weight:700;">${difSign}${it.diferencia} (${it.tipo_problema})</span>
          </div>`;
        }
        html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
            <div>
              <span style="font-size:13px;font-weight:700;color:var(--tx);">OC ${rec.oc_siesa}</span>
              <span style="font-size:11px;color:var(--tx3);margin-left:8px;">${rec.codigo}</span>
            </div>
            <span style="font-size:11px;color:var(--tx3);">${fecha}</span>
          </div>
          <div style="font-size:12px;color:var(--tx2);margin-bottom:8px;">${rec.proveedor_nombre || rec.proveedor_codigo || '—'}${rec.es_parcial ? ' · <span style="color:#f59e0b;">PARCIAL</span>' : ''}</div>
          <div style="background:var(--bg-s2);border-radius:8px;padding:6px 8px;">
            ${itemsHtml}
          </div>
          <div style="font-size:11px;color:var(--tx3);margin-top:4px;">${rec.total_problemas} item(s) con discrepancia</div>
        </div>`;
      }
      html += '</div>';
    }

    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando dock lock</div>';
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

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando cuarentena...</div>';

  try {
    const r = await get(`/api/compras/cuarentena?almacen_id=${ALMACEN_ID}`);

    if (!isP2) {
      set('cuar-pendientes', r.pendientes || 0);
      set('cuar-productos', r.total_productos_en_averias || 0);
    }

    let html = '';

    if (isP2) {
      html += `<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px;">
        <div style="background:#110a0a;border:1px solid #7f1d1d;border-radius:10px;padding:14px;text-align:center;">
          <div style="font-size:26px;font-weight:800;color:#f87171;">${r.pendientes || 0}</div>
          <div style="font-size:11px;color:#fca5a5;">Pendientes de gestión</div>
        </div>
        <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:14px;text-align:center;">
          <div style="font-size:26px;font-weight:800;color:var(--tx);">${r.total_productos_en_averias || 0}</div>
          <div style="font-size:11px;color:var(--tx3);">Productos en zona averías</div>
        </div>
      </div>`;
    }

    // Stock en zona AVERÍAS
    const stock = r.stock_en_averias || [];
    if (stock.length) {
      html += `<div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">Stock en zona AVERÍAS</div>`;
      html += '<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:16px;">';
      for (const s of stock) {
        html += `<div style="display:flex;justify-content:space-between;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:8px 12px;">
          <div style="font-size:12px;"><strong>${s.producto_codigo}</strong> — ${(s.producto_nombre||'').substring(0,35)}</div>
          <div style="font-size:14px;font-weight:800;color:#ef4444;">${s.cantidad_averiada} UND</div>
        </div>`;
      }
      html += '</div>';
    }

    // Tareas averiadas
    const devs = r.devoluciones_averiadas || [];
    if (devs.length) {
      html += `<div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">Devoluciones averiadas (${devs.length})</div>`;
      html += '<div style="display:flex;flex-direction:column;gap:6px;">';
      for (const d of devs) {
        const estadoColor = d.estado === 'PENDIENTE' ? '#f59e0b' : d.estado === 'EN_PROCESO' ? '#3b82f6' : d.estado === 'COMPLETADO' ? '#22c55e' : '#555';
        const diasColor = d.dias_sin_gestion > 7 ? '#ef4444' : d.dias_sin_gestion > 3 ? '#f59e0b' : 'var(--tx3)';
        const siesa = d.siesa_triggered ? '<span style="color:#22c55e;font-size:10px;">✓ Siesa</span>' : '<span style="color:#ef4444;font-size:10px;">✗ Siesa</span>';
        html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:10px 12px;">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <div>
              <span style="font-size:13px;font-weight:700;color:var(--tx);">${d.codigo}</span>
              <span style="font-size:11px;color:${estadoColor};margin-left:8px;font-weight:700;">${d.estado}</span>
              ${siesa}
            </div>
            <span style="font-size:12px;font-weight:700;color:${diasColor};">${d.dias_sin_gestion}d</span>
          </div>
          <div style="font-size:12px;color:var(--tx2);margin-top:4px;">${d.producto_codigo || '—'} — ${(d.producto_nombre||'').substring(0,35)}</div>
          <div style="font-size:11px;color:var(--tx3);margin-top:2px;">${d.cantidad} UND${d.observaciones ? ' · ' + d.observaciones.substring(0,60) : ''}</div>
        </div>`;
      }
      html += '</div>';
    }

    if (!stock.length && !devs.length) {
      html += '<div style="text-align:center;padding:30px;color:#22c55e;font-weight:700;">Sin averías pendientes</div>';
    }

    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error cargando cuarentena</div>';
  }
}


// ═══════════════════════════════════════════════════════════════════════════
// AUDIT TRAIL — búsqueda por OC/proveedor
// ═══════════════════════════════════════════════════════════════════════════

function compRenderAuditForm(prefix) {
  const target = document.getElementById('comp2-contenido');
  if (!target) return;
  target.innerHTML = `<div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center;">
    <input type="text" id="comp2-audit-buscar" placeholder="Buscar por OC, proveedor, remisión..."
      style="flex:1;min-width:200px;padding:10px 14px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:13px;"
      onkeydown="if(event.key==='Enter')compCargarAudit('comp2')">
    <button onclick="compCargarAudit('comp2')"
      style="padding:10px 18px;background:var(--pm);border:none;border-radius:8px;color:#fff;font-size:13px;font-weight:700;cursor:pointer;">
      Buscar
    </button>
  </div>
  <div id="comp2-audit-results">
    <div style="text-align:center;padding:30px;color:#555;">Escribe OC, proveedor o remisión y presiona Buscar</div>
  </div>`;
}

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

  target.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Buscando...</div>';

  try {
    const r = await get(`/api/compras/audit-trail?q=${encodeURIComponent(q)}&dias=${dias}&almacen_id=${ALMACEN_ID}`);
    const resultados = r.resultados || [];

    if (!resultados.length) {
      target.innerHTML = `<div style="text-align:center;padding:30px;color:#555;">Sin resultados para "${q}"</div>`;
      return;
    }

    let html = `<div style="font-size:11px;color:var(--tx3);margin-bottom:8px;">${resultados.length} recepción(es) encontradas</div>`;
    html += '<div style="display:flex;flex-direction:column;gap:8px;">';

    for (const rec of resultados) {
      const fecha = rec.fecha_confirmacion ? new Date(rec.fecha_confirmacion).toLocaleDateString('es-CO') : rec.fecha_creacion ? new Date(rec.fecha_creacion).toLocaleDateString('es-CO') : '—';
      const estadoColor = rec.estado === 'CONFIRMADA' ? '#22c55e' : rec.estado === 'EN_PROCESO' ? '#3b82f6' : rec.estado === 'CANCELADA' ? '#ef4444' : '#f59e0b';
      const siesa = rec.siesa_triggered ? '<span style="color:#22c55e;font-size:10px;">✓ Siesa</span>' : '<span style="color:#ef4444;font-size:10px;">✗ Siesa</span>';

      let itemsHtml = '';
      for (const it of (rec.items || [])) {
        const difColor = it.es_exceso ? '#f59e0b' : it.es_faltante ? '#3b82f6' : '#22c55e';
        const difText = it.diferencia !== 0 ? ` (${it.diferencia > 0 ? '+' : ''}${it.diferencia})` : '';
        itemsHtml += `<div style="display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid var(--brd);font-size:11px;">
          <span style="color:var(--tx);">${it.producto_codigo}${it.tipo === 'BONIFICACION' ? ' <span style="color:#8b5cf6;">BONIF</span>' : ''}</span>
          <span>OC: ${it.cantidad_ordenada} → Rec: <strong style="color:${difColor};">${it.cantidad_recibida}${difText}</strong></span>
        </div>`;
      }

      html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <div>
            <span style="font-size:13px;font-weight:700;color:var(--tx);">OC ${rec.oc_siesa}</span>
            <span style="font-size:11px;color:${estadoColor};margin-left:8px;font-weight:700;">${rec.estado}</span>
            ${siesa}
          </div>
          <span style="font-size:11px;color:var(--tx3);">${fecha}</span>
        </div>
        <div style="font-size:12px;color:var(--tx2);margin-bottom:2px;">${rec.proveedor_nombre || rec.proveedor_codigo || '—'}</div>
        <div style="font-size:11px;color:var(--tx3);margin-bottom:6px;">
          Recepcionista: <strong>${rec.recepcionista || '—'}</strong>
          · Remisión: ${rec.remision || '—'}
          · ${rec.codigo}
          ${rec.es_parcial ? ' · <span style="color:#f59e0b;">PARCIAL</span>' : ''}
        </div>
        <div style="background:var(--bg-s2);border-radius:8px;padding:6px 8px;max-height:200px;overflow-y:auto;">
          ${itemsHtml}
        </div>
        <div style="font-size:11px;color:var(--tx3);margin-top:4px;">${rec.total_items} item(s)</div>
      </div>`;
    }
    html += '</div>';
    target.innerHTML = html;
  } catch (e) {
    target.innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px;">Error en búsqueda</div>';
  }
}


// ══════════════════════════════════════════════════════════════════════════════
// BLOQUEOS DE RECOMPRA — el sistema que dice NO
// ══════════════════════════════════════════════════════════════════════════════

const _liqFmtComp = v => '$' + Number(v || 0).toLocaleString('es-CO');

async function compCargarBloqueos() {
  const lista = document.getElementById('comp-bloqueos-lista');
  const resumen = document.getElementById('comp-bloqueos-resumen');
  if (!lista) return;
  lista.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';
  try {
    const d = await get('/api/compras/bloqueados');
    const items = d.items || [];

    if (resumen) {
      resumen.innerHTML = `
        <div style="background:#110a0a;border:1px solid #7f1d1d;border-radius:12px;padding:16px;margin-bottom:16px;text-align:center;">
          <div style="font-size:11px;color:#fca5a5;text-transform:uppercase;font-weight:700;margin-bottom:4px;">Capital inmovilizado en cadáveres</div>
          <div style="font-size:28px;font-weight:800;color:#f87171;">${_liqFmtComp(d.total_inmovilizado)}</div>
          <div style="font-size:12px;color:#fca5a5;margin-top:4px;">${d.total_skus} SKU${d.total_skus !== 1 ? 's' : ''} bloqueado${d.total_skus !== 1 ? 's' : ''}</div>
        </div>`;
    }

    if (!items.length) {
      lista.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Sin productos bloqueados — usa "Generar lista inicial" para poblar</div>';
      return;
    }

    let html = '';
    items.forEach((it, idx) => {
      html += `
        <div style="background:var(--bg-s);border:1px solid ${idx < 3 ? '#7f1d1d' : 'var(--brd)'};border-radius:10px;padding:12px;margin-bottom:8px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
              <div style="font-size:13px;font-weight:700;color:var(--tx);">${it.codigo}</div>
              <div style="font-size:11px;color:var(--tx3);margin-top:2px;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${it.nombre}</div>
            </div>
            <div style="text-align:right;">
              <div style="font-size:14px;font-weight:800;color:#f87171;">${_liqFmtComp(it.capital_inmovilizado)}</div>
              <div style="font-size:10px;color:var(--tx3);">${it.stock} UND × ${_liqFmtComp(it.costo_unitario)}</div>
            </div>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;">
            <span style="font-size:10px;padding:2px 8px;border-radius:20px;background:#7f1d1d33;color:#fca5a5;font-weight:700;">${it.motivo}</span>
            <button onclick="compDesbloquear(${it.bloqueo_id},'${it.codigo}')"
              style="padding:4px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx3);font-size:11px;cursor:pointer;">
              Solicitar desbloqueo
            </button>
          </div>
        </div>`;
    });
    lista.innerHTML = html;

    // Cargar fugas
    compCargarFugas();
  } catch (e) {
    lista.innerHTML = `<div style="color:#ef4444;text-align:center;padding:20px;">${e.message || 'Error cargando bloqueos'}</div>`;
  }
}

async function compPoblarBloqueos() {
  if (!confirm('¿Generar lista inicial de bloqueos?\nSe bloquearán todos los SKUs con velocity=0 en 12 meses y stock existente.')) return;
  try {
    const r = await fetch(API + '/api/compras/bloqueados/poblar', {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      alerta(`${d.bloqueados_nuevos} SKU(s) bloqueado(s) — ${_liqFmtComp(d.total_capital_inmovilizado)} inmovilizado`, 'exito');
      compCargarBloqueos();
    } else {
      alerta(d.error || 'Error al poblar', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function compDesbloquear(bloqueoId, codigo) {
  const motivo = prompt(`¿Por qué desbloquear ${codigo}?\n(Motivo obligatorio — queda registrado)`);
  if (!motivo || !motivo.trim()) return;
  const cantidad = prompt('Cantidad máxima autorizada a comprar:');
  if (!cantidad || isNaN(cantidad) || Number(cantidad) <= 0) { alerta('Cantidad inválida', 'error'); return; }
  const vigencia = prompt('Vigencia del desbloqueo en días (default 30):', '30');
  const dias = parseInt(vigencia) || 30;

  try {
    const r = await fetch(API + `/api/compras/bloqueados/${bloqueoId}/desbloquear`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo: motivo.trim(), cantidad_autorizada: Number(cantidad), vigencia_dias: dias }),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      alerta(`${codigo} desbloqueado — máx ${cantidad} UND, vigencia ${dias} días`, 'exito');
      compCargarBloqueos();
    } else {
      alerta(d.error || 'Error al desbloquear', 'error');
    }
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

async function compCargarFugas() {
  const el = document.getElementById('comp-fugas-lista');
  if (!el) return;
  try {
    const d = await get('/api/compras/bloqueados/fugas');
    const fugas = d.fugas || [];
    if (!fugas.length) {
      el.innerHTML = '<div style="text-align:center;padding:20px;color:#22c55e;font-size:12px;">Sin fugas — todas las compras pasan por el sistema ✓</div>';
      return;
    }
    el.innerHTML = fugas.map(f => `
      <div style="display:flex;justify-content:space-between;padding:8px;background:#78350f22;border:1px solid #78350f;border-radius:8px;margin-bottom:6px;font-size:12px;">
        <div>
          <strong style="color:#fbbf24;">${f.producto_codigo}</strong> — ${f.producto_nombre || '—'}
          <div style="font-size:11px;color:var(--tx3);">OC: ${f.oc_siesa || '—'} · Prov: ${f.proveedor || '—'} · ${f.cantidad_recibida} UND</div>
        </div>
        <span style="color:var(--tx3);font-size:11px;white-space:nowrap;">${new Date(f.fecha).toLocaleDateString('es-CO')}</span>
      </div>`).join('');
  } catch (e) {}
}
