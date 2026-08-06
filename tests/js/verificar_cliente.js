// Ejercita la LÓGICA de cliente con payloads reales del servidor.
//
// Los tests de Python verifican que las funciones EXISTAN y que el servidor
// responda lo que ellas leen. Esto verifica lo del medio: que la función,
// alimentada con la respuesta real, produzca lo correcto.
//
// Es la franja donde vivían los tres bugs de esta semana —la placa que se leía
// de una global, el service worker cacheando /flota/, los dos rechazos 409
// indistinguibles—: cada pieza estaba bien y la unión no.
//
// Se corre desde `tests/test_cliente_js.py`, que falla si node no está.
const fs = require('fs');
const OK = [], FALLA = [];
const chk = (n, c, d = '') => (c ? OK : FALLA).push(`${n}  ${d}`.trim());

// DOM mínimo: solo lo que estas funciones tocan.
const nodos = {};
const mkEl = id => ({ id, innerHTML: '', dataset: {}, style: {}, value: '' });
global.document = {
  getElementById: id => (nodos[id] = nodos[id] || mkEl(id)),
  createElement: () => mkEl('x'),
};
global.window = { open: () => null };
global.navigator = { userAgent: 'node-test', onLine: true, vibrate: () => {} };
global.API = '';
global.TOKEN = 't';
global.alerta = (m, t) => { global._ultimaAlerta = [t, m]; };

// El `get` devuelve el payload REAL que verificamos contra PostgreSQL.
const RESPUESTAS = {};
global.get = async url => {
  for (const k of Object.keys(RESPUESTAS)) if (url.startsWith(k)) return RESPUESTAS[k];
  throw new Error('sin stub para ' + url);
};

const vm = require('vm');
const cargar = f => {
  let src = fs.readFileSync(f, 'utf8');
  // `const`/`let` de nivel superior no quedan en globalThis. En el navegador
  // sí son alcanzables entre módulos porque comparten el mismo scope de script;
  // acá se convierten a `var` para reproducir ese comportamiento.
  src = src.replace(/^(const|let) /gm, 'var ');
  vm.runInThisContext(src, { filename: f });
};
const RAIZ = require('path').resolve(__dirname, '..', '..');
cargar(RAIZ + '/app/static/pwa/compras_ia.js');
cargar(RAIZ + '/app/static/pwa/rutas.js');

(async () => {
  // ── repoVerEvidencia: 10 días, agotado del 4 al 6 de julio ────────────
  RESPUESTAS['/api/kardex/stock-diario'] = {
    referencia: 'SKU-V', bodega: 'todas',
    dias: Array.from({ length: 10 }, (_, i) => ({
      fecha: `2026-07-${String(i + 1).padStart(2, '0')}`,
      bodega: 'NB1', stock_cierre: (i >= 3 && i <= 5) ? 0 : 20,
      tuvo_stock: !(i >= 3 && i <= 5),
    })),
  };
  const fila = document.getElementById('rep-ev-0');
  await repoVerEvidencia('SKU-V', 'rep-ev-0');
  const h = fila.innerHTML;
  chk('evidencia: cuenta los días sin stock', h.includes('3 día-bodega SIN stock'));
  chk('evidencia: agrupa en UNA racha', h.includes('en 1 racha(s)'));
  chk('evidencia: la racha dura 3 días', h.includes('(3d)'), h.match(/\(\d+d\)/) || '');
  // El formato real de es-CO es «1 de jul» — el locale ignora day:'2-digit'.
  // La primera versión de este chequeo asumía '01 jul' y falló: la aserción
  // estaba mal, no el código.
  chk('evidencia: rango de fechas', h.includes('1 de jul') && h.includes('10 de jul'),
      (h.match(/\d+ de \w+/g) || []).slice(0, 2).join(' → '));
  chk('evidencia: nombra la bodega', h.includes('NB1'));
  chk('evidencia: NO avisa truncado con 10 filas', !h.includes('no cubre el año entero'));
  chk('evidencia: queda marcada como abierta', fila.dataset.abierto === '1');

  // Segundo toque = cierra.
  await repoVerEvidencia('SKU-V', 'rep-ev-0');
  chk('evidencia: el segundo toque la cierra', fila.innerHTML === '' && fila.dataset.abierto === '0');

  // ── Tope de 365: con 5 bodegas la ventana se recorta ──────────────────
  RESPUESTAS['/api/kardex/stock-diario'] = {
    dias: Array.from({ length: 365 }, (_, i) => ({
      fecha: `2026-0${(i % 9) + 1}-${String((i % 28) + 1).padStart(2, '0')}`,
      bodega: 'NB' + (i % 5), stock_cierre: 5, tuvo_stock: true,
    })),
  };
  const f2 = document.getElementById('rep-ev-1');
  await repoVerEvidencia('SKU-V', 'rep-ev-1');
  chk('evidencia: DECLARA el tope de 365', f2.innerHTML.includes('no cubre el año entero'));

  // ── Sin serie reconstruida ────────────────────────────────────────────
  RESPUESTAS['/api/kardex/stock-diario'] = { dias: [] };
  const f3 = document.getElementById('rep-ev-2');
  await repoVerEvidencia('SKU-V', 'rep-ev-2');
  chk('evidencia: sin serie dice CENSURADA y manda a arreglarlo',
      f3.innerHTML.includes('CENSURADA') && f3.innerHTML.includes('Reconstruir stock diario'));

  // ── Semáforo del kardex ───────────────────────────────────────────────
  RESPUESTAS['/api/kardex/reconciliar'] = {
    compuerta_ok: true, total_registros_kardex: 1234, conceptos_desconocidos: [],
  };
  await modelosSemaforoKardex();
  chk('semáforo verde con compuerta abierta',
      document.getElementById('modelos-semaforo').innerHTML.includes('Kardex completo'));
  RESPUESTAS['/api/kardex/reconciliar'] = {
    compuerta_ok: false, total_registros_kardex: 9, conceptos_desconocidos: [77, 88],
  };
  await modelosSemaforoKardex();
  const s = document.getElementById('modelos-semaforo').innerHTML;
  chk('semáforo rojo nombra cuántos conceptos', s.includes('2 concepto(s)'));
  chk('semáforo rojo manda a Datos', s.includes('Inventario › Datos'));

  // ── Manifiesto: el escape de lo que viene de la base ──────────────────
  RESPUESTAS['/api/muelle/manifiesto'] = {
    fecha: '2026-08-06', total_bultos: 2,
    manifiesto: [{ destino: 'Neiva', total_bultos: 2, pedidos: [{
      numero_pedido: 'PD-900', cliente: 'Cliente <de prueba> & Cía',
      total_bultos: 2, resumen: { Caja: 2 },
      bultos: [{ codigo_barras: 'B1', tipo: 'Caja', numero: 1, total: 2 }],
    }] }],
  };
  let escrito = '';
  global.window.open = () => ({ document: { write: s => { escrito = s; }, close() {} } });
  await muelleImprimirManifiesto();
  chk('manifiesto: escapa < > &',
      escrito.includes('Cliente &lt;de prueba&gt; &amp; Cía'),
      escrito.includes('<de prueba>') ? 'SIN ESCAPAR' : '');
  chk('manifiesto: deja columna de firma', escrito.includes('Recibido') && escrito.includes('class="firma"'));
  chk('manifiesto: pie con las tres firmas',
      escrito.includes('Entregó (bodega)') && escrito.includes('Recibió (conductor)') && escrito.includes('Placa'));

  // Vacío: no imprime papel en blanco.
  RESPUESTAS['/api/muelle/manifiesto'] = { manifiesto: [], fecha: 'x', total_bultos: 0 };
  escrito = '';
  await muelleImprimirManifiesto();
  chk('manifiesto: no imprime vacío', escrito === '' && /advertencia/.test(String(global._ultimaAlerta)));

  console.log(OK.map(x => '  OK   ' + x).join('\n'));
  if (FALLA.length) console.log(FALLA.map(x => '  FALLA ' + x).join('\n'));
  console.log(`\n${OK.length} verificaciones de cliente OK, ${FALLA.length} fallas`);
  process.exit(FALLA.length ? 1 : 0);
})();
