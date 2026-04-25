import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.recepcion import RecepcionMercancia
from app.services.recepcion_service import RecepcionService
from app.routes._auth_helpers import _solo_admin, Roles

recepcion_bp = Blueprint('recepcion', __name__)
logger = logging.getLogger(__name__)


def _es_recepcion_autorizado():
    """Retorna el usuario si puede gestionar recepciones, None si no."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.rol in Roles.RECEPCION_ROLES else None


@recepcion_bp.route('/', methods=['GET'])
@jwt_required()
def listar_recepciones():
    if not _es_recepcion_autorizado():
        return jsonify({'error': 'Sin permiso para listar recepciones'}), 403
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
    if not _es_recepcion_autorizado():
        return jsonify({'error': 'Sin permiso para ver recepciones'}), 403
    recepcion = RecepcionMercancia.query.get_or_404(id)
    return jsonify(recepcion.to_dict()), 200


@recepcion_bp.route('/crear', methods=['POST'])
@jwt_required()
def crear_recepcion():
    if not _es_recepcion_autorizado():
        return jsonify({'error': 'No autorizado — se requiere rol admin, jefe_almacen o recepcionista'}), 403
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
    if not _es_recepcion_autorizado():
        return jsonify({'error': 'Sin permiso para iniciar recepciones'}), 403
    try:
        recepcionista_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
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
    if not _es_recepcion_autorizado():
        return jsonify({'error': 'Sin permiso para escanear productos en recepción'}), 403
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
            es_empaque=data.get('es_empaque', False),
            es_bonificacion=data.get('es_bonificacion', False)
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
    from app.models.usuario import Usuario
    uid = get_jwt_identity()
    usuario = Usuario.query.get(int(uid))
    if not usuario or usuario.rol not in Roles.RECEPCION_ROLES:
        return jsonify({'error': 'No autorizado'}), 403
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
        logger.exception(f'[RECEPCION] Error inesperado en confirmar_recepcion id={id}')
        return jsonify({'error': str(e)}), 500


@recepcion_bp.route('/<int:id>/reiniciar-conteo', methods=['PUT'])
@jwt_required()
def reiniciar_conteo(id):
    if not _es_recepcion_autorizado():
        return jsonify({'error': 'No autorizado'}), 403
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
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede cancelar recepciones'}), 403
    data = request.get_json() or {}
    recepcion = RecepcionMercancia.query.get_or_404(id)
    if recepcion.estado == 'CONFIRMADA':
        return jsonify({'error': 'No se puede cancelar una recepción ya confirmada'}), 400
    recepcion.estado = 'CANCELADA'
    recepcion.observaciones = data.get('motivo')
    from app.extensions import db
    db.session.commit()
    return jsonify({'mensaje': 'Recepción cancelada', 'recepcion': recepcion.to_dict()}), 200