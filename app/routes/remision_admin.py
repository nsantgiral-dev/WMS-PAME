"""
Remisión desde el módulo administrador.

Responsabilidad única: control de acceso admin/gestión + respuesta HTTP.
Generación del documento delegada 100% a RemisionService (sin duplicar lógica).
Separado de packing.py para no mezclar rol de empacador con rol de gestión.
"""
import logging
from flask import Blueprint, Response, jsonify
from flask_jwt_extended import jwt_required

from app.models.packing import TareaPacking, EstadoPacking
from app.routes._auth_helpers import _es_gestion
from app.services.remision_service import RemisionService

remision_admin_bp = Blueprint('remision_admin', __name__)
logger = logging.getLogger(__name__)


@remision_admin_bp.route('/<int:packing_id>', methods=['GET'])
@jwt_required()
def imprimir_remision_admin(packing_id: int):
    """
    GET /api/admin/remision/<packing_id>

    Devuelve el HTML de remisión para cualquier tarea DESPACHADO + siesa_triggered.
    Accesible a admin, supervisor, jefe_almacen y gerente.
    No exige bultos: cubre pedidos reconciliados automáticamente (sin caja física).
    """
    u = _es_gestion()
    if not u:
        return jsonify({'error': 'Sin permiso — se requiere rol de gestión'}), 403

    tarea = TareaPacking.query.get_or_404(packing_id)

    if tarea.estado == EstadoPacking.CANCELADO:
        return jsonify({'error': 'No se puede imprimir remisión de una tarea cancelada'}), 409

    if not tarea.siesa_triggered:
        return jsonify({'error': 'La remisión solo está disponible una vez Siesa confirma el despacho'}), 409

    html = RemisionService.generar_html(tarea)
    logger.info(
        '[REMISION_ADMIN] usuario=%s imprimió remisión packing_id=%s pedido=%s',
        u.email, packing_id, tarea.numero_pedido_siesa
    )
    return Response(html, mimetype='text/html; charset=utf-8'), 200
