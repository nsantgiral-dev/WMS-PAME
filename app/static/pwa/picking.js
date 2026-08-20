// ══════════════════════════════════════════════════════════════════
// PICKING OPERARIO — pedirTarea, escaneo, confirmar, faltantes, LPN
// Dependencias globales (de app.js): get(), post(), alerta(), flash(),
//   vibrar(), beepOk(), beepError(), beepDone(), pantalla(), set(),
//   abrirCamara(), cerrarCamara(), TOKEN, OPERARIO, ALMACEN_ID, API,
//   TAREA_ACTUAL, RECEPCION_ACTUAL, DEVOLUCION_ACTUAL, _refreshBtn()
// Dependencias cross-module:
//   packing.js: empProcesarEscaneo(), imprimirEtiquetaLPN()
//   recepcion.js: procesarScanRecepcion(), procesarScanDevolucion()
// ══════════════════════════════════════════════════════════════════

/** Solicita la siguiente tarea al dispensador automático. Asigna TAREA_ACTUAL. */
async function pedirTarea() {
  try {
    const d = await get('/api/mobile/tarea-actual');
    if (d && d.avisos_pendientes && d.avisos_pendientes.length) {
      d.avisos_pendientes.forEach(a => alerta(a.mensaje, a.tipo || 'advertencia'));
    }
    if (!d || d.sin_tareas) {
      TAREA_ACTUAL = null;
      const _esTiendaOp = OPERARIO && ['picker_traslado', 'packer_traslado'].includes(OPERARIO.rol);
      document.getElementById('contenido-tarea').innerHTML = _esTiendaOp ? `
        <div style="text-align:center;padding:40px 20px 16px;">
          <div style="font-size:60px;">📦</div>
          <div style="font-size:22px;font-weight:700;margin-top:12px;">No hay tareas pendientes</div>
          <div style="font-size:14px;color:#666;margin-top:6px;">Serás asignado automáticamente cuando haya traslados o conteos disponibles</div>
          <div style="display:flex;gap:10px;justify-content:center;margin-top:14px;">
            <span style="font-size:11px;padding:3px 10px;border-radius:10px;background:#431407;color:#fb923c;font-weight:700;">TRASLADO</span>
            <span style="font-size:11px;padding:3px 10px;border-radius:10px;background:#78350f;color:#fcd34d;font-weight:700;">CONTEO</span>
          </div>
        </div>` : `
        <div style="text-align:center;padding:40px 20px 16px;">
          <div style="font-size:60px;">✓</div>
          <div style="font-size:24px;font-weight:700;margin-top:12px;">Sin tareas pendientes</div>
          <div style="font-size:14px;color:#666;margin-top:6px;">El sistema te asignará la próxima automáticamente</div>
          <div style="display:flex;gap:10px;justify-content:center;margin-top:14px;">
            <span style="font-size:11px;padding:3px 10px;border-radius:10px;background:#1e3a5f;color:#93c5fd;font-weight:700;">PEDIDO</span>
            <span style="font-size:11px;padding:3px 10px;border-radius:10px;background:#431407;color:#fb923c;font-weight:700;">TRASLADO</span>
          </div>
        </div>`;
      return;
    }
    TAREA_ACTUAL = d;
    renderTarea(d);
  } catch (e) {
    console.error('Error cargando tarea:', e);
    const el = document.getElementById('contenido-tarea');
    if (el) el.innerHTML = `
      <div style="text-align:center;padding:40px 20px;">
        <div style="font-size:40px;">⚠️</div>
        <div style="font-size:16px;font-weight:700;color:#f87171;margin-top:12px;">Error al cargar tareas</div>
        <div style="font-size:13px;color:#666;margin-top:6px;">${e.message || 'Error de conexión'}</div>
        <button onclick="pedirTarea()" style="margin-top:16px;padding:12px 24px;background:#1e3a5f;color:#93c5fd;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">
          🔄 Reintentar
        </button>
      </div>`;
  }
}


/**
 * Renderiza la tarea activa en el HUD del operario.
 * @param {{tipo: string, producto_codigo: string, producto_nombre: string, cantidad_requerida: number, cantidad_escaneada: number, ubicacion: string, referencia: string, lote: string, factor_conversion: number, unidad_empaque: string, empaques_escaneados: number, conteo_intercalado: Object|null}} t
 */
function renderTarea(t) {
  _pickingTotal = t.cantidad_escaneada || 0;
  const colores = { PICKING: '#1d4ed8', PACKING: '#7c3aed', CONTEO: '#b45309' };
  const color = colores[t.tipo] || '#333';
  const esConteo = t.tipo === 'CONTEO';
  const esPicking = t.tipo === 'PICKING';
  const pct = t.cantidad_requerida ? Math.min((t.cantidad_escaneada / t.cantidad_requerida) * 100, 100) : 0;
  const puedeCamara = OPERARIO && OPERARIO.puede_usar_camara;

  // Empaque
  const factor       = t.factor_conversion || 1;
  const tieneEmpaque = esPicking && factor > 1;
  const unidadLabel  = (t.unidad_empaque || 'PKG').toUpperCase();
  const pkgs         = t.empaques_escaneados || 0;
  const unds         = t.cantidad_escaneada || 0;
  const req          = t.cantidad_requerida || 0;
  const pkgsReq      = factor > 1 ? Math.ceil(req / factor) : req;
  const sueltas      = factor > 1 ? unds % factor : 0;

  const htmlContador = tieneEmpaque
    ? `<div style="background:#1a1a1a;border-radius:16px;padding:16px 20px;margin-bottom:12px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;">
          <div style="font-size:12px;color:#555;padding-top:6px;letter-spacing:1px;">CANTIDAD</div>
          <div style="text-align:right;">
            <div id="contador-pkg" style="font-size:80px;font-weight:900;color:#22c55e;line-height:1;">${pkgs}</div>
            <div id="contador-und" style="font-size:20px;font-weight:700;color:#22c55e;margin-top:2px;">de ${pkgsReq} ${unidadLabel}${sueltas > 0 ? ` +${sueltas} und` : ''}</div>
            <div id="contador-factor" style="font-size:12px;color:#555;margin-top:4px;">${unds}/${req} und totales</div>
          </div>
        </div>
        <div style="height:8px;background:#333;border-radius:4px;margin-top:12px;">
          <div id="barra" style="height:100%;background:#22c55e;border-radius:4px;width:${pct}%;transition:width 0.3s;"></div>
        </div>
      </div>`
    : esConteo
      ? `<div style="background:#1a1a1a;border-radius:16px;padding:20px;margin-bottom:12px;text-align:center;">
          <div style="font-size:13px;color:#666;">CONTEO CIEGO</div>
          <div id="contador" style="font-size:64px;font-weight:900;">${t.cantidad_escaneada || 0}</div>
          <div style="font-size:13px;color:#555;margin-top:6px;">Cuenta sin ver cantidad esperada</div>
        </div>`
      : `<div style="background:#1a1a1a;border-radius:16px;padding:20px;margin-bottom:12px;text-align:center;">
          <div style="font-size:13px;color:#666;">CANTIDAD</div>
          <div id="contador" style="font-size:64px;font-weight:900;">${unds}/${req}</div>
          <div style="height:8px;background:#333;border-radius:4px;margin-top:10px;">
            <div id="barra" style="height:100%;background:#22c55e;border-radius:4px;width:${pct}%;transition:width 0.3s;"></div>
          </div>
        </div>`;

  // Etiqueta de tipo de documento (PEDIDO / TRASLADO) para picking
  const _tipoDoc = t.tipo_documento || (t.referencia_documento && t.referencia_documento.startsWith('ST-') ? 'TRASLADO' : 'PEDIDO');
  const _etiquetaTipoDoc = _tipoDoc === 'TRASLADO'
    ? `<span style="font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;background:#431407;color:#fb923c;margin-left:8px;letter-spacing:.5px;">TRASLADO</span>`
    : `<span style="font-size:11px;font-weight:700;padding:2px 9px;border-radius:10px;background:#1e3a5f;color:#93c5fd;margin-left:8px;letter-spacing:.5px;">PEDIDO</span>`;
  // Misma franja lateral que en Packing Mixto — naranja traslado, azul pedido.
  const _acentoTipoDoc = esPicking ? `border-left:4px solid ${_tipoDoc === 'TRASLADO' ? '#c2410c' : '#1d4ed8'};` : '';

  document.getElementById('contenido-tarea').innerHTML = `
    <div style="padding:16px;${_acentoTipoDoc}">
      <div style="background:${color};color:#fff;border-radius:12px;padding:10px 16px;font-size:20px;font-weight:700;text-align:center;margin-bottom:16px;display:flex;align-items:center;justify-content:center;">${t.tipo}${esPicking ? _etiquetaTipoDoc : ''}</div>

      <div style="background:#000;border:1px solid #222;border-radius:16px;padding:20px;margin-bottom:12px;">
        <div style="font-size:13px;color:#666;">UBICACIÓN</div>
        <div style="font-size:44px;font-weight:900;letter-spacing:2px;">${t.ubicacion}</div>
      </div>

      <div style="background:#111;border-radius:16px;padding:16px;margin-bottom:12px;">
        <div style="font-size:13px;color:#666;">PRODUCTO</div>
        <div style="font-size:20px;font-weight:700;">${t.producto_nombre}</div>
        <div style="font-size:15px;color:#aaa;font-weight:400;">${t.producto_codigo}</div>
      </div>

      ${esPicking && t.producto_id ? `
      <div id="card-descomposicion" style="background:#0a1a0a;border:2px solid #166534;border-radius:16px;padding:16px;margin-bottom:12px;text-align:center;">
        <div style="font-size:12px;color:#4ade80;font-weight:700;margin-bottom:6px;letter-spacing:1px;">EMPAQUE SUGERIDO</div>
        <div id="descomp-texto" style="font-size:22px;font-weight:800;color:#fff;">Calculando...</div>
        <div id="descomp-hint" style="font-size:12px;color:#166534;margin-top:4px;"></div>
        <button id="btn-generar-lpn-picking" onclick="_generarLPNEnPicking()" style="display:none;margin-top:10px;width:100%;padding:10px;font-size:13px;font-weight:700;background:#1a2a1a;color:#4ade80;border:1px solid #166534;border-radius:10px;cursor:pointer;">
          📦 Paca sin etiqueta — Generar LPN e imprimir
        </button>
      </div>` : ''}

      ${htmlContador}

      ${puedeCamara ? `
      <button onclick="abrirCamara()" style="width:100%;padding:14px;font-size:17px;background:#fff;color:#000;border:2px solid #000;border-radius:12px;cursor:pointer;margin-bottom:10px;">
        📷 Escanear con cámara
      </button>
      <div id="camara-box" style="display:none;margin-bottom:10px;">
        <div id="lector-qr" style="border-radius:12px;overflow:hidden;"></div>
        <button onclick="cerrarCamara()" style="width:100%;padding:10px;margin-top:6px;font-size:15px;background:#333;color:#fff;border:none;border-radius:10px;cursor:pointer;">Cerrar cámara</button>
      </div>` : ''}

      <button id="btn-ok" onclick="${esPicking ? 'confirmarConGuard()' : 'confirmar()'}" ${(esConteo || esPicking || (req > 0 && unds >= req)) ? '' : 'disabled'}
        style="width:100%;padding:20px;font-size:22px;font-weight:700;background:${esPicking ? (unds >= req ? '#16a34a' : (unds > 0 ? '#b45309' : '#4b5563')) : ((esConteo || (req > 0 && unds >= req)) ? '#16a34a' : '#000')};color:#fff;border:none;border-radius:16px;cursor:pointer;opacity:${(esConteo || esPicking || (req > 0 && unds >= req)) ? 1 : 0.3};margin-bottom:10px;">
        ✓ Confirmar
      </button>

      ${!esConteo ? `
      <button onclick="confirmarManual(${t.id}, ${t.cantidad_requerida})"
        style="width:100%;padding:14px;font-size:15px;font-weight:600;background:#1a2a1a;color:#4ade80;border:1px solid #166534;border-radius:12px;cursor:pointer;margin-bottom:10px;">
        ✓ Confirmar conteo manual
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

  // Cargar descomposición de empaques en segundo plano
  if (esPicking && t.producto_id) {
    _cargarDescomposicionPicking(t.producto_id, t.almacen_id, t.cantidad_requerida);
  }
}

/**
 * Carga y muestra la descomposición empaque→unidades para el producto actual.
 * @param {number} productoId
 * @param {number} almacenId
 * @param {number} cantidad - Cantidad total requerida
 */
async function _cargarDescomposicionPicking(productoId, almacenId, cantidad) {
  const cardTexto = document.getElementById('descomp-texto');
  const cardHint  = document.getElementById('descomp-hint');
  const btnLPN    = document.getElementById('btn-generar-lpn-picking');
  if (!cardTexto) return;
  try {
    const d = await post('/api/empaques/descomponer', {
      producto_id: productoId,
      almacen_id: almacenId || null,
      cantidad_solicitada: cantidad
    });
    if (!cardTexto) return; // operario ya avanzó a otra tarea
    if (!d || d.factor_empaque <= 1) {
      const card = document.getElementById('card-descomposicion');
      if (card) card.style.display = 'none';
      return;
    }
    const lpns    = d.lpns || 0;
    const sueltas = d.sueltas || 0;
    const unidad  = d.unidad_empaque || 'PACA';
    let texto = '';
    if (lpns > 0 && sueltas > 0) {
      texto = `${lpns} ${unidad} + ${sueltas} UND`;
    } else if (lpns > 0) {
      texto = `${lpns} ${unidad}`;
    } else {
      texto = `${sueltas} UND sueltas`;
    }
    cardTexto.textContent = texto;
    cardHint.textContent  = `Escanea ${unidad.toLowerCase()} por ${unidad.toLowerCase()} — cada scan = ${d.factor_empaque} und`;

    // Lazy labeling: si hay pacas pero el producto no tiene LPNs activos,
    // mostrar el botón para que el picker genere la etiqueta en el momento.
    if (btnLPN && lpns > 0) {
      // Verificar si ya existen LPNs activos para este producto en bodega
      try {
        const lpnsActivos = await get(`/api/empaques/lpn/producto/${productoId}?almacen_id=${almacenId || ''}`);
        const hayLPNs = (lpnsActivos.lpns || []).length > 0;
        if (!hayLPNs) {
          btnLPN.style.display = 'block';
          // Guardar contexto para el generador
          btnLPN._productoId = productoId;
          btnLPN._almacenId  = almacenId;
          btnLPN._factor     = d.factor_empaque;
          btnLPN._unidad     = unidad;
        }
      } catch (_) { /* no bloquear el flujo si falla */ }
    }
  } catch (_) {
    if (cardTexto) {
      cardTexto.textContent = `${cantidad} UND`;
      cardHint.textContent  = '';
    }
  }
}

/** Genera un LPN (paca/caja) desde la tarea de picking actual e imprime etiqueta. */
async function _generarLPNEnPicking() {
  const btn = document.getElementById('btn-generar-lpn-picking');
  if (!btn || !TAREA_ACTUAL) return;

  const productoId = btn._productoId;
  const almacenId  = btn._almacenId;
  const factor     = btn._factor || 1;
  const unidad     = btn._unidad || 'PACA';

  const cantStr = prompt(
    `Paca sin etiqueta detectada.\n` +
    `¿Cuántas unidades tiene esta ${unidad}?\n` +
    `(Factor estándar: ${factor} und)`
  );
  if (cantStr === null) return;
  const cantidad = parseInt(cantStr) || factor;

  try {
    const r = await post('/api/empaques/lpn/generar', {
      producto_id: productoId,
      cantidad_actual: cantidad,
      almacen_id: almacenId || null,
      notas: `Etiquetado en picking — tarea ${TAREA_ACTUAL.id}`
    });
    if (r.error) { alerta(r.error, 'error'); return; }
    alerta(`LPN ${r.lpn.codigo} generado — imprimiendo etiqueta...`, 'exito');
    imprimirEtiquetaLPN(r.lpn, TAREA_ACTUAL.producto_nombre);
    // Ocultar el botón — ya tiene etiqueta
    btn.style.display = 'none';
  } catch (e) { alerta('Error generando LPN', 'error'); }
}

/**
 * Dispatcher central de escaneo — enruta a picking, packing, recepción o devolución
 * según el contexto activo (TAREA_ACTUAL, RECEPCION_ACTUAL, DEVOLUCION_ACTUAL).
 * @param {string} codigo - Código escaneado (barras, QR, o manual)
 */
async function procesarScan(codigo) {
  if (_TIENDA_OC_RECEPCION) { await tiendaOCProcesarScan(codigo); return; }
  if (DEVOLUCION_ACTUAL) { await procesarScanDevolucion(codigo); return; }
  if (RECEPCION_ACTUAL) { await procesarScanRecepcion(codigo); return; }
  // HUD del empacador activo
  if (EMP_TAREA && document.getElementById('emp-hud')?.classList.contains('activo')) {
    await empProcesarEscaneo(codigo); return;
  }
  if (!TAREA_ACTUAL) return;
  vibrar(); flash();

  // ── Picking: resolver empaque antes de registrar escaneo ─────────────────
  if (TAREA_ACTUAL.tipo === 'PICKING') {
    await _procesarScanPicking(codigo);
    return;
  }

  // Otros tipos (CONTEO, PACKING) — flujo original
  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: TAREA_ACTUAL.id,
      tipo: TAREA_ACTUAL.tipo,
      codigo,
      cantidad: 1
    });
    if (r.error) { beepError(); alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    beepOk();
    _actualizarContadorPicking(r);
  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

/**
 * Procesa un escaneo dentro del flujo de picking. Valida código vs producto, detecta empaque,
 * actualiza contadores, maneja total_acumulado idempotente.
 * @param {string} codigo - Código escaneado
 * @returns {Promise<{exito: boolean, cantidad_actual: number, cantidad_requerida: number, completado: boolean, es_empaque: boolean}>}
 */
async function _procesarScanPicking(codigo) {
  // 1. Preguntar al sistema qué es este código
  let scan;
  try {
    scan = await get(`/api/empaques/scan/${encodeURIComponent(codigo)}`);
  } catch (_) {
    scan = { tipo: 'NO_ENCONTRADO' };
  }

  const tipo = scan.tipo || 'NO_ENCONTRADO';

  if (tipo === 'GS1_AMBIGUO') {
    _modalAmbiguedadPicking(codigo, scan.ambiguos || []);
    return;
  }

  // Resolver código de producto y cantidad según tipo de código escaneado.
  // El backend PICKING valida por producto.codigo / codigo_siesa / codigo_barras —
  // nunca acepta un DUN-14 o código LPN directamente.
  let codigoParaBackend = codigo;  // default: código de producto base (EAN-13 en productos.codigo_barras)
  let cantidad = 1;
  let etiqueta = '';

  if ((tipo === 'GS1_UNICO' || tipo === 'EAN_BASE') && scan.producto?.codigo) {
    codigoParaBackend = scan.producto.codigo;
    cantidad = scan.factor || 1;
    if (cantidad > 1) {
      const unidad = scan.empaque?.unidad_medida || 'EMPAQUE';
      etiqueta = ` (+${cantidad} und — ${unidad})`;
    }
  } else if (tipo === 'LPN' && scan.producto?.codigo) {
    codigoParaBackend = scan.producto.codigo;
    cantidad = scan.factor || 1;  // scan.factor = lpn.cantidad_actual
    etiqueta = ` (LPN: ${cantidad} und)`;
  }
  // NO_ENCONTRADO → enviar codigo original, el backend dará error descriptivo

  // Incluir lpn_codigo cuando es un LPN — el backend lo vincula al traslado si aplica
  // GS1/EAN/LPN: el frontend ya conoce las unidades reales → usar modo idempotente.
  // NO_ENCONTRADO (barcode empaque directo): el frontend no conoce el factor → el backend
  // aplica factor_conversion con +=; no enviar total_acumulado en ese caso.
  const _scanResuelto = tipo !== 'NO_ENCONTRADO';
  if (_scanResuelto) _pickingTotal += cantidad;
  const payload = { tarea_id: TAREA_ACTUAL.id, tipo: 'PICKING', codigo: codigoParaBackend, cantidad };
  if (tipo === 'LPN') payload.lpn_codigo = codigo;  // 'LPN-XXXXXXX' original
  if (_scanResuelto) payload.total_acumulado = _pickingTotal;

  try {
    const r = await post('/api/mobile/escanear', payload);
    if (r.error) { beepError(); if (_scanResuelto) _pickingTotal -= cantidad; alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    beepOk();
    _pickingTotal = r.cantidad_actual;  // siempre sincronizar con verdad del servidor
    if (etiqueta) alerta(`Registrado${etiqueta}`, 'exito');
    _actualizarContadorPicking(r);
  } catch (e) { beepError(); if (_scanResuelto) _pickingTotal -= cantidad; alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

/**
 * Actualiza el HUD visual del picking con el resultado del escaneo.
 * @param {{cantidad_actual: number, cantidad_requerida: number, completado: boolean, es_empaque: boolean, empaques_escaneados: number, factor_conversion: number, unidad_empaque: string, mensaje: string}} r
 */
function _actualizarContadorPicking(r) {
  if (TAREA_ACTUAL) TAREA_ACTUAL.cantidad_escaneada = r.cantidad_actual || 0;
  const pkgEl = document.getElementById('contador-pkg');
  const undEl = document.getElementById('contador-und');

  if (pkgEl && undEl) {
    const factor   = TAREA_ACTUAL.factor_conversion || 1;
    const unidad   = (TAREA_ACTUAL.unidad_empaque || 'PKG').toUpperCase();
    const pkgs     = r.empaques_escaneados || 0;
    const unds     = r.cantidad_actual || 0;
    const req      = r.cantidad_requerida || 0;
    const pkgsReq  = factor > 1 ? Math.ceil(req / factor) : req;
    const sueltas  = factor > 1 ? unds % factor : 0;
    pkgEl.textContent = pkgs;
    undEl.textContent = `de ${pkgsReq} ${unidad}${sueltas > 0 ? ` +${sueltas} und` : ''}`;
    const factorEl = document.getElementById('contador-factor');
    if (factorEl) factorEl.textContent = `${unds}/${req} und totales`;
    if (r.puede_confirmar) { pkgEl.style.color = '#4ade80'; undEl.style.color = '#4ade80'; }
  } else {
    // Vista simple (unidades sueltas o conteo)
    const contador = document.getElementById('contador');
    if (contador) {
      contador.textContent = TAREA_ACTUAL.tipo === 'CONTEO'
        ? r.cantidad_contada
        : `${r.cantidad_actual}/${r.cantidad_requerida}`;
    }
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
}

/**
 * Modal cuando un código de barras coincide con múltiples empaques del mismo producto.
 * @param {string} codigo - Código escaneado
 * @param {{producto_codigo: string, factor: number, unidad: string}[]} empaques
 */
function _modalAmbiguedadPicking(codigo, empaques) {
  // empaques: array de ProductoEmpaque.to_dict() — incluye producto_codigo
  const opciones = empaques.map(e => `
    <button onclick="_elegirEmpaquePicking('${e.producto_codigo || e.referencia_item}', ${e.factor_conversion}, '${e.unidad_medida}', this.closest('.modal-ambig'))"
      style="width:100%;padding:16px;font-size:18px;font-weight:700;background:#1a1a1a;color:#fff;border:1px solid #333;border-radius:12px;cursor:pointer;margin-bottom:8px;">
      ${e.unidad_medida} — ${e.factor_conversion} und
      <div style="font-size:12px;color:#666;font-weight:400;margin-top:2px;">${e.producto_nombre || ''}</div>
    </button>`).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-ambig';
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.85);display:flex;align-items:flex-end;';
  modal.innerHTML = `
    <div style="background:#0a0a0a;border-top:2px solid #1d4ed8;border-radius:20px 20px 0 0;padding:24px;width:100%;max-height:70vh;overflow-y:auto;">
      <div style="font-size:16px;font-weight:700;color:#60a5fa;margin-bottom:4px;">Código en múltiples empaques</div>
      <div style="font-size:13px;color:#666;margin-bottom:16px;">${codigo} — ¿Cuál estás recogiendo?</div>
      ${opciones}
      <button onclick="this.closest('.modal-ambig').remove()"
        style="width:100%;padding:12px;font-size:14px;background:#111;color:#666;border:1px solid #222;border-radius:10px;cursor:pointer;margin-top:4px;">
        Cancelar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

/** @param {string} productoCodigo @param {number} factor @param {string} unidad @param {HTMLElement} modal */
async function _elegirEmpaquePicking(productoCodigo, factor, unidad, modal) {
  // productoCodigo ya es el código del producto (no el DUN-14) — el backend lo acepta
  if (modal) modal.remove();
  try {
    const r = await post('/api/mobile/escanear', {
      tarea_id: TAREA_ACTUAL.id,
      tipo: 'PICKING',
      codigo: productoCodigo,
      cantidad: factor
    });
    if (r.error) { beepError(); alerta(typeof r.error === 'object' ? r.error.mensaje : r.error, 'error'); return; }
    beepOk();
    alerta(`+${factor} und — ${unidad}`, 'exito');
    _actualizarContadorPicking(r);
  } catch (e) { beepError(); alerta(e.status ? e.message : 'Error de conexión', 'error'); }
}

/** Confirma la tarea actual (picking/packing/conteo). Envía al backend y pide siguiente tarea. */
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
    // Picking con packing asociado → mostrar botón etiqueta canasto
    if (r.canasto_data) {
      _modalEtiquetaCanasto(r.canasto_data);
      return;
    }
    // Conteos: mostrar resultado MATCH vs SEGUNDO_CONTEO antes de pedir siguiente tarea
    if (r.resultado === 'MATCH' || r.resultado === 'SEGUNDO_CONTEO') {
      const esMatch = r.resultado === 'MATCH';
      const overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:16px;';
      overlay.style.background = esMatch ? '#091F12' : '#0B1117';
      overlay.innerHTML = `
        <div style="font-size:80px;">${esMatch ? '✅' : '⚠️'}</div>
        <div style="font-size:28px;font-weight:900;color:${esMatch ? '#22C55E' : '#FBBF24'};text-align:center;padding:0 20px;">
          ${esMatch ? 'Inventario correcto' : 'Diferencia detectada'}
        </div>
        <div style="font-size:15px;color:${esMatch ? '#14532D' : '#415A70'};text-align:center;padding:0 30px;line-height:1.5;">
          ${esMatch ? 'El conteo cuadra con el sistema.' : 'Se asignó un segundo conteo\npara verificación.'}
        </div>`;
      document.body.appendChild(overlay);
      setTimeout(() => { overlay.remove(); pedirTarea(); }, esMatch ? 2000 : 3000);
    } else {
      alerta('¡Tarea completada!', 'exito');
      setTimeout(pedirTarea, 1500);
    }
  } catch (e) {
    if (e.status) {
      // Error del servidor (400/500) — mostrar mensaje real, no guardar offline
      alerta(e.message || 'Error al confirmar', 'error');
      if (btn) { btn.textContent = '✓ Confirmar'; btn.disabled = false; }
    } else {
      // Error de red real — guardar para sincronizar cuando haya WiFi
      guardarOffline(payload);
      TAREA_ACTUAL = null;
      setTimeout(pedirTarea, 2000);
    }
  }
}

/** Muestra modal de etiqueta canasto post-confirmación. @param {Object} canasto */
function _modalEtiquetaCanasto(canasto) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.93);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
  overlay.innerHTML = `
    <div style="background:#111;border-radius:16px;padding:28px 24px;width:100%;max-width:360px;border:2px solid #16a34a;text-align:center;">
      <div style="font-size:56px;margin-bottom:8px;">✅</div>
      <div style="font-size:22px;font-weight:900;color:#4ade80;margin-bottom:8px;">Picking completado</div>
      <div style="font-size:14px;color:#aaa;margin-bottom:20px;line-height:1.6;">
        ¿Cuántos canastos usaste para este pedido?
      </div>
      <div style="display:flex;align-items:center;justify-content:center;gap:16px;margin-bottom:24px;">
        <button id="_ecan-menos" style="width:48px;height:48px;background:#1a1a1a;color:#fff;border:2px solid #333;border-radius:12px;font-size:24px;font-weight:900;cursor:pointer;">−</button>
        <span id="_ecan-num" style="font-size:40px;font-weight:900;color:#fff;min-width:48px;">1</span>
        <button id="_ecan-mas" style="width:48px;height:48px;background:#1a1a1a;color:#fff;border:2px solid #333;border-radius:12px;font-size:24px;font-weight:900;cursor:pointer;">+</button>
      </div>
      <button id="_ecan-print" style="width:100%;padding:16px;background:#16a34a;color:#fff;border:none;border-radius:12px;font-size:16px;font-weight:800;cursor:pointer;margin-bottom:12px;">
        🖨 Imprimir etiquetas
      </button>
      <button id="_ecan-skip" style="width:100%;padding:14px;background:#1a1a1a;color:#666;border:1px solid #333;border-radius:12px;font-size:14px;font-weight:600;cursor:pointer;">
        Continuar sin imprimir
      </button>
    </div>`;
  document.body.appendChild(overlay);
  let copias = 1;
  const numEl = overlay.querySelector('#_ecan-num');
  overlay.querySelector('#_ecan-menos').onclick = () => { if (copias > 1) numEl.textContent = --copias; };
  overlay.querySelector('#_ecan-mas').onclick  = () => { if (copias < 10) numEl.textContent = ++copias; };
  overlay.querySelector('#_ecan-print').onclick = () => {
    const hoy = new Date().toLocaleString('es-CO');
    const bloque = `
      <div class="etiqueta-canasto">
        <div class="ec-titulo">CANASTO — PICKING PAME</div>
        <div class="ec-pedido">${canasto.pedido}</div>
        <div class="ec-cliente">${canasto.cliente || '—'}</div>
        <div class="ec-items">
          <div class="ec-item"><span class="ec-ref">${canasto.ref}</span><span class="ec-cant">${canasto.cantidad} uds</span></div>
        </div>
        <div class="ec-footer">Operario: ${canasto.operario} · ${hoy}</div>
      </div>`;
    const area = document.getElementById('print-area');
    area.innerHTML = bloque.repeat(copias);
    window.print();
    setTimeout(() => { area.innerHTML = ''; }, 1200);
    overlay.remove();
    setTimeout(pedirTarea, 800);
  };
  overlay.querySelector('#_ecan-skip').onclick = () => {
    overlay.remove();
    pedirTarea();
  };
}

/** Confirmar con guard de faltante: si cantidad < requerida, muestra modal parcial. */
async function confirmarConGuard() {
  if (!TAREA_ACTUAL) return;
  const unds    = TAREA_ACTUAL.cantidad_escaneada || 0;
  const req     = TAREA_ACTUAL.cantidad_requerida  || 0;
  const tareaId = TAREA_ACTUAL.id;

  if (req === 0 || unds >= req) {
    await confirmar();
    return;
  }

  const ok = await _modalFaltanteParcial(unds, req);
  if (!ok) return;

  await confirmar();
  _reportarFaltanteInfo(tareaId, unds, req).catch(() => {});
}

/** Modal cuando cantidad < requerida. @param {number} encontradas @param {number} requeridas */
function _modalFaltanteParcial(encontradas, requeridas) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:#111;border-radius:16px;padding:24px;width:100%;max-width:360px;border:2px solid #b45309;">
        <div style="font-size:20px;font-weight:800;color:#fb923c;margin-bottom:8px;">⚠ Faltante parcial</div>
        <div style="font-size:14px;color:#aaa;margin-bottom:20px;line-height:1.6;">
          Encontraste <strong style="color:#fb923c;font-size:18px;font-weight:800;">${encontradas}</strong> de
          <strong style="color:#fb923c;font-size:18px;font-weight:800;">${requeridas}</strong> unidades.<br>
          <span style="font-size:12px;color:#666;">Se notificará al administrador para revisar el faltante.</span>
        </div>
        <div style="display:flex;gap:10px;">
          <button id="_fp-no" style="flex:1;padding:14px;background:#1a1a1a;color:#aaa;border:1px solid #333;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">Cancelar</button>
          <button id="_fp-si" style="flex:1;padding:14px;background:#b45309;color:#fff;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;">Continuar</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector('#_fp-si').onclick = () => { overlay.remove(); resolve(true); };
    overlay.querySelector('#_fp-no').onclick = () => { overlay.remove(); resolve(false); };
  });
}

/** @param {number} tareaId @param {number} cantRecogida @param {number} cantSolicitada */
async function _reportarFaltanteInfo(tareaId, cantRecogida, cantSolicitada) {
  await post('/api/mobile/faltante-info', {
    tarea_id: tareaId,
    cantidad_recogida: cantRecogida,
    cantidad_solicitada: cantSolicitada,
  });
}

/**
 * Confirmación manual sin escáner — el operario introduce la cantidad físicamente contada.
 * @param {number} tareaId
 * @param {number} cantMax - Cantidad máxima permitida
 */
async function confirmarManual(tareaId, cantMax) {
  const cantStr = prompt(`¿Cuántas unidades encontraste físicamente? (máx. ${cantMax})`);
  if (cantStr === null) return;
  const cant = parseInt(cantStr, 10);
  if (isNaN(cant) || cant <= 0 || cant > cantMax) {
    alerta(`Cantidad inválida — debe ser entre 1 y ${cantMax}. Si no hay stock usa "Reportar problema".`, 'error');
    return;
  }
  if (!confirm(`¿Confirmar ${cant} unidades recogidas manualmente?`)) return;
  const payload = {
    tarea_id: tareaId,
    tipo: TAREA_ACTUAL?.tipo,
    items_escaneados: [],
    cantidad_manual: cant
  };
  try {
    const r = await post('/api/mobile/confirmar', payload);
    if (r.error) { alerta(r.error, 'error'); return; }
    alerta('¡Tarea completada!', 'exito');
    TAREA_ACTUAL = null;
    setTimeout(pedirTarea, 1500);
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Abre modal para que el operario reporte un problema (ubicación vacía, avería, producto incorrecto).
 * @param {number} tareaId
 */
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

        <button onclick="confirmarProblema(${tareaId},'FALTANTE',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          📉 Agotado — hay ubicación pero no queda stock
        </button>

        <button onclick="confirmarProblema(${tareaId},'MERCANCIA_AVERIADA',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          🚫 Mercancía averiada
        </button>

        <button onclick="confirmarProblema(${tareaId},'PRODUCTO_INCORRECTO',0)"
          style="width:100%;padding:14px;margin-bottom:8px;font-size:14px;font-weight:600;background:#7f1d1d;color:#f87171;border:none;border-radius:10px;cursor:pointer;text-align:left;">
          ❌ Producto incorrecto
        </button>

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

/**
 * Confirma el reporte de problema — cierra el picking con faltante y genera auditoría urgente.
 * @param {number} tareaId
 * @param {string} motivo - 'UBICACION_VACIA' | 'FALTANTE' | 'MERCANCIA_AVERIADA' | 'PRODUCTO_INCORRECTO'
 * @param {number} cantidadEncontrada - Lo que el operario encontró físicamente
 */
async function confirmarProblema(tareaId, motivo, cantidadEncontrada) {
  const observaciones = document.getElementById('obs-problema')?.value?.trim() || '';
  const modal = document.getElementById('modal-problema');
  if (modal) modal.remove();
  const tipo = TAREA_ACTUAL?.tipo || 'PICKING';
  try {
    await post('/api/mobile/reportar-problema', {
      tarea_id: tareaId,
      tipo,
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
    alerta(e.message || 'Error reportando problema', 'error');
  }
}

