/* compras_ia.js — UI de inteligencia de compras
 * Sub-tabs: Acuerdos Marco, Armador Contenedor, Deriva de Precios
 * + Inteligencia de inventario: TSB, Newsvendor, ROP, Clasificación S-B
 */
'use strict';

// ═══════════════════════════════════════════════════════════════════
// SUB-TAB: ACUERDOS MARCO
// ═══════════════════════════════════════════════════════════════════

async function compCargarAcuerdos() {
  const el = document.getElementById('comp-sec-acuerdos');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Cargando acuerdos...</div>';

  try {
    const [acuerdos, calendario] = await Promise.all([
      get('/api/compras/acuerdos'),
      get('/api/compras/calendario-vencimientos'),
    ]);
    _renderAcuerdos(el, acuerdos, calendario);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

function _renderAcuerdos(el, data, calendario) {
  const { acuerdos, total } = data;
  const { resumen } = calendario;
  let html = '';

  // KPIs
  html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px;">';
  html += _compKpi(total, 'Acuerdos vigentes', 'var(--green)');
  html += _compKpi(resumen.acuerdos_por_vencer, 'Por vencer', 'var(--yellow)');
  html += _compKpi(resumen.candidatos_nuevo_acuerdo, 'Candidatos', 'var(--blue)');
  html += '</div>';

  // Alertas de vencimiento
  if (calendario.por_vencer && calendario.por_vencer.length > 0) {
    html += '<div style="background:#FACC1522;border:1px solid var(--yellow);border-radius:8px;padding:10px;margin-bottom:12px;">';
    html += '<div style="font-size:12px;font-weight:700;color:var(--yellow);margin-bottom:6px;">Acuerdos por vencer</div>';
    for (const a of calendario.por_vencer) {
      html += `<div style="font-size:11px;color:var(--tx);margin-left:8px;">
        ${a.proveedor_nombre || '?'} — vence ${a.vigencia_hasta} (${a.dias_para_vencer}d)
      </div>`;
    }
    html += '</div>';
  }

  // Candidatos a nuevo acuerdo
  if (calendario.candidatos_acuerdo && calendario.candidatos_acuerdo.length > 0) {
    html += '<div style="background:#60A5FA22;border:1px solid var(--blue);border-radius:8px;padding:10px;margin-bottom:12px;">';
    html += '<div style="font-size:12px;font-weight:700;color:var(--blue);margin-bottom:6px;">Candidatos a acuerdo marco</div>';
    for (const c of calendario.candidatos_acuerdo.slice(0, 5)) {
      html += `<div style="font-size:11px;color:var(--tx);margin-left:8px;">
        ${c.referencia} ${c.nombre} — cotizado ${c.cotizaciones_trimestre}x este trimestre sin acuerdo
      </div>`;
    }
    html += '</div>';
  }

  // Lista de acuerdos vigentes
  html += '<div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">Acuerdos vigentes</div>';
  if (acuerdos.length === 0) {
    html += '<div style="color:var(--tx3);padding:20px;text-align:center;">Sin acuerdos registrados. Negocia con tus 10 proveedores grandes, trimestre a trimestre.</div>';
  } else {
    html += '<div style="display:flex;flex-direction:column;gap:4px;">';
    for (const a of acuerdos) {
      const color = a.dias_para_vencer < 21 ? 'var(--yellow)' : 'var(--green)';
      html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:6px;padding:8px 12px;display:flex;justify-content:space-between;align-items:center;">
        <div>
          <span style="font-size:12px;font-weight:600;color:var(--tx);">${a.proveedor_nombre || '?'}</span>
          <span style="font-size:11px;color:var(--tx3);margin-left:8px;">$${Number(a.precio_unitario).toLocaleString('es-CO')}</span>
        </div>
        <span style="font-size:10px;color:${color};">${a.dias_para_vencer}d restantes</span>
      </div>`;
    }
    html += '</div>';
  }

  el.innerHTML = html;
}

// ═══════════════════════════════════════════════════════════════════
// SUB-TAB: ARMADOR DE CONTENEDOR
// ═══════════════════════════════════════════════════════════════════

/** Tipo de contenedor seleccionado. Cambia el CBM objetivo contra el que se arma. */
let ARMADOR_TIPO = '40STD';
/** Catálogo cacheado — viene del backend, no se duplica aquí. */
let ARMADOR_TIPOS = null;

async function compCargarArmador(tipo) {
  const el = document.getElementById('comp-sec-armador');
  if (!el) return;
  if (tipo) ARMADOR_TIPO = tipo;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Calculando propuesta de contenedor...</div>';

  try {
    const pend = [
      get('/api/compras/armador/propuesta?tipo=' + encodeURIComponent(ARMADOR_TIPO)),
      get('/api/compras/armador/g5'),
      get('/api/compras/armador/sigma-lt'),
    ];
    if (!ARMADOR_TIPOS) pend.push(get('/api/compras/armador/tipos'));

    const [propuesta, g5, sigma, cat] = await Promise.all(pend);
    if (cat) ARMADOR_TIPOS = cat.tipos;
    _renderArmador(el, propuesta, g5, sigma);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

/** Selector de contenedor: cada tipo muestra su CBM útil. */
function _selectorContenedor(propuesta) {
  if (!ARMADOR_TIPOS || !ARMADOR_TIPOS.length) return '';

  const botones = ARMADOR_TIPOS.map(t => {
    const activo = t.tipo === ARMADOR_TIPO;
    return `<button onclick="compCargarArmador('${t.tipo}')"
      style="padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;
             border:1px solid ${activo ? 'var(--pm)' : 'var(--brd)'};
             background:${activo ? 'var(--pm)' : 'transparent'};
             color:${activo ? '#fff' : 'var(--tx3)'};
             font-weight:${activo ? '700' : '400'};">
      ${t.etiqueta}
      <span style="opacity:.75;font-size:10px;"> ${t.cbm_util} CBM</span>
    </button>`;
  }).join('');

  const sel = ARMADOR_TIPOS.find(t => t.tipo === ARMADOR_TIPO);
  return `<div style="margin-bottom:12px;">
    <div style="font-size:11px;font-weight:700;color:var(--tx3);margin-bottom:6px;">Contenedor a traer</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">${botones}</div>
    ${sel ? `<div style="font-size:10px;color:var(--tx3);margin-top:6px;">
      CBM útil ${sel.cbm_util} · objetivo de armado ${sel.cbm_objetivo} (90%) · payload ${sel.payload_kg.toLocaleString('es-CO')} kg
    </div>` : ''}
  </div>`;
}

/** Procedencia del σ_LT — va PEGADO a la ventana ETA, no en una caja aparte.
 *  Un comprador que no puede auditar de dónde sale la fecha no la obedece. */
function _procedenciaSigmaLt(s) {
  if (!s) return '';
  if (s.fuente === 'MEDIDO') {
    return `<span style="color:var(--green);"> · σ medido sobre ${s.n} contenedores</span>`;
  }
  const faltan = Math.max(0, 6 - (s.n || 0));
  return `<span style="color:var(--yellow);"> · σ=${s.sigma_lt}d supuesto, no medido`
       + ` — faltan ${faltan} contenedor(es) con fechas completas</span>`;
}

function _renderArmador(el, propuesta, g5, sigma) {
  let html = '';

  // Modo
  const modoColor = propuesta.modo === 'SHADOW' ? 'var(--yellow)' : 'var(--green)';
  html += `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
    <span style="font-size:12px;font-weight:700;padding:4px 10px;border-radius:4px;background:${modoColor}22;color:${modoColor};">
      ${propuesta.modo === 'SHADOW' ? 'SHADOW — fichas al ' + propuesta.cobertura_fichas_pct + '%' : 'ACTIVO'}
    </span>
    <span style="font-size:11px;color:var(--tx3);">Gatillo: ${propuesta.gatillo}</span>
  </div>`;

  // Compuertas G5
  html += '<div style="display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap;">';
  for (const [key, val] of Object.entries(g5)) {
    if (key === 'g5_ok' || key === 'total_skus_china') continue;
    const v = val;
    const ok = v.ok;
    html += `<span style="font-size:10px;padding:3px 8px;border-radius:4px;
      background:${ok ? 'var(--green)22' : 'var(--red)22'};color:${ok ? 'var(--green)' : 'var(--red)'};">
      ${ok ? '●' : '○'} ${v.nombre}
    </span>`;
  }
  html += '</div>';

  // Selector de contenedor — define el CBM objetivo de todo lo que sigue
  html += _selectorContenedor(propuesta);

  // Barras de progreso CBM + Peso
  html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px;">';
  html += _barraProgreso('CBM', propuesta.barras.cbm_acumulado, propuesta.barras.cbm_objetivo, propuesta.barras.cbm_pct, 'var(--pm)');
  html += _barraProgreso('Peso (kg)', propuesta.barras.peso_acumulado, propuesta.barras.peso_limite, propuesta.barras.peso_pct, '#8b5cf6');
  html += '</div>';

  // Resumen
  html += `<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:12px;">`;
  html += _compKpi(propuesta.total_items, 'Items total', 'var(--tx)');
  html += _compKpi(propuesta.items_deficit, 'Por deficit', 'var(--red)');
  html += _compKpi(propuesta.items_relleno, 'Relleno', 'var(--blue)');
  html += _compKpi('$' + (propuesta.valor_fob_usd || 0).toLocaleString('es-CO'), 'FOB USD', 'var(--green)');
  html += '</div>';

  // Ventana de llegada
  if (propuesta.ventana_llegada) {
    html += `<div style="font-size:11px;color:var(--tx3);margin-bottom:12px;">
      Ventana estimada: ${propuesta.ventana_llegada.desde} a ${propuesta.ventana_llegada.hasta}
      <br>${propuesta.ventana_llegada.nota}${_procedenciaSigmaLt(sigma)}
    </div>`;
  }

  // Items del contenedor
  if (propuesta.items && propuesta.items.length > 0) {
    html += '<div style="font-size:12px;font-weight:700;color:var(--tx);margin-bottom:6px;">Composicion propuesta</div>';
    html += '<div style="max-height:300px;overflow-y:auto;">';
    for (const item of propuesta.items) {
      const isRelleno = item.tipo === 'RELLENO';
      const border = isRelleno ? '1px dashed var(--blue)' : '1px solid var(--brd)';
      html += `<div style="background:var(--bg-s);border:${border};border-radius:6px;padding:6px 10px;margin-bottom:3px;
        display:flex;justify-content:space-between;align-items:center;font-size:11px;">
        <div>
          <span style="color:var(--tx);">${item.referencia}</span>
          <span style="color:var(--tx3);margin-left:6px;">${item.cajas} cajas</span>
          ${isRelleno ? '<span style="color:var(--blue);margin-left:4px;font-size:9px;">RELLENO</span>' : ''}
        </div>
        <div style="text-align:right;color:var(--tx3);">
          ${item.cbm} CBM | ${item.peso_kg} kg
        </div>
      </div>`;
    }
    html += '</div>';
  }

  // Excluidos
  if (propuesta.excluidos && propuesta.excluidos.length > 0) {
    html += `<div style="margin-top:10px;font-size:11px;color:var(--tx3);">
      ${propuesta.excluidos.length} SKUs excluidos (sin ficha o estimado)
    </div>`;
  }

  el.innerHTML = html;
}

function _barraProgreso(label, actual, total, pct, color) {
  const width = Math.min(pct, 100);
  return `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:10px;">
    <div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px;">
      <span style="color:var(--tx3);">${label}</span>
      <span style="color:var(--tx);">${actual} / ${total} (${pct}%)</span>
    </div>
    <div style="height:8px;background:var(--bg);border-radius:4px;overflow:hidden;">
      <div style="height:100%;width:${width}%;background:${color};border-radius:4px;transition:width 0.3s;"></div>
    </div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════
// SUB-TAB: DETECTOR DE DERIVA
// ═══════════════════════════════════════════════════════════════════

async function compCargarDeriva() {
  const el = document.getElementById('comp-sec-deriva');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Analizando deriva de precios...</div>';

  try {
    const result = await get('/api/compras/deriva?meses=3');
    _renderDeriva(el, result);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

function _renderDeriva(el, data) {
  let html = '';

  const n = data.total || 0;
  const sobre = data.sobrecostos || 0;

  html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:14px;">';
  html += _compKpi(n, 'Derivas detectadas', n > 0 ? 'var(--red)' : 'var(--green)');
  html += _compKpi(sobre, 'Sobrecostos', sobre > 0 ? 'var(--red)' : 'var(--green)');
  html += '</div>';

  if (n === 0) {
    html += '<div style="color:var(--green);padding:20px;text-align:center;font-size:13px;">Sin derivas detectadas — precios facturados coinciden con acuerdos.</div>';
  } else {
    html += '<div style="font-size:12px;font-weight:700;color:var(--tx);margin-bottom:6px;">Derivas precio facturado vs pactado</div>';
    for (const d of data.derivas) {
      const color = d.alerta === 'SOBRECOSTO' ? 'var(--red)' : 'var(--green)';
      html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:6px;padding:8px 12px;margin-bottom:4px;">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <div>
            <span style="font-size:12px;color:var(--tx);">${d.referencia}</span>
            <span style="font-size:10px;color:var(--tx3);margin-left:6px;">${d.nombre}</span>
          </div>
          <span style="font-size:13px;font-weight:700;color:${color};">${d.diferencia_pct > 0 ? '+' : ''}${d.diferencia_pct}%</span>
        </div>
        <div style="font-size:10px;color:var(--tx3);margin-top:2px;">
          Pactado: $${d.precio_pactado.toLocaleString('es-CO')} | Facturado: $${d.precio_facturado.toLocaleString('es-CO')} | ${d.proveedor_factura}
        </div>
      </div>`;
    }
  }

  el.innerHTML = html;
}

// ═══════════════════════════════════════════════════════════════════
// SUB-TAB: INTELIGENCIA DE INVENTARIO (Kardex + TSB + ROP)
// ═══════════════════════════════════════════════════════════════════

let _INV_IA_SUBTAB = 'sb';

async function compCargarInteligencia() {
  const el = document.getElementById('inv-ia-container');
  if (!el) return;
  if (_INV_IA_SUBTAB === 'sb') await _cargarClasificacionSB(el);
  else if (_INV_IA_SUBTAB === 'tsb') await _cargarTSB(el);
  else if (_INV_IA_SUBTAB === 'rop') await _cargarROP(el);
  else if (_INV_IA_SUBTAB === 'newsvendor') await _cargarNewsvendor(el);
}

function invIaSubtab(nombre) {
  _INV_IA_SUBTAB = nombre;
  const tabs = ['sb', 'tsb', 'rop', 'newsvendor'];
  tabs.forEach(t => {
    const tab = document.getElementById('inv-ia-sub-' + t);
    if (tab) {
      tab.style.background = t === nombre ? 'var(--pm)' : 'transparent';
      tab.style.color = t === nombre ? '#fff' : 'var(--tx3)';
      tab.style.fontWeight = t === nombre ? '700' : '400';
    }
  });
  compCargarInteligencia();
}

async function _cargarClasificacionSB(el) {
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Clasificando demanda (Syntetos-Boylan)...</div>';
  try {
    const r = await get('/api/kardex/clasificacion-sb?meses=12');
    let html = '';
    html += '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin-bottom:12px;">';
    for (const [cuad, info] of Object.entries(r.resumen || {})) {
      const colors = { SUAVE: 'var(--green)', ERRATICA: 'var(--yellow)', INTERMITENTE: 'var(--blue)', GRUMOSA: 'var(--red)' };
      html += _compKpi(info.cantidad, cuad, colors[cuad] || 'var(--tx)');
    }
    html += '</div>';
    html += `<div style="font-size:11px;color:var(--tx3);margin-bottom:8px;">${r.total_clasificados} SKUs clasificados | ${r.estacionales_excluidos} estacionales excluidos</div>`;
    if (r.clasificacion) {
      html += '<div style="max-height:400px;overflow-y:auto;">';
      for (const c of r.clasificacion.slice(0, 50)) {
        const colors = { SUAVE: 'var(--green)', ERRATICA: 'var(--yellow)', INTERMITENTE: 'var(--blue)', GRUMOSA: 'var(--red)' };
        html += `<div style="background:var(--bg-s);border-left:3px solid ${colors[c.cuadrante] || 'var(--brd)'};padding:6px 10px;margin-bottom:2px;font-size:11px;display:flex;justify-content:space-between;">
          <span style="color:var(--tx);">${c.referencia} <span style="color:var(--tx3);">${c.cuadrante}</span></span>
          <span style="color:var(--tx3);">ADI=${c.adi} CV2=${c.cv2} | ${c.demanda_total} uds</span>
        </div>`;
      }
      html += '</div>';
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

async function _cargarTSB(el) {
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Calculando pronosticos TSB...</div>';
  try {
    const r = await get('/api/kardex/pronostico-tsb?meses=12');
    let html = '';
    html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;">';
    html += _compKpi(r.total, 'SKUs pronosticados', 'var(--pm)');
    html += _compKpi(r.backtest.tsb_gana, 'TSB gana a MM8', 'var(--green)');
    html += _compKpi(r.backtest.porcentaje_tsb_gana + '%', 'Win rate TSB', r.backtest.porcentaje_tsb_gana > 50 ? 'var(--green)' : 'var(--red)');
    html += '</div>';
    if (r.pronosticos) {
      html += '<div style="max-height:400px;overflow-y:auto;">';
      for (const p of r.pronosticos.slice(0, 30)) {
        html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:6px;padding:6px 10px;margin-bottom:3px;font-size:11px;display:flex;justify-content:space-between;">
          <span style="color:var(--tx);">${p.referencia}</span>
          <span style="color:var(--tx3);">TSB=${p.tsb_semanal}/sem | p=${p.p_suavizado}d | ${p.eventos} eventos ${p.tsb_mejor === true ? '<span style="color:var(--green);">TSB gana</span>' : ''}</span>
        </div>`;
      }
      html += '</div>';
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

async function _cargarROP(el) {
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Calculando ROP dual...</div>';
  try {
    const r = await get('/api/compras/rop-dual');
    let html = '';
    html += '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px;">';
    // Nacional
    html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:12px;">
      <div style="font-size:13px;font-weight:700;color:var(--pm);margin-bottom:6px;">Nacional (LT=${r.nacional.lt_dias}d)</div>
      <div style="font-size:11px;color:var(--tx3);">${r.nacional.total} SKUs | ${r.nacional.bajo_rop} bajo ROP</div>
    </div>`;
    // China
    html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:12px;">
      <div style="font-size:13px;font-weight:700;color:var(--red);margin-bottom:6px;">China (LT=${r.china.lt_dias}d)</div>
      <div style="font-size:11px;color:var(--tx3);">${r.china.total} SKUs | ${r.china.con_deficit} con deficit | sigma=${r.china.sigma_lt} (${r.china.sigma_lt_fuente})</div>
    </div>`;
    html += '</div>';
    // Items China con deficit (los urgentes)
    const urgentes = (r.china.items || []).filter(i => i.deficit > 0).slice(0, 20);
    if (urgentes.length > 0) {
      html += '<div style="font-size:12px;font-weight:700;color:var(--red);margin-bottom:6px;">SKUs China con deficit</div>';
      html += '<div style="max-height:300px;overflow-y:auto;">';
      for (const item of urgentes) {
        html += `<div style="background:var(--bg-s);border-left:3px solid var(--red);padding:6px 10px;margin-bottom:2px;font-size:11px;display:flex;justify-content:space-between;">
          <span style="color:var(--tx);">${item.referencia}</span>
          <span style="color:var(--tx3);">cobertura ${item.cobertura_dias}d | deficit ${item.deficit} | stock ${item.stock_actual} + transito ${item.en_transito}</span>
        </div>`;
      }
      html += '</div>';
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

async function _cargarNewsvendor(el) {
  el.innerHTML = `<div style="color:var(--tx3);padding:20px;">
    <div style="font-size:13px;font-weight:700;color:var(--yellow);margin-bottom:8px;">Newsvendor — Compra de temporada escolar</div>
    <div style="font-size:12px;margin-bottom:12px;">DEADLINE: 7 de agosto 2026</div>
    <div style="font-size:11px;line-height:1.6;">
      Q* = cantidad optima que maximiza utilidad esperada bajo incertidumbre.<br>
      Necesita: ventas por temporada pasada por SKU escolar.<br><br>
      <strong>Uso:</strong> POST /api/kardex/newsvendor con:<br>
      <code style="background:var(--bg);padding:2px 6px;border-radius:4px;">
      {"items": [{"referencia": "CUAD-001", "ventas_pasadas": [1200, 1350], "costo_unitario": 5000}], "margen_pct": 0.40}
      </code><br><br>
      <span style="color:var(--yellow);">Pendiente: Santiago exporta ventas temporada 2024 y 2025 de Siesa.</span>
    </div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════
// HELPER
// ═══════════════════════════════════════════════════════════════════

function _compKpi(valor, label, color) {
  return `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:8px;text-align:center;">
    <div style="font-size:18px;font-weight:800;color:${color};">${valor}</div>
    <div style="font-size:10px;color:var(--tx3);">${label}</div>
  </div>`;
}
