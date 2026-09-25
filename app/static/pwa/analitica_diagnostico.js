/**
 * 📈 Analítica → 🩺 Diagnóstico (solo administración, 2026-09-24).
 *
 * Todo lo que un administrador necesita para JUZGAR los números, junto y fuera
 * del camino de la gerencia: la salud completa del dato, la bitácora de
 * acciones, los registros que no se pudieron unir a su pedido y cómo se mide
 * cada cifra. Antes eran dos sub-pestañas sueltas (Salud y Bitácora) y dos
 * bloques pegados al pie del Recorrido; la portada solo muestra UNA línea de
 * confianza con «Ver por qué ›», que abre esto.
 *
 * No calcula nada: reutiliza las vistas que ya existen —`anSaludCargar`
 * (analitica_salud.js), `anBitacoraCargar` (analitica_bitacora.js),
 * `anRecSinEnlazarHtml` (analitica_recorrido.js)— y el catálogo de
 * `/api/analitica/metricas`. Los botones pasan claves fijas, nunca un dato.
 */

const AN_DIAG = { seccion: 'salud', f: null };

const AN_DIAG_SECCIONES = {
  salud:        '🩺 Confianza del dato',
  bitacora:     '📜 Bitácora de acciones',
  sin_clave:    '🔗 Registros sin clave',
  como_se_mide: '📐 Cómo se mide cada cifra',
};

/** Entrada desde el shell. */
function anDiagnosticoCargar(el, f) {
  AN_DIAG.f = f;
  if (!el.dataset || !el.dataset.diag) {
    const botones = Object.keys(AN_DIAG_SECCIONES).map(k =>
      `<button id="an-diag-btn-${esc(k)}" onclick="anDiagSeccion('${esc(k)}')" style="padding:6px 12px;border-radius:6px;border:1px solid var(--brd);background:transparent;color:var(--tx2);font-size:var(--fs-sm);cursor:pointer;">${esc(AN_DIAG_SECCIONES[k])}</button>`).join('');
    const paneles = Object.keys(AN_DIAG_SECCIONES).map(k =>
      `<div id="an-diag-${esc(k)}" style="display:none;"></div>`).join('');
    el.innerHTML = `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">
        Para administración: con qué confianza leer las cifras y de dónde salen. La gerencia ve el resumen en 🎯 ¿Cómo vamos?.</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;">${botones}</div>${paneles}`;
    if (el.dataset) el.dataset.diag = '1';
  }
  return anDiagSeccion(AN_DIAG.seccion);
}

/** Muestra una sección y la carga con los filtros del shell. */
function anDiagSeccion(clave) {
  if (!AN_DIAG_SECCIONES[clave]) clave = 'salud';
  AN_DIAG.seccion = clave;
  Object.keys(AN_DIAG_SECCIONES).forEach(k => {
    const p = document.getElementById('an-diag-' + k);
    if (p) p.style.display = k === clave ? 'block' : 'none';
    const b = document.getElementById('an-diag-btn-' + k);
    if (b && b.style) {
      b.style.background = k === clave ? 'var(--acento-bg)' : 'transparent';
      b.style.color = k === clave ? 'var(--acento-tx)' : 'var(--tx2)';
      b.style.borderColor = k === clave ? 'var(--acento-brd)' : 'var(--brd)';
    }
  });
  const el = document.getElementById('an-diag-' + clave);
  if (!el) return Promise.resolve();
  const f = AN_DIAG.f || anFiltros();
  if (clave === 'salud') return Promise.resolve(anSaludCargar(el, f));
  if (clave === 'bitacora') return Promise.resolve(anBitacoraCargar(el, f));
  if (clave === 'sin_clave') return anDiagSinClave(el, f);
  return anDiagComoSeMide(el, f);
}

/** Los registros que no se unen a su pedido (antes, al pie del Recorrido). */
function anDiagSinClave(el, f) {
  const params = anQuery(f);
  return anCargarPanel({
    el, clave: 'diag-sin-clave', params,
    pedir: () => get('/api/analitica/recorrido?' + params),
    // Tocar un registro abre su línea de tiempo en 🧭 Recorrido
    // (`anRecPedidoSuelto` → `anRecorridoAbrir`).
    html: (d) => { _AN_REC.sueltos = (d.sin_enlazar && d.sin_enlazar.muestra) || []; return anRecSinEnlazarHtml(d.sin_enlazar || {}); },
  });
}

/** Qué mide cada cifra, de dónde sale, su meta y su dueño: el catálogo. */
function anDiagComoSeMide(el, f) {
  const params = anQuery(f);
  return anCargarPanel({
    el, clave: 'diag-como-se-mide', params,
    pedir: async () => {
      const [cat, rec] = await Promise.all([
        get('/api/analitica/metricas'),
        get('/api/analitica/recorrido?' + params).catch(() => null),
      ]);
      return { cat, rec };
    },
    html: (d) => anDiagComoSeMideHtml(d),
  });
}

const AN_DIAG_AGREGACION = {
  SUMA: 'se suman los días del período',
  TASA: 'numerador y denominador se suman por separado (nunca un promedio de tasas)',
  NIVEL: 'el saldo del último día con dato',
  COHORTE: 'se mide en vivo sobre los pedidos o casos del período; no se guarda por día',
};

function anDiagComoSeMideHtml(d) {
  const metricas = ((d.cat && d.cat.metricas) || []);
  const filas = metricas.map(m => {
    const meta = m.meta === null || m.meta === undefined ? 'sin meta'
      : `${m.direccion === 'SUBE_ES_BUENO' ? '≥' : '≤'} ${anPortFormato(m.unidad, m.meta)}${m.meta_por_dia ? ' por día' : ''}${m.meta_provisional ? ' (provisional)' : ''}`;
    return `<div class="tabla-fila" style="flex-direction:column;align-items:stretch;gap:2px;">
      <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;">
        <b style="color:var(--tx);">${esc(m.nombre)}</b>
        <span style="font-size:var(--fs-xs);color:var(--tx2);">Meta ${esc(meta)} · dueño: ${esc(m.dueno)}</span></div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);">${esc(m.mide)}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);">Mejor si ${m.direccion === 'SUBE_ES_BUENO' ? 'sube' : 'baja'} · ${esc(AN_DIAG_AGREGACION[m.agregacion] || m.agregacion)} · fuente: ${esc(m.fuente)}</div>
    </div>`;
  }).join('');
  const defs = (d.rec && d.rec.definiciones) || {};
  const defsHtml = Object.keys(defs).map(k =>
    `<div style="margin:4px 0;"><b>${esc(k.replace(/_/g, ' '))}</b>: ${esc(defs[k])}</div>`).join('');
  return `<div class="tabla-card"><div class="tabla-titulo">Indicadores (catálogo)</div>${filas || '<div class="tabla-nombre">Sin catálogo.</div>'}</div>
    ${defsHtml ? `<div class="tabla-card" style="font-size:var(--fs-sm);color:var(--tx2);"><div class="tabla-titulo">Recorrido del pedido</div>${defsHtml}</div>` : ''}`;
}
