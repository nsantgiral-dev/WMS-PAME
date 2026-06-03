from datetime import timedelta
from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token, jwt_required, get_jwt_identity
from app.extensions import db
from app.models.usuario import Usuario

auth_bp = Blueprint('auth', __name__)


from app.routes._auth_helpers import _solo_admin, Roles  # noqa: F401

_ROLES_VALIDOS = (
    Roles.ADMIN, Roles.SUPERVISOR, Roles.JEFE_ALMACEN, Roles.GERENTE,
    Roles.OPERARIO, Roles.EMPACADOR, Roles.RECEPCIONISTA, Roles.TIENDA, Roles.CONDUCTOR,
    Roles.COMPRAS, Roles.PICKER_TRASLADO, Roles.PACKER_TRASLADO,
)

@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({'error': 'Email y password requeridos'}), 400

    usuario = Usuario.query.filter_by(email=data['email'], activo=True).first()

    if not usuario or not usuario.check_password(data['password']):
        return jsonify({'error': 'Credenciales inválidas'}), 401

    expires = timedelta(days=30) if usuario.rol == 'conductor' else timedelta(days=7)
    token = create_access_token(identity=str(usuario.id), expires_delta=expires)

    return jsonify({
        'token': token,
        'usuario': usuario.to_dict()
    }), 200


@auth_bp.route('/me', methods=['GET'])
@jwt_required()
def me():
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    return jsonify(usuario.to_dict()), 200


@auth_bp.route('/register', methods=['POST'])
@jwt_required()
def register():
    if not _solo_admin():
        return jsonify({'error': 'Solo un administrador puede crear usuarios'}), 403

    data = request.get_json()
    if not data or not data.get('email') or not data.get('password') or not data.get('nombre'):
        return jsonify({'error': 'Nombre, email y password requeridos'}), 400

    if Usuario.query.filter_by(email=data['email']).first():
        return jsonify({'error': 'El email ya existe'}), 409

    # [40] Validar campos numéricos antes de convertir para evitar ValueError/500
    _cap_raw = data.get('capacidad_diaria_conteo', 15)
    try:
        _cap = max(0, int(_cap_raw)) if _cap_raw is not None else 15
    except (ValueError, TypeError):
        return jsonify({'error': 'capacidad_diaria_conteo debe ser un número entero'}), 400

    _rol_nuevo = data.get('rol', 'operario')
    if _rol_nuevo not in _ROLES_VALIDOS:
        return jsonify({'error': f'Rol inválido: {_rol_nuevo!r}. Roles válidos: {list(_ROLES_VALIDOS)}'}), 400

    usuario = Usuario(
        nombre=data['nombre'],
        email=data['email'],
        rol=_rol_nuevo,
        almacen_id=data.get('almacen_id'),
        puede_usar_camara=data.get('puede_usar_camara', False),
        puede_picar=data.get('puede_picar', True),
        puede_empacar=data.get('puede_empacar', False),
        puede_abastecer=data.get('puede_abastecer', False),
        capacidad_diaria_conteo=_cap,
        bodega_siesa_id=data.get('bodega_siesa_id'),
        siesa_co_id=data.get('siesa_co_id'),
        nombre_punto_venta=data.get('nombre_punto_venta'),
    )
    usuario.set_password(data['password'])

    db.session.add(usuario)
    db.session.flush()  # genera usuario.id sin hacer commit

    if _rol_nuevo == 'conductor':
        from app.models.conductor import Conductor
        cedula = (data.get('cedula') or '').strip()
        if cedula and not Conductor.query.filter_by(usuario_id=usuario.id).first():
            db.session.add(Conductor(
                nombre=usuario.nombre,
                cedula=cedula,
                telefono=(data.get('telefono') or '').strip() or None,
                usuario_id=usuario.id,
            ))

    try:
        db.session.commit()
    except Exception as _e_reg:
        db.session.rollback()
        if 'cedula' in str(_e_reg).lower() or 'conductor' in str(_e_reg).lower():
            return jsonify({'error': 'Cédula ya registrada en el sistema'}), 409
        raise

    return jsonify(usuario.to_dict()), 201


@auth_bp.route('/usuarios', methods=['GET'])
@jwt_required()
def listar_usuarios():
    """Lista todos los usuarios activos — solo admin."""
    if not _solo_admin():
        return jsonify({'error': 'Solo un administrador puede ver la lista de usuarios'}), 403
    usuarios = Usuario.query.filter_by(activo=True).order_by(Usuario.nombre).all()
    return jsonify({'usuarios': [u.to_dict() for u in usuarios]}), 200


@auth_bp.route('/usuarios/<int:uid>', methods=['PUT'])
@jwt_required()
def actualizar_usuario(uid):
    """Actualiza nombre, rol, capacidades y almacén de un usuario. Solo admin."""
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo un administrador puede modificar usuarios'}), 403

    usuario = Usuario.query.get_or_404(uid)
    data = request.get_json() or {}

    if 'nombre' in data:
        usuario.nombre = data['nombre']
    if 'rol' in data:
        # [B1] Validar que el rol sea uno de los valores definidos
        if data['rol'] not in _ROLES_VALIDOS:
            return jsonify({'error': f'Rol inválido: {data["rol"]!r}. Roles válidos: {list(_ROLES_VALIDOS)}'}), 400
        # Nadie puede auto-escalarse — admin solo puede cambiar rol de otros
        if uid == admin.id and data['rol'] != 'admin':
            return jsonify({'error': 'No puedes cambiar tu propio rol'}), 400
        usuario.rol = data['rol']
    if 'almacen_id' in data:
        usuario.almacen_id = data['almacen_id']
    if 'puede_usar_camara' in data:
        usuario.puede_usar_camara = bool(data['puede_usar_camara'])
    if 'puede_picar' in data:
        usuario.puede_picar = bool(data['puede_picar'])
    if 'puede_empacar' in data:
        usuario.puede_empacar = bool(data['puede_empacar'])
    if 'puede_abastecer' in data:
        usuario.puede_abastecer = bool(data['puede_abastecer'])
    if 'capacidad_diaria_conteo' in data:
        cap = data['capacidad_diaria_conteo']
        try:
            usuario.capacidad_diaria_conteo = max(0, int(cap)) if cap is not None else 15
        except (TypeError, ValueError):
            return jsonify({'error': 'capacidad_diaria_conteo debe ser un número entero'}), 400
    if 'activo' in data:
        usuario.activo = bool(data['activo'])
    if 'bodega_siesa_id' in data:
        usuario.bodega_siesa_id = data['bodega_siesa_id'] or None
    if 'siesa_co_id' in data:
        usuario.siesa_co_id = data['siesa_co_id'] or None
    if 'nombre_punto_venta' in data:
        usuario.nombre_punto_venta = data['nombre_punto_venta'] or None
    if 'password' in data and data['password']:
        usuario.set_password(data['password'])

    db.session.commit()

    if usuario.rol == 'conductor':
        from app.models.conductor import Conductor
        conductor = Conductor.query.filter_by(usuario_id=usuario.id).first()
        cedula = (data.get('cedula') or '').strip()
        telefono_c = (data.get('telefono') or '').strip() or None
        if conductor:
            if 'nombre' in data:
                conductor.nombre = usuario.nombre
            if telefono_c is not None:
                conductor.telefono = telefono_c
            db.session.commit()
        elif cedula:
            db.session.add(Conductor(
                nombre=usuario.nombre,
                cedula=cedula,
                telefono=telefono_c,
                usuario_id=usuario.id,
            ))
            db.session.commit()

    return jsonify(usuario.to_dict()), 200