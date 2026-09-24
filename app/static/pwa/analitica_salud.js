/* ──────────────────────────────────────────────────────────────────────────
 * 📈 Analítica → 🩺 Salud del dato  (Fase 1, 2026-09-24)
 *
 * «¿Puedo confiar en los números de hoy?» Pinta lo que calcula
 * `app/services/analitica_salud.py` (GET /api/analitica/salud). El veredicto
 * —por fuente y global— se decide en el servidor: acá solo se pinta. Si cada
 * pantalla armara el suyo, divergirían (Regla 0, «una política, una función»).
 *
 * Depende del shell `analitica.js` (anCargarPanel, anNum, anFrescura) y de
 * `esc()` de util.js. Todo dato interpolado va con esc(); los botones pasan
 * una posición, nunca el dato.
 * ────────────────────────────────────────────────────────────────────────── */

const AN_SALUD = { el: null, datos: null, abierto: null };

const AN_SALUD_PILDORA = { ok: 'badge-green', advertencia: 'badge-yellow', critico: 'badge-red' };
const AN_SALUD_NIVEL_TXT = { ok: 'OK', advertencia: 'Atención', critico: 'Crítico' };
const AN_SALUD_GLOBAL_NIVEL = { CONFIABLE: 'ok', CON_RESERVAS: 'advertencia', NO_CONFIABLE: 'critico' };

function anSaludQs(f) {
  const p = new URLSearchParams();
  if (f && f.desde) p.set('desde', f.desde);
  if (f && f.hasta) p.set('hasta', f.hasta);
  if (f && f.almacen_id) p.set('almacen_id', f.almacen_id);
  return p.toString();
}

function anSaludCargar(el, f) {
  AN_SALUD.el = el;
  const qs = anSaludQs(f);
  return anCargarPanel({
    el, clave: 'salud', params: qs,
    pedir: () => get('/api/analitica/salud?' + qs),
    html: (d) => { AN_SALUD.datos = d; return anSaludHtml(d, AN_SALUD.abierto); },
  });
}

/** Abre/cierra el detalle de una sección (drill-down sin salir del módulo). */
function anSaludAbrir(clave) {
  AN_SALUD.abierto = AN_SALUD.abierto === clave ? null : clave;
  if (AN_SALUD.el && AN_SALUD.datos) AN_SALUD.el.innerHTML = anSaludHtml(AN_SALUD.datos, AN_SALUD.abierto);
}

function anSaludAbrirFuente(i) { anSaludAbrir('fuente-' + Number(i)); }

function _anSaludPildora(nivel, texto) {
  const cls = AN_SALUD_PILDORA[nivel] || 'badge-yellow';
  return `<span class="badge ${cls}">${esc(texto || AN_SALUD_NIVEL_TXT[nivel] || nivel)}</span>`;
}

function _anSaludNum(n) {
  if (n === null || n === undefined) return 'sin dato';
  return (typeof anNum === 'function') ? anNum(n) : String(n);
}

function _anSaludPct(pct, n) {
  if (pct === null || pct === undefined || !n) return '—';
  return `${Math.round(pct * 100)} % de ${_anSaludNum(n)}`;
}

function _anSaludFecha(iso) {
  if (!iso) return 'nunca';
  // Fechas UTC del servidor → hora de Bogotá; días (YYYY-MM-DD) tal cual.
  if (/^\d{4}-\d{2}-\d{2}$/.test(iso)) return iso;
  const d = new Date(iso + (/[zZ]|[+-]\d{2}:?\d{2}$/.test(iso) ? '' : 'Z'));
  if (isNaN(d)) return String(iso);
  return d.toLocaleString('es-CO', { timeZone: 'America/Bogota', day: '2-digit', month: 'short',
    hour: '2-digit', minute: '2-digit' });
}

function _anSaludKpi(clave, valor, etiqueta, sub, nivel) {
  return `<div class="kpi-card" style="cursor:pointer;" onclick="anSaludAbrir('${clave}')">
    <div class="kpi-valor">${esc(valor)}</div>
    <div class="kpi-label">${esc(etiqueta)} ${_anSaludPildora(nivel)}</div>
    <div class="kpi-sub">${esc(sub)}</div>
  </div>`;
}

function _anSaludFuentes(d, abierto) {
  const fuentes = d.fuentes || [];
  const filas = fuentes.map((f, i) => {
    const abiertoAca = abierto === 'fuente-' + i;
    const cron = f.cron ? `${f.cron.tag} · ${f.cron.proceso === 'worker' ? 'worker' : 'web'}`
      + (f.cron.en_este_proceso ? ' · activo en este proceso' : ' · no visible desde este proceso') : 'sin cron: se actualiza a mano';
    const det = abiertoAca ? `<div style="padding:8px 0 4px;font-size:var(--fs-sm);color:var(--tx2);">
        <div><b>Alimenta:</b> ${esc(f.alimenta)}</div>
        <div><b>Última actualización:</b> ${esc(_anSaludFecha(f.ultima_actualizacion))}
          · ${f.completa === true ? 'completa' : f.completa === false ? 'incompleta' : 'completitud desconocida'}</div>
        <div><b>Cron:</b> ${esc(cron)}</div>
        ${f.cron && f.cron.nota ? `<div style="color:var(--tx3);">${esc(f.cron.nota)}</div>` : ''}
        ${_anSaludDetalleFuente(f)}
      </div>` : '';
    return `<div class="tabla-fila" style="flex-direction:column;align-items:stretch;cursor:pointer;" onclick="anSaludAbrirFuente(${i})">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">
        <span style="font-weight:700;color:var(--tx);">${esc(f.nombre)}${f.critica ? ' <span style="color:var(--tx3);font-weight:400;">· crítica</span>' : ''}</span>
        ${_anSaludPildora(f.nivel, f.veredicto_texto)}
      </div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-top:4px;">${esc(f.motivo)}</div>
      ${f.que_hacer ? `<div style="font-size:var(--fs-sm);color:var(--warn-tx);margin-top:2px;">Qué hacer: ${esc(f.que_hacer)}</div>` : ''}
      ${det}
    </div>`;
  }).join('');
  return `<div class="tabla-card"><div class="tabla-titulo">Fuentes del dato</div>${filas || '<div class="tabla-nombre">Sin fuentes</div>'}</div>`;
}

function _anSaludDetalleFuente(f) {
  const det = f.detalle || {};
  if (Array.isArray(det.bodegas) && det.bodegas.length) {
    return `<div style="margin-top:6px;">${det.bodegas.map(b =>
      `<div>${esc(b.bodega)}: ${b.filas ? esc(_anSaludFecha(b.actualizada_utc)) + ' · ' + esc(_anSaludNum(b.filas)) + ' filas' : 'sin datos'}${b.atrasada && b.filas ? ' · atrasada' : ''}</div>`).join('')}</div>`;
  }
  if (Array.isArray(det.alcances_sin_corrida_completa_ese_dia) && det.alcances_sin_corrida_completa_ese_dia.length) {
    return `<div>Sin corrida completa: ${esc(det.alcances_sin_corrida_completa_ese_dia.join(', '))}</div>`;
  }
  if (Array.isArray(det.problemas) && det.problemas.length) {
    return det.problemas.map(p => `<div>• ${esc(p.titulo)}</div>`).join('');
  }
  return '';
}

function _anSaludAuditoria(a, abierto) {
  if (!a) return '';
  const flujos = (a.por_flujo || []).map((x, i) => {
    const peores = abierto === 'auditoria' ? (x.peores || []).map(p =>
      `<div style="font-size:var(--fs-sm);color:var(--tx2);padding-left:10px;">
        ${_anSaludPildora(p.severidad === 'BLOQUEA' ? 'critico' : 'advertencia', p.severidad === 'BLOQUEA' ? 'Bloquea' : p.severidad === 'AVISA' ? 'Avisa' : 'Observa')}
        ${esc(p.codigo)} · ${esc(p.consecuencia)} (${esc(_anSaludNum(p.total))})
        ${p.ejemplos && p.ejemplos.length ? `<div style="color:var(--tx3);">Ej.: ${esc(p.ejemplos.join(', '))}</div>` : ''}
      </div>`).join('') : '';
    return `<div class="tabla-fila" style="flex-direction:column;align-items:stretch;">
      <div style="display:flex;justify-content:space-between;gap:8px;">
        <span class="tabla-nombre">${esc(x.flujo)} · ${esc(_anSaludNum(x.invariantes))} reglas</span>
        <span style="font-size:var(--fs-sm);color:var(--tx);">${esc(_anSaludNum(x.bloqueantes))} bloquean · ${esc(_anSaludNum(x.avisos))} avisos</span>
      </div>${peores}</div>`;
  }).join('');
  const sin = (a.flujos_no_evaluados || []).length
    ? `<div style="font-size:var(--fs-sm);color:var(--warn-tx);">Sin mirar por el tope: ${esc(a.flujos_no_evaluados.join(', '))}</div>` : '';
  const tope = a.tope ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">${esc(a.tope.texto)}${a.desde_cache ? ' · resultado guardado de ' + esc(_anSaludFecha(a.calculado_utc)) : ''}</div>` : '';
  return `<div class="tabla-card"><div class="tabla-titulo" style="cursor:pointer;" onclick="anSaludAbrir('auditoria')">Reglas entre etapas ${_anSaludPildora(a.nivel)}</div>
    ${a.error ? `<div style="color:var(--err-tx);">${esc(a.error)}</div>` : ''}${flujos}${sin}
    ${a.nota ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(a.nota)}</div>` : ''}${tope}</div>`;
}

function _anSaludCola(c, abierto) {
  if (!c) return '';
  const tipos = abierto === 'cola' && (c.fallidos_por_tipo || []).length
    ? c.fallidos_por_tipo.map(t => `<div class="tabla-fila"><span class="tabla-nombre">${esc(t.tipo)}</span><span>${esc(_anSaludNum(t.n))}</span></div>`).join('') : '';
  return `<div class="tabla-card"><div class="tabla-titulo" style="cursor:pointer;" onclick="anSaludAbrir('cola')">Envíos a Siesa ${_anSaludPildora(c.nivel)}</div>
    <div style="font-size:var(--fs-sm);color:var(--tx2);">${esc(c.texto)}</div>
    ${c.que_hacer ? `<div style="font-size:var(--fs-sm);color:var(--warn-tx);">Qué hacer: ${esc(c.que_hacer)}</div>` : ''}${tipos}</div>`;
}

function _anSaludCobertura(c) {
  if (!c) return '';
  const fila = (nombre, x) => x ? `<div class="tabla-fila"><span class="tabla-nombre">${esc(nombre)}</span>
    <span>${esc(_anSaludPct(x.pct, x.n))}${x.sin_clave ? ' · ' + esc(_anSaludNum(x.sin_clave)) + ' sin clave' : ''}</span></div>` : '';
  return `<div class="tabla-card"><div class="tabla-titulo">Tareas unidas a su pedido ${_anSaludPildora(c.nivel)}</div>
    <div style="font-size:var(--fs-sm);color:var(--tx2);">${esc(c.texto)}</div>
    ${fila('Picking', c.picking)}${fila('Packing', c.packing)}
    ${c.que_hacer ? `<div style="font-size:var(--fs-sm);color:var(--warn-tx);">Qué hacer: ${esc(c.que_hacer)}</div>` : ''}</div>`;
}

function _anSaludCrons(c) {
  if (!c) return '';
  const lista = (c.activos || []).length ? c.activos.map(t => esc(t)).join(' · ') : 'ninguno';
  return `<div class="tabla-card"><div class="tabla-titulo">Crons de este proceso (${esc(c.rol_de_este_proceso)})</div>
    <div style="font-size:var(--fs-sm);color:var(--tx2);">Activos: ${lista}</div>
    ${(c.omitidos || []).length ? `<div style="font-size:var(--fs-sm);color:var(--tx3);">Omitidos: ${c.omitidos.map(t => esc(t)).join(' · ')}</div>` : ''}
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">${esc(c.nota)}</div></div>`;
}

/** El HTML completo de la vista. Función pura: se prueba en Node. */
function anSaludHtml(d, abierto) {
  if (!d) return '';
  const g = d.global || {};
  const r = d.resumen || {};
  const nivelGlobal = AN_SALUD_GLOBAL_NIVEL[g.veredicto] || 'advertencia';
  const razones = (g.razones || []).map(x =>
    `<div style="font-size:var(--fs-sm);color:var(--tx2);margin-top:4px;">${_anSaludPildora(x.nivel)} ${esc(x.texto)}</div>`).join('');
  const frescura = (typeof anFrescura === 'function') ? anFrescura(d.meta) : '';
  const nivelFuentes = r.fuentes_al_dia === r.fuentes_total ? 'ok'
    : (d.fuentes || []).some(f => f.nivel === 'critico') ? 'critico' : 'advertencia';
  const nivelAud = (d.auditoria || {}).nivel || 'advertencia';
  const nivelCola = (d.cola_siesa || {}).nivel || 'advertencia';
  const nivelCob = (d.cobertura_claves || {}).nivel || 'advertencia';
  return `
  <div class="tabla-card" style="border-left:4px solid var(--${nivelGlobal === 'ok' ? 'ok' : nivelGlobal === 'critico' ? 'err' : 'warn'}-brd);">
    <div style="display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap;">
      <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);">${esc(g.texto || 'Sin veredicto')}</div>
      ${_anSaludPildora(nivelGlobal)}
    </div>
    ${razones}
    <div style="margin-top:6px;">${frescura}</div>
  </div>
  <div class="kpi-grid">
    ${_anSaludKpi('fuentes', `${_anSaludNum(r.fuentes_al_dia)} de ${_anSaludNum(r.fuentes_total)}`, 'Fuentes al día', 'toca para ver cada una', nivelFuentes)}
    ${_anSaludKpi('auditoria', _anSaludNum(r.bloqueantes), 'Hallazgos que bloquean', `${_anSaludNum(r.avisos)} avisos`, nivelAud)}
    ${_anSaludKpi('cola', _anSaludNum(r.jobs_fallidos), 'Envíos a Siesa fallidos', `${_anSaludNum((d.cola_siesa || {}).pendientes)} en cola`, nivelCola)}
    ${_anSaludKpi('cobertura', _anSaludPct(r.cobertura_clave_pct, r.cobertura_clave_n), 'Tareas unidas a su pedido', 'picking y packing del rango', nivelCob)}
  </div>
  ${_anSaludFuentes(d, abierto)}
  ${_anSaludAuditoria(d.auditoria, abierto)}
  ${_anSaludCola(d.cola_siesa, abierto)}
  ${_anSaludCobertura(d.cobertura_claves)}
  ${_anSaludCrons(d.crons)}`;
}
