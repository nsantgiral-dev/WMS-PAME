"""
Rutas de empaques y LPN.

/api/empaques/scan/<codigo>         GET  — resolver un código escaneado
/api/empaques/descomponer           POST — calcular pacas + sueltas para una cantidad
/api/empaques/lpn/generar           POST — crear un LPN nuevo en recepción
/api/empaques/lpn/<codigo>          GET  — info de un LPN
/api/empaques/lpn/<codigo>/consumir POST — marcar LPN como consumido (paca abierta)
/api/empaques/sync                  POST — trigger manual del sync nocturno (solo admin)
/api/empaques/sync/estado           GET  — estado del último sync
"""
import logging
from flask import Blueprint, request, jsonify, current_app

logger = logging.getLogger(__name__)
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.extensions import db
from app.models.lpn import LPN
from app.models.usuario import Usuario
from app.services.empaques_service import (
    scan_barcode, descomponer_en_empaques, generar_lpn, consumir_lpn
)
from app.services import empaques_sync_service
from app.routes._auth_helpers import Roles, _es_personal_almacen, _es_gestion

empaques_bp = Blueprint('empaques', __name__)


@empaques_bp.route('/scan/<path:codigo_barras>', methods=['GET'])
@jwt_required()
def scan(codigo_barras):
    """
    Resolución de barcode en tiempo real.
    El frontend llama esto cada vez que el operario escanea algo en recepción/picking/packing.

    Respuesta:
      tipo: GS1_UNICO | GS1_AMBIGUO | LPN | EAN_BASE | NO_ENCONTRADO
      producto, empaque, factor, lpn (si tipo=LPN), ambiguos (si tipo=GS1_AMBIGUO)
    """
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para escanear códigos'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    resultado = scan_barcode(codigo_barras, almacen_id=almacen_id)
    return jsonify(resultado), 200


@empaques_bp.route('/descomponer', methods=['POST'])
@jwt_required()
def descomponer():
    """
    Calcula la combinación óptima de LPNs + UNDs sueltas para una tarea.
    Usado por picking, packing y traslados antes de mostrar las instrucciones al operario.

    Body: { producto_id, cantidad_solicitada, almacen_id }
    """
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso'}), 403
    data = request.get_json() or {}
    producto_id = data.get('producto_id')
    cantidad = data.get('cantidad_solicitada')
    almacen_id = data.get('almacen_id')

    if not all([producto_id, cantidad, almacen_id]):
        return jsonify({'error': 'producto_id, cantidad_solicitada y almacen_id son requeridos'}), 400

    resultado = descomponer_en_empaques(producto_id, int(cantidad), int(almacen_id))
    return jsonify(resultado), 200


@empaques_bp.route('/lpn/generar', methods=['POST'])
@jwt_required()
def crear_lpn():
    """
    Genera un nuevo LPN para una paca sin código único.
    Usado en recepción cuando el operario encuentra una paca sin DUN-14 o con código ambiguo.

    Body: {
      producto_id,
      cantidad_actual,         ← UNDs reales en esta paca (puede diferir del factor estándar)
      almacen_id,
      empaque_id (opcional),   ← si ya conocemos el template de empaque
      recepcion_id (opcional),
      notas (opcional)
    }
    """
    data = request.get_json() or {}
    producto_id = data.get('producto_id')
    cantidad = data.get('cantidad_actual')
    almacen_id = data.get('almacen_id')

    if not all([producto_id, cantidad, almacen_id]):
        return jsonify({'error': 'producto_id, cantidad_actual y almacen_id son requeridos'}), 400

    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(usuario_id)
    if not u or u.rol not in Roles.RECEPCION_ROLES:
        return jsonify({'error': 'Sin permiso para generar LPNs'}), 403

    try:
        lpn = generar_lpn(
            producto_id=int(producto_id),
            cantidad_actual=int(cantidad),
            almacen_id=int(almacen_id),
            empaque_id=data.get('empaque_id'),
            recepcion_id=data.get('recepcion_id'),
            creado_por_id=usuario_id,
            notas=data.get('notas'),
        )
        db.session.commit()
        return jsonify({'lpn': lpn.to_dict(), 'mensaje': f'LPN {lpn.codigo} generado'}), 201
    except Exception as e:
        db.session.rollback()
        logger.exception('Error generando LPN')
        return jsonify({'error': str(e)}), 500


@empaques_bp.route('/lpn/<string:codigo>', methods=['GET'])
@jwt_required()
def get_lpn(codigo):
    """Info de un LPN específico."""
    if not _es_personal_almacen():
        return jsonify({'error': 'Sin permiso para consultar LPNs'}), 403
    lpn = LPN.query.filter_by(codigo=codigo.upper()).first()
    if not lpn:
        return jsonify({'error': f'LPN {codigo} no encontrado'}), 404
    return jsonify(lpn.to_dict()), 200


@empaques_bp.route('/lpn/<string:codigo>/consumir', methods=['POST'])
@jwt_required()
def consumir(codigo):
    """
    Marca el LPN como CONSUMIDO — la paca fue abierta.
    Las unidades liberadas deben ser ingresadas al stock suelto por el caller (frontend o flujo).

    Retorna las unidades liberadas para que el frontend actualice el inventario.
    """
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    u = Usuario.query.get(uid)
    if not u or u.rol not in Roles.RECEPCION_ROLES:
        return jsonify({'error': 'Solo recepcionistas pueden consumir LPNs'}), 403
    try:
        lpn, unidades = consumir_lpn(codigo.upper())
        db.session.commit()
        return jsonify({
            'lpn': lpn.to_dict(),
            'unidades_liberadas': unidades,
            'mensaje': f'LPN {codigo} consumido — {unidades} UNDs liberadas al inventario',
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        db.session.rollback()
        logger.exception('Error consumiendo LPN')
        return jsonify({'error': str(e)}), 500


@empaques_bp.route('/lpn/producto/<int:producto_id>', methods=['GET'])
@jwt_required()
def lpns_por_producto(producto_id):
    """Lista LPNs activos de un producto en un almacén. Usado por picking para saber qué hay."""
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para consultar LPNs'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400

    lpns = LPN.buscar_activos_por_producto(producto_id, almacen_id)
    return jsonify({'lpns': [l.to_dict() for l in lpns], 'total': len(lpns)}), 200


@empaques_bp.route('/sync', methods=['POST'])
@jwt_required()
def trigger_sync():
    """Trigger manual del sync de empaques (solo admin)."""
    try:
        usuario_id = int(get_jwt_identity())
    except (TypeError, ValueError):
        return jsonify({'error': 'Token inválido'}), 401
    usuario = Usuario.query.get(usuario_id)
    if not usuario or usuario.rol not in Roles.LEAD:
        return jsonify({'error': 'Solo admin puede disparar el sync'}), 403

    empaques_sync_service.ejecutar_sync(current_app._get_current_object())
    return jsonify({'mensaje': 'Sync de empaques iniciado en background'}), 202


@empaques_bp.route('/sync/estado', methods=['GET'])
@jwt_required()
def sync_estado():
    """Estado del último sync de empaques."""
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso para consultar estado del sync'}), 403
    return jsonify(empaques_sync_service.get_estado()), 200
