// ══════════════════════════════════════════════════════════════════════════
// Descarga de kardex — el gesto que enciende los modelos.
//
// Los cuatro modelos (S-B, TSB, ROP, Newsvendor) leen de KardexMovimiento.
// Sin descarga muestran 0, estén en la pestaña que estén. Esto es solo el
// disparador y su estado: NO es una pantalla de decisión y no debe crecer
// hasta serlo.
//
// El resto de endpoints del kardex (descensura, reconciliación, stock diario)
// NO van aquí: su lugar es como procedencia dentro de la pantalla que los usa
// — la demanda corregida junto al SKU que se va a reponer, no en un tab.
// ══════════════════════════════════════════════════════════════════════════

let _KARDEX_POLL = null;

async function kardexCargarPanel() {
  const el = document.getElementById('inv-datos-container');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Cargando estado…</div>';
  try {
    const estado = await get('/api/kardex/descargar/estado');
    _kardexRender(el, estado);
    if (estado.en_curso) _kardexIniciarPoll();
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

function _kardexRender(el, estado) {
  const enCurso = !!estado.en_curso;
  const r = estado.resultado;

  let html = `<div style="border:1px solid var(--brd);border-radius:10px;padding:14px;">
    <div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:4px;">Descargar kardex de Siesa</div>
    <div style="font-size:11px;color:var(--tx3);margin-bottom:10px;">
      Los modelos de Inteligencia leen del kardex. Sin esta descarga, todos muestran 0.
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <label style="font-size:11px;color:var(--tx3);">Desde
        <input type="date" id="kardex-desde" value="2024-01-01"
          style="margin-left:4px;padding:5px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:12px;">
      </label>
      <button onclick="kardexDescargar()" ${enCurso ? 'disabled' : ''}
        style="padding:7px 14px;border:none;border-radius:7px;font-size:12px;font-weight:700;
               cursor:${enCurso ? 'not-allowed' : 'pointer'};
               background:${enCurso ? 'var(--brd)' : 'var(--pm)'};color:#fff;">
        ${enCurso ? 'Descargando…' : 'Descargar'}
      </button>
      <button onclick="kardexVerPerfil()"
        style="padding:7px 12px;border:1px solid var(--pm);border-radius:7px;background:transparent;color:var(--pm);font-size:12px;font-weight:700;cursor:pointer;">
        Verificar perfil
      </button>
      <span style="font-size:11px;color:var(--tx3);">Corre en segundo plano y por tramos: si se corta, se reanuda.</span>
    </div>
    <div id="kardex-perfil-out"></div>
    <div style="margin-top:8px;font-size:11px;color:var(--yellow);border-left:2px solid var(--yellow);padding-left:8px;">
      Son ~17.000 peticiones contra el ERP que factura en los puntos de venta.
      Correr <strong>fuera de horario</strong> y avisando antes — no después de que
      alguien no pueda facturar a media mañana.
    </div>`;

  if (enCurso) {
    html += `<div style="font-size:12px;color:var(--yellow);margin-top:10px;">● En curso — el detalle queda en los logs de Railway.</div>`;
  } else if (r && r.error) {
    html += `<div style="font-size:12px;color:var(--red);margin-top:10px;">Última descarga falló: ${r.error}</div>`;
  } else if (r) {
    // Una descarga PARCIAL es un fallo, no un aviso. Antes las tres formas de
    // terminar —fin, timeout, excepción— daban el mismo mensaje verde.
    const completa = r.ok === true;
    const col = completa ? 'var(--green)' : 'var(--red)';
    const rp = r.rango_pedido || {}, rt = r.rango_traido || {};
    html += `<div style="margin-top:12px;border:1px solid ${col};border-radius:8px;padding:10px;">
      <div style="font-size:13px;font-weight:700;color:${col};margin-bottom:6px;">
        ${completa ? '✓ COMPLETA' : '✗ ' + (r.estado || 'PARCIAL')}
      </div>
      <div style="font-size:11px;color:var(--tx3);line-height:1.7;">
        ${(r.total_descargados || 0).toLocaleString('es-CO')} movimientos ·
        páginas ${r.pagina_inicial || 1}–${r.pagina_final || 0} ·
        ${r.errores || 0} errores · ${r.segundos || 0}s<br>
        <strong>Pedido:</strong> ${rp.desde || '—'} → ${rp.hasta || '—'}<br>
        <strong>Traído:</strong> ${rt.desde || '—'} → ${rt.hasta || '—'}
      </div>
      ${r.detalle_estado ? `<div style="font-size:11px;color:${col};margin-top:6px;">${r.detalle_estado}</div>` : ''}
      ${r.advertencia ? `<div style="font-size:11px;color:var(--red);margin-top:6px;font-weight:700;">${r.advertencia}</div>` : ''}
      ${_kardexPerfil(r.perfil_mensual)}
      ${r._supuesto_de_reanudacion ? `<div style="font-size:10px;color:var(--yellow);margin-top:6px;">${r._supuesto_de_reanudacion}</div>` : ''}
      ${r.reanudar_desde ? `<button onclick="kardexDescargar(${r.reanudar_desde})"
        style="margin-top:8px;padding:6px 12px;border:none;border-radius:6px;background:var(--red);color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
        Reanudar desde la página ${r.reanudar_desde}
      </button>` : ''}
    </div>`;
  } else {
    html += `<div style="font-size:12px;color:var(--tx3);margin-top:10px;">Sin descargas en esta sesión del servidor.</div>`;
  }

  html += '</div>';
  el.innerHTML = html;
}

/** Histograma de filas por mes.
 *
 *  El rango pedido-vs-traído compara la primera y la última fecha: no ve un
 *  agujero en marzo. Esto sí. Doce a dieciocho barras que un humano lee en
 *  cinco segundos — los picos de temporada donde deben estar, ningún mes en
 *  cero, ninguno anómalamente bajo.
 */
function _kardexPerfil(p) {
  if (!p || !p.meses || !p.meses.length) return '';
  const max = Math.max(...p.meses.map(m => m.filas), 1);
  const barras = p.meses.map(m => {
    const h = Math.max(3, Math.round(m.filas / max * 46));
    const col = m.sospechoso ? 'var(--red)' : 'var(--pm)';
    return `<div title="${m.mes}: ${m.filas.toLocaleString('es-CO')} filas"
      style="flex:1;min-width:6px;height:${h}px;background:${col};border-radius:2px 2px 0 0;"></div>`;
  }).join('');

  const limpio = p.sin_huecos;
  const col = limpio ? 'var(--green)' : 'var(--red)';
  return `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--brd);">
    <div style="font-size:11px;font-weight:700;color:var(--tx3);margin-bottom:6px;">
      Filas por mes — el hueco que el rango no ve
    </div>
    <div style="display:flex;gap:2px;align-items:flex-end;height:50px;">${barras}</div>
    <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--tx3);margin-top:3px;">
      <span>${p.meses[0].mes}</span><span>${p.meses[p.meses.length - 1].mes}</span>
    </div>
    <div style="font-size:11px;color:${col};margin-top:6px;">
      ${limpio
        ? '✓ Sin meses vacíos ni anómalamente bajos (mediana ' + (p.mediana_filas || 0).toLocaleString('es-CO') + ' filas/mes)'
        : '✗ Revisar: ' + [...(p.huecos || []), ...(p.meses_ausentes || [])].join(', ')}
    </div>
  </div>`;
}

function _kardexIniciarPoll() {
  if (_KARDEX_POLL) clearInterval(_KARDEX_POLL);
  _KARDEX_POLL = setInterval(async () => {
    try {
      const e = await get('/api/kardex/descargar/estado');
      if (!e.en_curso) {
        clearInterval(_KARDEX_POLL);
        _KARDEX_POLL = null;
        kardexCargarPanel();
      }
    } catch (_) {
      clearInterval(_KARDEX_POLL);
      _KARDEX_POLL = null;
    }
  }, 10000);
}

/** Perfil del kardex ALMACENADO, sin depender de una descarga reciente.
 *
 *  Es el paso de verificación humana antes de /reconstruir: el servidor puede
 *  haberse reiniciado y perdido el resultado de la última descarga, pero el
 *  kardex sigue ahí y su forma se puede mirar en cualquier momento.
 */
async function kardexVerPerfil() {
  const out = document.getElementById('kardex-perfil-out');
  if (out) out.innerHTML = '<div style="font-size:11px;color:var(--tx3);padding:6px 0;">Calculando perfil…</div>';
  try {
    const p = await get('/api/kardex/perfil-mensual');
    if (out) out.innerHTML = _kardexPerfil(p) ||
      '<div style="font-size:11px;color:var(--tx3);padding:6px 0;">Kardex vacío.</div>';
  } catch (e) {
    if (out) out.innerHTML = `<div style="font-size:11px;color:var(--red);">${e.message || e}</div>`;
  }
}

async function kardexDescargar(paginaInicial) {
  const desde = (document.getElementById('kardex-desde') || {}).value || '2024-01-01';
  try {
    const r = await post('/api/kardex/descargar', {
      fecha_desde: desde.replace(/-/g, ''),
      pagina_inicial: paginaInicial || 1,
    });
    alerta(r.mensaje || 'Descarga iniciada', 'exito');
    kardexCargarPanel();
  } catch (e) {
    alerta('Error: ' + (e.message || e), 'error');
  }
}
