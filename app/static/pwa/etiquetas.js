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

async function etqBuscarProducto() {
  const input = document.getElementById('etq-buscar');
  const resultado = document.getElementById('etq-resultado');
  if (!input || !resultado) return;

  const codigo = (input.value || '').trim();
  if (!codigo) return;

  ETQ_PRODUCTO_ACTUAL = null;
  resultado.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">Buscando...</div>';

  let prod, enVivo = false;
  try {
    prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
  } catch (eLocal) {
    resultado.innerHTML = '<div style="text-align:center;padding:20px;color:#555;">No está en el catálogo local — consultando Siesa en vivo (puede tardar unos segundos)...</div>';
    try {
      prod = await get('/api/siesa/producto-siesa-vivo/' + encodeURIComponent(codigo));
      enVivo = true;
    } catch (eVivo) {
      resultado.innerHTML = `<div style="text-align:center;padding:20px;color:#dc2626;">${eVivo.message}</div>`;
      return;
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
