/**
 * 🕒 Flota → Jornada del conductor (Fase 0, 2026-09-24).
 *
 * Lo que cada conductor dejó registrado en su día, a qué hora y con qué
 * confianza. Arriba, una fila por conductor que un jefe de bodega lee de un
 * vistazo; al tocarla, sus días; al tocar un día, la línea de tiempo con los
 * tramos sin registro resaltados y las señales con su evidencia.
 *
 * Todo número sale del servidor (`app/services/jornada_conductor.py`): acá no
 * se calcula ninguna tasa ni ningún umbral. La pantalla dice lo que no se sabe
 * («sin base», «no reconstruible», «hora del servidor») en vez de pintarlo en
 * cero. Un tramo sin registro es «sin explicar»: el dato no dice qué hizo la
 * persona, y la pantalla tampoco.
 *
 * Entrada: `flotaJornadaCargar`, con el contenedor (elemento o id). La
 * sub-pestaña de Flota que la llama la cablea el integrador. (Sin paréntesis
 * a propósito: el detector de alcance de `test_frontend_integrity` lee un
 * `nombre(` en el código de arranque como una llamada, y un comentario no
 * conecta nada.)
 *
 * Los botones pasan posiciones, nunca datos: el conductor y el día se buscan
 * en lo que ya está en memoria (`_FJ`).
 */

const _FJ = { el: null, desde: null, hasta: null, d: null, i: null, j: null, dia: null, pedido: 0 };

const FJ_ESTADO = {
  reconstruida: ['ok', 'Jornada reconstruida'],
  parcial: ['advertencia', 'Se ve solo un tramo'],
  no_reconstruible: ['neutro', 'Jornada no reconstruible'],
  sin_actividad: ['neutro', 'Sin actividad registrada'],
};

const FJ_MOTIVOS = {
  CLIENTE_CERRADO: 'cliente cerrado', FUERA_DE_HORARIO: 'fuera de horario',
  NO_PAGO_SE_QUEDO: 'no pagó y se quedó la mercancía', DIRECCION_ERRADA: 'dirección errada',
  NO_PIDIO: 'no pidió', MERCANCIA_AVERIADA: 'mercancía averiada', NO_PAGO: 'no pagó',
};

const FJ_ENTREGA = {
  ENTREGADO: 'entregado', PARCIAL: 'entrega parcial', RECHAZADO: 'rechazado',
  ENTREGADO_SIN_PAGO: 'entregado sin pago',
};

const FJ_UBICACION = { sede: 'en la sede', taller: 'en el taller', fuera_de_sede: 'fuera de sede' };

/** Entrada desde Flota. */
function flotaJornadaCargar(el) {
  const cont = (typeof el === 'string') ? document.getElementById(el) : el;
  if (!cont) return Promise.resolve();
  _FJ.el = cont;
  _FJ.i = null;
  _FJ.j = null;
  return fjCargarResumen();
}

// ── Piezas de estilo ────────────────────────────────────────────────────────

function fjPildora(nivel, texto) {
  const t = {
    ok: 'background:var(--ok-bg);color:var(--ok-tx);border:1px solid var(--ok-brd);',
    advertencia: 'background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);',
    critico: 'background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd);',
    info: 'background:var(--info-bg);color:var(--info-tx);border:1px solid var(--info-brd);',
    neutro: 'background:var(--bg-s2);color:var(--tx2);border:1px solid var(--brd);',
  }[nivel] || '';
  return `<span style="${t}display:inline-block;padding:2px 8px;border-radius:12px;font-size:var(--fs-xs);font-weight:700;white-space:nowrap;">${esc(texto)}</span>`;
}

function fjCaja(nivel, cuerpo) {
  const t = {
    advertencia: 'background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);',
    info: 'background:var(--info-bg);color:var(--info-tx);border:1px solid var(--info-brd);',
    critico: 'background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd);',
    neutro: 'background:var(--bg-s2);color:var(--tx);border:1px solid var(--brd);',
  }[nivel] || '';
  return `<div style="${t}border-radius:10px;padding:10px 12px;margin:8px 0;font-size:var(--fs-sm);line-height:1.45;">${cuerpo}</div>`;
}

/** «1 h 50 min» / «35 min» / «sin dato». */
function fjDuracion(min) {
  if (min === null || min === undefined) return 'sin dato';
  const m = Math.round(Number(min));
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h} h ${r} min` : `${h} h`;
}

/** Un decimal con coma: 0,3 · 4,0. */
function fjNum(x) {
  if (x === null || x === undefined) return 'sin dato';
  return Number(x).toFixed(1).replace('.', ',');
}

function fjPesos(x) {
  if (x === null || x === undefined) return 'sin dato';
  return '$' + Math.round(Number(x)).toLocaleString('es-CO');
}

function fjPct(x) {
  if (x === null || x === undefined) return 'sin dato';
  return `${Math.round(Number(x) * 100)} %`;
}

function fjSumarDias(iso, n) {
  const d = new Date(iso + 'T12:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

const FJ_BOTON = 'padding:10px 14px;min-height:44px;font-size:var(--fs-sm);font-weight:600;background:var(--bg-s2);color:var(--tx);border:1px solid var(--brd-b);border-radius:10px;cursor:pointer;';
const FJ_TARJETA = 'background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:12px 14px;margin:8px 0;';

// ── Resumen del período ─────────────────────────────────────────────────────

async function fjCargarResumen() {
  const el = _FJ.el;
  const n = ++_FJ.pedido;
  if (!_FJ.d) el.innerHTML = `<div style="padding:16px;color:var(--tx2);font-size:var(--fs-sm);">Cargando la jornada de los conductores…</div>`;
  let d;
  try {
    const q = (_FJ.desde && _FJ.hasta)
      ? '?desde=' + encodeURIComponent(_FJ.desde) + '&hasta=' + encodeURIComponent(_FJ.hasta) : '';
    d = await get('/api/jornada/resumen' + q);
  } catch (e) {
    if (n !== _FJ.pedido) return;
    el.innerHTML = fjCaja('critico', `No se pudo cargar la jornada: ${esc((e && e.message) || e)}`);
    return;
  }
  if (n !== _FJ.pedido) return;
  if (d && d.error) {
    el.innerHTML = fjCaja('critico', esc(d.error));
    return;
  }
  _FJ.d = d;
  _FJ.desde = d.desde;
  _FJ.hasta = d.hasta;
  el.innerHTML = fjHtmlResumen(d);
}

function fjRango(dias) {
  const hasta = _FJ.hasta || (_FJ.d && _FJ.d.hasta);
  if (!hasta) return fjCargarResumen();
  _FJ.desde = fjSumarDias(hasta, -(dias - 1));
  return fjCargarResumen();
}

function fjAplicarRango() {
  const a = document.getElementById('fj-desde');
  const b = document.getElementById('fj-hasta');
  if (a && a.value) _FJ.desde = a.value;
  if (b && b.value) _FJ.hasta = b.value;
  return fjCargarResumen();
}

function fjHtmlResumen(d) {
  const filas = (d.conductores || []).map((f, i) => fjHtmlFila(f, i)).join('')
    || `<div style="color:var(--tx2);font-size:var(--fs-sm);padding:10px 0;">Ningún conductor registrado.</div>`;
  const tel = d.hora_del_telefono || {};
  const avisoTel = tel.disponible ? '' : fjCaja('advertencia', `⚠️ ${esc(tel.declaracion || '')}`);
  return `<div style="max-width:980px;margin:0 auto;">
    <div style="display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:8px;">
      <div>
        <div style="font-size:var(--fs-xl);font-weight:800;color:var(--tx);">🕒 Jornada de los conductores</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);">Del ${esc(d.desde)} al ${esc(d.hasta)} · lo que cada uno dejó registrado. Un tramo sin registro es «sin explicar»: pregúntele antes de concluir.</div>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
        <input id="fj-desde" type="date" class="input-field" value="${esc(d.desde)}" style="max-width:160px;font-size:var(--fs-sm);">
        <input id="fj-hasta" type="date" class="input-field" value="${esc(d.hasta)}" style="max-width:160px;font-size:var(--fs-sm);">
        <button style="${FJ_BOTON}" onclick="fjAplicarRango()">Ver</button>
        <button style="${FJ_BOTON}" onclick="fjRango(7)">7 días</button>
        <button style="${FJ_BOTON}" onclick="fjRango(30)">30 días</button>
      </div>
    </div>
    ${avisoTel}
    ${filas}
    ${fjHtmlPie(d)}
  </div>`;
}

/** La frase que un jefe de bodega lee de corrido. */
function fjFraseResumen(f) {
  const partes = [];
  const cc = f.cierre_cargue || {};
  if (cc.mediana) {
    let comp = ' (sin compañeros para comparar)';
    if (cc.pares_mediana && cc.base === 'con_base') comp = ` (su ruta suele ${cc.pares_mediana})`;
    else if (cc.pares_mediana) comp = ` (sus compañeros: ${cc.pares_mediana}, pocos casos)`;
    partes.push(`Cerró cargue ${cc.mediana}${comp}`);
  }
  const p = f.paradas || {};
  if (f.jornadas) {
    partes.push(`${p.propias || 0} ${p.propias === 1 ? 'entrega' : 'entregas'}`
      + (p.por_otro ? ` y ${p.por_otro} confirmadas por otra persona` : ''));
  }
  const ne = f.no_explicado || {};
  if (f.jornadas && (ne.minutos === null || ne.minutos === undefined)) {
    partes.push('jornada no reconstruible');
  } else if (ne.minutos > 0) {
    partes.push(`${fjDuracion(ne.minutos)} sin explicar`);
  } else if (f.jornadas) {
    partes.push('nada sin explicar');
  }
  for (const [mot, t] of Object.entries(f.rechazos || {})) {
    if (!t || !t.observado) continue;
    const nombre = FJ_MOTIVOS[mot] || mot;
    const palabra = t.observado === 1 ? 'rechazo' : 'rechazos';
    partes.push(t.base === 'con_base'
      ? `${t.observado} ${palabra} por ${nombre}, sus compañeros en esa ruta: ${fjNum(t.esperado_pares)}`
      : `${t.observado} ${palabra} por ${nombre} (sin base para comparar)`);
  }
  const km = f.km_entre_turnos || {};
  if (km.km > 0) partes.push(`${km.km} km entre turnos, sin nadie a cargo`);
  if (!f.jornadas) partes.push('sin actividad registrada en el período');
  return partes.map((x) => esc(x)).join(' · ');
}

function fjHtmlFila(f, i) {
  const s = f.senales_abiertas || 0;
  const pildoras = [
    s ? fjPildora('advertencia', `${s} ${s === 1 ? 'señal' : 'señales'}`) : fjPildora('ok', 'sin señales'),
    f.cobertura_media === null || f.cobertura_media === undefined
      ? '' : fjPildora(f.cobertura_media >= 0.75 ? 'ok' : 'neutro', `registro ${fjPct(f.cobertura_media)}`),
  ].join(' ');
  const hj = f.horas_jornada || {};
  const sub = [
    `${f.jornadas} ${f.jornadas === 1 ? 'jornada' : 'jornadas'}`,
    f.reconstruidas ? `${f.reconstruidas} ${f.reconstruidas === 1 ? 'completa' : 'completas'}` : '',
    f.parciales ? `${f.parciales} solo en tramo` : '',
    f.no_reconstruibles ? `${f.no_reconstruibles} no reconstruibles` : '',
    hj.mediana !== null && hj.mediana !== undefined ? `jornada típica ${fjNum(hj.mediana)} h` : '',
    f.rutas && f.rutas.con_base ? `rutas más largas que el 90 % de sus compañeros: ${f.rutas.sobre_p90} de ${f.rutas.con_base}` : '',
  ].filter(Boolean).map((x) => esc(x)).join(' · ');
  const nombre = f.conductor ? f.conductor.nombre : '';
  const sinCuenta = f.conductor && !f.conductor.tiene_cuenta
    ? ` ${fjPildora('neutro', 'sin cuenta en la app')}` : '';
  return `<div role="button" tabindex="0" onclick="fjAbrirConductor(${i})" style="${FJ_TARJETA}cursor:pointer;">
    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;align-items:center;">
      <strong style="font-size:var(--fs-md);color:var(--tx);">${esc(nombre)}</strong>
      <span>${pildoras}${sinCuenta}</span>
    </div>
    <div style="font-size:var(--fs-sm);color:var(--tx);margin-top:6px;line-height:1.45;">${fjFraseResumen(f)}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:4px;">${sub}</div>
  </div>`;
}

// ── Los días de un conductor ────────────────────────────────────────────────

function fjAbrirConductor(i) {
  const f = _FJ.d && _FJ.d.conductores ? _FJ.d.conductores[i] : null;
  if (!f) return;
  _FJ.i = i;
  _FJ.j = null;
  _FJ.el.innerHTML = fjHtmlConductor(f);
}

function fjVolver() {
  _FJ.i = null;
  _FJ.j = null;
  if (_FJ.d) _FJ.el.innerHTML = fjHtmlResumen(_FJ.d);
}

function fjVolverConductor() {
  if (_FJ.i === null) return fjVolver();
  return fjAbrirConductor(_FJ.i);
}

function fjHtmlConductor(f) {
  const ci = _FJ.i;
  const dias = (f.dias || []).map((d, j) => {
    const [niv, txt] = FJ_ESTADO[d.estado] || ['neutro', d.estado];
    const horas = d.horas !== null && d.horas !== undefined
      ? `${d.primer_evento}–${d.ultimo_evento} · ${fjNum(d.horas)} h`
      : (d.tramo_observado_h !== null && d.tramo_observado_h !== undefined
        ? `se ve ${d.primer_evento}–${d.ultimo_evento}` : 'sin horas');
    const ne = d.no_explicado_min === null || d.no_explicado_min === undefined
      ? '' : (d.no_explicado_min > 0 ? `${fjDuracion(d.no_explicado_min)} sin explicar` : 'nada sin explicar');
    const partes = [horas, `${d.paradas} entregas`, ne,
      d.senales ? `${d.senales} ${d.senales === 1 ? 'señal' : 'señales'}` : ''].filter(Boolean);
    const porRevisar = d.senales ? fjPildora('advertencia', d.senales + ' por revisar') : '';
    return `<div role="button" tabindex="0" onclick="fjAbrirDia(${ci}, ${j})" style="${FJ_TARJETA}cursor:pointer;">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
        <strong style="color:var(--tx);font-size:var(--fs-md);">${esc(d.dia)}</strong>
        <span>${fjPildora(niv, txt)} ${porRevisar}</span>
      </div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-top:4px;">${partes.map((x) => esc(x)).join(' · ')}</div>
    </div>`;
  }).join('') || `<div style="color:var(--tx2);font-size:var(--fs-sm);">Sin jornadas en el período.</div>`;
  return `<div style="max-width:980px;margin:0 auto;">
    <button style="${FJ_BOTON}" onclick="fjVolver()">← Todos los conductores</button>
    <div style="font-size:var(--fs-xl);font-weight:800;color:var(--tx);margin:10px 0 2px;">${esc(f.conductor ? f.conductor.nombre : '')}</div>
    <div style="font-size:var(--fs-sm);color:var(--tx);line-height:1.45;">${fjFraseResumen(f)}</div>
    ${dias}
  </div>`;
}

// ── Un día: la línea de tiempo ─────────────────────────────────────────────

async function fjAbrirDia(i, j) {
  const f = _FJ.d && _FJ.d.conductores ? _FJ.d.conductores[i] : null;
  const d = f && f.dias ? f.dias[j] : null;
  if (!f || !d) return;
  _FJ.i = i;
  _FJ.j = j;
  const n = ++_FJ.pedido;
  _FJ.el.innerHTML = `<div style="padding:16px;color:var(--tx2);font-size:var(--fs-sm);">Armando la jornada del ${esc(d.dia)}…</div>`;
  let jor;
  try {
    jor = await get(`/api/jornada?dia=${encodeURIComponent(d.dia)}&conductor_id=${encodeURIComponent(f.conductor.id)}`);
  } catch (e) {
    if (n !== _FJ.pedido) return;
    _FJ.el.innerHTML = `<button style="${FJ_BOTON}" onclick="fjVolverConductor()">← Volver</button>`
      + fjCaja('critico', `No se pudo cargar el día: ${esc((e && e.message) || e)}`);
    return;
  }
  if (n !== _FJ.pedido) return;
  _FJ.dia = jor;
  _FJ.el.innerHTML = jor && jor.error
    ? `<button style="${FJ_BOTON}" onclick="fjVolverConductor()">← Volver</button>` + fjCaja('critico', esc(jor.error))
    : fjHtmlDia(jor);
}

function fjHtmlDia(j) {
  const [niv, txt] = FJ_ESTADO[j.estado] || ['neutro', j.estado];
  const cob = j.cobertura || {};
  const faltan = (cob.faltan || []).map((x) => `<li>${esc(x)}</li>`).join('');
  const estado = j.estado === 'reconstruida' ? '' : fjCaja(
    j.estado === 'parcial' ? 'advertencia' : 'neutro',
    `<strong>${esc(txt)}.</strong> ${esc(j.motivo_estado || '')}${faltan ? `<ul style="margin:6px 0 0 18px;padding:0;">${faltan}</ul>` : ''}`);
  const senales = (j.senales || []).map((s) => fjHtmlSenal(s)).join('');
  const bloqueSenales = senales
    ? `<div style="font-size:var(--fs-lg);font-weight:700;color:var(--tx);margin:14px 0 4px;">Para revisar con el conductor</div>${senales}`
    : `<div style="font-size:var(--fs-sm);color:var(--tx2);margin:10px 0;">Ninguna señal ese día.</div>`;
  // Lo que se miró y no se pudo juzgar (un tramo de km en duda). No es
  // «normal»: se dice, y se dice qué falta.
  const noEval = (j.senales_no_evaluables || []).map((x) =>
    `<li>${esc(x.texto)}</li>`).join('');
  const bloqueNoEval = noEval
    ? fjCaja('neutro', `<strong>No se pudo revisar.</strong><ul style="margin:6px 0 0 18px;padding:0;">${noEval}</ul>`)
    : '';
  return `<div style="max-width:980px;margin:0 auto;">
    <button style="${FJ_BOTON}" onclick="fjVolverConductor()">← Sus días</button>
    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;align-items:center;margin:10px 0 2px;">
      <div style="font-size:var(--fs-xl);font-weight:800;color:var(--tx);">${esc(j.conductor ? j.conductor.nombre : '')} · ${esc(j.dia)}</div>
      <span>${fjPildora(niv, txt)} ${fjPildora('neutro', `registro ${fjPct(cob.valor)}`)}</span>
    </div>
    ${estado}
    ${fjHtmlIndicadores(j)}
    ${bloqueSenales}
    ${bloqueNoEval}
    <div style="font-size:var(--fs-lg);font-weight:700;color:var(--tx);margin:14px 0 4px;">Línea de tiempo</div>
    ${fjHtmlLinea(j)}
    ${fjHtmlPie(j)}
  </div>`;
}

function fjKpi(valor, etiqueta) {
  return `<div style="${FJ_TARJETA}flex:1 1 140px;margin:4px;">
    <div style="font-size:var(--fs-xl);font-weight:800;color:var(--tx);">${esc(valor)}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx2);">${esc(etiqueta)}</div></div>`;
}

function fjHtmlIndicadores(j) {
  const jj = j.jornada || {};
  const p = j.paradas || {};
  const rech = Object.values(p.rechazos_por_motivo || {}).reduce((a, b) => a + b, 0);
  const kpis = [
    fjKpi(jj.primer_evento || 'sin dato', 'Primer registro suyo'),
    fjKpi(jj.ultimo_evento || 'sin dato', 'Último registro suyo'),
    fjKpi(jj.horas !== null && jj.horas !== undefined ? `${fjNum(jj.horas)} h`
      : (jj.tramo_observado_h !== null && jj.tramo_observado_h !== undefined
        ? `${fjNum(jj.tramo_observado_h)} h vistas` : 'no se sabe'), 'Jornada'),
    fjKpi(jj.no_explicado_min === null || jj.no_explicado_min === undefined
      ? 'no se sabe' : fjDuracion(jj.no_explicado_min), 'Sin explicar'),
    fjKpi(String(p.propias || 0), 'Entregas confirmadas por él'),
    fjKpi(String(rech), 'Rechazos'),
  ].join('');
  const rutas = (j.rutas || []).map((r) => {
    const dp = r.duracion_pares || {};
    const dur = r.duracion_h === null || r.duracion_h === undefined ? 'duración sin dato'
      : `duró ${fjNum(r.duracion_h)} h` + (dp.base === 'con_base'
        ? ` (sus compañeros: ${fjNum(dp.mediana)} h, el 90 % en ${fjNum(dp.p90)} h o menos · n=${dp.n})`
        : ` (sin base de compañeros: n=${dp.n || 0})`);
    const cierre = r.cierre_cargue ? `cerró cargue ${r.cierre_cargue}`
      + (r.cierre_cargue_pares ? ` (compañeros ${r.cierre_cargue_pares})` : '') : 'sin cierre de cargue';
    const partes = [`Ruta ${r.maestra || 'sin maestra'}`, r.placa || '', cierre, dur,
      `${r.paradas_propias} de ${r.paradas} ${r.paradas === 1 ? 'parada confirmada' : 'paradas confirmadas'} por él`,
      r.forzada ? 'cerrada a la fuerza por la oficina' : ''].filter(Boolean);
    return `<div style="font-size:var(--fs-sm);color:var(--tx);padding:6px 0;border-top:1px solid var(--brd);">${partes.map((x) => esc(x)).join(' · ')}</div>`;
  }).join('');
  return `<div style="display:flex;flex-wrap:wrap;margin:0 -4px;">${kpis}</div>${rutas}`;
}

const FJ_ETIQUETAS = {
  hora: 'Hora', cliente: 'Cliente', municipio: 'Municipio', estado: 'Estado',
  motivo_rechazo: 'Motivo', motivo: 'Motivo', placa: 'Placa', km: 'Km',
  segundos: 'Segundos', veredicto: 'Resultado', sin_dato: 'Sin contestar',
  entrego: 'Lo entregó', km_entrega: 'Km al entregar', recibio: 'Lo recibió',
  km_recibo: 'Km al recibir', rol: 'Él', monto: 'Monto', horas: 'Horas',
  cierre_cargue: 'Cierre del cargue', cierre_ruta: 'Cierre de la ruta',
  entrega: 'Entregado el', recibo: 'Recibido el', liquidada: 'Liquidada el',
  paradas: 'Paradas', con_gps: 'Con ubicación', con_foto: 'Con foto',
  hora_gps: 'Hora del GPS', pendiente: 'Sin liquidar',
  desde: 'Desde', hasta: 'Hasta', minutos: 'Minutos', no_explicado_min: 'Sin explicar (min)',
  referencia_min: 'Lo normal (min)', que_es: 'Tramo',
};

function fjValor(v) {
  if (v === null || v === undefined) return 'sin dato';
  if (v === true) return 'sí';
  if (v === false) return 'no';
  if (Array.isArray(v)) return `${v.length}`;
  if (typeof v === 'object') {
    if ('motivo_sin_dato' in v && v.motivo_sin_dato) return `sin ubicación (${v.motivo_sin_dato})`;
    if ('precision_m' in v) return `ubicación ±${v.precision_m} m`;
    return Object.keys(v).join(', ');
  }
  return String(v);
}

function fjHtmlEvidencia(lista) {
  return (lista || []).slice(0, 12).map((item) => {
    const partes = Object.keys(FJ_ETIQUETAS)
      .filter((k) => k in item)
      .map((k) => `${FJ_ETIQUETAS[k]}: ${fjValor(k === 'motivo_rechazo' ? (FJ_MOTIVOS[item[k]] || item[k]) : item[k])}`);
    return `<li>${esc(partes.join(' · '))}</li>`;
  }).join('');
}

function fjHtmlSenal(s) {
  const c = s.comparacion || {};
  const base = c.base === 'con_base'
    ? `comparado con sus compañeros (n=${c.n_pares !== undefined ? c.n_pares : c.n})`
    : (c.base === 'regla' ? 'regla fija' : 'sin base de compañeros: umbral fijo');
  const nivel = s.nivel === 'revisar' ? fjPildora('advertencia', 'para revisar') : fjPildora('info', 'para observar');
  const conf = fjPildora(s.confianza === 'alta' ? 'ok' : (s.confianza === 'media' ? 'info' : 'neutro'),
    `confianza ${s.confianza}`);
  return `<div style="${FJ_TARJETA}">
    <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px;">
      <strong style="color:var(--tx);font-size:var(--fs-md);">${esc(s.titulo)}</strong>
      <span>${nivel} ${conf}</span>
    </div>
    <div style="font-size:var(--fs-sm);color:var(--tx);margin-top:6px;line-height:1.45;">${esc(s.texto)}</div>
    <div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:4px;">${esc(base)}</div>
    <ul style="margin:6px 0 0 18px;padding:0;font-size:var(--fs-xs);color:var(--tx2);">${fjHtmlEvidencia(s.evidencia)}</ul>
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-top:6px;">${esc(s.que_hacer)}</div>
  </div>`;
}

/** La etiqueta de la hora: de dónde sale y cuánto creerle. */
function fjChipHora(e) {
  let nivel = 'info';
  let texto = 'hora del servidor';
  if (e.hora_fuente === 'telefono') {
    nivel = e.confianza === 'alta' ? 'ok' : 'info';
    texto = e.confianza === 'alta' ? 'hora del teléfono' : 'hora del teléfono, corregida';
  } else if (e.confianza === 'baja') {
    nivel = 'advertencia';
    texto = 'hora del servidor · pudo llegar tarde';
  }
  return `<span title="${esc(e.confianza_motivo || '')}">${fjPildora(nivel, texto)}</span>`;
}

function fjDetalleEvento(e) {
  const d = e.detalle || {};
  const p = [];
  switch (e.tipo) {
    case 'recibo_turno':
      p.push(d.placa, `${d.km} km`, `se lo entregó ${d.entregado_por}`);
      if (d.registrado_por_otra_persona) p.push('lo registró otra persona');
      break;
    case 'entrega_turno':
      p.push(d.placa, `${d.km} km`);
      if (d.km_turno !== null && d.km_turno !== undefined) p.push(`${d.km_turno} km en el turno`);
      if (d.queda) p.push(`queda ${FJ_UBICACION[d.queda] || d.queda}`);
      if (d.motivo_ubicacion) p.push(`motivo: ${d.motivo_ubicacion}`);
      if (d.recibe) p.push(`lo recibe ${d.recibe}`);
      if (d.cierre_forzado) p.push(`cierre forzado por la oficina: ${d.motivo_cierre_forzado || 'sin motivo'}`);
      break;
    case 'preoperacional':
      p.push(d.placa, d.veredicto, `${d.segundos_llenado} s para ${d.items} ítems`);
      if (d.hecha_por_otra_persona) p.push('la hizo otra persona');
      break;
    case 'cargue':
      p.push(`${d.bultos} bultos`, e.hora_fin ? `hasta ${e.hora_fin}` : '', d.nota);
      break;
    case 'cierre_cargue':
      p.push(`ruta ${d.maestra || 'sin maestra'}`,
        d.paradas ? `${d.paradas} ${d.paradas === 1 ? 'parada' : 'paradas'}` : '', d.nota);
      break;
    case 'tanqueo':
      p.push(d.placa, `${d.km} km`);
      break;
    case 'cierre_ruta':
    case 'cierre_ruta_forzado':
    case 'liquidacion':
      p.push(`ruta ${d.maestra || 'sin maestra'}`);
      if (e.tipo === 'cierre_ruta') p.push('no se guarda quién la cerró');
      break;
    default:
      if (e.tipo.indexOf('parada') === 0) {
        p.push(d.cliente ? `${d.cliente}${d.municipio ? ` (${d.municipio})` : ''}` : 'cliente sin nombre');
        p.push(FJ_ENTREGA[d.estado] || d.estado);
        if (d.motivo_rechazo) p.push(FJ_MOTIVOS[d.motivo_rechazo] || d.motivo_rechazo);
        if (d.forma_pago) p.push(`${d.forma_pago.toLowerCase()} ${fjPesos(d.monto_cobrado)}`);
        if (d.monto_descuento > 0) p.push(`descuento ${fjPesos(d.monto_descuento)}`);
        p.push(e.lat !== null && e.lat !== undefined
          ? `con ubicación${e.precision_m ? ` ±${e.precision_m} m` : ''}`
          : `sin ubicación${d.gps && d.gps.motivo_sin_dato ? ` (${d.gps.motivo_sin_dato})` : ''}`);
        p.push(d.con_foto ? 'con foto' : 'sin foto');
        if (d.reconfirmada) p.push('la corrigió después');
      }
  }
  return p.filter(Boolean).map((x) => esc(x)).join(' · ');
}

function fjHtmlEvento(e) {
  const ajeno = e.propio ? '' : ` ${fjPildora('neutro', 'no es un registro suyo')}`;
  const servidor = e.hora_fuente === 'telefono' && e.hora_servidor && e.hora_servidor !== e.hora
    ? `<div style="font-size:var(--fs-xs);color:var(--tx3);">llegó al servidor a las ${esc(e.hora_servidor)}</div>` : '';
  return `<div style="display:flex;gap:10px;padding:8px 0;border-top:1px solid var(--brd);">
    <div style="min-width:56px;font-size:var(--fs-lg);font-weight:800;color:var(--tx);">${esc(e.hora)}</div>
    <div style="flex:1;min-width:0;">
      <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;">
        <strong style="color:var(--tx);font-size:var(--fs-sm);">${esc(e.titulo)}</strong>${fjChipHora(e)}${ajeno}
      </div>
      <div style="font-size:var(--fs-sm);color:var(--tx2);margin-top:2px;overflow-wrap:anywhere;">${fjDetalleEvento(e)}</div>
      ${servidor}
    </div>
  </div>`;
}

function fjHtmlHueco(h) {
  const normal = h.base === 'con_base'
    ? `lo normal para sus compañeros: hasta ${fjDuracion(h.referencia_min)} (n=${h.n_pares})`
    : `sin base de compañeros: se compara contra ${fjDuracion(h.referencia_min)} fijos`;
  const baja = h.confianza === 'baja'
    ? ' · con hora del servidor: una parada pudo llegar tarde por falta de señal' : '';
  if (h.no_explicado_min > 0) {
    return fjCaja('advertencia',
      `⏸ <strong>${esc(fjDuracion(h.minutos))} sin registro</strong> ${esc(h.que_es)} (${esc(h.desde)}–${esc(h.hasta)}) — `
      + `<strong>${esc(fjDuracion(h.no_explicado_min))} sin explicar</strong>`
      + `<div style="font-size:var(--fs-xs);margin-top:4px;">${esc(normal + baja)}</div>`);
  }
  const normalTxt = fjDuracion(h.minutos) + ' ' + h.que_es + ': dentro de lo normal';
  return `<div style="font-size:var(--fs-xs);color:var(--tx3);padding:4px 0 4px 66px;">${esc(normalTxt)}</div>`;
}

function fjHtmlLinea(j) {
  const huecos = {};
  for (const h of (j.huecos || [])) huecos[h.despues_de] = h;
  const filas = (j.eventos || []).map((e, k) => fjHtmlEvento(e) + (huecos[k] ? fjHtmlHueco(huecos[k]) : ''));
  return filas.join('') || `<div style="font-size:var(--fs-sm);color:var(--tx2);">Ningún registro ese día.</div>`;
}

// ── Lo que no se puede ver, y el aviso legal ───────────────────────────────

function fjHtmlPie(d) {
  const nopuede = (d.no_puede_ver || []).map((x) => `<li>${esc(x)}</li>`).join('');
  const tel = d.hora_del_telefono ? `<div style="margin-top:6px;">${esc(d.hora_del_telefono.declaracion || '')}</div>` : '';
  const caidas = (d.fuentes_no_disponibles || []).length
    ? fjCaja('advertencia', `Fuentes que no se pudieron leer: ${esc(d.fuentes_no_disponibles.join('; '))}`) : '';
  const u = d.umbrales || {};
  const umbral = u.n_minimo_pares
    ? `<div style="margin-top:6px;">Se compara contra otros conductores de la misma ruta maestra, ${esc(u.ventana_pares_dias)} días hacia atrás; con menos de ${esc(u.n_minimo_pares)} casos la comparación sale «sin base» y no se propone nada.</div>` : '';
  return `${caidas}<details style="margin:16px 0 8px;font-size:var(--fs-xs);color:var(--tx2);">
    <summary style="cursor:pointer;font-size:var(--fs-sm);color:var(--tx);">Lo que esta vista no puede ver</summary>
    <ul style="margin:6px 0 0 18px;padding:0;">${nopuede}</ul>
    ${tel}${umbral}
  </details>
  ${fjCaja('neutro', `<span style="font-size:var(--fs-xs);">${esc(d.aviso_legal || '')}</span>`)}`;
}
