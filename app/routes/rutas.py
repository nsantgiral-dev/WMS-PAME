"""
Rutas de Despacho — controlador HTTP.
Toda la lógica de negocio vive en RutaService.
"""
import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models.conductor import Conductor
from app.models.ruta_despacho import RutaDespacho
from app.routes._auth_helpers import _es_admin_o_jefe, _solo_admin, Roles
from app.services.ruta_service import RutaService, ConflictError

logger = logging.getLogger(__name__)

rutas_bp = Blueprint('rutas', __name__)


def _uid() -> int:
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None


def _usuario():
    from app.models.usuario import Usuario
    uid = _uid()
    return Usuario.query.get(uid) if uid else None


# ── Conductores ──────────────────────────────────────────────────

@rutas_bp.route('/conductores', methods=['GET'])
@jwt_required()
def listar_conductores():
    u = _usuario()
    if not u or u.rol not in Roles.GESTION + (Roles.CONDUCTOR,):
        return jsonify({'error': 'Sin permiso para listar conductores'}), 403
    solo_activos = request.args.get('activos', 'true').lower() == 'true'
    puede_ver = u.rol in Roles.ALMACEN
    return jsonify({'conductores': RutaService.listar_conductores(solo_activos, puede_ver)}), 200


@rutas_bp.route('/conductores', methods=['POST'])
@jwt_required()
def crear_conductor():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede registrar conductores'}), 403
    try:
        c = RutaService.crear_conductor(request.get_json())
    except ConflictError as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'conductor': c.to_dict()}), 201


@rutas_bp.route('/conductores/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_conductor(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar conductores'}), 403
    try:
        c = RutaService.actualizar_conductor(id, request.get_json())
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'conductor': c.to_dict()}), 200


@rutas_bp.route('/conductores/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_conductor(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede desactivar conductores'}), 403
    try:
        RutaService.desactivar_conductor(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'ok': True}), 200


# ── Vehículos ─────────────────────────────────────────────────────

@rutas_bp.route('/vehiculos', methods=['GET'])
@jwt_required()
def listar_vehiculos():
    u = _usuario()
    if not u or u.rol not in Roles.GESTION + (Roles.CONDUCTOR,):
        return jsonify({'error': 'Sin permiso para listar vehículos'}), 403
    solo_activos = request.args.get('activos', 'true').lower() == 'true'
    return jsonify({'vehiculos': RutaService.listar_vehiculos(solo_activos)}), 200


@rutas_bp.route('/vehiculos', methods=['POST'])
@jwt_required()
def crear_vehiculo():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede registrar vehículos'}), 403
    try:
        v = RutaService.crear_vehiculo(request.get_json())
    except ConflictError as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'vehiculo': v.to_dict()}), 201


@rutas_bp.route('/vehiculos/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_vehiculo(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar vehículos'}), 403
    try:
        v = RutaService.actualizar_vehiculo(id, request.get_json())
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'vehiculo': v.to_dict()}), 200


@rutas_bp.route('/vehiculos/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_vehiculo(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede desactivar vehículos'}), 403
    try:
        RutaService.desactivar_vehiculo(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'ok': True}), 200


# ── Rutas Maestras ───────────────────────────────────────────────

@rutas_bp.route('/maestras', methods=['GET'])
@jwt_required()
def listar_maestras():
    u = _usuario()
    if not u or u.rol not in Roles.GESTION + (Roles.CONDUCTOR,):
        return jsonify({'error': 'Sin permiso para listar rutas maestras'}), 403
    solo_activas = request.args.get('activas', 'true').lower() == 'true'
    return jsonify({'maestras': RutaService.listar_maestras(solo_activas)}), 200


@rutas_bp.route('/maestras/<int:id>', methods=['GET'])
@jwt_required()
def obtener_maestra(id):
    u = _usuario()
    if not u or u.rol not in Roles.GESTION + (Roles.CONDUCTOR,):
        return jsonify({'error': 'Sin permiso para obtener ruta maestra'}), 403
    try:
        m = RutaService.obtener_maestra(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'maestra': m.to_dict()}), 200


@rutas_bp.route('/maestras', methods=['POST'])
@jwt_required()
def crear_maestra():
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear rutas maestras'}), 403
    try:
        m = RutaService.crear_maestra(request.get_json())
    except ConflictError as e:
        return jsonify({'error': str(e)}), 409
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'maestra': m.to_dict()}), 201


@rutas_bp.route('/maestras/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_maestra(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede modificar rutas maestras'}), 403
    try:
        m = RutaService.actualizar_maestra(id, request.get_json())
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'maestra': m.to_dict()}), 200


@rutas_bp.route('/maestras/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_maestra(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede desactivar rutas maestras'}), 403
    try:
        RutaService.desactivar_maestra(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'ok': True}), 200


# ── Programar viaje desde plantilla ─────────────────────────────

@rutas_bp.route('/programar', methods=['POST'])
@jwt_required()
def programar_viaje():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede programar viajes'}), 403
    try:
        ruta = RutaService.programar_viaje(request.get_json())
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ruta': ruta.to_dict()}), 201


# ── Rutas de Despacho ────────────────────────────────────────────

@rutas_bp.route('/', methods=['GET'])
@jwt_required()
def listar_rutas():
    u = _usuario()
    if not u:
        return jsonify({'error': 'Usuario no encontrado'}), 401
    if u.rol not in Roles.GESTION + (Roles.CONDUCTOR,):
        return jsonify({'error': 'Sin permiso para listar rutas de despacho'}), 403

    conductor_id = request.args.get('conductor_id', type=int)
    if u.rol == Roles.CONDUCTOR:
        conductor = Conductor.query.filter_by(usuario_id=_uid()).first()
        conductor_id = conductor.id if conductor else -1  # fuerza lista vacía si no vinculado

    paginado = RutaService.listar_rutas(
        conductor_id=conductor_id,
        vehiculo_id=request.args.get('vehiculo_id', type=int),
        estado=request.args.get('estado'),
        fecha=request.args.get('fecha'),
        page=request.args.get('page', 1, type=int),
    )
    return jsonify({'rutas': [r.to_dict() for r in paginado.items], 'total': paginado.total}), 200


@rutas_bp.route('/', methods=['POST'])
@jwt_required()
def crear_ruta():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede crear rutas'}), 403
    try:
        ruta = RutaService.crear_ruta(request.get_json())
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ruta': ruta.to_dict()}), 201


@rutas_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_ruta(id):
    u = _usuario()
    if not u or u.rol not in Roles.GESTION + (Roles.CONDUCTOR,):
        return jsonify({'error': 'Sin permiso para acceder a rutas de despacho'}), 403
    try:
        ruta = RutaService.obtener_ruta(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    if u.rol == Roles.CONDUCTOR:
        conductor = Conductor.query.filter_by(usuario_id=_uid()).first()
        if not conductor or ruta.conductor_id != conductor.id:
            return jsonify({'error': 'Sin permiso para ver esta ruta'}), 403
    return jsonify({'ruta': ruta.to_dict(include_bultos=True)}), 200


@rutas_bp.route('/<int:id>/iniciar', methods=['POST'])
@jwt_required()
def iniciar_ruta(id):
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede iniciar el cargue'}), 403
    try:
        resultado = RutaService.iniciar_ruta(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@rutas_bp.route('/<int:id>/sugeridos', methods=['GET'])
@jwt_required()
def sugeridos_ruta(id):
    ruta = RutaDespacho.query.get_or_404(id)
    conductor_ruta = Conductor.query.filter_by(usuario_id=_uid(), activo=True).first()
    if not _es_admin_o_jefe() and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a los sugeridos de esta ruta'}), 403
    try:
        sugeridos = RutaService.obtener_sugeridos(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'sugeridos': sugeridos, 'total': len(sugeridos)}), 200


@rutas_bp.route('/<int:id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_ruta(id):
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede cerrar rutas'}), 403
    try:
        ruta = RutaService.cerrar_ruta(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'ruta': ruta.to_dict(include_bultos=True)}), 200


@rutas_bp.route('/<int:id>/entregar', methods=['POST'])
@jwt_required()
def entregar_ruta(id):
    ruta = RutaDespacho.query.get_or_404(id)
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    conductor_ruta = Conductor.query.filter_by(usuario_id=uid, activo=True).first()
    if not _es_admin_o_jefe() and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a esta ruta'}), 403
    try:
        resultado = RutaService.entregar_ruta(id, request.get_json() or {}, uid)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.error(f'[RUTAS] Error en entregar_ruta {id}: {e}', exc_info=True)
        return jsonify({'error': 'Error registrando entrega de ruta — reintenta'}), 500
    return jsonify(resultado), 200


@rutas_bp.route('/mis-rutas', methods=['GET'])
@jwt_required()
def mis_rutas():
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    try:
        resultado = RutaService.mis_rutas(uid)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify(resultado), 200


@rutas_bp.route('/usuarios-conductores', methods=['GET'])
@jwt_required()
def usuarios_conductores():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere admin o jefe_almacen'}), 403
    return jsonify({'usuarios': RutaService.usuarios_conductores()}), 200


@rutas_bp.route('/bultos-rechazados', methods=['GET'])
@jwt_required()
def bultos_rechazados():
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Sin permiso — se requiere admin o jefe_almacen'}), 403
    page  = max(1, int(request.args.get('page',  1)))
    limit = min(200, max(1, int(request.args.get('limit', 50))))
    return jsonify(RutaService.bultos_rechazados(page, limit)), 200


# ── Última Milla: paradas y recaudos ────────────────────────────────

@rutas_bp.route('/<int:id>/paradas', methods=['GET'])
@jwt_required()
def listar_paradas(id):
    ruta = RutaDespacho.query.get_or_404(id)
    uid  = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    conductor_ruta = Conductor.query.filter_by(usuario_id=uid, activo=True).first()
    if not _es_admin_o_jefe() and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a esta ruta'}), 403
    try:
        resultado = RutaService.listar_paradas(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify(resultado), 200


@rutas_bp.route('/<int:id>/paradas/<int:tarea_id>/confirmar', methods=['POST'])
@jwt_required()
def confirmar_parada(id, tarea_id):
    from app.models.packing import TareaPacking
    ruta = RutaDespacho.query.get_or_404(id)
    TareaPacking.query.get_or_404(tarea_id)
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    if ruta.estado != 'EN_TRANSITO':
        return jsonify({'error': f'La ruta debe estar EN_TRANSITO, está {ruta.estado}'}), 400
    conductor_ruta = Conductor.query.filter_by(usuario_id=uid, activo=True).first()
    if not _es_admin_o_jefe() and (not conductor_ruta or conductor_ruta.id != ruta.conductor_id):
        return jsonify({'error': 'Sin acceso a esta ruta'}), 403
    try:
        recaudo_id, es_edicion = RutaService.confirmar_parada(id, tarea_id, uid, request.get_json() or {})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    from app.models.recaudo_entrega import RecaudoEntrega
    recaudo = RecaudoEntrega.query.get(recaudo_id)
    return jsonify({
        'ok':         True,
        'recaudo':    recaudo.to_dict(include_foto=True),
        'es_edicion': es_edicion,
    }), 200


@rutas_bp.route('/<int:id>/planilla', methods=['GET'])
@jwt_required()
def planilla_ruta(id):
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede ver la planilla'}), 403
    try:
        resultado = RutaService.planilla_ruta(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify(resultado), 200


@rutas_bp.route('/<int:id>/liquidar', methods=['POST'])
@jwt_required()
def liquidar_ruta(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede liquidar rutas'}), 403
    try:
        resultado = RutaService.liquidar_ruta(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@rutas_bp.route('/<int:id>/forzar-cierre', methods=['POST'])
@jwt_required()
def forzar_cierre_ruta(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede forzar el cierre de rutas'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    try:
        resultado = RutaService.forzar_cierre_ruta(id, uid)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200
