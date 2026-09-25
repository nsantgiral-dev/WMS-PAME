// ══════════════════════════════════════════════════════════════════════════
// Compras — la pantalla del comprador (2026-09-25)
//
// UNA pantalla para el rol `compras` y para el admin, con las mismas pestañas:
// 🛒 Bandeja · 🚢 Contenedor · 🎒 Temporada · 📦 Lo pedido · 🧾 Fuentes ·
// ⚙️ Avanzado (plegado). Arriba, fija, la franja de confianza del dato.
//
// **La pantalla no calcula.** Cada número llega hecho del servidor
// (`/api/compras/bandeja/*`, que a su vez solo compone lo que calcula el
// motor). Acá se formatea y se pone en palabras: sin restas ni sumas de
// cantidades, sin decidir urgencias.
//
// Reglas de la PWA: todo dato pasa por `esc()`; en un `onclick` viajan solo
// posiciones o palabras fijas; colores y tamaños por token; «sin dato» nunca
// es cero.
// ══════════════════════════════════════════════════════════════════════════

const CMP = {
  tab: 'bandeja', bandeja: null, confianza: null, contenedor: null, temporada: null,
  lopedido: null, filtro: null, irA: null, pos: {}, tipos: [], tipo: '40STD',
};

const CMP_TABS = ['bandeja', 'contenedor', 'temporada', 'lopedido', 'fuentes', 'avanzado'];

const CMP_URGENCIA = {
  URGENTE: { texto: 'Urgente', ayuda: 'se agota antes de que llegue', fondo: 'var(--err-bg)', tinta: 'var(--err-tx)', borde: 'var(--err-brd)' },
  ESTA_SEMANA: { texto: 'Esta semana', ayuda: 'ya está bajo su punto de pedido', fondo: 'var(--warn-bg)', tinta: 'var(--warn-tx)', borde: 'var(--warn-brd)' },
  PROXIMAS: { texto: 'Próximas', ayuda: 'cruza su punto de pedido en los próximos días', fondo: 'var(--info-bg)', tinta: 'var(--info-tx)', borde: 'var(--info-brd)' },
};

const CMP_NIVEL = {
  ok: { icono: '✓', tinta: 'var(--ok-tx)', fondo: 'var(--ok-bg)', borde: 'var(--ok-brd)' },
  aviso: { icono: '⚠', tinta: 'var(--warn-tx)', fondo: 'var(--warn-bg)', borde: 'var(--warn-brd)' },
  mal: { icono: '✗', tinta: 'var(--err-tx)', fondo: 'var(--err-bg)', borde: 'var(--err-brd)' },
};

// ── Formatos (presentación, no cálculo) ─────────────────────────────────────

/** Unidades enteras con separador de miles; `null` es «sin dato». */
function cmpN(x) {
  if (x === null || x === undefined || x === '') return 'sin dato';
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return Math.round(n).toLocaleString('es-CO');
}

/** «3 días», «1 día», «menos de 1 día». Días enteros hacia abajo: «alcanza
 *  2 días» cuando alcanza 2,9 es el lado prudente de leerlo. */
function cmpDias(x) {
  if (x === null || x === undefined || x === '') return 'sin dato';
  const n = Math.floor(Number(x));
  if (!Number.isFinite(n)) return String(x);
  if (n < 1) return 'menos de 1 día';
  return n === 1 ? '1 día' : `${n.toLocaleString('es-CO')} días`;
}

/** «25 sept 2026» de un `AAAA-MM-DD` (se lee como día, sin correr zona). */
function cmpFecha(iso) {
  if (!iso) return 'sin fecha';
  const d = new Date(String(iso).slice(0, 10) + 'T12:00:00Z');
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString('es-CO', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

/** Minutos → «hace 20 min», «hace 3 h», «hace 2 días». */
function cmpHace(min) {
  if (min === null || min === undefined) return 'sin dato';
  const m = Number(min);
  if (!Number.isFinite(m)) return 'sin dato';
  if (m < 60) return `hace ${Math.max(0, Math.round(m))} min`;
  if (m < 60 * 48) return `hace ${Math.round(m / 60)} h`;
  return `hace ${Math.round(m / 1440)} días`;
}

/** Un decimal (volumen en m³): «1,1». */
function cmpDec(x) {
  if (x === null || x === undefined || x === '') return 'sin dato';
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return n.toLocaleString('es-CO', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

function cmpPct(x) {
  if (x === null || x === undefined) return 'sin dato';
  return `${Math.round(Number(x) * 100)} %`;
}

/** Valor de una línea en su moneda: la OC en USD va en dólares, nunca como pesos. */
function cmpMonto(valor, moneda) {
  return String(moneda || 'COP').toUpperCase() === 'USD' ? fmtUsd(valor) : fmtPesos(valor);
}

function cmpCaja(titulo, cuerpo, nivel) {
  const c = CMP_NIVEL[nivel] || CMP_NIVEL.aviso;
  return `<div style="border:1px solid ${esc(c.borde)};background:${esc(c.fondo)};border-radius:12px;padding:14px 16px;margin-bottom:14px;">
    <div style="font-size:var(--fs-md);font-weight:700;color:${esc(c.tinta)};margin-bottom:6px;">${esc(c.icono)} ${esc(titulo)}</div>
    <div style="font-size:var(--fs-sm);color:var(--tx);line-height:var(--lh-texto);">${cuerpo}</div>
  </div>`;
}

function cmpCargando(texto) {
  return `<div style="padding:24px;color:var(--tx3);font-size:var(--fs-sm);">${esc(texto)}</div>`;
}

function cmpError(e) {
  return cmpCaja('No se pudo cargar', esc((e && e.message) || e), 'mal');
}

// ── Montaje: una pantalla, dos entradas ─────────────────────────────────────

/** El bloque `#tab-compras` es UNO. El admin lo ve en su panel; el rol
 *  `compras` en su pantalla: se mueve el nodo, no se copia (dos copias de los
 *  mismos ids serían dos pantallas que divergen). */
function cmpMontar(destino) {
  const bloque = document.getElementById('tab-compras');
  if (!bloque) return;
  const host = destino === 'compras'
    ? document.getElementById('compras-host')
    : document.getElementById('tab-compras-ancla');
  if (!host) return;
  if (destino === 'compras') {
    if (bloque.parentNode !== host) host.appendChild(bloque);
    bloque.style.display = 'block';
  } else if (host.parentNode && bloque.previousSibling !== host) {
    host.parentNode.insertBefore(bloque, host.nextSibling);
  }
}

/** Entrada única de la pantalla (admin y rol compras). */
function cmpIniciar() {
  cmpCargarConfianza();
  cmpTab(CMP.tab || 'bandeja');
}

/** @param {string} id - Pestaña principal (palabra fija de CMP_TABS). */
function cmpTab(id) {
  if (!CMP_TABS.includes(id)) id = 'bandeja';
  CMP.tab = id;
  CMP_TABS.forEach(t => {
    const sec = document.getElementById('comp-sec-' + t);
    const btn = document.getElementById('cmp-tab-' + t);
    if (sec) sec.style.display = t === id ? 'block' : 'none';
    if (btn) {
      btn.style.background = t === id ? 'var(--acento-bg)' : 'transparent';
      btn.style.color = t === id ? 'var(--acento-tx)' : 'var(--tx2)';
      btn.style.borderColor = t === id ? 'var(--acento-brd)' : 'var(--brd)';
      btn.style.fontWeight = t === id ? '700' : '500';
    }
  });
  if (id === 'bandeja') return cmpCargarBandeja();
  if (id === 'contenedor') return cmpCargarContenedor();
  if (id === 'temporada') return cmpCargarTemporada();
  if (id === 'lopedido') return cmpCargarLoPedido();
  if (id === 'fuentes') return fuentesCargar();
  if (id === 'avanzado') {
    compCargarResumen('comp');
    return compSubtab(COMP_SUBTAB || 'modelos');
  }
}

// ── Franja de confianza ─────────────────────────────────────────────────────

async function cmpCargarConfianza() {
  const el = document.getElementById('cmp-franja');
  if (!el) return;
  try {
    CMP.confianza = await get('/api/compras/bandeja/confianza');
    el.innerHTML = cmpFranjaHtml(CMP.confianza);
  } catch (e) {
    el.innerHTML = cmpCaja('No se pudo verificar si los datos están al día',
      `${esc(e.message || e)} — no decidir con estos números hasta saberlo.`, 'mal');
  }
}

function cmpSedesTexto(sedes) {
  return (sedes || []).map(s => {
    const cuando = s.sin_dato ? 'sin datos' : cmpHace(s.minutos);
    const tinta = s.sin_dato || s.atrasada ? 'var(--warn-tx)' : 'var(--tx2)';
    return `<span style="color:${tinta};white-space:nowrap;">${esc(s.bodega)} ${esc(cuando)}</span>`;
  }).join(' · ');
}

function cmpFranjaHtml(d) {
  const r = (d && d.renglones) || [];
  const cabeza = d && d.decidir === false
    ? `<div style="font-size:var(--fs-sm);font-weight:700;color:var(--err-tx);margin-bottom:6px;">✗ No decidir con estos números todavía: abajo está qué falta.</div>`
    : `<div style="font-size:var(--fs-sm);font-weight:700;color:var(--ok-tx);margin-bottom:6px;">✓ Los datos para decidir están al día (con los supuestos que se dicen abajo).</div>`;
  const filas = r.map(x => {
    const c = CMP_NIVEL[x.nivel] || CMP_NIVEL.aviso;
    let extra = '';
    if (x.clave === 'existencias' && (x.sedes || []).length) extra = `<div style="margin-top:2px;">${cmpSedesTexto(x.sedes)}</div>`;
    if (x.clave === 'ocs' && x.minutos !== null && x.minutos !== undefined) extra = ` <span style="color:var(--tx2);">(${esc(cmpHace(x.minutos))})</span>`;
    return `<div style="display:flex;gap:8px;align-items:flex-start;padding:4px 0;font-size:var(--fs-sm);">
      <span style="color:${esc(c.tinta)};font-weight:700;min-width:1em;">${esc(c.icono)}</span>
      <div style="min-width:0;overflow-wrap:anywhere;">
        <span style="color:var(--tx);font-weight:600;">${esc(x.titulo)}</span> ${extra}
        ${x.detalle ? `<div style="color:var(--tx2);font-size:var(--fs-xs);">${esc(x.detalle)}</div>` : ''}
        ${x.que_hacer ? `<div style="color:var(--tx3);font-size:var(--fs-xs);">Qué hacer: ${esc(x.que_hacer)}</div>` : ''}
      </div></div>`;
  }).join('');
  return `<details ${d && d.decidir === false ? 'open' : ''} style="border:1px solid var(--brd);background:var(--bg-s);border-radius:12px;padding:10px 14px;margin-bottom:14px;">
    <summary style="cursor:pointer;list-style:none;">${cabeza}</summary>${filas}</details>`;
}

// ── 🛒 Bandeja ──────────────────────────────────────────────────────────────

async function cmpCargarBandeja() {
  const el = document.getElementById('cmp-bandeja');
  if (!el) return;
  if (!CMP.bandeja) el.innerHTML = cmpCargando('Armando la bandeja: punto de pedido, empaques y precios…');
  try {
    CMP.bandeja = await get('/api/compras/bandeja');
    el.innerHTML = cmpBandejaHtml(CMP.bandeja, CMP.filtro);
    cmpIrSiHaceFalta();
  } catch (e) {
    el.innerHTML = cmpError(e);
  }
}

/** @param {string} f - URGENTE | ESTA_SEMANA | PROXIMAS | '' (palabra fija). */
function cmpFiltrar(f) {
  CMP.filtro = f || null;
  const el = document.getElementById('cmp-bandeja');
  if (el && CMP.bandeja) el.innerHTML = cmpBandejaHtml(CMP.bandeja, CMP.filtro);
}

function cmpPildora(urg) {
  const u = CMP_URGENCIA[urg];
  if (!u) return '';
  return `<span title="${esc(u.ayuda)}" style="font-size:var(--fs-xs);font-weight:700;padding:2px 8px;border-radius:999px;background:${esc(u.fondo)};color:${esc(u.tinta)};border:1px solid ${esc(u.borde)};white-space:nowrap;">${esc(u.texto)}</span>`;
}

function cmpSinKardexHtml(d) {
  const lista = (d.falta || []).map(f => `<li style="margin-bottom:6px;"><b>${esc(f.titulo || '')}</b>${f.que_hacer ? `<div style="color:var(--tx2);">${esc(f.que_hacer)}</div>` : ''}</li>`).join('');
  const titulo = d.estado === 'SIN_KARDEX'
    ? 'La bandeja no puede proponer: no hay ventas cargadas (kardex)'
    : 'La bandeja no puede proponer: ningún producto nacional tiene ventas en los últimos 12 meses';
  return cmpCaja(titulo, `Sin ventas no hay con qué saber cuánto se vende al día, y la bandeja no inventa cantidades.
    <ul style="margin:8px 0 0 18px;padding:0;">${lista}</ul>`, 'mal');
}

function cmpPorQueHtml(l) {
  const p = l.porque || {};
  const um = (l.empaque && l.empaque.unidad) || 'UND';
  const partes = [];
  partes.push(`Hay <b>${esc(cmpN(p.existencia))}</b> en las sedes − <b>${esc(cmpN(p.comprometido))}</b> vendidos sin despachar − <b>${esc(cmpN(p.salida_sin_confirmar))}</b> vendidos en tienda sin confirmar = <b>${esc(cmpN(p.disponible))}</b> disponibles de verdad.`);
  partes.push(`Ya pedido (órdenes abiertas y contenedores): <b>${esc(cmpN(p.ya_pedido))}</b>, así que cuenta con <b>${esc(cmpN(p.posicion))}</b>.`);
  partes.push(`Vende unas <b>${esc(cmpN(p.vende_dia))}</b> al día. La entrega tarda <b>${esc(cmpDias(p.entrega_dias))}</b> (${esc(p.entrega_fuente || 'sin fuente')}) y se vuelve a comprar cada <b>${esc(cmpDias(p.ciclo_dias))}</b>.`);
  partes.push(`Punto de pedido: <b>${esc(cmpN(p.punto_de_pedido))}</b> (incluye <b>${esc(cmpN(p.reserva_seguridad))}</b> de reserva de seguridad para tener existencias el ${esc(cmpPct(p.meta_servicio))} de los días).`);
  partes.push(`Cantidad a tener: <b>${esc(cmpN(p.nivel_objetivo))}</b>. Le faltan <b>${esc(cmpN(p.falta_para_objetivo))}</b>; se piden <b>${esc(cmpN(l.pedir_unidades))}</b>${p.redondeo_empaque ? ` (${esc(cmpN(p.redondeo_empaque))} de más para completar el empaque de ${esc(cmpN(l.empaque && l.empaque.unidades_por_empaque))} por ${esc(um)})` : ''}.`);
  if (p.faltan_datos_de_agotados) partes.push(`<span style="color:var(--warn-tx);">Le faltan datos de agotados: no se sabe qué días estuvo sin existencias, y la venta diaria puede estar por debajo de la real.</span>`);
  if (p.existencias_de_hace_dias) partes.push(`<span style="color:var(--warn-tx);">Las existencias de este producto son de hace ${esc(cmpDias(p.existencias_de_hace_dias))}.</span>`);
  if (l.empaque && !l.empaque.moq_conocido) partes.push(`<span style="color:var(--tx3);">El pedido mínimo del proveedor no está en ninguna fuente: confirmalo al pedir.</span>`);
  return partes.map(x => `<div style="margin-bottom:4px;">${x}</div>`).join('');
}

function cmpLineaHtml(l, i, j) {
  const em = l.empaque || {};
  const pr = l.precio || {};
  const cantidad = em.unidades_por_empaque > 1
    ? `${esc(cmpN(l.pedir_empaques))} ${esc(em.unidad)} de ${esc(cmpN(em.unidades_por_empaque))}`
    : 'por unidad';
  const precio = pr.unitario_cop === null || pr.unitario_cop === undefined
    ? `<span style="color:var(--warn-tx);">Sin precio conocido: cotizar antes de pedir</span>`
    : `${esc(fmtPesos(pr.unitario_cop))} c/u <span style="color:var(--tx3);">(${esc(pr.fuente)}${pr.viejo ? ', de hace más de 6 meses' : ''}${pr.convertido_de_usd ? ', convertido de dólares' : ''})</span> · <b>${esc(fmtPesos(l.valor_cop))}</b>`;
  const u = CMP_URGENCIA[l.urgencia] || CMP_URGENCIA.PROXIMAS;
  return `<div id="cmp-l-${i}-${j}" class="cmp-linea" style="border:1px solid var(--brd);border-left:4px solid ${esc(u.borde)};border-radius:10px;padding:10px 12px;background:var(--bg);">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;flex-wrap:wrap;">
      <div style="min-width:0;flex:1 1 200px;">
        <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);overflow-wrap:anywhere;">${esc(l.nombre || 'Producto sin nombre en el WMS')}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(l.referencia)}</div>
      </div>
      ${cmpPildora(l.urgencia)}
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:4px 16px;align-items:baseline;margin-top:6px;">
      <span style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);">Pedir ${esc(cmpN(l.pedir_unidades))}</span>
      <span style="font-size:var(--fs-sm);color:var(--tx2);">${cantidad}</span>
      <span style="font-size:var(--fs-sm);color:var(--tx2);">Alcanza ${esc(cmpDias(l.alcanza_dias))} · llega en ${esc(cmpDias(l.entrega_dias))} (${esc(cmpFecha(l.fecha_entrega_sugerida))})</span>
    </div>
    <div style="font-size:var(--fs-sm);color:var(--tx);margin-top:4px;">${precio}</div>
    <details id="cmp-pq-${i}-${j}" style="margin-top:6px;">
      <summary style="cursor:pointer;font-size:var(--fs-sm);color:var(--acento-tx);font-weight:600;min-height:44px;display:flex;align-items:center;">¿Por qué esta cantidad?</summary>
      <div style="font-size:var(--fs-sm);color:var(--tx2);line-height:var(--lh-texto);padding:6px 0 2px;">${cmpPorQueHtml(l)}</div>
    </details>
  </div>`;
}

function cmpProveedorHtml(p, i, filtro) {
  const lineas = (p.lineas || []).map((l, j) => [l, j]).filter(([l]) => !filtro || l.urgencia === filtro);
  if (!lineas.length) return '';
  const nombre = p.conocido ? (p.nombre || `Proveedor ${p.codigo}`) : 'Sin proveedor conocido';
  const sub = p.subtotal_cop === null
    ? 'sin precio en ninguna línea'
    : `${fmtPesos(p.subtotal_cop)}${p.subtotal_es_cota_inferior ? ` al menos (${cmpN(p.lineas_sin_precio)} sin precio)` : ''}`;
  const botones = p.conocido
    ? `<button onclick="cmpCopiarOc(${i})" style="min-height:44px;padding:8px 14px;border-radius:8px;border:1px solid var(--acento-brd);background:var(--acento-bg);color:var(--acento-tx);font-size:var(--fs-sm);font-weight:700;cursor:pointer;">Copiar OC</button>
       <button onclick="cmpExportarCsv(${i})" style="min-height:44px;padding:8px 14px;border-radius:8px;border:1px solid var(--brd);background:var(--bg-s);color:var(--tx);font-size:var(--fs-sm);font-weight:600;cursor:pointer;">Exportar CSV</button>`
    : `<span style="font-size:var(--fs-xs);color:var(--tx3);">Sin órdenes de compra de estos productos: elegí a quién pedirles.</span>`;
  return `<section style="border:1px solid var(--brd);background:var(--bg-s);border-radius:14px;padding:12px;margin-bottom:14px;">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;">
      <div style="min-width:0;flex:1 1 220px;">
        <div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);overflow-wrap:anywhere;">${esc(nombre)}</div>
        <div style="font-size:var(--fs-xs);color:var(--tx2);">${p.nit ? `NIT ${esc(p.nit)} · ` : ''}${esc(cmpN((p.lineas || []).length))} producto${(p.lineas || []).length === 1 ? '' : 's'} · ${esc(sub)}${p.urgentes ? ` · <span style="color:var(--err-tx);font-weight:700;">${esc(cmpN(p.urgentes))} urgente${p.urgentes === 1 ? '' : 's'}</span>` : ''}</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">${botones}</div>
    </div>
    <div style="display:flex;flex-direction:column;gap:8px;">${lineas.map(([l, j]) => cmpLineaHtml(l, i, j)).join('')}</div>
  </section>`;
}

function cmpExcluidosHtml(d) {
  const x = d.excluidos || {};
  const r = d.resumen || {};
  const partes = [];
  if ((x.bloqueados || []).length) {
    partes.push(`<div style="margin-bottom:6px;"><b>Bloqueados para recompra (${esc(cmpN(x.bloqueados.length))}):</b> ${x.bloqueados.map(b => `${esc(b.nombre || b.referencia)} <span style="color:var(--tx3);">${esc(b.referencia)}</span>`).join(' · ')}. Se desbloquean en ⚙️ Avanzado › Bloqueos.</div>`);
  }
  if ((x.sin_existencias_conocidas || []).length) {
    partes.push(`<div style="margin-bottom:6px;"><b>Sin existencias conocidas (${esc(cmpN(x.sin_existencias_conocidas.length))}):</b> Siesa no reporta existencias de ${x.sin_existencias_conocidas.slice(0, 12).map(s => esc(s.referencia)).join(', ')}${x.sin_existencias_conocidas.length > 12 ? '…' : ''}. No se propone cantidad sin saber cuánto hay.</div>`);
  }
  if ((x.sin_venta_reciente || []).length) {
    partes.push(`<div style="margin-bottom:6px;"><b>Dejaron de venderse (${esc(cmpN(x.sin_venta_reciente.length))}):</b> ${x.sin_venta_reciente.slice(0, 12).map(s => esc(s.referencia)).join(', ')}. No se reponen: revisar si se descontinuaron.</div>`);
  }
  if (r.china_van_por_contenedor) {
    partes.push(`<div><b>${esc(cmpN(r.china_van_por_contenedor))} productos de China</b> van por <a href="#" onclick="cmpTab('contenedor');return false;" style="color:var(--acento-tx);">🚢 Contenedor</a>.</div>`);
  }
  if (!partes.length) return '';
  return `<details style="border:1px solid var(--brd);border-radius:12px;padding:10px 14px;background:var(--bg-s);font-size:var(--fs-sm);color:var(--tx2);">
    <summary style="cursor:pointer;font-weight:700;color:var(--tx);">Lo que la bandeja no propone, y por qué</summary>
    <div style="margin-top:8px;line-height:var(--lh-texto);">${partes.join('')}</div></details>`;
}

function cmpBandejaHtml(d, filtro) {
  if (!d) return '';
  if (d.estado !== 'OK') return cmpSinKardexHtml(d);
  const r = d.resumen || {};
  const provs = d.proveedores || [];
  CMP.pos = {};
  provs.forEach((p, i) => (p.lineas || []).forEach((l, j) => { CMP.pos[l.referencia] = [i, j]; }));
  const valor = r.valor_total_cop === null
    ? 'sin precio en ninguna línea'
    : `${fmtPesos(r.valor_total_cop)}${r.valor_es_cota_inferior ? ` al menos (${cmpN(r.lineas_sin_precio)} sin precio)` : ''}`;
  const chip = (clave, texto, n) => {
    const activo = (filtro || '') === clave;
    return `<button onclick="cmpFiltrar('${clave}')" style="min-height:44px;padding:6px 12px;border-radius:999px;border:1px solid ${activo ? 'var(--acento-brd)' : 'var(--brd)'};background:${activo ? 'var(--acento-bg)' : 'transparent'};color:${activo ? 'var(--acento-tx)' : 'var(--tx2)'};font-size:var(--fs-sm);font-weight:${activo ? '700' : '500'};cursor:pointer;">${esc(texto)} ${esc(cmpN(n))}</button>`;
  };
  const ciclo = d.ciclo || {};
  const cab = `<div style="margin-bottom:12px;">
    <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);">${r.lineas ? `${esc(cmpN(r.lineas))} producto${r.lineas === 1 ? '' : 's'} para pedir a ${esc(cmpN(r.proveedores))} proveedor${r.proveedores === 1 ? '' : 'es'}` : 'Nada para pedir a proveedores nacionales hoy'}</div>
    ${r.lineas ? `<div style="font-size:var(--fs-sm);color:var(--tx2);">Valor: ${esc(valor)}</div>` : ''}
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:4px;">Meta: tener existencias el ${esc(cmpPct(d.nivel_servicio))} de los días · se compra cada ${esc(cmpDias(ciclo.dias))}${ciclo.fuente === 'CONFIGURADO' ? '' : ' (supuesto)'} · entrega nacional ${esc(cmpDias((d.entrega_nacional || {}).dias))} (${esc((d.entrega_nacional || {}).fuente || 'sin fuente')}) · destino ${esc((d.destino || {}).bodega || '')} (CO ${esc((d.destino || {}).co || 'sin dato')})</div>
    ${d.kardex_confiable ? '' : `<div style="font-size:var(--fs-sm);color:var(--err-tx);font-weight:700;margin-top:6px;">✗ Las ventas (kardex) no están al día: las cantidades de abajo pueden estar mal. Ver la franja de arriba.</div>`}
  </div>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;">
    ${chip('', 'Todo', r.lineas)}${chip('URGENTE', 'Urgentes', r.urgentes)}${chip('ESTA_SEMANA', 'Esta semana', r.esta_semana)}${chip('PROXIMAS', 'Próximas', r.proximas)}
  </div>
  <div id="cmp-aviso-ir"></div>`;
  const cuerpo = provs.map((p, i) => cmpProveedorHtml(p, i, filtro)).join('')
    || `<div style="padding:16px;color:var(--tx3);font-size:var(--fs-sm);">Ningún producto en este filtro.</div>`;
  return cab + cuerpo + cmpExcluidosHtml(d);
}

// ── Borrador de OC (copiar / CSV) ───────────────────────────────────────────

/** Las filas del borrador de OC de un proveedor, sin decidir nada: cada
 *  número es el que mandó el servidor. El formato de importación a Siesa NO
 *  está especificado: esto es un borrador a confirmar con el consultor. */
function cmpOcFilas(d, i) {
  const p = ((d && d.proveedores) || [])[i];
  if (!p) return null;
  const dest = d.destino || {};
  return {
    proveedor: p,
    encabezado: ['nit_proveedor', 'proveedor', 'co', 'bodega', 'referencia', 'descripcion',
      'unidad_medida', 'cantidad', 'unidades_base', 'precio_unitario_cop', 'valor_cop',
      'fecha_entrega', 'urgencia'],
    filas: (p.lineas || []).map(l => [
      p.nit || '', p.nombre || '', dest.co || '', dest.bodega || '', l.referencia, l.nombre || '',
      (l.empaque && l.empaque.unidad) || 'UND', l.pedir_empaques, l.pedir_unidades,
      (l.precio && l.precio.unitario_cop) ?? '', l.valor_cop ?? '', l.fecha_entrega_sugerida || '',
      (CMP_URGENCIA[l.urgencia] || {}).texto || '',
    ]),
  };
}

/** CSV con `;` (Excel en español) y comillas donde haga falta. */
function cmpCsv(encabezado, filas) {
  const celda = v => {
    const s = v === null || v === undefined ? '' : String(v);
    return /[;"\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return [encabezado, ...filas].map(f => f.map(celda).join(';')).join('\n');
}

function cmpOcTexto(oc) {
  const p = oc.proveedor;
  const cab = `BORRADOR DE ORDEN DE COMPRA — formato de importación a Siesa por confirmar con el consultor\nProveedor: ${p.nombre || ''} · NIT ${p.nit || 'sin dato'}\n`;
  return cab + oc.encabezado.join('\t') + '\n' + oc.filas.map(f => f.join('\t')).join('\n');
}

async function cmpCopiarOc(i) {
  const oc = cmpOcFilas(CMP.bandeja, Number(i));
  if (!oc) return;
  const texto = cmpOcTexto(oc);
  try {
    await navigator.clipboard.writeText(texto);
    alerta('Borrador de OC copiado: pegalo en Siesa o en el correo al proveedor', 'exito');
  } catch (e) {
    alerta('No se pudo copiar: usá «Exportar CSV»', 'error');
  }
}

function cmpDescargar(nombre, contenido) {
  const blob = new Blob(['﻿' + contenido], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = nombre;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function cmpExportarCsv(i) {
  const oc = cmpOcFilas(CMP.bandeja, Number(i));
  if (!oc) return;
  const nombre = `oc_borrador_${String(oc.proveedor.codigo || 'proveedor').replace(/[^\w-]/g, '')}.csv`;
  cmpDescargar(nombre, cmpCsv(oc.encabezado, oc.filas));
}

// ── Ir a una referencia (desde Analítica) ───────────────────────────────────

/** Marca una referencia para abrir la Bandeja en su fila (desde Analítica).
 *  No carga nada: la carga la hace la pestaña al abrirse (`cmpIniciar`), y
 *  si no está en la bandeja se dice por qué. */
function comprasIrABandeja(ref) {
  CMP.irA = ref || null;
  CMP.filtro = null;
  CMP.tab = 'bandeja';
}

async function cmpIrSiHaceFalta() {
  const ref = CMP.irA;
  if (!ref) return;
  CMP.irA = null;
  const aviso = document.getElementById('cmp-aviso-ir');
  const pos = CMP.pos[ref];
  if (pos) {
    const fila = document.getElementById(`cmp-l-${pos[0]}-${pos[1]}`);
    const pq = document.getElementById(`cmp-pq-${pos[0]}-${pos[1]}`);
    if (pq) pq.open = true;
    if (fila) {
      fila.style.boxShadow = '0 0 0 3px var(--acento-brd)';
      if (fila.scrollIntoView) fila.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    return;
  }
  if (!aviso) return;
  try {
    const r = await get('/api/compras/bandeja/sku?referencia=' + encodeURIComponent(ref));
    const extra = r.motivo === 'SOBRE_PUNTO_DE_PEDIDO' && r.dias_hasta_punto_de_pedido !== null
      ? ` Cruza su punto de pedido en unos ${esc(cmpDias(r.dias_hasta_punto_de_pedido))}.` : '';
    aviso.innerHTML = cmpCaja(`${ref} no está en la bandeja`, `${esc(r.texto || '')}${extra}`, 'aviso');
  } catch (e) {
    aviso.innerHTML = cmpCaja(`${ref} no está en la bandeja`, esc(e.message || e), 'aviso');
  }
}

// ── 🚢 Contenedor ───────────────────────────────────────────────────────────

/** @param {number} [k] - Posición del tipo de contenedor en CMP.tipos. */
async function cmpCargarContenedor(k) {
  const el = document.getElementById('cmp-contenedor');
  if (!el) return;
  if (k !== undefined && CMP.tipos[Number(k)]) CMP.tipo = CMP.tipos[Number(k)].tipo;
  el.innerHTML = cmpCargando('Calculando el contenedor…');
  try {
    CMP.contenedor = await get('/api/compras/bandeja/contenedor?tipo=' + encodeURIComponent(CMP.tipo));
    CMP.tipos = CMP.contenedor.tipos || [];
    el.innerHTML = cmpContenedorHtml(CMP.contenedor);
  } catch (e) {
    el.innerHTML = cmpError(e);
  }
}

function cmpBarra(etiqueta, actual, total, pct) {
  const ancho = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div style="border:1px solid var(--brd);border-radius:10px;padding:10px;background:var(--bg-s);">
    <div style="display:flex;justify-content:space-between;font-size:var(--fs-sm);margin-bottom:4px;gap:6px;flex-wrap:wrap;">
      <span style="color:var(--tx2);">${esc(etiqueta)}</span><span style="color:var(--tx);">${esc(actual)} de ${esc(total)} (${esc(cmpN(pct))} %)</span></div>
    <div style="height:10px;background:var(--bg);border-radius:6px;overflow:hidden;"><div style="height:100%;width:${ancho}%;background:var(--pm-fill);"></div></div></div>`;
}

function cmpTiposHtml(d) {
  return `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;">${(d.tipos || []).map((t, k) => {
    const activo = t.tipo === d.tipo;
    return `<button onclick="cmpCargarContenedor(${k})" style="min-height:44px;padding:6px 12px;border-radius:8px;border:1px solid ${activo ? 'var(--acento-brd)' : 'var(--brd)'};background:${activo ? 'var(--acento-bg)' : 'transparent'};color:${activo ? 'var(--acento-tx)' : 'var(--tx2)'};font-size:var(--fs-sm);cursor:pointer;">${esc(t.etiqueta)} · ${esc(cmpN(t.cbm_util))} m³</button>`;
  }).join('')}</div>`;
}

function cmpContenedorHtml(d) {
  if (!d) return '';
  const irFuentes = `<button onclick="cmpTab('fuentes')" style="min-height:44px;margin-top:10px;padding:8px 14px;border-radius:8px;border:1px solid var(--acento-brd);background:var(--acento-bg);color:var(--acento-tx);font-size:var(--fs-sm);font-weight:700;cursor:pointer;">Ir a 🧾 Fuentes</button>`;
  if (d.estado === 'SIN_ORIGEN') {
    return cmpCaja(d.titulo || 'No se puede calcular el contenedor',
      `<div style="font-size:var(--fs-lg);font-weight:800;margin:4px 0;">${esc(cmpN(d.skus_sin_origen))} productos sin origen</div>
       Sin saber qué se trae de China no hay con qué decir qué pedir en el contenedor, y esta pantalla no muestra una propuesta que parezca real.
       <div>${esc(d.que_hacer || '')}</div>${irFuentes}`, 'mal');
  }
  if (d.estado === 'SIN_FICHAS') {
    return cmpTiposHtml(d) + cmpCaja(d.titulo || 'Faltan fichas de importación',
      `Sin unidades por caja, CBM y peso no se puede armar el contenedor.
       <div style="margin-top:6px;color:var(--tx2);">${(d.sin_ficha_refs || []).slice(0, 15).map(r => esc(r)).join(', ')}${(d.sin_ficha_refs || []).length > 15 ? '…' : ''}</div>
       <div>${esc(d.que_hacer || '')}</div>${irFuentes}`, 'mal');
  }
  if (d.estado === 'SIN_FALTANTE') {
    return cmpTiposHtml(d) + cmpCaja('Hoy no hace falta contenedor', esc(d.titulo || ''), 'ok');
  }
  const b = d.barras || {};
  const v = d.ventana_llegada || {};
  const lt = d.lead_time || {};
  let html = cmpTiposHtml(d);
  if (d.modo === 'SHADOW') {
    html += cmpCaja('Borrador de prueba', `Las fichas cubren el ${esc(cmpN(d.cobertura_fichas_pct))} % de los productos de China: revisalo antes de pedir.`, 'aviso');
  }
  if (d.sin_ficha) {
    html += cmpCaja(`${cmpN(d.sin_ficha)} productos con faltante quedaron fuera por falta de ficha`,
      `${(d.sin_ficha_refs || []).slice(0, 12).map(r => esc(r)).join(', ')}. ${irFuentes}`, 'aviso');
  }
  if (d.alerta_temporada) {
    const a = d.alerta_temporada;
    html += cmpCaja(a.titulo, `Llegaría entre el ${esc(cmpFecha(v.desde))} y el ${esc(cmpFecha(v.hasta))}; la temporada empieza el ${esc(cmpFecha(a.temporada_inicio))}. ${a.vencida ? 'La fecha para pedir de China a tiempo era' : 'Para llegar a tiempo hay que pedir antes del'} ${esc(cmpFecha(a.fecha_limite_pedido))}.`, 'mal');
  }
  html += `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,220px),1fr));gap:8px;margin-bottom:12px;">
    ${cmpBarra('Volumen (m³)', cmpDec(b.cbm_acumulado), cmpDec(b.cbm_objetivo), b.cbm_pct)}
    ${cmpBarra('Peso (kg)', cmpN(b.peso_acumulado), cmpN(b.peso_limite), b.peso_pct)}
  </div>
  <div style="font-size:var(--fs-sm);color:var(--tx);margin-bottom:12px;line-height:var(--lh-texto);">
    Valor en origen (FOB): <b>${esc(fmtUsd(d.valor_fob_usd))}</b> · puesto en bodega, estimado: <b>${esc(fmtPesos(d.valor_nacionalizado_cop))}</b><br>
    Llegaría entre el <b>${esc(cmpFecha(v.desde))}</b> y el <b>${esc(cmpFecha(v.hasta))}</b> (de China tarda unos ${esc(cmpDias(lt.dias))} ± ${esc(cmpDias(lt.variacion_dias))}; ${esc(lt.fuente || 'sin fuente')})
  </div>
  <div style="display:flex;justify-content:flex-end;margin-bottom:8px;">
    <button onclick="cmpExportarPackingList()" style="min-height:44px;padding:8px 14px;border-radius:8px;border:1px solid var(--brd);background:var(--bg-s);color:var(--tx);font-size:var(--fs-sm);font-weight:600;cursor:pointer;">Exportar packing list</button></div>`;
  (d.proveedores || []).forEach(g => {
    html += `<section style="border:1px solid var(--brd);background:var(--bg-s);border-radius:14px;padding:12px;margin-bottom:12px;">
      <div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);">${esc(g.proveedor || 'Sin proveedor en la ficha')}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:8px;">${esc(cmpN(g.cajas))} cajas · ${esc(cmpN(g.unidades))} unidades · ${esc(cmpDec(g.cbm))} m³ · ${esc(fmtUsd(g.fob_usd))}${g.lineas_sin_fob ? ` al menos (${esc(cmpN(g.lineas_sin_fob))} sin precio FOB)` : ''}</div>
      ${(g.lineas || []).map(l => `<div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;border-top:1px solid var(--brd);padding:8px 0;font-size:var(--fs-sm);">
        <div style="min-width:0;flex:1 1 200px;"><div style="color:var(--tx);font-weight:600;overflow-wrap:anywhere;">${esc(l.nombre || 'Producto sin nombre')}${l.tipo === 'RELLENO' ? ' <span style="font-size:var(--fs-xs);color:var(--info-tx);">relleno</span>' : ''}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(l.referencia)}</div></div>
        <div style="color:var(--tx2);text-align:right;">${esc(cmpN(l.unidades))} u · ${esc(cmpN(l.cajas))} cajas de ${esc(cmpN(l.unidades_por_caja))} · ${esc(cmpDec(l.cbm))} m³<br><b style="color:var(--tx);">${esc(fmtUsd(l.costo_fob_usd))}</b>${l.costo_fob_usd_unidad !== null && l.costo_fob_usd_unidad !== undefined ? ` <span style="color:var(--tx3);">(${esc(fmtUsd(l.costo_fob_usd_unidad))} c/u)</span>` : ''}</div>
      </div>`).join('')}
    </section>`;
  });
  return html;
}

function cmpPackingFilas(d) {
  const filas = [];
  ((d && d.proveedores) || []).forEach(g => (g.lineas || []).forEach(l => filas.push([
    g.proveedor || '', l.referencia, l.nombre || '', l.cajas, l.unidades, l.unidades_por_caja,
    l.cbm, l.peso_kg, l.costo_fob_usd_unidad ?? '', l.costo_fob_usd ?? '', l.tipo === 'RELLENO' ? 'relleno' : 'faltante',
  ])));
  return filas;
}

function cmpExportarPackingList() {
  if (!CMP.contenedor || CMP.contenedor.estado !== 'PROPUESTA') return;
  const enc = ['proveedor', 'referencia', 'descripcion', 'cajas', 'unidades', 'unidades_por_caja',
    'cbm', 'peso_kg', 'fob_usd_unidad', 'fob_usd', 'motivo'];
  cmpDescargar(`packing_list_${CMP.contenedor.tipo || 'contenedor'}.csv`, cmpCsv(enc, cmpPackingFilas(CMP.contenedor)));
}

// ── 🎒 Temporada ────────────────────────────────────────────────────────────

async function cmpCargarTemporada() {
  const el = document.getElementById('cmp-temporada');
  if (!el) return;
  el.innerHTML = cmpCargando('Calculando el pedido de temporada…');
  try {
    CMP.temporada = await get('/api/compras/bandeja/temporada');
    el.innerHTML = cmpTemporadaHtml(CMP.temporada);
  } catch (e) {
    el.innerHTML = cmpError(e);
  }
}

function cmpLimiteTexto(o, nombre) {
  if (!o) return '';
  const cuando = o.vencida
    ? `<span style="color:var(--err-tx);font-weight:700;">la fecha para pedir era el ${esc(cmpFecha(o.fecha_limite))} (ya pasó)</span>`
    : `pedir antes del <b>${esc(cmpFecha(o.fecha_limite))}</b> (faltan ${esc(cmpDias(o.dias_restantes))})`;
  return `<div>${esc(nombre)}: ${cuando} <span style="color:var(--tx3);">· tarda ${esc(cmpDias(o.lt_dias))} ± ${esc(cmpDias(o.sigma_lt))}</span></div>`;
}

function cmpTemporadaHtml(d) {
  if (!d) return '';
  const t = d.temporada || {};
  const po = t.por_origen || {};
  const cab = `<div style="border:1px solid var(--brd);background:var(--bg-s);border-radius:12px;padding:12px 14px;margin-bottom:12px;font-size:var(--fs-sm);color:var(--tx);line-height:var(--lh-texto);">
    <div style="font-size:var(--fs-md);font-weight:800;">Temporada escolar ${esc(t.temporada || '')} · ${t.en_curso ? 'en curso' : `empieza el ${esc(cmpFecha(t.inicio))}`}</div>
    ${cmpLimiteTexto(po.NACIONAL, 'Nacional')}${cmpLimiteTexto(po.CHINA, 'China')}
    <div style="font-size:var(--fs-xs);color:var(--tx3);">Fecha límite = inicio de la temporada − (lo que tarda en llegar + su variación).</div></div>`;
  if (d.estado !== 'OK') return cab + cmpCaja('Todavía no hay pedido de temporada', esc(d.titulo || ''), 'aviso');
  const filas = (d.filas || []).map(f => {
    const conc = f.contenedor
      ? `<div style="font-size:var(--fs-xs);color:var(--warn-tx);">El contenedor propone ${esc(cmpN(f.contenedor.en_contenedor_unidades))}; manda la temporada (${esc(cmpN(f.contenedor.cifra))}): ${f.contenedor.ajuste_contenedor_unidades >= 0 ? 'sumale' : 'quitale'} ${esc(cmpN(Math.abs(f.contenedor.ajuste_contenedor_unidades)))} al contenedor.</div>` : '';
    const notas = [f.faltan_datos_de_agotados ? 'le faltan datos de agotados' : '', f.una_sola_temporada ? 'una sola temporada de historia: incertidumbre alta' : '']
      .filter(Boolean).map(esc).join(' · ');
    return `<tr style="border-top:1px solid var(--brd);">
      <td style="padding:8px;min-width:180px;"><div style="color:var(--tx);font-weight:600;">${esc(f.nombre || 'Producto sin nombre')}</div><div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(f.referencia)}${f.origen ? ` · ${esc(f.origen === 'CHINA' ? 'China' : f.origen === 'NACIONAL' ? 'nacional' : f.origen.toLowerCase())}` : ''}</div>${conc}${notas ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">${notas}</div>` : ''}</td>
      <td style="padding:8px;text-align:right;">${esc(cmpN(f.tener))}</td>
      <td style="padding:8px;text-align:right;">${f.hay_sin_dato ? '<span style="color:var(--warn-tx);">sin dato</span>' : esc(cmpN(f.hay))}</td>
      <td style="padding:8px;text-align:right;">${esc(cmpN(f.viene))}</td>
      <td style="padding:8px;text-align:right;font-weight:800;color:var(--tx);">${esc(cmpN(f.pedir))}</td>
      <td style="padding:8px;text-align:right;">${esc(fmtPesos(f.inversion_cop))}<div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(f.precio_fuente || '')}</div></td>
    </tr>`;
  }).join('');
  return cab + `<div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);margin-bottom:4px;">Pedir ${esc(cmpN(d.total_pedir_unidades))} unidades · ${esc(fmtPesos(d.total_inversion_cop))}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">Cubre ${esc(cmpN(d.cubiertos))} de ${esc(cmpN(d.skus_temporada))} productos de temporada. ${esc(d.advertencia_cobertura || '')} ${esc(d.nota_hay || '')}</div>
    ${d.nota_conciliacion ? `<div style="font-size:var(--fs-sm);color:var(--warn-tx);margin-bottom:8px;">${esc(d.nota_conciliacion)}</div>` : ''}
    <div style="overflow-x:auto;border:1px solid var(--brd);border-radius:12px;background:var(--bg-s);">
    <table style="width:100%;border-collapse:collapse;font-size:var(--fs-sm);color:var(--tx2);">
      <thead><tr style="color:var(--tx3);font-size:var(--fs-xs);text-align:right;">
        <th style="text-align:left;padding:8px;">Producto</th><th style="padding:8px;">Tener</th><th style="padding:8px;">Hay</th>
        <th style="padding:8px;">Viene</th><th style="padding:8px;">Pedir</th><th style="padding:8px;">Inversión</th></tr></thead>
      <tbody>${filas}</tbody></table></div>`;
}

/** El instrumento del comité (lista paralela, escenarios, acta) se carga al abrirlo. */
function cmpAbrirComite(el) {
  if (el && el.open && !el.dataset.cargado) {
    el.dataset.cargado = '1';
    temporadaCargar();
  }
}

// ── 📦 Lo pedido ────────────────────────────────────────────────────────────

async function cmpCargarLoPedido() {
  const el = document.getElementById('cmp-lopedido');
  if (!el) return;
  el.innerHTML = cmpCargando('Trayendo las órdenes abiertas y lo que llegó…');
  try {
    CMP.lopedido = await get('/api/compras/bandeja/lo-pedido');
    el.innerHTML = cmpLoPedidoHtml(CMP.lopedido);
    const der = document.getElementById('comp-sec-deriva');
    if (der) {
      if (CMP.lopedido.deriva) _renderDeriva(der, CMP.lopedido.deriva);
      else der.innerHTML = `<div style="font-size:var(--fs-sm);color:var(--warn-tx);">No se pudo comparar precios contra los acuerdos.</div>`;
    }
  } catch (e) {
    el.innerHTML = cmpError(e);
  }
}

function cmpOcHtml(o) {
  const estado = o.atrasada
    ? `<span style="color:var(--err-tx);font-weight:700;">atrasada ${esc(cmpDias(o.dias_atraso))}</span>`
    : (o.fecha_entrega ? `entrega el ${esc(cmpFecha(o.fecha_entrega))}` : '<span style="color:var(--warn-tx);">sin fecha de entrega</span>');
  return `<div style="border-top:1px solid var(--brd);padding:8px 0;">
    <div style="font-size:var(--fs-sm);color:var(--tx);"><b>${esc(o.oc)}</b> · pedida el ${esc(cmpFecha(o.fecha_oc))} · ${estado}${o.valor_pendiente !== null && o.valor_pendiente !== undefined ? ` · falta ${esc(cmpMonto(o.valor_pendiente, o.moneda))}${o.valor_es_cota_inferior ? ' al menos' : ''}` : ''}</div>
    ${(o.lineas || []).map(l => `<div style="font-size:var(--fs-xs);color:var(--tx2);padding-left:10px;">${esc(l.nombre || 'Producto sin nombre en el WMS')} <span style="color:var(--tx3);">${esc(l.referencia)}</span> · falta ${l.pendiente_unidades === null ? 'sin dato (sin unidad base)' : esc(cmpN(l.pendiente_unidades))}${l.atrasada ? ' · <span style="color:var(--err-tx);">atrasada</span>' : ''}</div>`).join('')}
  </div>`;
}

function cmpLoPedidoHtml(d) {
  if (!d) return '';
  let html = (d.errores || []).map(e => cmpCaja('Una parte no se pudo leer', esc(e), 'aviso')).join('');
  const o = d.ocs;
  if (o) {
    const f = o.frescura || {};
    html += `<div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);margin-bottom:4px;">Órdenes de compra abiertas: ${esc(cmpN(o.total_ocs))}${o.atrasadas ? ` · <span style="color:var(--err-tx);">${esc(cmpN(o.atrasadas))} atrasadas</span>` : ''}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:8px;">${f.completa_utc ? `Sincronizadas con Siesa ${esc(cmpHace(f.completa_hace_min))}.` : esc(f.nota || 'Nunca sincronizadas con Siesa.')}${o.sin_fecha_entrega ? ` · ${esc(cmpN(o.sin_fecha_entrega))} líneas sin fecha de entrega` : ''}${o.lineas_sin_unidad_base ? ` · ${esc(cmpN(o.lineas_sin_unidad_base))} líneas sin unidad base (no se suman)` : ''}</div>`;
    html += (o.proveedores || []).map(p => `<section style="border:1px solid var(--brd);background:var(--bg-s);border-radius:12px;padding:10px 12px;margin-bottom:10px;">
      <div style="font-size:var(--fs-sm);font-weight:800;color:var(--tx);">${esc(p.nombre || `Proveedor ${p.codigo || ''}`)}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx2);">${p.nit ? `NIT ${esc(p.nit)} · ` : ''}${esc(cmpN((p.ocs || []).length))} órdenes${p.atrasadas ? ` · <span style="color:var(--err-tx);font-weight:700;">${esc(cmpN(p.atrasadas))} atrasadas</span>` : ''}</div>
      ${(p.ocs || []).map(cmpOcHtml).join('')}</section>`).join('')
      || `<div style="font-size:var(--fs-sm);color:var(--tx3);margin-bottom:12px;">No hay órdenes abiertas en el espejo.</div>`;
  }
  const ll = d.llegadas;
  if (ll) {
    html += `<div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);margin:14px 0 6px;">Llegó en los últimos ${esc(cmpN(ll.dias))} días</div>`;
    html += (ll.recepciones || []).map(r => `<div style="font-size:var(--fs-sm);color:var(--tx2);border-top:1px solid var(--brd);padding:6px 0;">${esc(cmpFecha(r.dia))} · <b style="color:var(--tx);">${esc(r.proveedor_nombre || 'Proveedor sin nombre')}</b> · OC ${esc(r.oc || 'sin dato')} · ${esc(cmpN(r.lineas))} líneas, ${esc(cmpN(r.unidades))} u${r.parcial ? ' · <span style="color:var(--warn-tx);">parcial</span>' : ''}</div>`).join('')
      || `<div style="font-size:var(--fs-sm);color:var(--tx3);">No se confirmó ninguna recepción en ese tiempo.</div>`;
  }
  html += `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin:18px 0 6px;">
    <div style="font-size:var(--fs-md);font-weight:800;color:var(--tx);">¿Las órdenes respetan los acuerdos de precio?</div>
    <div style="display:flex;gap:6px;">${[3, 6, 12].map(m => `<button onclick="compCargarDeriva(${m})" style="min-height:44px;padding:6px 12px;border-radius:8px;border:1px solid var(--brd);background:transparent;color:var(--tx2);font-size:var(--fs-sm);cursor:pointer;">${m} meses</button>`).join('')}</div></div>
    <div id="comp-sec-deriva"></div>`;
  return html;
}
