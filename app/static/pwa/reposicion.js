// ══════════════════════════════════════════════════════════════════════════════
// PANEL ADMIN — REPOSICIÓN
// Dependencias globales (de app.js): get(), alerta(), API, TOKEN, ALMACEN_ID
// ══════════════════════════════════════════════════════════════════════════════

let _repSubActual = 'ubicaciones';
let _repModalUbId = null;

// ── Navegación interna ────────────────────────────────────────────────────────

/**
 * Switch the active reposicion sub-tab and load its section content.
 * @param {string} sec - Section key: 'ubicaciones', 'tareas', 'huerfanas', or 'jobs'.
 */
function repSubtab(sec) {
  _repSubActual = sec;
  ['ubicaciones','tareas','huerfanas','jobs'].forEach(s => {
    const cont = document.getElementById(`rep-sec-${s}`);
    const btn  = document.getElementById(`rep-sub-${s}`);
    if (!cont || !btn) return;
    const activo = s === sec;
    cont.style.display = activo ? 'block' : 'none';
    btn.style.background = activo ? 'var(--pm)' : 'transparent';
    btn.style.color = activo ? '#fff' : 'var(--tx3)';
    btn.style.fontWeight = activo ? '700' : '400';
  });
  if (sec === 'ubicaciones') repCargarUbicaciones();
  else if (sec === 'tareas')   repCargarTareas();
  else if (sec === 'huerfanas') repCargarHuerfanas();
  else if (sec === 'jobs')     repCargarJobs();
}

/** Check for failed Siesa jobs alert and load the active reposicion section. */
async function cargarReposicion() {
  // Revisar alerta de jobs fallidos en paralelo
  try {
    const d = await get('/api/reposicion/siesa-jobs/fallidos');
    const alerta = document.getElementById('rep-alerta-jobs');
    const txt    = document.getElementById('rep-alerta-jobs-txt');
    if (alerta && txt) {
      if (d.total > 0 && d.alerta) {
        alerta.style.display = 'block';
        txt.textContent = d.alerta;
      } else {
        alerta.style.display = 'none';
      }
    }
  } catch (_) {}

  // Cargar la sección activa
  repSubtab(_repSubActual);
}

// ── SECCIÓN 1: Ubicaciones PICKING ───────────────────────────────────────────

/** Fetch and render all PICKING-type ubicaciones with stock semaphore indicators. */
async function repCargarUbicaciones() {
  const el = document.getElementById('rep-lista-ubicaciones');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';
  try {
    const d = await get('/api/reposicion/ubicaciones');
    const ubs = d.ubicaciones || [];

    if (!ubs.length) {
      el.innerHTML = `
        <div style="text-align:center;padding:40px;color:#555;">
          <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">📍</div>
          <div style="font-size:15px;font-weight:600;">Sin ubicaciones PICKING registradas</div>
          <div style="font-size:12px;margin-top:6px;">Haz sync desde Siesa para importar ubicaciones PIK-*</div>
        </div>`;
      return;
    }

    el.innerHTML = ubs.map(u => {
      const actual    = u.stock_actual ?? 0;
      const minimo    = u.stock_minimo;
      const maximo    = u.stock_maximo;
      const sinLimite = minimo == null;
      const sku       = u.sku_asignado;
      const skuLabel  = sku ? `${sku.codigo} — ${sku.nombre}` : null;

      // Semáforo
      let color, label, pct = 0;
      if (sinLimite) {
        color = '#555'; label = 'Sin límite';
      } else if (actual < minimo) {
        color = '#ef4444'; label = 'Crítico';
        pct = Math.min(100, Math.round((actual / minimo) * 100));
      } else if (actual < minimo * 1.3) {
        color = '#f59e0b'; label = 'Alerta';
        pct = maximo ? Math.min(100, Math.round((actual / maximo) * 100)) : 60;
      } else {
        color = '#22c55e'; label = 'OK';
        pct = maximo ? Math.min(100, Math.round((actual / maximo) * 100)) : 80;
      }

      const skuEsc = skuLabel ? skuLabel.replace(/'/g, "\\'") : '';

      return `
        <div class="tabla-card" style="margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
            <div>
              <div style="font-size:16px;font-weight:800;font-family:monospace;color:var(--tx);">${u.codigo}</div>
              ${skuLabel
                ? `<div style="font-size:11px;color:#60a5fa;margin-top:3px;font-weight:600;">📦 ${skuLabel}</div>`
                : `<div style="font-size:11px;color:#555;margin-top:3px;">Sin producto asignado</div>`
              }
            </div>
            <div style="display:flex;align-items:center;gap:8px;">
              <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${label}</span>
              <button onclick="repAbrirModal(${u.id}, '${u.codigo}', ${minimo ?? ''}, ${maximo ?? ''}, ${u.secuencia_ruteo ?? ''}, '${skuEsc}')"
                style="padding:5px 10px;background:var(--bg);border:1px solid var(--brd);border-radius:6px;color:var(--tx2);font-size:11px;cursor:pointer;">
                Configurar
              </button>
            </div>
          </div>

          <!-- Barra de stock -->
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
            <div style="flex:1;background:var(--brd);border-radius:6px;height:8px;overflow:hidden;">
              <div style="width:${sinLimite ? 0 : pct}%;height:100%;background:${color};border-radius:6px;transition:width .3s;"></div>
            </div>
            <div style="font-size:13px;font-weight:700;color:var(--tx);min-width:36px;text-align:right;">${actual}</div>
          </div>

          <!-- Límites -->
          <div style="display:flex;gap:16px;font-size:11px;color:var(--tx3);">
            ${sinLimite
              ? `<span style="color:#f59e0b;">⚠ Sin mínimo/máximo — toca "Configurar" para activar reposición</span>`
              : `<span>Mín <strong style="color:var(--tx);">${minimo}</strong></span>
                 <span>Máx <strong style="color:var(--tx);">${maximo ?? '—'}</strong></span>
                 ${u.secuencia_ruteo != null ? `<span>Seq <strong style="color:var(--tx);">${u.secuencia_ruteo}</strong></span>` : ''}
                 <span style="color:#22c55e;">✓ Motor activo</span>`
            }
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando ubicaciones</div>';
  }
}

// Modal configurar límites
/**
 * Open the limits configuration modal for a PICKING ubicacion.
 * @param {number} ubId - Ubicacion ID.
 * @param {string} codigo - Ubicacion code.
 * @param {number|string} min - Current stock minimum or empty string.
 * @param {number|string} max - Current stock maximum or empty string.
 * @param {number|string} seq - Current routing sequence or empty string.
 * @param {string} sku - Assigned SKU label or empty string.
 */
function repAbrirModal(ubId, codigo, min, max, seq, sku) {
  _repModalUbId = ubId;
  const m = document.getElementById('modal-rep-limites');
  if (!m) return;
  document.getElementById('rep-modal-ub-nombre').textContent = codigo;
  document.getElementById('rep-modal-min').value = min !== '' ? min : '';
  document.getElementById('rep-modal-max').value = max !== '' ? max : '';
  document.getElementById('rep-modal-seq').value = seq !== '' ? seq : '';
  const skuEl = document.getElementById('rep-modal-sku');
  if (skuEl) skuEl.textContent = sku || 'Sin producto (vacía)';
  m.style.display = 'flex';
}

/** Close the limits configuration modal. */
function repCerrarModal() {
  const m = document.getElementById('modal-rep-limites');
  if (m) m.style.display = 'none';
  _repModalUbId = null;
}

/** Save min/max stock limits and routing sequence for the active ubicacion. */
async function repGuardarLimites() {
  if (!_repModalUbId) return;
  const min = parseInt(document.getElementById('rep-modal-min')?.value);
  const max = parseInt(document.getElementById('rep-modal-max')?.value);
  const seq = parseInt(document.getElementById('rep-modal-seq')?.value);

  const payload = {};
  if (!isNaN(min)) payload.stock_minimo = min;
  if (!isNaN(max)) payload.stock_maximo = max;
  if (!isNaN(seq)) payload.secuencia_ruteo = seq;

  if (!Object.keys(payload).length) { alerta('Ingresa al menos un límite', 'error'); return; }

  try {
    const r = await fetch(API + `/api/reposicion/ubicacion/${_repModalUbId}/limites`, {
      method: 'PATCH',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      alerta('Límites guardados', 'ok');
      repCerrarModal();
      repCargarUbicaciones();
    } else {
      alerta(d.error || 'Error guardando', 'error');
    }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Trigger stock level verification and generate reposicion tasks if needed. */
async function repVerificarStock() {
  try {
    const r = await fetch(API + '/api/reposicion/verificar-stock', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ almacen_id: ALMACEN_ID }),
    });
    const d = await r.json();
    if (r.ok) {
      const n = d.tareas_generadas || 0;
      alerta(n > 0 ? `${n} tarea${n > 1 ? 's' : ''} de reposición generada${n > 1 ? 's' : ''}` : 'Stock en niveles correctos — sin nuevas tareas', n > 0 ? 'ok' : 'info');
      repCargarUbicaciones();
    } else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/** Start a background sync of PIK-* ubicaciones from Siesa. */
async function repSyncUbicaciones() {
  try {
    const r = await fetch(API + '/api/reposicion/sync-ubicaciones', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await r.json();
    if (r.ok) {
      alerta(d.mensaje || 'Sync iniciado en background', 'ok');
    } else { alerta(d.error || 'Error', 'error'); }
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── SECCIÓN 2: Tareas de Reposición ─────────────────────────────────────────

/** Fetch and render reposicion tasks filtered by the selected status. */
async function repCargarTareas() {
  const el = document.getElementById('rep-lista-tareas');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';

  const estado = document.getElementById('rep-filtro-estado')?.value || 'PENDIENTE';

  try {
    let url = '/api/reposicion/pendientes';
    if (estado === 'EN_PROCESO') url += '?estado=EN_PROCESO';
    else if (estado === 'COMPLETADA') url += '?estado=COMPLETADA';
    else if (estado === 'CANCELADA') url += '?estado=CANCELADA';

    const d = await get(url);
    const tareas = d.tareas || [];

    if (!tareas.length) {
      el.innerHTML = `<div style="text-align:center;padding:40px;color:#555;">Sin tareas ${estado.toLowerCase()}s</div>`;
      return;
    }

    const estadoColor = { PENDIENTE: '#f59e0b', EN_PROCESO: '#3b82f6', COMPLETADA: '#22c55e', CANCELADA: '#555' };

    el.innerHTML = tareas.map(t => {
      const color = estadoColor[t.estado] || '#888';
      const fecha = t.fecha_creacion ? new Date(t.fecha_creacion).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
      return `
        <div class="tabla-card" style="margin-bottom:10px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
            <div>
              <div style="font-size:13px;font-weight:700;font-family:monospace;color:var(--tx);">${t.codigo}</div>
              <div style="font-size:12px;color:var(--tx3);margin-top:2px;">${fecha}</div>
            </div>
            <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${t.estado}</span>
          </div>

          <div style="font-size:13px;font-weight:600;color:var(--tx);margin-bottom:6px;">${t.producto_nombre || t.producto_codigo || '—'}</div>

          <div style="display:flex;gap:6px;align-items:center;margin-bottom:8px;flex-wrap:wrap;">
            <span style="font-size:12px;font-family:monospace;background:#166534;color:#4ade80;padding:3px 8px;border-radius:6px;">${t.ubicacion_reserva || '—'}</span>
            <span style="font-size:14px;color:#555;">→</span>
            <span style="font-size:12px;font-family:monospace;background:#1e3a5f;color:#60a5fa;padding:3px 8px;border-radius:6px;">${t.ubicacion_picking || '—'}</span>
            <span style="font-size:12px;color:var(--tx3);">${t.cantidad_unidades} uds</span>
          </div>

          <div style="display:flex;gap:6px;justify-content:flex-end;">
            ${t.lpn_codigo ? `<span style="font-size:11px;color:#555;font-family:monospace;">LPN: ${t.lpn_codigo}</span>` : ''}
            ${(t.estado === 'PENDIENTE' || t.estado === 'EN_PROCESO') ? `
              <button onclick="repCancelarTarea(${t.id}, '${t.codigo}')"
                style="padding:5px 10px;background:var(--bg);border:1px solid #7f1d1d;border-radius:6px;color:#f87171;font-size:11px;cursor:pointer;">
                Cancelar
              </button>` : ''
            }
            ${t.siesa_enviado ? `<span style="font-size:11px;color:#22c55e;">✓ Siesa</span>` : (t.estado === 'COMPLETADA' ? `<span style="font-size:11px;color:#f59e0b;">⏳ Pendiente Siesa</span>` : '')}
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando tareas</div>';
  }
}

/**
 * Cancel a reposicion task after confirmation.
 * @param {number} id - Reposicion task ID.
 * @param {string} codigo - Task code for the confirmation prompt.
 */
async function repCancelarTarea(id, codigo) {
  if (!confirm(`¿Cancelar tarea ${codigo}?`)) return;
  try {
    const r = await fetch(API + `/api/reposicion/cancelar/${id}`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ motivo: 'Cancelada desde admin' }),
    });
    const d = await r.json();
    if (r.ok && d.ok) { alerta('Tarea cancelada', 'ok'); repCargarTareas(); }
    else alerta(d.error || 'Error', 'error');
  } catch (e) { alerta('Error de conexión', 'error'); }
}

// ── SECCIÓN 3: Ubicaciones Huérfanas ─────────────────────────────────────────

/** Fetch and render orphaned ubicaciones that lack a valid prefix in Siesa. */
async function repCargarHuerfanas() {
  const el = document.getElementById('rep-lista-huerfanas');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';
  try {
    const d = await get('/api/reposicion/ubicaciones-huerfanas');
    const items = d.huerfanas || [];

    if (!items.length) {
      el.innerHTML = `
        <div style="text-align:center;padding:40px;color:#555;">
          <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">✅</div>
          <div style="font-size:15px;font-weight:600;">Sin ubicaciones huérfanas</div>
          <div style="font-size:12px;margin-top:6px;">Todos los códigos de Siesa tienen prefijo válido.</div>
        </div>`;
      return;
    }

    el.innerHTML = `
      <div style="font-size:12px;color:#f59e0b;margin-bottom:10px;font-weight:600;">${items.length} ubicación${items.length > 1 ? 'es' : ''} requiere${items.length === 1 ? '' : 'n'} corrección en Siesa</div>
      ${items.map(h => {
        const ultima = h.fecha_ultima_vez ? new Date(h.fecha_ultima_vez).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
        return `
          <div class="tabla-card" style="margin-bottom:10px;border-left:3px solid #f59e0b;">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;">
              <div>
                <div style="font-size:15px;font-weight:800;font-family:monospace;color:#fbbf24;">${h.codigo_siesa}</div>
                <div style="font-size:11px;color:var(--tx3);margin-top:2px;">${h.descripcion || 'Sin descripción'} · Bodega: ${h.bodega_id}</div>
              </div>
              <span style="font-size:11px;color:#f59e0b;background:#78350f44;padding:3px 8px;border-radius:20px;">${h.veces_detectada}x detectada</span>
            </div>
            <div style="font-size:11px;color:var(--tx3);margin-top:8px;">
              Última vez: ${ultima} · Estado Siesa: ${h.activo_siesa ? 'Activo' : 'Inactivo'}
            </div>
            <div style="font-size:11px;color:#92400e;margin-top:6px;background:#78350f22;padding:6px 8px;border-radius:6px;">
              Acción: renombrar en Siesa con prefijo <strong>PIK-</strong>, <strong>RES-</strong> o <strong>AVE-</strong> y esperar sync nocturno (03:00)
            </div>
          </div>`;
      }).join('')}`;
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando datos</div>';
  }
}

// ── SECCIÓN 4: Jobs Siesa (DLQ) ──────────────────────────────────────────────

/** Fetch and render Siesa DLQ jobs filtered by the selected status. */
async function repCargarJobs() {
  const el = document.getElementById('rep-lista-jobs');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;padding:30px;color:#555;">Cargando...</div>';

  const filtro = document.getElementById('rep-filtro-job')?.value || 'FALLIDO';

  try {
    let d;
    if (filtro === 'FALLIDO') {
      d = await get('/api/reposicion/siesa-jobs/fallidos');
      const jobs = d.jobs || [];

      if (!jobs.length) {
        el.innerHTML = `
          <div style="text-align:center;padding:40px;color:#555;">
            <div style="font-size:32px;margin-bottom:12px;opacity:0.4;">✅</div>
            <div style="font-size:15px;font-weight:600;">Sin jobs fallidos</div>
            <div style="font-size:12px;margin-top:6px;">Todas las transferencias a Siesa se procesaron correctamente.</div>
          </div>`;
        return;
      }

      el.innerHTML = jobs.map(j => _repJobCard(j, true)).join('');
    } else {
      d = await get(`/api/reposicion/siesa-jobs?estado=${filtro}`);
      const jobs = d.jobs || [];
      if (!jobs.length) {
        el.innerHTML = `<div style="text-align:center;padding:40px;color:#555;">Sin jobs ${filtro.toLowerCase()}s</div>`;
        return;
      }
      el.innerHTML = jobs.map(j => _repJobCard(j, false)).join('');
    }
  } catch (e) {
    el.innerHTML = '<div style="text-align:center;padding:30px;color:#ef4444;">Error cargando jobs</div>';
  }
}

/**
 * Build the HTML card for a Siesa DLQ job.
 * @param {Object} j - Job object with estado, intentos, error_ultimo, etc.
 * @param {boolean} mostrarReintentar - Whether to show the retry button.
 * @returns {string} HTML string for the job card.
 */
function _repJobCard(j, mostrarReintentar) {
  const estadoColor = { PENDIENTE: '#f59e0b', COMPLETADO: '#22c55e', FALLIDO: '#ef4444' };
  const color = estadoColor[j.estado] || '#888';
  const fecha = j.fecha_creacion ? new Date(j.fecha_creacion).toLocaleString('es-CO', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' }) : '—';
  const proximo = j.proximo_intento ? new Date(j.proximo_intento).toLocaleString('es-CO', { hour:'2-digit', minute:'2-digit' }) : null;

  return `
    <div class="tabla-card" style="margin-bottom:10px;${j.estado === 'FALLIDO' ? 'border-left:3px solid #ef4444;' : ''}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
        <div>
          <div style="font-size:12px;font-weight:700;color:var(--tx);">${j.tipo || '—'}</div>
          <div style="font-size:11px;color:var(--tx3);margin-top:2px;">${fecha}</div>
        </div>
        <div style="display:flex;align-items:center;gap:6px;">
          <span style="font-size:11px;color:#666;">${j.intentos || 0}/${j.max_intentos || 3} intentos</span>
          <span style="font-size:11px;font-weight:700;color:${color};background:${color}22;padding:3px 8px;border-radius:20px;">${j.estado}</span>
        </div>
      </div>
      ${j.error_ultimo ? `
        <div style="font-size:11px;color:#f87171;background:#7f1d1d22;padding:6px 8px;border-radius:6px;margin-bottom:8px;font-family:monospace;word-break:break-all;">
          ${j.error_ultimo.slice(0, 200)}
        </div>` : ''
      }
      ${proximo && j.estado === 'PENDIENTE' ? `
        <div style="font-size:11px;color:#f59e0b;margin-bottom:8px;">Próximo intento: ${proximo}</div>` : ''
      }
      ${j.referencia_tipo ? `
        <div style="font-size:11px;color:var(--tx3);">Ref: ${j.referencia_tipo} #${j.referencia_id || '—'}</div>` : ''
      }
      ${mostrarReintentar ? `
        <div style="display:flex;justify-content:flex-end;margin-top:8px;">
          <button onclick="repReintentar(${j.id})"
            style="padding:6px 14px;background:var(--pm);border:none;border-radius:6px;color:#fff;font-size:12px;font-weight:700;cursor:pointer;">
            Reintentar ahora
          </button>
        </div>` : ''
      }
    </div>`;
}

/**
 * Retry a specific failed Siesa job immediately.
 * @param {number} jobId - Siesa job ID.
 */
async function repReintentar(jobId) {
  try {
    const r = await fetch(API + `/api/reposicion/siesa-jobs/${jobId}/reintentar`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    const d = await r.json();
    if (r.ok && d.ok) { alerta('Job enviado a reintentar', 'ok'); repCargarJobs(); }
    else alerta(d.error || 'Error', 'error');
  } catch (e) { alerta('Error de conexión', 'error'); }
}

/**
 * Retry all failed DESPACHO_F470 Siesa jobs at once.
 * @param {HTMLButtonElement} btn - Button element to disable during processing.
 */
async function repReintentarTodosFallidos(btn) {
  if (!confirm('¿Reintentar TODOS los jobs DESPACHO_F470 fallidos? Esto enviará las facturas pendientes a Siesa.')) return;
  if (btn) { btn.disabled = true; btn.textContent = 'Procesando...'; }
  try {
    const r = await fetch(API + '/api/siesa/resetear-jobs-fallidos', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
    });
    const d = await r.json();
    if (r.ok) {
      alerta(d.mensaje || `${d.reseteados} job(s) enviados a reintentar`, 'ok');
      repCargarJobs();
    } else {
      alerta(d.error || 'Error al reintentar', 'error');
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Reintentar todos los fallidos'; }
  }
}

/**
 * Send a test email via the reposicion alerts SMTP endpoint.
 * @param {HTMLButtonElement} btn - Button element to disable during sending.
 */
async function repTestEmail(btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'Enviando...'; }
  try {
    const r = await fetch(API + '/api/reposicion/alertas/test-email', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      alerta('Email enviado — revisa la bandeja de wms@papeleriamedellin.com.co', 'ok');
    } else {
      alerta(d.error || 'Error SMTP — revisa las variables en Railway', 'error');
    }
  } catch (e) {
    alerta(`Error: ${e.message || 'no se pudo conectar al servidor'}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Enviar email de prueba ahora'; }
  }
}


// ══════════════════════════════════════════════════════════════════════════════
// ABASTECEDOR — Pantalla operario de reposición RESERVA → PICKING
// Movido desde app.js 2026-07-21
// ══════════════════════════════════════════════════════════════════════════════

let ABAST_TAREA = null;
let ABAST_TIMER = null;
// true cuando el HUD se abrió desde la cola unificada de picking.js (pedirTarea) —
// decide a dónde vuelve abastCerrarHUD() al terminar. false = abastecedor puro,
// pantalla dedicada de siempre.
let ABAST_UNIFICADO = false;

/**
 * Entrada desde la cola unificada (picking.js:pedirTarea) — el dispensador ya
 * asignó la tarea de reposición, acá solo se muestra el HUD de escaneo sobre
 * la pantalla actual. Sin botón, sin pantalla aparte: es un tipo de tarea más
 * en la misma cola de Pedido/Traslado/Conteo.
 */
function pickingReponerAhora(tarea) {
  ABAST_UNIFICADO = true;
  ABAST_TAREA = tarea;
  pantalla('pantalla-abastecedor');
  abastMostrarHUD(tarea);
}

function abastIniciar() {
  pantalla('pantalla-abastecedor');
  if (OPERARIO) actualizarUI(OPERARIO);
  const btnPicker = document.getElementById('abast-badge-picker');
  if (btnPicker) btnPicker.style.display = (OPERARIO?.puede_picar !== false) ? 'inline' : 'none';
  abastCargarTarea();
  ABAST_TIMER = setInterval(() => {
    if (!ABAST_TAREA) abastCargarTarea();
  }, 8000);
}

async function abastCargarTarea() {
  const cont = document.getElementById('abast-contenido');
  try {
    const d = await get('/api/reposicion/tarea-actual');
    if (d.sin_tareas) {
      ABAST_TAREA = null;
      if (cont) cont.innerHTML = `
        <div style="text-align:center;padding:60px 20px;">
          <div style="font-size:48px;margin-bottom:16px;opacity:0.3;">📦</div>
          <div style="font-size:18px;font-weight:700;color:#555;">Sin tareas de reposición</div>
          <div style="font-size:13px;color:#444;margin-top:8px;">El stock está en niveles correctos.</div>
          <div style="font-size:12px;color:#333;margin-top:24px;">Revisando automáticamente...</div>
        </div>`;
      if (OPERARIO?.puede_picar !== false) {
        if (cont) cont.innerHTML += `
          <div style="padding:0 16px 24px;">
            <button onclick="abastCambiarAModo('picker')"
              style="width:100%;padding:14px;background:#1e3a5f;border:1px solid #1e40af;border-radius:12px;color:#60a5fa;font-size:14px;font-weight:700;cursor:pointer;">
              Cambiar a modo Picker
            </button>
          </div>`;
      }
      return;
    }
    ABAST_TAREA = d;
    abastMostrarHUD(d);
  } catch (e) {
    if (cont) cont.innerHTML = `<div style="text-align:center;padding:40px;color:#ef4444;">Error de conexión</div>`;
  }
}

function abastMostrarHUD(tarea) {
  const hud = document.getElementById('abast-hud');
  const cont = document.getElementById('abast-contenido');
  if (!hud) return;
  if (cont) cont.style.display = 'none';
  hud.style.display = 'flex';
  document.getElementById('abast-hud-paso').textContent = 'Tarea de reposición';
  document.getElementById('abast-hud-instruccion').textContent = 'Busca la paca en la zona de reserva';
  document.getElementById('abast-hud-sub').textContent =
    `${tarea.producto_nombre || tarea.producto_codigo || '—'} · ${tarea.cantidad_unidades || '—'} uds`;
  document.getElementById('abast-ubicacion-origen').textContent = tarea.ubicacion_reserva || '—';
  document.getElementById('abast-lpn-codigo').textContent = `LPN: ${tarea.lpn_codigo || '—'}`;
  document.getElementById('abast-ubicacion-destino').textContent = tarea.ubicacion_picking || '—';
  document.getElementById('abast-cantidad').textContent =
    tarea.cantidad_unidades ? `${tarea.cantidad_unidades} unidades` : '';
  const inp = document.getElementById('abast-input-lpn');
  if (inp) { inp.value = ''; inp.focus(); }
}

function abastCerrarHUD() {
  const hud = document.getElementById('abast-hud');
  const cont = document.getElementById('abast-contenido');
  if (hud) hud.style.display = 'none';
  if (cont) cont.style.display = 'block';
  ABAST_TAREA = null;
  if (ABAST_UNIFICADO) {
    // Volvió de una tarea de la cola unificada — sigue en esa cola, no en la
    // pantalla dedicada del abastecedor puro.
    ABAST_UNIFICADO = false;
    clearInterval(ABAST_TIMER);
    pantalla('pantalla-operario');
    if (OPERARIO) actualizarUI(OPERARIO);
    pedirTarea();
    return;
  }
  abastCargarTarea();
}

async function abastConfirmarScan() {
  if (!ABAST_TAREA) return;
  const inp = document.getElementById('abast-input-lpn');
  const lpn_escaneado = (inp?.value || '').trim().toUpperCase();
  if (!lpn_escaneado) { alerta('Escanea el código LPN primero', 'error'); return; }
  const btn = document.getElementById('abast-btn-confirmar');
  if (btn) { btn.disabled = true; btn.textContent = 'Confirmando...'; }
  try {
    const r = await fetch(API + '/api/reposicion/confirmar', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + TOKEN, 'Content-Type': 'application/json' },
      body: JSON.stringify({ tarea_id: ABAST_TAREA.id, lpn_codigo_escaneado: lpn_escaneado }),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      _abastFlash('#166534');
      alerta(`Reposición completada — ${d.tarea?.unidades_movidas || ''} uds a ${ABAST_TAREA.ubicacion_picking}`, 'ok');
      ABAST_TAREA = null;
      setTimeout(abastCerrarHUD, 800);
    } else {
      _abastFlash('#7f1d1d');
      alerta(d.error || 'LPN incorrecto — verifica el código', 'error');
      if (inp) { inp.value = ''; inp.focus(); }
    }
  } catch (e) {
    alerta('Error de conexión', 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Confirmar entrega'; }
  }
}

function _abastFlash(color) {
  const f = document.getElementById('abast-flash');
  if (!f) return;
  f.style.background = color + '66';
  setTimeout(() => { f.style.background = 'transparent'; }, 300);
}

function abastCambiarAModo(modo) {
  clearInterval(ABAST_TIMER);
  ABAST_TAREA = null;
  const hud = document.getElementById('abast-hud');
  if (hud) hud.style.display = 'none';
  if (modo === 'picker') {
    pantalla('pantalla-operario');
    if (OPERARIO) actualizarUI(OPERARIO);
    pedirTarea();
    TIMER_OPERARIO = setInterval(() => { if (!TAREA_ACTUAL) pedirTarea(); }, 5000);
  }
}

