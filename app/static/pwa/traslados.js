// ══════════════════════════════════════════════════════════════════
// TRASLADOS — Admin tab + operario picker traslado
// Dependencias globales (de app.js): get(), post(), alerta(),
//   API, TOKEN, OPERARIO, ALMACEN_ID
// ══════════════════════════════════════════════════════════════════

// ══════════════════════════════════════════════════════════════════
// TRASLADOS — Admin tab
// ══════════════════════════════════════════════════════════════════

let _TRAS_SUBTAB = 'transito';
const TRAS_ESTADO = {
  transito:   ['EN_TRANSITO'],
  historial:  ['ENTREGADA','RECHAZADA','CANCELADA','REVERTIDA']
};

// «Averías» NO está en TRAS_ESTADO porque no es un estado: es una decisión
// pendiente. Tiene su propio endpoint —sin paginar— porque la lista general
// pagina de a 30 y una avería sin dictaminar desaparecía en cuanto hubiera 30
// traslados entregados más nuevos que ella.
const TRAS_COL = {
  BORRADOR:'#374151', ENVIADA:'#1d4ed8', EN_PICKING:'#7c3aed', PREPARADO:'#166534',
  EN_TRANSITO:'#9a3412', ENTREGADA:'#065f46',
  RECHAZADA:'#7f1d1d', CANCELADA:'#374151', REVERTIDA:'#4b5563'
};

/**
 * Switch the active traslados sub-tab and load its content.
 * @param {string} nombre - Sub-tab key: 'pedir', 'transito', or 'historial'.
 */
function trasSubtab(nombre) {
  _TRAS_SUBTAB = nombre;
  ['pedir','transito','averias','historial'].forEach(k => {
    const el = document.getElementById(`tras-tab-${k}`);
    if (!el) return;
    const activo = k === nombre;
    el.style.background = activo ? 'var(--pm-fill)' : 'transparent';
    el.style.color = activo ? '#fff' : '#415A70';
    el.style.fontWeight = activo ? '700' : '400';
  });
  const lista = document.getElementById('tras-lista');
  const panelPedir = document.getElementById('tras-panel-pedir');
  if (nombre === 'pedir') {
    if (lista) lista.style.display = 'none';
    if (panelPedir) panelPedir.style.display = 'block';
    adminPedirIniciar();
  } else if (nombre === 'averias') {
    if (lista) lista.style.display = 'block';
    if (panelPedir) panelPedir.style.display = 'none';
    cargarAveriasPendientes();
  } else {
    if (lista) lista.style.display = 'block';
    if (panelPedir) panelPedir.style.display = 'none';
    cargarTrasladosAdmin();
  }
}

/**
 * La cola del cuarto momento de validación: averías que llegaron al CD y
 * nadie dictaminó. Mientras estén acá, el sync las repuebla desde Siesa como
 * stock VENDIBLE — o sea que el CD puede despachar a un cliente mercancía que
 * alguien ya declaró rota. Por eso tiene badge: sin un número a la vista hay
 * que acordarse de abrir la pestaña, y acordarse no es un control.
 */
async function cargarAveriasPendientes() {
  const lista = document.getElementById('tras-lista');
  if (!lista) return;
  lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--tx3);">Cargando...</div>';
  try {
    const d = await get('/api/traslados/averias-pendientes');
    const todas = d.solicitudes || [];
    trasPintarBadgeAverias(todas.length);
    if (!todas.length) {
      lista.innerHTML = '<div style="text-align:center;padding:30px;color:var(--ok-tx);">✓ Ninguna avería esperando dictamen</div>';
      return;
    }
    lista.innerHTML = `
      <div style="background:var(--warn-bg);border:1px solid var(--warn-brd);border-radius:10px;padding:12px;margin-bottom:12px;font-size:var(--fs-sm);color:var(--tx2);">
        Estas ya llegaron y se contaron. Hasta que alguien decida si de verdad
        estaban averiadas, <b style="color:var(--warn-tx);">cuentan como stock vendible</b>.
      </div>` + todas.map(s => _renderTrasladoCard(s)).join('');
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--err-tx);">Error cargando averías</div>';
  }
}

/** @param {number} n - Cuántas averías esperan dictamen. */
function trasPintarBadgeAverias(n) {
  const b = document.getElementById('badge-tras-averias');
  if (!b) return;
  b.style.display = n > 0 ? 'inline-block' : 'none';
  b.textContent = n > 0 ? String(n) : '';
}

/** Fetch and render traslado cards for the current admin sub-tab. */
async function cargarTrasladosAdmin() {
  const lista = document.getElementById('tras-lista');
  if (!lista) return;
  const estados = TRAS_ESTADO[_TRAS_SUBTAB] || [];
  lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--tx3);">Cargando...</div>';
  try {
    // Cargar solicitudes para cada estado del subtab actual
    const promesas = estados.map(e => get(`/api/traslados/?estado=${e}`));
    const resultados = await Promise.all(promesas);
    const todas = resultados.flatMap(r => r.solicitudes || []);
    todas.sort((a,b) => new Date(b.fecha_creacion) - new Date(a.fecha_creacion));

    if (!todas.length) {
      lista.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Sin solicitudes</div>';
      return;
    }
    lista.innerHTML = todas.map(s => _renderTrasladoCard(s)).join('');
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--err-tx);">Error cargando traslados</div>';
  }
}

/**
 * Build the HTML card for a single traslado solicitud.
 * @param {Object} s - Solicitud object from the API.
 * @returns {string} HTML string for the card.
 */
function _renderTrasladoCard(s) {
  const col = TRAS_COL[s.estado] || '#374151';
  const fechaCreacion = s.fecha_creacion ? new Date(s.fecha_creacion) : null;
  const fecha = fechaCreacion ? fechaCreacion.toLocaleDateString('es-CO') : '';

  // Antigüedad — alertar si lleva demasiado tiempo en estados activos
  let alertaAntiguedad = '';
  if (fechaCreacion && ['EN_PICKING','PREPARADO','ENVIADA'].includes(s.estado)) {
    const diasTranscurridos = Math.floor((Date.now() - fechaCreacion) / 86400000);
    if (diasTranscurridos >= 3) {
      alertaAntiguedad = `<span style="color:var(--err-tx);font-weight:700;font-size:var(--fs-sm);">⚠ ${diasTranscurridos}d sin avanzar</span>`;
    } else if (diasTranscurridos >= 1) {
      alertaAntiguedad = `<span style="color:var(--warn-tx);font-size:var(--fs-sm);">${diasTranscurridos}d</span>`;
    }
  }

  const itemsResumen = (s.items || []).map(i => {
    const aprobado = i.cantidad_aprobada && i.cantidad_aprobada !== i.cantidad_solicitada
      ? ` <span style="color:var(--warn-tx);">(aprobado: ${esc(i.cantidad_aprobada)})</span>` : '';
    const enviado = i.cantidad_enviada > 0
      ? ` <span style="color:var(--ok-tx);">→ enviado: ${esc(i.cantidad_enviada)}</span>` : '';
    return `<div style="font-size:var(--fs-md);color:var(--tx3);">${esc(i.producto_codigo || i.producto_nombre)} · ${esc(i.cantidad_solicitada)} und${aprobado}${enviado}</div>`;
  }).join('');

  // Barra de progreso picking
  let pickingInfo = '';
  const pp = s.picking_progreso;
  if (pp && !pp.sin_tareas && pp.total > 0) {
    const pct = pp.porcentaje || 0;
    const barColor = pct === 100 ? '#4ade80' : pct > 50 ? '#f59e0b' : '#7c3aed';
    pickingInfo = `
      <div style="margin:8px 0 4px;">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
          <span style="font-size:var(--fs-sm);color:var(--tx2);">Picking: ${esc(pp.completadas)}/${esc(pp.total)} tareas</span>
          <span style="font-size:var(--fs-sm);color:${barColor};font-weight:700;">${pct}%</span>
        </div>
        <div style="height:4px;background:var(--bg-s2);border-radius:4px;overflow:hidden;">
          <div style="height:100%;width:${pct}%;background:${barColor};border-radius:4px;transition:width .3s;"></div>
        </div>
      </div>`;
  } else if (pp && pp.sin_tareas && s.estado === 'EN_PICKING') {
    pickingInfo = `<div style="font-size:var(--fs-sm);color:var(--warn-tx);margin:6px 0;">⚠ Sin tareas de picking — picking manual requerido</div>`;
  }

  const acciones = [];

  if (s.estado === 'ENVIADA') {
    acciones.push(`<button onclick="conBotonOcupado(event, () => trasAprobar(${esc(s.id)}))" style="flex:1;padding:10px;background:#166534;color:var(--tx);border:none;border-radius:8px;font-size:17px;font-weight:700;cursor:pointer;">Aprobar y asignar</button>`);
    acciones.push(`<button onclick="trasRechazar(${esc(s.id)})" style="padding:10px 12px;background:#7f1d1d;color:var(--tx);border:none;border-radius:8px;font-size:17px;cursor:pointer;">Rechazar</button>`);
  }

  if (s.estado === 'EN_PICKING') {
    const pickingCompleto = pp && !pp.sin_tareas && pp.completadas === pp.total && pp.total > 0;
    if (pickingCompleto) {
      // Picking formal completo → confirmar y avanzar a PREPARADO
      acciones.push(`<button onclick="trasConfirmarRecogida(${esc(s.id)})" style="flex:1;padding:10px;background:#166534;color:var(--tx);border:none;border-radius:8px;font-size:17px;font-weight:700;cursor:pointer;">✅ Confirmar recogida</button>`);
    } else {
      // Picking en proceso o manual → forzar confirmación o despachar directo
      acciones.push(`<button onclick="trasConfirmarRecogida(${esc(s.id)})" style="flex:1;padding:10px;background:#1e3a5f;color:var(--info-tx);border:1px solid var(--info-brd);border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">Confirmar recogida manual</button>`);
    }
    acciones.push(`<button onclick="trasDespacharDirecto(${esc(s.id)})" style="padding:10px 10px;background:#78350f;color:var(--warn-tx);border:1px solid #92400e;border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">Despachar ⚡</button>`);
    acciones.push(`<button onclick="trasReasignarOperario(${esc(s.id)})" style="padding:10px 10px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:8px;font-size:var(--fs-md);cursor:pointer;">↺ Operario</button>`);
  }

  if (s.estado === 'PREPARADO') {
    const packDespachado = s.packing_info && s.packing_info.estado === 'DESPACHADO';
    if (packDespachado) {
      acciones.push(`<div style="flex:1;padding:10px;color:var(--tx2);font-size:var(--fs-md);text-align:center;background:var(--bg-input);border:1px solid var(--info-brd);border-radius:8px;">⏳ Despacho en proceso...</div>`);
    } else {
      acciones.push(`<button onclick="conBotonOcupado(event, () => trasDespachar(${esc(s.id)}))" style="flex:1;padding:10px;background:#b45309;color:#fff;border:none;border-radius:8px;font-size:17px;font-weight:700;cursor:pointer;">🚛 Despachar</button>`);
    }
    acciones.push(`<button onclick="trasVerLPNs(${esc(s.id)})" style="padding:10px 10px;background:var(--bg-input);color:var(--lila-tx);border:1px solid var(--lila-brd);border-radius:8px;font-size:var(--fs-md);cursor:pointer;">📦 LPNs</button>`);
  }

  if (s.estado === 'EN_TRANSITO') {
    acciones.push(`<span style="font-size:var(--fs-md);color:var(--warn-tx);font-weight:600;">🚚 Mercancía en camino — la tienda confirma recepción</span>`);
  }

  // ── Recuperación cuando Siesa falló ────────────────────────────────────
  //
  // Las tres funciones existían, estaban probadas y NINGÚN botón las llamaba.
  // Peor: `traslado_service` escribe en el aviso «Usa WMS Admin → Traslados →
  // Reintentar despacho» y lo manda por correo — el mensaje de error señalaba
  // un botón inexistente.
  //
  // Se necesitan el día que Siesa falla, que es a las 6 p.m. de un viernes.
  // Una capacidad de recuperación que exige armar un curl con un JWT **no
  // existe cuando hace falta**: existe cuando hay tiempo, y cuando hay tiempo
  // no hace falta.
  //
  // Se muestran solo cuando falta el consecutivo que corresponde, para que un
  // botón visible signifique siempre «esto está trabado» y no decore la
  // tarjeta de un traslado sano.
  //
  // Los rótulos son los que el propio sistema le dicta al operario: el correo
  // y el `siesa_error` de `traslado_service` dicen «Reintentar despacho», así
  // que el botón se llama «Reintentar despacho» y no «Reintentar salida». Un
  // instructivo que nombra un botón que no existe con ese nombre se lee como
  // «esto no está» y se termina llamando a soporte.
  const recuperacion = [];

  // faltaSalida/faltaEntrada solo cuentan una vez que el paso físico
  // correspondiente YA OCURRIÓ (despachar()/confirmar_recepcion() avanzan el
  // estado sin importar si Siesa respondió, ver traslado_service.py). Antes
  // de eso el consec ausente es lo esperado —nadie lo pidió todavía—, no un
  // fallo: PREPARADO significa "aún no se despachó" y EN_TRANSITO significa
  // "aún no se recibió", no "Siesa falló".
  const faltaSalida = !s.siesa_salida_consec
    && ['EN_TRANSITO', 'ENTREGADA'].includes(s.estado);
  const faltaEntrada = s.modo_transferencia === 'EN_TRANSITO'
    && s.siesa_salida_consec && !s.siesa_entrada_consec
    && s.estado === 'ENTREGADA';

  if (faltaSalida) {
    recuperacion.push(`<button onclick="trasReintentarDespachoSiesa(${esc(s.id)})" style="padding:8px 10px;background:#7f1d1d;color:var(--err-tx);border:1px solid var(--err-brd);border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">↻ Reintentar despacho (STS)</button>`);
  }
  if (faltaEntrada) {
    recuperacion.push(`<button onclick="trasReintentarRecepcionSiesa(${esc(s.id)})" style="padding:8px 10px;background:#7f1d1d;color:var(--err-tx);border:1px solid var(--err-brd);border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">↻ Reintentar recepción (ETS)</button>`);
  }

  const bloqueRecuperacion = recuperacion.length ? `
    <div style="margin-top:10px;padding:8px;background:var(--err-bg);border:1px solid var(--err-brd);border-radius:8px;">
      <div style="font-size:var(--fs-sm);color:var(--err-tx);font-weight:700;margin-bottom:6px;">
        ⚠ RECUPERACIÓN — crean documentos en Siesa. Verificá primero en Siesa que el documento no exista.
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">${recuperacion.join('')}</div>
    </div>` : '';

  // Revertir traslado no crea documentos en Siesa (el STS ya enviado se anula
  // a mano allá) — es un control normal para un traslado sano en tránsito, no
  // una recuperación de fallo. Va aparte para no heredar la caja roja de
  // "RECUPERACIÓN" ni su advertencia, que describe solo los reintentos de
  // arriba.
  const bloqueRevertir = s.estado === 'EN_TRANSITO' ? `
    <div style="margin-top:10px;">
      <button onclick="trasRevertir(${esc(s.id)})" style="padding:8px 10px;background:var(--bg-input);color:var(--warn-tx);border:1px solid #92400e;border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">↩ Revertir traslado</button>
    </div>` : '';

  // ── Avería: el cuarto momento de validación ──────────────────────────
  // Solo aparece cuando hay algo que decidir: es un traslado de averías, ya
  // llegó y se contó (ENTREGADA), y nadie lo dictaminó todavía.
  //
  // `=== null` y no `!s.averia_veredicto`: un veredicto `false` («no estaba
  // averiada») es una decisión tomada, y con truthiness volvería a pedir el
  // dictamen de algo ya resuelto.
  let bloqueDictamen = '';
  if (s.es_averia) {
    if (s.estado === 'ENTREGADA' && s.averia_veredicto === null) {
      bloqueDictamen = `
    <div style="margin-top:10px;border:1px solid var(--warn-brd);background:var(--warn-bg);border-radius:8px;padding:10px;">
      <div style="font-size:var(--fs-md);color:var(--warn-tx);font-weight:700;margin-bottom:4px;">⚠ Avería sin dictaminar</div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:8px;">Llegó y se contó. Antes de ubicarla hay que decir si de verdad estaba averiada.</div>
      ${s.averia_evidencia ? `<div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:8px;">Evidencia del punto: ${esc(s.averia_evidencia)}</div>` : ''}
      <div style="display:flex;gap:6px;flex-wrap:wrap;">
        <button onclick="trasDictaminarAveria(${esc(s.id)}, true)" style="padding:8px 10px;background:#7f1d1d;color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">Sí estaba averiada</button>
        <button onclick="trasDictaminarAveria(${esc(s.id)}, false)" style="padding:8px 10px;background:#14532d;color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-md);font-weight:700;cursor:pointer;">No — vuelve a vendible</button>
      </div>
    </div>`;
    } else if (s.averia_veredicto !== null) {
      const ok = s.averia_veredicto;
      bloqueDictamen = `
    <div style="margin-top:10px;font-size:var(--fs-sm);color:${ok ? 'var(--err-tx)' : 'var(--ok-tx)'};">
      ${ok ? '✓ Confirmada como averiada' : '✓ Dictaminada NO averiada — vuelve a vendible'}
      ${s.averia_veredicto_nombre ? ` · ${esc(s.averia_veredicto_nombre)}` : ''}
      ${s.averia_veredicto_nota ? `<div style="color:var(--tx2);">${esc(s.averia_veredicto_nota)}</div>` : ''}
    </div>`;
    }
  }

  const operarioTag = s.operario_nombre
    ? `<div style="font-size:var(--fs-md);color:var(--lila-tx);margin-bottom:6px;">👷 ${esc(s.operario_nombre)}${s.estado==='PREPARADO' ? ' · Listo para despachar' : s.estado==='EN_PICKING' ? ' · Recogiendo' : ''}</div>`
    : (s.estado === 'EN_PICKING' ? `<div style="font-size:var(--fs-md);color:var(--warn-tx);margin-bottom:6px;">⚠ Sin operario asignado</div>` : '');

  return `
  <div style="background:var(--bg-s);border:1px solid ${s.estado==='EN_PICKING' && pp?.sin_tareas ? 'var(--warn-brd)' : 'var(--brd)'};border-radius:12px;padding:14px;margin-bottom:10px;">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
      <div>
        <div style="font-size:17px;font-weight:700;">${esc(s.codigo)}</div>
        <div style="font-size:var(--fs-md);color:var(--tx3);margin-top:2px;">${esc(s.nombre_punto_venta || s.bodega_destino_siesa)} · ${fecha} ${alertaAntiguedad}</div>
      </div>
      <span style="background:${col};color:#fff;font-size:19px;font-weight:700;padding:3px 8px;border-radius:8px;white-space:nowrap;">${s.estado.replace(/_/g, ' ')}</span>
    </div>
    <div style="margin-bottom:6px;">${itemsResumen}</div>
    ${pickingInfo}
    ${operarioTag}
    ${s.siesa_error ? `<div style="font-size:var(--fs-sm);color:var(--err-tx);margin-bottom:8px;">⚠ Siesa: ${esc(s.siesa_error)}</div>` : ''}
    ${acciones.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:10px;">${acciones.join('')}</div>` : ''}
    ${bloqueRecuperacion}
    ${bloqueRevertir}
    ${bloqueDictamen}
  </div>`;
}

// ── Admin Pedir — solicitar traslado hacia NB1 ──────────────────
// _AP_STOCK guarda solo la PÁGINA actual — el catálogo completo de una
// bodega (~4000 SKU, ver CLAUDE.md) no se manda entero al celular. Filtro y
// paginación viven en el servidor (`/api/traslados/stock-disponible`), sobre
// el mismo cache de Siesa que ya existía; acá solo se pide la página.
let _AP_STOCK = [];
let _AP_STOCK_ESTADO = 'idle';
let _AP_CARRITO = [];
let _AP_ORIGEN = null;
let _AP_FILTRO = '';
let _AP_PAGINA = 1;
const _AP_POR_PAGINA = 30;
let _AP_TOTAL_FILTRADO = 0;
let _AP_TOTAL_PAGINAS = 1;
let _AP_FILTRO_TIMER = null;
let _AP_INICIADO = false;

/** Initialize the "Pedir" panel: populate origin selector and load stock. */
function adminPedirIniciar() {
  if (_AP_INICIADO) return;
  _AP_INICIADO = true;
  const sel = document.getElementById('admin-pedir-origen');
  if (!sel) return;
  const opciones = _BODEGAS_ORIGEN.filter(b => b.id !== 'NB1');
  sel.innerHTML = opciones.map(b =>
    `<option value="${esc(b.id)}" data-nombre="${esc(b.nombre)}" style="background:var(--bg-s);color:var(--tx);">${esc(b.nombre)} (${esc(b.id)})</option>`
  ).join('');
  const def = opciones[0];
  if (def) {
    sel.value = def.id;
    _AP_ORIGEN = { id: def.id, nombre: def.nombre };
  }
  adminPedirCargarStock();
}

/**
 * Handle origin bodega change; reset cart and reload stock.
 * @param {HTMLSelectElement} sel - The origin bodega dropdown.
 */
function adminPedirCambiarOrigen(sel) {
  const opt = sel.options[sel.selectedIndex];
  _AP_ORIGEN = { id: sel.value, nombre: opt.dataset.nombre || sel.value };
  _AP_CARRITO = [];
  adminPedirActualizarCarrito();
  _AP_STOCK = [];
  _AP_FILTRO = '';
  _AP_PAGINA = 1;
  const buscar = document.getElementById('admin-pedir-buscar');
  if (buscar) buscar.value = '';
  _AP_STOCK_ESTADO = 'cargando';
  adminPedirCargarStock();
}

/** Fetch the current page of available stock from the selected origin bodega (filtro/paginación server-side). */
async function adminPedirCargarStock() {
  _AP_STOCK_ESTADO = 'cargando';
  adminPedirRenderStock();
  try {
    const qs = new URLSearchParams({
      bodega: _AP_ORIGEN?.id || 'NS1',
      page: _AP_PAGINA,
      per_page: _AP_POR_PAGINA,
    });
    if (_AP_FILTRO) qs.set('q', _AP_FILTRO);
    const d = await get(`/api/traslados/stock-disponible?${qs}`);
    _AP_STOCK = (d.items || []).filter(i => i.producto_id && i.disponible > 0);
    _AP_TOTAL_FILTRADO = d.total_filtrado ?? _AP_STOCK.length;
    _AP_TOTAL_PAGINAS = d.total_paginas || 1;
    _AP_PAGINA = d.pagina || _AP_PAGINA;
    _AP_STOCK_ESTADO = 'listo';
  } catch (e) {
    _AP_STOCK_ESTADO = 'error';
  }
  adminPedirRenderStock();
}

/** Invalidate stock cache on the server and reload fresh data. */
async function adminPedirActualizarStock() {
  const btn = document.getElementById('admin-pedir-btn-refresh');
  if (btn) { btn.disabled = true; btn.style.opacity = '0.4'; btn.textContent = '…'; }
  try {
    await fetch(API + `/api/traslados/invalidar-cache-stock?bodega=${_AP_ORIGEN?.id || 'NS1'}`, {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN },
    });
    _AP_STOCK = [];
    _AP_STOCK_ESTADO = 'cargando';
    await adminPedirCargarStock();
  } catch (e) { alerta('Error actualizando stock', 'error'); }
  finally { if (btn) { btn.disabled = false; btn.style.opacity = '1'; btn.textContent = '↻'; } }
}

/** Apply text filter from the search input — debounced: el filtro corre en el servidor, no en el celular. */
function adminPedirFiltrarStock() {
  clearTimeout(_AP_FILTRO_TIMER);
  _AP_FILTRO_TIMER = setTimeout(() => {
    _AP_FILTRO = (document.getElementById('admin-pedir-buscar')?.value || '').trim().toLowerCase();
    _AP_PAGINA = 1;
    adminPedirCargarStock();
  }, 350);
}

/**
 * Navigate to a specific page in the stock list (pide esa página al servidor).
 * @param {number} p - Page number to display.
 */
async function adminPedirIrPagina(p) {
  _AP_PAGINA = p;
  await adminPedirCargarStock();
  document.getElementById('admin-pedir-stock-lista')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/** Render the paginated stock grid with filter and cart state applied. */
function adminPedirRenderStock() {
  const el = document.getElementById('admin-pedir-stock-lista');
  if (!el) return;
  if (_AP_STOCK_ESTADO === 'cargando') {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:var(--tx3);">Consultando stock...</div>';
    return;
  }
  if (_AP_STOCK_ESTADO === 'error') {
    el.innerHTML = `<div style="text-align:center;padding:40px;"><div style="color:var(--err-tx);margin-bottom:12px;">No se pudo cargar el stock</div>
      <button onclick="adminPedirCargarStock()" style="padding:10px 20px;background:#1d4ed8;color:#fff;border:none;border-radius:8px;cursor:pointer;">Reintentar</button></div>`;
    return;
  }
  if (!_AP_STOCK.length) {
    el.innerHTML = `<div style="text-align:center;padding:30px;color:var(--tx3);">${_AP_FILTRO ? 'Sin resultados' : 'Sin productos disponibles'}</div>`;
    return;
  }
  // La página ya viene recortada y filtrada del servidor (stock-disponible) —
  // acá solo se pinta lo que llegó, sin volver a filtrar/cortar en el celular.
  const totalPags = _AP_TOTAL_PAGINAS;
  const rango = 2, desde = Math.max(1, _AP_PAGINA - rango), hasta = Math.min(totalPags, _AP_PAGINA + rango);
  const nums = [];
  if (desde > 1) nums.push('<span style="color:var(--tx3);">…</span>');
  for (let p = desde; p <= hasta; p++) {
    nums.push(`<button onclick="adminPedirIrPagina(${p})" style="min-width:44px;min-height:44px;padding:6px 8px;background:${p===_AP_PAGINA?'var(--pm-fill)':'var(--bg-s2)'};color:#fff;border:none;border-radius:6px;font-size:var(--fs-xs);font-weight:${p===_AP_PAGINA?'700':'400'};cursor:pointer;">${p}</button>`);
  }
  if (hasta < totalPags) nums.push('<span style="color:var(--tx3);">…</span>');
  const nav = totalPags > 1 ? `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 0 14px;flex-wrap:wrap;">
    <button onclick="adminPedirIrPagina(${_AP_PAGINA-1})" ${_AP_PAGINA===1?'disabled':''} style="padding:7px 14px;background:var(--bg-s2);color:${_AP_PAGINA===1?'var(--tx3)':'var(--tx)'};border:none;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">← Ant</button>
    <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:center;">${nums.join('')}</div>
    <button onclick="adminPedirIrPagina(${_AP_PAGINA+1})" ${_AP_PAGINA===totalPags?'disabled':''} style="padding:7px 14px;background:var(--bg-s2);color:${_AP_PAGINA===totalPags?'var(--tx3)':'var(--tx)'};border:none;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">Sig →</button>
  </div><div style="text-align:center;font-size:var(--fs-xs);color:var(--tx3);margin-bottom:10px;">${_AP_TOTAL_FILTRADO} productos · pág ${_AP_PAGINA}/${totalPags}</div>` : '';

  el.innerHTML = nav + _AP_STOCK.map(item => {
    const enCarrito = _AP_CARRITO.find(c => c.codigo_siesa === item.codigo_siesa);
    const qid = 'ap-qty-' + (item.codigo_siesa || '').replace(/[^a-zA-Z0-9]/g, '-');
    const nombreEsc = (item.nombre || '').replace(/\\/g,'\\\\').replace(/'/g,"\\'");
    return `<div style="background:var(--bg-s);border:1px solid ${enCarrito?'#4ade80':'var(--brd)'};border-radius:10px;padding:12px;margin-bottom:8px;display:flex;align-items:center;gap:12px;">
      <div style="flex:1;min-width:0;">
        <div style="font-size:var(--fs-sm);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${esc(item.nombre||'—')}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(item.codigo_siesa||'')} · Disponible: <span style="color:var(--ok-tx);font-weight:700;">${esc(item.disponible)}</span></div>
      </div>
      <div style="display:flex;align-items:center;gap:6px;flex-shrink:0;">
        <input type="number" min="1" max="${esc(item.disponible)}" value="${enCarrito?.cantidad||1}" id="${qid}"
          style="width:56px;padding:7px;background:var(--bg-s);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:var(--fs-sm);text-align:center;">
        <button onclick="adminPedirAgregarCarrito('${esc(item.codigo_siesa)}','${nombreEsc}',${esc(item.disponible)},${esc(item.producto_id||'null')})"
          style="padding:8px 12px;background:${enCarrito?'#4ade80':'#fff'};color:#000;border:none;border-radius:8px;font-size:var(--fs-xs);font-weight:700;cursor:pointer;">${enCarrito?'✓':'+'}</button>
      </div>
    </div>`;
  }).join('') + nav;
}

/**
 * Add or update a product in the request cart.
 * @param {string} codigoSiesa - Siesa product code.
 * @param {string} nombre - Product display name.
 * @param {number} disponible - Maximum available quantity.
 * @param {number|null} productoId - WMS product ID.
 */
function adminPedirAgregarCarrito(codigoSiesa, nombre, disponible, productoId) {
  const inp = document.getElementById(`ap-qty-${codigoSiesa.replace(/[^a-zA-Z0-9]/g,'-')}`);
  const cantidad = Math.min(parseInt(inp?.value || 1), disponible);
  if (cantidad < 1) return;
  const idx = _AP_CARRITO.findIndex(c => c.codigo_siesa === codigoSiesa);
  if (idx >= 0) _AP_CARRITO[idx].cantidad = cantidad;
  else _AP_CARRITO.push({ codigo_siesa: codigoSiesa, nombre, disponible, cantidad, producto_id: productoId });
  adminPedirActualizarCarrito();
  adminPedirRenderStock();
}

/** Re-render the cart summary panel with current items. */
function adminPedirActualizarCarrito() {
  const header = document.getElementById('admin-pedir-carrito-header');
  const items = document.getElementById('admin-pedir-carrito-items');
  if (!header || !items) return;
  if (!_AP_CARRITO.length) { header.style.display = 'none'; return; }
  header.style.display = 'block';
  items.innerHTML = _AP_CARRITO.map(c =>
    `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--brd);">
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:var(--fs-xs);color:var(--tx);">${esc(c.nombre)}</span>
      <span style="flex-shrink:0;color:var(--ok-tx);font-weight:700;font-size:var(--fs-sm);">${esc(c.cantidad)}</span>
      <button onclick="adminPedirQuitarCarrito('${esc(c.codigo_siesa)}')" style="flex-shrink:0;background:none;border:none;color:var(--err-tx);cursor:pointer;font-size:var(--fs-sm);padding:2px 4px;">✕</button>
    </div>`
  ).join('');
}

/**
 * Remove a product from the request cart by its Siesa code.
 * @param {string} codigoSiesa - Siesa product code to remove.
 */
function adminPedirQuitarCarrito(codigoSiesa) {
  _AP_CARRITO = _AP_CARRITO.filter(c => c.codigo_siesa !== codigoSiesa);
  adminPedirActualizarCarrito();
  adminPedirRenderStock();
}

/** Create and immediately send a traslado solicitud from the current cart. */
async function adminPedirEnviarSolicitud() {
  if (!_AP_CARRITO.length) { alerta('El carrito está vacío', 'error'); return; }
  const origen = _AP_ORIGEN?.nombre || _AP_ORIGEN?.id || 'la bodega';
  if (!await _modalConfirmar(`¿Solicitar ${_AP_CARRITO.length} producto${_AP_CARRITO.length!==1?'s':''} desde ${origen} para Bodega Principal (NB1)?`, { titulo: 'Solicitar traslado' })) return;
  const items = _AP_CARRITO.filter(c => c.producto_id).map(c => ({
    producto_id: c.producto_id,
    cantidad_solicitada: c.cantidad,
    disponible_siesa: c.disponible,
  }));
  if (!items.length) { alerta('No se encontraron productos válidos', 'error'); return; }
  try {
    const r = await fetch(API + '/api/traslados/', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        items,
        bodega_origen_siesa: _AP_ORIGEN.id,
        bodega_destino_siesa: 'NB1',
        nombre_punto_venta: 'Bodega Principal (NB1)',
      })
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error creando solicitud', 'error'); return; }
    const r2 = await fetch(API + `/api/traslados/${d.id}/enviar`, {
      method: 'POST', headers: { Authorization: 'Bearer ' + TOKEN }
    });
    if (r2.ok) {
      alerta('Pedido enviado', 'exito');
      _AP_CARRITO = [];
      adminPedirActualizarCarrito();
      adminPedirRenderStock();
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Fetch and render traslados assigned to the current operario. */
async function cargarTrasladosOperario() {
  const contenedor = document.getElementById('traslados-operario');
  if (!contenedor) return;
  try {
    const d = await get('/api/traslados/mis-traslados');
    const traslados = d.traslados || [];
    if (!traslados.length) {
      contenedor.innerHTML = '';
      return;
    }
    contenedor.innerHTML = `
      <div style="font-size:var(--fs-xs);font-weight:700;color:var(--lila-tx);margin-bottom:10px;text-transform:uppercase;letter-spacing:1px;">
        Traslados asignados a ti (${esc(traslados.length)})
      </div>
      ${traslados.map(t => _renderTrasladoOperario(t)).join('')}`;
  } catch (e) {
    contenedor.innerHTML = '<div style="text-align:center;padding:20px;color:var(--err-tx);font-size:var(--fs-xs);">Error cargando tus traslados — desliza para reintentar</div>';
  }
}

/**
 * Build the HTML card for a traslado assigned to an operario.
 * @param {Object} t - Traslado object with items and metadata.
 * @returns {string} HTML string for the operario card.
 */
function _renderTrasladoOperario(t) {
  const itemsHtml = (t.items || []).map(i => {
    const cant = i.cantidad_aprobada || i.cantidad_solicitada;
    return `<div style="font-size:var(--fs-xs);color:var(--tx2);">${esc(i.producto_codigo)} — <b style="color:var(--tx);">${cant} und</b></div>`;
  }).join('');
  return `
    <div style="background:var(--lila-bg);border:1px solid var(--lila-brd);border-radius:12px;padding:14px;margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div style="font-size:var(--fs-sm);font-weight:700;">${esc(t.codigo)}</div>
        <span style="font-size:var(--fs-xs);color:var(--lila-tx);">${esc(t.nombre_punto_venta || t.bodega_destino_siesa)}</span>
      </div>
      <div style="margin-bottom:10px;">${itemsHtml}</div>
      <button onclick="trasConfirmarRecogida(${esc(t.id)})"
        style="width:100%;padding:12px;background:#7c3aed;color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">
        Confirmar recogida
      </button>
    </div>`;
}

/**
 * Confirm picking completion for a traslado, advancing it to PREPARADO.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasConfirmarRecogida(id) {
  if (!await _modalConfirmar('¿Confirmar recogida completa? El traslado pasará a PREPARADO y podrás despacharlo.', { titulo: 'Confirmar recogida' })) return;
  try {
    await post(`/api/traslados/${id}/confirmar-picking`, {});
    alerta('Recogida confirmada — listo para despachar', 'exito');
    cargarTrasladosAdmin();
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

/**
 * Dispatch a traslado directly, skipping picking confirmation.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasDespacharDirecto(id) {
  if (!await _modalConfirmar('¿Despachar directamente sin confirmar picking? Se usarán las cantidades aprobadas como enviadas.', { titulo: 'Despacho directo' })) return;
  try {
    await post(`/api/traslados/${id}/despachar`, {});
    alerta('Despachado — mercancía en tránsito', 'exito');
    cargarTrasladosAdmin();
  } catch (e) { alerta(e.message || 'Error', 'error'); }
}

/**
 * Show a modal to reassign a different operario to a traslado.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasReasignarOperario(id) {
  let operariosData;
  try {
    operariosData = await get('/api/traslados/operarios-disponibles');
  } catch (e) { alerta('Error cargando operarios', 'error'); return; }

  const operarios = operariosData.operarios || [];
  if (!operarios.length) { alerta('No hay operarios disponibles', 'advertencia'); return; }

  const opciones = operarios.map(o => `<option value="${esc(o.id)}">${esc(o.nombre)}</option>`).join('');
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:900;display:flex;align-items:center;justify-content:center;padding:20px;';
  modal.innerHTML = `
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:16px;padding:24px;width:100%;max-width:440px;">
      <div style="font-size:var(--fs-md);font-weight:700;margin-bottom:16px;">↺ Reasignar operario</div>
      <select id="modal-nuevo-operario" style="width:100%;padding:10px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);margin-bottom:16px;">
        ${opciones}
      </select>
      <div style="display:flex;gap:8px;">
        <button onclick="this.closest('div[style*=fixed]').remove()" style="flex:1;padding:10px;background:var(--bg-s2);color:var(--tx2);border:1px solid var(--brd);border-radius:8px;cursor:pointer;">Cancelar</button>
        <button id="btn-confirmar-reasignar" style="flex:1;padding:10px;background:#7c3aed;color:var(--tx);border:none;border-radius:8px;font-weight:700;cursor:pointer;">Confirmar</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector('#btn-confirmar-reasignar').onclick = async () => {
    const nuevo_id = parseInt(modal.querySelector('#modal-nuevo-operario').value);
    modal.remove();
    try {
      await post(`/api/traslados/${id}/reasignar-operario`, { operario_id: nuevo_id });
      alerta('Operario reasignado', 'exito');
      cargarTrasladosAdmin();
    } catch (e) { alerta(e.message || 'Error', 'error'); }
  };
}

/**
 * Open the approval modal to approve quantities and assign an operario.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasAprobar(id) {
  // Carga solicitud y operarios en paralelo
  let solicitud, operariosData;
  try {
    [solicitud, operariosData] = await Promise.all([
      fetch(API + `/api/traslados/${id}`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
      fetch(API + `/api/traslados/operarios-disponibles`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
    ]);
  } catch (e) { alerta('Error de conexión', 'error'); return; }

  const items = solicitud.items || [];
  const operarios = operariosData.operarios || [];

  const filasItems = items.map(i => `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <div style="flex:1;font-size:var(--fs-xs);">
        <div style="font-weight:600;">${esc(i.producto_codigo)}</div>
        <div style="color:var(--tx3);font-size:var(--fs-xs);">Solicitado: ${esc(i.cantidad_solicitada)} · Disp. Siesa: ${i.disponible_siesa ?? '—'}</div>
      </div>
      <div style="display:flex;align-items:center;gap:4px;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);">Aprobar:</label>
        <input type="number" id="apr-${esc(i.id)}" value="${esc(i.cantidad_solicitada)}" min="0"
          style="width:70px;padding:6px;background:var(--bg-input);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:var(--fs-sm);text-align:center;">
      </div>
    </div>
  `).join('');

  const opcioneOperarios = operarios.length
    ? `<option value="">Sin asignar (admin recoge)</option>` + operarios.map(o => `<option value="${esc(o.id)}">${esc(o.nombre)}</option>`).join('')
    : `<option value="">No hay operarios disponibles</option>`;

  const modal = document.createElement('div');
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:440px;border:1px solid #166534;max-height:85vh;overflow-y:auto;">
        <div style="font-size:17px;font-weight:700;margin-bottom:4px;">Aprobar y asignar traslado</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:16px;">${esc(solicitud.nombre_punto_venta || solicitud.bodega_destino_siesa)}</div>

        <div style="font-size:var(--fs-xs);font-weight:600;margin-bottom:8px;color:var(--tx2);">CANTIDADES A ENVIAR</div>
        ${filasItems}

        <div style="font-size:var(--fs-xs);font-weight:600;margin-top:14px;margin-bottom:8px;color:var(--tx2);">OPERARIO QUE RECOGE</div>
        <select id="apr-operario"
          style="width:100%;padding:10px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);margin-bottom:16px;">
          ${opcioneOperarios}
        </select>

        <div style="display:flex;gap:8px;">
          <button id="btn-apr-ok" style="flex:1;padding:12px;background:#166534;color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">Aprobar</button>
          <button onclick="this.closest('[style*=fixed]').parentElement.remove()" style="padding:12px 16px;background:var(--bg-s2);color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">Cancelar</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  modal.querySelector('#btn-apr-ok').onclick = async () => {
    const items_aprobados = items.map(i => ({
      id: i.id,
      cantidad_aprobada: Number(document.getElementById(`apr-${i.id}`).value) || 0
    }));
    const operario_id = document.getElementById('apr-operario').value
      ? Number(document.getElementById('apr-operario').value) : null;
    modal.remove();
    try {
      const r = await fetch(API + `/api/traslados/${id}/aprobar`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify({ items_aprobados, operario_id })
      });
      const d = await r.json();
      if (r.ok) {
        alerta(operario_id ? 'Aprobado — operario notificado' : 'Aprobado — sin operario asignado', 'exito');
        cargarTrasladosAdmin();
      } else { alerta(d.error || 'Error', 'error'); }
    } catch (e) { alerta('Error de conexión', 'error'); }
  };
}

/**
 * Reject a traslado solicitud with a reason prompt.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasRechazar(id) {
  const motivo = await _modalTexto('Rechazar solicitud', 'Motivo del rechazo:');
  if (!motivo) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/rechazar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo })
    });
    const d = await r.json();
    if (r.ok) { alerta('Solicitud rechazada', 'advertencia'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Display a bottom-sheet modal with LPNs linked to a traslado.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasVerLPNs(id) {
  let data;
  try {
    data = await get(`/api/traslados/${id}/lpns`);
  } catch (e) { alerta('Error cargando LPNs', 'error'); return; }

  const lpns = data.lpns || [];
  const estadoColor = { ACTIVO: '#4ade80', EN_TRANSITO: '#f59e0b', CONSUMIDO: '#6b7280' };

  const filas = lpns.length
    ? lpns.map(l => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--brd);">
          <div>
            <div style="font-size:var(--fs-sm);font-weight:700;color:var(--lila-tx);">${esc(l.codigo)}</div>
            <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(l.producto_codigo)} — ${esc(l.cantidad_actual)} und</div>
          </div>
          <span style="font-size:var(--fs-xs);font-weight:700;color:${esc(estadoColor[l.estado]||'var(--tx)')};">${esc(l.estado)}</span>
        </div>`).join('')
    : '<div style="color:var(--tx3);text-align:center;padding:20px;">Sin LPNs vinculados a este traslado</div>';

  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.9);display:flex;align-items:flex-end;';
  modal.innerHTML = `
    <div style="background:var(--bg-s);border-top:2px solid #7c3aed;border-radius:20px 20px 0 0;padding:24px;width:100%;max-height:75vh;overflow-y:auto;">
      <div style="font-size:var(--fs-md);font-weight:700;color:var(--lila-tx);margin-bottom:4px;">📦 LPNs — Traslado #${id}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:16px;">${esc(lpns.length)} paca(s)/caja(s) vinculadas</div>
      ${filas}
      <button onclick="this.closest('[style*=fixed]').remove()"
        style="width:100%;padding:14px;margin-top:16px;background:var(--bg-s);color:var(--tx3);border:1px solid var(--brd);border-radius:10px;cursor:pointer;font-size:var(--fs-sm);">
        Cerrar
      </button>
    </div>`;
  document.body.appendChild(modal);
}

/**
 * Confirm dispatch of a prepared traslado and notify Siesa.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasDespachar(id) {
  if (!await _modalConfirmar('¿Confirmar despacho? El operario ya preparó los ítems. Se notificará a Siesa (salida de bodega).', { titulo: 'Confirmar despacho' })) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/despachar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (r.ok) { alerta('Despachado — mercancía en tránsito', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Confirm reception of a traslado at the destination store.
 * @param {number} id - Traslado solicitud ID.
 */
// `trasConfirmarRecepcion` se retiró el 2026-09-17: no tenía UN SOLO caller
// —ni onclick, ni HTML, ni otra función— y además mandaba `{}`, que el guard
// de `confirmar_recepcion` rechaza desde agosto por exigir las cantidades
// contadas. O sea: código muerto que, de haberse alcanzado, habría fallado.
//
// El trinquete de integridad no lo vio porque mide COBERTURA DE URLS, no
// alcanzabilidad de funciones: `/api/traslados/<id>/recibir` sí lo consume la
// pantalla del recepcionista, así que la URL contaba como viva. Su propio
// comentario ya anticipa ese escalón pendiente.
//
// Quien reciba un traslado cuenta ítem por ítem desde Recepción → Traslados.

/**
 * Revert a traslado, returning units to the source warehouse inventory.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasRevertir(id) {
  const motivo = await _modalTexto('Revertir traslado', 'Motivo de la reversión (opcional): Ej. "Camión regresó — mercancía no entregada"', { obligatorio: false });
  if (motivo === null) return;
  if (!await _modalConfirmar('¿Revertir este traslado? Las unidades volverán al inventario del almacén.\n⚠ Deberás anular manualmente el STS en Siesa.', { titulo: 'Revertir traslado', peligro: true })) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/revertir`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo })
    });
    const d = await r.json();
    if (r.ok) { alerta(d.mensaje || 'Traslado revertido — unidades devueltas al inventario', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error al revertir', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Retry the Siesa reception entry (connector 173079) for a traslado.
 * @param {number} id - Traslado solicitud ID.
 */
/**
 * Dictamen del CD sobre una avería recibida — el cuarto momento de validación.
 *
 * Se pide nota SOLO cuando el veredicto es «no estaba averiada»: ese desenlace
 * contradice a quien la declaró en el punto, y la persona del punto tiene
 * derecho a leer por qué. Confirmar no contradice a nadie.
 */
async function trasDictaminarAveria(id, confirmada) {
  const titulo = confirmada
    ? 'Confirmar que la mercancía SÍ estaba averiada.\n\nVa a la zona de averías y sale el documento a la bodega de averías en Siesa.'
    : 'Dictaminar que NO estaba averiada.\n\nVuelve al inventario vendible y no sale ningún documento.';
  let nota = null;
  if (!confirmada) {
    nota = await _modalTexto('No estaba averiada', esc(titulo).replace(/\n/g, '<br>') +
      '<br><br>¿Por qué? (lo va a leer quien la declaró)',
      { obligatorio: true, textoConfirmar: 'Dictaminar' });
    if (nota === null) return;
  } else if (!(await _modalConfirmar(esc(titulo),
      { titulo: 'Sí estaba averiada', textoConfirmar: 'Confirmar avería' }))) {
    return;
  }
  try {
    const r = await fetch(API + `/api/traslados/${id}/dictaminar-averia`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirmada: confirmada, nota: nota })
    });
    const d = await r.json();
    if (r.ok) {
      alerta(confirmada ? 'Avería confirmada' : 'Dictaminada NO averiada', 'exito');
      cargarTrasladosAdmin();
    } else { alerta(d.error || 'No se pudo dictaminar', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

async function trasReintentarRecepcionSiesa(id) {
  if (!await _modalConfirmar(
    'El sistema ya verifica automáticamente en Siesa si el documento existe '
    + 'antes de reenviar. Solo llega hasta acá si esa verificación no encontró '
    + 'nada — pero si el documento SÍ existía y Siesa no devolvió un '
    + 'consecutivo legible, reintentar deja DOS ENTRADAS DUPLICADAS y el '
    + 'inventario de la bodega destino sube el doble. Anularla es un ajuste a '
    + 'mano en Siesa.',
    { titulo: '¿Reintentar entrada en Siesa (ETS 173079)?', peligro: true }
  )) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/reintentar-recepcion`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (r.ok) { alerta('Entrada Siesa registrada', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error al reintentar', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Retry the Siesa dispatch notification for a traslado without changing state.
 * @param {number} id - Traslado solicitud ID.
 */
async function trasReintentarDespachoSiesa(id) {
  if (!await _modalConfirmar(
    'El sistema ya verifica automáticamente en Siesa si el documento existe '
    + 'antes de reenviar. Solo llega hasta acá si esa verificación no encontró '
    + 'nada — pero si el documento SÍ existía y Siesa no devolvió un '
    + 'consecutivo legible, reintentar deja DOS SALIDAS DUPLICADAS y la '
    + 'bodega origen descarga el doble, puede quedar en negativo.\n\n'
    + 'No mueve el estado del traslado.',
    { titulo: '¿Reintentar salida en tránsito (STS 173076/174930)?', peligro: true }
  )) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/reintentar-despacho`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (r.ok) { alerta('Siesa notificado — despacho registrado', 'exito'); cargarTrasladosAdmin(); }
    else { alerta(d.error || 'Error al reintentar', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}



// Requisiciones — movido desde app.js 2026-07-21
// ─────────────────────────────────────────────────────────────────────────────
// MÓDULO REQUISICIONES — Solicitudes de traslado enviadas desde tienda
// Responsabilidad única: mostrar requisiciones y disparar despacho.
// No comparte estado ni funciones con tab-pedidos ni tab-traslados.
// ─────────────────────────────────────────────────────────────────────────────

const _REQ_ESTADOS = ['ENVIADA', 'EN_PICKING', 'EN_PACKING', 'PREPARADO', 'EN_TRANSITO', 'ENTREGADA'];
const REQ_TAB_LABELS = ['PENDIENTE APROBAR', 'EN PICKING', 'EN EMPAQUE', 'LISTO DESPACHAR', 'EN TRÁNSITO', 'RECIBIDO'];
let REQ_TAB_ACTIVO = 0;
let REQ_GRUPOS_COUNT = [0, 0, 0, 0, 0, 0];
// Antes se pedían las 6 listas completas en paralelo para pintar 6 badges y
// una sola pestaña visible — el resto del payload se descartaba sin usarlo.
// Ahora los badges salen de un GROUP BY liviano (`/conteos-por-estado`) y solo
// se trae la lista de la pestaña activa, paginada de verdad — antes, más de
// 30 solicitudes en un estado quedaban invisibles sin aviso (el límite del
// backend ya existía, solo faltaba el control de página en la pantalla).
let REQ_PAGINA = 1;
let REQ_TOTAL_PAGINAS = 1;
let REQ_CARGANDO = false;

/** Fetch and render transfer requisitions (RIT): conteos de las 6 pestañas + la página activa. */
async function cargarRequisiciones() {
  const lista = document.getElementById('req-lista');
  if (!lista) return;
  if (REQ_CARGANDO) return;
  REQ_CARGANDO = true;
  lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--tx3);">Cargando...</div>';
  try {
    const [conteos] = await Promise.all([
      get('/api/traslados/conteos-por-estado').catch(() => ({ conteos: {} })),
      _reqCargarPaginaActiva(),
    ]);
    REQ_GRUPOS_COUNT = _REQ_ESTADOS.map(e => conteos.conteos?.[e] || 0);
    _reqRenderTabs();
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--err-tx);">Error cargando requisiciones</div>';
  } finally {
    REQ_CARGANDO = false;
  }
}

/** Fetch solo la página actual de la pestaña activa y renderiza la lista + el paginador. */
async function _reqCargarPaginaActiva() {
  const lista = document.getElementById('req-lista');
  if (!lista) return;
  const estado = _REQ_ESTADOS[REQ_TAB_ACTIVO];
  const esRecibido = estado === 'ENTREGADA';
  try {
    const d = await get(`/api/traslados/?estado=${estado}&page=${REQ_PAGINA}`);
    // RECIBIDO ya está resuelto — se mantiene el mismo criterio de antes
    // (mostrar solo lo último para confirmar recepción, sin paginador) en vez
    // de convertirlo en un historial navegable que nadie pidió.
    const solicitudes = esRecibido ? (d.solicitudes || []).slice(0, 5) : (d.solicitudes || []);
    REQ_TOTAL_PAGINAS = esRecibido ? 1 : (d.paginas || 1);
    REQ_PAGINA = Math.min(REQ_PAGINA, REQ_TOTAL_PAGINAS);
    lista.innerHTML = (solicitudes.length
      ? solicitudes.map(s => _renderRequisicionCard(s)).join('')
      : '<div style="text-align:center;padding:40px;color:var(--tx3);">Sin requisiciones en esta pestaña ✓</div>')
      + _reqRenderPaginador();
  } catch (e) {
    lista.innerHTML = '<div style="text-align:center;padding:20px;color:var(--err-tx);">Error cargando requisiciones</div>';
  }
}

/** Igual patrón visual que el paginador de "Pedir" en Traslados (adminPedirRenderStock). */
function _reqRenderPaginador() {
  if (REQ_TOTAL_PAGINAS <= 1) return '';
  const rango = 2, desde = Math.max(1, REQ_PAGINA - rango), hasta = Math.min(REQ_TOTAL_PAGINAS, REQ_PAGINA + rango);
  const nums = [];
  if (desde > 1) nums.push('<span style="color:var(--tx3);">…</span>');
  for (let p = desde; p <= hasta; p++) {
    nums.push(`<button onclick="reqIrPagina(${p})" style="min-width:44px;min-height:44px;padding:6px 8px;background:${p===REQ_PAGINA?'var(--pm-fill)':'var(--bg-s2)'};color:#fff;border:none;border-radius:6px;font-size:var(--fs-xs);font-weight:${p===REQ_PAGINA?'700':'400'};cursor:pointer;">${p}</button>`);
  }
  if (hasta < REQ_TOTAL_PAGINAS) nums.push('<span style="color:var(--tx3);">…</span>');
  return `<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:14px 0 4px;flex-wrap:wrap;">
    <button onclick="reqIrPagina(${REQ_PAGINA-1})" ${REQ_PAGINA===1?'disabled':''} style="padding:7px 14px;background:var(--bg-s2);color:${REQ_PAGINA===1?'var(--tx3)':'var(--tx)'};border:none;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">← Ant</button>
    <div style="display:flex;gap:4px;flex-wrap:wrap;justify-content:center;">${nums.join('')}</div>
    <button onclick="reqIrPagina(${REQ_PAGINA+1})" ${REQ_PAGINA===REQ_TOTAL_PAGINAS?'disabled':''} style="padding:7px 14px;background:var(--bg-s2);color:${REQ_PAGINA===REQ_TOTAL_PAGINAS?'var(--tx3)':'var(--tx)'};border:none;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">Sig →</button>
  </div><div style="text-align:center;font-size:var(--fs-xs);color:var(--tx3);">pág ${REQ_PAGINA}/${REQ_TOTAL_PAGINAS}</div>`;
}

/** Render solo las pestañas (los badges de conteo) — la lista la pinta _reqCargarPaginaActiva. */
function _reqRenderTabs() {
  const tabsEl = document.getElementById('req-tabs');
  if (!tabsEl) return;
  tabsEl.innerHTML = REQ_TAB_LABELS.map((label, i) => {
    const count = REQ_GRUPOS_COUNT[i] || 0;
    return `<div class="subtab${i === REQ_TAB_ACTIVO ? ' active' : ''}" onclick="reqCambiarTab(${i})">${label}${count ? ` (${count})` : ''}</div>`;
  }).join('');
}

/** @param {number} idx - Requisition sub-tab index to activate. */
async function reqCambiarTab(idx) {
  REQ_TAB_ACTIVO = idx;
  REQ_PAGINA = 1;
  _reqRenderTabs();
  await _reqCargarPaginaActiva();
}

/** @param {number} p - Page number to navigate to within the active tab. */
async function reqIrPagina(p) {
  REQ_PAGINA = p;
  await _reqCargarPaginaActiva();
  document.getElementById('req-lista')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

const _REQ_BODEGA_NOMBRES = {
  'NB1':'Bodega Principal','NC1':'Neiva Centro','NS1':'Neiva Sur Principal',
  'NS2':'Neiva Sur Fundación (parqueo licitaciones)',
  'FC1':'Florencia Centro','PC1':'Pitalito Centro',
  'PT1':'Pitalito Terminal','FF1':'Feria Florencia','FN1':'Santa Lucía Plaza','FP1':'Feria Pitalito',
};
/**
 * @param {number} id - Warehouse ID.
 * @returns {string} Display name for the warehouse.
 */
function _reqNombreBodega(id) {
  return id ? (_REQ_BODEGA_NOMBRES[id] ? `${_REQ_BODEGA_NOMBRES[id]} (${id})` : id) : '—';
}

/**
 * @param {Object} r - Requisition object.
 * @returns {string} HTML card with status and action buttons.
 */
function _renderRequisicionCard(r) {
  const BADGE = {
    ENVIADA:     { color: '#d97706', bg: '#fef3c7', label: '⏳ Pendiente aprobar' },
    EN_PICKING:  { color: '#2563eb', bg: '#dbeafe', label: '🔍 En picking' },
    EN_PACKING:  { color: '#ea580c', bg: '#fff7ed', label: '📦 En empaque' },
    PREPARADO:   { color: '#7c3aed', bg: '#ede9fe', label: '✅ Listo despachar' },
    EN_TRANSITO: { color: '#0891b2', bg: '#cffafe', label: '🚚 En tránsito' },
    ENTREGADA:   { color: '#15803d', bg: '#dcfce7', label: '✓ Recibido' },
  };
  const badge  = BADGE[r.estado] || { color: '#6b7280', bg: '#f3f4f6', label: r.estado };
  const fecha  = r.fecha_creacion ? new Date(r.fecha_creacion).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
  const items  = (r.items || []);
  const totalUnd = items.reduce((s, i) => s + (i.cantidad_solicitada || 0), 0);

  const itemsHtml = items.slice(0, 4).map(i =>
    `<div style="display:flex;justify-content:space-between;font-size:var(--fs-xs);color:var(--tx3);padding:2px 0;">
      <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:70%;">${esc(i.producto_nombre || i.producto_codigo_siesa || '—')}</span>
      <span style="font-weight:600;color:var(--tx2);">${esc(i.cantidad_solicitada)}</span>
    </div>`
  ).join('');
  const masItems = items.length > 4
    ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">+${items.length - 4} más</div>`
    : '';

  const accionBtn =
    r.estado === 'ENVIADA'
      ? `<div style="display:flex;gap:6px;flex-wrap:wrap;">
           <button onclick="reqRechazar(${esc(r.id)})"
             style="padding:8px 14px;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;
                    background:#dc2626;color:#fff;border:none;">
             ✕ Rechazar
           </button>
           <button onclick="reqEditarAprobar(${esc(r.id)})"
             style="padding:8px 14px;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;
                    background:#1d4ed8;color:#fff;border:none;">
             ✏ Editar
           </button>
           <button onclick="aprobarRequisicion(${esc(r.id)})"
             style="padding:8px 14px;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;
                    background:#15803d;color:#fff;border:none;">
             ✓ Aprobar
           </button>
         </div>`
    : r.estado === 'EN_PICKING'
      ? `<span style="font-size:var(--fs-xs);color:var(--info-tx);font-weight:600;">🔍 Operario pickeando...</span>`
    : r.estado === 'EN_PACKING'
      ? `<div style="text-align:right;">
           <span style="font-size:var(--fs-xs);color:var(--orange);font-weight:600;">📦 Empacando en ${_reqNombreBodega(r.bodega_origen_siesa)}...</span>
           ${r.packing_info ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">${esc(r.packing_info.codigo)} · ${esc(r.packing_info.empacador || 'sin asignar')}</div>` : ''}
         </div>`
    : r.estado === 'PREPARADO'
      ? `<button onclick="despacharRequisicion(${esc(r.id)})"
           style="padding:8px 16px;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;
                  background:var(--bg-s);color:var(--tx);border:1px solid var(--brd);">
           🚚 Despachar
         </button>`
    : r.estado === 'EN_TRANSITO'
      ? `<span style="font-size:var(--fs-xs);color:var(--info-tx);font-weight:600;">🚚 En camino a ${_reqNombreBodega(r.bodega_destino_siesa)}</span>`
    : r.estado === 'ENTREGADA'
      ? `<span style="font-size:var(--fs-xs);color:var(--ok-tx);font-weight:600;">✓ Recibido${r.fecha_entrega ? ' · ' + new Date(r.fecha_entrega).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : ''}</span>`
    : '';

  return `
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:14px;margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx1);">${esc(r.codigo)}</div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:4px;flex-wrap:wrap;">
            <span style="font-size:var(--fs-xs);font-weight:600;padding:2px 7px;border-radius:4px;background:#1e3a5f;color:var(--info-tx);">
              📦 ${_reqNombreBodega(r.bodega_origen_siesa)}
            </span>
            <span style="font-size:var(--fs-xs);color:var(--tx3);">→</span>
            <span style="font-size:var(--fs-xs);font-weight:600;padding:2px 7px;border-radius:4px;background:var(--warn-bg);color:var(--orange);">
              🏪 ${r.nombre_punto_venta ? `${esc(r.nombre_punto_venta)} (${esc(r.bodega_destino_siesa || '')})` : _reqNombreBodega(r.bodega_destino_siesa)}
            </span>
          </div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:3px;">
            Solicita: <strong style="color:var(--tx2);">${esc(r.solicitante_nombre || '—')}</strong> · ${fecha}
          </div>
        </div>
        <span style="font-size:var(--fs-xs);font-weight:600;padding:3px 9px;border-radius:20px;
                     color:${esc(badge.color)};background:${esc(badge.bg)};">
          ${esc(badge.label)}
        </span>
      </div>
      <div style="background:var(--bg-s2);border-radius:8px;padding:8px;margin-bottom:10px;">
        ${itemsHtml}${masItems}
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;border-top:1px solid var(--brd);padding-top:4px;">
          ${esc(items.length)} producto${items.length !== 1 ? 's' : ''} · ${totalUnd} unidades
        </div>
      </div>
      <div style="display:flex;justify-content:flex-end;">
        ${accionBtn}
      </div>
    </div>`;
}

/** @param {number} id - Requisition ID to dispatch (triggers Siesa STS from RIT). */
async function despacharRequisicion(id) {
  if (!await _modalConfirmar('¿Confirmar despacho de esta requisición?', { titulo: 'Confirmar despacho' })) return;
  try {
    const r = await fetch(`/api/traslados/${id}/despachar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN }
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al despachar', 'error'); return; }
    alerta('Requisición despachada ✓', 'exito');
    await cargarRequisiciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

/** @param {number} id - Requisition ID to approve. */
async function aprobarRequisicion(id) {
  if (!await _modalConfirmar('¿Aprobar esta requisición? Se crearán las tareas de picking en Bodega.', { titulo: 'Aprobar requisición' })) return;
  try {
    const r = await fetch(`/api/traslados/${id}/aprobar`, {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({})
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error al aprobar', 'error'); return; }
    alerta('Requisición aprobada — el operario de traslado puede iniciar el picking ✓', 'exito');
    await cargarRequisiciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

/** @param {number} id - Requisition ID to reject. */
async function reqRechazar(id) {
  const motivo = await _modalTexto('Rechazar requisición', 'Motivo del rechazo:');
  if (!motivo) return;
  try {
    const r = await fetch(API + `/api/traslados/${id}/rechazar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo })
    });
    const d = await r.json();
    if (r.ok) { alerta('Solicitud rechazada', 'advertencia'); cargarRequisiciones(); }
    else { alerta(d.error || 'Error al rechazar', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** @param {number} id - Requisition ID to open the edit/approve modal for. */
async function reqEditarAprobar(id) {
  let solicitud, operariosData;
  try {
    [solicitud, operariosData] = await Promise.all([
      fetch(API + `/api/traslados/${id}`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
      fetch(API + `/api/traslados/operarios-disponibles`, { headers: { Authorization: 'Bearer ' + TOKEN } }).then(r => r.json()),
    ]);
  } catch (e) { alerta('Error de conexión', 'error'); return; }

  const items = solicitud.items || [];
  const operarios = operariosData.operarios || [];

  const filasItems = items.map(i => `
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
      <div style="flex:1;font-size:var(--fs-xs);">
        <div style="font-weight:600;">${esc(i.producto_nombre || i.producto_codigo)}</div>
        <div style="color:var(--tx3);font-size:var(--fs-xs);">Solicitado: ${esc(i.cantidad_solicitada)} · Disp. Siesa: ${i.disponible_siesa ?? '—'}</div>
      </div>
      <div style="display:flex;align-items:center;gap:4px;">
        <label style="font-size:var(--fs-xs);color:var(--tx2);">Aprobar:</label>
        <input type="number" id="req-apr-${esc(i.id)}" value="${esc(i.cantidad_solicitada)}" min="0"
          style="width:70px;padding:6px;background:var(--bg-input);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:var(--fs-sm);text-align:center;">
      </div>
    </div>
  `).join('');

  const opcionesOperarios = operarios.length
    ? `<option value="">Sin asignar (admin recoge)</option>` + operarios.map(o => `<option value="${esc(o.id)}">${esc(o.nombre)}</option>`).join('')
    : `<option value="">No hay operarios disponibles</option>`;

  const modal = document.createElement('div');
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;">
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:440px;border:1px solid #166534;max-height:85vh;overflow-y:auto;">
        <div style="font-size:17px;font-weight:700;margin-bottom:4px;">Editar y aprobar requisición</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:16px;">${esc(solicitud.nombre_punto_venta || solicitud.bodega_destino_siesa || '—')}</div>

        <div style="font-size:var(--fs-xs);font-weight:600;margin-bottom:8px;color:var(--tx2);">CANTIDADES A ENVIAR</div>
        ${filasItems}

        <div style="font-size:var(--fs-xs);font-weight:600;margin-top:14px;margin-bottom:8px;color:var(--tx2);">OPERARIO QUE RECOGE</div>
        <select id="req-apr-operario"
          style="width:100%;padding:10px;background:var(--bg-input);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:var(--fs-sm);margin-bottom:16px;">
          ${opcionesOperarios}
        </select>

        <div style="display:flex;gap:8px;">
          <button id="btn-req-apr-ok" style="flex:1;padding:12px;background:#166534;color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">Aprobar</button>
          <button onclick="this.closest('[style*=fixed]').parentElement.remove()" style="padding:12px 16px;background:var(--bg-s2);color:var(--tx);border:none;border-radius:8px;font-size:var(--fs-sm);cursor:pointer;">Cancelar</button>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  modal.querySelector('#btn-req-apr-ok').onclick = async () => {
    const items_aprobados = items.map(i => ({
      id: i.id,
      cantidad_aprobada: Number(document.getElementById(`req-apr-${i.id}`).value) || 0
    }));
    const operario_id = document.getElementById('req-apr-operario').value
      ? Number(document.getElementById('req-apr-operario').value) : null;
    modal.remove();
    try {
      const r = await fetch(API + `/api/traslados/${id}/aprobar`, {
        method: 'POST',
        headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
        body: JSON.stringify({ items_aprobados, operario_id })
      });
      const d = await r.json();
      if (r.ok) {
        alerta(operario_id ? 'Aprobado — operario notificado' : 'Aprobado — sin operario asignado', 'exito');
        cargarRequisiciones();
      } else { alerta(d.error || 'Error', 'error'); }
    } catch (e) { alerta('Error de conexión', 'error'); }
  };
}
