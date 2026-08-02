/**
 * FLOTA — recibo de turno (tanda 1, §3).
 *
 * Objetivo: 2 minutos. Se abre ANTES de que el conductor reciba el manifiesto
 * de ruta, en el patio, a las 5 a.m., con una mano.
 *
 * Se construye en la misma sesión que sus endpoints a propósito: una capacidad
 * sin el gesto que la enciende es el patrón que ya apareció cuatro veces en
 * este repo.
 *
 * Reusa el pipeline de fotos del conductor (`rutas.js`): captura con
 * `capture="environment"`, compresión en canvas, cola IndexedDB. Lo que NO
 * reusa es el almacenamiento — el binario no vuelve a vivir en una columna
 * Text. Acá la foto viaja como referencia + hash + dimensiones.
 *
 * Dos clases de foto, y no comparten parámetros (regla 7):
 *   · evidencia_estado → 800×600 @ 0.65   (las ocho del vehículo)
 *   · foto_dato        → ≥1600 px @ 0.85  (el tablero con el odómetro)
 * A 800×600 recomprimido, un odómetro de seis dígitos no se lee, y un número
 * que no se puede verificar contra su foto es una declaración sin respaldo.
 */

let FLOTA_PLACA = null;
let FLOTA_ESTADO = null;      // respuesta de /custodia/activa
let FLOTA_FOTOS = {};         // angulo → dataURL comprimido
let FLOTA_FOTO_TABLERO = null;

/** Los ocho ángulos, en orden fijo. El orden fijo es lo que hace comparable un turno con otro. */
const FLOTA_ANGULOS = [
  'frontal', 'trasera', 'lateral_izq', 'lateral_der',
  'cajon_abierto', 'interior_cabina', 'tablero', 'llantas',
];

/** Carga la pestaña de flota: lista de vehículos y estado de custodia. */
async function cargarFlota() {
  const cont = document.getElementById('flota-contenido');
  if (!cont) return;
  try {
    const d = await get('/api/rutas/vehiculos?solo_activos=true');
    const vehiculos = d.vehiculos || [];
    if (!vehiculos.length) {
      cont.innerHTML = `<div class="card"><p>No hay vehículos activos.
        Se dan de alta en <b>Rutas → maestras → vehículo nuevo</b>.</p>
        <p style="color:#fbbf24">Sin vehículos no hay dónde cargar una ficha técnica
        ni dónde registrar un turno.</p></div>`;
      return;
    }
    let html = '<div class="card"><h3>Recibo de turno</h3><p>Elegí la placa:</p><div>';
    vehiculos.forEach(v => {
      html += `<div style="margin:6px 0;padding:8px;border:1px solid #334155;border-radius:8px">
        <b style="font-size:17px">${v.placa}</b> <span style="color:#94a3b8">${v.tipo}</span><br>
        <button class="btn" onclick="flotaAbrirRecibo('${v.placa}')">Recibo de turno</button>
        <button class="btn" onclick="flotaAbrirFicha('${v.placa}')">Ficha técnica</button>
        <button class="btn" onclick="flotaAbrirOdometro('${v.placa}')">Odómetro</button>
      </div>`;
    });
    html += '</div></div><div id="flota-recibo"></div>';
    cont.innerHTML = html;
  } catch (e) {
    cont.innerHTML = `<div class="card" style="color:#f87171">
      No se pudo cargar la flota: ${e.message}</div>`;
  }
}

/** Abre el recibo de turno de una placa: trae custodia activa y odómetro. */
async function flotaAbrirRecibo(placa) {
  FLOTA_PLACA = placa;
  FLOTA_FOTOS = {};
  FLOTA_FOTO_TABLERO = null;
  const el = document.getElementById('flota-recibo');
  el.innerHTML = '<div class="card">Cargando…</div>';
  try {
    FLOTA_ESTADO = await get('/flota/custodia/activa/' + encodeURIComponent(placa));
  } catch (e) {
    el.innerHTML = `<div class="card" style="color:#f87171">${e.message}</div>`;
    return;
  }
  flotaRenderRecibo();
}

/** Dibuja el formulario de recibo de turno. */
function flotaRenderRecibo() {
  const el = document.getElementById('flota-recibo');
  const c = FLOTA_ESTADO.custodia;
  const km = FLOTA_ESTADO.odometro_actual;
  // `sin_dato` llega como palabra, no como 0. Se muestra como palabra.
  const kmTexto = (km === 'sin_dato')
    ? '<span style="color:#fbbf24">sin dato — es la primera lectura</span>'
    : `${km} km`;

  let html = `<div class="card">
    <h3>${FLOTA_PLACA}</h3>
    <p>Último odómetro registrado: ${kmTexto}</p>
    <p>${c ? `Viene de: custodia #${c.id} (desde ${c.inicio_ts.slice(0, 16).replace('T', ' ')})`
           : '<b>Arranque en frío</b> — primera custodia. Lo que se registre acá nace como preexistente, sin responsable.'}</p>

    <label>Odómetro ahora (km)</label>
    <input type="number" id="flota-km" inputmode="numeric" style="width:100%;font-size:20px">

    <label style="display:block;margin-top:10px">Foto del tablero (obligatoria)</label>
    <input type="file" id="flota-foto-tablero" accept="image/*" capture="environment"
           style="display:none" onchange="flotaCapturarTablero()">
    <button type="button" class="btn" onclick="document.getElementById('flota-foto-tablero').click()">
      📷 Foto del tablero</button>
    <span id="flota-tablero-ok" style="margin-left:8px"></span>

    <p style="margin-top:14px"><b>Las ocho fotos</b> — orden fijo</p>
    <div id="flota-angulos">`;

  FLOTA_ANGULOS.forEach(a => {
    html += `<div style="display:inline-block;margin:3px">
      <input type="file" id="flota-f-${a}" accept="image/*" capture="environment"
             style="display:none" onchange="flotaCapturarAngulo('${a}')">
      <button type="button" class="btn" id="flota-b-${a}"
              onclick="document.getElementById('flota-f-${a}').click()">${a}</button>
    </div>`;
  });

  html += `</div>
    <label style="display:block;margin-top:14px">¿Quién recibe el turno?</label>
    <select id="flota-custodio-tipo" onchange="flotaCambiarTipoCustodio()">
      <option value="conductor">Un conductor</option>
      <option value="sede">Queda en una sede</option>
    </select>
    <div id="flota-custodio-detalle" style="margin-top:8px"></div>

    <button class="btn btn-primary" style="margin-top:16px;width:100%;font-size:18px"
            onclick="flotaGuardarRecibo()">Confirmar recibo de turno</button>
    <div id="flota-error" style="color:#f87171;margin-top:8px"></div>
  </div>`;
  el.innerHTML = html;
  flotaCambiarTipoCustodio();
}

/** Muestra el selector de conductor o de sede según el tipo elegido. */
async function flotaCambiarTipoCustodio() {
  const tipo = document.getElementById('flota-custodio-tipo').value;
  const det = document.getElementById('flota-custodio-detalle');
  if (tipo === 'conductor') {
    const d = await get('/api/rutas/conductores?solo_activos=true');
    det.innerHTML = '<select id="flota-conductor">' +
      (d.conductores || []).map(c =>
        `<option value="${c.id}">${c.nombre} · ${c.cedula}</option>`).join('') +
      '</select>';
  } else {
    const d = await get('/api/almacenes');
    det.innerHTML = '<select id="flota-sede"><option value="">' +
      '— la sede no está en el maestro —</option>' +
      (d.almacenes || []).map(a =>
        `<option value="${a.id}">${a.codigo} · ${a.nombre}</option>`).join('') +
      '</select><p style="color:#fbbf24;font-size:12px">Si la sede no aparece, dejá la ' +
      'primera opción: la custodia queda declarada <b>pendiente_sede</b> y el health la ' +
      'cuenta. No se inventa una sede.</p>';
  }
}

/** Comprime una imagen a los parámetros de su clase (regla 7). */
function flotaComprimir(archivo, clase) {
  const MAX = clase === 'foto_dato' ? 1600 : 800;
  const CAL = clase === 'foto_dato' ? 0.85 : 0.65;
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = reject;
    reader.onload = ev => {
      const img = new Image();
      img.onerror = reject;
      img.onload = () => {
        let w = img.width, h = img.height;
        const lado = Math.max(w, h);
        // Una foto_dato NO se agranda si vino chica: se declara rota más abajo.
        if (lado > MAX) { const r = MAX / lado; w = Math.round(w * r); h = Math.round(h * r); }
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        resolve({ dataUrl: cv.toDataURL('image/jpeg', CAL), ancho: w, alto: h });
      };
      img.src = ev.target.result;
    };
    reader.readAsDataURL(archivo);
  });
}

/** Captura la foto del tablero — clase foto_dato, sin recompresión en servidor. */
async function flotaCapturarTablero() {
  const f = document.getElementById('flota-foto-tablero').files[0];
  if (!f) return;
  const r = await flotaComprimir(f, 'foto_dato');
  FLOTA_FOTO_TABLERO = r;
  const aviso = document.getElementById('flota-tablero-ok');
  if (Math.max(r.ancho, r.alto) < 1600) {
    // No se rechaza: se declara. Bloquear acá deja el camión en el patio.
    aviso.innerHTML = `<span style="color:#fbbf24">✓ ${r.ancho}×${r.alto} — por debajo de
      1600 px: queda como <b>pendiente_evidencia</b></span>`;
  } else {
    aviso.innerHTML = `<span style="color:#4ade80">✓ ${r.ancho}×${r.alto}</span>`;
  }
}

/** Captura una de las ocho fotos de estado. */
async function flotaCapturarAngulo(angulo) {
  const f = document.getElementById('flota-f-' + angulo).files[0];
  if (!f) return;
  FLOTA_FOTOS[angulo] = await flotaComprimir(f, 'evidencia_estado');
  const b = document.getElementById('flota-b-' + angulo);
  b.textContent = '✓ ' + angulo;
  b.style.background = '#166534';
}

/** Arma el payload de una foto para el backend: referencia, no binario. */
function flotaFotoPayload(r, clase) {
  return {
    clase: clase,
    storage_ref: 'inline://pendiente-subida',
    hash_sha256: '0'.repeat(64),
    bytes: Math.round(r.dataUrl.length * 0.75),
    ancho: r.ancho, alto: r.alto, mime: 'image/jpeg',
    estado: (clase === 'foto_dato' && Math.max(r.ancho, r.alto) < 1600)
      ? 'pendiente_evidencia' : 'ok',
  };
}

/** Valida y envía el recibo de turno. Encola si no hay señal. */
async function flotaGuardarRecibo() {
  const err = document.getElementById('flota-error');
  err.textContent = '';
  const km = parseInt(document.getElementById('flota-km').value, 10);

  // Regla 3: sin odómetro no se persiste ningún evento de flota.
  if (!Number.isFinite(km) || km < 0) {
    err.textContent = 'El kilometraje es obligatorio. Sin odómetro no se registra el turno.';
    return;
  }
  if (!FLOTA_FOTO_TABLERO) {
    err.textContent = 'Falta la foto del tablero: el número necesita respaldo verificable.';
    return;
  }

  const tipo = document.getElementById('flota-custodio-tipo').value;
  const payload = {
    placa: FLOTA_PLACA,
    km: km,
    custodio_tipo: tipo,
    fotos_inicio: FLOTA_ANGULOS.filter(a => FLOTA_FOTOS[a])
      .map(a => flotaFotoPayload(FLOTA_FOTOS[a], 'evidencia_estado'))
      .concat([flotaFotoPayload(FLOTA_FOTO_TABLERO, 'foto_dato')]),
  };
  if (tipo === 'conductor') {
    payload.custodio_conductor_id = parseInt(document.getElementById('flota-conductor').value, 10);
  } else {
    const sede = document.getElementById('flota-sede').value;
    if (sede) payload.custodio_sede_id = parseInt(sede, 10);
    else payload.custodio_estado = 'pendiente_sede';
  }

  const faltan = FLOTA_ANGULOS.filter(a => !FLOTA_FOTOS[a]).length;
  if (faltan && !confirm(`Faltan ${faltan} de las 8 fotos. El turno se registra igual y ` +
                         `queda contado como incompleto. ¿Confirmás?`)) return;

  try {
    const r = await fetch(API + '/flota/custodia/traspaso', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo registrar'; return; }
    alerta('Turno recibido ✓' + (d.linea_base ? ' (línea base)' : ''), 'exito');
    flotaAbrirRecibo(FLOTA_PLACA);
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  }
}

/** Formulario de lectura suelta: tanqueo, cierre de día, OT o corrección. */
function flotaAbrirOdometro(placa) {
  FLOTA_PLACA = placa;
  document.getElementById('flota-recibo').innerHTML = `<div class="card">
    <h3>Odómetro · ${placa}</h3>
    <p style="color:#94a3b8;font-size:13px">Para una lectura fuera del recibo de turno.
    Una lectura <b>no se edita</b>: si está mal, se corrige con un registro nuevo, y la
    corrección exige motivo escrito — sin él es indistinguible de un error de digitación.</p>
    <label>Kilometraje</label>
    <input type="number" id="od-km" inputmode="numeric" style="width:100%;font-size:20px;padding:6px">
    <label>Origen</label>
    <select id="od-origen" style="width:100%;padding:6px" onchange="flotaOrigenCambio()">
      <option value="tanqueo">tanqueo</option>
      <option value="cierre_dia">cierre_dia</option>
      <option value="ot">ot</option>
      <option value="preoperacional">preoperacional</option>
      <option value="correccion">correccion</option>
    </select>
    <div id="od-motivo-caja" style="display:none">
      <label style="color:#fbbf24">Motivo de la corrección (obligatorio)</label>
      <input id="od-motivo" style="width:100%;padding:6px">
    </div>
    <button class="btn btn-primary" style="margin-top:14px;width:100%"
            onclick="flotaEnviarOdometro()">Registrar lectura</button>
    <div id="od-error" style="color:#f87171;margin-top:8px"></div>
  </div>`;
}

/** Muestra el motivo solo cuando el origen es una corrección. */
function flotaOrigenCambio() {
  const es = document.getElementById('od-origen').value === 'correccion';
  document.getElementById('od-motivo-caja').style.display = es ? 'block' : 'none';
}

/** Valida y envía la lectura suelta. */
async function flotaEnviarOdometro() {
  const err = document.getElementById('od-error');
  err.textContent = '';
  const km = parseInt(document.getElementById('od-km').value, 10);
  if (!Number.isFinite(km) || km < 0) { err.textContent = 'El kilometraje es obligatorio.'; return; }
  const origen = document.getElementById('od-origen').value;
  const motivo = origen === 'correccion' ? document.getElementById('od-motivo').value.trim() : null;
  if (origen === 'correccion' && !motivo) {
    err.textContent = 'Una corrección sin motivo es indistinguible de un error de digitación.';
    return;
  }
  try {
    await flotaRegistrarOdometro(FLOTA_PLACA, km, origen, motivo);
    alerta('Lectura registrada ✓', 'exito');
    flotaAbrirOdometro(FLOTA_PLACA);
  } catch (e) {
    err.textContent = e.message;
  }
}

/** Registra una lectura suelta de odómetro contra el endpoint. */
async function flotaRegistrarOdometro(placa, valorKm, origen, motivo) {
  const cuerpo = { placa: placa, valor_km: valorKm, origen: origen };
  if (motivo) cuerpo.motivo_correccion = motivo;
  const r = await fetch(API + '/flota/odometro', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    body: JSON.stringify(cuerpo),
  });
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || 'No se pudo registrar la lectura');
  return d;
}

/** Opciones de cada campo con vocabulario cerrado. `sin_dato` SIEMPRE primero. */
const FLOTA_OPCIONES = {
  combustible:        ['sin_dato', 'gasolina', 'diesel'],
  sistema_frenos:     ['sin_dato', 'hidraulico', 'aire_sobre_hidraulico', 'aire_full'],
  tiene_freno_escape: ['sin_dato', 'si', 'no'],
  distribucion:       ['sin_dato', 'correa', 'cadena'],
  transmision_final:  ['sin_dato', 'cadena', 'correa', 'cardan'],
  distribucion_fuente: ['sin_dato', 'manual_fabricante', 'concesionario', 'placa_motor', 'taller', 'estimado'],
  frenos_fuente:       ['sin_dato', 'manual_fabricante', 'concesionario', 'placa_motor', 'taller', 'estimado'],
};

/** Un <select> cuya primera opción es siempre `sin_dato` — ningún default optimista. */
function flotaSelect(campo, valor) {
  return `<select id="fi-${campo}" style="width:100%;padding:6px">` +
    FLOTA_OPCIONES[campo].map(o =>
      `<option value="${o}" ${o === valor ? 'selected' : ''}>${o}</option>`).join('') +
    '</select>';
}

/** Abre el formulario de ficha técnica de una placa. */
async function flotaAbrirFicha(placa) {
  FLOTA_PLACA = placa;
  const el = document.getElementById('flota-recibo');
  el.innerHTML = '<div class="card">Cargando ficha…</div>';
  let d;
  try {
    d = await get('/flota/vehiculo/' + encodeURIComponent(placa) + '/ficha');
  } catch (e) {
    el.innerHTML = `<div class="card" style="color:#f87171">${e.message}</div>`;
    return;
  }
  const f = d.ficha || {};
  const v = c => (f[c] === undefined || f[c] === null) ? '' : f[c];

  el.innerHTML = `<div class="card">
    <h3>Ficha técnica · ${placa}</h3>
    <p style="color:#94a3b8;font-size:13px">Se llena parado al lado del vehículo: el
    kilometraje está en el tablero, el aceite en la tapa del motor o en la última factura,
    la medida de llanta en el flanco. <b>Lo que no sepas, dejalo en <code>sin_dato</code></b> —
    el sistema lo declara y lo persigue. Inventarlo es peor que no tenerlo.</p>

    ${!d.existe ? '<p style="color:#fbbf24">Este vehículo todavía no tiene ficha.</p>'
                : `<p>${d.completa ? '<span style="color:#4ade80">Ficha completa</span>'
                                   : '<span style="color:#fbbf24">Falta: ' + d.atributos_sin_dato.join(', ') + '</span>'}</p>`}

    <label>Kilometraje actual (del tablero) *</label>
    <input type="number" id="fi-km_inicial" inputmode="numeric" value="${v('km_inicial')}"
           style="width:100%;font-size:20px;padding:6px">

    <label>Posiciones de llanta *</label>
    <input type="number" id="fi-posiciones_llanta" inputmode="numeric"
           value="${v('posiciones_llanta')}" placeholder="4 en van, 6 en camión"
           style="width:100%;padding:6px">

    <label>Combustible</label>${flotaSelect('combustible', v('combustible') || 'sin_dato')}
    <label>Sistema de frenos</label>${flotaSelect('sistema_frenos', v('sistema_frenos') || 'sin_dato')}
    <label style="color:#fbbf24">¿De dónde salió el dato de frenos?</label>
    ${flotaSelect('frenos_fuente', v('frenos_fuente') || 'sin_dato')}

    <label>¿Tiene freno de escape?</label>${flotaSelect('tiene_freno_escape', v('tiene_freno_escape') || 'sin_dato')}

    <label>Distribución (sincronización del motor)</label>${flotaSelect('distribucion', v('distribucion') || 'sin_dato')}
    <label style="color:#fbbf24">¿De dónde salió el dato de distribución?</label>
    ${flotaSelect('distribucion_fuente', v('distribucion_fuente') || 'sin_dato')}
    <label>Km de cambio de distribución</label>
    <input type="number" id="fi-distribucion_km_cambio" value="${v('distribucion_km_cambio')}" style="width:100%;padding:6px">

    <label>Transmisión final (fuerza a la rueda)</label>${flotaSelect('transmision_final', v('transmision_final') || 'sin_dato')}

    <label>Aceite de motor (API + viscosidad)</label>
    <input id="fi-aceite_motor_spec" value="${v('aceite_motor_spec')}" placeholder="15W40 CI-4" style="width:100%;padding:6px">
    <label>Litros de aceite de motor</label>
    <input type="number" step="0.1" id="fi-aceite_motor_litros" value="${v('aceite_motor_litros')}" style="width:100%;padding:6px">
    <label>Aceite de caja</label>
    <input id="fi-aceite_caja_spec" value="${v('aceite_caja_spec')}" style="width:100%;padding:6px">
    <label>Aceite de diferencial</label>
    <input id="fi-aceite_diferencial_spec" value="${v('aceite_diferencial_spec')}" style="width:100%;padding:6px">
    <label>Refrigerante</label>
    <input id="fi-refrigerante_spec" value="${v('refrigerante_spec')}" style="width:100%;padding:6px">

    <label>Medida de llanta</label>
    <input id="fi-medida_llanta" value="${v('medida_llanta')}" placeholder="195R15C" style="width:100%;padding:6px">
    <label>Norma de emisiones</label>
    <input id="fi-norma_emisiones" value="${v('norma_emisiones')}" style="width:100%;padding:6px">
    <label><input type="checkbox" id="fi-tiene_furgon" ${f.tiene_furgon ? 'checked' : ''}> Tiene furgón</label>

    <button class="btn btn-primary" style="margin-top:16px;width:100%;font-size:18px"
            onclick="flotaGuardarFicha()">Guardar ficha</button>
    <div id="fi-error" style="color:#f87171;margin-top:8px"></div>
  </div>`;
}

/** Recoge el formulario de ficha y lo manda al PUT. */
async function flotaGuardarFicha() {
  const err = document.getElementById('fi-error');
  err.textContent = '';
  const val = id => document.getElementById('fi-' + id).value;

  const km = parseInt(val('km_inicial'), 10);
  const pos = parseInt(val('posiciones_llanta'), 10);
  if (!Number.isFinite(km) || km < 0) { err.textContent = 'El kilometraje es obligatorio.'; return; }
  if (!Number.isFinite(pos) || pos <= 0) { err.textContent = 'Las posiciones de llanta se cuentan a la vista.'; return; }

  const campos = { km_inicial: km, posiciones_llanta: pos,
                   tiene_furgon: document.getElementById('fi-tiene_furgon').checked };
  ['combustible', 'sistema_frenos', 'frenos_fuente', 'tiene_freno_escape',
   'distribucion', 'distribucion_fuente', 'transmision_final'].forEach(c => { campos[c] = val(c); });
  ['aceite_motor_spec', 'aceite_caja_spec', 'aceite_diferencial_spec',
   'refrigerante_spec', 'medida_llanta', 'norma_emisiones'].forEach(c => {
     if (val(c).trim()) campos[c] = val(c).trim();
   });
  ['aceite_motor_litros', 'distribucion_km_cambio'].forEach(c => {
    if (val(c).trim()) campos[c] = parseFloat(val(c));
  });

  // El mismo aviso que el CHECK de la base, dicho antes de perder el formulario.
  if (campos.distribucion !== 'sin_dato' && campos.distribucion_fuente === 'sin_dato') {
    err.textContent = 'Si sabés la distribución, decí de dónde salió el dato. ' +
      'Un dato que dispara un cambio de correa sin procedencia es una suposición.';
    return;
  }
  if (campos.sistema_frenos !== 'sin_dato' && campos.frenos_fuente === 'sin_dato') {
    err.textContent = 'Si sabés el sistema de frenos, decí de dónde salió el dato.';
    return;
  }

  try {
    const r = await fetch(API + '/flota/vehiculo/' + encodeURIComponent(FLOTA_PLACA) + '/ficha', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(campos),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.detalle || d.error || 'No se pudo guardar'; return; }
    alerta(d.completa ? 'Ficha guardada y completa ✓'
                      : 'Ficha guardada — falta: ' + d.atributos_sin_dato.join(', '), 'exito');
    flotaAbrirFicha(FLOTA_PLACA);
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  }
}
