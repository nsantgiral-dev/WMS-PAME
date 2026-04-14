from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from app.services.dashboard_service import DashboardService

dashboard_bp = Blueprint('dashboard', __name__)


@dashboard_bp.route('/kpis', methods=['GET'])
@jwt_required()
def kpis_operativos():
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
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    try:
        from app.models.conteo import SesionConteo
        kpis         = DashboardService.kpis_operativos(almacen_id)
        productividad = DashboardService.productividad_operarios(almacen_id, dias=7)
        alertas      = DashboardService.alertas_stock(almacen_id)
        movimientos  = DashboardService.movimientos_recientes(almacen_id, limite=10)

        auditorias_urgentes = (SesionConteo.query
            .filter_by(tipo='EXCEPCION_PICKING')
            .filter(SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO', 'DESCUADRE']))
            .count())

        from app.models.picking import TareaPicking
        tareas_bloqueadas = TareaPicking.query.filter_by(estado='BLOQUEADO').count()

        return jsonify({
            'kpis': kpis,
            'productividad': productividad,
            'alertas_stock': alertas,
            'movimientos_recientes': movimientos,
            'auditorias_urgentes': auditorias_urgentes,
            'tareas_bloqueadas': tareas_bloqueadas,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500