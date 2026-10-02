/**
 * Utilidades compartidas por toda la PWA. Se carga PRIMERO.
 *
 * Existe como archivo aparte y no dentro de `app.js` por una razón de pruebas:
 * los arneses de Node (los `test_render_..._js.py` de `tests/flota`) cargan
 * el módulo bajo
 * prueba en un `vm` y **stubbean** lo que viene de `app.js` —`get`,
 * `horaColombia`, `alerta`—. Un `esc` stubbeado convertiría todos los tests de
 * escapado en tests del stub: el arnés diría verde con la función real rota.
 * Acá el arnés carga el archivo de verdad.
 */

/** Escapa un valor para meterlo en HTML. **Convierte, no decide.**
 *
 * ## Por qué existe
 *
 * La PWA arma su HTML con literales de plantilla y lo asigna a `innerHTML`.
 * Cualquier texto que una persona escribió —el motivo de un cierre forzado, la
 * descripción de un daño, el nombre de un taller— viajaba crudo hasta ahí. El
 * caso concreto que lo destapó: `POST /flota/custodia/traspaso` es
 * `LECTURA_FLOTA`, o sea que **el conductor escribe** el motivo, y ese texto se
 * pinta en la pantalla de gestión. De la cuenta con menos permisos del módulo a
 * la que más tiene.
 *
 * ## Por qué acá y no en el `get()`
 *
 * Escapar la respuesta entera al recibirla parece más barato y corrompe datos:
 * un texto que se edita y se vuelve a enviar viajaría con `&lt;` adentro y se
 * guardaría así. El escape pertenece al sitio donde el dato se vuelve HTML, no
 * al sitio donde llega.
 *
 * ## Por qué no un saneador del HTML ya armado
 *
 * Porque esta app pone sus propios `onclick=` en la misma cadena que los datos.
 * Un saneador que borre manejadores inline mata la mitad de la interfaz, y uno
 * que los respete deja pasar el inyectado. Después de concatenar ya no se
 * distinguen: hay que escapar antes.
 *
 * ## Lo que NO resuelve, dicho para que nadie lo suponga
 *
 * Un dato metido dentro de JavaScript dentro de un atributo
 * —`onclick="abrir('${x}')"`— **no queda protegido**: el navegador decodifica
 * las entidades del atributo antes de que el JS corra, así que un `&#39;`
 * vuelve a ser `'` y rompe la cadena igual. No empeora nada (sin `esc` rompe
 * idéntico) pero tampoco arregla. Esos sitios necesitan otra cosa y están
 * contados aparte.
 *
 * `String(v)` y no un default a `''`: reproduce exactamente lo que hacía el
 * literal de plantilla —`null` se veía «null»— para que este cambio mueva UNA
 * cosa. Convertir los nulos en vacío es una decisión de producto distinta, y
 * en este repo esconder un hueco tiene su propio historial.
 */
function esc(v) {
  return String(v)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Pesos colombianos para leer: «$1.234.567», separador de miles con punto.
 *
 * **Uno para toda Flota** (2026-09-24): había tres —`flotaPesos`, `fjPesos` y
 * el de la evidencia de las señales— y el mismo valor salía «$20.000» en una
 * pantalla y «2.000E+4» en otra. Vive acá porque los arneses de Node cargan
 * `util.js` de verdad y no todos cargan `flota.js`.
 *
 * `null`/vacío es «sin dato», nunca «$0»: un cero inventado se lee como un
 * gasto que no hubo (Regla 0). Lo que no es un número se muestra tal cual.
 */
function fmtPesos(x) {
  if (x === null || x === undefined || x === '') return 'sin dato';
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return '$' + Math.round(n).toLocaleString('es-CO');
}

/** Dólares para leer: «US$ 4.291,20» — con su signo, para que nunca se lean
 * como pesos (el Armador pintaba «$4.291,2 FOB USD»: un «$» suelto que en
 * Colombia es pesos). Dos decimales siempre: un FOB unitario de US$ 0,45 es
 * un precio, no un redondeo. `null`/vacío es «sin dato», nunca «US$ 0». */
function fmtUsd(x) {
  if (x === null || x === undefined || x === '') return 'sin dato';
  const n = Number(x);
  if (!Number.isFinite(n)) return String(x);
  return 'US$ ' + n.toLocaleString('es-CO', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** El día de Bogotá (`YYYY-MM-DD`) de un instante: **la única** del PWA.
 *
 * Una fecha que alguien LEE como día (un filtro «hoy», la fecha de un
 * tanqueo, el período de un KPI) es el día de Bogotá, no el de UTC (Regla 5).
 * `toISOString().slice(0, 10)` da el día UTC: entre las 7 p. m. y la
 * medianoche de Neiva ya es «mañana». Había cinco copias de esta función
 * (liquidación, tablero, flota, analítica) y tres sitios de rutas con
 * `toISOString()` (2026-09-25); ahora hay una, y el trinquete
 * `test_hoy_bogota_una_sola.py` impide que vuelva otra.
 *
 * @param {Date|string|number} [fecha] - el instante (por defecto, ahora)
 * @param {number} [desplazamientoDias] - días a sumar (negativo = atrás)
 * @returns {string} `YYYY-MM-DD`
 */
function hoyBogota(fecha, desplazamientoDias) {
  const base = fecha instanceof Date ? fecha : (fecha != null ? new Date(fecha) : new Date());
  const d = new Date(base.getTime() + (Number(desplazamientoDias) || 0) * 86400000);
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/Bogota',
      year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
  } catch (_) {
    // Bogotá no tiene horario de verano: UTC−5 fijo.
    const b = new Date(d.getTime() - 5 * 3600 * 1000);
    const dos = (n) => String(n).padStart(2, '0');
    return `${b.getUTCFullYear()}-${dos(b.getUTCMonth() + 1)}-${dos(b.getUTCDate())}`;
  }
}

/* ── Paquetes (2026-10-02) ──────────────────────────────────────────────────
 *
 * El inventario, Siesa, el conteo y el picking siguen en UNIDADES: el paquete
 * solo cambia cómo se ve y cómo se pide. `emp` es lo que manda el servidor
 * (`empaque_producto.Empaque.a_dict`): `{unidad, factor}`, o `null` si el
 * producto no viene en paquete. El total que cuenta lo recalcula el servidor
 * (`unidades_de_linea`): la pantalla solo ayuda a pedir.
 *
 * Decisiones del dueño: si viene en paquete, la casilla ARRANCA en paquetes
 * (salvo que no alcance uno completo: entonces en unidades); se ve cuántas
 * unidades trae el paquete y cuántas suman los elegidos; y se pueden pedir
 * unidades sueltas junto con los paquetes.
 */

/** Factor del paquete, o 0 si el producto no viene en paquete. */
function empaqueFactor(emp) {
  const f = Number(emp && emp.factor);
  return Number.isInteger(f) && f > 1 ? f : 0;
}

function _empNum(n) {
  return Number(n).toLocaleString('es-CO', { maximumFractionDigits: 0 });
}

/** «PQ × 12 und»: cuántas unidades trae el paquete. */
function empaqueEtiqueta(emp) {
  const f = empaqueFactor(emp);
  return f ? `${String(emp.unidad || 'Paquete')} × ${_empNum(f)} und` : '';
}

/** `{paquetes, sueltas}` de una cantidad en unidades. */
function empaqueDescomponer(unidades, emp) {
  const u = Math.max(0, Math.trunc(Number(unidades) || 0));
  const f = empaqueFactor(emp);
  if (!f) return { paquetes: 0, sueltas: u };
  return { paquetes: Math.floor(u / f), sueltas: u % f };
}

/** «1.368 und · 114 PQ×12», «29 und · 2 PQ×12 + 5 und»,
 *  «5 und · no alcanza un PQ×12». Sin paquete: «1.368 und». Texto plano:
 *  quien lo pinta lo escapa. */
function empaqueTexto(unidades, emp) {
  const u = Math.max(0, Math.trunc(Number(unidades) || 0));
  const f = empaqueFactor(emp);
  const base = `${_empNum(u)} und`;
  if (!f) return base;
  const unidad = String(emp.unidad || 'Paquete');
  const { paquetes, sueltas } = empaqueDescomponer(u, emp);
  if (!paquetes) return `${base} · no alcanza un ${unidad}×${_empNum(f)}`;
  return `${base} · ${_empNum(paquetes)} ${unidad}×${_empNum(f)}` + (sueltas ? ` + ${_empNum(sueltas)} und` : '');
}

/** «2 PQ + 5 und = 29 und» (o «29 und» sin paquete). Texto plano. */
function empaqueResumen(paquetes, sueltas, emp) {
  const f = empaqueFactor(emp);
  const p = Math.max(0, Math.trunc(Number(paquetes) || 0));
  const s = Math.max(0, Math.trunc(Number(sueltas) || 0));
  if (!f || !p) return `${_empNum(s)} und`;
  const unidad = String(emp.unidad || 'Paquete');
  const masSueltas = s ? ` + ${_empNum(s)} und` : '';
  return `${_empNum(p)} ${unidad}${masSueltas} = ${_empNum(p * f + s)} und`;
}

/** Cómo se pidió una línea de traslado ya guardada: lo que la persona eligió
 *  (`pedido_como`) si lo hay, si no la cantidad descompuesta con el paquete
 *  vigente. `item` es `ItemSolicitudTraslado.to_dict()`. */
function empaqueDeLinea(item, unidades) {
  const pc = item && item.pedido_como;
  const u = unidades != null ? unidades : (item && item.cantidad_solicitada);
  if (pc && empaqueFactor(pc) && Number(u) === Number(item.cantidad_solicitada)) {
    return empaqueResumen(pc.paquetes, pc.sueltas, pc);
  }
  return empaqueTexto(u, item && item.empaque);
}

/** Lo que se precarga al pintar las casillas: paquetes si alcanza uno
 *  completo, si no unidades. `inicial` (`{paquetes, sueltas}`) manda. */
function empaqueInicial(emp, disponible, inicial) {
  if (inicial) return { paquetes: inicial.paquetes || 0, sueltas: inicial.sueltas || 0 };
  const f = empaqueFactor(emp);
  if (f && Number(disponible) >= f) return { paquetes: 1, sueltas: 0 };
  return { paquetes: 0, sueltas: 1 };
}

/** Las casillas para pedir una línea. `id` solo con [A-Za-z0-9-] (va en un
 *  atributo y dentro de un `onclick`). Con paquete: [PQ] + [und] y el total en
 *  vivo; sin paquete: una casilla en unidades, como siempre. `minimo` = 0
 *  donde se puede aprobar cero (el ajuste del admin). */
function empaqueCasillasHtml(id, emp, disponible, inicial, minimo) {
  const min = minimo === 0 ? 0 : 1;
  const seguro = String(id).replace(/[^A-Za-z0-9-]/g, '-');
  const f = empaqueFactor(emp);
  const ini = empaqueInicial(emp, disponible, inicial);
  const caja = 'width:58px;padding:7px;background:var(--bg-s);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:var(--fs-sm);text-align:center;';
  if (!f) {
    return `<input type="number" min="${min}" value="${esc(ini.sueltas)}" id="${seguro}-und" aria-label="Unidades" style="${caja}">`;
  }
  const alcanza = Number(disponible) >= f;
  return `<div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px;">
    <div style="display:flex;align-items:center;gap:4px;">
      <input type="number" min="0" value="${esc(ini.paquetes)}" id="${seguro}-pq" aria-label="Paquetes"
        oninput="empaqueActualizarTotal('${seguro}', ${f})" style="${caja}">
      <span style="font-size:var(--fs-xs);color:var(--tx2);">${esc(String(emp.unidad || 'Paquete'))}</span>
      <span style="font-size:var(--fs-xs);color:var(--tx3);">+</span>
      <input type="number" min="0" value="${esc(ini.sueltas)}" id="${seguro}-und" aria-label="Unidades sueltas"
        oninput="empaqueActualizarTotal('${seguro}', ${f})" style="${caja}">
      <span style="font-size:var(--fs-xs);color:var(--tx2);">und</span>
    </div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);">${esc(empaqueEtiqueta(emp))}${alcanza ? '' : ' · no alcanza un paquete completo'}</div>
    <div id="${seguro}-total" style="font-size:var(--fs-xs);font-weight:700;color:var(--ok-tx);">= ${esc(_empNum(ini.paquetes * f + ini.sueltas))} und</div>
  </div>`;
}

/** Lo que dicen las casillas: `{paquetes, sueltas, total}` (enteros ≥ 0). */
function empaqueLeer(id, emp) {
  const seguro = String(id).replace(/[^A-Za-z0-9-]/g, '-');
  const leer = (suf) => {
    const el = typeof document !== 'undefined' ? document.getElementById(`${seguro}-${suf}`) : null;
    const n = Math.trunc(Number(el && el.value));
    return Number.isFinite(n) && n > 0 ? n : 0;
  };
  const f = empaqueFactor(emp);
  const paquetes = f ? leer('pq') : 0;
  const sueltas = leer('und');
  return { paquetes, sueltas, total: paquetes * (f || 1) + sueltas };
}

/** Reescribe «= N und» mientras la persona escribe. */
function empaqueActualizarTotal(id, factor) {
  const seguro = String(id).replace(/[^A-Za-z0-9-]/g, '-');
  const f = Math.max(1, Math.trunc(Number(factor) || 1));
  const v = (suf) => {
    const el = document.getElementById(`${seguro}-${suf}`);
    const n = Math.trunc(Number(el && el.value));
    return Number.isFinite(n) && n > 0 ? n : 0;
  };
  const el = document.getElementById(`${seguro}-total`);
  if (el) el.textContent = `= ${_empNum(v('pq') * f + v('und'))} und`;
}
