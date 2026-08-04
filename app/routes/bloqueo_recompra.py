"""
Endpoints de bloqueo de recompra.

GET  /api/compras/bloqueados           — lista de SKUs bloqueados con capital inmovilizado
POST /api/compras/bloqueados/poblar    — genera lista inicial (velocity=0 12m + stock>0)
POST /api/compras/bloqueados/verificar — verifica si items de una OC están bloqueados
POST /api/compras/bloqueados/<id>/desbloquear — desbloquea con restricciones
GET  /api/compras/bloqueados/fugas     — lista de fugas (compras por fuera del sistema)
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_compras, _get_uid

bloqueo_bp = Blueprint('bloqueo_recompra', __name__)


@bloqueo_bp.route('/bloqueados', methods=['GET'])
@jwt_required()
def listar_bloqueados():
    """Vista en pesos: SKUs bloqueados × costo × existencia."""
    if not _es_compras():
        return jsonify({'error': 'Solo admin/gerente puede ver bloqueos'}), 403
    from app.services.bloqueo_recompra_service import BloqueoRecompraService
    resultado = BloqueoRecompraService.vista_capital_inmovilizado()
    return jsonify(resultado), 200


@bloqueo_bp.route('/bloqueados/poblar', methods=['POST'])
@jwt_required()
def poblar_bloqueos():
    """Genera lista inicial de bloqueos (velocity=0 12m + stock>0)."""
    if not _es_compras():
        return jsonify({'error': 'Solo admin/gerente puede poblar bloqueos'}), 403
    from app.services.bloqueo_recompra_service import BloqueoRecompraService
    try:
        resultado = BloqueoRecompraService.poblar_lista_inicial()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True, **resultado}), 200


@bloqueo_bp.route('/bloqueados/verificar', methods=['POST'])
@jwt_required()
def verificar_oc():
    """Verifica si items de una OC están bloqueados (pre-check antes de generar OC)."""
    if not _es_compras():
        return jsonify({'error': 'Sin permiso para verificar órdenes de compra'}), 403
    data = request.get_json() or {}
    codigos = data.get('codigos', [])
    if not codigos:
        return jsonify({'error': 'codigos es requerido'}), 400
    from app.services.bloqueo_recompra_service import BloqueoRecompraService
    resultado = BloqueoRecompraService.verificar_oc(codigos)
    return jsonify(resultado), 200


@bloqueo_bp.route('/bloqueados/<int:bloqueo_id>/desbloquear', methods=['POST'])
@jwt_required()
def desbloquear(bloqueo_id):
    """Desbloquea un producto con restricciones (cantidad + vigencia + motivo)."""
    if not _es_compras():
        return jsonify({'error': 'Sin permiso para desbloquear SKUs'}), 403
    uid = _get_uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    data = request.get_json() or {}
    motivo = data.get('motivo', '')
    cantidad = data.get('cantidad_autorizada', 0)
    vigencia = data.get('vigencia_dias', 30)

    from app.services.bloqueo_recompra_service import BloqueoRecompraService
    try:
        bloqueo = BloqueoRecompraService.desbloquear(
            bloqueo_id, uid, motivo, cantidad, vigencia)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'bloqueo': bloqueo.to_dict()}), 200


@bloqueo_bp.route('/bloqueados/fugas', methods=['GET'])
@jwt_required()
def listar_fugas():
    """Lista de fugas: recepciones de SKUs bloqueados (compras por fuera del sistema)."""
    if not _es_compras():
        return jsonify({'error': 'Solo admin/gerente puede ver fugas'}), 403
    from app.services.bloqueo_recompra_service import BloqueoRecompraService
    fugas = BloqueoRecompraService.listar_fugas()
    return jsonify({'fugas': fugas, 'total': len(fugas)}), 200
