// ══════════════════════════════════════════════════════════════════════════
// Temporada — instrumento de comité, no pantalla de administración.
//
// Se abre una vez al año, frente a la fundadora y tesorería. El modelo puede
// tener razón y perder la reunión: por eso la lista paralela de ella va AL
// LADO del Q*, con la diferencia en pesos, y hay export para el acta.
//
// Las filas donde el modelo y ella más difieren son la conversación más
// valiosa del comité — por eso se ordenan por diferencia, no por inversión.
// ══════════════════════════════════════════════════════════════════════════

let _TEMP_DATA = null;
let _TEMP_JUICIOS = {};
let _TEMP_ESCENARIO = 0;      // % de ajuste a la demanda para el "¿y si...?"
const TEMPORADA_ACTUAL = '2026-27';

async function temporadaCargar() {
  const el = document.getElementById('temporada-container');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Calculando Q* de temporada…</div>';
  try {
    const [pedido, juicios] = await Promise.all([
      get('/api/kardex/temporada/pedido'),
      get('/api/kardex/temporada/juicios?temporada=' + TEMPORADA_ACTUAL),
    ]);
    _TEMP_DATA = pedido;
    _TEMP_JUICIOS = juicios.juicios || {};
    _tempRender(el, _TEMP_DATA);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

/** Registra el juicio en el SERVIDOR, con autor y fecha, junto a la foto del
 *  modelo en ese momento. En enero de 2027 este es el dataset que compara lo
 *  que ella pidió, lo que el modelo pidió y lo que se vendió. */
async function temporadaSetParalela(ref, valor) {
  const fila = (_TEMP_DATA.items || []).find(i => i.referencia === ref) || {};
  const n = parseInt(valor, 10);
  try {
    await post('/api/kardex/temporada/juicios', {
      temporada: TEMPORADA_ACTUAL,
      referencia: ref,
      cantidad_juicio: isNaN(n) ? null : n,
      q_modelo: fila.q_optimo,
      costo_unitario: fila.costo_unitario,
      distribucion: fila.distribucion,
    });
    if (isNaN(n)) delete _TEMP_JUICIOS[ref];
    else _TEMP_JUICIOS[ref] = { cantidad_juicio: n };
    _tempRender(document.getElementById('temporada-container'), _TEMP_DATA);
  } catch (e) {
    alerta('No se pudo registrar el juicio: ' + (e.message || e), 'error');
  }
}

function _tempLeerParalela() {
  const m = {};
  for (const [ref, j] of Object.entries(_TEMP_JUICIOS)) m[ref] = j.cantidad_juicio;
  return m;
}

/** "¿Y si la demanda cae 20%?" — el comité explora con la herramienta en vez
 *  de juzgarla. Reescala mu y sigma; el ratio crítico no cambia. */
function temporadaEscenario(pct) {
  _TEMP_ESCENARIO = pct;
  _tempRender(document.getElementById('temporada-container'), _TEMP_DATA);
}

/** Q* bajo el escenario activo. Q* es lineal en mu y sigma, así que reescalar
 *  la demanda reescala el Q* — no hay que volver al servidor. */
function _tempQ(fila) {
  const f = 1 + _TEMP_ESCENARIO / 100;
  return Math.max(0, Math.round(fila.q_optimo * f));
}

function _tempRender(el, d) {
  if (!el) return;
  if (d.error) {
    el.innerHTML = `<div style="padding:20px;color:var(--yellow);font-size:12px;">${d.error}</div>`;
    return;
  }

  const par = _tempLeerParalela();
  const cob = d.cobertura || {};
  let html = '';

  // ── Cobertura: qué fracción de la decisión cubre el modelo ──────────────
  const pct = cob.pct_cubierto || 0;
  const colorCob = pct >= 80 ? 'var(--green)' : (pct >= 50 ? 'var(--yellow)' : 'var(--red)');
  html += `<div style="border:1px solid ${colorCob};border-radius:10px;padding:12px;margin-bottom:14px;">
    <div style="font-size:13px;font-weight:700;color:${colorCob};margin-bottom:6px;">
      El modelo cubre ${cob.cubiertos_por_modelo || 0} de ${cob.skus_temporada || 0} SKUs de temporada (${pct}%)
    </div>
    <div style="font-size:11px;color:var(--tx3);line-height:1.7;">
      Excluidos por costo fantasma: <strong>${cob.excluidos_costo_fantasma || 0}</strong> ·
      por lista negra: <strong>${cob.excluidos_lista_negra || 0}</strong> ·
      sin producto: <strong>${cob.excluidos_sin_producto || 0}</strong>
    </div>
    ${cob.advertencia ? `<div style="font-size:11px;color:${colorCob};margin-top:6px;">${cob.advertencia}</div>` : ''}
  </div>`;

  // ── Totales, con el contraste contra la lista paralela ──────────────────
  const filas = (d.items || []).filter(i => !i.error);
  let invModelo = 0, invParalela = 0, conParalela = 0;
  for (const f of filas) {
    const q = _tempQ(f);
    invModelo += q * (f.costo_unitario || 0);
    if (par[f.referencia] != null) {
      conParalela++;
      invParalela += par[f.referencia] * (f.costo_unitario || 0);
    } else {
      invParalela += q * (f.costo_unitario || 0);
    }
  }
  const dif = invParalela - invModelo;

  html += `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px;">
    ${_compKpi('$' + Math.round(invModelo).toLocaleString('es-CO'), 'Inversión modelo', 'var(--tx)')}
    ${_compKpi('$' + Math.round(invParalela).toLocaleString('es-CO'), 'Con lista paralela', 'var(--blue)')}
    ${_compKpi((dif >= 0 ? '+$' : '−$') + Math.abs(Math.round(dif)).toLocaleString('es-CO'), 'Diferencia', dif >= 0 ? 'var(--yellow)' : 'var(--green)')}
  </div>`;

  // ── "¿Y si...?" — el comité explora con la herramienta, no la juzga ─────
  const escenarios = [-30, -20, -10, 0, 10, 20];
  html += `<div style="border:1px solid var(--brd);border-radius:8px;padding:10px;margin-bottom:12px;">
    <div style="font-size:11px;font-weight:700;color:var(--tx3);margin-bottom:6px;">¿Y si la demanda...?</div>
    <div style="display:flex;gap:5px;flex-wrap:wrap;">
      ${escenarios.map(e => `<button onclick="temporadaEscenario(${e})"
        style="padding:5px 11px;border-radius:6px;cursor:pointer;font-size:11px;
               border:1px solid ${e === _TEMP_ESCENARIO ? 'var(--pm)' : 'var(--brd)'};
               background:${e === _TEMP_ESCENARIO ? 'var(--pm)' : 'transparent'};
               color:${e === _TEMP_ESCENARIO ? '#fff' : 'var(--tx3)'};
               font-weight:${e === _TEMP_ESCENARIO ? '700' : '400'};">
        ${e === 0 ? 'Base' : (e > 0 ? '+' : '') + e + '%'}</button>`).join('')}
    </div>
    ${_TEMP_ESCENARIO !== 0 ? `<div style="font-size:10px;color:var(--yellow);margin-top:6px;">
      Escenario activo: demanda ${_TEMP_ESCENARIO > 0 ? '+' : ''}${_TEMP_ESCENARIO}%. El acta se exporta con el escenario BASE.
    </div>` : ''}
  </div>`;

  // ── Banda de sensibilidad: cuánta plata depende de la POLÍTICA ─────────
  const bs = d.banda_sensibilidad || {};
  if (bs.exposicion_pesos != null) {
    const par2 = d.parametros || {};
    html += `<div style="border:1px solid var(--brd);border-radius:8px;padding:10px;margin-bottom:12px;">
      <div style="font-size:11px;font-weight:700;color:var(--tx3);margin-bottom:6px;">
        Sensibilidad al ratio crítico (±10 puntos)
      </div>
      <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--tx);">
        <span>CR−10: <strong>$${(bs.inversion_cr_menos_10 || 0).toLocaleString('es-CO')}</strong></span>
        <span>Base: <strong>$${(bs.inversion_base || 0).toLocaleString('es-CO')}</strong></span>
        <span>CR+10: <strong>$${(bs.inversion_cr_mas_10 || 0).toLocaleString('es-CO')}</strong></span>
        <span style="color:var(--yellow);">Exposición: <strong>$${(bs.exposicion_pesos || 0).toLocaleString('es-CO')}</strong></span>
      </div>
      <div style="font-size:10px;color:var(--tx3);margin-top:6px;line-height:1.6;">
        Ese rango es lo que está en juego por la <strong>política de tasas</strong>, no por la demanda.
        Capital ${Math.round((par2.tasa_capital || 0.30) * 100)}% · liquidación ${Math.round((par2.tasa_liquidacion || 0.60) * 100)}%
        — ratificar por escrito ANTES de correr el modelo.
      </div>
    </div>`;
  }

  html += `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center;">
    <button onclick="temporadaExportar()"
      style="padding:7px 14px;border:none;border-radius:7px;background:var(--pm);color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
      Exportar acta (PDF)
    </button>
    <span style="font-size:11px;color:var(--tx3);">
      ${conParalela} de ${filas.length} filas con cifra de la lista paralela.
    </span>
  </div>`;

  // ── Tabla: Q* contra lista paralela, ordenada por desacuerdo ────────────
  const conDif = filas.map(f => {
    const p = par[f.referencia];
    const difU = p != null ? p - _tempQ(f) : null;
    const difP = difU != null ? difU * (f.costo_unitario || 0) : null;
    return { ...f, _p: p, _difU: difU, _difP: difP };
  }).sort((a, b) => Math.abs(b._difP || 0) - Math.abs(a._difP || 0));

  html += `<div style="overflow-x:auto;"><table style="width:100%;border-collapse:collapse;font-size:11px;">
    <thead><tr style="border-bottom:2px solid var(--brd);color:var(--tx3);text-align:right;">
      <th style="text-align:left;padding:6px;">Referencia</th>
      <th style="padding:6px;">Temporadas</th>
      <th style="padding:6px;">Demanda esp.</th>
      <th style="padding:6px;">Q* modelo</th>
      <th style="padding:6px;">Lista paralela</th>
      <th style="padding:6px;">Dif. unid.</th>
      <th style="padding:6px;">Dif. $</th>
    </tr></thead><tbody>`;

  for (const f of conDif) {
    const alerta = f.advertencia_1_temporada;
    const difColor = f._difU == null ? 'var(--tx3)'
      : (f._difU > 0 ? 'var(--yellow)' : (f._difU < 0 ? 'var(--green)' : 'var(--tx3)'));
    html += `<tr style="border-bottom:1px solid var(--brd);text-align:right;">
      <td style="text-align:left;padding:6px;">
        <div style="color:var(--tx);font-weight:600;">${f.referencia}</div>
        <div style="color:var(--tx3);font-size:10px;">${(f.nombre || '').slice(0, 42)}</div>
      </td>
      <td style="padding:6px;color:${alerta ? 'var(--yellow)' : 'var(--tx3)'};" title="${f.distribucion || ''}">
        ${f.n_temporadas}${alerta ? ' ⚠' : ''}
      </td>
      <td style="padding:6px;color:var(--tx3);">${f.demanda_esperada}</td>
      <td style="padding:6px;color:var(--tx);font-weight:700;">${_tempQ(f)}</td>
      <td style="padding:6px;">
        <input type="number" value="${f._p != null ? f._p : ''}" placeholder="—"
          onchange="temporadaSetParalela('${f.referencia}', this.value)"
          style="width:70px;padding:3px;text-align:right;background:var(--bg);border:1px solid var(--brd);border-radius:4px;color:var(--tx);font-size:11px;">
      </td>
      <td style="padding:6px;color:${difColor};font-weight:700;">
        ${f._difU != null ? (f._difU > 0 ? '+' : '') + f._difU : '—'}
      </td>
      <td style="padding:6px;color:${difColor};">
        ${f._difP != null ? (f._difP > 0 ? '+$' : '−$') + Math.abs(Math.round(f._difP)).toLocaleString('es-CO') : '—'}
      </td>
    </tr>`;
  }
  html += '</tbody></table></div>';

  html += `<div style="font-size:10px;color:var(--tx3);margin-top:10px;line-height:1.6;">
    ⚠ = una sola temporada: distribución <strong>Normal inflada</strong> (CV 30% ×1.5), incertidumbre ALTA.
    Una observación no es una distribución — con la empírica el Q* colapsaría a "pide lo que vendiste".
    El export del 1 de agosto lleva n de 1 a 3 y cambia la distribución a empírica.<br>
    Demanda DESCENSURADA por días con stock — lo que se agotó en enero no se lee como "no se vendía".<br>
    Ratio crítico por SKU: Cu = precio − costo · Co = costo × (capital ${((d.parametros || {}).tasa_capital * 100) || 30}% + liquidación ${((d.parametros || {}).tasa_liquidacion * 100) || 60}%).
  </div>`;

  el.innerHTML = html;
}

/** Acta imprimible. Sin librerías: el diálogo de impresión del navegador
 *  guarda a PDF, y eso es lo que va al acta del comité. */
function temporadaExportar() {
  if (!_TEMP_DATA) return;
  const parJuicio = _tempLeerParalela();
  const cob = _TEMP_DATA.cobertura || {};
  const par = _TEMP_DATA.parametros || {};
  const bs = _TEMP_DATA.banda_sensibilidad || {};
  const filas = (_TEMP_DATA.items || []).filter(i => !i.error);

  let invM = 0, invP = 0;
  const cuerpo = filas.map(f => {
    const p = parJuicio[f.referencia];
    const q = p != null ? p : f.q_optimo;
    invM += f.inversion_optima || 0;
    invP += q * (f.costo_unitario || 0);
    const dU = p != null ? p - f.q_optimo : '';
    return `<tr>
      <td>${f.referencia}</td><td>${(f.nombre || '')}</td>
      <td class="n">${f.n_temporadas}</td>
      <td class="n">${f.demanda_esperada}</td>
      <td class="n"><b>${f.q_optimo}</b></td>
      <td class="n">${p != null ? p : '—'}</td>
      <td class="n">${dU !== '' ? (dU > 0 ? '+' + dU : dU) : '—'}</td>
    </tr>`;
  }).join('');

  // Sello del acta en hora de Bogotá, con la zona VISIBLE.
  // toISOString() da UTC: una firma a las 3 p.m. en Neiva se registraría como
  // 20:00. Es arreglo de presentación, no de almacenamiento — la migración
  // naive→aware del backend va después del comité.
  const _bog = new Intl.DateTimeFormat('es-CO', {
    timeZone: 'America/Bogota', dateStyle: 'short', timeStyle: 'short',
  }).format(new Date());
  const hoy = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Bogota', year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date());
  const sello = `${_bog} (hora de Colombia, UTC−5)`;
  const w = window.open('', '_blank');
  if (!w) { alerta('Permite ventanas emergentes para exportar', 'error'); return; }
  w.document.write(`<html><head><title>Acta pedido temporada ${hoy}</title>
    <style>
      body{font-family:system-ui,sans-serif;font-size:11px;color:#111;margin:24px;}
      h1{font-size:16px;margin:0 0 2px;} h2{font-size:12px;margin:16px 0 6px;}
      .sub{color:#666;font-size:10px;margin-bottom:14px;}
      table{width:100%;border-collapse:collapse;}
      th,td{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;}
      th{background:#f4f4f4;font-size:10px;} .n{text-align:right;}
      .box{border:1px solid #ccc;padding:8px;margin-bottom:12px;font-size:10px;}
      .tot{margin-top:12px;font-size:12px;} .firma{margin-top:40px;font-size:10px;}
    </style></head><body>
    <h1>Pedido de temporada escolar — acta de comité</h1>
    <div class="sub">Generado ${sello} · Papelería Medellín</div>
    <div class="box">
      <b>Cobertura del modelo:</b> ${cob.cubiertos_por_modelo || 0} de ${cob.skus_temporada || 0} SKUs (${cob.pct_cubierto || 0}%).
      Excluidos: ${cob.excluidos_costo_fantasma || 0} por costo no confiable,
      ${cob.excluidos_lista_negra || 0} por lista negra.
      El resto de la decisión se toma sin modelo.<br>
      <b>Método:</b> newsvendor sobre demanda descensurada por días con stock.
      Ratio crítico por SKU (Cu = precio − costo; Co = costo × capital+liquidación).<br>
      <b>Política de tasas ratificada:</b> capital ${Math.round((par.tasa_capital || 0.30) * 100)}% ·
      liquidación ${Math.round((par.tasa_liquidacion || 0.60) * 100)}%.
      Fijada antes de la corrida, no después de ver los números.<br>
      <b>Sensibilidad (CR ±10 pts):</b>
      $${(bs.inversion_cr_menos_10 || 0).toLocaleString('es-CO')} /
      $${(bs.inversion_base || 0).toLocaleString('es-CO')} /
      $${(bs.inversion_cr_mas_10 || 0).toLocaleString('es-CO')}
      — exposición $${(bs.exposicion_pesos || 0).toLocaleString('es-CO')} por la política de tasas.
    </div>
    <h2>Detalle</h2>
    <table><thead><tr>
      <th>Ref</th><th>Nombre</th><th class="n">Temp.</th><th class="n">Dem. esp.</th>
      <th class="n">Q* modelo</th><th class="n">Lista paralela</th><th class="n">Dif.</th>
    </tr></thead><tbody>${cuerpo}</tbody></table>
    <div class="tot">
      <b>Inversión según modelo:</b> $${Math.round(invM).toLocaleString('es-CO')}<br>
      <b>Inversión según decisión del comité:</b> $${Math.round(invP).toLocaleString('es-CO')}<br>
      <b>Diferencia:</b> $${Math.round(invP - invM).toLocaleString('es-CO')}
    </div>
    <div class="firma">
      Decisión aprobada por: ____________________  ____________________  ____________________<br>
      <span style="color:#666;">Las filas con mayor diferencia entre Q* y lista paralela son las que exigen justificación en acta.</span>
    </div>
    </body></html>`);
  w.document.close();
  setTimeout(() => w.print(), 300);
}
