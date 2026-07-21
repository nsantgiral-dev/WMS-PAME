"""
Health-check de integración Siesa/Connekta.

Rutas:
  GET /api/health/ping  — pública, solo ok/error (Railway probe + monitoring externo)
  GET /api/health/siesa — requiere JWT admin/gestion (detalle completo de configuración)

No verifica `API_v2_Conceptos` porque ese endpoint no está en el contrato
documentado de Connekta para esta instalación. La validez del motivo de traslado
se confirma en el primer despacho real — Siesa rechaza con error claro si es inválido.
"""
import logging
from datetime import datetime
from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_gestion

health_bp = Blueprint('health', __name__)
logger = logging.getLogger(__name__)

_VARS_CRITICAS = [
    ('SIESA_MOTIVO_TRASLADO',         'Motivo para transferencias (173076/173079)'),
    ('SIESA_TIPO_DOCTO_TRANSITO_SALIDA',  'Tipo doc salida en tránsito (conector 173076)'),
    ('SIESA_TIPO_DOCTO_TRANSITO_ENTRADA', 'Tipo doc entrada en tránsito (conector 173079)'),
    ('SIESA_BODEGA_TRANSITO',         'Bodega de tránsito (TRA1 u otro)'),
    ('SIESA_UBICACION_ENTRADA_DEFAULT', 'Ubicación ancla en bodega destino para 173079 (multi-ubicaciones) — usar REC en todas las sedes'),
    ('SIESA_UNIDAD_NEGOCIO',           'Unidad de negocio 173076/173079 — Siesa NO hereda de bodega en traslados, obligatorio'),
    ('SIESA_COND_PAGO_VENTAS',        'Condición de pago ventas fallback (C01 u otro)'),
    ('SIESA_TIPO_DOCTO_FACTURA',      'Tipo doc factura electrónica (conector 238925)'),
    ('SIESA_TIPO_DOCTO_ENTRADA_OC',   'Tipo doc entrada por OC (conector 142948)'),
]


@health_bp.route('/ping', methods=['GET'])
def health_ping():
    """Endpoint público mínimo — solo indica si el servicio está activo."""
    from app.services.connekta_gateway import connekta
    try:
        return jsonify({
            'ok': True,
            'modo_simulacion': connekta.modo_simulacion,
            'circuit_breaker': connekta.circuit_state(),
        }), 200
    except Exception:
        return jsonify({'ok': False}), 503


@health_bp.route('/diag', methods=['GET'])
def health_diag():
    """Página de diagnóstico — prueba DB, API, y JS en el browser del usuario."""
    html = '''<!DOCTYPE html><html><head><meta charset="utf-8"><title>WMS Diagnóstico</title>
    <style>body{font-family:monospace;padding:20px;background:#111;color:#0f0}
    .ok{color:#4ade80}.fail{color:#ef4444}.wait{color:#facc15}
    pre{background:#222;padding:10px;border-radius:8px;overflow-x:auto}</style></head>
    <body><h2>WMS Diagnóstico</h2><div id="r"></div>
    <script>
    const r = document.getElementById('r');
    function log(msg, cls='ok') { r.innerHTML += `<div class="${cls}">${new Date().toLocaleTimeString()} ${msg}</div>`; }

    async function run() {
      // 1. Test health (no DB)
      log('1. Testing /api/health/ping...', 'wait');
      try {
        const t0 = Date.now();
        const h = await fetch('/api/health/ping').then(r=>r.json());
        log(`1. Health OK en ${Date.now()-t0}ms — circuit_breaker: ${h.circuit_breaker.state}`);
      } catch(e) { log('1. Health FAILED: ' + e.message, 'fail'); }

      // 2. Test DB via login (expects 400 or 401)
      log('2. Testing DB via /api/auth/login...', 'wait');
      try {
        const t0 = Date.now();
        const resp = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
        log(`2. Login endpoint: HTTP ${resp.status} en ${Date.now()-t0}ms (DB accesible)`);
      } catch(e) { log('2. Login FAILED: ' + e.message, 'fail'); }

      // 3. Test TOKEN from localStorage
      const token = localStorage.getItem('wms_token');
      if (!token) {
        log('3. TOKEN: NULL — no hay sesion guardada. Debes hacer login primero.', 'fail');
        return;
      }
      log('3. TOKEN encontrado: ' + token.substring(0,20) + '...');

      // 4. Test authenticated endpoint
      const endpoints = [
        '/api/rutas/?fecha_desde=2026-07-21&fecha_hasta=2026-07-21',
        '/api/conteo/?estados=PENDIENTE,EN_PROCESO&page=1',
        '/api/reposicion/ubicaciones',
        '/api/muelle/listos',
      ];
      for (const ep of endpoints) {
        log(`4. Testing ${ep}...`, 'wait');
        try {
          const t0 = Date.now();
          const resp = await fetch(ep, {headers:{Authorization:'Bearer '+token}});
          const ms = Date.now()-t0;
          if (resp.status === 401) {
            log(`4. ${ep} → 401 TOKEN EXPIRADO — haz login de nuevo`, 'fail');
          } else if (resp.ok) {
            const data = await resp.json();
            log(`4. ${ep} → HTTP ${resp.status} en ${ms}ms — OK`);
            log(`   Response keys: ${Object.keys(data).join(', ')}`, 'ok');
          } else {
            const body = await resp.text();
            log(`4. ${ep} → HTTP ${resp.status} en ${ms}ms: ${body.substring(0,200)}`, 'fail');
          }
        } catch(e) { log(`4. ${ep} → NETWORK ERROR: ${e.message}`, 'fail'); }
      }

      // 5. Load app.js and check if functions are defined
      log('5. Loading app.js to check function definitions...', 'wait');
      try {
        const appJs = await fetch('/static/pwa/app.js');
        const appJsText = await appJs.text();
        log(`5a. app.js downloaded: ${appJsText.length} bytes`);
        // Check for key functions in the source
        const fns = ['get','post','tab','cargarAdmin','mostrarSegunRol','salir'];
        const missing = fns.filter(fn => !appJsText.includes('function ' + fn));
        if (missing.length) {
          log('5b. MISSING functions in app.js source: ' + missing.join(', '), 'fail');
        } else {
          log('5b. All core functions found in app.js source');
        }
        // Try to eval a subset to check runtime errors
        try {
          // Just check the globals section (first ~50 lines)
          const lines = appJsText.split('\\n');
          log('5c. app.js has ' + lines.length + ' lines, first line: ' + lines[0].substring(0,60));
        } catch(e2) { log('5c. Parse check error: ' + e2.message, 'fail'); }
      } catch(e) { log('5. app.js LOAD FAILED: ' + e.message, 'fail'); }

      // 6. Check error in browser console by trying to load all modules
      log('6. Testing script execution...', 'wait');
      const scripts = ['app.js','picking.js','packing.js','recepcion.js','rutas.js','traslados.js','conteo.js','reposicion.js','liquidacion.js','layout.js'];
      window.onerror = function(msg, src, line, col) {
        log('RUNTIME ERROR: ' + msg + ' at ' + (src||'?').split('/').pop() + ':' + line, 'fail');
      };
      for (const s of scripts) {
        try {
          const el = document.createElement('script');
          el.src = '/static/pwa/' + s + '?diag=' + Date.now();
          const p = new Promise((ok, fail) => { el.onload = ok; el.onerror = fail; });
          document.head.appendChild(el);
          await p;
          log('6. ' + s + ' loaded OK');
        } catch(e) { log('6. ' + s + ' FAILED TO LOAD', 'fail'); }
      }

      // 7. Check if functions are NOW defined after loading
      log('7. Checking if functions are defined after loading...', 'wait');
      const check = ['get','post','tab','cargarAdmin','cargarRutas','cargarListaRutas','cargarReposicion','cargarConteos','cargarInventario','cargarMuelle','cargarTrasladosAdmin','cargarLayout','cargarLiquidacion'];
      for (const fn of check) {
        if (typeof window[fn] === 'function') {
          log('7. ' + fn + '() ✓');
        } else {
          log('7. ' + fn + '() NOT DEFINED ✗', 'fail');
        }
      }

      // 8. SW
      log('8. Service Worker: ' + (navigator.serviceWorker?.controller ? 'ACTIVE (' + navigator.serviceWorker.controller.scriptURL + ')' : 'none'));

      log('=== DIAGNOSTICO COMPLETO ===');
    }
    run();
    </script></body></html>'''
    return html, 200, {'Content-Type': 'text/html'}


@health_bp.route('/siesa', methods=['GET'])
@jwt_required()
def health_siesa():
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Acceso restringido a roles de gestión'}), 403
    import os
    from app.services.connekta_gateway import connekta

    resultado = {
        'timestamp': datetime.utcnow().isoformat(),
        'modo': (
            'simulacion' if connekta.modo_simulacion
            else 'ensayo' if connekta.modo_ensayo
            else 'produccion'
        ),
        'variables': {},
        'conectividad': None,
        'dlq': {},
        'ok': True,
        'advertencias': [],
    }

    # ── 1. Variables de entorno ──────────────────────────────────────────────
    for var, descripcion in _VARS_CRITICAS:
        val = os.getenv(var, '')
        estado = 'ok' if val else 'FALTA'
        resultado['variables'][var] = {'valor': val or None, 'estado': estado, 'descripcion': descripcion}
        if not val:
            resultado['ok'] = False
            resultado['advertencias'].append(f'Variable {var} no configurada ({descripcion})')

    # Verificación especial: bodega_transito vs modo
    bod_transito = os.getenv('SIESA_BODEGA_TRANSITO', '')
    tipo_salida = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_SALIDA', '')
    tipo_entrada = os.getenv('SIESA_TIPO_DOCTO_TRANSITO_ENTRADA', '')
    if bod_transito and (not tipo_salida or not tipo_entrada):
        resultado['advertencias'].append(
            'SIESA_BODEGA_TRANSITO configurada pero faltan TIPO_DOCTO_TRANSITO_SALIDA o ENTRADA — '
            'el despacho EN_TRANSITO fallará'
        )
        resultado['ok'] = False

    # ── 2. Conectividad Connekta ─────────────────────────────────────────────
    if connekta.modo_simulacion:
        resultado['conectividad'] = {'estado': 'simulado', 'detalle': 'modo_simulacion activo'}
    else:
        try:
            # GET inocuo — consulta 1 ítem del catálogo. Si Connekta responde, hay red.
            r = connekta.get_items_catalogo(pagina=1)
            simulado = r.get('simulado', False)
            resultado['conectividad'] = {
                'estado': 'ok',
                'simulado': simulado,
                'detalle': 'Connekta respondió correctamente',
            }
        except Exception as e:
            resultado['conectividad'] = {
                'estado': 'ERROR',
                'detalle': str(e)[:200],
            }
            resultado['ok'] = False
            resultado['advertencias'].append(f'Connekta no responde: {str(e)[:100]}')

    # ── 3. DLQ — jobs fallidos pendientes ───────────────────────────────────
    try:
        from app.services.siesa_job_service import get_jobs_fallidos
        fallidos = get_jobs_fallidos()
        resultado['dlq'] = {
            'jobs_fallidos': len(fallidos),
            'alerta': (
                f'{len(fallidos)} job(s) en FALLIDO — requieren reintento manual'
                if fallidos else None
            ),
        }
        if fallidos:
            resultado['advertencias'].append(f'DLQ: {len(fallidos)} job(s) fallidos sin resolver')
    except Exception as e:
        resultado['dlq'] = {'estado': 'error_leyendo', 'detalle': str(e)[:100]}

    # ── 4. Nota sobre motivo ─────────────────────────────────────────────────
    motivo = os.getenv('SIESA_MOTIVO_TRASLADO', '')
    resultado['nota_motivo'] = (
        f'Motivo "{motivo}" configurado. Validez confirmada por consultor Siesa. '
        'Si Siesa lo rechaza en producción, el error llegará vía siesa_error en el traslado '
        'y email DLQ — no se necesita health-check dinámico de maestros.'
    )

    resultado['circuit_breaker'] = connekta.circuit_state()

    status_code = 200 if resultado['ok'] else 503
    return jsonify(resultado), status_code
