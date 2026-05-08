"""
Despacho parcial — módulo administrador.

Responsabilidad única: control de acceso gestión + respuesta HTTP.
La lógica 142945→142943 está delegada 100% a DespachoParialService.
No importa ni toca nada de packing.py ni del flujo del operario.
"""
import logging
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.models.packing import TareaPacking, EstadoPacking
from app.routes._auth_helpers import _es_gestion

despacho_parcial_bp = Blueprint('despacho_parcial', __name__)
logger = logging.getLogger(__name__)


@despacho_parcial_bp.route('/<int:packing_id>/compromisos', methods=['GET'])
@jwt_required()
def get_compromisos(packing_id: int):
    """
    GET /api/despacho_parcial/<packing_id>/compromisos
    Devuelve las líneas de compromiso del pedido desde Siesa para revisión previa.
    Campos clave por ítem: f120_referencia, f400_cant_comprometida_1.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    from app.services.despacho_parcial_service import DespachoParialService
    compromisos = DespachoParialService.obtener_compromisos(
        tarea.tipo_docto_pedido_siesa,
        tarea.consec_docto_pedido_siesa,
    )
    return jsonify({
        'packing_id': packing_id,
        'pedido': tarea.numero_pedido_siesa,
        'compromisos': compromisos,
    }), 200


@despacho_parcial_bp.route('/<int:packing_id>/despachar', methods=['POST'])
@jwt_required()
def despachar_parcial(packing_id: int):
    """
    POST /api/despacho_parcial/<packing_id>/despachar
    Body: {"cantidades": {"CODIGO_PRODUCTO": 10.0, ...}}
    Ejecuta 142945 (RM) → 142943 (FE) y marca la tarea DESPACHADO.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede despachar una tarea cancelada'}), 409

    body = request.get_json(silent=True) or {}
    cantidades = body.get('cantidades')
    if not cantidades or not isinstance(cantidades, dict):
        return jsonify({'error': 'Se requiere body {"cantidades": {"codigo": qty}}'}), 400

    from app.services.despacho_parcial_service import DespachoParialService
    try:
        resultado = DespachoParialService.despachar_parcial(tarea, cantidades)
        logger.info(
            '[DESPACHO_PARCIAL] usuario=%s despachó packing_id=%s → %s',
            u.email, packing_id, resultado.get('rm')
        )
        return jsonify({'ok': True, **resultado}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        logger.exception('[DESPACHO_PARCIAL] Error Siesa packing_id=%s: %s', packing_id, e)
        return jsonify({'error': f'Error Siesa: {str(e)}'}), 502


@despacho_parcial_bp.route('/<int:packing_id>/facturar-remision', methods=['POST'])
@jwt_required()
def facturar_remision(packing_id: int):
    """
    POST /api/despacho_parcial/<packing_id>/facturar-remision

    Carril de recuperación: detecta la RM existente en Siesa y genera la FE (142943).
    No requiere body. Usar cuando cerrar_caja creó la RM pero la FE falló.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede facturar una tarea cancelada'}), 409

    from app.services.despacho_parcial_service import DespachoParialService
    try:
        resultado = DespachoParialService.facturar_remision_existente(tarea)
        logger.info(
            '[FACTURAR_RM] usuario=%s facturó remisión packing_id=%s → %s',
            u.email, packing_id, resultado.get('rm')
        )
        return jsonify({'ok': True, **resultado}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        logger.exception('[FACTURAR_RM] Error Siesa packing_id=%s: %s', packing_id, e)
        return jsonify({'error': f'Error Siesa: {str(e)}'}), 502


@despacho_parcial_bp.route('/<int:packing_id>/facturar-rm-manual', methods=['POST'])
@jwt_required()
def facturar_rm_manual(packing_id: int):
    """
    POST /api/despacho_parcial/<packing_id>/facturar-rm-manual
    Body: {"tipo_rm": "RS", "consec_rm": 1234}

    Carril de emergencia: convierte una RM conocida a FE (142943) cuando
    el consecutivo no está en BD y API_v2_Ventas_Remisiones_DesdePedido no existe.
    El operario busca el número de RM en Siesa y lo ingresa manualmente.
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)
    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede facturar una tarea cancelada'}), 409

    body = request.get_json(silent=True) or {}
    tipo_rm   = str(body.get('tipo_rm', '')).strip().upper()
    consec_rm = body.get('consec_rm')

    if not tipo_rm or consec_rm is None:
        return jsonify({'error': 'Se requiere body {"tipo_rm": "RS", "consec_rm": 1234}'}), 400
    try:
        consec_rm = int(consec_rm)
    except (TypeError, ValueError):
        return jsonify({'error': 'consec_rm debe ser un entero'}), 400

    from app.services.despacho_parcial_service import DespachoParialService
    try:
        resultado = DespachoParialService.facturar_rm_con_consec(tarea, tipo_rm, consec_rm)
        logger.info(
            '[FACTURAR_RM_MANUAL] usuario=%s packing_id=%s %s-%s → ok',
            u.email, packing_id, tipo_rm, consec_rm
        )
        return jsonify({'ok': True, **resultado}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 409
    except Exception as e:
        logger.exception('[FACTURAR_RM_MANUAL] Error packing_id=%s: %s', packing_id, e)
        return jsonify({'error': f'Error Siesa: {str(e)}'}), 502
