/**
 * Retención de cartera — el bloque del tablero y el respaldo en el WMS.
 *
 * La vía principal para decidir es el Gestor de Cartera (API servicio a
 * servicio `/api/cartera/*`). Acá se VE lo retenido y, si el usuario tiene el
 * permiso `puede_autorizar_cartera`, se decide con motivo. Ningún botón
 * promete un 403: los de decidir solo aparecen con el permiso que el servidor
 * confirma (`puede_autorizar`).
 *
 * Todo dato que se pinta va con `esc()`; en los `onclick` solo viajan
 * posiciones de `CARTERA_RETENIDAS`.
 */

let CARTERA_RETENIDAS = [];
let CARTERA_PUEDE_AUTORIZAR = false;
let CARTERA_USUARIO_ID = null;
let CARTERA_PUEDE_LOTE = false;

function _carteraPesos(v) {
  if (v === null || v === undefined) return 'sin dato';
  return '$' + Math.round(Number(v)).toLocaleString('es-CO');
}

function _carteraAntiguedad(h) {
  if (h === null || h === undefined) return '';
  if (h < 24) return `${Math.round(h)} h`;
  return `${Math.floor(h / 24)} d`;
}

/** Bloque del tablero: pedidos retenidos por cartera y su antigüedad. */
async function carteraCargarBloque() {
  const el = document.getElementById('cartera-bloque');
  if (!el) return;
  let d;
  try {
    d = await get('/api/cartera/panel/retenciones?estado=RETENIDO');
  } catch (e) {
    if (e.status === 403) { el.style.display = 'none'; return; }
    el.style.display = '';
    el.innerHTML = `<div style="font-size:var(--fs-xs);color:var(--err-tx);">No se pudo leer cartera: ${esc(e.message || '')}</div>`;
    return;
  }
  el.style.display = '';
  CARTERA_RETENIDAS = d.retenciones || [];
  CARTERA_PUEDE_AUTORIZAR = !!d.puede_autorizar;
  CARTERA_USUARIO_ID = d.usuario_id || null;
  CARTERA_PUEDE_LOTE = !!d.puede_autorizar_lote;
  const n = CARTERA_RETENIDAS.length;
  const viejas = CARTERA_RETENIDAS.filter(r => (r.antiguedad_horas || 0) > 72).length;
  const filas = CARTERA_RETENIDAS.slice(0, 20).map((r, i) => {
    const motivos = (r.motivos || []).filter(m => m.retiene).map(m => m.texto).join(' · ');
    const propio = CARTERA_USUARIO_ID && r.iniciado_por && r.iniciado_por.id === CARTERA_USUARIO_ID;
    const botones = [
      CARTERA_PUEDE_AUTORIZAR && !propio && (r.acciones || []).includes('autorizar')
        ? `<button onclick="carteraAutorizar(${i})" style="padding:6px 10px;border-radius:6px;border:1px solid var(--warn-brd);background:var(--warn-bg);color:var(--warn-tx);font-size:var(--fs-xs);font-weight:700;cursor:pointer;">Autorizar crédito</button>` : '',
      CARTERA_PUEDE_AUTORIZAR && !propio
        ? `<button onclick="carteraConvertir(${i})" style="padding:6px 10px;border-radius:6px;border:1px solid var(--info-brd);background:var(--info-bg);color:var(--info-tx);font-size:var(--fs-xs);font-weight:700;cursor:pointer;">Pasar a contado</button>` : '',
      `<button onclick="carteraReevaluar(${i})" style="padding:6px 10px;border-radius:6px;border:1px solid var(--brd);background:var(--bg-input);color:var(--tx);font-size:var(--fs-xs);font-weight:600;cursor:pointer;">Re-evaluar</button>`,
    ].filter(Boolean).join(' ');
    const soloContado = (r.acciones || []).includes('convertir_contado')
      && !(r.acciones || []).includes('autorizar');
    const nota = (propio ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">Lo iniciaste vos: lo autoriza otra persona.</div>` : '')
      + (soloContado ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">El cliente tiene un acuerdo de pago vigente: este pedido solo puede salir de contado.</div>` : '');
    return `<div style="border-top:1px solid var(--brd);padding:8px 0;">
        <div style="display:flex;justify-content:space-between;gap:8px;">
          <div style="font-size:var(--fs-sm);font-weight:700;color:var(--tx);">${esc(r.pedido || r.pedido_clave)} · ${esc(r.cliente || r.nit)}</div>
          <div style="font-size:var(--fs-xs);color:var(--tx3);white-space:nowrap;">${esc(_carteraAntiguedad(r.antiguedad_horas))}</div>
        </div>
        <div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:2px;">${esc(_carteraPesos(r.valor))} · ${esc(r.cond_pago || '')} · ${esc(r.compuerta_texto || 'retenido')}</div>
        <div style="font-size:var(--fs-xs);color:var(--warn-tx);margin-top:2px;">${esc(motivos)}</div>
        ${nota}
        <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;">${botones}</div>
      </div>`;
  }).join('');
  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div>
        <div style="font-size:var(--fs-sm);font-weight:800;color:var(--warn-tx);">⛔ Retenidos por cartera</div>
        <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:2px;">Crédito real con mora, sin cupo o sobre el cupo. Contado siempre sale.</div>
      </div>
      <div style="text-align:right;">
        <span style="font-size:var(--fs-md);font-weight:800;color:var(--warn-tx);">${esc(n)}</span>
        ${viejas ? `<div style="font-size:var(--fs-xs);color:var(--err-tx);">${esc(viejas)} con más de 3 días</div>` : ''}
      </div>
    </div>
    ${n ? filas : `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">Ningún pedido retenido.</div>`}
    ${n > 20 ? `<div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">Mostrando 20 de ${esc(n)}.</div>` : ''}
    ${CARTERA_PUEDE_LOTE ? `<button onclick="carteraLoteVer()" style="width:100%;margin-top:10px;padding:8px;border-radius:8px;border:1px solid var(--brd);background:var(--bg-input);color:var(--tx2);font-size:var(--fs-xs);font-weight:600;cursor:pointer;">Paradas de crédito anteriores a la regla de contado</button>` : ''}`;
}

async function _carteraMotivo(titulo) {
  const m = await _modalTexto('Motivo', esc(titulo).replace(/\n/g, '<br>'),
                              { obligatorio: true, textoConfirmar: 'Continuar' });
  if (m === null) return null;
  return m.trim();
}

async function carteraAutorizar(i) {
  const r = CARTERA_RETENIDAS[i];
  if (!r) return;
  const motivo = await _carteraMotivo(`Autorizar a crédito el pedido ${r.pedido || ''} (${r.cliente || ''}).\nMotivo (queda en la bitácora con su nombre):`);
  if (motivo === null) return;
  const tope = await _modalCantidad('Tope de la autorización',
    'Tope en pesos que cubre esta autorización. Si lo empacado lo supera, vuelve a retener.',
    { min: 1, valorInicial: Math.round(Number(r.valor || 0)), textoConfirmar: 'Autorizar' });
  if (tope === null) return;
  try {
    await post(`/api/cartera/panel/retenciones/${r.id}/autorizar`,
               { motivo, tope_valor: tope });
    alerta('Autorizado — el pedido puede seguir', 'exito');
  } catch (e) { alerta(e.message || 'No se pudo autorizar', 'error'); }
  carteraCargarBloque();
}

async function carteraConvertir(i) {
  const r = CARTERA_RETENIDAS[i];
  if (!r) return;
  const motivo = await _carteraMotivo(`Pasar a CONTADO el pedido ${r.pedido || ''}: la factura sale para cobrar al entregar.\nMotivo:`);
  if (motivo === null) return;
  try {
    await post(`/api/cartera/panel/retenciones/${r.id}/convertir-contado`, { motivo });
    alerta('Convertido a contado — el conductor lo cobra al entregar', 'exito');
  } catch (e) { alerta(e.message || 'No se pudo convertir', 'error'); }
  carteraCargarBloque();
}

async function carteraReevaluar(i) {
  const r = CARTERA_RETENIDAS[i];
  if (!r) return;
  try {
    const d = await post(`/api/cartera/panel/retenciones/${r.id}/reevaluar`, {});
    const x = d.retencion || {};
    alerta(x.estado === 'LIBERADO_PAGO' ? 'Liberado: ya no hay mora ni exceso de cupo'
                                        : 'Sigue retenido: ' + (x.resumen || ''),
           x.estado === 'LIBERADO_PAGO' ? 'exito' : 'advertencia');
  } catch (e) { alerta(e.message || 'No se pudo re-evaluar', 'error'); }
  carteraCargarBloque();
}

async function carteraLoteVer() {
  let d;
  try {
    d = await get('/api/cartera/panel/credito-lote');
  } catch (e) { alerta(e.message || 'No se pudo leer', 'error'); return; }
  if (!d.n) { alerta(`Ninguna parada anterior al ${d.corte} espera autorización`, 'info'); return; }
  const motivo = await _carteraMotivo(`${d.n} parada(s) confirmadas antes del ${d.corte} figuran como crédito no autorizado.\nMotivo común para autorizarlas (queda por parada en la bitácora):`);
  if (motivo === null) return;
  try {
    const r = await post('/api/cartera/panel/credito-lote', { motivo });
    alerta(`${r.autorizadas} parada(s) autorizadas`, 'exito');
  } catch (e) { alerta(e.message || 'No se pudo autorizar el lote', 'error'); }
}

/** Una caja retenida en el cierre que cartera ya liberó: se cierra de nuevo con sus bultos. */
async function carteraCerrarLiberado(packingId) {
  if (!(await _modalConfirmar('Se cierra con las piezas ya declaradas y se envía a Siesa.',
      { titulo: 'Cartera liberó este pedido: ¿cerrar la caja?', textoConfirmar: 'Cerrar caja' }))) return;
  // El texto del desenlace lo decide la misma función que el empaque
  // (`empMensajeCierre`, packing.js): en cola no es «procesado».
  const _msj = (d, s) => (typeof empMensajeCierre === 'function'
    ? empMensajeCierre(d, s) : { texto: (d && d.error) || 'Caja cerrada', tipo: s < 300 ? 'exito' : 'error' });
  try {
    const d = await post(`/api/packing/${packingId}/cerrar`, { bultos: [] });
    const m = _msj(d, 200); alerta(m.texto, m.tipo);
  } catch (e) { const m = _msj(e.body || { error: e.message }, e.status); alerta(m.texto, m.tipo); }
  if (typeof cargarPedidos === 'function') setTimeout(cargarPedidos, 800);
}
