/* Analítica de flota — despromediada.
 *
 * Sub-tab de Flota, no tab propio. `app.js:164` esconde todo `.nav-tab` cuyo
 * `onclick` no diga `tab-flota`, así que un `tab-analitica` sería invisible
 * justo para `control_flota`, que es el único rol que existe para leerlo.
 *
 * ## El criterio, escrito acá para que no se «corrija» en seis meses
 *
 * Con seis vehículos, **despromediar es enumerar, no estimar**:
 *
 *  1. Ninguna cifra sin su enumeración al lado. No hay un panel que muestre un
 *     solo número: cada panel es una lista de filas con placa.
 *  2. Prohibido percentil, desviación estándar y caja de bigotes. Un p90 de
 *     seis datos es el máximo con otro nombre; una sd de seis tiene ~32% de
 *     error relativo; y las bisagras de un boxplot de seis SON dos camiones
 *     concretos — mejor nombrarlos.
 *  3. La dispersión que significa algo es **dentro** de un vehículo, no entre
 *     vehículos. El canon prohíbe comparar el CPK de un NHR con el de un
 *     motocarro; comparar las cuatro posiciones de llanta del MISMO camión no
 *     tiene confundidores, porque es la misma ruta y el mismo conductor.
 *  4. Todo `sin_dato` lleva su causa, y la causa nombra el gesto que la
 *     arregla. «Sin CPK» no es información; «sin CPK porque nadie registró
 *     gastos» sí.
 *  5. Cada fila declara su base y su ventana (regla 13), no la página.
 *
 * ## Y la disciplina es la OPUESTA a la de Pendientes
 *
 * Pendientes (la bandeja) no muestra nada cuando no hay nada que hacer, y eso
 * está bien ahí: una lista de trabajo que siempre muestra algo se deja de
 * mirar — la lección de los 639 avisos.
 *
 * Acá es al revés, y hay que dejarlo escrito o alguien lo va a «arreglar»: el
 * trabajo entero de este tab es mostrar **lo que todavía no se puede medir**.
 * Un panel que desaparece por estar vacío es indistinguible de uno que nunca se
 * escribió. Por eso `flotaAnPanelVacio` es un render de primera clase.
 *
 * Sin canvas: el CDN de Chart.js es el que el service worker se niega a
 * cachear, su plugin de anotaciones nunca cargó, y un canvas no se puede
 * inspeccionar desde el arnés de Node — o sea, todo gráfico quedaría fuera del
 * único mecanismo que este repo tiene para verificar que algo se pintó.
 */

/** El sub-tab elegido sobrevive al F5.
 *
 * `control_flota` va a vivir en la bandeja; rebotarlo a otra pestaña en cada
 * recarga es cómo se deja de usar una pantalla.
 *
 * Desde el 2026-09-24 son seis: Hoy · Pendientes · Señales · 🕒 Jornada ·
 * Vehículos · Analítica. Hoy, Pendientes, Señales y Vehículos pintan la bandeja
 * (`flota_bandeja.js`) en `#flota-contenido`; Jornada pinta
 * `flotaJornadaCargar` (`flota_jornada.js`) en `#flota-jornada`; Analítica
 * pinta acá, en `#flota-analitica`. El valor viejo `operacion` —el que quedó
 * guardado en los teléfonos— se lee como Hoy.
 */
const FLOTA_SUBTABS = ['hoy', 'pendientes', 'senales', 'jornada', 'vehiculos', 'analitica'];

function flotaNormalizarSubtab(nombre) {
  if (nombre === 'operacion') return 'hoy';
  return FLOTA_SUBTABS.includes(nombre) ? nombre : 'hoy';
}

let FLOTA_SUBTAB = 'hoy';
try { FLOTA_SUBTAB = flotaNormalizarSubtab(localStorage.getItem('flota_subtab')); } catch (e) { /* modo privado */ }

/** Punto de entrada del tab. Despacha; no es una capa. */
async function flotaEntrar() {
  flotaSubtab(FLOTA_SUBTAB);
}

/** Enciende una pestaña y apaga las otras. Mismo patrón que `invSubtab`. */
function flotaSubtab(nombre) {
  FLOTA_SUBTAB = flotaNormalizarSubtab(nombre);
  try { localStorage.setItem('flota_subtab', FLOTA_SUBTAB); } catch (e) { /* modo privado */ }

  FLOTA_SUBTABS.forEach(k => {
    const activo = k === FLOTA_SUBTAB;
    const t = document.getElementById('flota-sub-' + k);
    if (t) {
      t.style.background = activo ? 'var(--pm-fill)' : 'transparent';
      t.style.color = activo ? '#fff' : 'var(--tx2)';
      t.style.fontWeight = activo ? '700' : '400';
    }
  });
  const analitica = FLOTA_SUBTAB === 'analitica';
  const jornada = FLOTA_SUBTAB === 'jornada';
  const pAn = document.getElementById('flota-analitica');
  const pJo = document.getElementById('flota-jornada');
  const pCo = document.getElementById('flota-contenido');
  if (pAn) pAn.style.display = analitica ? 'block' : 'none';
  if (pJo) pJo.style.display = jornada ? 'block' : 'none';
  if (pCo) pCo.style.display = (analitica || jornada) ? 'none' : 'block';

  if (analitica) return flotaCargarAnalitica();
  if (jornada) return flotaJornadaCargar('flota-jornada');
  // Sin forzar: cambiar de pestaña repinta la bandeja que ya se leyó, si es
  // reciente. `cargarFlota()` —la que llaman Rutas y las acciones— sí fuerza.
  return flotaBandejaCargar(false);
}

/** `/flota/health`, pedido UNA vez por carga y compartido.
 *
 * Lo leen Analítica y el Diagnóstico plegado de la bandeja. Antes lo pedían
 * dos bloques de la misma pantalla, uno detrás del otro. `refrescar` lo vuelve
 * a pedir (al entrar a Analítica); un fallo no queda guardado.
 */
let FLOTA_HEALTH_PROMESA = null;

function flotaHealth(refrescar) {
  if (refrescar || !FLOTA_HEALTH_PROMESA) {
    FLOTA_HEALTH_PROMESA = get('/flota/health');
    FLOTA_HEALTH_PROMESA.catch(() => { FLOTA_HEALTH_PROMESA = null; });
  }
  return FLOTA_HEALTH_PROMESA;
}

/** Un panel que todavía no tiene datos. **No es un `return ''`.**
 *
 * Dice tres cosas y las tres hacen falta: qué se va a medir, contra qué base, y
 * **el gesto exacto que lo enciende**. Sin la tercera, el panel informa de un
 * hueco y deja al que lo lee sin nada que hacer, que es cómo un tablero se
 * vuelve decoración.
 */
function flotaAnPanelVacio(titulo, base, gesto) {
  return `<div class="tabla-card">
    <div class="tabla-titulo">${titulo}</div>
    <p style="color:var(--tx2);margin:6px 0 0">
      <b>Esperando el primer registro.</b> ${base}</p>
    <p style="color:var(--tx2);font-size:var(--fs-xs);margin:6px 0 0">Se enciende: ${gesto}</p>
  </div>`;
}

/** Una barra apilada de segmentos `[etiqueta, cantidad, color]`. Sin canvas.
 *
 * Con total 0 devuelve una barra hueca y no una división por cero: un vehículo
 * sin lecturas tiene que ocupar su renglón, porque es el caso a atender.
 */
function flotaAnBarra(segmentos, total) {
  if (!total) return '<div style="height:10px;background:var(--bg2);border-radius:5px"></div>';
  const tramos = segmentos
    .filter(([, n]) => n > 0)
    .map(([, n, color]) => `<div style="width:${(n * 100 / total).toFixed(1)}%;background:${color}"></div>`)
    .join('');
  return `<div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--bg2)">${tramos}</div>`;
}

/** El encabezado: de qué mundo salen estos números y de qué día.
 *
 * Va arriba y no al final. El aviso de datos de prueba vivía como un renglón
 * más al fondo de una lista larga, que es donde no se lee.
 */
function flotaAnProcedencia(h) {
  const p = h.procedencia_del_tablero || {};
  const rojo = p.datos_reales === false;
  return `<div class="tabla-card" style="border-left:3px solid var(--${rojo ? 'yellow' : 'tx2'})">
    <div class="tabla-titulo">De dónde salen estos números</div>
    <p style="margin:4px 0;font-size:var(--fs-sm)">
      Ambiente: <b>${esc(p.ambiente || 'sin declarar')}</b> ·
      día operativo <b>${esc(p.dia_operativo || '—')}</b> (Bogotá) ·
      calculado ${(p.calculado_ts || '—').replace('T', ' ').slice(0, 19)}</p>
    ${rojo ? `<p style="color:var(--yellow);font-size:var(--fs-xs);margin:6px 0 0">
      <b>NO son de la operación real.</b> Sirven para probar la pantalla, no
      para decidir nada.</p>` : ''}
  </div>`;
}

/** El kilómetro de cada camión. **El único panel con datos hoy.**
 *
 * Va primero porque mientras esté en rojo, el CPK, el km/día y el km por llanta
 * salen `sin_dato` por diseño — y porque es la única métrica cuyo tablero es a
 * la vez el trabajo: mirarla y arreglarla son el mismo gesto.
 *
 * Compara cada vehículo **consigo mismo** y no contra los otros: un porcentaje
 * de flota sobre 26 lecturas escondería que las cuatro de un camión son todas
 * dudosas.
 */
function flotaAnLecturas(h) {
  const filas = h.lecturas_por_vehiculo;
  if (filas === null || filas === undefined) {
    return flotaAnPanelVacio('Calidad del kilómetro',
      'La tabla de lecturas de odómetro todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  if (!filas.length) {
    return flotaAnPanelVacio('Calidad del kilómetro',
      'No hay vehículos activos que medir.',
      `dar de alta un vehículo ${flotaDondeSeDaDeAlta()}.`);
  }
  const cuerpo = filas.map(f => {
    const c = f.por_confianza || {};
    const verif = c.verificada || 0, decl = c.declarada || 0, dud = c.dudosa || 0;
    const sinFoto = f.n - (f.con_foto || 0);
    return `<div style="margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;font-size:var(--fs-sm)">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--tx2)">${esc(f.n)} lectura(s)${f.n ? ` · ${esc(f.vigentes)} vigente(s)` : ''}</span>
      </div>
      ${flotaAnBarra([['verificada', verif, 'var(--green,#3fb950)'],
                      ['declarada', decl, 'var(--tx2)'],
                      ['dudosa', dud, 'var(--red)']], f.n)}
      <div style="font-size:var(--fs-xs);color:var(--tx2);margin-top:3px">
        ${f.motivo
          ? f.motivo
          : `${verif} verificada(s) · ${decl} declarada(s) · ${dud} dudosa(s) ·
             <b style="color:var(--${sinFoto ? 'red' : 'tx2'})">${sinFoto} sin foto</b>
             ${f.primera ? ` · desde ${esc(f.primera)}` : ''}`}
      </div>
      <div style="font-size:var(--fs-xs);color:var(--tx3,var(--tx2))">${esc(f.base)} · ${esc(f.etiqueta)}</div>
    </div>`;
  }).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Calidad del kilómetro, por vehículo</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">
      Sin un kilómetro que se pueda sostener no hay CPK, ni km por llanta, ni
      preventivo por kilometraje. <b>Una lectura sin foto no se puede
      verificar</b>, y una dudosa no divide nada hasta que alguien la mire.</p>
    ${cuerpo}
  </div>`;
}

/** Qué le falta a cada camión para poder medirse. **El índice del tab.**
 *
 * Una grilla de seis filas y no un «73% de cobertura»: con seis vehículos el
 * porcentaje es estrictamente MENOS información que la lista, porque no dice a
 * cuál llamar.
 *
 * `null` en una celda es «no se pudo mirar», y se pinta distinto de `false`. Un
 * parque sin ficha levantada no puede verse igual que uno impecable.
 */
function flotaAnCobertura(h) {
  const filas = h.cobertura_por_vehiculo;
  if (!filas || !filas.length) {
    return flotaAnPanelVacio('Lo que la ficha no dice',
      'No hay vehículos activos que revisar.',
      `dar de alta un vehículo ${flotaDondeSeDaDeAlta()}.`);
  }
  const cols = [
    ['ficha_completa', 'Ficha'],
    ['capacidad_tanque', 'Tanque'],
    ['posiciones_llanta', 'Posiciones'],
    ['documentos', 'Papeles'],
    ['lecturas', 'Odómetro'],
  ];
  const marca = (v) => v === null || v === undefined
    ? '<span style="color:var(--yellow)" title="no se pudo mirar">?</span>'
    : (v ? '<span style="color:var(--green,#3fb950)">✓</span>'
         : '<span style="color:var(--red)">✗</span>');
  const cabecera = cols.map(([, t]) =>
    `<th style="font-weight:400;color:var(--tx2);font-size:var(--fs-xs);padding:2px 6px">${t}</th>`).join('');
  const cuerpo = filas.map(f => `<tr>
      <td style="padding:2px 6px"><b>${esc(f.placa)}</b></td>
      ${cols.map(([k]) => `<td style="text-align:center;padding:2px 6px">${marca(f[k])}</td>`).join('')}
    </tr>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Lo que la ficha no dice</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">
      Cada ✗ apaga una medición aguas abajo: sin capacidad de tanque no hay
      detector de sobre-tanqueo, sin posiciones no hay vida de llanta.
      <b>«?» no es «no»</b>: es que la tabla no existe y no se pudo mirar.</p>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:var(--fs-sm)">
        <thead><tr><th style="text-align:left;padding:2px 6px"></th>${cabecera}</tr></thead>
        <tbody>${cuerpo}</tbody>
      </table>
    </div>
    <p style="font-size:var(--fs-xs);color:var(--tx3,var(--tx2));margin:8px 0 0">
      Base: ficha técnica, documentos y lecturas registradas por vehículo ·
      vehículos activos al día de hoy</p>
  </div>`;
}

/** Días de hallazgo abierto — el indicador que la ficha del especialista
 * promete desde el 2026-08-04 y que ninguna pantalla mostraba.
 *
 * Despromediado y en ese orden: **primero los casos con placa, después el
 * promedio con su n.** Con un puñado de hallazgos el promedio no dice a qué
 * camión llamar, y el canon ya prohíbe compararlo entre zonas — los tiempos de
 * taller y de repuesto son distintos en Neiva, Pitalito y Florencia.
 *
 * Los abiertos y los cerrados van SEPARADOS y no se promedian juntos: uno mide
 * riesgo terminado y el otro riesgo corriendo. El canon lo prohíbe en su punto
 * 6 y el dominio los devuelve en campos distintos (`dias` vs `dias_lleva`).
 */
function flotaAnHallazgos(h) {
  const d = h.dias_hallazgo_abierto;
  if (d === null || d === undefined) {
    return flotaAnPanelVacio('Días de hallazgo abierto',
      'La tabla de hallazgos todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  if (!d.casos.length) {
    return flotaAnPanelVacio('Días de hallazgo abierto',
      'Ningún daño reportado todavía. El indicador nace al cerrar el primero, ' +
      'con su odómetro y su evidencia.',
      'reportar un daño desde «Daños» en el expediente del vehículo.');
  }

  const abiertos = d.casos.filter(c => c.entra && c.estado === 'abierto');
  const cerrados = d.casos.filter(c => c.entra && c.estado !== 'abierto');
  const fuera = d.casos.filter(c => !c.entra);

  const fila = (c, texto, color) => `<li style="margin-bottom:6px">
      <b style="color:var(--${color})">${esc(c.placa)}</b>
      <span style="color:var(--tx2);font-size:var(--fs-sm)"> · ${esc(c.criticidad)} · ${texto}</span>
    </li>`;

  // El reloj corriendo primero: es lo único de este panel sobre lo que alguien
  // puede hacer algo hoy.
  const vivos = abiertos.length
    ? `<p style="margin:8px 0 4px"><b>Abiertos ahora</b> — el reloj corriendo</p>
       <ul style="list-style:none;padding:0;margin:0">${abiertos.map(c =>
         fila(c, `lleva ${esc(c.dias_lleva)} día(s)` +
                 (c.vencido ? ' · <b>VENCIDO</b>' : '') +
                 (c.aplazado_veces ? ` · aplazado ${esc(c.aplazado_veces)} vez/veces` : ''),
              c.vencido ? 'red' : 'yellow')).join('')}</ul>`
    : '<p style="margin:8px 0 4px;color:var(--tx2)">Ningún hallazgo abierto.</p>';

  const resueltos = cerrados.length
    ? `<p style="margin:12px 0 4px"><b>Resueltos</b> — duración real</p>
       <ul style="list-style:none;padding:0;margin:0">${cerrados.map(c =>
         fila(c, `${esc(c.dias)} día(s), reportado el ${esc(c.reportado)}`, 'tx2')).join('')}</ul>`
    : '';

  // El denominador, visible. Un indicador que solo reporta lo que mira
  // devolvería «0 días promedio» sobre una flota llena de línea base.
  const excluidos = fuera.length
    ? `<p style="font-size:var(--fs-xs);color:var(--tx2);margin:10px 0 0">
         <b>${esc(fuera.length)} fuera del indicador</b>: ${
           [...new Set(fuera.map(c => c.motivo_fuera))].join(' · ')}</p>`
    : '';

  const promedio = d.n
    ? `<b>${esc(d.promedio_dias)} días</b> en promedio, sobre <b>${esc(d.n)}</b> hallazgo(s) cerrado(s)`
    : `<b>sin dato</b> — ${esc(d.motivo)}`;

  return `<div class="tabla-card">
    <div class="tabla-titulo">Días de hallazgo abierto</div>
    ${vivos}
    ${resueltos}
    ${excluidos}
    <p style="margin:12px 0 0;padding-top:8px;border-top:1px solid var(--bg2)">
      ${promedio}</p>
    <p style="font-size:var(--fs-xs);color:var(--tx3,var(--tx2));margin:4px 0 0">
      ${esc(d.base)} · ${esc(d.etiqueta)}</p>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:8px 0 0">
      Mide riesgo real, no gestión: el reloj para cuando el vehículo
      <b>vuelve reparado</b>, no al aprobar la orden ni al entrar al taller.
      <b>No compara zonas</b> — los tiempos de repuesto son distintos en Neiva,
      Pitalito y Florencia, y un promedio comparado mediría geografía.</p>
  </div>`;
}

/** Rendimiento km/galón por vehículo — con lo que le falta a cada uno.
 *
 * Ordenado por **placa y no por rendimiento**: ordenarlo por el número lo
 * convertiría en un ranking sin que nadie lo hubiera decidido, y el canon dice
 * que esto no compara vehículos — un motocarro y un NHR no rinden igual y la
 * diferencia no dice nada.
 *
 * A diferencia de la pantalla del conductor, acá el número provisional SÍ se
 * muestra, con su advertencia: la pregunta de control de flota es «¿ya se puede
 * medir esto?», y para contestarla hace falta verlo.
 */
function flotaAnRendimiento(h) {
  const filas = h.rendimiento_por_vehiculo;
  if (filas === null || filas === undefined) {
    return flotaAnPanelVacio('Rendimiento km/galón',
      'La tabla de tanqueos todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  if (!filas.length) {
    return flotaAnPanelVacio('Rendimiento km/galón',
      'No hay vehículos activos que medir.',
      `dar de alta un vehículo ${flotaDondeSeDaDeAlta()}.`);
  }
  const cuerpo = filas.map(f => `<div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:var(--fs-sm)">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--${f.publicable ? 'tx2' : 'yellow'})">${
          f.km_galon === 'sin_dato' ? 'sin dato' : `${esc(f.km_galon)} km/gal`}${
          f.publicable ? '' : ' · provisional'}</span>
      </div>
      <div style="font-size:var(--fs-xs);color:var(--tx2)">
        ${f.motivo || `${esc(f.ventanas)} ventana(s) · ${esc(f.dias_historia)} día(s) de historia`}${
          f.tanqueos_fuera_por_parcial
            ? ` · ${esc(f.tanqueos_fuera_por_parcial)} tanqueo(s) fuera por no estar marcados «lleno»`
            : ''}</div>
      <div style="font-size:var(--fs-xs);color:var(--tx3,var(--tx2))">${esc(f.base)} · ${esc(f.etiqueta)}</div>
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Rendimiento km/galón, por vehículo</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">
      Se mide de <b>tanque lleno a tanque lleno</b>: entre dos llenos, lo que
      entró al tanque es lo que se gastó. Un tanqueo sin marcar «lleno» no
      empeora la medición — la impide. <b>No compara vehículos</b> ni mide a
      quien maneja.</p>
    ${cuerpo}
  </div>`;
}

/** Una tarjeta de contadores que NO se suman entre sí.
 *
 * Los tres o cuatro números de estos paneles se atienden llamando a personas
 * distintas —al taller, a contabilidad, al concesionario—, y un total no dice a
 * quién llamar. Es la lección de los 639 avisos aplicada a la forma del panel:
 * separados, con nombre, y cada uno con lo que significa un cero.
 */
function flotaAnFilasContador(filas) {
  return filas.map(([n, etiqueta, nota]) => `<div style="margin-bottom:8px">
      <b style="font-size:var(--fs-md);color:var(--${n === null || n === undefined ? 'yellow' : (n ? 'red' : 'tx2')})">${
        n === null || n === undefined ? '—' : n}</b>
      <span style="font-size:var(--fs-sm)"> ${etiqueta}</span>
      <div style="font-size:var(--fs-xs);color:var(--tx2)">${nota}</div>
    </div>`).join('');
}

/** La tarjeta completa alrededor de esas filas. */
function flotaAnContadores(titulo, intro, filas, pie, lista) {
  return `<div class="tabla-card">
    <div class="tabla-titulo">${titulo}</div>
    ${intro ? `<p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">${intro}</p>` : ''}
    ${flotaAnFilasContador(filas)}
    ${lista || ''}
    ${pie ? `<p style="font-size:var(--fs-xs);color:var(--tx3,var(--tx2));margin:6px 0 0">${pie}</p>` : ''}
  </div>`;
}

/** El costo por kilómetro del mes, los seis, con su motivo.
 *
 * Hasta el 2026-09-04 este campo escondía a los vehículos sin gasto y devolvía
 * `[]` cuando nadie había registrado nada — o sea, escondía el tablero entero
 * justo en el estado en que vive la operación. Ahora salen los seis, y el que
 * no tiene cifra dice **cuál de las tres causas** lo dejó sin ella, porque las
 * tres se corrigen llamando a personas distintas.
 */
function flotaAnCPK(h) {
  const filas = h.cpk_mes;
  if (filas === null || filas === undefined) {
    return flotaAnPanelVacio('Costo por kilómetro',
      'La tabla de gastos todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  if (!filas.length) {
    return flotaAnPanelVacio('Costo por kilómetro',
      'No hay vehículos activos que medir.',
      `dar de alta un vehículo ${flotaDondeSeDaDeAlta()}.`);
  }
  const cuerpo = filas.map(f => `<div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:var(--fs-sm)">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--tx2)">${
          f.cpk === 'sin_dato' ? 'sin dato'
            : `${flotaPesos(f.cpk)}/km · ${flotaPesos(f.pesos)} ÷ ${
                Number(f.km).toLocaleString('es-CO')} km · odómetro ${esc(f.marca)}`}</span>
      </div>
      ${f.motivo ? `<div style="font-size:var(--fs-xs);color:var(--tx2)">${esc(f.motivo)}</div>` : ''}
      <div style="font-size:var(--fs-xs);color:var(--tx3,var(--tx2))">${esc(f.base)} · ${esc(f.desde)} a ${esc(f.hasta)} · ${esc(f.n)} lectura(s)</div>
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Costo por kilómetro · mes en curso</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">
      Es lo que se registró, no el costo de tener el camión: no incluye
      depreciación ni financiación. <b>No se compara entre vehículos</b> — un
      NHR y un motocarro no cuestan igual y la diferencia mide la composición
      del parque, no la operación. Para «¿qué camión se come la plata?» está el
      panel de pesos por mes, acá abajo.</p>
    ${cuerpo}
  </div>`;
}

/** «¿Qué camión se come la plata?» — la única comparación entre vehículos que
 * el canon autoriza, y existe porque el panel de arriba la promete por escrito.
 *
 * `flotaAnCPK` dice **en pantalla** «Para «¿qué camión se come la plata?» está
 * el panel de pesos por mes». Ese panel no existía en ningún archivo: la frase
 * mandaba a una pantalla que nadie había escrito. `gestion-admin.md:81` manda
 * al `admin` a esa misma pregunta una vez al mes, y es la **única decisión de
 * plata del módulo** — o sea, la que se quedó sin dónde tomarse.
 *
 * ## Por qué acá SÍ se ordena de mayor a menor, y en el CPK no
 *
 * El canon §3 prohíbe comparar el CPK de un NHR con el de un motocarro: la
 * diferencia mediría la composición del parque, no la operación. Los pesos del
 * mes no tienen ese problema — son plata que salió, y la pregunta del que
 * decide es cuál salió más. No es una excepción al canon: es la pregunta que el
 * canon manda a hacer en otra unidad.
 *
 * ## Los dos ceros, que es el defecto principal que el canon previene
 *
 * `hubo_gastos` viajaba en el payload y **ninguna pantalla lo distinguía**.
 * Acá es la diferencia entre las dos únicas filas que se pueden confundir:
 *
 *   - `false` → «sin registro», y va **fuera del orden**. Ordenarlo como $0 lo
 *     dejaría de último, o sea coronado como el camión más barato de la flota —
 *     que es exactamente la lectura que el canon §6 existe para impedir.
 *   - `true` con `pesos = 0` → `$0` **con su renglón**: hay gastos del vehículo
 *     y ninguno cayó en el mes. Eso sí es una afirmación sobre la flota.
 *
 * Y por eso el total dice a cuántos vehículos cubre. Un total sobre 2 de 6 que
 * no lo declara es el número que se lleva a una reunión creyendo que es la
 * flota entera.
 */
function flotaAnPesosMes(h) {
  const filas = h.cpk_mes;
  if (filas === null || filas === undefined) {
    return flotaAnPanelVacio('Pesos por mes',
      'La tabla de gastos todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  if (!filas.length) {
    return flotaAnPanelVacio('Pesos por mes',
      'No hay vehículos activos que medir.',
      `dar de alta un vehículo ${flotaDondeSeDaDeAlta()}.`);
  }
  const con = filas.filter(f => f.hubo_gastos);
  const sin = filas.filter(f => !f.hubo_gastos);
  if (!con.length) {
    return flotaAnPanelVacio('Pesos por mes',
      `Ninguno de los ${filas.length} vehículo(s) activo(s) tiene un solo gasto ` +
      'registrado, y un $0 acá se leería como una flota que no cuesta nada.',
      'registrar un gasto desde «Gastos» en el expediente del vehículo.');
  }
  const v = filas[0];
  const total = con.reduce((s, f) => s + Number(f.pesos), 0);
  const orden = con.slice().sort((a, b) => Number(b.pesos) - Number(a.pesos));
  const cuerpo = orden.map(f => `<div style="display:flex;justify-content:space-between;font-size:var(--fs-sm);margin-bottom:6px">
      <b>${esc(f.placa)}</b>
      <span>${flotaPesos(f.pesos)}${
        Number(f.pesos) === 0
          ? ' <span style="color:var(--tx2);font-weight:400">· ningún gasto cayó en el mes</span>'
          : ''}</span>
    </div>`).join('');
  const cola = sin.length ? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--brd)">
      <div style="font-size:var(--fs-xs);color:var(--tx2);margin-bottom:6px"><b>Sin registro</b> —
        van aparte y no entran al orden. Cero gastos registrados no es un camión
        barato: es un camión del que no se sabe.</div>
      ${sin.map(f => `<div style="display:flex;justify-content:space-between;font-size:var(--fs-sm)">
        <b>${esc(f.placa)}</b><span style="color:var(--tx2)">sin registro</span></div>`).join('')}
    </div>` : '';
  return `<div class="tabla-card">
    <div class="tabla-titulo">Pesos por mes · ¿qué camión se come la plata?</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">
      <b>Esta sí se compara entre vehículos</b> y es la única del tab que se
      puede: son pesos que salieron. Para eso está acá y no arriba — el costo
      por kilómetro no se compara, porque un NHR y un motocarro no cuestan
      igual. <b>No explica una subida</b>: dice cuánto, no por qué.</p>
    ${cuerpo}
    <div style="display:flex;justify-content:space-between;font-size:var(--fs-sm);font-weight:700;margin-top:8px;padding-top:8px;border-top:1px solid var(--brd)">
      <span>Total</span><span>${flotaPesos(total)}</span></div>
    ${cola}
    <div style="font-size:var(--fs-xs);color:var(--tx3,var(--tx2));margin-top:6px">
      ${esc(v.base)} · ${esc(v.desde)} a ${esc(v.hasta)} · sobre ${esc(con.length)} de ${esc(filas.length)}
      vehículo(s) con gastos registrados${
        sin.length ? ` — el total NO incluye ${esc(sin.length)} sin registro` : ''}</div>
  </div>`;
}

/** Vida de llanta por posición — **donde el despromediado rinde de verdad.**
 *
 * Es la única comparación del sistema donde los confundidores desaparecen por
 * construcción: misma placa, mismo conductor, mismas rutas, mismo mes. Si la
 * posición 5 dura la mitad que la 6, no es la ruta ni quien maneja — es
 * alineación, presión o un eje que se come el flanco interno.
 *
 * Y por eso el panel dice explícitamente qué comparación SÍ vale acá y cuál no:
 * entre posiciones del mismo camión, sí; entre camiones, no.
 */
function flotaAnLlantas(h) {
  const filas = h.km_por_posicion;
  const montadas = h.llantas_montadas, sinLlanta = h.posiciones_sin_llanta;
  if (filas === null || filas === undefined) {
    return flotaAnPanelVacio('Vida de llanta por posición',
      'La tabla de montajes todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  if (!filas.length) {
    const total = (montadas || 0) + (sinLlanta || 0);
    return flotaAnPanelVacio('Vida de llanta por posición',
      `Ninguna vida cerrada todavía: ${montadas || 0} llanta(s) montada(s) de ` +
      `${total || '—'} posición(es) declarada(s). La primera medición sale al ` +
      `<b>desmontar</b>, no al montar.` +
      (h.vehiculos_sin_posiciones_llanta
        ? ` ${h.vehiculos_sin_posiciones_llanta} vehículo(s) no declaran cuántas posiciones tienen.`
        : ''),
      'dar de alta una llanta y montarla desde «Llantas» en el expediente.');
  }
  const porPlaca = {};
  filas.forEach(f => { (porPlaca[f.placa] = porPlaca[f.placa] || []).push(f); });
  const cuerpo = Object.entries(porPlaca).map(([placa, ps]) => `<div style="margin-bottom:12px">
      <b style="font-size:var(--fs-sm)">${placa}</b>
      <ul style="list-style:none;padding:0;margin:4px 0 0">${ps.map(p => `
        <li style="font-size:var(--fs-sm);color:var(--tx2)">
          posición ${esc(p.posicion)}: ${p.mediana_km === 'sin_dato'
            ? `sin dato — ${esc(p.n)} vida(s) cerrada(s), faltan ${esc(p.faltan)}`
            : `<b>${Number(p.mediana_km).toLocaleString('es-CO')} km</b> de mediana sobre ${esc(p.n)} vida(s)`}
        </li>`).join('')}</ul>
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Vida de llanta por posición</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">
      <b>Acá sí se comparan filas — pero solo dentro del mismo camión.</b> Las
      posiciones de un vehículo comparten ruta, conductor y mes, así que una que
      dura la mitad que su vecina no es azar: es alineación, presión o un eje.
      Entre camiones distintos la comparación no dice nada, y una direccional no
      dura lo que una de tracción.</p>
    ${cuerpo}
  </div>`;
}

/** El plan preventivo: qué tarea está vencida y de cuál camión.
 *
 * Los cuatro contadores van separados y **ninguno se suma a otro**, porque cada
 * uno se corrige llamando a alguien distinto: vencida → al taller; por vencer →
 * a conseguir el repuesto; sin línea base → a quien sepa cuándo se hizo la
 * última vez; sin intervalo → al concesionario.
 */
function flotaAnPreventivo(h) {
  if (h.tareas_vencidas === null || h.tareas_vencidas === undefined) {
    return flotaAnPanelVacio('Plan preventivo',
      'El plan todavía no está sembrado en esta base.',
      'encender <code>FLOTA_PREVENTIVO</code> — el cron nace apagado (regla 10).');
  }
  return flotaAnContadores('Plan preventivo',
    'Los cuatro se atienden llamando a personas distintas, y por eso no se suman.',
    [[h.tareas_vencidas, 'tarea(s) vencida(s)', 'Al taller.'],
     [h.tareas_por_vencer, 'por vencer', 'A conseguir el repuesto antes.'],
     [h.tareas_sin_linea_base, 'sin línea base',
      'Nunca se ejecutaron: no están al día ni vencidas, porque no hay contra qué comparar.'],
     [h.tareas_sin_intervalo, 'sin intervalo declarado',
      '<b>El que impide que los otros tres se apaguen sin que nadie lo note.</b> ' +
      'La ficha dice QUÉ aceite lleva y no CADA CUÁNTOS KM se cambia.']],
    'Sin umbral de km/día: no hay una sola medición con la que fijarlo (regla 13).');
}

/** Ritmo de uso — km por día, los seis, con su marca de confianza.
 *
 * Ya salía enumerado y con motivo desde que nació: es el campo que este tab
 * copió para arreglar `cpk_mes`. Publica un HECHO y **ningún umbral** — es el
 * que va a permitir fijar `DIAS_AVISO_PREVENTIVO` con dato dentro de un mes, en
 * vez de a ojo hoy.
 */
function flotaAnRitmo(h) {
  const filas = h.km_dia_por_vehiculo;
  if (filas === null || filas === undefined) {
    return flotaAnPanelVacio('Ritmo de uso (km/día)',
      'La tabla de lecturas todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  if (!filas.length) {
    return flotaAnPanelVacio('Ritmo de uso (km/día)',
      'No hay vehículos activos que medir.',
      `dar de alta un vehículo ${flotaDondeSeDaDeAlta()}.`);
  }
  const cuerpo = filas.map(f => `<div style="margin-bottom:8px;font-size:var(--fs-sm)">
      <div style="display:flex;justify-content:space-between">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--tx2)">${
          f.km_dia === 'sin_dato' ? 'sin dato'
            : `${esc(f.km_dia)} km/día · ${esc(f.n)} lectura(s) en ${esc(f.dias)} día(s) · ${esc(f.marca)}`}</span>
      </div>
      ${f.motivo ? `<div style="font-size:var(--fs-xs);color:var(--tx2)">${esc(f.motivo)}</div>` : ''}
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Ritmo de uso · km por día</div>
    <p style="font-size:var(--fs-xs);color:var(--tx2);margin:0 0 10px">
      Un hecho, <b>sin umbral</b>. Nadie sabe todavía cuántos km/día son muchos
      en esta flota, y un techo escrito hoy sería a ojo. Se publica para poder
      fijarlo con dato dentro de un mes.</p>
    ${cuerpo}
  </div>`;
}

/** Taller y garantía. Tres contadores que no se suman.
 *
 * `garantias_vigentes` en cero durante meses **no** significa «no hubo taller»:
 * significa que la búsqueda que evita pagar dos veces no tiene sobre qué
 * pronunciarse. Sin este panel las dos se ven igual de vacías.
 */
function flotaAnTaller(h) {
  if (h.ot_abiertas === null || h.ot_abiertas === undefined) {
    return flotaAnPanelVacio('Taller y garantía',
      'La tabla de órdenes de trabajo todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  return flotaAnContadores('Taller y garantía', '',
    [[h.ot_abiertas, 'camión(es) adentro ahora', 'Órdenes sin cerrar.'],
     [h.trabajos_sin_factura, 'trabajo(s) cerrado(s) sin factura',
      'A contabilidad. Es el precio de que la OT no lleve valor: el camión entra hoy y la factura llega el 30.'],
     [h.garantias_vigentes, 'garantía(s) todavía vigente(s)',
      'No se atiende: se consulta ANTES de mandar el camión. En cero durante meses significa que la búsqueda que evita pagar dos veces no tiene sobre qué pronunciarse — distinto de «no hubo taller».']],
    'Sin umbral de duración de visita: no hay una sola medición todavía.');
}

/** Arma el tab. UN solo `get`: una foto, un estado.
 *
 * Si cada panel pidiera lo suyo, dos paneles del mismo tablero podrían quedar
 * en estados distintos y contradecirse — es el mismo argumento por el que
 * `_cuenta_preventivo` hace una sola pasada en el adaptador.
 */
async function flotaCargarAnalitica() {
  const cont = document.getElementById('flota-analitica');
  if (!cont) return;
  cont.innerHTML = '<p style="color:var(--tx2)">Cargando…</p>';
  let h;
  try {
    h = await flotaHealth(true);
  } catch (e) {
    // Se DECLARA la falla en vez de dejar el panel vacío: un tab en blanco es
    // indistinguible de uno sin datos, y las dos cosas se atienden distinto.
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo leer el estado de la flota: ${esc(e.message)}</div>`;
    return;
  }
  // **Solo lo que no es accionable del día**, y primero lo que decide plata
  // (2026-09-24). Papeles, custodia, inspección y el «recorrido de la semana»
  // repetían la bandeja —Hoy y Pendientes, con placa y botón— con otro
  // número y otro texto; «Salud de la flota» (41 renglones de prosa) se
  // retiró: lo accionable está en Pendientes y lo técnico en el Diagnóstico
  // plegado de abajo, junto con la calidad del kilómetro y lo que la ficha no
  // dice, que son del que mantiene el dato.
  cont.innerHTML = [
    flotaAnProcedencia(h),
    flotaAnCPK(h),
    flotaAnPesosMes(h),
    flotaAnRendimiento(h),
    flotaAnTaller(h),
    flotaAnLlantas(h),
    flotaAnPreventivo(h),
    flotaAnRitmo(h),
    flotaAnHallazgos(h),
  ].join('') + flotaAnDiagnosticoPlegado();
}

/** El diagnóstico técnico, plegado al final de Analítica. Se arma a pedido
 * (`flotaBandejaDiagnostico`, con el mismo health) y trae los contadores
 * técnicos, la calidad del kilómetro, lo que la ficha no dice y los avisos. */
function flotaAnDiagnosticoPlegado() {
  return `<div class="tabla-card"><details>
    <summary style="cursor:pointer" onclick="flotaBandejaDiagnostico()"><b>Diagnóstico técnico</b>
      <span style="font-size:var(--fs-xs);color:var(--tx2)">— para quien mantiene el sistema</span></summary>
    <div id="flota-diagnostico" style="font-size:var(--fs-sm)">
      <p style="color:var(--tx2)">Tocá «Diagnóstico técnico» para cargarlo.</p></div>
  </details></div>`;
}
