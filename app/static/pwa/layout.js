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
let _layoutUltimoCuerpo = null;   // { pasillo, fila, cuerpo, entrepanos, huecosPorNivel } — para "repetir"
let _layoutUbAsignarId = null;
let _layoutProductoId = null;

const _ZONA_COLOR = { PICKING: '#60a5fa', RESERVA: '#22c55e', AVERIAS: '#ef4444', GENERAL: '#555' };
const _ZONAS_TABS = ['PICKING', 'RESERVA', 'AVERIAS', 'GENERAL'];

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

async function cargarLayout() {
  layoutSubtab(_layoutSubActual);
}

function _layoutRenderUbicacionCard(u) {
  const color = _ZONA_COLOR[u.tipo_zona] || '#888';
  const skuLabel = u.producto_asignado_codigo
    ? `📦 ${u.producto_asignado_codigo}`
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
      const codigoCuerpo = `${g.pasillo}${g.fila}-${String(g.cuerpo).padStart(2, '0')}`;
      const nivelesEnZona = [...g.niveles.keys()].sort((a, b) => a - b);
      html += `
        <div style="font-size:12px;font-weight:700;color:var(--tx2);background:var(--bg-s2);border-radius:8px;padding:8px 12px;margin:16px 0 8px;">
          Cuerpo ${codigoCuerpo} · ${nivelesEnZona.length} entrepaño(s) en ${_layoutZonaActual} · ${g.items.length} hueco(s)
        </div>`;
      nivelesEnZona.forEach(nivel => {
        const huecos = g.niveles.get(nivel).sort((a, b) => a.hueco - b.hueco);
        html += `
          <div style="font-size:11px;font-weight:600;color:var(--tx3);padding:4px 4px;margin:8px 0 4px;">
            Entrepaño ${nivel} · ${huecos.length} hueco(s)
          </div>`;
        huecos.forEach(u => { html += _layoutRenderUbicacionCard(u); });
      });
    } else {
      g.items.forEach(u => { html += _layoutRenderUbicacionCard(u); });
    }
  });

  el.innerHTML = html;
}

// ── Modal: Crear ubicación — Cuerpo completo (Mecanismo A), wizard de 2 pasos ─
// Dirección de 5 ejes: Pasillo -> Fila (1/2) -> Cuerpo -> Entrepaños -> Huecos.
// La zona se sugiere sola por entrepaño (piso->PICKING, alto->RESERVA) — no se pide.
// Paso 1 define el cuerpo (pasillo/fila/número/cantidad de entrepaños); paso 2
// pide, entrepaño por entrepaño, cuántos huecos tiene — variable, no un único
// número para todo el cuerpo, porque la profundidad física no es uniforme.
// El SKU de cada hueco se asigna después, aparte (Asignar SKU, Mecanismo B).

let _layoutCuerpoHuecosPrevios = null; // huecosPorNivel del último cuerpo creado, para "repetir"

async function layoutAbrirModalCuerpo() {
  const m = document.getElementById('modal-layout-cuerpo');
  if (!m) return;

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

  const btnRepetir = document.getElementById('layout-cuerpo-btn-repetir');
  if (btnRepetir) btnRepetir.style.display = _layoutUltimoCuerpo ? 'block' : 'none';

  document.getElementById('layout-cuerpo-paso1').style.display = 'block';
  document.getElementById('layout-cuerpo-paso2').style.display = 'none';
  m.style.display = 'flex';
}

function layoutCerrarModalCuerpo() {
  const m = document.getElementById('modal-layout-cuerpo');
  if (m) m.style.display = 'none';
}

function layoutRepetirCuerpoAnterior() {
  if (!_layoutUltimoCuerpo) return;
  document.getElementById('layout-cuerpo-pasillo').value = _layoutUltimoCuerpo.pasillo;
  document.getElementById('layout-cuerpo-fila').value = _layoutUltimoCuerpo.fila;
  document.getElementById('layout-cuerpo-numero').value = _layoutUltimoCuerpo.cuerpo + 1;
  document.getElementById('layout-cuerpo-entrepanos').value = _layoutUltimoCuerpo.entrepanos;
  _layoutCuerpoHuecosPrevios = _layoutUltimoCuerpo.huecosPorNivel;
}

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
    const zona = nivel <= 2 ? 'PICKING · piso' : 'RESERVA · alto';
    const valor = (_layoutCuerpoHuecosPrevios && _layoutCuerpoHuecosPrevios[nivel - 1]) || 1;
    html += `
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
        <div style="flex:1;font-size:12px;color:var(--tx2);">Entrepaño ${nivel} <span style="color:var(--tx3);">(${zona})</span></div>
        <input id="layout-cuerpo-hueco-nivel-${nivel}" type="number" min="1" value="${valor}"
          style="width:72px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:14px;box-sizing:border-box;text-align:center;">
      </div>`;
  }
  cont.innerHTML = html;

  document.getElementById('layout-cuerpo-paso1').style.display = 'none';
  document.getElementById('layout-cuerpo-paso2').style.display = 'block';
}

function layoutCuerpoVolverAPaso1() {
  document.getElementById('layout-cuerpo-paso2').style.display = 'none';
  document.getElementById('layout-cuerpo-paso1').style.display = 'block';
}

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
    const payload = { pasillo, fila, cuerpo, cantidad_entrepanos, huecos_por_nivel };
    await post(`/api/almacenes/${ALMACEN_ID}/ubicaciones/cuerpo`, payload);
    _layoutUltimoCuerpo = { pasillo, fila, cuerpo, entrepanos: cantidad_entrepanos, huecosPorNivel: huecos_por_nivel };
    const totalHuecos = huecos_por_nivel.reduce((a, b) => a + b, 0);
    alerta(`Cuerpo ${pasillo}${fila}-${String(cuerpo).padStart(2,'0')} creado — ${cantidad_entrepanos} entrepaño(s), ${totalHuecos} hueco(s)`, 'ok');
    layoutCerrarModalCuerpo();
    layoutCargarUbicaciones();
  } catch (e) {
    alerta(e.message || 'Error creando el cuerpo', 'error');
  }
}

// ── Modal: Editar fila (bloque ya creado) ────────────────────────────────────

let _layoutFilaEnEdicion = null; // { pasillo, fila }

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

function layoutCerrarModalEditarFila() {
  const m = document.getElementById('modal-layout-editar-fila');
  if (m) m.style.display = 'none';
}

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

function layoutCerrarModalEliminarFila() {
  const m = document.getElementById('modal-layout-eliminar-fila');
  if (m) m.style.display = 'none';
}

async function layoutGuardarEliminarFila() {
  if (!_layoutFilaEnBorrado) return;
  const { pasillo, fila } = _layoutFilaEnBorrado;
  const codigoFila = `${pasillo}${String(fila).padStart(2, '0')}`;
  if (!confirm(`¿Eliminar la fila ${codigoFila}? Esta acción no se puede deshacer. Solo se borrarán las posiciones sin stock ni historial.`)) return;

  try {
    const r = await fetch(API + `/api/almacenes/${ALMACEN_ID}/ubicaciones/fila`, {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ pasillo, fila: parseInt(fila) }),
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
  } catch (e) {
    alerta(e.message || 'Error eliminando la fila', 'error');
  }
}

// ── AVERIAS numeradas ─────────────────────────────────────────────────────

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
    capWrap.style.display = tipoZona === 'PICKING' ? 'block' : 'none';
    document.getElementById('layout-asignar-capacidad').value = '';
  }
  m.style.display = 'flex';
  setTimeout(() => document.getElementById('layout-asignar-codigo')?.focus(), 50);
}

function layoutCerrarModalAsignar() {
  const m = document.getElementById('modal-layout-asignar');
  if (m) m.style.display = 'none';
  _layoutUbAsignarId = null;
  _layoutProductoId = null;
}

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

function layoutCerrarModalEditarUbicacion() {
  const m = document.getElementById('modal-layout-editar-ubicacion');
  if (m) m.style.display = 'none';
  _layoutUbEditarId = null;
}

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

async function layoutEliminarUbicacion(ubId, codigo) {
  if (!confirm(`¿Eliminar la ubicación ${codigo}? Esta acción no se puede deshacer. Solo se puede eliminar si no tiene stock ni historial.`)) return;
  try {
    const r = await fetch(API + `/api/almacenes/ubicaciones/${ubId}`, {
      method: 'DELETE',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'Error eliminando la ubicación', 'error'); return; }
    alerta(`${d.codigo || codigo} eliminada`, 'ok');
    layoutCargarUbicaciones();
  } catch (e) {
    alerta('Error de conexión', 'error');
  }
}

// ── Modal: Reclasificar ubicación ────────────────────────────────────────────

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

function layoutCerrarModalReclasificar() {
  const m = document.getElementById('modal-layout-reclasificar');
  if (m) m.style.display = 'none';
  _layoutUbAsignarId = null;
}

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


// ─────────────────────────────────────────────────────────────────────────────
