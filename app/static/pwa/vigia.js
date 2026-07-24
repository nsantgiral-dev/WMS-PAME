/* vigia.js — Panel del Vigía CUSUM
 * Monitoreo de corrimientos operativos por C.O.
 * Jerarquía visual: facturas (frecuencia) > despachos (volumen) > facturacion (valor)
 */
'use strict';

let _VIGIA_CHART = null;
let _VIGIA_SERIE_ACTIVA = null;

const _VIGIA_TIPO_ORDEN = ['planillas', 'facturas', 'despachos', 'facturacion'];
const _VIGIA_TIPO_LABEL = {
  planillas: 'Planillas por corredor',
  facturas: 'Facturas unicas',
  despachos: 'Lineas despachadas',
  facturacion: 'Facturacion',
};
const _VIGIA_TIPO_ICON = {
  planillas: '📋',
  facturas: '🔔',
  despachos: '📦',
  facturacion: '💰',
};
const _VIGIA_TIPO_DESC = {
  planillas: 'Frecuencia de ruta por corredor — serie reina, detecta primero',
  facturas: 'Frecuencia de servicio — proxy de planillas',
  despachos: 'Volumen por visita — confirma',
  facturacion: 'Valor monetario — confirma',
};

async function cargarVigia() {
  const el = document.getElementById('vigia-container');
  if (!el) return;

  el.innerHTML = '<div style="color:var(--tx3);padding:20px;">Cargando Vigia...</div>';

  try {
    const [d, salud] = await Promise.all([
      get('/api/vigia/resumen'),
      get('/api/vigia/salud').catch(() => null),
    ]);
    _vigiaRenderPanel(el, d, salud);
  } catch (e) {
    el.innerHTML = `<div style="color:var(--red);padding:20px;">Error: ${e.message || e}</div>`;
  }
}

function _vigiaRenderPanel(el, data, salud) {
  const { cos, alarmas, alarmas_resumen, total_series } = data;
  const coKeys = Object.keys(cos).sort();
  const nAlarmas = alarmas_resumen.ALARMA || 0;
  const nAvisos = alarmas_resumen.AVISO || 0;

  let html = '';

  // Banner G0: latencia de conectores
  if (salud && !salud.g0_ok) {
    html += `<div style="background:#F8717122;border:2px solid var(--red);border-radius:8px;padding:12px;margin-bottom:12px;">
      <div style="font-weight:700;color:var(--red);font-size:13px;margin-bottom:6px;">G0 — Datos desactualizados</div>`;
    for (const c of (salud.conectores || [])) {
      if (!c.ok) {
        const latencia = c.latencia_horas != null ? `${c.latencia_horas}h` : `${c.latencia_dias}d`;
        html += `<div style="font-size:12px;color:var(--tx);margin-left:8px;">
          <span style="color:var(--red);">●</span> ${c.nombre}: ultima sync hace ${latencia}
        </div>`;
      }
    }
    html += '</div>';
  } else if (salud && salud.conectores && salud.conectores.length > 0) {
    html += `<div style="font-size:11px;color:var(--green);margin-bottom:8px;">
      G0 ● Conectores OK — datos actualizados
    </div>`;
  }

  // Header con resumen de alarmas
  html += '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;">';
  html += _vigiaBadgeResumen(nAlarmas, 'ALARMA', 'var(--red)');
  html += _vigiaBadgeResumen(nAvisos, 'AVISO', 'var(--yellow)');
  html += `<div style="flex:1;text-align:right;color:var(--tx3);font-size:12px;align-self:center;">
    ${total_series} series | ${coKeys.length} C.O.s
  </div>`;
  html += '</div>';

  // Alarmas abiertas (si hay)
  if (alarmas.length > 0) {
    html += '<div style="margin-bottom:16px;">';
    html += '<div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">Alarmas abiertas</div>';
    html += '<div style="display:flex;flex-direction:column;gap:6px;">';
    for (const a of alarmas) {
      const esAlarma = a.severidad === 'ALARMA';
      const color = esAlarma ? 'var(--red)' : 'var(--yellow)';
      const border = esAlarma ? '2px solid var(--red)' : '1px solid var(--yellow)';
      const dir = a.tipo === 'BAJA' ? '↓' : '↑';
      html += `<div style="background:var(--bg-s);border:${border};border-radius:8px;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;">
        <div>
          <span style="color:${color};font-weight:700;font-size:13px;">${a.severidad} ${dir}</span>
          <span style="color:var(--tx);font-size:13px;margin-left:8px;">${a.serie}</span>
          <span style="color:var(--tx3);font-size:11px;margin-left:8px;">${a.semana}</span>
          <span style="color:var(--tx3);font-size:11px;margin-left:4px;">S=${a.s_valor.toFixed(1)}</span>
        </div>
        <button onclick="vigiaAbrirCerrar(${a.id}, '${a.severidad}')"
          style="padding:6px 14px;background:${color};border:none;border-radius:6px;color:#fff;font-size:11px;font-weight:700;cursor:pointer;">
          Cerrar
        </button>
      </div>`;
    }
    html += '</div></div>';
  }

  // Series por C.O. — jerarquía: facturas > despachos > facturación
  if (coKeys.length === 0) {
    html += '<div style="color:var(--tx3);padding:20px;text-align:center;">Sin series cargadas. Carga un archivo TXT de ventas para comenzar.</div>';
  } else {
    html += '<div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">Series por C.O.</div>';
    for (const co of coKeys) {
      const series = cos[co];
      html += `<div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:12px;margin-bottom:8px;">`;
      html += `<div style="font-size:14px;font-weight:700;color:var(--pm);margin-bottom:6px;">C.O. ${co}</div>`;

      for (const tipo of _VIGIA_TIPO_ORDEN) {
        const s = series[tipo];
        if (!s) {
          // Placeholder visible para serie pendiente (planillas)
          if (tipo === 'planillas') {
            html += `<div style="display:flex;align-items:center;gap:8px;padding:8px;margin:2px 0;border-radius:6px;
              background:var(--bg);border:1px dashed var(--brd);opacity:0.7;">
              <span style="color:var(--tx3);font-size:16px;">○</span>
              <span style="font-size:12px;">${_VIGIA_TIPO_ICON[tipo]}</span>
              <div style="flex:1;">
                <div style="font-size:12px;font-weight:600;color:var(--tx3);">${_VIGIA_TIPO_LABEL[tipo]}</div>
                <div style="font-size:10px;color:var(--yellow);">Pendiente de conector Generic Transfer (Nelly)</div>
              </div>
            </div>`;
          }
          continue;
        }
        const hasAlarma = alarmas.some(a => a.serie === s.serie && a.severidad === 'ALARMA');
        const hasAviso = alarmas.some(a => a.serie === s.serie && a.severidad === 'AVISO');
        const dotColor = hasAlarma ? 'var(--red)' : (hasAviso ? 'var(--yellow)' : 'var(--green)');
        const isActive = _VIGIA_SERIE_ACTIVA === s.serie;

        html += `<div onclick="vigiaSeleccionarSerie('${s.serie}')"
          style="display:flex;align-items:center;gap:8px;padding:8px;margin:2px 0;border-radius:6px;cursor:pointer;
            background:${isActive ? 'var(--pm)22' : 'transparent'};border:${isActive ? '1px solid var(--pm)' : '1px solid transparent'};">
          <span style="color:${dotColor};font-size:16px;">●</span>
          <span style="font-size:12px;">${_VIGIA_TIPO_ICON[tipo] || ''}</span>
          <div style="flex:1;">
            <div style="font-size:12px;font-weight:600;color:var(--tx);">${_VIGIA_TIPO_LABEL[tipo] || tipo}</div>
            <div style="font-size:10px;color:var(--tx3);">${_VIGIA_TIPO_DESC[tipo] || ''}</div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:11px;color:var(--tx3);">${s.semanas} sem</div>
            <div style="font-size:10px;color:var(--tx3);">${s.desde || '?'} a ${s.hasta || '?'}</div>
          </div>
        </div>`;
      }
      html += '</div>';
    }
  }

  // Area del grafico CUSUM
  html += `<div id="vigia-chart-area" style="margin-top:16px;display:${_VIGIA_SERIE_ACTIVA ? 'block' : 'none'};">
    <div style="font-size:13px;font-weight:700;color:var(--tx);margin-bottom:8px;">
      CUSUM: <span id="vigia-chart-titulo" style="color:var(--pm);">${_VIGIA_SERIE_ACTIVA || ''}</span>
    </div>
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:8px;padding:12px;">
      <canvas id="vigia-cusum-chart" style="width:100%;height:280px;"></canvas>
      <div id="vigia-chart-stats" style="margin-top:8px;font-size:11px;color:var(--tx3);"></div>
    </div>
  </div>`;

  // Modal cerrar alarma
  html += `<div id="vigia-modal-cerrar" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9999;
    align-items:center;justify-content:center;">
    <div style="background:var(--bg-s);border:1px solid var(--brd);border-radius:12px;padding:20px;max-width:420px;width:90%;">
      <div style="font-size:15px;font-weight:700;color:var(--tx);margin-bottom:12px;">Cerrar alarma</div>
      <div id="vigia-modal-info" style="font-size:12px;color:var(--tx3);margin-bottom:12px;"></div>
      <div style="margin-bottom:10px;">
        <label style="font-size:11px;color:var(--tx3);display:block;margin-bottom:4px;">Responsable del plan</label>
        <select id="vigia-modal-responsable"
          style="width:100%;padding:8px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:8px;
            color:var(--tx);font-size:13px;box-sizing:border-box;">
          <option value="">-- Seleccionar responsable --</option>
        </select>
      </div>
      <textarea id="vigia-modal-causa" placeholder="Causa del corrimiento (min. 20 caracteres para ALARMA)..."
        style="width:100%;height:80px;background:var(--bg);border:1px solid var(--brd);border-radius:8px;padding:10px;
          color:var(--tx);font-size:13px;resize:vertical;box-sizing:border-box;"></textarea>
      <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end;">
        <button onclick="vigiaCerrarModal()"
          style="padding:8px 16px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx);font-size:12px;cursor:pointer;">
          Cancelar
        </button>
        <button onclick="vigiaConfirmarCierre()"
          style="padding:8px 16px;background:var(--pm);border:none;border-radius:6px;color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
          Confirmar cierre
        </button>
      </div>
    </div>
  </div>`;

  // Carga TXT (admin only)
  html += `<div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--brd);">
    <div style="font-size:12px;font-weight:700;color:var(--tx3);margin-bottom:8px;">Cargar datos (admin)</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <input type="file" id="vigia-file" accept=".txt"
        style="flex:1;min-width:200px;padding:8px;background:var(--bg);border:1px solid var(--brd);border-radius:8px;color:var(--tx);font-size:12px;">
      <button onclick="vigiaCargarTxt()"
        style="padding:8px 16px;background:var(--pm);border:none;border-radius:8px;color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
        Cargar TXT
      </button>
    </div>
  </div>`;

  el.innerHTML = html;
}

function _vigiaBadgeResumen(count, label, color) {
  return `<div style="background:${count > 0 ? color + '22' : 'var(--bg-s)'};border:1px solid ${count > 0 ? color : 'var(--brd)'};
    border-radius:8px;padding:8px 14px;display:flex;align-items:center;gap:8px;">
    <span style="font-size:20px;font-weight:800;color:${count > 0 ? color : 'var(--tx3)'};">${count}</span>
    <span style="font-size:12px;color:${count > 0 ? color : 'var(--tx3)'};font-weight:600;">${label}${count !== 1 ? 'S' : ''}</span>
  </div>`;
}

async function vigiaSeleccionarSerie(nombre) {
  _VIGIA_SERIE_ACTIVA = nombre;

  // Mostrar area del grafico
  const area = document.getElementById('vigia-chart-area');
  if (area) area.style.display = 'block';
  const titulo = document.getElementById('vigia-chart-titulo');
  if (titulo) titulo.textContent = nombre;

  // Re-render el panel para marcar la serie activa
  try {
    const d = await get('/api/vigia/resumen');
    const el = document.getElementById('vigia-container');
    if (el) _vigiaRenderPanel(el, d);
  } catch (e) { /* ignore */ }

  // Ejecutar CUSUM y graficar
  try {
    const r = await (await fetch(API + '/api/vigia/ejecutar/' + nombre, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
    })).json();

    if (r.error) {
      const stats = document.getElementById('vigia-chart-stats');
      if (stats) stats.innerHTML = `<span style="color:var(--red);">${r.error}</span>`;
      return;
    }

    _vigiaGraficar(r);
  } catch (e) {
    const stats = document.getElementById('vigia-chart-stats');
    if (stats) stats.innerHTML = `<span style="color:var(--red);">Error: ${e.message}</span>`;
  }
}

function _vigiaGraficar(resultado) {
  const ctx = document.getElementById('vigia-cusum-chart');
  if (!ctx || !window.Chart) return;
  if (_VIGIA_CHART) _VIGIA_CHART.destroy();

  const labels = resultado.cusum.map(p => p.semana.slice(5)); // MM-DD
  const valores = resultado.cusum.map(p => p.valor);
  const sPlus = resultado.cusum.map(p => p.s_plus);
  const sMinus = resultado.cusum.map(p => p.s_minus);

  // Colores de fondo por punto para la serie de valor
  const bgColors = resultado.cusum.map(p => {
    if (p.severidad === 'ALARMA') return p.tipo === 'BAJA' ? '#F8717188' : '#60A5FA88';
    if (p.severidad === 'AVISO') return '#FACC1566';
    return '#E2EEF822';
  });

  _VIGIA_CHART = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          type: 'bar',
          label: 'Valor',
          data: valores,
          backgroundColor: bgColors,
          borderRadius: 3,
          yAxisID: 'y',
          order: 2,
        },
        {
          type: 'line',
          label: 'S+',
          data: sPlus,
          borderColor: '#60A5FA',
          backgroundColor: '#60A5FA22',
          tension: 0.3,
          pointRadius: 1,
          borderWidth: 2,
          fill: false,
          yAxisID: 'y1',
          order: 1,
        },
        {
          type: 'line',
          label: 'S-',
          data: sMinus,
          borderColor: '#F87171',
          backgroundColor: '#F8717122',
          tension: 0.3,
          pointRadius: 1,
          borderWidth: 2,
          fill: false,
          yAxisID: 'y1',
          order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: {
          display: true,
          position: 'top',
          labels: { color: '#7A96B0', font: { size: 10 }, boxWidth: 12, padding: 8 },
        },
        annotation: {
          annotations: {
            lineAviso: {
              type: 'line', yMin: resultado.h_aviso, yMax: resultado.h_aviso,
              borderColor: '#FACC15', borderWidth: 1, borderDash: [4, 4],
              yScaleID: 'y1',
              label: { content: 'Aviso', enabled: true, position: 'end',
                       font: { size: 9 }, color: '#FACC15' },
            },
            lineAlarma: {
              type: 'line', yMin: resultado.h_alarma, yMax: resultado.h_alarma,
              borderColor: '#F87171', borderWidth: 1, borderDash: [4, 4],
              yScaleID: 'y1',
              label: { content: 'Alarma', enabled: true, position: 'end',
                       font: { size: 9 }, color: '#F87171' },
            },
          },
        },
      },
      scales: {
        x: { ticks: { color: '#555', font: { size: 9 }, maxRotation: 45 }, grid: { color: '#111' } },
        y: {
          position: 'left',
          ticks: { color: '#7A96B0', font: { size: 9 } },
          grid: { color: '#1C2B3A' },
          title: { display: true, text: 'Valor', color: '#7A96B0', font: { size: 10 } },
        },
        y1: {
          position: 'right',
          ticks: { color: '#555', font: { size: 9 } },
          grid: { drawOnChartArea: false },
          title: { display: true, text: 'S+ / S-', color: '#555', font: { size: 10 } },
          beginAtZero: true,
        },
      },
    },
  });

  // Stats debajo del grafico
  const stats = document.getElementById('vigia-chart-stats');
  if (stats) {
    const alarmas = resultado.cusum.filter(p => p.alarma);
    const ultimaSemana = resultado.cusum[resultado.cusum.length - 1];
    stats.innerHTML = `
      <span>mu=${Number(resultado.mu_ref).toLocaleString('es-CO')} |
      sigma=${Number(resultado.sigma_ref).toLocaleString('es-CO')} |
      k=${resultado.k} |
      h_aviso=${resultado.h_aviso} |
      h_alarma=${resultado.h_alarma} |
      ${resultado.semanas} semanas |
      ${alarmas.length} puntos con alarma</span>
      ${ultimaSemana ? `<br>Ultima semana: S+=${ultimaSemana.s_plus.toFixed(2)}, S-=${ultimaSemana.s_minus.toFixed(2)}` : ''}
    `;
  }
}

// === Cerrar alarma ===

let _VIGIA_ALARMA_CERRAR = null;

async function vigiaAbrirCerrar(alarmaId, severidad) {
  _VIGIA_ALARMA_CERRAR = { id: alarmaId, severidad };
  const modal = document.getElementById('vigia-modal-cerrar');
  if (modal) modal.style.display = 'flex';
  const info = document.getElementById('vigia-modal-info');
  if (info) {
    const minChars = severidad === 'ALARMA' ? 20 : 5;
    info.textContent = `${severidad}: Causa minimo ${minChars} caracteres. Selecciona quien queda dueno del plan.`;
  }
  const textarea = document.getElementById('vigia-modal-causa');
  if (textarea) { textarea.value = ''; }

  // Cargar usuarios admin/gerente para selector de responsable
  const sel = document.getElementById('vigia-modal-responsable');
  if (sel) {
    sel.innerHTML = '<option value="">-- Seleccionar responsable --</option>';
    try {
      const r = await get('/api/auth/usuarios');
      const usuarios = r.usuarios || r || [];
      for (const u of usuarios) {
        if (['admin', 'gerente', 'jefe_almacen', 'supervisor'].includes(u.rol)) {
          sel.innerHTML += `<option value="${u.id}">${u.nombre} (${u.rol})</option>`;
        }
      }
    } catch (e) {
      // Si falla, el usuario actual queda como default
      sel.innerHTML += `<option value="${OPERARIO.id}" selected>${OPERARIO.nombre}</option>`;
    }
  }
}

function vigiaCerrarModal() {
  const modal = document.getElementById('vigia-modal-cerrar');
  if (modal) modal.style.display = 'none';
  _VIGIA_ALARMA_CERRAR = null;
}

async function vigiaConfirmarCierre() {
  if (!_VIGIA_ALARMA_CERRAR) return;
  const causa = (document.getElementById('vigia-modal-causa') || {}).value || '';
  const responsableId = (document.getElementById('vigia-modal-responsable') || {}).value || '';

  if (_VIGIA_ALARMA_CERRAR.severidad === 'ALARMA' && !responsableId) {
    alerta('Selecciona un responsable para cerrar una ALARMA', 'error');
    return;
  }

  try {
    const r = await (await fetch(API + '/api/vigia/alarmas/' + _VIGIA_ALARMA_CERRAR.id + '/cerrar', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ causa, responsable_id: responsableId ? parseInt(responsableId) : null }),
    })).json();

    if (r.error) {
      alerta(r.error, 'error');
      return;
    }
    alerta('Alarma cerrada', 'exito');
    vigiaCerrarModal();
    cargarVigia();
  } catch (e) {
    alerta('Error: ' + (e.message || e), 'error');
  }
}

// === Carga TXT ===

async function vigiaCargarTxt() {
  const fileInput = document.getElementById('vigia-file');
  if (!fileInput || !fileInput.files[0]) {
    alerta('Selecciona un archivo TXT', 'error');
    return;
  }

  const formData = new FormData();
  formData.append('archivo', fileInput.files[0]);

  try {
    alerta('Cargando archivo...', 'advertencia');
    const r = await fetch(API + '/api/vigia/cargar-txt', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN },
      body: formData,
    });
    const d = await r.json();
    if (d.error) {
      alerta(d.error, 'error');
      return;
    }
    alerta(`Cargados: ${d.registros_procesados} registros, ${d.series_creadas} series nuevas`, 'exito');
    fileInput.value = '';
    cargarVigia();
  } catch (e) {
    alerta('Error: ' + (e.message || e), 'error');
  }
}
