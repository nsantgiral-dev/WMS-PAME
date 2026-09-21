import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.recepcion import RecepcionMercancia, EstadoRecepcion
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

    from sqlalchemy.orm import selectinload as _sl
    from app.models.recepcion import ItemRecepcion as _IR
    query = (RecepcionMercancia.query
             .options(_sl(RecepcionMercancia.items).selectinload(_IR.producto))
             .order_by(RecepcionMercancia.fecha_creacion.desc()))
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
    from sqlalchemy.orm import selectinload as _sl
    from app.models.recepcion import ItemRecepcion as _IR
    recepcion = (RecepcionMercancia.query
                 .options(_sl(RecepcionMercancia.items).selectinload(_IR.producto))
                 .get_or_404(id))
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


@recepcion_bp.route('/<int:id>/averia', methods=['POST'])
@jwt_required()
def declarar_averia(id):
    """Declara cuántas de las unidades YA contadas llegaron averiadas.

    Puerta separada del escaneo porque el recepcionista cuenta rápido con la
    pistola y revisa después. La regla es la misma —vive en
    `RecepcionService._aplicar_averia`— pero el gesto es otro.
    """
    if not _es_recepcion_autorizado():
        return jsonify({'error': 'Sin permiso para declarar averías en recepción'}), 403
    # Pertenencia: una avería solo se declara sobre una recepción del propio
    # punto. El guard va ACÁ y no en el servicio a propósito — la autorización
    # es del borde; al servicio lo llaman también otros servicios, y meterle
    # una pregunta sobre «quién sos» lo volvería dependiente de la sesión.
    #
    # El admin queda exento: en producción no tiene bodega ni almacén, así que
    # un guard que falle cerrado lo dejaría fuera de todo.
    from app.models.recepcion import RecepcionMercancia
    from app.models.usuario import Usuario as _Usuario
    from app.services.alcance import bodega_de_la_recepcion, usuario_es_de_la_bodega
    # `db.session.get`, no el API legado de consulta: el trinquete de deuda lo
    # cuenta por texto y su tope solo puede bajar.
    from app.extensions import db as _db
    _u = _db.session.get(_Usuario, int(get_jwt_identity()))
    if _u and _u.rol != 'admin':
        _rec = _db.session.get(RecepcionMercancia, id)
        if not _rec:
            return jsonify({'error': 'Recepción no encontrada'}), 404
        if not usuario_es_de_la_bodega(_u, bodega_de_la_recepcion(_rec)):
            return jsonify({
                'error': 'Esa recepción no pertenece a tu punto de venta'
            }), 403

    data = request.get_json() or {}
    for campo in ('producto_id', 'cantidad_averiada'):
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    try:
        return jsonify(RecepcionService.declarar_averia(
            recepcion_id=id,
            producto_id=data['producto_id'],
            cantidad_averiada=data['cantidad_averiada'],
            motivo=data.get('motivo'),
        )), 200
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
            es_bonificacion=data.get('es_bonificacion', False),
            cantidad_averiada=data.get('cantidad_averiada', 0),
            motivo_averia=data.get('motivo_averia'),
            scan_id=data.get('scan_id'),
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
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(uid)
    if not usuario or usuario.rol not in Roles.RECEPCION_ROLES:
        return jsonify({'error': 'No autorizado'}), 403
    data = request.get_json() or {}
    try:
        recepcion = RecepcionService.confirmar_recepcion(
            recepcion_id=id,
            observaciones=data.get('observaciones'),
            num_remision_prov=data.get('num_remision_prov', '')
        )
        # Detector de fugas: registrar si algún item recibido está bloqueado
        fugas_detectadas = 0
        try:
            from app.services.bloqueo_recompra_service import BloqueoRecompraService
            for item in (recepcion.items or []):
                if item.producto_id:
                    fuga = BloqueoRecompraService.registrar_fuga(
                        producto_id=item.producto_id,
                        recepcion_id=recepcion.id,
                        oc_siesa=recepcion.numero_oc_siesa or '',
                        proveedor=recepcion.proveedor_codigo or '',
                        cantidad=int(item.cantidad_recibida or 0),
                    )
                    if fuga:
                        fugas_detectadas += 1
        except Exception as _e_fuga:
            logger.warning('[RECEPCION] Error verificando fugas: %s', _e_fuga)
        return jsonify({
            'mensaje': 'Recepción confirmada — Siesa está generando entrada contable',
            'recepcion': recepcion.to_dict(),
            'siesa_triggered': recepcion.siesa_triggered,
            'es_parcial': recepcion.es_parcial,
            'tiene_cross_dock': recepcion.tiene_cross_dock,
            'fugas_detectadas': fugas_detectadas,
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception(f'[RECEPCION] Error inesperado en confirmar_recepcion id={id}')
        return jsonify({'error': str(e)}), 500


@recepcion_bp.route('/<int:id>/cancelar', methods=['PUT'])
@jwt_required()
def cancelar_recepcion(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede cancelar recepciones'}), 403
    data = request.get_json() or {}
    recepcion = RecepcionMercancia.query.get_or_404(id)
    if recepcion.estado == EstadoRecepcion.CONFIRMADA:
        return jsonify({'error': 'No se puede cancelar una recepción ya confirmada'}), 400
    recepcion.estado = EstadoRecepcion.CANCELADA
    recepcion.observaciones = data.get('motivo')
    from app.extensions import db
    db.session.commit()
    return jsonify({'mensaje': 'Recepción cancelada', 'recepcion': recepcion.to_dict()}), 200