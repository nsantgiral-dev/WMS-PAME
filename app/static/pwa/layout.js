// ═══════════════════════════════════════════════════════════════════════════
// MÓDULO LAYOUT — ubicaciones físicas del almacén (PICKING/RESERVA/AVERIAS)
// Dependencias globales (de app.js): get(), post(), alerta(), set(),
//   API, TOKEN, ALMACEN_ID
// ═══════════════════════════════════════════════════════════════════════════

// MÓDULO LAYOUT — ubicaciones gestionadas 100% en el WMS, independiente de
// Reposición. Este módulo es el cimiento (crear/clasificar/poblar); Reposición,
// Picking, Recepción, Averías y Compras son consumidores de estos datos.
// ─────────────────────────────────────────────────────────────────────────────

let _layoutSubActual = 'ubicaciones';
let _layoutZonaActual = 'PICKING';
let _layoutUbicacionesCache = [];
let _layoutUltimoCuerpo = null;   // { pasillo, fila, cuerpo, zona, entrepanos, huecosPorNivel } — para "repetir"
let _layoutUbAsignarId = null;
let _layoutProductoId = null;

const _ZONA_COLOR = { PICKING: '#60a5fa', RESERVA: '#22c55e', AVERIAS: '#ef4444', IMPORTADOS: '#f59e0b', GENERAL: '#555' };
const _ZONAS_TABS = ['PICKING', 'RESERVA', 'AVERIAS', 'IMPORTADOS', 'GENERAL'];
// Zonas donde el Hueco nunca se comparte (1 SKU <-> 1 ubicación) — replica
// _ZONAS_SLOT_UNICO de layout_service.py: ahí sí tiene sentido pedir
// "capacidad máxima del hueco" porque hueco y SKU son la misma cosa.
const _ZONAS_SLOT_UNICO = ['PICKING', 'IMPORTADOS'];

/**
 * Switch the active zone tab and re-render ubicaciones for that zone.
 * @param {string} zona - Zone key: 'PICKING', 'RESERVA', 'AVERIAS', or 'GENERAL'.
 */
function layoutZonaTab(zona) {
  _layoutZonaActual = zona;
  _ZONAS_TABS.forEach(z => {
    const btn = document.getElementById(`layout-zona-tab-${z}`);
    if (!btn) return;
    const activo = z === zona;
    const color = _ZONA_COLOR[z];
    btn.style.background = activo ? color : 'transparent';
    btn.style.color = activo ? '#fff' : color;
  });
  layoutRenderUbicaciones();
}

/**
 * Switch the layout sub-tab between ubicaciones and import views.
 * @param {string} sec - Section key: 'ubicaciones' or 'importar'.
 */
function layoutSubtab(sec) {
  _layoutSubActual = sec;
  ['ubicaciones', 'importar'].forEach(s => {
    const cont = document.getElementById(`layout-sec-${s}`);
    const btn  = document.getElementById(`layout-sub-${s}`);
    if (!cont || !btn) return;
    const activo = s === sec;
    cont.style.display = activo ? 'block' : 'none';
    btn.style.background = activo ? 'var(--pm)' : 'transparent';
    btn.style.color = activo ? '#fff' : 'var(--tx3)';
    btn.style.fontWeight = activo ? '700' : '400';
  });
  if (sec === 'ubicaciones') layoutCargarUbicaciones();
}

/** Entry point to load the layout module with the current sub-tab. */
async function cargarLayout() {
  layoutSubtab(_layoutSubActual);
}

/**
 * Build the HTML card for a single ubicacion with actions.
 * @param {Object} u - Ubicacion object with code, zone, stock, and product data.
 * @returns {string} HTML string for the ubicacion card.
 */
function _layoutRenderUbicacionCard(u) {
  const color = _ZONA_COLOR[u.tipo_zona] || '#888';
  const skuLabel = u.producto_asignado_codigo
    ? `📦 ${u.producto_asignado_codigo}${u.producto_asignado_nombre ? ' — ' + u.producto_asignado_nombre : ''}`
    : (u.tipo_zona === 'PICKING' ? 'Sin SKU asignado' : null);

  return `
    <div class="tabla-card" style="margin-bottom:10px;">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:15px;font-weight:800;font-family:monospace;color:var(--tx);">${u.codigo}</div>
          ${skuLabel ? `<div style="font-size:11px;color:${u.producto_asignado_codigo ? '#60a5fa' : '#555'};margin-top:3px;font-weight:600;">${skuLabel}</div>` : ''}
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;justify-content:flex-end;">
          <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${u.tipo_zona}</span>
          ${!u.activo ? `<span style="font-size:10px;color:#888;background:#88888822;padding:3px 8px;border-radius:20px;">INACTIVA</span>` : ''}
        </div>
      </div>
      <div style="display:flex;gap:16px;font-size:11px;color:var(--tx3);margin-bottom:10px;">
        <span>Stock <strong style="color:var(--tx);">${u.stock_actual ?? 0}</strong></span>
        ${u.capacidad_maxima != null ? `<span>Capacidad <strong style="color:var(--tx);">${u.capacidad_maxima}</strong></span>` : ''}
        <span style="color:#666;">${u.origen === 'MANUAL' ? 'WMS' : 'Siesa'}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button onclick="layoutAbrirModalAsignar(${u.id}, '${u.codigo}', '${u.tipo_zona}')"
          style="flex:1;min-width:90px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:12px;cursor:pointer;">
          Asignar SKU
        </button>
        <button onclick="layoutAbrirModalEditarUbicacion(${u.id})"
          style="flex:1;min-width:90px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:12px;cursor:pointer;">
          Editar
        </button>
        <button onclick="layoutEliminarUbicacion(${u.id}, '${u.codigo}')"
          style="flex:1;min-width:90px;padding:8px;background:var(--bg);border:1px solid #7f1d1d;border-radius:6px;color:#f87171;font-size:12px;cursor:pointer;">
          Eliminar
        </button>
        <button onclick="layoutAbrirModalReclasificar(${u.id})"
          style="flex:1;min-width:90px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:12px;cursor:pointer;">
          Reclasificar
        </button>
      </div>
    </div>`;
}

/** Fetch all ubicaciones for the current almacen and trigger zone rendering. */
async function layoutCargarUbicaciones() {
  const el = document.getElementById('layout-lista-ubicaciones');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';
  try {
    const d = await get(`/api/almacenes/${ALMACEN_ID}/layout`);
    _layoutUbicacionesCache = d.ubicaciones || [];
    layoutZonaTab(_layoutZonaActual);
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando el layout</div>';
  }
}

// Pinta solo la zona activa (pestaña). Dentro de ella, cada grupo — fila legada,
// Cuerpo, o ubicación suelta (AVERIAS/GENERAL) — se ordena por la fecha de
// creación de su miembro más nuevo: lo que se acaba de crear queda primero.
/** Render cached ubicaciones grouped by fila/cuerpo for the active zone tab. */
function layoutRenderUbicaciones() {
  const el = document.getElementById('layout-lista-ubicaciones');
  if (!el) return;

  _ZONAS_TABS.forEach(z => {
    const count = _layoutUbicacionesCache.filter(u => u.tipo_zona === z).length;
    const badge = document.getElementById(`layout-zona-count-${z}`);
    if (badge) badge.textContent = `(${count})`;
  });

  const items = _layoutUbicacionesCache.filter(u => u.tipo_zona === _layoutZonaActual);

  const resumen = document.getElementById('layout-resumen');
  if (resumen) resumen.textContent = `${items.length} ubicación(es) en ${_layoutZonaActual}`;

  if (!items.length) {
    el.innerHTML = `
      <div style="text-align:center;padding:40px;color:#555;">
        <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">🧭</div>
        <div style="font-size:15px;font-weight:600;">Sin ubicaciones en ${_layoutZonaActual}</div>
        <div style="font-size:12px;margin-top:6px;">Usa "+ Crear ubicación" para empezar a armar el layout de esta bodega</div>
      </div>`;
    return;
  }

  const ts = u => u.fecha_creacion ? new Date(u.fecha_creacion).getTime() : 0;

  // legado: fila plana (estante) — mecanismo anterior al rediseño de 5 ejes.
  // cuerpo: Cuerpo -> Entrepaño (nivel) -> Huecos — mecanismo nuevo (Mecanismo A).
  //         un Cuerpo puede tener entrepaños en varias zonas; al filtrar `items`
  //         por zona antes de agrupar, cada grupo de cuerpo queda ya con solo
  //         los entrepaños de la pestaña activa.
  // suelta: AVERIAS/GENERAL sin dirección física de cuerpo, una por grupo.
  const grupos = [];
  const filasPorClave = new Map();
  const cuerposPorClave = new Map();

  items.forEach(u => {
    if (u.pasillo && u.estante) {
      const clave = `${u.pasillo}|${u.estante}`;
      let g = filasPorClave.get(clave);
      if (!g) {
        g = { tipo: 'legado', pasillo: u.pasillo, estante: u.estante, items: [] };
        filasPorClave.set(clave, g);
        grupos.push(g);
      }
      g.items.push(u);
    } else if (u.pasillo && u.fila != null && u.cuerpo != null) {
      const clave = `${u.pasillo}|${u.fila}|${u.cuerpo}`;
      let g = cuerposPorClave.get(clave);
      if (!g) {
        g = { tipo: 'cuerpo', pasillo: u.pasillo, fila: u.fila, cuerpo: u.cuerpo, niveles: new Map(), items: [] };
        cuerposPorClave.set(clave, g);
        grupos.push(g);
      }
      if (!g.niveles.has(u.nivel)) g.niveles.set(u.nivel, []);
      g.niveles.get(u.nivel).push(u);
      g.items.push(u);
    } else {
      grupos.push({ tipo: 'suelta', items: [u] });
    }
  });

  grupos.forEach(g => {
    g.items.sort((a, b) => a.codigo.localeCompare(b.codigo));
    g.masReciente = Math.max(...g.items.map(ts));
  });
  grupos.sort((a, b) => b.masReciente - a.masReciente);

  let html = '';
  grupos.forEach(g => {
    if (g.tipo === 'legado') {
      const codigoFila = `${g.pasillo}${String(g.estante).padStart(2, '0')}`;
      html += `
        <div style="display:flex;justify-content:space-between;align-items:center;background:var(--bg-s2);border-radius:8px;padding:8px 12px;margin:16px 0 8px;">
          <div style="font-size:12px;font-weight:700;color:var(--tx2);">Fila ${codigoFila} · ${_layoutZonaActual} · ${g.items.length} posición(es)</div>
          <div style="display:flex;gap:6px;">
            <button onclick="layoutAbrirModalEditarFila('${g.pasillo}','${g.estante}')"
              style="padding:5px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:11px;cursor:pointer;">
              ✏ Editar
            </button>
            <button onclick="layoutAbrirModalEliminarFila('${g.pasillo}','${g.estante}')"
              style="padding:5px 10px;background:var(--bg);border:1px solid #7f1d1d;border-radius:6px;color:#f87171;font-size:11px;cursor:pointer;">
              🗑 Eliminar
            </button>
          </div>
        </div>`;
      g.items.forEach(u => { html += _layoutRenderUbicacionCard(u); });
    } else if (g.tipo === 'cuerpo') {
      // Nomenclatura real del código: {PREFIJO_ZONA}-{PASILLO}{FILA}-C{CUERPO} — ej. PIK-A1-C01.
      const codigoCuerpo = g.items[0].codigo.split('-').slice(0, 3).join('-');
      const nivelesEnZona = [...g.niveles.keys()].sort((a, b) => a - b);
      const idsCuerpoCsv = g.items.map(u => u.id).join(',');
      const zonaCuerpo = g.items[0].tipo_zona; // un cuerpo es 100% de una sola zona
      let cuerpoHtml = `
        <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap;">
          <div style="font-size:20px;font-weight:800;font-family:monospace;color:var(--tx);">${codigoCuerpo}</div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button onclick="layoutImprimirEtiquetasCuerpo('${idsCuerpoCsv}')"
              style="padding:7px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:11px;font-weight:700;cursor:pointer;">
              🖨 Etiquetas
            </button>
            <button onclick="layoutAbrirModalEditarCuerpo('${g.pasillo}', ${g.fila}, ${g.cuerpo}, ${nivelesEnZona.length})"
              style="padding:7px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:11px;font-weight:700;cursor:pointer;">
              ✏ Editar
            </button>
            <button onclick="layoutAbrirModalReclasificarCuerpo('${g.pasillo}', ${g.fila}, ${g.cuerpo}, '${zonaCuerpo}')"
              style="padding:7px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:11px;font-weight:700;cursor:pointer;">
              ⇄ Reclasificar
            </button>
            <button onclick="layoutEliminarCuerpo('${g.pasillo}', ${g.fila}, ${g.cuerpo})"
              style="padding:7px 10px;background:var(--bg);border:1px solid #7f1d1d;border-radius:6px;color:#f87171;font-size:11px;font-weight:700;cursor:pointer;">
              🗑 Eliminar
            </button>
          </div>
        </div>`;
      nivelesEnZona.forEach((nivel, idx) => {
        const huecos = g.niveles.get(nivel).sort((a, b) => a.hueco - b.hueco);
        cuerpoHtml += _layoutRenderEntrepanoSeccion(huecos, idx === 0);
      });
      html += `<div class="tabla-card" style="margin-top:16px;">${cuerpoHtml}</div>`;
    } else {
      g.items.forEach(u => { html += _layoutRenderUbicacionCard(u); });
    }
  });

  el.innerHTML = html;
}

// ── Modal: Crear Cuerpo completo (Mecanismo A), wizard de 2 pasos ────────────
// Dirección de 5 ejes: Pasillo -> Fila (1/2) -> Cuerpo -> Entrepaños -> Huecos.
// Un Cuerpo es 100% de una sola zona — PICKING, RESERVA e IMPORTADOS se arman
// como Cuerpos separados (botones "+ Crear picking" / "+ Crear reserva" /
// "+ Crear importados"), no mezclados por
// Nivel dentro del mismo Cuerpo: así cada Cuerpo se revisa completo en una sola
// pestaña de zona. Paso 1 define el cuerpo (pasillo/fila/número/cantidad de
// entrepaños); paso 2 pide, entrepaño por entrepaño, cuántos huecos tiene —
// variable, no un único número para todo el cuerpo, porque la profundidad
// física no es uniforme. El SKU de cada hueco se asigna después, aparte
// (Asignar SKU, Mecanismo B).

let _layoutCuerpoZona = 'PICKING';
let _layoutCuerpoHuecosPrevios = null; // huecosPorNivel del último cuerpo creado, para "repetir"

/** Open the 2-step wizard modal to create a full cuerpo (body) with entrepanos and huecos. */
async function layoutAbrirModalCuerpo(zona) {
  const m = document.getElementById('modal-layout-cuerpo');
  if (!m) return;
  _layoutCuerpoZona = zona;

  document.getElementById('layout-cuerpo-titulo').textContent =
    zona === 'RESERVA' ? 'Crear Cuerpo — RESERVA' : 'Crear Cuerpo — PICKING';
  document.getElementById('layout-cuerpo-zona-hint').textContent =
    `El entrepaño 1 es el más bajo (piso) y sube desde ahí. Todo este Cuerpo queda en zona ${zona}.`;

  const existentes = [...new Set(_layoutUbicacionesCache.map(u => u.pasillo).filter(Boolean))].sort();
  let disponibles = [];
  try {
    const d = await get(`/api/almacenes/${ALMACEN_ID}/pasillos-disponibles?cantidad=5`);
    disponibles = d.letras || [];
  } catch (_) {}

  const sel = document.getElementById('layout-cuerpo-pasillo');
  let html = '';
  if (existentes.length) {
    html += `<optgroup label="Pasillos existentes">` +
      existentes.map(p => `<option value="${p}">${p}</option>`).join('') + `</optgroup>`;
  }
  html += `<optgroup label="Pasillos nuevos">` +
    disponibles.map(p => `<option value="${p}">${p} (nuevo)</option>`).join('') + `</optgroup>`;
  sel.innerHTML = html;

  document.getElementById('layout-cuerpo-fila').value = '1';
  document.getElementById('layout-cuerpo-numero').value = '';
  document.getElementById('layout-cuerpo-entrepanos').value = '';
  document.getElementById('layout-cuerpo-huecos-container').innerHTML = '';
  _layoutCuerpoHuecosPrevios = null;

  // "Repetir" solo tiene sentido si el último cuerpo creado fue de la misma zona.
  const btnRepetir = document.getElementById('layout-cuerpo-btn-repetir');
  if (btnRepetir) btnRepetir.style.display = (_layoutUltimoCuerpo && _layoutUltimoCuerpo.zona === zona) ? 'block' : 'none';

  document.getElementById('layout-cuerpo-paso1').style.display = 'block';
  document.getElementById('layout-cuerpo-paso2').style.display = 'none';
  m.style.display = 'flex';
}

/** Close the cuerpo creation wizard modal. */
function layoutCerrarModalCuerpo() {
  const m = document.getElementById('modal-layout-cuerpo');
  if (m) m.style.display = 'none';
}

/** Pre-fill the cuerpo form with the last created cuerpo's values (next number). */
function layoutRepetirCuerpoAnterior() {
  if (!_layoutUltimoCuerpo) return;
  document.getElementById('layout-cuerpo-pasillo').value = _layoutUltimoCuerpo.pasillo;
  document.getElementById('layout-cuerpo-fila').value = _layoutUltimoCuerpo.fila;
  document.getElementById('layout-cuerpo-numero').value = _layoutUltimoCuerpo.cuerpo + 1;
  document.getElementById('layout-cuerpo-entrepanos').value = _layoutUltimoCuerpo.entrepanos;
  _layoutCuerpoHuecosPrevios = _layoutUltimoCuerpo.huecosPorNivel;
}

/** Validate step 1 inputs and advance to step 2 (huecos per entrepano). */
function layoutCuerpoIrAPaso2() {
  const pasillo = document.getElementById('layout-cuerpo-pasillo').value;
  const fila = parseInt(document.getElementById('layout-cuerpo-fila').value);
  const cuerpo = parseInt(document.getElementById('layout-cuerpo-numero').value);
  const cantidad_entrepanos = parseInt(document.getElementById('layout-cuerpo-entrepanos').value);

  if (!pasillo || isNaN(fila) || isNaN(cuerpo) || isNaN(cantidad_entrepanos) || cantidad_entrepanos < 1) {
    alerta('Completa pasillo, fila, cuerpo y cantidad de entrepaños', 'error');
    return;
  }

  document.getElementById('layout-cuerpo-paso2-titulo').textContent =
    `Cuerpo ${pasillo}${fila}-${String(cuerpo).padStart(2, '0')} — huecos por entrepaño`;

  const cont = document.getElementById('layout-cuerpo-huecos-container');
  let html = '';
  for (let nivel = 1; nivel <= cantidad_entrepanos; nivel++) {
    const valor = (_layoutCuerpoHuecosPrevios && _layoutCuerpoHuecosPrevios[nivel - 1]) || 1;
    html += `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
        <div style="flex:1;font-size:12px;color:var(--tx2);">Entrepaño ${nivel}</div>
        <input id="layout-cuerpo-hueco-nivel-${nivel}" type="number" min="1" value="${valor}"
          style="width:72px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:14px;box-sizing:border-box;text-align:center;">
      </div>`;
  }
  cont.innerHTML = html;

  document.getElementById('layout-cuerpo-paso1').style.display = 'none';
  document.getElementById('layout-cuerpo-paso2').style.display = 'block';
}

/** Go back from step 2 to step 1 in the cuerpo creation wizard. */
function layoutCuerpoVolverAPaso1() {
  document.getElementById('layout-cuerpo-paso2').style.display = 'none';
  document.getElementById('layout-cuerpo-paso1').style.display = 'block';
}

/** Submit the cuerpo creation with all entrepanos and huecos to the API. */
async function layoutGuardarCuerpo() {
  const pasillo = document.getElementById('layout-cuerpo-pasillo').value;
  const fila = parseInt(document.getElementById('layout-cuerpo-fila').value);
  const cuerpo = parseInt(document.getElementById('layout-cuerpo-numero').value);
  const cantidad_entrepanos = parseInt(document.getElementById('layout-cuerpo-entrepanos').value);

  const huecos_por_nivel = [];
  for (let nivel = 1; nivel <= cantidad_entrepanos; nivel++) {
    const valor = parseInt(document.getElementById(`layout-cuerpo-hueco-nivel-${nivel}`)?.value);
    huecos_por_nivel.push(isNaN(valor) || valor < 1 ? 1 : valor);
  }

  try {
    const payload = { pasillo, fila, cuerpo, cantidad_entrepanos, tipo_zona: _layoutCuerpoZona, huecos_por_nivel };
    await post(`/api/almacenes/${ALMACEN_ID}/ubicaciones/cuerpo`, payload);
    _layoutUltimoCuerpo = { pasillo, fila, cuerpo, zona: _layoutCuerpoZona, entrepanos: cantidad_entrepanos, huecosPorNivel: huecos_por_nivel };
    const totalHuecos = huecos_por_nivel.reduce((a, b) => a + b, 0);
    alerta(`Cuerpo ${pasillo}${fila}-${String(cuerpo).padStart(2,'0')} (${_layoutCuerpoZona}) creado — ${cantidad_entrepanos} entrepaño(s), ${totalHuecos} hueco(s)`, 'ok');
    layoutCerrarModalCuerpo();
    layoutCargarUbicaciones();
  } catch (e) {
    alerta(e.message || 'Error creando el cuerpo', 'error');
  }
}

// ── Cuerpo completo: Editar (remodular) / Eliminar / Reclasificar ───────────
// A diferencia de los 3 iconos dentro de "Ver" (que actúan sobre UN hueco),
// estos botones del encabezado del Cuerpo actúan sobre TODOS sus
// entrepaños/huecos a la vez — ver layout_service.py editar_cuerpo() /
// eliminar_cuerpo() / reclasificar_cuerpo().

let _layoutCuerpoEditando = null; // { pasillo, fila, cuerpo }

/** Open the remodulate-cuerpo wizard (2 steps, same shape as crear), pre-filled with the current entrepaño count. */
function layoutAbrirModalEditarCuerpo(pasillo, fila, cuerpo, cantidadActual) {
  const m = document.getElementById('modal-layout-editar-cuerpo');
  if (!m) return;
  _layoutCuerpoEditando = { pasillo, fila, cuerpo };
  const codigoCuerpo = `${pasillo}${fila}-C${String(cuerpo).padStart(2, '0')}`;
  document.getElementById('layout-editar-cuerpo-titulo').textContent = `Editar ${codigoCuerpo}`;
  document.getElementById('layout-editar-cuerpo-entrepanos').value = cantidadActual;
  document.getElementById('layout-editar-cuerpo-huecos-container').innerHTML = '';
  document.getElementById('layout-editar-cuerpo-paso1').style.display = 'block';
  document.getElementById('layout-editar-cuerpo-paso2').style.display = 'none';
  m.style.display = 'flex';
}

/** Close the remodulate-cuerpo wizard modal. */
function layoutCerrarModalEditarCuerpo() {
  const m = document.getElementById('modal-layout-editar-cuerpo');
  if (m) m.style.display = 'none';
  _layoutCuerpoEditando = null;
}

/** Validate step 1 (new entrepaño count) and advance to step 2 (huecos per entrepaño). */
function layoutEditarCuerpoIrAPaso2() {
  const cantidad_entrepanos = parseInt(document.getElementById('layout-editar-cuerpo-entrepanos').value);
  if (isNaN(cantidad_entrepanos) || cantidad_entrepanos < 1) {
    alerta('Indica la nueva cantidad de entrepaños', 'error');
    return;
  }
  const cont = document.getElementById('layout-editar-cuerpo-huecos-container');
  let html = '';
  for (let nivel = 1; nivel <= cantidad_entrepanos; nivel++) {
    html += `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
        <div style="flex:1;font-size:12px;color:var(--tx2);">Entrepaño ${nivel}</div>
        <input id="layout-editar-cuerpo-hueco-nivel-${nivel}" type="number" min="1" value="1"
          style="width:72px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:14px;box-sizing:border-box;text-align:center;">
      </div>`;
  }
  cont.innerHTML = html;
  document.getElementById('layout-editar-cuerpo-paso1').style.display = 'none';
  document.getElementById('layout-editar-cuerpo-paso2').style.display = 'block';
}

/** Go back from step 2 to step 1 in the remodulate-cuerpo wizard. */
function layoutEditarCuerpoVolverAPaso1() {
  document.getElementById('layout-editar-cuerpo-paso2').style.display = 'none';
  document.getElementById('layout-editar-cuerpo-paso1').style.display = 'block';
}

/** Submit the cuerpo remodulation (PUT) — wipes and rebuilds with the new entrepaño/hueco count. */
async function layoutGuardarEditarCuerpo() {
  if (!_layoutCuerpoEditando) return;
  const { pasillo, fila, cuerpo } = _layoutCuerpoEditando;
  const cantidad_entrepanos = parseInt(document.getElementById('layout-editar-cuerpo-entrepanos').value);
  const huecos_por_nivel = [];
  for (let nivel = 1; nivel <= cantidad_entrepanos; nivel++) {
    const valor = parseInt(document.getElementById(`layout-editar-cuerpo-hueco-nivel-${nivel}`)?.value);
    huecos_por_nivel.push(isNaN(valor) || valor < 1 ? 1 : valor);
  }
  try {
    const r = await fetch(API + `/api/almacenes/${ALMACEN_ID}/ubicaciones/cuerpo`, {
      method: 'PUT',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ pasillo, fila, cuerpo, cantidad_entrepanos, huecos_por_nivel }),
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error remodulando el cuerpo', 'error'); return; }
    const totalHuecos = huecos_por_nivel.reduce((a, b) => a + b, 0);
    alerta(`Cuerpo remodulado — ${cantidad_entrepanos} entrepaño(s), ${totalHuecos} hueco(s). Vuelve a asignar los SKU.`, 'ok');
    layoutCerrarModalEditarCuerpo();
    layoutCargarUbicaciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

/**
 * Delete an entire cuerpo (all its entrepanos/huecos) — blocks (all-or-nothing)
 * if any hueco has stock or real operational history.
 */
async function layoutEliminarCuerpo(pasillo, fila, cuerpo, forzar) {
  const codigoCuerpo = `${pasillo}${fila}-C${String(cuerpo).padStart(2, '0')}`;
  const msg = forzar
    ? `⚠ FORZAR eliminación de TODO el cuerpo ${codigoCuerpo} — esto borra también el historial real de picking/reposición de cada hueco, PARA SIEMPRE. ¿Continuar?`
    : `¿Eliminar TODO el cuerpo ${codigoCuerpo} (todos sus entrepaños y huecos)? Esta acción no se puede deshacer. Se bloquea completo si algún hueco tiene stock o historial real — usa "Reclasificar > Desactivar cuerpo" en ese caso.`;
  if (!confirm(msg)) return;
  try {
    const r = await fetch(API + `/api/almacenes/${ALMACEN_ID}/ubicaciones/cuerpo`, {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ pasillo, fila, cuerpo, forzar: !!forzar }),
    });
    const d = await r.json();
    if (!r.ok) {
      if (!forzar && r.status === 400) {
        alerta(d.error || 'Error eliminando el cuerpo', 'error');
        if (confirm(`Bloqueado: ${d.error}\n\n¿Forzar de todos modos? Requiere rol admin y borra el historial para siempre.`)) {
          return layoutEliminarCuerpo(pasillo, fila, cuerpo, true);
        }
        return;
      }
      alerta(d.error || 'Error eliminando el cuerpo', 'error');
      return;
    }
    alerta(`Cuerpo ${codigoCuerpo} eliminado${forzar ? ' (forzado)' : ''} — ${d.total} hueco(s)`, 'ok');
    layoutCargarUbicaciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

let _layoutCuerpoReclasificando = null; // { pasillo, fila, cuerpo }

/** Open the bulk zone-change / deactivate modal for an entire cuerpo. */
function layoutAbrirModalReclasificarCuerpo(pasillo, fila, cuerpo, zonaActual) {
  const m = document.getElementById('modal-layout-reclasificar-cuerpo');
  if (!m) return;
  _layoutCuerpoReclasificando = { pasillo, fila, cuerpo };
  const codigoCuerpo = `${pasillo}${fila}-C${String(cuerpo).padStart(2, '0')}`;
  document.getElementById('layout-reclasificar-cuerpo-codigo').textContent = codigoCuerpo;
  document.getElementById('layout-reclasificar-cuerpo-zona').value = zonaActual;
  document.getElementById('layout-reclasificar-cuerpo-desactivar').checked = false;
  document.getElementById('layout-reclasificar-cuerpo-resultado').innerHTML = '';
  m.style.display = 'flex';
}

/** Close the bulk reclassify-cuerpo modal. */
function layoutCerrarModalReclasificarCuerpo() {
  const m = document.getElementById('modal-layout-reclasificar-cuerpo');
  if (m) m.style.display = 'none';
  _layoutCuerpoReclasificando = null;
}

/**
 * Save the bulk reclassification — either a zone change for the whole cuerpo,
 * or deactivating it completely (the safe path to retire a cuerpo blocked
 * from deletion by real history, without losing the audit trail).
 */
async function layoutGuardarReclasificarCuerpo() {
  if (!_layoutCuerpoReclasificando) return;
  const { pasillo, fila, cuerpo } = _layoutCuerpoReclasificando;
  const desactivar = document.getElementById('layout-reclasificar-cuerpo-desactivar').checked;
  const tipo_zona = document.getElementById('layout-reclasificar-cuerpo-zona').value;

  const payload = { pasillo, fila, cuerpo };
  if (desactivar) {
    payload.activo = false;
  } else {
    payload.tipo_zona = tipo_zona;
  }

  try {
    const r = await fetch(API + `/api/almacenes/${ALMACEN_ID}/ubicaciones/cuerpo`, {
      method: 'PATCH',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error reclasificando el cuerpo', 'error'); return; }

    const bloqueadas = Object.keys(d.bloqueadas || {});
    const resEl = document.getElementById('layout-reclasificar-cuerpo-resultado');
    resEl.innerHTML = bloqueadas.length
      ? `⚠ ${bloqueadas.length} hueco(s) con stock, no se pudieron cambiar: ${bloqueadas.join(', ')}`
      : '';
    alerta(
      `${d.actualizadas.length} hueco(s) actualizado(s)${bloqueadas.length ? ` — ${bloqueadas.length} bloqueado(s)` : ''}`,
      bloqueadas.length ? 'advertencia' : 'ok',
    );
    layoutCargarUbicaciones();
    if (!bloqueadas.length) setTimeout(layoutCerrarModalReclasificarCuerpo, 200);
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ── Modal: Editar fila (bloque ya creado) ────────────────────────────────────

let _layoutFilaEnEdicion = null; // { pasillo, fila }

/**
 * Open the modal to bulk-edit all positions in a legacy fila (row).
 * @param {string} pasillo - Aisle letter.
 * @param {string} fila - Row number within the aisle.
 */
function layoutAbrirModalEditarFila(pasillo, fila) {
  const m = document.getElementById('modal-layout-editar-fila');
  if (!m) return;

  _layoutFilaEnEdicion = { pasillo, fila };
  const codigoFila = `${pasillo}${String(fila).padStart(2, '0')}`;
  const count = (_layoutUbicacionesCache || []).filter(u => u.pasillo === pasillo && u.estante === fila).length;
  document.getElementById('layout-editar-fila-titulo').textContent = `Editando fila ${codigoFila} · ${count} posición(es)`;

  document.getElementById('layout-editar-fila-zona').value = '';
  document.getElementById('layout-editar-fila-capacidad').value = '';
  document.getElementById('layout-editar-fila-activo').value = '';
  document.getElementById('layout-editar-fila-resultado').innerHTML = '';

  m.style.display = 'flex';
}

/** Close the fila edit modal. */
function layoutCerrarModalEditarFila() {
  const m = document.getElementById('modal-layout-editar-fila');
  if (m) m.style.display = 'none';
}

/** Save bulk edits (zone, capacity, active) for all positions in the fila. */
async function layoutGuardarEditarFila() {
  if (!_layoutFilaEnEdicion) return;
  const { pasillo, fila } = _layoutFilaEnEdicion;
  const tipo_zona = document.getElementById('layout-editar-fila-zona').value || null;
  const capacidadRaw = document.getElementById('layout-editar-fila-capacidad').value;
  const capacidad_maxima = capacidadRaw !== '' ? parseInt(capacidadRaw) : null;
  const activoRaw = document.getElementById('layout-editar-fila-activo').value;
  const activo = activoRaw !== '' ? activoRaw === '1' : null;

  if (tipo_zona === null && capacidad_maxima === null && activo === null) {
    alerta('Cambia al menos un campo: zona, capacidad o estado', 'error');
    return;
  }

  try {
    const r = await fetch(API + `/api/almacenes/${ALMACEN_ID}/ubicaciones/fila`, {
      method: 'PATCH',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ pasillo, fila: parseInt(fila), tipo_zona, capacidad_maxima, activo }),
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error editando la fila', 'error'); return; }
    const resEl = document.getElementById('layout-editar-fila-resultado');
    const bloqueadasCodigos = Object.keys(d.bloqueadas || {});
    let html = `<div style="color:#4ade80;">✓ ${d.actualizadas.length}/${d.total_posiciones} posición(es) actualizada(s)</div>`;
    if (bloqueadasCodigos.length) {
      html += bloqueadasCodigos.map(c => `<div style="color:#f87171;margin-top:4px;">✗ ${c}: ${d.bloqueadas[c]}</div>`).join('');
    }
    if (d.advertencias && d.advertencias.length) {
      html += d.advertencias.map(a => `<div style="color:#facc15;margin-top:4px;">⚠ ${a}</div>`).join('');
    }
    resEl.innerHTML = html;
    alerta(`${d.actualizadas.length}/${d.total_posiciones} posición(es) actualizada(s)`, bloqueadasCodigos.length ? 'advertencia' : 'ok');
    layoutCargarUbicaciones();
  } catch (e) {
    alerta(e.message || 'Error editando la fila', 'error');
  }
}

// ── Modal: Eliminar fila (solo posiciones sin historial) ─────────────────────

let _layoutFilaEnBorrado = null; // { pasillo, fila }

/**
 * Open the modal to delete an entire fila (only positions without history).
 * @param {string} pasillo - Aisle letter.
 * @param {string} fila - Row number within the aisle.
 */
function layoutAbrirModalEliminarFila(pasillo, fila) {
  const m = document.getElementById('modal-layout-eliminar-fila');
  if (!m) return;

  _layoutFilaEnBorrado = { pasillo, fila };
  const codigoFila = `${pasillo}${String(fila).padStart(2, '0')}`;
  const count = (_layoutUbicacionesCache || []).filter(u => u.pasillo === pasillo && u.estante === fila).length;
  document.getElementById('layout-eliminar-fila-titulo').textContent = `Eliminar fila ${codigoFila} · ${count} posición(es)`;

  document.getElementById('layout-eliminar-fila-resultado').innerHTML = '';
  m.style.display = 'flex';
}

/** Close the fila deletion modal. */
function layoutCerrarModalEliminarFila() {
  const m = document.getElementById('modal-layout-eliminar-fila');
  if (m) m.style.display = 'none';
}

/** Delete all eligible positions in the fila after confirmation. */
async function layoutGuardarEliminarFila(forzar) {
  if (!_layoutFilaEnBorrado) return;
  const { pasillo, fila } = _layoutFilaEnBorrado;
  const codigoFila = `${pasillo}${String(fila).padStart(2, '0')}`;
  const msg = forzar
    ? `⚠ FORZAR eliminación de la fila ${codigoFila} — esto borra también el historial real de picking/reposición de cada posición, PARA SIEMPRE. ¿Continuar?`
    : `¿Eliminar la fila ${codigoFila}? Esta acción no se puede deshacer. Solo se borrarán las posiciones sin stock ni historial.`;
  if (!confirm(msg)) return;

  try {
    const r = await fetch(API + `/api/almacenes/${ALMACEN_ID}/ubicaciones/fila`, {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ pasillo, fila: parseInt(fila), forzar: !!forzar }),
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error eliminando la fila', 'error'); return; }

    const resEl = document.getElementById('layout-eliminar-fila-resultado');
    const bloqueadasCodigos = Object.keys(d.bloqueadas || {});
    let html = `<div style="color:#4ade80;">✓ ${d.eliminadas.length}/${d.total_posiciones} posición(es) eliminada(s)</div>`;
    if (bloqueadasCodigos.length) {
      html += bloqueadasCodigos.map(c => `<div style="color:#f87171;margin-top:4px;">✗ ${c}: ${d.bloqueadas[c]}</div>`).join('');
    }
    resEl.innerHTML = html;
    alerta(`${d.eliminadas.length}/${d.total_posiciones} posición(es) eliminada(s)`, bloqueadasCodigos.length ? 'advertencia' : 'ok');
    layoutCargarUbicaciones();

    // Bloqueadas por stock/historial: ofrecer forzar como reintento explícito, solo sobre lo que falló.
    if (!forzar && bloqueadasCodigos.length) {
      if (confirm(`${bloqueadasCodigos.length} posición(es) bloqueada(s) por stock/historial.\n\n¿Forzar de todos modos sobre TODA la fila? Requiere rol admin y borra el historial para siempre.`)) {
        return layoutGuardarEliminarFila(true);
      }
    }
  } catch (e) {
    alerta(e.message || 'Error eliminando la fila', 'error');
  }
}

// ── AVERIAS numeradas ─────────────────────────────────────────────────────

/** Create the next numbered AVERIAS ubicacion for the current almacen. */
async function layoutCrearAverias() {
  try {
    const d = await post(`/api/almacenes/${ALMACEN_ID}/ubicaciones/averias`, {});
    alerta(`${d.codigo} creada`, 'ok');
    layoutCargarUbicaciones();
  } catch (e) {
    alerta(e.message || 'Error creando la ubicación de averías', 'error');
  }
}

// ── Modal: Asignar SKU (Mecanismo B) ─────────────────────────────────────────

/**
 * Open the SKU assignment modal for a ubicacion.
 * @param {number} ubId - Ubicacion ID.
 * @param {string} codigo - Ubicacion code for display.
 * @param {string} tipoZona - Zone type to conditionally show capacity input.
 */
function layoutAbrirModalAsignar(ubId, codigo, tipoZona) {
  _layoutUbAsignarId = ubId;
  _layoutProductoId = null;
  const m = document.getElementById('modal-layout-asignar');
  if (!m) return;
  document.getElementById('layout-asignar-ub-codigo').textContent = codigo;
  document.getElementById('layout-asignar-codigo').value = '';
  document.getElementById('layout-asignar-cantidad').value = '';
  document.getElementById('layout-asignar-confirmacion').style.display = 'none';
  const capWrap = document.getElementById('layout-asignar-capacidad-wrap');
  if (capWrap) {
    capWrap.style.display = _ZONAS_SLOT_UNICO.includes(tipoZona) ? 'block' : 'none';
    document.getElementById('layout-asignar-capacidad').value = '';
  }
  m.style.display = 'flex';
  setTimeout(() => document.getElementById('layout-asignar-codigo')?.focus(), 50);
}

/** Close the SKU assignment modal and reset state. */
function layoutCerrarModalAsignar() {
  const m = document.getElementById('modal-layout-asignar');
  if (m) m.style.display = 'none';
  _layoutUbAsignarId = null;
  _layoutProductoId = null;
}

/** Search for a product by code and display confirmation if found. */
async function layoutBuscarProducto() {
  const codigo = document.getElementById('layout-asignar-codigo').value.trim();
  if (!codigo) return;
  try {
    const d = await get(`/api/productos/?q=${encodeURIComponent(codigo)}&per_page=1`);
    const prod = (d.productos || [])[0];
    const conf = document.getElementById('layout-asignar-confirmacion');
    if (!prod) {
      conf.style.display = 'none';
      _layoutProductoId = null;
      alerta('Producto no encontrado — verifica el código o búscalo por nombre', 'error');
      return;
    }
    _layoutProductoId = prod.id;
    document.getElementById('layout-asignar-prod-codigo').textContent = prod.codigo;
    document.getElementById('layout-asignar-prod-nombre').textContent = prod.nombre;
    conf.style.display = 'block';
    document.getElementById('layout-asignar-cantidad')?.focus();
  } catch (e) {
    alerta('Error buscando el producto', 'error');
  }
}

/** Confirm and submit the SKU assignment with quantity and optional capacity. */
async function layoutConfirmarAsignar() {
  if (!_layoutUbAsignarId) return;
  if (!_layoutProductoId) { alerta('Busca y confirma el producto antes de continuar', 'error'); return; }
  const cantidad = parseInt(document.getElementById('layout-asignar-cantidad').value);
  if (isNaN(cantidad) || cantidad <= 0) { alerta('Ingresa una cantidad válida', 'error'); return; }

  const capRaw = document.getElementById('layout-asignar-capacidad')?.value;
  const capacidad_maxima = capRaw ? parseInt(capRaw) : null;

  try {
    const payload = { producto_id: _layoutProductoId, cantidad };
    if (capacidad_maxima !== null && !isNaN(capacidad_maxima)) payload.capacidad_maxima = capacidad_maxima;
    const d = await post(`/api/almacenes/ubicaciones/${_layoutUbAsignarId}/asignar`, payload);
    alerta(`${d.producto_codigo} asignado — total ${d.cantidad_total} UNDs`, 'ok');
    layoutCerrarModalAsignar();
    layoutCargarUbicaciones();
  } catch (e) {
    alerta(e.message || 'Error asignando el producto', 'error');
  }
}

// ── Modal: Editar ubicación individual ───────────────────────────────────────

let _layoutUbEditarId = null;

/**
 * Open the modal to edit an individual ubicacion's zone, capacity, or active state.
 * @param {number} ubId - Ubicacion ID.
 */
function layoutAbrirModalEditarUbicacion(ubId) {
  const ub = _layoutUbicacionesCache.find(u => u.id === ubId);
  if (!ub) return;
  _layoutUbEditarId = ubId;
  const m = document.getElementById('modal-layout-editar-ubicacion');
  if (!m) return;
  document.getElementById('layout-editar-ub-titulo').textContent = `Editar ${ub.codigo}`;
  document.getElementById('layout-editar-ub-zona').value = '';
  document.getElementById('layout-editar-ub-capacidad').value = '';
  document.getElementById('layout-editar-ub-activo').value = '';
  document.getElementById('layout-editar-ub-resultado').innerHTML = '';
  m.style.display = 'flex';
}

/** Close the individual ubicacion edit modal. */
function layoutCerrarModalEditarUbicacion() {
  const m = document.getElementById('modal-layout-editar-ubicacion');
  if (m) m.style.display = 'none';
  _layoutUbEditarId = null;
}

/** Save changes to an individual ubicacion (zone, capacity, active state). */
async function layoutGuardarEditarUbicacion() {
  if (!_layoutUbEditarId) return;
  const tipo_zona = document.getElementById('layout-editar-ub-zona').value || null;
  const capRaw = document.getElementById('layout-editar-ub-capacidad').value;
  const capacidad_maxima = capRaw !== '' ? parseInt(capRaw) : null;
  const activoRaw = document.getElementById('layout-editar-ub-activo').value;
  const activo = activoRaw !== '' ? activoRaw === '1' : null;

  if (tipo_zona === null && capacidad_maxima === null && activo === null) {
    alerta('Cambia al menos un campo: zona, capacidad o estado', 'error');
    return;
  }

  try {
    const r = await fetch(API + `/api/almacenes/ubicaciones/${_layoutUbEditarId}`, {
      method: 'PATCH',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ tipo_zona, capacidad_maxima, activo }),
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error editando la ubicación', 'error'); return; }

    const resEl = document.getElementById('layout-editar-ub-resultado');
    resEl.innerHTML = (d.advertencias && d.advertencias.length)
      ? d.advertencias.map(a => `<div style="color:#facc15;margin-top:4px;">⚠ ${a}</div>`).join('')
      : '';
    alerta('Ubicación actualizada', 'ok');
    layoutCargarUbicaciones();
    setTimeout(layoutCerrarModalEditarUbicacion, d.advertencias?.length ? 1400 : 200);
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ── Eliminar ubicación individual (sin stock ni historial) ──────────────────

/**
 * Delete a single ubicacion if it has no stock or history.
 * @param {number} ubId - Ubicacion ID.
 * @param {string} codigo - Ubicacion code for the confirmation prompt.
 */
async function layoutEliminarUbicacion(ubId, codigo, forzar) {
  const msg = forzar
    ? `⚠ FORZAR eliminación de ${codigo} — esto borra también el historial real de picking/reposición asociado, PARA SIEMPRE. Esto no es una prueba reversible. ¿Continuar?`
    : `¿Eliminar la ubicación ${codigo}? Esta acción no se puede deshacer. Solo se puede eliminar si no tiene stock ni historial.`;
  if (!confirm(msg)) return;
  try {
    const url = API + `/api/almacenes/ubicaciones/${ubId}` + (forzar ? '?forzar=true' : '');
    const r = await fetch(url, {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (!r.ok) {
      // Bloqueada por stock/historial: ofrecer forzar como reintento explícito, nunca por defecto.
      if (!forzar && r.status === 400) {
        alerta(d.error || 'Error eliminando la ubicación', 'error');
        if (confirm(`Bloqueada: ${d.error}\n\n¿Forzar de todos modos? Requiere rol admin y borra el historial para siempre.`)) {
          return layoutEliminarUbicacion(ubId, codigo, true);
        }
        return;
      }
      alerta(d.error || 'Error eliminando la ubicación', 'error');
      return;
    }
    alerta(`${d.codigo || codigo} eliminada${forzar ? ' (forzado)' : ''}`, 'ok');
    layoutCargarUbicaciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ── Modal: Reclasificar ubicación ────────────────────────────────────────────

/**
 * Open the reclassify modal to change a ubicacion's zone and optionally release its slot.
 * @param {number} ubId - Ubicacion ID.
 */
function layoutAbrirModalReclasificar(ubId) {
  const ub = _layoutUbicacionesCache.find(u => u.id === ubId);
  if (!ub) return;
  _layoutUbAsignarId = ubId; // reutilizamos la misma variable de "ubicación activa"
  const m = document.getElementById('modal-layout-reclasificar');
  if (!m) return;
  document.getElementById('layout-reclasificar-codigo').textContent = ub.codigo;
  document.getElementById('layout-reclasificar-zona').value = ub.tipo_zona;
  document.getElementById('layout-reclasificar-capacidad').value = ub.capacidad_maxima ?? '';
  document.getElementById('layout-reclasificar-liberar').checked = false;
  document.getElementById('layout-reclasificar-advertencias').textContent = '';
  m.style.display = 'flex';
}

/** Close the reclassify modal. */
function layoutCerrarModalReclasificar() {
  const m = document.getElementById('modal-layout-reclasificar');
  if (m) m.style.display = 'none';
  _layoutUbAsignarId = null;
}

/** Save the reclassification (zone change + optional slot release) for the ubicacion. */
async function layoutGuardarReclasificar() {
  if (!_layoutUbAsignarId) return;
  const tipo_zona = document.getElementById('layout-reclasificar-zona').value;
  const capRaw = document.getElementById('layout-reclasificar-capacidad').value;
  const liberar_slot = document.getElementById('layout-reclasificar-liberar').checked;

  const payload = { tipo_zona, liberar_slot };
  if (capRaw !== '') payload.capacidad_maxima = parseInt(capRaw);

  try {
    const r = await fetch(API + `/api/almacenes/ubicaciones/${_layoutUbAsignarId}`, {
      method: 'PATCH',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error reclasificando', 'error'); return; }

    if (d.advertencias && d.advertencias.length) {
      document.getElementById('layout-reclasificar-advertencias').textContent = '⚠ ' + d.advertencias.join(' · ');
    }
    alerta('Ubicación actualizada', 'ok');
    layoutCargarUbicaciones();
    setTimeout(layoutCerrarModalReclasificar, d.advertencias?.length ? 1400 : 200);
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ── Importador Excel (Mecanismo A, opción masiva) ────────────────────────────

/**
 * Upload an Excel file to bulk-import ubicaciones into the current almacen.
 * @param {HTMLButtonElement} btn - Button element to disable during import.
 */
async function layoutImportarExcel(btn) {
  const input = document.getElementById('layout-import-file');
  const file = input?.files?.[0];
  if (!file) { alerta('Selecciona un archivo .xlsx', 'error'); return; }

  const resultado = document.getElementById('layout-import-resultado');
  if (btn) { btn.disabled = true; btn.textContent = 'Importando...'; }

  const form = new FormData();
  form.append('archivo', file);

  try {
    const r = await fetch(API + `/api/almacenes/${ALMACEN_ID}/ubicaciones/importar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
      body: form,
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error procesando el archivo', 'error'); return; }

    const erroresHtml = (d.errores || []).map(e =>
      `<div style="font-size:12px;color:#f87171;padding:4px 0;">Fila ${e.fila}: ${e.error}</div>`
    ).join('');

    resultado.innerHTML = `
      <div class="tabla-card">
        <div style="font-size:13px;font-weight:700;color:#4ade80;margin-bottom:8px;">✓ ${d.ok} fila(s) importada(s) correctamente</div>
        ${d.errores?.length ? `<div style="font-size:12px;font-weight:700;color:#f87171;margin-bottom:4px;">${d.errores.length} fila(s) con error:</div>${erroresHtml}` : ''}
      </div>`;
    alerta(`Importación completa — ${d.ok} ok, ${d.errores?.length || 0} error(es)`, d.errores?.length ? 'advertencia' : 'ok');
    layoutCargarUbicaciones();
  } catch (e) {
    alerta('Error de conexión subiendo el archivo', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Importar'; }
    input.value = '';
  }
}


// ── Entrepaño: render, asignar SKU batch, ver detalle, imprimir ─────────────
// Post-modularización: gestión de entrepaños como unidad. Movidas desde app.js.
// _layoutCuerpoZona se declara arriba, junto al wizard de Crear Cuerpo.

let _layoutAsignarEntHuecos = [];
let _layoutAsignarEntProductos = {};
let _layoutAsignarEntZona = 'PICKING';
let _layoutAsignarEntEditando = new Set(); // ids de hueco con SKU ya asignado que el usuario abrió para cambiar

function _layoutRenderEntrepanoSeccion(huecos, esPrimero) {
  const zona = huecos[0].tipo_zona;
  const color = _ZONA_COLOR[zona] || '#888';
  const nivel = huecos[0].nivel;
  const idsCsv = huecos.map(u => u.id).join(',');
  const sinAsignar = huecos.filter(u => !u.producto_asignado_codigo).length;
  const subtitulo = sinAsignar
    ? `${huecos.length} hueco(s) · ${sinAsignar} sin SKU asignado`
    : `${huecos.length} hueco(s) · todos asignados`;
  return `
    <div style="${esPrimero ? '' : 'border-top:1px solid var(--brd);margin-top:14px;padding-top:14px;'}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:15px;font-weight:800;color:var(--tx);">Entrepaño ${nivel}</div>
          <div style="font-size:11px;color:${sinAsignar ? '#555' : '#60a5fa'};margin-top:3px;font-weight:600;">${subtitulo}</div>
        </div>
        <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${zona}</span>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button onclick="layoutAbrirModalAsignarEntrepano('${idsCsv}', '${zona}')"
          style="flex:1;min-width:120px;padding:10px;background:var(--pm);border:none;border-radius:6px;color:#fff;font-size:12px;font-weight:700;cursor:pointer;">Asignar SKU</button>
        <button onclick="layoutAbrirModalVerEntrepano('${idsCsv}')"
          style="flex:1;min-width:90px;padding:10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:12px;cursor:pointer;">Ver</button>
      </div>
    </div>`;
}

function layoutImprimirEtiquetasCuerpo(idsCsv) {
  const ids = idsCsv.split(',').map(Number);
  const huecos = ids.map(id => _layoutUbicacionesCache.find(u => u.id === id)).filter(Boolean).sort((a, b) => a.codigo.localeCompare(b.codigo));
  if (!huecos.length) return;
  const area = document.getElementById('print-area');
  if (!area) return;
  area.innerHTML = huecos.map(u => {
    const titulo = u.producto_asignado_codigo
      ? (u.producto_asignado_nombre || u.producto_asignado_codigo)
      : 'Sin SKU asignado';
    return `<div class="etiqueta-ubicacion"><div class="eu-titulo">${titulo}</div><svg id="ub-bc-${u.id}"></svg><div class="eu-codigo">${u.codigo}</div><div class="eu-zona">${u.tipo_zona}</div></div>`;
  }).join('');
  huecos.forEach(u => { try { JsBarcode(`#ub-bc-${u.id}`, u.codigo, { format: 'CODE128', displayValue: false, height: 55, margin: 0 }); } catch (e) {} });
  setTimeout(() => { window.print(); setTimeout(() => { area.innerHTML = ''; }, 1000); }, 300);
}

function layoutAbrirModalAsignarEntrepano(idsCsv, zona) {
  const ids = idsCsv.split(',').map(Number);
  const huecos = ids.map(id => _layoutUbicacionesCache.find(u => u.id === id)).filter(Boolean).sort((a, b) => a.codigo.localeCompare(b.codigo));
  if (!huecos.length) return;
  _layoutAsignarEntHuecos = huecos;
  _layoutAsignarEntProductos = {};
  _layoutAsignarEntZona = zona;
  _layoutAsignarEntEditando = new Set();
  const m = document.getElementById('modal-layout-asignar-entrepano');
  if (!m) return;
  const codigoCuerpo = huecos[0].codigo.split('-').slice(0, 3).join('-');
  document.getElementById('layout-asignar-ent-titulo').textContent = `${codigoCuerpo} · ${huecos.length} hueco(s) — deja en blanco los que no vayas a asignar hoy`;
  _layoutRenderFilasAsignarEntrepano();
  const resEl = document.getElementById('layout-asignar-ent-resultado');
  if (resEl) resEl.innerHTML = '';
  m.style.display = 'flex';
  // Autofocus solo si el primer hueco tiene input visible (sin SKU, o recién puesto en edición)
  const primero = huecos[0];
  if (!primero.producto_asignado_codigo || _layoutAsignarEntEditando.has(primero.id)) {
    setTimeout(() => document.getElementById(`layout-asignar-ent-codigo-${primero.id}`)?.focus(), 50);
  }
}

/** Modo vista (SKU ya asignado) — referencia grande y legible + botón Cambiar SKU. */
function _layoutAsignarEntFilaVista(u, huecoLabel) {
  const referencia = `${u.producto_asignado_codigo}${u.producto_asignado_nombre ? ' — ' + u.producto_asignado_nombre : ''}`;
  return `
    <div class="tabla-card" style="margin-bottom:8px;padding:10px;">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;">
        <div style="min-width:0;">
          <div style="font-size:11px;font-weight:700;font-family:monospace;color:var(--tx3);">${huecoLabel}</div>
          <div style="font-size:16px;font-weight:800;color:var(--tx);margin-top:2px;line-height:1.3;">${referencia}</div>
        </div>
        <button onclick="layoutEditarSkuEntrepano(${u.id})"
          style="flex-shrink:0;padding:9px 16px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap;">
          Cambiar SKU
        </button>
      </div>
    </div>`;
}

/** Modo edición — inputs de código/cantidad/capacidad (hueco nuevo o SKU existente a cambiar). */
function _layoutAsignarEntFilaEdicion(u, huecoLabel, zona) {
  const cambiando = u.producto_asignado_codigo
    ? `Cambiando: ${u.producto_asignado_codigo}${u.producto_asignado_nombre ? ' — ' + u.producto_asignado_nombre : ''}`
    : '';
  return `
    <div class="tabla-card" style="margin-bottom:8px;padding:10px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
        <div style="font-size:12px;font-weight:700;font-family:monospace;color:var(--tx);">${huecoLabel}</div>
        <div id="layout-asignar-ent-estado-${u.id}" style="font-size:11px;color:#60a5fa;">${cambiando}</div>
      </div>
      <div style="display:flex;gap:6px;">
        <input id="layout-asignar-ent-codigo-${u.id}" type="text" placeholder="Código o código de barras" autocomplete="off"
          onkeydown="if(event.key==='Enter'){event.preventDefault();layoutBuscarProductoEntrepano(${u.id});}"
          style="flex:1;min-width:0;padding:9px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:13px;box-sizing:border-box;">
        <input id="layout-asignar-ent-cantidad-${u.id}" type="number" min="1" placeholder="Cant."
          onkeydown="if(event.key==='Enter'){event.preventDefault();layoutAsignarEntSiguiente(${u.id});}"
          style="width:64px;padding:9px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:13px;text-align:center;box-sizing:border-box;">
      </div>
      ${_ZONAS_SLOT_UNICO.includes(zona) ? `<input id="layout-asignar-ent-capacidad-${u.id}" type="number" min="0" placeholder="Capacidad máxima (opcional)" value="${u.capacidad_maxima ?? ''}" style="width:100%;margin-top:6px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:12px;box-sizing:border-box;">` : ''}
    </div>`;
}

function _layoutRenderFilasAsignarEntrepano() {
  const cont = document.getElementById('layout-asignar-ent-filas');
  if (!cont) return;
  cont.innerHTML = _layoutAsignarEntHuecos.map(u => {
    const huecoLabel = u.codigo.split('-').pop();
    const enVista = u.producto_asignado_codigo && !_layoutAsignarEntEditando.has(u.id);
    return enVista
      ? _layoutAsignarEntFilaVista(u, huecoLabel)
      : _layoutAsignarEntFilaEdicion(u, huecoLabel, _layoutAsignarEntZona);
  }).join('');
}

/** El usuario quiere cambiar el SKU de un hueco ya asignado — pasa esa fila a modo edición. */
function layoutEditarSkuEntrepano(huecoId) {
  _layoutAsignarEntEditando.add(huecoId);
  _layoutRenderFilasAsignarEntrepano();
  setTimeout(() => document.getElementById(`layout-asignar-ent-codigo-${huecoId}`)?.focus(), 50);
}

function layoutCerrarModalAsignarEntrepano() {
  const m = document.getElementById('modal-layout-asignar-entrepano');
  if (m) m.style.display = 'none';
  _layoutAsignarEntHuecos = [];
  _layoutAsignarEntProductos = {};
  _layoutAsignarEntEditando = new Set();
}

async function layoutBuscarProductoEntrepano(huecoId) {
  const inp = document.getElementById(`layout-asignar-ent-codigo-${huecoId}`);
  const estado = document.getElementById(`layout-asignar-ent-estado-${huecoId}`);
  const codigo = inp?.value.trim();
  if (!codigo) return;
  try {
    const d = await get(`/api/productos/?q=${encodeURIComponent(codigo)}&per_page=1`);
    const prod = (d.productos || [])[0];
    if (!prod) { delete _layoutAsignarEntProductos[huecoId]; if (estado) { estado.textContent = 'No encontrado'; estado.style.color = '#f87171'; } return; }
    _layoutAsignarEntProductos[huecoId] = prod.id;
    if (estado) { estado.textContent = `✓ ${prod.codigo} — ${prod.nombre}`; estado.style.color = '#4ade80'; }
    document.getElementById(`layout-asignar-ent-cantidad-${huecoId}`)?.focus();
  } catch (e) { if (estado) { estado.textContent = 'Error buscando'; estado.style.color = '#f87171'; } }
}

function layoutAsignarEntSiguiente(huecoId) {
  const idx = _layoutAsignarEntHuecos.findIndex(u => u.id === huecoId);
  const siguiente = _layoutAsignarEntHuecos[idx + 1];
  if (siguiente) document.getElementById(`layout-asignar-ent-codigo-${siguiente.id}`)?.focus();
}

async function layoutConfirmarAsignarEntrepano() {
  let ok = 0; const errores = [];
  for (const u of _layoutAsignarEntHuecos) {
    const productoId = _layoutAsignarEntProductos[u.id];
    const cantidad = parseInt(document.getElementById(`layout-asignar-ent-cantidad-${u.id}`)?.value);
    if (!productoId || isNaN(cantidad) || cantidad <= 0) continue;
    const capRaw = document.getElementById(`layout-asignar-ent-capacidad-${u.id}`)?.value;
    const capacidad_maxima = capRaw ? parseInt(capRaw) : null;
    try {
      const payload = { producto_id: productoId, cantidad };
      if (capacidad_maxima !== null && !isNaN(capacidad_maxima)) payload.capacidad_maxima = capacidad_maxima;
      await post(`/api/almacenes/ubicaciones/${u.id}/asignar`, payload);
      ok++;
    } catch (e) { errores.push(`${u.codigo.split('-').pop()}: ${e.message || 'error'}`); }
  }
  if (ok === 0 && errores.length === 0) { alerta('No hay ninguna fila completa (código + cantidad) para asignar', 'error'); return; }
  alerta(`${ok} hueco(s) asignado(s)${errores.length ? ` — ${errores.length} error(es)` : ''}`, errores.length ? 'advertencia' : 'ok');
  layoutCargarUbicaciones();
  if (errores.length === 0) { layoutCerrarModalAsignarEntrepano(); }
  else { const resEl = document.getElementById('layout-asignar-ent-resultado'); if (resEl) resEl.innerHTML = errores.map(e => `<div style="color:#f87171;font-size:11px;margin-top:2px;">✗ ${e}</div>`).join(''); }
}

function layoutAbrirModalVerEntrepano(idsCsv) {
  const ids = idsCsv.split(',').map(Number);
  const huecos = ids.map(id => _layoutUbicacionesCache.find(u => u.id === id)).filter(Boolean).sort((a, b) => a.codigo.localeCompare(b.codigo));
  if (!huecos.length) return;
  const m = document.getElementById('modal-layout-ver-entrepano');
  if (!m) return;
  const codigoCuerpo = huecos[0].codigo.split('-').slice(0, 3).join('-');
  document.getElementById('layout-ver-ent-titulo').textContent = `${codigoCuerpo} · Entrepaño ${huecos[0].nivel} · ${huecos.length} hueco(s)`;
  const cont = document.getElementById('layout-ver-ent-filas');
  cont.innerHTML = huecos.map(u => {
    const huecoLabel = u.codigo.split('-').pop();
    const skuLabel = u.producto_asignado_codigo ? `📦 ${u.producto_asignado_nombre || u.producto_asignado_codigo} · ${u.stock_actual ?? 0} UND` : 'Sin SKU asignado';
    return `<div style="display:flex;align-items:center;gap:8px;padding:9px 0;border-top:1px solid var(--brd);"><div style="font-size:12px;font-family:monospace;font-weight:700;color:var(--tx);min-width:34px;">${huecoLabel}</div><div style="flex:1;font-size:12px;color:${u.producto_asignado_codigo ? '#60a5fa' : '#888'};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${skuLabel}${!u.activo ? ' · INACTIVA' : ''}</div><div style="display:flex;gap:4px;flex-shrink:0;"><button title="Editar" onclick="layoutAbrirModalEditarUbicacion(${u.id})" style="padding:5px 8px;background:var(--bg);border:1px solid var(--brd);border-radius:5px;color:var(--tx2);font-size:11px;cursor:pointer;">✏</button><button title="Eliminar" onclick="layoutEliminarUbicacion(${u.id}, '${u.codigo}')" style="padding:5px 8px;background:var(--bg);border:1px solid #7f1d1d;border-radius:5px;color:#f87171;font-size:11px;cursor:pointer;">🗑</button><button title="Reclasificar" onclick="layoutAbrirModalReclasificar(${u.id})" style="padding:5px 8px;background:var(--bg);border:1px solid var(--brd);border-radius:5px;color:var(--tx2);font-size:11px;cursor:pointer;">⇄</button></div></div>`;
  }).join('');
  m.style.display = 'flex';
}

function layoutCerrarModalVerEntrepano() {
  const m = document.getElementById('modal-layout-ver-entrepano');
  if (m) m.style.display = 'none';
}
