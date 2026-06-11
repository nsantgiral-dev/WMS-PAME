"""
Factura electrónica desde el módulo administrador.
Responsabilidad única: control de acceso gestión + entrega del HTML de FE.
Generación delegada 100% a FacturaFEService.
Aislado de remision_admin.py para no mezclar roles ni flujos.
"""
import logging
from flask import Blueprint, Response, jsonify
from flask_jwt_extended import jwt_required

from app.models.packing import TareaPacking, EstadoPacking
from app.routes._auth_helpers import _es_gestion, _solo_admin
from app.services.factura_fe_service import FacturaFEService

factura_admin_bp = Blueprint('factura_admin', __name__)
logger = logging.getLogger(__name__)


@factura_admin_bp.route('/debug-tablas', methods=['GET'])
@jwt_required()
def debug_tablas():
    """GET /api/admin/factura/debug-tablas — lista tablas accesibles en Siesa (solo admin)."""
    u = _solo_admin()
    if not u:
        return jsonify({'error': 'Sin permiso'}), 403
    try:
        from app.services.connekta_gateway import connekta
        res = connekta._get(
            'papeleriamedellin_pame_descubrir_tablas',
            url=connekta.url_get_dinamico,
        )
        return jsonify(res), 200
    except Exception as e:
        logger.exception('[DEBUG] Error en debug_tablas')
        return jsonify({'error': str(e)}), 500


@factura_admin_bp.route('/<int:packing_id>/debug-campos', methods=['GET'])
@jwt_required()
def debug_campos_fe(packing_id: int):
    """
    GET /api/admin/factura/<packing_id>/debug-campos — solo admin.
    Devuelve la respuesta cruda de Connekta para descubrir los nombres reales
    de los campos de API_v2_Ventas_Facturas_DesdePedido.
    Usar en QA para validar field mapping antes de desplegar a producción.
    """
    u = _solo_admin()
    if not u:
        return jsonify({'error': 'Sin permiso'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)
    try:
        from app.services.connekta_gateway import connekta
        lineas = connekta.get_detalle_factura(
            tipo_docto_rm=tarea.rm_tipo or '',
            consec_rm=tarea.rm_consec or 0,
            consec_pedido=tarea.consec_docto_pedido_siesa,
        )
        campos = list(lineas[0].keys()) if lineas else []
        return jsonify({
            'packing_id': packing_id,
            'rm': f'{tarea.rm_tipo}-{tarea.rm_consec}',
            'consec_pedido': tarea.consec_docto_pedido_siesa,
            'total_lineas': len(lineas),
            'campos_disponibles': campos,
            'primera_fila': lineas[0] if lineas else None,
        }), 200
    except Exception as e:
        logger.exception('[DEBUG] Error en debug_campos_fe packing_id=%s', packing_id)
        return jsonify({'error': str(e)}), 500


@factura_admin_bp.route('/<int:packing_id>', methods=['GET'])
@jwt_required()
def imprimir_factura_admin(packing_id: int):
    """
    GET /api/admin/factura/<packing_id>

    Devuelve el HTML de factura electrónica con datos reales de Siesa.
    Requiere rol de gestión (admin, supervisor, jefe_almacen, gerente).
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede imprimir factura de una tarea cancelada'}), 409

    lineas_fe = FacturaFEService.obtener_lineas(tarea)
    html = FacturaFEService.generar_html(tarea, lineas_fe)

    logger.info(
        '[FACTURA_ADMIN] usuario=%s imprimió factura packing_id=%s pedido=%s fe_lineas=%d',
        u.email, packing_id, tarea.numero_pedido_siesa, len(lineas_fe)
    )
    return Response(html, mimetype='text/html; charset=utf-8'), 200
