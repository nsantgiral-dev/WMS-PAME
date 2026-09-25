/**
 * 📈 Analítica — el shell del módulo (Fase 1, 2026-09-24).
 *
 * El BI de la empresa, dentro del WMS. La tesis: bajo la Ley 1116 la caja vale
 * más, y la operación convierte un pedido en caja cobrada sin fugas. Cada vista
 * contesta una parte de esa pregunta y vive en SU archivo, cargado después de
 * este:
 *
 *   analitica_portada.js     → anPortadaCargar(el, f)      🎯 ¿Cómo vamos? (abre por defecto)
 *   analitica_recorrido.js   → anRecorridoCargar(el, f)
 *   analitica_fugas.js       → anFugasCargar(el, f)
 *   analitica_diagnostico.js → anDiagnosticoCargar(el, f)  (solo admin: junta
 *       analitica_salud.js → anSaludCargar y analitica_bitacora.js →
 *       anBitacoraCargar, más los registros sin clave y cómo se mide cada cifra)
 *
 * Este archivo pone lo común: la barra de filtros (almacén, desde, hasta), la
 * sub-navegación, el cargador de paneles y los formatos. **No se refresca por
 * timer**: es de consulta, y un tablero que se repinta cada 30 s mientras
 * alguien lee una cifra le cambia la cifra debajo del dedo.
 *
 * Las vistas se llaman en runtime (flechas en `AN_VISTAS`), nunca en
 * parse-time: el orden de carga de los archivos no importa para eso.
 */

// La portada primero: la pestaña contesta «¿cómo vamos?» antes que nada. El
// juicio de confianza y el detalle técnico (salud, bitácora, sin clave, cómo
// se mide) se juntan en 🩺 Diagnóstico, que es de administración.
const AN_VISTAS = {
  portada:     { titulo: '🎯 ¿Cómo vamos?',         cargar: (el, f) => anPortadaCargar(el, f) },
  recorrido:   { titulo: '🧭 Recorrido del pedido', cargar: (el, f) => anRecorridoCargar(el, f) },
  fugas:       { titulo: '💸 Fugas',                cargar: (el, f) => anFugasCargar(el, f) },
  diagnostico: { titulo: '🩺 Diagnóstico',          cargar: (el, f) => anDiagnosticoCargar(el, f), soloAdmin: true },
};
const AN_VISTA_INICIAL = 'portada';
/** Sub-pestañas de antes que ahora viven dentro de 🩺 Diagnóstico: una
 *  preferencia guardada con esos nombres no deja la pestaña en blanco. */
const _AN_VISTAS_MOVIDAS = { salud: 'diagnostico', bitacora: 'diagnostico' };

const AN_DIAS_POR_DEFECTO = 30;
const _AN_CLAVE_SUBTAB = 'wms_an_subtab';
/** Estado de carga por panel: número de carga, parámetros y HTML a la vista. */
const _AN_CARGA = {};
let _AN_SUBTAB = null;
let _AN_ALMACENES = null;

/** Día operativo de Bogotá (UTC−5, sin horario de verano) como `YYYY-MM-DD`. */
function anHoyBogota(desplazamientoDias = 0) {
  const ms = Date.now() - 5 * 3600 * 1000 + desplazamientoDias * 86400 * 1000;
  return new Date(ms).toISOString().slice(0, 10);
}

/** Entrada a la pestaña. Pinta el shell la primera vez y abre la sub-pestaña. */
async function cargarAnalitica() {
  const raiz = document.getElementById('tab-analitica');
  if (!raiz) return;
  if (!raiz.dataset.pintado) {
    raiz.innerHTML = anShellHtml();
    raiz.dataset.pintado = '1';
    const d = document.getElementById('an-f-desde');
    const h = document.getElementById('an-f-hasta');
    if (d && !d.value) d.value = anHoyBogota(-(AN_DIAS_POR_DEFECTO - 1));
    if (h && !h.value) h.value = anHoyBogota(0);
    anCargarAlmacenes();
  }
  // Abre en la portada. Lo recordado solo vale dentro de la misma sesión de la
  // página (`_AN_SUBTAB`): al volver a entrar, la pregunta es «¿cómo vamos?».
  anSubtab(_AN_SUBTAB || AN_VISTA_INICIAL);
}

/** ¿El usuario es administrador? Diagnóstico y el recálculo son suyos. */
function anEsAdmin() {
  try { return typeof OPERARIO !== 'undefined' && !!OPERARIO && OPERARIO.rol === 'admin'; } catch (e) { return false; }
}

/** Las vistas que este usuario puede abrir. */
function anVistaPermitida(clave) {
  const v = AN_VISTAS[clave];
  return !!v && (!v.soloAdmin || anEsAdmin());
}

/** El marco: título, filtros comunes, sub-pestañas y un panel por vista. */
function anShellHtml() {
  const claves = Object.keys(AN_VISTAS);
  const tabs = claves.filter(k => anVistaPermitida(k)).map(k =>
    `<div class="subtab" id="an-tab-${esc(k)}" onclick="anSubtab('${esc(k)}')">${esc(AN_VISTAS[k].titulo)}</div>`
  ).join('');
  const paneles = claves.map(k =>
    `<div id="an-panel-${esc(k)}" style="display:none;"></div>`).join('');
  const campo = 'padding:6px 8px;border-radius:6px;border:1px solid var(--brd);'
    + 'background:var(--bg-input);color:var(--tx);font-size:var(--fs-sm);';
  return `
    <div style="font-size:var(--fs-md);font-weight:700;margin-bottom:4px;">📈 Analítica</div>
    <div style="font-size:var(--fs-xs);color:var(--tx3);margin-bottom:12px;">
      Del pedido aprobado a la plata en caja: cuánto llega, cuánto tarda y dónde se pierde, contra la meta.
      Solo lee la base del WMS y las fotos ya guardadas; no consulta Siesa.
    </div>
    <div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;margin-bottom:12px;">
      <label style="display:flex;flex-direction:column;gap:2px;font-size:var(--fs-xs);color:var(--tx2);">Almacén
        <select id="an-f-almacen" style="${campo}min-width:140px;"><option value="">Todos</option></select></label>
      <label style="display:flex;flex-direction:column;gap:2px;font-size:var(--fs-xs);color:var(--tx2);">Desde
        <input id="an-f-desde" type="date" style="${campo}"></label>
      <label style="display:flex;flex-direction:column;gap:2px;font-size:var(--fs-xs);color:var(--tx2);">Hasta
        <input id="an-f-hasta" type="date" style="${campo}"></label>
      <button onclick="anActualizar()" style="padding:7px 14px;border-radius:6px;border:1px solid var(--acento-brd);background:var(--acento-bg);color:var(--acento-tx);font-size:var(--fs-sm);font-weight:600;cursor:pointer;">Actualizar</button>
    </div>
    <div class="subtabs">${tabs}</div>
    ${paneles}`;
}

/** Las opciones del selector de almacén. Si falla, queda «Todos» y se dice. */
async function anCargarAlmacenes() {
  const sel = document.getElementById('an-f-almacen');
  if (!sel) return;
  try {
    if (!_AN_ALMACENES) _AN_ALMACENES = await get('/api/almacenes/');
    const previo = sel.value;
    sel.innerHTML = '<option value="">Todos</option>' + (_AN_ALMACENES || []).map(a =>
      `<option value="${esc(a.id)}">${esc(a.nombre || a.codigo)}</option>`).join('');
    sel.value = previo;
  } catch (e) {
    sel.title = 'No se pudo cargar la lista de almacenes: ' + (e && e.message || '');
  }
}

/** `{almacen_id, desde, hasta}` — strings `YYYY-MM-DD`; almacén '' = todos. */
function anFiltros() {
  const v = (id) => { const el = document.getElementById(id); return el ? String(el.value || '') : ''; };
  return {
    almacen_id: v('an-f-almacen'),
    desde: v('an-f-desde') || anHoyBogota(-(AN_DIAS_POR_DEFECTO - 1)),
    hasta: v('an-f-hasta') || anHoyBogota(0),
  };
}

/** Los filtros como query string, sin los vacíos. */
function anQuery(f, extra) {
  const p = Object.assign({}, f || anFiltros(), extra || {});
  return Object.keys(p).filter(k => p[k] !== '' && p[k] !== null && p[k] !== undefined)
    .map(k => `${encodeURIComponent(k)}=${encodeURIComponent(p[k])}`).join('&');
}

/** Activa una sub-pestaña, muestra su panel y la carga con los filtros de hoy. */
function anSubtab(clave) {
  if (_AN_VISTAS_MOVIDAS[clave]) clave = _AN_VISTAS_MOVIDAS[clave];
  if (!anVistaPermitida(clave)) clave = AN_VISTA_INICIAL;
  _AN_SUBTAB = clave;
  try { localStorage.setItem(_AN_CLAVE_SUBTAB, clave); } catch (e) { /* sin almacenamiento: igual funciona */ }
  Object.keys(AN_VISTAS).forEach(k => {
    const t = document.getElementById('an-tab-' + k);
    if (t) t.classList.toggle('active', k === clave);
    const p = document.getElementById('an-panel-' + k);
    if (p) p.style.display = k === clave ? 'block' : 'none';
  });
  const el = document.getElementById('an-panel-' + clave);
  if (!el) return Promise.resolve();
  try {
    // Devuelve la carga: quien salta desde otra vista (p. ej. de una fuga al
    // recorrido de su pedido) espera a que la vista exista antes de abrir.
    return Promise.resolve(AN_VISTAS[clave].cargar(el, anFiltros()));
  } catch (e) {
    el.innerHTML = `<div style="padding:20px;color:var(--err-tx);">Esta vista no está disponible: ${esc(e && e.message || 'error')}</div>`;
    return Promise.resolve();
  }
}

/** «Actualizar»: vuelve a pedir la vista abierta con los filtros del momento. */
function anActualizar() {
  anSubtab(_AN_SUBTAB || AN_VISTA_INICIAL);
}

/**
 * Carga un panel sin parpadeo y sin que una respuesta vieja pise una nueva.
 *
 * `{ el, clave, params, pedir, html, error }`:
 *   · sin «Cargando…» si ya hay datos del mismo `params` a la vista;
 *   · cada carga lleva número: si llega una más nueva antes, la vieja se tira;
 *   · si falla con datos a la vista, los conserva y avisa arriba.
 * `html: (d) => f(d)` — flecha que LLAMA al render, no el render por nombre
 * (el grafo de alcance de test_frontend_integrity solo ve llamadas).
 */
async function anCargarPanel(o) {
  const el = o.el;
  if (!el) return false;
  const est = _AN_CARGA[o.clave] || (_AN_CARGA[o.clave] = { seq: 0, el: null, params: null, html: null });
  const params = o.params === undefined ? '' : String(o.params);
  const conDatos = est.el === el && est.html !== null && est.params === params;
  const mio = ++est.seq;
  if (!conDatos) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);font-size:var(--fs-sm);">Cargando…</div>';
  }
  let d, err = null;
  try { d = await o.pedir(); } catch (e) { err = e || new Error('Error'); }
  if (mio !== est.seq) return false;
  if (err) {
    const msg = esc(err.message || 'No se pudo cargar');
    if (conDatos) {
      el.innerHTML = `<div style="padding:8px 12px;margin-bottom:8px;border-radius:6px;background:var(--warn-bg);color:var(--warn-tx);border:1px solid var(--warn-brd);font-size:var(--fs-xs);">No se pudo actualizar (${msg}). Lo que ves es la carga anterior.</div>` + est.html;
    } else {
      el.innerHTML = o.error ? o.error(err)
        : `<div style="text-align:center;padding:30px;color:var(--err-tx);">${msg}</div>`;
      est.html = null;
    }
    return false;
  }
  el.innerHTML = o.html(d);
  est.el = el; est.html = el.innerHTML; est.params = params;
  if (o.despues) o.despues(d);
  return true;
}

// ── Formatos compartidos: «no sabemos» nunca se pinta como cero ──────────────

/** `$1.234.567`, o «sin dato» si el valor no existe. */
function anPesos(n) {
  if (n === null || n === undefined || n === '' || isNaN(Number(n))) return 'sin dato';
  return '$' + Math.round(Number(n)).toLocaleString('es-CO');
}

/** Número con separador de miles (hasta un decimal), o «sin dato». */
function anNum(n) {
  if (n === null || n === undefined || n === '' || isNaN(Number(n))) return 'sin dato';
  return Number(n).toLocaleString('es-CO', { maximumFractionDigits: 1 });
}

/** «82 % de 340» — siempre con el denominador; «— de 0» si no hay base. */
function anPct(x, n) {
  const den = Number(n) || 0;
  if (!den || x === null || x === undefined || isNaN(Number(x))) return `— de ${anNum(den)}`;
  const pct = Number(x) * 100;
  return `${pct.toLocaleString('es-CO', { maximumFractionDigits: pct < 10 ? 1 : 0 })} % de ${anNum(den)}`;
}

/** Una fecha UTC del servidor (ISO sin zona) como `Date`. */
function anFechaUtc(iso) {
  if (!iso) return null;
  const s = String(iso);
  const d = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z');
  return isNaN(d.getTime()) ? null : d;
}

/** Hora y día de Bogotá de un ISO UTC: «hoy 14:02» o «2026-09-23 14:02». */
function anCuando(iso) {
  const d = anFechaUtc(iso);
  if (!d) return 'sin fecha';
  const dia = new Date(d.getTime() - 5 * 3600 * 1000).toISOString().slice(0, 10);
  const hora = d.toLocaleTimeString('es-CO', { timeZone: 'America/Bogota', hour: '2-digit', minute: '2-digit' });
  return dia === anHoyBogota(0) ? `hoy ${hora}` : `${dia} ${hora}`;
}

/**
 * La línea chica de frescura: de cuándo es el dato y si la fuente estaba
 * completa. Una fuente con `completa === false` se nombra con su motivo; no se
 * resume en «fuente incompleta» a secas, porque eso no dice qué falta.
 *
 * **Una línea, no un bloque** (2026-09-24): los avisos de calidad del dato
 * pegados encima de cada cifra tapaban la cifra. La línea dice cuántas fuentes
 * están incompletas y el detalle se abre al tocarla. El juicio de confianza
 * completo vive en 🎯 ¿Cómo vamos? (arriba) y en 🩺 Diagnóstico.
 */
function anFrescura(meta) {
  if (!meta) return '';
  const fuentes = meta.fuentes || {};
  const incompletas = Object.keys(fuentes).map(k => fuentes[k])
    .filter(f => f && f.completa === false);
  const base = `Dato de ${esc(anCuando(meta.calculado_en))}`;
  if (!incompletas.length) {
    return `<div style="font-size:var(--fs-xs);color:var(--tx3);margin:4px 0 10px;">${base} · fuentes completas</div>`;
  }
  const avisos = incompletas.map(f =>
    `<div style="margin-top:2px;">⚪ <b>${esc(f.nombre || 'Fuente sin nombre')}</b>: ${esc(f.motivo || 'incompleta, sin motivo declarado')}</div>`).join('');
  const n = incompletas.length;
  return `<details style="font-size:var(--fs-xs);color:var(--tx3);margin:4px 0 10px;">
    <summary style="cursor:pointer;">${base} · ${esc(anNum(n))} ${n === 1 ? 'fuente incompleta' : 'fuentes incompletas'} ›</summary>
    <div style="color:var(--tx2);padding:4px 0 0 14px;">${avisos}</div></details>`;
}
