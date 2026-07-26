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
        # El modo alimenta el banner global: si los datos de pantalla NO son la
        # realidad, quien los mira debe saberlo. Ver un número de ensayo sin
        # etiqueta entrena a obedecer números falsos.
        #
        # WMS_ENSAYO es INDEPENDIENTE de Connekta a propósito. Los flags de
        # connekta describen si los POST llegan a Siesa; NO describen si los
        # datos en pantalla son de prueba. En un ensayo con vestuario hay
        # credenciales reales y datos ficticios a la vez — sin esta variable el
        # banner se apagaría justo cuando más se necesita.
        import os as _os
        if _os.environ.get('WMS_ENSAYO', '').lower() == 'true':
            modo = 'ensayo'
        else:
            modo = ('simulacion' if connekta.modo_simulacion
                    else 'ensayo' if getattr(connekta, 'modo_ensayo', False)
                    else 'produccion')
        return jsonify({
            'ok': True,
            'modo': modo,
            'modo_simulacion': connekta.modo_simulacion,
            'circuit_breaker': connekta.circuit_state(),
        }), 200
    except Exception:
        return jsonify({'ok': False}), 503



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
