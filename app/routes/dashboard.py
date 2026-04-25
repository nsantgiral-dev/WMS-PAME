from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_admin_o_jefe, _es_gestion
from app.services.dashboard_service import DashboardService

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/kpis', methods=['GET'])
@jwt_required()
def kpis_operativos():
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        resultado = DashboardService.kpis_operativos(almacen_id)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@dashboard_bp.route('/productividad', methods=['GET'])
@jwt_required()
def productividad():
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    dias = request.args.get('dias', 7, type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        resultado = DashboardService.productividad_operarios(almacen_id, dias)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@dashboard_bp.route('/movimientos', methods=['GET'])
@jwt_required()
def movimientos_recientes():
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    limite = request.args.get('limite', 20, type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        resultado = DashboardService.movimientos_recientes(almacen_id, limite)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@dashboard_bp.route('/alertas-stock', methods=['GET'])
@jwt_required()
def alertas_stock():
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        resultado = DashboardService.alertas_stock(almacen_id)
        return jsonify(resultado), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@dashboard_bp.route('/resumen-completo', methods=['GET'])
@jwt_required()
def resumen_completo():
    """
    Endpoint único que consolida todos los KPIs.
    Ideal para la pantalla principal del dashboard.
    """
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        from app.models.conteo import SesionConteo
        from app.models.picking import TareaPicking
        from app.services.traslado_monitor_service import get_resumen_alertas
        from app.services.dashboard_service import _tendencia_7d

        kpis         = DashboardService.kpis_operativos(almacen_id)
        productividad = DashboardService.productividad_operarios(almacen_id, dias=7)
        alertas      = DashboardService.alertas_stock(almacen_id)
        movimientos  = DashboardService.movimientos_recientes(almacen_id, limite=10)
        traslados_rutas = DashboardService.kpis_traslados_rutas()

        auditorias_urgentes = (SesionConteo.query
            .filter_by(tipo='EXCEPCION_PICKING', almacen_id=almacen_id)
            .filter(SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO', 'DESCUADRE']))
            .count())

        tareas_bloqueadas = TareaPicking.query.filter_by(estado='BLOQUEADO').count()
        traslados_riesgo  = get_resumen_alertas()

        tendencia_7d = _tendencia_7d()

        return jsonify({
            'kpis': kpis,
            'productividad': productividad,
            'alertas_stock': alertas,
            'movimientos_recientes': movimientos,
            'auditorias_urgentes': auditorias_urgentes,
            'tareas_bloqueadas': tareas_bloqueadas,
            'traslados_en_riesgo': traslados_riesgo,
            'traslados': traslados_rutas['traslados'],
            'rutas': traslados_rutas['rutas'],
            'tendencia_7d': tendencia_7d,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500