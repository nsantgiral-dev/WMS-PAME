/**
 * Etiquetas de producto — búsqueda + generación + impresión de código de barras.
 *
 * Módulo aislado (SRP): no comparte estado ni funciones con app.js. Solo
 * consume helpers globales ya expuestos por app.js (API, TOKEN, get, alerta)
 * y el contenedor compartido #print-area que ya usan las etiquetas de
 * LPN/bulto/canasto — mismo contrato, sin tocar su lógica.
 *
 * Backend: reutiliza GET /api/siesa/producto/<codigo> (routes/siesa.py),
 * que ya busca por codigo, codigo_siesa o codigo_barras. Cero endpoints
 * nuevos, cero escritura en BD.
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

  let prod;
  try {
    prod = await get('/api/siesa/producto/' + encodeURIComponent(codigo));
  } catch (e) {
    resultado.innerHTML = `<div style="text-align:center;padding:20px;color:#dc2626;">${e.message}</div>`;
    return;
  }

  if (!prod.codigo_siesa) {
    resultado.innerHTML = `<div style="text-align:center;padding:20px;color:#d97706;">
      ${prod.nombre || prod.codigo} no tiene código Siesa registrado — no se puede generar la etiqueta.
    </div>`;
    return;
  }

  ETQ_PRODUCTO_ACTUAL = prod;
  resultado.innerHTML = `
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:10px;padding:16px;max-width:320px;">
      <div style="font-size:14px;font-weight:700;margin-bottom:4px;">${prod.nombre || ''}</div>
      <div style="font-size:12px;color:var(--tx3);margin-bottom:10px;">Código Siesa: ${prod.codigo_siesa}</div>
      <svg id="etq-preview-svg" style="width:100%;height:60px;"></svg>
      <button onclick="etqImprimir()"
        style="width:100%;margin-top:10px;padding:10px;background:var(--pm);border:none;border-radius:8px;color:#fff;font-size:13px;font-weight:700;cursor:pointer;">
        🖨 Imprimir etiqueta
      </button>
    </div>`;

  try {
    JsBarcode('#etq-preview-svg', prod.codigo_siesa, {
      format: 'CODE128', displayValue: true, height: 50, margin: 0, fontSize: 12
    });
  } catch (_) {}
}

function etqImprimir() {
  const prod = ETQ_PRODUCTO_ACTUAL;
  if (!prod || !prod.codigo_siesa) return;

  const area = document.getElementById('print-area');
  if (!area) return;

  const hoy = new Date().toLocaleDateString('es-CO');
  const uid = `etq-bc-${Date.now()}`;

  area.innerHTML = `
    <div class="etiqueta-lpn">
      <div class="el-titulo">PRODUCTO — PAPELERÍA MEDELLÍN</div>
      <svg id="${uid}"></svg>
      <div class="el-codigo">${prod.codigo_siesa}</div>
      <div class="el-producto">${prod.nombre || ''}</div>
      <div class="el-fecha">${hoy}</div>
    </div>`;

  try {
    JsBarcode(`#${uid}`, prod.codigo_siesa, {
      format: 'CODE128', displayValue: false, height: 55, margin: 0
    });
  } catch (_) {}

  setTimeout(() => {
    window.print();
    setTimeout(() => { area.innerHTML = ''; }, 1000);
  }, 300);
}
