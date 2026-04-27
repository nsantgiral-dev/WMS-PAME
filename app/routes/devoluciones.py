"""
Rutas de Logística Inversa (Devoluciones físicas).

GET  /api/devoluciones/             → lista tareas PENDIENTE/EN_PROCESO
POST /api/devoluciones/<id>/ubicar  → el recepcionista confirma dónde pone la mercancía
POST /api/devoluciones/<id>/descartar → descarta la tarea (diferencial era error de Siesa)
"""
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.devolucion_service import listar_pendientes, confirmar_ubicacion, descartar
from app.routes._auth_helpers import Roles

devoluciones_bp = Blueprint('devoluciones', __name__)


@devoluciones_bp.route('/', methods=['GET'])
@jwt_required()
def listar():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.RECEPCION_ROLES + tuple(Roles.SUPERVISION):
        return jsonify({'error': 'Sin permiso para listar devoluciones'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    return jsonify({'tareas': listar_pendientes(almacen_id)}), 200


@devoluciones_bp.route('/<int:tarea_id>/ubicar', methods=['POST'])
@jwt_required()
def ubicar(tarea_id):
    from app.models.usuario import Usuario
    try:
        recepcionista_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(recepcionista_id)
    if not u or u.rol not in Roles.RECEPCION_ROLES:
        return jsonify({'error': 'Sin permiso para confirmar devoluciones'}), 403
    data = request.get_json()
    ubicacion_codigo = data.get('ubicacion_codigo', '').strip()
    es_averiado = data.get('es_averiado', False)
    observaciones = data.get('observaciones', '')

    if not ubicacion_codigo and not es_averiado:
        return jsonify({'error': 'Indica el código de ubicación o marca como averiado'}), 400

    # Si es averiado, el código de ubicación es AVERIADOS automáticamente
    if es_averiado:
        ubicacion_codigo = 'AVERIADOS'

    try:
        tarea = confirmar_ubicacion(tarea_id, ubicacion_codigo, recepcionista_id,
                                    es_averiado=es_averiado, observaciones=observaciones)
        return jsonify({'mensaje': 'Devolución ubicada — stock WMS actualizado', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@devoluciones_bp.route('/<int:tarea_id>/descartar', methods=['POST'])
@jwt_required()
def descartar_tarea(tarea_id):
    from app.models.usuario import Usuario
    try:
        recepcionista_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(recepcionista_id)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso para descartar devoluciones'}), 403
    data = request.get_json() or {}
    motivo = data.get('motivo', 'Descartado por recepcionista')
    try:
        tarea = descartar(tarea_id, recepcionista_id, motivo)
        return jsonify({'mensaje': 'Tarea descartada', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
