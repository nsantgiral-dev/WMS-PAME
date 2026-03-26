from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from app.extensions import db
from app.models.almacen import Almacen
from app.models.ubicacion import Ubicacion

almacenes_bp = Blueprint('almacenes', __name__)

@almacenes_bp.route('/', methods=['GET'])
@jwt_required()
def listar_almacenes():
    almacenes = Almacen.query.filter_by(activo=True).all()
    return jsonify([a.to_dict() for a in almacenes]), 200


@almacenes_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_almacen(id):
    almacen = Almacen.query.get_or_404(id)
    return jsonify(almacen.to_dict()), 200


@almacenes_bp.route('/', methods=['POST'])
@jwt_required()
def crear_almacen():
    data = request.get_json()
    if not data or not data.get('codigo') or not data.get('nombre'):
        return jsonify({'error': 'Codigo y nombre requeridos'}), 400

    if Almacen.query.filter_by(codigo=data['codigo']).first():
        return jsonify({'error': 'El codigo ya existe'}), 409

    almacen = Almacen(
        codigo=data['codigo'],
        nombre=data['nombre'],
        direccion=data.get('direccion'),
        ciudad=data.get('ciudad')
    )

    db.session.add(almacen)
    db.session.commit()
    return jsonify(almacen.to_dict()), 201


@almacenes_bp.route('/<int:id>/ubicaciones', methods=['GET'])
@jwt_required()
def listar_ubicaciones(id):
    almacen = Almacen.query.get_or_404(id)
    ubicaciones = Ubicacion.query.filter_by(
        almacen_id=almacen.id,
        activo=True
    ).all()
    return jsonify([u.to_dict() for u in ubicaciones]), 200


@almacenes_bp.route('/<int:id>/ubicaciones', methods=['POST'])
@jwt_required()
def crear_ubicacion(id):
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