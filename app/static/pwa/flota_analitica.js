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
 * ## Y la disciplina es la OPUESTA a la del bloque de salud
 *
 * `flotaBloqueSalud` devuelve vacío cuando no hay nada que hacer, y eso está
 * bien ahí: es una lista de pendientes, y una que siempre muestra algo se deja
 * de mirar — la lección de los 639 avisos.
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
 * `control_flota` va a vivir en Analítica; rebotarlo a Operación en cada
 * recarga es cómo se deja de usar una pantalla.
 */
let FLOTA_SUBTAB = 'operacion';
try { FLOTA_SUBTAB = localStorage.getItem('flota_subtab') || 'operacion'; } catch (e) { /* modo privado */ }

/** Punto de entrada del tab. Despacha; no es una capa. */
async function flotaEntrar() {
  flotaSubtab(FLOTA_SUBTAB);
}

/** Enciende un panel y apaga el otro. Mismo patrón que `invSubtab`. */
function flotaSubtab(nombre) {
  FLOTA_SUBTAB = (nombre === 'analitica') ? 'analitica' : 'operacion';
  try { localStorage.setItem('flota_subtab', FLOTA_SUBTAB); } catch (e) { /* modo privado */ }

  const pares = {
    operacion: ['flota-sub-operacion', 'flota-contenido'],
    analitica: ['flota-sub-analitica', 'flota-analitica'],
  };
  Object.entries(pares).forEach(([k, [tab, panel]]) => {
    const activo = k === FLOTA_SUBTAB;
    const t = document.getElementById(tab);
    if (t) {
      t.style.background = activo ? '#1E8395' : 'transparent';
      t.style.color = activo ? '#fff' : '#415A70';
      t.style.fontWeight = activo ? '700' : '400';
    }
    const p = document.getElementById(panel);
    if (p) p.style.display = activo ? 'block' : 'none';
  });

  if (FLOTA_SUBTAB === 'analitica') flotaCargarAnalitica();
  else cargarFlota();
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
    <p style="color:var(--tx2);font-size:12px;margin:6px 0 0">Se enciende: ${gesto}</p>
  </div>`;
}

/** El recorrido de la semana: las cinco señales, arriba, con su destino.
 *
 * ## El problema que resuelve, medido
 *
 * `especialista-control-flota.md` define el trabajo del rol como **30 minutos,
 * una vez por semana**, sobre cinco señales concretas, y dice que *«los paneles
 * que necesitan trabajo van arriba»*. En el orden real, cuatro de las cinco
 * caen en los paneles 11, 13 y 14 de 14 — arriba está lo que **todavía no se
 * puede medir** (CPK, rendimiento, taller, llantas, preventivo, ritmo salen
 * `sin dato` hasta que el kilómetro se sostenga).
 *
 * Las dos cosas son ciertas a la vez y ninguna está mal: el orden de abajo es
 * de **dependencia** —cada panel dice qué gesto enciende el siguiente— y es un
 * orden para quien construye. Lo que faltaba era el de quien opera.
 *
 * ## Por qué es un índice y no un panel más
 *
 * Repite cinco números que ya están abajo, y eso normalmente es la receta para
 * que dos pantallas digan cosas distintas. Acá no puede pasar: **los lee del
 * mismo objeto `h`, en el mismo render**. Lo que no repite es la
 * interpretación — ni el motivo, ni la base, ni el gesto. Para eso está el
 * panel, y este bloque dice cuál.
 *
 * ## Y no esconde las que están limpias
 *
 * La disciplina de este tab, otra vez: un renglón que desaparece por estar en
 * cero es indistinguible de uno que nunca se escribió. Un cero acá es una
 * afirmación —«esta semana no hay nada que perseguir por este lado»— y es
 * media respuesta de las cinco.
 */
function flotaAnSemana(h) {
  const dhc = h.dias_hallazgo_abierto;
  // `fichas_completas` va al revés que las otras cuatro: pide trabajo cuando es
  // MENOR que el parque, no cuando crece.
  //
  // **Y NO se calcula como `vehiculos_activos - fichas_completas`.** Se intentó
  // y estaba mal: `fichas_completas` cuenta fichas completas de TODOS los
  // vehículos y `vehiculos_activos` solo los activos, así que un vehículo dado
  // de baja con la ficha completa hace la resta negativa — «-1 fichas sin
  // completar». Es el defecto contra el que advierte el comentario de
  // `cpk_mes`: dos denominadores que se calculan distinto hacen que el tablero
  // diga una cosa donde el health dice otra.
  //
  // `cobertura_por_vehiculo` YA enumera exactamente los vehículos activos con
  // su `ficha_completa`, que es el mismo predicado del panel al que este
  // renglón manda. Se cuenta de ahí: una fuente, no dos.
  const cob = h.cobertura_por_vehiculo;
  // Lista vacía → `sin dato`, no `0`. «Ninguna ficha pendiente» y «no hay
  // vehículos que medir» se ven idénticos en un cero, y el primero autoriza a
  // no hacer nada. Es la misma distinción que hace el panel al que manda.
  const fichasFaltan = (cob === null || cob === undefined || !cob.length)
    ? null : cob.filter(v => !v.ficha_completa).length;

  const señales = [
    ['Fichas técnicas sin completar', fichasFaltan, 'Lo que la ficha no dice'],
    // `dhc.n_vencidos` y NO `casos.filter(c => c.vencido).length`. El campo ya
    // viaja calculado por el dominio sobre los hallazgos que ENTRAN al
    // indicador; el filtro del cliente daba el mismo número solo porque los
    // casos que no entran no traen la clave `vencido`. Dos implementaciones del
    // mismo número, una apoyada en un accidente del serializador.
    ['Daños que pasaron su fecha límite',
     dhc ? dhc.n_vencidos : null,
     'Días de hallazgo abierto'],
    ['Documentos vencidos', h.documentos_vencidos, 'Papeles'],
    ['Turnos cerrados a la fuerza', h.custodias_cerradas_forzadas, 'Custodia'],
    ['Custodias sin las fotos completas', h.custodias_sin_foto_completa, 'Custodia'],
  ];

  const filas = señales.map(([que, n, donde]) => {
    // `null` es «no se pudo mirar», no «cero». Son estados distintos y el
    // segundo autoriza a no hacer nada; el primero no.
    const sinDato = (n === null || n === undefined);
    const pide = !sinDato && n > 0;
    const valor = sinDato
      ? '<span style="color:var(--tx2)">sin dato</span>'
      : `<b style="color:${pide ? 'var(--red)' : 'var(--tx2)'}">${n}</b>`;
    return `<div style="display:flex;justify-content:space-between;gap:10px;
                        font-size:13px;margin-bottom:4px">
      <span>${que} <span style="color:var(--tx3,var(--tx2));font-size:11px">·
        ${donde}</span></span>
      ${valor}
    </div>`;
  }).join('');

  return `<div class="tabla-card">
    <div class="tabla-titulo">El recorrido de la semana</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
      Las cinco señales de control de flota, con el panel donde vive cada una.
      <b>Treinta minutos, una vez por semana.</b> Un número en rojo es alguien a
      quien llamar; el panel de abajo dice quién. Los ceros también son
      respuesta y por eso no se esconden.</p>
    ${filas}
    <p style="font-size:11px;color:var(--tx3,var(--tx2));margin:8px 0 0">
      <b>Esta no es la pantalla de todos.</b> La decisión mensual sobre el gasto
      es de gestión y se toma en «Costo por kilómetro» y «Pesos por mes»; acá no
      hay nada que decidir sobre plata.</p>
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
    <p style="margin:4px 0;font-size:13px">
      Ambiente: <b>${esc(p.ambiente || 'sin declarar')}</b> ·
      día operativo <b>${esc(p.dia_operativo || '—')}</b> (Bogotá) ·
      calculado ${(p.calculado_ts || '—').replace('T', ' ').slice(0, 19)}</p>
    ${rojo ? `<p style="color:var(--yellow);font-size:12px;margin:6px 0 0">
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
      'dar de alta un vehículo en Rutas → Vehículos.');
  }
  const cuerpo = filas.map(f => {
    const c = f.por_confianza || {};
    const verif = c.verificada || 0, decl = c.declarada || 0, dud = c.dudosa || 0;
    const sinFoto = f.n - (f.con_foto || 0);
    return `<div style="margin-bottom:12px">
      <div style="display:flex;justify-content:space-between;font-size:13px">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--tx2)">${esc(f.n)} lectura(s)${f.n ? ` · ${esc(f.vigentes)} vigente(s)` : ''}</span>
      </div>
      ${flotaAnBarra([['verificada', verif, 'var(--green,#3fb950)'],
                      ['declarada', decl, 'var(--tx2)'],
                      ['dudosa', dud, 'var(--red)']], f.n)}
      <div style="font-size:12px;color:var(--tx2);margin-top:3px">
        ${f.motivo
          ? f.motivo
          : `${verif} verificada(s) · ${decl} declarada(s) · ${dud} dudosa(s) ·
             <b style="color:var(--${sinFoto ? 'red' : 'tx2'})">${sinFoto} sin foto</b>
             ${f.primera ? ` · desde ${esc(f.primera)}` : ''}`}
      </div>
      <div style="font-size:11px;color:var(--tx3,var(--tx2))">${esc(f.base)} · ${esc(f.etiqueta)}</div>
    </div>`;
  }).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Calidad del kilómetro, por vehículo</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
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
      'dar de alta un vehículo en Rutas → Vehículos.');
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
    `<th style="font-weight:400;color:var(--tx2);font-size:11px;padding:2px 6px">${t}</th>`).join('');
  const cuerpo = filas.map(f => `<tr>
      <td style="padding:2px 6px"><b>${esc(f.placa)}</b></td>
      ${cols.map(([k]) => `<td style="text-align:center;padding:2px 6px">${marca(f[k])}</td>`).join('')}
    </tr>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Lo que la ficha no dice</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
      Cada ✗ apaga una medición aguas abajo: sin capacidad de tanque no hay
      detector de sobre-tanqueo, sin posiciones no hay vida de llanta.
      <b>«?» no es «no»</b>: es que la tabla no existe y no se pudo mirar.</p>
    <div style="overflow-x:auto">
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr><th style="text-align:left;padding:2px 6px"></th>${cabecera}</tr></thead>
        <tbody>${cuerpo}</tbody>
      </table>
    </div>
    <p style="font-size:11px;color:var(--tx3,var(--tx2));margin:8px 0 0">
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
      <span style="color:var(--tx2);font-size:13px"> · ${esc(c.criticidad)} · ${texto}</span>
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
    ? `<p style="font-size:12px;color:var(--tx2);margin:10px 0 0">
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
    <p style="font-size:11px;color:var(--tx3,var(--tx2));margin:4px 0 0">
      ${esc(d.base)} · ${esc(d.etiqueta)}</p>
    <p style="font-size:12px;color:var(--tx2);margin:8px 0 0">
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
      'dar de alta un vehículo en Rutas → Vehículos.');
  }
  const cuerpo = filas.map(f => `<div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:13px">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--${f.publicable ? 'tx2' : 'yellow'})">${
          f.km_galon === 'sin_dato' ? 'sin dato' : `${esc(f.km_galon)} km/gal`}${
          f.publicable ? '' : ' · provisional'}</span>
      </div>
      <div style="font-size:12px;color:var(--tx2)">
        ${f.motivo || `${esc(f.ventanas)} ventana(s) · ${esc(f.dias_historia)} día(s) de historia`}${
          f.tanqueos_fuera_por_parcial
            ? ` · ${esc(f.tanqueos_fuera_por_parcial)} tanqueo(s) fuera por no estar marcados «lleno»`
            : ''}</div>
      <div style="font-size:11px;color:var(--tx3,var(--tx2))">${esc(f.base)} · ${esc(f.etiqueta)}</div>
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Rendimiento km/galón, por vehículo</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
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
      <b style="font-size:16px;color:var(--${n === null || n === undefined ? 'yellow' : (n ? 'red' : 'tx2')})">${
        n === null || n === undefined ? '—' : n}</b>
      <span style="font-size:13px"> ${etiqueta}</span>
      <div style="font-size:12px;color:var(--tx2)">${nota}</div>
    </div>`).join('');
}

/** La tarjeta completa alrededor de esas filas. */
function flotaAnContadores(titulo, intro, filas, pie, lista) {
  return `<div class="tabla-card">
    <div class="tabla-titulo">${titulo}</div>
    ${intro ? `<p style="font-size:12px;color:var(--tx2);margin:0 0 10px">${intro}</p>` : ''}
    ${flotaAnFilasContador(filas)}
    ${lista || ''}
    ${pie ? `<p style="font-size:11px;color:var(--tx3,var(--tx2));margin:6px 0 0">${pie}</p>` : ''}
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
      'dar de alta un vehículo en Rutas → Vehículos.');
  }
  const cuerpo = filas.map(f => `<div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;font-size:13px">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--tx2)">${
          f.cpk === 'sin_dato' ? 'sin dato'
            : `${flotaPesos(f.cpk)}/km · ${flotaPesos(f.pesos)} ÷ ${
                Number(f.km).toLocaleString('es-CO')} km · odómetro ${esc(f.marca)}`}</span>
      </div>
      ${f.motivo ? `<div style="font-size:12px;color:var(--tx2)">${esc(f.motivo)}</div>` : ''}
      <div style="font-size:11px;color:var(--tx3,var(--tx2))">${esc(f.base)} · ${esc(f.desde)} a ${esc(f.hasta)} · ${esc(f.n)} lectura(s)</div>
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Costo por kilómetro · mes en curso</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
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
      'dar de alta un vehículo en Rutas → Vehículos.');
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
  const cuerpo = orden.map(f => `<div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:6px">
      <b>${esc(f.placa)}</b>
      <span>${flotaPesos(f.pesos)}${
        Number(f.pesos) === 0
          ? ' <span style="color:var(--tx2);font-weight:400">· ningún gasto cayó en el mes</span>'
          : ''}</span>
    </div>`).join('');
  const cola = sin.length ? `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--brd)">
      <div style="font-size:12px;color:var(--tx2);margin-bottom:6px"><b>Sin registro</b> —
        van aparte y no entran al orden. Cero gastos registrados no es un camión
        barato: es un camión del que no se sabe.</div>
      ${sin.map(f => `<div style="display:flex;justify-content:space-between;font-size:13px">
        <b>${esc(f.placa)}</b><span style="color:var(--tx2)">sin registro</span></div>`).join('')}
    </div>` : '';
  return `<div class="tabla-card">
    <div class="tabla-titulo">Pesos por mes · ¿qué camión se come la plata?</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
      <b>Esta sí se compara entre vehículos</b> y es la única del tab que se
      puede: son pesos que salieron. Para eso está acá y no arriba — el costo
      por kilómetro no se compara, porque un NHR y un motocarro no cuestan
      igual. <b>No explica una subida</b>: dice cuánto, no por qué.</p>
    ${cuerpo}
    <div style="display:flex;justify-content:space-between;font-size:13px;font-weight:700;margin-top:8px;padding-top:8px;border-top:1px solid var(--brd)">
      <span>Total</span><span>${flotaPesos(total)}</span></div>
    ${cola}
    <div style="font-size:11px;color:var(--tx3,var(--tx2));margin-top:6px">
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
      <b style="font-size:13px">${placa}</b>
      <ul style="list-style:none;padding:0;margin:4px 0 0">${ps.map(p => `
        <li style="font-size:13px;color:var(--tx2)">
          posición ${esc(p.posicion)}: ${p.mediana_km === 'sin_dato'
            ? `sin dato — ${esc(p.n)} vida(s) cerrada(s), faltan ${esc(p.faltan)}`
            : `<b>${Number(p.mediana_km).toLocaleString('es-CO')} km</b> de mediana sobre ${esc(p.n)} vida(s)`}
        </li>`).join('')}</ul>
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Vida de llanta por posición</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
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
      'dar de alta un vehículo en Rutas → Vehículos.');
  }
  const cuerpo = filas.map(f => `<div style="margin-bottom:8px;font-size:13px">
      <div style="display:flex;justify-content:space-between">
        <b>${esc(f.placa)}</b>
        <span style="color:var(--tx2)">${
          f.km_dia === 'sin_dato' ? 'sin dato'
            : `${esc(f.km_dia)} km/día · ${esc(f.n)} lectura(s) en ${esc(f.dias)} día(s) · ${esc(f.marca)}`}</span>
      </div>
      ${f.motivo ? `<div style="font-size:12px;color:var(--tx2)">${esc(f.motivo)}</div>` : ''}
    </div>`).join('');
  return `<div class="tabla-card">
    <div class="tabla-titulo">Ritmo de uso · km por día</div>
    <p style="font-size:12px;color:var(--tx2);margin:0 0 10px">
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

/** Inspección diaria — y el número que contesta la regla 11.
 *
 * `segundos_llenado_30d` existe porque la respuesta de quien no quiere trabajar
 * es **marcar todo óptimo en veinte segundos**. Publica la mediana y el caso
 * mínimo con su vehículo, no un promedio: el promedio esconde justo al que la
 * llenó corriendo.
 */
function flotaAnInspeccion(h) {
  if (h.vehiculos_sin_inspeccion_hoy === null
      || h.vehiculos_sin_inspeccion_hoy === undefined) {
    return flotaAnPanelVacio('Inspección diaria',
      'La tabla de inspecciones todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  // **No `|| {}`.** Ese fallback convertía un `null` en un objeto vacío y el
  // panel publicaba «Mediana de llenado: undefineds sobre undefined
  // inspección(es)» — basura con autoridad, que es peor que un hueco.
  //
  // Hoy no se alcanza, y por un accidente: `segundos_llenado_30d` y
  // `vehiculos_sin_inspeccion_hoy` guardan la MISMA tabla, así que el guard de
  // arriba corta antes. Eso es un acuerdo entre dos campos, no un invariante —
  // el día que uno gane una dependencia que el otro no tiene, el `undefineds`
  // se publica y nadie lo va a ver venir. Se cierra acá en vez de confiar en la
  // coincidencia.
  const s = h.segundos_llenado_30d;
  if (s === null || s === undefined) {
    return `<div class="tabla-card">
      <div class="tabla-titulo">Inspección diaria</div>
      ${flotaAnFilasContador([
        [h.vehiculos_sin_inspeccion_hoy, 'camión(es) que nadie miró hoy',
         'La hace el conductor, con el turno ya recibido.'],
        [h.inspecciones_incompletas_hoy, 'mirado(s) a medias',
         '<b>Incompleta no es «no apto»</b>: es «no sé», y no habilita despacho.'],
      ])}
      <div style="font-size:12px;color:var(--tx2)">Sin dato del tiempo de
        llenado. Nace con la primera inspección contestada.</div>
    </div>`;
  }
  const tiempo = s.nota
    ? `<div style="font-size:12px;color:var(--tx2)">${esc(s.nota)}</div>`
    : `<div style="font-size:13px">Mediana de llenado: <b>${esc(s.mediana)}s</b>
         <span style="color:var(--tx2)">sobre ${esc(s.n)} inspección(es)</span></div>
       ${s.minimo ? `<div style="font-size:12px;color:var(--tx2)">
         La más rápida: ${esc(s.minimo.segundos)}s para ${esc(s.minimo.items)} ítem(s)
         · veredicto ${esc(s.minimo.veredicto)}</div>` : ''}`;
  return `<div class="tabla-card">
    <div class="tabla-titulo">Inspección diaria</div>
    ${flotaAnFilasContador([
      [h.vehiculos_sin_inspeccion_hoy, 'camión(es) que nadie miró hoy',
       'La hace el conductor, con el turno ya recibido.'],
      [h.inspecciones_incompletas_hoy, 'mirado(s) a medias',
       '<b>Incompleta no es «no apto»</b>: es «no sé», y no habilita despacho.'],
    ])}
    ${tiempo}
    <p style="font-size:12px;color:var(--tx2);margin:8px 0 0">
      El tiempo de llenado está acá por la regla 11: la forma de maximizar una
      inspección sin hacerla es <b>marcar todo óptimo en veinte segundos</b>. Se
      publica la mediana y el caso más rápido, no un promedio — el promedio
      esconde justo al que la llenó corriendo.</p>
  </div>`;
}

/** Papeles: qué vence y en cuántos días.
 *
 * Los tres separados. `documentos_no_encontrados` no es «vencido»: es que nadie
 * cargó el papel, y se corrige distinto — uno se renueva, el otro se busca.
 */
/** La lista con placa que va DEBAJO de los contadores.
 *
 * El criterio 1 de este archivo dice que no hay un panel que muestre un solo
 * número. Cinco paneles lo incumplían: Taller, Preventivo, Inspección, Papeles
 * y Custodia. Los dos últimos son también **dos de las cinco señales con las
 * que se mide a control de flota**, y su ficha describe el trabajo como
 * «persigue lo vencido» — que con un contador pelado obliga a abrir los seis
 * expedientes para saber a cuál.
 *
 * Los contadores se quedan: *«vencido»* y *«sin cargar»* se atienden llamando a
 * personas distintas y sumarlos esconde el peor. Lo que se agrega es la otra
 * mitad, la que dice a quién llamar.
 *
 * `null` no pinta nada: el panel ya declaró arriba que la tabla no existe, y
 * repetirlo con otras palabras es cómo un renglón deja de leerse.
 */
function flotaAnListaConPlaca(filas, vacio, render) {
  if (filas === null || filas === undefined) return '';
  if (!filas.length) {
    return `<p style="font-size:12px;color:var(--tx2);margin:10px 0 0">${vacio}</p>`;
  }
  return `<div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--brd)">
    ${filas.map(f => `<div style="font-size:13px;margin-bottom:5px">
      <b>${esc(f.placa)}</b> <span style="color:var(--tx2)">${render(f)}</span></div>`).join('')}
  </div>`;
}

/** Cómo se lee un documento que pide trabajo.
 *
 * Las tres banderas se pintan **juntas y no como una categoría**: un papel
 * `no_encontrado` cuya fecha ya pasó es las dos cosas, y elegir una escondería
 * la otra. `dias` viene firmado — el signo es la diferencia entre «sacá la
 * cita» y «bajá el camión».
 */
function flotaAnTextoDocumento(f) {
  const partes = [];
  if (f.vencido) {
    partes.push(`<span style="color:var(--red)">venció hace ${Math.abs(f.dias)} día(s)</span>`);
  }
  if (f.por_vencer_30d) {
    // `dias === 0` es hoy. «Vence en 0 día(s)» hace pensar en un error de
    // cálculo justo el día en que el camión no debería salir mañana.
    partes.push(`<span style="color:var(--yellow)">${
      f.dias === 0 ? 'vence hoy' : `vence en ${esc(f.dias)} día(s)`}</span>`);
  }
  if (f.no_encontrado) partes.push('nadie lo ha podido mostrar');
  return `${f.tipo} — ${partes.join(' · ')}`;
}

function flotaAnPapeles(h) {
  if (h.documentos_vencidos === null || h.documentos_vencidos === undefined) {
    return flotaAnPanelVacio('Papeles',
      'La tabla de documentos todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  const lista = flotaAnListaConPlaca(
    h.documentos_por_vehiculo,
    'Ningún documento vencido, por vencer ni sin cargar. Los que hay están al día.',
    flotaAnTextoDocumento);
  return flotaAnContadores('Papeles', '',
    [[h.documentos_vencidos, 'documento(s) vencido(s)', 'El vehículo no debería salir.'],
     [h.documentos_por_vencer_30d, 'vence(n) en 30 días',
      'Todavía hay tiempo. Una cita de tecnomecánica en Neiva tarda unos quince días.'],
     [h.documentos_no_encontrados, 'sin cargar',
      '<b>No es lo mismo que vencido</b>: uno se renueva, el otro se busca. Un papel que nadie subió no se puede juzgar.']],
    'Corte en día operativo de Bogotá, no UTC: un SOAT que vence hoy aparecería vencido una noche antes.',
    lista);
}

/** Custodia: quién responde por cada camión, y qué turnos se cerraron solos.
 *
 * `custodias_cerradas_forzadas` es la señal que `gestion-admin.md` y
 * `especialista-control-flota.md` mandan revisar cada semana. Si crece, el
 * problema no es el sistema: es que nadie está cerrando turno.
 */
/** Cómo se lee un turno que pide trabajo.
 *
 * Las dos cosas que un turno puede tener mal son independientes y se atienden
 * distinto: un cierre forzado es una conducta —alguien cerró el turno de otro—
 * y unas fotos incompletas son un registro que no va a servir de evidencia. Un
 * mismo turno puede ser las dos, y las dos se dicen.
 *
 * `mitad_incompleta` va en el texto porque es lo que hace la fila accionable:
 * «faltan las de inicio» y «faltan las de cierre» se arreglan hablando con
 * personas distintas y en momentos distintos del día.
 */
function flotaAnTextoCustodia(f) {
  const dia = (f.inicio_ts || '').slice(0, 10) || 'sin fecha';
  const partes = [];
  if (f.cierre_forzado) {
    partes.push(`<span style="color:var(--red)">cerrado a la fuerza</span>${
      f.cierre_forzado_motivo ? `: «${esc(f.cierre_forzado_motivo)}»` : ' (sin motivo escrito)'}`);
  }
  if (f.sin_foto_completa) {
    partes.push(`faltan fotos de ${f.mitad_incompleta} (${f.fotos} de ${f.fotos_exigidas})`);
  }
  return `turno del ${dia}${f.abierta ? ', todavía abierto' : ''} — ${partes.join(' · ')}`;
}

function flotaAnCustodia(h) {
  if (h.vehiculos_sin_custodia_activa === null
      || h.vehiculos_sin_custodia_activa === undefined) {
    return flotaAnPanelVacio('Custodia',
      'La tabla de custodias todavía no existe en esta base.',
      'la migración del módulo de flota.');
  }
  const lista = flotaAnListaConPlaca(
    h.custodias_por_vehiculo,
    'Ningún turno cerrado a la fuerza ni con fotos incompletas.',
    flotaAnTextoCustodia);
  return flotaAnContadores('Custodia', '',
    [[h.vehiculos_sin_custodia_activa, 'vehículo(s) sin nadie que responda ahora', ''],
     [h.custodias_cerradas_forzadas, 'turno(s) cerrado(s) a la fuerza',
      'Si crece, el problema no es el sistema: es que nadie está cerrando turno.'],
     [h.custodias_sin_foto_completa, 'custodia(s) sin las fotos completas',
      'Sin fotos comparables, un golpe nuevo no se le puede atribuir a nadie — ni al conductor ni al turno anterior.'],
     [h.custodias_pendiente_sede, 'sin declarar dónde quedó el vehículo',
      'Un camión fuera de sede sin motivo escrito es un activo pasando la noche fuera del control de la empresa.']],
    '', lista);
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
    h = await get('/flota/health');
  } catch (e) {
    // Se DECLARA la falla en vez de dejar el panel vacío: un tab en blanco es
    // indistinguible de uno sin datos, y las dos cosas se atienden distinto.
    cont.innerHTML = `<div class="tabla-card" style="color:var(--red)">
      No se pudo leer el estado de la flota: ${esc(e.message)}</div>`;
    return;
  }
  // El orden no es temático: es **qué panel le dice al que mira qué gesto
  // hacer para encender el siguiente**. Con seis vehículos y seis tablas
  // vacías, este tab es sobre todo un mapa de lo que falta.
  //
  // El kilómetro va primero porque mientras esté en rojo, el CPK, el km/día y
  // el km por llanta salen `sin_dato` por diseño. La cobertura va segunda
  // porque es el índice: cada ✗ es la condición de existencia de un panel de
  // abajo.
  cont.innerHTML = [
    flotaAnProcedencia(h),
    flotaAnSemana(h),
    flotaAnLecturas(h),
    flotaAnCobertura(h),
    flotaAnCPK(h),
    flotaAnPesosMes(h),
    flotaAnRendimiento(h),
    flotaAnTaller(h),
    flotaAnLlantas(h),
    flotaAnPreventivo(h),
    flotaAnRitmo(h),
    flotaAnHallazgos(h),
    flotaAnInspeccion(h),
    flotaAnPapeles(h),
    flotaAnCustodia(h),
  ].join('');
}
