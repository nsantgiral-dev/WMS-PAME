from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.picking import TareaPicking
from app.services.picking_service import PickingService

picking_bp = Blueprint('picking', __name__)


@picking_bp.route('/', methods=['GET'])
@jwt_required()
def listar_tareas():
    estado = request.args.get('estado')
    operario_id = request.args.get('operario_id', type=int)
    almacen_id = request.args.get('almacen_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)

    query = TareaPicking.query.order_by(
        TareaPicking.prioridad.desc(),
        TareaPicking.fecha_creacion.asc()
    )

    if estado:
        query = query.filter_by(estado=estado)
    if operario_id:
        query = query.filter_by(operario_id=operario_id)
    if almacen_id:
        query = query.filter_by(almacen_id=almacen_id)

    tareas = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'tareas': [t.to_dict() for t in tareas.items],
        'total': tareas.total,
        'pagina_actual': page
    }), 200


@picking_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_tarea(id):
    tarea = TareaPicking.query.get_or_404(id)
    return jsonify(tarea.to_dict()), 200


@picking_bp.route('/crear', methods=['POST'])
@jwt_required()
def crear_tarea():
    data = request.get_json()
    requeridos = ['producto_id', 'cantidad', 'almacen_id']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    try:
        tareas = PickingService.crear_tareas(
            producto_id=data['producto_id'],
            cantidad=data['cantidad'],
            almacen_id=data['almacen_id'],
            referencia_documento=data.get('referencia_documento'),
            tipo_documento=data.get('tipo_documento'),
            operario_id=data.get('operario_id'),
            prioridad=data.get('prioridad', 1)
        )
        return jsonify({
            'mensaje': f'{len(tareas)} tarea(s) de picking creadas',
            'tareas': [t.to_dict() for t in tareas]
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@picking_bp.route('/<int:id>/iniciar', methods=['PUT'])
@jwt_required()
def iniciar_tarea(id):
    usuario_id = int(get_jwt_identity())
    try:
        tarea = PickingService.iniciar_picking(id, usuario_id)
        return jsonify(tarea.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@picking_bp.route('/<int:id>/confirmar', methods=['PUT'])
@jwt_required()
def confirmar_tarea(id):
    usuario_id = int(get_jwt_identity())
    data = request.get_json()
    if 'cantidad_recogida' not in data:
        return jsonify({'error': 'cantidad_recogida requerida'}), 400
    try:
        tarea = PickingService.confirmar_picking(
            tarea_id=id,
            cantidad_recogida=data['cantidad_recogida'],
            usuario_id=usuario_id
        )
        return jsonify({
            'mensaje': 'Picking confirmado exitosamente',
            'tarea': tarea.to_dict()
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@picking_bp.route('/<int:id>/cancelar', methods=['PUT'])
@jwt_required()
def cancelar_tarea(id):
    data = request.get_json() or {}
    try:
        tarea = PickingService.cancelar_picking(
            tarea_id=id,
            motivo=data.get('motivo')
        )
        return jsonify({
            'mensaje': 'Tarea cancelada',
            'tarea': tarea.to_dict()
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@picking_bp.route('/fefo', methods=['POST'])
@jwt_required()
def calcular_fefo():
    data = request.get_json()
    requeridos = ['producto_id', 'cantidad', 'almacen_id']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    resultado = PickingService.calcular_fefo(
        producto_id=data['producto_id'],
        cantidad=data['cantidad'],
        almacen_id=data['almacen_id']
    )
    return jsonify(resultado), 200


@picking_bp.route('/siguiente-tarea', methods=['GET'])
@jwt_required()
def siguiente_tarea():
    """
    Dispensador automático — el operario pide trabajo, el sistema asigna.
    Nunca espera asignación manual.
    """
    operario_id = int(get_jwt_identity())

    tarea_activa = TareaPicking.query.filter_by(
        operario_id=operario_id,
        estado='EN_PROCESO'
    ).first()

    if tarea_activa:
        return jsonify({
            'tarea': tarea_activa.to_dict(),
            'mensaje': 'Tienes una tarea en proceso'
        }), 200

    tarea = TareaPicking.query.filter_by(
        estado='PENDIENTE',
        operario_id=None
    ).order_by(
        TareaPicking.prioridad.desc(),
        TareaPicking.fecha_creacion.asc()
    ).first()

    if not tarea:
        return jsonify({
            'sin_tareas': True,
            'mensaje': 'No hay tareas pendientes en la cola'
        }), 200

    tarea.operario_id = operario_id
    tarea.estado = 'EN_PROCESO'
    tarea.fecha_inicio = datetime.utcnow()
    db.session.commit()

    return jsonify({
        'tarea': tarea.to_dict(),
        'mensaje': 'Tarea asignada automáticamente'
    }), 200


@picking_bp.route('/mis-tareas-activas', methods=['GET'])
@jwt_required()
def mis_tareas_activas():
    """Tareas activas del operario actual."""
    operario_id = int(get_jwt_identity())
    tareas = TareaPicking.query.filter(
        TareaPicking.operario_id == operario_id,
        TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO'])
    ).order_by(TareaPicking.prioridad.desc()).all()
    return jsonify({
        'tareas': [t.to_dict() for t in tareas],
        'total': len(tareas)
    }), 200


@picking_bp.route('/<int:id>/reportar-problema', methods=['POST'])
@jwt_required()
def reportar_problema(id):
    """
    Operario reporta un problema — Ubicación vacía o Mercancía averiada.
    Bloquea la tarea, la saca de la cola del operario y
    la envía al dashboard del jefe para resolución.
    El operario pasa automáticamente a la siguiente tarea.
    """
    operario_id = int(get_jwt_identity())
    data = request.get_json() or {}
    motivo = data.get('motivo', 'PROBLEMA_REPORTADO')

    tarea = TareaPicking.query.get_or_404(id)

    if tarea.operario_id != operario_id:
        return jsonify({'error': 'Esta tarea no te pertenece'}), 403

    tarea.estado = 'BLOQUEADO'
    tarea.operario_id = None
    db.session.commit()

    return jsonify({
        'mensaje': 'Problema reportado — el jefe de almacén lo resolverá',
        'motivo': motivo,
        'tarea_id': id
    }), 200