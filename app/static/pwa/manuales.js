/**
 * Manual de Usuario — un PDF por rol, servido como archivo estático desde
 * app/static/manuales/ (sin backend nuevo). Ver abre el PDF en pestaña
 * nueva (el navegador lo renderiza solo, sin librería); Descargar fuerza
 * la descarga con el atributo `download` del propio HTML.
 *
 * Para agregar un manual nuevo (Abastecedor, Conductor, etc.): agregar el
 * PDF a app/static/manuales/ y una línea a este arreglo — nada más.
 */
const MANUALES = [
  { rol: 'Picker (Operario de Picking)', bodega: 'NB1', archivo: '/static/manuales/manual_picker_nb1.pdf' },
  { rol: 'Empacador', bodega: 'NB1', archivo: '/static/manuales/manual_empacador_nb1.pdf' },
];

function cargarManuales() {
  const el = document.getElementById('manuales-lista');
  if (!el) return;

  if (!MANUALES.length) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:var(--tx3);">Todavía no hay manuales publicados</div>';
    return;
  }

  el.innerHTML = MANUALES.map(m => `
    <div class="tabla-fila">
      <div>
        <div class="tabla-nombre">${m.rol}</div>
        <div style="font-size:11px;color:var(--tx3);">Bodega ${m.bodega}</div>
      </div>
      <div style="display:flex;gap:8px;">
        <a href="${m.archivo}" target="_blank" rel="noopener"
           style="padding:8px 14px;background:var(--pm);border-radius:8px;color:#fff;font-size:13px;font-weight:700;text-decoration:none;">
          Ver PDF
        </a>
        <a href="${m.archivo}" download
           style="padding:8px 14px;background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:13px;font-weight:700;text-decoration:none;">
          Descargar
        </a>
      </div>
    </div>`).join('');
}
