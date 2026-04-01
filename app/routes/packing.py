from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.packing import TareaPacking
from app.services.packing_service import PackingService
from app.services.connekta_gateway import connekta

packing_bp = Blueprint('packing', __name__)


@packing_bp.route('/', methods=['GET'])
@jwt_required()
def listar_tareas():
    estado = request.args.get('estado')
    almacen_id = request.args.get('almacen_id', type=int)
    page = request.args.get('page', 1, type=int)

    query = TareaPacking.query.order_by(TareaPacking.fecha_creacion.desc())

    if estado:
        query = query.filter_by(estado=estado)
    if almacen_id:
        query = query.filter_by(almacen_id=almacen_id)

    tareas = query.paginate(page=page, per_page=50, error_out=False)

    return jsonify({
        'tareas': [t.to_dict() for t in tareas.items],
        'total': tareas.total,
        'pagina_actual': page
    }), 200


@packing_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_tarea(id):
    tarea = TareaPacking.query.get_or_404(id)
    return jsonify(tarea.to_dict()), 200


@packing_bp.route('/crear-desde-picking', methods=['POST'])
@jwt_required()
def crear_desde_picking():
    data = request.get_json()
    requeridos = ['tareas_picking_ids', 'numero_pedido_siesa', 'almacen_id']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        tarea = PackingService.crear_desde_picking(
            tareas_picking_ids=data['tareas_picking_ids'],
            numero_pedido_siesa=data['numero_pedido_siesa'],
            almacen_id=data['almacen_id'],
            tipo_docto_pedido_siesa=data.get('tipo_docto_pedido_siesa', ''),
            consec_docto_pedido_siesa=data.get('consec_docto_pedido_siesa', '')
        )
        return jsonify({
            'mensaje': 'Tarea de packing creada',
            'tarea': tarea.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/crear-manual', methods=['POST'])
@jwt_required()
def crear_manual():
    data = request.get_json()
    requeridos = ['numero_pedido_siesa', 'almacen_id', 'items']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        tarea = PackingService.crear_manual(
            numero_pedido_siesa=data['numero_pedido_siesa'],
            almacen_id=data['almacen_id'],
            items=data['items'],
            tipo_docto_pedido_siesa=data.get('tipo_docto_pedido_siesa', ''),
            consec_docto_pedido_siesa=data.get('consec_docto_pedido_siesa', '')
        )
        return jsonify({
            'mensaje': 'Tarea de packing creada manualmente',
            'tarea': tarea.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/iniciar', methods=['PUT'])
@jwt_required()
def iniciar_tarea(id):
    empacador_id = int(get_jwt_identity())
    try:
        tarea = PackingService.iniciar(id, empacador_id)
        return jsonify(tarea.to_dict()), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/escanear', methods=['POST'])
@jwt_required()
def escanear_item(id):
    data = request.get_json()
    requeridos = ['producto_id', 'cantidad_real']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        resultado = PackingService.escanear_item(
            tarea_id=id,
            producto_id=data['producto_id'],
            cantidad_real=data['cantidad_real'],
            lote=data.get('lote')
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/<int:id>/confirmar', methods=['PUT'])
@jwt_required()
def confirmar_packing(id):
    """Paso 1: verifica ítems → estado VERIFICADO. NO dispara Siesa."""
    data = request.get_json() or {}
    try:
        PackingService.confirmar_packing(
            tarea_id=id,
            observaciones=data.get('observaciones'),
            forzar=data.get('forzar', False)
        )
        from app.models.packing import TareaPacking
        tarea = TareaPacking.query.get(id)
        return jsonify({
            'mensaje': 'Ítems verificados — declara las piezas físicas para cerrar',
            'tarea': tarea.to_dict(),
        }), 200
    except ValueError as e:
        error = e.args[0]
        if isinstance(error, dict):
            return jsonify(error), 409
        return jsonify({'error': error}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/<int:id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_packing(id):
    """
    Paso 2: declara bultos físicos, genera códigos de barras y dispara Siesa.
    Body: {"bultos": [{"tipo": "Caja", "cantidad": 2}, {"tipo": "Bolsa", "cantidad": 1}]}
    """
    data = request.get_json() or {}
    bultos_data = data.get('bultos', [])
    if not bultos_data:
        return jsonify({'error': 'Debes declarar al menos una pieza'}), 400
    try:
        bultos = PackingService.cerrar_packing(tarea_id=id, bultos_data=bultos_data)
        from app.models.packing import TareaPacking
        tarea = TareaPacking.query.get(id)
        return jsonify({
            'ok': True,
            'mensaje': f'{len(bultos)} pieza(s) registradas — Siesa generó la remisión',
            'siesa_triggered': tarea.siesa_triggered,
            'numero_pedido': tarea.numero_pedido_siesa,
            'cliente': tarea.cliente or '',
            'municipio': tarea.municipio or '',
            'bultos': [b.to_dict() for b in bultos]
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@packing_bp.route('/<int:id>/cancelar', methods=['PUT'])
@jwt_required()
def cancelar_tarea(id):
    data = request.get_json() or {}
    try:
        tarea = PackingService.cancelar(id, motivo=data.get('motivo'))
        return jsonify({'mensaje': 'Tarea cancelada', 'tarea': tarea.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@packing_bp.route('/connekta/estado', methods=['GET'])
@jwt_required()
def estado_connekta():
    """Verifica el estado de la integración con Siesa/Connekta."""
    return jsonify(connekta.estado()), 200