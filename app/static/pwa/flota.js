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
let FLOTA_FOTO_DOC = null;

/** Ángulos fijos, en orden. El orden fijo es lo que hace comparable un turno con otro. */
const FLOTA_ANGULOS_FIJOS = [
  'frontal', 'trasera', 'lateral_izq', 'lateral_der',
  'cajon_abierto', 'interior_cabina', 'tablero',
];

/** Los ángulos de ESTE vehículo. Los arma el servidor contra su ficha técnica.
 *
 * Antes era una constante con un solo `llantas` para todo el parque. Un furgón
 * tiene 4 ruedas y un camión 6, y una foto llamada "llantas" no ubica nada: un
 * flanco herido o una tuerca floja está en una rueda concreta. Sin poder decir
 * cuál, la evidencia no sirve para atribuir el daño — que es para lo que se
 * toma.
 */
let FLOTA_ANGULOS = FLOTA_ANGULOS_FIJOS.slice();

/** Nombre legible de un ángulo. `llanta_3` no le dice nada a nadie a las 5 a.m. */
function flotaNombreAngulo(a) {
  const m = /^llanta_(\d+)$/.exec(a);
  if (m) return `llanta ${m[1]}`;
  return a.replace(/_/g, ' ');
}

/** Dice de dónde salió el número de llantas, porque no todas las fuentes valen igual. */
function flotaNotaLlantas() {
  const n = FLOTA_ESTADO.posiciones_llanta;
  const fuente = FLOTA_ESTADO.posiciones_llanta_fuente;
  if (fuente === 'ficha') return '';
  const razon = fuente === 'tipo'
    ? `deducidas del tipo <b>${FLOTA_ESTADO.tipo || 'del vehículo'}</b>`
    : 'un supuesto — no se pudo deducir del tipo';
  return `<p style="color:var(--yellow);font-size:12px;margin:4px 0">
    ${n} posiciones de llanta: ${razon}, no de la ficha técnica.
    Cargá la ficha para que el número sea un dato.</p>`;
}

/** Carga la pestaña de flota: lista de vehículos y estado de custodia. */
async function cargarFlota() {
  const cont = document.getElementById('flota-contenido');
  if (!cont) return;
  try {
    const d = await get('/api/rutas/vehiculos?activos=true');
    const vehiculos = d.vehiculos || [];
    if (!vehiculos.length) {
      cont.innerHTML = `<div class="tabla-card"><p>No hay vehículos activos.
        Se dan de alta en <b>Rutas → maestras → vehículo nuevo</b>.</p>
        <p style="color:var(--yellow)">Sin vehículos no hay dónde cargar una ficha técnica
        ni dónde registrar un turno.</p></div>`;
      return;
    }
    let html = await flotaBloqueForzados();
    html += '<div class="tabla-card"><div class="tabla-titulo">Expedientes de flota</div>' +
      '<p style="font-size:12px;color:var(--tx2);margin:0 0 12px">El alta y la baja de ' +
      'vehículos se hacen en <b>Rutas → Vehículos</b>. Acá vive el expediente de cada uno.</p><div>';
    vehiculos.forEach(v => {
      html += `<div class="flota-veh">
        <div class="flota-placa">${v.placa}</div>
        <div class="flota-tipo">${v.tipo}${v.capacidad_kg ? ' · ' + v.capacidad_kg + ' kg' : ''}</div>
        <button class="btn-flota" onclick="flotaAbrirRecibo('${v.placa}')">Recibo de turno</button>
        <button class="btn-flota" onclick="flotaAbrirFicha('${v.placa}')">Ficha técnica</button>
        <button class="btn-flota" onclick="flotaAbrirOdometro('${v.placa}')">Odómetro</button>
        <button class="btn-flota" onclick="flotaAbrirDocumentos('${v.placa}')">Documentos</button>
      </div>`;
    });
    html += '</div></div>';
    cont.innerHTML = html;
    flotaAsegurarModal();
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo cargar la flota: ${e.message}</div>`;
  }
}

/** Crea el modal una sola vez y lo deja oculto.
 *
 * El formulario va en modal y no debajo de la lista por una razón que no es
 * estética: en un celular, un formulario suelto después de cinco vehículos
 * obliga a hacer scroll pasando cuatro placas ajenas, y deja de estar claro a
 * cuál pertenece. Un odómetro registrado en el camión equivocado se convierte
 * en el `km_inicial` de otro vehículo y contamina todo lo que cuelgue de él.
 */
function flotaAsegurarModal() {
  if (document.getElementById('flota-modal')) return;
  const m = document.createElement('div');
  m.id = 'flota-modal';
  m.style.cssText = 'display:none;position:fixed;inset:0;z-index:900;' +
    'background:rgba(0,0,0,.85);overflow-y:auto;padding:0;';
  m.innerHTML = `
    <div style="max-width:640px;margin:0 auto;min-height:100%;background:var(--bg);">
      <div id="flota-modal-cabeza" class="flota-modal-cabeza">
        <div>
          <div id="flota-modal-placa" class="flota-modal-placa"></div>
          <div id="flota-modal-titulo" style="font-size:12px;color:var(--tx2);"></div>
        </div>
        <button class="btn-flota" onclick="flotaCerrarModal()">Cerrar</button>
      </div>
      <div id="flota-recibo" style="padding:16px;"></div>
    </div>`;
  document.body.appendChild(m);
}

/** Corre el modal debajo del banner de modo, para que la placa se vea.
 *
 * El banner (`#banner-modo`) está en `z-index: 9999` y el modal en `900`: el
 * banner pinta encima y tapa exactamente el encabezado pegajoso con la placa.
 * Reportado el 2026-08-03 — en las capturas se lee "Recibo de turno" y la placa
 * no aparece por ningún lado.
 *
 * **Por qué se corre el modal y no se le sube el z-index:** el banner dice
 * "DATOS DE PRUEBA — nada de esto es real". Taparlo justo en la pantalla donde
 * se cargan los datos sería cambiar un aviso por otro, y el que se pierde es el
 * que evita que alguien tome un número de ensayo por bueno. Los dos tienen que
 * verse: uno dice qué camión, el otro dice si esto cuenta.
 */
function flotaBajarModalDebajoDelBanner() {
  const m = document.getElementById('flota-modal');
  const b = document.getElementById('banner-modo');
  if (!m) return;
  const alto = (b && b.style.display !== 'none') ? b.offsetHeight : 0;
  m.style.top = alto + 'px';
}

/** Abre el modal con la placa SIEMPRE visible en el encabezado.
 *
 * La placa va en un encabezado pegajoso: aunque el formulario sea largo y el
 * conductor baje hasta el botón de guardar, sigue viendo de qué camión está
 * hablando. Ese es el punto entero.
 */
function flotaAbrirModal(titulo, placa) {
  flotaAsegurarModal();
  flotaBajarModalDebajoDelBanner();
  document.getElementById('flota-modal-placa').textContent = placa || '';
  document.getElementById('flota-modal-titulo').textContent = titulo;
  document.getElementById('flota-recibo').innerHTML =
    '<div style="padding:20px;color:var(--tx3)">Cargando…</div>';
  document.getElementById('flota-modal').style.display = 'block';
  document.body.style.overflow = 'hidden';
}

/** Cierra el modal y devuelve el scroll a la página. */
function flotaCerrarModal() {
  const m = document.getElementById('flota-modal');
  if (m) m.style.display = 'none';
  document.body.style.overflow = '';
}

/** Abre el recibo de turno de una placa: trae custodia activa y odómetro. */
async function flotaAbrirRecibo(placa) {
  FLOTA_PLACA = placa;
  FLOTA_FOTOS = {};
  FLOTA_FOTO_TABLERO = null;
  flotaAbrirModal('Recibo de turno', placa);
  const el = document.getElementById('flota-recibo');
  try {
    FLOTA_ESTADO = await get('/flota/custodia/activa/' + encodeURIComponent(placa));
  } catch (e) {
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${e.message}</div>`;
    return;
  }
  // Los ángulos los decide el SERVIDOR contra la ficha de este vehículo. Si la
  // respuesta no los trae —una versión vieja en caché del service worker— se
  // usan los fijos: se piden menos fotos, pero el conductor no queda sin
  // formulario a las 5 a.m.
  FLOTA_ANGULOS = (FLOTA_ESTADO.angulos && FLOTA_ESTADO.angulos.length)
    ? FLOTA_ESTADO.angulos
    : FLOTA_ANGULOS_FIJOS.slice();
  flotaRenderRecibo();
}

/** Dibuja el formulario de recibo de turno. */
function flotaRenderRecibo() {
  const el = document.getElementById('flota-recibo');
  const c = FLOTA_ESTADO.custodia;
  const km = FLOTA_ESTADO.odometro_actual;
  // `sin_dato` llega como palabra, no como 0. Se muestra como palabra.
  const kmTexto = (km === 'sin_dato')
    ? '<span style="color:var(--yellow)">sin dato — es la primera lectura</span>'
    : `${km} km`;

  let html = `<div class="tabla-card">
    <p>Último odómetro registrado: ${kmTexto}</p>
    <p>${c ? `Viene de: custodia #${c.id} (desde ${horaColombia(c.inicio_ts)})
              <button class="btn-flota" style="padding:2px 8px;font-size:12px"
                      onclick="flotaVerFotosDeCustodia(${c.id})">ver sus fotos</button>`
           : '<b>Arranque en frío</b> — primera custodia. Lo que se registre acá nace como preexistente, sin responsable.'}</p>

    <label>Odómetro ahora (km)</label>
    <input type="number" id="flota-km" inputmode="numeric" style="width:100%;font-size:20px">

    <label style="display:block;margin-top:10px">Foto del tablero (obligatoria)</label>
    <input type="file" id="flota-foto-tablero" accept="image/*" capture="environment"
           style="display:none" onchange="flotaCapturarTablero()">
    <button type="button" class="btn-flota" onclick="document.getElementById('flota-foto-tablero').click()">
      📷 Foto del tablero</button>
    <span id="flota-tablero-ok" style="margin-left:8px"></span>

    <p style="margin-top:14px"><b>Las ${FLOTA_ANGULOS.length} fotos</b> — orden fijo</p>
    ${flotaNotaLlantas()}
    <div id="flota-angulos">`;

  FLOTA_ANGULOS.forEach(a => {
    html += `<div style="display:inline-block;margin:3px">
      <input type="file" id="flota-f-${a}" accept="image/*" capture="environment"
             style="display:none" onchange="flotaCapturarAngulo('${a}')">
      <button type="button" class="btn-flota" id="flota-b-${a}"
              onclick="document.getElementById('flota-f-${a}').click()">${flotaNombreAngulo(a)}</button>
    </div>`;
  });

  html += `</div>
    <label style="display:block;margin-top:14px">¿Quién recibe el turno?</label>
    <select id="flota-custodio-tipo" onchange="flotaCambiarTipoCustodio()">
      <option value="conductor">Un conductor</option>
      <option value="sede">Queda en una sede</option>
    </select>
    <div id="flota-custodio-detalle" style="margin-top:8px"></div>

    <!-- La placa va TAMBIÉN en el botón: es lo último que se mira antes de
         confirmar, y el encabezado puede quedar fuera de pantalla. Dos veces la
         misma placa no es redundancia — es que el gesto irreversible diga sobre
         qué vehículo se ejerce. -->
    <button class="btn-primary" id="flota-guardar" style="margin-top:16px;width:100%;font-size:18px"
            onclick="flotaGuardarRecibo()">Confirmar recibo de turno · ${FLOTA_PLACA}</button>
    <div id="flota-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
  el.innerHTML = html;
  flotaCambiarTipoCustodio();
}

/** Muestra el selector de conductor o de sede según el tipo elegido. */
async function flotaCambiarTipoCustodio() {
  const tipo = document.getElementById('flota-custodio-tipo').value;
  const det = document.getElementById('flota-custodio-detalle');
  if (tipo === 'conductor') {
    const d = await get('/api/rutas/conductores?activos=true');
    const lista = d.conductores || [];
    det.innerHTML = '<select id="flota-conductor">' +
      lista.map(c => {
        const id = identidadConductor(c, lista);
        return `<option value="${c.id}">${c.nombre}${id ? ' · ' + id : ''}</option>`;
      }).join('') +
      '</select>';
  } else {
    const d = await get('/api/almacenes');
    det.innerHTML = '<select id="flota-sede"><option value="">' +
      '— la sede no está en el maestro —</option>' +
      (d.almacenes || []).map(a =>
        `<option value="${a.id}">${a.codigo} · ${a.nombre}</option>`).join('') +
      '</select><p style="color:var(--yellow);font-size:12px">Si la sede no aparece, dejá la ' +
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
    aviso.innerHTML = `<span style="color:var(--yellow)">✓ ${r.ancho}×${r.alto} — por debajo de
      1600 px: queda como <b>pendiente_evidencia</b></span>`;
  } else {
    aviso.innerHTML = `<span style="color:var(--green)">✓ ${r.ancho}×${r.alto}</span>`;
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
function flotaFotoPayload(r, clase, angulo) {
  // Manda la IMAGEN. Hasta el 2026-08-03 mandaba una referencia inventada y un
  // hash de ceros: el navegador comprimía la foto y la tiraba, y la fila decía
  // que existía una evidencia que no existía.
  //
  // El base64 viaja por la red y no toca la base — el servidor lo decodifica,
  // escribe el archivo y guarda la ruta y el hash reales. La regla 7 prohíbe el
  // binario en una columna, no en un request.
  return {
    clase: clase,
    // Qué parte del vehículo muestra. Sin esto las ocho fotos llegan anónimas
    // y el orden no las identifica: abajo se filtran las faltantes, así que
    // con `frontal` sin tomar la primera del arreglo es `trasera`.
    angulo: angulo || null,
    data_url: r.dataUrl,
    ancho: r.ancho, alto: r.alto, mime: 'image/jpeg',
  };
}

/** Trae una foto guardada y la muestra. El visor que faltaba.
 *
 * `GET /flota/foto/<id>` exige JWT en un header, y un `<a href target=_blank>`
 * **no manda headers**: el único enlace "ver foto" que había en el PWA devolvía
 * 401 siempre. Nunca funcionó, y como nadie lo abrió, nadie lo supo.
 *
 * Eso hacía que el almacén fuera de solo escritura en la práctica: se guardaba
 * la evidencia y no había gesto humano capaz de mirarla. La única forma de
 * comprobar que un odómetro es legible es abrir la foto que quedó — no la que
 * está en el celular.
 */
async function flotaVerFoto(fotoId, titulo) {
  const cont = document.getElementById('flota-visor');
  if (!cont) {
    // Ruidoso a propósito: un `return` callado acá es un botón que no hace nada
    // y nadie reporta. Es la forma exacta en que el enlace roto sobrevivió.
    alerta('La pantalla no tiene dónde mostrar la foto — falta #flota-visor', 'error');
    return;
  }
  cont.innerHTML = '<p style="color:var(--tx3)">Trayendo la foto…</p>';
  try {
    const r = await fetch(`${API}/flota/foto/${fotoId}`,
                          { headers: { Authorization: 'Bearer ' + TOKEN } });
    if (!r.ok) {
      // 410 = la fila existe y afirma que hay foto, pero el archivo no está.
      // Se dice con esas palabras: es una inconsistencia, no un "no encontrado".
      const d = await r.json().catch(() => ({}));
      cont.innerHTML = `<p style="color:var(--red)">
        ${r.status === 410 ? 'La fila dice que hay foto, pero el archivo no está en el almacén.'
                           : 'No se pudo traer la foto.'}
        ${d.error ? '<br><small>' + d.error + '</small>' : ''}</p>`;
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const kb = Math.round(blob.size / 1024);
    cont.innerHTML = `
      <p style="margin:0 0 6px"><b>${titulo || 'Foto'}</b> · ${kb} KB —
        <a href="${url}" target="_blank" style="color:var(--pm-light)">abrir en grande</a></p>
      <img src="${url}" style="max-width:100%;border-radius:8px;border:1px solid #333">
      <p style="font-size:11px;color:var(--tx3);margin-top:4px">
        Si es el tablero: hacé zoom y verificá que se lean los seis dígitos.
        Si no se leen, los parámetros de <code>foto_dato</code> están cortos.</p>`;
  } catch (e) {
    cont.innerHTML = `<p style="color:var(--red)">Sin conexión: ${e.message}</p>`;
  }
}

/** Lista las fotos de una custodia y deja verlas. */
async function flotaVerFotosDeCustodia(custodiaId) {
  const el = document.getElementById('flota-recibo');
  flotaAbrirModal('Fotos del turno', FLOTA_PLACA);
  try {
    const d = await get(`/flota/custodia/${custodiaId}/fotos`);
    const porAngulo = {};
    d.fotos.forEach(f => { if (f.angulo) porAngulo[f.angulo] = f; });
    const sinAngulo = d.fotos.filter(f => !f.angulo);

    // Se listan los ángulos ESPERADOS, no solo los que llegaron: un hueco que
    // no se muestra es un hueco que nadie va a llenar.
    let filas = d.angulos_esperados.map(a => {
      const f = porAngulo[a];
      if (!f) return `<li style="color:var(--yellow)">${flotaNombreAngulo(a)} — <b>falta</b></li>`;
      if (f.estado === 'pendiente_evidencia') {
        return `<li style="color:var(--red)">${flotaNombreAngulo(a)} —
          se registró pero <b>el archivo no se guardó</b></li>`;
      }
      return `<li>${flotaNombreAngulo(a)} · ${f.ancho}×${f.alto} ·
        ${Math.round(f.bytes / 1024)} KB
        <button class="btn-flota" style="padding:2px 8px;font-size:12px"
                onclick="flotaVerFoto(${f.id}, '${flotaNombreAngulo(a)}')">ver</button></li>`;
    }).join('');

    if (sinAngulo.length) {
      filas += `<li style="color:var(--tx2);margin-top:6px">
        ${sinAngulo.length} foto(s) <b>sin ángulo</b> — se guardaron antes de que
        el sistema registrara cuál era cuál. No se puede saber a qué parte
        corresponden, y adivinarlo por el orden sería inventar:
        ${sinAngulo.map(f => `<button class="btn-flota" style="padding:2px 8px;font-size:12px"
            onclick="flotaVerFoto(${f.id}, 'sin ángulo')">#${f.id}</button>`).join(' ')}</li>`;
    }

    el.innerHTML = `<div class="tabla-card">
      <ul style="line-height:1.8;list-style:none;padding:0">${filas}</ul>
      <div id="flota-visor" style="margin-top:12px"></div>
    </div>`;
  } catch (e) {
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${e.message}</div>`;
  }
}

/** Bloquea un botón mientras sube, y dice cuánto va.
 *
 * Nueve fotos a 1600 px por la señal de un patio no son instantáneas. Un botón
 * que no responde durante diez segundos se toca dos veces — y un segundo POST
 * de traspaso abre una custodia más, con el mismo conductor y el mismo
 * kilometraje, indistinguible de un turno real. Deshabilitarlo no es cortesía
 * de interfaz: es lo que impide el registro duplicado.
 *
 * Devuelve la función que lo restaura.
 */
function flotaBotonOcupado(id, texto) {
  const b = document.getElementById(id);
  if (!b) return () => {};
  const original = b.textContent;
  const estabaDeshabilitado = b.disabled;
  b.disabled = true;
  b.style.opacity = '0.7';
  b.textContent = texto;
  return () => {
    b.disabled = estabaDeshabilitado;
    b.style.opacity = '';
    b.textContent = original;
  };
}

/** Cuánto pesa lo que se va a subir, para poder avisar antes de empezar. */
function flotaPesoAproximado(payload) {
  const total = (payload.fotos_inicio || [])
    .reduce((s, f) => s + (f.data_url ? f.data_url.length : 0), 0);
  // base64 infla ~4/3. Devuelve KB de verdad, no de string.
  return Math.round(total * 0.75 / 1024);
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
      .map(a => flotaFotoPayload(FLOTA_FOTOS[a], 'evidencia_estado', a))
      .concat([flotaFotoPayload(FLOTA_FOTO_TABLERO, 'foto_dato', 'tablero')]),
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

  const n = payload.fotos_inicio.length;
  const restaurar = flotaBotonOcupado(
    'flota-guardar', `Subiendo ${n} fotos (${flotaPesoAproximado(payload)} KB)…`);
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
  } finally {
    // En `finally`: si el POST falla, el botón tiene que volver. Un botón que
    // queda deshabilitado tras un error deja al conductor sin poder reintentar.
    restaurar();
  }
}

/** Formulario de lectura suelta: tanqueo, cierre de día, OT o corrección. */
function flotaAbrirOdometro(placa) {
  FLOTA_PLACA = placa;
  flotaAbrirModal('Lectura de odómetro', placa);
  document.getElementById('flota-recibo').innerHTML = `<div class="tabla-card">
    <p style="color:var(--tx2);font-size:13px">Para una lectura fuera del recibo de turno.
    Una lectura <b>no se edita</b>: si está mal, se corrige con un registro nuevo, y la
    corrección exige motivo escrito — sin él es indistinguible de un error de digitación.</p>
    <label>Kilometraje</label>
    <input type="number" id="od-km" inputmode="numeric" style="width:100%;font-size:20px;padding:6px">
    <label>Origen</label>
    <select id="od-origen" style="width:100%;padding:6px" onchange="flotaOrigenCambio()">
      <option value="tanqueo">Tanqueo — para calcular km/galón</option>
      <option value="cierre_dia">Cierre de día — sin entrega de turno</option>
      <option value="correccion">Corrección de una lectura anterior</option>
    </select>
    <div id="od-motivo-caja" style="display:none">
      <label style="color:var(--yellow)">Motivo de la corrección (obligatorio)</label>
      <input id="od-motivo" style="width:100%;padding:6px">
    </div>
    <button class="btn-primary" style="margin-top:14px;width:100%"
            onclick="flotaEnviarOdometro()">Registrar lectura</button>
    <div id="od-error" style="color:var(--red);margin-top:8px"></div>
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
  flotaAbrirModal('Ficha técnica', placa);
  const el = document.getElementById('flota-recibo');
  let d;
  try {
    d = await get('/flota/vehiculo/' + encodeURIComponent(placa) + '/ficha');
  } catch (e) {
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${e.message}</div>`;
    return;
  }
  const f = d.ficha || {};
  const v = c => (f[c] === undefined || f[c] === null) ? '' : f[c];

  el.innerHTML = `<div class="tabla-card">
    <p style="color:var(--tx2);font-size:13px">Se llena parado al lado del vehículo: el
    kilometraje está en el tablero, el aceite en la tapa del motor o en la última factura,
    la medida de llanta en el flanco. <b>Lo que no sepas, dejalo en <code>sin_dato</code></b> —
    el sistema lo declara y lo persigue. Inventarlo es peor que no tenerlo.</p>

    ${!d.existe ? '<p style="color:var(--yellow)">Este vehículo todavía no tiene ficha.</p>'
                : `<p>${d.completa ? '<span style="color:var(--green)">Ficha completa</span>'
                                   : '<span style="color:var(--yellow)">Falta: ' + d.atributos_sin_dato.join(', ') + '</span>'}</p>`}

    <label>Kilometraje actual (del tablero) *</label>
    <input type="number" id="fi-km_inicial" inputmode="numeric" value="${v('km_inicial')}"
           style="width:100%;font-size:20px;padding:6px">

    <label>Posiciones de llanta *</label>
    <input type="number" id="fi-posiciones_llanta" inputmode="numeric"
           value="${v('posiciones_llanta')}" placeholder="4 en van, 6 en camión"
           style="width:100%;padding:6px">

    <label>Combustible</label>${flotaSelect('combustible', v('combustible') || 'sin_dato')}
    <label>Sistema de frenos</label>${flotaSelect('sistema_frenos', v('sistema_frenos') || 'sin_dato')}
    <label style="color:var(--yellow)">¿De dónde salió el dato de frenos?</label>
    ${flotaSelect('frenos_fuente', v('frenos_fuente') || 'sin_dato')}

    <label>¿Tiene freno de escape?</label>${flotaSelect('tiene_freno_escape', v('tiene_freno_escape') || 'sin_dato')}

    <label>Distribución (sincronización del motor)</label>${flotaSelect('distribucion', v('distribucion') || 'sin_dato')}
    <label style="color:var(--yellow)">¿De dónde salió el dato de distribución?</label>
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

    <button class="btn-primary" style="margin-top:16px;width:100%;font-size:18px"
            onclick="flotaGuardarFicha()">Guardar ficha</button>
    <div id="fi-error" style="color:var(--red);margin-top:8px"></div>
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

/** Documentos del vehículo: SOAT, tecnomecánica, póliza, tarjeta de propiedad. */
async function flotaAbrirDocumentos(placa) {
  FLOTA_PLACA = placa;
  FLOTA_FOTO_DOC = null;
  flotaAbrirModal('Documentos', placa);
  const el = document.getElementById('flota-recibo');
  let d;
  try {
    d = await get('/flota/vehiculo/' + encodeURIComponent(placa) + '/documentos');
  } catch (e) {
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${e.message}</div>`;
    return;
  }

  let filas = d.documentos.map(x => {
    if (x.estado === 'no_encontrado') {
      return `<li style="color:var(--red)"><b>${x.tipo}</b> — NO ENCONTRADO
        · hallazgo bloqueante</li>`;
    }
    const color = x.vencido ? 'var(--red)' : (x.dias_para_vencer <= 30 ? 'var(--yellow)' : 'var(--green)');
    const nota = x.vencido ? `VENCIDO hace ${-x.dias_para_vencer} días`
                           : `vence en ${x.dias_para_vencer} días`;
    // Botón y no `<a href>`: el endpoint exige el token en un header y una
    // pestaña nueva no manda headers. Este enlace devolvía 401 siempre — y como
    // nadie lo abrió, pasó por bueno desde que se escribió.
    const foto = x.foto_id
      ? ` · <button class="btn-flota" style="padding:2px 8px;font-size:12px"
             onclick="flotaVerFoto(${x.foto_id}, '${x.tipo}')">ver foto</button>`
      : ' · <span style="color:var(--tx3)">sin foto</span>';
    return `<li style="color:${color}"><b>${x.tipo}</b> ${x.numero} · ${x.entidad}
      · ${x.fecha_vencimiento} — ${nota}${foto}</li>`;
  }).join('');
  if (!filas) filas = '<li style="color:var(--tx2)">Ninguno registrado todavía.</li>';

  el.innerHTML = `<div class="tabla-card">
    <ul style="line-height:1.7">${filas}</ul>
    ${d.sin_verificar.length ? `<p style="color:var(--yellow)">Sin verificar:
      ${d.sin_verificar.join(', ')} — <b>no es lo mismo que no encontrado</b>:
      esto significa que nadie lo ha mirado todavía.</p>` : ''}
    <div id="flota-visor" style="margin-top:12px"></div>

    <hr style="border-color:var(--brd-b);margin:14px 0">
    <label>Tipo</label>
    <select id="doc-tipo" style="width:100%;padding:6px">
      <option value="soat">SOAT</option>
      <option value="rtm">Tecnomecánica (RTM)</option>
      <option value="poliza_rc">Póliza RC</option>
      <option value="tarjeta_propiedad">Tarjeta de propiedad</option>
    </select>

    <label>Estado</label>
    <select id="doc-estado" style="width:100%;padding:6px" onchange="flotaDocEstadoCambio()">
      <option value="vigente">Lo tengo a la vista</option>
      <option value="no_encontrado">No aparece</option>
    </select>

    <div id="doc-campos">
      <label>Número</label>
      <input id="doc-numero" style="width:100%;padding:6px">
      <label>Entidad</label>
      <input id="doc-entidad" style="width:100%;padding:6px">
      <label>Fecha de expedición</label>
      <input type="date" id="doc-expedicion" style="width:100%;padding:6px">
      <label>Fecha de vencimiento</label>
      <input type="date" id="doc-vencimiento" style="width:100%;padding:6px">
      <label style="display:block;margin-top:8px">Foto del documento</label>
      <input type="file" id="doc-foto" accept="image/*" capture="environment"
             style="display:none" onchange="flotaCapturarDocumento()">
      <button type="button" class="btn-flota"
              onclick="document.getElementById('doc-foto').click()">📷 Foto</button>
      <span id="doc-foto-ok" style="margin-left:8px"></span>
    </div>
    <p id="doc-aviso-no" style="display:none;color:var(--red)">
      Queda registrado como <b>no encontrado</b>. Eso es un hallazgo bloqueante,
      no un campo vacío — y el health lo cuenta aparte de los vencidos.</p>

    <button class="btn-primary" style="margin-top:14px;width:100%"
            onclick="flotaGuardarDocumento()">Guardar documento</button>
    <div id="doc-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
}

/** Oculta los campos cuando el documento no apareció: no hay de dónde sacarlos. */
function flotaDocEstadoCambio() {
  const no = document.getElementById('doc-estado').value === 'no_encontrado';
  document.getElementById('doc-campos').style.display = no ? 'none' : 'block';
  document.getElementById('doc-aviso-no').style.display = no ? 'block' : 'none';
}

/** Captura la foto del documento — clase foto_dato, sin recompresión en servidor. */
async function flotaCapturarDocumento() {
  const f = document.getElementById('doc-foto').files[0];
  if (!f) return;
  const r = await flotaComprimir(f, 'foto_dato');
  FLOTA_FOTO_DOC = r;
  document.getElementById('doc-foto-ok').innerHTML =
    (Math.max(r.ancho, r.alto) < 1600)
      ? `<span style="color:var(--yellow)">✓ ${r.ancho}×${r.alto} — queda pendiente_evidencia</span>`
      : `<span style="color:var(--green)">✓ ${r.ancho}×${r.alto}</span>`;
}

/** Valida y guarda el documento. */
async function flotaGuardarDocumento() {
  const err = document.getElementById('doc-error');
  err.textContent = '';
  const estado = document.getElementById('doc-estado').value;
  const cuerpo = { tipo: document.getElementById('doc-tipo').value, estado: estado };

  if (estado === 'vigente') {
    cuerpo.numero = document.getElementById('doc-numero').value.trim();
    cuerpo.entidad = document.getElementById('doc-entidad').value.trim();
    cuerpo.fecha_expedicion = document.getElementById('doc-expedicion').value;
    cuerpo.fecha_vencimiento = document.getElementById('doc-vencimiento').value;
    if (!cuerpo.numero || !cuerpo.entidad || !cuerpo.fecha_vencimiento) {
      err.textContent = 'Con el documento a la vista: número, entidad y vencimiento. ' +
        'Si no lo tenés, marcá "No aparece" — es una afirmación distinta.';
      return;
    }
    if (FLOTA_FOTO_DOC) cuerpo.foto = flotaFotoPayload(FLOTA_FOTO_DOC, 'foto_dato');
  }

  try {
    const r = await fetch(API + '/flota/vehiculo/' + encodeURIComponent(FLOTA_PLACA) + '/documentos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(cuerpo),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.detalle || d.error || 'No se pudo guardar'; return; }
    alerta(estado === 'no_encontrado' ? 'Registrado como NO ENCONTRADO' : 'Documento guardado ✓',
           estado === 'no_encontrado' ? 'advertencia' : 'exito');
    flotaAbrirDocumentos(FLOTA_PLACA);
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  }
}

/** Bloque de cierres forzados: turnos cerrados sin la firma de su custodio.
 *
 * Va arriba de todo y con nombre porque es lo único que hoy hace que el
 * custodio anterior se entere antes de tres días. El aviso automático llega en
 * la tanda 2; mientras tanto, el procedimiento pide que quien fuerza avise el
 * mismo día — esto es el respaldo de que se hizo, no el aviso.
 */
async function flotaBloqueForzados() {
  let d;
  try {
    d = await get('/flota/custodia/cierres-forzados');
  } catch (e) {
    return '';
  }
  const cierres = d.cierres || [];
  if (!cierres.length) return '';
  const filas = cierres.map(c => `
    <li style="margin-bottom:8px">
      <b>${c.placa}</b> — lo tenía <b>${c.lo_tenia}</b>, lo cerró ${c.forzado_por}
      el ${horaColombia(c.cuando)}<br>
      <span style="color:var(--tx2)">${c.motivo || ''}</span>
    </li>`).join('');
  return `<div class="tabla-card" style="border-left:3px solid var(--red)">
    <h3 style="color:var(--red)">Turnos cerrados a la fuerza (${cierres.length})</h3>
    <p style="font-size:13px;color:var(--tx2)">Sin firma del custodio anterior y sin
    fotos de cierre: el turno siguiente arrancó sin nada con qué comparar.
    <b>Si este bloque crece, el problema no es el sistema — es que no se está
    cerrando turno.</b></p>
    <ul style="line-height:1.6">${filas}</ul>
  </div>`;
}

// ══════════════════════════════════════════════════════════════════════
// VISTA DEL CONDUCTOR — dentro de pantalla-conductor, no del módulo Flota
//
// El conductor ve solo lo suyo: su vehículo del día, el recibo de turno, y
// sus reportes. Si el admin registra por él, el conductor no está reportando
// nada — la app deja de ser su respaldo y pasa a ser un registro sobre él
// hecho por otro.
// ══════════════════════════════════════════════════════════════════════

let FLOTA_COND = null;        // respuesta de /conductor/mi-turno
let FLOTA_COND_ELEGIDO = null;

/** Carga el turno del conductor y pinta el bloque de flota. */
async function flotaCondCargar() {
  const el = document.getElementById('cond-flota');
  if (!el) return;
  try {
    FLOTA_COND = await get('/flota/conductor/mi-turno');
  } catch (e) {
    // Un conductor sin ficha vinculada no puede operar flota, pero SÍ sus
    // rutas: no se le rompe la pantalla por esto.
    el.innerHTML = '';
    return;
  }
  FLOTA_COND_ELEGIDO = FLOTA_COND.vehiculo_id;
  const placa = FLOTA_COND.placa;
  const bar = document.getElementById('cond-vehiculo');
  if (bar) bar.textContent = placa ? `🚚 ${placa}` : 'Sin vehículo asignado';
  flotaCondRender();
}

/** Dibuja el bloque según de dónde salió la placa. */
function flotaCondRender() {
  const el = document.getElementById('cond-flota');
  const d = FLOTA_COND;
  const km = d.odometro_actual === 'sin_dato'
    ? '<span style="color:var(--yellow)">sin dato — primera lectura</span>'
    : `${d.odometro_actual} km`;

  // Tres orígenes, tres mensajes distintos. No es lo mismo "este es tu
  // vehículo" que "creemos que es este": la segunda pide mirar la placa.
  let cabeza;
  if (d.origen === 'custodia') {
    cabeza = `<div class="flota-placa">${d.placa}</div>
      <div style="color:var(--green);font-size:13px">Tu turno está abierto · ${km}</div>`;
  } else if (d.origen === 'ruta') {
    cabeza = `<div class="flota-placa">${d.placa}</div>
      <div style="color:var(--yellow);font-size:13px">Según tu ruta de hoy.
      <b>Confirmá que la placa es la del camión que tenés enfrente.</b> · ${km}</div>`;
  } else {
    cabeza = `<div style="color:var(--yellow);font-size:14px">Elegí el vehículo que vas a recibir:</div>`;
  }

  let lista = '';
  if (d.origen !== 'custodia') {
    lista = '<div style="margin-top:10px">' + (d.candidatos || []).map(c => {
      if (c.ocupado_por) {
        // El mensaje nombra a la persona. Un 409 crudo deja al conductor
        // mirando el celular en el patio sin saber a quién llamar.
        return `<div style="padding:10px;margin:4px 0;border:1px solid var(--rbg);border-radius:8px;opacity:.75">
          <b>${c.placa}</b> · ${c.tipo}<br>
          <span style="color:var(--red);font-size:12px">Lo tiene ${c.ocupado_por}.
          Si lo vas a recibir vos, tiene que cerrar su turno primero.</span>
        </div>`;
      }
      const sel = c.vehiculo_id === FLOTA_COND_ELEGIDO;
      return `<button class="btn-flota ${sel ? 'ok' : ''}" onclick="flotaCondElegir(${c.vehiculo_id})"
        style="display:block;width:100%;text-align:left">
        ${sel ? '✓ ' : ''}<b style="font-size:19px;letter-spacing:.05em">${c.placa}</b> · ${c.tipo}</button>`;
    }).join('') + '</div>';
  }

  el.innerHTML = `<div class="flota-veh">
    ${cabeza}${lista}
    <div style="display:flex;gap:6px;margin-top:12px">
      <button class="btn-primary" style="flex:2;margin-top:0" onclick="flotaCondAbrirRecibo()">
        ${d.tiene_turno_abierto ? 'Entregar turno' : 'Recibir turno'}</button>
      <button class="btn-flota" style="flex:1" onclick="flotaCondMisReportes()">Mis reportes</button>
    </div>
    <div id="cond-flota-form"></div>
  </div>`;
}

/** Marca el vehículo elegido de la lista. */
function flotaCondElegir(id) {
  FLOTA_COND_ELEGIDO = id;
  const c = (FLOTA_COND.candidatos || []).find(x => x.vehiculo_id === id);
  FLOTA_COND.placa = c ? c.placa : '';
  const bar = document.getElementById('cond-vehiculo');
  if (bar && c) bar.textContent = `🚚 ${c.placa}`;
  flotaCondRender();
}

/** Abre el formulario de recibo de turno del conductor. */
async function flotaCondAbrirRecibo() {
  if (!FLOTA_COND_ELEGIDO) { alerta('Elegí primero el vehículo', 'error'); return; }
  const c = (FLOTA_COND.candidatos || []).find(x => x.vehiculo_id === FLOTA_COND_ELEGIDO);
  FLOTA_PLACA = c ? c.placa : FLOTA_COND.placa;
  FLOTA_FOTOS = {};
  FLOTA_FOTO_TABLERO = null;

  // Los ángulos son de ESTE vehículo, no del que se abrió antes. Sin esta
  // consulta, un conductor que pasa de un furgón a un camión sigue viendo 4
  // posiciones de llanta y las dos que faltan no se las pide nadie.
  try {
    FLOTA_ESTADO = await get('/flota/custodia/activa/' + encodeURIComponent(FLOTA_PLACA));
    FLOTA_ANGULOS = (FLOTA_ESTADO.angulos && FLOTA_ESTADO.angulos.length)
      ? FLOTA_ESTADO.angulos
      : FLOTA_ANGULOS_FIJOS.slice();
  } catch (e) {
    FLOTA_ANGULOS = FLOTA_ANGULOS_FIJOS.slice();
    alerta('Sin señal: se piden las fotos fijas, sin las de llanta', 'error');
  }

  let angulos = FLOTA_ANGULOS.map(a => `<div style="display:inline-block;margin:3px">
    <input type="file" id="flota-f-${a}" accept="image/*" capture="environment"
           style="display:none" onchange="flotaCapturarAngulo('${a}')">
    <button type="button" class="btn-flota" id="flota-b-${a}"
            onclick="document.getElementById('flota-f-${a}').click()">${flotaNombreAngulo(a)}</button></div>`).join('');

  document.getElementById('cond-flota-form').innerHTML = `
    <hr style="border-color:#333;margin:14px 0">
    <div style="font-size:20px;font-weight:800;margin-bottom:8px">${FLOTA_PLACA}</div>
    <label class="input-label">Kilometraje del tablero</label>
    <input type="number" id="cf-km" inputmode="numeric" class="input-field"
           style="font-size:26px;font-weight:700;text-align:center">
    <input type="file" id="flota-foto-tablero" accept="image/*" capture="environment"
           style="display:none" onchange="flotaCapturarTablero()">
    <button type="button" class="btn-flota" style="margin-top:8px"
            onclick="document.getElementById('flota-foto-tablero').click()">📷 Foto del tablero</button>
    <span id="flota-tablero-ok" style="margin-left:8px"></span>
    <p style="margin-top:12px"><b>Las ${FLOTA_ANGULOS.length} fotos</b></p><div>${angulos}</div>
    <button class="btn-primary" id="cf-guardar" onclick="flotaCondGuardar()">Confirmar ${FLOTA_PLACA}</button>
    <div id="cf-error" style="color:var(--red);margin-top:8px"></div>`;
}

/** Valida y envía el recibo de turno del conductor. */
async function flotaCondGuardar() {
  const err = document.getElementById('cf-error');
  err.textContent = '';
  const km = parseInt(document.getElementById('cf-km').value, 10);
  if (!Number.isFinite(km) || km < 0) {
    err.textContent = 'El kilometraje es obligatorio. Sin odómetro no se registra el turno.';
    return;
  }
  if (!FLOTA_FOTO_TABLERO) {
    err.textContent = 'Falta la foto del tablero: el número necesita respaldo verificable.';
    return;
  }
  const faltan = FLOTA_ANGULOS.filter(a => !FLOTA_FOTOS[a]).length;
  if (faltan && !confirm(`Faltan ${faltan} de las ${FLOTA_ANGULOS.length} fotos. ` +
      `El turno se registra igual y queda contado como incompleto. ¿Confirmás?`)) return;

  const payload = {
    placa: FLOTA_PLACA, km: km, custodio_tipo: 'conductor',
    custodio_conductor_id: FLOTA_COND.conductor.id,
    fotos_inicio: FLOTA_ANGULOS.filter(a => FLOTA_FOTOS[a])
      .map(a => flotaFotoPayload(FLOTA_FOTOS[a], 'evidencia_estado', a))
      .concat([flotaFotoPayload(FLOTA_FOTO_TABLERO, 'foto_dato', 'tablero')]),
  };
  const restaurar = flotaBotonOcupado(
    'cf-guardar',
    `Subiendo ${payload.fotos_inicio.length} fotos (${flotaPesoAproximado(payload)} KB)…`);
  try {
    const r = await fetch(API + '/flota/custodia/traspaso', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo registrar'; return; }
    alerta('Turno recibido ✓' + (d.linea_base ? ' (línea base)' : ''), 'exito');
    flotaCondCargar();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    restaurar();
  }
}

/** "Mis reportes y en qué van" — lo que hace que la app sea su respaldo. */
async function flotaCondMisReportes() {
  const el = document.getElementById('cond-flota-form');
  el.innerHTML = '<div style="padding:12px">Cargando…</div>';
  let d;
  try {
    d = await get('/flota/conductor/mis-reportes');
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:12px">${e.message}</div>`;
    return;
  }
  const turnos = d.turnos || [];
  if (!turnos.length) {
    el.innerHTML = '<div style="padding:12px;color:var(--tx3)">Todavía no registraste ningún turno.</div>';
    return;
  }
  el.innerHTML = '<hr style="border-color:#333;margin:14px 0"><ul style="line-height:1.7;padding-left:18px">' +
    turnos.map(t => {
      const cuando = horaColombia(t.inicio);
      if (t.cerrado_a_la_fuerza) {
        return `<li style="color:var(--red)"><b>${t.placa}</b> ${cuando} —
          <b>te cerraron el turno</b>: ${t.motivo_del_cierre_forzado || 'sin motivo'}</li>`;
      }
      if (t.abierto) return `<li style="color:var(--green)"><b>${t.placa}</b> ${cuando} — abierto ahora</li>`;
      return `<li><b>${t.placa}</b> ${cuando} — cerrado · ${t.km_fin - t.km_inicio} km</li>`;
    }).join('') + '</ul>';
}
