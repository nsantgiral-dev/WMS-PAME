from datetime import datetime
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.picking import TareaPicking, EstadoPicking
from app.services.picking_service import PickingService
from app.routes._auth_helpers import Roles
from app.models.usuario import Usuario

picking_bp = Blueprint('picking', __name__)


@picking_bp.route('/', methods=['GET'])
@jwt_required()
def listar_tareas():
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso para listar tareas de picking'}), 403
    estado = request.args.get('estado')
    operario_id = request.args.get('operario_id', type=int)
    almacen_id = request.args.get('almacen_id', type=int)
    activas = request.args.get('activas', '').lower() == 'true'
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)

    from sqlalchemy.orm import selectinload as _sl
    query = (TareaPicking.query
             .options(_sl(TareaPicking.producto), _sl(TareaPicking.ubicacion))
             .order_by(TareaPicking.prioridad.desc(), TareaPicking.fecha_creacion.asc()))

    if activas:
        query = query.filter(TareaPicking.estado.in_([EstadoPicking.PENDIENTE, EstadoPicking.EN_PROCESO, EstadoPicking.BLOQUEADO]))
    elif estado:
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


@picking_bp.route('/purgar-ceros', methods=['DELETE'])
@jwt_required()
def purgar_picks_cero():
    """Elimina tareas COMPLETADO con cantidad_recogida=0 (registros basura de confirms fallidos)."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    referencia = request.args.get('referencia')
    query = TareaPicking.query.filter(
        TareaPicking.estado == EstadoPicking.COMPLETADO,
        TareaPicking.cantidad_recogida == 0
    )
    if referencia:
        query = query.filter_by(referencia_documento=referencia)
    tareas = query.all()
    count = len(tareas)
    for t in tareas:
        db.session.delete(t)
    db.session.commit()
    return jsonify({'eliminadas': count}), 200


@picking_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_tarea(id):
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u:
        return jsonify({'error': 'Usuario no encontrado'}), 401
    tarea = TareaPicking.query.get_or_404(id)
    # Operarios solo ven sus propias tareas; supervisores/admin ven todo
    if u.rol not in Roles.SUPERVISION and tarea.operario_id != uid:
        return jsonify({'error': 'Sin permiso para ver esta tarea'}), 403
    return jsonify(tarea.to_dict()), 200


@picking_bp.route('/crear', methods=['POST'])
@jwt_required()
def crear_tarea():
    # [42] Solo admin, supervisor o jefe_almacen pueden crear tareas de picking manualmente
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso — se requiere rol admin, supervisor o jefe_almacen'}), 403

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
    from app.models.usuario import Usuario
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(usuario_id)
    if not u or u.rol not in Roles.SUPERVISION + (Roles.OPERARIO,):
        return jsonify({'error': 'Sin permiso para iniciar tareas de picking'}), 403
    tarea_check = TareaPicking.query.get_or_404(id)
    if u.rol not in Roles.SUPERVISION and tarea_check.operario_id is not None and tarea_check.operario_id != usuario_id:
        return jsonify({'error': 'Esta tarea no te pertenece'}), 403
    try:
        tarea = PickingService.iniciar_picking(id, usuario_id)
        return jsonify(tarea.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@picking_bp.route('/<int:id>/confirmar', methods=['PUT'])
@jwt_required()
def confirmar_tarea(id):
    from app.models.usuario import Usuario
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(usuario_id)
    if not u or u.rol not in Roles.SUPERVISION + (Roles.OPERARIO,):
        return jsonify({'error': 'Sin permiso para confirmar picking'}), 403
    tarea_check = TareaPicking.query.get_or_404(id)
    if u.rol not in Roles.SUPERVISION and tarea_check.operario_id != usuario_id:
        return jsonify({'error': 'Esta tarea no te pertenece'}), 403
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
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso — se requiere rol admin, supervisor o jefe_almacen'}), 403
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


@picking_bp.route('/<int:id>/reabrir', methods=['PUT'])
@jwt_required()
def reabrir_tarea(id):
    """
    Admin reabre una tarea BLOQUEADA → PENDIENTE.
    Libera el inventario congelado y la devuelve al pool sin operario.
    Solo admin o supervisor.
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Solo admin, supervisor o jefe puede reabrir tareas'}), 403
    try:
        tarea = PickingService.reabrir_picking(tarea_id=id)
        return jsonify({'mensaje': 'Tarea reabierta — vuelve al pool de picking', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@picking_bp.route('/<int:id>/auditar', methods=['POST'])
@jwt_required()
def auditar_tarea(id):
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Solo admin o supervisor puede auditar tareas'}), 403
    data = request.get_json() or {}
    resultado = data.get('resultado', '').strip()
    if not resultado:
        return jsonify({'error': 'El campo resultado es requerido'}), 400
    try:
        tarea = PickingService.auditar_tarea(
            tarea_id=id,
            admin_id=uid,
            resultado=resultado,
            cantidad_hallada=int(data.get('cantidad_hallada', 0) or 0),
            ubicacion_hallada=data.get('ubicacion_hallada'),
            observaciones=data.get('observaciones'),
        )
        return jsonify({'mensaje': 'Auditoría registrada', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@picking_bp.route('/fefo', methods=['POST'])
@jwt_required()
def calcular_fefo():
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in (Roles.OPERARIO, Roles.JEFE_ALMACEN, Roles.ADMIN, Roles.SUPERVISOR):
        return jsonify({'error': 'Sin permiso para calcular FEFO'}), 403
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
    from app.models.usuario import Usuario
    try:
        operario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(operario_id)
    if not u or u.rol not in Roles.SUPERVISION + (Roles.OPERARIO,):
        return jsonify({'error': 'Sin permiso para recibir tareas de picking'}), 403

    tarea_activa = TareaPicking.query.filter_by(
        operario_id=operario_id,
        estado=EstadoPicking.EN_PROCESO
    ).first()

    if tarea_activa:
        return jsonify({
            'tarea': tarea_activa.to_dict(),
            'mensaje': 'Tienes una tarea en proceso'
        }), 200

    # with_for_update() — lock pesimista a nivel de fila.
    # Evita que dos operarios reciban la misma tarea si llaman simultáneamente.
    tarea = TareaPicking.query.filter_by(
        estado=EstadoPicking.PENDIENTE,
        operario_id=None
    ).order_by(
        TareaPicking.prioridad.desc(),
        TareaPicking.fecha_creacion.asc()
    ).with_for_update(skip_locked=True).first()

    if not tarea:
        return jsonify({
            'sin_tareas': True,
            'mensaje': 'No hay tareas pendientes en la cola'
        }), 200

    tarea_id = tarea.id
    tarea.operario_id = operario_id
    tarea.estado = EstadoPicking.EN_PROCESO
    tarea.fecha_inicio = datetime.utcnow()
    tarea.cantidad_recogida = 0
    tarea.empaques_escaneados = 0
    db.session.commit()

    # Recargar después del commit — expire_on_commit invalida los atributos del objeto
    tarea = TareaPicking.query.get(tarea_id)
    return jsonify({
        'tarea': tarea.to_dict(),
        'mensaje': 'Tarea asignada automáticamente'
    }), 200


@picking_bp.route('/mis-tareas-activas', methods=['GET'])
@jwt_required()
def mis_tareas_activas():
    """Tareas activas del operario actual."""
    try:
        operario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    from sqlalchemy.orm import selectinload as _sl
    tareas = (TareaPicking.query
              .options(_sl(TareaPicking.producto), _sl(TareaPicking.ubicacion))
              .filter(
                  TareaPicking.operario_id == operario_id,
                  TareaPicking.estado.in_([EstadoPicking.PENDIENTE, EstadoPicking.EN_PROCESO])
              ).order_by(TareaPicking.prioridad.desc()).all())
    return jsonify({
        'tareas': [t.to_dict() for t in tareas],
        'total': len(tareas)
    }), 200


@picking_bp.route('/<int:id>/reportar-problema', methods=['POST'])
@jwt_required()
def reportar_problema(id):
    """
    Supervisor Guard — Operario reporta faltante o ubicación vacía.

    Payload: { motivo, cantidad_encontrada (int, opcional — default 0) }
    Motivos: UBICACION_VACIA | FALTANTE | MERCANCIA_AVERIADA | PRODUCTO_INCORRECTO
    """
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in (Roles.OPERARIO, Roles.JEFE_ALMACEN, Roles.ADMIN, Roles.SUPERVISOR):
        return jsonify({'error': 'Sin permiso para reportar problemas en picking'}), 403
    data = request.get_json() or {}
    try:
        resultado = PickingService.reportar_problema(
            tarea_id=id,
            operario_id=uid,
            motivo=data.get('motivo', 'UBICACION_VACIA'),
            cantidad_encontrada=int(data.get('cantidad_encontrada', 0)),
            observaciones=data.get('observaciones') or None,
        )
        return jsonify(resultado), 200
    except PermissionError as e:
        return jsonify({'error': str(e)}), 403
    except ValueError as e:
        return jsonify({'error': str(e)}), 404


