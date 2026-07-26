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
const _TEMP_LS = 'temporada_lista_paralela';

/** La lista paralela vive en el navegador: se escribe antes del comité y no
 *  se pierde al recargar. No va al servidor — es el juicio de ella, no un dato
 *  del sistema. */
function _tempLeerParalela() {
  try { return JSON.parse(localStorage.getItem(_TEMP_LS) || '{}'); }
  catch (_) { return {}; }
}
function _tempGuardarParalela(m) {
  try { localStorage.setItem(_TEMP_LS, JSON.stringify(m)); } catch (_) {}
}

async function temporadaCargar() {
  const el = document.getElementById('inv-ia-container');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Calculando Q* de temporada…</div>';
  try {
    _TEMP_DATA = await get('/api/kardex/temporada/pedido');
    _tempRender(el, _TEMP_DATA);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

function temporadaSetParalela(ref, valor) {
  const m = _tempLeerParalela();
  const n = parseInt(valor, 10);
  if (isNaN(n)) delete m[ref]; else m[ref] = n;
  _tempGuardarParalela(m);
  if (_TEMP_DATA) _tempRender(document.getElementById('inv-ia-container'), _TEMP_DATA);
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
    invModelo += f.inversion_optima || 0;
    if (par[f.referencia] != null) {
      conParalela++;
      invParalela += par[f.referencia] * (f.costo_unitario || 0);
    } else {
      invParalela += f.inversion_optima || 0;
    }
  }
  const dif = invParalela - invModelo;

  html += `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px;">
    ${_compKpi('$' + Math.round(invModelo).toLocaleString('es-CO'), 'Inversión modelo', 'var(--tx)')}
    ${_compKpi('$' + Math.round(invParalela).toLocaleString('es-CO'), 'Con lista paralela', 'var(--blue)')}
    ${_compKpi((dif >= 0 ? '+$' : '−$') + Math.abs(Math.round(dif)).toLocaleString('es-CO'), 'Diferencia', dif >= 0 ? 'var(--yellow)' : 'var(--green)')}
  </div>`;

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
    const difU = p != null ? p - f.q_optimo : null;
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
      <td style="padding:6px;color:${alerta ? 'var(--yellow)' : 'var(--tx3)'};">
        ${f.n_temporadas}${alerta ? ' ⚠' : ''}
      </td>
      <td style="padding:6px;color:var(--tx3);">${f.demanda_esperada}</td>
      <td style="padding:6px;color:var(--tx);font-weight:700;">${f.q_optimo}</td>
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
    ⚠ = una sola temporada de historia: σ inflado ×1.5 por factor de ignorancia.<br>
    Demanda DESCENSURADA por días con stock — lo que se agotó en enero no se lee como "no se vendía".<br>
    Ratio crítico por SKU: Cu = precio − costo · Co = costo × (capital ${((d.parametros || {}).tasa_capital * 100) || 30}% + liquidación ${((d.parametros || {}).tasa_liquidacion * 100) || 60}%).
  </div>`;

  el.innerHTML = html;
}

/** Acta imprimible. Sin librerías: el diálogo de impresión del navegador
 *  guarda a PDF, y eso es lo que va al acta del comité. */
function temporadaExportar() {
  if (!_TEMP_DATA) return;
  const par = _tempLeerParalela();
  const cob = _TEMP_DATA.cobertura || {};
  const filas = (_TEMP_DATA.items || []).filter(i => !i.error);

  let invM = 0, invP = 0;
  const cuerpo = filas.map(f => {
    const p = par[f.referencia];
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

  const hoy = new Date().toISOString().slice(0, 10);
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
    <div class="sub">Generado ${hoy} · Papelería Medellín</div>
    <div class="box">
      <b>Cobertura del modelo:</b> ${cob.cubiertos_por_modelo || 0} de ${cob.skus_temporada || 0} SKUs (${cob.pct_cubierto || 0}%).
      Excluidos: ${cob.excluidos_costo_fantasma || 0} por costo no confiable,
      ${cob.excluidos_lista_negra || 0} por lista negra.
      El resto de la decisión se toma sin modelo.<br>
      <b>Método:</b> newsvendor sobre demanda descensurada por días con stock.
      Ratio crítico por SKU (Cu = precio − costo; Co = costo × capital+liquidación).
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
