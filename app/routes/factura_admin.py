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
from app.routes._auth_helpers import _es_gestion
from app.services.factura_fe_service import FacturaFEService

factura_admin_bp = Blueprint('factura_admin', __name__)
logger = logging.getLogger(__name__)


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
