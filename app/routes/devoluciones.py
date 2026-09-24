"""
Rutas de Devolución de Cliente (Recepción).

Reemplaza el flujo reactivo de reconciliación (TareaDevolucion, DEPRECATED —
ver app/models/devolucion.py). El recepcionista busca el pedido/factura
original, cuenta lo devuelto (total o parcial, con o sin avería) y confirma:
eso ingresa el stock físico y dispara automáticamente la Nota Crédito (142946).

GET  /api/devoluciones/pedido/<numero_pedido_siesa>  → previsualiza líneas facturadas
POST /api/devoluciones/                              → crea la devolución (ABIERTA)
POST /api/devoluciones/<id>/confirmar                → entrada física + NC automática
POST /api/devoluciones/<id>/cancelar                 → descarta una ABIERTA
GET  /api/devoluciones/<id>                          → detalle (para reanudar una ABIERTA)
GET  /api/devoluciones/pendientes-aprobacion-nc       → NC creadas en Siesa (Elaboración) sin aprobar
POST /api/devoluciones/<id>/marcar-nc-aprobada        → respaldo manual (motivo, bitácora FORZAR)

«Llegó el camión» (m045devol — la devolución de ruta nace EN_CAMION):
GET  /api/devoluciones/llegadas                        → cola por ruta: bultos por recibir + devoluciones sin contar
POST /api/devoluciones/llegadas/<ruta_id>/bulto        → escanear un bulto que volvió (RETORNADO)
POST /api/devoluciones/llegadas/<ruta_id>/cerrar       → lo no escaneado queda FALTANTE; EN_CAMION → ABIERTA
POST /api/devoluciones/<id>/preparar-conteo            → amarra las líneas a la factura (con red) y devuelve el detalle
GET  /api/devoluciones/tablero                         → avisos (24/48 h, NC 3 días, RC 48 h) + medición
POST /api/devoluciones/verificar-nc                    → lee f350_ind_estado de las NC y marca las aprobadas
"""
import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.services.devolucion_cliente_service import DevolucionClienteService
from app.models.devolucion_cliente import DevolucionCliente
from app.routes._auth_helpers import Roles

devoluciones_bp = Blueprint('devoluciones', __name__)
logger = logging.getLogger(__name__)


def _es_recepcion():
    """Retorna el usuario si puede gestionar devoluciones, None si no."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = db.session.get(Usuario, uid)
    return u if u and u.rol in Roles.RECEPCION_ROLES else None


@devoluciones_bp.route('/pedido/<numero_pedido_siesa>', methods=['GET'])
@jwt_required()
def buscar_pedido(numero_pedido_siesa):
    if not _es_recepcion():
        return jsonify({'error': 'Sin permiso para buscar devoluciones'}), 403
    try:
        resultado = DevolucionClienteService.buscar_pedido(numero_pedido_siesa)
        return jsonify(resultado), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@devoluciones_bp.route('/', methods=['POST'])
@jwt_required()
def crear_devolucion():
    usuario = _es_recepcion()
    if not usuario:
        return jsonify({'error': 'Sin permiso para crear devoluciones'}), 403
    data = request.get_json() or {}
    requeridos = ['tarea_packing_id', 'tipo_docto_fe', 'consec_fe', 'almacen_id', 'lineas']
    for campo in requeridos:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400
    try:
        devolucion = DevolucionClienteService.crear_devolucion(
            tarea_packing_id=data['tarea_packing_id'],
            tipo_docto_fe=data['tipo_docto_fe'],
            consec_fe=data['consec_fe'],
            almacen_id=data['almacen_id'],
            recepcionista_id=usuario.id,
            lineas=data['lineas'],
            es_total=data.get('es_total', False),
            observaciones=data.get('observaciones'),
        )
        return jsonify({
            'mensaje': 'Devolución creada — confirma para ingresar el stock y generar la Nota Crédito',
            'devolucion': devolucion.to_dict(),
        }), 201
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@devoluciones_bp.route('/<int:devolucion_id>', methods=['GET'])
@jwt_required()
def obtener_devolucion(devolucion_id):
    if not _es_recepcion():
        return jsonify({'error': 'Sin permiso para ver devoluciones'}), 403
    devolucion = DevolucionCliente.query.get_or_404(devolucion_id)
    return jsonify(devolucion.to_dict()), 200


@devoluciones_bp.route('/<int:devolucion_id>/confirmar', methods=['POST'])
@jwt_required()
def confirmar_devolucion(devolucion_id):
    usuario = _es_recepcion()
    if not usuario:
        return jsonify({'error': 'Sin permiso para confirmar devoluciones'}), 403
    data = request.get_json(silent=True) or {}
    try:
        devolucion = DevolucionClienteService.confirmar_entrada_fisica(
            devolucion_id, usuario.id, lineas_ajustadas=data.get('lineas'))
        return jsonify({
            'mensaje': 'Devolución confirmada — stock ingresado, Nota Crédito en proceso hacia Siesa',
            'devolucion': devolucion.to_dict(),
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception('[DEVOLUCIONES] Error inesperado confirmando devolucion_id=%s', devolucion_id)
        return jsonify({'error': str(e)}), 500


@devoluciones_bp.route('/<int:devolucion_id>/cancelar', methods=['POST'])
@jwt_required()
def cancelar_devolucion(devolucion_id):
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso para cancelar devoluciones'}), 403
    data = request.get_json() or {}
    try:
        devolucion = DevolucionClienteService.cancelar(devolucion_id, uid, data.get('motivo'))
        return jsonify({'mensaje': 'Devolución cancelada', 'devolucion': devolucion.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@devoluciones_bp.route('/pendientes-de-ruta', methods=['GET'])
@jwt_required()
def pendientes_de_ruta():
    """
    Devoluciones que Liquidación de ruta armó automáticamente (entrega
    Parcial/Rechazada) y siguen esperando que recepción las confirme
    físicamente — ya vienen con las líneas exactas, por pedido.
    """
    if not _es_recepcion():
        return jsonify({'error': 'Sin permiso para ver devoluciones'}), 403
    pendientes = DevolucionClienteService.listar_pendientes_de_ruta()
    return jsonify({'pendientes': pendientes, 'total': len(pendientes)}), 200


@devoluciones_bp.route('/pendientes-aprobacion-nc', methods=['GET'])
@jwt_required()
def pendientes_aprobacion_nc():
    """
    NC ya creadas en Siesa (Elaboración — CLAUDE.md Regla #21) que todavía
    nadie ha marcado como aprobadas+cruzadas manualmente en Siesa.
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    pendientes = DevolucionClienteService.listar_pendientes_aprobacion_nc()
    return jsonify({'pendientes': pendientes, 'total': len(pendientes)}), 200


@devoluciones_bp.route('/<int:devolucion_id>/marcar-nc-aprobada', methods=['POST'])
@jwt_required()
def marcar_nc_aprobada(devolucion_id):
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    data = request.get_json(silent=True) or {}
    try:
        devolucion = DevolucionClienteService.marcar_nc_aprobada(
            devolucion_id, uid, motivo=data.get('motivo'))
        return jsonify({'mensaje': 'NC marcada como aprobada', 'devolucion': devolucion.to_dict()}), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


# ── «Llegó el camión» (m045devol) ────────────────────────────────────────────

@devoluciones_bp.route('/llegadas', methods=['GET'])
@jwt_required()
def cola_llegadas():
    """Rutas con bultos que el conductor declaró de vuelta sin recibir, o con
    devoluciones sin contar. Se vacía sola."""
    if not _es_recepcion():
        return jsonify({'error': 'Sin permiso para ver devoluciones'}), 403
    from app.services import devolucion_ruta as _dr
    rutas = _dr.cola_llegadas()
    return jsonify({'rutas': rutas, 'total': len(rutas)}), 200


@devoluciones_bp.route('/llegadas/<int:ruta_id>/bulto', methods=['POST'])
@jwt_required()
def escanear_bulto_de_vuelta(ruta_id):
    usuario = _es_recepcion()
    if not usuario:
        return jsonify({'error': 'Sin permiso para recibir devoluciones'}), 403
    from app.services import devolucion_ruta as _dr
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_dr.escanear_bulto_de_vuelta(
            ruta_id, data.get('codigo_barras'), usuario.id)), 200
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@devoluciones_bp.route('/llegadas/<int:ruta_id>/cerrar', methods=['POST'])
@jwt_required()
def cerrar_llegada(ruta_id):
    usuario = _es_recepcion()
    if not usuario:
        return jsonify({'error': 'Sin permiso para recibir devoluciones'}), 403
    from app.services import devolucion_ruta as _dr
    try:
        return jsonify(_dr.cerrar_llegada(ruta_id, usuario.id)), 200
    except LookupError as e:
        return jsonify({'error': str(e)}), 404
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@devoluciones_bp.route('/<int:devolucion_id>/preparar-conteo', methods=['POST'])
@jwt_required()
def preparar_conteo(devolucion_id):
    """Amarra las líneas a la factura (con red) antes de contar. Si Siesa no
    responde, devuelve el detalle igual y lo dice (`vinculacion.error`): el
    conteo lo vuelve a intentar al confirmar."""
    if not _es_recepcion():
        return jsonify({'error': 'Sin permiso para ver devoluciones'}), 403
    from app.services import devolucion_ruta as _dr
    from app.services.devolucion_cliente_service import connekta as _cx
    devolucion = DevolucionCliente.query.get_or_404(devolucion_id)
    vinculacion = {}
    if devolucion.es_de_ruta and devolucion.estado in ('EN_CAMION', 'ABIERTA'):
        try:
            vinculacion = _dr.vincular_a_factura(devolucion, gateway=_cx)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            vinculacion = {'ok': False, 'error': f'Siesa no respondió: {e}'[:300]}
            devolucion = DevolucionCliente.query.get_or_404(devolucion_id)
    return jsonify({**devolucion.to_dict(), 'vinculacion': vinculacion}), 200


@devoluciones_bp.route('/tablero', methods=['GET'])
@jwt_required()
def tablero():
    """Lo que lleva demasiado esperando y cuánto tarda la mercancía en volver a
    ser inventario. Mismas funciones que el resumen diario por correo."""
    if not _es_recepcion():
        return jsonify({'error': 'Sin permiso para ver devoluciones'}), 403
    from app.services import devolucion_nc_verificador as _ver
    from app.services import devolucion_ruta as _dr
    return jsonify({'avisos': _dr.avisos(), 'medicion': _dr.medicion(),
                    'verificacion_nc': _ver.estado()}), 200


@devoluciones_bp.route('/verificar-nc', methods=['POST'])
@jwt_required()
def verificar_nc():
    """Lee `f350_ind_estado` de las NC pendientes y marca las aprobadas (con
    lo que la consulta ya registrada trae). Supervisión: escribe aprobaciones
    y libera inventario a picking."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = db.session.get(Usuario, uid)
    if not u or u.rol not in Roles.SUPERVISION:
        return jsonify({'error': 'Sin permiso'}), 403
    from app.services import devolucion_nc_verificador as _ver
    try:
        return jsonify(_ver.verificar()), 200
    except Exception as e:
        db.session.rollback()
        logger.exception('[DEVOLUCIONES] verificar-nc falló')
        return jsonify({'error': f'No se pudo leer Siesa: {e}'[:300]}), 502
