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
      <button onclick="kardexProbarPaginacion()"
        style="padding:7px 12px;border:1px solid var(--yellow);border-radius:7px;background:transparent;color:var(--yellow);font-size:12px;font-weight:700;cursor:pointer;">
        Probar orden
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
        ${r.errores || 0} errores · ${r.segundos || 0}s
        ${r.duplicados_omitidos ? '· <strong>' + r.duplicados_omitidos.toLocaleString('es-CO') + ' duplicados omitidos</strong>' : ''}<br>
        ${r.total_declarado_por_siesa != null
          ? '<strong>Siesa declara:</strong> ' + r.total_declarado_por_siesa.toLocaleString('es-CO') + ' registros<br>' : ''}
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

  // ── Los dos pasos que van DESPUÉS de la descarga, en ese orden ──────────
  //
  // Descargar → reconstruir → verificar. No es estética: reconstruir sobre una
  // descarga incompleta fabrica días sin stock, y verificar antes de
  // reconstruir no dice nada sobre la serie que los modelos van a leer.
  const completa = !!(r && r.ok === true);
  html += `<div style="border:1px solid var(--brd);border-radius:10px;padding:14px;margin-top:12px;">
    <div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:4px;">
      Reconstruir stock diario</div>
    <div style="font-size:11px;color:var(--tx3);margin-bottom:10px;line-height:1.6;">
      Calcula qué stock había cada día, hacia atrás desde el saldo actual. Sin
      esto no se sabe qué días un SKU estuvo agotado — y una demanda medida sobre
      días sin mercancía se lee como baja cuando en realidad <b>no había qué
      vender</b>. Es lo que Reposición y el Armador llaman «demanda censurada».
    </div>
    ${completa ? '' : `<div style="font-size:11px;color:var(--yellow);border-left:2px solid var(--yellow);padding-left:8px;margin-bottom:10px;">
      La última descarga no quedó COMPLETA. El servidor va a rechazar la
      reconstrucción — y con razón: sobre un kardex truncado inventaría días sin
      movimiento.</div>`}
    <button onclick="kardexReconstruir(false)" ${enCurso ? 'disabled' : ''}
      style="padding:7px 14px;border:none;border-radius:7px;font-size:12px;font-weight:700;
             cursor:${enCurso ? 'not-allowed' : 'pointer'};
             background:${enCurso ? 'var(--brd)' : 'var(--pm)'};color:#fff;">
      Reconstruir stock diario
    </button>
    <div id="kardex-reconstruir-out" style="margin-top:10px;"></div>
  </div>

  <div style="border:1px solid var(--brd);border-radius:10px;padding:14px;margin-top:12px;">
    <div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:4px;">
      ¿Le creo al kardex?</div>
    <div style="font-size:11px;color:var(--tx3);margin-bottom:10px;line-height:1.6;">
      La compuerta de completitud. Revisa que <b>ningún concepto quede sin
      clasificar</b> —uno solo deja un agujero que ningún modelo reporta— y
      muestra las unidades vendidas por mes y bodega para cruzarlas contra las
      facturas de Siesa. Ese cruce lo tiene que hacer una persona: el otro lado
      del número vive en el ERP.
    </div>
    <button onclick="kardexReconciliar()"
      style="padding:7px 14px;border:1px solid var(--pm);border-radius:7px;background:transparent;color:var(--pm);font-size:12px;font-weight:700;cursor:pointer;">
      Verificar completitud
    </button>
    <div id="kardex-reconciliar-out" style="margin-top:10px;"></div>
  </div>`;

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
/** ¿Es estable el orden de la consulta paginada?
 *
 *  De eso depende que reanudar sea seguro. Dos peticiones separadas en el
 *  tiempo contra la misma página: si el contenido cambia, la reanudación
 *  multi-sesión produciría huecos y hay que hacerlo en una sola corrida.
 */
async function kardexProbarPaginacion() {
  const out = document.getElementById('kardex-perfil-out');
  if (out) out.innerHTML = '<div style="font-size:11px;color:var(--tx3);padding:6px 0;">Diagnosticando… 4 peticiones, ~2 minutos. No cierres la pestaña.</div>';
  try {
    const r = await post('/api/kardex/probar-paginacion', { pagina: 50, espera_s: 90 });
    if (!out) return;
    if (r.error) { out.innerHTML = `<div style="font-size:11px;color:var(--red);">${r.error}</div>`; return; }
    const ok = r.se_puede_paginar;
    const col = ok ? (r.causa_probable === 'DERIVA_POR_INSERCION' ? 'var(--yellow)' : 'var(--green)') : 'var(--red)';
    const corto = Object.keys(r).find(k => k.startsWith('iguales_tras_') && k !== 'iguales_tras_5s');
    out.innerHTML = `<div style="margin-top:8px;border:1px solid ${col};border-radius:8px;padding:10px;">
      <div style="font-size:13px;font-weight:700;color:${col};">
        ${ok ? 'Paginación USABLE' : 'Paginación NO USABLE'} — ${r.causa_probable}
      </div>
      <div style="font-size:11px;color:var(--tx3);margin-top:6px;line-height:1.7;">
        Página ${r.pagina}, ${r.filas} filas.<br>
        Tras <strong>5s</strong>: ${r.iguales_tras_5s}/${r.filas} en la misma posición
        ${r.estable_corto ? '<span style="color:var(--green);">(estable)</span>' : '<span style="color:var(--red);">(ya cambió)</span>'}<br>
        Tras <strong>90s</strong>: ${r[corto] ?? '—'}/${r.filas}
        ${r.estable_largo ? '<span style="color:var(--green);">(estable)</span>' : '<span style="color:var(--red);">(cambió)</span>'}<br>
        Solape con la página siguiente: <strong>${r.solape_con_pagina_siguiente}</strong> filas
        ${r.solape_con_pagina_siguiente ? '<span style="color:var(--red);"> — páginas contiguas no deberían compartir ninguna</span>' : ''}
      </div>
      <div style="font-size:11px;color:${col};margin-top:8px;">${r.veredicto}</div>
    </div>`;
  } catch (e) {
    if (out) out.innerHTML = `<div style="font-size:11px;color:var(--red);">${e.message || e}</div>`;
  }
}

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

// ═══════════════════════════════════════════════════════════════════════════
// RECONSTRUIR STOCK DIARIO — el botón que dos pantallas ya prometían
//
// `compras_ia.js` dice, en dos lugares distintos y en rojo: «Correr
// *Reconstruir stock diario* en Inventario › Datos». Ese botón **no existía**.
// El endpoint sí, con su lógica de rechazo y todo; lo que faltaba era el gesto.
//
// Una pantalla que da una instrucción imposible de seguir es peor que una que
// no dice nada: quien la lee va, no lo encuentra, y aprende que los avisos del
// sistema no llevan a ningún lado.
//
// QUÉ HACE: reconstruye el stock de cada día hacia atrás, desde el saldo actual
// restando movimientos. Sin esa serie no se sabe qué días un SKU estuvo agotado,
// y sin eso la demanda queda CENSURADA — se lee como baja cuando en realidad no
// había qué vender. Es la precondición de la descensura, no un botón suelto.
// ═══════════════════════════════════════════════════════════════════════════

/** Reconstruye la serie de stock diario. Respeta el rechazo del servidor.
 *
 * El endpoint es DENY-BY-DEFAULT: se niega si la última descarga no quedó
 * COMPLETA, porque reconstruir sobre un kardex truncado **inventa días sin
 * movimiento** — demanda censurada fabricada por el descargador, que contamina
 * justo el modelo que existe para corregir la censura.
 *
 * El override existe y no se esconde: se muestra con el motivo del rechazo a la
 * vista. Un rechazo que no se puede saltar se saltea por fuera del sistema.
 */
async function kardexReconstruir(forzar) {
  const out = document.getElementById('kardex-reconstruir-out');
  if (!out) return;
  out.innerHTML = '<div style="font-size:12px;color:var(--tx3);padding:8px 0;">Reconstruyendo…</div>';
  try {
    const r = await fetch(API + '/api/kardex/reconstruir', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(forzar ? { forzar: true } : {}),
    });
    const d = await r.json();

    // LOS DOS RECHAZOS SON 409 — no se distinguen por el status.
    //
    // «Hay una descarga en curso» y «la última descarga no quedó COMPLETA»
    // devuelven ambos 409. Distinguirlos por el código hacía que el segundo se
    // mostrara como el primero: «esperá a que termine la descarga» sobre un
    // problema que no se arregla esperando, y sin ofrecer el override. La
    // diferencia está en el CUERPO: el rechazo por calidad trae `por_que` y
    // `override`; el de «ahora no», no.
    //
    // Lo encontró el test de contrato, no la pantalla: aislado el mensaje se
    // veía razonable.
    const esRechazoPorCalidad = !!(d.por_que || d.override || d.estado_descarga);
    if (r.status === 409 && !esRechazoPorCalidad) {
      out.innerHTML = `<div style="border:1px solid var(--yellow);border-radius:8px;padding:10px;font-size:12px;color:var(--yellow);">
        ${d.error || 'Hay una descarga en curso.'}
        <div style="color:var(--tx3);margin-top:4px;">Reconstruir ahora usaría datos a medias. Esperá a que termine.</div></div>`;
      return;
    }
    if (!r.ok) {
      // El rechazo del servidor trae POR QUÉ y QUÉ HACER. Se muestran los dos:
      // un "RECHAZADO" pelado manda a la gente a buscar el override sin
      // entender qué está saltando.
      out.innerHTML = `<div style="border:1px solid var(--red);border-radius:8px;padding:10px;">
        <div style="font-size:12px;font-weight:700;color:var(--red);margin-bottom:6px;">
          ${d.error || 'No se pudo reconstruir'}</div>
        ${d.estado_descarga ? `<div style="font-size:11px;color:var(--tx3);">Última descarga: <b>${d.estado_descarga}</b></div>` : ''}
        ${d.por_que ? `<div style="font-size:11px;color:var(--tx2);margin-top:6px;">${d.por_que}</div>` : ''}
        ${d.que_hacer ? `<div style="font-size:11px;color:var(--tx2);margin-top:6px;"><b>Qué hacer:</b> ${d.que_hacer}</div>` : ''}
        ${forzar ? '' : `<button onclick="kardexReconstruirForzar()"
          style="margin-top:10px;padding:6px 12px;border:1px solid var(--red);border-radius:6px;background:transparent;color:var(--red);font-size:11px;font-weight:700;cursor:pointer;">
          Reconstruir igual (queda registrado)</button>`}
      </div>`;
      return;
    }

    out.innerHTML = `<div style="border:1px solid var(--green);border-radius:8px;padding:10px;font-size:12px;color:var(--tx2);">
      <b style="color:var(--green);">✓ Reconstruido</b>
      ${d.dias != null ? ` · ${Number(d.dias).toLocaleString('es-CO')} día(s)` : ''}
      ${d.referencias != null ? ` · ${Number(d.referencias).toLocaleString('es-CO')} referencia(s)` : ''}
      ${forzar ? '<div style="color:var(--yellow);margin-top:6px;font-size:11px;">Se forzó sobre una descarga incompleta — la demanda censurada de este cálculo puede estar inventada.</div>' : ''}
    </div>`;
  } catch (e) {
    out.innerHTML = `<div style="font-size:12px;color:var(--red);">Sin conexión: ${e.message}</div>`;
  }
}

/** El override. Confirma nombrando lo que se está saltando. */
function kardexReconstruirForzar() {
  if (!confirm(
      'La última descarga del kardex NO quedó completa.\n\n' +
      'Reconstruir igual inventa días sin movimiento donde en realidad no se ' +
      'descargaron los datos. Esos días falsos se leen como "agotado", inflan ' +
      'la corrección por censura y contaminan el ROP y la temporada — sin una ' +
      'sola alarma.\n\n' +
      '¿Reconstruir de todos modos?')) return;
  kardexReconstruir(true);
}

// ═══════════════════════════════════════════════════════════════════════════
// ¿LE CREO AL KARDEX? — la compuerta de completitud
//
// Dos preguntas que nadie podía hacerse desde la aplicación:
//
//   1. ¿Hay conceptos de movimiento SIN CLASIFICAR? Uno solo detiene el
//      cálculo: un concepto que el sistema no sabe si es demanda o no, y que
//      se omite, es un agujero invisible en toda la serie.
//   2. ¿Las unidades del kardex cuadran con las facturas de Siesa? Si difieren
//      más de 2% en alguna bodega, la descarga está incompleta.
//
// La segunda NO la puede contestar el sistema solo —la factura vive en Siesa—
// así que la pantalla muestra el número del kardex y dice contra qué cruzarlo.
// ═══════════════════════════════════════════════════════════════════════════

async function kardexReconciliar() {
  const out = document.getElementById('kardex-reconciliar-out');
  if (!out) return;
  out.innerHTML = '<div style="font-size:12px;color:var(--tx3);padding:8px 0;">Cruzando…</div>';
  let d;
  try {
    d = await get('/api/kardex/reconciliar?meses=12');
  } catch (e) {
    out.innerHTML = `<div style="font-size:12px;color:var(--red);">${e.message}</div>`;
    return;
  }

  const ok = d.compuerta_ok === true;
  const sinClasificar = Object.entries(d.conceptos_detalle || {})
    .filter(([, v]) => !v.clasificado);

  let html = `<div style="border:1px solid ${ok ? 'var(--green)' : 'var(--red)'};border-radius:8px;padding:10px;">
    <div style="font-size:13px;font-weight:700;color:${ok ? 'var(--green)' : 'var(--red)'};">
      ${ok ? '✓ Compuerta abierta' : '✗ Compuerta CERRADA'}
    </div>
    <div style="font-size:11px;color:var(--tx3);margin-top:4px;">
      ${(d.total_registros_kardex || 0).toLocaleString('es-CO')} movimientos ·
      ${(d.bodegas_con_datos || []).length} bodega(s): ${(d.bodegas_con_datos || []).join(', ') || '—'}
    </div>`;

  if (!ok) {
    html += `<div style="font-size:11px;color:var(--red);margin-top:8px;line-height:1.6;">
      <b>${sinClasificar.length} concepto(s) sin clasificar.</b> Cada uno es un
      movimiento que el sistema no sabe si es demanda. Omitirlos deja un agujero
      que ningún modelo va a reportar — hay que agregarlos a
      <code>CONCEPTO_DEFINICION</code> antes de calcular nada.
      <div style="margin-top:6px;">
        ${sinClasificar.map(([c, v]) =>
          `<div>· concepto <b>${c}</b> — ${v.registros.toLocaleString('es-CO')} registro(s)</div>`).join('')}
      </div></div>`;
  }

  // El cruce contra Siesa. Se muestran las unidades del kardex por mes×bodega:
  // el otro lado del cruce está en el ERP y lo tiene que mirar una persona.
  const ventas = d.ventas_por_mes_bodega || [];
  if (ventas.length) {
    html += `<details style="margin-top:10px;">
      <summary style="font-size:11px;color:var(--pm);cursor:pointer;">
        Unidades vendidas según el kardex (${ventas.length} fila(s)) — para cruzar contra Siesa
      </summary>
      <div style="font-size:11px;color:var(--tx2);margin:6px 0;">${d.instruccion || ''}</div>
      <div style="max-height:260px;overflow:auto;">
      <table style="width:100%;border-collapse:collapse;font-size:11px;">
        <thead><tr style="color:var(--tx3);text-align:right;border-bottom:1px solid var(--brd);">
          <th style="text-align:left;padding:4px;">Mes</th><th style="padding:4px;">Bodega</th>
          <th style="padding:4px;">Unidades</th><th style="padding:4px;">Registros</th></tr></thead>
        <tbody>${ventas.map(v => `<tr style="text-align:right;border-bottom:1px solid var(--brd);">
          <td style="text-align:left;padding:4px;color:var(--tx);">${v.mes}</td>
          <td style="padding:4px;color:var(--tx3);">${v.bodega}</td>
          <td style="padding:4px;color:var(--tx);">${Math.round(v.unidades_vendidas).toLocaleString('es-CO')}</td>
          <td style="padding:4px;color:var(--tx3);">${v.registros}</td></tr>`).join('')}
        </tbody></table></div></details>`;
  }

  html += '</div>';
  out.innerHTML = html;
}
