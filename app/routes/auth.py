from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from app.extensions import db
from app.models.usuario import Usuario

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email y password requeridos'}), 400

    usuario = Usuario.query.filter_by(email=data['email'], activo=True).first()

    if not usuario or not usuario.check_password(data['password']):
        return jsonify({'error': 'Credenciales inválidas'}), 401

    token = create_access_token(identity=str(usuario.id))

    return jsonify({
        'token': token,
        'usuario': usuario.to_dict()
    }), 200


@auth_bp.route('/me', methods=['GET'])
@jwt_required()
def me():
    usuario_id = get_jwt_identity()
    usuario = Usuario.query.get(int(usuario_id))
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    return jsonify(usuario.to_dict()), 200


@auth_bp.route('/register', methods=['POST'])
@jwt_required()
def register():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password') or not data.get('nombre'):
        return jsonify({'error': 'Nombre, email y password requeridos'}), 400

    if Usuario.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'El email ya existe'}), 409

    usuario = Usuario(
        nombre=data['nombre'],
        email=data['email'],
        rol=data.get('rol', 'operario'),
        almacen_id=data.get('almacen_id')
    )
    usuario.set_password(data['password'])

    db.session.add(usuario)
    db.session.commit()

    return jsonify(usuario.to_dict()), 201