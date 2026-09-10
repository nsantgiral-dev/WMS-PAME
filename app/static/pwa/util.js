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
