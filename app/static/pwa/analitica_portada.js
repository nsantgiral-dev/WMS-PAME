/**
 * 📈 Analítica → 🎯 ¿Cómo vamos? — la portada (2026-09-24).
 *
 * La primera sub-pestaña y la que abre por defecto. Contesta en diez segundos
 * si la empresa convierte pedido en caja rápido y sin fugas: seis indicadores,
 * cada uno CONTRA SU META, con su tendencia de 8 semanas, la variación contra
 * el período anterior y su dueño. Al tocar uno se abre su porqué y la lista de
 * qué hacer, con un botón a la pantalla que lo resuelve.
 *
 * Todo número y todo semáforo sale del servidor (`/api/analitica/resumen` →
 * `portada`, que lee `app/services/analitica_portada.py`): acá no se calcula
 * ninguna tasa ni se decide ningún color. La pantalla solo decide cómo se ve y
 * dice lo que no se sabe — «sin dato» en gris con su razón, nunca en rojo.
 *
 * Arriba, UNA línea de confianza con el veredicto de 🩺 Salud del dato
 * (`/api/analitica/salud`, que se pide aparte: tarda más y no debe frenar las
 * cifras). Los botones pasan posiciones, nunca un dato.
 *
 * Depende del shell `analitica.js` (runtime): anCargarPanel, anQuery, anPesos,
 * anNum, anFrescura, anSubtab, anEsAdmin. De `get`/`post`/`alerta`/`tab`
 * (app.js) y `esc` (util.js).
 */

const AN_PORT = { f: null, d: null, abierta: null, salud: null, saludError: null, saludAbierta: false, cargaSalud: 0 };

/** Pantallas a las que la portada puede mandar (lista blanca: el destino viene
 *  del servidor y nunca se pasa a `tab()` sin estar acá). */
const AN_PORT_TABS = ['tab-pedidos', 'tab-inventario', 'tab-liquidacion', 'tab-connekta', 'tab-compras'];

const AN_PORT_NIVEL = {
  verde:    { punto: '●', estilo: 'background:var(--ok-bg);color:var(--ok-tx);border:1px solid var(--ok-brd);', trazo: 'var(--ok-tx)' },
  amarillo: { punto: '●', estilo: 'background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);', trazo: 'var(--warn-tx)' },
  rojo:     { punto: '●', estilo: 'background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd);', trazo: 'var(--err-tx)' },
  sin_dato: { punto: '⚪', estilo: 'background:var(--bg-s2);color:var(--tx2);border:1px solid var(--brd);', trazo: 'var(--tx3)' },
  sin_meta: { punto: '⚪', estilo: 'background:var(--bg-s2);color:var(--tx2);border:1px solid var(--brd);', trazo: 'var(--tx3)' },
};

const AN_PORT_CONFIANZA = {
  CONFIABLE: { nivel: 'verde', texto: 'Puedes decidir con estos números' },
  CON_RESERVAS: { nivel: 'amarillo', texto: 'Úsalos con cuidado: hay datos incompletos' },
  NO_CONFIABLE: { nivel: 'rojo', texto: 'Todavía no decidas con estos números' },
};

/** Entrada desde el shell: `f` = `{almacen_id, desde, hasta}`. */
function anPortadaCargar(el, f) {
  AN_PORT.f = f;
  AN_PORT.abierta = null;
  const params = anQuery(f);
  anPortPedirSalud(params);
  return anCargarPanel({
    el, clave: 'portada', params,
    pedir: () => get('/api/analitica/resumen?' + params),
    html: (d) => { AN_PORT.d = d; return anPortHtml(d); },
    error: (e) => `<div style="padding:16px;color:var(--err-tx);font-size:var(--fs-sm);">No se pudo calcular la portada: ${esc(e && e.message ? e.message : e)}</div>`,
  });
}

/** La confianza llega aparte: la auditoría tarda y no debe frenar las cifras. */
async function anPortPedirSalud(params) {
  const n = ++AN_PORT.cargaSalud;
  AN_PORT.saludError = null;
  try {
    const s = await get('/api/analitica/salud?' + params);
    if (n !== AN_PORT.cargaSalud) return;
    AN_PORT.salud = s;
  } catch (e) {
    if (n !== AN_PORT.cargaSalud) return;
    AN_PORT.salud = null;
    AN_PORT.saludError = (e && e.message) || 'error';
  }
  const el = document.getElementById('an-port-confianza');
  if (el) el.innerHTML = anPortConfianzaHtml();
}

function _anPortRepintar() {
  const el = document.getElementById('an-panel-portada');
  if (el && AN_PORT.d) el.innerHTML = anPortHtml(AN_PORT.d);
}

// ── Formatos ────────────────────────────────────────────────────────────────

/** El valor grande de una tarjeta, en su unidad. `null` → «Sin dato». */
function anPortValor(t) {
  const v = t.periodo ? t.periodo.valor : null;
  if (v === null || v === undefined) return 'Sin dato';
  return anPortFormato(t.unidad, v);
}

function anPortFormato(unidad, v) {
  if (v === null || v === undefined) return 'sin dato';
  if (unidad === 'proporcion') return `${anNum(Math.round(Number(v) * 1000) / 10)} %`;
  if (unidad === 'pesos') return anPesos(v);
  if (unidad === 'dias') {
    const d = Number(v);
    return d < 1 ? `${anNum(Math.round(d * 24))} h` : `${anNum(Math.round(d * 10) / 10)} días`;
  }
  return anNum(v);
}

/** «≥ 95 %», «≤ 2 días», «$0». */
function anPortMetaTexto(t, sem) {
  const s = sem || t.semaforo || {};
  if (s.meta === null || s.meta === undefined) return 'sin meta';
  const signo = t.direccion === 'SUBE_ES_BUENO' ? '≥' : '≤';
  const meta = anPortFormato(t.unidad, s.meta);
  return t.unidad === 'pesos' && Number(s.meta) === 0 ? meta : `${signo} ${meta}`;
}

function anPortPildora(nivel, texto) {
  const n = AN_PORT_NIVEL[nivel] || AN_PORT_NIVEL.sin_dato;
  const estilo = n.estilo;   // marcado propio (tokens), no dato
  return `<span style="${estilo}display:inline-block;padding:2px 8px;border-radius:12px;font-size:var(--fs-xs);font-weight:700;white-space:nowrap;">${esc(n.punto + ' ' + texto)}</span>`;
}

/**
 * La frase de gerencia de cada indicador: el número dicho como se diría en una
 * reunión («de cada $100 que cerraron, $27 llegaron completos a caja»). Los
 * detalles técnicos (n, p90, sin marca) no van acá: van en el `title` y en el
 * porqué.
 */
function anPortFrase(t) {
  const p = t.periodo || {};
  const v = p.valor;
  if (v === null || v === undefined) return anPortRazonSinDato(t);
  const n = p.n;
  switch (t.clave) {
    case 'llega_a_caja':
      return `De cada $100 que cerraron, $${anNum(Math.round(v * 100))} llegaron completos a caja (${anNum(n)} pedidos).`;
    case 'ciclo_caja':
      return `Un pedido típico tarda ${anPortFormato('dias', v)} de aprobado a caja liquidada (${anNum(n)} pedidos).`;
    case 'plata_en_riesgo':
      return Number(v) === 0 && !p.es_piso
        ? 'No hay plata cobrada o entregada esperando llegar a caja.'
        : `${anPesos(v)}${p.es_piso ? ' al menos' : ''} cobrados o entregados que todavía no llegan a caja (${anNum(n)} casos).`;
    case 'fill_rate':
      return `De cada 100 unidades pedidas, se despacharon ${anNum(Math.round(v * 100))}.`;
    case 'venta_perdida':
      return Number(v) === 0 && !p.es_piso
        ? 'No se dejó de vender nada por agotados.'
        : `Se dejaron de vender${p.es_piso ? ' al menos' : ''} ${anPesos(v)} por agotados (${anNum(n)} agotados).`;
    case 'exactitud_inventario':
      return `De cada 100 conteos, ${anNum(Math.round(v * 100))} cuadraron exacto con Siesa.`;
    default:
      return '';
  }
}

/** Por qué no hay número, en palabras. */
function anPortRazonSinDato(t) {
  const p = t.periodo || {};
  if (p.sin_calcular) {
    return 'Sale del cálculo diario, que no ha corrido para estos días (ver el aviso de arriba).';
  }
  const m = p.motivo || 'no hay con qué medir';
  return 'Sin dato: ' + m.charAt(0).toLowerCase() + m.slice(1) + '.';
}

/** El detalle técnico que no va en la frase: va en el `title` y en el porqué. */
function anPortTecnico(t) {
  const p = t.periodo || {};
  const partes = [];
  if (p.n !== null && p.n !== undefined) partes.push(`n = ${anNum(p.n)}`);
  if (p.p90 !== null && p.p90 !== undefined) partes.push(`p90 ${anPortFormato('dias', p.p90)}`);
  if (p.sin_marca) partes.push(`${anNum(p.sin_marca)} sin marca de aprobación`);
  if (p.dias_sin_medir) partes.push(`${anNum(p.dias_sin_medir)} de ${anNum(p.dias)} días sin medir`);
  if (p.motivo && p.valor !== null && p.valor !== undefined) partes.push(p.motivo);
  partes.push(t.origen === 'en_vivo' ? 'calculado al abrir la portada' : 'del cálculo diario guardado');
  return partes.join(' · ');
}

/** «▲ 3 pts vs. período anterior», con color solo si la comparación vale. */
function anPortVariacion(t) {
  const v = t.variacion || {};
  if (v.absoluta === null || v.absoluta === undefined) {
    return `<span style="color:var(--tx3);">⚪ sin período anterior con qué comparar</span>`;
  }
  const a = Number(v.absoluta);
  if (!a) return `<span style="color:var(--tx3);">＝ igual que el período anterior</span>`;
  const flecha = a > 0 ? '▲' : '▼';
  let cuanto;
  if (t.unidad === 'proporcion') cuanto = `${anNum(Math.round(Math.abs(a) * 1000) / 10)} pts`;
  else cuanto = anPortFormato(t.unidad, Math.abs(a));
  const color = !v.comparable ? 'var(--tx2)' : (v.favorable ? 'var(--ok-tx)' : 'var(--err-tx)');
  const aviso = v.comparable ? '' : ' (con datos incompletos)';
  return `<span style="color:${color};" title="${esc(v.motivo || '')}">${esc(flecha + ' ' + cuanto)} vs. período anterior${esc(aviso)}</span>`;
}

// ── La tendencia: sparkline SVG, sin librerías ──────────────────────────────

/**
 * 8 semanas en un SVG chico. Los puntos llevan el color de su semáforo; una
 * semana sin dato es un hueco (la línea se corta) con una marca gris abajo,
 * nunca un cero. La línea punteada es la meta. Colores de tokens.
 */
function anPortSparkline(t) {
  const puntos = t.tendencia || [];
  const W = 112, H = 32, PAD = 4;
  const vals = puntos.map(p => (p.valor === null || p.valor === undefined) ? null : Number(p.valor));
  const conDato = vals.filter(v => v !== null);
  const metaSem = t.semaforo && t.semaforo.meta !== null && t.semaforo.meta !== undefined
    ? Number(t.semaforo.meta) * (t.meta_por_dia ? 7 / Math.max(1, Number((AN_PORT.d && AN_PORT.d.portada && AN_PORT.d.portada.periodo.dias) || 1)) : 1)
    : null;
  const dominio = conDato.concat(metaSem === null ? [] : [metaSem]);
  const lo = dominio.length ? Math.min(...dominio) : 0;
  const hi = dominio.length ? Math.max(...dominio) : 1;
  const rango = hi - lo || 1;
  const x = (i) => PAD + (W - 2 * PAD) * (puntos.length > 1 ? i / (puntos.length - 1) : 0.5);
  const y = (v) => H - PAD - (H - 2 * PAD) * ((v - lo) / rango);
  let tramos = '', actual = [];
  vals.forEach((v, i) => {
    if (v === null) { if (actual.length > 1) tramos += `<polyline points="${actual.join(' ')}" style="fill:none;stroke:var(--tx2);stroke-width:1.5;"/>`; actual = []; return; }
    actual.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  });
  if (actual.length > 1) tramos += `<polyline points="${actual.join(' ')}" style="fill:none;stroke:var(--tx2);stroke-width:1.5;"/>`;
  const marcas = vals.map((v, i) => {
    if (v === null) return `<line x1="${x(i).toFixed(1)}" y1="${H - 2}" x2="${x(i).toFixed(1)}" y2="${H - 5}" style="stroke:var(--tx3);stroke-width:1.5;"/>`;
    const nivel = puntos[i].semaforo;
    const trazo = (AN_PORT_NIVEL[nivel] || AN_PORT_NIVEL.sin_dato).trazo;
    return `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="${i === vals.length - 1 ? 3 : 2}" style="fill:${trazo};"/>`;
  }).join('');
  const meta = metaSem === null ? '' :
    `<line x1="${PAD}" y1="${y(metaSem).toFixed(1)}" x2="${W - PAD}" y2="${y(metaSem).toFixed(1)}" style="stroke:var(--tx3);stroke-width:1;stroke-dasharray:3 3;"/>`;
  const resumen = puntos.map(p => `${p.desde}→${p.hasta}: ${p.valor === null || p.valor === undefined ? 'sin dato' : anPortFormato(t.unidad, p.valor)}`).join('\n');
  return `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${esc('Tendencia de 8 semanas de ' + t.nombre)}" style="flex:0 0 auto;display:block;"><title>${esc('Últimas 8 semanas (7 días cada una)\n' + resumen)}</title>${meta}${tramos}${marcas}</svg>`;
}

// ── La portada ──────────────────────────────────────────────────────────────

function anPortHtml(d) {
  const p = (d && d.portada) || {};
  const tarjetas = p.tarjetas || [];
  const c = p.conteo || {};
  const partes = [];
  if (c.verde) partes.push(anPortPildora('verde', `${anNum(c.verde)} en meta`));
  if (c.amarillo) partes.push(anPortPildora('amarillo', `${anNum(c.amarillo)} cerca`));
  if (c.rojo) partes.push(anPortPildora('rojo', `${anNum(c.rojo)} fuera de meta`));
  if (c.sin_dato) partes.push(anPortPildora('sin_dato', `${anNum(c.sin_dato)} sin dato`));
  const periodo = p.periodo || {};
  const alm = anPortAlmacen();

  const grid = [];
  tarjetas.forEach((t, i) => {
    grid.push(anPortTarjeta(t, i));
    if (AN_PORT.abierta === i) grid.push(`<div id="an-port-detalle" style="grid-column:1/-1;">${anPortDetalleHtml(t, i)}</div>`);
  });

  const hayPiso = tarjetas.some(t => t.periodo && t.periodo.es_piso);
  const hayProvisional = tarjetas.some(t => t.meta_provisional);
  const como = p.como_se_mide || {};
  // «piso» y «metas» ya se dijeron una vez al pie: no se repiten adentro.
  const comoHtml = Object.keys(como).filter(k => k !== 'piso' && k !== 'metas')
    .map(k => `<div style="margin:4px 0;">${esc(como[k])}</div>`).join('');

  return `<div class="an-portada" style="display:flex;flex-direction:column;gap:12px;min-width:0;">
    <div id="an-port-confianza">${anPortConfianzaHtml()}</div>
    <div>
      <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);">¿Cómo vamos?</div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);">Del pedido aprobado a la plata en caja, contra la meta · del ${esc(periodo.desde || '')} al ${esc(periodo.hasta || '')} · ${esc(alm)}</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px;">${partes.join('')}</div>
    </div>
    ${anPortAvisoKpi(p)}
    <div class="an-port-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,270px),1fr));gap:10px;">${grid.join('')}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);display:flex;flex-direction:column;gap:2px;">
      ${hayPiso ? `<div>«al menos»: ${esc(como.piso ? como.piso.replace(/^«al menos»:\s*/, '') : 'hay casos sin valor que no suman.')}</div>` : ''}
      ${hayProvisional ? '<div>Metas provisionales: las propuso el equipo de analítica y la gerencia todavía no las confirma.</div>' : ''}
      <div>La línea punteada de cada tendencia es la meta; cada punto son 7 días; un hueco es una semana sin dato, no un cero.</div>
    </div>
    <details class="tabla-card" style="font-size:var(--fs-xs);color:var(--tx2);">
      <summary style="cursor:pointer;font-size:var(--fs-sm);color:var(--tx);">Cómo se mide la portada</summary>${comoHtml}</details>
  </div>`;
}

function anPortAlmacen() {
  const sel = document.getElementById('an-f-almacen');
  if (!sel || !sel.value) return 'todos los almacenes';
  const op = sel.options && sel.selectedIndex >= 0 ? sel.options[sel.selectedIndex] : null;
  return op && op.text ? op.text : 'almacén ' + sel.value;
}

/** La línea de confianza: el veredicto global de Salud, en palabras. */
function anPortConfianzaHtml() {
  const s = AN_PORT.salud;
  const base = 'display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;padding:8px 12px;border-radius:10px;font-size:var(--fs-sm);';
  if (!s) {
    const texto = AN_PORT.saludError
      ? `No se pudo revisar la confianza del dato (${esc(AN_PORT.saludError)}).`
      : 'Revisando si los datos de hoy son confiables…';
    return `<div style="${base}background:var(--bg-s2);color:var(--tx2);border:1px solid var(--brd);">${texto}</div>`;
  }
  const g = s.global || {};
  const conf = AN_PORT_CONFIANZA[g.veredicto] || { nivel: 'sin_dato', texto: 'Sin veredicto de confianza' };
  const n = AN_PORT_NIVEL[conf.nivel];
  const razones = g.razones || [];
  const graves = razones.filter(r => r.nivel === 'critico').length;
  const avisos = razones.length - graves;
  const cuenta = [graves ? `${anNum(graves)} problema(s) grave(s)` : '', avisos ? `${anNum(avisos)} aviso(s)` : '']
    .filter(Boolean).join(' y ');
  const boton = razones.length
    ? `<button onclick="anPortVerConfianza()" style="background:none;border:none;color:inherit;font-weight:700;cursor:pointer;font-size:var(--fs-sm);padding:4px 0;">${AN_PORT.saludAbierta ? 'Ocultar ‹' : 'Ver por qué ›'}</button>` : '';
  const lista = AN_PORT.saludAbierta && razones.length
    ? `<div style="flex-basis:100%;display:flex;flex-direction:column;gap:3px;margin-top:4px;">${razones.map(r =>
      `<div style="font-size:var(--fs-xs);">${r.nivel === 'critico' ? '●' : '○'} ${esc(r.texto)}</div>`).join('')}</div>` : '';
  const estilo = n.estilo;   // marcado propio (tokens), no dato
  return `<div style="${base}${estilo}">
    <span><b>${esc(n.punto)} Confianza del dato:</b> ${esc(conf.texto)}${cuenta ? ' · ' + esc(cuenta) : ''}</span>${boton}${lista}</div>`;
}

/** «Ver por qué ›»: el admin va al Diagnóstico completo; los demás lo ven acá. */
function anPortVerConfianza() {
  if (anEsAdmin()) { anSubtab('diagnostico'); return; }
  AN_PORT.saludAbierta = !AN_PORT.saludAbierta;
  const el = document.getElementById('an-port-confianza');
  if (el) el.innerHTML = anPortConfianzaHtml();
}

/** El KPI diario apagado se dice UNA vez, con cómo se enciende (admin). */
function anPortAvisoKpi(p) {
  const k = p.kpi_diario || {};
  const tarjetas = p.tarjetas || [];
  const afectadas = tarjetas.filter(t => (k.dependen || []).includes(t.clave) && t.periodo && t.periodo.sin_calcular);
  if (!afectadas.length) return '';
  const nombres = afectadas.map(t => t.nombre).join(' y ');
  const estado = k.encendido ? 'no ha corrido para estos días' : 'está apagado';
  const admin = anEsAdmin()
    ? `<div style="margin-top:4px;">${esc(k.como_encender || '')}</div>
       <button onclick="anPortRecalcular()" style="margin-top:6px;padding:6px 12px;border-radius:6px;border:1px solid var(--acento-brd);background:var(--acento-bg);color:var(--acento-tx);font-size:var(--fs-sm);font-weight:600;cursor:pointer;">Calcular ahora los últimos ${esc(anNum(anPortDiasRecalculo(p)))} días</button>`
    : '<div style="margin-top:4px;">Pídele al administrador que lo encienda.</div>';
  return `<div style="padding:10px 12px;border-radius:10px;background:var(--info-bg);color:var(--info-tx);border:1px solid var(--info-brd);font-size:var(--fs-sm);">
    <b>${esc(nombres)}</b> salen del cálculo diario de indicadores, que ${esc(estado)}. Por eso salen en gris: no es que estén mal, es que no hay con qué medir.${admin}</div>`;
}

function anPortDiasRecalculo(p) {
  const dias = (p.periodo && p.periodo.dias) || 30;
  return Math.max(1, Math.min(dias, (p.kpi_diario && p.kpi_diario.tope_dias_recalculo) || 31));
}

/** Días por llamada al recálculo: cada día son ~15 métricas × almacenes, y
 *  `post()` corta a los 25 s. En tandas chicas, cada una termina a tiempo y
 *  el recálculo es idempotente (una tanda repetida no duplica nada). */
const AN_PORT_TANDA_RECALCULO = 5;

/** Admin: calcula a mano el KPI diario de los días del período (hasta ayer). */
async function anPortRecalcular() {
  const p = (AN_PORT.d && AN_PORT.d.portada) || {};
  const dias = anPortDiasRecalculo(p);
  const hasta = hoyBogota(null, -1);
  const desde = hoyBogota(null, -dias);
  if (typeof confirm === 'function' && !confirm(`¿Calcular el KPI diario del ${desde} al ${hasta}? Puede tardar un par de minutos.`)) return;
  const almacen = (AN_PORT.f && AN_PORT.f.almacen_id) || null;
  try {
    for (let fin = -1; fin >= -dias; fin -= AN_PORT_TANDA_RECALCULO) {
      const ini = Math.max(-dias, fin - AN_PORT_TANDA_RECALCULO + 1);
      if (typeof alerta === 'function') alerta(`Calculando del ${hoyBogota(null, ini)} al ${hoyBogota(null, fin)}…`, 'info');
      const r = await post('/api/analitica/kpi/recalcular', { desde: hoyBogota(null, ini), hasta: hoyBogota(null, fin), almacen_id: almacen });
      if (r && r.error) throw new Error(r.error);
    }
    if (typeof alerta === 'function') alerta('KPI diario calculado. Actualizando la portada…', 'ok');
    anActualizar();
  } catch (e) {
    if (typeof alerta === 'function') alerta('No se pudo calcular: ' + ((e && e.message) || e), 'error');
  }
}

/** Una tarjeta: valor, semáforo contra la meta, frase, tendencia, variación, dueño. */
function anPortTarjeta(t, i) {
  const sem = t.semaforo || {};
  const nivel = sem.nivel || 'sin_dato';
  const sinDato = !t.periodo || t.periodo.valor === null || t.periodo.valor === undefined;
  const piso = t.periodo && t.periodo.es_piso && !sinDato;
  const abierta = AN_PORT.abierta === i;
  const borde = (AN_PORT_NIVEL[nivel] || AN_PORT_NIVEL.sin_dato).trazo;
  const valor = sinDato
    ? `<span style="color:var(--tx3);">Sin dato</span>`
    : `${esc(anPortValor(t))}${piso ? '<span style="font-size:var(--fs-sm);font-weight:400;color:var(--tx3);"> al menos</span>' : ''}`;
  const meta = `Meta ${esc(anPortMetaTexto(t))}${t.meta_provisional ? ' · provisional' : ''}`;
  return `<div class="an-port-tarjeta" data-clave="${esc(t.clave)}" style="min-width:0;background:var(--bg-s);border:1px solid var(--brd);border-left:4px solid ${borde};border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:6px;">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">
      <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(t.nombre)}</div>
      ${anPortPildora(nivel, sem.texto || 'Sin dato')}
    </div>
    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;align-items:flex-end;">
      <div style="font-size:var(--fs-2xl);font-weight:800;color:var(--tx);white-space:nowrap;line-height:1.1;">${valor}</div>
      ${anPortSparkline(t)}
    </div>
    <div style="font-size:var(--fs-sm);color:${sinDato ? 'var(--tx3)' : 'var(--tx2)'};" title="${esc(anPortTecnico(t))}">${esc(anPortFrase(t))}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);">${meta} · ${anPortVariacion(t)}</div>
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;font-size:var(--fs-xs);color:var(--tx3);">
      <span>Dueño: ${esc(t.dueno)}</span>
      <button onclick="anPortAbrir(${i})" style="background:none;border:1px solid var(--brd);color:var(--tx);border-radius:6px;padding:6px 10px;cursor:pointer;font-size:var(--fs-xs);font-weight:600;">${abierta ? 'Cerrar ‹' : 'Por qué y qué hacer ›'}</button>
    </div>
  </div>`;
}

function anPortAbrir(i) {
  AN_PORT.abierta = AN_PORT.abierta === Number(i) ? null : Number(i);
  _anPortRepintar();
  const det = document.getElementById('an-port-detalle');
  if (det && det.scrollIntoView) det.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

// ── El porqué y el qué hacer ────────────────────────────────────────────────

function anPortDetalleHtml(t, i) {
  const pq = t.por_que || {};
  let porque = '';
  if (t.clave === 'llega_a_caja') porque = anPortPorQueLlega(pq);
  else if (t.clave === 'ciclo_caja') porque = anPortPorQueCiclo(pq);
  else if (t.clave === 'plata_en_riesgo') porque = anPortPorQuePlata(t);
  else if (t.clave === 'venta_perdida') porque = anPortPorQueVenta(pq);
  else porque = anPortPorQueKpi(t);
  const acciones = anPortAcciones(t, i);
  return `<div class="tabla-card" style="margin:0;border-left:4px solid var(--acento-brd);">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">
      <div style="font-size:var(--fs-md);font-weight:700;color:var(--tx);">${esc(t.nombre)}: por qué y qué hacer</div>
      <button onclick="anPortAbrir(${i})" style="background:none;border:1px solid var(--brd);color:var(--tx2);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:var(--fs-xs);">Cerrar</button>
    </div>
    <div style="font-size:var(--fs-sm);color:var(--tx2);margin:4px 0 8px;">${esc(t.mide)}</div>
    ${pq.error ? `<div style="font-size:var(--fs-sm);color:var(--warn-tx);">No se pudo armar el porqué: ${esc(pq.error)}</div>` : ''}
    ${porque}
    ${acciones}
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:8px;">Detalle técnico: ${esc(anPortTecnico(t))} · fuente: ${esc(t.fuente)}</div>
  </div>`;
}

function _anPortFila(izq, der, sub) {
  return `<div style="display:flex;justify-content:space-between;gap:8px;padding:5px 0;border-bottom:1px solid var(--brd);font-size:var(--fs-sm);">
    <div style="min-width:0;overflow-wrap:anywhere;"><div style="color:var(--tx);">${izq}</div>${sub ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">${sub}</div>` : ''}</div>
    <div style="white-space:nowrap;color:var(--tx2);text-align:right;">${der}</div></div>`;
}

function _anPortTitulo(texto) {
  return `<div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);margin:10px 0 4px;">${esc(texto)}</div>`;
}

function anPortPorQueLlega(pq) {
  const filas = (pq.perdidas || []).map(g => _anPortFila(
    esc(g.motivo || 'sin motivo'),
    `${esc(anNum(g.pedidos))} pedido(s) · ${g.sin_valor && !g.valor ? 'sin valor' : esc(anPesos(g.valor))}`,
    `${g.tipo === 'cayo' ? 'Se cayó en' : 'Perdió una parte en'}: ${esc(g.etapa || 'etapa sin dato')}`)).join('');
  return _anPortTitulo('Dónde se pierde la plata') + (filas || '<div style="font-size:var(--fs-sm);color:var(--tx3);">Ningún pedido del período tuvo pérdidas.</div>');
}

function anPortPorQueCiclo(pq) {
  const tramos = pq.tramos || [];
  const max = Math.max(1, ...tramos.map(x => Number(x.mediana_horas) || 0));
  const lento = pq.tramo_mas_lento;
  const filas = tramos.map(x => {
    const ancho = x.mediana_horas === null || x.mediana_horas === undefined ? 0 : Math.max(2, Math.round(100 * Number(x.mediana_horas) / max));
    const esLento = lento && lento.hasta === x.hasta;
    return `<div style="padding:4px 0;font-size:var(--fs-sm);">
      <div style="display:flex;justify-content:space-between;gap:8px;"><span style="color:var(--tx);${esLento ? 'font-weight:700;' : ''}">${esc(x.desde)} → ${esc(x.hasta)}${esLento ? ' · el más lento' : ''}</span>
      <span style="color:var(--tx2);white-space:nowrap;">${x.mediana_horas === null || x.mediana_horas === undefined ? 'sin medición' : esc(anPortFormato('dias', Number(x.mediana_horas) / 24))}</span></div>
      <div style="height:8px;background:var(--bg-s2);border-radius:4px;overflow:hidden;"><div style="height:100%;width:${ancho}%;background:${esLento ? 'var(--warn-tx)' : 'var(--pm-fill)'};"></div></div></div>`;
  }).join('');
  return _anPortTitulo('Cuánto tarda cada tramo (mediana)') + (filas || '<div style="font-size:var(--fs-sm);color:var(--tx3);">Sin tramos medidos.</div>');
}

function anPortPorQuePlata(t) {
  const comp = (t.periodo && t.periodo.componentes) || [];
  const filas = comp.map(c => _anPortFila(esc(c.titulo),
    c.sin_dato ? 'sin dato' : `${c.pesos === null || c.pesos === undefined ? 'sin valor' : esc(anPesos(c.pesos))} · ${esc(anNum(c.casos))} caso(s)`,
    c.sin_valor ? `${esc(anNum(c.sin_valor))} sin valor: no suman` : '')).join('');
  return _anPortTitulo('De dónde sale') + filas;
}

function anPortPorQueVenta(pq) {
  const filas = (pq.categorias || []).map(g => _anPortFila(esc(g.motivo || 'Sin categoría'),
    g.casos === g.sin_valor ? `sin precio · ${esc(anNum(g.casos))}` : `${esc(anPesos(g.pesos))} · ${esc(anNum(g.casos))}`,
    g.sin_valor && g.casos !== g.sin_valor ? `${esc(anNum(g.sin_valor))} sin precio: no suman` : '')).join('');
  return _anPortTitulo('Por categoría') + (filas || '<div style="font-size:var(--fs-sm);color:var(--tx3);">Ningún agotado en el período.</div>');
}

function anPortPorQueKpi(t) {
  const p = t.periodo || {};
  if (p.valor === null || p.valor === undefined) {
    return `<div style="font-size:var(--fs-sm);color:var(--tx2);">${esc(anPortRazonSinDato(t))}</div>`;
  }
  const num = p.numerador, den = p.denominador;
  return `<div style="font-size:var(--fs-sm);color:var(--tx2);">${num !== null && num !== undefined && den ? esc(`${anNum(num)} de ${anNum(den)}`) : ''}${p.dias_sin_medir ? esc(` · ${anNum(p.dias_sin_medir)} día(s) sin medir`) : ''}</div>`;
}

/** La lista de qué hacer: cada fila con su botón. Botón = posición, nunca dato. */
function anPortAcciones(t, i) {
  const lista = (t.por_que && t.por_que.que_hacer) || [];
  if (!lista.length) return '';
  const grupos = {};
  lista.forEach((a, k) => {
    const g = a.grupo === 'esperando' ? 'Los que más llevan esperando'
      : a.grupo === 'lentos' ? 'Los que más tardaron en llegar a caja' : 'Qué hacer';
    (grupos[g] = grupos[g] || []).push([a, k]);
  });
  const total = t.por_que && t.por_que.total_acciones;
  return Object.keys(grupos).map(g => _anPortTitulo(g) + grupos[g].map(([a, k]) => {
    const boton = anPortBotonAccion(a) ? `<button onclick="anPortAccion(${i},${k})" style="padding:6px 10px;border-radius:6px;border:1px solid var(--acento-brd);background:var(--acento-bg);color:var(--acento-tx);font-size:var(--fs-xs);font-weight:600;cursor:pointer;white-space:nowrap;">${esc(anPortBotonAccion(a))}</button>` : '';
    const monto = a.pesos !== undefined ? (a.pesos === null ? 'sin valor' : anPesos(a.pesos))
      : (a.valor !== undefined ? (a.valor === null ? 'sin valor' : anPesos(a.valor)) : '');
    const titulo = (a.numero ? `Pedido ${a.numero}` : (a.titulo || '')) + (a.cliente ? ' · ' + a.cliente : '');
    const sub = [a.fuente, a.estado, a.etapa, a.motivo, a.detalle, anPortCuanto(a)].filter(Boolean).join(' · ');
    return `<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--brd);">
      <div style="min-width:0;flex:1 1 140px;overflow-wrap:anywhere;font-size:var(--fs-sm);"><div style="color:var(--tx);">${esc(titulo)}</div>
        ${sub ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(sub)}</div>` : ''}</div>
      <div style="display:flex;flex-wrap:wrap;justify-content:flex-end;gap:8px;align-items:center;flex:0 1 auto;"><span style="font-size:var(--fs-sm);color:var(--tx2);white-space:nowrap;">${esc(monto)}</span>${boton}</div></div>`;
  }).join('')).join('') + (total && total > lista.length ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">Se muestran ${esc(anNum(lista.length))} de ${esc(anNum(total))}: el resto está en 💸 Fugas.</div>` : '');
}

/** El tiempo de una fila de qué hacer, dicho según qué mide: lo que un pedido
 *  lleva esperando, lo que tardó en llegar a caja, o hace cuánto pasó. */
function anPortCuanto(a) {
  if (a.dias === null || a.dias === undefined) return '';
  const d = Number(a.dias);
  const t = anPortFormato('dias', d);
  if (a.grupo === 'esperando') return `lleva ${t}`;
  if (a.grupo === 'lentos') return `tardó ${t}`;
  return d < 1 ? 'hoy' : `hace ${t}`;
}

function anPortBotonAccion(a) {
  switch (a.tipo) {
    case 'ver_pedido': return 'Ver pedido ›';
    case 'liquidar_ruta': return anPortPuedeAbrir('tab-liquidacion') ? 'Liquidar ruta ›' : '';
    case 'reintentar': return anPortPuedeAbrir(a.destino === 'liquidacion' ? 'tab-liquidacion' : 'tab-connekta') ? 'Ir a reintentar ›' : '';
    case 'ver_fuga': return 'Ver en Fugas ›';
    case 'ver_bandeja': return anPortPuedeAbrir('tab-compras') ? 'Ver en la Bandeja de compras ›' : '';
    case 'ir_tab': return anPortPuedeAbrir(a.tab) ? 'Ir ›' : '';
    default: return '';
  }
}

/** ¿Este usuario ve esa pestaña? (el nav la esconde por rol). */
function anPortPuedeAbrir(tabId) {
  if (!AN_PORT_TABS.includes(tabId)) return false;
  if (typeof document === 'undefined' || !document.querySelector) return true;
  const nav = document.querySelector(`.nav-tab[onclick*="'${tabId}'"]`);
  return !nav || nav.style.display !== 'none';
}

/** Ejecuta la acción `k` de la tarjeta `i` (la busca en memoria: el onclick
 *  solo lleva posiciones). */
async function anPortAccion(i, k) {
  const t = AN_PORT.d && AN_PORT.d.portada && AN_PORT.d.portada.tarjetas[Number(i)];
  const a = t && t.por_que && (t.por_que.que_hacer || [])[Number(k)];
  if (!a) return;
  if (a.tipo === 'ver_pedido' && a.pedido_clave) {
    await anRecorridoAbrir(a.pedido_clave);
  } else if (a.tipo === 'liquidar_ruta' && a.ruta_id && anPortPuedeAbrir('tab-liquidacion')) {
    await tab('tab-liquidacion');
    liqAbrirRuta(Number(a.ruta_id));
  } else if (a.tipo === 'reintentar') {
    if (a.destino === 'liquidacion' && anPortPuedeAbrir('tab-liquidacion')) {
      await tab('tab-liquidacion');
      liqSubtab('jobs');
    } else if (anPortPuedeAbrir('tab-connekta')) {
      await tab('tab-connekta');
    }
  } else if (a.tipo === 'ver_fuga') {
    await anSubtab('fugas');
    const fugas = (AN_FUGAS.datos && AN_FUGAS.datos.fugas) || [];
    const idx = fugas.findIndex(x => x.clave === a.fuga);
    if (idx >= 0) anFugasAbrir(idx);
  } else if (a.tipo === 'ver_bandeja' && a.referencia && anPortPuedeAbrir('tab-compras')) {
    comprasIrABandeja(a.referencia);   // marca la fila; la pestaña la abre
    await tab('tab-compras');
  } else if (a.tipo === 'ir_tab' && anPortPuedeAbrir(a.tab)) {
    await tab(a.tab);
  }
}
