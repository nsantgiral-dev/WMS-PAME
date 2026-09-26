// ══════════════════════════════════════════════════════════════════
// EMPACADOR / PACKING — HUD, escaneo, bultos, factura, etiquetas
// Dependencias globales (de app.js): get(), post(), alerta(), flash(),
//   TOKEN, OPERARIO, beepOk(), beepError(), beepDone(), vibrar()
// ══════════════════════════════════════════════════════════════════

// EMPACADOR — Estado global
// ─────────────────────────────────────────────────────────────

/** @type {Object|null} TareaPacking activa en el HUD */
let EMP_TAREA = null;
/** @type {{id: number, producto_codigo: string, cantidad_esperada: number, cantidad_real: number, verificado: boolean}[]} */
let EMP_ITEMS = [];
/** @type {number} Índice del ítem que se está escaneando */
let EMP_ITEM_IDX = 0;
/** @type {Object<number, {factor: number, unidad: string}>} producto_id → empaque info */
let EMP_EMPAQUES = {};
/** @type {Object[]} Última lista de tareas de packing cargada del servidor, sin filtrar */
let EMP_TAREAS_ALL = [];
/** @type {'PEDIDO'|'TRASLADO'} Pestaña activa en la lista de tareas */
let EMP_FILTRO_TIPO = 'PEDIDO';

// ─────────────────────────────────────────────────────────────
// EMPACADOR — Lista de tareas
// ─────────────────────────────────────────────────────────────

/** Carga del servidor y renderiza la lista de tareas de packing asignadas al empacador. */
async function empCargarTareas() {
  if (_empBloqueoOfflineInfo()) { _empMostrarBloqueoOffline(); return; }
  const el = document.getElementById('emp-lista');
  if (!el) return;
  try {
    const d = await get('/api/packing/?activas=true');
    EMP_TAREAS_ALL = (d.tareas || []).filter(t =>
      ['PENDIENTE', 'EN_PROCESO'].includes(t.estado) ||
      (t.estado === 'VERIFICADO' && !t.siesa_triggered)  // Siesa falló — permitir reintento
    );

    // Si la pestaña activa ya no tiene tareas de ese tipo, cambia a la que sí tenga
    const hayTrasladosTotal = EMP_TAREAS_ALL.some(t => t.tipo_documento === 'TRASLADO');
    const hayPedidosTotal   = EMP_TAREAS_ALL.some(t => t.tipo_documento !== 'TRASLADO');
    if (EMP_FILTRO_TIPO === 'TRASLADO' && !hayTrasladosTotal && hayPedidosTotal) {
      EMP_FILTRO_TIPO = 'PEDIDO';
    } else if (EMP_FILTRO_TIPO === 'PEDIDO' && !hayPedidosTotal && hayTrasladosTotal) {
      EMP_FILTRO_TIPO = 'TRASLADO';
    }

    empRenderListaTareas();
  } catch (e) {
    el.innerHTML = '<div style="color:var(--err-tx);text-align:center;padding:40px;">Error cargando tareas</div>';
  }
}

/** Cambia la pestaña de filtro (Pedidos/Traslados) y re-renderiza sin llamar al servidor. */
function empSetFiltroTipo(tipo) {
  EMP_FILTRO_TIPO = tipo;
  empRenderListaTareas();
}

/** Renderiza pestañas + lista de tareas a partir de EMP_TAREAS_ALL, aplicando EMP_FILTRO_TIPO. */
function empRenderListaTareas() {
  const el = document.getElementById('emp-lista');
  if (!el) return;

  if (!EMP_TAREAS_ALL.length) {
    const titulo = document.getElementById('emp-modo-titulo');
    if (titulo) titulo.textContent = '';
    el.innerHTML = `<div style="text-align:center;padding:60px 20px;color:var(--tx3);">
      Sin tareas de empaque pendientes ✓<br>
      <button onclick="_refreshBtn(event, empCargarTareas)" style="margin-top:20px;background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);padding:10px 20px;border-radius:10px;cursor:pointer;">↻ Actualizar</button>
    </div>`;
    return;
  }

  // Título de contexto en el header blanco
  const hayTraslados = EMP_TAREAS_ALL.some(t => t.tipo_documento === 'TRASLADO');
  const hayPedidos   = EMP_TAREAS_ALL.some(t => t.tipo_documento !== 'TRASLADO');
  const titulo = document.getElementById('emp-modo-titulo');
  if (titulo) {
    if (hayTraslados && !hayPedidos) {
      titulo.textContent = '📦 Packing Traslado';
      titulo.style.color = 'var(--orange)';
    } else if (hayPedidos && !hayTraslados) {
      titulo.textContent = '🛒 Packing Pedido';
      titulo.style.color = 'var(--info-tx)';
    } else {
      titulo.textContent = '📦 Packing Mixto';
      titulo.style.color = 'var(--tx)';
    }
  }

  // Pestañas — solo si hay ambos tipos, si no no hay nada que separar
  let tabsHtml = '';
  if (hayTraslados && hayPedidos) {
    const nPedidos = EMP_TAREAS_ALL.filter(t => t.tipo_documento !== 'TRASLADO').length;
    const nTraslados = EMP_TAREAS_ALL.filter(t => t.tipo_documento === 'TRASLADO').length;
    const tab = (tipo, label, count, colorActivo) => {
      const activo = EMP_FILTRO_TIPO === tipo;
      return `<button onclick="empSetFiltroTipo('${tipo}')"
        style="flex:1;padding:9px 6px;border-radius:8px;border:1px solid ${activo ? colorActivo : 'var(--brd)'};background:${activo ? colorActivo : 'transparent'};color:${activo ? 'var(--tx)' : '#aaa'};font-size:var(--fs-xs);font-weight:700;cursor:pointer;transition:.15s;">
        ${label} (${count})
      </button>`;
    };
    tabsHtml = `<div style="display:flex;gap:8px;padding:4px 0 12px;">
      ${tab('PEDIDO', '🛒 Pedidos', nPedidos, '#1d4ed8')}
      ${tab('TRASLADO', '📦 Traslados', nTraslados, '#c2410c')}
    </div>`;
  }

  const tareas = EMP_TAREAS_ALL.filter(t => (t.tipo_documento === 'TRASLADO') === (EMP_FILTRO_TIPO === 'TRASLADO'));

  if (!tareas.length) {
    el.innerHTML = `${tabsHtml}<div style="text-align:center;padding:40px 20px;color:var(--tx3);">Sin tareas en esta pestaña</div>`;
    return;
  }

  el.innerHTML = `
      ${tabsHtml}
      <div style="font-size:var(--fs-xs);font-weight:600;color:var(--tx2);padding:4px 0 12px;">TAREAS DE EMPAQUE</div>
      ${tareas.map(t => {
        const verificados = t.items_verificados || 0;
        const total = t.total_items || 0;
        const pct = total ? Math.round(verificados / total * 100) : 0;
        const pickingListo = t.picking_listo !== false;
        const pedidoAnulado = t.pedido_anulado_siesa === true;
        const _puedeCancelarPacking = OPERARIO && ['admin', 'supervisor'].includes(OPERARIO.rol);
        // Retenido por cartera en el cierre: no es un fallo de Siesa. Lo libera
        // cartera; después se cierra desde la cola de pedidos.
        const retenidoCartera = !!t.retencion_cartera && !t.siesa_triggered;
        const siesaFallo = t.estado === 'VERIFICADO' && !t.siesa_triggered && !pedidoAnulado && !retenidoCartera;
        const enProceso = t.estado === 'EN_PROCESO';
        const bloqueado = (!pickingListo && t.estado === 'PENDIENTE') || pedidoAnulado;
        // Una tarea BLOQUEADA (backorder Siesa, faltante, avería...) no se
        // resuelve pickeando — la resuelve un admin en Bodega → Auditoría.
        // "Esperando picking" ahí manda al empacador a esperar algo que
        // nunca va a pasar solo.
        const enAuditoria = bloqueado && !pedidoAnulado && t.picking_bloqueado === true;
        const color = pedidoAnulado ? 'var(--red)' : enAuditoria ? '#c084fc' : bloqueado ? '#6b7280' : siesaFallo ? '#fca5a5' : enProceso ? '#93c5fd' : '#facc15';
        const bg    = pedidoAnulado ? 'var(--rbg)' : enAuditoria ? '#2e1065' : bloqueado ? '#1a1a1a'  : siesaFallo ? '#7f1d1d'  : enProceso ? '#1e3a5f' : '#713f12';
        const label = retenidoCartera ? '⛔ Retenido por cartera' : pedidoAnulado ? '🚫 PEDIDO ANULADO EN SIESA' : enAuditoria ? '🔍 En auditoría' : bloqueado ? 'Esperando picking' : siesaFallo ? '⚠ Reintentar Siesa' : enProceso ? 'En proceso' : 'Pendiente';
        const anulado_banner = pedidoAnulado ? `
          <div style="margin-top:10px;background:var(--rbg);border:1px solid var(--rbrd);border-radius:8px;padding:10px 12px;">
            <div style="font-size:var(--fs-xs);font-weight:700;color:var(--red);margin-bottom:4px;">🚫 Pedido anulado en Siesa (estado ${esc(t.pedido_estado_siesa_detectado || '9')})</div>
            <div style="font-size:var(--fs-xs);color:var(--tx2);line-height:1.4;">
              El área comercial anuló este pedido en el ERP.<br>
              <strong>Acción:</strong> ${_puedeCancelarPacking ? 'Cancelar este packing y esperar el nuevo pedido clonado.' : 'Avise a su supervisor para que cancele este packing.'}
            </div>
            ${_puedeCancelarPacking ? `
            <button onclick="event.stopPropagation();empCancelarPacking(${esc(t.id)})"
              style="margin-top:8px;width:100%;padding:8px;background:#dc2626;border:none;color:#fff;border-radius:8px;cursor:pointer;font-size:var(--fs-xs);font-weight:700;">
              Cancelar packing
            </button>` : ''}
          </div>` : '';
        const limpiarBtn = siesaFallo ? `
          <button onclick="event.stopPropagation();empLimpiarSiesa(${esc(t.id)})"
            style="margin-top:8px;width:100%;padding:8px;background:var(--bg-input);border:1px solid #444;color:var(--tx2);border-radius:8px;cursor:pointer;font-size:var(--fs-xs);">
            🗑 Limpiar bultos y redeclarar piezas
          </button>` : '';
        const esTraslado = t.tipo_documento === 'TRASLADO';
        const refDisplay = t.referencia_doc || t.numero_pedido_siesa || '—';
        const etiquetaHtml = esTraslado
          ? `<span style="font-size:var(--fs-xs);font-weight:700;padding:2px 8px;border-radius:10px;background:var(--warn-bg);color:var(--orange);letter-spacing:.5px;margin-left:8px;">TRASLADO</span>`
          : `<span style="font-size:var(--fs-xs);font-weight:700;padding:2px 8px;border-radius:10px;background:#1e3a5f;color:var(--info-tx);letter-spacing:.5px;margin-left:8px;">PEDIDO</span>`;
        const destinoHtml = esTraslado && t.tienda_destino
          ? `<div style="font-size:var(--fs-xs);color:var(--orange);margin-top:2px;">→ ${esc(t.tienda_destino)}</div>` : '';
        // Franja lateral: naranja = traslado, azul = pedido — mismo lenguaje de color del badge,
        // solo se omite si la tarjeta ya tiene su propio borde de error (pedido anulado).
        const acentoLateral = `border-left:4px solid ${esTraslado ? '#c2410c' : '#1d4ed8'};`;
        return `
        <div class="emp-task-card" onclick="${(bloqueado || pedidoAnulado || retenidoCartera) ? '' : `empIniciarHUD(${esc(t.id)})`}"
          style="${(bloqueado || pedidoAnulado || retenidoCartera) ? 'cursor:default;' : 'cursor:pointer;'}${pedidoAnulado ? 'border:2px solid var(--red);' : acentoLateral}">
          <div class="emp-task-pedido" style="display:flex;align-items:center;">${refDisplay}${etiquetaHtml}</div>
          ${destinoHtml}
          <div class="emp-task-sub">${total} producto(s) · ${esc(t.items_verificados || 0)}/${total} verificados</div>
          ${total > 0 ? `<div style="margin-top:10px;background:var(--bg-input);border-radius:8px;height:6px;overflow:hidden;">
            <div style="height:100%;background:#4ade80;width:${pct}%;border-radius:8px;transition:width 0.3s;"></div>
          </div>` : ''}
          <span class="emp-task-badge" style="${retenidoCartera ? 'background:var(--warn-bg);color:var(--warn-tx);' : `background:${bg};color:${color};`}">${label}</span>
          ${retenidoCartera ? `<div style="margin-top:6px;font-size:var(--fs-xs);color:var(--warn-tx);">${esc(t.retencion_cartera.resumen || '')}</div>` : ''}
          ${anulado_banner}
          ${limpiarBtn}
        </div>`;
      }).join('')}`;
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: iniciar tarea y mostrar primer ítem
// ─────────────────────────────────────────────────────────────

/**
 * Inicia el HUD de empaque — carga items, sincroniza picking, muestra primer ítem.
 * @param {number} packingId
 */
/** Ingreso manual del código en el HUD de empaque — mismo destino que la cámara. */
function empEscanearManual() {
  const inp = document.getElementById('emp-codigo-manual');
  const v = (inp?.value || '').trim();
  if (!v) return;
  inp.value = '';
  empProcesarEscaneo(v);
}

async function empIniciarHUD(packingId) {
  try {
    // Cargar detalle completo de la tarea
    const t = await get(`/api/packing/${packingId}`);
    if (!t || !t.id) { alerta('Tarea no encontrada', 'error'); return; }

    // Bloquear si el picking aún no está completo
    if (t.picking_listo === false && t.estado === 'PENDIENTE') {
      alerta('El operario aún está pickeando — espere a que termine', 'advertencia');
      return;
    }

    EMP_TAREA = { ...t, id: packingId };

    // Mismo gate que ya aplican picking.js/conteo.js: si un admin le revocó
    // la cámara a este operario, el HUD de empaque debe respetarlo también
    // en vez de mostrar el botón igual.
    const _btnCam = document.getElementById('emp-btn-camara');
    if (_btnCam) _btnCam.style.display = (OPERARIO && OPERARIO.puede_usar_camara) ? '' : 'none';

    // Retry Siesa: bultos ya creados pero Siesa falló — reintentar directamente
    if (t.estado === 'VERIFICADO' && !t.siesa_triggered && t.bultos?.length) {
      await empReintentarSiesa(t);
      return;
    }

    // Iniciar si aún está PENDIENTE
    if (t.estado === 'PENDIENTE') {
      // Ajustar cantidades con lo que el picker realmente recogió (faltantes parciales)
      try { await post(`/api/packing/${packingId}/sincronizar-picking`, {}); } catch (e) { console.error('sincronizar-picking:', e); }
      try {
        await put(`/api/packing/${packingId}/iniciar`, {});
      } catch (e) {
        // Otro empacador ya tomó esta tarea (o cambió de estado) entre que
        // se cargó la lista y este clic -- no seguir: sin esto, el HUD se
        // activaba igual sobre una tarea que ya no es de este empacador,
        // y terminaba en "Exceso" infinito o un 403 al intentar cerrar caja.
        EMP_TAREA = null;
        alerta(e.message || 'Otro empacador ya tomó esta tarea — actualizando lista', 'advertencia');
        empCargarTareas();
        return;
      }
      // Re-cargar para obtener cantidades actualizadas tras el sync de picking
      const tFresh = await get(`/api/packing/${packingId}`);
      if (tFresh && tFresh.id) Object.assign(t, tFresh);
    }

    // Ítems pendientes de verificar van primero
    EMP_ITEMS = [...(t.items || [])].sort((a, b) => a.verificado - b.verificado);
    EMP_ITEM_IDX = EMP_ITEMS.findIndex(i => !i.verificado);
    if (EMP_ITEM_IDX < 0) EMP_ITEM_IDX = 0;

    // Poblar empaques directamente desde factor_conversion del producto (fuente de verdad)
    EMP_EMPAQUES = {};
    for (const item of EMP_ITEMS) {
      const fc = item.factor_conversion || 1;
      if (fc > 1) {
        EMP_EMPAQUES[item.producto_id] = {
          factor: fc,
          unidad: item.unidad_empaque || 'PIEZA'
        };
      }
    }

    empRenderHUDItem();
    document.getElementById('emp-hud').classList.add('activo');
    if (!/Mobi|Android|iPhone|iPad/i.test(navigator.userAgent)) {
      document.getElementById('scanner-input').focus();
    }
  } catch (e) { alerta('Error iniciando tarea', 'error'); }
}

/** Cancela el packing activo y libera la tarea. @param {number} packingId */
async function empCancelarPacking(packingId) {
  if (!(await _modalConfirmar('El pedido fue anulado en Siesa. La mercancía que ya fue pickeada debe devolverse a la ubicación o esperar el nuevo pedido.', { titulo: '¿Cancelar este packing?', peligro: true }))) return;
  try {
    await put(`/api/packing/${packingId}/cancelar`, { motivo: 'Pedido anulado en Siesa ERP — cancelado desde WMS' });
    alerta('Packing cancelado — avise al jefe de almacén para devolver la mercancía', 'info');
    empCargarTareas();
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}

/** Resetea flags Siesa de la tarea para reintento. @param {number} packingId */
async function empLimpiarSiesa(packingId) {
  if (!(await _modalConfirmar('¿Eliminar los bultos registrados y volver a declarar las piezas?'))) return;
  try {
    await post(`/api/packing/${packingId}/resetear-siesa`, {});
    alerta('Listo — declare las piezas de nuevo al abrir la tarea', 'exito');
    empCargarTareas();
  } catch (e) { alerta(e.message || 'Error de conexión', 'error'); }
}


/** Reintenta envío a Siesa para un packing fallido. @param {Object} t — tarea packing */
async function empReintentarSiesa(t) {
  // Los bultos ya existen — el backend los reutiliza, solo reintenta Siesa
  const bultoResumen = t.bultos.reduce((acc, b) => {
    acc[b.tipo] = (acc[b.tipo] || 0) + 1;
    return acc;
  }, {});
  const resumenTexto = Object.entries(bultoResumen).map(([tipo, n]) => `${n} ${tipo}`).join(', ');

  alerta(`Reintentando Siesa para ${t.numero_pedido_siesa} (${resumenTexto})…`, 'info');

  try {
    // bultos_data vacío — el backend detecta bultos existentes y solo reintenta Siesa
    const data = await post(`/api/packing/${t.id}/cerrar`, { bultos: t.bultos.map(b => ({ tipo: b.tipo, cantidad: 1 })) });
    empImprimirEtiquetas(data.bultos, {
      numero_pedido: data.numero_pedido,
      cliente: data.cliente,
      municipio: data.municipio
    });
    const m = empMensajeCierre(data, 200);
    alerta(`${t.numero_pedido_siesa}: ${m.texto}`, m.tipo);
    empCargarTareas();
  } catch (e) {
    const m = empMensajeCierre(e.body || { error: e.message }, e.status);
    alerta(m.texto, m.tipo);
    empCargarTareas();
  }
}


/** Cierra el HUD de empaque y vuelve a la lista de tareas. */
function empCerrarHUD() {
  document.getElementById('emp-hud').classList.remove('activo');
  EMP_TAREA = null;
  EMP_ITEMS = [];
  EMP_ITEM_IDX = 0;
  EMP_EMPAQUES = {};
  empCargarTareas();
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: renderizar ítem actual
// ─────────────────────────────────────────────────────────────

/** Renderiza el ítem actual en el HUD del empacador. */
function empRenderHUDItem() {
  if (!EMP_TAREA || !EMP_ITEMS.length) return;

  const verificados = EMP_ITEMS.filter(i => i.verificado).length;
  const total = EMP_ITEMS.length;
  const pendientes = EMP_ITEMS.filter(i => !i.verificado);
  const item = pendientes[0] || EMP_ITEMS[EMP_ITEM_IDX] || EMP_ITEMS[0];

  // Calcular display en piezas si el producto tiene empaque.
  // cantidad_esperada puede estar en UND (normalizado) o en unidades de empaque / PQ (legado).
  // Heurístico: si cantEsp > 0 && cantEsp < factor → está en PQ (no dividir).
  const emp = EMP_EMPAQUES[item.producto_id];
  const factor = emp ? emp.factor : 1;
  const unidad = emp ? emp.unidad : 'und';
  const cantReal = item.cantidad_real || 0;
  const cantEsp  = item.cantidad_esperada || 0;

  // esPQ: cantidad_esperada en unidades de empaque (legado pre-normalización)
  const esPQ = factor > 1 && cantEsp > 0 && cantEsp < factor;

  const piezasReal = esPQ
    ? cantReal                            // cantReal ya está en PQ
    : (factor > 1 ? Math.floor(cantReal / factor) : cantReal);
  const piezasEsp = esPQ
    ? cantEsp                             // cantEsp ya está en PQ
    : (factor > 1 ? Math.ceil(cantEsp / factor) : cantEsp);
  const sueltas = esPQ
    ? 0
    : (factor > 1 ? cantReal % factor : 0);

  document.getElementById('emp-hud-pedido').textContent = EMP_TAREA.numero_pedido_siesa;
  document.getElementById('emp-hud-producto').textContent = item.producto_nombre || item.producto_codigo || '—';

  const undEl = document.getElementById('emp-hud-und');
  if (factor > 1) {
    document.getElementById('emp-hud-contador').textContent = piezasReal;
    document.getElementById('emp-hud-de').textContent =
      `de ${piezasEsp} ${unidad}${sueltas > 0 ? ` (+${sueltas} sueltas)` : ''}`;
    // Línea secundaria: en UND para items normalizados, en PQ para legado
    if (undEl) undEl.textContent = esPQ
      ? `${cantReal * factor} / ${cantEsp * factor} und estimadas`
      : `${cantReal} / ${cantEsp} und`;
  } else {
    document.getElementById('emp-hud-contador').textContent = cantReal;
    document.getElementById('emp-hud-de').textContent = `de ${cantEsp}`;
    if (undEl) undEl.textContent = '';
  }
  document.getElementById('emp-hud-items').textContent = `${verificados} de ${total} ítems verificados`;

  const pct = total ? Math.round(verificados / total * 100) : 0;
  document.getElementById('emp-hud-barra').style.width = pct + '%';

  // Botón cerrar caja: solo visible si TODOS verificados
  const btn = document.getElementById('emp-btn-cerrar-caja');
  if (verificados === total && total > 0) {
    btn.style.display = 'block';
    btn.disabled = false;
    document.getElementById('emp-hud-producto').textContent = '¡Todo verificado! Cierre la caja.';
  } else {
    btn.style.display = 'none';
  }
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: procesar escaneo láser
// ─────────────────────────────────────────────────────────────

/**
 * Procesa escaneo en packing — valida producto vs item esperado, maneja empaques.
 * @param {string} codigo - Código escaneado
 */
async function empProcesarEscaneo(codigo) {
  if (!EMP_TAREA) return;

  // Resolución de código compartida con picking (ver resolverEscaneoEmpaque
  // en app.js) — si el operario escanea un DUN-14 (caja/paca), identifica el
  // producto real y la cantidad = factor (no 1) a enviar al backend.
  const scan = await resolverEscaneoEmpaque(codigo);

  if (scan.tipo === 'GS1_AMBIGUO') {
    _modalAmbiguedadPackingEmp(codigo, scan.ambiguos || []);
    return;
  }

  const { codigoParaBackend, cantidad: cantidadParaBackend, unidad } = scan;
  let etiquetaEmpaque = '';
  if (scan.tipo === 'LPN') {
    etiquetaEmpaque = `LPN — ${cantidadParaBackend} und`;
  } else if (cantidadParaBackend > 1) {
    etiquetaEmpaque = `${unidad || 'PIEZA'} completa — ${cantidadParaBackend} und`;
  }

  // total_acumulado: el backend YA soporta este modo idempotente para PACKING
  // (mobile_service.py, misma protección with_for_update que PICKING) — antes
  // packing.js nunca lo mandaba, así que quedaba afuera de la cola offline
  // que sí tiene picking (un +=, no un "fijar total", no es seguro de encolar
  // ni reintentar tras un timeout sin saber si el POST original ya llegó).
  const itemLocal = EMP_ITEMS.find(i => i.producto_codigo === codigoParaBackend);
  const totalAcumulado = itemLocal ? (itemLocal.cantidad_real || 0) + cantidadParaBackend : null;
  const payload = {
    accion: 'packing_escanear',
    tarea_id: EMP_TAREA.id,
    tipo: 'PACKING',
    codigo: codigoParaBackend,
    cantidad: cantidadParaBackend,
  };
  if (totalAcumulado !== null) payload.total_acumulado = totalAcumulado;

  try {
    const r = await postConReintento('/api/mobile/escanear', payload);

    if (r.error) {
      empFlash('rojo', r.error);
      return;
    }

    // Actualizar estado local del ítem
    const item = EMP_ITEMS.find(i =>
      (r.producto_id && i.producto_id === r.producto_id) ||
      (r.producto_codigo && i.producto_codigo === r.producto_codigo)
    );
    if (item) {
      item.cantidad_real = r.cantidad_actual;
      item.verificado = r.item_completado;
    }

    empFlash('verde', etiquetaEmpaque || null);

    // Si todos los ítems están listos
    if (r.todos_completados) {
      const detalle = await get(`/api/packing/${EMP_TAREA.id}`);
      EMP_ITEMS = detalle.items || EMP_ITEMS;
    }

    empRenderHUDItem();

  } catch (e) {
    if (e.status) {
      // Error del servidor — no encolar, el estado local no cambió
      empFlash('rojo', e.message && e.message !== '401' ? e.message : 'Error');
      return;
    }
    if (totalAcumulado !== null) {
      // Corte de red real y el scan es idempotente — encolar y confiar en
      // que aplicará (mismo criterio que picking.js): revertir aquí solo
      // invitaría a re-escanear el mismo código y duplicar contra la cola.
      guardarOffline(payload);
      if (itemLocal) itemLocal.cantidad_real = totalAcumulado;
      empFlash('verde', etiquetaEmpaque || null);
      empRenderHUDItem();
    } else {
      empFlash('rojo', 'Sin conexión — vuelva a escanear');
    }
  }
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: flash visual verde/rojo
// ─────────────────────────────────────────────────────────────

/**
 * Flash visual en el HUD de packing (verde=OK, rojo=error).
 * @param {'verde'|'rojo'|'amarillo'} color
 * @param {string} mensaje
 */
function empFlash(color, mensaje) {
  const flash = document.getElementById('emp-flash');
  const hud = document.getElementById('emp-hud');
  const esVerde = color === 'verde';

  flash.style.background = esVerde ? '#15803d' : '#991b1b';
  flash.style.opacity = '0.7';

  const msgEl = document.getElementById('emp-hud-producto');
  const prevText = msgEl ? msgEl.textContent : '';

  if (esVerde && mensaje && msgEl) {
    // Mostrar brevemente qué se registró (ej. "CAJA completa — 24 und").
    // Al expirar, re-renderizar el ítem ACTUAL (no restaurar prevText:
    // empRenderHUDItem ya pudo haber avanzado al siguiente producto).
    msgEl.style.color = 'var(--ok-tx)';
    msgEl.textContent = mensaje;
    setTimeout(() => {
      msgEl.style.color = '';
      empRenderHUDItem();   // ← ítem correcto, no el anterior
    }, 900);
  } else if (!esVerde && mensaje && msgEl) {
    hud.style.background = '#1a0000';
    msgEl.style.color = 'var(--err-tx)';
    msgEl.textContent = '⚠ ' + mensaje;
    setTimeout(() => {
      msgEl.style.color = '';
      msgEl.textContent = prevText;
      hud.style.background = '';
    }, 1800);
  }

  setTimeout(() => {
    flash.style.opacity = '0';
    if (esVerde) hud.style.background = '';
  }, esVerde ? 150 : 300);
}

// ─────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────
// EMPACADOR — Modal ambigüedad de empaque en packing
// ─────────────────────────────────────────────────────────────

/**
 * Modal de ambigüedad abierto: el código escaneado y sus empaques. El botón lleva
 * solo su POSICIÓN en `empaques`, no el dato: un código o una unidad dentro de
 * `onclick="fn('…')"` no se protege con `esc()` —el navegador decodifica `&#39;`
 * a `'` antes de correr el JS— y una comilla en el dato rompe la cadena. Ver
 * CLAUDE.md, «Todo dato que se pinta va con esc()».
 * @type {{codigo: string, empaques: Array<Object>}}
 */
let _AMBIGUEDAD_PACKING_EMP = { codigo: '', empaques: [] };

/** Modal de ambigüedad de empaque en packing. @param {string} codigo @param {Array} ambiguos */
function _modalAmbiguedadPackingEmp(codigo, ambiguos) {
  // ambiguos: array de ProductoEmpaque.to_dict()
  _AMBIGUEDAD_PACKING_EMP = { codigo, empaques: ambiguos.slice() };
  const opciones = ambiguos.map((e, i) => `
    <button onclick="_elegirEmpaqueAmbiguoPacking(${i}, this.closest('.modal-ambig-emp'))"
      style="width:100%;padding:16px;font-size:var(--fs-lg);font-weight:700;background:var(--bg-input);color:var(--tx);border:1px solid var(--brd);border-radius:12px;cursor:pointer;margin-bottom:8px;">
      ${esc(e.unidad_medida)} — ${esc(e.factor_conversion)} und
      <div style="font-size:var(--fs-xs);color:var(--tx3);font-weight:400;margin-top:2px;">${esc(e.producto_nombre || e.referencia_item || '')}</div>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-ambig-emp';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.85);display:flex;align-items:flex-end;';
  modal.innerHTML = `
    <div style="background:var(--bg-s);border-top:2px solid #7c3aed;border-radius:20px 20px 0 0;padding:24px;width:100%;max-height:70vh;overflow-y:auto;">
      <div style="font-size:var(--fs-md);font-weight:700;color:var(--lila-tx);margin-bottom:4px;">Código en múltiples empaques</div>
      <div style="font-size:var(--fs-sm);color:var(--tx3);margin-bottom:16px;">${esc(codigo)} — ¿Cuál está empacando?</div>
      ${opciones}
      <button onclick="this.closest('.modal-ambig-emp').remove()"
        style="width:100%;padding:12px;font-size:var(--fs-sm);background:var(--bg-s);color:var(--tx3);border:1px solid var(--brd);border-radius:10px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

/**
 * El botón del modal: busca el empaque por posición y sigue igual que antes.
 * `Number(...)`: el factor viajaba como literal numérico dentro del onclick, así
 * que llegaba como número; se conserva.
 * @param {number} i @param {HTMLElement} modal
 */
function _elegirEmpaqueAmbiguoPacking(i, modal) {
  const e = _AMBIGUEDAD_PACKING_EMP.empaques[i];
  if (!e) { if (modal) modal.remove(); return; }
  return _elegirEmpaquePacking(_AMBIGUEDAD_PACKING_EMP.codigo, e.producto_codigo || '',
                               Number(e.factor_conversion), e.unidad_medida, modal);
}

/** @param {string} codigoBarras @param {string} productoCodigo @param {number} factor @param {string} unidad @param {HTMLElement} modal */
async function _elegirEmpaquePacking(codigoBarras, productoCodigo, factor, unidad, modal) {
  if (modal) modal.remove();
  if (!EMP_TAREA) return;
  try {
    const r = await postConReintento('/api/mobile/escanear', {
      tarea_id: EMP_TAREA.id,
      tipo: 'PACKING',
      codigo: productoCodigo || codigoBarras,
      cantidad: factor
    });
    if (r.error) { empFlash('rojo', r.error); return; }
    const item = EMP_ITEMS.find(i => r.producto_id && i.producto_id === r.producto_id);
    if (item) { item.cantidad_real = r.cantidad_actual; item.verificado = r.item_completado; }
    empFlash('verde', `${unidad} — ${factor} und`);
    if (r.todos_completados) {
      const detalle = await get(`/api/packing/${EMP_TAREA.id}`);
      EMP_ITEMS = detalle.items || EMP_ITEMS;
    }
    empRenderHUDItem();
  } catch (e) { empFlash('rojo', e.message && e.message !== '401' ? e.message : 'Error de conexión'); }
}

// ─────────────────────────────────────────────────────────────
// EMPACADOR — HUD: confirmar packing → Siesa se dispara solo
// ─────────────────────────────────────────────────────────────

/** Confirma el packing completo → abre modal de bultos → cierra caja → Siesa DLQ. */
async function empConfirmarPacking() {
  if (!EMP_TAREA) return;
  const btn = document.getElementById('emp-btn-cerrar-caja');
  btn.disabled = true;
  btn.textContent = 'Verificando...';

  try {
    try {
      await put(`/api/packing/${EMP_TAREA.id}/confirmar`, { forzar: false });
    } catch (e) {
      if (e.status === 409 && e.body && e.body.diferencias) {
        const resumen = e.body.diferencias.map(d => `${d.producto}: esperado ${d.esperado}, real ${d.real}`).join('\n');
        if (await _modalConfirmar(resumen, { titulo: 'Hay diferencias en cantidades', textoConfirmar: 'Confirmar de todas formas', peligro: true })) {
          try {
            await put(`/api/packing/${EMP_TAREA.id}/confirmar`, { forzar: true });
          } catch (e2) {
            empFlash('rojo', e2.message || 'Error confirmando');
            btn.disabled = false; btn.textContent = 'Cerrar Caja ✓';
            return;
          }
        } else {
          btn.disabled = false; btn.textContent = 'Cerrar Caja ✓';
          return;
        }
      } else {
        empFlash('rojo', e.message || 'Error confirmando');
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
    const _btnConf = document.querySelector('#modal-bultos button[onclick="bultosConfirmar()"]');
    if (_btnConf) { _btnConf.disabled = false; _btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
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

/** Agrega una línea de bulto al modal de declaración. @param {string} tipo — Caja|Bolsa|Rollo|Plancha|Estiba */
function bultosAgregarLinea(tipo) {
  const existing = _BULTOS_LINEAS.find(l => l.tipo === tipo);
  if (existing) { existing.cantidad++; }
  else { _BULTOS_LINEAS.push({ tipo, cantidad: 1 }); }
  bultosRenderLineas();
}

/** Renderiza las líneas de bultos en el modal. */
function bultosRenderLineas() {
  const el = document.getElementById('modal-bultos-lineas');
  if (!el) return;
  if (!_BULTOS_LINEAS.length) {
    el.innerHTML = '<div style="color:var(--tx3);font-size:var(--fs-sm);text-align:center;padding:12px;">Agregue al menos una pieza ↑</div>';
    return;
  }
  el.innerHTML = _BULTOS_LINEAS.map((l, i) => `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
      <div style="flex:1;font-size:var(--fs-sm);font-weight:600;">${esc(l.tipo)}</div>
      <button onclick="bultosAjustarCantidad(${i},-1)" style="width:44px;height:44px;background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);border-radius:6px;cursor:pointer;font-size:var(--fs-lg);">−</button>
      <div style="min-width:28px;text-align:center;font-size:17px;font-weight:700;">${esc(l.cantidad)}</div>
      <button onclick="bultosAjustarCantidad(${i},1)" style="width:44px;height:44px;background:var(--bg-s2);border:1px solid var(--brd);color:var(--tx);border-radius:6px;cursor:pointer;font-size:var(--fs-lg);">+</button>
      <button onclick="bultosEliminarLinea(${i})" style="width:44px;height:44px;background:var(--bg-input);border:1px solid var(--brd);color:var(--err-tx);border-radius:6px;cursor:pointer;font-size:var(--fs-sm);">✕</button>
    </div>`).join('');
}

/** @param {number} idx @param {number} delta — +1 o -1 */
function bultosAjustarCantidad(idx, delta) {
  _BULTOS_LINEAS[idx].cantidad = Math.max(1, _BULTOS_LINEAS[idx].cantidad + delta);
  bultosRenderLineas();
}

/** Elimina una línea de bulto. @param {number} idx */
function bultosEliminarLinea(idx) {
  _BULTOS_LINEAS.splice(idx, 1);
  bultosRenderLineas();
}

/** Cancela el modal de bultos sin cerrar la caja. */
function bultosCancelar() {
  document.getElementById('modal-bultos').style.display = 'none';
  _BULTOS_LINEAS = [];
}

/** Confirma bultos → cierra caja → encola Siesa DLQ. */
async function bultosConfirmar() {
  const errEl = document.getElementById('modal-bultos-error');
  errEl.textContent = '';
  const total = _BULTOS_LINEAS.reduce((s, l) => s + l.cantidad, 0);
  if (!_BULTOS_LINEAS.length || total < 1) {
    errEl.textContent = 'Debe agregar al menos una pieza';
    return;
  }

  const btnConf = document.querySelector('#modal-bultos button[onclick="bultosConfirmar()"]');
  if (btnConf) { btnConf.disabled = true; btnConf.textContent = 'Cerrando...'; }

  const _abort = new AbortController();
  const _timeout = setTimeout(() => _abort.abort(), 45000);

  try {
    const r = await fetch(`/api/packing/${EMP_TAREA.id}/cerrar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ bultos: _BULTOS_LINEAS.map(l => ({ tipo: l.tipo, cantidad: l.cantidad })) }),
      signal: _abort.signal
    });
    clearTimeout(_timeout);
    const data = await r.json();

    if (!r.ok) {
      const m = empMensajeCierre(data, r.status);
      if (data.estado_cierre === 'RETENIDO_CARTERA') {
        // La caja quedó con sus piezas, esperando a cartera: no es un error
        // que el empacador pueda corregir reintentando.
        document.getElementById('modal-bultos').style.display = 'none';
        _BULTOS_LINEAS = [];
        document.getElementById('emp-hud')?.classList.remove('activo');
        EMP_TAREA = null;
        EMP_ITEMS = [];
        alerta(m.texto, m.tipo);
        empCargarTareas();
        return;
      }
      errEl.textContent = m.texto;
      if (btnConf) { btnConf.disabled = false; btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
      return;
    }

    const tareaId = EMP_TAREA.id;

    document.getElementById('modal-bultos').style.display = 'none';
    _BULTOS_LINEAS = [];

    _empPostCierreExitoso(data, tareaId);

  } catch (e) {
    // Corte de red real (o el timeout de 45s de arriba) — no reintentar solo,
    // encolar el cierre y bloquear la pantalla: el bulto no puede moverse a
    // despacho sin su etiqueta impresa, así que el empacador debe esperar
    // aquí a que sincronice en vez de seguir con la siguiente tarea.
    clearTimeout(_timeout);
    guardarOffline({
      accion: 'cerrar_packing',
      tarea_id: EMP_TAREA.id,
      bultos: _BULTOS_LINEAS.map(l => ({ tipo: l.tipo, cantidad: l.cantidad })),
    });
    localStorage.setItem('wms_emp_bloqueado', JSON.stringify({
      tarea_id: EMP_TAREA.id,
      numero_pedido: EMP_TAREA.numero_pedido_siesa,
      cliente: EMP_TAREA.cliente,
      municipio: EMP_TAREA.municipio,
    }));
    document.getElementById('modal-bultos').style.display = 'none';
    _BULTOS_LINEAS = [];
    _empMostrarBloqueoOffline();
  }
}

/**
 * Acciones comunes tras un cierre de packing exitoso — ya sea inmediato (respuesta
 * directa de `/api/packing/<id>/cerrar`) o diferido (sincronizado offline vía
 * `onSync_cerrar_packing`). Debe quedar idéntico en ambos casos.
 * @param {Object} data - Respuesta de cierre (mismo shape en ambas vías).
 * @param {number} tareaId
 */
function _empPostCierreExitoso(data, tareaId) {
  empImprimirEtiquetas(data.bultos, {
    numero_pedido: data.numero_pedido,
    cliente: data.cliente,
    municipio: data.municipio
  });
  document.getElementById('emp-hud')?.classList.remove('activo');
  EMP_TAREA = null;
  EMP_ITEMS = [];
  const _m = empMensajeCierre(data, 200);
  alerta(_m.texto, _m.tipo);
  empCargarTareas();
  // Factura solo para empacadores NB1 con tareas PD — los packer_traslado
  // cierran traslados (numero_pedido=null) y no generan factura/remisión.
  const _esPacTras = OPERARIO && ['packer_traslado', 'picker_traslado'].includes(OPERARIO.rol);
  if (data.numero_pedido && !_esPacTras) empMostrarBotonFactura(tareaId, data.numero_pedido);
}

// ─────────────────────────────────────────────────────────────
// BLOQUEO OFFLINE — pantalla de espera mientras un cierre de caja
// queda pendiente de sincronizar (ver bultosConfirmar / syncOffline en app.js)
// ─────────────────────────────────────────────────────────────

let _EMP_BLOQUEO_TIMER = null;

/** @returns {{tarea_id:number, numero_pedido:string, cliente:string, municipio:string}|null} */
function _empBloqueoOfflineInfo() {
  try {
    const raw = localStorage.getItem('wms_emp_bloqueado');
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}

/** Muestra la pantalla de bloqueo — no se puede seguir empacando hasta que sincronice. */
function _empMostrarBloqueoOffline() {
  const info = _empBloqueoOfflineInfo();
  if (!info || document.getElementById('emp-bloqueo-offline')) return;
  const overlay = document.createElement('div');
  overlay.id = 'emp-bloqueo-offline';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:var(--bg-s);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:30px;text-align:center;';
  overlay.innerHTML = `
    <div style="font-size:56px;">📡</div>
    <div style="font-size:20px;font-weight:900;color:var(--warn-tx);">Cierre pendiente de conexión</div>
    <div style="font-size:var(--fs-md);color:var(--tx2);line-height:1.6;max-width:320px;">
      Pedido ${esc(info.numero_pedido || '—')} · ${esc(info.cliente || '')}<br><br>
      No mueva esta pieza. Se cierra y la etiqueta se imprime sola apenas vuelva la señal.
    </div>
    <div style="font-size:var(--fs-sm);color:var(--tx3);">Reintentando automáticamente…</div>`;
  document.body.appendChild(overlay);
  // navigator.onLine no siempre avisa a tiempo (ver rutas.js) — reintentar también por polling.
  if (!_EMP_BLOQUEO_TIMER) _EMP_BLOQUEO_TIMER = setInterval(() => { if (navigator.onLine) syncOffline(); }, 8000);
}

/** Quita la pantalla de bloqueo y detiene el polling de reintento. */
function _empOcultarBloqueoOffline() {
  const overlay = document.getElementById('emp-bloqueo-offline');
  if (overlay) overlay.remove();
  if (_EMP_BLOQUEO_TIMER) { clearInterval(_EMP_BLOQUEO_TIMER); _EMP_BLOQUEO_TIMER = null; }
}

/**
 * Callback invocado por `syncOffline()` (app.js) cuando un cierre de packing
 * encolado offline por fin sincronizó con éxito.
 * @param {Object} resultado - Mismo shape que la respuesta de `/api/packing/<id>/cerrar`.
 */
function onSync_cerrar_packing(resultado) {
  const info = _empBloqueoOfflineInfo();
  localStorage.removeItem('wms_emp_bloqueado');
  _empOcultarBloqueoOffline();
  _empPostCierreExitoso(resultado, info ? info.tarea_id : null);
}

/**
 * El servidor decidió que ese cierre encolado no sale (retenido por cartera,
 * sin permiso): la pantalla de espera se levanta y se dice por qué. Antes se
 * quedaba en «Reintentando automáticamente…» para siempre.
 * @param {Object} entrada - fila de `resultados` de `/api/mobile/sync`
 */
function onSyncRechazo_cerrar_packing(entrada) {
  localStorage.removeItem('wms_emp_bloqueado');
  _empOcultarBloqueoOffline();
  const m = empMensajeCierre({ error: entrada.error, estado_cierre: entrada.estado_cierre },
                             entrada.estado_cierre === 'RETENIDO_CARTERA' ? 409 : 400);
  alerta(m.texto, m.tipo);
  empCargarTareas();
}

/** El cierre encolado sigue sin salir, y se dice por qué (Siesa caído). */
function onSyncPendiente_cerrar_packing(entrada) {
  const overlay = document.getElementById('emp-bloqueo-offline');
  if (!overlay) return;
  let aviso = overlay.querySelector('#emp-bloqueo-motivo');
  if (!aviso) {
    aviso = document.createElement('div');
    aviso.id = 'emp-bloqueo-motivo';
    aviso.style.cssText = 'font-size:var(--fs-sm);color:var(--err-tx);font-weight:700;max-width:320px;';
    overlay.appendChild(aviso);
  }
  aviso.textContent = empMensajeCierre({ error: entrada.error, estado_cierre: entrada.estado_cierre }, 503).texto;
}

/**
 * Lo que el empacador lee al cerrar una caja. **Una función** para las tres
 * vías (cierre directo, reintento, cola offline) — antes cada una decía lo
 * suyo, y dos decían «Siesa procesó la factura» cuando solo se había encolado.
 *
 * · 2xx + `estado_siesa: CONFIRMADO` → Siesa ya confirmó la remisión.
 * · 2xx + `EN_COLA` (o sin el campo)  → quedó EN COLA; no se afirma nada más.
 * · `estado_cierre: RETENIDO_CARTERA` → «Retenido por cartera» con el motivo
 *   del servidor. No es un error de Siesa ni del empaque.
 * · `estado_cierre: SIESA_NO_DISPONIBLE` → «Siesa no está disponible: no se
 *   puede facturar», nunca «procesado».
 * · Cualquier otro error: el texto del servidor.
 * @param {Object} data - cuerpo de la respuesta
 * @param {number} status - código HTTP (200 si fue bien)
 * @returns {{tipo: string, texto: string}}
 */
function empMensajeCierre(data, status) {
  const d = data || {};
  if (status >= 200 && status < 300) {
    const n = Array.isArray(d.bultos) ? d.bultos.length : 0;
    if (d.estado_siesa === 'CONFIRMADO') {
      return { tipo: 'exito', texto: `${n} pieza(s) registradas — Siesa confirmó la remisión` };
    }
    return { tipo: 'exito', texto: `${n} pieza(s) registradas — la factura quedó en cola para Siesa; se confirma en unos segundos` };
  }
  if (d.estado_cierre === 'RETENIDO_CARTERA') {
    const motivo = String(d.error || '').replace(/^Retenido por cartera:\s*/i, '');
    return { tipo: 'advertencia',
             texto: `Retenido por cartera: ${motivo || 'sin detalle'} La caja quedó con sus piezas; se cierra cuando cartera la libere.` };
  }
  if (d.estado_cierre === 'SIESA_NO_DISPONIBLE') {
    const texto = String(d.error || '');
    return { tipo: 'error',
             texto: /^Siesa no está disponible/.test(texto) ? texto
                    : `Siesa no está disponible: no se puede facturar. ${texto}`.trim() };
  }
  return { tipo: 'error', texto: d.error || `No se pudo cerrar la caja (error ${status || 'de conexión'})` };
}

// ─────────────────────────────────────────────────────────────
// FACTURA — botón flotante post-cierre e impresión con JWT
// ─────────────────────────────────────────────────────────────

/** Muestra botón flotante para imprimir factura. @param {number} packingId @param {string} numeroPedido */
function empMostrarBotonFactura(packingId, numeroPedido) {
  const existing = document.getElementById('btn-remision-flotante');
  if (existing) existing.remove();

  const div = document.createElement('div');
  div.id = 'btn-remision-flotante';
  div.style.cssText = 'position:fixed;bottom:90px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;flex-direction:column;align-items:center;gap:8px;';
  div.innerHTML = `
    <div style="background:#14532d;border:1px solid #15803d;color:#bbf7d0;font-size:var(--fs-xs);font-weight:600;padding:6px 14px;border-radius:20px;text-align:center;">
      Pedido ${numeroPedido || ''} cerrado
    </div>
    <button onclick="empImprimirFactura(${packingId})"
      style="background:#15803d;color:#fff;border:none;border-radius:12px;padding:14px 28px;font-size:var(--fs-md);font-weight:700;cursor:pointer;box-shadow:0 4px 20px rgba(0,0,0,0.5);">
      🖨 Imprimir Factura
    </button>
    <button onclick="document.getElementById('btn-remision-flotante').remove()"
      style="background:transparent;color:var(--tx3);border:none;font-size:var(--fs-xs);cursor:pointer;padding:4px;">
      Cerrar
    </button>`;
  document.body.appendChild(div);
}

/** Imprime factura electrónica en ventana emergente. @param {number} packingId */
async function empImprimirFactura(packingId) {
  // Usa `imprimirDocumento` de app.js: era el mismo bloque copiado.
  await imprimirDocumento(`/api/packing/${packingId}/factura`, 'la factura');
}

// ─────────────────────────────────────────────────────────────
// ETIQUETA LPN — imprime la etiqueta de una paca/caja física
// Se llama desde recepción (manual y DUN-14) y desde picking
// (lazy labeling de inventario heredado sin etiqueta).
// ─────────────────────────────────────────────────────────────

/**
 * Imprime etiqueta de LPN (paca/caja física) con código de barras.
 * @param {{codigo: string, cantidad_actual: number, factor_conversion: number}} lpn
 * @param {string} productoNombre
 */
function imprimirEtiquetaLPN(lpn, productoNombre) {
  if (!puedeImprimirEtiquetas('etiquetas de paca/caja')) return;
  const area = document.getElementById('print-area');
  if (!area) return;

  const hoy = new Date().toLocaleDateString('es-CO');
  const uid = `lpn-bc-${lpn.id || Date.now()}`;

  area.innerHTML = `
    <div class="etiqueta-lpn">
      <div class="el-titulo">BODEGA — PACA / CAJA</div>
      <svg id="${uid}"></svg>
      <div class="el-codigo">${esc(lpn.codigo)}</div>
      <div class="el-producto">${productoNombre || lpn.producto_nombre || ''}</div>
      <div class="el-cantidad">${esc(lpn.cantidad_actual)} UND</div>
      <div class="el-fecha">${hoy}</div>
    </div>`;

  pintarCodigoBarras(`#${uid}`, lpn.codigo);

  setTimeout(() => {
    window.print();
    setTimeout(() => { area.innerHTML = ''; }, 1000);
  }, 300);
}

/** Imprime etiquetas de bultos y caja. @param {Array} bultos @param {Object} meta */
function empImprimirEtiquetas(bultos, meta) {
  if (!bultos?.length) return;
  // Es la más cara de las cuatro: el código del bulto es contra lo que el
  // cliente firma en la entrega. Sin barra, esa cuenta se hace de memoria.
  if (!puedeImprimirEtiquetas('etiquetas de bulto')) return;

  const area = document.getElementById('print-area');
  if (!area) return;

  area.innerHTML = bultos.map(b => `
    <div class="etiqueta-print">
      <div class="ep-pedido">${esc(meta.numero_pedido || '')}</div>
      <div class="ep-cliente">${esc(meta.cliente || '')}</div>
      <div class="ep-municipio">${esc(meta.municipio || '')}</div>
      <svg id="bc-${esc(b.id)}"></svg>
      <div class="ep-codigo">${esc(b.codigo_barras)}</div>
      <div class="ep-pieza">${esc(b.tipo)} ${esc(b.numero)} de ${esc(b.total)}</div>
    </div>`).join('');

  // Renderizar códigos de barras antes de imprimir
  bultos.forEach(b => pintarCodigoBarras(`#bc-${b.id}`, b.codigo_barras,
                                         { height: 50 }));

  setTimeout(() => {
    window.print();
    // Limpiar después de imprimir
    setTimeout(() => { area.innerHTML = ''; }, 1000);
  }, 300);
}

