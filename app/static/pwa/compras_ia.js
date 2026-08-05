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
  // El buscador de precios por producto vive acá: es donde alguien de compras
  // ya está mirando el precio de algo.
  _preciosAsegurarBuscador(el);
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
      get('/api/compras/rop-dual'),
    ];
    if (!ARMADOR_TIPOS) pend.push(get('/api/compras/armador/tipos'));

    const [propuesta, g5, sigma, rop, cat] = await Promise.all(pend);
    if (cat) ARMADOR_TIPOS = cat.tipos;
    _renderArmador(el, propuesta, g5, sigma, rop);
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

/** Déficit China con procedencia por fila.
 *
 *  La procedencia NO es adorno de confianza: es dispositivo de seguridad. El
 *  bug de 25x habría producido números absurdos en pantalla, y lo único que
 *  separa a un humano de aprobarlos es ver, en la MISMA fila, de dónde salió
 *  la demanda. Un aviso agregado no basta: la señal va donde está el número.
 */
function _tablaDeficitChina(rop) {
  if (!rop || !rop.china) return '';
  const ch = rop.china;
  const items = (ch.items || []).filter(i => (i.deficit || 0) > 0).slice(0, 40);
  const d = rop.delta_vs_formula_anterior || {};

  let html = `<div style="margin-bottom:14px;">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:6px;">
      <span style="font-size:12px;font-weight:700;color:var(--tx);">Déficit China — qué pide entrar al contenedor</span>
      <span style="font-size:10px;color:var(--tx3);">
        LT ${ch.lt_dias}d · σ ${ch.sigma_lt}d (${ch.sigma_lt_fuente}) · R ${ch.r_dias}d
      </span>
    </div>`;

  if (d.skus_censurados) {
    html += `<div style="background:#F8717122;border:1px solid var(--red);border-radius:8px;padding:8px 10px;margin-bottom:8px;font-size:11px;color:var(--red);">
      <strong>${d.skus_censurados} SKU(s) con demanda CENSURADA</strong> — sin StockDiario, el cálculo
      subestima. Correr <em>Reconstruir stock diario</em> en Inventario › Datos antes de decidir.
    </div>`;
  }
  if (d.multiplicador && d.multiplicador !== 1) {
    html += `<div style="font-size:11px;color:var(--tx3);margin-bottom:8px;">
      Colchón: $${''}${(d.safety_stock_antes || 0).toLocaleString('es-CO')} u → ${(d.safety_stock_despues || 0).toLocaleString('es-CO')} u
      (<strong>${d.multiplicador}×</strong> por la fórmula §M0.4)${d.topados_por_cobertura ? ` · ${d.topados_por_cobertura} topado(s) por cobertura máx.` : ''}
    </div>`;
  }

  if (!items.length) {
    html += '<div style="font-size:11px;color:var(--tx3);padding:10px 0;">Sin déficit China.</div></div>';
    return html;
  }

  html += `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;">
    <thead><tr style="border-bottom:2px solid var(--brd);color:var(--tx3);text-align:right;">
      <th style="text-align:left;padding:5px;">Referencia</th>
      <th style="padding:5px;">Demanda usada</th>
      <th style="padding:5px;">Procedencia</th>
      <th style="padding:5px;">Posición</th>
      <th style="padding:5px;">S objetivo</th>
      <th style="padding:5px;">Déficit</th>
      <th style="padding:5px;">Colchón</th>
    </tr></thead><tbody>`;

  for (const it of items) {
    const cens = it.censurado;
    const fc = it.factor_censura || 1;
    const procColor = cens ? 'var(--red)' : (fc > 1.15 ? 'var(--yellow)' : 'var(--green)');
    const proc = cens
      ? 'CENSURADA · sin StockDiario'
      : `${it.dias_con_stock}d con stock · +${Math.round((fc - 1) * 100)}%`;
    html += `<tr style="border-bottom:1px solid var(--brd);text-align:right;">
      <td style="text-align:left;padding:5px;color:var(--tx);font-weight:600;">${it.referencia}</td>
      <td style="padding:5px;color:var(--tx);">${it.d_avg_diaria}/d
        <span style="color:var(--tx3);font-size:10px;"> σ ${it.sigma_d_diaria}</span></td>
      <td style="padding:5px;color:${procColor};font-size:10px;">${proc}</td>
      <td style="padding:5px;color:var(--tx3);">${it.posicion}</td>
      <td style="padding:5px;color:var(--tx);">${it.s_objetivo}${it.topado_por_cobertura ? ' <span style="color:var(--yellow);" title="topado por cobertura máxima">▲</span>' : ''}</td>
      <td style="padding:5px;color:var(--pm);font-weight:700;">${it.deficit}</td>
      <td style="padding:5px;color:var(--tx3);font-size:10px;">${it.ss_formula_anterior} → ${it.safety_stock}</td>
    </tr>`;
  }
  html += `</tbody></table></div>
    <div style="font-size:10px;color:var(--tx3);margin-top:6px;">
      ▲ = topado por cobertura máxima (${rop.cobertura_max_dias}d). σ_d: ${rop.estimador_sigma_d}.
      Fórmula: ${rop.formula}
    </div></div>`;
  return html;
}

function _renderArmador(el, propuesta, g5, sigma, rop) {
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

  // Déficit China — insumo del armado, no información previa. Antes vivía en
  // otra pestaña: había que mirar qué falta en una pantalla y armarlo en otra.
  html += _tablaDeficitChina(rop);

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
      // El margen por CBM es la metrica que DECIDE el orden del relleno y
      // hasta el 2026-08-05 no se mostraba en ninguna parte. Valia 0 para
      // todos —el maestro nunca tiene precio— y nadie podia notarlo mirando
      // la pantalla: la metrica ordenaba y era invisible.
      //
      // Ahora va con su procedencia. Una fila sobre cotizacion vigente y una
      // sobre margen supuesto no valen lo mismo, y el comite firma la fila.
      const sup = item.precio_es_supuesto;
      const margen = isRelleno && item.margen_por_cbm !== undefined
        ? `<div style="font-size:10px;color:${sup ? 'var(--yellow)' : 'var(--green)'};">
             ${item.margen_por_cbm_rango
               ? `margen/CBM ${item.margen_por_cbm_rango[0]}–${item.margen_por_cbm_rango[1]}`
               : `margen/CBM ${item.margen_por_cbm}`}
             <span style="color:var(--tx3);">· ${item.margen_fuente || 'SIN_COSTO'}</span>
             ${sup ? '<b> · supuesto</b>' : ''}
           </div>`
        : '';
      html += `<div style="background:var(--bg-s);border:${border};border-radius:6px;padding:6px 10px;margin-bottom:3px;
        display:flex;justify-content:space-between;align-items:center;font-size:11px;">
        <div>
          <span style="color:var(--tx);">${item.referencia}</span>
          <span style="color:var(--tx3);margin-left:6px;">${item.cajas} cajas</span>
          ${isRelleno ? '<span style="color:var(--blue);margin-left:4px;font-size:9px;">RELLENO</span>' : ''}
          ${margen}
        </div>
        <div style="text-align:right;color:var(--tx3);">
          ${item.cbm} CBM | ${item.peso_kg} kg
        </div>
      </div>`;
    }
    html += '</div>';
  }

  // Sobre que datos se armo este contenedor. Sin esto, uno armado enteramente
  // sobre margen supuesto se ve identico a uno armado sobre cotizaciones.
  const cob = propuesta.margen_cobertura;
  if (cob && cob.total_skus) {
    const sinCosto = cob.sin_costo || 0;
    const supuestos = cob.precio_supuesto || 0;
    const grave = sinCosto > cob.total_skus / 2;
    html += `<div style="margin-top:10px;padding:8px 10px;border-radius:6px;font-size:11px;
      background:var(--bg-s);border-left:3px solid ${grave ? 'var(--red)' : 'var(--yellow)'};">
      <b>Sobre qué datos se armó esto</b><br>
      ${cob.con_costo}/${cob.total_skus} SKU con costo de alguna fuente
      ${sinCosto ? ` · <span style="color:var(--red)">${sinCosto} sin costo de ninguna</span>` : ''}
      ${supuestos ? ` · ${supuestos} con precio supuesto` : ''}
      ${grave ? '<br><b style="color:var(--red)">Más de la mitad del ranking no tiene costo: el orden no es una decisión informada.</b>' : ''}
    </div>`;
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

let _MODELOS_SUBTAB = 'sb';

// ═══════════════════════════════════════════════════════════════════
// MODELOS — pantalla de CONFIANZA, no de decisión.
// Nadie abre un listado de pronósticos para decidir algo: se abre para
// auditar si el modelo merece crédito. Tráfico bajo, profundidad permitida.
// ═══════════════════════════════════════════════════════════════════

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
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Calculando pronósticos TSB…</div>';
  try {
    const r = await get('/api/kardex/pronostico-tsb?meses=12');
    const b = r.tamiz_mase || {};
    let html = '';

    // ── LA COMPUERTA primero. Es lo único que decide si el modelo manda ────
    const ok = b.supera_tamiz;
    const color = ok ? 'var(--green)' : 'var(--yellow)';
    html += `<div style="border:1px solid ${color};border-radius:10px;padding:12px;margin-bottom:14px;">
      <div style="font-size:13px;font-weight:700;color:${color};margin-bottom:6px;">
        Tamiz MASE: ${ok ? 'SUPERADO' : 'NO SUPERADO'}
        <span style="font-weight:400;font-size:10px;color:var(--tx3);"> — no es la compuerta</span>
      </div>
      <div style="font-size:11px;color:var(--tx3);line-height:1.7;">
        ${b.evaluados || 0} de ${b.n_minimo || 100} SKUs mínimos ·
        gana en <strong>${b.porcentaje_tsb_gana || 0}%</strong>
        (IC95 ${(b.ic95_victorias || [0, 0])[0]}–${(b.ic95_victorias || [0, 0])[1]}%)
        · ponderado por valor: <strong>${b.porcentaje_ponderado_por_valor || 0}%</strong>
        ${b.azar_descartado ? '' : '<span style="color:var(--yellow);"> · azar NO descartado</span>'}
      </div>
      <div style="font-size:11px;color:var(--tx3);margin-top:6px;">${b._por_que_no || ''}</div>
      <div style="font-size:11px;color:var(--yellow);margin-top:6px;">
        <strong>Compuerta real:</strong> ${b.compuerta_real || ''}
      </div>
      <div style="font-size:10px;color:var(--tx3);margin-top:4px;">${r.sigma_d_del_rop || ''}</div>
      <div style="font-size:10px;color:var(--tx3);margin-top:6px;">
        ${b.metrica || ''}<br>${r.demanda || ''}
      </div>
    </div>`;

    html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px;">';
    html += _compKpi(r.total || 0, 'SKUs pronosticados', 'var(--pm)');
    html += _compKpi(b.tsb_gana || 0, 'TSB gana a MM8', 'var(--green)');
    html += _compKpi((b.porcentaje_tsb_gana || 0) + '%', 'Win rate', ok ? 'var(--green)' : 'var(--yellow)');
    html += '</div>';

    const lista = r.pronosticos || [];
    if (!lista.length) {
      html += '<div style="font-size:11px;color:var(--tx3);padding:14px 0;">Sin SKUs con 12+ semanas de historia. ¿Está descargado el kardex?</div>';
    } else {
      html += `<div style="overflow-x:auto;max-height:420px;overflow-y:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:11px;">
        <thead><tr style="border-bottom:2px solid var(--brd);color:var(--tx3);text-align:right;position:sticky;top:0;background:var(--bg);">
          <th style="text-align:left;padding:5px;">Referencia</th>
          <th style="padding:5px;">TSB /sem</th>
          <th style="padding:5px;">MM8 /sem</th>
          <th style="padding:5px;">MASE TSB</th>
          <th style="padding:5px;">MASE MM8</th>
          <th style="padding:5px;">Semanas</th>
        </tr></thead><tbody>`;
      for (const p of lista.slice(0, 60)) {
        const gana = p.tsb_mejor === true;
        html += `<tr style="border-bottom:1px solid var(--brd);text-align:right;">
          <td style="text-align:left;padding:5px;color:var(--tx);font-weight:600;">
            ${gana ? '<span style="color:var(--green);">● </span>' : ''}${p.referencia}
          </td>
          <td style="padding:5px;color:var(--tx);">${p.tsb_semanal}</td>
          <td style="padding:5px;color:var(--tx3);">${p.media_movil_8sem}</td>
          <td style="padding:5px;color:${gana ? 'var(--green)' : 'var(--tx3)'};font-weight:${gana ? '700' : '400'};">${p.mase_tsb ?? '—'}</td>
          <td style="padding:5px;color:var(--tx3);">${p.mase_mm8 ?? '—'}</td>
          <td style="padding:5px;color:var(--tx3);">${p.semanas} (${p.semanas_test} test)</td>
        </tr>`;
      }
      html += `</tbody></table></div>
        <div style="font-size:10px;color:var(--tx3);margin-top:8px;">
          ● = el TSB le gana a la media móvil de 8 semanas. MASE &lt; 1 significa mejor
          que repetir el último valor observado.
        </div>`;
    }
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

async function modelosCargar() {
  const el = document.getElementById('modelos-container');
  if (!el) return;
  if (_MODELOS_SUBTAB === 'sb') await _cargarClasificacionSB(el);
  else if (_MODELOS_SUBTAB === 'tsb') await _cargarTSB(el);
}

function modelosSubtab(nombre) {
  _MODELOS_SUBTAB = nombre;
  ['sb', 'tsb'].forEach(t => {
    const tab = document.getElementById('mod-sub-' + t);
    if (tab) {
      tab.style.background = t === nombre ? 'var(--pm)' : 'transparent';
      tab.style.color = t === nombre ? '#fff' : 'var(--tx3)';
      tab.style.fontWeight = t === nombre ? '700' : '400';
    }
  });
  modelosCargar();
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

// ═══════════════════════════════════════════════════════════════════
// REPOSICIÓN NACIONAL — la decisión semanal.
//
// Lista provisional, deliberadamente simple. La pantalla buena ("Reposición":
// ROP nacional + bloqueos + precio de acuerdo vigente en una sola superficie)
// se diseña con el Dueño de Compras cuando exista, porque su flujo de trabajo
// nadie lo ha ejecutado nunca.
//
// Mientras tanto esto EXISTE, que es lo que importa: la reorganización le dio
// una superficie excelente a la decisión trimestral y no puede dejar huérfana
// a la semanal, que es la mayoría del catálogo.
// ═══════════════════════════════════════════════════════════════════

async function compCargarNacional() {
  const el = document.getElementById('nacional-container');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Calculando puntos de reorden…</div>';
  try {
    const rop = await get('/api/compras/rop-dual');
    _renderNacional(el, rop);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

function _renderNacional(el, rop) {
  const nac = (rop && rop.nacional) || {};
  const items = (nac.items || []).slice();
  const d = rop.delta_vs_formula_anterior || {};

  // Lo que está bajo ROP primero, y dentro de eso lo de menor cobertura:
  // el orden es la urgencia, no el alfabeto.
  items.sort((a, b) => (b.bajo_rop ? 1 : 0) - (a.bajo_rop ? 1 : 0)
                    || a.cobertura_dias - b.cobertura_dias);

  let html = `<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px;margin-bottom:10px;">
    <span style="font-size:13px;font-weight:700;color:var(--tx);">Reposición nacional</span>
    <span style="font-size:10px;color:var(--tx3);">LT ${nac.lt_dias}d · σ_LT ${nac.sigma_lt}d · revisión continua (R=0)</span>
  </div>`;

  html += `<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px;">
    ${_compKpi(nac.bajo_rop || 0, 'Bajo punto de reorden', (nac.bajo_rop || 0) > 0 ? 'var(--red)' : 'var(--green)')}
    ${_compKpi(nac.total || 0, 'SKUs nacionales', 'var(--tx)')}
  </div>`;

  if (d.skus_censurados) {
    html += `<div style="background:#F8717122;border:1px solid var(--red);border-radius:8px;padding:8px 10px;margin-bottom:10px;font-size:11px;color:var(--red);">
      <strong>${d.skus_censurados} SKU(s) con demanda CENSURADA</strong> — sin StockDiario el cálculo
      subestima. Correr <em>Reconstruir stock diario</em> en Inventario › Datos.
    </div>`;
  }

  if (!items.length) {
    html += '<div style="font-size:11px;color:var(--tx3);padding:14px 0;">Sin SKUs nacionales con demanda. ¿Está descargado el kardex?</div>';
    el.innerHTML = html;
    return;
  }

  html += `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;">
    <thead><tr style="border-bottom:2px solid var(--brd);color:var(--tx3);text-align:right;">
      <th style="text-align:left;padding:5px;">Referencia</th>
      <th style="padding:5px;">Demanda usada</th>
      <th style="padding:5px;">Procedencia</th>
      <th style="padding:5px;">Stock</th>
      <th style="padding:5px;">ROP</th>
      <th style="padding:5px;">Cobertura</th>
      <th style="padding:5px;">Colchón</th>
    </tr></thead><tbody>`;

  for (const it of items.slice(0, 60)) {
    const cens = it.censurado;
    const fc = it.factor_censura || 1;
    const procColor = cens ? 'var(--red)' : (fc > 1.15 ? 'var(--yellow)' : 'var(--green)');
    const proc = cens ? 'CENSURADA · sin StockDiario'
                      : `${it.dias_con_stock}d con stock · +${Math.round((fc - 1) * 100)}%`;
    html += `<tr style="border-bottom:1px solid var(--brd);text-align:right;
      background:${it.bajo_rop ? '#F8717111' : 'transparent'};">
      <td style="text-align:left;padding:5px;color:var(--tx);font-weight:600;">
        ${it.bajo_rop ? '<span style="color:var(--red);">● </span>' : ''}${it.referencia}
      </td>
      <td style="padding:5px;color:var(--tx);">${it.d_avg_diaria}/d
        <span style="color:var(--tx3);font-size:10px;"> σ ${it.sigma_d_diaria}</span></td>
      <td style="padding:5px;color:${procColor};font-size:10px;">${proc}</td>
      <td style="padding:5px;color:var(--tx3);">${it.stock_actual}</td>
      <td style="padding:5px;color:var(--tx);font-weight:700;">${it.rop}</td>
      <td style="padding:5px;color:${it.cobertura_dias < 7 ? 'var(--red)' : 'var(--tx3)'};">${it.cobertura_dias}d</td>
      <td style="padding:5px;color:var(--tx3);font-size:10px;">${it.ss_formula_anterior} → ${it.safety_stock}</td>
    </tr>`;
  }
  html += `</tbody></table></div>
    <div style="font-size:10px;color:var(--tx3);margin-top:8px;line-height:1.6;">
      ● = bajo punto de reorden. σ_d: ${rop.estimador_sigma_d}.<br>
      Provisional: la pantalla definitiva fusiona esto con bloqueos y precio de acuerdo vigente —
      se diseña con el Dueño de Compras, porque nadie ha ejecutado ese flujo todavía.
    </div>`;
  el.innerHTML = html;
}

// ═══════════════════════════════════════════════════════════════════════════
// PRECIOS POR PRODUCTO — comparador + registro
//
// Dos endpoints huérfanos que eran la misma pantalla:
// `precios/comparador/<id>` y `precios/registrar`.
//
// El segundo importa más de lo que parece: es UNA DE LAS DOS ENTRADAS DE
// PRECIO del sistema. Hasta hoy los precios solo entraban por acuerdo marco,
// así que `costo_service` —la jerarquía que decide el costo de un SKU y que el
// armador usa para ordenar el contenedor— se quedaba sin la capa de cotización
// para todo producto sin acuerdo vigente.
//
// Registrar una cotización acá mueve el costo de ese SKU de KARDEX_PROMEDIO
// (un promedio de hace meses) a COTIZACION (un precio de este trimestre).
// ═══════════════════════════════════════════════════════════════════════════

let _PRECIOS_PRODUCTO = null;
let _PRECIOS_PROVEEDORES = [];

/** Busca un producto y muestra sus precios vigentes. */
async function preciosBuscarProducto() {
  const q = (document.getElementById('precio-buscar')?.value || '').trim();
  const el = document.getElementById('precios-panel');
  if (!q || !el) return;
  el.innerHTML = '<p style="color:var(--tx3);font-size:12px;">Buscando…</p>';
  try {
    const d = await get(`/api/productos/?q=${encodeURIComponent(q)}&per_page=1`);
    const prod = (d.productos || d.items || [])[0];
    if (!prod) { el.innerHTML = '<p style="color:var(--yellow);font-size:12px;">Sin resultados</p>'; return; }
    _PRECIOS_PRODUCTO = prod;
    await preciosRenderComparador();
  } catch (e) {
    el.innerHTML = `<p style="color:var(--red);font-size:12px;">${e.message}</p>`;
  }
}

async function preciosRenderComparador() {
  const el = document.getElementById('precios-panel');
  const p = _PRECIOS_PRODUCTO;
  if (!el || !p) return;

  const [cmp, provs] = await Promise.all([
    get(`/api/compras/precios/comparador/${p.id}`).catch(e => ({ _error: e.message })),
    _PRECIOS_PROVEEDORES.length
      ? Promise.resolve(_PRECIOS_PROVEEDORES)
      : get('/api/compras/proveedores').catch(() => []),
  ]);
  if (Array.isArray(provs)) _PRECIOS_PROVEEDORES = provs;

  const filas = cmp._error
    ? `<p style="color:var(--red);font-size:12px;">${cmp._error}</p>`
    : ((cmp.precios || []).length
        ? (cmp.precios || []).map(x => `
            <div class="tabla-fila">
              <span class="tabla-nombre" style="font-size:12px;">
                ${x.proveedor}
                <span style="color:var(--tx3);"> · ${x.fuente || x.tipo || ''}</span>
              </span>
              <span style="font-size:13px;font-weight:700;">$${(x.precio || 0).toLocaleString('es-CO')}</span>
            </div>`).join('')
        : `<p style="font-size:12px;color:var(--yellow);">
             Sin precios vigentes. El costo de este SKU sale del kardex — un
             promedio, no un precio pactado.</p>`);

  el.innerHTML = `
    <div class="tabla-card">
      <div class="tabla-titulo">${p.codigo_siesa} · ${p.nombre || ''}</div>
      ${filas}
      <hr style="border-color:var(--brd);margin:12px 0;">
      <div style="font-size:12px;font-weight:700;margin-bottom:6px;">Registrar una cotización</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">
        <select id="precio-prov" style="padding:6px;border-radius:6px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);font-size:12px;">
          <option value="">— proveedor —</option>
          ${(_PRECIOS_PROVEEDORES || []).map(v =>
            `<option value="${v.id}">${v.nombre}</option>`).join('')}
        </select>
        <input id="precio-valor" type="number" step="0.01" placeholder="precio unitario"
               style="padding:6px;border-radius:6px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);font-size:12px;">
        <input id="precio-cant" type="number" placeholder="cantidad cotizada"
               style="padding:6px;border-radius:6px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);font-size:12px;">
        <input id="precio-quien" placeholder="quién cotizó"
               style="padding:6px;border-radius:6px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);font-size:12px;">
      </div>
      <button class="btn-flota" style="width:100%;margin-top:8px;"
              onclick="preciosRegistrar()">Registrar cotización</button>
      <p style="font-size:11px;color:var(--tx3);margin:6px 0 0;">
        Queda con tu nombre y la fecha de hoy. Mueve el costo de este SKU de
        promedio del kardex a cotización vigente — y eso cambia cómo se ordena
        el contenedor.
      </p>
    </div>`;
}

async function preciosRegistrar() {
  const p = _PRECIOS_PRODUCTO;
  const prov = parseInt(document.getElementById('precio-prov')?.value, 10);
  const valor = parseFloat(document.getElementById('precio-valor')?.value);
  if (!p || !Number.isFinite(prov)) { alerta('Elegí el proveedor', 'error'); return; }
  if (!Number.isFinite(valor) || valor <= 0) { alerta('El precio tiene que ser mayor a cero', 'error'); return; }

  const cant = parseInt(document.getElementById('precio-cant')?.value, 10);
  try {
    await post('/api/compras/precios/registrar', {
      producto_id: p.id,
      proveedor_id: prov,
      precio_unitario: valor,
      cantidad_cotizada: Number.isFinite(cant) ? cant : null,
      cotizado_por: (document.getElementById('precio-quien')?.value || '').trim(),
      fuente: 'COTIZACION',
    });
    alerta('Cotización registrada ✓', 'exito');
    preciosRenderComparador();
  } catch (e) {
    alerta('No se pudo registrar: ' + e.message, 'error');
  }
}


/** Inserta el buscador de precios arriba del listado de acuerdos. */
function _preciosAsegurarBuscador(contenedor) {
  if (document.getElementById('precio-buscar')) return;
  const caja = document.createElement('div');
  caja.className = 'tabla-card';
  caja.style.marginBottom = '12px';
  caja.innerHTML = `
    <div class="tabla-titulo">Precios por producto</div>
    <div style="display:flex;gap:6px;">
      <input id="precio-buscar" placeholder="código o nombre del producto"
             style="flex:1;padding:7px;border-radius:6px;border:1px solid var(--brd);background:var(--bg);color:var(--tx);font-size:12px;"
             onkeydown="if(event.key==='Enter')preciosBuscarProducto()">
      <button class="btn-flota" onclick="preciosBuscarProducto()">Buscar</button>
    </div>
    <div id="precios-panel" style="margin-top:10px;"></div>`;
  contenedor.parentNode.insertBefore(caja, contenedor);
}
