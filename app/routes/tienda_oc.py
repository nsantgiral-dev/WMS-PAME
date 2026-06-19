"""
Rutas de Recepción OC para Puntos de Venta.

SOLID: blueprint propio, auth propia (rol=='tienda'), sin tocar recepcion.py ni siesa.py.
Delega a TiendaOCService (lógica tienda) y RecepcionService (escaneo/confirmación).
"""
import logging
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.services.tienda_oc_service import TiendaOCService
from app.services.recepcion_service import RecepcionService

tienda_oc_bp = Blueprint('tienda_oc', __name__)
logger = logging.getLogger(__name__)


_BODEGA_CO_MAP = {
    'NC1': '002', 'NS1': '001', 'PC1': '004', 'FC1': '006',
    'FF1': '009', 'FN1': '007', 'NB1': '003', 'PT1': '005',
    'NS2': '001', 'FP1': '008',
}


def _get_tienda_user():
    """Retorna el usuario si tiene rol tienda con bodega configurada, None si no."""
    from app.models.usuario import Usuario
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    if not u or u.rol != 'tienda':
        return None
    if not u.bodega_siesa_id:
        return None
    co_correcto = _BODEGA_CO_MAP.get(u.bodega_siesa_id)
    if co_correcto and u.siesa_co_id != co_correcto:
        u.siesa_co_id = co_correcto
        from app.extensions import db
        db.session.commit()
        logger.info(f'[TIENDA-OC] Corregido siesa_co_id={co_correcto} para usuario {u.id} ({u.bodega_siesa_id})')
    if not u.siesa_co_id:
        return None
    return u


def _validar_recepcion_tienda(recepcion_id, usuario):
    """Valida que la recepción existe y pertenece a la bodega de la tienda."""
    from app.models.recepcion import RecepcionMercancia
    recepcion = RecepcionMercancia.query.get(recepcion_id)
    if not recepcion:
        return None, 'Recepción no encontrada'
    if recepcion.co_oc_siesa != usuario.siesa_co_id:
        return None, 'Esta recepción no pertenece a tu punto de venta'
    return recepcion, None


@tienda_oc_bp.route('/', methods=['GET'])
@jwt_required()
def listar_ocs():
    """Lista OCs pendientes de Siesa filtradas por la bodega/CO de la tienda."""
    u = _get_tienda_user()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol tienda con bodega configurada'}), 403

    try:
        resultado = TiendaOCService.listar_ocs(u.siesa_co_id, u.bodega_siesa_id)
        return jsonify(resultado), 200
    except Exception as e:
        logger.error(f'[TIENDA-OC] Error listando OCs para {u.bodega_siesa_id}: {e}')
        return jsonify({'error': 'Error consultando OCs en Siesa'}), 500


@tienda_oc_bp.route('/iniciar', methods=['POST'])
@jwt_required()
def iniciar_recepcion():
    """Crea e inicia una recepción OC para la tienda."""
    u = _get_tienda_user()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol tienda con bodega configurada'}), 403

    data = request.get_json()
    for campo in ['numero_oc', 'tipo_docto', 'consec_docto', 'items']:
        if campo not in data:
            return jsonify({'error': f'Campo requerido: {campo}'}), 400

    try:
        recepcion = TiendaOCService.iniciar_recepcion(data, u)
        es_existente = recepcion.fecha_inicio is not None and recepcion.estado == 'EN_PROCESO'
        return jsonify({
            'mensaje': 'Recepción en proceso — listo para escanear' if es_existente
                       else 'Recepción iniciada — listo para escanear',
            'recepcion': recepcion.to_dict()
        }), 200 if es_existente else 201
    except ValueError as e:
        msg = str(e)
        if 'ya fue recepcionada' in msg:
            return jsonify({'error': msg}), 409
        if 'Tipo de Proveedor' in msg or 'Pasivo Estimado' in msg:
            return jsonify({'error': msg}), 422
        return jsonify({'error': msg}), 400


@tienda_oc_bp.route('/<int:id>/escanear', methods=['POST'])
@jwt_required()
def escanear_producto(id):
    """Escaneo de producto — delega directamente a RecepcionService."""
    u = _get_tienda_user()
    if not u:
        return jsonify({'error': 'Sin permiso'}), 403

    recepcion, err = _validar_recepcion_tienda(id, u)
    if err:
        return jsonify({'error': err}), 404 if 'no encontrada' in err else 403

    data = request.get_json()
    producto_id = data.get('producto_id')
    cantidad = data.get('cantidad', 1)

    if not producto_id:
        return jsonify({'error': 'producto_id requerido'}), 400

    try:
        resultado = RecepcionService.escanear_producto(
            recepcion_id=id,
            producto_id=producto_id,
            cantidad=cantidad,
            lote=data.get('lote'),
            fecha_vencimiento=data.get('fecha_vencimiento'),
            es_empaque=data.get('es_empaque', False),
            es_bonificacion=data.get('es_bonificacion', False),
        )
        return jsonify(resultado), 200
    except ValueError as e:
        err_data = e.args[0] if e.args else str(e)
        if isinstance(err_data, dict):
            return jsonify(err_data), 400
        return jsonify({'error': str(e)}), 400


@tienda_oc_bp.route('/<int:id>/confirmar', methods=['PUT'])
@jwt_required()
def confirmar_recepcion(id):
    """Confirma recepción, ingresa inventario y dispara ENTRADA_OC en Siesa."""
    u = _get_tienda_user()
    if not u:
        return jsonify({'error': 'Sin permiso'}), 403

    recepcion, err = _validar_recepcion_tienda(id, u)
    if err:
        return jsonify({'error': err}), 404 if 'no encontrada' in err else 403

    data = request.get_json() or {}

    try:
        recepcion = RecepcionService.confirmar_recepcion(
            recepcion_id=id,
            observaciones=data.get('observaciones'),
            num_remision_prov=data.get('num_remision_prov'),
        )
        return jsonify({
            'mensaje': 'Recepción confirmada — entrada enviada a Siesa',
            'recepcion': recepcion.to_dict()
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 400


@tienda_oc_bp.route('/<int:id>', methods=['GET'])
@jwt_required()
def obtener_recepcion(id):
    """Obtiene una recepción con sus ítems — para continuar escaneo."""
    u = _get_tienda_user()
    if not u:
        return jsonify({'error': 'Sin permiso'}), 403

    recepcion, err = _validar_recepcion_tienda(id, u)
    if err:
        return jsonify({'error': err}), 404 if 'no encontrada' in err else 403

    from sqlalchemy.orm import selectinload
    from app.models.recepcion import RecepcionMercancia, ItemRecepcion
    recepcion = (RecepcionMercancia.query
                 .options(selectinload(RecepcionMercancia.items).selectinload(ItemRecepcion.producto))
                 .get(id))

    return jsonify(recepcion.to_dict()), 200
