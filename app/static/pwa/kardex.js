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
      <span style="font-size:11px;color:var(--tx3);">Corre en segundo plano. Puede tardar varios minutos.</span>
    </div>`;

  if (enCurso) {
    html += `<div style="font-size:12px;color:var(--yellow);margin-top:10px;">● En curso — el detalle queda en los logs de Railway.</div>`;
  } else if (r && r.error) {
    html += `<div style="font-size:12px;color:var(--red);margin-top:10px;">Última descarga falló: ${r.error}</div>`;
  } else if (r) {
    html += `<div style="font-size:12px;color:var(--green);margin-top:10px;">
      ✓ ${(r.total_descargados || 0).toLocaleString('es-CO')} movimientos · ${r.paginas || 0} páginas · ${r.errores || 0} errores
    </div>`;
  } else {
    html += `<div style="font-size:12px;color:var(--tx3);margin-top:10px;">Sin descargas en esta sesión del servidor.</div>`;
  }

  html += '</div>';
  el.innerHTML = html;
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

async function kardexDescargar() {
  const desde = (document.getElementById('kardex-desde') || {}).value || '2024-01-01';
  try {
    const r = await post('/api/kardex/descargar', { fecha_desde: desde.replace(/-/g, '') });
    alerta(r.mensaje || 'Descarga iniciada', 'exito');
    kardexCargarPanel();
  } catch (e) {
    alerta('Error: ' + (e.message || e), 'error');
  }
}
