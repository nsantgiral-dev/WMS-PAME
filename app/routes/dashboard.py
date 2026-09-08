import logging
from datetime import date as _date

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required
from app.routes._auth_helpers import _es_admin_o_jefe, _es_gestion
from app.services.dashboard_service import DashboardService
from app.utils.fecha import dia_operativo

dashboard_bp = Blueprint('dashboard', __name__)
logger = logging.getLogger(__name__)


def _rango_fechas_desde_query():
    """`fecha_desde`/`fecha_hasta` (ISO, opcionales) → objetos `date`, default
    a "solo hoy" (día operativo Bogotá) si no vienen — mismo criterio que ya
    usa `RutaService.listar_rutas`. El parseo vive en la ruta a propósito:
    las funciones de `app/services/metricas/` son puras y no conocen `request`.
    """
    hoy = dia_operativo()
    fecha_desde_str = request.args.get('fecha_desde')
    fecha_hasta_str = request.args.get('fecha_hasta')
    fecha_desde = _date.fromisoformat(fecha_desde_str) if fecha_desde_str else hoy
    fecha_hasta = _date.fromisoformat(fecha_hasta_str) if fecha_hasta_str else fecha_desde
    return fecha_desde, fecha_hasta


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
        logger.exception(f'[DASHBOARD] kpis_operativos almacen={almacen_id}')
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
        logger.exception(f'[DASHBOARD] productividad almacen={almacen_id}')
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
        logger.exception(f'[DASHBOARD] movimientos_recientes almacen={almacen_id}')
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
        logger.exception(f'[DASHBOARD] alertas_stock almacen={almacen_id}')
        return jsonify({'error': str(e)}), 500


# ───────────────────────── Tablero BI ─────────────────────────
# fecha_desde/fecha_hasta (ISO, opcionales, default "hoy") vía
# _rango_fechas_desde_query(). Sin caché ni comparativo de período anterior
# en esta entrega — decisión explícita (ver plan de implementación).

@dashboard_bp.route('/bi/pedidos-despachados', methods=['GET'])
@jwt_required()
def bi_pedidos_despachados():
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    fecha_desde, fecha_hasta = _rango_fechas_desde_query()
    try:
        from app.services.metricas.pedidos_despachados import calcular_pedidos_despachados
        resultado = calcular_pedidos_despachados(almacen_id, fecha_desde, fecha_hasta)
        return jsonify(resultado), 200
    except Exception as e:
        logger.exception(f'[DASHBOARD] bi_pedidos_despachados almacen={almacen_id}')
        return jsonify({'error': str(e)}), 500


@dashboard_bp.route('/bi/pedidos-despachados/detalle', methods=['GET'])
@jwt_required()
def bi_pedidos_despachados_detalle():
    if not _es_gestion():
        return jsonify({'error': 'Sin permiso'}), 403
    almacen_id = request.args.get('almacen_id', type=int)
    if not almacen_id:
        return jsonify({'error': 'almacen_id es requerido'}), 400
    fecha_desde, fecha_hasta = _rango_fechas_desde_query()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    try:
        from app.services.metricas.pedidos_despachados import listar_pedidos_despachados_detalle
        pagina = listar_pedidos_despachados_detalle(almacen_id, fecha_desde, fecha_hasta, page, per_page)
        return jsonify({
            'items': [t.to_dict() for t in pagina.items],
            'total': pagina.total,
            'page': pagina.page,
            'per_page': pagina.per_page,
        }), 200
    except Exception as e:
        logger.exception(f'[DASHBOARD] bi_pedidos_despachados_detalle almacen={almacen_id}')
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
    from app.models.conteo import SesionConteo
    from app.services.traslado_monitor_service import get_resumen_alertas
    from app.services.dashboard_service import _tendencia_7d
    import logging as _log
    _logger = _log.getLogger(__name__)

    def _safe(fn, *args, **kwargs):
        """Llama fn y retorna None si falla — evita que un módulo deje el dashboard en blanco."""
        try:
            return fn(*args, **kwargs)
        except Exception as _e:
            _logger.error(f'[DASHBOARD] {fn.__name__} falló: {_e}', exc_info=True)
            return None

    kpis            = _safe(DashboardService.kpis_operativos, almacen_id)
    productividad   = _safe(DashboardService.productividad_operarios, almacen_id, dias=7)
    alertas         = _safe(DashboardService.alertas_stock, almacen_id)
    movimientos     = _safe(DashboardService.movimientos_recientes, almacen_id, limite=10)
    traslados_rutas = _safe(DashboardService.kpis_traslados_rutas) or {'traslados': {}, 'rutas': {}}
    tendencia_7d    = _safe(_tendencia_7d)
    traslados_riesgo = _safe(get_resumen_alertas)

    try:
        auditorias_urgentes = (SesionConteo.query
            .filter_by(tipo='EXCEPCION_PICKING', almacen_id=almacen_id)
            .filter(SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO', 'DESCUADRE']))
            .count())
    except Exception:
        logger.exception('[DASHBOARD] auditorias_urgentes query falló')
        auditorias_urgentes = None

    return jsonify({
        'kpis': kpis,
        'productividad': productividad,
        'alertas_stock': alertas,
        'movimientos_recientes': movimientos,
        'auditorias_urgentes': auditorias_urgentes,
        'traslados_en_riesgo': traslados_riesgo,
        'traslados': traslados_rutas['traslados'],
        'rutas': traslados_rutas['rutas'],
        'tendencia_7d': tendencia_7d,
    }), 200
