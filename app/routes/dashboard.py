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
        from datetime import datetime, timedelta
        from app.models.conteo import SesionConteo
        from app.models.picking import TareaPicking
        from app.models.traslado import SolicitudTraslado
        from app.models.ruta_despacho import RutaDespacho
        from app.services.traslado_monitor_service import get_resumen_alertas
        from app.services.dashboard_service import _tendencia_7d

        kpis         = DashboardService.kpis_operativos(almacen_id)
        productividad = DashboardService.productividad_operarios(almacen_id, dias=7)
        alertas      = DashboardService.alertas_stock(almacen_id)
        movimientos  = DashboardService.movimientos_recientes(almacen_id, limite=10)

        auditorias_urgentes = (SesionConteo.query
            .filter_by(tipo='EXCEPCION_PICKING', almacen_id=almacen_id)
            .filter(SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO', 'DESCUADRE']))
            .count())

        tareas_bloqueadas = TareaPicking.query.filter_by(estado='BLOQUEADO').count()
        traslados_riesgo  = get_resumen_alertas()

        # ── Traslados KPIs ──────────────────────────────────────────
        hoy = datetime.utcnow().date()
        inicio_hoy = datetime.combine(hoy, datetime.min.time())
        traslados_kpis = {
            'en_picking':     SolicitudTraslado.query.filter_by(estado='EN_PICKING').count(),
            'preparado':      SolicitudTraslado.query.filter_by(estado='PREPARADO').count(),
            'en_transito':    SolicitudTraslado.query.filter_by(estado='EN_TRANSITO').count(),
            'entregadas_hoy': SolicitudTraslado.query.filter(
                SolicitudTraslado.estado == 'ENTREGADA',
                SolicitudTraslado.fecha_entrega >= inicio_hoy
            ).count(),
        }
        traslados_kpis['total_activos'] = (
            traslados_kpis['en_picking'] +
            traslados_kpis['preparado'] +
            traslados_kpis['en_transito']
        )

        # ── Rutas KPIs ──────────────────────────────────────────────
        rutas_kpis = {
            'en_cargue':      RutaDespacho.query.filter_by(estado='EN_CARGUE').count(),
            'en_transito':    RutaDespacho.query.filter_by(estado='EN_TRANSITO').count(),
            'entregadas_hoy': RutaDespacho.query.filter(
                RutaDespacho.estado == 'ENTREGADA',
                RutaDespacho.fecha_entregada >= inicio_hoy
            ).count(),
        }

        tendencia_7d = _tendencia_7d()

        return jsonify({
            'kpis': kpis,
            'productividad': productividad,
            'alertas_stock': alertas,
            'movimientos_recientes': movimientos,
            'auditorias_urgentes': auditorias_urgentes,
            'tareas_bloqueadas': tareas_bloqueadas,
            'traslados_en_riesgo': traslados_riesgo,
            'traslados': traslados_kpis,
            'rutas': rutas_kpis,
            'tendencia_7d': tendencia_7d,
        }), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500