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
    activas = request.args.get('activas', '').lower() == 'true'
    page = request.args.get('page', 1, type=int)
    per_page = min(request.args.get('per_page', 50, type=int), 200)

    query = TareaPicking.query.order_by(
        TareaPicking.prioridad.desc(),
        TareaPicking.fecha_creacion.asc()
    )

    if activas:
        query = query.filter(TareaPicking.estado.in_(['PENDIENTE', 'EN_PROCESO', 'BLOQUEADO']))
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
    referencia = request.args.get('referencia')
    query = TareaPicking.query.filter(
        TareaPicking.estado == 'COMPLETADO',
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

    # with_for_update() — lock pesimista a nivel de fila.
    # Evita que dos operarios reciban la misma tarea si llaman simultáneamente.
    tarea = TareaPicking.query.filter_by(
        estado='PENDIENTE',
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
    Supervisor Guard — Operario reporta faltante o ubicación vacía.

    Flujo:
    1. Bloquea UbicacionProducto con FOR UPDATE.
    2. Incrementa `bloqueado` por la cantidad no encontrada.
    3. Si hubo short-pick (cantidad_encontrada > 0), confirma esa parte.
    4. Pone la tarea en BLOQUEADO.
    5. Crea SesionConteo EXCEPCION_PICKING → aparece en "Auditorías Urgentes".

    Payload: { motivo, cantidad_encontrada (int, opcional — default 0) }
    Motivos: UBICACION_VACIA | FALTANTE | MERCANCIA_AVERIADA | PRODUCTO_INCORRECTO
    """
    from app.models.inventario import UbicacionProducto
    from app.services.conteo_service import ConteoService

    operario_id = int(get_jwt_identity())
    data = request.get_json() or {}
    motivo = data.get('motivo', 'UBICACION_VACIA')
    cantidad_encontrada = int(data.get('cantidad_encontrada', 0))

    tarea = TareaPicking.query.get_or_404(id)

    if tarea.operario_id != operario_id:
        return jsonify({'error': 'Esta tarea no te pertenece'}), 403

    cantidad_faltante = max(0, tarea.cantidad_solicitada - cantidad_encontrada)

    # 1. Bloquear la fila de inventario con FOR UPDATE
    inv = (UbicacionProducto.query
           .filter_by(ubicacion_id=tarea.ubicacion_id, producto_id=tarea.producto_id)
           .with_for_update().first())

    if inv and cantidad_faltante > 0:
        inv.bloqueado = inv.bloqueado + cantidad_faltante

    # 2. Short-pick: si encontró algo, confirmar esa parte
    if cantidad_encontrada > 0:
        tarea.cantidad_recogida = cantidad_encontrada
        from app.models.inventario import MovimientoInventario
        import uuid as _uuid
        from datetime import datetime as _dt
        if inv:
            inv.cantidad = max(0, inv.cantidad - cantidad_encontrada)
            inv.reservado = max(0, inv.reservado - cantidad_encontrada)
        db.session.add(MovimientoInventario(
            producto_id=tarea.producto_id,
            ubicacion_id=tarea.ubicacion_id,
            almacen_id=tarea.almacen_id,
            tipo='SHORT_PICK',
            cantidad=cantidad_encontrada,
            motivo=f'Short-pick parcial — tarea {tarea.codigo} — faltó {cantidad_faltante}',
            numero_documento=tarea.referencia_documento,
            usuario_id=operario_id,
            idempotency_key=f'SP-{tarea.id}-{int(_dt.utcnow().timestamp()*1000)}',
        ))

    # 3. Bloquear la tarea
    tarea.estado = 'BLOQUEADO'
    tarea.operario_id = None

    # 4. Crear auditoría urgente solo para faltantes reales
    auditoria_id = None
    if motivo in ('UBICACION_VACIA', 'FALTANTE') and cantidad_faltante > 0:
        sesion = ConteoService.generar_auditoria_por_excepcion(
            tarea_picking_id=tarea.id,
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id,
            almacen_id=tarea.almacen_id,
        )
        auditoria_id = sesion.id

    db.session.commit()

    return jsonify({
        'ok': True,
        'mensaje': 'Problema reportado — el jefe de almacén lo resolverá',
        'motivo': motivo,
        'tarea_id': id,
        'cantidad_encontrada': cantidad_encontrada,
        'cantidad_faltante': cantidad_faltante,
        'auditoria_id': auditoria_id,
    }), 200