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
    el.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px;">Error cargando tareas</div>';
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
    el.innerHTML = `<div style="text-align:center;padding:60px 20px;color:#555;">
      Sin tareas de empaque pendientes ✓<br>
      <button onclick="_refreshBtn(event, empCargarTareas)" style="margin-top:20px;background:#222;border:1px solid #333;color:#fff;padding:10px 20px;border-radius:10px;cursor:pointer;">↻ Actualizar</button>
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
      titulo.style.color = '#c2410c';
    } else if (hayPedidos && !hayTraslados) {
      titulo.textContent = '🛒 Packing Pedido';
      titulo.style.color = '#1d4ed8';
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
        style="flex:1;padding:9px 6px;border-radius:8px;border:1px solid ${activo ? colorActivo : '#333'};background:${activo ? colorActivo : 'transparent'};color:${activo ? '#fff' : '#aaa'};font-size:12px;font-weight:700;cursor:pointer;transition:.15s;">
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
    el.innerHTML = `${tabsHtml}<div style="text-align:center;padding:40px 20px;color:#555;">Sin tareas en esta pestaña</div>`;
    return;
  }

  el.innerHTML = `
      ${tabsHtml}
      <div style="font-size:12px;font-weight:600;color:#aaa;padding:4px 0 12px;">TAREAS DE EMPAQUE</div>
      ${tareas.map(t => {
        const verificados = t.items_verificados || 0;
        const total = t.total_items || 0;
        const pct = total ? Math.round(verificados / total * 100) : 0;
        const pickingListo = t.picking_listo !== false;
        const pedidoAnulado = t.pedido_anulado_siesa === true;
        const _puedeCancelarPacking = OPERARIO && ['admin', 'supervisor'].includes(OPERARIO.rol);
        const siesaFallo = t.estado === 'VERIFICADO' && !t.siesa_triggered && !pedidoAnulado;
        const enProceso = t.estado === 'EN_PROCESO';
        const bloqueado = (!pickingListo && t.estado === 'PENDIENTE') || pedidoAnulado;
        // Una tarea BLOQUEADA (backorder Siesa, faltante, avería...) no se
        // resuelve pickeando — la resuelve un admin en Bodega → Auditoría.
        // "Esperando picking" ahí manda al empacador a esperar algo que
        // nunca va a pasar solo.
        const enAuditoria = bloqueado && !pedidoAnulado && t.picking_bloqueado === true;
        const color = pedidoAnulado ? 'var(--red)' : enAuditoria ? '#c084fc' : bloqueado ? '#6b7280' : siesaFallo ? '#fca5a5' : enProceso ? '#93c5fd' : '#facc15';
        const bg    = pedidoAnulado ? 'var(--rbg)' : enAuditoria ? '#2e1065' : bloqueado ? '#1a1a1a'  : siesaFallo ? '#7f1d1d'  : enProceso ? '#1e3a5f' : '#713f12';
        const label = pedidoAnulado ? '🚫 PEDIDO ANULADO EN SIESA' : enAuditoria ? '🔍 En auditoría' : bloqueado ? 'Esperando picking' : siesaFallo ? '⚠ Reintentar Siesa' : enProceso ? 'En proceso' : 'Pendiente';
        const anulado_banner = pedidoAnulado ? `
          <div style="margin-top:10px;background:var(--rbg);border:1px solid var(--rbrd);border-radius:8px;padding:10px 12px;">
            <div style="font-size:12px;font-weight:700;color:var(--red);margin-bottom:4px;">🚫 Pedido anulado en Siesa (estado ${t.pedido_estado_siesa_detectado || '9'})</div>
            <div style="font-size:11px;color:var(--tx2);line-height:1.4;">
              El área comercial anuló este pedido en el ERP.<br>
              <strong>Acción:</strong> ${_puedeCancelarPacking ? 'Cancelar este packing y esperar el nuevo pedido clonado.' : 'Avisa a tu supervisor para que cancele este packing.'}
            </div>
            ${_puedeCancelarPacking ? `
            <button onclick="event.stopPropagation();empCancelarPacking(${t.id})"
              style="margin-top:8px;width:100%;padding:8px;background:var(--red);border:none;color:#fff;border-radius:8px;cursor:pointer;font-size:12px;font-weight:700;">
              Cancelar packing
            </button>` : ''}
          </div>` : '';
        const limpiarBtn = siesaFallo ? `
          <button onclick="event.stopPropagation();empLimpiarSiesa(${t.id})"
            style="margin-top:8px;width:100%;padding:8px;background:#1a1a1a;border:1px solid #444;color:#aaa;border-radius:8px;cursor:pointer;font-size:12px;">
            🗑 Limpiar bultos y redeclarar piezas
          </button>` : '';
        const esTraslado = t.tipo_documento === 'TRASLADO';
        const refDisplay = t.referencia_doc || t.numero_pedido_siesa || '—';
        const etiquetaHtml = esTraslado
          ? `<span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;background:#431407;color:#fb923c;letter-spacing:.5px;margin-left:8px;">TRASLADO</span>`
          : `<span style="font-size:10px;font-weight:700;padding:2px 8px;border-radius:10px;background:#1e3a5f;color:#93c5fd;letter-spacing:.5px;margin-left:8px;">PEDIDO</span>`;
        const destinoHtml = esTraslado && t.tienda_destino
          ? `<div style="font-size:11px;color:#fb923c;margin-top:2px;">→ ${t.tienda_destino}</div>` : '';
        // Franja lateral: naranja = traslado, azul = pedido — mismo lenguaje de color del badge,
        // solo se omite si la tarjeta ya tiene su propio borde de error (pedido anulado).
        const acentoLateral = `border-left:4px solid ${esTraslado ? '#c2410c' : '#1d4ed8'};`;
        return `
        <div class="emp-task-card" onclick="${(bloqueado || pedidoAnulado) ? '' : `empIniciarHUD(${t.id})`}"
          style="${(bloqueado || pedidoAnulado) ? 'cursor:default;' : 'cursor:pointer;'}${pedidoAnulado ? 'border:2px solid var(--red);' : acentoLateral}">
          <div class="emp-task-pedido" style="display:flex;align-items:center;">${refDisplay}${etiquetaHtml}</div>
          ${destinoHtml}
          <div class="emp-task-sub">${total} producto(s) · ${t.items_verificados || 0}/${total} verificados</div>
          ${total > 0 ? `<div style="margin-top:10px;background:#1a1a1a;border-radius:8px;height:6px;overflow:hidden;">
            <div style="height:100%;background:#4ade80;width:${pct}%;border-radius:8px;transition:width 0.3s;"></div>
          </div>` : ''}
          <span class="emp-task-badge" style="background:${bg};color:${color};">${label}</span>
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
      // Ajustar cantidades con lo que el picker realmente recogió (faltantes parciales)
      await fetch(`/api/packing/${packingId}/sincronizar-picking`, {
        method: 'POST',
        headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }
      });
      await fetch(`/api/packing/${packingId}/iniciar`, {
        method: 'PUT',
        headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }
      });
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
  if (!confirm('¿Cancelar este packing? El pedido fue anulado en Siesa. La mercancía que ya fue pickeada debe devolverse a la ubicación o esperar el nuevo pedido.')) return;
  try {
    const r = await fetch(`/api/packing/${packingId}/cancelar`, {
      method: 'PUT',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo: 'Pedido anulado en Siesa ERP — cancelado desde WMS' })
    });
    const data = await r.json();
    if (!r.ok) { alerta(data.error || 'Error al cancelar', 'error'); return; }
    alerta('Packing cancelado — avisa al jefe de almacén para devolver la mercancía', 'info');
    empCargarTareas();
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Resetea flags Siesa de la tarea para reintento. @param {number} packingId */
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
    alerta(`${t.numero_pedido_siesa} despachado — Siesa procesó la factura`, 'exito');
    empCargarTareas();
  } catch (e) {
    alerta('Error de conexión al reintentar Siesa', 'error');
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
    document.getElementById('emp-hud-producto').textContent = '¡Todo verificado! Cierra la caja.';
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

  // ── Resolver empaque antes de registrar ───────────────────────────────────
  // Si el operario escanea un DUN-14 (caja/paca), necesitamos:
  //   1. Identificar el producto real (no el barcode de la caja)
  //   2. Enviar cantidad = factor (no 1) al backend
  let codigoParaBackend = codigo;
  let cantidadParaBackend = 1;
  let etiquetaEmpaque = '';

  try {
    const scan = await get(`/api/empaques/scan/${encodeURIComponent(codigo)}`);
    const tipo = scan.tipo || 'NO_ENCONTRADO';

    if (tipo === 'GS1_UNICO' && scan.producto && scan.factor > 1) {
      // Barcode de empaque → enviar código de producto y factor como cantidad
      codigoParaBackend = scan.producto.codigo;
      cantidadParaBackend = scan.factor;
      etiquetaEmpaque = `${scan.empaque?.unidad_medida || 'PIEZA'} completa — ${scan.factor} und`;
    } else if (tipo === 'LPN' && scan.producto) {
      codigoParaBackend = scan.producto.codigo;
      cantidadParaBackend = scan.factor || 1;
      etiquetaEmpaque = `LPN — ${cantidadParaBackend} und`;
    } else if (tipo === 'GS1_AMBIGUO') {
      _modalAmbiguedadPackingEmp(codigo, scan.ambiguos || []);
      return;
    }
    // EAN_BASE o NO_ENCONTRADO → flujo original (codigoParaBackend = codigo, cantidad = 1)
  } catch (_) {
    // Si /api/empaques/scan falla, continuar con flujo original
  }

  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: EMP_TAREA.id,
      tipo: 'PACKING',
      codigo: codigoParaBackend,
      cantidad: cantidadParaBackend
    });

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
    empFlash('rojo', e.message && e.message !== '401' ? e.message : 'Error de conexión');
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
    msgEl.style.color = '#4ade80';
    msgEl.textContent = mensaje;
    setTimeout(() => {
      msgEl.style.color = '';
      empRenderHUDItem();   // ← ítem correcto, no el anterior
    }, 900);
  } else if (!esVerde && mensaje && msgEl) {
    hud.style.background = '#1a0000';
    msgEl.style.color = '#f87171';
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

/** Modal de ambigüedad de empaque en packing. @param {string} codigo @param {Array} ambiguos */
function _modalAmbiguedadPackingEmp(codigo, ambiguos) {
  // ambiguos: array de ProductoEmpaque.to_dict()
  const opciones = ambiguos.map(e => `
    <button onclick="_elegirEmpaquePacking('${codigo}', '${e.producto_codigo || ''}', ${e.factor_conversion}, '${e.unidad_medida}', this.closest('.modal-ambig-emp'))"
      style="width:100%;padding:16px;font-size:18px;font-weight:700;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:12px;cursor:pointer;margin-bottom:8px;">
      ${e.unidad_medida} — ${e.factor_conversion} und
      <div style="font-size:12px;color:#666;font-weight:400;margin-top:2px;">${e.producto_nombre || e.referencia_item || ''}</div>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-ambig-emp';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.85);display:flex;align-items:flex-end;';
  modal.innerHTML = `
    <div style="background:#0a0a0a;border-top:2px solid #7c3aed;border-radius:20px 20px 0 0;padding:24px;width:100%;max-height:70vh;overflow-y:auto;">
      <div style="font-size:16px;font-weight:700;color:#a78bfa;margin-bottom:4px;">Código en múltiples empaques</div>
      <div style="font-size:13px;color:#666;margin-bottom:16px;">${codigo} — ¿Cuál estás empacando?</div>
      ${opciones}
      <button onclick="this.closest('.modal-ambig-emp').remove()"
        style="width:100%;padding:12px;font-size:14px;background:#111;color:#666;border:1px solid #222;border-radius:10px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

/** @param {string} codigoBarras @param {string} productoCodigo @param {number} factor @param {string} unidad @param {HTMLElement} modal */
async function _elegirEmpaquePacking(codigoBarras, productoCodigo, factor, unidad, modal) {
  if (modal) modal.remove();
  if (!EMP_TAREA) return;
  try {
    const r = await post('/api/mobile/escanear', {
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
    errEl.textContent = 'Debes agregar al menos una pieza';
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
      errEl.textContent = data.error || 'Error al cerrar';
      if (btnConf) { btnConf.disabled = false; btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
      return;
    }

    const tareaId = EMP_TAREA.id;

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
    alerta(`${data.bultos.length} pieza(s) registradas — Siesa procesó la factura`, 'exito');
    empCargarTareas();
    // Factura solo para empacadores NB1 con tareas PD — los packer_traslado
    // cierran traslados (numero_pedido=null) y no generan factura/remisión.
    const _esPacTras = OPERARIO && ['packer_traslado','picker_traslado'].includes(OPERARIO.rol);
    if (data.numero_pedido && !_esPacTras) empMostrarBotonFactura(tareaId, data.numero_pedido);

  } catch (e) {
    errEl.textContent = 'Error de conexión';
    if (btnConf) { btnConf.disabled = false; btnConf.textContent = 'Cerrar Caja y Etiquetar →'; }
  }
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
    <div style="background:#14532d;border:1px solid #16a34a;color:#bbf7d0;font-size:11px;font-weight:600;padding:6px 14px;border-radius:20px;text-align:center;">
      Pedido ${numeroPedido || ''} cerrado
    </div>
    <button onclick="empImprimirFactura(${packingId})"
      style="background:#16a34a;color:#fff;border:none;border-radius:12px;padding:14px 28px;font-size:15px;font-weight:700;cursor:pointer;box-shadow:0 4px 20px rgba(0,0,0,0.5);">
      🖨 Imprimir Factura
    </button>
    <button onclick="document.getElementById('btn-remision-flotante').remove()"
      style="background:transparent;color:#6b7280;border:none;font-size:12px;cursor:pointer;padding:4px;">
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
      <div class="el-codigo">${lpn.codigo}</div>
      <div class="el-producto">${productoNombre || lpn.producto_nombre || ''}</div>
      <div class="el-cantidad">${lpn.cantidad_actual} UND</div>
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
      <div class="ep-pedido">${meta.numero_pedido || ''}</div>
      <div class="ep-cliente">${meta.cliente || ''}</div>
      <div class="ep-municipio">${meta.municipio || ''}</div>
      <svg id="bc-${b.id}"></svg>
      <div class="ep-codigo">${b.codigo_barras}</div>
      <div class="ep-pieza">${b.tipo} ${b.numero} de ${b.total}</div>
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

