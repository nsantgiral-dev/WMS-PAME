/**
 * Tablero BI — consume /api/dashboard/bi/*. Sección dentro de Dashboard
 * (no un tab aparte), se refresca junto con el resto del dashboard.
 *
 * Métricas 1 (pedidos despachados), 3 (pendientes/fill rate) y 5 (venta
 * perdida $). Fijo sobre ALMACEN_ID (NB1, igual que el resto del dashboard
 * admin — no hay selector de bodega en ninguna pantalla de este panel).
 *
 * Alcance explícito de esta entrega: sin comparativo de período anterior,
 * sin export — sí hay tendencia por día (Chart.js, mismo patrón que
 * graficaTendencia() en app.js) usando `por_dia` de cada endpoint.
 */

let _BI_SUBTAB = 'despachados';
let _BI_DET_PAGE = 1;
let BI_CHART = null;

const BI_SUBTABS = [
  { key: 'despachados', label: 'Despachados' },
  { key: 'pendientes', label: 'Pendientes' },
  { key: 'venta_perdida', label: 'Venta perdida $' },
];

function biSubtab(key) {
  _BI_SUBTAB = key;
  _BI_DET_PAGE = 1;
  cargarBI();
}

function biFiltrar() {
  _BI_DET_PAGE = 1;
  cargarBI();
}

function biCambiarPagina(page) {
  _BI_DET_PAGE = page;
  cargarBI();
}

function biRenderSubtabs() {
  const el = document.getElementById('bi-tabs');
  if (!el) return;
  el.innerHTML = BI_SUBTABS.map(s =>
    `<div class="subtab${s.key === _BI_SUBTAB ? ' active' : ''}" onclick="biSubtab('${esc(s.key)}')">${esc(s.label)}</div>`
  ).join('');
}

/** Día operativo de Bogotá (`YYYY-MM-DD`): `toISOString()` es el día UTC y
 *  después de las 7 p. m. el tablero abría en «mañana» (Regla 5). */
function biHoyBogota(fecha) {
  const d = fecha || new Date();
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Bogota',
      year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
  } catch (_) {
    return new Date(d.getTime() - 5 * 3600 * 1000).toISOString().slice(0, 10);
  }
}

/** Lee bi-fecha-desde/hasta, inicializándolos a "hoy" (Bogotá) si están vacíos. */
function biRangoFechas() {
  const hoy = biHoyBogota();
  const desdeEl = document.getElementById('bi-fecha-desde');
  const hastaEl = document.getElementById('bi-fecha-hasta');
  if (desdeEl && !desdeEl.value) desdeEl.value = hoy;
  if (hastaEl && !hastaEl.value) hastaEl.value = hoy;
  return { desde: desdeEl?.value || hoy, hasta: hastaEl?.value || hoy };
}

function biMoneda(v) {
  if (v === null || v === undefined) return '—';
  return '$' + Number(v).toLocaleString('es-CO', { maximumFractionDigits: 0 });
}

/** Hora Bogotá de un instante del servidor. Las columnas son UTC **sin zona**
 *  (`2026-09-24T22:10:00`): `new Date` las leía como hora local y el tablero
 *  corría la hora cinco horas. Sin designador de zona, se lee como UTC. */
function biFechaHora(iso) {
  if (!iso) return '—';
  const texto = String(iso);
  const conZona = /(Z|[+-]\d{2}:?\d{2})$/.test(texto) ? texto : texto + 'Z';
  const d = new Date(conZona);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString('es-CO', { day: '2-digit', month: '2-digit', hour: '2-digit',
    minute: '2-digit', timeZone: 'America/Bogota' });
}

function biUltimaActualizacion() {
  const ahora = new Date().toLocaleTimeString('es-CO', { hour: '2-digit', minute: '2-digit' });
  return `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">Última actualización: ${ahora} · Fuente: WMS</div>`;
}

function biBadges(obj) {
  const entradas = Object.entries(obj || {});
  if (!entradas.length) return '';
  return `<div style="display:flex;gap:6px;flex-wrap:wrap;margin:10px 0;">` +
    entradas.map(([k, v]) => `<span class="badge badge-blue">${esc(k)}: ${esc(v)}</span>`).join('') +
    `</div>`;
}

function biPaginacionHTML(total, page, perPage) {
  const totalPag = Math.max(1, Math.ceil(total / perPage));
  if (totalPag <= 1) return '';
  return `
    <div style="display:flex;justify-content:space-between;align-items:center;width:100%;">
      <button onclick="biCambiarPagina(${page - 1})" ${page <= 1 ? 'disabled' : ''}
        style="padding:8px 14px;background:var(--bg-s);border:1px solid var(--brd);color:${page <= 1 ? 'var(--tx3)' : 'var(--tx)'};border-radius:8px;font-size:var(--fs-sm);cursor:${page <= 1 ? 'default' : 'pointer'};">
        ← Anterior
      </button>
      <span style="font-size:var(--fs-xs);color:var(--tx3);">${total.toLocaleString()} · Pág ${page}/${totalPag}</span>
      <button onclick="biCambiarPagina(${page + 1})" ${page >= totalPag ? 'disabled' : ''}
        style="padding:8px 14px;background:var(--bg-s);border:1px solid var(--brd);color:${page >= totalPag ? 'var(--tx3)' : 'var(--tx)'};border-radius:8px;font-size:var(--fs-sm);cursor:${page >= totalPag ? 'default' : 'pointer'};">
        Siguiente →
      </button>
    </div>`;
}

/**
 * Dibuja la tendencia por día (barra), mismo patrón que graficaTendencia()
 * en app.js: destruir la instancia previa antes de recrear, si no el canvas
 * se re-crea encima del viejo y filtra memoria en cada refresco de 30s.
 */
function biRenderChart(porDia, label, color) {
  const canvas = document.getElementById('bi-chart');
  const tituloEl = document.getElementById('bi-chart-titulo');
  if (!canvas) return;
  if (tituloEl) tituloEl.textContent = label;

  const fechas = Object.keys(porDia || {}).sort();
  if (BI_CHART) { BI_CHART.destroy(); BI_CHART = null; }

  if (!fechas.length) {
    canvas.style.display = 'none';
    return;
  }
  canvas.style.display = 'block';
  const valores = fechas.map(f => porDia[f]);

  BI_CHART = new Chart(canvas, {
    type: 'bar',
    data: {
      labels: fechas,
      datasets: [{ label, data: valores, backgroundColor: color, borderRadius: 4 }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { color: '#7A96B0' }, grid: { display: false } },
        y: { ticks: { color: '#7A96B0' }, beginAtZero: true, grid: { color: '#1a1a1a' } },
      },
    },
  });
}

function biErrorHTML(mensaje, reintentarFn) {
  return `<div style="text-align:center;padding:20px;color:var(--err-tx);">${mensaje}<br>
    <button onclick="${reintentarFn}" style="margin-top:8px;padding:6px 12px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);cursor:pointer;">Reintentar</button></div>`;
}

async function cargarBI() {
  biRenderSubtabs();
  if (_BI_SUBTAB === 'despachados') await biCargarDespachados();
  else if (_BI_SUBTAB === 'pendientes') await biCargarPendientes();
  else if (_BI_SUBTAB === 'venta_perdida') await biCargarVentaPerdida();
}

async function biCargarDespachados() {
  const kpiEl = document.getElementById('bi-kpi');
  const descEl = document.getElementById('bi-desglose');
  const listaEl = document.getElementById('bi-detalle-lista');
  const pagEl = document.getElementById('bi-detalle-paginacion');
  const tituloEl = document.getElementById('bi-detalle-titulo');
  if (!kpiEl) return;
  tituloEl.textContent = 'Pedidos despachados — detalle';
  kpiEl.innerHTML = '<div style="color:var(--tx3);padding:10px 0;">Cargando...</div>';
  descEl.innerHTML = '';
  listaEl.innerHTML = 'Cargando...';
  pagEl.innerHTML = '';

  const { desde, hasta } = biRangoFechas();
  const qs = `almacen_id=${ALMACEN_ID}&fecha_desde=${desde}&fecha_hasta=${hasta}`;
  try {
    const [kpi, det] = await Promise.all([
      get(`/api/dashboard/bi/pedidos-despachados?${qs}`),
      get(`/api/dashboard/bi/pedidos-despachados/detalle?${qs}&page=${_BI_DET_PAGE}&per_page=50`),
    ]);

    kpiEl.innerHTML = `
      <div class="kpi-grid" style="margin-bottom:6px;">
        <div class="kpi-card"><div class="kpi-valor">${esc(kpi.pedidos)}</div><div class="kpi-label">Pedidos</div></div>
        <div class="kpi-card"><div class="kpi-valor">${esc(kpi.lineas)}</div><div class="kpi-label">Líneas</div></div>
        <div class="kpi-card"><div class="kpi-valor">${esc(kpi.unidades)}</div><div class="kpi-label">Unidades</div></div>
        <div class="kpi-card"><div class="kpi-valor">${esc(biValorDespachado(kpi))}</div><div class="kpi-label">Valor total</div></div>
      </div>
      ${biNotaSinValor(kpi)}
      ${biUltimaActualizacion()}`;
    biRenderChart(kpi.por_dia, 'Valor despachado por día', '#2BAAB8');

    listaEl.innerHTML = det.items.length ? det.items.map(t => `
      <div class="tabla-fila">
        <div style="min-width:0;">
          <div class="tabla-nombre">Pedido ${esc(biPedidoDe(t))} · ${esc(t.cliente || 'cliente sin dato')}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(biFechaHora(t.fecha_despachado))} · ${esc(t.total_items)} línea(s) · empaque ${esc(t.codigo)}</div>
        </div>
        <div style="font-weight:700;white-space:nowrap;">${t.valor_factura === null || t.valor_factura === undefined
          ? '<span style="color:var(--tx3);font-weight:400;">sin valor</span>' : esc(biMoneda(t.valor_factura))}</div>
      </div>`).join('')
      : '<div style="text-align:center;padding:30px;color:var(--tx3);">Sin pedidos despachados en este rango</div>';

    pagEl.innerHTML = biPaginacionHTML(det.total, det.page, det.per_page);
  } catch (e) {
    kpiEl.innerHTML = biErrorHTML(e.message || 'Error cargando el KPI', 'biCargarDespachados()');
    listaEl.innerHTML = '';
    biRenderChart({}, 'Valor despachado por día', '#2BAAB8');
  }
}

async function biCargarPendientes() {
  const kpiEl = document.getElementById('bi-kpi');
  const descEl = document.getElementById('bi-desglose');
  const listaEl = document.getElementById('bi-detalle-lista');
  const pagEl = document.getElementById('bi-detalle-paginacion');
  const tituloEl = document.getElementById('bi-detalle-titulo');
  if (!kpiEl) return;
  tituloEl.textContent = 'Pedidos pendientes — detalle';
  kpiEl.innerHTML = '<div style="color:var(--tx3);padding:10px 0;">Cargando...</div>';
  descEl.innerHTML = '';
  listaEl.innerHTML = 'Cargando...';
  pagEl.innerHTML = '';

  const { desde, hasta } = biRangoFechas();
  const qs = `almacen_id=${ALMACEN_ID}&fecha_desde=${desde}&fecha_hasta=${hasta}`;
  try {
    const [kpi, det] = await Promise.all([
      get(`/api/dashboard/bi/pedidos-pendientes?${qs}`),
      get(`/api/dashboard/bi/pedidos-pendientes/detalle?${qs}&page=${_BI_DET_PAGE}&per_page=50`),
    ]);

    const fillRateTxt = (kpi.fill_rate === null || kpi.fill_rate === undefined)
      ? '—' : (kpi.fill_rate * 100).toFixed(1) + '%';

    kpiEl.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin-bottom:6px;">
        <div class="kpi-card"><div class="kpi-valor">${esc(kpi.lineas_pendientes)}</div><div class="kpi-label">Líneas pendientes</div></div>
        <div class="kpi-card" title="Sobre los pedidos que todavía están pendientes en Siesa: lo ya cumplido sale de la lista, así que no es el «Nivel de servicio» de 📈 Analítica (que mide la historia completa del pedido)."><div class="kpi-valor">${esc(fillRateTxt)}</div><div class="kpi-label">Servido de lo pendiente</div></div>
      </div>
      ${biUltimaActualizacion()}`;
    descEl.innerHTML = biBadges(kpi.por_motivo);
    biRenderChart(kpi.por_dia, 'Líneas pendientes por día', '#F59E0B');

    listaEl.innerHTML = det.items.length ? det.items.map(p => `
      <div class="tabla-fila">
        <div>
          <div class="tabla-nombre">${esc(p.numero_pedido)} — ${esc(p.item_descripcion || '')}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(p.cliente || 'Sin cliente')} · ${esc(p.motivo)} · ${p.dias_atraso ?? '—'} día(s) de atraso</div>
        </div>
        <div style="text-align:right;font-size:var(--fs-sm);">${esc(p.cantidad_pendiente)} / ${esc(p.cantidad_pedida)}</div>
      </div>`).join('')
      : '<div style="text-align:center;padding:30px;color:var(--tx3);">Sin pedidos pendientes en este rango</div>';

    pagEl.innerHTML = biPaginacionHTML(det.total, det.page, det.per_page);
  } catch (e) {
    kpiEl.innerHTML = biErrorHTML(e.message || 'Error cargando el KPI', 'biCargarPendientes()');
    listaEl.innerHTML = '';
    biRenderChart({}, 'Líneas pendientes por día', '#F59E0B');
  }
}

/** Un agotado sin precio de venta no vale $0: no se sabe cuánto vale. El
 *  servidor no lo suma y lo declara (`sin_precio`, `total_es_cota_inferior`);
 *  la pantalla solo lo dice. */
function _biSinPrecioHtml(kpi) {
  const sp = (kpi && kpi.sin_precio) || {};
  if (!sp.eventos) return '';
  return `<div style="font-size:var(--fs-xs);color:var(--tx3);margin:4px 0 8px;">⚠ El total es un piso: ${esc(sp.eventos)} agotado(s) (${esc(sp.unidades)} und) no tienen precio de venta y no están sumados.</div>`;
}

async function biCargarVentaPerdida() {
  const kpiEl = document.getElementById('bi-kpi');
  const descEl = document.getElementById('bi-desglose');
  const listaEl = document.getElementById('bi-detalle-lista');
  const pagEl = document.getElementById('bi-detalle-paginacion');
  const tituloEl = document.getElementById('bi-detalle-titulo');
  if (!kpiEl) return;
  tituloEl.textContent = 'Venta perdida por agotados — detalle';
  kpiEl.innerHTML = '<div style="color:var(--tx3);padding:10px 0;">Cargando...</div>';
  descEl.innerHTML = '';
  listaEl.innerHTML = 'Cargando...';
  pagEl.innerHTML = '';

  const { desde, hasta } = biRangoFechas();
  const qs = `almacen_id=${ALMACEN_ID}&fecha_desde=${desde}&fecha_hasta=${hasta}`;
  try {
    const [kpi, det] = await Promise.all([
      get(`/api/dashboard/bi/venta-perdida?${qs}`),
      get(`/api/dashboard/bi/venta-perdida/detalle?${qs}&page=${_BI_DET_PAGE}&per_page=50`),
    ]);

    kpiEl.innerHTML = `
      <div style="display:grid;grid-template-columns:1fr;gap:10px;margin-bottom:6px;">
        <div class="kpi-card"><div class="kpi-valor">${biMoneda(kpi.venta_perdida_total)}</div><div class="kpi-label">Venta perdida en el rango</div></div>
      </div>
      ${_biSinPrecioHtml(kpi)}
      ${biUltimaActualizacion()}`;
    descEl.innerHTML = biBadges(biCategoriasVentaPerdida(kpi));
    biRenderChart(kpi.por_dia, 'Venta perdida $ por día', '#F87171');

    listaEl.innerHTML = det.items.length ? det.items.map(ev => `
      <div class="tabla-fila">
        <div>
          <div class="tabla-nombre">${esc(ev.producto_codigo || ('SKU ' + ev.producto_id))} — ${esc(ev.producto_nombre || '')}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(ev.pedido_siesa_ref || 'Sin pedido')} · ${esc(ev.categoria_producto || 'Sin categoría')} · ${biFechaHora(ev.creado_en)}</div>
        </div>
        <div style="text-align:right;">
          <div style="font-weight:700;">${ev.precio_venta_capturado == null ? '<span style="color:var(--tx3);font-weight:400;">sin precio</span>' : esc(biMoneda(ev.cantidad_faltante * ev.precio_venta_capturado))}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(ev.cantidad_faltante)} und</div>
        </div>
      </div>`).join('')
      : '<div style="text-align:center;padding:30px;color:var(--tx3);">Sin eventos de agotado en este rango — histórico solo desde que se activó la captura</div>';

    pagEl.innerHTML = biPaginacionHTML(det.total, det.page, det.per_page);
  } catch (e) {
    kpiEl.innerHTML = biErrorHTML(e.message || 'Error cargando el KPI', 'biCargarVentaPerdida()');
    listaEl.innerHTML = '';
    biRenderChart({}, 'Venta perdida $ por día', '#F87171');
  }
}

// ── Sin valor ≠ $0 (2026-09-24) ─────────────────────────────────────────────
// Un despacho sin `valor_factura` o un agotado sin precio no valen $0: no se
// sabe cuánto valen. El servidor ya los separa (`sin_valor_factura`,
// `sin_precio_por_categoria`); estas funciones solo lo dicen.

/** «sin valor», «$X al menos» o «$X». */
function biValorDespachado(kpi) {
  const sin = Number((kpi && kpi.sin_valor_factura) || 0);
  const n = Number((kpi && kpi.pedidos) || 0);
  if (n && sin >= n) return 'sin valor';
  const v = biMoneda(kpi ? kpi.valor_total : null);
  return sin ? v + ' al menos' : v;
}

function biNotaSinValor(kpi) {
  const sin = Number((kpi && kpi.sin_valor_factura) || 0);
  if (!sin) return '';
  return `<div style="font-size:var(--fs-xs);color:var(--tx3);margin:4px 0 8px;">⚪ ${esc(sin)} despacho(s) sin valor de factura: no suman al total (no valen $0, no se sabe cuánto valen).</div>`;
}

/** El pedido que se ve: el número de Siesa, o la clave, o el documento. */
function biPedidoDe(t) {
  return (t && (t.numero_pedido_siesa || t.pedido_clave || t.referencia_doc)) || 'sin número';
}

/** `{categoría: texto}` sin pintar $0 donde solo hay agotados sin precio. */
function biCategoriasVentaPerdida(kpi) {
  const sin = (kpi && kpi.sin_precio_por_categoria) || {};
  const out = {};
  Object.entries((kpi && kpi.por_categoria) || {}).forEach(([k, v]) => {
    const n = Number(sin[k] || 0);
    if (n && !Number(v)) out[k] = `sin precio (${n})`;
    else out[k] = biMoneda(v) + (n ? ' al menos' : '');
  });
  return out;
}
