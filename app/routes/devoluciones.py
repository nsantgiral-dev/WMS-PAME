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
    try:
        devolucion = DevolucionClienteService.confirmar_entrada_fisica(devolucion_id, usuario.id)
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
