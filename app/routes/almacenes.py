from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.exc import IntegrityError
from app.extensions import db
from app.models.almacen import Almacen
from app.models.ubicacion import Ubicacion
from app.services import layout_service

almacenes_bp = Blueprint('almacenes', __name__)


from app.routes._auth_helpers import (_solo_admin, _es_personal_almacen,
                                       _es_admin_o_jefe, _es_control_flota)

@almacenes_bp.route('/', methods=['GET'])
@jwt_required()
def listar_almacenes():
    # `_es_control_flota` se suma aparte en vez de ensanchar
    # `_es_personal_almacen`: ese helper autoriza operaciones de almacén, y
    # meter ahí a control_flota le daría permisos que el procedimiento le niega.
    # La pantalla de flota necesita la lista de sedes para registrar una
    # custodia, y nada más.
    if not (_es_personal_almacen() or _es_control_flota()):
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


@almacenes_bp.route('/<int:id>/ubicaciones/cuerpo', methods=['POST'])
@jwt_required()
def crear_cuerpo(id):
    """
    Mecanismo A: crea un Cuerpo completo — sus Entrepaños (Nivel) y Huecos, en
    bloque. Un Cuerpo es 100% de una sola Zona (tipo_zona) — PICKING, RESERVA e
    IMPORTADOS se arman como Cuerpos separados, no mezclados por Nivel dentro del mismo Cuerpo.
    Payload: { pasillo, fila, cuerpo, cantidad_entrepanos, tipo_zona, huecos_por_nivel? }
    tipo_zona debe ser PICKING, RESERVA o IMPORTADOS. huecos_por_nivel es una lista de N
    enteros (uno por entrepaño, N=cantidad_entrepanos, en orden de Nivel 1..N);
    si no viene, cada entrepaño nace con 1 hueco.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    campos = ('pasillo', 'fila', 'cuerpo', 'cantidad_entrepanos', 'tipo_zona')
    if not all(data.get(c) for c in campos):
        return jsonify({'error': f'Requeridos: {", ".join(campos)}'}), 400
    try:
        huecos_por_nivel = data.get('huecos_por_nivel')
        if huecos_por_nivel is not None:
            huecos_por_nivel = [int(h) for h in huecos_por_nivel]
        creadas = layout_service.crear_cuerpo(
            almacen_id=id,
            pasillo=data['pasillo'],
            fila=int(data['fila']),
            cuerpo=int(data['cuerpo']),
            cantidad_entrepanos=int(data['cantidad_entrepanos']),
            tipo_zona=data['tipo_zona'],
            huecos_por_nivel=huecos_por_nivel,
        )
        return jsonify({'ubicaciones': [u.to_dict() for u in creadas]}), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/<int:id>/ubicaciones/cuerpo', methods=['PUT'])
@jwt_required()
def editar_cuerpo(id):
    """
    Remodula un Cuerpo existente: cambia su cantidad de entrepaños/huecos.
    Borra los huecos actuales (y sus SKUs) y reconstruye limpio con la nueva
    numeración — misma zona de siempre, no se puede cambiar aquí (para eso
    está el PATCH de reclasificar). El stock físico real se devuelve
    automáticamente a SIESA-GENERAL antes de borrar — no se pierde nada.
    Payload: { pasillo, fila, cuerpo, cantidad_entrepanos, huecos_por_nivel? }
    Bloquea (400) si algún hueco del cuerpo tiene historial operativo real.
    """
    usuario = _es_admin_o_jefe()
    if not usuario:
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    campos = ('pasillo', 'fila', 'cuerpo', 'cantidad_entrepanos')
    if not all(data.get(c) for c in campos):
        return jsonify({'error': f'Requeridos: {", ".join(campos)}'}), 400
    try:
        huecos_por_nivel = data.get('huecos_por_nivel')
        if huecos_por_nivel is not None:
            huecos_por_nivel = [int(h) for h in huecos_por_nivel]
        creadas = layout_service.editar_cuerpo(
            almacen_id=id,
            pasillo=data['pasillo'],
            fila=int(data['fila']),
            cuerpo=int(data['cuerpo']),
            cantidad_entrepanos=int(data['cantidad_entrepanos']),
            huecos_por_nivel=huecos_por_nivel,
            usuario_id=usuario.id,
        )
        return jsonify({'ubicaciones': [u.to_dict() for u in creadas]}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/<int:id>/ubicaciones/cuerpo', methods=['PATCH'])
@jwt_required()
def reclasificar_cuerpo(id):
    """
    Reclasifica todo un Cuerpo de una vez: cambia su zona (RESERVA<->PICKING<->
    AVERIAS<->IMPORTADOS) o lo desactiva completo (activo=false — camino recomendado para
    retirar un cuerpo con historial real sin perder su trazabilidad, cuando
    el DELETE de este mismo path lo bloquea).
    Payload: { pasillo, fila, cuerpo, tipo_zona?, activo? }
    Al menos uno de tipo_zona/activo debe venir presente.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    campos = ('pasillo', 'fila', 'cuerpo')
    if not all(data.get(c) for c in campos):
        return jsonify({'error': f'Requeridos: {", ".join(campos)}'}), 400
    if data.get('tipo_zona') is None and data.get('activo') is None:
        return jsonify({'error': 'Indica al menos un campo a cambiar: tipo_zona o activo'}), 400
    try:
        resultado = layout_service.reclasificar_cuerpo(
            almacen_id=id,
            pasillo=data['pasillo'],
            fila=int(data['fila']),
            cuerpo=int(data['cuerpo']),
            tipo_zona=data.get('tipo_zona'),
            activo=data.get('activo'),
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/<int:id>/ubicaciones/cuerpo', methods=['DELETE'])
@jwt_required()
def eliminar_cuerpo(id):
    """
    Elimina un Cuerpo completo — todos sus entrepaños y huecos, sin excepción.
    Todo o nada: bloquea (400) si CUALQUIER hueco tiene stock activo o
    historial operativo real. Usa el PATCH de este mismo path con
    activo=false para retirar un cuerpo con historial sin perder su rastro.
    Payload: { pasillo, fila, cuerpo, forzar? }
    forzar=true se salta el guardarraíl y borra en cascada el historial real
    (TareaPicking/TareaReposicion/MovimientoInventario) — pérdida de datos
    permanente, requiere admin (no basta jefe_almacen).
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    campos = ('pasillo', 'fila', 'cuerpo')
    if not all(data.get(c) for c in campos):
        return jsonify({'error': f'Requeridos: {", ".join(campos)}'}), 400
    forzar = bool(data.get('forzar', False))
    if forzar and not _solo_admin():
        return jsonify({'error': 'forzar=true requiere rol admin'}), 403
    try:
        resultado = layout_service.eliminar_cuerpo(
            almacen_id=id,
            pasillo=data['pasillo'],
            fila=int(data['fila']),
            cuerpo=int(data['cuerpo']),
            forzar=forzar,
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/<int:id>/ubicaciones/fila', methods=['PATCH'])
@jwt_required()
def editar_fila(id):
    """
    Edita en bloque todas las posiciones de una fila ya creada.
    Payload: { pasillo, fila, tipo_zona?, capacidad_maxima?, activo? }
    Al menos uno de tipo_zona/capacidad_maxima/activo debe venir presente.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    if not data.get('pasillo') or not data.get('fila'):
        return jsonify({'error': 'Requeridos: pasillo, fila'}), 400
    if data.get('tipo_zona') is None and data.get('capacidad_maxima') is None and data.get('activo') is None:
        return jsonify({'error': 'Indica al menos un campo a cambiar: tipo_zona, capacidad_maxima o activo'}), 400
    try:
        resultado = layout_service.editar_fila(
            almacen_id=id,
            pasillo=data['pasillo'],
            fila=int(data['fila']),
            tipo_zona=data.get('tipo_zona'),
            capacidad_maxima=data.get('capacidad_maxima'),
            activo=data.get('activo'),
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/<int:id>/ubicaciones/fila', methods=['DELETE'])
@jwt_required()
def eliminar_fila(id):
    """
    Elimina en bloque las posiciones de una fila que nunca se usaron (sin stock,
    sin historial de picking/reposición/movimientos). Pensado para deshacer una
    fila creada por error, no para dar de baja infraestructura en operación.
    Payload: { pasillo, fila, forzar? }
    forzar=true se salta el guardarraíl y borra en cascada el historial real
    por posición — pérdida de datos permanente, requiere admin.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    Almacen.query.get_or_404(id)
    data = request.get_json() or {}
    if not data.get('pasillo') or not data.get('fila'):
        return jsonify({'error': 'Requeridos: pasillo, fila'}), 400
    forzar = bool(data.get('forzar', False))
    if forzar and not _solo_admin():
        return jsonify({'error': 'forzar=true requiere rol admin'}), 403
    try:
        resultado = layout_service.eliminar_fila(
            almacen_id=id,
            pasillo=data['pasillo'],
            fila=int(data['fila']),
            forzar=forzar,
        )
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@almacenes_bp.route('/ubicaciones/<int:ubicacion_id>', methods=['DELETE'])
@jwt_required()
def eliminar_ubicacion(ubicacion_id):
    """
    Elimina una sola ubicación que nunca se usó (sin stock ni historial).
    Mismo guardarraíl que eliminar_fila, a nivel de una sola posición.
    Query string: ?forzar=true se salta el guardarraíl y borra en cascada el
    historial real (TareaPicking/TareaReposicion/MovimientoInventario) —
    pérdida de datos permanente, requiere admin (no basta jefe_almacen).
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    forzar = request.args.get('forzar', '').lower() == 'true'
    if forzar and not _solo_admin():
        return jsonify({'error': 'forzar=true requiere rol admin'}), 403
    try:
        resultado = layout_service.eliminar_ubicacion(ubicacion_id, forzar=forzar)
        return jsonify(resultado), 200
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
    Payload: { producto_id, cantidad, capacidad_maxima? }
    capacidad_maxima solo se acepta si la ubicación es PICKING (ver asignar_producto).
    """
    usuario = _es_admin_o_jefe()
    if not usuario:
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    data = request.get_json() or {}
    if not data.get('producto_id') or data.get('cantidad') is None:
        return jsonify({'error': 'producto_id y cantidad son requeridos'}), 400
    try:
        capacidad_maxima = data.get('capacidad_maxima')
        resultado = layout_service.asignar_producto(
            ubicacion_id=ubicacion_id,
            producto_id=int(data['producto_id']),
            cantidad=int(data['cantidad']),
            usuario_id=usuario.id,
            capacidad_maxima=int(capacidad_maxima) if capacidad_maxima is not None else None,
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