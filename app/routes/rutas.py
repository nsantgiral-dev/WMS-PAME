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
from app.models.recaudo_entrega import EstadoEntrega
from app.services.ruta_service import RutaService, ConflictError, AdvertenciasDeFlota
from app.utils.fecha import dia_operativo as _dia_operativo, dia_operativo_de as _dia_operativo_de

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


def _respuesta_advertencias(e):
    """409 con lo que la flota sabe del vehículo. No es un error: la pantalla
    muestra la lista y vuelve a mandar con `motivo_advertencias`."""
    return jsonify({
        'error': str(e),
        'advertencias_flota': e.advertencias,
        'requiere_motivo': True,
    }), 409


# ── Conductores ──────────────────────────────────────────────────

@rutas_bp.route('/conductores', methods=['GET'])
@jwt_required()
def listar_conductores():
    u = _usuario()
    if not u or u.rol not in Roles.LECTURA_FLOTA:
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


@rutas_bp.route('/conductores/<int:id>/cuenta', methods=['POST'])
@jwt_required()
def crear_cuenta_conductor(id):
    """Le da cuenta PWA a un conductor existente, sin duplicar su fila.

    Sin esto, un conductor ya registrado no puede entrar nunca a la app: el
    alta de usuario crea otro Conductor y la cédula única lo rechaza.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede crear cuentas'}), 403
    datos = request.get_json(silent=True) or {}
    try:
        conductor, usuario = RutaService.crear_cuenta_para_conductor(
            id, datos.get('email'), datos.get('password'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ConflictError as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'conductor': conductor.to_dict(), 'usuario_id': usuario.id}), 201


@rutas_bp.route('/conductores/<int:id>', methods=['PUT'])
@jwt_required()
def actualizar_conductor(id):
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo admin puede modificar conductores'}), 403
    try:
        c = RutaService.actualizar_conductor(id, request.get_json(), usuario_id=admin.id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'conductor': c.to_dict()}), 200


@rutas_bp.route('/conductores/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_conductor(id):
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo admin puede desactivar conductores'}), 403
    try:
        RutaService.desactivar_conductor(
            id, usuario_id=admin.id,
            motivo=(request.get_json(silent=True) or {}).get('motivo'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'ok': True}), 200


# ── Vehículos ─────────────────────────────────────────────────────

@rutas_bp.route('/vehiculos', methods=['GET'])
@jwt_required()
def listar_vehiculos():
    u = _usuario()
    if not u or u.rol not in Roles.LECTURA_FLOTA:
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
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo admin puede modificar vehículos'}), 403
    try:
        v = RutaService.actualizar_vehiculo(id, request.get_json(), usuario_id=admin.id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'vehiculo': v.to_dict()}), 200


@rutas_bp.route('/vehiculos/<int:id>', methods=['DELETE'])
@jwt_required()
def desactivar_vehiculo(id):
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo admin puede desactivar vehículos'}), 403
    try:
        RutaService.desactivar_vehiculo(
            id, usuario_id=admin.id,
            motivo=(request.get_json(silent=True) or {}).get('motivo'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify({'ok': True}), 200


# ── Municipios ───────────────────────────────────────────────────

@rutas_bp.route('/municipios', methods=['GET'])
@jwt_required()
def listar_municipios():
    from app.extensions import db
    from app.models.packing import TareaPacking
    from app.models.ruta_maestra import RutaMaestraParada
    from app.utils.dane_municipios import DANE
    from sqlalchemy import distinct
    de_pedidos = {r[0] for r in db.session.query(distinct(TareaPacking.municipio))
                  .filter(TareaPacking.municipio.isnot(None),
                          TareaPacking.municipio != '').all() if r[0]}
    de_rutas   = {r[0] for r in db.session.query(distinct(RutaMaestraParada.municipio))
                  .filter(RutaMaestraParada.municipio.isnot(None),
                          RutaMaestraParada.municipio != '').all() if r[0]}
    return jsonify({'municipios': sorted(de_pedidos | de_rutas | set(DANE.values()))}), 200


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
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo admin puede modificar rutas maestras'}), 403
    try:
        m = RutaService.actualizar_maestra(id, request.get_json(), usuario_id=admin.id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'maestra': m.to_dict()}), 200


@rutas_bp.route('/maestras/<int:id>', methods=['DELETE'])
@jwt_required()
def eliminar_maestra(id):
    admin = _solo_admin()
    if not admin:
        return jsonify({'error': 'Solo admin puede eliminar rutas maestras'}), 403
    try:
        RutaService.eliminar_maestra(
            id, usuario_id=admin.id,
            motivo=(request.get_json(silent=True) or {}).get('motivo'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ConflictError as e:
        return jsonify({'error': str(e)}), 409
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

    # Soporta rango (fecha_desde/fecha_hasta) o fecha única (backwards compatible)
    fecha_desde = request.args.get('fecha_desde') or request.args.get('fecha')
    fecha_hasta = request.args.get('fecha_hasta')

    paginado = RutaService.listar_rutas(
        conductor_id=conductor_id,
        vehiculo_id=request.args.get('vehiculo_id', type=int),
        estado=request.args.get('estado'),
        fecha_desde=fecha_desde,
        fecha_hasta=fecha_hasta,
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
    data = request.get_json(silent=True) or {}
    try:
        resultado = RutaService.iniciar_ruta(
            id, motivo_advertencias=data.get('motivo_advertencias'), usuario_id=_uid())
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except AdvertenciasDeFlota as e:
        return _respuesta_advertencias(e)
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
    data = request.get_json(silent=True) or {}
    try:
        ruta = RutaService.cerrar_ruta(
            id, motivo_advertencias=data.get('motivo_advertencias'), usuario_id=_uid())
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except AdvertenciasDeFlota as e:
        return _respuesta_advertencias(e)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, 'ruta': ruta.to_dict(include_bultos=True),
                    # Informa, no bloquea: facturas sin condición conocida, de
                    # contado documental o ya saldadas (`_informe_de_cobro`).
                    'informe_cobro': getattr(ruta, 'informe_cobro', [])}), 200


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
    u = _usuario()
    if u and u.rol not in Roles.GESTION + (Roles.CONDUCTOR,):
        return jsonify({'error': 'Sin permiso'}), 403
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
        resultado = RutaService.liquidar_ruta(
            id, usuario_id=_uid(),
            motivo_devoluciones=(request.get_json(silent=True) or {}).get('motivo_devoluciones'))
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
        resultado = RutaService.forzar_cierre_ruta(
            id, uid, motivo=(request.get_json(silent=True) or {}).get('motivo'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@rutas_bp.route('/<int:id>/liquidar-siesa', methods=['POST'])
@jwt_required()
def liquidar_ruta_siesa(id):
    """Dispara la liquidación financiera: encola jobs Siesa (142888/142946/142882)."""
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede liquidar rutas en Siesa'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    try:
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.liquidar_ruta_siesa(id, admin_id=uid)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'ok': True, **resultado}), 200


# ── Liquidación per-recaudo (flujo guiado) ─────────────────────────

@rutas_bp.route('/<int:ruta_id>/recaudos/<int:recaudo_id>/preview-siesa', methods=['GET'])
@jwt_required()
def preview_siesa_recaudo(ruta_id, recaudo_id):
    """Preview de acciones Siesa pendientes para un recaudo específico."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede ver preview Siesa'}), 403
    # Validate recaudo belongs to ruta
    from app.models.recaudo_entrega import RecaudoEntrega
    recaudo = RecaudoEntrega.query.get(recaudo_id)
    if not recaudo or recaudo.ruta_id != ruta_id:
        return jsonify({'error': 'Recaudo no pertenece a esta ruta'}), 404
    try:
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.preview_acciones_recaudo(recaudo_id)
    except (LookupError, ValueError) as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@rutas_bp.route('/<int:ruta_id>/recaudos/<int:recaudo_id>/confirmar-retencion', methods=['POST'])
@jwt_required()
def confirmar_retencion_recaudo(ruta_id, recaudo_id):
    """Decisión del admin sobre el motivo de retención declarado por el
    conductor en campo — desbloquea (o bloquea a propósito) el registro de
    cobro. Ver LiquidacionService.confirmar_retencion."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede confirmar retenciones'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    from app.extensions import db
    from app.models.recaudo_entrega import RecaudoEntrega
    recaudo = db.session.get(RecaudoEntrega, recaudo_id)
    if not recaudo or recaudo.ruta_id != ruta_id:
        return jsonify({'error': 'Recaudo no pertenece a esta ruta'}), 404
    data = request.get_json() or {}
    if 'confirmar' not in data:
        return jsonify({'error': "Falta 'confirmar' (true/false) en el cuerpo"}), 400
    try:
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.confirmar_retencion(
            recaudo_id, admin_id=uid, confirmar=bool(data['confirmar']))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'recaudo': resultado}), 200


@rutas_bp.route('/<int:ruta_id>/recaudos/<int:recaudo_id>/corregir-monto', methods=['POST'])
@jwt_required()
def corregir_monto_recaudo(ruta_id, recaudo_id):
    """Corrige monto_cobrado cuando el número que declaró el conductor
    resultó estar mal (no un faltante real — un dato de origen incorrecto),
    para cuando la ruta ya pasó de EN_TRANSITO y `confirmar_parada` ya no
    permite editarlo. Ver LiquidacionService.corregir_monto_declarado."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede corregir el monto declarado'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    from app.models.recaudo_entrega import RecaudoEntrega
    recaudo = RecaudoEntrega.query.get(recaudo_id)
    if not recaudo or recaudo.ruta_id != ruta_id:
        return jsonify({'error': 'Recaudo no pertenece a esta ruta'}), 404
    data = request.get_json() or {}
    try:
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.corregir_monto_declarado(
            recaudo_id,
            nuevo_monto=data.get('monto'),
            razon=data.get('razon', ''),
            admin_id=uid,
        )
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'recaudo': resultado}), 200


@rutas_bp.route('/<int:ruta_id>/recaudos/<int:recaudo_id>/registrar-cobro', methods=['POST'])
@jwt_required()
def registrar_cobro_recaudo(ruta_id, recaudo_id):
    """Registra cobro (RC) + retenciones (DC) para un recaudo específico."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede registrar cobro'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    from app.models.recaudo_entrega import RecaudoEntrega
    recaudo = RecaudoEntrega.query.get(recaudo_id)
    if not recaudo or recaudo.ruta_id != ruta_id:
        return jsonify({'error': 'Recaudo no pertenece a esta ruta'}), 404
    data = request.get_json() or {}
    try:
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.registrar_cobro_recaudo(
            recaudo_id,
            admin_id=uid,
            retenciones=data.get('retenciones', []),
            monto_override=data.get('monto_override'),
            ajuste_valor=data.get('ajuste_valor', 0) or 0,
            ajuste_es_sobrante=bool(data.get('ajuste_es_sobrante', False)),
            ajuste_razon=data.get('ajuste_razon', '') or '',
        )
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


@rutas_bp.route('/<int:ruta_id>/recaudos/<int:recaudo_id>/autorizar-credito', methods=['POST'])
@jwt_required()
def autorizar_credito_recaudo(ruta_id, recaudo_id):
    """La oficina autoriza como CRÉDITO una parada de contado contraentrega que
    no trajo plata (`credito_no_autorizado`). Razón obligatoria, a la bitácora.

    `_solo_admin`, igual que `/liquidar` y `/liquidar-siesa`: otorgar crédito
    que nadie evaluó no puede pedir menos que liquidar.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede autorizar un crédito'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401
    from app.models.recaudo_entrega import RecaudoEntrega
    recaudo = RecaudoEntrega.query.get(recaudo_id)
    if not recaudo or recaudo.ruta_id != ruta_id:
        return jsonify({'error': 'Recaudo no pertenece a esta ruta'}), 404
    data = request.get_json(silent=True) or {}
    try:
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.autorizar_credito(
            recaudo_id, admin_id=uid, razon=data.get('razon'))
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'recaudo': resultado}), 200


# ── Liquidación — dashboard, detalle, one-click ───────────────────

def _distribucion(valores: list, total_recaudos: int) -> dict:
    """Percentiles del valor por parada, con su cobertura pegada.

    Sin cobertura los percentiles son un número suelto: describen las paradas
    que tienen valor anotado, no la operación. Y el sesgo no es aleatorio —
    tiene valor la parada cuya FE alguien resolvió alguna vez.
    """
    n = len(valores)
    if not n:
        return {
            'con_valor': 0, 'de_un_total_de': total_recaudos, 'cobertura_pct': None,
            'percentiles': None,
            'nota': ('Ninguna parada tiene valor anotado todavía. Se llena sola '
                     'cuando alguien abre la liquidación de una ruta.'),
        }
    ordenados = sorted(valores)

    def _p(q):
        # Índice por rango, sin interpolar: con pocas muestras interpolar
        # inventa un valor que no corresponde a ninguna parada real.
        i = min(n - 1, max(0, int(round(q * (n - 1)))))
        return round(ordenados[i], 2)

    return {
        'con_valor': n,
        'de_un_total_de': total_recaudos,
        'cobertura_pct': round(100.0 * n / total_recaudos, 1) if total_recaudos else None,
        'percentiles': {'p50': _p(.50), 'p75': _p(.75), 'p90': _p(.90),
                        'p95': _p(.95), 'p99': _p(.99),
                        'max': round(ordenados[-1], 2)},
        'nota': ('Exposición por parada (neto de la FE), NO lo cobrado. Un tope '
                 'calculado sobre monto_cobrado quedaría bajo: en un rechazo '
                 'ese vale cero y el riesgo fue el total.'),
    }


@rutas_bp.route('/motivos-rechazo', methods=['GET'])
@jwt_required()
def motivos_rechazo():
    """El catálogo de motivos para el desplegable del conductor.

    Sale del backend y no de una lista en el JS: dos catálogos del mismo
    dominio divergen. Ya pasó con la lectura de la condición de pago (dos
    sitios, los dos hacia contado) y con los tipos de vehículo (el dominio
    conocía «camioneta» y el formulario no la ofrecía).

    Lo puede leer cualquiera que pueda registrar una entrega — el conductor
    incluido, que es quien lo necesita.
    """
    from app.services.motivos_rechazo import para_frontend
    return jsonify({'motivos': para_frontend()}), 200


@rutas_bp.route('/geo/cobertura', methods=['GET'])
@jwt_required()
def geo_cobertura():
    """Cuántos clientes ya tienen coordenada y cuántos no. **La medida.**

    Ese número subiendo mes a mes es el activo entero de la captura del
    conductor: sin él, en tres meses nadie sabe si la funcionalidad sirvió, y
    lo primero que se corta es lo que no se puede mostrar.

    **Vive en rutas y no en el health de flota, y la elección tiene motivo.**
    Lo que se mide acá es una propiedad del **maestro de clientes** —dónde
    queda la tienda—, no del vehículo: no cambia si se vende un camión, no
    entra en el CPK y no dispara ninguna tarea de mantenimiento. Flota
    contesta «¿el camión está en condiciones de salir?»; esto contesta
    «¿sabemos a dónde mandarlo?». Meterlo en `/flota/health` habría puesto el
    número donde nadie que planea rutas lo mira, que es la forma más segura de
    que deje de mirarse.

    Lo lee admin o jefe: es una medida de gestión, no un dato de la calle.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe puede ver la cobertura'}), 403
    from app.services import geo_cliente as _geo
    return jsonify(_geo.cobertura()), 200


@rutas_bp.route('/<int:ruta_id>/reconciliacion', methods=['GET'])
@jwt_required()
def reconciliacion_ruta(ruta_id):
    """Las cuatro columnas que tienen que ser iguales, y dónde se rompe.

        entregas que debían cobrarse = cobros = RC en Siesa = recaudo verificado

    Las tres primeras salen de la base y **se verifican entre sí, no contra la
    plata**: un conductor que cobra $100, registra $100 y entrega $90 produce
    tres números perfectos. La cuarta la cuenta una persona al cerrar, hoy no
    tiene captura, y el reporte lo declara en vez de rellenarla.

    La política vive en `services/reconciliacion_ruta.py` y no acá: cuando
    alguien la ponga en un correo o en un tablero, tiene que leer lo mismo. Si
    divergieran, el que nadie mira es el correo.
    """
    # Muestra montos cobrados, fugas y qué paradas quedaron sin recibo: es
    # información financiera de la ruta, no operativa. El conductor no la ve.
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén'}), 403
    from app.services.reconciliacion_ruta import reconciliar
    try:
        return jsonify(reconciliar(ruta_id)), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 404


@rutas_bp.route('/liquidacion/desglose', methods=['GET'])
@jwt_required()
def liquidacion_desglose():
    """Los tres números que faltaban para decidir sobre el flujo de facturación.

    Se estuvieron estimando toda la semana y ninguno se podía sacar de las
    pantallas existentes — el dashboard agrega por ruta, no desglosa.

    **1. `forma_pago` × `estado_entrega`.** Cuánto del flujo es contado (lo
    único que se movería si la factura pasa a emitirse en la liquidación) y con
    qué frecuencia hay PARCIAL o RECHAZADO, que es cuando haría falta la
    devolución de remisión — y también cuántas veces se ejerce el control «no
    paga completo, no se entrega», que devuelve mercancía física al camión.

    **2. Rezago de liquidación.** Días entre que la ruta queda ENTREGADA y
    LIQUIDADA. Hoy no hay ninguna alerta cuando eso no pasa: se buscó en los
    schedulers y en el servicio de alertas y no existe.

    ⚠️ **Este número es un piso, no una estimación.** Hoy liquidar no tiene
    consecuencia fiscal; si la factura pasa a emitirse ahí, la tendría. Medir
    la latencia de un proceso sin consecuencias y proyectarla a uno con
    consecuencias es un error de método — puede ir para los dos lados. Sirve
    para decir «no van a tardar menos que esto», no para planear.

    **3. Alertas de condición de pago ausente.** Cuántas veces se emitió una
    factura como CONTADO porque el pedido no traía `f430_id_cond_pago`.

    Esa pregunta se estuvo discutiendo con conteos de facturas de otro sistema,
    que **no pueden detectarlo**: el fallback rellena el campo antes de emitir,
    así que toda factura sale con condición. Contar facturas mira el único
    lugar donde la evidencia está garantizada limpia. Lo que sí lo detecta es
    la alerta que el gateway encola, y que vive en esta base.
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede ver el desglose'}), 403

    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import SiesaJob
    from app.services.connekta_gateway import connekta
    from app.services.motivos_rechazo import SIN_RETORNO as _MR_SIN_RETORNO

    # ── 1. Desglose ──────────────────────────────────────────────────────
    matriz, por_pago, por_estado, por_modo, por_motivo = {}, {}, {}, {}, {}
    cruce_modo, credito, valores = {}, [], []
    #: Qué condición DECLARA el pedido de cada parada. Responde la verificación
    #: que se venía planteando como «abrir un pedido en Siesa, dos minutos»,
    #: pero sobre todos los pedidos a la vez en vez de sobre uno.
    por_condicion = {}
    #: Contado contraentrega vs crédito real (2026-09-24): cuántas paradas por
    #: condición y días, con cómo las clasifica la política. Y las de contado
    #: que salieron sin plata y sin autorización, con identificadores.
    from app.services import cond_pago as _cp_des
    por_dias, sin_autorizar = {}, []
    #: Tope declarado, no silencioso. Si se recorta, la respuesta lo dice —
    #: una lista truncada sin avisar se lee como «esas son todas».
    _TOPE_CREDITO = 500
    total = 0
    for r in RecaudoEntrega.query.all():
        # Sin cobro no hay forma de pago (`forma_pago_de`): una parada «no
        # pagó y se quedó» vieja con CREDITO guardado no es una parada CRÉDITO.
        from app.models.recaudo_entrega import forma_pago_de as _fp_de
        fp = (_fp_de(r.estado_entrega, r.forma_pago) or '(sin forma de pago)').upper()
        ee = (r.estado_entrega or '(sin estado)').upper()
        matriz[f'{fp} | {ee}'] = matriz.get(f'{fp} | {ee}', 0) + 1
        por_pago[fp] = por_pago.get(fp, 0) + 1
        por_estado[ee] = por_estado.get(ee, 0) + 1
        # `(sin registrar)` y no `LIBRE`: las paradas confirmadas antes del
        # 2026-08-13 no guardaban el modo. Contarlas como LIBRE inflaría
        # justo el número de riesgo; contarlas como DINAMICO lo escondería.
        _tv = r.tarea
        if _tv is not None and _tv.valor_factura is not None:
            valores.append(float(_tv.valor_factura))
        # `(sin consultar)` no es `(sin condición)`: el primero es que nadie
        # abrió esa ruta en línea; el segundo es que Siesa respondió sin el
        # dato. Colapsarlos es el defecto que ya costó una vez.
        _cobro_d = _cp_des.cobro_de_recaudo(r, _tv)
        _dias_d = (f'{_cobro_d["dias"]} día{"" if _cobro_d["dias"] == 1 else "s"}'
                   if _cobro_d['dias'] is not None else 'días desconocidos')
        _clase_d = ('crédito real' if not _cobro_d['cobrar'] else
                    'contado' if _cobro_d['origen'] == _cp_des.MAESTRO else 'contado supuesto')
        # Con la MISMA política de cobro que `por_dias_credito`: antes salía de
        # `clasificar`, que rotula «credito» todo lo que no es el código de
        # contado — C02 (1 día) y C03 (8) incluidos, que se cobran en la puerta.
        if _tv is None or (_tv.cond_pago is None and _tv.cond_pago_fe is None):
            _clase = '(sin consultar)'
        else:
            _clase = f'{_clase_d} ({_cobro_d["codigo"] or "vacío"})'
        por_condicion[_clase] = por_condicion.get(_clase, 0) + 1
        _k_d = f'{_cobro_d["codigo"] or "(sin condición)"} ({_dias_d}) · {_clase_d}'
        por_dias[_k_d] = por_dias.get(_k_d, 0) + 1
        _no_aut = _cp_des.credito_no_autorizado(r, _tv)
        if _no_aut and len(sin_autorizar) < _TOPE_CREDITO:
            sin_autorizar.append({
                'recaudo_id': r.id, 'ruta_id': r.ruta_id,
                'cliente': (_tv.cliente if _tv is not None else None),
                'pedido': (_tv.numero_pedido_siesa if _tv is not None else None),
                'forma_pago': r.forma_pago, 'estado_entrega': ee,
                'monto_cobrado': float(r.monto_cobrado or 0),
                'valor': (float(_tv.valor_factura)
                          if _tv is not None and _tv.valor_factura is not None else None),
                'cond_pago': _cobro_d['codigo'], 'dias_credito': _cobro_d['dias'],
                'clasif_origen': _cobro_d['origen'],
            })
        mp = r.modo_pantalla or '(sin registrar)'
        por_modo[mp] = por_modo.get(mp, 0) + 1
        # El cruce que la matriz de arriba no da: modo × forma de pago. La
        # pregunta de cartera es «paradas donde el conductor pudo elegir y
        # eligió CRÉDITO», y eso no sale de dos conteos separados.
        cruce_modo[f'{mp} | {fp}'] = cruce_modo.get(f'{mp} | {fp}', 0) + 1
        # Las de CRÉDITO, con identificadores. Un conteo no se puede cruzar
        # contra la cartera; una lista sí. **Es el proxy que SÍ funciona hacia
        # atrás**: `modo_pantalla` se empezó a registrar el 2026-08-13, pero
        # una parada marcada CRÉDITO nunca dispara recibo de caja, así que su
        # factura queda abierta — se haya elegido en modo LIBRE o no.
        if fp == 'CREDITO' and len(credito) < _TOPE_CREDITO:
            _t = r.tarea
            _ru = r.ruta
            credito.append({
                'recaudo_id': r.id,
                'ruta_id': r.ruta_id,
                # El día que alguien LEE, en Bogotá (Regla 5): la columna es UTC.
                'fecha_entregada': (_dia_operativo_de(_ru.fecha_entregada).isoformat()
                                    if _ru is not None and _ru.fecha_entregada else None),
                'cliente': (_t.cliente if _t is not None else None),
                'pedido': (f'{_t.tipo_docto_pedido_siesa}-{_t.consec_docto_pedido_siesa}'
                           if _t is not None else None),
                'remision': (f'{_t.rm_tipo}-{_t.rm_consec}'
                             if _t is not None and _t.rm_consec else None),
                # La FACTURA, que es por donde cartera indexa. Se lee de lo
                # PERSISTIDO — este endpoint no llama a Siesa: resolverla acá
                # serían cientos de consultas al ERP en un solo request.
                # `null` = todavía no se resolvió para esa tarea, no que no
                # exista. Se llena sola cuando alguien la resuelve.
                'factura': (f'{_t.fe_tipo}-{_t.fe_consec}'
                            if _t is not None and _t.fe_tipo else None),
                # La EXPOSICIÓN de la parada, no lo cobrado. `monto_cobrado`
                # vale cero en un rechazo y el riesgo fue el total.
                'valor': (float(_t.valor_factura)
                          if _t is not None and _t.valor_factura is not None else None),
                'estado_entrega': ee,
                'modo_pantalla': r.modo_pantalla,
                # Crédito sobre una factura de contado que nadie autorizó.
                'credito_no_autorizado': _no_aut,
                'credito_autorizado': _cp_des.credito_autorizado(r),
            })
        if ee == 'RECHAZADO':
            mr = r.motivo_rechazo or '(sin registrar)'
            por_motivo[mr] = por_motivo.get(mr, 0) + 1
        total += 1

    _no_entregado = sum(v for k, v in por_estado.items() if k in ('PARCIAL', 'RECHAZADO'))

    # ── 2. Rezago ────────────────────────────────────────────────────────
    # La política vive en `services/rezago_liquidacion.py` porque el cron de
    # las 06:30 lee exactamente lo mismo. Duplicarla haría que el correo
    # hablara de un universo y esta pantalla de otro — y el que nadie mira es
    # el correo.
    from app.services import rezago_liquidacion as _rz
    _diag = _rz.diagnostico()
    sin_liquidar, dias = _diag['rutas'], _diag['dias']

    # ── 3. Alertas de condición ausente ──────────────────────────────────
    # `count()` y no traer las filas: si algún día son miles, este endpoint no
    # puede volverse el problema que vino a medir.
    #
    # Se filtra por `tipo_alerta`, NO por una frase del cuerpo. Esto decía
    # `like('%data incompleta%')` y al reescribirse el texto de la alerta el
    # contador se quedó en cero para siempre — sin fallar, que es lo peligroso.
    # El tipo es la identidad del aviso; la prosa es presentación.
    def _jobs_alerta(tipo):
        return (SiesaJob.query
                .filter(SiesaJob.tipo == 'ALERTA_EMAIL')
                .filter(SiesaJob.payload.like(f'%"tipo_alerta": "{tipo}"%'))
                .order_by(SiesaJob.id.desc()))

    def _cuenta_alertas(tipo):
        return _jobs_alerta(tipo).count()

    alertas_cond = _cuenta_alertas('DATA_MAESTRA_COND_PAGO')
    # Distinto y más grave: el pedido declaraba contado y la FE va a quedar en
    # Elaboración. Verificado en producción el 2026-08-13.
    alertas_fe_contado = _cuenta_alertas('FE_CONTADO_NO_APROBABLE')

    def _documentos_a_revisar(tipo, limite=200):
        """De contador a lista de trabajo.

        Un número dice «pasó N veces»; esto dice **cuáles**, que es lo que
        alguien puede ir a mirar en Siesa. Es la misma diferencia que hubo
        entre saber que había 159 jobs en la DLQ y poder triarlos.

        `cond_pago_emitida` separa dos poblaciones que el contador mezclaba:

          · presente → la FE salió con la condición de ruta. Sano.
          · ausente  → se emitió bajo el fallback viejo, que era el código de
            CONTADO. **Esa factura pudo quedar en Elaboración con la remisión
            ya hecha**, y es lo que hay que ir a revisar.

        La distinción es estructural y no por fecha a propósito: una fecha de
        corte escrita acá se desincroniza del despliegue real.
        """
        import json as _json
        import re as _re
        salida = []
        for job in _jobs_alerta(tipo).limit(limite).all():
            try:
                p = _json.loads(job.payload)
            except Exception:
                continue
            rm = p.get('rm_consec')
            if rm is None:
                # Alerta vieja: el número de la remisión solo vive dentro del
                # texto del correo. Se saca de ahí y **se marca**, en vez de
                # devolverlo como si fuera un campo.
                m = _re.search(r'RM-(\d+)', p.get('cuerpo_texto') or '')
                rm = m.group(1) if m else None
            emitida = p.get('cond_pago_emitida')
            salida.append({
                'rm_tipo': p.get('rm_tipo'),
                'rm_consec': rm,
                'tercero': p.get('tercero'),
                'cond_pago_emitida': emitida,
                'fecha': job.fecha_creacion.isoformat() if job.fecha_creacion else None,
                'campos_propios': 'rm_consec' in p,
                'revisar_en_siesa': emitida is None,
            })
        return salida

    _docs_cond = _documentos_a_revisar('DATA_MAESTRA_COND_PAGO')
    _docs_contado = _documentos_a_revisar('FE_CONTADO_NO_APROBABLE')

    # EL DENOMINADOR, sin el cual el conteo de arriba no dice nada.
    #
    # «0 alertas» significa cosas opuestas según cuántas facturas haya emitido
    # el gateway: con 5, es «no hemos llegado a probarlo»; con 5.000, es «el
    # fallback es código muerto». Y acá el denominador va a ser chico —la
    # cadena de despacho es nueva— así que un cero NO cierra la pregunta.
    #
    # Ojo con la confusión que ya costó una vuelta: las ~382.000 facturas del
    # extracto de Siesa **no pasaron por este gateway**. Sirven para saber si
    # los PEDIDOS traen condición; no dicen nada del fallback.
    from app.models.packing import TareaPacking
    _emitidas = TareaPacking.query.filter(TareaPacking.rm_consec.isnot(None)).count()

    return jsonify({
        'recaudos': {
            'total': total,
            'matriz': matriz,
            'por_forma_pago': por_pago,
            'por_estado_entrega': por_estado,
            'por_modo_pantalla': por_modo,
            'en_modo_libre': por_modo.get('LIBRE', 0),
            'motivos_rechazo': por_motivo,
            # Qué DECLARA el pedido, no qué hizo el conductor. Si sale todo
            # `contado (C01)`, el bloqueo por mora nunca frena rutas y el
            # método «No retener pedidos de contado» ya cubre el caso.
            'condicion_declarada': por_condicion,
            'modo_x_forma_pago': cruce_modo,
            # El caso donde el estado dice RECHAZADO pero el inventario NO
            # volvió al camión. Es faltante de inventario disfrazado de
            # devolución, y solo aparece en un conteo físico.
            'rechazos_sin_retorno': sum(
                n for m, n in por_motivo.items() if m in _MR_SIN_RETORNO),
            'parcial_o_rechazado': _no_entregado,
            'pct_parcial_o_rechazado': (
                round(100.0 * _no_entregado / total, 1) if total else None),
        },
        # ── Distribución de valores por parada ──────────────────────────
        #
        # El insumo del tope por debajo del cual una parada declarada de
        # contado pasaría sin evaluación de crédito. Percentiles y no promedio:
        # un tope se elige mirando la cola, no el centro.
        #
        # **La cobertura va primero y no es decorativa.** Si solo una fracción
        # de las paradas tiene valor anotado, los percentiles describen esa
        # fracción y no la operación. Un umbral puesto sobre una muestra
        # sesgada deja pasar justo lo que venía a frenar — y el sesgo acá no es
        # aleatorio: tienen valor las paradas cuya FE alguien resolvió.
        'distribucion_valor_parada': _distribucion(valores, total),
        # ── Contado contraentrega vs crédito real ──────────────────────
        'por_dias_credito': {
            'umbral_dias': _cp_des.umbral_dias()[0],
            'conteo': por_dias,
            'nota': ('Clasificación de cada parada según la política de '
                     'cobro: hasta el umbral es contado contraentrega, por '
                     'encima crédito real; «supuesto» = sin condición '
                     'conocida, se cobra.'),
        },
        'credito_no_autorizado': {
            'total': len(sin_autorizar),
            'truncado': len(sin_autorizar) >= _TOPE_CREDITO,
            'valor_sin_cobrar': round(sum(
                max(0.0, x['valor'] - x['monto_cobrado'])
                for x in sin_autorizar if x['valor'] is not None), 2),
            'sin_valor': sum(1 for x in sin_autorizar if x['valor'] is None),
            'detalle': sin_autorizar,
            'nota': ('Paradas de contado contraentrega registradas CREDITO/EXENTO '
                     'o con $0 y sin autorización. Bloquean la liquidación de su '
                     'ruta hasta que se cobren o se autoricen con razón.'),
        },
        'paradas_credito': {
            'total': por_pago.get('CREDITO', 0),
            'listadas': len(credito),
            'truncado': por_pago.get('CREDITO', 0) > len(credito),
            'detalle': credito,
            # Cuántas se pueden cruzar documento contra documento y cuántas
            # solo por cliente+fecha. Sin esta cifra, una lista con la mitad de
            # las facturas en `null` se lee como si el cruce fuera completo.
            'con_factura': sum(1 for x in credito if x['factura']),
            'sin_factura': sum(1 for x in credito if not x['factura']),
            'nota': ('Una parada marcada CRÉDITO nunca dispara recibo de caja, '
                     'así que su factura queda abierta en cartera. Sirve hacia '
                     'atrás; `modo_pantalla` solo desde el 2026-08-13. '
                     '`factura` sale de lo persistido en la tarea: null = aún '
                     'no se resolvió, no que no exista.'),
        },
        'rezago_liquidacion': {
            'rutas_entregadas_sin_liquidar': len(sin_liquidar),
            # Entregadas antes de FECHA_INICIO_AUDITORIA: fuera de la cuenta.
            'antes_del_corte': _diag.get('antes_del_corte', 0),
            'atrasadas': len(_diag['atrasadas']),
            # La entrega ocurrió en un mes y el recaudo va a registrarse en
            # otro. Liquidar rápido ya no lo corrige — el período no se mueve.
            'cruzan_mes': len(_diag['cruzan_mes']),
            'dias_max': max(dias) if dias else None,
            'dias_promedio': round(sum(dias) / len(dias), 1) if dias else None,
            'detalle': sorted(sin_liquidar,
                              key=lambda x: (x['dias'] is None, -(x['dias'] or 0)))[:50],
            'nota': ('Mientras no se liquiden, la factura de cada parada computa '
                     'mora y consume cupo: es el mecanismo de la cartera '
                     'fantasma. El cron de las 06:30 alerta sobre esto mismo.'),
        },
        'condicion_pago_ausente': {
            'alertas': alertas_cond,
            'facturas_emitidas_por_el_gateway': _emitidas,
            'concluyente': _emitidas >= 50,
            'documentos': _docs_cond,
            # LA CIFRA QUE IMPORTA de esta sección, y no es el total.
            #
            # Hasta el 2026-08-13 el hueco de `f430_id_cond_pago` se tapaba con
            # el código de CONTADO. Cada una de estas remisiones pudo dejar una
            # factura en Elaboración **con el inventario ya descargado** — que
            # es peor que no facturar, porque la mercancía ya salió.
            'a_revisar_en_siesa': sum(1 for d in _docs_cond if d['revisar_en_siesa']),
            'nota': ('Veces que el pedido no traía condición de pago. Contar '
                     'facturas NO lo detecta: el fallback rellena el campo antes '
                     'de emitir. «A revisar en Siesa» son las emitidas con el '
                     'fallback viejo (contado): revisar si la factura quedó en '
                     'Elaboración. Las nuevas salen con la condición de ruta.'),
        },
        'fe_contado_no_aprobable': {
            'alertas': alertas_fe_contado,
            'documentos': _docs_contado,
            'nota': ('Pedidos de ruta que declararon CONTADO. Su factura queda en '
                     'Elaboración —Siesa no la aprueba sin recaudo— con el '
                     'inventario ya descargado. Cualquier valor > 0 es un '
                     'documento trabado que alguien tiene que corregir en Siesa.'),
        },
    }), 200


@rutas_bp.route('/liquidacion/dashboard', methods=['GET'])
@jwt_required()
def liquidacion_dashboard():
    """Dashboard de liquidación: rutas del día agrupadas por estado financiero."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede ver el dashboard de liquidación'}), 403

    from datetime import date as _date
    from sqlalchemy import and_, func, or_
    from sqlalchemy.orm import selectinload, joinedload
    from app.models.bulto import Bulto
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import SiesaJob
    from app.services.connekta_gateway import connekta
    from app.services.motivos_rechazo import SIN_RETORNO as _MR_SIN_RETORNO
    from collections import Counter

    # Soporta rango de fechas (fecha_desde/fecha_hasta) o fecha única (backwards compatible)
    fecha_desde_str = request.args.get('fecha_desde') or request.args.get('fecha')
    fecha_hasta_str = request.args.get('fecha_hasta')
    try:
        fecha_desde = _date.fromisoformat(fecha_desde_str) if fecha_desde_str else _dia_operativo()
        fecha_hasta = _date.fromisoformat(fecha_hasta_str) if fecha_hasta_str else fecha_desde
    except ValueError:
        return jsonify({'error': 'Formato de fecha inválido — usar YYYY-MM-DD'}), 400

    if fecha_hasta < fecha_desde:
        fecha_desde, fecha_hasta = fecha_hasta, fecha_desde

    # Rutas entregadas o con procesamiento financiero en el rango
    # Eager load recaudos + bultos→tarea para evitar N+1 (~125 queries → 3)
    rutas = (RutaDespacho.query
             .options(
                 selectinload(RutaDespacho.recaudos),
                 selectinload(RutaDespacho.bultos).selectinload(Bulto.tarea),
                 joinedload(RutaDespacho.conductor),
                 joinedload(RutaDespacho.vehiculo),
                 joinedload(RutaDespacho.ruta_maestra),
             )
             # `fecha_programada IS NULL` cuenta como "siempre dentro del
             # rango", no como "nunca" — que es lo que un `>=`/`<=` normal le
             # hace a NULL en SQL. `crear_ruta()` (ruta ad-hoc del muelle, sin
             # RutaMaestra) la dejaba sin asignar, y esta consulta la
             # descartaba en silencio para CUALQUIER rango de fechas: una
             # ruta ya despachada y con recaudos reales quedaba invisible
             # para liquidar, sin que ningún filtro la recuperara. Ahora
             # `crear_ruta()` la asigna (Regla 0), pero esto se queda como
             # red de seguridad para lo que ya quedó huérfano en producción y
             # para cualquier otro camino de creación que se le olvide.
             .filter(or_(
                 RutaDespacho.fecha_programada.is_(None),
                 and_(
                     RutaDespacho.fecha_programada >= fecha_desde,
                     RutaDespacho.fecha_programada <= fecha_hasta,
                 ),
             ))
             .filter(or_(
                 RutaDespacho.estado == 'ENTREGADA',
                 RutaDespacho.estado_financiero != 'PENDIENTE',
             ))
             .all())

    total_efectivo = 0
    total_transferencia = 0
    total_credito = 0
    total_recaudado = 0
    pendientes = 0
    liquidadas = 0
    rutas_out = []

    from app.services import senales_ruta as _sr
    _faltantes = _sr.faltantes_de_retorno_de_recaudos(
        [r.id for ruta in rutas for r in ruta.recaudos])

    for ruta in rutas:
        recaudos = ruta.recaudos  # preloaded via selectinload
        tareas = ruta.tareas_unicas()

        # Contadores por factura (TareaPacking), NO por parada física:
        # un cliente con dos facturas en una visita cuenta dos veces.
        total_paradas = len(tareas)
        facturas_gestionadas = len(recaudos)
        paradas_entregadas = sum(1 for r in recaudos if r.estado_entrega == EstadoEntrega.ENTREGADO)
        paradas_parciales = sum(1 for r in recaudos if r.estado_entrega == EstadoEntrega.PARCIAL)
        paradas_rechazadas = sum(1 for r in recaudos if r.estado_entrega == EstadoEntrega.RECHAZADO)
        # Cuenta aparte. Sumarla a rechazadas diría que la mercancía volvió;
        # sumarla a entregadas diría que se cobró. No es ninguna de las dos.
        paradas_sin_pago = sum(1 for r in recaudos
                               if r.estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO)

        # Contadores Siesa
        siesa_nc = sum(1 for r in recaudos if r.siesa_nc_triggered)
        siesa_rc = sum(1 for r in recaudos if r.siesa_rc_triggered)
        siesa_dc = sum(1 for r in recaudos if r.siesa_dc_triggered)

        # Jobs fallidos vinculados a recaudos de esta ruta
        recaudo_ids = [r.id for r in recaudos]
        jobs_fallidos = 0
        if recaudo_ids:
            # Trabados de verdad (`fallidos_vigentes`): un FALLIDO superado
            # por un reintento que entró ya no es un documento pendiente.
            from app.services.siesa_job_service import fallidos_vigentes
            jobs_fallidos = len(fallidos_vigentes(
                tipos=('NOTA_CREDITO_FACTURA', 'RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET'),
                referencia_tipo='RecaudoEntrega', referencia_ids=recaudo_ids)['jobs'])

        # Montos por forma de pago
        ruta_recaudado = 0
        for r in recaudos:
            monto = float(r.monto_cobrado or 0)
            ruta_recaudado += monto
            fp = (r.forma_pago or '').upper()
            if fp == 'EFECTIVO':
                total_efectivo += monto
            # `TRANSFERENCIA` a secas (retrocompatible) + los medios
            # específicos por banco (TRANSFERENCIA_BANCOLOMBIA_AH, etc.,
            # alineados con `MedioPago` de gestor-cartera-pame) + TARJETA —
            # todo lo que no es efectivo ni crédito cae en este bucket. Un
            # `==` fijo contra el string viejo dejaba de contar cualquier
            # medio nuevo sin que nada avisara — el monto seguía sumando a
            # `ruta_recaudado`/`total_recaudado`, solo desaparecía del
            # desglose por medio.
            elif fp.startswith('TRANSFERENCIA') or fp in ('CONSIGNACION', 'TARJETA'):
                total_transferencia += monto
            elif fp == 'CREDITO':
                total_credito += monto

        total_recaudado += ruta_recaudado

        ef = ruta.estado_financiero or 'PENDIENTE'
        if ef == 'LIQUIDADA':
            liquidadas += 1
        else:
            pendientes += 1

        rd = ruta.to_dict()
        rd['total_recaudado'] = ruta_recaudado
        rd['total_paradas'] = total_paradas
        rd['facturas_gestionadas'] = facturas_gestionadas
        # Alias de transición: un conductor con la PWA vieja en caché sigue
        # leyendo la clave anterior. Retirar cuando todos los equipos hayan
        # recargado (post go-live).
        rd['paradas_gestionadas'] = facturas_gestionadas
        rd['paradas_entregadas'] = paradas_entregadas
        rd['paradas_parciales'] = paradas_parciales
        rd['paradas_rechazadas'] = paradas_rechazadas
        rd['paradas_sin_pago'] = paradas_sin_pago
        rd['siesa_nc_enviados'] = siesa_nc
        rd['siesa_rc_enviados'] = siesa_rc
        rd['siesa_dc_enviados'] = siesa_dc
        rd['jobs_fallidos'] = jobs_fallidos
        # Señales de fuga de la ruta (`senales_ruta.senales_de_recaudo`): cuántas
        # paradas tienen algo que mirar, y cuáles claves. Datos para quien
        # liquida, nunca un veredicto sobre el conductor.
        _sen = Counter()
        for r in recaudos:
            for x in _sr.senales_de_recaudo(r, ruta, _faltantes.get(r.id)):
                _sen[x['clave']] += 1
        rd['senales'] = dict(_sen)
        rd['faltante_retorno_unidades'] = round(sum(
            _faltantes[r.id]['faltante_unidades'] for r in recaudos if r.id in _faltantes), 4)
        rutas_out.append(rd)

    # Señales por conductor, en la cabecera de la liquidación. Ninguna es una
    # sanción: el encargado decide si pregunta. Efectivo en poder no depende
    # del rango —es lo que está afuera HOY—; la tasa de «se quedó sin pagar» sí.
    from app.services.analitica_fugas import tasa_sin_pago_por_conductor
    senales_conductor = {
        'efectivo_en_poder': _sr.efectivo_en_poder_por_conductor(),
        'sin_pago_por_conductor': tasa_sin_pago_por_conductor(fecha_desde, fecha_hasta),
    }

    return jsonify({
        'resumen': {
            'total_rutas': len(rutas),
            'pendientes': pendientes,
            'liquidadas': liquidadas,
            'total_recaudado': total_recaudado,
            'total_efectivo': total_efectivo,
            'total_transferencia': total_transferencia,
            'total_credito': total_credito,
        },
        'rutas': rutas_out,
        'senales_conductor': senales_conductor,
    }), 200


@rutas_bp.route('/<int:id>/liquidacion-detalle', methods=['GET'])
@jwt_required()
def liquidacion_detalle(id):
    """Detalle de liquidación de una ruta: recaudos + datos de factura Siesa."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede ver detalle de liquidación'}), 403
    try:
        from app.services.liquidacion_service import LiquidacionService
        resultado = LiquidacionService.preparar_detalle_ruta(id)
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    return jsonify(resultado), 200


@rutas_bp.route('/<int:id>/liquidar-completo', methods=['POST'])
@jwt_required()
def liquidar_completo(id):
    """
    One-click: verifica cantidades, aplica retenciones, cambia estado financiero
    y dispara todos los conectores Siesa (NCE/RC/DC).

    **`_solo_admin`, no `_es_admin_o_jefe`.** Este endpoint ejecuta lo mismo que
    `/liquidar` y `/liquidar-siesa`, que exigen admin — y encima encola las
    retenciones. Pedía menos que cualquiera de las dos operaciones que hace: un
    jefe de almacén recibía 403 en las dos granulares y **200** acá.

    El invariante, que vale más allá de este caso: **un endpoint compuesto no
    puede exigir menos que el más estricto de sus componentes.** Un atajo de
    conveniencia que relaja el permiso convierte la comodidad en escalada.
    Trinquete: `tests/test_permiso_compuesto.py`.
    """
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede liquidar rutas — este endpoint '
                                 'ejecuta las mismas operaciones que /liquidar y '
                                 '/liquidar-siesa'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401

    from app.extensions import db
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import SiesaJob
    from app.services.connekta_gateway import connekta
    from app.services.motivos_rechazo import SIN_RETORNO as _MR_SIN_RETORNO
    from app.services.liquidacion_service import (
        LiquidacionService, RETENCION_PUC, RETENCION_TASA, _obtener_tercero,
        _pucs_en_cola as _pucs_de_recaudo,
    )

    ruta = RutaDespacho.query.get(id)
    if not ruta:
        return jsonify({'error': 'Ruta no encontrada'}), 404
    if ruta.estado != 'ENTREGADA':
        return jsonify({'error': f'La ruta debe estar ENTREGADA para liquidar (estado actual: {ruta.estado})'}), 400

    data = request.get_json() or {}
    recaudos_payload = data.get('recaudos', [])
    errores = []
    retenciones_encoladas = 0

    for rp in recaudos_payload:
        recaudo_id = rp.get('recaudo_id')
        recaudo = RecaudoEntrega.query.get(recaudo_id)
        if not recaudo or recaudo.ruta_id != id:
            errores.append(f'Recaudo {recaudo_id} no encontrado o no pertenece a la ruta')
            continue

        # (a) Actualizar cantidades verificadas por el Líder
        cantidades_verificadas = rp.get('cantidades_verificadas', [])
        if cantidades_verificadas and recaudo.items_entregados:
            # Copia PROFUNDA: mutar los dicts del JSON en su sitio deja el valor
            # viejo igual al nuevo y SQLAlchemy no ve el cambio.
            items = [dict(it) for it in recaudo.items_entregados]
            for cv in cantidades_verificadas:
                codigo = cv.get('codigo', '')
                cant_devuelta = int(cv.get('cantidad_devuelta', 0))
                for it in items:
                    if it.get('codigo') == codigo:
                        # Lo que declaró el conductor no se pisa: queda al lado,
                        # una sola vez, para medir después el faltante de
                        # retorno contra lo que cuente recepción.
                        it.setdefault('cantidad_devuelta_conductor',
                                      it.get('cantidad_devuelta'))
                        it['cantidad_devuelta'] = cant_devuelta
                        pedido = int(it.get('cantidad_pedida', 0))
                        it['cantidad_entregada'] = max(0, pedido - cant_devuelta)
                        break
            recaudo.items_entregados = items
            # La devolución de la parada nació al confirmarla (m045devol): la
            # corrección del líder la actualiza si todavía no se contó. Lo que
            # dijo el conductor sigue siendo lo declarado.
            from app.services import devolucion_ruta as _dr_lc
            _dev_lc = _dr_lc.devolucion_vigente(recaudo.id)
            if _dev_lc is not None and _dev_lc.estado in ('EN_CAMION', 'ABIERTA'):
                _dr_lc.sincronizar_con_parada(recaudo, uid)
            elif _dev_lc is not None:
                errores.append(
                    f'Recaudo {recaudo.id}: la devolución {_dev_lc.codigo} ya se contó en '
                    f'bodega ({_dev_lc.estado}); la cantidad corregida no cambia lo contado.')

        # (b) Retenciones
        retenciones = rp.get('retenciones', [])
        if retenciones:
            tarea = recaudo.tarea
            tipos_ret = []
            monto_total_ret = 0

            # Obtener base gravable desde Siesa si es posible, fallback a monto_cobrado
            base_gravable = float(recaudo.monto_cobrado or 0)
            # El IVA REAL de la factura. Sin esto, el cálculo de abajo lo
            # inventaba multiplicando el subtotal por 0.19 — y en una factura
            # con líneas exentas eso infla la retención.
            total_iva = 0.0
            # La FE, no el pedido — ver `app/services/fe_resolver.py`.
            from app.services.fe_resolver import resolver_fe_o_none
            _tipo_fe, _consec_fe = resolver_fe_o_none(tarea) if tarea else (None, None)
            # ── La base de retención sale de Siesa o no sale ─────────────
            # El fallback era `base_gravable = monto_cobrado` y `total_iva = 0`,
            # y producía **dos daños opuestos, los dos silenciosos**:
            #
            # · reteIVA sobre `total_iva = 0` da 0 → el `continue` de abajo
            #   descarta la línea y **el documento contable nunca se encola**,
            #   con la pantalla diciendo `ok: true`;
            # · retefuente e ICA se calculan sobre `monto_cobrado`, que es lo
            #   que recaudó el conductor —**con IVA** y neto de descuentos—,
            #   no el `f470_vlr_bruto`. El DC sale a Siesa por ~19% de más.
            #
            # La misma política en `liquidacion_service.registrar_cobro_recaudo`
            # levanta `ValueError('Datos de Siesa no disponibles')`. Dos
            # implementaciones, resultados opuestos; ésta era la degradada.
            _base_de_siesa = False
            if _tipo_fe and _consec_fe:
                try:
                    from app.services.connekta_gateway import connekta
                    lineas_raw = connekta.get_rowids_factura(_tipo_fe, _consec_fe)
                    if lineas_raw:
                        base_gravable = sum(float(ln.get('f470_vlr_bruto', 0)) for ln in lineas_raw)
                        total_iva = sum(float(ln.get('f470_vlr_imp', 0)) for ln in lineas_raw)
                        _base_de_siesa = True
                except Exception as e:
                    logger.error(
                        '[LIQUIDAR-COMPLETO] no se pudo obtener la base gravable '
                        'de Siesa para el recaudo %d: %s', recaudo.id, e)
            if not _base_de_siesa:
                # Se declara en `errores`, que es lo que decide el `ok` de la
                # respuesta. El `except` anterior no lo tocaba, así que la
                # pantalla salía en verde.
                errores.append(
                    f'Recaudo {recaudo.id}: no se pudo leer la base gravable de '
                    f'la factura en Siesa. Las retenciones NO se encolaron — '
                    f'calcularlas sobre el monto recaudado daría retefuente e '
                    f'ICA sobre una base con IVA, y reteIVA en cero.')
                continue

            # Obtener tercero para los DCs — sin NIT los jobs DC fallarán en Siesa
            tercero_nit, sucursal = '', '001'
            if tarea:
                try:
                    tercero_nit, sucursal = _obtener_tercero(tarea)
                except Exception as e:
                    logger.warning(
                        '[LIQUIDAR-COMPLETO] No se pudo obtener tercero para '
                        'recaudo %d (tarea %d): %s — DCs se encolarán sin NIT',
                        recaudo.id, tarea.id, e,
                    )
                    errores.append(
                        f'Recaudo {recaudo.id}: no se pudo obtener NIT del tercero ({e}). '
                        'Las retenciones se encolarán pero pueden fallar en Siesa.'
                    )

            # La FE, no el pedido — ver `app/services/fe_resolver.py`. Acá se
            # reusa lo ya resuelto arriba en vez de volver a consultar Siesa.
            tipo_docto_fe = _tipo_fe or ''
            consec_fe = _consec_fe or ''
            notas_base = f'WMS Ruta #{id} | Liquidación completa'

            # Lo que la bandera pretendía evitar, hecho sobre la cola: un job
            # DC vivo para este recaudo y esta cuenta significa que ya se
            # encoló. Mirar la cola es lo que corresponde — la bandera del
            # recaudo habla de lo enviado, no de lo encolado. Compartida con
            # `_encolar_documento_contable` — era la misma consulta escrita
            # dos veces, y la otra copia es la que corre a continuación
            # (Step 4 → `liquidar_ruta_siesa` → `_procesar_recaudo`) sin
            # saber que esta ya encoló el DC de la misma cuenta.
            _pucs_en_cola = _pucs_de_recaudo(recaudo.id)

            for ret in retenciones:
                tipo_ret = ret.get('tipo', '')
                if tipo_ret not in RETENCION_PUC:
                    errores.append(f'Tipo de retención desconocido: {tipo_ret}')
                    continue
                if RETENCION_PUC[tipo_ret] in _pucs_en_cola:
                    continue

                # Una función, tres sitios. Acá estaba la tercera versión, y
                # era la equivocada: `base_gravable * 0.19` inventa el IVA.
                from app.services.liquidacion_service import monto_de_retencion
                monto_ret = monto_de_retencion(tipo_ret, base_gravable, total_iva)

                if monto_ret <= 0:
                    continue

                tipos_ret.append(tipo_ret)
                monto_total_ret += monto_ret

                # Encolar DC directamente
                SiesaJob.encolar(
                    tipo='DOCUMENTO_CONTABLE_RET',
                    payload={
                        'recaudo_id': recaudo.id,
                        'tipo_docto_fe': tipo_docto_fe,
                        'consec_fe': str(consec_fe),
                        'tercero_nit': tercero_nit,
                        'sucursal': sucursal,
                        'cuenta_puc': RETENCION_PUC[tipo_ret],
                        'monto': monto_ret,
                        'base_gravable': base_gravable,
                        'notas': f'{notas_base} | Retención {tipo_ret}',
                    },
                    referencia_tipo='RecaudoEntrega',
                    referencia_id=recaudo.id,
                    creado_por_id=uid,
                )
                retenciones_encoladas += 1

            if tipos_ret:
                recaudo.motivo_descuento = ','.join(tipos_ret)
                recaudo.monto_descuento = monto_total_ret
                # NO se toca `siesa_dc_triggered` acá.
                #
                # Hasta el 2026-08-13 esta línea la encendía «para evitar doble
                # encolado», y esa misma bandera es la GUARDA DE IDEMPOTENCIA
                # del ejecutor (`siesa_job_service.py`). El resultado: cada job
                # que este endpoint encolaba leía la bandera, se declaraba
                # idempotente y se marcaba completado **sin enviar nada**.
                #
                # **Ningún documento de retención llegó nunca a Siesa por esta
                # vía**, y el log, la pantalla y el tablero decían que sí.
                #
                # Una bandera con dos significados —«ya encolé» y «ya envié»—
                # no puede servir para los dos. El anti-doble-encolado vive
                # ahora en `_ya_hay_dc_encolado`, que mira la cola.

    db.session.commit()

    # Step 3: Set estado_financiero = LIQUIDADA
    try:
        resultado_liquidar = RutaService.liquidar_ruta(
            id, usuario_id=uid, motivo_devoluciones=data.get('motivo_devoluciones'))
    except (LookupError, ValueError) as e:
        errores.append(f'Error al liquidar ruta: {e}')
        resultado_liquidar = {}

    # Step 4: Fire Siesa connectors (NCE/RC — DCs already enqueued above)
    try:
        resultado_siesa = LiquidacionService.liquidar_ruta_siesa(id, admin_id=uid)
    except (LookupError, ValueError) as e:
        errores.append(f'Error en liquidación Siesa: {e}')
        resultado_siesa = {}

    return jsonify({
        'ok': len(errores) == 0,
        'liquidacion': resultado_liquidar,
        'siesa': resultado_siesa,
        'retenciones_encoladas': retenciones_encoladas,
        'errores': errores,
    }), 200
