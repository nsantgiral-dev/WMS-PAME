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
// angulo → id de la foto de APERTURA del turno que se está cerrando. Es la
// referencia de encuadre: sin ella, "frontal" de apertura y "frontal" de cierre
// pueden ser dos planos distintos y la comparación no concluye nada.
let FLOTA_REFERENCIA = {};

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

/** Los ángulos que van en la GRILLA: todos menos el tablero.
 *
 * El tablero tiene su propio campo arriba —es `foto_dato`, mínimo 1600 px, sin
 * recompresión— y estaba TAMBIÉN en la grilla: se pedía dos veces y se mandaban
 * dos fotos. Lo reportó Yesid el 2026-08-05.
 *
 * Se filtra en un solo lugar y los tres formularios lo usan: el mismo filtro
 * escrito tres veces se arregla en uno y diverge en los otros dos.
 */
function flotaAngulosDeGrilla(angulos) {
  return (angulos || []).filter(a => a !== 'tablero');
}

/** La convención de orientación y numeración, tal como la definió Yesid.
 *
 * Sin esto `lateral_izq` y `llanta_3` **no significan nada**. Lo dijo él mismo
 * el 2026-08-05, después de tomar las trece fotos del THP696: *"cada persona
 * puede tomar diferentes puntos de referencia"*. Tenía razón — el izquierdo de
 * uno es el derecho del otro si uno se para de frente al camión y el otro
 * detrás.
 *
 * Y lo que se pierde no es prolijidad: la evidencia se toma para poder decir
 * CUÁL rueda tenía el flanco herido. Sin convención no lo dice, y trece fotos
 * pasan a ser trece fotos.
 *
 * Va en la pantalla y no en un instructivo aparte: quien la necesita está
 * parado al lado del vehículo con el teléfono en la mano.
 */
function flotaConvencionFotos() {
  return `<div style="border-left:3px solid var(--pm-light);padding:6px 10px;
       margin:8px 0;font-size:13px;color:var(--tx2)">
    <b>Cómo orientarse</b> — siempre igual, o las fotos no se pueden comparar
    entre turnos:<br>
    · <b>Izquierda y derecha</b> se toman <b>mirando el vehículo de frente</b>
      (parado adelante, mirando hacia atrás). Nunca desde el portón.<br>
    · <b>Llanta 1 = delantera derecha.</b> Las siguientes van en sentido
      <b>antihorario</b>: 2 delantera izquierda, 3 trasera izquierda,
      4 trasera derecha.
  </div>`;
}

/** Llena un `<select>` con las sedes. Una sola función para los dos sitios.
 *
 * **El endpoint devuelve una LISTA, no `{almacenes: [...]}`**:
 *
 *     return jsonify([a.to_dict() for a in almacenes]), 200
 *
 * El código hacía `d.almacenes || []` → `undefined || []` → array vacío. El
 * desplegable de sede salía **sin una sola opción**, en la entrega y en el
 * modal de escritorio, y nunca funcionó desde que se escribió (2026-08-03).
 * Se toleran las dos formas porque un cambio de contrato no puede volver a
 * vaciar la pantalla en silencio.
 *
 * Y si la consulta falla, **se dice**. Antes el `catch` se la tragaba y el
 * `<select>` quedaba vacío: indistinguible de "no hay sedes". Un desplegable
 * vacío sin explicación es la regla 5 rota en la cara del usuario.
 *
 * Los códigos son los centros de costo reales — NB1, NC1, NS1, FC1, PC1 — y van
 * primero: es lo que la gente busca con la vista.
 */
async function flotaLlenarSedes(idSelect) {
  const sel = document.getElementById(idSelect);
  if (!sel) return;
  const vacia = '<option value="">— la sede no está en el maestro —</option>';
  try {
    // CON barra final. Sin ella Flask responde 308 hacia `/api/almacenes/`, y
    // detrás del proxy de Railway ese `Location` sale como `http://` —la app no
    // tenía ProxyFix, así que Flask no veía el `X-Forwarded-Proto`—. Desde una
    // página HTTPS eso es contenido mixto: el navegador lo bloquea, el `catch`
    // se dispara y el desplegable queda en «no se pudo cargar la lista», que es
    // lo que impidió entregar el turno el 2026-08-05.
    //
    // ProxyFix ya está puesto y arregla la clase entera; esta barra elimina el
    // redirect de raíz para que ni siquiera dependa de eso.
    const d = await get('/api/almacenes/');
    const lista = Array.isArray(d) ? d : (d.almacenes || []);
    if (!lista.length) {
      sel.innerHTML = vacia;
      return;
    }
    sel.innerHTML = vacia + lista.map(a =>
      `<option value="${a.id}">${a.codigo} · ${a.nombre}</option>`).join('');
  } catch (e) {
    // Ruidoso: la custodia va a quedar `pendiente_sede` y quien entrega tiene
    // que saber por qué, no descubrirlo en el health la semana que viene.
    sel.innerHTML = `<option value="">— no se pudo cargar la lista —</option>`;
    alerta('No se pudieron cargar las sedes: ' + e.message +
           '. La custodia va a quedar pendiente_sede.', 'error');
  }
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
    let html = await flotaBloqueFueraDeSede();
    html += await flotaBloqueForzados();
    html += await flotaBloqueAvisos();
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
        <!-- NO dice "Cerrar". El rechazo de traspaso pide «cerrar el turno» y
             este era el único botón con esa palabra en pantalla: Yesid lo
             apretó buscando cumplir la instrucción y perdió lo cargado. Un
             botón cuyo nombre coincide con otra acción del sistema no es
             ambiguo por descuido — es una trampa. -->
        <button class="btn-flota" onclick="flotaCerrarModal()">✕ Salir</button>
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

/** Cuánto trabajo sin guardar hay ahora mismo en el formulario abierto.
 *
 * Las fotos viven SOLO en memoria de JavaScript hasta que se confirma el turno.
 * No es una decisión: comprimir trece fotos y sostenerlas en `localStorage`
 * excede la cuota del navegador. Lo que sí se puede es no perderlas sin avisar.
 */
function flotaTrabajoSinGuardar() {
  let n = Object.keys(FLOTA_FOTOS).length;
  if (FLOTA_FOTO_TABLERO) n += 1;
  return n;
}

/** Cierra el modal y devuelve el scroll a la página.
 *
 * Pregunta antes si hay fotos cargadas. El 2026-08-05 Yesid perdió DOS VECES
 * todo lo que había tomado: la primera buscando el botón que el mensaje de
 * error le pedía apretar, la segunda cuando la entrega falló. Trece fotos son
 * quince minutos parado al lado del camión — no se descartan en silencio.
 */
function flotaCerrarModal() {
  const n = flotaTrabajoSinGuardar();
  if (n && !confirm(
        `Tenés ${n} foto(s) tomadas y sin guardar. Si salís se pierden y hay ` +
        `que tomarlas de nuevo.\n\n¿Salir igual?`)) return;
  FLOTA_FOTOS = {};
  FLOTA_FOTO_TABLERO = null;
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

    <p style="margin-top:14px"><b>${flotaAngulosDeGrilla(FLOTA_ANGULOS).length} fotos más</b>
       — orden fijo. La del tablero ya está arriba.</p>
    ${flotaConvencionFotos()}
    ${flotaNotaLlantas()}
    <div id="flota-angulos">`;

  flotaAngulosDeGrilla(FLOTA_ANGULOS).forEach(a => {
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
    <button class="btn-primary" id="flota-guardar" data-placa="${FLOTA_PLACA}"
            style="margin-top:16px;width:100%;font-size:18px"
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
    det.innerHTML = '<select id="flota-sede"></select>' +
      '<p style="color:var(--yellow);font-size:12px">Si la sede no aparece, dejá la ' +
      'primera opción: la custodia queda declarada <b>pendiente_sede</b> y el health la ' +
      'cuenta. No se inventa una sede.</p>';
    await flotaLlenarSedes('flota-sede');
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
    // `ancho`/`alto` van en null cuando el adjunto es un PDF: no tiene píxeles.
    // El servidor no le cree al mime declarado acá —lo saca del data URL— pero
    // mandar 'image/jpeg' sobre un PDF sería escribir algo que se sabe falso.
    ancho: r.ancho || null, alto: r.alto || null,
    mime: r.mime || 'image/jpeg',
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
/** Dibuja la foto. Una sola función para el camino cacheado y el de red.
 *
 * Separada a propósito: si el pintado viviera solo dentro del `try` del fetch,
 * el atajo de caché tendría su propia copia del HTML y las dos divergirían —
 * el mismo fallback en dos sitios, que en este repo ya costó 25×.
 */
function flotaPintarFoto(cont, url, bytes, titulo, mime) {
  const cabecera = `
    <p style="margin:0 0 6px"><b>${titulo || 'Archivo'}</b> · ${Math.round(bytes / 1024)} KB —
      <a href="${url}" target="_blank" style="color:var(--pm-light)">abrir en grande</a></p>`;
  // Un PDF metido en un <img> no falla ruidosamente: pinta un icono roto y
  // parece que el archivo no esta. Se distingue por tipo, no por esperanza.
  if (mime === 'application/pdf') {
    cont.innerHTML = cabecera + `
      <object data="${url}" type="application/pdf"
              style="width:100%;height:60vh;border-radius:8px;border:1px solid #333">
        <p style="color:var(--tx2)">Este navegador no muestra PDF incrustado —
          usa "abrir en grande".</p>
      </object>`;
    return;
  }
  cont.innerHTML = cabecera + `
    <img src="${url}" style="max-width:100%;border-radius:8px;border:1px solid #333">
    <p style="font-size:11px;color:var(--tx3);margin-top:4px">
      Si es el tablero: hacé zoom y verificá que se lean los seis dígitos.
      Si no se leen, los parámetros de <code>foto_dato</code> están cortos.</p>`;
}

/** fotoId → objectURL ya descargado. Vacío al empezar cada entrega. */
let FLOTA_REF_CACHE = {};

/** Baja las cuatro fotos de referencia mientras el conductor escribe.
 *
 * Sin esto, cada "cómo estaba" es un viaje a la red del patio: cuatro toques,
 * cuatro esperas. A cinco segundos cada uno son veinte sobre un presupuesto de
 * cuarenta — la mitad de la entrega gastada en mirar, no en registrar.
 *
 * El momento es gratis: corre mientras se teclea el odómetro y se saca la foto
 * del tablero, que son treinta segundos en los que la red no hace nada. No se
 * espera —`await` acá bloquearía el formulario— y si alguna falla, el botón
 * sigue funcionando: cae al fetch de siempre.
 */
function flotaPrecargarReferencias() {
  FLOTA_REF_CACHE = {};
  Object.values(FLOTA_REFERENCIA).forEach(id => {
    fetch(`${API}/flota/foto/${id}`, { headers: { Authorization: 'Bearer ' + TOKEN } })
      .then(r => (r.ok ? r.blob() : null))
      .then(b => { if (b) FLOTA_REF_CACHE[id] = { url: URL.createObjectURL(b), size: b.size, mime: b.type }; })
      .catch(() => { /* se baja al tocar, como antes */ });
  });
}

async function flotaVerFoto(fotoId, titulo) {
  const cont = document.getElementById('flota-visor');
  if (!cont) {
    // Ruidoso a propósito: un `return` callado acá es un botón que no hace nada
    // y nadie reporta. Es la forma exacta en que el enlace roto sobrevivió.
    alerta('La pantalla no tiene dónde mostrar la foto — falta #flota-visor', 'error');
    return;
  }
  // Si ya se precargó, es instantáneo: sin viaje a la red, sin espera.
  const ya = FLOTA_REF_CACHE[fotoId];
  if (ya) { flotaPintarFoto(cont, ya.url, ya.size, titulo, ya.mime); return; }

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
    flotaPintarFoto(cont, URL.createObjectURL(blob), blob.size, titulo, blob.type);
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
      // La `clase` SÍ se guardó siempre, y en un recibo hay exactamente una
      // `foto_dato`: el tablero. Eso identifica la foto del odómetro sin
      // adivinar — es un dato registrado, no una inferencia por posición.
      const tablero = sinAngulo.filter(f => f.clase === 'foto_dato');
      const resto = sinAngulo.filter(f => f.clase !== 'foto_dato');
      filas += `<li style="color:var(--tx2);margin-top:6px">
        ${sinAngulo.length} foto(s) <b>sin ángulo</b> — se guardaron antes de que
        el sistema registrara cuál era cuál. No se puede saber a qué parte del
        vehículo corresponden, y adivinarlo por el orden sería inventar.</li>`;
      if (tablero.length) {
        filas += `<li style="color:var(--green);margin-top:4px">
          Salvo el <b>tablero</b>: es la única <code>foto_dato</code> del recibo,
          y la clase sí quedó guardada.
          ${tablero.map(f => `<button class="btn-flota" style="padding:2px 8px;font-size:12px"
              onclick="flotaVerFoto(${f.id}, 'tablero — el del odómetro')">
              ver tablero (${f.ancho}×${f.alto})</button>`).join(' ')}</li>`;
      }
      if (resto.length) {
        filas += `<li style="color:var(--tx2);margin-top:4px">Las otras:
          ${resto.map(f => `<button class="btn-flota" style="padding:2px 8px;font-size:12px"
              onclick="flotaVerFoto(${f.id}, 'sin ángulo')">#${f.id}</button>`).join(' ')}</li>`;
      }
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

/** La placa del formulario que está en pantalla, verificada contra la global.
 *
 * Devuelve la placa, o `null` tras escribir el motivo en `idError`. Una sola
 * función para los tres formularios (recibo de escritorio, recibo del conductor,
 * entrega): la misma política implementada tres veces diverge, y acá divergir
 * significa que uno de los tres vuelve a guardar en el vehículo equivocado.
 */
function flotaPlacaDelFormulario(idBoton, idError) {
  const err = document.getElementById(idError);
  const boton = document.getElementById(idBoton);
  const placa = boton ? boton.dataset.placa : '';
  if (!placa) {
    err.textContent = 'El formulario no sabe de qué vehículo es. Cerralo y abrilo ' +
      'de nuevo — no se manda nada hasta que esté claro.';
    return null;
  }
  if (placa !== FLOTA_PLACA) {
    err.textContent = `Este formulario es del ${placa} y la pantalla se movió al ` +
      `${FLOTA_PLACA}. No se guarda nada: las fotos quedarían en el vehículo ` +
      `equivocado. Abrí de nuevo el del ${placa}.`;
    return null;
  }
  return placa;
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

  // LA PLACA SALE DEL FORMULARIO, NO DE LA GLOBAL.
  //
  // El 2026-08-05 Yesid reportó que las fotos de la THP696 quedaron guardadas
  // en la UPQ606. La causa: el rótulo del botón se escribía al DIBUJAR el
  // formulario y `payload.placa` se leía al APRETARLO — dos lecturas de una
  // variable global en dos momentos, y tres funciones (`flotaAbrirFicha`,
  // `flotaAbrirOdometro`, `flotaAbrirDocumentos`) la cambian sin redibujar el
  // recibo. El resultado era evidencia con hash y GPS atada al vehículo
  // equivocado, con el rótulo del vehículo correcto en pantalla.
  //
  // Sellar la placa en el DOM no alcanza por sí solo: la comprobación de abajo
  // es la que convierte un error silencioso en uno que se ve. Limpiar el estado
  // en esas tres funciones habría tapado el síntoma sin cerrar la clase.
  const placa = flotaPlacaDelFormulario('flota-guardar', 'flota-error');
  if (!placa) return;

  const tipo = document.getElementById('flota-custodio-tipo').value;
  const payload = {
    placa: placa,
    km: km,
    custodio_tipo: tipo,
    fotos_inicio: flotaAngulosDeGrilla(FLOTA_ANGULOS).filter(a => FLOTA_FOTOS[a])
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

  // El tablero no se cuenta acá: ya se validó arriba y es obligatorio. Contarlo
  // hacía que el aviso dijera "faltan N" incluyendo una foto que sí estaba.
  const grilla = flotaAngulosDeGrilla(FLOTA_ANGULOS);
  const faltan = grilla.filter(a => !FLOTA_FOTOS[a]).length;
  if (faltan && !confirm(`Faltan ${faltan} de las ${grilla.length} fotos. El turno se ` +
                         `registra igual y queda contado como incompleto. ¿Confirmás?`)) return;

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
    flotaAbrirRecibo(placa);
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
    la medida de llanta en el flanco. <b>Lo que no sepas, dejalo vacío o en <code>sin_dato</code></b> —
    el sistema lo declara y lo persigue. Inventarlo es peor que no tenerlo.</p>
    <p style="color:var(--yellow);font-size:13px">El texto gris de cada campo es un
    <b>ejemplo de formato</b>, no la respuesta de este vehículo. Copiarlo sin mirar llena
    la ficha de datos plausibles y ajenos — y eso no se nota nunca, porque un hueco se ve
    y un valor inventado no.</p>

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
    <input type="number" id="fi-distribucion_km_cambio" value="${v('distribucion_km_cambio')}"
           placeholder="ej. 60000 — está en el manual, no lo estimes"
           style="width:100%;padding:6px">

    <label>Transmisión final (fuerza a la rueda)</label>${flotaSelect('transmision_final', v('transmision_final') || 'sin_dato')}

    <label>Aceite de motor (API + viscosidad)</label>
    <input id="fi-aceite_motor_spec" value="${v('aceite_motor_spec')}"
           placeholder="ej. 15W40 CI-4 (viscosidad + norma API)"
           style="width:100%;padding:6px">
    <label>Litros de aceite de motor</label>
    <input type="number" step="0.1" id="fi-aceite_motor_litros" value="${v('aceite_motor_litros')}"
           placeholder="ej. 7.5 — lo que se le echa en un cambio"
           style="width:100%;padding:6px">
    <label>Aceite de caja</label>
    <input id="fi-aceite_caja_spec" value="${v('aceite_caja_spec')}"
           placeholder="ej. 80W90 GL-4 (caja mecánica) · ATF (automática)"
           style="width:100%;padding:6px">
    <label>Aceite de diferencial</label>
    <input id="fi-aceite_diferencial_spec" value="${v('aceite_diferencial_spec')}"
           placeholder="ej. 85W140 GL-5 — suele NO ser el mismo de la caja"
           style="width:100%;padding:6px">
    <label>Refrigerante</label>
    <input id="fi-refrigerante_spec" value="${v('refrigerante_spec')}"
           placeholder="ej. verde etilenglicol 50/50 · rojo orgánico — anotá el COLOR"
           style="width:100%;padding:6px">

    <label>Medida de llanta</label>
    <input id="fi-medida_llanta" value="${v('medida_llanta')}"
           placeholder="ej. 195R15C — está impresa en el flanco"
           style="width:100%;padding:6px">
    <label>Norma de emisiones</label>
    <input id="fi-norma_emisiones" value="${v('norma_emisiones')}"
           placeholder="ej. Euro IV · Euro V — va en la tarjeta de propiedad"
           style="width:100%;padding:6px">
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
    // La tarjeta de propiedad NO vence: acredita titularidad mientras el
    // vehículo sea del titular. Antes había que inventarle una fecha para poder
    // guardar y quedaba «vence en 6955 días», que es ruido con aspecto de dato.
    const color = !x.vence ? 'var(--green)'
      : (x.vencido ? 'var(--red)' : (x.dias_para_vencer <= 30 ? 'var(--yellow)' : 'var(--green)'));
    const nota = !x.vence ? 'no vence'
      : (x.vencido ? `VENCIDO hace ${-x.dias_para_vencer} días`
                   : `vence en ${x.dias_para_vencer} días`);
    // Botón y no `<a href>`: el endpoint exige el token en un header y una
    // pestaña nueva no manda headers. Este enlace devolvía 401 siempre — y como
    // nadie lo abrió, pasó por bueno desde que se escribió.
    const a = x.adjunto;
    let foto;
    if (!a) {
      foto = ' · <span style="color:var(--tx3)">sin archivo</span>';
    } else if (a.estado === 'pendiente_evidencia') {
      // La fila afirma que hay un archivo y el almacén no lo tiene. Decirlo
      // acá y no al abrirlo: si se ve igual que uno sano, nadie lo revisa.
      foto = ' · <span style="color:var(--red)">archivo NO guardado</span>';
    } else {
      foto = ` · <button class="btn-flota" style="padding:2px 8px;font-size:12px"
             onclick="flotaVerFoto(${a.id}, '${x.tipo}')">ver ${a.es_pdf ? 'PDF' : 'imagen'}</button>`;
    }
    return `<li style="color:${color}"><b>${x.tipo}</b> ${x.numero} · ${x.entidad}
      ${x.vence ? '· ' + x.fecha_vencimiento + ' ' : ''}— ${nota}${foto}</li>`;
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
    <select id="doc-tipo" style="width:100%;padding:6px" onchange="flotaDocTipoCambio()">
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
      <div id="doc-caja-vencimiento">
        <label>Fecha de vencimiento</label>
        <input type="date" id="doc-vencimiento" style="width:100%;padding:6px">
      </div>
      <p id="doc-no-vence" style="display:none;color:var(--tx2);font-size:13px">
        La tarjeta de propiedad <b>no vence</b>: acredita titularidad mientras el
        vehículo sea del titular. No se le pide fecha — inventarle una la volvería
        indistinguible de un documento que sí caduca.</p>
      <label style="display:block;margin-top:8px">Archivo del documento</label>
      <!-- Dos entradas y no una: el atributo capture abre la cámara directo, y
           sin él el teléfono ofrece el explorador de archivos. Con una sola
           había que elegir cuál de las dos cosas hacer imposible — y el SOAT
           llega por correo en PDF, así que la que sobraba era la cámara. -->
      <input type="file" id="doc-foto" accept="image/*" capture="environment"
             style="display:none" onchange="flotaCapturarDocumento(this)">
      <input type="file" id="doc-archivo" accept="image/*,application/pdf"
             style="display:none" onchange="flotaCapturarDocumento(this)">
      <button type="button" class="btn-flota"
              onclick="document.getElementById('doc-foto').click()">📷 Foto</button>
      <button type="button" class="btn-flota" style="margin-left:6px"
              onclick="document.getElementById('doc-archivo').click()">📎 Archivo (PDF o imagen)</button>
      <span id="doc-foto-ok" style="margin-left:8px"></span>
    </div>
    <p id="doc-aviso-no" style="display:none;color:var(--red)">
      Queda registrado como <b>no encontrado</b>. Eso es un hallazgo bloqueante,
      no un campo vacío — y el health lo cuenta aparte de los vencidos.</p>

    <button class="btn-primary" style="margin-top:14px;width:100%"
            onclick="flotaGuardarDocumento()">Guardar documento</button>
    <div id="doc-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;

  // El formulario arranca en SOAT, que sí vence — pero si el primer gesto del
  // usuario es cambiar el tipo, `onchange` no se dispara al dibujar. Se llama
  // una vez para que el estado inicial y el estado tras un cambio se armen por
  // el mismo camino: dos caminos para el mismo estado divergen.
  flotaDocTipoCambio();
}

/** Los tipos que no vencen. Espejo de `TIPOS_SIN_VENCIMIENTO` del dominio.
 *
 * El servidor manda `vence` en cada fila y además ignora el vencimiento de
 * estos tipos aunque el cliente lo mande: acá es solo para no PEDIR el dato.
 * La regla vive en el dominio; esto es cortesía de formulario.
 */
const FLOTA_TIPOS_SIN_VENCIMIENTO = ['tarjeta_propiedad'];

/** Esconde la fecha de vencimiento en los documentos que no vencen. */
function flotaDocTipoCambio() {
  const tipo = document.getElementById('doc-tipo').value;
  const vence = FLOTA_TIPOS_SIN_VENCIMIENTO.indexOf(tipo) === -1;
  const caja = document.getElementById('doc-caja-vencimiento');
  const aviso = document.getElementById('doc-no-vence');
  if (caja) caja.style.display = vence ? 'block' : 'none';
  if (aviso) aviso.style.display = vence ? 'none' : 'block';
  if (!vence) document.getElementById('doc-vencimiento').value = '';
}

/** Oculta los campos cuando el documento no apareció: no hay de dónde sacarlos. */
function flotaDocEstadoCambio() {
  const no = document.getElementById('doc-estado').value === 'no_encontrado';
  document.getElementById('doc-campos').style.display = no ? 'none' : 'block';
  document.getElementById('doc-aviso-no').style.display = no ? 'block' : 'none';
}

/** Lee un archivo tal cual, sin pasarlo por canvas. Para lo que no es imagen. */
function flotaLeerArchivo(archivo) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = reject;
    reader.onload = ev => resolve({
      dataUrl: ev.target.result, ancho: null, alto: null,
      mime: archivo.type, nombre: archivo.name, bytes: archivo.size,
    });
    reader.readAsDataURL(archivo);
  });
}

/** Toma el adjunto del documento: PDF tal cual, imagen comprimida.
 *
 * Clase `documento_adjunto`, no `foto_dato`. No es una sutileza de vocabulario:
 * `foto_dato` exige 1600 px o queda declarada rota, y con el CHECK de la tabla
 * **una foto de SOAT de 1200 px ni siquiera se podía guardar** — devolvía 409
 * "viola una regla de la base" mientras la pantalla prometía que quedaría como
 * pendiente_evidencia. El umbral existe para el odómetro fotografiado a las
 * 5 a.m.; el vencimiento del SOAT además se digita en su propio campo.
 */
async function flotaCapturarDocumento(input) {
  const f = input.files[0];
  if (!f) return;
  const aviso = document.getElementById('doc-foto-ok');
  try {
    FLOTA_FOTO_DOC = (f.type === 'application/pdf')
      ? await flotaLeerArchivo(f)
      : await flotaComprimir(f, 'foto_dato');
  } catch (e) {
    FLOTA_FOTO_DOC = null;
    aviso.innerHTML = `<span style="color:var(--red)">No se pudo leer el archivo: ${e.message}</span>`;
    return;
  }
  const r = FLOTA_FOTO_DOC;
  aviso.innerHTML = r.ancho
    ? `<span style="color:var(--green)">✓ ${r.ancho}×${r.alto}</span>`
    : `<span style="color:var(--green)">✓ ${f.name} · ${Math.round(f.size / 1024)} KB</span>`;
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
    const vence = FLOTA_TIPOS_SIN_VENCIMIENTO.indexOf(cuerpo.tipo) === -1;
    if (!vence) cuerpo.fecha_vencimiento = '';
    if (!cuerpo.numero || !cuerpo.entidad || (vence && !cuerpo.fecha_vencimiento)) {
      err.textContent = 'Con el documento a la vista: número, entidad' +
        (vence ? ' y vencimiento' : '') + '. ' +
        'Si no lo tenés, marcá "No aparece" — es una afirmación distinta.';
      return;
    }
    if (FLOTA_FOTO_DOC) {
      cuerpo.archivo = flotaFotoPayload(FLOTA_FOTO_DOC, 'documento_adjunto');
    }
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
/** Vehículos durmiendo fuera de sede, con nombre de quién responde.
 *
 * No es un detalle de ubicación: es un camión pasando la noche fuera del
 * control de la empresa. Que se vea el lunes en el tablero, y no cuando
 * aparezca un golpe y haya que reconstruir dónde estuvo.
 *
 * Si un vehículo aparece acá tres semanas seguidas, dejó de ser una excepción y
 * es una costumbre que nadie decidió. Verla es el primer paso para decidirla.
 */
async function flotaBloqueFueraDeSede() {
  let d;
  try {
    d = await get('/flota/custodia/fuera-de-sede');
  } catch (e) {
    return '';
  }
  const filas = d.fuera_de_sede || [];
  if (!filas.length) return '';
  return `<div class="tabla-card" style="border-left:3px solid var(--yellow)">
    <h3 style="color:var(--yellow)">Fuera de sede ahora (${filas.length})</h3>
    <p style="font-size:13px;color:var(--tx2)">Estos vehículos <b>no están en un
    patio de la empresa</b>. La custodia sigue en la persona que los tiene — no
    pasó a ninguna sede, porque ninguna sede los vio.</p>
    <ul style="line-height:1.6">${filas.map(f => `
      <li style="margin-bottom:8px">
        <b>${f.placa}</b> — responde <b>${f.responde}</b>, desde ${horaColombia(f.desde)}
        · ${f.km} km<br>
        <span style="color:var(--tx2)">${f.motivo || 'sin motivo escrito'}</span>
      </li>`).join('')}</ul>
  </div>`;
}

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

  // Con el turno abierto, flota se colapsa a una línea.
  //
  // El conductor abre la app para ENTREGAR PEDIDOS. Flota es un trámite de dos
  // minutos que hace una vez al día. Ponerlo entero arriba —con la lista de
  // vehículos y diez turnos de historial— lo obliga a atravesarlo para llegar a
  // su trabajo, todos los días. Eso es invertir la prioridad, y lo hice yo.
  if (d.tiene_turno_abierto) {
    el.innerHTML = `<div class="flota-veh" style="padding:10px 14px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:17px;font-weight:800;letter-spacing:.05em">🚚 ${d.placa}</span>
        <span style="color:var(--green);font-size:13px">turno abierto · ${km}</span>
        <span style="flex:1"></span>
        <button class="btn-flota" style="padding:6px 12px;font-size:13px"
                onclick="flotaCondAbrirEntrega()">Entregar turno</button>
        <button class="btn-flota" style="padding:6px 12px;font-size:13px"
                onclick="flotaCondMisReportes()">Mis turnos</button>
      </div>
      <div id="cond-flota-form"></div>
    </div>`;
    return;
  }

  // Sin turno abierto sí ocupa espacio: recibir el vehículo es lo primero que
  // hay que hacer, antes del manifiesto de ruta.
  el.innerHTML = `<div class="flota-veh">
    ${cabeza}${lista}
    <div style="display:flex;gap:6px;margin-top:12px">
      <button class="btn-primary" style="flex:2;margin-top:0" onclick="flotaCondAbrirRecibo()">
        Recibir turno</button>
      <button class="btn-flota" style="flex:1" onclick="flotaCondMisReportes()">Mis turnos</button>
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

  let angulos = flotaAngulosDeGrilla(FLOTA_ANGULOS).map(a => `<div style="display:inline-block;margin:3px">
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
    <p style="margin-top:12px"><b>${flotaAngulosDeGrilla(FLOTA_ANGULOS).length} fotos más</b>
       — la del tablero ya está arriba.</p>
    ${flotaConvencionFotos()}<div>${angulos}</div>
    <button class="btn-primary" id="cf-guardar" data-placa="${FLOTA_PLACA}"
            onclick="flotaCondGuardar()">Confirmar ${FLOTA_PLACA}</button>
    <div id="cf-error" style="color:var(--red);margin-top:8px"></div>`;
}

/** Los cuatro ángulos que se piden al ENTREGAR. Asimetría deliberada.
 *
 * Recibir es exhaustivo porque protege a quien asume el vehículo. Entregar es
 * rápido porque cierra el reloj y detecta lo grueso.
 *
 * El motivo de no pedir las trece al cerrar: son las 6 p.m., el conductor
 * terminó y quiere irse. La primera semana toma las trece. La tercera saca
 * trece fotos del piso, y eso es peor que no tener nada — parece registro y no
 * lo es. Cuatro que se toman bien valen más que trece que se falsifican.
 */
const FLOTA_ANGULOS_ENTREGA = ['frontal', 'trasera', 'lateral_izq', 'lateral_der'];

/** Abre el formulario de ENTREGA. No es el recibo con otro texto.
 *
 * Hasta el 2026-08-03 "Entregar turno" llamaba a `flotaCondAbrirRecibo()`: el
 * mismo formulario, el mismo POST, y el resultado era abrirse una custodia
 * nueva a sí mismo. Nueve toques produjeron nueve custodias de cero kilómetros
 * en el THP696. El botón no fallaba — decía una cosa y hacía otra.
 */
async function flotaCondAbrirEntrega() {
  const c = (FLOTA_COND.candidatos || []).find(x => x.vehiculo_id === FLOTA_COND_ELEGIDO)
            || (FLOTA_COND.candidatos || [])[0];
  FLOTA_PLACA = c ? c.placa : FLOTA_COND.placa;
  FLOTA_FOTOS = {};
  FLOTA_FOTO_TABLERO = null;

  // Las fotos de apertura del turno que se está cerrando: son la referencia
  // contra la que se van a comparar estas. Sin el mismo encuadre, "frontal" de
  // apertura y "frontal" de cierre son dos planos distintos y la comparación no
  // concluye nada — que es lo único que hace que estas cuatro sean evidencia.
  FLOTA_REFERENCIA = {};
  try {
    const est = await get('/flota/custodia/activa/' + encodeURIComponent(FLOTA_PLACA));
    if (est.custodia) {
      const f = await get(`/flota/custodia/${est.custodia.id}/fotos`);
      (f.fotos || []).forEach(x => {
        if (x.angulo && x.momento === 'custodia_inicio') FLOTA_REFERENCIA[x.angulo] = x.id;
      });
    }
  } catch (e) {
    // Sin referencia se entrega igual: dejar al conductor sin poder cerrar el
    // turno por falta de una ayuda visual sería peor que cerrarlo sin ella.
  }

  const angulos = FLOTA_ANGULOS_ENTREGA.map(a => `
    <div style="display:inline-block;margin:3px;text-align:center">
      <input type="file" id="flota-f-${a}" accept="image/*" capture="environment"
             style="display:none" onchange="flotaCapturarAngulo('${a}')">
      <button type="button" class="btn-flota" id="flota-b-${a}"
              onclick="document.getElementById('flota-f-${a}').click()">${flotaNombreAngulo(a)}</button>
      ${FLOTA_REFERENCIA[a] ? `<div><button class="btn-flota"
           style="padding:1px 6px;font-size:11px;margin-top:2px"
           onclick="flotaVerFoto(${FLOTA_REFERENCIA[a]}, 'así estaba al recibir — ${flotaNombreAngulo(a)}')"
           >cómo estaba</button></div>` : ''}
    </div>`).join('');

  document.getElementById('cond-flota-form').innerHTML = `
    <hr style="border-color:#333;margin:14px 0">
    <div style="font-size:20px;font-weight:800;margin-bottom:2px">Entregar ${FLOTA_PLACA}</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 8px">
      Cierra tu turno. Cuatro fotos, no trece — las que detectan un golpe nuevo.</p>

    <label class="input-label">Kilometraje del tablero</label>
    <input type="number" id="cf-km" inputmode="numeric" class="input-field"
           style="font-size:26px;font-weight:700;text-align:center">
    <input type="file" id="flota-foto-tablero" accept="image/*" capture="environment"
           style="display:none" onchange="flotaCapturarTablero()">
    <button type="button" class="btn-flota" style="margin-top:8px"
            onclick="document.getElementById('flota-foto-tablero').click()">📷 Foto del tablero</button>
    <span id="flota-tablero-ok" style="margin-left:8px"></span>

    <p style="margin-top:12px"><b>Las 4 fotos</b>
      ${Object.keys(FLOTA_REFERENCIA).length
        ? '<span style="font-size:12px;color:var(--tx2)">— "cómo estaba" te muestra la de cuando lo recibiste</span>'
        : ''}</p>
    ${flotaConvencionFotos()}
    <div>${angulos}</div>

    <label class="input-label" style="margin-top:12px">¿Dónde queda el vehículo?</label>
    <select id="cf-ubicacion" class="input-field" onchange="flotaEntregaUbicacionCambio()">
      <option value="sede">En la sede — patio</option>
      <option value="taller">En el taller</option>
      <option value="fuera_de_sede">Fuera de sede</option>
    </select>
    <div id="cf-fuera-caja" style="display:none">
      <label class="input-label" style="color:var(--yellow)">¿Por qué queda fuera? (obligatorio)</label>
      <input id="cf-ubicacion-motivo" class="input-field">
      <p style="font-size:12px;color:var(--yellow);margin:4px 0">
        El vehículo <b>sigue bajo tu responsabilidad</b> — no pasa a la sede.
        Queda marcado en el tablero de control de flota.</p>
    </div>
    <div id="cf-sede-caja"><label class="input-label">¿Qué sede?</label>
      <select id="cf-sede" class="input-field"></select></div>

    <button class="btn-primary" id="cf-guardar" data-placa="${FLOTA_PLACA}"
            onclick="flotaCondEntregar()">Entregar ${FLOTA_PLACA}</button>
    <div id="cf-error" style="color:var(--red);margin-top:8px"></div>`;

  // Sin await: baja las referencias en segundo plano mientras el conductor
  // teclea el odómetro. Cuando toque "cómo estaba", ya están.
  flotaPrecargarReferencias();

  await flotaLlenarSedes('cf-sede');
}

/** Muestra el motivo solo cuando queda fuera de sede. */
function flotaEntregaUbicacionCambio() {
  const u = document.getElementById('cf-ubicacion').value;
  const fuera = u === 'fuera_de_sede';
  document.getElementById('cf-fuera-caja').style.display = fuera ? 'block' : 'none';
  document.getElementById('cf-sede-caja').style.display = fuera ? 'none' : 'block';
}

/** Cierra el turno: el vehículo pasa a la sede, o sigue con el conductor. */
async function flotaCondEntregar() {
  const err = document.getElementById('cf-error');
  err.textContent = '';
  const km = parseInt(document.getElementById('cf-km').value, 10);
  if (!Number.isFinite(km) || km < 0) {
    err.textContent = 'El kilometraje es obligatorio. Sin odómetro no se cierra el turno.';
    return;
  }
  if (!FLOTA_FOTO_TABLERO) {
    err.textContent = 'Falta la foto del tablero: el número necesita respaldo verificable.';
    return;
  }

  const ubicacion = document.getElementById('cf-ubicacion').value;
  const fuera = ubicacion === 'fuera_de_sede';
  const motivo = fuera ? (document.getElementById('cf-ubicacion-motivo').value || '').trim() : '';
  if (fuera && !motivo) {
    err.textContent = 'Un vehículo que pasa la noche fuera de sede exige motivo escrito.';
    return;
  }

  // Dónde está y quién responde: dos hechos. Fuera de sede el vehículo NO pasa
  // a la sede — sigue siendo del conductor, que es quien lo tiene.
  const placa = flotaPlacaDelFormulario('cf-guardar', 'cf-error');
  if (!placa) return;

  const payload = {
    placa: placa, km: km,
    ubicacion: ubicacion,
    fotos_fin: FLOTA_ANGULOS_ENTREGA.filter(a => FLOTA_FOTOS[a])
      .map(a => flotaFotoPayload(FLOTA_FOTOS[a], 'evidencia_estado', a))
      .concat([flotaFotoPayload(FLOTA_FOTO_TABLERO, 'foto_dato', 'tablero')]),
  };
  if (fuera) {
    payload.custodio_tipo = 'conductor';
    payload.custodio_conductor_id = FLOTA_COND.conductor.id;
    payload.ubicacion_motivo = motivo;
  } else {
    payload.custodio_tipo = 'sede';
    const sede = document.getElementById('cf-sede').value;
    if (sede) payload.custodio_sede_id = parseInt(sede, 10);
    else payload.custodio_estado = 'pendiente_sede';
  }

  const faltan = FLOTA_ANGULOS_ENTREGA.filter(a => !FLOTA_FOTOS[a]).length;
  if (faltan && !confirm(`Faltan ${faltan} de las 4 fotos. El turno se cierra igual ` +
      `y queda contado como incompleto — pero sin ellas, un golpe que aparezca ` +
      `mañana no se le puede atribuir a nadie. ¿Confirmás?`)) return;

  const restaurar = flotaBotonOcupado(
    'cf-guardar',
    `Subiendo ${payload.fotos_fin.length} fotos (${flotaPesoAproximado({fotos_inicio: payload.fotos_fin})} KB)…`);
  try {
    const r = await fetch(API + '/flota/custodia/traspaso', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo cerrar el turno'; return; }
    alerta('Turno entregado ✓', 'exito');
    flotaCondCargar();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    restaurar();
  }
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
  const grilla = flotaAngulosDeGrilla(FLOTA_ANGULOS);
  const faltan = grilla.filter(a => !FLOTA_FOTOS[a]).length;
  if (faltan && !confirm(`Faltan ${faltan} de las ${grilla.length} fotos. ` +
      `El turno se registra igual y queda contado como incompleto. ¿Confirmás?`)) return;

  const placa = flotaPlacaDelFormulario('cf-guardar', 'cf-error');
  if (!placa) return;

  const payload = {
    placa: placa, km: km, custodio_tipo: 'conductor',
    custodio_conductor_id: FLOTA_COND.conductor.id,
    fotos_inicio: flotaAngulosDeGrilla(FLOTA_ANGULOS).filter(a => FLOTA_FOTOS[a])
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
  // Los turnos de cero kilómetros se agrupan en una línea.
  //
  // Diez filas idénticas no informan: son ruido que esconde las que sí dicen
  // algo —un cierre forzado, un turno con kilómetros—. Y en este caso además
  // son el rastro de un bug: nueve custodias de 0 km en el mismo minuto porque
  // el botón decía "Entregar" y ejecutaba un recibo.
  const filas = [];
  let vacios = 0;
  turnos.forEach(t => {
    const cuando = horaColombia(t.inicio);
    if (t.cerrado_a_la_fuerza) {
      filas.push(`<li style="color:var(--red)"><b>${t.placa}</b> ${cuando} —
        <b>te cerraron el turno</b>: ${t.motivo_del_cierre_forzado || 'sin motivo'}</li>`);
    } else if (t.abierto) {
      filas.push(`<li style="color:var(--green)"><b>${t.placa}</b> ${cuando} — abierto ahora</li>`);
    } else if ((t.km_fin - t.km_inicio) === 0) {
      vacios++;   // se cuentan, no se listan
    } else {
      filas.push(`<li><b>${t.placa}</b> ${cuando} — cerrado · ${t.km_fin - t.km_inicio} km</li>`);
    }
  });
  if (vacios) {
    filas.push(`<li style="color:var(--tx3)">${vacios} turno(s) de <b>0 km</b> —
      abiertos y cerrados sin rodar. No se listan uno por uno.</li>`);
  }
  el.innerHTML = '<hr style="border-color:#333;margin:14px 0">' +
    '<ul style="line-height:1.7;padding-left:18px">' + filas.join('') + '</ul>';
}


/** Qué avisos salieron, a quién, y si llegaron.
 *
 * Va en el tablero y no escondido en una sub-pantalla porque el número que
 * importa —cuántos salieron y nunca confirmaron entrega— es el que descubre el
 * modo de fallo real: el canal acepta mensajes que no llegan. Un contador de
 * "enviados" no lo puede ver, y en cartera esa confusión costó semanas de creer
 * que se había avisado.
 *
 * Los avisos SIMULADOS se muestran distintos de los reales. `CanalNotificacionDev`
 * costó una hora de creer que 1.485 personas habían recibido un cobro que nunca
 * salió; un tablero que los pinta igual reproduce ese error de un vistazo.
 */
async function flotaBloqueAvisos() {
  let d;
  try {
    d = await get('/flota/avisos');
  } catch (e) {
    return '';
  }
  const avisos = d.avisos || [];

  const estado = (a) => {
    if (a.estado === 'fallido') return `<span style="color:var(--red)">no salió</span>`;
    if (a.estado === 'entregado_al_proveedor') {
      // El estado que hace honesto al resto: el proveedor dijo "lo recibí".
      return `<span style="color:var(--yellow)">aceptado, sin confirmar entrega</span>`;
    }
    if (a.estado === 'entregado') return `<span style="color:var(--green)">entregado</span>`;
    if (a.estado === 'leido') return `<span style="color:var(--green)">leído</span>`;
    return a.estado;
  };

  let filas = avisos.slice(0, 12).map(a => {
    let params = a.parametros;
    try { params = JSON.parse(a.parametros).join(' · '); } catch (e) {}
    return `<li${a.simulado ? ' style="opacity:.6"' : ''}>
      ${a.simulado ? '<b style="color:var(--yellow)">[SIMULADO]</b> ' : ''}
      ${a.telefono} — ${params} · ${estado(a)}
      ${a.detalle ? `<br><small style="color:var(--red)">${a.detalle}</small>` : ''}</li>`;
  }).join('');
  if (!filas) filas = '<li style="color:var(--tx2)">Ninguno todavía.</li>';

  const alarma = d.sin_confirmar_6h > 0
    ? `<p style="color:var(--red)"><b>${d.sin_confirmar_6h} aviso(s) salieron hace más de
       6 horas y nunca confirmaron entrega.</b> El proveedor los aceptó y no hay
       evidencia de que hayan llegado — que es el modo de fallo que este registro
       existe para hacer visible.</p>`
    : '';

  const apagado = !d.encendido
    ? `<p style="color:var(--tx2)">Los avisos están <b>apagados</b>
       (<code>FLOTA_AVISOS</code>). Nace apagado a propósito: un cron que escribe
       no se enciende solo.</p>`
    : (!d.canal_real
        ? `<p style="color:var(--yellow)">Encendido en modo <b>simulado</b>: se
           registra todo y no sale ningún WhatsApp. Para mandar de verdad,
           <code>FLOTA_AVISOS_REALES=true</code>.</p>`
        : '');

  return `<div class="tabla-card">
    <div class="tabla-titulo">Avisos de vencimiento</div>
    ${apagado}${alarma}
    <ul style="line-height:1.7">${filas}</ul>
    <button class="btn-flota" onclick="flotaBarrerAvisos()">Revisar vencimientos ahora</button>
  </div>`;
}

/** Dispara el barrido a mano. Existe para poder ejercerlo ANTES de encender el
 * cron — un barrido que solo corre de noche es uno que nadie vio correr. */
async function flotaBarrerAvisos() {
  try {
    const r = await fetch(API + '/flota/avisos/barrer', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'No se pudo', 'error'); return; }
    if (d.motivo) { alerta(d.motivo, 'advertencia'); return; }
    alerta(`Revisados ${d.revisados} · en ventana ${d.en_ventana} · ` +
           `enviados ${d.enviados} · ya avisados ${d.ya_avisados}` +
           (d.sin_destinatario ? ` · SIN DESTINATARIO ${d.sin_destinatario}` : ''),
           d.sin_destinatario ? 'advertencia' : 'exito');
    flotaTablero();
  } catch (e) {
    alerta('Sin conexión: ' + e.message, 'error');
  }
}
