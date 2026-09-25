/* Bandeja de flota — la bandeja primero, el catálogo después.
 *
 * Pestaña Flota de control_flota y gestión: Hoy · Pendientes · Señales ·
 * Vehículos · Analítica. Las cuatro primeras salen de UN solo viaje,
 * `GET /flota/bandeja`, que arma el servidor (`flota/adaptadores/bandeja.py`).
 * Esta pantalla no calcula nada: pinta lo que el servidor ya decidió —el
 * semáforo con su porqué, los pendientes con su acción, las señales con su
 * evidencia— y traduce códigos a palabras donde el servidor no lo hizo.
 *
 * ## Lo que reemplaza
 *
 * Antes lo primero era «Salud de la flota»: 41 renglones de prosa sin una
 * placa ni un botón, y después seis tarjetas con nueve botones iguales. Para
 * saber qué camión atender había que abrir los seis expedientes. Ahora:
 *
 *   · Hoy: una fila por vehículo con su semáforo; la fila abre el expediente.
 *   · Pendientes: lo que alguien tiene que hacer, con placa y botón. Los daños
 *     de toda la flota son una sola cola; decidir es de gestión.
 *   · Señales: lo que no cuadra, con sus números. PROPONEN, no culpan (regla 2
 *     de flota/CLAUDE.md): la pantalla lo dice arriba, cada vez.
 *   · Vehículos: el catálogo de siempre, al final y no al principio.
 *
 * ## Reglas de esta pantalla
 *
 *   · Todo dato va con `esc()`. En un `onclick` solo viajan posiciones de
 *     arreglo — nunca una placa ni un texto: el navegador decodifica las
 *     entidades del atributo antes de correr el JS, y `esc` no protege ahí.
 *   · Un vehículo sin dato es ámbar y lo dice. Nunca verde por omisión.
 *   · `/flota/health` se pide una vez (`flotaHealth`), y solo si se abre
 *     Analítica o el Diagnóstico plegado.
 */

/** La última bandeja leída, y cuándo. Las pestañas se pintan de acá. */
let FLOTA_BANDEJA = null;
let FLOTA_BANDEJA_TS = 0;
let FLOTA_BANDEJA_SEQ = 0;

/** Una bandeja de hace menos de esto se repinta sin volver a pedirla. */
const FLOTA_BANDEJA_VIGENCIA_MS = 120000;

/** Las pestañas del expediente, en orden. `onclick` lleva la POSICIÓN. */
const FLOTA_EXP_PESTANAS = [
  ['resumen', 'Resumen'], ['danos', 'Daños'], ['gastos', 'Gastos'],
  ['taller', 'Taller'], ['llantas', 'Llantas'], ['preventivo', 'Preventivo'],
  ['documentos', 'Papeles'], ['ficha', 'Ficha'],
];

const FLOTA_SEMAFORO = {
  rojo:  { etiqueta: 'Atender hoy', tx: 'var(--err-tx)', bg: 'var(--err-bg)', brd: 'var(--err-brd)' },
  ambar: { etiqueta: 'Con pendientes', tx: 'var(--warn-tx)', bg: 'var(--warn-bg)', brd: 'var(--warn-brd)' },
  verde: { etiqueta: 'Sin pendientes conocidos', tx: 'var(--ok-tx)', bg: 'var(--ok-bg)', brd: 'var(--ok-brd)' },
};

const FLOTA_CLASE_SENAL = {
  km_sin_ruta: 'Kilómetros en días sin ruta',
  km_de_ruta: 'Una ruta más larga que lo habitual',
  galones: 'Más combustible del que el recorrido gasta',
  precio_galon: 'Galón más caro que el resto de la flota',
  turno_de_la_ruta: 'La ruta de hoy no es de quien tiene el turno',
};

/** Los códigos que viajan como VALOR en la evidencia de una señal. Antes se
 * pintaban tal cual («forma: salio_sin_turno», QA e2e 2026-09-24). */
const FLOTA_VALOR_EVIDENCIA = {
  turno_de_otro: 'el turno está a nombre de otro conductor',
  salio_sin_turno: 'la ruta salió sin el turno del conductor',
  conductor_con_otro_vehiculo: 'el conductor tiene abierto el turno de otro vehículo',
};

/** Un valor de la evidencia en palabras: el código conocido por su frase, el
 * desconocido sin guiones bajos (se ve, y alguien lo agrega arriba). */
function flotaValorEvidencia(v) {
  if (typeof v !== 'string') return v;
  if (Object.prototype.hasOwnProperty.call(FLOTA_VALOR_EVIDENCIA, v)) return FLOTA_VALOR_EVIDENCIA[v];
  return /^[a-z]+(_[a-z0-9]+)+$/.test(v) ? v.replace(/_/g, ' ') : v;
}

/** Una palabra para un código. Si no está, el código tal cual: se ve y se
 * agrega, en vez de esconderse detrás de un texto inventado. */
function flotaPalabraDe(mapa, codigo) {
  return Object.prototype.hasOwnProperty.call(mapa, codigo) ? mapa[codigo] : String(codigo);
}

/** El chip de color. El texto dice el color con palabras: el color solo no
 * alcanza (daltonismo, sol de frente, pantalla en gris). */
function flotaChipSemaforo(color) {
  const s = FLOTA_SEMAFORO[color] || FLOTA_SEMAFORO.ambar;
  return `<span class="badge" style="background:${esc(s.bg)};color:${esc(s.tx)};border:1px solid ${esc(s.brd)}">${esc(s.etiqueta)}</span>`;
}

function flotaChipUrgencia(urgencia) {
  return urgencia === 'rojo'
    ? '<span class="badge" style="background:var(--err-bg);color:var(--err-tx);border:1px solid var(--err-brd)">Hoy</span>'
    : '<span class="badge" style="background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd)">Con plazo</span>';
}

// ═══════════════════════════════════════════════════════════════════════════
// Carga
// ═══════════════════════════════════════════════════════════════════════════

/** Trae la bandeja (o reutiliza una reciente) y pinta la pestaña elegida.
 *
 * Una respuesta vieja no pisa a una nueva: si mientras llegaba se pidió otra,
 * se descarta. Un fallo de red se DICE en la pestaña — un panel vacío es
 * indistinguible de «no hay nada que hacer».
 */
async function flotaBandejaCargar(forzar) {
  const cont = document.getElementById('flota-contenido');
  if (!cont) return;
  const fresca = FLOTA_BANDEJA && (Date.now() - FLOTA_BANDEJA_TS) < FLOTA_BANDEJA_VIGENCIA_MS;
  if (!forzar && fresca) {
    flotaBandejaPintar();
    return;
  }
  if (!FLOTA_BANDEJA) cont.innerHTML = '<p style="color:var(--tx2)">Cargando la flota…</p>';
  const seq = ++FLOTA_BANDEJA_SEQ;
  let b;
  try {
    b = await get('/flota/bandeja');
  } catch (e) {
    if (seq !== FLOTA_BANDEJA_SEQ) return;
    cont.innerHTML = `<div class="tabla-card" style="color:var(--err-tx)">
      No se pudo leer la flota: ${esc(e.message)}</div>`;
    return;
  }
  if (seq !== FLOTA_BANDEJA_SEQ) return;
  FLOTA_BANDEJA = b;
  FLOTA_BANDEJA_TS = Date.now();
  flotaBandejaPintar();
}

/** Pinta la pestaña elegida de la bandeja ya leída. */
function flotaBandejaPintar() {
  const cont = document.getElementById('flota-contenido');
  const b = FLOTA_BANDEJA;
  if (!cont || !b) return;
  flotaBandejaContadores(b);
  const vista = (typeof FLOTA_SUBTAB === 'string') ? FLOTA_SUBTAB : 'hoy';
  let html;
  if (vista === 'pendientes') html = flotaBandejaPendientesHtml(b);
  else if (vista === 'senales') html = flotaBandejaSenalesHtml(b);
  else if (vista === 'vehiculos') html = flotaBandejaVehiculosHtml(b);
  else html = flotaBandejaHoyHtml(b);
  cont.innerHTML = html;
  if (typeof flotaAsegurarModal === 'function') flotaAsegurarModal();
}

/** Los números al lado del nombre de cada pestaña. Por `textContent`. */
function flotaBandejaContadores(b) {
  const pon = (id, n) => {
    const el = document.getElementById(id);
    if (el) el.textContent = n ? ` (${n})` : '';
  };
  pon('flota-n-pendientes', (b.pendientes || []).length);
  pon('flota-n-senales', (b.senales || []).length);
}

/** Lo que dice arriba de toda pestaña: de qué día es y cuándo se leyó. */
function flotaBandejaCabecera(b) {
  const filtro = b.filtro
    ? `<br>Filtrado por sede: ${esc(b.filtro.fuera_del_filtro)} vehículo(s) quedan afuera
       (${esc(b.filtro.sin_sede_conocida)} nunca pasaron por una sede).`
    : '';
  return `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px">
    <span style="font-size:var(--fs-xs);color:var(--tx2)">Día ${esc(b.dia_operativo)} ·
      leído ${esc(horaColombia(b.calculado_ts))}${filtro}</span>
    <button class="btn-flota" onclick="flotaBandejaCargar(true)">Actualizar</button>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Hoy
// ═══════════════════════════════════════════════════════════════════════════

function flotaBandejaHoyHtml(b) {
  const filas = b.hoy || [];
  if (!filas.length) {
    const dondeAlta = `Se dan de alta ${flotaDondeSeDaDeAlta()}.`;
    return flotaBandejaCabecera(b) + `<div class="tabla-card"><p>No hay vehículos activos
      ${b.filtro ? 'en esta sede' : ''}.</p>
      <p style="color:var(--tx2)">${dondeAlta} Sin vehículos no hay dónde cargar una
      ficha técnica ni dónde registrar un turno.</p></div>`;
  }
  const cuenta = { rojo: 0, ambar: 0, verde: 0 };
  filas.forEach(f => { if (cuenta[f.semaforo.color] !== undefined) cuenta[f.semaforo.color] += 1; });
  const resumen = `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
    ${flotaChipSemaforo('rojo')} <b>${esc(cuenta.rojo)}</b>
    ${flotaChipSemaforo('ambar')} <b>${esc(cuenta.ambar)}</b>
    ${flotaChipSemaforo('verde')} <b>${esc(cuenta.verde)}</b>
  </div>`;
  const orden = { rojo: 0, ambar: 1, verde: 2 };
  const lista = filas.slice().sort((x, y) =>
    (orden[x.semaforo.color] - orden[y.semaforo.color]) || (x.pos - y.pos));
  return flotaBandejaCabecera(b) + resumen +
    lista.map(f => flotaBandejaFilaHoy(f)).join('');
}

/** Una fila por vehículo. Toda la fila abre el expediente en su resumen. */
function flotaBandejaFilaHoy(f) {
  const s = FLOTA_SEMAFORO[f.semaforo.color] || FLOTA_SEMAFORO.ambar;
  const porque = (f.semaforo.porque || []).slice(0, 3).map(p =>
    `<li>${esc(p.texto)}</li>`).join('');
  const mas = (f.semaforo.porque || []).length > 3
    ? `<li style="color:var(--tx2)">y ${esc(f.semaforo.porque.length - 3)} más en el resumen</li>` : '';
  const quien = f.custodio ? f.custodio.texto : 'nadie tiene el turno registrado';
  const rutas = (f.rutas_hoy || []).length
    ? f.rutas_hoy.map(r => esc(r.texto)).join(' · ')
    : 'sin ruta hoy';
  return `<div class="tabla-card" style="border-left:4px solid ${esc(s.brd)};cursor:pointer"
       onclick="flotaExpediente(${Number(f.pos)}, 0)">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <span class="flota-placa">${esc(f.placa)}</span>
      ${flotaChipSemaforo(f.semaforo.color)}
      <span style="font-size:var(--fs-xs);color:var(--tx2)">${esc(f.tipo)}</span>
    </div>
    <div style="font-size:var(--fs-sm);color:var(--tx);margin-top:6px;line-height:1.5">
      ${esc(quien)} · ${esc(f.donde.texto)}<br>
      ${esc(f.km.texto)}<br>
      ${esc(f.inspeccion.texto)} · ${rutas}
    </div>
    <ul style="margin:6px 0 0 18px;padding:0;font-size:var(--fs-sm);color:${esc(s.tx)}">${porque}${mas}</ul>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Pendientes — y la cola de decisiones de daños de toda la flota
// ═══════════════════════════════════════════════════════════════════════════

/** Grupos en el orden en que se atienden. Cada pendiente cae en uno. */
const FLOTA_GRUPOS_PENDIENTES = [
  ['Daños por decidir', ['dano']],
  ['Papeles', ['documento']],
  // Los despachos que salieron reconociendo advertencias de flota (el FORZAR
  // del muelle): control de flota no ve 📈, así que acá es donde se entera.
  ['Salidas con advertencias', ['despacho_forzado']],
  ['Turnos: cierres forzados y fotos que faltan', ['cierre_forzado', 'turno_sin_fotos']],
  ['Kilometrajes por verificar', ['km_dudoso']],
  ['Ficha y mantenimiento', ['ficha', 'preventivo']],
];

function flotaBandejaPendientesHtml(b) {
  const todos = b.pendientes || [];
  if (!todos.length) {
    return flotaBandejaCabecera(b) + `<div class="tabla-card"><p>Nada pendiente con lo que
      está registrado.</p><p style="color:var(--tx2);font-size:var(--fs-sm)">No quiere decir
      que la flota esté bien: quiere decir que nada de lo cargado pide trabajo.</p></div>` +
      flotaBandejaHistorialPlegado();
  }
  let html = flotaBandejaCabecera(b);
  FLOTA_GRUPOS_PENDIENTES.forEach(([titulo, clases]) => {
    const filas = [];
    todos.forEach((p, j) => { if (clases.includes(p.clase)) filas.push([p, j]); });
    if (!filas.length) return;
    const aviso = (clases[0] === 'dano' && !b.puede_decidir)
      ? `<p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 8px">
           <b>Cerrar, aplazar o descartar un daño lo decide gestión.</b> Los días
           abiertos y los vencidos son señales con las que se mide a control de
           flota: el botón que los baja no puede ser suyo. Lo que sí: escalar el
           que venza.</p>`
      : '';
    const extra = (clases[0] === 'km_dudoso')
      ? '<button class="btn-primary" style="margin-bottom:8px" onclick="flotaBandejaVerificar()">Verificar kilometrajes</button>'
      : '';
    html += `<div class="tabla-card"><div class="tabla-titulo">${esc(titulo)} (${esc(filas.length)})</div>
      ${aviso}${extra}
      <ul style="list-style:none;padding:0;margin:0">
        ${filas.map(([p, j]) => flotaBandejaFilaPendiente(p, j, b.puede_decidir)).join('')}
      </ul></div>`;
  });
  return html + flotaBandejaHistorialPlegado();
}

/** Fuera de sede ahora y TODOS los cierres forzados, plegados y a pedido.
 *
 * La lista de arriba trae los forzados de la última semana, que es lo que
 * todavía se puede preguntar en persona. El historial entero es para cuando
 * alguien quiere ver si se volvió costumbre — y no se pide si nadie lo abre.
 */
function flotaBandejaHistorialPlegado() {
  return `<div class="tabla-card"><details>
    <summary style="cursor:pointer" onclick="flotaBandejaHistorialTurnos()"><b>Historial de turnos</b>
      <span style="font-size:var(--fs-xs);color:var(--tx2)">— fuera de sede ahora y todos los cierres forzados</span></summary>
    <div id="flota-historial-turnos" style="font-size:var(--fs-sm)">
      <p style="color:var(--tx2)">Tocá «Historial de turnos» para cargarlo.</p></div>
  </details></div>`;
}

async function flotaBandejaHistorialTurnos() {
  const cont = document.getElementById('flota-historial-turnos');
  if (!cont) return;
  cont.innerHTML = '<p style="color:var(--tx2)">Cargando…</p>';
  // En paralelo: son dos lecturas independientes.
  const [fuera, forzados] = await Promise.all([flotaBloqueFueraDeSede(), flotaBloqueForzados()]);
  cont.innerHTML = (fuera + forzados) ||
    '<p>Ningún vehículo fuera de sede ahora y ningún turno cerrado a la fuerza.</p>';
}

function flotaBandejaFilaPendiente(p, j, puedeDecidir) {
  let botones = '';
  if (p.accion && p.accion.tipo === 'decidir_dano') {
    botones = puedeDecidir
      ? `<button class="btn-flota" onclick="flotaBandejaDecidir(${Number(j)}, 0)">Reparado</button>
         <button class="btn-flota" onclick="flotaBandejaDecidir(${Number(j)}, 1)">Aplazar 7 días</button>
         <button class="btn-flota" onclick="flotaBandejaDecidir(${Number(j)}, 2)">No era nada</button>
         <button class="btn-flota" onclick="flotaPendienteAccion(${Number(j)})">Ver daños</button>`
      : `<button class="btn-flota" onclick="flotaPendienteAccion(${Number(j)})">Ver daños</button>`;
  } else if (p.accion && p.accion.tipo !== 'verificar_km') {
    botones = `<button class="btn-flota" onclick="flotaPendienteAccion(${Number(j)})">${esc(flotaTextoAccion(p.accion))}</button>`;
  }
  // Un pendiente con varios renglones (los papeles de un vehículo) se lee
  // como lista, no como una frase de cuatro cláusulas.
  const cuerpo = (p.lineas && p.lineas.length > 1)
    ? `<ul style="margin:4px 0 0 18px;padding:0">${p.lineas.map(l => `<li>${esc(l)}</li>`).join('')}</ul>`
    : esc(p.texto);
  return `<li style="border-top:1px solid var(--brd);padding:8px 0">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      ${flotaChipUrgencia(p.urgencia)} <b>${esc(p.placa)}</b> ${(p.lineas && p.lineas.length > 1) ? '' : cuerpo}
    </div>${(p.lineas && p.lineas.length > 1) ? cuerpo : ''}
    <div style="font-size:var(--fs-xs);color:var(--tx2);margin:2px 0 6px">${esc(p.detalle || '')}</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap">${botones}</div>
  </li>`;
}

function flotaTextoAccion(accion) {
  if (accion.tipo === 'fotos_turno') return 'Ver el turno';
  const textos = { documentos: 'Cargar papeles', ficha: 'Completar ficha',
                   preventivo: 'Ver preventivo', danos: 'Ver daños',
                   gastos: 'Ver gastos', resumen: 'Abrir expediente' };
  return flotaPalabraDe(textos, accion.pestana);
}

/** El índice del vehículo en `hoy` para una placa, o -1. */
function flotaPosDePlaca(placa) {
  const hoy = (FLOTA_BANDEJA && FLOTA_BANDEJA.hoy) || [];
  const f = hoy.find(x => x.placa === placa);
  return f ? f.pos : -1;
}

/** El botón de un pendiente: busca la fila por su posición y actúa. */
async function flotaPendienteAccion(j) {
  const p = FLOTA_BANDEJA && FLOTA_BANDEJA.pendientes[j];
  if (!p) return;
  const a = p.accion || {};
  if (a.tipo === 'verificar_km') { flotaBandejaVerificar(); return; }
  if (a.tipo === 'fotos_turno') {
    flotaExpOcultar();
    FLOTA_PLACA = p.placa;
    await flotaVerFotosDeCustodia(Number(a.custodia_id));
    return;
  }
  const pos = flotaPosDePlaca(p.placa);
  if (pos < 0) return;
  const k = FLOTA_EXP_PESTANAS.findIndex(([clave]) => clave === a.pestana);
  await flotaExpediente(pos, k < 0 ? 0 : k);
}

function flotaBandejaVerificar() {
  flotaExpOcultar();
  flotaAbrirVerificacion();
}

/** Las tres salidas de un daño desde la cola de toda la flota.
 *
 * Misma URL que el expediente (`FLOTA_HALLAZGO_URL`, escrita entera) y la
 * misma cortesía: descartar y aplazar no se mandan sin motivo. El control de
 * verdad es del servidor: `DECIDE_FLOTA` devuelve 403 a quien no decide, y
 * esta pantalla ni siquiera muestra los botones si `puede_decidir` es falso.
 */
async function flotaBandejaDecidir(j, k) {
  const p = FLOTA_BANDEJA && FLOTA_BANDEJA.pendientes[j];
  if (!p || !p.accion || p.accion.tipo !== 'decidir_dano') return;
  const verbo = ['cerrar', 'aplazar', 'descartar'][k];
  if (!verbo) return;
  let cuerpo;
  if (verbo === 'cerrar') {
    cuerpo = { nota: prompt('¿Qué se hizo? (opcional)') || '' };
  } else {
    const pregunta = verbo === 'aplazar'
      ? '¿Por qué se aplaza? (obligatorio — queda en la bitácora)'
      : '¿Por qué no era un daño? (obligatorio)';
    const motivo = prompt(pregunta);
    if (!motivo || !motivo.trim()) {
      alerta(verbo === 'aplazar'
        ? 'Un plazo que se mueve sin razón anotada es un plazo que no existe.'
        : 'Un descarte sin motivo escrito no se puede distinguir de hacer desaparecer un daño incómodo.',
        'advertencia');
      return;
    }
    cuerpo = { motivo: motivo.trim() };
  }
  try {
    const r = await fetch(API + FLOTA_HALLAZGO_URL[verbo](Number(p.accion.hallazgo_id)), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + TOKEN },
      body: JSON.stringify(cuerpo),
    });
    const d = await r.json();
    if (!r.ok) { alerta(flotaMensajeDeError(d), 'error'); return; }
    alerta('Listo ✓ · ' + p.placa, 'exito');
    await flotaBandejaCargar(true);
  } catch (e) {
    alerta('Sin conexión: ' + e.message, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Señales
// ═══════════════════════════════════════════════════════════════════════════

function flotaBandejaSenalesHtml(b) {
  const senales = b.senales || [];
  const noEval = b.senales_no_evaluables || [];
  let html = flotaBandejaCabecera(b) + `<div class="tabla-card" style="border-left:4px solid var(--info-brd)">
    <p style="margin:0;font-size:var(--fs-sm)"><b>Una señal propone dónde mirar; no dice
    que alguien hizo algo mal.</b> Casi todas tienen una explicación normal —un
    taller, un encargo, una factura mal digitada—, y con encontrarla basta. Nada
    de esto es una sanción ni se le muestra a nadie más.</p></div>`;
  if (!senales.length) {
    html += `<div class="tabla-card"><p>Ninguna señal en los últimos ${esc(b.umbrales ? b.umbrales.ventana_senales_dias : '30')} días.</p>
      <p style="color:var(--tx2);font-size:var(--fs-sm)">Mirá abajo lo que no se pudo revisar:
      sin eso, «ninguna señal» puede ser «nada que mirar».</p></div>`;
  } else {
    html += senales.map((s, k) => flotaBandejaTarjetaSenal(s, k)).join('');
  }
  html += flotaBandejaNoEvaluables(noEval);
  html += flotaBandejaUmbrales(b.umbrales || {});
  return html;
}

/** La evidencia de una señal, en palabras: cada clave con su etiqueta y su
 * formato. Una clave que no está acá NO se pinta: una clave cruda
 * («forma: turno_de_otro», «galones esperados: 10.857142857…») es ruido con
 * autoridad, y lo que dice ya está en el texto de la señal. `tipo` decide el
 * formato: hora de Bogotá, fecha, pesos, 2 decimales o entero con miles. */
const FLOTA_EVIDENCIA = {
  km_desde: ['Km al empezar', 'entero'], km_hasta: ['Km al terminar', 'entero'],
  km: ['Kilómetros', 'entero'], desde: ['Desde', 'hora'], hasta: ['Hasta', 'hora'],
  dias: ['Días sin ruta', 'fechas'],
  rutas_del_vehiculo_esos_dias: ['Rutas del vehículo esos días', 'entero'],
  dia: ['Día', 'fecha'], mediana_km: ['Lo habitual en esa ruta (mediana)', 'entero'],
  n: ['Casos comparados', 'entero'], ruta_maestra: ['Ruta maestra', 'texto'],
  galones: ['Galones que entraron', 'decimal'],
  galones_esperados: ['Galones esperados', 'decimal'],
  rendimiento_km_galon: ['Rendimiento medido (km por galón)', 'decimal'],
  ventanas_del_rendimiento: ['Ventanas del rendimiento', 'entero'],
  fecha: ['Fecha', 'fecha'], estacion: ['Estación', 'texto'],
  valor: ['Valor de la factura', 'pesos'], precio_galon: ['Precio del galón', 'pesos'],
  mediana_flota: ['Lo habitual en la flota (mediana)', 'pesos'],
  conductor_de_la_ruta: ['Conductor de la ruta', 'texto'],
};

function flotaEvidenciaValor(tipo, v) {
  if (v === null || v === undefined || v === '') return 'sin dato';
  const num = Number(v);
  if (tipo === 'entero') return Number.isFinite(num) ? Math.round(num).toLocaleString('es-CO') : String(v);
  if (tipo === 'decimal') {
    return Number.isFinite(num)
      ? num.toLocaleString('es-CO', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
      : String(v);
  }
  if (tipo === 'pesos') return Number.isFinite(num) ? fmtPesos(num) : String(v);
  if (tipo === 'fecha') return flotaFechaCorta(v);
  if (tipo === 'fechas') return (Array.isArray(v) ? v : [v]).map(flotaFechaCorta).join(', ');
  if (tipo === 'hora') {
    // Bogotá es UTC−5 todo el año (sin horario de verano): se resta a mano y
    // no se depende de la tabla de zonas del navegador (regla 5 del WMS).
    const d = new Date(v);
    if (isNaN(d)) return String(v);
    const b = new Date(d.getTime() - 5 * 3600 * 1000);
    const dos = (x) => String(x).padStart(2, '0');
    return `${dos(b.getUTCDate())}/${dos(b.getUTCMonth() + 1)} ${dos(b.getUTCHours())}:${dos(b.getUTCMinutes())}`;
  }
  return String(v);
}

function flotaBandejaTarjetaSenal(s, k) {
  const e = s.evidencia || {};
  const ev = Object.keys(FLOTA_EVIDENCIA).filter(clave => clave in e)
    .map(clave => {
      const [etiqueta, tipo] = FLOTA_EVIDENCIA[clave];
      return `<li><span style="color:var(--tx2)">${esc(etiqueta)}:</span>
      ${esc(flotaEvidenciaValor(tipo, e[clave]))}</li>`;
    }).join('');
  return `<div class="tabla-card" style="border-left:4px solid var(--warn-brd)">
    <div style="font-size:var(--fs-xs);color:var(--tx2)">${esc(flotaPalabraDe(FLOTA_CLASE_SENAL, s.clase))}</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0">
      <b>${esc(s.placa)}</b> <span>${esc(s.titulo)}</span>
    </div>
    <p style="margin:4px 0;font-size:var(--fs-sm)">${esc(s.texto)}</p>
    <p style="margin:4px 0;font-size:var(--fs-xs);color:var(--tx2)">Contexto: ${esc(s.contexto)}</p>
    <p style="margin:4px 0;font-size:var(--fs-sm)"><b>Qué mirar:</b> ${esc(s.propone)}</p>
    <details><summary style="cursor:pointer;font-size:var(--fs-xs);color:var(--tx2)">Evidencia</summary>
      <ul style="font-size:var(--fs-xs);margin:4px 0 0 18px;padding:0">${ev}</ul></details>
    <button class="btn-flota" style="margin-top:8px" onclick="flotaSenalAbrir(${Number(k)})">Abrir el caso</button>
  </div>`;
}

/** «Lo que no se pudo revisar», agrupado por qué y por qué no: la misma
 * frase cinco veces (una por placa) se deja de leer. Un renglón por
 * (señal, motivo), con las placas y los casos al lado. */
function flotaBandejaNoEvaluables(filas) {
  if (!filas.length) return '';
  const grupos = [];
  const idx = {};
  filas.forEach(f => {
    const clave = f.clase + '\u0000' + f.motivo;
    if (!(clave in idx)) {
      idx[clave] = grupos.length;
      grupos.push({ clase: f.clase, motivo: f.motivo, placas: [], casos: 0 });
    }
    const g = grupos[idx[clave]];
    const placa = f.placa || 'sin placa';
    if (!g.placas.includes(placa)) g.placas.push(placa);
    g.casos += f.casos || 1;
  });
  const li = grupos.map(g => `<li style="margin-bottom:6px">
    <b>${esc(flotaPalabraDe(FLOTA_CLASE_SENAL, g.clase))}</b>: ${esc(g.motivo)}
    <div style="font-size:var(--fs-xs);color:var(--tx2)">${esc(g.placas.join(', '))}${
      g.casos > g.placas.length ? ` · ${esc(g.casos)} casos` : ''}</div></li>`).join('');
  return `<div class="tabla-card"><details>
    <summary style="cursor:pointer"><b>Lo que no se pudo revisar (${esc(grupos.length)})</b></summary>
    <p style="font-size:var(--fs-xs);color:var(--tx2)">Sin el dato no hay señal, y eso no
    es lo mismo que «está bien». Cada renglón dice qué falta para poder mirarlo.</p>
    <ul style="font-size:var(--fs-sm);margin:0 0 0 18px;padding:0">${li}</ul>
  </details></div>`;
}

function flotaBandejaUmbrales(u) {
  const filas = [
    ['km_tolerancia_sin_ruta', 'km que un vehículo puede moverse en días sin ruta sin que se marque'],
    ['min_recorridos_ruta', 'recorridos medidos de una ruta antes de saber qué es lo normal'],
    ['factor_km_ruta', 'veces la mediana de su ruta para marcar un recorrido'],
    ['tolerancia_galones', 'veces los galones esperados para marcar un tanqueo'],
    ['min_tanqueos_precio', 'tanqueos de la flota para comparar el precio del galón'],
    ['factor_precio', 'veces la mediana del galón para marcar un precio'],
  ].map(([clave, texto]) => `<li>${esc(texto)}: <b>${esc(u[clave] === undefined ? 'sin dato' : u[clave])}</b></li>`).join('');
  return `<div class="tabla-card"><details>
    <summary style="cursor:pointer"><b>Con qué vara se juzga</b></summary>
    <p style="font-size:var(--fs-xs);color:var(--tx2)">Números elegidos sin una sola
    medición de esta flota, porque todavía no la hay. Se publican para poder
    fijarlos con dato cuando haya un mes de bandeja.</p>
    <ul style="font-size:var(--fs-sm);margin:0 0 0 18px;padding:0">${filas}</ul>
  </details></div>`;
}

/** «Abrir el caso»: el expediente del vehículo en la pestaña donde está la
 * evidencia. No crea nada ni marca a nadie: abre donde mirar. */
async function flotaSenalAbrir(k) {
  const s = FLOTA_BANDEJA && FLOTA_BANDEJA.senales[k];
  if (!s) return;
  const pos = flotaPosDePlaca(s.placa);
  if (pos < 0) return;
  const pestana = (s.caso && s.caso.pestana) || 'resumen';
  const i = FLOTA_EXP_PESTANAS.findIndex(([clave]) => clave === pestana);
  await flotaExpediente(pos, i < 0 ? 0 : i);
}

// ═══════════════════════════════════════════════════════════════════════════
// Vehículos — el catálogo, al final
// ═══════════════════════════════════════════════════════════════════════════

function flotaBandejaVehiculosHtml(b) {
  const alta = `El alta y la baja de vehículos se hacen ${flotaDondeSeDaDeAlta()}.`;
  const FICHA = { completa: 'ficha completa', incompleta: 'ficha incompleta', sin_ficha: 'sin ficha' };
  const filas = (b.hoy || []).map(f => `<tr style="cursor:pointer" onclick="flotaExpediente(${Number(f.pos)}, 0)">
      <td>${flotaChipSemaforo(f.semaforo.color)}</td>
      <td><b>${esc(f.placa)}</b></td>
      <td>${esc(f.tipo)}${f.capacidad_kg ? ' · ' + esc(f.capacidad_kg) + ' kg' : ''}</td>
      <td>${esc(flotaPalabraDe(FICHA, f.ficha))}</td>
      <td>${esc(f.danos_abiertos)} daño(s) abierto(s)</td>
    </tr>`).join('');
  return flotaBandejaCabecera(b) + `<div class="tabla-card">
    <div class="tabla-titulo">Vehículos activos (${esc((b.hoy || []).length)})</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">${alta}
      Tocá una fila para abrir su expediente.</p>
    <div style="overflow-x:auto"><table style="width:100%;font-size:var(--fs-sm)">${filas}</table></div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Expediente con pestañas — reutiliza los modales de siempre
// ═══════════════════════════════════════════════════════════════════════════

/** La barra de pestañas del expediente. Pura: la prueba el arnés de Node. */
function flotaExpBarraHtml(pos, activa) {
  return `<div style="display:flex;gap:4px;flex-wrap:wrap;padding:8px 16px 0">` +
    FLOTA_EXP_PESTANAS.map(([_c, nombre], k) => {
      const on = k === activa;
      return `<button class="btn-flota${on ? ' ok' : ''}" style="padding:4px 10px;font-size:var(--fs-xs)"
        onclick="flotaExpediente(${Number(pos)}, ${Number(k)})">${esc(nombre)}</button>`;
    }).join('') + '</div>';
}

/** Pone (o actualiza) la barra encima del contenido del modal.
 *
 * Vive como HERMANA de `#flota-recibo`, no adentro: cada pestaña reescribe
 * `#flota-recibo` entero cuando guarda algo, y una barra adentro desaparecería
 * al primer guardado.
 */
function flotaExpBarra(pos, activa) {
  const recibo = document.getElementById('flota-recibo');
  if (!recibo) return;
  let barra = document.getElementById('flota-exp-pestanas');
  if (!barra || !barra.parentNode) {
    barra = document.createElement('div');
    barra.id = 'flota-exp-pestanas';
    if (recibo.parentNode && recibo.parentNode.insertBefore) {
      recibo.parentNode.insertBefore(barra, recibo);
    }
  }
  barra.innerHTML = flotaExpBarraHtml(pos, activa);
  barra.style.display = 'block';
}

/** Esconde la barra cuando el modal se usa para otra cosa (verificación,
 * fotos de un turno): una barra con la placa de antes mentiría. */
function flotaExpOcultar() {
  const barra = document.getElementById('flota-exp-pestanas');
  if (barra) barra.style.display = 'none';
}

/** Abre el expediente del vehículo `pos` en la pestaña `k`. */
async function flotaExpediente(pos, k) {
  const f = FLOTA_BANDEJA && FLOTA_BANDEJA.hoy[pos];
  if (!f) return;
  const clave = (FLOTA_EXP_PESTANAS[k] || FLOTA_EXP_PESTANAS[0])[0];
  const placa = f.placa;
  if (clave === 'resumen') flotaExpResumen(pos);
  else if (clave === 'danos') await flotaAbrirDanos(placa);
  else if (clave === 'gastos') await flotaAbrirGastos(placa);
  else if (clave === 'taller') await flotaAbrirTaller(placa);
  else if (clave === 'llantas') await flotaAbrirLlantas(placa);
  else if (clave === 'preventivo') await flotaAbrirPreventivo(placa);
  else if (clave === 'documentos') await flotaAbrirDocumentos(placa);
  else if (clave === 'ficha') await flotaAbrirFicha(placa);
  flotaExpBarra(pos, FLOTA_EXP_PESTANAS.findIndex(([c]) => c === clave));
}

/** El resumen del vehículo: lo mismo que la fila de Hoy, entero, más sus
 * pendientes y señales, y los dos gestos del turno que no son pestaña. */
function flotaExpResumen(pos) {
  const b = FLOTA_BANDEJA;
  const f = b && b.hoy[pos];
  if (!f) return;
  FLOTA_PLACA = f.placa;
  flotaAbrirModal('Expediente', f.placa);
  document.getElementById('flota-recibo').innerHTML = flotaExpResumenHtml(b, pos);
}

function flotaExpResumenHtml(b, pos) {
  const f = b.hoy[pos];
  const s = FLOTA_SEMAFORO[f.semaforo.color] || FLOTA_SEMAFORO.ambar;
  const porque = (f.semaforo.porque || []).map(p => `<li>${esc(p.texto)}</li>`).join('');
  const pend = [];
  (b.pendientes || []).forEach((p, j) => {
    if (p.placa === f.placa) pend.push(flotaBandejaFilaPendiente(p, j, b.puede_decidir));
  });
  const sen = [];
  (b.senales || []).forEach((x, k) => {
    if (x.placa === f.placa) sen.push(`<li style="margin-bottom:6px">${esc(x.titulo)} —
      <span style="color:var(--tx2)">${esc(x.texto)}</span>
      <button class="btn-flota" style="padding:2px 8px;font-size:var(--fs-xs)"
              onclick="flotaSenalAbrir(${Number(k)})">Abrir</button></li>`);
  });
  const rutas = (f.rutas_hoy || []).length
    ? f.rutas_hoy.map(r => `<li>${esc(r.texto)}</li>`).join('') : '<li>sin ruta hoy</li>';
  const fotos = f.custodio
    ? `<button class="btn-flota" onclick="flotaExpFotosTurno(${Number(pos)})">Fotos del turno</button>` : '';
  return `<div class="tabla-card" style="border-left:4px solid ${esc(s.brd)}">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        ${flotaChipSemaforo(f.semaforo.color)}
        <span style="font-size:var(--fs-xs);color:var(--tx2)">${esc(f.tipo)}</span></div>
      <ul style="margin:8px 0 0 18px;padding:0;color:${esc(s.tx)}">${porque}</ul>
    </div>
    <div class="tabla-card" style="font-size:var(--fs-sm);line-height:1.6">
      <b>Quién lo tiene:</b> ${esc(f.custodio ? f.custodio.texto : 'nadie tiene el turno registrado')}<br>
      <b>Dónde:</b> ${esc(f.donde.texto)}<br>
      <b>Kilometraje:</b> ${esc(f.km.texto)}<br>
      <b>Inspección:</b> ${esc(f.inspeccion.texto)}<br>
      <b>Ruta de hoy:</b><ul style="margin:0 0 0 18px;padding:0">${rutas}</ul>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
        <button class="btn-flota" onclick="flotaExpRecibo(${Number(pos)})">Recibo de turno</button>
        <button class="btn-flota" onclick="flotaExpOdometro(${Number(pos)})">Registrar kilometraje</button>
        ${fotos}
      </div>
    </div>
    ${pend.length ? `<div class="tabla-card"><div class="tabla-titulo">Pendientes (${esc(pend.length)})</div>
      <ul style="list-style:none;padding:0;margin:0">${pend.join('')}</ul></div>` : ''}
    ${sen.length ? `<div class="tabla-card"><div class="tabla-titulo">Señales (${esc(sen.length)})</div>
      <ul style="margin:0 0 0 18px;padding:0;font-size:var(--fs-sm)">${sen.join('')}</ul></div>` : ''}`;
}

function flotaExpRecibo(pos) {
  const f = FLOTA_BANDEJA && FLOTA_BANDEJA.hoy[pos];
  if (!f) return;
  flotaExpOcultar();
  flotaAbrirRecibo(f.placa);
}

function flotaExpOdometro(pos) {
  const f = FLOTA_BANDEJA && FLOTA_BANDEJA.hoy[pos];
  if (!f) return;
  flotaExpOcultar();
  flotaAbrirOdometro(f.placa);
}

function flotaExpFotosTurno(pos) {
  const f = FLOTA_BANDEJA && FLOTA_BANDEJA.hoy[pos];
  if (!f || !f.custodio) return;
  flotaExpOcultar();
  FLOTA_PLACA = f.placa;
  flotaVerFotosDeCustodia(Number(f.custodio.custodia_id));
}

// ═══════════════════════════════════════════════════════════════════════════
// Diagnóstico técnico — plegado al final de Analítica, a pedido
// ═══════════════════════════════════════════════════════════════════════════
//
// Vivía al pie de Hoy (2026-09-24 se mudó): lo primero que ve el encargado es
// su trabajo del día, no los contadores de quien mantiene el sistema. El
// plegado lo pinta `flotaAnDiagnosticoPlegado()` (flota_analitica.js).

/** Los avisos de vencimiento, a pedido: son el único pedazo del diagnóstico
 * que necesita otra lectura (`/flota/avisos`). */
async function flotaBandejaDiagnostico() {
  const cont = document.getElementById('flota-diagnostico');
  if (!cont) return;
  cont.innerHTML = '<p style="color:var(--tx2)">Cargando…</p>';
  cont.innerHTML = (await flotaBloqueAvisos())
    || '<p style="color:var(--tx2)">Sin avisos de vencimiento registrados.</p>';
}

/** Un contador del health para leer: `null` es «sin dato», no cero. */
function flotaDiagNumero(x) {
  if (x === null || x === undefined) return 'sin dato (la tabla no existe)';
  if (Array.isArray(x)) return x.length === 1 ? '1 fila' : `${x.length.toLocaleString('es-CO')} filas`;
  return Number(x).toLocaleString('es-CO');
}

/** El salto de kilometraje más grande del mes: un HECHO, sin juzgarlo (no
 * hay todavía un mes de mediciones con qué fijar un umbral de km/día). */
function flotaDiagSalto(s) {
  if (s === null || s === undefined) return 'sin dato (la tabla no existe)';
  if (s.delta_km === null || s.delta_km === undefined) return s.nota || 'sin lecturas';
  const horas = (s.horas === null || s.horas === undefined) ? 'sin hora anterior'
    : `en ${Number(s.horas).toLocaleString('es-CO', { maximumFractionDigits: 2 })} h`;
  return `+${Number(s.delta_km).toLocaleString('es-CO')} km ${horas}`;
}

/** El tiempo de llenado de la inspección (regla 11: la forma de maximizarla
 * sin hacerla es marcar todo óptimo en veinte segundos). */
function flotaDiagLlenado(s) {
  if (s === null || s === undefined) return 'sin dato (la tabla no existe)';
  if (!s.minimo) return s.nota || 'ninguna inspección';
  // El veredicto de la más rápida, en palabras y en femenino («no apta»): el
  // panel de Analítica que lo pintaba se mudó acá (costuras 2026-09-24) y el
  // dato no se pierde con la mudanza.
  const ver = s.minimo.veredicto ? ` (${flotaPalabra('veredicto', s.minimo.veredicto)})` : '';
  return `mediana ${s.mediana} s · la más rápida ${s.minimo.segundos} s para `
    + `${s.minimo.items} ítems${ver} · ${s.n} inspecciones`;
}

/** Los números del health, en palabras y agrupados. Lo accionable de cada uno
 * está en Hoy o en Pendientes, con placa y botón; acá quedan para quien
 * mantiene el dato. Ningún campo del health queda mudo
 * (`test_render_salud_js::TestNingunCampoDelHealthQuedaMudo`). */
function flotaBandejaDiagnosticoHtml(h) {
  const n = flotaDiagNumero;
  const grupos = [
    ['De dónde salen', [
      ['Base', `${h.ambiente || 'sin declarar'}${h.datos_reales === false ? ' — estos números NO son de la operación real' : ''}`],
      ['Vehículos activos', n(h.vehiculos_activos)],
      ['Fichas técnicas completas', n(h.fichas_completas)],
      ['Conductores activos sin cuenta para entrar a la app', n(h.conductores_activos_sin_cuenta)],
      ['Rutas viejas sin placa (no se pueden cruzar con ningún vehículo)', n(h.rutas_historicas_sin_placa)],
    ]],
    ['Kilometraje', [
      ['Vehículos sin ninguna lectura', n(h.vehiculos_sin_lectura)],
      ['Lecturas sin foto del tablero', n(h.lecturas_sin_foto)],
      ['Lecturas en duda esperando que alguien las mire', n(h.lecturas_dudosas_pendientes)],
      ['Lecturas verificadas en 30 días', n(h.lecturas_verificadas_30d)],
      ['Correcciones de kilometraje en 30 días', n(h.lecturas_correccion_30d)],
      ['Salto de kilometraje más grande del mes', flotaDiagSalto(h.salto_km_maximo_30d)],
      ['Lecturas con el mismo segundo que otra', n(h.lecturas_ts_duplicado)],
      ['Fichas cuyo km inicial no cuadra con las lecturas', n(h.fichas_con_ancla_incoherente)],
    ]],
    ['Papeles y turnos (lo accionable está en Pendientes)', [
      ['Papeles vencidos', n(h.documentos_vencidos)],
      ['Papeles por vencer en 30 días', n(h.documentos_por_vencer_30d)],
      ['Papeles que nadie pudo mostrar', n(h.documentos_no_encontrados)],
      ['Papeles que piden trabajo', n(h.documentos_por_vehiculo)],
      ['Vehículos sin nadie que tenga el turno', n(h.vehiculos_sin_custodia_activa)],
      ['Turnos cerrados a la fuerza', n(h.custodias_cerradas_forzadas)],
      ['Turnos sin las fotos completas', n(h.custodias_sin_foto_completa)],
      ['Turnos que piden trabajo', n(h.custodias_por_vehiculo)],
      ['Turnos cerrados en una sede que no está en el maestro de almacenes', n(h.custodias_pendiente_sede)],
    ]],
    ['Daños e inspección (lo accionable está en Hoy y Pendientes)', [
      ['Daños abiertos', n(h.hallazgos_abiertos)],
      ['Vehículos sin inspección de hoy', n(h.vehiculos_sin_inspeccion_hoy)],
      ['Inspecciones de hoy incompletas', n(h.inspecciones_incompletas_hoy)],
      ['Tiempo de llenado de la inspección (30 días)', flotaDiagLlenado(h.segundos_llenado_30d)],
    ]],
    ['Combustible, gastos y fotos', [
      ['Tanqueos por encima de la capacidad del tanque', n(h.tanqueos_sobre_capacidad)],
      ['Tanqueos de vehículos sin capacidad de tanque en la ficha (el detector no los puede mirar)', n(h.tanqueos_sin_capacidad_declarada)],
      ['Gastos sin documento', n(h.gastos_sin_documento)],
      ['Fotos registradas cuyo archivo no se guardó', n(h.fotos_pendiente_evidencia)],
    ]],
  ];
  return grupos.map(([titulo, filas]) => `<p style="margin:10px 0 4px"><b>${esc(titulo)}</b></p>
    <ul style="margin:0 0 8px 18px;padding:0">${filas.map(([t, v]) =>
      `<li>${esc(t)}: <b>${esc(v)}</b></li>`).join('')}</ul>`).join('');
}
