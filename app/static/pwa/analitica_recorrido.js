/**
 * 📈 Analítica → 🧭 Recorrido del pedido (Fase 1, 2026-09-24).
 *
 * De lo que Siesa aprobó, cuánto se volvió caja liquidada, en cuánto tiempo y
 * dónde se cayó lo demás. Arriba las cifras que un gerente lee en diez
 * segundos; abajo el embudo por etapa; al tocar una etapa, sus pedidos; al
 * tocar un pedido, su línea de tiempo entera.
 *
 * Todo número sale del servidor (`app/services/analitica_recorrido.py`): acá
 * no se calcula ninguna tasa. La pantalla solo decide cómo se ve, y dice lo que
 * no se sabe («sin valor», «sin marca», «sin clave») en vez de pintarlo en cero.
 *
 * Los botones pasan un índice, nunca un dato: el pedido se busca en la lista
 * que ya está en memoria (`_AN_REC`).
 */

const _AN_REC = { f: null, d: null, filas: [], sueltos: [], etapa: null, vista: 'en', pagina: 1 };
const _AN_REC_POR_PAGINA = 25;

/**
 * Referencias con las que se pinta la píldora. **Provisionales**: no son metas
 * del negocio, son el punto a partir del cual la cifra merece mirarse.
 * Ciclo de caja: la factura de ruta nace a crédito de un día y la ruta se
 * liquida el mismo día (ver «Alerta de ruta entregada sin liquidar»).
 */
const AN_REC_REFERENCIAS = {
  ciclo_dias: { ok: 2, advertencia: 5 },
  sin_fuga: { ok: 0.95, advertencia: 0.85 },
};

const _AN_REC_ESTADO = {
  COMPLETO_SIN_FUGA: 'ok', COMPLETO_CON_FUGA: 'advertencia', FUGA: 'critico',
  DETENIDO: 'advertencia', EN_CURSO: 'neutro', FUERA_DEL_WMS: 'neutro',
};

/** Entrada desde el shell: `f` = `{almacen_id, desde, hasta}`. */
function anRecorridoCargar(el, f) {
  _AN_REC.f = f;
  const params = anQuery(f);
  return anCargarPanel({
    el, clave: 'recorrido', params,
    pedir: () => get('/api/analitica/recorrido?' + params),
    html: (d) => anRecHtml(d),
  });
}

// ── Piezas de estilo ────────────────────────────────────────────────────────

/** Píldora de estado: ok / advertencia / critico / neutro. */
function anRecPildora(nivel, texto) {
  const t = {
    ok: 'background:var(--ok-bg);color:var(--ok-tx);border:1px solid var(--ok-brd);',
    advertencia: 'background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);',
    critico: 'background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd);',
    neutro: 'background:var(--bg-s2);color:var(--tx2);border:1px solid var(--brd);',
  }[nivel] || '';
  return `<span style="${t}display:inline-block;padding:2px 8px;border-radius:12px;font-size:var(--fs-xs);font-weight:700;white-space:nowrap;">${esc(texto)}</span>`;
}

/** «2,5 h» o «3,1 días» — horas si es menos de dos días. */
function anRecDuracion(horas) {
  if (horas === null || horas === undefined) return 'sin dato';
  const h = Number(horas);
  return h < 48 ? `${anNum(h)} h` : `${anNum(h / 24)} días`;
}

function anRecNivelCiclo(dias) {
  if (dias === null || dias === undefined) return ['neutro', 'sin medición'];
  const r = AN_REC_REFERENCIAS.ciclo_dias;
  if (dias <= r.ok) return ['ok', 'a tiempo'];
  if (dias <= r.advertencia) return ['advertencia', 'lento'];
  return ['critico', 'muy lento'];
}

function anRecNivelSinFuga(tasa) {
  if (tasa === null || tasa === undefined) return ['neutro', 'sin base'];
  const r = AN_REC_REFERENCIAS.sin_fuga;
  if (tasa >= r.ok) return ['ok', 'sano'];
  if (tasa >= r.advertencia) return ['advertencia', 'con pérdidas'];
  return ['critico', 'pérdidas altas'];
}

function anRecKpi(valor, etiqueta, sub, pildora) {
  return `<div class="kpi-card">
    <div class="kpi-valor" style="font-size:var(--fs-2xl);">${valor}</div>
    <div class="kpi-label">${etiqueta} ${pildora || ''}</div>
    <div class="kpi-sub">${sub}</div></div>`;
}

// ── El resumen y el embudo ──────────────────────────────────────────────────

/** Toda la vista del recorrido. `d` = respuesta de `/api/analitica/recorrido`. */
function anRecHtml(d) {
  _AN_REC.d = d;
  _AN_REC.sueltos = (d.sin_enlazar && d.sin_enlazar.muestra) || [];
  const g = d.guia || {};
  const ciclo = g.ciclo_caja || {};
  const sf = g.valor_sin_fuga || {};
  const sfc = g.valor_sin_fuga_cerrados || {};
  const comp = g.composicion || [];
  const fuga = comp.find(c => c.estado === 'FUGA') || {};
  const det = comp.find(c => c.estado === 'DETENIDO') || {};
  const se = d.sin_enlazar || {};
  const [nC, tC] = anRecNivelCiclo(ciclo.mediana_dias);
  const [nF, tF] = anRecNivelSinFuga(sfc.tasa);

  const kpis = [
    anRecKpi(esc(anNum(d.pedidos)), 'Pedidos que entraron',
      `del ${esc(d.meta && d.meta.desde)} al ${esc(d.meta && d.meta.hasta)}`, ''),
    anRecKpi(ciclo.mediana_dias === null || ciclo.mediana_dias === undefined ? 'sin dato'
      : `${esc(anNum(ciclo.mediana_dias))} días`, 'Ciclo de caja (mediana)',
      `p90 ${esc(ciclo.p90_dias === null || ciclo.p90_dias === undefined ? 'sin dato' : anNum(ciclo.p90_dias) + ' días')} · n=${esc(anNum(ciclo.n))} de ${esc(anNum(ciclo.liquidados))} liquidados`
        + (ciclo.sin_marca ? ` · ${esc(anNum(ciclo.sin_marca))} sin marca de aprobación` : ''),
      anRecPildora(nC, tC)),
    anRecKpi(esc(anPct(sfc.tasa, sfc.pedidos_base)), 'Valor que cerró sin fuga',
      `de lo ya cerrado · ${esc(anPesos(sfc.valor_base))}. Sobre toda la cohorte: ${esc(anPct(sf.tasa, sf.pedidos_base))}`
        + (sf.pedidos_sin_valor ? ` · ${esc(anNum(sf.pedidos_sin_valor))} sin valor` : ''),
      anRecPildora(nF, tF)),
    anRecKpi(esc(anNum(fuga.pedidos || 0)), 'Se cayeron',
      `${esc(anPesos(fuga.valor))}` + (det.pedidos ? ` · ${esc(anNum(det.pedidos))} detenidos` : ''),
      fuga.pedidos ? anRecPildora('critico', 'revisar') : anRecPildora('ok', 'ninguno')),
  ].join('');

  const embudo = d.embudo || [];
  const n0 = embudo.length ? embudo[0].pedidos : 0;
  const filas = embudo.map((e, i) => anRecFilaEtapa(e, i, n0)).join('');

  const compHtml = comp.filter(c => c.pedidos).map(c =>
    `<div style="display:flex;justify-content:space-between;gap:8px;padding:4px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-sm);">
       <span>${anRecPildora(_AN_REC_ESTADO[c.estado] || 'neutro', c.titulo)}</span>
       <span style="color:var(--tx2);">${esc(anNum(c.pedidos))} pedidos · ${esc(anPesos(c.valor))}${c.sin_valor ? ` · ${esc(anNum(c.sin_valor))} sin valor` : ''}</span>
     </div>`).join('') || '<div style="color:var(--tx3);font-size:var(--fs-sm);">Ningún pedido en el rango.</div>';

  const defs = d.definiciones || {};
  const defsHtml = Object.keys(defs).map(k =>
    `<div style="margin:4px 0;"><b>${esc(k.replace(/_/g, ' '))}</b>: ${esc(defs[k])}</div>`).join('');

  return `${anFrescura(d.meta)}
    <div class="kpi-grid">${kpis}</div>
    <div class="tabla-card">
      <div class="tabla-titulo">Embudo: del pedido aprobado a la caja liquidada</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">
        Tocá una etapa para ver sus pedidos. El ancho es la parte de los ${esc(anNum(n0))} pedidos que llegó.
      </div>
      ${filas}
    </div>
    <div id="an-rec-detalle"></div>
    <div class="tabla-card">
      <div class="tabla-titulo">Cómo terminó cada pedido</div>${compHtml}
    </div>
    ${anRecSinEnlazarHtml(se)}
    <details class="tabla-card" style="font-size:var(--fs-xs);color:var(--tx2);">
      <summary style="cursor:pointer;font-size:var(--fs-sm);color:var(--tx);">Cómo se mide cada cifra</summary>
      ${defsHtml}
      <div style="margin-top:6px;">Las píldoras usan referencias provisionales (ciclo ≤ ${esc(AN_REC_REFERENCIAS.ciclo_dias.ok)} días a tiempo; sin fuga ≥ ${esc(AN_REC_REFERENCIAS.sin_fuga.ok * 100)} % sano), no metas del negocio.</div>
    </details>`;
}

/** Una etapa del embudo: barra, cifras, conversión, tiempo y fugas. */
function anRecFilaEtapa(e, i, n0) {
  const ancho = n0 ? Math.max(2, Math.round(100 * e.pedidos / n0)) : 0;
  const conv = e.conversion_anterior
    ? `${esc(anPct(e.conversion_anterior.tasa, e.conversion_anterior.n))} de la anterior`
    : 'punto de partida';
  const t = e.tiempo_desde_anterior;
  const tiempo = t
    ? (t.n ? `mediana ${esc(anRecDuracion(t.mediana_horas))} · p90 ${esc(anRecDuracion(t.p90_horas))} · n=${esc(anNum(t.n))}`
      : 'tiempo sin medición (n=0)')
      + (t.sin_marca ? ` · ${esc(anNum(t.sin_marca))} sin marca` : '')
      + (t.negativos ? ` · ${esc(anNum(t.negativos))} con marcas en orden imposible` : '')
    : '';
  const valor = `${esc(anPesos(e.valor))}${e.sin_valor ? ` · ${esc(anNum(e.sin_valor))} sin valor` : ''}`;
  let extra = '';
  if (e.valor_facturado) {
    extra += `<div>Facturado: ${esc(anPesos(e.valor_facturado.valor))} (${esc(anNum(e.valor_facturado.n))} de ${esc(anNum(e.valor_facturado.de))} con valor de factura)</div>`;
  }
  if (e.cobrado_en_puerta) {
    const c = e.cobrado_en_puerta;
    extra += `<div>Cobrado en la puerta: ${esc(anPesos(c.valor))} (${esc(anNum(c.n))} de ${esc(anNum(c.de))})${c.a_credito ? ` · ${esc(anNum(c.a_credito))} a crédito, pasan a cartera` : ''}</div>`;
  }
  const chips = (lista, nivel, vista) => (lista || []).map(f =>
    `<span onclick="event.stopPropagation();anRecEtapa(${i},'${vista}')" style="cursor:pointer;margin:2px 4px 0 0;display:inline-block;">${anRecPildora(nivel, f.motivo + ' · ' + anNum(f.pedidos) + ' · ' + anPesos(f.valor))}</span>`).join('');
  const fugas = chips(e.fugas, 'critico', 'fuga') + chips(e.detenidos, 'advertencia', 'fuga')
    + chips(e.perdidas_parciales, 'advertencia', 'llegaron');
  return `<div onclick="anRecEtapa(${i},'en')" style="cursor:pointer;padding:8px 0;border-bottom:1px solid var(--brd);">
    <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:var(--fs-sm);">
      <b>${esc(e.titulo)}</b>
      <span>${esc(anNum(e.pedidos))} pedidos · ${valor}</span>
    </div>
    <div style="height:14px;background:var(--bg-s2);border-radius:7px;margin:4px 0;overflow:hidden;">
      <div style="height:100%;width:${ancho}%;background:var(--pm-fill);border-radius:7px;"></div>
    </div>
    <div style="font-size:var(--fs-xs);color:var(--tx2);">${conv}${tiempo ? ' · ' + tiempo : ''}${e.en_etapa ? ` · <b>${esc(anNum(e.en_etapa))} esperando aquí</b>` : ''}${e.sin_marca ? ` · ${esc(anNum(e.sin_marca))} sin marca de hora` : ''}</div>
    ${extra ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">${extra}</div>` : ''}
    ${fugas ? `<div style="margin-top:2px;">${fugas}</div>` : ''}
  </div>`;
}

/** Lo que no tiene clave: se ve, con su mini embudo, y no entra al principal. */
function anRecSinEnlazarHtml(se) {
  if (!se || (!se.empaques && !se.picking_sin_clave && !se.historia_sin_clave)) {
    return `<div class="tabla-card" style="font-size:var(--fs-sm);color:var(--tx2);">
      <div class="tabla-titulo">Registros sin clave de pedido</div>Ninguno en el rango: toda la cadena se pudo unir.</div>`;
  }
  const mini = (se.embudo || []).map(e =>
    `<span style="margin-right:10px;">${esc(e.titulo)}: <b>${esc(anNum(e.pedidos))}</b></span>`).join('');
  const muestra = (se.muestra || []).map((p, k) =>
    `<div onclick="anRecPedidoSuelto(${k})" style="cursor:pointer;padding:4px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-sm);">
       ${esc(p.numero)} · ${esc(p.cliente || 'sin cliente')} · ${esc(p.etapa_actual_titulo)}</div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Registros sin clave de pedido ${anRecPildora('advertencia', 'no entran al embudo')}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:6px;">${esc(se.que_significa)}</div>
    <div style="font-size:var(--fs-sm);margin-bottom:6px;">
      ${esc(anNum(se.empaques))} empaques sin clave · ${esc(anNum(se.picking_sin_clave))} tareas de picking sin clave · ${esc(anNum(se.historia_sin_clave))} líneas de Siesa sin clave
      · facturado ${esc(anPesos(se.valor_facturado))}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:6px;">${mini}</div>
    ${muestra}
  </div>`;
}

// ── Drill-down: los pedidos de una etapa ────────────────────────────────────

/** Abre la lista de una etapa. `vista`: 'en' | 'llegaron' | 'fuga'. */
function anRecEtapa(i, vista = 'en', pagina = 1) {
  const e = _AN_REC.d && _AN_REC.d.embudo && _AN_REC.d.embudo[i];
  if (!e) return;
  _AN_REC.etapa = i; _AN_REC.vista = vista; _AN_REC.pagina = pagina;
  const params = anQuery(_AN_REC.f, { vista, page: pagina, per_page: _AN_REC_POR_PAGINA });
  anCargarPanel({
    el: document.getElementById('an-rec-detalle'), clave: 'recorrido-detalle',
    params: e.etapa + '?' + params,
    pedir: () => get(`/api/analitica/recorrido/etapa/${encodeURIComponent(e.etapa)}?${params}`),
    html: (d) => anRecEtapaHtml(d),
  });
}

function anRecEtapaVista(vista) { anRecEtapa(_AN_REC.etapa, vista, 1); }
function anRecPagina(delta) { anRecEtapa(_AN_REC.etapa, _AN_REC.vista, Math.max(1, _AN_REC.pagina + delta)); }
function anRecCerrarDetalle() {
  const el = document.getElementById('an-rec-detalle');
  if (el) el.innerHTML = '';
}

/** La tabla de pedidos de una etapa. `d` = respuesta de `/recorrido/etapa/<etapa>`. */
function anRecEtapaHtml(d) {
  _AN_REC.filas = d.pedidos || [];
  const vistas = [['en', 'Esperando aquí'], ['llegaron', 'Llegaron'], ['fuga', 'Se cayeron o detuvieron']];
  const botones = vistas.map(([v, t]) =>
    `<button onclick="anRecEtapaVista('${v}')" style="padding:5px 10px;border-radius:6px;cursor:pointer;font-size:var(--fs-xs);border:1px solid ${d.vista === v ? 'var(--acento-brd)' : 'var(--brd)'};background:${d.vista === v ? 'var(--acento-bg)' : 'transparent'};color:${d.vista === v ? 'var(--acento-tx)' : 'var(--tx2)'};">${t}</button>`).join(' ');
  const filas = _AN_REC.filas.map((p, k) => `
    <tr onclick="anRecPedido(${k})" style="cursor:pointer;border-bottom:1px solid var(--brd);">
      <td style="padding:6px 4px;"><b>${esc(p.numero)}</b>${p.co ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">CO ${esc(p.co)}</div>` : ''}</td>
      <td style="padding:6px 4px;">${esc(p.cliente || 'sin cliente')}</td>
      <td style="padding:6px 4px;text-align:right;white-space:nowrap;">${esc(anPesos(p.valor))}</td>
      <td style="padding:6px 4px;">${anRecPildora(_AN_REC_ESTADO[p.estado] || 'neutro', p.estado_titulo)}
        <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(p.etapa_actual_titulo)}${p.motivo ? ' · ' + esc(p.motivo) : ''}${(p.perdidas_parciales || []).length ? ' · ' + esc(p.perdidas_parciales.join(', ')) : ''}</div></td>
      <td style="padding:6px 4px;text-align:right;white-space:nowrap;">${p.dias_en_etapa === null || p.dias_en_etapa === undefined ? 'sin marca' : esc(anNum(p.dias_en_etapa)) + ' d'}</td>
    </tr>`).join('');
  const total = d.total || 0;
  const desde = total ? (d.pagina - 1) * d.por_pagina + 1 : 0;
  const hasta = Math.min(total, d.pagina * d.por_pagina);
  return `<div class="tabla-card">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;">
      <div class="tabla-titulo" style="margin:0;">${esc(d.titulo)} — ${esc(anNum(total))} pedidos</div>
      <button onclick="anRecCerrarDetalle()" style="background:none;border:1px solid var(--brd);color:var(--tx2);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:var(--fs-xs);">Cerrar</button>
    </div>
    <div style="margin:8px 0;">${botones}</div>
    ${total ? `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:var(--fs-sm);">
      <thead><tr style="color:var(--tx3);font-size:var(--fs-xs);text-align:left;">
        <th style="padding:4px;">Pedido</th><th style="padding:4px;">Cliente</th><th style="padding:4px;text-align:right;">Valor</th>
        <th style="padding:4px;">Cómo va</th><th style="padding:4px;text-align:right;">Días en la etapa</th></tr></thead>
      <tbody>${filas}</tbody></table></div>`
      : '<div style="color:var(--tx3);font-size:var(--fs-sm);padding:8px 0;">Ningún pedido en esta vista.</div>'}
    <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:var(--fs-xs);color:var(--tx3);">
      <span>${esc(anNum(desde))}–${esc(anNum(hasta))} de ${esc(anNum(total))} · tocá un pedido para ver su recorrido</span>
      <span>
        ${d.pagina > 1 ? '<button onclick="anRecPagina(-1)" style="background:none;border:1px solid var(--brd);color:var(--tx2);border-radius:6px;padding:3px 8px;cursor:pointer;font-size:var(--fs-xs);">‹ Anterior</button>' : ''}
        ${hasta < total ? '<button onclick="anRecPagina(1)" style="background:none;border:1px solid var(--brd);color:var(--tx2);border-radius:6px;padding:3px 8px;cursor:pointer;font-size:var(--fs-xs);">Siguiente ›</button>' : ''}
      </span>
    </div>
  </div>`;
}

// ── La línea de tiempo de un pedido ─────────────────────────────────────────

function anRecPedido(k) {
  const p = _AN_REC.filas[k];
  if (p) anRecAbrirPedido(p.clave);
}

function anRecPedidoSuelto(k) {
  const p = _AN_REC.sueltos[k];
  if (p) anRecAbrirPedido(p.clave);
}

function anRecAbrirPedido(clave) {
  anCargarPanel({
    el: document.getElementById('an-rec-detalle'), clave: 'recorrido-detalle',
    params: 'pedido:' + clave,
    pedir: () => get(`/api/analitica/recorrido/pedido/${encodeURIComponent(clave)}`),
    html: (d) => anRecPedidoHtml(d),
  });
}

/** Entrada desde otras vistas (💸 Fugas, Bitácora): abre el Recorrido y, ya
 *  pintado, la línea de tiempo de ese pedido. */
async function anRecorridoAbrir(clave) {
  if (!clave) return;
  await anSubtab('recorrido');
  anRecAbrirPedido(clave);
  const det = document.getElementById('an-rec-detalle');
  if (det && det.scrollIntoView) det.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function anRecVolverEtapa() {
  if (_AN_REC.etapa === null || _AN_REC.etapa === undefined) anRecCerrarDetalle();
  else anRecEtapa(_AN_REC.etapa, _AN_REC.vista, _AN_REC.pagina);
}

/** Todo lo que el WMS sabe de un pedido. `d` = `/recorrido/pedido/<clave>`. */
function anRecPedidoHtml(d) {
  const p = d.pedido || {};
  const pasos = (d.etapas || []).map(e => {
    const nivel = e.alcanzada ? (e.sin_marca ? 'advertencia' : 'ok') : 'neutro';
    const cuando = e.alcanzada ? (e.en ? anCuando(e.en) : 'sin marca de hora') : 'no llegó';
    return `<div style="flex:1;min-width:92px;padding:6px;border-radius:8px;border:1px solid var(--brd);background:var(--bg-s2);">
      <div style="font-size:var(--fs-xs);font-weight:700;">${e.alcanzada ? '✓' : '○'} ${esc(e.titulo)}</div>
      <div style="font-size:var(--fs-xs);margin-top:2px;">${anRecPildora(nivel, cuando)}</div></div>`;
  }).join('');
  const eventos = (d.eventos || []).map(ev => {
    const doc = ev.documento;
    const docTxt = doc ? ` · ${esc((doc.documentos || []).join(', ') || 'consecutivo sin dato')} ${anRecPildora(doc.resultado === 'ENVIADO' || doc.resultado === 'YA_SALDADA' ? 'ok' : (doc.resultado === 'FALLIDO' ? 'critico' : 'advertencia'), doc.resultado || 'sin resultado')}` : '';
    const color = ev.tipo === 'bitacora' ? 'var(--lila-tx)' : ev.tipo === 'alerta' ? 'var(--warn-tx)' : ev.tipo === 'cola' ? 'var(--info-tx)' : 'var(--tx)';
    return `<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-sm);">
      <div style="min-width:110px;color:var(--tx3);font-size:var(--fs-xs);">${esc(ev.en ? anCuando(ev.en) : 'sin hora')}</div>
      <div style="flex:1;">
        <div style="color:${color};font-weight:600;">${esc(ev.titulo)}${docTxt}</div>
        ${ev.detalle ? `<div style="color:var(--tx2);font-size:var(--fs-xs);">${esc(ev.detalle)}</div>` : ''}
        ${ev.quien ? `<div style="color:var(--tx3);font-size:var(--fs-xs);">por ${esc(ev.quien)}</div>` : ''}
      </div></div>`;
  }).join('') || '<div style="color:var(--tx3);font-size:var(--fs-sm);">Sin eventos registrados.</div>';
  const motivo = p.motivo ? `<div style="font-size:var(--fs-sm);margin-top:4px;">${esc(p.motivo)}</div>` : '';
  const parciales = (p.perdidas_parciales || []).length
    ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:2px;">Pérdidas parciales: ${esc(p.perdidas_parciales.join(', '))}</div>` : '';
  return `<div class="tabla-card">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;flex-wrap:wrap;">
      <div>
        <div class="tabla-titulo" style="margin:0;">Pedido ${esc(p.numero)}${p.co ? ' · CO ' + esc(p.co) : ''}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);">${esc(p.cliente || 'sin cliente')} · valor ${esc(anPesos(p.valor))}${p.valor_motivo ? ' (' + esc(p.valor_motivo) + ')' : ''}
          · facturado ${esc(anPesos(p.valor_facturado))} · cobrado ${esc(anPesos(p.monto_cobrado))}${p.a_credito ? ' · a crédito' : ''}</div>
        <div style="margin-top:4px;">${anRecPildora(_AN_REC_ESTADO[p.estado] || 'neutro', p.estado_titulo)}</div>
        ${motivo}${parciales}
      </div>
      <button onclick="anRecVolverEtapa()" style="background:none;border:1px solid var(--brd);color:var(--tx2);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:var(--fs-xs);">‹ Volver</button>
    </div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0;">${pasos}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:4px;">
      ${esc(anNum(d.eventos ? d.eventos.length : 0))} eventos · ${esc(anNum(d.acciones_bitacora))} acciones de la bitácora · entrada por ${esc(p.entrada_fuente === 'SIESA' ? 'historia de Siesa' : 'primer registro del WMS')}</div>
    ${eventos}
  </div>`;
}
