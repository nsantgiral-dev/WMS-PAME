"""
Monitor de Muelle — controlador HTTP.
Toda la lógica de negocio vive en MuelleService.
"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_admin_o_jefe
from app.services.muelle_service import MuelleService, ConflictError

muelle_bp = Blueprint('muelle', __name__)


@muelle_bp.route('/listos', methods=['GET'])
@jwt_required()
def listos():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso para ver bultos listos en muelle'}), 403
    return jsonify(MuelleService.listar_bultos_listos()), 200


@muelle_bp.route('/asignar', methods=['POST'])
@jwt_required()
def asignar_a_ruta():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere rol admin o jefe_almacen'}), 403
    data = request.get_json()
    ruta_id = data.get('ruta_id')
    if not ruta_id:
        return jsonify({'error': 'ruta_id es requerido'}), 400
    try:
        resultado = MuelleService.asignar_a_ruta(
            ruta_id=ruta_id,
            bultos_ids=data.get('bultos_ids', []),
            pedido_siesa=data.get('pedido_siesa'),
        )
    except ConflictError as e:
        return jsonify({'error': str(e)}), 409
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@muelle_bp.route('/desasignar/<int:id>', methods=['DELETE'])
@jwt_required()
def desasignar_de_ruta(id):
    if not _es_admin_o_jefe():
        return jsonify({'error': 'No autorizado'}), 403
    try:
        resultado = MuelleService.desasignar_de_ruta(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@muelle_bp.route('/cargar/<string:codigo_barras>', methods=['POST'])
@jwt_required()
def cargar_bulto(codigo_barras):
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere rol admin o jefe_almacen'}), 403
    data = request.get_json(silent=True) or {}
    ruta_id_raw = data.get('ruta_id')
    if not ruta_id_raw:
        return jsonify({'error': 'ruta_id es requerido para verificación'}), 400
    try:
        ruta_id = int(ruta_id_raw)
    except (ValueError, TypeError):
        return jsonify({'error': 'ruta_id debe ser un número entero válido'}), 400
    try:
        resultado = MuelleService.cargar_bulto(codigo_barras, ruta_id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@muelle_bp.route('/manifiesto', methods=['GET'])
@jwt_required()
def manifiesto():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso para ver el manifiesto'}), 403
    return jsonify(MuelleService.obtener_manifiesto()), 200


@muelle_bp.route('/historial', methods=['GET'])
@jwt_required()
def historial():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso para ver historial de muelle'}), 403
    return jsonify({'bultos': MuelleService.obtener_historial()}), 200
