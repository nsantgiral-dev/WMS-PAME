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
        almacen_id=data.get('almacen_id'),
        puede_usar_camara=data.get('puede_usar_camara', False),
        puede_picar=data.get('puede_picar', True),
        puede_empacar=data.get('puede_empacar', False),
    )
    usuario.set_password(data['password'])

    db.session.add(usuario)
    db.session.commit()

    return jsonify(usuario.to_dict()), 201


@auth_bp.route('/usuarios', methods=['GET'])
@jwt_required()
def listar_usuarios():
    """Lista todos los usuarios activos — para el panel de admin."""
    usuarios = Usuario.query.filter_by(activo=True).order_by(Usuario.nombre).all()
    return jsonify({'usuarios': [u.to_dict() for u in usuarios]}), 200


@auth_bp.route('/usuarios/<int:uid>', methods=['PUT'])
@jwt_required()
def actualizar_usuario(uid):
    """Actualiza nombre, rol, capacidades y almacén de un usuario."""
    usuario = Usuario.query.get_or_404(uid)
    data = request.get_json() or {}

    if 'nombre' in data:
        usuario.nombre = data['nombre']
    if 'rol' in data:
        usuario.rol = data['rol']
    if 'almacen_id' in data:
        usuario.almacen_id = data['almacen_id']
    if 'puede_usar_camara' in data:
        usuario.puede_usar_camara = bool(data['puede_usar_camara'])
    if 'puede_picar' in data:
        usuario.puede_picar = bool(data['puede_picar'])
    if 'puede_empacar' in data:
        usuario.puede_empacar = bool(data['puede_empacar'])
    if 'activo' in data:
        usuario.activo = bool(data['activo'])
    if 'password' in data and data['password']:
        usuario.set_password(data['password'])

    db.session.commit()
    return jsonify(usuario.to_dict()), 200