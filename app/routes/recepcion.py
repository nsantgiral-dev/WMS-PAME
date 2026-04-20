from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.recepcion import RecepcionMercancia
from app.services.recepcion_service import RecepcionService

recepcion_bp = Blueprint('recepcion', __name__)


@recepcion_bp.route('/', methods=['GET'])
@jwt_required()
def listar_recepciones():
    estado = request.args.get('estado')
    almacen_id = request.args.get('almacen_id', type=int)
    page = request.args.get('page', 1, type=int)

    query = RecepcionMercancia.query.order_by(
        RecepcionMercancia.fecha_creacion.desc()
    )
    if estado:
        query = query.filter_by(estado=estado)
    if almacen_id:
        query = query.filter_by(almacen_id=almacen_id)

    recepciones = query.paginate(page=page, per_page=50, error_out=False)

    return jsonify({
        'recepciones': [r.to_dict() for r in recepciones.items],
        'total': recepciones.total,
        'pagina_actual': page
    }), 200


@recepcion_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_recepcion(id):
    recepcion = RecepcionMercancia.query.get_or_404(id)
    return jsonify(recepcion.to_dict()), 200


@recepcion_bp.route('/crear', methods=['POST'])
@jwt_required()
def crear_recepcion():
    data = request.get_json()
    requeridos = ['numero_oc_siesa', 'almacen_id', 'items']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        recepcion = RecepcionService.crear_recepcion(
            numero_oc_siesa=data['numero_oc_siesa'],
            almacen_id=data['almacen_id'],
            proveedor_codigo=data.get('proveedor_codigo', ''),
            proveedor_nombre=data.get('proveedor_nombre', ''),
            items=data['items'],
            co_oc_siesa=data.get('co_oc_siesa', ''),
            tipo_docto_oc_siesa=data.get('tipo_docto_oc_siesa', ''),
            consec_docto_oc_siesa=data.get('consec_docto_oc_siesa', '')
        )
        return jsonify({
            'mensaje': 'Recepción creada — listo para iniciar escaneo ciego',
            'recepcion': recepcion.to_dict()
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@recepcion_bp.route('/<int:id>/iniciar', methods=['PUT'])
@jwt_required()
def iniciar_recepcion(id):
    recepcionista_id = int(get_jwt_identity())
    try:
        recepcion = RecepcionService.iniciar(id, recepcionista_id)
        return jsonify({
            'mensaje': 'Recepción iniciada — escanea los productos',
            'recepcion': recepcion.to_dict()
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@recepcion_bp.route('/<int:id>/escanear', methods=['POST'])
@jwt_required()
def escanear_producto(id):
    data = request.get_json()
    requeridos = ['producto_id', 'cantidad']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        resultado = RecepcionService.escanear_producto(
            recepcion_id=id,
            producto_id=data['producto_id'],
            cantidad=data['cantidad'],
            lote=data.get('lote'),
            fecha_vencimiento=data.get('fecha_vencimiento'),
            es_empaque=data.get('es_empaque', False)
        )
        return jsonify(resultado), 200
    except ValueError as e:
        error = e.args[0]
        if isinstance(error, dict):
            return jsonify(error), 409
        return jsonify({'error': error}), 400


@recepcion_bp.route('/<int:id>/confirmar', methods=['PUT'])
@jwt_required()
def confirmar_recepcion(id):
    data = request.get_json() or {}
    try:
        recepcion = RecepcionService.confirmar_recepcion(
            recepcion_id=id,
            observaciones=data.get('observaciones'),
            num_remision_prov=data.get('num_remision_prov', '')
        )
        return jsonify({
            'mensaje': 'Recepción confirmada — Siesa está generando entrada contable',
            'recepcion': recepcion.to_dict(),
            'siesa_triggered': recepcion.siesa_triggered,
            'es_parcial': recepcion.es_parcial,
            'tiene_cross_dock': recepcion.tiene_cross_dock
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@recepcion_bp.route('/<int:id>/reiniciar-conteo', methods=['PUT'])
@jwt_required()
def reiniciar_conteo(id):
    from app.extensions import db
    from app.models.recepcion import ItemRecepcion
    recepcion = RecepcionMercancia.query.get_or_404(id)
    if recepcion.estado not in ('EN_PROCESO', 'ABIERTA'):
        return jsonify({'error': 'Solo se puede reiniciar una recepción en proceso'}), 400
    for item in recepcion.items:
        item.cantidad_recibida = 0
        item.empaques_escaneados = 0
        item.destino = None
        item.ubicacion_id = None
    db.session.commit()
    return jsonify({'mensaje': 'Conteo reiniciado', 'recepcion': recepcion.to_dict()}), 200


@recepcion_bp.route('/<int:id>/cancelar', methods=['PUT'])
@jwt_required()
def cancelar_recepcion(id):
    data = request.get_json() or {}
    recepcion = RecepcionMercancia.query.get_or_404(id)
    if recepcion.estado == 'CONFIRMADA':
        return jsonify({'error': 'No se puede cancelar una recepción ya confirmada'}), 400
    recepcion.estado = 'CANCELADA'
    recepcion.observaciones = data.get('motivo')
    from app.extensions import db
    db.session.commit()
    return jsonify({'mensaje': 'Recepción cancelada', 'recepcion': recepcion.to_dict()}), 200