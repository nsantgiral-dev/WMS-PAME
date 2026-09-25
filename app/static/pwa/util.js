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
