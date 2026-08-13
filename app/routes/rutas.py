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
from app.services.ruta_service import RutaService, ConflictError
from app.utils.fecha import dia_operativo as _dia_operativo

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
def eliminar_maestra(id):
    if not _solo_admin():
        return jsonify({'error': 'Solo admin puede eliminar rutas maestras'}), 403
    try:
        RutaService.eliminar_maestra(id)
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
        )
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify(resultado), 200


# ── Liquidación — dashboard, detalle, one-click ───────────────────

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
    from app.services.motivos_rechazo import SIN_RETORNO as _MR_SIN_RETORNO

    # ── 1. Desglose ──────────────────────────────────────────────────────
    matriz, por_pago, por_estado, por_modo, por_motivo = {}, {}, {}, {}, {}
    cruce_modo, credito = {}, []
    #: Tope declarado, no silencioso. Si se recorta, la respuesta lo dice —
    #: una lista truncada sin avisar se lee como «esas son todas».
    _TOPE_CREDITO = 500
    total = 0
    for r in RecaudoEntrega.query.all():
        fp = (r.forma_pago or '(sin forma de pago)').upper()
        ee = (r.estado_entrega or '(sin estado)').upper()
        matriz[f'{fp} | {ee}'] = matriz.get(f'{fp} | {ee}', 0) + 1
        por_pago[fp] = por_pago.get(fp, 0) + 1
        por_estado[ee] = por_estado.get(ee, 0) + 1
        # `(sin registrar)` y no `LIBRE`: las paradas confirmadas antes del
        # 2026-08-13 no guardaban el modo. Contarlas como LIBRE inflaría
        # justo el número de riesgo; contarlas como DINAMICO lo escondería.
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
                'fecha_entregada': (_ru.fecha_entregada.date().isoformat()
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
                'estado_entrega': ee,
                'modo_pantalla': r.modo_pantalla,
            })
        if ee == 'RECHAZADO':
            mr = r.motivo_rechazo or '(sin registrar)'
            por_motivo[mr] = por_motivo.get(mr, 0) + 1
        total += 1

    _no_entregado = sum(v for k, v in por_estado.items() if k in ('PARCIAL', 'RECHAZADO'))

    # ── 2. Rezago ────────────────────────────────────────────────────────
    from app.models.ruta_despacho import EstadoFinancieroRuta as _EFR
    from app.utils.fecha import ahora_bogota
    # Se compara en DÍAS de Bogotá, no en UTC: una ruta entregada a las 8 p.m.
    # tiene 0 días de rezago, no 1. Regla 5 — lo que alguien LEE como día no
    # sale de utcnow.
    hoy = ahora_bogota().date()
    sin_liquidar, dias = [], []
    for ruta in (RutaDespacho.query
                 .filter(RutaDespacho.estado == 'ENTREGADA')
                 .filter(RutaDespacho.estado_financiero != _EFR.LIQUIDADA)
                 .all()):
        # `fecha_entregada` es DateTime y `fecha_programada` es Date — se
        # normaliza a fecha antes de restar, o revienta al mezclarlas.
        _fe = ruta.fecha_entregada
        ref = _fe.date() if _fe else ruta.fecha_programada
        d = (hoy - ref).days if ref else None
        if d is not None:
            dias.append(d)
        sin_liquidar.append({'ruta_id': ruta.id, 'dias': d,
                             'estado_financiero': ruta.estado_financiero})

    # ── 3. Alertas de condición ausente ──────────────────────────────────
    # `count()` y no traer las filas: si algún día son miles, este endpoint no
    # puede volverse el problema que vino a medir.
    alertas_cond = (SiesaJob.query
                    .filter(SiesaJob.tipo == 'ALERTA_EMAIL')
                    .filter(SiesaJob.payload.like('%data incompleta%'))
                    .count())

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
            'dias_max': max(dias) if dias else None,
            'dias_promedio': round(sum(dias) / len(dias), 1) if dias else None,
            'detalle': sorted(sin_liquidar,
                              key=lambda x: (x['dias'] is None, -(x['dias'] or 0)))[:50],
            'nota': ('Piso, no estimación: hoy liquidar no tiene consecuencia '
                     'fiscal. Si la factura pasa a emitirse acá, la tendría.'),
        },
        'condicion_pago_ausente': {
            'alertas': alertas_cond,
            'facturas_emitidas_por_el_gateway': _emitidas,
            'concluyente': _emitidas >= 50,
            'nota': ('Veces que se facturó como CONTADO porque el pedido no traía '
                     'f430_id_cond_pago. Contar facturas NO lo detecta: el '
                     'fallback rellena el campo antes de emitir.'),
        },
    }), 200


@rutas_bp.route('/liquidacion/dashboard', methods=['GET'])
@jwt_required()
def liquidacion_dashboard():
    """Dashboard de liquidación: rutas del día agrupadas por estado financiero."""
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede ver el dashboard de liquidación'}), 403

    from datetime import date as _date
    from sqlalchemy import func, or_
    from sqlalchemy.orm import selectinload, joinedload
    from app.models.bulto import Bulto
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import SiesaJob
    from app.services.motivos_rechazo import SIN_RETORNO as _MR_SIN_RETORNO

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
             .filter(RutaDespacho.fecha_programada >= fecha_desde)
             .filter(RutaDespacho.fecha_programada <= fecha_hasta)
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

        # Contadores Siesa
        siesa_nc = sum(1 for r in recaudos if r.siesa_nc_triggered)
        siesa_rc = sum(1 for r in recaudos if r.siesa_rc_triggered)
        siesa_dc = sum(1 for r in recaudos if r.siesa_dc_triggered)

        # Jobs fallidos vinculados a recaudos de esta ruta
        recaudo_ids = [r.id for r in recaudos]
        jobs_fallidos = 0
        if recaudo_ids:
            jobs_fallidos = (SiesaJob.query
                             .filter(
                                 SiesaJob.referencia_tipo == 'RecaudoEntrega',
                                 SiesaJob.referencia_id.in_(recaudo_ids),
                                 SiesaJob.tipo.in_([
                                     'NOTA_CREDITO_FACTURA', 'RECIBO_CAJA',
                                     'DOCUMENTO_CONTABLE_RET',
                                 ]),
                                 SiesaJob.estado == 'FALLIDO',
                             )
                             .count())

        # Montos por forma de pago
        ruta_recaudado = 0
        for r in recaudos:
            monto = float(r.monto_cobrado or 0)
            ruta_recaudado += monto
            fp = (r.forma_pago or '').upper()
            if fp == 'EFECTIVO':
                total_efectivo += monto
            elif fp == 'TRANSFERENCIA':
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
        rd['siesa_nc_enviados'] = siesa_nc
        rd['siesa_rc_enviados'] = siesa_rc
        rd['siesa_dc_enviados'] = siesa_dc
        rd['jobs_fallidos'] = jobs_fallidos
        rutas_out.append(rd)

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
    """
    if not _es_admin_o_jefe():
        return jsonify({'error': 'Solo admin o jefe de almacén puede liquidar rutas'}), 403
    uid = _uid()
    if not uid:
        return jsonify({'error': 'Token inválido'}), 401

    from app.extensions import db
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.siesa_job import SiesaJob
    from app.services.motivos_rechazo import SIN_RETORNO as _MR_SIN_RETORNO
    from app.services.liquidacion_service import (
        LiquidacionService, RETENCION_PUC, RETENCION_TASA, _obtener_tercero,
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
            items = list(recaudo.items_entregados)  # copy from JSON
            for cv in cantidades_verificadas:
                codigo = cv.get('codigo', '')
                cant_devuelta = int(cv.get('cantidad_devuelta', 0))
                for it in items:
                    if it.get('codigo') == codigo:
                        it['cantidad_devuelta'] = cant_devuelta
                        pedido = int(it.get('cantidad_pedida', 0))
                        it['cantidad_entregada'] = max(0, pedido - cant_devuelta)
                        break
            recaudo.items_entregados = items

        # (b) Retenciones
        retenciones = rp.get('retenciones', [])
        if retenciones:
            tarea = recaudo.tarea
            tipos_ret = []
            monto_total_ret = 0

            # Obtener base gravable desde Siesa si es posible, fallback a monto_cobrado
            base_gravable = float(recaudo.monto_cobrado or 0)
            # La FE, no el pedido — ver `app/services/fe_resolver.py`.
            from app.services.fe_resolver import resolver_fe_o_none
            _tipo_fe, _consec_fe = resolver_fe_o_none(tarea) if tarea else (None, None)
            if _tipo_fe and _consec_fe:
                try:
                    from app.services.connekta_gateway import connekta
                    lineas_raw = connekta.get_rowids_factura(_tipo_fe, _consec_fe)
                    if lineas_raw:
                        base_gravable = sum(float(ln.get('f470_vlr_bruto', 0)) for ln in lineas_raw)
                except Exception as e:
                    logger.warning(
                        '[LIQUIDAR-COMPLETO] No se pudo obtener base gravable Siesa para '
                        'recaudo %d: %s — usando monto_cobrado como base',
                        recaudo.id, e,
                    )

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

            for ret in retenciones:
                tipo_ret = ret.get('tipo', '')
                if tipo_ret not in RETENCION_PUC:
                    errores.append(f'Tipo de retención desconocido: {tipo_ret}')
                    continue

                tasa = RETENCION_TASA.get(tipo_ret, 0)
                # Para RETEIVA se aplica sobre el IVA, no sobre la base
                if tipo_ret == 'RETEIVA':
                    monto_ret = round(base_gravable * 0.19 * tasa, 2)
                else:
                    monto_ret = round(base_gravable * tasa, 2)

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
                recaudo.siesa_dc_triggered = True  # prevent duplicate enqueue in liquidar_ruta_siesa

    db.session.commit()

    # Step 3: Set estado_financiero = LIQUIDADA
    try:
        resultado_liquidar = RutaService.liquidar_ruta(id)
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
