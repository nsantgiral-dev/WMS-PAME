from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.exc import IntegrityError
from app.extensions import db
from app.models.almacen import Almacen
from app.models.ubicacion import Ubicacion
from app.services import layout_service

almacenes_bp = Blueprint('almacenes', __name__)


from app.routes._auth_helpers import _solo_admin, _es_personal_almacen, _es_admin_o_jefe

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
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({'error': 'El código de almacén ya existe'}), 409
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

    tipo_zona = data.get('tipo_zona')
    if tipo_zona is not None and tipo_zona not in (*layout_service.ZONAS_VALIDAS, 'GENERAL'):
        return jsonify({'error': f'tipo_zona debe ser una de {layout_service.ZONAS_VALIDAS} o GENERAL'}), 400

    ubicacion = Ubicacion(
        codigo=data['codigo'],
        almacen_id=almacen.id,
        zona=data.get('zona'),
        pasillo=data.get('pasillo'),
        estante=data.get('estante'),
        nivel=data.get('nivel'),
        tipo=data.get('tipo', 'estanteria'),
        capacidad_maxima=data.get('capacidad_maxima'),
        tipo_zona=tipo_zona or 'GENERAL',
        origen='MANUAL',
    )

    db.session.add(ubicacion)
    db.session.commit()
    return jsonify(ubicacion.to_dict()), 201


# ──────────────────────────────────────────────────────────────────────────────
# Módulo de Layout — ubicaciones gestionadas 100% en el WMS
# ──────────────────────────────────────────────────────────────────────────────

@almacenes_bp.route('/<int:id>/pasillos-disponibles', methods=['GET'])
@jwt_required()
def pasillos_disponibles(id):
    """Letras de pasillo aún no usadas en este almacén — A-Z, luego AA, AB..."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    cantidad = request.args.get('cantidad', 5, type=int)
    return jsonify({'letras': layout_service.letras_disponibles(id, cantidad)}), 200


@almacenes_bp.route('/<int:id>/ubicaciones/fila', methods=['POST'])
@jwt_required()
def crear_fila(id):
    """
    Nivel 1 del Mecanismo A: crea las N posiciones de una fila en bloque.
    Payload: { pasillo, fila, cantidad_posiciones, tipo_zona, capacidad_maxima? }
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    campos = ('pasillo', 'fila', 'cantidad_posiciones', 'tipo_zona')
    if not all(data.get(c) for c in campos):
        return jsonify({'error': f'Requeridos: {", ".join(campos)}'}), 400
    try:
        creadas = layout_service.crear_fila(
            almacen_id=id,
            pasillo=data['pasillo'],
            fila=int(data['fila']),
            cantidad_posiciones=int(data['cantidad_posiciones']),
            tipo_zona=data['tipo_zona'],
            capacidad_maxima=data.get('capacidad_maxima'),
        )
        return jsonify({'ubicaciones': [u.to_dict() for u in creadas]}), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/<int:id>/ubicaciones/averias', methods=['POST'])
@jwt_required()
def crear_averias(id):
    """Crea la siguiente ubicación AVERIAS disponible (AVE1, AVE2...)."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    ub = layout_service.crear_ubicacion_averias(id, data.get('capacidad_maxima'))
    return jsonify(ub.to_dict()), 201


@almacenes_bp.route('/ubicaciones/<int:ubicacion_id>', methods=['PATCH'])
@jwt_required()
def reclasificar_ubicacion(ubicacion_id):
    """
    Reclasifica una ubicación existente: zona, capacidad, estado, o liberar
    su slot de PICKING. Guardarraíles en layout_service (no toca stock activo).
    Payload: { tipo_zona?, capacidad_maxima?, activo?, liberar_slot? }
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    data = request.get_json() or {}
    try:
        resultado = layout_service.reclasificar_ubicacion(
            ubicacion_id=ubicacion_id,
            tipo_zona=data.get('tipo_zona'),
            capacidad_maxima=data.get('capacidad_maxima'),
            activo=data.get('activo'),
            liberar_slot=bool(data.get('liberar_slot', False)),
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/ubicaciones/<int:ubicacion_id>/asignar', methods=['POST'])
@jwt_required()
def asignar_ubicacion(ubicacion_id):
    """
    Mecanismo B: amarra un SKU a una ubicación y suma la cantidad contada.
    Payload: { producto_id, cantidad }
    """
    usuario = _es_admin_o_jefe()
    if not usuario:
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    data = request.get_json() or {}
    if not data.get('producto_id') or data.get('cantidad') is None:
        return jsonify({'error': 'producto_id y cantidad son requeridos'}), 400
    try:
        resultado = layout_service.asignar_producto(
            ubicacion_id=ubicacion_id,
            producto_id=int(data['producto_id']),
            cantidad=int(data['cantidad']),
            usuario_id=usuario.id,
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/<int:id>/ubicaciones/importar', methods=['POST'])
@jwt_required()
def importar_ubicaciones(id):
    """
    Mecanismo A (opción masiva): sube un Excel (ubicacion_codigo | producto_codigo
    | cantidad) para el poblamiento inicial. No aborta en la primera fila mala —
    reporta éxito/error fila por fila.
    """
    usuario = _es_admin_o_jefe()
    if not usuario:
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    if 'archivo' not in request.files:
        return jsonify({'error': 'Falta el archivo (campo "archivo")'}), 400
    try:
        resultado = layout_service.importar_excel(id, request.files['archivo'], usuario.id)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': f'No se pudo procesar el archivo: {e}'}), 400


@almacenes_bp.route('/<int:id>/layout', methods=['GET'])
@jwt_required()
def layout_completo(id):
    """Vista completa del layout de un almacén — no solo PICKING, todo el mapa."""
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso'}), 403
    Almacen.query.get_or_404(id)
    ubicaciones = Ubicacion.query.filter_by(almacen_id=id).order_by(
        Ubicacion.tipo_zona, Ubicacion.pasillo, Ubicacion.estante, Ubicacion.codigo
    ).all()

    # Stock actual por ubicación en una sola query — evita N+1.
    from app.models.inventario import UbicacionProducto
    ub_ids = [u.id for u in ubicaciones]
    stock_map = {}
    if ub_ids:
        filas = db.session.query(
            UbicacionProducto.ubicacion_id,
            db.func.coalesce(db.func.sum(UbicacionProducto.cantidad), 0),
        ).filter(UbicacionProducto.ubicacion_id.in_(ub_ids)).group_by(
            UbicacionProducto.ubicacion_id
        ).all()
        stock_map = {ubid: total for ubid, total in filas}

    resultado = []
    for u in ubicaciones:
        d = u.to_dict()
        d['stock_actual'] = stock_map.get(u.id, 0)
        resultado.append(d)

    return jsonify({
        'ubicaciones': resultado,
        'total': len(resultado),
        'sin_clasificar': sum(1 for u in ubicaciones if u.tipo_zona == 'GENERAL'),
    }), 200