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

/** Los roles que **deciden** el desenlace de un daño o de una visita al taller.
 *
 * Espejo de `Roles.GESTION` y de `DECIDE_FLOTA` en `flota/api/_permisos.py`.
 * Control de flota NO está: ve todo, reporta el daño, registra el gasto y la
 * factura — pero no manda el camión al taller ni cierra el hallazgo, porque
 * `dias_hallazgo_abierto` y `hallazgos_vencidos` son dos de las cinco señales
 * con las que se lo mide, y quien es medido por un contador no puede tener el
 * botón que lo baja (regla 11, un nivel más arriba).
 *
 * **Está duplicada del backend y eso es un riesgo declarado**, no un descuido:
 * no hay un `/me` que devuelva permisos, y derivarla de otro lado sería
 * inventar una segunda fuente igual. La divergencia la atrapa un trinquete —
 * `tests/flota/test_permisos_flota.py::TestLaUIYElBackendDicenLoMismo` compara
 * esta lista contra `Roles.GESTION` y falla si alguien mueve una sola.
 *
 * Esconder el botón NO es el control de acceso: el guard vive en el backend y
 * la matriz rol × endpoint lo ejerce por HTTP. Esto existe porque dejarle a la
 * vista un gesto que el sistema le va a negar con 403 **enseña a ignorar los
 * errores**, que es la razón por la que a este rol se le esconden las otras
 * pestañas (`especialista-control-flota.md:18`).
 */
const FLOTA_ROLES_DECIDEN = ['admin', 'gerente', 'jefe_almacen', 'supervisor'];

/** ¿Este usuario decide el desenlace, o solo lo registra? */
function flotaDecide() {
  const u = (typeof OPERARIO !== 'undefined' && OPERARIO) ? OPERARIO : null;
  return !!u && FLOTA_ROLES_DECIDEN.includes(u.rol);
}

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
      `<option value="${esc(a.id)}">${esc(a.codigo)} · ${esc(a.nombre)}</option>`).join('');
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
    ? `deducidas del tipo <b>${esc(FLOTA_ESTADO.tipo || 'del vehículo')}</b>`
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
    let html = await flotaBloqueSalud();
    // Antes de los otros bloques y después del health: es trabajo que alguien
    // puede hacer HOY en dos minutos, y hasta que se haga el CPK de esos
    // vehículos no existe.
    html += await flotaBloqueDudosas();
    html += await flotaBloqueFueraDeSede();
    html += await flotaBloqueForzados();
    html += await flotaBloqueAvisos();
    html += '<div class="tabla-card"><div class="tabla-titulo">Expedientes de flota</div>' +
      '<p style="font-size:12px;color:var(--tx2);margin:0 0 12px">El alta y la baja de ' +
      'vehículos se hacen en <b>Rutas → Vehículos</b>. Acá vive el expediente de cada uno.</p><div>';
    vehiculos.forEach(v => {
      html += `<div class="flota-veh">
        <div class="flota-placa">${esc(v.placa)}</div>
        <div class="flota-tipo">${esc(v.tipo)}${v.capacidad_kg ? ' · ' + v.capacidad_kg + ' kg' : ''}</div>
        <button class="btn-flota" onclick="flotaAbrirRecibo('${esc(v.placa)}')">Recibo de turno</button>
        <button class="btn-flota" onclick="flotaAbrirFicha('${esc(v.placa)}')">Ficha técnica</button>
        <button class="btn-flota" onclick="flotaAbrirOdometro('${esc(v.placa)}')">Odómetro</button>
        <button class="btn-flota" onclick="flotaAbrirDocumentos('${esc(v.placa)}')">Documentos</button>
        <button class="btn-flota" onclick="flotaAbrirDanos('${esc(v.placa)}')">Daños</button>
        <button class="btn-flota" onclick="flotaAbrirGastos('${esc(v.placa)}')">Gastos</button>
        <button class="btn-flota" onclick="flotaAbrirTaller('${esc(v.placa)}')">Taller</button>
        <button class="btn-flota" onclick="flotaAbrirLlantas('${esc(v.placa)}')">Llantas</button>
        <button class="btn-flota" onclick="flotaAbrirPreventivo('${esc(v.placa)}')">Preventivo</button>
      </div>`;
    });
    html += '</div></div>';
    cont.innerHTML = html;
    flotaAsegurarModal();
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo cargar la flota: ${esc(e.message)}</div>`;
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
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${esc(e.message)}</div>`;
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
    <p>${c ? `Viene de: custodia #${esc(c.id)} (desde ${horaColombia(c.inicio_ts)})
              <button class="btn-flota" style="padding:2px 8px;font-size:12px"
                      onclick="flotaVerFotosDeCustodia(${esc(c.id)})">ver sus fotos</button>`
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
    ${c ? `
    <label style="display:block;margin-top:14px;color:var(--yellow)">Motivo del cierre forzado
      (solo si quien lo tiene ahora no puede cerrar su propio turno)</label>
    <input id="flota-motivo-forzado" style="width:100%;padding:6px"
           placeholder="Ej: el vehículo quedó en la sede, nadie lo puede cerrar por acá">
    <p style="font-size:12px;color:var(--tx2);margin-top:2px">Solo hace falta si el turno
      anterior no lo cierra su propio custodio con sus fotos. Si no aplica, dejalo vacío.</p>
    ` : ''}

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
        return `<option value="${esc(c.id)}">${esc(c.nombre)}${id ? ' · ' + id : ''}</option>`;
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
    aviso.innerHTML = `<span style="color:var(--yellow)">✓ ${esc(r.ancho)}×${esc(r.alto)} — por debajo de
      1600 px: queda como <b>pendiente_evidencia</b></span>`;
  } else {
    aviso.innerHTML = `<span style="color:var(--green)">✓ ${esc(r.ancho)}×${esc(r.alto)}</span>`;
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
    cont.innerHTML = `<p style="color:var(--red)">Sin conexión: ${esc(e.message)}</p>`;
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
      return `<li>${flotaNombreAngulo(a)} · ${esc(f.ancho)}×${esc(f.alto)} ·
        ${Math.round(f.bytes / 1024)} KB
        <button class="btn-flota" style="padding:2px 8px;font-size:12px"
                onclick="flotaVerFoto(${esc(f.id)}, '${flotaNombreAngulo(a)}')">ver</button></li>`;
    }).join('');

    if (sinAngulo.length) {
      // La `clase` SÍ se guardó siempre, y en un recibo hay exactamente una
      // `foto_dato`: el tablero. Eso identifica la foto del odómetro sin
      // adivinar — es un dato registrado, no una inferencia por posición.
      const tablero = sinAngulo.filter(f => f.clase === 'foto_dato');
      const resto = sinAngulo.filter(f => f.clase !== 'foto_dato');
      filas += `<li style="color:var(--tx2);margin-top:6px">
        ${esc(sinAngulo.length)} foto(s) <b>sin ángulo</b> — se guardaron antes de que
        el sistema registrara cuál era cuál. No se puede saber a qué parte del
        vehículo corresponden, y adivinarlo por el orden sería inventar.</li>`;
      if (tablero.length) {
        filas += `<li style="color:var(--green);margin-top:4px">
          Salvo el <b>tablero</b>: es la única <code>foto_dato</code> del recibo,
          y la clase sí quedó guardada.
          ${tablero.map(f => `<button class="btn-flota" style="padding:2px 8px;font-size:12px"
              onclick="flotaVerFoto(${esc(f.id)}, 'tablero — el del odómetro')">
              ver tablero (${esc(f.ancho)}×${esc(f.alto)})</button>`).join(' ')}</li>`;
      }
      if (resto.length) {
        filas += `<li style="color:var(--tx2);margin-top:4px">Las otras:
          ${resto.map(f => `<button class="btn-flota" style="padding:2px 8px;font-size:12px"
              onclick="flotaVerFoto(${esc(f.id)}, 'sin ángulo')">#${esc(f.id)}</button>`).join(' ')}</li>`;
      }
    }

    el.innerHTML = `<div class="tabla-card">
      <ul style="line-height:1.8;list-style:none;padding:0">${filas}</ul>
      <div id="flota-visor" style="margin-top:12px"></div>
    </div>`;
  } catch (e) {
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${esc(e.message)}</div>`;
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

  // Solo existe cuando ya había una custodia vigente (ver flotaRenderRecibo).
  // Vacío si no aplica: el backend solo lo exige cuando de verdad hace falta,
  // y mandarlo vacío en el caso normal no cambia nada.
  const motivoForzadoEl = document.getElementById('flota-motivo-forzado');
  const motivoForzado = motivoForzadoEl ? motivoForzadoEl.value.trim() : '';
  if (motivoForzado) payload.motivo_forzado = motivoForzado;

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
            onclick="flotaEnviarOdometro()" id="od-guardar"
            data-placa="${placa}">Registrar lectura</button>
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
  const placa = flotaPlacaDelFormulario('od-guardar', 'od-error');
  if (!placa) return;
  try {
    // **El retorno se lee.** Hasta el 2026-09-03 esta línea era
    // `await flotaRegistrarOdometro(...)` sin asignar: el endpoint devolvía
    // `confianza` y `motivo_dudosa` —se esmeró en calcularlos— y la pantalla
    // decía «✓» en verde y los tiraba. El conductor se iba convencido de que
    // su número quedó firme, y quedaba dudoso: fuera del CPK hasta que alguien
    // pase por la cola de verificación con una foto que él pudo haber sacado
    // ahí mismo, parado al lado del camión.
    const guardada = await flotaRegistrarOdometro(placa, km, origen, motivo);
    if (guardada && guardada.confianza === 'dudosa') {
      alerta(`Lectura registrada, pero queda DUDOSA: ${guardada.motivo_dudosa}`,
             'advertencia');
    } else {
      alerta('Lectura registrada ✓', 'exito');
    }
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

// ═══════════════════════════════════════════════════════════════════════════
// LA COLA DE VERIFICACIÓN — donde un número se vuelve un dato
// ═══════════════════════════════════════════════════════════════════════════
//
// Toda lectura nace marcada (`confianza`) y ninguna nace `verificada`: esa
// palabra la escribe una persona, acá, con la foto al lado. Es lo único que
// separa «un número que alguien tecleó» de «un número que alguien respalda», y
// de eso depende que el CPK de ese vehículo se pueda publicar.
//
// **No es una pantalla que nace vacía** (regla 12): en producción hay 26
// lecturas sin foto del tablero —26 de 26, medido el 2026-09-01— y la regla 1
// de `confianza_al_nacer` las marca `dudosa`. La cola tiene contenido desde el
// primer día.
//
// Las URLs van escritas ENTERAS y no armadas con `+ verbo`: el trinquete de
// rutas huérfanas ignora los comentarios desde el 2026-09-02, así que una URL
// que solo exista en tiempo de ejecución se declara sin consumidor — y tiene
// razón, porque no se puede auditar leyendo el repo. Mismo motivo que
// `FLOTA_HALLAZGO_URL`.
const FLOTA_DUDOSAS_URL = '/flota/odometro/dudosas';
const FLOTA_VERIFICAR_URL = (id) => `/flota/odometro/${id}/verificar`;

/** El aviso del tablero: cuántos kilometrajes esperan a que alguien los mire.
 *
 * Devuelve vacío cuando no hay ninguno — la disciplina de todos los bloques de
 * esta pantalla. Un tablero que siempre muestra algo se deja de mirar.
 */
async function flotaBloqueDudosas() {
  let d;
  try {
    d = await get(FLOTA_DUDOSAS_URL);
  } catch (e) {
    return '';
  }
  const n = (d && d.total) || 0;
  if (!n) return '';
  return `<div class="tabla-card" style="border-left:3px solid var(--yellow)">
    <h3>${n} kilometraje(s) sin verificar</h3>
    <p style="color:var(--tx2);font-size:13px;margin:4px 0 10px">
      No es que estén mal: es que nadie los pudo cotejar todavía. Mientras uno de
      estos sea un extremo del mes, el <b>costo por kilómetro de ese vehículo no
      se publica</b> — sale «sin dato», que es lo que corresponde y no un número
      inventado.</p>
    <button class="btn-primary" onclick="flotaAbrirVerificacion()">
      Verificar kilometrajes</button>
  </div>`;
}

/** Abre la cola. La foto grande, el número al lado, y dos salidas. */
async function flotaAbrirVerificacion() {
  // Sin placa en el encabezado: la cola es de TODA la flota y cada fila trae la
  // suya. Poner una placa acá diría que lo que se está mirando es de ese
  // vehículo, y el error de verificar el kilometraje del camión equivocado es
  // exactamente el que esta pantalla existe para no cometer.
  flotaAbrirModal('Kilometrajes por verificar', '');
  await flotaRenderVerificacion();
}

/** Pinta la cola completa. */
async function flotaRenderVerificacion() {
  const cont = document.getElementById('flota-recibo');
  cont.innerHTML = '<div class="tabla-card">Cargando…</div>';
  let d;
  try {
    d = await get(FLOTA_DUDOSAS_URL);
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo cargar la cola: ${esc(e.message)}</div>`;
    return;
  }
  const pendientes = (d && d.pendientes) || [];
  if (!pendientes.length) {
    cont.innerHTML = `<div class="tabla-card">
      <p>No hay kilometrajes en duda.</p>
      <p style="color:var(--tx2);font-size:13px">Una lectura entra acá cuando no
        tiene foto del tablero, cuando el reloj no avanzó y el odómetro sí, o
        cuando salta un orden de magnitud respecto de la anterior.</p></div>`;
    return;
  }
  cont.innerHTML = `<div class="tabla-card">
      <p style="color:var(--tx2);font-size:13px;margin:0 0 10px">
        <b>Confirmar deja tu nombre y la fecha en la fila.</b> Si el número está
        mal, no se edita: se corrige con un registro nuevo, y la corrección pide
        motivo escrito. La más vieja va primero.</p>
      <ul style="list-style:none;padding:0">
        ${pendientes.map(p => flotaFilaDudosa(p)).join('')}</ul>
    </div>
    <div class="tabla-card" id="flota-visor">
      <p style="color:var(--tx3)">Tocá «Ver la foto» en una fila para mirarla acá.</p>
    </div>`;
}

/** Una fila de la cola: el número grande, el motivo, y las dos salidas.
 *
 * `tiene_foto` se pinta explícito y en amarillo cuando falta. Confirmar una
 * lectura sin foto es la palabra de quien confirma, no la de una evidencia —
 * y quien lo hace tiene derecho a saber qué está firmando. Esconderlo
 * convertiría las 26 lecturas sin respaldo en 26 verificaciones que no
 * verificaron nada.
 */
function flotaFilaDudosa(p) {
  const foto = p.tiene_foto
    ? `<button class="btn-flota" style="padding:4px 10px;font-size:12px"
               onclick="flotaVerFoto(${esc(p.foto_id)}, 'Tablero de ${esc(p.placa)}')">Ver la foto</button>`
    : `<span style="color:var(--yellow);font-size:12px">sin foto del tablero —
         confirmarla es tu palabra, no la de una foto</span>`;
  return `<li style="margin-bottom:14px;border-left:2px solid var(--bd);padding-left:10px">
    <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
      <b style="font-size:20px">${Number(p.valor_km).toLocaleString('es-CO')} km</b>
      <span class="flota-placa" style="font-size:15px">${esc(p.placa)}</span>
      <span style="font-size:12px;color:var(--tx2)">${horaColombia(p.ts)} · ${esc(p.origen)}</span>
    </div>
    <div style="font-size:12px;color:var(--yellow);margin:2px 0 6px">${esc(p.motivo || '')}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">
      ${foto}
      <button class="btn-flota" style="padding:4px 10px;font-size:12px"
              onclick="flotaConfirmarKm(${esc(p.lectura_id)})">Confirmar</button>
      <button class="btn-flota" style="padding:4px 10px;font-size:12px"
              onclick="flotaCorregirKm('${esc(p.placa)}', ${esc(p.valor_km)})">Corregir</button>
    </div>
  </li>`;
}

/** Confirma que el número es el del tablero. Queda quién y cuándo. */
async function flotaConfirmarKm(lecturaId) {
  if (!confirm('Vas a afirmar que ese kilometraje es el que muestra el tablero.\n' +
               'Queda tu nombre y la fecha en la fila.\n\n¿Confirmar?')) return;
  try {
    const r = await fetch(API + FLOTA_VERIFICAR_URL(lecturaId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: '{}',
    });
    const d = await r.json();
    if (!r.ok) { alerta(d.error || 'No se pudo verificar', 'error'); return; }
    alerta('Kilometraje verificado ✓', 'exito');
    await flotaRenderVerificacion();
  } catch (e) {
    alerta('Sin conexión: ' + e.message, 'error');
  }
}

/** Corrige: **no edita la fila, registra una lectura nueva.**
 *
 * Reusa `flotaRegistrarOdometro` con `origen=correccion` — la única puerta que
 * existe para eso y la que exige motivo. Una segunda puerta desde acá sería la
 * misma política escrita dos veces, y la de esta pantalla sería la que un día
 * deje de pedir el motivo.
 *
 * La lectura vieja no se borra ni se marca: la tabla es append-only. Lo que
 * pasa es que sale de la cola, porque una corrección declara «de acá en
 * adelante, esto es lo cierto» y el servidor aplica esa misma ventana.
 */
async function flotaCorregirKm(placa, kmActual) {
  const dicho = prompt(`Kilometraje correcto de ${placa} (el registrado dice ` +
                       `${kmActual}):`);
  if (dicho === null) return;
  const km = parseInt(dicho, 10);
  if (!Number.isFinite(km) || km < 0) {
    alerta('El kilometraje corregido tiene que ser un número.', 'advertencia');
    return;
  }
  const motivo = prompt('¿Por qué se corrige? (obligatorio — sin motivo, una ' +
                        'corrección es indistinguible de un error de digitación)');
  if (!motivo || !motivo.trim()) {
    alerta('Una corrección sin motivo escrito no se puede distinguir de un ' +
           'error de digitación.', 'advertencia');
    return;
  }
  try {
    await flotaRegistrarOdometro(placa, km, 'correccion', motivo.trim());
    alerta('Corrección registrada ✓', 'exito');
    await flotaRenderVerificacion();
  } catch (e) {
    alerta(e.message, 'error');
  }
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
  // Mismo vocabulario que las otras dos procedencias: es el `FUENTE` del
  // dominio, y `sin_dato` va PRIMERO — ninguna opción viene marcada.
  capacidad_tanque_fuente: ['sin_dato', 'manual_fabricante', 'concesionario', 'placa_motor', 'taller', 'estimado'],
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
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${esc(e.message)}</div>`;
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
    <label>Capacidad del tanque (galones)</label>
    <input type="number" step="0.01" id="fi-capacidad_tanque_galones"
           value="${v('capacidad_tanque_galones')}"
           placeholder="ej. 15 — está en el manual, o se mide llenándolo desde vacío"
           style="width:100%;padding:6px">
    <label>¿De dónde salió ese número?</label>
    ${flotaSelect('capacidad_tanque_fuente', d.ficha.capacidad_tanque_fuente)}
    <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
      Es lo único que hace posible detectar un tanqueo por encima de lo que
      cabe: un tanque de 15 galones que recibe 22 no es un error de medición.
      Sin este dato el sistema no puede revisarlo, y lo dice en vez de suponerlo.</p>

    <label>Norma de emisiones</label>
    <input id="fi-norma_emisiones" value="${v('norma_emisiones')}"
           placeholder="ej. Euro IV · Euro V — va en la tarjeta de propiedad"
           style="width:100%;padding:6px">
    <label><input type="checkbox" id="fi-tiene_furgon" ${f.tiene_furgon ? 'checked' : ''}> Tiene furgón</label>

    <button class="btn-primary" style="margin-top:16px;width:100%;font-size:18px"
            onclick="flotaGuardarFicha()" id="fi-guardar"
            data-placa="${placa}">Guardar ficha</button>
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
  // La capacidad viaja SIEMPRE con su procedencia, nunca sola: el CHECK
  // `ck_flota_capacidad_tanque_con_procedencia` exige el par, y mandar uno
  // solo devuelve 409. Un número sin fuente se lee como si alguien lo hubiera
  // verificado — el mismo criterio que ya rige para distribución y frenos.
  campos.capacidad_tanque_fuente = val('capacidad_tanque_fuente');
  if (val('capacidad_tanque_galones').trim()) {
    campos.capacidad_tanque_galones = parseFloat(val('capacidad_tanque_galones'));
  }

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
  const _capacidad = campos.capacidad_tanque_galones;
  const _capFuente = campos.capacidad_tanque_fuente;
  if (_capacidad !== undefined && _capFuente === 'sin_dato') {
    err.textContent = 'Si sabés la capacidad del tanque, decí de dónde salió. ' +
      'Un número sin procedencia se lee como si alguien lo hubiera verificado.';
    return;
  }
  if (_capacidad === undefined && _capFuente !== 'sin_dato') {
    err.textContent = 'Pusiste una procedencia sin capacidad. Si no sabés cuántos ' +
      'galones caben, dejá la fuente en «sin_dato»: no saber es una respuesta.';
    return;
  }

  // La MISMA clase que cruzó las fotos el 2026-08-05, en el formulario de al
  // lado. Se arregló para los tres que mandan fotos y no para éste, y el
  // síntoma fue peor de leer: la ficha se guardaba —en OTRO vehículo—, la
  // pantalla decía «Ficha guardada y completa ✓», y al reabrir el vehículo
  // estaba vacío. Un éxito que miente sobre a qué se aplicó.
  const placa = flotaPlacaDelFormulario('fi-guardar', 'fi-error');
  if (!placa) return;

  try {
    const r = await fetch(API + '/flota/vehiculo/' + encodeURIComponent(placa) + '/ficha', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(campos),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.detalle || d.error || 'No se pudo guardar'; return; }
    alerta((d.completa ? 'Ficha guardada y completa ✓'
                       : 'Ficha guardada — falta: ' + d.atributos_sin_dato.join(', '))
           + ' · ' + placa, 'exito');
    flotaAbrirFicha(placa);
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
    el.innerHTML = `<div class="tabla-card" style="color:var(--red)">${esc(e.message)}</div>`;
    return;
  }

  let filas = d.documentos.map(x => {
    if (x.estado === 'no_encontrado') {
      return `<li style="color:var(--red)"><b>${esc(x.tipo)}</b> — NO ENCONTRADO
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
             onclick="flotaVerFoto(${esc(a.id)}, '${esc(x.tipo)}')">ver ${a.es_pdf ? 'PDF' : 'imagen'}</button>`;
    }
    return `<li style="color:${color}"><b>${esc(x.tipo)}</b> ${esc(x.numero)} · ${esc(x.entidad)}
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
            onclick="flotaGuardarDocumento()" id="doc-guardar"
            data-placa="${placa}">Guardar documento</button>
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
    aviso.innerHTML = `<span style="color:var(--red)">No se pudo leer el archivo: ${esc(e.message)}</span>`;
    return;
  }
  const r = FLOTA_FOTO_DOC;
  aviso.innerHTML = r.ancho
    ? `<span style="color:var(--green)">✓ ${esc(r.ancho)}×${esc(r.alto)}</span>`
    : `<span style="color:var(--green)">✓ ${esc(f.name)} · ${Math.round(f.size / 1024)} KB</span>`;
}

/** Valida y guarda el documento. */
async function flotaGuardarDocumento() {
  const err = document.getElementById('doc-error');
  err.textContent = '';
  const placa = flotaPlacaDelFormulario('doc-guardar', 'doc-error');
  if (!placa) return;
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
    const r = await fetch(API + '/flota/vehiculo/' + encodeURIComponent(placa) + '/documentos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(cuerpo),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.detalle || d.error || 'No se pudo guardar'; return; }
    alerta(estado === 'no_encontrado' ? 'Registrado como NO ENCONTRADO' : 'Documento guardado ✓',
           estado === 'no_encontrado' ? 'advertencia' : 'exito');
    flotaAbrirDocumentos(placa);
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
/** Lo que el health mide del odómetro y de los papeles, y hasta hoy no se veía.
 *
 * `GET /flota/health` mide 21 campos y **no tenía un solo consumidor** en todo
 * el repo — está exento del guard de huérfanos «porque un health lo leen
 * monitores», y no hay monitores. El 2026-09-01 se le agregaron seis campos de
 * odómetro a un tablero que nadie abría; esto cierra ese círculo.
 *
 * Sigue la disciplina de los otros bloques: **devuelve vacío cuando no hay nada
 * que hacer.** No es una pared de números — un tablero que siempre muestra algo
 * se deja de mirar, que es la lección de los 639 avisos conocidos.
 *
 * El salto de kilometraje se pinta como HECHO y sin juzgarlo: no hay un umbral
 * de km/día porque todavía no hay un mes de mediciones con qué fijarlo. Decir
 * "el salto más grande del mes fue X" es una medición; decir "X está mal" sin
 * base sería un número inventado.
 */
async function flotaBloqueSalud() {
  let h;
  try {
    h = await get('/flota/health');
  } catch (e) {
    return '';
  }
  const filas = [];

  // Papeles: lo más grave arriba. `documentos_vencidos` existía desde la tanda
  // 1 y era el número que nadie miraba — la "otra vía" a la que el aviso de
  // vencimiento remitía sin que existiera.
  if (h.documentos_vencidos > 0) {
    filas.push(['red', `${h.documentos_vencidos} documento(s) VENCIDOS`,
      'Un vehículo con SOAT o tecnomecánica vencida no puede circular. ' +
      'Desde el 2026-09-01 el barrido también avisa por WhatsApp, una vez por semana.']);
  }
  if (h.documentos_no_encontrados > 0) {
    filas.push(['red', `${h.documentos_no_encontrados} documento(s) que nadie pudo mostrar`,
      'No es lo mismo que vencido: es que no se sabe si existe.']);
  }

  // Daños. Van arriba del odómetro porque un bloqueante vencido es un camión
  // que no debería estar saliendo, y eso se decide hoy; el odómetro es el
  // denominador de lo que se calcula después.
  //
  // Vencido primero y con su propia línea: sumado a «abiertos» desaparece,
  // que es precisamente lo que hace ilegible un canal de avisos.
  if (h.hallazgos_vencidos > 0) {
    filas.push(['red', `${h.hallazgos_vencidos} daño(s) pasados de su fecha límite`,
      'El plazo lo fijó la gravedad al reportarlos, no alguien a mano. ' +
      'Se abren desde el botón «Daños» de cada vehículo.']);
  }
  if (h.hallazgos_abiertos > 0) {
    filas.push(['yellow', `${h.hallazgos_abiertos} daño(s) abiertos en la flota`,
      'Todavía dentro de plazo. Un daño aplazado varias veces no está ' +
      'gestionado: el contador de aplazos lo dice en el expediente.']);
  }

  // Inspección diaria. Va acá porque decide HOY —igual que el daño vencido— y
  // porque los dos números contestan preguntas distintas que no se pueden
  // sumar: cuántos camiones nadie miró, y cuántos se miraron a medias.
  //
  // Un vehículo sin inspección no aparece en ningún otro lado del tablero: no
  // tiene daño, no tiene aviso, no tiene fila. La ausencia es invisible salvo
  // que alguien la cuente, y por eso este es el campo que este bloque más
  // necesitaba.
  if (h.vehiculos_sin_inspeccion_hoy > 0) {
    filas.push(['yellow', `${h.vehiculos_sin_inspeccion_hoy} vehículo(s) sin inspección de hoy`,
      'No es que estén bien: es que hoy nadie los miró. La hace el conductor ' +
      'desde «Inspección de hoy», con el turno ya recibido.']);
  }
  if (h.inspecciones_incompletas_hoy > 0) {
    filas.push(['yellow', `${h.inspecciones_incompletas_hoy} inspección(es) de hoy quedaron incompletas`,
      'Incompleta no es «casi apta»: hay ítems que nadie contestó. No saber ' +
      'tampoco habilita despacho, y se corrige mirando, no en el taller.']);
  }

  const llenado = h.segundos_llenado_30d;
  if (llenado && llenado.minimo) {
    filas.push(['tx2',
      `Inspección más rápida del mes: ${llenado.minimo.segundos}s para ` +
      `${llenado.minimo.items} ítems (mediana ${llenado.mediana}s, ${llenado.n} inspecciones)`,
      'Es un dato, no una alarma: todavía no hay un piso medido con qué ' +
      'compararlo. Se publica para poder fijarlo con dato en vez de a ojo.']);
  }

  // Odómetro: el denominador de todo lo que viene después (CPK, preventivo por
  // kilometraje, consumo de combustible).
  if (h.vehiculos_sin_lectura > 0) {
    filas.push(['yellow', `${h.vehiculos_sin_lectura} vehículo(s) sin ninguna lectura de odómetro`,
      'No es que tengan 0 km: es que no se sabe cuánto han rodado. Sin esto no hay costo por kilómetro.']);
  }
  if (h.lecturas_sin_foto > 0) {
    filas.push(['yellow', `${h.lecturas_sin_foto} lectura(s) sin foto del tablero`,
      'Un kilometraje sin foto es un número que nadie puede verificar contra el vehículo.']);
  }
  if (h.fichas_con_ancla_incoherente > 0) {
    filas.push(['yellow', `${h.fichas_con_ancla_incoherente} ficha(s) con kilometraje inicial incoherente`,
      'El km inicial de la ficha es mayor que la primera lectura registrada. Uno de los dos está mal y hay que mirar cuál.']);
  }
  if (h.lecturas_correccion_30d > 0) {
    filas.push(['yellow', `${h.lecturas_correccion_30d} corrección(es) de odómetro este mes`,
      'Una corrección salta la validación de monotonía. Si son muchas, la vía de escape se volvió la vía normal.']);
  }

  // La confianza del kilómetro. Los dos van juntos y en líneas separadas: uno
  // es la deuda y el otro dice si alguien la está pagando. Sumados —o peor,
  // convertidos en un porcentaje— «cero verificadas sobre cero dudosas» (una
  // flota sana) se vería igual que «cero verificadas sobre veinte» (una cola
  // que nadie abre), y esos son justo los dos estados que hay que distinguir.
  if (h.lecturas_dudosas_pendientes > 0) {
    filas.push(['yellow', `${h.lecturas_dudosas_pendientes} kilometraje(s) esperan verificación`,
      'Mientras uno de estos sea un extremo del mes, el costo por kilómetro de ' +
      'ese vehículo sale «sin dato» — no bajo: no calculable. Se resuelven en ' +
      '«Verificar kilometrajes», con la foto al lado.']);
  }
  if (h.lecturas_verificadas_30d > 0) {
    filas.push(['tx2', `${h.lecturas_verificadas_30d} kilometraje(s) verificados este mes`,
      'Alguien los miró contra la foto y dejó su nombre. Es lo único que ' +
      'distingue un número que se tecleó de uno que se puede sostener.']);
  }

  const salto = h.salto_km_maximo_30d;
  if (salto && salto.delta_km) {
    const horas = salto.horas === null ? 'sin fecha' : `${salto.horas} h`;
    filas.push(['tx2', `Salto de kilometraje más grande del mes: ${salto.delta_km.toLocaleString('es-CO')} km en ${horas}`,
      'Es un dato, no una alarma: todavía no hay un techo medido con qué compararlo. Se publica para poder fijarlo.']);
  }

  // La plata que sale (2026-09-02). Va después del odómetro porque el odómetro
  // es su denominador: un CPK sobre kilómetros que nadie midió no dice nada.
  //
  // El primero es el ÚNICO detector de la fase, y **no acusa a nadie** (regla
  // 2): dice que dos datos no pueden ser los dos ciertos.
  if (h.tanqueos_sobre_capacidad > 0) {
    filas.push(['red', `${h.tanqueos_sobre_capacidad} tanqueo(s) con más galones de los que caben`,
      'La capacidad de la ficha y los galones registrados no pueden ser los dos ' +
      'ciertos. Puede ser una capacidad mal levantada, un tanque auxiliar que la ' +
      'ficha no conoce, dos vehículos en la misma factura o un dedo en el teclado: ' +
      'hay que mirar cuál, y se mira igual de rápido en los cuatro casos.']);
  }
  // Y este es el que impide que el de arriba se apague sin que nadie lo note:
  // sin capacidad en la ficha no hay contra qué revisar, y eso NO es «está
  // bien». Cero exceso con cien sin revisar es un detector apagado.
  if (h.tanqueos_sin_capacidad_declarada > 0) {
    filas.push(['yellow', `${h.tanqueos_sin_capacidad_declarada} tanqueo(s) que no se pudieron revisar`,
      'La ficha de esos vehículos no dice cuántos galones caben, así que nada ' +
      'los compara contra nada. No es que estén bien: es que no se miraron.']);
  }
  if (h.gastos_sin_documento > 0) {
    filas.push(['yellow', `${h.gastos_sin_documento} gasto(s) sin número de factura`,
      'Existen operativamente y no se pueden cruzar con la causación de Siesa. ' +
      'Es la medida de si la operación está entregando las facturas.']);
  }

  // El CPK, como HECHO y por vehículo. No hay promedio de flota a propósito:
  // el canon dice que el CPK no compara vehículos, y promediar un NHR con un
  // motocarro mide la composición del parque, no la operación.
  const cpk = h.cpk_mes;
  if (cpk && cpk.length) {
    const linea = cpk.map(v => v.cpk === 'sin_dato'
      ? `${v.placa}: sin dato`
      : `${v.placa}: $${Number(v.cpk).toLocaleString('es-CO')}/km`).join(' · ');
    filas.push(['tx2', `Costo por kilómetro del mes — ${linea}`,
      'Es lo registrado dividido entre los kilómetros medidos, no el costo de ' +
      'tener el camión. No se comparan entre sí: un NHR y un motocarro no ' +
      'cuestan igual y la diferencia no dice nada.']);
  }

  // Taller y garantía (2026-09-02). Los tres van separados y ninguno se suma:
  // al primero se le llama al taller, al segundo a contabilidad, y el tercero
  // no se atiende — se consulta antes de mandar el camión.
  if (h.ot_abiertas > 0) {
    filas.push(['yellow', `${h.ot_abiertas} orden(es) de trabajo abiertas`,
      'Son camiones que están en el taller ahora mismo. No dice si están ' +
      'demorados: todavía no hay una sola medición de cuánto dura una visita, ' +
      'y un techo escrito hoy sería a ojo.']);
  }
  // El precio de que la orden de trabajo NO lleve valor: el camión entra hoy y
  // la factura llega el 30, y lo que impide que esa decisión se vuelva un
  // agujero es que la ausencia se cuente. Solo cuenta órdenes ya cerradas — un
  // camión que sigue adentro no es una factura perdida.
  if (h.trabajos_sin_factura > 0) {
    filas.push(['yellow', `${h.trabajos_sin_factura} trabajo(s) de taller sin factura recibida`,
      'El vehículo ya volvió y el gasto no se puede cruzar con la causación. ' +
      'La orden de trabajo no lleva valor a propósito: la factura llega ' +
      'después, y esto mide si está llegando.']);
  }
  // Este es el que dice si la fase está haciendo algo. En cero durante meses
  // significa que la búsqueda que evita pagar dos veces no tiene sobre qué
  // pronunciarse — distinto de «no hubo taller», que se lee arriba.
  if (h.garantias_vigentes > 0) {
    filas.push(['tx2', `${h.garantias_vigentes} reparación(es) todavía en garantía`,
      'Antes de mandar el camión al taller por el mismo sistema, conviene ' +
      'mirarlas: el caso que evitan es el que se paga dos veces. Se ven en ' +
      '«Taller», en el expediente de cada vehículo.']);
  }

  // Llantas (2026-09-02). Los dos primeros van en líneas separadas y el orden
  // importa: el segundo es el que impide que el primero se apague en silencio.
  //
  // Y el renglón NO dice «el camión anda sin llanta», porque casi nunca es eso:
  // dice que la llanta está puesta y nadie la registró. Un texto que sonara a
  // falla mecánica mandaría a alguien a mirar un camión que está bien, y a la
  // tercera vez el bloque se deja de leer.
  if (h.posiciones_sin_llanta > 0) {
    filas.push(['yellow', `${h.posiciones_sin_llanta} posición(es) de llanta sin registrar`,
      'No es que el camión ande sin rueda: es que la llanta está puesta y ' +
      'nadie la registró. Mide cuánto le falta al inventario para describir ' +
      'el vehículo real. Se completa desde «Llantas», en cada vehículo.']);
  }
  if (h.vehiculos_sin_posiciones_llanta > 0) {
    filas.push(['yellow', `${h.vehiculos_sin_posiciones_llanta} vehículo(s) sin ficha, que no se pudieron revisar`,
      'Su ficha técnica no dice cuántas posiciones de llanta tiene, así que ' +
      'nada los compara contra nada. No es que estén completos: es que no se ' +
      'miraron.']);
  }
  if (h.llantas_montadas > 0) {
    filas.push(['tx2', `${h.llantas_montadas} llanta(s) montadas y con su reloj corriendo`,
      'Cada una acumula kilómetros desde el montaje. El kilometraje no se ' +
      'guarda: se calcula entre las dos lecturas de odómetro, y sale con su ' +
      'marca de confianza.']);
  }
  // El hecho medido por posición, SIN umbral (regla 13): no hay una sola llanta
  // medida en esta flota, y un «se cambia a los X km» escrito hoy sería a ojo.
  // Por posición y no por vehículo a secas porque el modo de fallo caro no es
  // que se gasten, es que se gasten mal — y eso solo se ve por posición.
  const pos = h.km_por_posicion;
  if (pos && pos.length) {
    const conMediana = pos.filter(p => p.mediana_km !== 'sin_dato');
    const linea = conMediana.length
      ? conMediana.map(p => `${p.placa} pos ${p.posicion}: ${Number(p.mediana_km).toLocaleString('es-CO')} km`).join(' · ')
      : pos.map(p => `${p.placa} pos ${p.posicion}: ${p.n} de ${p.n + p.faltan}`).join(' · ');
    filas.push(['tx2', `Vida de llanta medida por posición — ${linea}`,
      conMediana.length
        ? 'Es la mediana de las vidas ya cerradas de esa posición. No se ' +
          'compara con otra posición ni con otro vehículo: una direccional y ' +
          'una de tracción no duran lo mismo y la diferencia no dice nada.'
        : 'Todavía no alcanzan para fijar una vida útil: se muestran cuántas ' +
          'llantas desmontadas hay y cuántas faltan. Con menos, una sola ' +
          'pinchada partiría la mediana a la mitad.']);
  }

  // ── Preventivo (2026-09-02) ──────────────────────────────────────────
  //
  // `distribucion_km_cambio` está cargado en la base desde la tanda 1 y nadie
  // lo leía. Esto es el lector, y va en rojo arriba de todo lo amarillo por una
  // razón concreta: una correa de distribución que revienta en un motor de
  // interferencia no es una correa, es un motor.
  //
  // Los cuatro contadores van en LÍNEAS SEPARADAS y no sumados, porque cada uno
  // se corrige llamando a una persona distinta: al taller, al que consigue el
  // repuesto, al que sabe cuándo se hizo la última vez, y al concesionario.
  if (h.tareas_vencidas > 0) {
    filas.push(['red', `${h.tareas_vencidas} tarea(s) de mantenimiento VENCIDAS`,
      'El odómetro ya pasó el kilometraje de cambio que dice la ficha. No hay ' +
      'umbral acá: el número lo puso el fabricante. Se ven y se registran en ' +
      'el botón «Preventivo» de cada vehículo.']);
  }
  if (h.tareas_por_vencer > 0) {
    filas.push(['yellow', `${h.tareas_por_vencer} tarea(s) llegan al cambio pronto`,
      'Calculado con el ritmo medido de cada vehículo, no con un promedio de ' +
      'flota. Es el momento de conseguir el repuesto, que es lo que de verdad ' +
      'tarda.']);
  }
  if (h.tareas_sin_linea_base > 0) {
    filas.push(['yellow', `${h.tareas_sin_linea_base} tarea(s) sin saber cuándo se hizo la última vez`,
      'No están al día ni vencidas: no hay contra qué comparar, y por eso no ' +
      'avisan. Es el trabajo más barato del tablero — se cierra registrando ' +
      'la última ejecución en «Preventivo».']);
  }
  // El que impide que los tres de arriba se apaguen sin que nadie lo note. La
  // ficha dice QUÉ aceite lleva el motor y no CADA CUÁNTOS KM se cambia.
  if (h.tareas_sin_intervalo > 0) {
    filas.push(['yellow', `${h.tareas_sin_intervalo} tarea(s) sin intervalo declarado`,
      'La ficha dice qué lleva el vehículo, no cada cuántos kilómetros se ' +
      'cambia. Nada las compara contra nada: no es que estén bien, es que no ' +
      'se pudieron mirar. Se arregla con una llamada y el intervalo se guarda ' +
      'con de dónde salió.']);
  }
  // El HECHO que descarga la regla 13 sobre el único umbral de la fase. Se
  // pinta sin juzgarlo: no hay un km/día «normal» medido con qué compararlo.
  const ritmos = h.km_dia_por_vehiculo;
  if (ritmos && ritmos.length) {
    const conDato = ritmos.filter(r => r.km_dia !== 'sin_dato');
    const linea = conDato.length
      ? conDato.map(r => `${r.placa}: ${r.km_dia} km/día (${r.marca}, ${r.n} lecturas)`).join(' · ')
      : ritmos.map(r => `${r.placa}: sin dato`).join(' · ');
    filas.push(['tx2', `Ritmo de uso medido — ${linea}`,
      conDato.length
        ? 'Es lo que convierte «faltan 500 km» en «unos 6 días». No se compara ' +
          'entre vehículos: un motocarro urbano y un camión de ruta no ruedan ' +
          'igual y la diferencia no dice nada.'
        : 'Ningún vehículo tiene dos lecturas vigentes con las que medirlo, ' +
          'así que los días que faltan para cada mantenimiento salen «sin ' +
          'dato». No es cero: es que no se puede calcular todavía.']);
  }

  // ── Los doce campos que se medían y no leía nadie (2026-09-03) ────────
  //
  // `/flota/health` publicaba 44 campos y la pantalla pintaba 32. Los otros
  // doce eran **captura sin lector**: el mismo defecto que este bloque existe
  // para cerrar, cometido dentro de él. Once venían de la tanda 1 y uno
  // —`lecturas_ts_duplicado`— se agregó el 2026-09-01 y nació mudo.
  //
  // Van agrupados por lo que significan y no apilados: un tablero que suelta
  // doce números seguidos se deja de mirar, que es la lección de los 639
  // avisos conocidos.

  // Custodia: quién responde por cada camión, y qué falta para poder decirlo.
  if (h.vehiculos_sin_custodia_activa > 0) {
    filas.push(['red', `${h.vehiculos_sin_custodia_activa} vehículo(s) sin responsable ahora mismo`,
      'Nadie tiene la custodia. Si aparece un golpe hoy, no hay a quién preguntarle qué pasó.']);
  }
  if (h.custodias_cerradas_forzadas > 0) {
    filas.push(['yellow', `${h.custodias_cerradas_forzadas} turno(s) cerrados sin la firma del custodio`,
      'Mide conducta, no fallas: alguien cerró el turno de otro sin fotos de cierre, ' +
      'y el turno siguiente arrancó sin nada con qué comparar.']);
  }
  if (h.custodias_pendiente_sede > 0) {
    filas.push(['yellow', `${h.custodias_pendiente_sede} custodia(s) con la sede sin resolver`,
      'El vehículo quedó en una sede que el WMS todavía no tiene como fila.']);
  }
  if (h.custodias_sin_foto_completa > 0) {
    filas.push(['tx2', `${h.custodias_sin_foto_completa} turno(s) sin el juego completo de fotos`,
      'No bloquea la salida a propósito — un camión no se queda en el patio por una foto. ' +
      'Pero sin las dos puntas no hay con qué atribuir un daño.']);
  }

  // Papeles y cuentas: lo que hace falta para que el sistema sepa de quién habla.
  if (h.conductores_activos_sin_cuenta > 0) {
    filas.push(['yellow', `${h.conductores_activos_sin_cuenta} conductor(es) activos sin cuenta de usuario`,
      'Sin cuenta no pueden recibir su propio turno ni reportar un daño con su nombre: ' +
      'el sistema no puede distinguirlos de alguien que dice serlo.']);
  }
  if (h.documentos_por_vencer_30d > 0) {
    filas.push(['tx2', `${h.documentos_por_vencer_30d} documento(s) vencen dentro de 30 días`,
      'Todavía hay tiempo. Una cita de tecnomecánica en Neiva tarda unos quince días.']);
  }
  if (h.fotos_pendiente_evidencia > 0) {
    filas.push(['yellow', `${h.fotos_pendiente_evidencia} foto(s) que el almacén no pudo guardar`,
      'La fila quedó marcada en vez de fingir que el archivo existe. No hay evidencia detrás.']);
  }

  // Odómetro: el ruido que hay que poder contar antes de calcular sobre la serie.
  if (h.lecturas_ts_duplicado > 0) {
    filas.push(['tx2', `${h.lecturas_ts_duplicado} lectura(s) comparten vehículo y segundo con otra`,
      'Vienen de reintentos. No rompen nada hoy, pero el orden de la serie se ' +
      'resuelve por hora y un empate es ruido sobre el que después se calcula el CPK.']);
  }

  // Cobertura del levantamiento: cuánto del expediente está hecho.
  if (h.vehiculos_activos > 0 && h.fichas_completas < h.vehiculos_activos) {
    filas.push(['tx2', `${h.fichas_completas} de ${h.vehiculos_activos} fichas técnicas completas`,
      'Lo que falta de la ficha apaga lo que cuelga de ella: sin capacidad de tanque no ' +
      'hay control de sobre-tanqueo, y sin kilometraje de correa no hay preventivo.']);
  }
  if (h.rutas_historicas_sin_placa > 0) {
    filas.push(['tx2', `${h.rutas_historicas_sin_placa} ruta(s) históricas sin placa`,
      'No se pueden atribuir a ningún vehículo, así que no entran a ningún costo por kilómetro.']);
  }

  // Y de qué mundo salen todos los números de arriba. Va AL FINAL y siempre
  // que no sean datos reales: un tablero de QA que no dice que es de QA es
  // peor que ninguno — es el incidente de las ocho horas escribiendo en la
  // base equivocada, con otra cara.
  if (h.datos_reales === false) {
    filas.push(['yellow', `Estos números NO son de la operación real (${h.ambiente || 'ambiente sin declarar'})`,
      'Sirven para probar la pantalla, no para decidir nada.']);
  }

  if (!filas.length) return '';
  const li = filas.map(([color, titulo, nota]) => `
    <li style="margin-bottom:10px">
      <b style="color:var(--${color})">${titulo}</b><br>
      <span style="color:var(--tx2);font-size:13px">${nota}</span>
    </li>`).join('');
  return `<div class="tabla-card" style="border-left:3px solid var(--yellow)">
    <h3>Salud de la flota</h3>
    <ul style="line-height:1.5;margin:0">${li}</ul>
  </div>`;
}

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
    <h3 style="color:var(--yellow)">Fuera de sede ahora (${esc(filas.length)})</h3>
    <p style="font-size:13px;color:var(--tx2)">Estos vehículos <b>no están en un
    patio de la empresa</b>. La custodia sigue en la persona que los tiene — no
    pasó a ninguna sede, porque ninguna sede los vio.</p>
    <ul style="line-height:1.6">${filas.map(f => `
      <li style="margin-bottom:8px">
        <b>${esc(f.placa)}</b> — responde <b>${esc(f.responde)}</b>, desde ${horaColombia(f.desde)}
        · ${esc(f.km)} km<br>
        <span style="color:var(--tx2)">${esc(f.motivo || 'sin motivo escrito')}</span>
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
      <b>${esc(c.placa)}</b> — lo tenía <b>${esc(c.lo_tenia)}</b>, lo cerró ${esc(c.forzado_por)}
      el ${horaColombia(c.cuando)}<br>
      <span style="color:var(--tx2)">${esc(c.motivo || '')}</span>
    </li>`).join('');
  return `<div class="tabla-card" style="border-left:3px solid var(--red)">
    <h3 style="color:var(--red)">Turnos cerrados a la fuerza (${esc(cierres.length)})</h3>
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

/** El texto de un error de la API, con lo que el servidor se esmeró en decir.
 *
 * Un 403 de flota trae `tu_rol` y `roles_permitidos` — el docstring de `exige`
 * dice por qué: *«El 403 dice qué hace falta, no solo que no. Un "sin permiso"
 * pelado deja a quien lo recibe sin saber a quién pedirle qué, y termina en un
 * mensaje de WhatsApp al desarrollador.»*
 *
 * **Y terminaba ahí igual**, porque la pantalla mostraba solo `error`. El
 * servidor lo decía y nadie lo leía.
 */
function flotaMensajeDeError(d) {
  if (!d) return 'Error sin detalle';
  let txt = d.error || d.detalle || 'Error sin detalle';
  if (d.tu_rol || d.roles_permitidos) {
    txt += ` — tu rol es «${d.tu_rol || 'sin sesión'}»`;
    if (d.roles_permitidos && d.roles_permitidos.length) {
      txt += `; esto lo hace ${d.roles_permitidos.join(' o ')}`;
    }
  }
  return txt;
}

/** Tanqueo y lectura suelta, desde la pantalla del CONDUCTOR.
 *
 * Los dos endpoints piden `LECTURA_FLOTA` —el permiso se escribió para él, con
 * el argumento «el que tanquea es el que maneja»— y hasta el 2026-09-03 vivían
 * SOLO en el panel del encargado. O sea: el que tiene la factura en la mano no
 * podía cargarla, y quien podía no estaba en la estación.
 *
 * No lo veía el trinquete de rutas huérfanas porque el endpoint **sí** tenía
 * consumidor: el del escritorio. La forma es más fina que «endpoint sin
 * pantalla» — es **un permiso más ancho que el gesto que la pantalla ofrece**,
 * y por eso hizo falta un detector nuevo que cruza los roles que el endpoint
 * autoriza contra los roles que pueden llegar al botón.
 *
 * Reusa `flotaAbrirGastos` y `flotaAbrirOdometro`: la pantalla es la misma, lo
 * que faltaba era la puerta. Una segunda copia del formulario sería la que
 * diverja el día que cambie el CHECK del tanque.
 */
function flotaCondTanquear() {
  const placa = (FLOTA_COND && FLOTA_COND.placa) || FLOTA_PLACA;
  if (!placa) { alerta('Primero recibí el turno.', 'advertencia'); return; }
  FLOTA_PLACA = placa;
  flotaAbrirGastos(placa);
}

function flotaCondOdometro() {
  const placa = (FLOTA_COND && FLOTA_COND.placa) || FLOTA_PLACA;
  if (!placa) { alerta('Primero recibí el turno.', 'advertencia'); return; }
  FLOTA_PLACA = placa;
  flotaAbrirOdometro(placa);
}

/** Lo que le pasa al camión, dicho antes de que lo agarre.
 *
 * Va ARRIBA de los botones y no escondido en un submenú: el conductor abre esta
 * pantalla dos minutos a las 5 a.m., y un dato que exige un toque más no existe.
 *
 * **Informa, no bloquea.** Es la secuencia obligatoria del módulo —medir,
 * corregir, imponer— y la regla 1: dejar un camión en el patio por una pantalla
 * es cómo la operación desmonta el sistema en 48 horas. Lo que cierra es la
 * frase «no sabía».
 *
 * Devuelve vacío cuando no hay nada que decir, igual que los bloques del
 * tablero del encargado: una línea que siempre aparece se deja de leer.
 */
/** El km/galón del camión que el conductor tiene hoy.
 *
 * `piso-conductor.md:149` lo promete como señal de que está haciendo bien el
 * trabajo desde el 2026-08-04, y el sistema se lo negaba —
 * `docs/procedimientos/README.md:16` dice que ningún procedimiento puede
 * prometer lo que el sistema niega.
 *
 * **Sin semáforo y sin meta.** No hay una sola medición de esta flota con la
 * que fijar un techo, y un umbral escrito hoy sería a ojo (regla 13). Se dice
 * cuánto rindió; si está bien o mal lo decide alguien con datos, dentro de unos
 * meses.
 *
 * Y se dice, en la pantalla y no en un instructivo aparte, **lo que el número
 * no afirma**: mide el vehículo, no a quien maneja. Una ruta con más montaña,
 * un filtro tapado y un sifón dan exactamente el mismo número.
 */
function flotaCondRendimiento(r) {
  if (!r) return '';   // sin camión asignado hoy: no hay nada que medir
  // `publicable: false` NO se pinta como un hueco vacío: se pinta como una
  // medición en curso, con lo que falta. Un vehículo midiendo desde hace un mes
  // y uno que nadie tanqueó nunca se corrigen distinto — el primero solo
  // necesita que pase el tiempo.
  const cuerpo = r.publicable
    ? `<b style="font-size:18px">${esc(r.km_galon)} km/galón</b>
       <div style="font-size:12px;color:var(--tx2);margin-top:2px">
         sobre ${esc(r.ventanas)} ventana(s) de tanque lleno a tanque lleno,
         ${esc(r.dias_historia)} día(s) de historia${
           r.tanqueos_fuera_por_parcial
             ? ` · ${esc(r.tanqueos_fuera_por_parcial)} tanqueo(s) quedaron fuera por no estar marcados «lleno»`
             : ''}</div>`
    : `<b style="color:var(--tx2)">Midiendo todavía</b>
       <div style="font-size:12px;color:var(--tx2);margin-top:2px">${esc(r.motivo)}</div>`;
  return `<div class="tabla-card" style="margin-top:10px">
    <div style="font-size:13px;color:var(--tx2);margin-bottom:4px">Rendimiento del vehículo</div>
    ${cuerpo}
    <div style="font-size:11px;color:var(--tx2);margin-top:6px">${esc(r.no_afirma)}</div>
  </div>`;
}

function flotaCondEstado(e) {
  if (!e) return '';
  const lineas = [];

  if (e.hallazgos_vencidos > 0) {
    lineas.push(['var(--red)',
      `${e.hallazgos_vencidos} daño(s) pasados de su fecha límite`]);
  } else if (e.hallazgos_abiertos > 0) {
    lineas.push(['var(--yellow)', `${e.hallazgos_abiertos} daño(s) abiertos`]);
  }
  // El peor, con su nombre: un contador dice cuántos, no cuál mirar.
  if (e.hallazgo_peor) {
    lineas.push([e.hallazgo_peor.vencido ? 'var(--red)' : 'var(--tx2)',
      `${e.hallazgo_peor.criticidad}: ${e.hallazgo_peor.descripcion}`]);
  }

  (e.documentos_vencidos || []).forEach(d => {
    lineas.push(['var(--red)', `${d.tipo} VENCIDO desde ${d.vencio}`]);
  });

  const i = e.inspeccion_de_hoy || {};
  if (!i.hecha) {
    lineas.push(['var(--yellow)', 'Falta la inspección de hoy']);
  } else if (i.habilita_despacho === false) {
    // `habilita_despacho` tenía un solo lector —un mensaje que desaparecía— y
    // ahora le llega a quien decide si arranca.
    lineas.push(['var(--red)',
      `Inspección de hoy: ${i.veredicto} · NO habilita despacho`]);
  }

  if (!lineas.length) return '';
  return `<div style="margin:6px 0 10px;padding:8px 10px;border-left:3px solid var(--red);
      background:rgba(255,255,255,.03);border-radius:4px">
    ${lineas.map(([c, txt]) =>
      `<div style="color:${c};font-size:13px;line-height:1.5">${txt}</div>`).join('')}
  </div>`;
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
    cabeza = `<div class="flota-placa">${esc(d.placa)}</div>
      <div style="color:var(--green);font-size:13px">Tu turno está abierto · ${km}</div>`;
  } else if (d.origen === 'ruta') {
    cabeza = `<div class="flota-placa">${esc(d.placa)}</div>
      <div style="color:var(--yellow);font-size:13px">Según tu ruta de hoy.
      <b>Confirmá que la placa es la del camión que tenés enfrente.</b> · ${km}</div>`;
  } else {
    cabeza = `<div style="color:var(--yellow);font-size:14px">Elegí el vehículo que vas a recibir:</div>`;
  }

  // Con origen 'ruta' ya se sabe cuál vehículo le toca — mostrar los otros
  // cinco al lado invita a tocar el equivocado por error. Solo 'eleccion'
  // (sin custodia propia ni ruta de hoy) necesita de verdad elegir de una
  // lista. La ruta sigue siendo sugerencia, no verdad (turno.py) — por eso
  // queda un escape de un toque, no un botón invisible ni un bloqueo duro.
  let lista = '';
  if (d.origen === 'eleccion') {
    lista = flotaCondListaCandidatos();
  } else if (d.origen === 'ruta') {
    lista = `<div style="margin-top:8px">
      <button class="btn-flota" style="font-size:12px;padding:4px 10px"
              onclick="flotaCondMostrarTodos()">¿No es este tu vehículo? Elegir otro</button>
      <div id="cond-flota-todos"></div>
    </div>`;
  }

  // Con el turno abierto, flota se colapsa a una línea.
  //
  // El conductor abre la app para ENTREGAR PEDIDOS. Flota es un trámite de dos
  // minutos que hace una vez al día. Ponerlo entero arriba —con la lista de
  // vehículos y diez turnos de historial— lo obliga a atravesarlo para llegar a
  // su trabajo, todos los días. Eso es invertir la prioridad, y lo hice yo.
  // La inspección vive acá y no en el bloque de abajo **a propósito**: es
  // PREoperacional del turno que ya se recibió. Antes del recibo no hay
  // custodia ni kilometraje anclado, así que una inspección de ese momento
  // colgaría de una lectura que todavía nadie tomó — y el gesto real de las
  // 5 a.m. es recibir el camión y después mirarlo. Es el primer botón porque es
  // lo primero que hay que hacer con el turno abierto.
  if (d.tiene_turno_abierto) {
    el.innerHTML = `<div class="flota-veh" style="padding:10px 14px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <span style="font-size:17px;font-weight:800;letter-spacing:.05em">🚚 ${esc(d.placa)}</span>
        <span style="color:var(--green);font-size:13px">turno abierto · ${km}</span>
        <span style="flex:1"></span>
        <button class="btn-primary" style="padding:6px 12px;font-size:13px;margin-top:0"
                onclick="flotaCondInspeccion()">Inspección de hoy</button>
        <button class="btn-flota" style="padding:6px 12px;font-size:13px"
                onclick="flotaCondAbrirEntrega()">Entregar turno</button>
        <button class="btn-flota" style="padding:6px 12px;font-size:13px"
                onclick="flotaCondReportarDano()">Reportar daño</button>
        <button class="btn-flota" style="padding:6px 12px;font-size:13px"
                onclick="flotaCondTanquear()">Tanqueo</button>
        <button class="btn-flota" style="padding:6px 12px;font-size:13px"
                onclick="flotaCondOdometro()">Odómetro</button>
        <button class="btn-flota" style="padding:6px 12px;font-size:13px"
                onclick="flotaCondMisReportes()">Mis turnos</button>
      </div>
      ${flotaCondEstado(d.estado_vehiculo)}
      ${flotaCondRendimiento(d.rendimiento)}
      <div id="cond-flota-form"></div>
    </div>`;
    return;
  }

  // Sin turno abierto sí ocupa espacio: recibir el vehículo es lo primero que
  // hay que hacer, antes del manifiesto de ruta.
  el.innerHTML = `<div class="flota-veh">
    ${cabeza}${flotaCondEstado(d.estado_vehiculo)}${lista}
    <div style="display:flex;gap:6px;margin-top:12px">
      <button class="btn-primary" style="flex:2;margin-top:0" onclick="flotaCondAbrirRecibo()">
        Recibir turno</button>
      <button class="btn-flota" style="flex:1" onclick="flotaCondMisReportes()">Mis turnos</button>
    </div>
    <div id="cond-flota-form"></div>
  </div>`;
}

/** HTML de la lista completa de vehículos elegibles, con quién los tiene. */
function flotaCondListaCandidatos() {
  return '<div style="margin-top:10px">' + (FLOTA_COND.candidatos || []).map(c => {
    if (c.ocupado_por) {
      // El mensaje nombra a la persona. Un 409 crudo deja al conductor
      // mirando el celular en el patio sin saber a quién llamar.
      return `<div style="padding:10px;margin:4px 0;border:1px solid var(--rbg);border-radius:8px;opacity:.75">
        <b>${esc(c.placa)}</b> · ${esc(c.tipo)}<br>
        <span style="color:var(--red);font-size:12px">Lo tiene ${esc(c.ocupado_por)}.
        Si lo vas a recibir vos, tiene que cerrar su turno primero.</span>
      </div>`;
    }
    const sel = c.vehiculo_id === FLOTA_COND_ELEGIDO;
    return `<button class="btn-flota ${sel ? 'ok' : ''}" onclick="flotaCondElegir(${esc(c.vehiculo_id)})"
      style="display:block;width:100%;text-align:left">
      ${sel ? '✓ ' : ''}<b style="font-size:19px;letter-spacing:.05em">${esc(c.placa)}</b> · ${esc(c.tipo)}</button>`;
  }).join('') + '</div>';
}

/** Despliega la lista completa cuando el vehículo de la ruta no es el correcto. */
function flotaCondMostrarTodos() {
  const el = document.getElementById('cond-flota-todos');
  if (el) el.innerHTML = flotaCondListaCandidatos();
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
    <!-- Sin este div, «cómo estaba» responde «la pantalla no tiene dónde
         mostrar la foto». Estaba en el recibo de escritorio y en documentos, y
         faltaba en las DOS pantallas del conductor — que son las únicas donde
         ese botón existe. -->
    <div id="flota-visor" style="margin-top:12px"></div>
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
           onclick="flotaVerFoto(${esc(FLOTA_REFERENCIA[a])}, 'así estaba al recibir — ${flotaNombreAngulo(a)}')"
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
    <div id="flota-visor" style="margin-top:12px"></div>

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
    el.innerHTML = `<div style="color:var(--red);padding:12px">${esc(e.message)}</div>`;
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
      filas.push(`<li style="color:var(--red)"><b>${esc(t.placa)}</b> ${cuando} —
        <b>te cerraron el turno</b>: ${esc(t.motivo_del_cierre_forzado || 'sin motivo')}</li>`);
    } else if (t.abierto) {
      filas.push(`<li style="color:var(--green)"><b>${esc(t.placa)}</b> ${cuando} — abierto ahora</li>`);
    } else if ((t.km_fin - t.km_inicio) === 0) {
      vacios++;   // se cuentan, no se listan
    } else {
      filas.push(`<li><b>${esc(t.placa)}</b> ${cuando} — cerrado · ${t.km_fin - t.km_inicio} km</li>`);
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
      ${esc(a.telefono)} — ${params} · ${estado(a)}
      ${a.detalle ? `<br><small style="color:var(--red)">${esc(a.detalle)}</small>` : ''}</li>`;
  }).join('');
  if (!filas) filas = '<li style="color:var(--tx2)">Ninguno todavía.</li>';

  const alarma = d.sin_confirmar_6h > 0
    ? `<p style="color:var(--red)"><b>${esc(d.sin_confirmar_6h)} aviso(s) salieron hace más de
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
    if (!r.ok) { alerta(flotaMensajeDeError(d), 'error'); return; }
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

// ═══════════════════════════════════════════════════════════════════════════
// Daños — el pedido «llevar control sobre los daños que pasan»
// ═══════════════════════════════════════════════════════════════════════════

/** Chip de severidad. El color es el plazo, no la estética.
 *
 * `bloqueante` es HOY, `mayor` siete días, `menor` treinta. Los tres colores
 * corresponden a esos tres plazos y no a "qué tan feo se ve": si el rojo se
 * usara para lo llamativo, dejaría de decir cuándo hay que arreglarlo.
 */
function flotaChipCriticidad(c) {
  const COLOR = { bloqueante: 'var(--red)', mayor: 'var(--yellow)', menor: 'var(--tx2)' };
  return `<span style="font-size:11px;font-weight:700;text-transform:uppercase;
    color:${esc(COLOR[c])};border:1px solid ${esc(COLOR[c])};border-radius:6px;padding:1px 6px">${c}</span>`;
}

/** Una fila de daño, con lo que hace falta para decidir qué hacer con ella.
 *
 * Los tres juicios —vencido, días abiertos, si entra al indicador— vienen del
 * SERVIDOR, que los pide al dominio. No se recalculan acá: una regla escrita
 * dos veces diverge, y la copia que diverge es la que la gente mira.
 */
function flotaFilaHallazgo(h, conAcciones) {
  const abierto = h.estado === 'abierto';
  const alerta_ = h.vencido
    ? `<span style="color:var(--red);font-weight:700">VENCIDO</span> · `
    : '';
  const aplazos = h.aplazado_veces > 0
    ? ` · <span style="color:var(--yellow)">aplazado ${esc(h.aplazado_veces)}×</span>`
    : '';
  // Se dice por qué no cuenta, en vez de esconderlo. Un hallazgo de línea base
  // se ve igual de abierto que cualquiera y hay que arreglarlo igual — lo que
  // no hace es contarle a nadie el tiempo que lleva.
  const base = h.linea_base
    ? ` · <span style="color:var(--tx2)" title="Estaba antes de que alguien recibiera el vehículo: se arregla igual, pero no le cuenta a nadie">preexistente</span>`
    : '';
  const acciones = (abierto && conAcciones && flotaDecide()) ? `
    <div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
      <button class="btn-flota" style="padding:4px 10px;font-size:12px"
              onclick="flotaCerrarHallazgo(${esc(h.id)})">Reparado</button>
      <button class="btn-flota" style="padding:4px 10px;font-size:12px"
              onclick="flotaAplazarHallazgo(${esc(h.id)})">Aplazar 7 días</button>
      <button class="btn-flota" style="padding:4px 10px;font-size:12px"
              onclick="flotaDescartarHallazgo(${esc(h.id)})">No era nada</button>
    </div>` : '';
  const desenlace = abierto ? '' :
    `<div style="font-size:12px;color:var(--tx2);margin-top:4px">
       ${esc(h.estado)} · ${h.cerrado_ts ? horaColombia(h.cerrado_ts) : 'sin fecha'}
       ${h.motivo_cierre ? '· ' + h.motivo_cierre : ''}</div>`;

  return `<li style="margin-bottom:12px;border-left:2px solid var(--bd);padding-left:10px">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      ${flotaChipCriticidad(h.criticidad)}
      <b>${esc(h.descripcion)}</b>
    </div>
    <div style="font-size:12px;color:var(--tx2);margin-top:2px">
      ${alerta_}lleva ${esc(h.dias_abierto)} día(s) · límite ${horaColombia(h.fecha_limite)}${aplazos}${base}
    </div>
    ${desenlace}${acciones}
  </li>`;
}

/** Abre el expediente de daños del vehículo. */
async function flotaAbrirDanos(placa) {
  FLOTA_PLACA = placa;
  flotaAbrirModal('Daños del vehículo', placa);
  const cont = document.getElementById('flota-recibo');
  cont.innerHTML = '<div class="tabla-card">Cargando…</div>';
  await flotaRenderDanos(true);
}

/** Pinta la lista y el formulario. `conAcciones` distingue las dos pantallas:
 * el conductor reporta, no cierra — cerrar lo que uno mismo reportó es el
 * camino barato de la regla 11. */
async function flotaRenderDanos(conAcciones) {
  const cont = document.getElementById('flota-recibo');
  let d;
  try {
    d = await get('/flota/hallazgos/' + encodeURIComponent(FLOTA_PLACA));
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudieron cargar los daños: ${esc(e.message)}</div>`;
    return;
  }
  const lista = (d.hallazgos || []).length
    ? `<ul style="list-style:none;padding:0">${
        d.hallazgos.map(h => flotaFilaHallazgo(h, conAcciones)).join('')}</ul>`
    : `<p style="color:var(--tx2)">Sin daños abiertos.</p>`;

  const encabezado = d.vencidos
    ? `<div style="color:var(--red);font-weight:700;margin-bottom:8px">
         ${esc(d.vencidos)} de ${esc(d.abiertos)} pasaron su fecha límite</div>`
    : '';

  // Va UNA vez arriba de la lista y no en cada fila: repetida seis veces es
  // ruido, y el ruido es cómo un renglón deja de leerse. Se pinta solo en la
  // pantalla donde los botones existirían — al conductor no le falta nada.
  const escalar = (conAcciones && !flotaDecide())
    ? `<div style="font-size:12px;color:var(--tx2);margin-bottom:8px;
                   border-left:3px solid var(--brd);padding-left:8px">
         <b>Cerrar, aplazar o descartar un daño lo decide gestión.</b> Los días
         que un daño lleva abierto y los que pasaron su fecha límite son dos de
         las señales con las que se mide a control de flota — el botón que las
         baja no puede ser suyo. Lo que sí: reportarlo acá abajo, y escalar el
         que venza.</div>`
    : '';

  cont.innerHTML = `<div class="tabla-card">
    ${encabezado}${escalar}${lista}
  </div>
  <div class="tabla-card">
    <div class="tabla-titulo">Reportar un daño</div>
    <label>Qué encontró</label>
    <input id="hz-desc" style="width:100%;padding:6px" placeholder="Ej: fuga de aceite en el diferencial">
    <label>Qué tan grave</label>
    <select id="hz-crit" style="width:100%;padding:6px">
      <option value="bloqueante">Bloqueante — el vehículo no debe salir. Se arregla HOY</option>
      <option value="mayor" selected>Mayor — hay 7 días</option>
      <option value="menor">Menor — hay 30 días</option>
    </select>
    <label>Kilometraje actual</label>
    <input type="number" id="hz-km" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    <p style="font-size:12px;color:var(--tx2);margin:6px 0 0">
      El kilometraje va con el daño para poder cruzarlo después contra los
      mantenimientos. La fecha límite <b>no se elige</b>: sale de la gravedad.</p>
    <button class="btn-primary" style="margin-top:12px;width:100%"
            onclick="flotaReportarDano(${conAcciones ? 'true' : 'false'})"
            id="hz-guardar" data-placa="${FLOTA_PLACA}">Reportar</button>
    <div id="hz-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
}

/** Envía el reporte. */
async function flotaReportarDano(conAcciones) {
  const err = document.getElementById('hz-error');
  err.textContent = '';
  const desc = document.getElementById('hz-desc').value.trim();
  if (!desc) { err.textContent = 'Sin descripción nadie va a saber qué buscar en el vehículo.'; return; }
  const km = parseInt(document.getElementById('hz-km').value, 10);
  if (!Number.isFinite(km) || km < 0) { err.textContent = 'El kilometraje es obligatorio.'; return; }
  const placa = flotaPlacaDelFormulario('hz-guardar', 'hz-error');
  if (!placa) return;

  const listo = flotaBotonOcupado('hz-guardar', 'Reportando…');
  try {
    const r = await fetch(API + '/flota/hallazgos', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({
        placa: placa, descripcion: desc, km: km,
        criticidad: document.getElementById('hz-crit').value,
      }),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = flotaMensajeDeError(d); return; }
    alerta('Daño registrado ✓', 'exito');
    await flotaRenderDanos(conAcciones);
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

/** Las tres salidas del hallazgo, contra el mismo endpoint por verbo.
 *
 * `descartar` y `aplazar` piden motivo y **no se mandan sin él**: el servidor
 * también lo exige, y pedirlo acá evita un viaje que iba a fallar. La
 * validación de verdad es la del servidor — esta es cortesía, no control.
 *
 * **Las tres URLs van escritas enteras, no armadas con `+ verbo`.** El
 * trinquete de rutas huérfanas mide adyacencia sobre el texto del PWA: con la
 * URL concatenada, `/flota/hallazgos/${id}/${verbo}` no contiene ningún
 * `/aplazar` y el endpoint se declara sin consumidor. Lo destapó el trinquete
 * en esta misma tanda — y tenía razón: una URL que solo existe en tiempo de
 * ejecución no se puede auditar leyendo el repo.
 */
const FLOTA_HALLAZGO_URL = {
  cerrar:     (id) => `/flota/hallazgos/${id}/cerrar`,
  descartar:  (id) => `/flota/hallazgos/${id}/descartar`,
  aplazar:    (id) => `/flota/hallazgos/${id}/aplazar`,
};

async function flotaAccionHallazgo(id, verbo, cuerpo) {
  try {
    const r = await fetch(API + FLOTA_HALLAZGO_URL[verbo](id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(cuerpo || {}),
    });
    const d = await r.json();
    if (!r.ok) { alerta(flotaMensajeDeError(d), 'error'); return; }
    alerta('Listo ✓', 'exito');
    await flotaRenderDanos(true);
  } catch (e) {
    alerta('Sin conexión: ' + e.message, 'error');
  }
}

function flotaCerrarHallazgo(id) {
  const nota = prompt('¿Qué se hizo? (opcional)') || '';
  flotaAccionHallazgo(id, 'cerrar', { nota: nota });
}

function flotaDescartarHallazgo(id) {
  const motivo = prompt('¿Por qué no era un daño? (obligatorio)');
  if (!motivo || !motivo.trim()) {
    alerta('Un descarte sin motivo escrito no se puede distinguir de hacer ' +
           'desaparecer un hallazgo incómodo.', 'advertencia');
    return;
  }
  flotaAccionHallazgo(id, 'descartar', { motivo: motivo.trim() });
}

function flotaAplazarHallazgo(id) {
  const motivo = prompt('¿Por qué se aplaza? (obligatorio — queda en la bitácora)');
  if (!motivo || !motivo.trim()) {
    alerta('Un plazo que se mueve sin razón anotada es un plazo que no existe.',
           'advertencia');
    return;
  }
  flotaAccionHallazgo(id, 'aplazar', { motivo: motivo.trim() });
}

/** El conductor reporta desde su pantalla, sin las acciones de desenlace. */
async function flotaCondReportarDano() {
  // De `FLOTA_COND` y no de `FLOTA_PLACA`: la global solo se llena al abrir el
  // recibo o la entrega, y el botón vive en la línea colapsada del turno
  // abierto — donde nadie pasó por ninguna de las dos. Sin esto, reportar un
  // daño con el turno ya abierto no encontraba placa.
  FLOTA_PLACA = (FLOTA_COND && FLOTA_COND.placa) || FLOTA_PLACA;
  if (!FLOTA_PLACA) { alerta('Primero elegí el vehículo.', 'advertencia'); return; }
  flotaAbrirModal('Daños del vehículo', FLOTA_PLACA);
  document.getElementById('flota-recibo').innerHTML =
    '<div class="tabla-card">Cargando…</div>';
  await flotaRenderDanos(false);
}

// ═══════════════════════════════════════════════════════════════════════════
// INSPECCIÓN DIARIA — la pantalla del conductor
//
// **Objetivo: dos minutos.** No es una aspiración de diseño: es la condición
// para que exista el dato. Un formulario de cinco minutos a las 5 a.m. se
// contesta marcando todo óptimo sin leer (regla 11), y entonces el registro
// afirma que alguien miró un camión que nadie miró.
//
// Tres decisiones, y las tres se pagan si se "mejoran":
//
// · **Ningún control nace marcado** (regla 1). Ni un `checked`, ni un `<select>`
//   con la primera opción puesta. Un default optimista convierte el sistema en
//   una fábrica de evidencia falsa de seguridad, y esa evidencia se usa después
//   frente a una aseguradora. Lo que no se toca **no se manda**, y el servidor
//   lo escribe como `sin_dato`: la ausencia queda escrita como ausencia.
//
// · **El gesto va en pantalla, no en un manual.** «Revisar frenos» se contesta
//   de memoria; «pisar a fondo y sostener 5 segundos, ¿el pedal sigue
//   hundiéndose?» no. Es lo que hace que los dos minutos sean de mirar.
//
// · **El orden lo manda el servidor.** Bloqueantes fijos, el resto barajado por
//   la fecha. La pantalla no reordena ni agrupa: con orden fijo, a la tercera
//   semana el pulgar responde sin leer.
//
// El reloj arranca cuando la lista se pinta y se manda en `segundos_llenado`.
// No hay umbral —no hay una sola medición todavía (regla 13)— y por eso la
// pantalla tampoco avisa nada sobre él: se registra el hecho.
// ═══════════════════════════════════════════════════════════════════════════

let FLOTA_INSP = null;          // la lista del día, tal cual la mandó el servidor
let FLOTA_INSP_RESP = {};       // {item_id: 'optimo' | 'no_apto'} — arranca VACÍO
let FLOTA_INSP_NOTA = {};       // {item_id: texto} — solo de los `no_apto`
let FLOTA_INSP_INICIO = 0;      // el reloj de la regla 11

/** Abre la inspección de hoy del vehículo del turno. */
async function flotaCondInspeccion() {
  // Misma razón que en `flotaCondReportarDano`: el botón vive en la línea del
  // turno abierto, donde nadie pasó por el recibo que llena la global.
  FLOTA_PLACA = (FLOTA_COND && FLOTA_COND.placa) || FLOTA_PLACA;
  if (!FLOTA_PLACA) { alerta('Primero elegí el vehículo.', 'advertencia'); return; }
  flotaAbrirModal('Inspección de hoy', FLOTA_PLACA);
  const cont = document.getElementById('flota-recibo');
  cont.innerHTML = '<div class="tabla-card">Cargando la lista de hoy…</div>';

  try {
    FLOTA_INSP = await get('/flota/inspeccion/items/' + encodeURIComponent(FLOTA_PLACA));
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo cargar la inspección: ${esc(e.message)}</div>`;
    return;
  }
  // Estado nuevo en cada apertura. Heredar lo marcado del vehículo anterior
  // sería la misma clase de defecto que cruzó las fotos de dos camiones.
  FLOTA_INSP_RESP = {};
  FLOTA_INSP_NOTA = {};
  FLOTA_INSP_INICIO = Date.now();
  flotaCondInspeccionPintar();
}

/** Vuelca el formulario en el modal. */
function flotaCondInspeccionPintar() {
  document.getElementById('flota-recibo').innerHTML = flotaCondInspeccionHTML();
}

/** El formulario entero como texto — separado del pintado para poder EJECUTARLO
 * en un test y mirar lo que quedó, en vez de buscar substrings en el archivo.
 *
 * Un aserto de substring sobre el fuente pasa con la función desconectada. Ya
 * pasó en este repo con `app.js`, y se descubrió mutándolo.
 */
function flotaCondInspeccionHTML() {
  const d = FLOTA_INSP;
  const items = (d && d.items) || [];
  const ya = (d && d.ya_respondidas_hoy) || [];

  const contestados = items.filter(i => FLOTA_INSP_RESP[i.item_id]).length;
  const faltan = items.length - contestados;

  // Lo que ya se contestó hoy. No bloquea la segunda —dos turnos en un día son
  // reales— pero quien abre tiene que saber que ya hay una: sin esto, la
  // pantalla invita a repetir la inspección hasta que dé apto.
  const previas = ya.length ? `<div class="tabla-card">
    <div class="tabla-titulo">Hoy ya se inspeccionó ${esc(ya.length)} vez(ces)</div>
    <ul style="line-height:1.6;padding-left:18px">${ya.map(i => `
      <li><b>${esc(i.veredicto)}</b> · ${i.items_esperados - i.items_sin_dato} de
      ${esc(i.items_esperados)} contestados · ${esc(i.segundos_llenado)}s</li>`).join('')}</ul>
    <p style="font-size:12px;color:var(--tx2);margin:6px 0 0">Los daños que ya
    nacieron no se borran: una segunda inspección no los tapa.</p>
  </div>` : '';

  const filas = items.map(i => `
    <li id="insp-item-${esc(i.item_id)}" style="margin-bottom:14px;border-left:3px solid ${
      i.bloqueante ? 'var(--red)' : 'var(--bd)'};padding-left:10px">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="color:var(--tx2);font-size:13px">${esc(i.orden_mostrado)}.</span>
        <b style="font-size:15px">${esc(i.nombre)}</b>
        ${flotaChipCriticidad(i.criticidad)}
      </div>
      <div style="font-size:13px;color:var(--tx2);margin:2px 0 6px">${esc(i.gesto)}</div>
      <div id="insp-ctrl-${esc(i.item_id)}">${flotaCondControlesItem(i)}</div>
    </li>`).join('');

  return `${previas}
  <div class="tabla-card">
    <div class="tabla-titulo">${esc(items.length)} ítems · ${d ? d.bloqueantes : 0} bloqueantes</div>
    <p style="font-size:13px;color:var(--tx2);margin:0 0 4px">
      Los primeros ${d ? d.bloqueantes : 0} deciden si el camión sale hoy. El resto
      cambia de orden cada día a propósito: es para que se lean, no para que se
      recuerden.</p>
    <p id="insp-faltan" style="font-size:13px;color:var(--yellow);margin:0">
      Faltan ${faltan} de ${esc(items.length)}</p>
  </div>
  <div class="tabla-card">
    <ul style="list-style:none;padding:0;margin:0">${filas}</ul>
  </div>
  <div class="tabla-card">
    <label>Kilometraje ahora</label>
    <input type="number" id="insp-km" inputmode="numeric"
           style="width:100%;padding:6px;font-size:18px">
    <p style="font-size:12px;color:var(--tx2);margin:6px 0 0">
      Sin odómetro no se registra ningún evento de flota. Es lo que después
      permite cruzar esta inspección contra los mantenimientos.</p>
    <label>Observación (opcional)</label>
    <input id="insp-obs" style="width:100%;padding:6px"
           placeholder="Algo que no encaje en ningún ítem">
    <button class="btn-primary" style="margin-top:12px;width:100%"
            onclick="flotaCondGuardarInspeccion()"
            id="insp-guardar" data-placa="${FLOTA_PLACA}">Terminar inspección</button>
    <div id="insp-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
}

/** Los dos botones de un ítem, más la nota si se marcó una falla.
 *
 * **Dos y no tres**: no hay botón de «no sé». No responder ya es `sin_dato` —
 * el servidor escribe la fila igual— y un tercer botón sería un camino de un
 * toque para declarar «no sé» sobre veintiocho ítems, que es la regla 11 con
 * ayuda. Lo que no se toca queda contado y a la vista en «Faltan N».
 *
 * Ninguno nace marcado. `class="btn-flota ok"` aparece SOLO cuando ya se
 * contestó, y eso es lo que hace que la pantalla no afirme nada por su cuenta.
 */
function flotaCondControlesItem(item) {
  const r = FLOTA_INSP_RESP[item.item_id];
  const nota = r === 'no_apto' ? `
    <input id="insp-nota-${esc(item.item_id)}" style="width:100%;padding:6px;margin-top:6px"
           value="${esc(FLOTA_INSP_NOTA[item.item_id] || '')}"
           oninput="flotaCondNotaItem(${esc(item.item_id)}, this.value)"
           placeholder="¿Cuál, dónde, qué tan grande? (opcional)">
    <div style="font-size:12px;color:var(--tx2);margin-top:4px">
      Queda un daño abierto con ${esc(item.dias_de_plazo)} día(s) de plazo. Lo cierra
      quien lo repara, no vos.</div>` : '';
  return `<div style="display:flex;gap:8px">
      <button class="btn-flota ${r === 'optimo' ? 'ok' : ''}" style="flex:1"
              onclick="flotaCondMarcarItem(${esc(item.item_id)}, 'optimo')">Bien</button>
      <button class="btn-flota ${r === 'no_apto' ? 'ok' : ''}" style="flex:1"
              onclick="flotaCondMarcarItem(${esc(item.item_id)}, 'no_apto')">Mal</button>
    </div>${nota}`;
}

/** Marca un ítem y repinta SOLO ese ítem.
 *
 * Repintar la lista entera perdería el foco de la nota que se está escribiendo
 * y devolvería el scroll al principio en cada toque — con veintiocho ítems eso
 * son dos minutos convertidos en cinco.
 */
function flotaCondMarcarItem(itemId, respuesta) {
  FLOTA_INSP_RESP[itemId] = respuesta;
  if (respuesta !== 'no_apto') delete FLOTA_INSP_NOTA[itemId];
  const item = (FLOTA_INSP.items || []).find(i => i.item_id === itemId);
  const ctrl = document.getElementById('insp-ctrl-' + itemId);
  if (ctrl && item) ctrl.innerHTML = flotaCondControlesItem(item);
  const faltan = document.getElementById('insp-faltan');
  if (faltan) {
    const total = (FLOTA_INSP.items || []).length;
    const hechos = (FLOTA_INSP.items || [])
      .filter(i => FLOTA_INSP_RESP[i.item_id]).length;
    faltan.textContent = `Faltan ${total - hechos} de ${total}`;
  }
}

/** Guarda la nota de un ítem sin repintar: si repintara, se perdería el foco. */
function flotaCondNotaItem(itemId, texto) {
  FLOTA_INSP_NOTA[itemId] = texto;
}

/** Manda la inspección. Lo que no se contestó viaja como ausencia, no como dato.
 *
 * El aviso de huecos dice **qué significa** el resultado, no solo cuántos
 * faltan: `incompleta` no es «casi apto», es «no se sabe», y no habilita
 * despacho. Un «¿confirmás?» pelado enseña a apretar Aceptar.
 */
async function flotaCondGuardarInspeccion() {
  const err = document.getElementById('insp-error');
  err.textContent = '';
  const items = (FLOTA_INSP && FLOTA_INSP.items) || [];
  const km = parseInt(document.getElementById('insp-km').value, 10);
  if (!Number.isFinite(km) || km < 0) {
    err.textContent = 'El kilometraje es obligatorio: sin odómetro no se ' +
      'registra ningún evento de flota.';
    return;
  }
  const placa = flotaPlacaDelFormulario('insp-guardar', 'insp-error');
  if (!placa) return;

  const faltan = items.filter(i => !FLOTA_INSP_RESP[i.item_id]);
  const bloqueantesEnBlanco = faltan.filter(i => i.bloqueante).length;
  if (faltan.length && !confirm(
      `Quedan ${faltan.length} ítem(s) sin contestar` +
      (bloqueantesEnBlanco ? `, ${bloqueantesEnBlanco} de ellos bloqueantes` : '') +
      `.\n\nLa inspección queda INCOMPLETA: no dice que el vehículo esté bien, ` +
      `dice que no se sabe — y no habilita despacho. ¿Mandarla así?`)) return;

  const payload = {
    placa: placa,
    km: km,
    // El reloj de la regla 11. Se manda medido, no estimado: es el único dato
    // que distingue mirar de marcar.
    segundos_llenado: Math.max(0, Math.round((Date.now() - FLOTA_INSP_INICIO) / 1000)),
    observacion: document.getElementById('insp-obs').value,
    respuestas: items
      .filter(i => FLOTA_INSP_RESP[i.item_id])
      .map(i => ({
        item_id: i.item_id,
        respuesta: FLOTA_INSP_RESP[i.item_id],
        nota: FLOTA_INSP_NOTA[i.item_id] || null,
      })),
  };

  const listo = flotaBotonOcupado('insp-guardar', 'Registrando…');
  try {
    const r = await fetch(API + '/flota/inspeccion', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo registrar'; return; }
    // El veredicto se dice tal cual, sin traducirlo a «listo ✓». Que el camión
    // no esté habilitado es la información, no un error de la pantalla.
    alerta(`Inspección registrada · ${d.veredicto}` +
           (d.hallazgos.length ? ` · ${d.hallazgos.length} daño(s) abiertos` : '') +
           (d.habilita_despacho ? '' : ' · NO habilita despacho'),
           d.habilita_despacho ? 'exito' : 'advertencia');
    // No se cierra el modal: se vuelve a abrir la lista, que ahora trae
    // `ya_respondidas_hoy` con el veredicto. Cerrar dejaría al conductor sin
    // ver qué quedó registrado, y `flotaCerrarModal` además preguntaría por las
    // fotos del recibo —ya guardadas— como si estuviera por perderlas.
    await flotaCondInspeccion();
    flotaCondCargar();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

// ══════════════════════════════════════════════════════════════════════
// LA PLATA QUE SALE — gastos y tanqueos (2026-09-02)
//
// `flota/dominio/costos.py` tenía 415 líneas de cálculo, su canon y **cero
// callers**. Esta pantalla es el gesto que lo enciende: sin ella, el costo por
// kilómetro es una función correcta que nadie llama, que es el patrón que este
// módulo lleva pagando toda la semana.
//
// Dos endpoints y **las dos URL escritas enteras**, nunca armadas con `+ verbo`
// ni con la categoría concatenada: el trinquete de rutas huérfanas mide
// adyacencia sobre el texto del PWA, y una URL que solo existe en tiempo de
// ejecución no se puede auditar leyendo el repo. Ya destapó exactamente esto
// con `/aplazar` en la tanda del hallazgo — ver `FLOTA_HALLAZGO_URL`.
// ══════════════════════════════════════════════════════════════════════

/** Las dos puertas de la plata. Enteras, literales, grepeables. */
const FLOTA_GASTO_URL = '/flota/gastos';
const FLOTA_TANQUEO_URL = '/flota/tanqueos';

/** Los vocabularios que el servidor publica, y que la pantalla NO copia.
 *
 * Si el JS llevara su propia lista de categorías periodificables, el día que se
 * agregue una el formulario no pediría el período y el gasto entero caería
 * sobre un día — sin error, sin aviso y con cara de número medido. Regla 0 con
 * consecuencia.
 */
let FLOTA_GASTO_META = { periodo: [], campo: [] };

/** Pesos con separador de miles. `1.400E+4` no es un precio que alguien lea. */
function flotaPesos(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return '$' + n.toLocaleString('es-CO', { maximumFractionDigits: 0 });
}

/** El expediente de plata de un vehículo. */
async function flotaAbrirGastos(placa) {
  FLOTA_PLACA = placa;
  flotaAbrirModal('Gastos del vehículo', placa);
  document.getElementById('flota-recibo').innerHTML =
    '<div class="tabla-card">Cargando…</div>';
  await flotaRenderGastos();
}

/** Una fila de gasto. El tanqueo muestra además su galonaje y su precio.
 *
 * `excede_capacidad` se pinta con sus TRES respuestas y `sin_dato` **no se
 * dibuja como si estuviera bien**: significa que la ficha no dice cuántos
 * galones caben y no hay contra qué revisar. Pintarlo igual que un tanqueo
 * normal es cómo un detector se apaga sin que nadie lo note.
 *
 * Y el aviso **no acusa a nadie** (regla 2 del módulo): dice que dos datos no
 * pueden ser los dos ciertos. Una capacidad mal levantada, un tanque auxiliar
 * que la ficha no conoce, dos vehículos en la misma factura y un dedo en el
 * teclado producen este mismo renglón, y todas se investigan igual de rápido.
 */
function flotaFilaGasto(g) {
  const tq = g.tanqueo;
  let extra = '';
  if (tq) {
    const aviso = tq.excede_capacidad === true
      ? `<div style="color:var(--red);font-size:12px;margin-top:2px">
           ${esc(tq.galones)} galones sobre un tanque declarado más chico —
           la capacidad de la ficha y este registro no pueden ser los dos
           ciertos. Hay que mirar cuál de los dos está mal.</div>`
      : (tq.excede_capacidad === 'sin_dato'
        ? `<div style="color:var(--tx2);font-size:12px;margin-top:2px">
             Sin capacidad de tanque en la ficha: este tanqueo <b>no se pudo
             revisar</b>. No es que esté bien.</div>`
        : '');
    extra = `<div style="font-size:12px;color:var(--tx2)">
        ${esc(tq.galones)} gal · ${flotaPesos(tq.precio_galon)}/gal · ${esc(tq.estacion)}
        · tanque <b>${esc(tq.tanque)}</b></div>${aviso}`;
  }
  const periodo = g.cubre_periodo
    ? `<div style="font-size:12px;color:var(--tx2)">cubre ${esc(g.periodo_desde)}
         → ${esc(g.periodo_hasta)} · se reparte por día, no cae entero en un mes</div>`
    : '';
  const doc = g.documento_numero
    ? `doc ${g.documento_numero}`
    : `<span style="color:var(--yellow)">sin documento — no se puede cruzar
         con la causación</span>`;
  return `<li style="margin-bottom:12px;border-left:2px solid var(--bd);padding-left:10px">
    <div><b>${esc(g.categoria)}</b> · ${flotaPesos(g.valor)} · ${esc(g.fecha)}</div>
    <div style="font-size:12px;color:var(--tx2)">${esc(g.proveedor)} · ${doc}
      · ${esc(g.km)} km · origen ${esc(g.origen_costo)}</div>
    ${periodo}${extra}
  </li>`;
}

/** Pinta el expediente: el CPK del mes, el rendimiento, la lista y el formulario.
 *
 * El CPK sale **con sus dos insumos**. Un número solo no se puede auditar, y
 * éste va a la pantalla de quien decide: tiene que poder rehacer la división.
 * `sin_dato` se dice con palabras y con el motivo — nunca como `$0`, que se
 * leería como un vehículo gratis.
 */
async function flotaRenderGastos() {
  const cont = document.getElementById('flota-recibo');
  let d;
  try {
    d = await get(FLOTA_GASTO_URL + '/' + encodeURIComponent(FLOTA_PLACA));
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudieron cargar los gastos: ${esc(e.message)}</div>`;
    return;
  }

  FLOTA_GASTO_META = {
    periodo: d.categorias_con_periodo || [],
    campo: d.categorias_de_campo || [],
  };

  // El motivo lo dice el SERVIDOR (`cpk_motivo`), no se re-deriva acá.
  //
  // Hasta el 2026-09-04 esta línea lo adivinaba con `d.km_recorridos > 0`, y
  // con eso solo distinguía dos de los cuatro casos — **y los confundía**: un
  // vehículo con dos lecturas dudosas y kilómetros recorridos tiene km > 0, así
  // que la pantalla decía «no hay ningún gasto registrado» sobre un vehículo
  // que sí tenía gastos. El texto era una afirmación falsa sobre contabilidad,
  // y mandaba a cargar una factura que ya estaba cargada.
  //
  // Una política, una función: `medicion._motivo_cpk` decide, los dos lectores
  // —este expediente y el tablero— muestran lo mismo.
  const cpk = d.cpk === 'sin_dato'
    ? `<b>sin dato</b> — ${esc(d.cpk_motivo || 'no se declaró el motivo.')}`
    : `<b>${flotaPesos(d.cpk)} por kilómetro</b>
       <span style="color:var(--tx2)">= ${flotaPesos(d.pesos_imputados)}
       ÷ ${d.km_recorridos.toLocaleString('es-CO')} km · odómetro ${esc(d.cpk_marca)}
       · ${esc(d.lecturas_en_ventana)} lectura(s) en la ventana</span>`;
  const rend = d.rendimiento_km_galon === 'sin_dato'
    ? '<span style="color:var(--tx2)">sin dato — hacen falta dos tanqueos con ' +
      '<b>tanque lleno</b>. Entre llenos, lo que entró al tanque es lo que se ' +
      'gastó; sobre un parcial, el número mide lo que quedaba adentro.</span>'
    : `<b>${esc(d.rendimiento_km_galon)} km/galón</b>`;

  const lista = (d.gastos || []).length
    ? `<ul style="list-style:none;padding:0">${d.gastos.map(g => flotaFilaGasto(g)).join('')}</ul>`
    : '<p style="color:var(--tx2)">Sin gastos registrados.</p>';

  const opciones = (arr) => arr.map(c => `<option value="${c}">${c}</option>`).join('');

  cont.innerHTML = `<div class="tabla-card">
    <div class="tabla-titulo">Costo por kilómetro · ${esc(d.desde)} a ${esc(d.hasta)}</div>
    <p style="margin:4px 0">${cpk}</p>
    <p style="margin:4px 0">Rendimiento: ${rend}</p>
    <p style="font-size:12px;color:var(--tx2);margin:6px 0 0">
      Es lo que se registró, no el costo de tener el camión: no incluye
      depreciación ni financiación. <b>No se compara con otro vehículo</b> —
      un NHR y un motocarro no cuestan igual y la diferencia no dice nada.</p>
  </div>
  <div class="tabla-card">${lista}</div>
  <div class="tabla-card">
    <div class="tabla-titulo">Registrar un gasto</div>
    <label>Qué fue</label>
    <select id="gs-cat" style="width:100%;padding:6px" onchange="flotaGastoCambioCategoria()">
      ${opciones(d.categorias)}
    </select>
    <label>Fecha del hecho</label>
    <input type="date" id="gs-fecha" style="width:100%;padding:6px">
    <label>Valor total (pesos)</label>
    <input type="number" id="gs-valor" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    <label>A quién se le pagó</label>
    <input id="gs-prov" style="width:100%;padding:6px" placeholder="Ej: Terpel Neiva">
    <label>De dónde salió la plata</label>
    <select id="gs-origen" style="width:100%;padding:6px">${opciones(d.origenes_costo)}</select>
    <label>Número de factura o documento <span style="color:var(--tx2)">(opcional)</span></label>
    <input id="gs-doc" style="width:100%;padding:6px" placeholder="Sin esto no se puede cruzar con Siesa">
    <label>Centro de operación <span style="color:var(--tx2)">(opcional)</span></label>
    <input id="gs-co" style="width:100%;padding:6px" placeholder="Ej: 003">
    <label>Descripción <span style="color:var(--tx2)">(obligatoria si es «otro»)</span></label>
    <input id="gs-desc" style="width:100%;padding:6px">

    <div id="gs-campo-km">
      <label>Kilometraje ahora</label>
      <input type="number" id="gs-km" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    </div>

    <div id="gs-campo-periodo" style="display:none">
      <label>Período que cubre — desde</label>
      <input type="date" id="gs-pdesde" style="width:100%;padding:6px">
      <label>Período que cubre — hasta</label>
      <input type="date" id="gs-phasta" style="width:100%;padding:6px">
      <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
        El SOAT no se carga al costo del día que se pagó: se reparte por día
        sobre el período que cubre.</p>
    </div>

    <div id="gs-campo-tanqueo" style="display:none">
      <label>Galones</label>
      <input type="number" step="0.001" id="gs-gal" inputmode="decimal" style="width:100%;padding:6px;font-size:18px">
      <label>Estación</label>
      <input id="gs-est" style="width:100%;padding:6px" placeholder="Ej: Terpel Av. 26">
      <label>Cómo quedó el tanque</label>
      <select id="gs-tanque" style="width:100%;padding:6px">
        <option value="" selected>— elegí una —</option>
        ${opciones(d.estados_tanque)}
      </select>
      <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
        <b>Ninguna viene marcada, a propósito.</b> El rendimiento solo se puede
        medir de tanque lleno a tanque lleno. Un «lleno» puesto sin mirar no da
        error: produce un número inventado que nadie va a poder desmentir.
        <b>«No sé» es una respuesta válida.</b></p>
      ${d.capacidad_tanque_galones
        ? `<p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
             Tanque declarado en la ficha: ${esc(d.capacidad_tanque_galones)} galones.</p>`
        : `<p style="font-size:12px;color:var(--yellow);margin:4px 0 0">
             La ficha no dice cuántos galones caben, así que nada va a poder
             revisar este registro contra la capacidad.</p>`}
    </div>

    <button class="btn-primary" style="margin-top:12px;width:100%"
            onclick="flotaGuardarGasto()"
            id="gs-guardar" data-placa="${FLOTA_PLACA}">Registrar</button>
    <div id="gs-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
  flotaGastoCambioCategoria();
}

/** Muestra los campos que la categoría elegida necesita, y esconde los otros.
 *
 * **Las listas vienen del servidor** (`categorias_con_periodo`,
 * `categorias_de_campo`), no escritas acá: si el JS llevara su propia copia, el
 * día que se agregue una categoría periodificable el formulario no pediría el
 * período y el gasto entero caería sobre un día — la regla 0 con consecuencia.
 */
function flotaGastoCambioCategoria() {
  const sel = document.getElementById('gs-cat');
  if (!sel) return;
  const cat = sel.value;
  const conPeriodo = (FLOTA_GASTO_META.periodo || []).includes(cat);
  const deCampo = (FLOTA_GASTO_META.campo || []).includes(cat);
  const esTanqueo = cat === 'combustible';
  const mostrar = (id, si) => {
    const e = document.getElementById(id);
    if (e) e.style.display = si ? 'block' : 'none';
  };
  mostrar('gs-campo-periodo', conPeriodo);
  mostrar('gs-campo-tanqueo', esTanqueo);
  // El kilometraje se pide solo para lo que ocurre con el vehículo delante. Un
  // SOAT se paga en una oficina: pedirle el odómetro a quien lo registra
  // produce un número inventado, y el servidor lo rechaza por eso mismo.
  mostrar('gs-campo-km', deCampo);
}

/** Envía el gasto por la puerta que le corresponde.
 *
 * Un tanqueo va por la puerta del tanqueo y **no** por la del gasto genérico:
 * por la otra entraría sin galones, sumaría al costo por kilómetro y no
 * aportaría un solo galón al rendimiento. El servidor también lo rechaza; esto
 * evita el viaje.
 *
 * **Este comentario no nombra la URL a propósito, y no es un detalle de
 * redacción.** El trinquete de rutas huérfanas mide adyacencia sobre el TEXTO
 * del PWA: con la URL escrita también acá, romper la constante —armándola por
 * concatenación, por ejemplo— dejaba el endpoint sin un solo consumidor real y
 * el guard seguía en verde, satisfecho por esta línea. Lo destapó la mutación
 * 30 del arnés del 2026-09-02, y es la octava vez en este repo que un detector
 * de texto se atrapa en su propio comentario. La URL vive en UN lugar:
 * `FLOTA_TANQUEO_URL`.
 */
async function flotaGuardarGasto() {
  const err = document.getElementById('gs-error');
  err.textContent = '';
  const cat = document.getElementById('gs-cat').value;
  const fecha = document.getElementById('gs-fecha').value;
  const valor = document.getElementById('gs-valor').value;
  const prov = document.getElementById('gs-prov').value.trim();
  if (!fecha) { err.textContent = 'Falta la fecha del hecho.'; return; }
  if (!valor || Number(valor) <= 0) {
    err.textContent = 'Un gasto de $0 no es un gasto barato: es una fila sin valor.';
    return;
  }
  if (!prov) { err.textContent = 'Falta a quién se le pagó.'; return; }

  const placa = flotaPlacaDelFormulario('gs-guardar', 'gs-error');
  if (!placa) return;

  const base = {
    placa: placa, fecha: fecha, valor: valor, proveedor: prov,
    origen_costo: document.getElementById('gs-origen').value,
    documento_numero: document.getElementById('gs-doc').value.trim(),
    centro_op: document.getElementById('gs-co').value.trim(),
    descripcion: document.getElementById('gs-desc').value.trim(),
  };
  const km = document.getElementById('gs-km').value;
  let url = FLOTA_GASTO_URL;
  let cuerpo = Object.assign({ categoria: cat }, base);

  if (cat === 'combustible') {
    const tanque = document.getElementById('gs-tanque').value;
    if (!tanque) {
      err.textContent = 'Falta cómo quedó el tanque. No hay opción por defecto: ' +
        'de ese campo depende que el rendimiento se pueda medir.';
      return;
    }
    url = FLOTA_TANQUEO_URL;
    cuerpo = Object.assign({
      galones: document.getElementById('gs-gal').value,
      tanque: tanque,
      estacion: document.getElementById('gs-est').value.trim(),
      km: km,
    }, base);
  } else if ((FLOTA_GASTO_META.campo || []).includes(cat)) {
    cuerpo.km = km;
  } else {
    cuerpo.periodo_desde = document.getElementById('gs-pdesde').value;
    cuerpo.periodo_hasta = document.getElementById('gs-phasta').value;
  }

  const listo = flotaBotonOcupado('gs-guardar', 'Registrando…');
  try {
    const r = await fetch(API + url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(cuerpo),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo registrar'; return; }
    // El exceso de capacidad se dice al registrar, no se esconde hasta que
    // alguien abra el tablero. Y se dice sin acusar a nadie.
    if (d.tanqueo && d.tanqueo.excede_capacidad === true) {
      alerta('Registrado. Ojo: entraron más galones de los que la ficha dice ' +
             'que caben — uno de los dos datos está mal.', 'advertencia');
    } else {
      alerta('Gasto registrado ✓ · ' + placa, 'exito');
    }
    await flotaRenderGastos();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

// ══════════════════════════════════════════════════════════════════════
// TALLER Y GARANTÍA — la visita, los trabajos, y lo que todavía cubre
// ══════════════════════════════════════════════════════════════════════
//
// El pedido del plan tiene dos mitades y la segunda es la que vale la plata:
//
//   1. La orden de trabajo **no lleva el costo**. El camión entra hoy y la
//      factura llega el 30. Por eso la pantalla tiene un gesto aparte para
//      registrarla, y mientras no llega el trabajo aparece marcado.
//   2. Al abrir una orden correctiva, se muestran las reparaciones del mismo
//      sistema del mismo vehículo que **todavía están en garantía** — antes de
//      mandar el camión. **No bloquea, propone.** El caso que evita es el que
//      se paga dos veces.
//
// Los juicios de vigencia vienen del SERVIDOR, que los pide al dominio. Acá
// solo se filtra por igualdad de sistema: filtrar una lista ya juzgada no es
// copiar una política; recalcular `vigente` en el navegador sí lo sería, y la
// copia de la pantalla es siempre la que diverge — la que la gente mira.
//
// **Las URL van escritas enteras, nunca armadas con `+ verbo`.** El trinquete
// de rutas huérfanas mide adyacencia sobre el texto del PWA, y una URL que solo
// existe en tiempo de ejecución no se puede auditar leyendo el repo. Ya destapó
// exactamente esto con `/aplazar` y con la de tanqueos — ver `FLOTA_HALLAZGO_URL`.

/** Las cinco puertas del taller. Enteras, literales, grepeables. */
const FLOTA_OT_URL = {
  listar:        (placa) => `/flota/ordenes/${placa}`,
  abrir:         '/flota/ordenes',
  intervenciones: (id) => `/flota/ordenes/${id}/intervenciones`,
  cerrar:        (id) => `/flota/ordenes/${id}/cerrar`,
  anular:        (id) => `/flota/ordenes/${id}/anular`,
  factura:       (id) => `/flota/ordenes/${id}/factura`,
};

/** Lo último que devolvió el servidor. **No es un caché**: es el material del
 * filtro por sistema, que corre sin volver a pedir nada mientras alguien llena
 * el formulario. Un segundo viaje a mitad del formulario es un renglón que a
 * veces no llega, y justo ése es el que evita pagar dos veces. */
let FLOTA_TALLER = { garantias: [], sistemas: [], km_actual: null };

/** El expediente de taller de un vehículo. */
async function flotaAbrirTaller(placa) {
  FLOTA_PLACA = placa;
  flotaAbrirModal('Taller y garantía', placa);
  document.getElementById('flota-recibo').innerHTML =
    '<div class="tabla-card">Cargando…</div>';
  await flotaRenderTaller();
}

/** Cómo se dice una vigencia que **no se pudo juzgar**.
 *
 * `sin_dato` no se pinta como si estuviera vencida ni como si cubriera: las dos
 * mentirían en direcciones opuestas y la primera es la cara —dejar de reclamar
 * una garantía que sí cubría—. Se dice qué falta para poder juzgarla, que es lo
 * único accionable.
 */
function flotaTextoVigencia(g) {
  const fecha = g.por_fecha === 'sin_dato'
    ? (g.hasta_fecha ? 'fecha sin juzgar' : 'sin plazo por fecha')
    : `${g.por_fecha ? 'vigente' : 'vencida'} hasta ${g.hasta_fecha}`;
  const km = g.por_km === 'sin_dato'
    ? (g.hasta_km === null
        ? 'sin plazo por kilómetros'
        : `hasta ${g.hasta_km} km · el vehículo no tiene odómetro registrado, así que esto no se pudo revisar`)
    : `${g.por_km ? 'vigente' : 'vencida'} hasta ${g.hasta_km} km (hoy ${g.km_actual})`;
  return `${fecha} · ${km}`;
}

/** Una garantía viva, con las DOS dimensiones a la vista.
 *
 * Las dos van escritas aunque una sola alcance para que cubra. Es la contracara
 * de que la búsqueda use «o» en vez de «y»: una garantía comercial real dice
 * «6 meses **o** 10.000 km, lo que ocurra primero», así que un renglón que solo
 * dijera «vigente» podría estar vigente por fecha y pasado de kilómetros. El
 * sistema propone y quien llama al taller decide, y para decidir necesita ver
 * las dos.
 */
function flotaFilaGarantia(g) {
  return `<li style="margin-bottom:8px">
    <b>${esc(g.sistema)}</b>${g.descripcion ? ' · ' + g.descripcion : ''}
    <div style="font-size:12px;color:var(--tx2)">${flotaTextoVigencia(g)}</div>
  </li>`;
}

/** Un trabajo dentro de su orden. Marca el que todavía no tiene factura. */
function flotaFilaTrabajo(t) {
  const factura = t.gasto_id === null
    ? `<span style="color:var(--yellow)">sin factura recibida</span>`
    : `factura registrada (gasto ${t.gasto_id})`;
  const gar = t.garantia_declarada === 'si'
    ? flotaTextoVigencia(t)
    : (t.garantia_declarada === 'no'
      ? 'la factura dice que no trae garantía'
      : 'no se preguntó si traía garantía — que no es lo mismo que no tenerla');
  return `<li style="margin-bottom:8px">
    <b>${esc(t.sistema)}</b>${t.descripcion ? ' · ' + t.descripcion : ''}
    <div style="font-size:12px;color:var(--tx2)">${gar}</div>
    <div style="font-size:12px;color:var(--tx2)">${factura}</div>
  </li>`;
}

/** Una orden con sus trabajos y los gestos que admite en su estado. */
function flotaFilaOrden(o) {
  const abierta = o.estado === 'abierta';
  const trabajos = (o.intervenciones || []).length
    ? `<ul style="list-style:none;padding:0;margin:6px 0">${
        o.intervenciones.map(t => flotaFilaTrabajo(t)).join('')}</ul>`
    : `<p style="font-size:12px;color:var(--tx2);margin:6px 0">
         Todavía no hay ningún trabajo registrado. Una orden no se puede cerrar
         así: cerrarla dejaría una visita al taller sin registro de qué se hizo.
         Si de verdad no se hizo nada, se anula con motivo escrito.</p>`;

  // El formulario de trabajos va SIEMPRE: registrar qué se hizo es registro y
  // control de flota lo conserva. Lo que se condiciona son los dos verbos que
  // cierran el ciclo de la visita.
  const acciones = abierta ? `
    ${flotaDecide() ? `<div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap">
      <button class="btn-flota" style="padding:4px 10px;font-size:12px"
              id="cerrar-ot-${esc(o.id)}"
              onclick="flotaCerrarOT(${esc(o.id)}, ${o.hallazgo_id !== null})">Volvió del taller</button>
      <button class="btn-flota" style="padding:4px 10px;font-size:12px"
              id="anular-ot-${esc(o.id)}"
              onclick="flotaAnularOT(${esc(o.id)})">Anular</button>
    </div>` : ''}
    ${flotaFormTrabajo(o.id)}` : '';

  const factura = (!abierta && o.sin_factura > 0)
    ? flotaFormFactura(o) : '';

  const desenlace = abierta ? '' :
    `<div style="font-size:12px;color:var(--tx2)">${esc(o.estado)} ·
       ${o.cerrada_ts ? horaColombia(o.cerrada_ts) : 'sin fecha'}
       ${o.motivo_cierre ? '· ' + o.motivo_cierre : ''}</div>`;

  return `<li style="margin-bottom:14px;border-left:2px solid var(--bd);padding-left:10px">
    <div><b>${esc(o.tipo)}</b> · ${esc(o.taller)} · ${esc(o.km)} km</div>
    <div style="font-size:13px">${esc(o.descripcion)}</div>
    <div style="font-size:12px;color:var(--tx2)">
      abierta ${horaColombia(o.abierta_ts)}
      ${o.hallazgo_id !== null ? ' · nació del daño ' + o.hallazgo_id : ''}</div>
    ${desenlace}${trabajos}${acciones}${factura}
  </li>`;
}

/** El formulario de registrar un trabajo, dentro de su orden.
 *
 * `garantia_hasta_fecha` y `garantia_hasta_km` **no son campos de este
 * formulario y no pueden serlo**: se calculan. Lo que se pide es lo que la
 * factura dice —cuántos meses, cuántos kilómetros— y el servidor devuelve 400
 * si alguien manda los derivados. Si se pudieran teclear, «vence en marzo»
 * sería una frase y no una garantía.
 */
function flotaFormTrabajo(id) {
  const ops = (FLOTA_TALLER.sistemas || [])
    .map(s => `<option value="${s}">${s}</option>`).join('');
  return `<div class="tabla-card" style="margin-top:8px">
    <div class="tabla-titulo">Registrar un trabajo de esta visita</div>
    <label>Qué parte se tocó</label>
    <select id="tr-${id}-sistema" style="width:100%;padding:6px">${ops}</select>
    <label>Qué se hizo <span style="color:var(--tx2)">(obligatorio si es «otro»)</span></label>
    <input id="tr-${id}-desc" style="width:100%;padding:6px" placeholder="Ej: cambio de kit de embrague">
    <label>¿La factura dice que trae garantía?</label>
    <select id="tr-${id}-gar" style="width:100%;padding:6px">
      <option value="" selected>— elegí una —</option>
      <option value="si">Sí, y dice hasta cuándo</option>
      <option value="no">No trae garantía</option>
      <option value="sin_dato">No sé / no dice nada</option>
    </select>
    <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
      <b>«No dice nada» no es «no trae».</b> Una factura que no lo menciona no
      es una factura sin garantía: es que no se preguntó, y las dos cosas se
      reclaman distinto.</p>
    <label>Meses de garantía <span style="color:var(--tx2)">(si la factura los dice)</span></label>
    <input type="number" id="tr-${id}-meses" inputmode="numeric" style="width:100%;padding:6px">
    <label>Kilómetros de garantía <span style="color:var(--tx2)">(si la factura los dice)</span></label>
    <input type="number" id="tr-${id}-km" inputmode="numeric" style="width:100%;padding:6px">
    <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
      La fecha y el kilometraje de vencimiento <b>no se escriben</b>: se calculan
      contra el día de hoy y el kilometraje con el que entró el camión.</p>
    <button class="btn-primary" style="margin-top:10px;width:100%"
            onclick="flotaRegistrarTrabajo(${id})"
            id="tr-${id}-guardar" data-placa="${FLOTA_PLACA}">Registrar trabajo</button>
    <div id="tr-${id}-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
}

/** El formulario de la factura que llegó después.
 *
 * Los trabajos van con casilla y **ninguna asumida**: una factura del taller
 * puede cubrir dos de los tres trabajos de la visita, y darlos todos por
 * cubiertos dejaría el tercero contado como facturado sin que nadie lo mirara.
 *
 * **No pide kilometraje**, y no es un olvido: la factura se digita treinta días
 * después, en una oficina, sin el vehículo delante. El gasto se ancla al
 * odómetro con el que el camión entró al taller, que es el kilometraje al que
 * el trabajo se hizo.
 */
function flotaFormFactura(o) {
  const pendientes = (o.intervenciones || []).filter(t => t.gasto_id === null);
  const casillas = pendientes.map(t => `
    <label style="display:block;font-weight:400">
      <input type="checkbox" checked id="fa-${esc(o.id)}-i-${esc(t.intervencion_id)}"
             data-interv="${esc(t.intervencion_id)}"> ${esc(t.sistema)}
      ${t.descripcion ? '· ' + t.descripcion : ''}
    </label>`).join('');
  const cats = (FLOTA_TALLER.categorias || [])
    .map(c => `<option value="${c}">${c}</option>`).join('');
  const orgs = (FLOTA_TALLER.origenes || [])
    .map(c => `<option value="${c}">${c}</option>`).join('');
  return `<div class="tabla-card" style="margin-top:8px">
    <div class="tabla-titulo">Llegó la factura de esta visita</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 6px">
      Qué trabajos cubre. Una factura puede cubrir unos y no otros.</p>
    ${casillas}
    <label>Qué clase de gasto es</label>
    <select id="fa-${esc(o.id)}-cat" style="width:100%;padding:6px">${cats}</select>
    <label>Fecha de la factura</label>
    <input type="date" id="fa-${esc(o.id)}-fecha" style="width:100%;padding:6px">
    <label>Valor total (pesos)</label>
    <input type="number" id="fa-${esc(o.id)}-valor" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    <label>A quién se le pagó</label>
    <input id="fa-${esc(o.id)}-prov" style="width:100%;padding:6px" placeholder="Ej: Taller Los Andes">
    <label>De dónde salió la plata</label>
    <select id="fa-${esc(o.id)}-origen" style="width:100%;padding:6px">${orgs}</select>
    <label>Número de factura <span style="color:var(--tx2)">(opcional)</span></label>
    <input id="fa-${esc(o.id)}-doc" style="width:100%;padding:6px" placeholder="Sin esto no se puede cruzar con Siesa">
    <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
      El kilometraje no se pide: el gasto se ancla al odómetro con el que el
      camión entró al taller, que es cuando el trabajo se hizo.</p>
    <button class="btn-primary" style="margin-top:10px;width:100%"
            onclick="flotaGuardarFacturaOT(${esc(o.id)})"
            id="fa-${esc(o.id)}-guardar" data-placa="${FLOTA_PLACA}">Registrar factura</button>
    <div id="fa-${esc(o.id)}-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
}

/** Pinta el expediente completo: garantías vivas, órdenes y el formulario. */
async function flotaRenderTaller() {
  const cont = document.getElementById('flota-recibo');
  let d;
  try {
    d = await get(FLOTA_OT_URL.listar(encodeURIComponent(FLOTA_PLACA)));
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo cargar el taller: ${esc(e.message)}</div>`;
    return;
  }

  // `pendientes` es {orden_id: [intervencion_id, ...]} y se arma acá, del
  // mismo payload que pinta las casillas. Existe porque el navegador no puede
  // recorrer casillas que todavía no leyó: sin la lista, «qué trabajos cubre
  // esta factura» habría que deducirlo del DOM, y una casilla que no se
  // encuentra se lee como «no marcada» — o sea, un trabajo que queda sin
  // facturar en silencio.
  const pendientes = {};
  (d.ordenes || []).forEach(o => {
    pendientes[o.id] = (o.intervenciones || [])
      .filter(t => t.gasto_id === null).map(t => t.intervencion_id);
  });

  FLOTA_TALLER = {
    garantias: d.garantias_vigentes || [],
    sistemas: d.sistemas || [],
    categorias: d.categorias_de_taller || [],
    origenes: d.origenes_costo || [],
    km_actual: d.km_actual,
    pendientes: pendientes,
  };

  const ordenes = (d.ordenes || []).length
    ? `<ul style="list-style:none;padding:0">${
        d.ordenes.map(o => flotaFilaOrden(o)).join('')}</ul>`
    : '<p style="color:var(--tx2)">Este vehículo no ha entrado al taller.</p>';

  const ops = (d.sistemas || [])
    .map(s => `<option value="${s}">${s}</option>`).join('');
  const tipos = (d.tipos || [])
    .map(t => `<option value="${t}">${t}</option>`).join('');

  // Mandar el camión al taller compromete plata: es decisión, no registro.
  // Quien no decide ve por qué y a quién le toca, en vez de un formulario que
  // el backend le va a rechazar con 403 después de llenarlo entero.
  const formAbrir = !flotaDecide() ? `<div class="tabla-card">
    <div class="tabla-titulo">Abrir una orden de trabajo</div>
    <p style="font-size:13px;color:var(--tx2);margin:6px 0 0">
      <b>Mandar un camión al taller lo decide gestión.</b> Control de flota
      señala el daño y escala; la orden la abre quien decide sobre plata.</p>
    <p style="font-size:12px;color:var(--tx2);margin:6px 0 0">
      Lo que sí podés hacer acá: registrar los trabajos y la factura de las
      órdenes que ya existen, más abajo.</p>
  </div>` : `<div class="tabla-card">
    <div class="tabla-titulo">Abrir una orden de trabajo</div>
    <label>Por qué entra</label>
    <select id="ot-tipo" style="width:100%;padding:6px">${tipos}</select>
    <label>Qué vas a mandar a revisar</label>
    <select id="ot-sistema" style="width:100%;padding:6px"
            onchange="flotaTallerSistemaCambio()">${ops}</select>
    <div id="ot-garantias"></div>
    <label>A qué taller</label>
    <input id="ot-taller" style="width:100%;padding:6px" placeholder="Ej: Taller Los Andes — Neiva">
    <label>Qué se pidió hacer</label>
    <input id="ot-desc" style="width:100%;padding:6px" placeholder="Ej: revisar ruido al embragar">
    <label>Kilometraje con el que entra</label>
    <input type="number" id="ot-km" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    <p style="font-size:12px;color:var(--tx2);margin:6px 0 0">
      El kilometraje va con la orden para poder calcular hasta qué kilómetro
      cubre lo que se repare, y para auditar después si el mantenimiento era
      necesario.</p>
    <button class="btn-primary" style="margin-top:12px;width:100%"
            onclick="flotaAbrirOT()" id="ot-guardar"
            data-placa="${FLOTA_PLACA}">Abrir orden</button>
    <div id="ot-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;

  cont.innerHTML = `${formAbrir}
  <div class="tabla-card">
    <div class="tabla-titulo">Órdenes de este vehículo</div>
    ${ordenes}
  </div>`;
  flotaTallerSistemaCambio();
}

/** Muestra las garantías vivas del sistema elegido, ANTES de mandar el camión.
 *
 * **Esto es lo que hace que la fase valga la plata.** No bloquea nada: pinta un
 * renglón. Bloquear dejaría un camión roto en patio por un dato que puede estar
 * mal levantado, y la operación desmonta el sistema en 48 horas.
 *
 * El filtro es por igualdad de cadena sobre una lista que el servidor **ya
 * juzgó**. Recalcular la vigencia acá sería la segunda copia de esa política, y
 * la copia de la pantalla es la que diverge.
 */
function flotaTallerSistemaCambio() {
  const cont = document.getElementById('ot-garantias');
  const sel = document.getElementById('ot-sistema');
  if (!cont || !sel) return;
  const vivas = (FLOTA_TALLER.garantias || []).filter(g => g.sistema === sel.value);
  if (!vivas.length) { cont.innerHTML = ''; return; }
  cont.innerHTML = `<div style="border-left:3px solid var(--yellow);padding:8px;margin:8px 0">
    <b style="color:var(--yellow)">Este vehículo tiene ${esc(vivas.length)}
      reparación(es) de ${esc(sel.value)} todavía en garantía</b>
    <ul style="list-style:none;padding:0;margin:6px 0">${
      vivas.map(g => flotaFilaGarantia(g)).join('')}</ul>
    <div style="font-size:12px;color:var(--tx2)">
      No bloquea nada: puede ser otra cosa. Pero conviene llamar al taller antes
      de mandar el camión — el caso que esto evita es el que se paga dos veces.</div>
  </div>`;
}

async function flotaPostTaller(url, cuerpo, idBoton, idError, textoOcupado) {
  const err = document.getElementById(idError);
  if (err) err.textContent = '';
  const listo = flotaBotonOcupado(idBoton, textoOcupado);
  try {
    const r = await fetch(API + url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(cuerpo),
    });
    const d = await r.json();
    if (!r.ok) {
      if (err) err.textContent = d.error || 'No se pudo';
      else alerta(d.error || 'No se pudo', 'error');
      return false;
    }
    return true;
  } catch (e) {
    if (err) err.textContent = 'Sin conexión: ' + e.message;
    else alerta('Sin conexión: ' + e.message, 'error');
    return false;
  } finally {
    listo();
  }
}

/** Manda el camión al taller. */
async function flotaAbrirOT() {
  const err = document.getElementById('ot-error');
  err.textContent = '';
  const taller = document.getElementById('ot-taller').value.trim();
  const desc = document.getElementById('ot-desc').value.trim();
  const km = parseInt(document.getElementById('ot-km').value, 10);
  if (!taller) { err.textContent = 'Falta a qué taller va.'; return; }
  if (!desc) { err.textContent = 'Sin descripción, nadie va a poder revisar si lo que hicieron era lo que se pidió.'; return; }
  if (!Number.isFinite(km) || km < 0) { err.textContent = 'El kilometraje es obligatorio.'; return; }
  const placa = flotaPlacaDelFormulario('ot-guardar', 'ot-error');
  if (!placa) return;

  const ok = await flotaPostTaller(FLOTA_OT_URL.abrir, {
    placa: placa, tipo: document.getElementById('ot-tipo').value,
    taller: taller, descripcion: desc, km: km,
  }, 'ot-guardar', 'ot-error', 'Abriendo…');
  if (!ok) return;
  alerta('Orden abierta ✓ · ' + placa, 'exito');
  await flotaRenderTaller();
}

/** Registra qué se hizo. La garantía se calcula; acá solo se dice el plazo. */
async function flotaRegistrarTrabajo(id) {
  const err = document.getElementById(`tr-${id}-error`);
  err.textContent = '';
  const sistema = document.getElementById(`tr-${id}-sistema`).value;
  const gar = document.getElementById(`tr-${id}-gar`).value;
  if (!gar) {
    err.textContent = 'Falta decir si la factura trae garantía. No hay opción ' +
      'por defecto: «no dice nada» no es lo mismo que «no trae».';
    return;
  }
  const cuerpo = {
    sistema: sistema, garantia_declarada: gar,
    descripcion: document.getElementById(`tr-${id}-desc`).value.trim(),
  };
  const meses = document.getElementById(`tr-${id}-meses`).value;
  const kms = document.getElementById(`tr-${id}-km`).value;
  if (gar === 'si') {
    if (!meses && !kms) {
      err.textContent = 'Una garantía sin plazo no se puede reclamar, y es peor ' +
        'que ninguna: se ve verde y no cubre nada. Poné los meses, los ' +
        'kilómetros, o los dos.';
      return;
    }
    if (meses) cuerpo.garantia_meses = meses;
    if (kms) cuerpo.garantia_km = kms;
  }

  const ok = await flotaPostTaller(FLOTA_OT_URL.intervenciones(id), cuerpo,
    `tr-${id}-guardar`, `tr-${id}-error`, 'Registrando…');
  if (!ok) return;
  alerta('Trabajo registrado ✓', 'exito');
  await flotaRenderTaller();
}

/** El vehículo volvió. Pregunta si además cierra el daño que abrió la orden.
 *
 * La pregunta es explícita y la respuesta por defecto es **no cerrarlo**: un
 * camión puede volver del taller con el daño todavía abierto —faltó un
 * repuesto, se arregló otra cosa— y cerrarlo solo porque volvió inventaría una
 * reparación.
 */
async function flotaCerrarOT(id, tieneHallazgo) {
  const cerrarDano = tieneHallazgo
    ? confirm('¿El daño que abrió esta orden quedó reparado?\n\n' +
              'Aceptar lo cierra. Cancelar cierra la orden y deja el daño ' +
              'abierto — que es lo correcto si volvió sin arreglar.')
    : false;
  const ok = await flotaPostTaller(FLOTA_OT_URL.cerrar(id),
    { cerrar_hallazgo: cerrarDano }, `cerrar-ot-${id}`, null, 'Cerrando…');
  if (!ok) return;
  alerta('Orden cerrada ✓', 'exito');
  await flotaRenderTaller();
}

/** La visita no ocurrió. Exige motivo escrito y no toca el daño. */
async function flotaAnularOT(id) {
  const motivo = prompt('¿Por qué se anula? (obligatorio — el daño sigue abierto)');
  if (!motivo || !motivo.trim()) {
    alerta('Una anulación sin motivo escrito no se distingue de hacer ' +
           'desaparecer una visita incómoda.', 'advertencia');
    return;
  }
  const ok = await flotaPostTaller(FLOTA_OT_URL.anular(id),
    { motivo: motivo.trim() }, `anular-ot-${id}`, null, 'Anulando…');
  if (!ok) return;
  alerta('Orden anulada ✓', 'exito');
  await flotaRenderTaller();
}

/** Registra la factura que llegó después y la cuelga de los trabajos marcados. */
async function flotaGuardarFacturaOT(id) {
  const err = document.getElementById(`fa-${id}-error`);
  err.textContent = '';
  const marcadas = (FLOTA_TALLER.pendientes || {})[id] || [];
  const ids = marcadas.filter(x => {
    const c = document.getElementById(`fa-${id}-i-${x}`);
    return c && c.checked;
  });
  if (!ids.length) {
    err.textContent = 'Hay que marcar qué trabajos cubre la factura. Sin eso, ' +
      'el gasto quedaría escrito y los trabajos seguirían contando como sin ' +
      'factura recibida.';
    return;
  }
  const fecha = document.getElementById(`fa-${id}-fecha`).value;
  const valor = document.getElementById(`fa-${id}-valor`).value;
  const prov = document.getElementById(`fa-${id}-prov`).value.trim();
  if (!fecha) { err.textContent = 'Falta la fecha de la factura.'; return; }
  if (!valor || Number(valor) <= 0) {
    err.textContent = 'Un gasto de $0 no es un gasto barato: es una fila sin valor.';
    return;
  }
  if (!prov) { err.textContent = 'Falta a quién se le pagó.'; return; }

  const ok = await flotaPostTaller(FLOTA_OT_URL.factura(id), {
    intervenciones: ids,
    categoria: document.getElementById(`fa-${id}-cat`).value,
    fecha: fecha, valor: valor, proveedor: prov,
    origen_costo: document.getElementById(`fa-${id}-origen`).value,
    documento_numero: document.getElementById(`fa-${id}-doc`).value.trim(),
  }, `fa-${id}-guardar`, `fa-${id}-error`, 'Registrando…');
  if (!ok) return;
  alerta('Factura registrada ✓ · ' + flotaPesos(valor), 'exito');
  await flotaRenderTaller();
}

// ══════════════════════════════════════════════════════════════════════
// LLANTAS — la entidad que sobrevive al vehículo (fase 4, 2026-09-02)
//
// Tres URL y **las tres escritas enteras**, nunca armadas con `+ verbo` ni con
// la placa concatenada dentro de una plantilla que el guard no pueda leer: el
// trinquete de rutas huérfanas mide adyacencia sobre el TEXTO del PWA, y una
// URL que solo existe en tiempo de ejecución no se puede auditar leyendo el
// repo. Ya destapó exactamente esto con `/aplazar` en la tanda del hallazgo y
// con `/flota/tanqueos` en la de gastos — ver `FLOTA_HALLAZGO_URL`.
// ══════════════════════════════════════════════════════════════════════

/** Las tres puertas de la llanta. Enteras, literales, grepeables. */
const FLOTA_LLANTA_URL = '/flota/llantas';
const FLOTA_MONTAJE_URL = '/flota/montajes';
const FLOTA_DESMONTAJE_URL = (id) => `/flota/montajes/${id}/desmontar`;

/** El vocabulario que el servidor publica, y que la pantalla NO copia.
 *
 * Si el JS llevara su propia lista de motivos, el día que se agregue uno el
 * desplegable no lo ofrecería y esa causa de desmontaje no existiría nunca en
 * los datos — sin error y sin aviso. Regla 0 con consecuencia.
 */
let FLOTA_LLANTA_META = { motivos: [], vidas: 0 };

/** El expediente de llantas de un vehículo. */
async function flotaAbrirLlantas(placa) {
  FLOTA_PLACA = placa;
  flotaAbrirModal('Llantas del vehículo', placa);
  document.getElementById('flota-recibo').innerHTML =
    '<div class="tabla-card">Cargando…</div>';
  await flotaRenderLlantas();
}

/** Cómo se dice un kilometraje de llanta. **Tres respuestas, no una.**
 *
 * `vigente` NO se pinta como 0 km: la llanta sigue puesta y su vida no terminó.
 * Una llanta con «0 km» aparecería como la que menos dura del parque, que es
 * exactamente al revés. Y `sin_dato` tampoco es cero: es que los dos extremos
 * del tramo son kilometrajes que nadie puede respaldar todavía.
 */
function flotaKmLlanta(km, marca) {
  if (km === 'vigente') return '<b>montada</b> — su vida todavía no terminó';
  if (km === 'sin_dato') {
    return '<span style="color:var(--yellow)">sin dato</span> — los dos ' +
      'kilometrajes están en duda. Se resuelve en «Verificar kilometrajes».';
  }
  return `<b>${Number(km).toLocaleString('es-CO')} km</b>` +
    (marca === 'dudosa'
      ? ' <span style="color:var(--yellow)">(un extremo en duda)</span>' : '');
}

/** Una fila de montaje: qué llanta, en qué posición, desde cuándo y por qué salió. */
function flotaFilaMontaje(m) {
  const estado = m.vigente
    ? `<b style="color:var(--green,var(--tx2))">montada</b> desde ${esc(m.inicio.slice(0, 10))}`
    : `${esc(m.inicio.slice(0, 10))} → ${String(m.fin).slice(0, 10)} · salió por <b>${esc(m.motivo_desmontaje)}</b>`;
  // `desgaste_irregular` se marca porque es la única respuesta que NO habla de
  // la llanta: habla del eje. Una llanta que murió por desalineación y una que
  // cumplió su vida se ven iguales en la lista si esto no se dice.
  const aviso = m.motivo_desmontaje === 'desgaste_irregular'
    ? `<div style="color:var(--yellow);font-size:12px;margin-top:2px">
         Desgaste irregular: eso no lo explica la llanta. Conviene mirar
         alineación, presión y suspensión de ese eje antes de poner la
         siguiente en la misma posición.</div>`
    : '';
  const doc = m.gasto_id
    ? ` · factura registrada`
    : ` · <span style="color:var(--tx2)">sin gasto asociado (rotación)</span>`;
  return `<li style="margin-bottom:12px;border-left:2px solid var(--bd);padding-left:10px">
    <div><b>Posición ${esc(m.posicion)}</b> · llanta <b>${esc(m.codigo)}</b>
      <span style="font-size:12px;color:var(--tx2)">${esc(m.marca_llanta)} ${esc(m.medida)}</span></div>
    <div style="font-size:12px;color:var(--tx2)">${estado}${doc}</div>
    <div style="font-size:12px;color:var(--tx2)">
      ${m.km_inicio.toLocaleString('es-CO')} km${m.km_fin === null ? '' : ' → ' + m.km_fin.toLocaleString('es-CO') + ' km'}
      · ${flotaKmLlanta(m.km, m.km_marca)}</div>
    ${aviso}
    ${m.vigente ? `<button class="btn-flota" style="padding:2px 8px;font-size:12px;margin-top:4px"
        onclick="flotaAbrirDesmontaje(${esc(m.id)}, '${esc(m.codigo)}', ${esc(m.posicion)})">Desmontar</button>` : ''}
  </li>`;
}

/** Pinta el expediente: qué hay puesto, qué falta, qué se midió, y los formularios.
 *
 * `posiciones_libres` se dice **con el motivo correcto**: casi nunca significa
 * que el camión ande sin rueda, sino que la llanta está puesta y nadie la
 * registró. Y cuando el vehículo no tiene ficha sale `sin_dato`, no una lista
 * vacía: vacía se leería como «están todas cubiertas».
 */
async function flotaRenderLlantas() {
  const cont = document.getElementById('flota-recibo');
  let d;
  try {
    d = await get(FLOTA_LLANTA_URL + '/' + encodeURIComponent(FLOTA_PLACA));
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudieron cargar las llantas: ${esc(e.message)}</div>`;
    return;
  }

  FLOTA_LLANTA_META = {
    motivos: d.motivos_desmontaje || [],
    vidas: d.vidas_para_fijar_util || 0,
  };

  const libres = d.posiciones_libres;
  const bloqueLibres = libres === 'sin_dato'
    ? `<p style="color:var(--yellow)">Este vehículo <b>no tiene ficha técnica</b>,
       así que no se sabe cuántas posiciones de llanta tiene. No es que estén
       todas cubiertas: es que no hay contra qué revisarlo, y por eso tampoco se
       puede montar ninguna todavía.</p>`
    : (libres.length
      ? `<p style="color:var(--yellow)">Sin llanta registrada:
           <b>posición ${libres.join(', ')}</b>.<br>
           <span style="font-size:12px;color:var(--tx2)">Casi siempre quiere
           decir que la llanta está puesta y nadie la registró, no que el camión
           ande sin rueda.</span></p>`
      : `<p style="color:var(--tx2)">Las ${esc(d.posiciones_declaradas)} posiciones
           tienen su llanta registrada.</p>`);

  const vida = (d.km_por_posicion || []).length
    ? `<ul style="list-style:none;padding:0;font-size:13px">${d.km_por_posicion.map(p => `
        <li>Posición <b>${esc(p.posicion)}</b>: ${esc(p.n)} vida(s) medida(s)
          ${p.mediana_km === 'sin_dato'
            ? `<span style="color:var(--tx2)">— faltan ${esc(p.faltan)} para poder
                 decir cuánto dura. Con menos, una sola pinchada parte la
                 mediana a la mitad.</span>`
            : `— mediana <b>${Number(p.mediana_km).toLocaleString('es-CO')} km</b>`}
          <span style="color:var(--tx2)">(${p.km.map(k => k.toLocaleString('es-CO')).join(', ')})</span>
        </li>`).join('')}</ul>`
    : `<p style="color:var(--tx2)">Todavía no se desmontó una sola llanta de
         este vehículo, así que no hay ninguna vida medida. No es cero: es que
         no hay con qué medirla.</p>`;

  const montajes = (d.montajes || []).length
    ? `<ul style="list-style:none;padding:0">${d.montajes.map(m => flotaFilaMontaje(m)).join('')}</ul>`
    : '<p style="color:var(--tx2)">Sin montajes registrados.</p>';

  const disponibles = (d.disponibles || []).map(ll =>
    `<option value="${esc(ll.id)}">${esc(ll.codigo)} · ${esc(ll.medida)}${ll.ultimo_motivo ? ' · salió por ' + ll.ultimo_motivo : ''}</option>`
  ).join('');

  cont.innerHTML = `<div class="tabla-card">
    <div class="tabla-titulo">Llantas puestas</div>
    ${bloqueLibres}
  </div>
  <div class="tabla-card">
    <div class="tabla-titulo">Vida medida por posición</div>
    ${vida}
    <p style="font-size:12px;color:var(--tx2);margin:6px 0 0">
      Es un dato, no una alarma: no hay un kilometraje de cambio medido en esta
      flota y ninguno se inventa acá. <b>No se compara una posición con otra</b>
      — una direccional y una de tracción no duran lo mismo.</p>
  </div>
  <div class="tabla-card">${montajes}</div>
  <div class="tabla-card">
    <div class="tabla-titulo">Dar de alta una llanta</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 8px">Entra al
      inventario. <b>No la monta en ningún camión</b> — eso es el formulario de
      abajo.</p>
    <label>Código marcado en la llanta</label>
    <input id="ll-codigo" style="width:100%;padding:6px" placeholder="Lo que la distingue de las otras cinco iguales">
    <label>Medida</label>
    <input id="ll-medida" style="width:100%;padding:6px" placeholder="Ej: 215/75R17.5">
    <label>Marca <span style="color:var(--tx2)">(opcional)</span></label>
    <input id="ll-marca" style="width:100%;padding:6px">
    <button class="btn-flota" style="margin-top:10px;width:100%"
            onclick="flotaGuardarLlanta()" id="ll-alta">Dar de alta</button>
    <div id="ll-error" style="color:var(--red);margin-top:8px"></div>
  </div>
  <div class="tabla-card">
    <div class="tabla-titulo">Montar una llanta</div>
    <label>Cuál</label>
    <select id="mt-llanta" style="width:100%;padding:6px">
      <option value="" selected>— elegí una —</option>${disponibles}
    </select>
    <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">Solo aparecen las
      que hoy no están puestas en ningún vehículo. Si alguna salió por un corte
      de flanco, el motivo va al lado: todavía no hay baja de llanta, así que
      volver a montarla es una decisión de quien la ve.</p>
    <label>Posición</label>
    <input type="number" id="mt-pos" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    <label>Kilometraje ahora</label>
    <input type="number" id="mt-km" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    <label>Observación <span style="color:var(--tx2)">(opcional)</span></label>
    <input id="mt-obs" style="width:100%;padding:6px">
    <button class="btn-primary" style="margin-top:12px;width:100%"
            onclick="flotaMontarLlanta()"
            id="mt-guardar" data-placa="${FLOTA_PLACA}">Montar</button>
    <div id="mt-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
}

/** Da de alta la llanta y vuelve a pintar. */
async function flotaGuardarLlanta() {
  const err = document.getElementById('ll-error');
  err.textContent = '';
  const codigo = document.getElementById('ll-codigo').value.trim();
  const medida = document.getElementById('ll-medida').value.trim();
  if (!codigo) {
    err.textContent = 'Falta el código: es lo único que distingue esta llanta ' +
      'de las otras cinco iguales del mismo camión.';
    return;
  }
  if (!medida) { err.textContent = 'Falta la medida — se lee del flanco.'; return; }

  const listo = flotaBotonOcupado('ll-alta', 'Guardando…');
  try {
    const r = await fetch(API + FLOTA_LLANTA_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({
        codigo: codigo, medida: medida,
        marca: document.getElementById('ll-marca').value.trim(),
      }),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo dar de alta'; return; }
    alerta('Llanta ' + d.codigo + ' dada de alta ✓', 'exito');
    await flotaRenderLlantas();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

/** Monta la llanta elegida en la posición indicada. */
async function flotaMontarLlanta() {
  const err = document.getElementById('mt-error');
  err.textContent = '';
  const llanta = document.getElementById('mt-llanta').value;
  const pos = document.getElementById('mt-pos').value;
  const km = document.getElementById('mt-km').value;
  if (!llanta) { err.textContent = 'Elegí cuál llanta se está montando.'; return; }
  if (!pos) {
    err.textContent = 'Falta la posición. No hay una por defecto: una llanta ' +
      'registrada en la posición equivocada deja el kilometraje de esa rueda ' +
      'hablando de otra.';
    return;
  }
  if (!km) {
    err.textContent = 'Falta el kilometraje. Sin él no se puede saber después ' +
      'cuánto rodó esta llanta (regla 3).';
    return;
  }

  const placa = flotaPlacaDelFormulario('mt-guardar', 'mt-error');
  if (!placa) return;

  const listo = flotaBotonOcupado('mt-guardar', 'Montando…');
  try {
    const r = await fetch(API + FLOTA_MONTAJE_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({
        placa: placa, llanta_id: llanta, posicion: pos, km: km,
        observacion: document.getElementById('mt-obs').value.trim(),
      }),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo montar'; return; }
    alerta('Llanta ' + d.codigo + ' montada en la posición ' + d.posicion + ' ✓', 'exito');
    await flotaRenderLlantas();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

/** Pide el motivo del desmontaje. **Ninguna opción viene marcada, a propósito.**
 *
 * De este campo depende que el análisis exista: sin `desgaste_irregular`, una
 * llanta que murió por una desalineación y una que cumplió su vida quedan
 * indistinguibles en el histórico, y el eje que come flancos no aparece nunca.
 * «No sé» es una respuesta válida y hay que elegirla — lo que no puede pasar es
 * que la opción cómoda sea «desgaste normal».
 */
function flotaAbrirDesmontaje(montajeId, codigo, posicion) {
  const cont = document.getElementById('flota-recibo');
  const opciones = (FLOTA_LLANTA_META.motivos || [])
    .map(m => `<option value="${m}">${m}</option>`).join('');
  cont.innerHTML = `<div class="tabla-card">
    <div class="tabla-titulo">Desmontar la llanta ${codigo} (posición ${posicion})</div>
    <label>Kilometraje ahora</label>
    <input type="number" id="dm-km" inputmode="numeric" style="width:100%;padding:6px;font-size:18px">
    <label>Por qué sale</label>
    <select id="dm-motivo" style="width:100%;padding:6px">
      <option value="" selected>— elegí uno —</option>${opciones}
    </select>
    <p style="font-size:12px;color:var(--tx2);margin:4px 0 0">
      <b>Ninguna viene marcada, a propósito.</b> De este campo depende que se
      pueda ver un eje que come flancos: <b>desgaste irregular</b> no habla de la
      llanta, habla de la alineación. <b>Rotación</b> es una llanta que salió
      entera y vuelve. <b>«No sé» es una respuesta válida</b> — lo que no sirve
      es marcar «desgaste normal» sin mirar.</p>
    <label>Observación <span style="color:var(--tx2)">(opcional)</span></label>
    <input id="dm-obs" style="width:100%;padding:6px">
    <button class="btn-primary" style="margin-top:12px;width:100%"
            onclick="flotaDesmontarLlanta(${montajeId})" id="dm-guardar">Desmontar</button>
    <button class="btn-flota" style="margin-top:8px;width:100%"
            onclick="flotaRenderLlantas()">Volver</button>
    <div id="dm-error" style="color:var(--red);margin-top:8px"></div>
  </div>`;
}

/** Cierra el tramo. */
async function flotaDesmontarLlanta(montajeId) {
  const err = document.getElementById('dm-error');
  err.textContent = '';
  const km = document.getElementById('dm-km').value;
  const motivo = document.getElementById('dm-motivo').value;
  if (!km) { err.textContent = 'Falta el kilometraje de salida (regla 3).'; return; }
  if (!motivo) {
    err.textContent = 'Falta por qué sale. Sin motivo, una llanta que murió ' +
      'por una desalineación y una que cumplió su vida quedan iguales en el ' +
      'histórico. «No sé» está en la lista.';
    return;
  }

  const listo = flotaBotonOcupado('dm-guardar', 'Desmontando…');
  try {
    const r = await fetch(API + FLOTA_DESMONTAJE_URL(montajeId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({
        km: km, motivo: motivo,
        observacion: document.getElementById('dm-obs').value.trim(),
      }),
    });
    const d = await r.json();
    if (!r.ok) { err.textContent = d.error || 'No se pudo desmontar'; return; }
    if (d.motivo_desmontaje === 'desgaste_irregular') {
      alerta('Desmontada. Ojo: el desgaste irregular no lo explica la llanta — ' +
             'conviene mirar alineación y presión de ese eje.', 'advertencia');
    } else {
      alerta('Llanta desmontada ✓', 'exito');
    }
    await flotaRenderLlantas();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

// ══════════════════════════════════════════════════════════════════════
// PREVENTIVO — la correa que envejece mientras la ficha ya dice cuándo
// cambiarla (2026-09-02)
//
// `flota_ficha_tecnica.distribucion_km_cambio` está cargado en la base desde la
// tanda 1 y **nadie lo leía**. Esta pantalla es el gesto que lo enciende: sin
// ella, el plan preventivo es una tabla que se llena sola y que nadie abre — el
// patrón que este módulo lleva pagando toda la semana.
//
// Las cuatro URL van escritas ENTERAS, nunca armadas con `+ verbo` ni con el id
// concatenado en un fragmento. El trinquete de rutas huérfanas mide adyacencia
// sobre el texto del PWA y **desde el 2026-09-02 ignora los comentarios**: una
// URL que solo existe en tiempo de ejecución no se puede auditar leyendo el
// repo, y una que solo vive en un comentario tampoco.
// ══════════════════════════════════════════════════════════════════════

/** Lo último que el servidor dijo del plan del vehículo abierto. */
let FLOTA_PREV = { tareas: [], ritmo: null, dias_aviso: null };

/** Cómo se pinta cada estado. **`sin_dato` no se dibuja como si estuviera bien.**
 *
 * Los cinco estados tienen color propio y ninguno cae a verde por omisión: un
 * estado nuevo que alguien agregue al dominio y olvide acá sale con su palabra
 * cruda y sin color, que es visible. Un `|| 'ok'` lo pintaría en verde, que es
 * exactamente la evidencia falsa de seguridad que la regla 1 prohíbe.
 */
const FLOTA_PREV_COLOR = {
  vencida: 'red', por_vencer: 'yellow', al_dia: 'tx2',
  sin_linea_base: 'yellow', sin_intervalo: 'yellow',
};

/** Qué significa cada estado, en la palabra que usa quien decide.
 *
 * Va acá y no en el servidor porque es redacción de pantalla; lo que NO está
 * acá es la decisión de cuál estado le toca a cada tarea — esa la emite
 * `flota/dominio/preventivo.py::diagnosticar` y esta pantalla no la recalcula.
 */
const FLOTA_PREV_TEXTO = {
  vencida: 'VENCIDA — el odómetro ya pasó el kilometraje de cambio',
  por_vencer: 'llega pronto al cambio',
  al_dia: 'todavía no toca',
  sin_linea_base: 'nunca se registró una ejecución: no hay contra qué comparar',
  sin_intervalo: 'la ficha no dice cada cuántos kilómetros toca',
};

/** El expediente preventivo de un vehículo. */
async function flotaAbrirPreventivo(placa) {
  FLOTA_PLACA = placa;
  flotaAbrirModal('Mantenimiento preventivo', placa);
  document.getElementById('flota-recibo').innerHTML =
    '<div class="tabla-card">Cargando…</div>';
  await flotaRenderPreventivo();
}

/** Una tarea del plan, con su estado, sus números y **su procedencia**.
 *
 * La fuente se pinta SIEMPRE y en la misma línea que el intervalo. Es la
 * decisión 2 del plan: un intervalo de 60.000 km que salió de `estimado` y uno
 * que salió del manual del fabricante no valen lo mismo, y el que los mira
 * tiene que poder distinguirlos sin abrir la ficha. Sin esto, un número sin
 * procedencia se lee como si alguien lo hubiera verificado.
 */
function flotaFilaTarea(t) {
  const color = FLOTA_PREV_COLOR[t.estado];
  const texto = FLOTA_PREV_TEXTO[t.estado];
  const km = t.km_restante === 'sin_dato'
    ? ''
    : ` · faltan ${Number(t.km_restante).toLocaleString('es-CO')} km`;
  // «unos N días» y nunca «vence el día X»: es una proyección sobre el ritmo
  // medido, no una fecha. Y `sin_dato` se dice con palabras — no se calla, que
  // es lo que haría creer que el número no hacía falta.
  const dias = t.dias_estimados === 'sin_dato'
    ? (t.km_restante === 'sin_dato' ? ''
      : ' · sin ritmo medido, no se sabe en cuántos días')
    : ` · unos ${t.dias_estimados} día(s)`;
  const intervalo = t.intervalo_km === 'sin_dato'
    ? `<span style="color:var(--yellow)">sin intervalo declarado</span>`
    : `cada ${Number(t.intervalo_km).toLocaleString('es-CO')} km` +
      ` <span style="color:var(--${t.fuente_blanda ? 'yellow' : 'tx2'})">` +
      `(fuente: ${esc(t.fuente)}${t.fuente_blanda ? ' — no es documental' : ''})</span>`;
  const ultima = t.ultima_ejecucion_km === 'sin_dato'
    ? 'nunca registrada'
    : `última a ${Number(t.ultima_ejecucion_km).toLocaleString('es-CO')} km`;
  const manual = t.origen === 'manual'
    ? ' · intervalo escrito a mano: la siembra desde la ficha no lo pisa'
    : '';
  return `<li style="margin-bottom:12px;border-left:2px solid var(--bd);padding-left:10px">
    <div><b>${esc(t.nombre)}</b> — <span style="color:var(--${color})">${texto}</span></div>
    <div style="font-size:12px;color:var(--tx2)">${intervalo} · ${ultima}${km}${dias}${manual}</div>
    ${t.nota ? `<div style="font-size:12px;color:var(--tx2)">${esc(t.nota)}</div>` : ''}
    <div style="margin-top:6px">
      <button class="btn-flota" id="prev-hecho-${esc(t.plan_id)}"
              onclick="flotaRegistrarEjecucion(${esc(t.plan_id)})">Se hizo</button>
      <button class="btn-flota"
              onclick="flotaFijarIntervalo(${esc(t.plan_id)})">Fijar intervalo</button>
    </div>
  </li>`;
}

/** Pinta el plan: el ritmo medido, la lista de tareas y el botón de siembra. */
async function flotaRenderPreventivo() {
  const cont = document.getElementById('flota-recibo');
  let d;
  try {
    d = await get('/flota/preventivo/' + FLOTA_PLACA);
  } catch (e) {
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo cargar el plan: ${esc(e.message)}</div>`;
    return;
  }
  FLOTA_PREV = d;

  // El ritmo, con `n` y `dias` al lado. Un km/día suelto sobre dos lecturas de
  // un mismo día dice lo mismo que sobre cuarenta de tres meses, y son dos
  // números distintos.
  const r = d.ritmo;
  const ritmo = r.km_dia === 'sin_dato'
    ? `<p style="color:var(--yellow);font-size:13px">Ritmo de uso: <b>sin dato</b>
       — ${esc(r.motivo)}. Sin él, «faltan 500 km» no se puede traducir a días.</p>`
    : `<p style="font-size:13px;color:var(--tx2)">Ritmo de uso: <b>${esc(r.km_dia)} km/día</b>
       (${esc(r.marca)}, ${esc(r.n)} lecturas sobre ${esc(r.dias)} días). Es lo que convierte
       los kilómetros que faltan en días. No se compara con otro vehículo.</p>`;

  const tareas = d.tareas || [];
  const lista = tareas.length
    ? `<ul style="line-height:1.4;padding-left:0;list-style:none">
         ${tareas.map(t => flotaFilaTarea(t)).join('')}</ul>`
    : `<p style="color:var(--yellow)">Este vehículo no tiene ninguna tarea de
       plan. Se siembran desde la ficha técnica: si la ficha no declara ni el
       kilometraje de distribución ni una especificación de aceite, no hay nada
       que proponer.</p>`;

  cont.innerHTML = `
    <div class="tabla-card">
      <h3>Mantenimiento preventivo · ${esc(d.placa)}</h3>
      ${ritmo}
      <p style="font-size:12px;color:var(--tx2)">«Llega pronto» significa dentro
      de <b>${esc(d.dias_aviso)} días</b> al ritmo medido de este vehículo. Es el único
      número elegido de esta pantalla: los kilómetros los pone el fabricante.</p>
      ${lista}
      <div style="margin-top:12px">
        <button class="btn-flota" id="prev-sembrar"
                onclick="flotaSembrarPlan()">Sembrar desde la ficha</button>
        <div style="font-size:12px;color:var(--tx2);margin-top:4px">
          Vuelve a leer la ficha y crea las tareas que falten. No pisa lo que
          alguien escribió a mano ni retira nada.</div>
      </div>
      <div id="prev-err" style="color:var(--red);margin-top:8px"></div>
    </div>`;
}

/** Relee la ficha y crea las tareas que falten. Idempotente. */
async function flotaSembrarPlan() {
  const err = document.getElementById('prev-err');
  err.textContent = '';
  const listo = flotaBotonOcupado('prev-sembrar', 'Sembrando…');
  try {
    const rp = await fetch(API + '/flota/preventivo/' + FLOTA_PLACA + '/sembrar', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
    });
    const d = await rp.json();
    if (!rp.ok) { err.textContent = d.error || 'No se pudo sembrar'; return; }
    const s = d.resumen;
    // Se dice qué hizo, incluso cuando no hizo nada. Un «listo ✓» sobre cero
    // filas creadas es indistinguible de una siembra que no corrió.
    alerta(`Plan al día · ${s.creadas} creada(s) · ${s.actualizadas} actualizada(s)` +
           (s.respetadas_manual ? ` · ${s.respetadas_manual} escritas a mano, sin tocar` : ''),
           'exito');
    await flotaRenderPreventivo();
    cargarFlota();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

/** Registra que la tarea se hizo. Pide el kilometraje: regla 3, sin default.
 *
 * **No ofrece «el último conocido»** ni lo precarga. Es el default peligroso de
 * este dominio: quien registra el cambio de aceite miró el tablero, o no
 * debería estar registrándolo.
 */
async function flotaRegistrarEjecucion(planId) {
  const err = document.getElementById('prev-err');
  err.textContent = '';
  const km = prompt('¿Qué kilometraje marca el tablero AHORA?\n\n' +
                    'Es el que establece desde dónde se cuenta el próximo cambio.');
  if (km === null) return;
  const n = parseInt(String(km).replace(/\D/g, ''), 10);
  if (!Number.isFinite(n)) { err.textContent = 'Kilometraje inválido'; return; }
  const taller = prompt('¿Quién lo hizo? (taller, opcional)') || '';

  const listo = flotaBotonOcupado('prev-hecho-' + planId, 'Registrando…');
  try {
    const rp = await fetch(API + '/flota/preventivo/tarea/' + planId + '/ejecucion', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ km: n, taller: taller.trim() }),
    });
    const d = await rp.json();
    if (!rp.ok) { err.textContent = d.error || 'No se pudo registrar'; return; }
    alerta('Ejecución registrada ✓ — el reloj de esa tarea arranca en ese kilometraje',
           'exito');
    await flotaRenderPreventivo();
    cargarFlota();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  } finally {
    listo();
  }
}

/** Escribe el intervalo que la ficha no trae, **con de dónde salió**.
 *
 * La procedencia se pide en el mismo gesto que el número y no después: un
 * formulario que la deje para «luego» produce intervalos sin fuente, y un
 * intervalo sin fuente se lee más tarde como si alguien lo hubiera verificado.
 */
async function flotaFijarIntervalo(planId) {
  const err = document.getElementById('prev-err');
  err.textContent = '';
  const km = prompt('¿Cada cuántos kilómetros toca esta tarea?\n\n' +
                    'Dejalo vacío para retirar el intervalo y volver a «sin dato».');
  if (km === null) return;
  const limpio = String(km).replace(/\D/g, '');
  const n = limpio === '' ? null : parseInt(limpio, 10);

  let fuente = 'sin_dato';
  if (n !== null) {
    fuente = prompt('¿De dónde salió ese número?\n\n' +
                    'manual_fabricante · concesionario · placa_motor · taller · estimado\n\n' +
                    'Un intervalo sin procedencia se lee después como si alguien ' +
                    'lo hubiera verificado.') || '';
    if (!fuente.trim()) { err.textContent = 'Sin procedencia no se guarda'; return; }
  }

  try {
    const rp = await fetch(API + '/flota/preventivo/tarea/' + planId, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify({ intervalo_km: n, fuente: fuente.trim() }),
    });
    const d = await rp.json();
    if (!rp.ok) { err.textContent = d.error || 'No se pudo guardar'; return; }
    alerta(n === null ? 'Intervalo retirado — la tarea vuelve a «sin intervalo»'
                      : 'Intervalo guardado con su procedencia ✓', 'exito');
    await flotaRenderPreventivo();
    cargarFlota();
  } catch (e) {
    err.textContent = 'Sin conexión: ' + e.message;
  }
}
