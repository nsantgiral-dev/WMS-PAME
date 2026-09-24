// ═══════════════════════════════════════════════════════════════════════════
// Analítica → 💸 Fugas (Fase 1, 2026-09-24)
//
// Plata que la operación deja ir en el rango. La política vive en
// `app/services/analitica_fugas.py`; esta vista solo pinta lo que el servidor
// ya decidió (orden, totales, qué queda fuera del total y por qué).
//
// Depende del shell `analitica.js` (runtime, nunca parse-time):
// `anCargarPanel`, `anPesos`, `anNum`, `anPct`, `anFrescura`. Y de `get`
// (app.js) y `esc` (util.js).
//
// Reglas de pantalla:
//  · «sin valor» NUNCA se pinta como $0: se dice «sin valor» y cuántos casos;
//  · el total del período dice cuántas fugas quedaron fuera por no tener valor;
//  · ningún dato dentro de un onclick: los botones pasan una posición.
// ═══════════════════════════════════════════════════════════════════════════

const AN_FUGAS = { datos: null, detalle: null, clave: null, pagina: 1, filtros: null, carga: 0 };

const AN_FUGAS_ESTADO = {
  ok: { texto: 'Sin fugas', estilo: 'background:var(--ok-bg);color:var(--ok-tx);border:1px solid var(--ok-brd);' },
  advertencia: { texto: 'Hay fugas', estilo: 'background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);' },
  critico: { texto: 'Crece o sin dato', estilo: 'background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd);' },
};

function anFugasQs(f) {
  const p = [];
  if (f && f.almacen_id) p.push('almacen_id=' + encodeURIComponent(f.almacen_id));
  if (f && f.desde) p.push('desde=' + encodeURIComponent(f.desde));
  if (f && f.hasta) p.push('hasta=' + encodeURIComponent(f.hasta));
  return p.join('&');
}

function anFugasCargar(el, f) {
  AN_FUGAS.filtros = f || {};
  return anCargarPanel({
    el,
    clave: 'fugas',
    params: AN_FUGAS.filtros,
    pedir: () => get('/api/analitica/fugas?' + anFugasQs(AN_FUGAS.filtros)),
    html: (d) => { AN_FUGAS.datos = d; return anFugasHtml(d); },
    error: (e) => `<div style="padding:12px;color:var(--err-tx);font-size:var(--fs-sm);">No se pudieron calcular las fugas: ${esc(e && e.message ? e.message : e)}</div>`,
  });
}

// ── Piezas chicas ─────────────────────────────────────────────────────────

function anFugasPildora(estado) {
  const e = AN_FUGAS_ESTADO[estado] || AN_FUGAS_ESTADO.advertencia;
  const estilo = e.estilo;   // marcado propio, no dato
  return `<span style="${estilo}border-radius:10px;padding:1px 8px;font-size:var(--fs-xs);font-weight:600;white-space:nowrap;">${esc(e.texto)}</span>`;
}

function anFugasValor(f) {
  if (f.sin_dato) return `<span style="color:var(--err-tx);">Sin dato</span>`;
  if (f.pesos === null || f.pesos === undefined) {
    return `<span style="color:var(--warn-tx);">Sin valor</span>`;
  }
  const cota = f.es_cota_inferior ? `<span style="color:var(--tx3);font-size:var(--fs-xs);font-weight:400;"> (al menos)</span>` : '';
  return `${esc(anPesos(f.pesos))}${cota}`;
}

function anFugasTendencia(t) {
  if (!t || t.direccion === 'sin_base') {
    return `<span style="color:var(--tx3);">sin base para comparar</span>`;
  }
  const flecha = t.direccion === 'sube' ? '▲' : t.direccion === 'baja' ? '▼' : '＝';
  const color = t.direccion === 'sube' ? 'var(--err-tx)' : t.direccion === 'baja' ? 'var(--ok-tx)' : 'var(--tx3)';
  const cuanto = (t.delta_pesos !== null && t.delta_pesos !== undefined && t.anterior && t.anterior.pesos !== null)
    ? anPesos(Math.abs(t.delta_pesos))
    : `${anNum(Math.abs(t.delta_casos || 0))} casos`;
  const antes = t.anterior && t.anterior.pesos !== null && t.anterior.pesos !== undefined
    ? anPesos(t.anterior.pesos) : `${anNum(t.anterior ? t.anterior.casos : 0)} casos`;
  return `<span style="color:${color};">${esc(flecha)} ${esc(cuanto)}</span> <span style="color:var(--tx3);">vs. ${esc(antes)} el período anterior</span>`;
}

function anFugasDondePesa(f) {
  const alm = (f.por_almacen || [])[0];
  const mot = (f.por_motivo || [])[0];
  const partes = [];
  if (alm) partes.push(`${esc(alm.almacen)}${alm.ciudad ? ' · ' + esc(alm.ciudad) : ''}`);
  if (mot) partes.push(`${esc(f.dimension_motivo)}: ${esc(mot.motivo)}`);
  if (!partes.length) return '';
  return `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">Pesa más en ${partes.join(' — ')}</div>`;
}

// ── El resumen ────────────────────────────────────────────────────────────

function anFugasHtml(d) {
  const r = d.resumen || {};
  const fugas = d.fugas || [];
  const maxPesos = Math.max(1, ...fugas.map(f => Math.abs(f.aporte_al_total || 0)));
  const fuera = (r.fuera_del_total || []).map(x => `${esc(x.titulo)} (${esc(x.por)})`).join(' · ');
  const tarjetas = fugas.map((f, i) => {
    const barra = f.aporte_al_total
      ? `<div style="height:6px;border-radius:3px;background:var(--bg-s);margin-top:6px;"><div style="height:6px;border-radius:3px;background:var(--pm-fill);width:${Math.round(100 * Math.abs(f.aporte_al_total) / maxPesos)}%;"></div></div>`
      : '';
    const sinValor = f.sin_valor && f.sin_valor.casos
      ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:2px;">${esc(anNum(f.sin_valor.casos))} caso(s) sin precio o costo — no suman</div>` : '';
    const casos = f.casos === null || f.casos === undefined
      ? esc(f.sin_dato || 'sin dato')
      : `${esc(anNum(f.casos))} caso(s)${f.unidades !== null && f.unidades !== undefined ? ' · ' + esc(anNum(f.unidades)) + ' ' + esc(f.unidad) : ''}`;
    return `<div class="an-fuga" data-clave="${esc(f.clave)}" onclick="anFugasAbrir(${i})" style="cursor:pointer;background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:10px 12px;">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">
        <div style="font-size:var(--fs-sm);font-weight:600;color:var(--tx);">${esc(f.titulo)}</div>
        ${anFugasPildora(f.estado)}
      </div>
      <div style="font-size:var(--fs-lg);font-weight:700;color:var(--tx);margin-top:4px;">${anFugasValor(f)}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx2);">${casos}</div>
      ${sinValor}
      <div style="font-size:var(--fs-xs);margin-top:2px;">${anFugasTendencia(f.tendencia)}</div>
      ${anFugasDondePesa(f)}
      ${f.aporte_al_total === null && f.fuera_del_total_por && (f.casos || f.sin_dato) ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">Fuera del total: ${esc(f.fuera_del_total_por)}</div>` : ''}
      ${barra}
    </div>`;
  }).join('');

  return `<div style="display:flex;flex-direction:column;gap:12px;">
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:12px 14px;">
      <div style="font-size:var(--fs-sm);color:var(--tx2);">💸 Fugas del período · ${esc((d.meta || {}).almacen || 'Todos')}</div>
      <div style="font-size:var(--fs-2xl);font-weight:800;color:var(--tx);">${esc(anPesos(r.total_pesos))}${r.es_cota_inferior ? `<span style="font-size:var(--fs-sm);font-weight:400;color:var(--tx3);"> al menos</span>` : ''}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx2);">Suma ${esc(anNum(r.fugas_que_suman || 0))} fuga(s) con valor conocido. ${r.fugas_fuera_del_total ? esc(anNum(r.fugas_fuera_del_total)) + ' quedan fuera por no tener valor: ' + fuera : 'Ninguna quedó fuera.'}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);">Período anterior (igual duración): ${esc(anPesos(r.total_anterior_pesos))} · ${esc(anNum(r.casos || 0))} caso(s) en el período</div>
      <div style="margin-top:4px;">${anFrescura(d.meta || {})}</div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,260px),1fr));gap:10px;">${tarjetas}</div>
    <div id="an-fugas-detalle"></div>
  </div>`;
}

// ── El detalle (drill-down) ───────────────────────────────────────────────

function anFugasAbrir(i) {
  const f = AN_FUGAS.datos && AN_FUGAS.datos.fugas ? AN_FUGAS.datos.fugas[i] : null;
  if (!f) return;
  AN_FUGAS.clave = f.clave;
  AN_FUGAS.pagina = 1;
  anFugasPedirDetalle();
}

function anFugasPagina(delta) {
  AN_FUGAS.pagina = Math.max(1, AN_FUGAS.pagina + delta);
  anFugasPedirDetalle();
}

function anFugasCerrarDetalle() {
  AN_FUGAS.clave = null;
  AN_FUGAS.detalle = null;
  const el = document.getElementById('an-fugas-detalle');
  if (el) el.innerHTML = '';
}

async function anFugasPedirDetalle() {
  const el = document.getElementById('an-fugas-detalle');
  if (!el || !AN_FUGAS.clave) return;
  const n = ++AN_FUGAS.carga;
  if (!AN_FUGAS.detalle) el.innerHTML = `<div style="padding:10px;color:var(--tx3);font-size:var(--fs-sm);">Cargando el detalle…</div>`;
  try {
    const qs = anFugasQs(AN_FUGAS.filtros);
    const d = await get(`/api/analitica/fugas/${encodeURIComponent(AN_FUGAS.clave)}?${qs}&page=${AN_FUGAS.pagina}&per_page=25`);
    if (n !== AN_FUGAS.carga) return;          // una respuesta vieja no pisa
    AN_FUGAS.detalle = d;
    el.innerHTML = anFugasDetalleHtml(d);
    if (el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    if (n !== AN_FUGAS.carga) return;
    const aviso = `<div style="padding:8px;color:var(--err-tx);font-size:var(--fs-sm);">No se pudo cargar el detalle: ${esc(e && e.message ? e.message : e)}</div>`;
    el.innerHTML = AN_FUGAS.detalle ? aviso + anFugasDetalleHtml(AN_FUGAS.detalle) : aviso;
  }
}

function anFugasTablaGrupo(filas, etiqueta, clave) {
  if (!filas || !filas.length) return '';
  const cuerpo = filas.slice(0, 8).map(g => `<div style="display:flex;justify-content:space-between;gap:8px;padding:3px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-xs);">
      <span style="color:var(--tx);">${esc(g[clave])}</span>
      <span style="color:var(--tx2);white-space:nowrap;">${g.casos === g.sin_valor ? 'sin valor' : esc(anPesos(g.pesos))} · ${esc(anNum(g.casos))}${g.sin_valor && g.casos !== g.sin_valor ? ' (' + esc(anNum(g.sin_valor)) + ' sin valor)' : ''}</span>
    </div>`).join('');
  return `<div style="flex:1 1 240px;min-width:0;">
    <div style="font-size:var(--fs-xs);font-weight:600;color:var(--tx2);margin-bottom:4px;">${esc(etiqueta)}</div>${cuerpo}</div>`;
}

function anFugasDetalleCaso(c, i) {
  const det = c.detalle || {};
  const hechos = ['cliente', 'producto', 'nombre', 'que', 'lo_hizo', 'lo_tiro', 'nota_credito', 'como', 'error', 'dias', 'bodega']
    .filter(k => det[k] !== null && det[k] !== undefined && det[k] !== '')
    .map(k => `${esc(AN_FUGAS_ETIQUETAS[k] || k)}: ${esc(det[k])}`).join(' · ');
  const recorrido = c.pedido_clave
    ? `<button class="btn btn-sm" data-pedido-clave="${esc(c.pedido_clave)}" onclick="anFugasVerRecorrido(${i})" style="font-size:var(--fs-xs);">🧭 Recorrido</button>` : '';
  return `<div class="an-fuga-caso" data-pedido-clave="${esc(c.pedido_clave || '')}" style="padding:8px 0;border-bottom:1px solid var(--brd);">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">
      <div style="min-width:0;">
        <div style="font-size:var(--fs-sm);font-weight:600;color:var(--tx);overflow-wrap:anywhere;">${esc(c.referencia)}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx2);">${esc(c.almacen)} · ${esc(c.motivo)}${c.dia ? ' · ' + esc(c.dia) : ''}</div>
      </div>
      <div style="text-align:right;white-space:nowrap;">
        <div style="font-size:var(--fs-sm);font-weight:700;color:${c.sin_valor ? 'var(--warn-tx)' : 'var(--tx)'};">${c.sin_valor ? 'Sin valor' : esc(anPesos(c.pesos))}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);">${c.unidades === null || c.unidades === undefined ? '' : esc(anNum(c.unidades)) + ' und'}</div>
      </div>
    </div>
    ${hechos ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;overflow-wrap:anywhere;">${hechos}</div>` : ''}
    ${recorrido}
  </div>`;
}

const AN_FUGAS_ETIQUETAS = {
  cliente: 'Cliente', producto: 'Producto', nombre: 'Nombre', que: 'Qué pasó', lo_hizo: 'Lo hizo',
  lo_tiro: 'Lo tiró', nota_credito: 'NC', como: 'Cómo', error: 'Error', dias: 'Días', bodega: 'Bodega',
};

function anFugasDetalleHtml(d) {
  const f = d.fuga || {};
  const casos = (d.casos || []).map((c, i) => anFugasDetalleCaso(c, i)).join('')
    || `<div style="padding:8px;color:var(--tx3);font-size:var(--fs-sm);">Sin casos en el período.</div>`;
  const paginas = Math.max(1, Math.ceil((d.total || 0) / (d.por_pagina || 25)));
  const nav = paginas > 1 ? `<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:var(--fs-xs);color:var(--tx2);">
      <button class="btn btn-sm" onclick="anFugasPagina(-1)" ${d.pagina <= 1 ? 'disabled' : ''}>← Anterior</button>
      <span>Página ${esc(anNum(d.pagina))} de ${esc(anNum(paginas))} · ${esc(anNum(d.total))} casos</span>
      <button class="btn btn-sm" onclick="anFugasPagina(1)" ${d.pagina >= paginas ? 'disabled' : ''}>Siguiente →</button>
    </div>` : '';
  const faltan = (f.faltan || []).length
    ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);">Sin medir: ${(f.faltan || []).map(x => esc(x)).join(' · ')}</div>` : '';
  return `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:12px 14px;margin-top:4px;">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">
      <div>
        <div style="font-size:var(--fs-md);font-weight:700;color:var(--tx);">${esc(f.titulo)}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx2);">${esc(f.definicion)}</div>
      </div>
      <button class="btn btn-sm" onclick="anFugasCerrarDetalle()" style="font-size:var(--fs-xs);">✕</button>
    </div>
    <div style="font-size:var(--fs-lg);font-weight:700;color:var(--tx);margin-top:6px;">${anFugasValor(f)}</div>
    <div style="font-size:var(--fs-xs);margin-bottom:6px;">${anFugasTendencia(f.tendencia)}</div>
    ${faltan}
    <div style="display:flex;flex-wrap:wrap;gap:12px;margin:8px 0;">
      ${anFugasTablaGrupo(f.por_almacen, 'Por almacén', 'almacen')}
      ${anFugasTablaGrupo(f.por_motivo, 'Por ' + (f.dimension_motivo || 'motivo').toLowerCase(), 'motivo')}
    </div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);">Los casos sin valor van primero: no saber cuánto valen no los vuelve chicos.</div>
    ${casos}
    ${nav}
    <div style="margin-top:6px;">${anFrescura(d.meta || {})}</div>
  </div>`;
}

// El recorrido del pedido lo pinta otra vista (🧭 Recorrido). Acá solo se
// entrega la clave: si la vista existe, se abre; si no, se muestra la clave.
function anFugasVerRecorrido(i) {
  const c = AN_FUGAS.detalle && AN_FUGAS.detalle.casos ? AN_FUGAS.detalle.casos[i] : null;
  if (!c || !c.pedido_clave) return;
  if (typeof anRecorridoAbrir === 'function') {
    anRecorridoAbrir(c.pedido_clave);
  } else if (typeof alerta === 'function') {
    alerta('Pedido ' + c.pedido_clave, 'info');
  }
}
