from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.almacen import Almacen
from app.models.ubicacion import Ubicacion

almacenes_bp = Blueprint('almacenes', __name__)


from app.routes._auth_helpers import _solo_admin, _es_personal_almacen

@almacenes_bp.route('/', methods=['GET'])
@jwt_required()
def listar_almacenes():
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para listar almacenes'}), 403
    almacenes = Almacen.query.filter_by(activo=True).all()
    return jsonify([a.to_dict() for a in almacenes]), 200


@almacenes_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_almacen(id):
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para consultar almacenes'}), 403
    almacen = Almacen.query.get_or_404(id)
    return jsonify(almacen.to_dict()), 200


@almacenes_bp.route('/', methods=['POST'])
@jwt_required()
def crear_almacen():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear almacenes'}), 403
    data = request.get_json()
    if not data or not data.get('codigo') or not data.get('nombre'):
        return jsonify({'error': 'Codigo y nombre requeridos'}), 400

    if Almacen.query.filter_by(codigo=data['codigo']).first():
        return jsonify({'error': 'El codigo ya existe'}), 409

    almacen = Almacen(
        codigo=data['codigo'],
        nombre=data['nombre'],
        direccion=data.get('direccion'),
        ciudad=data.get('ciudad'),
        bodega_siesa_id=data.get('bodega_siesa_id'),
        centro_op_siesa=data.get('centro_op_siesa'),
    )

    db.session.add(almacen)
    db.session.commit()
    return jsonify(almacen.to_dict()), 201


@almacenes_bp.route('/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_almacen(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar almacenes'}), 403
    almacen = Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    if 'codigo' in data:
        almacen.codigo = data['codigo']
    if 'nombre' in data:
        almacen.nombre = data['nombre']
    if 'direccion' in data:
        almacen.direccion = data['direccion']
    if 'ciudad' in data:
        almacen.ciudad = data['ciudad']
    if 'activo' in data:
        almacen.activo = bool(data['activo'])
    if 'bodega_siesa_id' in data:
        almacen.bodega_siesa_id = data['bodega_siesa_id'] or None
    if 'centro_op_siesa' in data:
        almacen.centro_op_siesa = data['centro_op_siesa'] or None
    db.session.commit()
    return jsonify(almacen.to_dict()), 200


@almacenes_bp.route('/<int:id>/ubicaciones', methods=['GET'])
@jwt_required()
def listar_ubicaciones(id):
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para listar ubicaciones'}), 403
    almacen = Almacen.query.get_or_404(id)
    ubicaciones = Ubicacion.query.filter_by(
        almacen_id=almacen.id,
        activo=True
    ).all()
    return jsonify([u.to_dict() for u in ubicaciones]), 200


@almacenes_bp.route('/<int:id>/ubicaciones', methods=['POST'])
@jwt_required()
def crear_ubicacion(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear ubicaciones'}), 403
    almacen = Almacen.query.get_or_404(id)
    data = request.get_json()

    if not data or not data.get('codigo'):
        return jsonify({'error': 'Codigo requerido'}), 400

    if Ubicacion.query.filter_by(codigo=data['codigo']).first():
        return jsonify({'error': 'El codigo ya existe'}), 409

    ubicacion = Ubicacion(
        codigo=data['codigo'],
        almacen_id=almacen.id,
        zona=data.get('zona'),
        pasillo=data.get('pasillo'),
        estante=data.get('estante'),
        nivel=data.get('nivel'),
        tipo=data.get('tipo', 'estanteria'),
        capacidad_maxima=data.get('capacidad_maxima')
    )

    db.session.add(ubicacion)
    db.session.commit()
    return jsonify(ubicacion.to_dict()), 201