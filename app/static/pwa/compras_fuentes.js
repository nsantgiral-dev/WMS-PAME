/* compras_fuentes.js — Compras → 🧾 Fuentes (m046compras).
 *
 * Lo mínimo para ver y cargar las fuentes de datos de compras: el espejo de
 * OCs de Siesa («en camino»), el lead time medido, el kardex automático, la
 * carga de origen / marca / fichas de importación con vista previa, y los
 * contenedores con sus ítems. No decide nada: las cifras las calcula el
 * servidor (`compras_fuentes`), esta pantalla las pinta.
 *
 * Todo dato va con `esc()`; los `onclick` llevan solo posiciones o palabras
 * fijas, nunca un dato.
 */

/** Contenedores de la última carga: los botones los citan por posición. */
let FUENTES_CONTENEDORES = [];
/** Tipo de la última vista previa de archivo: «Aplicar» exige haberla visto. */
let FUENTES_PREVIA_TIPO = null;
/** La marca de Siesa solo se aplica después de verla. */
let FUENTES_MARCA_VISTA = false;

const FUENTES_TIPOS_CARGA = {
  ORIGEN_MARCA: 'codigo, origen (NACIONAL / CHINA / IMPORTADO), marca',
  FICHAS: 'codigo, unidades_por_caja, cbm_por_caja, peso_kg_por_caja, moq_cajas, proveedor_china, costo_fob_usd, fuente (PACKING_LIST / AGENTE / ESTIMADO)',
};

const FUENTES_ESTADOS_CONT = ['BORRADOR', 'EN_PRODUCCION', 'NAVEGANDO', 'EN_PUERTO',
  'NACIONALIZACION', 'EN_RUTA_CEDI', 'RECIBIDO'];
const FUENTES_ESTADO_PALABRA = {
  BORRADOR: 'En armado', EN_PRODUCCION: 'En producción', NAVEGANDO: 'Navegando',
  EN_PUERTO: 'En puerto', NACIONALIZACION: 'Nacionalización',
  EN_RUTA_CEDI: 'En ruta al CDI', RECIBIDO: 'Recibido',
};
const FUENTES_LT_FUENTE = {
  MEDIDO: 'medido', PARCIAL: 'medido, pocas observaciones', DEFAULT_CONSERVADOR: 'supuesto (sin datos)', CONFIGURADO: 'configurado (ROP_LT_NACIONAL_DIAS)',
};

/** Número con separador de miles; `null` es «sin dato», nunca 0. */
function fuentesNum(v, dec = 0) {
  if (v === null || v === undefined || v === '') return 'sin dato';
  const n = Number(v);
  if (!isFinite(n)) return 'sin dato';
  return n.toLocaleString('es-CO', { maximumFractionDigits: dec, minimumFractionDigits: 0 });
}

/** Hora de Bogotá de un ISO UTC sin zona. */
function fuentesHora(iso) {
  if (!iso) return 'nunca';
  const s = String(iso);
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z');
  if (isNaN(d)) return s;
  return d.toLocaleString('es-CO', { timeZone: 'America/Bogota', dateStyle: 'short', timeStyle: 'short' });
}

function fuentesTarjeta(titulo, cuerpo) {
  return `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:12px 14px;margin-bottom:12px;">
    <div style="font-weight:700;color:var(--tx);margin-bottom:8px;">${titulo}</div>${cuerpo}</div>`;
}

function fuentesAviso(texto, tono = 'warn') {
  return `<div style="background:var(--${tono}-bg);border:1px solid var(--${tono}-brd);color:var(--${tono}-tx);border-radius:8px;padding:8px 10px;font-size:var(--fs-sm);margin:6px 0;">${esc(texto)}</div>`;
}

/** Entrada del sub-tab: `compSubtab('fuentes')` (recepcion.js). */
async function fuentesCargar() {
  const el = document.getElementById('fuentes-container');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Cargando…</div>';
  let d;
  try {
    d = await get('/api/compras/fuentes/estado');
  } catch (e) {
    el.innerHTML = fuentesAviso(e.message || 'No se pudo cargar el estado de las fuentes', 'err');
    return;
  }
  let conts = { contenedores: [] };
  try {
    conts = await get('/api/compras/armador/contenedores');
  } catch (e) {
    conts = { contenedores: [], error: e.message || 'No se pudieron leer los contenedores' };
  }
  FUENTES_CONTENEDORES = conts.contenedores || [];
  el.innerHTML = [
    fuentesHtmlOc(d.compras_oc || {}),
    fuentesHtmlEnCamino(d.en_camino || {}),
    fuentesHtmlLeadTime(d.lead_time || {}),
    fuentesHtmlKardex(d.kardex_auto || {}),
    fuentesHtmlCarga(),
    fuentesHtmlContenedores(conts),
  ].join('');
}

function fuentesHtmlOc(c) {
  const sync = c.sync_abiertas || {};
  const provs = Object.entries(c.proveedores_por_fuente || {})
    .map(([f, n]) => `${esc(f === 'None' ? 'sin fuente' : f)}: ${esc(fuentesNum(n))}`).join(' · ') || 'ninguno';
  const cuerpo = `
    ${c.encendido ? '' : fuentesAviso('El cron de OCs está apagado (COMPRAS_OC_SYNC). Solo se sincroniza con el botón.', 'info')}
    ${c.error ? fuentesAviso(c.error, 'err') : ''}
    ${sync.nota ? fuentesAviso(sync.nota) : ''}
    <div style="font-size:var(--fs-sm);color:var(--tx2);line-height:1.8;">
      Última sincronización: <strong>${esc(fuentesHora(sync.ultima_utc))}</strong>
      ${sync.ultima_ok === false ? '<span style="color:var(--warn-tx);">(incompleta: no cerró nada)</span>' : ''}<br>
      Última completa: ${esc(fuentesHora(sync.completa_utc))}<br>
      Líneas abiertas: ${esc(fuentesNum(c.lineas_abiertas))} · con pendiente ${esc(fuentesNum(c.lineas_abiertas_con_pendiente))}
      · sin unidad base ${esc(fuentesNum(c.lineas_abiertas_sin_unidad_base))}<br>
      Proveedores: ${provs}
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;">
      <button class="btn" onclick="fuentesSync(event, 'abiertas')">Sincronizar OCs abiertas</button>
      <button class="btn" onclick="fuentesSync(event, 'historial')">Historial y proveedores</button>
    </div>`;
  return fuentesTarjeta('📥 Órdenes de compra de Siesa', cuerpo);
}

function fuentesHtmlEnCamino(e) {
  const dec = e.declaracion || {};
  const filas = (e.top || []).map(t => `<tr>
      <td>${esc(t.referencia)}</td>
      <td style="text-align:right;">${esc(fuentesNum(t.oc))}</td>
      <td style="text-align:right;">${esc(fuentesNum(t.contenedores))}</td>
      <td style="text-align:right;font-weight:700;">${esc(fuentesNum(t.total))}</td>
      <td>${t.solapamiento_posible ? '<span style="color:var(--warn-tx);" title="Hay OC abierta y contenedor sin OC citada: puede estar contado dos veces">⚠ posible doble</span>' : ''}
          ${t.lineas_sin_unidad_base ? `<span style="color:var(--warn-tx);">${esc(t.lineas_sin_unidad_base)} sin unidad</span>` : ''}</td>
    </tr>`).join('');
  const avisos = [];
  (dec.fuentes_con_error || []).forEach(f => avisos.push(`La fuente ${f.fuente === 'OC_SIESA' ? 'de OCs de Siesa' : 'de contenedores'} falló: lo que viene está incompleto (${f.error}).`));
  if (dec.nota) avisos.push(dec.nota);
  if (dec.contenedor_oc_no_verificable) avisos.push(`${dec.contenedor_oc_no_verificable} ítem(s) de contenedor citan una OC que no se pudo verificar: se suman.`);
  if (dec.lineas_sin_unidad_base) avisos.push(`${dec.lineas_sin_unidad_base} línea(s) de OC sin unidad base: no se suman.`);
  if (dec.lineas_fuera_de_lista_blanca) avisos.push(`${dec.lineas_fuera_de_lista_blanca} línea(s) van a bodegas no operadas (p. ej. AV1/TRA1): no cuentan.`);
  if (dec.skus_con_solapamiento_posible) avisos.push(`${dec.skus_con_solapamiento_posible} SKU con posible doble conteo (contenedor sin OC citada).`);
  if (dec.contenedor_cita_oc_cerrada) avisos.push(`${dec.contenedor_cita_oc_cerrada} ítem(s) de contenedor citan una OC ya cerrada: no se suman.`);
  const cuerpo = `
    <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:6px;">
      ${esc(fuentesNum(e.skus))} SKU · ${esc(fuentesNum(e.unidades))} unidades en camino a las bodegas operadas.
      Es el término de tránsito de la posición del armador.
    </div>
    ${avisos.map(a => fuentesAviso(a)).join('')}
    ${filas ? `<div style="overflow-x:auto;"><table style="width:100%;font-size:var(--fs-sm);color:var(--tx2);border-collapse:collapse;">
      <thead><tr style="color:var(--tx3);"><th style="text-align:left;">SKU</th><th style="text-align:right;">OC</th><th style="text-align:right;">Contenedor</th><th style="text-align:right;">Total</th><th></th></tr></thead>
      <tbody>${filas}</tbody></table></div>` : '<div style="color:var(--tx3);font-size:var(--fs-sm);">Nada en camino registrado.</div>'}`;
  return fuentesTarjeta('🚚 En camino', cuerpo);
}

function fuentesLtLinea(nombre, lt) {
  if (!lt) return '';
  return `<div>${esc(nombre)}: <strong>${esc(fuentesNum(lt.lt_dias, 1))} ± ${esc(fuentesNum(lt.sigma_lt, 1))} días</strong>
    · ${esc(FUENTES_LT_FUENTE[lt.fuente] || lt.fuente)} (n=${esc(fuentesNum(lt.n))})</div>`;
}

function fuentesHtmlLeadTime(l) {
  const filas = (l.por_proveedor || []).map(p => `<tr>
      <td>${esc(p.proveedor)}</td>
      <td style="text-align:right;">${esc(fuentesNum(p.n_proveedor))}</td>
      <td style="text-align:right;">${esc(fuentesNum(p.lt_dias, 1))}</td>
      <td style="text-align:right;">${esc(fuentesNum(p.sigma_lt, 1))}</td>
      <td>${esc(p.nivel === 'PROVEEDOR' ? 'del proveedor' : p.nivel === 'ORIGEN' ? 'del origen (pocas OCs suyas)' : 'supuesto')}</td>
    </tr>`).join('');
  const desc = Object.entries(l.descartadas || {}).map(([k, n]) => `${esc(k.replace(/_/g, ' '))}: ${esc(n)}`).join(' · ');
  const cuerpo = `
    <div style="font-size:var(--fs-sm);color:var(--tx2);line-height:1.8;">
      ${fuentesLtLinea('Nacional', l.nacional)}${fuentesLtLinea('China', l.china)}
      <div style="color:var(--tx3);">Medido de ${esc(fuentesNum(l.observaciones))} OC(s): fecha de la OC → primera entrada
        (recepción del WMS, o la marca de Siesa). Con menos de 3 por proveedor se usa el del origen; sin datos, el supuesto. Con 3 a 5 observaciones
        se usa el mayor entre lo medido y el supuesto.
        ${desc ? `Descartadas: ${desc}.` : ''}</div>
    </div>
    ${filas ? `<div style="overflow-x:auto;margin-top:8px;"><table style="width:100%;font-size:var(--fs-sm);color:var(--tx2);border-collapse:collapse;">
      <thead><tr style="color:var(--tx3);"><th style="text-align:left;">Proveedor</th><th style="text-align:right;">OCs</th><th style="text-align:right;">Días</th><th style="text-align:right;">±</th><th style="text-align:left;">Se usa</th></tr></thead>
      <tbody>${filas}</tbody></table></div>` : ''}`;
  return fuentesTarjeta('⏱️ Lead time', cuerpo);
}

function fuentesHtmlKardex(k) {
  const u = k.ultima_descarga || {};
  const cuerpo = `
    ${k.encendido ? '' : fuentesAviso('La descarga automática del kardex está apagada (KARDEX_AUTO). Se descarga con «Descargar» en Inventario → Datos.', 'info')}
    ${k.problema_ventana ? fuentesAviso(k.problema_ventana) : ''}
    ${k.error ? fuentesAviso(k.error, 'err') : ''}
    <div style="font-size:var(--fs-sm);color:var(--tx2);line-height:1.8;">
      Ventana: ${esc((k.ventana_efectiva || []).join(' – ') || 'ninguna')} (Bogotá) · ${esc(fuentesNum(k.minutos_por_dia))} min por día<br>
      Última descarga: ${esc(fuentesHora(u.inicio_utc))} · ${esc(u.en_curso ? 'en curso' : u.interrumpida ? 'interrumpida' : (u.estado || 'sin estado'))}
      ${u.reanudar_desde ? `· sigue desde la página ${esc(u.reanudar_desde)}` : ''}
      <div style="color:var(--tx3);">${esc(k.nota || '')}</div>
    </div>`;
  return fuentesTarjeta('📚 Kardex automático', cuerpo);
}

function fuentesHtmlCarga() {
  const opciones = Object.keys(FUENTES_TIPOS_CARGA)
    .map(t => `<option value="${esc(t)}">${esc(t === 'FICHAS' ? 'Fichas de importación' : 'Origen y marca')}</option>`).join('');
  const cuerpo = `
    <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:8px;">
      Archivo CSV o Excel. Primero la vista previa; después «Aplicar» escribe solo lo válido.
      Una celda vacía no borra nada. Sobrescribir un valor queda en la bitácora.
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <select id="fuentes-tipo" onchange="fuentesTipoCambio()" style="padding:8px;">${opciones}</select>
      <input type="file" id="fuentes-archivo" accept=".csv,.xlsx,.txt">
      <button class="btn" onclick="fuentesPrevia(event)">Vista previa</button>
      <button class="btn" onclick="fuentesAplicar(event)">Aplicar</button>
    </div>
    <div id="fuentes-columnas" style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">Columnas: ${esc(FUENTES_TIPOS_CARGA.ORIGEN_MARCA)}</div>
    <div id="fuentes-resultado" style="margin-top:8px;"></div>
    <div style="border-top:1px solid var(--brd);margin-top:12px;padding-top:10px;">
      <div style="font-weight:700;color:var(--tx);margin-bottom:6px;">Un SKU a mano</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
        <input id="fuentes-sku" placeholder="Código del producto" style="padding:8px;min-width:150px;">
        <select id="fuentes-sku-origen" style="padding:8px;">
          <option value="">Origen (sin cambio)</option><option value="NACIONAL">Nacional</option>
          <option value="CHINA">China</option><option value="IMPORTADO">Importado</option>
        </select>
        <input id="fuentes-sku-marca" placeholder="Marca (p. ej. M003)" style="padding:8px;min-width:120px;">
        <button class="btn" onclick="fuentesGuardarSku(event)">Guardar</button>
      </div>
    </div>
    <div style="border-top:1px solid var(--brd);margin-top:12px;padding-top:10px;">
      <div style="font-weight:700;color:var(--tx);margin-bottom:6px;">Marca desde Siesa</div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:6px;">Lee la clasificación del ítem en Siesa (plan configurado en SIESA_CRITERIO_MARCA).</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button class="btn" onclick="fuentesMarcaPrevia(event)">Ver qué cambiaría</button>
        <button class="btn" onclick="fuentesMarcaAplicar(event)">Aplicar marcas de Siesa</button>
      </div>
      <div id="fuentes-marca" style="margin-top:8px;"></div>
    </div>`;
  return fuentesTarjeta('🏷️ Origen, marca y fichas de importación', cuerpo);
}

function fuentesTipoCambio() {
  const t = (document.getElementById('fuentes-tipo') || {}).value || 'ORIGEN_MARCA';
  const c = document.getElementById('fuentes-columnas');
  if (c) c.textContent = 'Columnas: ' + (FUENTES_TIPOS_CARGA[t] || '');
  FUENTES_PREVIA_TIPO = null;
}

function fuentesHtmlPrevia(r) {
  const res = r.resumen || {};
  if ((r.faltan_columnas || []).length) {
    return fuentesAviso('Faltan columnas: ' + r.faltan_columnas.join(', '), 'err');
  }
  const cambios = c => Object.entries(c || {}).map(([k, v]) =>
    `${esc(k)}: ${esc(v.antes === null || v.antes === undefined || v.antes === '' ? '—' : v.antes)} → <strong>${esc(v.despues)}</strong>`).join(' · ');
  const val = (r.validas || []).map(v => `<li>${esc(v.codigo)} ${v.nuevo ? '(ficha nueva)' : ''} — ${cambios(v.cambios)}</li>`).join('');
  const inv = (r.invalidas || []).map(v => `<li style="color:var(--err-tx);">Fila ${esc(v.fila)} ${esc(v.codigo || '')}: ${esc((v.errores || []).join('; '))}</li>`).join('');
  return `<div style="font-size:var(--fs-sm);color:var(--tx2);">
      ${r.escritas !== undefined ? `<div style="color:var(--ok-tx);font-weight:700;">Escritas: ${esc(r.escritas)}</div>` : ''}
      ${esc(res.filas || 0)} fila(s): <strong>${esc(res.validas || 0)}</strong> con cambios
      (${esc(res.sobrescriben || 0)} sobrescriben un valor) · ${esc(res.sin_cambio || 0)} sin cambio ·
      <span style="color:${res.invalidas ? 'var(--err-tx)' : 'var(--tx2)'};">${esc(res.invalidas || 0)} inválidas</span>
      ${(r.columnas_desconocidas || []).length ? `<br>Columnas que no se leen: ${esc(r.columnas_desconocidas.join(', '))}` : ''}
      ${r.validas_recortadas ? `<br>(+${esc(r.validas_recortadas)} con cambios no listadas)` : ''}
      ${r.invalidas_recortadas ? `<br>(+${esc(r.invalidas_recortadas)} inválidas no listadas)` : ''}
      ${inv ? `<ul style="margin:6px 0;padding-left:18px;">${inv}</ul>` : ''}
      ${val ? `<ul style="margin:6px 0;padding-left:18px;">${val}</ul>` : ''}
    </div>`;
}

function _fuentesFormulario() {
  const tipo = (document.getElementById('fuentes-tipo') || {}).value;
  const input = document.getElementById('fuentes-archivo');
  if (!input || !input.files || !input.files[0]) { alerta('Elegí un archivo', 'error'); return null; }
  const fd = new FormData();
  fd.append('tipo', tipo);
  fd.append('archivo', input.files[0]);
  return { fd, tipo };
}

async function fuentesPrevia(event) {
  const f = _fuentesFormulario();
  if (!f) return;
  const out = document.getElementById('fuentes-resultado');
  await conBotonOcupado(event, async () => {
    try {
      const r = await subirArchivoConProgreso('/api/compras/fuentes/carga/vista-previa', f.fd);
      FUENTES_PREVIA_TIPO = f.tipo;
      if (out) out.innerHTML = fuentesHtmlPrevia(r);
    } catch (e) {
      if (out) out.innerHTML = fuentesAviso(e.message || 'No se pudo leer el archivo', 'err');
    }
  }, 'Leyendo…');
}

async function fuentesAplicar(event) {
  const f = _fuentesFormulario();
  if (!f) return;
  if (FUENTES_PREVIA_TIPO !== f.tipo) { alerta('Primero la vista previa de este archivo', 'error'); return; }
  if (!confirm('¿Escribir las filas válidas? Las inválidas se ignoran.')) return;
  const out = document.getElementById('fuentes-resultado');
  await conBotonOcupado(event, async () => {
    try {
      const r = await subirArchivoConProgreso('/api/compras/fuentes/carga/aplicar', f.fd);
      if (out) out.innerHTML = fuentesHtmlPrevia(r);
      alerta(`Escritas: ${r.escritas || 0}`, 'success');
      FUENTES_PREVIA_TIPO = null;
    } catch (e) {
      if (out) out.innerHTML = fuentesAviso(e.message || 'No se pudo aplicar', 'err');
    }
  }, 'Aplicando…');
}

async function fuentesGuardarSku(event) {
  const codigo = ((document.getElementById('fuentes-sku') || {}).value || '').trim();
  const origen = (document.getElementById('fuentes-sku-origen') || {}).value || '';
  const marca = ((document.getElementById('fuentes-sku-marca') || {}).value || '').trim();
  if (!codigo) { alerta('Falta el código', 'error'); return; }
  if (!origen && !marca) { alerta('Elegí un origen o escribí una marca', 'error'); return; }
  await conBotonOcupado(event, async () => {
    try {
      const r = await put('/api/compras/fuentes/producto', { codigo, origen, marca });
      alerta(r.escritas ? 'Guardado' : 'Sin cambios: ya tenía ese valor', 'success');
    } catch (e) {
      alerta(e.message || 'No se pudo guardar', 'error');
    }
  }, 'Guardando…');
}

function fuentesHtmlMarca(r) {
  if (r.omitido) return fuentesAviso(r.omitido, 'info');
  if (r.campos_no_reconocidos) {
    return fuentesAviso('Siesa respondió con campos que no son los del plano: ' + r.campos_no_reconocidos.join(', '));
  }
  return (r.completa === false ? fuentesAviso('Lectura incompleta: ' + (r.motivo_incompleta || '')) : '')
    + (r.nota ? fuentesAviso(r.nota, 'info') : '') + fuentesHtmlPrevia(r);
}

async function fuentesMarcaPrevia(event) {
  const out = document.getElementById('fuentes-marca');
  await conBotonOcupado(event, async () => {
    try {
      const r = await get('/api/compras/fuentes/marca-siesa/vista-previa');
      FUENTES_MARCA_VISTA = !r.omitido && !r.campos_no_reconocidos;
      if (out) out.innerHTML = fuentesHtmlMarca(r);
    } catch (e) {
      if (out) out.innerHTML = fuentesAviso(e.message || 'No se pudo leer Siesa', 'err');
    }
  }, 'Leyendo Siesa…');
}

async function fuentesMarcaAplicar(event) {
  if (!FUENTES_MARCA_VISTA) { alerta('Primero «Ver qué cambiaría»', 'error'); return; }
  if (!confirm('¿Escribir las marcas leídas de Siesa?')) return;
  const out = document.getElementById('fuentes-marca');
  await conBotonOcupado(event, async () => {
    try {
      const r = await post('/api/compras/fuentes/marca-siesa/aplicar', {});
      if (out) out.innerHTML = fuentesHtmlMarca(r);
      FUENTES_MARCA_VISTA = false;
    } catch (e) {
      if (out) out.innerHTML = fuentesAviso(e.message || 'No se pudo aplicar', 'err');
    }
  }, 'Aplicando…');
}

async function fuentesSync(event, que) {
  await conBotonOcupado(event, async () => {
    try {
      const r = await post('/api/compras/fuentes/sync-oc', { que: que === 'historial' ? 'historial' : 'abiertas' });
      alerta(r.mensaje || 'Sincronización iniciada', 'success');
    } catch (e) {
      alerta(e.message || 'No se pudo iniciar', 'error');
    }
  }, 'Iniciando…');
}

// ── Contenedores ──────────────────────────────────────────────────────────

function fuentesHtmlContenedores(conts) {
  const opcionesEstado = sel => FUENTES_ESTADOS_CONT
    .map(e => `<option value="${esc(e)}" ${e === sel ? 'selected' : ''}>${esc(FUENTES_ESTADO_PALABRA[e] || e)}</option>`).join('');
  const filas = FUENTES_CONTENEDORES.map((c, i) => `<div style="border:1px solid var(--brd);border-radius:8px;padding:8px;margin-bottom:6px;font-size:var(--fs-sm);color:var(--tx2);">
      <strong>${esc(c.numero || 'sin número')}</strong> · ${esc(c.tipo || '')} · ${esc(c.proveedor || 'sin proveedor')}
      · OC ${esc(c.fecha_oc || '—')} · ETA ${esc(c.fecha_eta || '—')} · recibido ${esc(c.fecha_recepcion_cedi || '—')}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;align-items:center;">
        <select id="fuentes-cont-estado-${i}" style="padding:6px;">${opcionesEstado(c.estado)}</select>
        <button class="btn" onclick="fuentesContEstado(event, ${i})">Cambiar estado</button>
        <button class="btn" onclick="fuentesContItems(event, ${i})">Ítems</button>
      </div>
      <div id="fuentes-cont-items-${i}"></div>
    </div>`).join('');
  const cuerpo = `
    ${conts.error ? fuentesAviso(conts.error, 'err') : ''}
    <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:8px;">
      Los ítems de un contenedor que no está «En armado» ni «Recibido» cuentan como en camino.
      Al marcarlo Recibido se fecha la llegada al CDI: esa fecha mide el lead time de China.
    </div>
    ${filas || '<div style="color:var(--tx3);font-size:var(--fs-sm);">Sin contenedores registrados.</div>'}
    <div style="border-top:1px solid var(--brd);margin-top:10px;padding-top:10px;">
      <div style="font-weight:700;color:var(--tx);margin-bottom:6px;">Registrar contenedor</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <input id="fuentes-cont-numero" placeholder="Número (MSKU…)" style="padding:8px;min-width:130px;">
        <select id="fuentes-cont-tipo" style="padding:8px;"><option value="40STD">40' STD</option><option value="40HC">40' HQ</option><option value="20STD">20' STD</option></select>
        <input id="fuentes-cont-proveedor" placeholder="Proveedor" style="padding:8px;min-width:130px;">
        <label style="font-size:var(--fs-xs);color:var(--tx3);">Fecha OC <input type="date" id="fuentes-cont-foc" style="padding:6px;"></label>
        <label style="font-size:var(--fs-xs);color:var(--tx3);">ETA <input type="date" id="fuentes-cont-eta" style="padding:6px;"></label>
        <button class="btn" onclick="fuentesContCrear(event)">Registrar</button>
      </div>
    </div>`;
  return fuentesTarjeta('🚢 Contenedores', cuerpo);
}

async function fuentesContCrear(event) {
  const v = id => ((document.getElementById(id) || {}).value || '').trim();
  if (!v('fuentes-cont-numero')) { alerta('Falta el número del contenedor', 'error'); return; }
  await conBotonOcupado(event, async () => {
    try {
      await post('/api/compras/armador/contenedores', {
        numero: v('fuentes-cont-numero'), tipo: v('fuentes-cont-tipo') || '40STD',
        proveedor: v('fuentes-cont-proveedor'), fecha_oc: v('fuentes-cont-foc') || null,
        fecha_eta: v('fuentes-cont-eta') || null, estado: 'BORRADOR',
      });
      alerta('Contenedor registrado (en armado)', 'success');
      await fuentesCargar();
    } catch (e) {
      alerta(e.message || 'No se pudo registrar', 'error');
    }
  }, 'Registrando…');
}

async function fuentesContEstado(event, i) {
  const c = FUENTES_CONTENEDORES[i];
  if (!c) return;
  const estado = (document.getElementById('fuentes-cont-estado-' + i) || {}).value;
  await conBotonOcupado(event, async () => {
    try {
      const r = await put(`/api/compras/fuentes/contenedores/${c.id}/estado`, { estado });
      alerta(`Estado cambiado · ${r.items_movidos || 0} ítem(s)`, 'success');
      await fuentesCargar();
    } catch (e) {
      alerta(e.message || 'No se pudo cambiar', 'error');
    }
  }, 'Guardando…');
}

async function fuentesContItems(event, i) {
  const c = FUENTES_CONTENEDORES[i];
  const el = document.getElementById('fuentes-cont-items-' + i);
  if (!c || !el) return;
  await conBotonOcupado(event, async () => {
    let d;
    try {
      d = await get(`/api/compras/fuentes/contenedores/${c.id}/items`);
    } catch (e) {
      el.innerHTML = fuentesAviso(e.message || 'No se pudieron leer los ítems', 'err');
      return;
    }
    const lista = (d.items || []).map(it => `<li>${esc(it.codigo)} · ${esc(fuentesNum(it.cantidad))} und · ${esc(FUENTES_ESTADO_PALABRA[it.estado] || it.estado)}
        ${it.oc_referencia ? `· OC ${esc(it.oc_referencia)}` : ''}</li>`).join('');
    el.innerHTML = `<div style="margin-top:8px;font-size:var(--fs-sm);color:var(--tx2);">
        ${lista ? `<ul style="margin:4px 0;padding-left:18px;">${lista}</ul>` : 'Sin ítems.'}
        <textarea id="fuentes-cont-texto-${i}" rows="4" placeholder="codigo,cantidad (una línea por SKU)" style="width:100%;margin-top:6px;padding:8px;"></textarea>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;">
          <input id="fuentes-cont-oc-${i}" placeholder="OC de Siesa (CO-TIPO-CONSEC), si la hay" style="padding:8px;min-width:220px;">
          <button class="btn" onclick="fuentesContCargarItems(event, ${i})">Cargar ítems</button>
        </div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);">Con la OC citada, el ítem no se suma encima del pendiente de esa OC.</div>
      </div>`;
  }, 'Cargando…');
}

async function fuentesContCargarItems(event, i) {
  const c = FUENTES_CONTENEDORES[i];
  if (!c) return;
  const texto = ((document.getElementById('fuentes-cont-texto-' + i) || {}).value || '').trim();
  const oc = ((document.getElementById('fuentes-cont-oc-' + i) || {}).value || '').trim();
  const filas = texto.split(/\r?\n/).map(l => l.split(/[;,\t]/)).filter(p => p[0] && p[0].trim())
    .map(p => ({ codigo: p[0].trim(), cantidad: (p[1] || '').trim() }));
  if (!filas.length) { alerta('Escribí al menos una línea codigo,cantidad', 'error'); return; }
  await conBotonOcupado(event, async () => {
    try {
      const r = await post(`/api/compras/fuentes/contenedores/${c.id}/items`, { filas, oc_referencia: oc || null });
      alerta(`Cargados: ${r.cargados || 0}`, 'success');
      await fuentesCargar();
    } catch (e) {
      alerta(e.message || 'No se cargó nada', 'error');
    }
  }, 'Cargando…');
}
