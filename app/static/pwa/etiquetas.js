/**
 * Etiquetas de producto — búsqueda + generación + impresión de código de barras.
 *
 * Módulo aislado (SRP): no comparte estado ni funciones con app.js. Solo
 * consume helpers globales ya expuestos por app.js (API, TOKEN, get, alerta)
 * y el contenedor compartido #print-area que ya usan las etiquetas de
 * LPN/bulto/canasto — mismo contrato, sin tocar su lógica.
 *
 * Backend: intenta primero GET /api/siesa/producto/<codigo> (catálogo local,
 * rápido). Si el ítem aún no sincronizó (recién creado en Siesa), cae a
 * GET /api/siesa/producto-siesa-vivo/<codigo> — consulta en vivo a Connekta,
 * exclusiva de esta herramienta. Ninguna de las dos rutas usadas por
 * picking/packing en caliente se modificó.
 */

let ETQ_PRODUCTO_ACTUAL = null;
let _ETQ_SUGERIR_TIMER = null;

function _etqEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[ch]);
}

/** Filas clicables de productos (sugerencias mientras se escribe y lista de
 * coincidencias por descripción). Elegir una fila busca ese código exacto. */
function _etqFilasHtml(productos) {
  return productos.map(p => {
    const barras = p.codigo_barras ? ' · ' + _etqEsc(p.codigo_barras) : '';
    return `
      <div onclick="etqElegir(this.dataset.codigo)" data-codigo="${_etqEsc(p.codigo)}"
        style="padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--brd);font-size:13px;"
        onmouseover="this.style.background='var(--bg-input)'" onmouseout="this.style.background=''">
        <div style="font-weight:700;color:var(--tx);">${_etqEsc(p.codigo)}${barras}</div>
        <div style="color:var(--tx3);font-size:12px;">${_etqEsc(p.nombre || '')}</div>
      </div>`;
  }).join('');
}

function etqOcultarSugerencias() {
  const box = document.getElementById('etq-sugerencias');
  if (box) { box.style.display = 'none'; box.innerHTML = ''; }
}

/** Autocompletado por código, descripción o código de barras
 * (GET /api/productos/?q=, el mismo endpoint del catálogo general). */
function etqSugerir(valor) {
  clearTimeout(_ETQ_SUGERIR_TIMER);
  const q = (valor || '').trim();
  const box = document.getElementById('etq-sugerencias');
  if (!box) return;
  if (q.length < 2) { etqOcultarSugerencias(); return; }

  _ETQ_SUGERIR_TIMER = setTimeout(async () => {
    let productos = [];
    try {
      const d = await get('/api/productos/?q=' + encodeURIComponent(q) + '&per_page=8');
      productos = d.productos || [];
    } catch (e) { etqOcultarSugerencias(); return; }
    if (!productos.length) { etqOcultarSugerencias(); return; }
    box.innerHTML = _etqFilasHtml(productos);
    box.style.display = 'block';
  }, 250);
}

function etqElegir(codigo) {
  const input = document.getElementById('etq-buscar');
  if (input) input.value = codigo;
  etqOcultarSugerencias();
  etqBuscarProducto();
}

/** Coincidencias por descripción/código parcial en el catálogo local. */
async function _etqBuscarPorDescripcion(q) {
  try {
    const d = await get('/api/productos/?q=' + encodeURIComponent(q) + '&per_page=20');
    return d.productos || [];
  } catch (e) { return []; }
}

async function etqBuscarProducto() {
  const input = document.getElementById('etq-buscar');
  const resultado = document.getElementById('etq-resultado');
  if (!input || !resultado) return;

  const codigo = (input.value || '').trim();
  if (!codigo) return;

  etqOcultarSugerencias();
  ETQ_PRODUCTO_ACTUAL = null;
  resultado.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Buscando...</div>';

  let prod, enVivo = false;
  try {
    prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
  } catch (eLocal) {
    // Ni código WMS, ni referencia, ni EAN exactos. Si no es solo dígitos
    // (id de ítem / EAN), puede ser una descripción: se ofrece la lista del
    // catálogo local ANTES de ir a Siesa en vivo, que es lento.
    if (!/^\d+$/.test(codigo)) {
      const coincidencias = await _etqBuscarPorDescripcion(codigo);
      if (coincidencias.length) {
        resultado.innerHTML = `
          <div style="font-size:12px;color:var(--tx3);margin-bottom:6px;">
            ${coincidencias.length} coincidencia(s) por descripción — elige una:
          </div>
          <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;max-width:480px;overflow:hidden;">
            ${_etqFilasHtml(coincidencias)}
          </div>`;
        return;
      }
    }
    resultado.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">No está en el catálogo local — consultando Siesa en vivo (puede tardar unos segundos)...</div>';
    try {
      prod = await get('/api/siesa/producto-siesa-vivo/' + encodeURIComponent(codigo));
      enVivo = true;
    } catch (eVivo) {
      resultado.innerHTML = `<div style="text-align:center;padding:20px;color:#dc2626;">${_etqEsc(eVivo.message)}</div>`;
      return;
    }
    // La consulta por id de ítem (ej. "260") entra por la ruta en vivo aunque
    // el producto SÍ esté en el catálogo local bajo su referencia. Si está,
    // se usa el local y no se marca "aún no sincronizado" — sería falso.
    if (prod && prod.codigo_siesa) {
      try {
        prod = await get('/api/siesa/producto/' + encodeURIComponent(prod.codigo_siesa));
        enVivo = false;
      } catch (eNoLocal) { /* de verdad no está sincronizado: se queda en vivo */ }
    }
  }

  if (!prod.codigo_siesa) {
    resultado.innerHTML = `<div style="text-align:center;padding:20px;color:#d97706;">
      ${prod.nombre || prod.codigo} no tiene código Siesa registrado — no se puede generar la etiqueta.
    </div>`;
    return;
  }

  // Preferir el EAN real (API_v2_ItemsBarras). La referencia (codigo_siesa,
  // ej. 'ARTESA898') es el SKU interno — solo se usa como respaldo cuando
  // Siesa no tiene un EAN asignado para el ítem.
  const codigoParaBarra = prod.codigo_barras || prod.codigo_siesa;
  const esReferenciaFallback = !prod.codigo_barras;

  ETQ_PRODUCTO_ACTUAL = { ...prod, codigo_para_barra: codigoParaBarra };
  resultado.innerHTML = `
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:16px;max-width:320px;">
      ${enVivo ? '<div style="font-size:11px;color:#f59e0b;font-weight:700;margin-bottom:6px;">🔴 EN VIVO — aún no sincronizado en el catálogo local</div>' : ''}
      <div style="font-size:14px;font-weight:700;margin-bottom:4px;">${prod.nombre || ''}</div>
      <div style="font-size:12px;color:var(--tx3);margin-bottom:2px;">Referencia Siesa: ${prod.codigo_siesa}</div>
      <div style="font-size:12px;color:var(--tx3);margin-bottom:10px;">
        ${esReferenciaFallback
          ? '<span style="color:#d97706;">Sin EAN en Siesa — se imprimirá la referencia interna</span>'
          : 'Código de barras EAN: ' + prod.codigo_barras}
      </div>
      <svg id="etq-preview-svg" style="width:100%;height:60px;"></svg>
      <button onclick="etqImprimir()"
        style="width:100%;margin-top:10px;padding:10px;background:var(--pm);border:none;border-radius:8px;color:#fff;font-size:13px;font-weight:700;cursor:pointer;">
        🖨 Imprimir etiqueta
      </button>
    </div>`;

  // La vista previa es donde el problema tiene que verse: si acá no hay barra,
  // el botón de imprimir tampoco va a producir una.
  if (!pintarCodigoBarras('#etq-preview-svg', codigoParaBarra,
                          { displayValue: true, height: 50, fontSize: 12 })) {
    const svg = document.getElementById('etq-preview-svg');
    if (svg) svg.outerHTML =
      '<div style="padding:10px;border:1px dashed #dc2626;border-radius:6px;'
      + 'color:#dc2626;font-size:11px;text-align:center;">No cargó el generador '
      + 'de códigos de barras — recargá la página (Ctrl+F5)</div>';
  }
}

function etqImprimir() {
  const prod = ETQ_PRODUCTO_ACTUAL;
  if (!prod || !prod.codigo_para_barra) return;
  if (!puedeImprimirEtiquetas('etiquetas de producto')) return;

  const area = document.getElementById('print-area');
  if (!area) return;

  const hoy = new Date().toLocaleDateString('es-CO');
  const uid = `etq-bc-${Date.now()}`;

  area.innerHTML = `
    <div class="etiqueta-lpn">
      <div class="el-titulo">PRODUCTO — PAPELERÍA MEDELLÍN</div>
      <svg id="${uid}"></svg>
      <div class="el-codigo">${prod.codigo_para_barra}</div>
      <div class="el-producto">${prod.nombre || ''}</div>
      <div class="el-fecha">${hoy}</div>
    </div>`;

  pintarCodigoBarras(`#${uid}`, prod.codigo_para_barra);

  setTimeout(() => {
    window.print();
    setTimeout(() => { area.innerHTML = ''; }, 1000);
  }, 300);
}
