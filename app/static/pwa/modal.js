/**
 * El modal propio de la app: `_modalConfirmar`, `_modalTexto`, `_modalCantidad`.
 * Se carga después de `util.js` y antes de `app.js`.
 *
 * ## Por qué es un archivo aparte (2026-09-25)
 *
 * Vivía en `app.js`, y los arneses de Node de flota cargan `util.js` +
 * `flota.js` sin `app.js`: por eso flota se había quedado con 18
 * `confirm()`/`prompt()` nativos —en el teléfono un diálogo del sistema
 * operativo que corta el texto largo y que un toque distraído contesta
 * «Aceptar»—, incluido el tanqueo sin foto del recibo, que es plata.
 *
 * No va en `util.js` a propósito: los arneses que prueban una decisión
 * **stubbean** el modal (guionan qué contesta la persona) y `util.js` se carga
 * de verdad en todos; si el modal viviera ahí, sus declaraciones pisarían los
 * stubs. Un arnés que quiera el modal real carga este archivo.
 *
 * El `mensajeHtml` se inserta tal cual: todo dato que vaya adentro, con `esc()`.
 */

/**
 * Modal propio para capturar una cantidad numérica — reemplaza `prompt()`.
 * Teclado numérico garantizado (`inputmode="numeric"`), valida el rango
 * antes de dejar confirmar (no substituye silenciosamente un valor inválido
 * como hacía `parseInt(x) || default`), y con un solo paso en vez de
 * encadenar `prompt()` + `confirm()`.
 * @param {string} titulo
 * @param {string} mensajeHtml - se inserta tal cual, permite HTML simple
 * @param {Object} [opts]
 * @param {number} [opts.min=1]
 * @param {number} [opts.max]
 * @param {number|string} [opts.valorInicial='']
 * @param {string} [opts.textoConfirmar='Confirmar']
 * @param {string} [opts.textoCancelar='Cancelar']
 * @returns {Promise<number|null>} la cantidad, o null si se canceló
 */
function _modalCantidad(titulo, mensajeHtml, opts = {}) {
  const {
    min = 1, max, valorInicial = '',
    textoConfirmar = 'Confirmar', textoCancelar = 'Cancelar',
  } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:360px;border:1px solid var(--brd);">
        <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);margin-bottom:10px;">${titulo}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:16px;line-height:1.5;">${mensajeHtml}</div>
        <input id="_mc-input" type="number" inputmode="numeric"
          ${min != null ? `min="${min}"` : ''} ${max != null ? `max="${max}"` : ''} value="${valorInicial}"
          style="width:100%;padding:14px;font-size:var(--fs-xl);font-weight:700;background:var(--bg-s);border:2px solid var(--brd);border-radius:10px;color:var(--tx);text-align:center;margin-bottom:6px;box-sizing:border-box;">
        <div id="_mc-error" style="font-size:var(--fs-xs);color:var(--err-tx);min-height:16px;margin-bottom:10px;"></div>
        <div style="display:flex;gap:10px;">
          <button id="_mc-no" style="flex:1;padding:14px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoCancelar}</button>
          <button id="_mc-si" style="flex:1;padding:14px;background:var(--pm-fill);color:#fff;border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoConfirmar}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#_mc-input');
    const errEl = overlay.querySelector('#_mc-error');
    const cerrar = valor => { overlay.remove(); resolve(valor); };
    const intentarConfirmar = () => {
      const val = parseInt(input.value, 10);
      if (isNaN(val) || val < min || (max != null && val > max)) {
        errEl.textContent = max != null ? `Debe ser un número entre ${min} y ${max}` : `Debe ser un número desde ${min}`;
        input.focus();
        return;
      }
      cerrar(val);
    };
    overlay.querySelector('#_mc-si').onclick = intentarConfirmar;
    overlay.querySelector('#_mc-no').onclick = () => cerrar(null);
    input.addEventListener('keydown', e => { if (e.key === 'Enter') intentarConfirmar(); });
    input.focus();
    input.select();
  });
}

/**
 * Modal de confirmación compartido — reemplazo de `confirm()` nativo, que en
 * iOS/Android bloquea el hilo con un diálogo del sistema operativo (no
 * estilizable, texto largo se corta) en vez de la UI de la app.
 * @param {string} mensajeHtml
 * @param {{titulo?:string, textoConfirmar?:string, textoCancelar?:string, peligro?:boolean}} [opts]
 * @returns {Promise<boolean>}
 */
function _modalConfirmar(mensajeHtml, opts = {}) {
  const {
    titulo = '¿Confirmar?', textoConfirmar = 'Confirmar', textoCancelar = 'Cancelar',
    peligro = false,
  } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:400px;border:1px solid var(--brd);max-height:80vh;overflow-y:auto;">
        <div style="font-size:17px;font-weight:800;color:var(--tx);margin-bottom:10px;">${titulo}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx);margin-bottom:20px;line-height:1.5;white-space:pre-line;">${mensajeHtml}</div>
        <div style="display:flex;gap:10px;">
          <button id="_mconf-no" style="flex:1;padding:14px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoCancelar}</button>
          <button id="_mconf-si" style="flex:1;padding:14px;background:${peligro ? '#7f1d1d' : 'var(--pm-fill)'};color:#fff;border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoConfirmar}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const cerrar = valor => { overlay.remove(); resolve(valor); };
    overlay.querySelector('#_mconf-si').onclick = () => cerrar(true);
    overlay.querySelector('#_mconf-no').onclick = () => cerrar(false);
  });
}

/**
 * Modal propio para capturar texto libre — reemplaza `prompt()` para motivos,
 * observaciones, etc. Mismo patrón que `_modalCantidad`/`_modalConfirmar`.
 * @param {string} titulo
 * @param {string} mensajeHtml
 * @param {{obligatorio?:boolean, valorInicial?:string, placeholder?:string, textoConfirmar?:string, textoCancelar?:string}} [opts]
 * @returns {Promise<string|null>} el texto, o null si se canceló
 */
function _modalTexto(titulo, mensajeHtml, opts = {}) {
  const {
    obligatorio = true, valorInicial = '', placeholder = '',
    textoConfirmar = 'Confirmar', textoCancelar = 'Cancelar',
  } = opts;
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px;';
    overlay.innerHTML = `
      <div style="background:var(--bg-s);border-radius:16px;padding:24px;width:100%;max-width:400px;border:1px solid var(--brd);">
        <div style="font-size:var(--fs-lg);font-weight:800;color:var(--tx);margin-bottom:10px;">${titulo}</div>
        <div style="font-size:var(--fs-sm);color:var(--tx2);margin-bottom:16px;line-height:1.5;">${mensajeHtml}</div>
        <textarea id="_mt-input" placeholder="${placeholder}" rows="3"
          style="width:100%;padding:12px;font-size:var(--fs-md);background:var(--bg-s);border:2px solid var(--brd);border-radius:10px;color:var(--tx);margin-bottom:6px;box-sizing:border-box;font-family:inherit;resize:vertical;">${valorInicial}</textarea>
        <div id="_mt-error" style="font-size:var(--fs-xs);color:var(--err-tx);min-height:16px;margin-bottom:10px;"></div>
        <div style="display:flex;gap:10px;">
          <button id="_mt-no" style="flex:1;padding:14px;background:var(--bg-input);color:var(--tx2);border:1px solid var(--brd);border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoCancelar}</button>
          <button id="_mt-si" style="flex:1;padding:14px;background:var(--pm-fill);color:#fff;border:none;border-radius:10px;font-size:var(--fs-sm);font-weight:700;cursor:pointer;">${textoConfirmar}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const input = overlay.querySelector('#_mt-input');
    const errEl = overlay.querySelector('#_mt-error');
    const cerrar = valor => { overlay.remove(); resolve(valor); };
    const intentarConfirmar = () => {
      const val = input.value.trim();
      if (obligatorio && !val) {
        errEl.textContent = 'Este campo es obligatorio';
        input.focus();
        return;
      }
      cerrar(val);
    };
    overlay.querySelector('#_mt-si').onclick = intentarConfirmar;
    overlay.querySelector('#_mt-no').onclick = () => cerrar(null);
    input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); intentarConfirmar(); } });
    input.focus();
  });
}
