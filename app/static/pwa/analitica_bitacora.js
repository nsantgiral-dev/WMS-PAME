/* ──────────────────────────────────────────────────────────────────────────
 * 📈 Analítica → 📜 Bitácora  (Fase 1, 2026-09-24)
 *
 * Lo que alguien eliminó, canceló, anuló, reabrió o editó: quién, cuándo, por
 * qué y qué cambió. Lee GET /api/analitica/bitacora (la lista, ya enriquecida
 * por `analitica_salud.describir_acciones`: frase, nombre, pedido, hora
 * Bogotá, antes → después) y GET /api/analitica/bitacora/patrones (por
 * persona, por acción y por hora del día, cada uno con su n).
 *
 * Filtros propios (acción, entidad, persona) dentro del rango y el almacén
 * de la barra común (`anFiltros()` del shell). Todo dato con esc(); los
 * controles pasan una posición o el valor del propio control, nunca el dato.
 * ────────────────────────────────────────────────────────────────────────── */

const AN_BIT = {
  el: null, f: null,
  filtros: { accion: '', entidad: '', usuario_id: '' },
  pagina: 1, porPagina: 50,
  acciones: [], total: 0, vocabulario: [], patrones: null,
  abierta: null, cargandoMas: false,
};

function anBitQs(f, extra) {
  const p = new URLSearchParams();
  if (f && f.desde) p.set('desde', f.desde);
  if (f && f.hasta) p.set('hasta', f.hasta);
  if (f && f.almacen_id) p.set('almacen_id', f.almacen_id);
  const fl = AN_BIT.filtros;
  if (fl.accion) p.set('accion', fl.accion);
  if (fl.entidad) p.set('entidad', fl.entidad);
  if (fl.usuario_id) p.set('usuario_id', fl.usuario_id);
  Object.entries(extra || {}).forEach(([k, v]) => p.set(k, v));
  return p.toString();
}

function anBitacoraCargar(el, f) {
  AN_BIT.el = el;
  AN_BIT.f = f;
  AN_BIT.pagina = 1;
  AN_BIT.abierta = null;
  const qs = anBitQs(f);
  const qsLista = anBitQs(f, { page: 1, per_page: AN_BIT.porPagina });
  return anCargarPanel({
    el, clave: 'bitacora', params: qs,
    pedir: async () => {
      const [lista, patrones] = await Promise.all([
        get('/api/analitica/bitacora?' + qsLista),
        get('/api/analitica/bitacora/patrones?' + qs),
      ]);
      return { lista, patrones };
    },
    html: (d) => {
      AN_BIT.acciones = (d.lista && d.lista.acciones) || [];
      AN_BIT.total = (d.lista && d.lista.total) || 0;
      AN_BIT.vocabulario = (d.lista && d.lista.vocabulario) || [];
      AN_BIT.patrones = d.patrones || null;
      return anBitHtml(AN_BIT);
    },
  });
}

function _anBitRepintar() {
  if (AN_BIT.el) AN_BIT.el.innerHTML = anBitHtml(AN_BIT);
}

/** Un filtro propio cambió: se vuelve a pedir la primera página. */
function anBitFiltro(campo, valor) {
  if (!(campo in AN_BIT.filtros)) return;
  AN_BIT.filtros[campo] = valor || '';
  if (AN_BIT.el) anBitacoraCargar(AN_BIT.el, AN_BIT.f);
}

function anBitLimpiar() {
  AN_BIT.filtros = { accion: '', entidad: '', usuario_id: '' };
  if (AN_BIT.el) anBitacoraCargar(AN_BIT.el, AN_BIT.f);
}

/** Filtrar por la persona de la fila i del patrón (clic en la barra). */
function anBitPorPersona(i) {
  const p = ((AN_BIT.patrones || {}).por_persona || [])[Number(i)];
  if (!p || p.usuario_id === null || p.usuario_id === undefined) return;
  anBitFiltro('usuario_id', String(p.usuario_id));
}

function anBitPorAccion(i) {
  const a = ((AN_BIT.patrones || {}).por_accion || [])[Number(i)];
  if (a) anBitFiltro('accion', a.accion);
}

function anBitAbrir(i) {
  AN_BIT.abierta = AN_BIT.abierta === Number(i) ? null : Number(i);
  _anBitRepintar();
}

async function anBitMas() {
  if (AN_BIT.cargandoMas || AN_BIT.acciones.length >= AN_BIT.total) return;
  AN_BIT.cargandoMas = true;
  try {
    const sig = AN_BIT.pagina + 1;
    const d = await get('/api/analitica/bitacora?' + anBitQs(AN_BIT.f, { page: sig, per_page: AN_BIT.porPagina }));
    AN_BIT.pagina = sig;
    AN_BIT.acciones = AN_BIT.acciones.concat(d.acciones || []);
    AN_BIT.total = d.total || AN_BIT.total;
  } catch (e) {
    if (typeof alerta === 'function') alerta('No se pudieron traer más acciones: ' + (e && e.message ? e.message : e));
  } finally {
    AN_BIT.cargandoMas = false;
    _anBitRepintar();
  }
}

function _anBitNum(n) {
  if (n === null || n === undefined) return 'sin dato';
  return (typeof anNum === 'function') ? anNum(n) : String(n);
}

function _anBitOpciones(lista, valorActual, valorDe, textoDe, todos) {
  const ops = [`<option value="">${esc(todos)}</option>`].concat((lista || []).map(o => {
    const v = String(valorDe(o));
    return `<option value="${esc(v)}"${v === String(valorActual) ? ' selected' : ''}>${esc(textoDe(o))}</option>`;
  }));
  return ops.join('');
}

function _anBitBarras(filas, etiqueta, clic) {
  const max = Math.max(1, ...filas.map(x => x.n || 0));
  return filas.map((x, i) => `
    <div class="tabla-fila" style="gap:8px;${clic ? 'cursor:pointer;' : ''}"${clic ? ` onclick="${clic}(${i})"` : ''}>
      <span class="tabla-nombre" style="flex:0 0 40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${esc(etiqueta(x))}</span>
      <span style="flex:1;background:var(--bg-s);border:1px solid var(--brd);border-radius:6px;height:12px;overflow:hidden;">
        <span style="display:block;height:100%;width:${Math.round(100 * (x.n || 0) / max)}%;background:var(--pm-fill);"></span>
      </span>
      <span style="flex:0 0 auto;font-size:var(--fs-sm);color:var(--tx);">${esc(_anBitNum(x.n))}</span>
    </div>`).join('');
}

function _anBitFila(a, i, abierta) {
  // El motivo en palabras (un código de bloqueo llega traducido por el
  // servidor). Liquidar y bloquear no piden un motivo escrito: no se les
  // pinta «sin motivo», que acusaría una falta que no existe.
  const texto = a.motivo_legible || a.motivo;
  const motivo = texto ? `— motivo: «${esc(texto)}»`
    : (a.pide_motivo === false ? '' : '— sin motivo');
  const cuando = `${esc(a.dia_operativo || '')} ${esc(a.hora_bogota || '')}`;
  const donde = a.almacen_nombre ? ` · ${esc(a.almacen_nombre)}` : '';
  let detalle = '';
  if (abierta) {
    const cambios = (a.cambios || []).map(c => `
      <div style="display:flex;gap:6px;flex-wrap:wrap;font-size:var(--fs-sm);padding:2px 0;">
        <span style="color:var(--tx3);min-width:30%;">${esc(c.campo)}</span>
        <span style="color:var(--tx2);">${esc(c.antes)}</span>
        <span style="color:var(--tx3);">→</span>
        <span style="color:var(--tx);">${esc(c.despues)}</span>
      </div>`).join('');
    detalle = `<div style="margin-top:6px;padding:8px;border:1px solid var(--brd);border-radius:8px;">
      ${cambios || '<div style="font-size:var(--fs-sm);color:var(--tx3);">No se guardó qué cambió.</div>'}
      ${a.cambios_omitidos ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">y ${esc(_anBitNum(a.cambios_omitidos))} campo(s) más</div>` : ''}
      ${a.origen ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">Origen: ${esc(a.origen)}</div>` : ''}
    </div>`;
  }
  return `<div class="tabla-fila" style="flex-direction:column;align-items:stretch;cursor:pointer;" onclick="anBitAbrir(${i})">
    <div style="font-size:var(--fs-sm);color:var(--tx);">${esc(a.frase)} <span style="color:var(--tx2);">${motivo}</span></div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);">${cuando}${donde}</div>
    ${detalle}
  </div>`;
}

/** El HTML completo de la vista. Función pura: se prueba en Node. */
function anBitHtml(s) {
  const pat = s.patrones || {};
  const op = pat.opciones || {};
  const fl = s.filtros || {};
  const frescura = (typeof anFrescura === 'function') ? anFrescura(pat.meta) : '';
  const total = pat.total;
  const sinMotivo = pat.sin_motivo;
  // Denominador: solo las acciones que piden un motivo escrito (el servidor
  // excluye liquidar y bloquear, `sin_motivo_excluye`).
  const baseMotivo = pat.sin_motivo_base === undefined || pat.sin_motivo_base === null ? total : pat.sin_motivo_base;
  const pctSinMotivo = baseMotivo ? `${Math.round(100 * sinMotivo / baseMotivo)} % de ${_anBitNum(baseMotivo)}` : '—';
  const personas = pat.por_persona || [];
  const acciones = pat.por_accion || [];
  const horas = pat.por_hora || [];
  const quien = personas[0];

  const filtros = `
  <div class="tabla-card" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
    <select onchange="anBitFiltro('accion', this.value)" style="flex:1 1 140px;min-width:0;">
      ${_anBitOpciones(op.acciones, fl.accion, o => o.accion, o => (o.verbo || o.accion), 'Toda acción')}
    </select>
    <select onchange="anBitFiltro('entidad', this.value)" style="flex:1 1 140px;min-width:0;">
      ${_anBitOpciones(op.entidades, fl.entidad, o => o.entidad, o => o.nombre + ' (' + _anBitNum(o.n) + ')', 'Todo tipo de registro')}
    </select>
    <select onchange="anBitFiltro('usuario_id', this.value)" style="flex:1 1 140px;min-width:0;">
      ${_anBitOpciones(op.personas, fl.usuario_id, o => o.usuario_id, o => o.nombre + ' (' + _anBitNum(o.n) + ')', 'Toda persona')}
    </select>
    ${(fl.accion || fl.entidad || fl.usuario_id) ? '<button class="btn-flota" onclick="anBitLimpiar()">Quitar filtros</button>' : ''}
  </div>`;

  const kpis = `
  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi-valor">${esc(_anBitNum(total))}</div>
      <div class="kpi-label">Acciones en el rango</div><div class="kpi-sub">eliminar, cancelar, reabrir, editar…</div></div>
    <div class="kpi-card"><div class="kpi-valor">${esc(pctSinMotivo)}</div>
      <div class="kpi-label">Sin motivo escrito</div><div class="kpi-sub">${esc(_anBitNum(sinMotivo))} de las que piden motivo · liquidar y bloquear no cuentan</div></div>
    <div class="kpi-card"><div class="kpi-valor">${esc(_anBitNum(personas.length))}</div>
      <div class="kpi-label">Personas</div><div class="kpi-sub">${quien ? esc(quien.nombre) + ' encabeza con ' + esc(_anBitNum(quien.n)) : 'nadie en el rango'}</div></div>
  </div>`;

  const horaEtiqueta = x => `${String(x.hora).padStart(2, '0')}:00`;
  const patrones = `
  <div class="tabla-card"><div class="tabla-titulo">Por persona</div>
    ${personas.length ? _anBitBarras(personas, x => x.nombre, 'anBitPorPersona') : '<div class="tabla-nombre">Sin acciones</div>'}</div>
  <div class="tabla-card"><div class="tabla-titulo">Por acción</div>
    ${acciones.length ? _anBitBarras(acciones, x => x.verbo || x.accion, 'anBitPorAccion') : '<div class="tabla-nombre">Sin acciones</div>'}</div>
  <div class="tabla-card"><div class="tabla-titulo">Por hora del día (Bogotá) · n = ${esc(_anBitNum(pat.por_hora_n))}</div>
    ${horas.some(x => x.n) ? _anBitBarras(horas.filter(x => x.n), horaEtiqueta, null) : '<div class="tabla-nombre">Sin acciones</div>'}
    ${pat.tope && pat.tope.truncado ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);">Solo las últimas ${esc(_anBitNum(pat.tope.filas_por_hora))} acciones</div>` : ''}
  </div>`;

  const filas = (s.acciones || []).map((a, i) => _anBitFila(a, i, s.abierta === i)).join('');
  const mas = (s.acciones || []).length < (s.total || 0)
    ? `<button class="btn-flota" style="width:100%;margin-top:8px;" onclick="anBitMas()">Ver más (${esc(_anBitNum((s.acciones || []).length))} de ${esc(_anBitNum(s.total))})</button>` : '';
  const lista = `<div class="tabla-card"><div class="tabla-titulo">Qué pasó, de lo más reciente a lo más viejo</div>
    ${filas || '<div class="tabla-nombre">Nada registrado con estos filtros.</div>'}${mas}</div>`;

  const nota = (pat.meta && pat.meta.fuentes && pat.meta.fuentes.bitacora_acciones)
    ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin:4px 0 8px;">${esc(pat.meta.fuentes.bitacora_acciones.nota)}</div>` : '';

  return `${frescura}${filtros}${kpis}${lista}${patrones}${nota}`;
}
